"""v1.1 T2: per-gameweek history store (event/{gw}/live → player_gw_history).

No network: ``http.get_json`` is monkeypatched to serve a small fixture that
mirrors the real payload (elements[].stats / explain).
"""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

from app import db as dbmod
from app.fetchers import fpl as fpl_fetcher

FIX = Path(__file__).parent / "fixtures"
LIVE = json.loads((FIX / "sample_event_live.json").read_text(encoding="utf-8"))


def _rows(gw=None):
    sql = "SELECT * FROM player_gw_history"
    params: tuple = ()
    if gw is not None:
        sql += " WHERE gw = ?"
        params = (gw,)
    return dbmod.query(sql + " ORDER BY gw, player_id", params)


def _serve(monkeypatch, calls, fail_gw=(), payload=None):
    async def fake_get_json(url, **kw):
        calls.append(url)
        gw = int(url.rstrip("/").split("/")[-2])
        if gw in fail_gw:
            raise RuntimeError("boom")
        return payload if payload is not None else LIVE

    monkeypatch.setattr(fpl_fetcher.http, "get_json", fake_get_json)


def _gws_called(calls):
    return [int(u.rstrip("/").split("/")[-2]) for u in calls]


def _set_events(finished: dict[int, int]):
    """finished = {gw: data_checked}; replaces the fixture's events."""
    dbmod.execute("DELETE FROM events")
    for gw, checked in finished.items():
        dbmod.execute(
            "INSERT INTO events (id, name, deadline_time, is_current, is_next, finished, "
            "data_checked, released) VALUES (?,?,?,0,0,1,?,1)",
            (gw, f"Gameweek {gw}", "2026-09-01T10:00:00Z", checked))


def test_parse_rows_and_team_matches(db_path, monkeypatch):
    _serve(monkeypatch, [])
    n = asyncio.run(fpl_fetcher.fetch_gw_history(3, final=True))
    assert n == 5
    by_id = {r["player_id"]: r for r in _rows(3)}
    assert (by_id[1]["team_matches"], by_id[1]["minutes"], by_id[1]["starts"],
            by_id[1]["total_points"]) == (1, 90, 1, 6)
    assert (by_id[2]["team_matches"], by_id[2]["minutes"], by_id[2]["starts"]) == (1, 20, 0)
    assert (by_id[3]["team_matches"], by_id[3]["minutes"]) == (1, 0)       # unused, still 1 match
    assert (by_id[4]["team_matches"], by_id[4]["minutes"]) == (0, 0)       # blank GW
    assert (by_id[5]["team_matches"], by_id[5]["minutes"], by_id[5]["starts"]) == (2, 180, 2)
    assert all(r["final"] == 1 for r in by_id.values())


def test_minutes_without_explain_count_one_match(db_path, monkeypatch):
    """D18: minutes > 0 with an empty explain should not happen; count 1."""
    payload = copy.deepcopy(LIVE)
    payload["elements"][3]["stats"]["minutes"] = 30      # the blank-GW player
    _serve(monkeypatch, [], payload=payload)
    asyncio.run(fpl_fetcher.fetch_gw_history(3, final=False))
    assert {r["player_id"]: r for r in _rows(3)}[4]["team_matches"] == 1


def test_missing_starts_is_null_and_drift_logged_once(db_path, monkeypatch):
    payload = copy.deepcopy(LIVE)
    for e in payload["elements"]:
        e["stats"].pop("starts", None)
    _serve(monkeypatch, [], payload=payload)
    monkeypatch.setattr(fpl_fetcher, "_drift", [])
    asyncio.run(fpl_fetcher.fetch_gw_history(3, final=True))
    asyncio.run(fpl_fetcher.fetch_gw_history(4, final=True))
    assert all(r["starts"] is None for r in _rows())
    assert fpl_fetcher._drift == ["event/live.stats.starts"]


def test_upsert_overwrites_and_marks_final(db_path):
    dbmod.upsert_gw_history(2, [(1, 1, 45, 1, 3)], final=False)
    assert _rows(2)[0]["final"] == 0
    dbmod.upsert_gw_history(2, [(1, 1, 90, 1, 6)], final=True)
    rows = _rows(2)
    assert len(rows) == 1
    assert (rows[0]["minutes"], rows[0]["total_points"], rows[0]["final"]) == (90, 6, 1)


