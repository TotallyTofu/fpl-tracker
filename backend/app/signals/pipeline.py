"""Signal pipeline (PLAN-3 T2.9): one function per new raw item.

``process_item``: LLM extraction (when configured) + rule backstop → store →
mark processed (with takeaways). ``process_pending_items`` runs the whole
queue, expiring stale signals first. Official FPL news changes are handled by
``process_official_news`` (called from the bootstrap fetch).
"""
from __future__ import annotations

import logging

from ..config import Settings, load_settings
from ..db import query
from . import ingest, rule_extractor, store
from .llm_extractor import extract_signals_llm
from .names import get_name_index

log = logging.getLogger("fpl.signals.pipeline")


def _player_list() -> list[dict]:
    return query(
        """SELECT id, web_name, first_name, second_name, team_code
           FROM players WHERE removed = 0"""
    )


async def process_item(item: dict, settings: Settings | None = None) -> int:
    """Extract signals for one raw item. Returns count stored.

    LLM first (when ready), rule path always as backstop. Never raises —
    a bad item must not block the queue.
    """
    settings = settings or load_settings()
    try:
        idx = get_name_index()
        players = _player_list()
        text = f"{item.get('title') or ''}\n{item.get('body') or ''}"

        merged: list[dict] = []
        if settings.llm_ready:
            llm = await extract_signals_llm(
                text,
                item.get("source") or "unknown",
                item.get("published_at"),
                players,
                settings,
            )
            for s in llm:
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


async def process_pending_items(limit: int = 50, settings: Settings | None = None) -> int:
    """Expire stale signals, then process the pending queue. Returns signals stored."""
    settings = settings or load_settings()
    store.expire_stale()
    total = 0
    for item in ingest.pending_items(limit):
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