"""Season rollover tests (FIX.MD A10)."""
import asyncio

from app import db as dbmod

NOW = "2026-09-19T12:00:00Z"

SEASON_TABLES = (
    "players", "fixtures", "events", "chips",
    "lineups", "lineup_players", "suggestions",
    "live_matches", "live_player_points", "official_news_cache",
    "signals", "raw_items", "chip_plays_log",
)


def test_rollover_with_lineups_succeeds(db_path):
    """A10: with live lineups/lineup_players/live_matches present, rollover must
    succeed (children deleted before parents under FK on) and wipe everything
    season-scoped, including the chip-play log."""
    dbmod.execute(
        "INSERT INTO lineups (name, transfer_bank, chips, is_current, kind, created_at, updated_at) "
        "VALUES ('Test XI', 3, '{}', 1, 'current', ?, ?)", (NOW, NOW))
    dbmod.execute(
        "INSERT INTO lineup_players (lineup_id, player_id, role) VALUES (1, 1, 'starter')")
    dbmod.execute(
        "INSERT INTO live_matches (fixture_id, event, kickoff_time, status, updated_at) "
        "VALUES (1, 6, '2026-09-27T15:00:00Z', 'pre', ?)", (NOW,))
    dbmod.execute(
        "INSERT INTO chip_plays_log (lineup_id, gw, chip, played_at) VALUES (1, 5, 'freehit', ?)",
        (NOW,))
    dbmod.execute(
        "INSERT INTO signals (player_id, category, sentiment, confidence, summary, source, "
        "retrieved_at, expires_at) VALUES (1, 'injury', 'negative', 0.8, 't', 'fpl-official', ?, ?)",
        (NOW, NOW))

    assert dbmod.check_season_rollover("2026/27") is True
    assert dbmod.get_meta("season") == "2026/27"
    for table in SEASON_TABLES:
        assert dbmod.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"] == 0, table


def test_bootstrap_survives_rollover_with_saved_lineup(db_path, monkeypatch):
    """FIX.MD §10.3 integration guard: the wipe runs INSIDE fetch_bootstrap(), so
    an exception there aborts the whole FPL refresh (no players/fixtures/events,
    every scheduled poll failing) — the app limps on last season's data. The
    call site must therefore swallow a rollover failure, and the successful path
    must repopulate the season tables with normalised chip names."""
    dbmod.execute(
        "INSERT INTO lineups (name, transfer_bank, chips, is_current, kind, created_at, updated_at) "
        "VALUES ('Real', 3, '{}', 1, 'current', ?, ?)", (NOW, NOW))
    dbmod.execute(
        "INSERT INTO lineup_players (lineup_id, player_id, role) VALUES (1, 1, 'starter')")
    dbmod.set_meta("season", "2025/26")

    from app.fetchers import fpl as fpl_fetcher

    async def fake_get_json(url, **kw):
        return {
            "elements": [], "teams": [], "game_settings": {},
            # GW1 deadline in 2027 → marker "2027/28", i.e. a season change
            "events": [{"id": 1, "deadline_time": "2027-08-14T00:00:00Z"}],
            "chips": [{"id": 1, "name": "3xc", "number": 1, "start_event": 1,
                       "stop_event": 19, "chip_type": "team"}],
        }

    monkeypatch.setattr(fpl_fetcher.http, "get_json", fake_get_json)
    asyncio.run(fpl_fetcher.fetch_bootstrap())

    assert dbmod.get_meta("season") == "2027/28"
    assert dbmod.query_one("SELECT COUNT(*) AS n FROM lineups")["n"] == 0
    assert [r["name"] for r in dbmod.query("SELECT name FROM chips")] == ["triple_captain"]