"""Signal pipeline (PLAN-3 T2.9): one function per new raw item.

``process_item``: LLM extraction (when configured) + rule backstop → store →
mark processed (with takeaways). ``process_pending_items`` runs the whole
queue within a time budget (FIX N8), expiring stale signals first. Official
FPL news changes are handled by ``process_official_news`` (called from the
bootstrap fetch).

FIX N9: a cheap LLM circuit breaker skips the LLM for a pass when it is
failing repeatedly. FIX N10: a hard LLM failure is retryable — the attempt is
counted and the item is requeued up to ``MAX_EXTRACT_ATTEMPTS`` times.

v1.0: the keyword extractor is a FALLBACK, not a co-author. When the LLM reads
an item successfully only its signals are stored (keyword matches used to be
stored next to them and often contradicted them). Rules run when the LLM is
not configured, after its final failed attempt, or when it has been down for
longer than ``llm.llm_wait_hours``. While the breaker is open, items wait
(they used to be marked done with rules only, so 169 items never reached the
LLM); ``requeue_skipped`` puts those older items back in the queue.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from ..config import Settings, load_settings
from ..db import execute, now_utc, query
from . import ingest, rule_extractor, store
from .llm_extractor import extract_signals_llm
from .names import get_name_index

log = logging.getLogger("fpl.signals.pipeline")

# FIX N10: requeue an item this many times on a hard LLM failure before
# giving up (rule signals are saved on every attempt).
MAX_EXTRACT_ATTEMPTS = 3

_BREAKER_WINDOW_MIN = 30
_BREAKER_MIN_ERRORS = 2
_BREAKER_NOTE = "circuit breaker open"


def _waited_too_long(item: dict, hours: float) -> bool:
    """True when the item has waited longer than ``hours`` since retrieval."""
    ts = item.get("retrieved_at")
    if not ts:
        return True
    try:
        got = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - got > timedelta(hours=hours)


def _player_list() -> list[dict]:
    # A23.1 (rev 2): selected_by_percent rides along so _relevant_players can
    # keep the most-owned names when the 40-player prompt cap bites. Without the
    # column every sort key was 0.0 and the "top by ownership" truncation was a
    # silent no-op (the unit test passed a synthetic list that had the field).
    return query(
        """SELECT id, web_name, first_name, second_name, team_code, selected_by_percent
           FROM players WHERE removed = 0"""
    )


def _llm_breaker_open() -> bool:
    """FIX N9: circuit breaker — ≥ 2 LLM errors in the last 30 min AND more
    errors than oks → skip the LLM for this pass (rules still run). Reads the
    same poll_log rows the meta API surfaces (``finished_at``); re-closes
    automatically on the next successful call."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=_BREAKER_WINDOW_MIN)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    recent = query(
        "SELECT status FROM poll_log WHERE source = 'llm' AND finished_at >= ? "
        "ORDER BY id DESC LIMIT 10",
        (cutoff,),
    )
    errors = sum(1 for r in recent if r["status"] == "error")
    oks = sum(1 for r in recent if r["status"] == "ok")
    return errors >= _BREAKER_MIN_ERRORS and errors > oks


async def process_item(item: dict, settings: Settings | None = None) -> int:
    """Extract signals for one raw item. Returns count stored.

    LLM first (when ready); the keyword rules only when the LLM is not
    configured or could not read the item (see module doc). Never raises —
    a bad item must not block the queue. Returns -1 when the item was left
    waiting for the LLM (breaker open).
    """
    settings = settings or load_settings()
    try:
        idx = get_name_index()
        players = _player_list()
        text = f"{item.get('title') or ''}\n{item.get('body') or ''}"

        llm_signals: list[dict] = []
        llm_ok = False
        breaker_note: str | None = None
        if settings.llm_ready:
            if _llm_breaker_open():
                wait_h = float(getattr(settings.config.llm, "llm_wait_hours", 6) or 0)
                if not _waited_too_long(item, wait_h):
                    log.info("raw_items.id=%s: LLM failing (breaker open) — item waits",
                             item.get("id"))
                    return -1
                breaker_note = (f"Keyword match only: the LLM was failing for over "
                                f"{wait_h:g} h ({_BREAKER_NOTE})")
            else:
                llm = await extract_signals_llm(
                    text,
                    item.get("source") or "unknown",
                    item.get("published_at"),
                    players,
                    settings,
                )
                if llm is None:
                    attempts = ingest.bump_extract_attempts(item["id"])
                    if attempts < MAX_EXTRACT_ATTEMPTS:
                        log.info("raw_items.id=%s: LLM hard-failed (attempt %d/%d) — requeued",
                                 item["id"], attempts, MAX_EXTRACT_ATTEMPTS)
                        return 0
                    breaker_note = (f"Keyword match only: the LLM failed "
                                    f"{MAX_EXTRACT_ATTEMPTS} times on this item")
                else:
                    llm_signals = llm
                    llm_ok = True

        merged: list[dict] = []
        for s in llm_signals:
            merged.append(
                {
                    **s,
                    "source": store._source_ref(f"{item.get('source')}:{item.get('external_id')}"),
                    "url": item.get("url"),
                    "published_at": item.get("published_at"),
                    "raw_item_id": item.get("id"),
                    "model": f"llm:{settings.llm_model}",
                }
            )
        if not llm_ok:
            merged.extend(rule_extractor.extract_signals_rule(item, idx))

        n = store.save_signals(merged)
        takeaways = [s["summary"] for s in merged if s.get("summary")][:5]
        if breaker_note:
            takeaways = [breaker_note] + takeaways[:4]   # FIX N9: surface it
        ingest.mark_processed(item["id"], takeaways or None)
        return n
    except Exception:
        log.exception("process_item failed for raw_items.id=%s", item.get("id"))
        # still dequeue so one bad item cannot wedge the queue
        try:
            ingest.mark_processed(item["id"], None)
        except Exception:
            log.exception("mark_processed failed for raw_items.id=%s", item.get("id"))
        return 0


