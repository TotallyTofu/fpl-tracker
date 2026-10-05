"""Meta endpoints: season state, health, on-demand refresh, settings, LLM test."""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, HTTPException

from .. import season as season_svc
from ..config import ConfigFile, load_config, load_settings, save_config
from ..db import query, recent_polls
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


@router.get("/meta/team-fixtures")
async def get_team_fixtures(count: int = 3) -> dict:
    """Each club's next ``count`` gameweeks from the next GW: opponent short
    name, home/away and FPL difficulty (1–5). A GW with no fixture is a blank."""
    season = season_svc.current_season()
    start = season.get("next_gw") or season.get("current_gw")
    teams = {r["id"]: {"short": r["short_name"], "name": r["name"]}
             for r in query("SELECT id, name, short_name FROM teams")}
    out: dict[int, list[dict]] = {t: [] for t in teams}
    if start:
        count = max(1, min(count, 8))
        rows = query(
            "SELECT event, home_team, away_team, difficulty_home, difficulty_away, kickoff_time "
            "FROM fixtures WHERE event BETWEEN ? AND ? ORDER BY event, kickoff_time",
            (start, start + count - 1))
        for r in rows:
            for team, opp, home, d in ((r["home_team"], r["away_team"], True, r["difficulty_home"]),
                                       (r["away_team"], r["home_team"], False, r["difficulty_away"])):
                if team in out:
                    out[team].append({"gw": r["event"], "opp": teams.get(opp, {}).get("short", "?"),
                                      "home": home, "d": d or 3})
    return {"from_gw": start, "count": count, "teams": teams, "fixtures": out}


_SOURCES = ("fpl-official", "bbc", "espn", "reddit", "youtube")


@router.get("/meta/sources")
async def get_sources() -> dict:
    """Per-source health for the News page: enabled, last poll, items,
    live signals, items waiting for extraction, and source-specific notes."""
    from ..db import now_utc

    s = load_settings()
    cfg = s.config
    now = now_utc()
    enabled = {
        "fpl-official": cfg.sources.fpl.enabled,
        "bbc": cfg.sources.bbc.enabled,
        "espn": cfg.sources.espn.enabled,
        "reddit": cfg.sources.reddit.enabled,
        "youtube": cfg.sources.youtube.enabled,
    }
    poll_name = {"fpl-official": "fpl"}
    out = []
    for src in _SOURCES:
        last = query("SELECT status, rows, error, finished_at FROM poll_log WHERE source = ? "
                     "ORDER BY id DESC LIMIT 1", (poll_name.get(src, src),))
        items = query(
            "SELECT COUNT(*) AS n, MAX(retrieved_at) AS last, "
            "SUM(CASE WHEN processed = 0 THEN 1 ELSE 0 END) AS waiting, "
            "SUM(CASE WHEN published_at IS NULL THEN 1 ELSE 0 END) AS undated, "
            "SUM(CASE WHEN takeaways LIKE '%circuit breaker open%' THEN 1 ELSE 0 END) AS skipped "
            "FROM raw_items WHERE source = ?", (src,))[0]
        sigs = query("SELECT COUNT(*) AS n FROM signals WHERE expires_at > ? AND "
                     "(source = ? OR source LIKE ?)", (now, src, f"{src}:%"))[0]["n"]
        out.append({
            "source": src,
            "enabled": enabled[src],
            "last_poll": last[0] if last else None,
            "items": items["n"] or 0,
            "last_item": items["last"],
            "waiting": items["waiting"] or 0,
            "undated": items["undated"] or 0,
            "skipped_by_llm_breaker": items["skipped"] or 0,
            "live_signals": sigs,
        })
    llm_rows = query("SELECT status, COUNT(*) AS n FROM poll_log WHERE source = 'llm' "
                     "AND started_at > strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-7 days') "
                     "GROUP BY status")
    llm_counts = {r["status"]: r["n"] for r in llm_rows}
    llm_last = query("SELECT status, error, finished_at FROM poll_log WHERE source = 'llm' "
                     "ORDER BY id DESC LIMIT 1")
    return {
        "sources": out,
        "llm": {
            "ready": s.llm_ready,
            "model": s.llm_model or None,
            "base_url": s.llm_base_url,
            "disable_thinking": bool(getattr(cfg.llm, "disable_thinking", True)),
            "ok_7d": llm_counts.get("ok", 0),
            "errors_7d": llm_counts.get("error", 0),
            "last": llm_last[0] if llm_last else None,
        },
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
    # v1.0: the Reddit OAuth secret is a credential too — never echoed.
    reddit_secret_set = bool(out["sources"]["reddit"].get("oauth_client_secret"))
    out["sources"]["reddit"]["oauth_client_secret"] = ""
    out["reddit_status"] = {"secret_set": reddit_secret_set}
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
    stored = load_config()
    if not body.llm.api_key:                       # empty = "leave as-is" (FIX.MD A3)
        # preserve the *config.json* value, not the env-merged one, so a PUT
        # never copies an env key into the file
        body.llm.api_key = stored.llm.api_key
    if not body.sources.reddit.oauth_client_secret:  # v1.0: same rule for the Reddit secret
        body.sources.reddit.oauth_client_secret = stored.sources.reddit.oauth_client_secret
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
    # v1.0 security: the saved key is only ever sent to the saved endpoint. A
    # draft base_url must come with its own key — otherwise any page that can
    # reach this local server could have the app post your key to its server.
    if base_url and base_url.rstrip("/") != (s.llm_base_url or "").rstrip("/") and not api_key:
        return {"ok": False, "error": "To test a different base URL, type the API key for it too "
                                      "(the saved key is only sent to the saved URL)."}
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