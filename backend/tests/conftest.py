"""Shared fixtures: temp DB with deterministic sample data + a lightweight cfg.

Run from the backend/ directory:  .venv/Scripts/python -m pytest tests -q
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

from app import db as dbmod  # noqa: E402

NOW = "2026-09-19T12:00:00Z"

TEAMS = [(1, "Alpha FC"), (2, "Beta FC"), (3, "Gamma FC"), (4, "Delta FC"), (5, "Epsilon FC")]

# (id, web_name, element_type, team, now_cost, ep_next, selected_by_percent, status)
# now_cost is in FPL units: 10 = £1m (a £4.5m player = 45). Squad budget = 1000.
PLAYERS = [
    (1, "Goal One", 1, 1, 45, 8.0, 50.0, "a"),
    (2, "Goal Two", 1, 2, 45, 7.0, 40.0, "a"),
    (3, "Def A One", 2, 1, 40, 9.0, 60.0, "a"),
    (4, "Def A Two", 2, 1, 45, 8.0, 55.0, "a"),
    (5, "Def B One", 2, 2, 40, 8.5, 50.0, "a"),
    (6, "Def B Two", 2, 2, 42, 7.5, 45.0, "a"),
    (7, "Def C One", 2, 3, 45, 9.5, 48.0, "a"),
    (8, "Def C Two", 2, 5, 40, 7.0, 42.0, "a"),
    (9, "Def D One", 2, 4, 45, 8.0, 47.0, "a"),
    (10, "Def D Two", 2, 4, 40, 6.5, 41.0, "a"),
    (11, "Mid A One", 3, 1, 50, 11.0, 52.0, "a"),
    (12, "Mid A Two", 3, 1, 55, 10.0, 49.0, "a"),
    (13, "Mid B One", 3, 2, 50, 10.5, 51.0, "a"),
    (14, "Mid B Two", 3, 2, 55, 9.5, 46.0, "a"),
    (15, "Mid C One", 3, 3, 55, 12.0, 44.0, "a"),
    (16, "Mid C Two", 3, 5, 50, 9.0, 43.0, "a"),
    (17, "Mid D One", 3, 4, 55, 10.0, 45.0, "a"),
    (18, "Mid D Two", 3, 4, 50, 8.5, 40.0, "a"),
    (19, "Fwd A One", 4, 1, 80, 13.0, 58.0, "a"),
    (20, "Fwd A Two", 4, 1, 75, 11.0, 54.0, "a"),
    (21, "Fwd B One", 4, 2, 80, 12.0, 56.0, "a"),
    (22, "Fwd B Two", 4, 2, 75, 10.0, 50.0, "a"),
    (23, "Fwd C One", 4, 3, 85, 14.0, 30.0, "a"),
    (24, "Fwd D One", 4, 5, 80, 11.5, 35.0, "a"),
    (25, "Saka Junior", 4, 5, 85, 12.5, 15.0, "a"),
    (26, "Fwd C Two", 4, 3, 75, 10.5, 28.0, "a"),
    (27, "Fwd D Two", 4, 4, 70, 9.5, 25.0, "a"),
    (28, "Mid C Two", 3, 3, 50, 9.5, 40.0, "a"),
]

EVENTS = [
    (5, "Gameweek 5", "2026-09-20T10:00:00Z", 1, 0),
    (6, "Gameweek 6", "2026-09-27T10:00:00Z", 0, 1),
]

FIXTURES_GW6 = [
    (6, 1, 2, "2026-09-26T15:00:00Z", 1, 2),
    (6, 3, 4, "2026-09-26T17:30:00Z", 2, 1),
]

CHIPS = [
    (1, "wildcard", 1, 1, 38, "wildcard", 1),
    (2, "freehit", 1, 1, 38, "free_hit", 1),
    (3, "bboost", 1, 6, 6, "3x_points", 1),
    (4, "triple_captain", 1, 6, 6, "3x_captain", 1),
]


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    p = tmp_path / "test_fpl.db"
    monkeypatch.setattr(dbmod, "DB_PATH", p)
    dbmod.init_db(p)
    conn = dbmod.get_conn(p)
    conn.executemany("INSERT INTO teams (id, name, fetched_at) VALUES (?,?,?)",
                     [(t[0], t[1], NOW) for t in TEAMS])
    conn.executemany(
        "INSERT INTO events (id, name, deadline_time, is_current, is_next, finished, data_checked, released) "
        "VALUES (?,?,?,?,?,0,0,1)",
        [(e[0], e[1], e[2], e[3], e[4]) for e in EVENTS],
    )
    conn.executemany(
        "INSERT INTO fixtures (event, home_team, away_team, kickoff_time, status, difficulty_home, difficulty_away, fetched_at) "
        "VALUES (?,?,?,?, 'not_started', ?, ?, ?)",
        [(f[0], f[1], f[2], f[3], f[4], f[5], NOW) for f in FIXTURES_GW6],
    )
    conn.executemany(
        "INSERT INTO chips (id, name, number, start_event, stop_event, chip_type, set_index, fetched_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [(c[0], c[1], c[2], c[3], c[4], c[5], c[6], NOW) for c in CHIPS],
    )
    conn.executemany(
        "INSERT INTO players (id, web_name, element_type, team, now_cost, ep_next, selected_by_percent, "
        "status, can_select, removed, chance_of_playing_next_round, form, fetched_at) "
        "VALUES (?,?,?,?,?,?,?,?,1,0,100,5.0,?)",
        [(pl[0], pl[1], pl[2], pl[3], pl[4], pl[5], pl[6], pl[7], NOW) for pl in PLAYERS],
    )
    conn.commit()
    conn.close()
    return p


@pytest.fixture()
def cfg():
    """Lightweight cfg with the exact fields the optimizer reads (fast test solver)."""
    return types.SimpleNamespace(
        optimizer=types.SimpleNamespace(
            weights=types.SimpleNamespace(ep=0.7, form=0.15, fixture=0.15),
            solver=types.SimpleNamespace(restarts=3, timebox_sec=2),
            differential_lambda=3.0,
            differential_ep_floor=0.4,
            availability=types.SimpleNamespace(
                active=False, doubt=0.5, chance_null=0.9, chance_100=1.0, chance_50=0.5, chance_0=0.0
            ),
            signal=types.SimpleNamespace(neg_per=-0.5, neg_cap=-0.6, pos_per=0.1, pos_cap=0.2),
        )
    )


def mkplayer(pid, et, team, cost, role="starter", bench_order=None, cap=False, vc=False,
             status="a", can_select=1, name=None):
    """Minimal player dict for rules.validate_lineup."""
    return {
        "player_id": pid,
        "web_name": name or f"P{pid}",
        "element_type": et,
        "team": team,
        "now_cost": cost,
        "status": status,
        "can_select": can_select,
        "role": role,
        "bench_order": bench_order,
        "is_captain": cap,
        "is_vice_captain": vc,
    }


def valid_squad():
    """A fully valid 15-player squad (2/5/5/3, XI 1/4/3/3, bench 1-4, C+VC starters,
    exactly 3 per club — costs sum to 805/1000)."""
    squad = [
        mkplayer(1, 1, 1, 45, cap=True),       # GK  t1
        mkplayer(2, 1, 2, 45, role="bench", bench_order=1),   # GK  t2
        mkplayer(3, 2, 1, 40),                 # DEF t1
        mkplayer(5, 2, 2, 40),                 # DEF t2
        mkplayer(7, 2, 3, 45),                 # DEF t3
        mkplayer(9, 2, 4, 45),                 # DEF t4
        mkplayer(8, 2, 5, 40, role="bench", bench_order=2),   # DEF t5
        mkplayer(13, 3, 2, 50, vc=True),       # MID t2
        mkplayer(15, 3, 3, 55),                # MID t3
        mkplayer(17, 3, 4, 55),                # MID t4
        mkplayer(18, 3, 4, 50, role="bench", bench_order=3),  # MID t4
        mkplayer(16, 3, 5, 50, role="bench", bench_order=4),  # MID t5
        mkplayer(19, 4, 1, 80),                # FWD t1
        mkplayer(23, 4, 3, 85),                # FWD t3
        mkplayer(24, 4, 5, 80),                # FWD t5
    ]
    return squad