async def process_pending_items(limit: int = 50, timebox_sec: int | None = None,
                                settings: Settings | None = None) -> int:
    """Expire stale signals, then process the pending queue. Returns signals stored.

    FIX N8: the pass is time-budgeted (``llm.extract_timebox_sec``, default
    480 s) — the check runs between items; when the budget is exhausted the
    remaining items stay pending for the next pass (the scheduler job
    coalesces, so a pass that overruns must not run forever).
    """
    settings = settings or load_settings()
    if timebox_sec is None:
        timebox_sec = getattr(settings.config.llm, "extract_timebox_sec", None)
    store.expire_stale()
    total = 0
    deadline = time.monotonic() + timebox_sec if timebox_sec else None
    for item in ingest.pending_items(limit):
        if deadline is not None and time.monotonic() > deadline:
            log.info("extraction pass hit its %ss timebox — %s signal(s) stored, "
                     "rest of the queue stays pending", timebox_sec, total)
            break
        n = await process_item(item, settings)
        total += max(0, n)
    return total


def requeue_skipped(days: int = 7) -> dict:
    """Put items that were marked done WITHOUT the LLM (the old circuit-breaker
    behaviour) back in the queue, for items retrieved in the last ``days``.
    Their keyword-only signals are removed first so the LLM's reading
    replaces them instead of sitting next to them."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = query(
        "SELECT id FROM raw_items WHERE processed = 1 AND takeaways LIKE ? AND retrieved_at >= ?",
        (f"%{_BREAKER_NOTE}%", cutoff),
    )
    ids = [r["id"] for r in rows]
    if not ids:
        return {"requeued": 0, "signals_removed": 0}
    marks = ",".join("?" * len(ids))
    from ..db import get_conn
    conn = get_conn()           # one connection, one transaction: all or nothing
    try:
        removed = conn.execute(
            f"DELETE FROM signals WHERE model = 'rules' AND raw_item_id IN ({marks})", ids).rowcount
        conn.execute(f"UPDATE raw_items SET processed = 0, extract_attempts = 0, takeaways = NULL "
                     f"WHERE id IN ({marks})", ids)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"requeued": len(ids), "signals_removed": removed}


def process_official_news(players: list[dict]) -> int:
    """Official FPL news/status changes → raw items (kind='official-news') + signals.

    Called from the bootstrap fetch (fetchers/fpl.py). Returns count stored.
    """
    signals = rule_extractor.ingest_official_news(players)
    if not signals:
        return 0
    by_player: dict[int, list[dict]] = {}
    for s in signals:
        by_player.setdefault(s["player_id"], []).append(s)
    count = 0
    for pid, sigs in by_player.items():
        p = next((x for x in players if x["id"] == pid), None)
        web = p.get("web_name") if p else f"player {pid}"
        news = (p.get("news") if p else None) or ""
        item_id = ingest.ingest_item(
            source="fpl-official",
            external_id=f"player-{pid}",
            kind="official-news",
            title=f"{web}: {news}" if news else f"{web}: status change",
            url=None,
            published_at=p.get("news_added") if p else None,
            body=news or None,
        )
        for s in sigs:
            s["raw_item_id"] = item_id
        count += store.save_signals(sigs)
        if item_id:
            ingest.mark_processed(item_id, [s["summary"] for s in sigs][:5])
    return count