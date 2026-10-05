"""Season state service (PLAN-2 T1.4): current GW, deadlines, chip windows, live window.

All times UTC; the UI converts to local time.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .db import get_meta, query

log = logging.getLogger("fpl.season")

CHIPS = ("wildcard", "freehit", "bboost", "triple_captain")


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def current_gw_id() -> int | None:
    row = query_one_cached("SELECT id FROM events WHERE is_current = 1")
    return row["id"] if row else None


def query_one_cached(sql: str, params: tuple = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def live_window() -> tuple[str | None, str | None]:
    """[start, end] of the ±2 h window around any current-GW kickoff (outline §5.2)."""
    gw = current_gw_id()
    if gw is None:
        return (None, None)
    now = _now()
    rows = query(
        "SELECT kickoff_time FROM fixtures WHERE event = ? AND status != 'finished'", (gw,)
    )
    kicks = []
    for r in rows:
        try:
            kicks.append(_parse(r["kickoff_time"]))
        except (ValueError, TypeError):
            continue
    if not kicks:
        return (None, None)
    start = min(kicks) - timedelta(hours=2)
    end = max(kicks) + timedelta(hours=2)
    if now < start or now > end:
        return (None, None)
    return (_fmt(start), _fmt(end))


def active_chip_windows() -> list[dict]:
    """Chip sets (with start/stop GW) that cover the current or next GW."""
    chips = query("SELECT * FROM chips ORDER BY name, set_index")
    cur = current_gw_id()
    nxt = query_one_cached("SELECT id FROM events WHERE is_next = 1")
    next_gw = nxt["id"] if nxt else (cur + 1 if cur else None)
    out = []
    for c in chips:
        if c["name"] not in CHIPS:
            # Defensive: an unrecognised name (e.g. a future API rename that
            # ingest failed to normalise) must not silently disable a chip.
            log.warning("chips table has unknown chip name %r — skipping", c["name"])
            continue
        covers_next = next_gw is not None and c["start_event"] <= next_gw <= c["stop_event"]
        covers_cur = cur is not None and c["start_event"] <= cur <= c["stop_event"]
        if covers_next or covers_cur:
            out.append(
                {
                    "chip": c["name"],
                    "set": c["set_index"],
                    "start_event": c["start_event"],
                    "stop_event": c["stop_event"],
                    "playable_next_gw": covers_next,
                }
            )
    return out


def next_gw_fixtures(gw: int | None) -> list[dict]:
    """Next-GW fixtures with team names, for the Dashboard card (T1.14)."""
    if not gw:
        return []
    rows = query(
        """SELECT f.event, f.home_team, f.away_team, f.kickoff_time, f.status,
                  f.minutes, f.score_home, f.score_away,
                  th.name AS home_name, ta.name AS away_name
           FROM fixtures f
           JOIN teams th ON th.id = f.home_team
           JOIN teams ta ON ta.id = f.away_team
           WHERE f.event = ?
           ORDER BY f.kickoff_time""",
        (gw,),
    )
    return [dict(r) for r in rows]


def current_season() -> dict:
    events = query("SELECT * FROM events ORDER BY id")
    cur = next((e for e in events if e["is_current"]), None)
    nxt = next((e for e in events if e["is_next"]), None)
    now = _now()
    deadline_is_past = bool(nxt and _parse(nxt["deadline_time"]) < now)
    lw_start, lw_end = live_window()
    cur_gw = cur["id"] if cur else None
    next_gw = nxt["id"] if nxt else None
    return {
        "season": get_meta("season"),
        "current_gw": cur_gw,
        "next_gw": next_gw,
        "deadline": nxt["deadline_time"] if nxt else None,
        "deadline_is_past": deadline_is_past,
        "live_mode": lw_start is not None,
        "live_window": [lw_start, lw_end] if lw_start else None,
        "chip_windows": active_chip_windows(),
        "events_total": len(events),
        "fixtures_next_gw": next_gw_fixtures(next_gw),
    }