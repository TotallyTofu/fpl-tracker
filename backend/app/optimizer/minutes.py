"""Minutes model (v1.1): how much of each match a player plays, from his
per-gameweek history (``player_gw_history``).

    share  = expected share of 90 minutes per team match (recent window + season)
    rate90 = points per 90, shrunk toward the position average

``scoring.ep_final`` blends ``flag × rate90 × share × fixtures`` with the v1
projection (default 50/50). So a player who has been rotated or used as a
substitute is projected lower than FPL's form-based ``ep_next`` suggests.

The constants below come from the 2025-26 backtest (ADD-FEATURES.MD App. A/B).
They are fitted values, not rules: ``scripts/scorecard.py`` re-checks them on
this season's data.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ..db import query

log = logging.getLogger("fpl.minutes")

PRIOR_PTS90 = {1: 3.4, 2: 3.8, 3: 4.4, 4: 5.0}   # 2025-26 points per 90 by position
RECENT_N = 5            # last 5 GWs in which the player's team played
RECENT_W = 0.6          # weight of the recent window vs the season
SHRINK_MIN = 450        # prior counts as 450 minutes (5 full matches)


def load_history(before_gw: int, player_ids: list[int] | None = None) -> dict[int, list[dict]]:
    """Rows with gw < before_gw AND team_matches > 0 (a blank gameweek says
    nothing about a player), grouped by player, ascending gw. One query;
    ``player_ids`` narrows it to those players."""
    sql = ("SELECT player_id, gw, team_matches, minutes, starts, total_points "
           "FROM player_gw_history WHERE gw < ? AND team_matches > 0")
    params: list = [before_gw]
    if player_ids is not None:
        sql += " AND player_id IN (%s)" % ",".join("?" * len(player_ids))
        params += list(player_ids)
    out: dict[int, list[dict]] = {}
    for r in query(sql + " ORDER BY player_id, gw", params):
        out.setdefault(r["player_id"], []).append(r)
    return out


@dataclass
class MinutesProfile:
    share: float                  # expected share of 90 minutes per team match, ≤ 1
    rate90: float                 # shrunk points per 90
    start_rate5: float | None     # starts per team match over the last RECENT_N GWs
    last_match: str | None        # "started" | "came_on" | "no_minutes"
    matches: int                  # team matches in history


def profile(rows: list[dict], element_type: int) -> MinutesProfile | None:
    """None when the player has no team match in history."""
    rows = [r for r in rows if (r.get("team_matches") or 0) > 0]
    if not rows:
        return None
    rec = rows[-RECENT_N:]

    def tot(rs: list[dict], key: str) -> int:
        return sum(int(r[key] or 0) for r in rs)

    tm_all, tm_rec = tot(rows, "team_matches"), tot(rec, "team_matches")
    min_all, min_rec = tot(rows, "minutes"), tot(rec, "minutes")
    share = min(1.0, RECENT_W * min_rec / (90.0 * tm_rec)
                + (1 - RECENT_W) * min_all / (90.0 * tm_all))
    prior = PRIOR_PTS90.get(element_type, PRIOR_PTS90[3])
    rate90 = (tot(rows, "total_points") + prior * SHRINK_MIN / 90.0) \
        / ((min_all + SHRINK_MIN) / 90.0)
    start_rate5 = (None if any(r.get("starts") is None for r in rec)
                   else min(1.0, tot(rec, "starts") / tm_rec))
    last = rows[-1]
    if last.get("starts") is not None and last["starts"] >= 1:
        last_match = "started"
    elif (last.get("minutes") or 0) > 0:
        last_match = "came_on"
    else:
        last_match = "no_minutes"
    return MinutesProfile(share=share, rate90=rate90, start_rate5=start_rate5,
                          last_match=last_match, matches=tm_all)


# P(start next GW) for unflagged players, by start rate over the last 5 team
# matches × what happened in the last match. 2025-26 GW12-38; cell sizes are in
# ADD-FEATURES.MD App. B.
START_TABLE = {            # bucket: (started, came_on, no_minutes)
    0: (0.30, 0.31, 0.14),  # start rate 0-19%  ("started" cell n=25, rounded down)
    1: (0.67, 0.38, 0.25),  # 20-39%
    2: (0.69, 0.39, 0.29),  # 40-59%
    3: (0.77, 0.53, 0.29),  # 60-79%
    4: (0.88, 0.57, 0.51),  # 80-100%
}
_LAST_MATCH_COL = {"started": 0, "came_on": 1, "no_minutes": 2}

# Flagged players start far less often than they play (whole pool: fit 65%
# start; 75% flag 39%, 50% flag 15%, 25% flag 2%).
START_FLAG_FACTOR = {75: 0.60, 50: 0.25, 25: 0.05}


def start_probability(p: dict, prof: MinutesProfile | None) -> float | None:
    """P(starts the next gameweek). 0 for hard gates (u/s/can_select=0) and
    flags below 25%; None without history or without starts data. Display
    only: nothing in the solver reads it."""
    if p.get("status") in ("u", "s") or p.get("can_select") == 0:
        return 0.0
    c = p.get("chance_of_playing_next_round")
    if c is not None and c < 25:
        return 0.0
    if prof is None or prof.start_rate5 is None or prof.last_match is None:
        return None
    bucket = min(4, int(prof.start_rate5 * 5 + 1e-9))   # 1e-9: float noise at 0.2/0.4/0.6/0.8
    prob = START_TABLE[bucket][_LAST_MATCH_COL[prof.last_match]]
    if c is not None and c < 100:
        prob *= START_FLAG_FACTOR[75 if c >= 75 else 50 if c >= 50 else 25]
    return prob


def _profiles(players: list[dict], target_gw: int, id_key: str):
    """(player, MinutesProfile | None) pairs. History problems never break a
    projection or a page: the players just get no profile."""
    ids = [p[id_key] for p in players]
    try:
        hist = load_history(target_gw, ids) if ids else {}
    except Exception:
        log.exception("minutes history unavailable — falling back to v1 projections")
        hist = {}
    return [(p, profile(hist.get(p[id_key], []), p.get("element_type"))) for p in players]


def attach_profiles(players: list[dict], target_gw: int) -> None:
    """Add the minutes-model fields to player dicts in place: ``mm_share``,
    ``mm_rate90``, ``start_rate5``, ``last_match`` (None without history) and
    ``p_start``. Players without history fall back to the v1 projection."""
    for p, prof in _profiles(players, target_gw, "id"):
        p["mm_share"] = prof.share if prof else None
        p["mm_rate90"] = prof.rate90 if prof else None
        p["start_rate5"] = prof.start_rate5 if prof else None
        p["last_match"] = prof.last_match if prof else None
        p["p_start"] = start_probability(p, prof)


def attach_start_info(players: list[dict], target_gw: int | None, id_key: str = "id") -> None:
    """Display fields only (``p_start`` and ``start_rate5`` rounded to 2 dp,
    ``last_match``) for API payloads such as the saved lineup; None when there
    is no history."""
    if target_gw is None:
        for p in players:
            p["p_start"], p["last_match"], p["start_rate5"] = None, None, None
        return
    for p, prof in _profiles(players, target_gw, id_key):
        ps = start_probability(p, prof)
        p["p_start"] = round(ps, 2) if ps is not None else None
        p["last_match"] = prof.last_match if prof else None
        p["start_rate5"] = (round(prof.start_rate5, 2)
                            if prof and prof.start_rate5 is not None else None)
