"""Meta endpoints: season state, health, on-demand refresh, settings, LLM test."""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, HTTPException

from .. import season as season_svc
from ..config import ConfigFile, load_config, load_settings, save_config
from ..db import recent_polls
from ..fetchers import fpl as fpl_fetcher
from ..startup import refresh_one

log = logging.getLogger("fpl.api.meta")
router = APIRouter()


@router.get("/meta/season")
async def get_season() -> dict:
    return season_svc.current_season()


@router.get("/meta/startup-refresh")
async def get_startup_refresh() -> dict:
    """State of the boot-time full news refresh (idle/running/done)."""
    from ..startup import get_startup_state

    return get_startup_state()


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


@router.get("/meta/db-stats")
async def get_db_stats() -> dict:
    """DB stats for Settings → Data: row counts, file size, recent poll errors."""
    from ..config import DB_PATH
    from ..db import get_conn

    tables = [
        "players", "teams", "events", "fixtures", "lineups", "lineup_players",
        "suggestions", "raw_items", "signals", "official_news_cache",
        "chip_plays_log", "poll_log",
    ]
    conn = get_conn()
    try:
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
        errors = conn.execute(
            "SELECT source, status, rows, error, finished_at FROM poll_log "
            "WHERE status = 'error' ORDER BY id DESC LIMIT 10"
        ).fetchall()
    finally:
        conn.close()
    return {
        "row_counts": counts,
        "db_size_bytes": DB_PATH.stat().st_size if DB_PATH.exists() else 0,
        "recent_errors": [dict(e) for e in errors],
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
            else:
                results[t] = await refresh_one(t, cfg)
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


def _settings_payload(s) -> dict:
    """Shared settings payload (FIX.MD A17): GET and PUT build it the same way,
    so they cannot diverge. A3: the key is redacted, never echoed."""
    out = s.config.model_dump()
    out["llm"]["api_key"] = ""            # A3
    # which source won, per value (.env beats config.json when set)
    from_env = {
        "base_url": bool(os.getenv("LLM_BASE_URL", "").strip()),
        "key": bool(os.getenv("LLM_API_KEY", "").strip()),
        "model": bool(os.getenv("LLM_MODEL", "").strip()),
    }
    def src(k: str) -> str:
        return ".env" if from_env[k] else "config.json"
    out["llm_status"] = {
        "ready": s.llm_ready,
        "base_url": s.llm_base_url,
        "model": s.llm_model or None,
        "key_set": bool(s.llm_api_key),
        "note": (
            "LLM values come from .env when set, else config.json (Settings UI). "
            f"Currently: base_url from {src('base_url')}, key from {src('key')}, "
            f"model from {src('model')}. The key is never returned by this endpoint — "
            "leave the field blank to keep the saved key."
        ),
    }
    try:
        import pulp  # noqa: F401

        out["pulp_available"] = True
    except ImportError:
        out["pulp_available"] = False
    return out


@router.get("/settings")
async def get_settings() -> dict:
    return _settings_payload(load_settings())


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
    if not body.llm.api_key:                       # empty = "leave as-is" (FIX.MD A3)
        # preserve the *config.json* value, not the env-merged one, so a PUT
        # never copies an env key into the file
        body.llm.api_key = load_config().llm.api_key
    save_config(body)
    # A17: return the effective (env-merged) values, same shape as GET —
    # status row stays correct after every save.
    return _settings_payload(load_settings())


@router.post("/settings/test-llm")
async def test_llm(body: dict | None = None) -> dict:
    """Trivial chat/completions call against the configured endpoint.

    Accepts an optional draft body (base_url/api_key/model) so the Settings UI
    can test the *edited* values before saving (FIX.MD A4). Empty/missing
    fields fall back to the saved settings — the redacted key field (A3)
    therefore still tests the saved key.

    A20: routes through llm_extractor.test_llm_connection (256-token budget,
    configured timeout) so this probe and the scheduler-side health check
    cannot drift apart again.
    """
    from ..signals import llm_extractor

    s = load_settings()
    b = body or {}
    base_url = (b.get("base_url") or "").strip()
    api_key = (b.get("api_key") or "").strip()
    model = (b.get("model") or "").strip()
    if not (api_key or s.llm_api_key) or not (model or s.llm_model):
        return {"ok": False, "error": "LLM model or API key not set (fill them in this page or .env)"}
    # A20: the one shared probe (256-token budget, configured timeout) — this
    # endpoint and the scheduler-side health check cannot drift apart again.
    out = await llm_extractor.test_llm_connection(
        s,
        base_url=base_url or None,
        api_key=api_key or None,
        model=model or None,
    )
    if out["ok"]:
        # keep the response shape the Settings UI consumes: {ok, model, reply}
        return {"ok": True,
                "model": out.get("model") or (model or s.llm_model),
                "reply": (out.get("reply") or "")[:100]}
    return {"ok": False, "error": out.get("detail") or out.get("status") or "unknown error"}