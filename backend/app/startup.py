"""Startup full news refresh (M3 revised scope — replaces the SSE/realtime plan).

The app boots with FPL data (synchronous bootstrap in main.lifespan) so the UI
is usable immediately; then this module runs a FULL news refresh (all enabled
news sources + signal extraction) as a background task. Progress is persisted
in the `meta` table (key `startup_refresh`) so the UI can poll
GET /api/meta/startup-refresh and notify the user when the refresh finishes.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from .config import ConfigFile, load_settings, save_config
from .db import get_meta, set_meta

log = logging.getLogger("fpl.startup")

META_KEY = "startup_refresh"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_startup_state() -> dict:
    """Current startup-refresh state. `idle` = no run recorded yet this boot."""
    raw = get_meta(META_KEY)
    if not raw:
        return {"status": "idle"}
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {"status": "idle"}


def _save_state(state: dict) -> None:
    set_meta(META_KEY, json.dumps(state))


async def refresh_one(target: str, cfg: ConfigFile) -> dict | str:
    """Run one news-source fetcher. Returns its result dict or a status string.

    Shared by the on-demand POST /api/refresh/{source} endpoint and the
    startup background refresh, so both behave identically.
    """
    if target == "bbc":
        if not cfg.sources.bbc.enabled:
            return "skipped (disabled)"
        from .fetchers.bbc import refresh_bbc

        return await refresh_bbc()
    if target == "espn":
        if not cfg.sources.espn.enabled:
            return "skipped (espn disabled in settings)"
        from .fetchers.espn import refresh_espn

        return await refresh_espn()
    if target == "reddit":
        if not cfg.sources.reddit.enabled:
            return "skipped (disabled)"
        from .fetchers.reddit import refresh_reddit

        return await refresh_reddit(cfg)
    if target == "youtube":
        if not cfg.sources.youtube.enabled:
            return "skipped (disabled)"
        from .fetchers.youtube import refresh_youtube

        before = {c.channel_id for c in cfg.sources.youtube.channels}
        result = await refresh_youtube(cfg)
        after = {c.channel_id for c in cfg.sources.youtube.channels}
        if after - before:  # handle→channel_id resolved this poll
            save_config(cfg)
        return result
    return f"unknown source '{target}'"


async def run_full_refresh() -> dict:
    """Full news refresh: every enabled news source, then the signal pipeline.

    Never raises — a failure in one source is recorded in its result and the
    rest still run. Persists state transitions so the UI can observe them.
    """
    cfg = load_settings().config
    state: dict = {
        "status": "running",
        "started_at": _now(),
        "finished_at": None,
        "results": {},
        "signals_stored": 0,
    }
    _save_state(state)
    results: dict = state["results"]

    for target in ("bbc", "espn", "reddit", "youtube"):
        try:
            results[target] = await refresh_one(target, cfg)
        except Exception as e:
            log.exception("startup refresh %s failed", target)
            results[target] = f"error: {e}"

    try:
        from .signals.pipeline import process_pending_items

        # FIX N8: time-budgeted like the scheduled pass.
        state["signals_stored"] = await process_pending_items(
            limit=50, timebox_sec=cfg.llm.extract_timebox_sec)
    except Exception:
        log.exception("startup signal pipeline failed (non-fatal)")

    state["finished_at"] = _now()
    state["status"] = "done"
    _save_state(state)
    log.info(
        "startup news refresh complete: %s (signals stored: %s)",
        {k: (v if isinstance(v, str) else "ok") for k, v in results.items()},
        state["signals_stored"],
    )
    return state