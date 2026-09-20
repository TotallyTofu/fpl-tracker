"""FPL official API fetcher (PLAN-2 T1.3, T3.3).

Endpoints:
- /api/bootstrap-static/      → players, events, chips, teams, game_settings
- /api/fixtures/              → team-level fixtures (base rows; also the PRIMARY
                                live-score source — ESPN is blocked (403) from the
                                user's network, see PLAN-4 T3.2 note)
- /api/element-summary/{pid}/ → per-player fixture difficulty + live history (M3)
- /api/entry/{id}/            → optional public entry (M4)

Defensive parsing per D18: missing fields → NULL + drift logged, never a crash.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from ..db import (
    check_season_rollover,
    execute,
    get_meta,
    log_poll,
    merge_fixture_difficulties,
    now_utc,
    query,
    query_one,
    set_meta,
    upsert_chips,
    upsert_events,
    upsert_fixtures,
    upsert_players,
    upsert_teams,
)
from ..httpclient import http

log = logging.getLogger("fpl.fetch")

BASE = "https://fantasy.premierleague.com/api"

REQUIRED_PLAYER_FIELDS = [
    "id", "web_name", "element_type", "team", "now_cost", "ep_next", "status",
    "can_select", "selected_by_percent",
]
REQUIRED_EVENT_FIELDS = ["id", "deadline_time"]
REQUIRED_CHIP_FIELDS = ["name", "start_event", "stop_event"]

_game_settings: dict[str, Any] = {}
_drift: list[str] = []


def get_game_settings() -> dict[str, Any]:
    return dict(_game_settings)


def get_drift() -> list[str]:
    return list(_drift)


def _season_marker(events: list[dict]) -> str:
    """Season marker from the GW1 deadline year, e.g. '2026/27'."""
    for e in events:
        if e.get("id") == 1:
            try:
                y = int(str(e.get("deadline_time", "2000-01-01"))[:4])
                return f"{y}/{(y + 1) % 100:02d}"
            except ValueError:
                pass
    return "unknown"


def _check_drift(data: dict) -> None:
    drift: list[str] = []
    for el in data.get("elements", [])[:3]:
        for f in REQUIRED_PLAYER_FIELDS:
            if f not in el:
                drift.append(f"elements[].{f}")
    for ev in data.get("events", []):
        for f in REQUIRED_EVENT_FIELDS:
            if f not in ev:
                drift.append(f"events[].{f}")
    for ch in data.get("chips", []):
        for f in REQUIRED_CHIP_FIELDS:
            if f not in ch:
                drift.append(f"chips[].{f}")
    if "game_settings" not in data:
        drift.append("game_settings")
    for d in drift:
        if d not in _drift:
            _drift.append(d)
            log.warning("SCHEMA DRIFT: missing %s", d)


async def fetch_bootstrap() -> dict[str, Any]:
    """Fetch bootstrap-static → players/events/chips/teams + game_settings cache."""
    started = now_utc()
    try:
        data = await http.get_json(f"{BASE}/bootstrap-static/")
    except Exception as e:
        log_poll("fpl", "error", error=f"bootstrap: {e}", started_at=started)
        raise
    _check_drift(data)

    if _game_settings:
        pass
    gs = data.get("game_settings") or {}
    if gs:
        _game_settings.clear()
        _game_settings.update(gs)

    events = data.get("events", [])
    marker = _season_marker(events)
    if marker != "unknown" and check_season_rollover(marker):
        log.warning("season rollover detected → wiped season-scoped tables (marker=%s)", marker)

    n_players = upsert_players(data.get("elements", []))
    n_teams = upsert_teams(data.get("teams", []))
    n_events = upsert_events(events)
    n_chips = upsert_chips(data.get("chips", []))
    log_poll("fpl", "ok", rows=n_players + n_teams + n_events + n_chips, started_at=started)
    log.info("bootstrap: %d players, %d teams, %d events, %d chips (season=%s)",
             n_players, n_teams, n_events, n_chips, marker)
    return data


async def _fetch_difficulty_for_team(team_id: int, min_event: int) -> int:
    """One element-summary call (cheapest player of the team) → merge difficulties."""
    rows = query(
        "SELECT id FROM players WHERE team = ? AND removed = 0 ORDER BY id LIMIT 1",
        (team_id,),
    )
    if not rows:
        return 0
    pid = rows[0]["id"]
    try:
        data = await http.get_json(f"{BASE}/element-summary/{pid}/")
    except Exception as e:
        log.warning("element-summary/%s failed: %s", pid, e)
        return 0
    fixtures = [fx for fx in data.get("fixtures", []) if (fx.get("event") or 0) >= min_event]
    return merge_fixture_difficulties(fixtures)


async def fetch_fixtures(with_difficulty: bool = True) -> int:
    """Base fixture rows from /api/fixtures/ + per-team difficulty (cached)."""
    started = now_utc()
    try:
        rows_api = await http.get_json(f"{BASE}/fixtures/")
    except Exception as e:
        log_poll("fpl", "error", error=f"fixtures: {e}", started_at=started)
        return 0
    rows = []
    for f in rows_api:
        if f.get("id") is None:
            continue
        rows.append(
            {
                "id": f.get("id"),
                "event": f.get("event"),
                "home_team": f.get("team_h"),
                "away_team": f.get("team_a"),
                "kickoff_time": f.get("kickoff_time"),
                "status": "finished" if f.get("finished") else ("in_play" if f.get("started") else "not_started"),
                "minutes": f.get("minutes"),
                "score_home": f.get("team_h_score"),
                "score_away": f.get("team_a_score"),
                "raw_json": json.dumps(f),
            }
        )
    n = upsert_fixtures(rows)

    if with_difficulty:
        cur = query("SELECT id FROM events WHERE is_current = 1")
        cur_gw = cur[0]["id"] if cur else 1
        teams = query("SELECT DISTINCT team FROM players WHERE team IS NOT NULL")
        total = 0
        for t in teams:
            team_id = t["team"]
            miss = query_one(
                """SELECT MIN(event) AS e FROM fixtures
                   WHERE (home_team = ? AND difficulty_home IS NULL)
                      OR (away_team = ? AND difficulty_away IS NULL)""",
                (team_id, team_id),
            )
            if miss and miss["e"] is not None:
                total += await _fetch_difficulty_for_team(team_id, miss["e"])
        if total:
            log.info("fixtures: merged %d difficulty values", total)

    log_poll("fpl", "ok", rows=n, started_at=started)
    return n


async def refresh_all_fpl() -> None:
    """Full FPL refresh: bootstrap + fixtures (+difficulty) + official-news signals."""
    await fetch_bootstrap()
    await fetch_fixtures()
    # M2 T2.7b: official FPL status/news changes → conf 1.0 signals (non-fatal).
    try:
        from ..signals.pipeline import process_official_news

        players = query(
            """SELECT id, web_name, status, news, news_added,
                      chance_of_playing_next_round
               FROM players WHERE removed = 0"""
        )
        n = process_official_news(players)
        if n:
            log.info("official news: %d signals", n)
    except Exception:
        log.exception("official news processing failed (non-fatal)")


# --- live (M3) -------------------------------------------------------------------


async def fetch_live_state() -> list[dict]:
    """Poll /api/fixtures/ for current-GW matches → live_matches + fixtures update.

    Returns the changed rows (for SSE events). This is the PRIMARY live-score
    source (official FPL data); ESPN is an optional secondary (403 on user net).
    """
    cur = query("SELECT id FROM events WHERE is_current = 1")
    if not cur:
        return []
    cur_gw = cur[0]["id"]
    try:
        rows_api = await http.get_json(f"{BASE}/fixtures/")
    except Exception as e:
        log.warning("live fixtures poll failed: %s", e)
        return []
    changed: list[dict] = []
    team_names = {t["id"]: t["name"] for t in query("SELECT id, name FROM teams")}
    for f in rows_api:
        if f.get("event") != cur_gw:
            continue
        fid = f.get("id")
        if fid is None:
            continue
        status = "post" if f.get("finished") else ("in" if f.get("started") else "pre")
        minute = f.get("minutes")
        sh, sa = f.get("team_h_score"), f.get("team_a_score")
        prev = query_one("SELECT * FROM live_matches WHERE fixture_id = ?", (fid,))
        if prev and (prev["status"], prev["minute"], prev["score_home"], prev["score_away"]) == (
            status, minute, sh, sa
        ):
            continue
        execute(
            """INSERT INTO live_matches (fixture_id, event, kickoff_time, status, minute,
               score_home, score_away, home_name, away_name, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(fixture_id) DO UPDATE SET status=excluded.status,
               minute=excluded.minute, score_home=excluded.score_home,
               score_away=excluded.score_away, updated_at=excluded.updated_at""",
            (
                fid, cur_gw, f.get("kickoff_time"), status, minute, sh, sa,
                team_names.get(f.get("team_h")), team_names.get(f.get("team_a")), now_utc(),
            ),
        )
        execute(
            """UPDATE fixtures SET status = ?, minutes = ?, score_home = ?, score_away = ?
               WHERE id = ?""",
            (
                "finished" if f.get("finished") else ("in_play" if f.get("started") else "not_started"),
                minute, sh, sa, fid,
            ),
        )
        changed.append(
            {
                "fixture_id": fid,
                "event": cur_gw,
                "kickoff_time": f.get("kickoff_time"),
                "status": status,
                "minute": minute,
                "score_home": sh,
                "score_away": sa,
                "home_name": team_names.get(f.get("team_h")),
                "away_name": team_names.get(f.get("team_a")),
            }
        )
    if changed:
        log_poll("fpl", "ok", rows=len(changed))
    return changed


async def fetch_live_player_points(player_ids: list[int]) -> list[dict]:
    """element-summary for the user's players → live_player_points (M3, 60 s cadence)."""
    changed: list[dict] = []
    for pid in player_ids:
        try:
            data = await http.get_json(f"{BASE}/element-summary/{pid}/")
        except Exception as e:
            log.warning("element-summary/%s (live) failed: %s", pid, e)
            continue
        cur_row = query_one("SELECT * FROM events WHERE is_current = 1")
        cur_gw = cur_row["id"] if cur_row else None
        total, bonus, minutes, started = 0, 0, 0, 0
        for h in data.get("history", []):
            if h.get("round") == cur_gw:
                # API drift tolerance: total_points (2026/27) vs event_points (older)
                total = h.get("total_points", h.get("event_points", 0)) or 0
                bonus = h.get("bonus", 0) or 0
                minutes = h.get("minutes", 0) or 0
                started = 1 if (h.get("minutes") or 0) > 0 else 0
                break
        prev = query_one("SELECT * FROM live_player_points WHERE player_id = ?", (pid,))
        if prev and (prev["points"], prev["bonus"], prev["minutes"]) == (total, bonus, minutes):
            continue
        execute(
            """INSERT INTO live_player_points (player_id, points, bonus, minutes, started, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(player_id) DO UPDATE SET points=excluded.points,
               bonus=excluded.bonus, minutes=excluded.minutes, started=excluded.started,
               updated_at=excluded.updated_at""",
            (pid, total, bonus, minutes, started, now_utc()),
        )
        changed.append({"player_id": pid, "points": total, "bonus": bonus, "minutes": minutes})
    if changed:
        log_poll("fpl", "ok", rows=len(changed))
    return changed


def get_season_marker() -> str | None:
    return get_meta("season")