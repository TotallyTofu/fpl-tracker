"""Signal pipeline (PLAN-3 T2.9): one function per new raw item.

``process_item``: LLM extraction (when configured) + rule backstop → store →
mark processed (with takeaways). ``process_pending_items`` runs the whole
queue within a time budget (FIX N8), expiring stale signals first. Official
FPL news changes are handled by ``process_official_news`` (called from the
bootstrap fetch).

FIX N9: a cheap LLM circuit breaker skips the LLM for a pass when it is
failing repeatedly (rules still run). FIX N10: a hard LLM failure is
retryable — the attempt is counted and the item is requeued up to
``MAX_EXTRACT_ATTEMPTS`` times instead of being lost forever.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from ..config import Settings, load_settings
from ..db import now_utc, query
from . import ingest, rule_extractor, store
from .llm_extractor import extract_signals_llm
from .names import get_name_index

log = logging.getLogger("fpl.signals.pipeline")

# FIX N10: requeue an item this many times on a hard LLM failure before
# giving up (rule signals are saved on every attempt).
MAX_EXTRACT_ATTEMPTS = 3

_BREAKER_WINDOW_MIN = 30
_BREAKER_MIN_ERRORS = 2


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

    LLM first (when ready), rule path always as backstop. Never raises —
    a bad item must not block the queue.

    FIX N10: a hard LLM failure (``extract_signals_llm`` → None) is
    retryable — the attempt is counted (``raw_items.extract_attempts``) and
    the item stays pending (requeued) until ``MAX_EXTRACT_ATTEMPTS``; the
    rule-path signals are saved on every attempt.
    """
    settings = settings or load_settings()
    try:
        idx = get_name_index()
        players = _player_list()
        text = f"{item.get('title') or ''}\n{item.get('body') or ''}"

        llm_signals: list[dict] = []
        llm_retryable = False
        breaker_note: str | None = None
        if settings.llm_ready:
            if _llm_breaker_open():
                breaker_note = ("LLM skipped this pass: circuit breaker open "
                                f"(≥{_BREAKER_MIN_ERRORS} failures in the last "
                                f"{_BREAKER_WINDOW_MIN} min)")
                log.info("raw_items.id=%s: %s", item.get("id"), breaker_note)
            else:
                llm = await extract_signals_llm(
                    text,
                    item.get("source") or "unknown",
                    item.get("published_at"),
                    players,
                    settings,
                )
                if llm is None:
                    llm_retryable = True   # FIX N10: hard error → retryable
                else:
                    llm_signals = llm

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
        rules = rule_extractor.extract_signals_rule(item, idx)
        merged.extend(rules)

        n = store.save_signals(merged)
        takeaways = [s["summary"] for s in merged if s.get("summary")][:5]
        if breaker_note:
            takeaways = [breaker_note] + takeaways[:4]   # FIX N9: surface it
        if llm_retryable:
            attempts = ingest.bump_extract_attempts(item["id"])
            if attempts < MAX_EXTRACT_ATTEMPTS:
                # FIX N10: leave processed = 0 — the next pass retries the LLM;
                # rule takeaways are kept so the item is never empty-handed.
                if takeaways:
                    ingest.set_takeaways(item["id"], takeaways)
                log.info("raw_items.id=%s: LLM hard-failed (attempt %d/%d) — requeued",
                         item["id"], attempts, MAX_EXTRACT_ATTEMPTS)
                return n
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
        total += await process_item(item, settings)
    return total


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