def test_sync_backfills_every_finished_gw_then_makes_no_calls(db_path, monkeypatch):
    _set_events({1: 1, 2: 1, 3: 1})
    calls: list[str] = []
    _serve(monkeypatch, calls)
    assert asyncio.run(fpl_fetcher.sync_gw_history()) == 15
    assert _gws_called(calls) == [1, 2, 3]
    assert {r["gw"] for r in _rows()} == {1, 2, 3}
    calls.clear()
    assert asyncio.run(fpl_fetcher.sync_gw_history()) == 0
    assert calls == []                                   # all final → 0 history calls


def test_sync_ignores_unfinished_gameweeks(db_path, monkeypatch):
    # the shared fixture has GW5 (current, unfinished) and GW6 (next)
    calls: list[str] = []
    _serve(monkeypatch, calls)
    assert asyncio.run(fpl_fetcher.sync_gw_history()) == 0
    assert calls == []


def test_sync_refetches_non_final_gw_until_data_checked(db_path, monkeypatch):
    _set_events({1: 1, 2: 0})
    calls: list[str] = []
    _serve(monkeypatch, calls)
    asyncio.run(fpl_fetcher.sync_gw_history())
    assert _gws_called(calls) == [1, 2]
    assert {r["final"] for r in _rows(1)} == {1}
    assert {r["final"] for r in _rows(2)} == {0}         # fetched before data_checked
    calls.clear()
    asyncio.run(fpl_fetcher.sync_gw_history())
    assert _gws_called(calls) == [2]                     # only GW2 again
    dbmod.execute("UPDATE events SET data_checked = 1 WHERE id = 2")
    calls.clear()
    asyncio.run(fpl_fetcher.sync_gw_history())
    assert _gws_called(calls) == [2]
    assert {r["final"] for r in _rows(2)} == {1}
    calls.clear()
    asyncio.run(fpl_fetcher.sync_gw_history())
    assert calls == []                                   # now final: never again


def test_sync_error_leaves_other_gameweeks_intact(db_path, monkeypatch):
    _set_events({1: 1, 2: 1, 3: 1})
    _serve(monkeypatch, [], fail_gw=(2,))
    assert asyncio.run(fpl_fetcher.sync_gw_history()) == 10
    assert {r["gw"] for r in _rows()} == {1, 3}
    poll = dbmod.query_one("SELECT status, error FROM poll_log WHERE source = 'fpl-history'")
    assert poll["status"] == "error" and "GW2" in poll["error"]
    # the failed GW is retried on the next sync
    calls: list[str] = []
    _serve(monkeypatch, calls)
    asyncio.run(fpl_fetcher.sync_gw_history())
    assert _gws_called(calls) == [2]
    assert {r["gw"] for r in _rows()} == {1, 2, 3}


def test_refresh_all_fpl_survives_history_failure(db_path, monkeypatch):
    async def noop():
        return None

    async def boom():
        raise RuntimeError("history down")

    monkeypatch.setattr(fpl_fetcher, "fetch_bootstrap", noop)
    monkeypatch.setattr(fpl_fetcher, "fetch_fixtures", noop)
    monkeypatch.setattr(fpl_fetcher, "sync_gw_history", boom)
    asyncio.run(fpl_fetcher.refresh_all_fpl())           # must not raise


def test_match_count_sanity_check_logs_mismatch(db_path, caplog):
    """GW6 fixtures: teams 1-2, 3-4, 5-6 → one match each. Player 1 (team 1)
    reported with 2 matches disagrees with the fixtures table; player 3 (team 1)
    with 1 match agrees."""
    rows = [(1, 2, 90, 1, 6), (3, 1, 90, 1, 5)]
    with caplog.at_level("WARNING", logger="fpl.fetch"):
        fpl_fetcher._check_history_matches(6, rows)
    assert "1 of 2 players" in caplog.text


def test_rollover_wipes_history(db_path):
    dbmod.upsert_gw_history(1, [(1, 1, 90, 1, 6)], final=True)
    assert dbmod.check_season_rollover("2027/28") is True
    assert _rows() == []
