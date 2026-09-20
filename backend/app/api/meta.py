"""Meta endpoints: season state, health, on-demand refresh, settings, LLM test."""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException

from .. import season as season_svc
from ..config import ConfigFile, load_settings, save_config
from ..db import recent_polls
from ..fetchers import fpl as fpl_fetcher

log = logging.getLogger("fpl.api.meta")
router = APIRouter()


@router.get("/meta/season")
async def get_season() -> dict:
    return season_svc.current_season()


@router.get("/meta/health")
async def get_health() -> dict:
    polls = recent_polls(20)
    last: dict[str, dict] = {}
    for p in polls:
        if p["source"] not in last:
            last[p["source"]] = {
                "status": p["status"],
                "rows": p["rows"],
                "error": p["error"],
                "finished_at": p["finished_at"],
            }
    return {
        "db_ok": True,
        "last_polls": last,
        "schema_drift": fpl_fetcher.get_drift(),
    }


@router.post("/refresh/{source}")
async def refresh(source: str) -> dict:
    """On-demand refresh (PLAN.MD §8.4). Runs the fetcher immediately, then the
    signal pipeline over any newly ingested items."""
    if source not in ("fpl", "espn", "bbc", "reddit", "youtube", "all"):
        raise HTTPException(404, f"unknown source '{source}'")
    targets = ["fpl", "espn", "bbc", "reddit", "youtube"] if source == "all" else [source]
    cfg = load_settings().config
    results: dict[str, dict | str] = {}
    for t in targets:
        try:
            if t == "fpl":
                await fpl_fetcher.refresh_all_fpl()
                results[t] = "ok"
            elif t == "bbc":
                from ..fetchers.bbc import refresh_bbc

                results[t] = await refresh_bbc()
            elif t == "espn":
                if not cfg.sources.espn.enabled:
                    results[t] = "skipped (espn disabled in settings)"
                    continue
                from ..fetchers.espn import refresh_espn

                results[t] = await refresh_espn()
            elif t == "reddit":
                from ..fetchers.reddit import refresh_reddit

                results[t] = await refresh_reddit(cfg)
            elif t == "youtube":
                from ..fetchers.youtube import refresh_youtube

                before = {c.channel_id for c in cfg.sources.youtube.channels}
                results[t] = await refresh_youtube(cfg)
                after = {c.channel_id for c in cfg.sources.youtube.channels}
                if after - before:  # handle→channel_id resolved this poll
                    save_config(cfg)
        except Exception as e:
            log.exception("refresh %s failed", t)
            results[t] = f"error: {e}"
    # signal pipeline over newly ingested items (non-fatal)
    try:
        from ..signals.pipeline import process_pending_items

        n = await process_pending_items(limit=50)
        if n:
            results["signals_stored"] = n
    except Exception:
        log.exception("signal pipeline failed (non-fatal)")
    return {"results": results}


@router.get("/settings")
async def get_settings() -> dict:
    s = load_settings()
    out = s.config.model_dump()
    out["llm_status"] = {
        "ready": s.llm_ready,
        "base_url": s.llm_base_url,
        "model": s.llm_model or None,
        "key_set": bool(s.llm_api_key),
        "note": "LLM values come from .env when set, else config.json (Settings UI).",
    }
    return out


@router.put("/settings")
async def put_settings(body: ConfigFile) -> dict:
    w = body.optimizer.weights
    total = w.ep + w.form + w.fixture
    if abs(total - 1.0) > 0.01:
        raise HTTPException(422, f"optimizer weights must sum to 1.0 (got {total:.2f})")
    if body.sources.fpl.bootstrap_interval_min < 5:
        raise HTTPException(422, "fpl.bootstrap_interval_min must be >= 5")
    if body.sources.youtube.interval_min < 5:
        raise HTTPException(422, "youtube.interval_min must be >= 5")
    if body.optimizer.solver.timebox_sec < 1 or body.optimizer.solver.timebox_sec > 60:
        raise HTTPException(422, "solver.timebox_sec must be 1–60")
    save_config(body)
    return body.model_dump()


@router.post("/settings/test-llm")
async def test_llm() -> dict:
    """Trivial chat/completions call against the configured endpoint."""
    s = load_settings()
    if not s.llm_api_key or not s.llm_model:
        return {"ok": False, "error": "LLM model or API key not set (fill them in this page or .env)"}
    url = s.llm_base_url.rstrip("/") + "/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                url,
                headers={"Authorization": f"Bearer {s.llm_api_key}"},
                json={
                    "model": s.llm_model,
                    "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
                    "max_tokens": 8,
                },
            )
        if r.status_code != 200:
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        data = r.json()
        reply = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return {"ok": True, "model": data.get("model", s.llm_model), "reply": reply[:100]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}