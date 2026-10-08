"""v1.1 T6: the projection log (what the app projected before each deadline)."""
import asyncio

import pytest

from app import db as dbmod
from app.config import ConfigFile
from app.fetchers import fpl as fpl_fetcher
from app.optimizer import projlog

BEFORE = "2026-09-26T12:00:00Z"        # the shared fixture's GW6 deadline is 2026-09-27T10:00:00Z
AFTER = "2026-09-27T10:00:01Z"


def _rows(gw=6):
    return {r["player_id"]: r for r in
            dbmod.query("SELECT * FROM projection_log WHERE gw = ? ORDER BY player_id", (gw,))}


def test_writes_one_row_per_eligible_player(db_path):
    n = projlog.log_projections(ConfigFile(), now=BEFORE)
    rows = _rows()
    assert n == len(rows) == 28                       # every fixture player is eligible
    r = rows[11]
    assert r["status"] == "a" and r["chance"] == 100 and r["ep_next"] == 11.0
    assert r["logged_at"] == BEFORE
    assert r["ep_final"] > 0 and r["ep_v10"] > 0
    assert r["minutes_ep"] is None and r["p_start"] is None        # no history in this DB


def test_is_idempotent(db_path):
    projlog.log_projections(ConfigFile(), now=BEFORE)
    first = _rows()
    assert projlog.log_projections(ConfigFile(), now="2026-09-26T18:00:00Z") == 28
    second = _rows()
    assert len(second) == 28 and set(second) == set(first)
    assert all(r["logged_at"] == "2026-09-26T18:00:00Z" for r in second.values())   # last write wins


def test_nothing_is_written_after_the_deadline(db_path):
    assert projlog.log_projections(ConfigFile(), now=AFTER) == 0
    assert _rows() == {}
    projlog.log_projections(ConfigFile(), now=BEFORE)
    before = _rows()
    assert projlog.log_projections(ConfigFile(), now=AFTER) == 0
    assert _rows() == before                          # frozen at the last pre-deadline write


def test_no_next_gameweek_logs_nothing(db_path):
    dbmod.execute("UPDATE events SET is_next = 0")
    assert projlog.log_projections(ConfigFile(), now=BEFORE) == 0


def test_rows_freeze_when_next_gw_advances(db_path):
    projlog.log_projections(ConfigFile(), now=BEFORE)
    frozen = _rows(6)
    # the GW6 deadline passes: GW6 becomes current and GW7 the next
    dbmod.execute("UPDATE events SET is_current = 0, is_next = 0")
    dbmod.execute("UPDATE events SET is_current = 1 WHERE id = 6")
    dbmod.execute("INSERT INTO events (id, name, deadline_time, is_current, is_next, finished, "
                  "data_checked, released) VALUES (7, 'Gameweek 7', '2026-10-04T10:00:00Z', 0, 1, 0, 0, 1)")
    assert projlog.log_projections(ConfigFile(), now="2026-09-28T09:00:00Z") == 28
    assert _rows(6) == frozen
    assert len(_rows(7)) == 28


def test_a_player_who_becomes_unavailable_leaves_the_log(db_path):
    projlog.log_projections(ConfigFile(), now=BEFORE)
    assert 12 in _rows()
    dbmod.execute("UPDATE players SET status = 'u' WHERE id = 12")
    projlog.log_projections(ConfigFile(), now="2026-09-26T18:00:00Z")
    assert 12 not in _rows() and len(_rows()) == 27


def test_ep_v10_reproduces_the_old_formula_and_ep_final_the_new_one(db_path):
    """Player 11: form 5, FPL's ep_next for a 75% flag = 5 × 0.75 = 3.75, home
    fixture difficulty 1 (MID: ×1.15). v1.0 = (0.7·3.75 + 0.15·5·0.75)/0.85 × 1.15;
    v1.1 un-scales ep_next (5.0) and applies the 0.60 curve."""
    dbmod.execute("UPDATE players SET ep_next = 3.75, chance_of_playing_next_round = 75 WHERE id = 11")
    projlog.log_projections(ConfigFile(), now=BEFORE)
    r = _rows()[11]
    assert r["ep_v10"] == pytest.approx((0.7 * 3.75 + 0.15 * 5.0 * 0.75) / 0.85 * 1.15)
    assert r["ep_final"] == pytest.approx((0.7 * 5.0 * 0.6 + 0.15 * 5.0 * 0.6) / 0.85 * 1.15)
    assert r["ep_final"] < r["ep_v10"]


def test_log_carries_minutes_estimate_and_start_probability(with_history):
    projlog.log_projections(ConfigFile(), now=BEFORE)
    rows = _rows()
    assert rows[11]["p_start"] == 0.88
    assert rows[11]["minutes_ep"] == pytest.approx(5.4 * 1.0)         # rate90 5.4 × share 1.0
    assert rows[12]["minutes_ep"] == pytest.approx(4.0 * 0.3)
    assert rows[12]["ep_final"] < rows[12]["ep_v10"]                   # rotation player drops
    assert rows[1]["minutes_ep"] is None


def test_minutes_ep_counts_a_double_gameweek(with_history):
    dbmod.execute("INSERT INTO fixtures (event, home_team, away_team, kickoff_time, status, "
                  "difficulty_home, difficulty_away, fetched_at) VALUES "
                  "(6, 1, 3, '2026-09-28T15:00:00Z', 'not_started', 3, 3, '2026-09-19T12:00:00Z')")
    projlog.log_projections(ConfigFile(), now=BEFORE)
    assert _rows()[11]["minutes_ep"] == pytest.approx(5.4 * 1.0 * 2)  # team 1 plays twice


def test_refresh_survives_a_projection_log_failure(db_path, monkeypatch):
    async def noop():
        return None

    def boom(*a, **k):
        raise RuntimeError("log down")

    monkeypatch.setattr(fpl_fetcher, "fetch_bootstrap", noop)
    monkeypatch.setattr(fpl_fetcher, "fetch_fixtures", noop)
    monkeypatch.setattr(fpl_fetcher, "sync_gw_history", noop)
    monkeypatch.setattr(projlog, "log_projections", boom)
    asyncio.run(fpl_fetcher.refresh_all_fpl())        # must not raise


def test_rollover_wipes_projection_log(db_path):
    projlog.log_projections(ConfigFile(), now=BEFORE)
    assert _rows()
    assert dbmod.check_season_rollover("2027/28") is True
    assert dbmod.query("SELECT COUNT(*) AS n FROM projection_log")[0]["n"] == 0
