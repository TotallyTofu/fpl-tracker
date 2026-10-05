"""Entry endpoints: public FPL entry rank (T4.4, PLAN.MD §8.8).

No login — public endpoint only. 400 when the entry ID is unset, non-numeric,
or not found upstream; 502 when the upstream fetch fails.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from ..config import load_settings
from ..fetchers import fpl as fpl_fetcher

log = logging.getLogger("fpl.api.entry")
router = APIRouter()


@router.get("/entry")
async def get_entry() -> dict:
    cfg = load_settings().config
    entry_id = (cfg.group.fpl_entry_id or "").strip()
    if not entry_id:
        raise HTTPException(400, "fpl_entry_id not set (Settings → Group)")
    if not entry_id.isdigit():
        raise HTTPException(400, "fpl_entry_id must be numeric — it is the number in your FPL team URL")
    try:
        data = await fpl_fetcher.fetch_entry(entry_id)
    except Exception as e:
        log.exception("entry fetch failed")
        raise HTTPException(502, f"entry fetch failed: {e}")
    if data is None:
        raise HTTPException(400, f"entry {entry_id} not found — check the ID in your FPL team URL")
    return data