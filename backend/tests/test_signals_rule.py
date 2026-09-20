"""Rule extractor tests (PLAN-3 T2.7 / T2.12): keywords, no-fuzzy, age skip,
YouTube description-only, official-news diff (incl. cold start)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import db as dbmod
from app.signals import names as names_mod
from app.signals import rule_extractor
from app.signals.rule_extractor import (
    MAX_ITEM_AGE_DAYS,
    extract_signals_rule,
    ingest_official_news,
)

NOW = "2026-09-19T12:00:00Z"


def _idx(db_path):
    return names_mod.name_index(
        dbmod.query(
            """SELECT p.id, p.web_name, p.known_name, p.first_name, p.second_name,
                      p.team, t.name AS team_name
               FROM players p LEFT JOIN teams t ON t.id = p.team
               WHERE p.removed = 0"""
        )
    )


def _item(title, body, source="bbc", pub=NOW, ext="x1", url="http://example.com/a"):
    return {
        "id": 1,
        "source": source,
        "external_id": ext,
        "title": title,
        "body": body,
        "url": url,
        "published_at": pub,
    }


def test_injury_keyword(db_path):
    idx = _idx(db_path)
    item = _item("Goal One ruled out", "Goal One is ruled out with a hamstring injury.")
    sigs = extract_signals_rule(item, idx)
    assert sigs, "expected an injury signal"
    s = next(x for x in sigs if x["player_id"] == 1)
    assert s["category"] == "injury"
    assert s["sentiment"] == "negative"
    assert s["confidence"] <= 0.6  # rule signals never exceed 0.6
    assert s["model"] == "rules"
    assert s["source"] == "bbc:x1"


def test_doubt_keyword(db_path):
    idx = _idx(db_path)
    item = _item("Doubt over Def A One", "Def A One is a doubt for the match.")
    sigs = extract_signals_rule(item, idx)
    s = next(x for x in sigs if x["player_id"] == 3)
    assert s["category"] == "injury"  # 'doubt' pattern lives under injury
    assert s["sentiment"] == "negative"


def test_no_fuzzy_matching(db_path):
    """'Gol One' (typo) must NOT resolve — rule path is exact-only (V9)."""
    idx = _idx(db_path)
    item = _item("Gol One injured", "Gol One is injured and will miss the game.")
    assert extract_signals_rule(item, idx) == []


def test_unmatched_names_dropped(db_path):
    idx = _idx(db_path)
    item = _item("Zed Qwerty injury", "Zed Qwerty is injured. Goal One is fit to play.")
    sigs = extract_signals_rule(item, idx)
    assert all(s["player_id"] == 1 for s in sigs)  # only the matched player
    assert sigs and sigs[0]["category"] == "return"  # 'fit to play' → return


def test_age_skip(db_path):
    idx = _idx(db_path)
    old = (datetime.now(timezone.utc) - timedelta(days=MAX_ITEM_AGE_DAYS + 1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    item = _item("Goal One injury", "Goal One is injured.", pub=old)
    assert extract_signals_rule(item, idx) == []


def test_youtube_transcript_ignored_on_rule_path(db_path):
    idx = _idx(db_path)
    body = "Def A One is a doubt with a knock.\n\n--- TRANSCRIPT ---\nGoal One is injured."
    item = _item("Def A One news", body, source="youtube")
    sigs = extract_signals_rule(item, idx)
    pids = {s["player_id"] for s in sigs}
    assert pids == {3}  # transcript (player 1) ignored on the rule path


def test_one_signal_per_player_category(db_path):
    idx = _idx(db_path)
    body = "Goal One is injured. Goal One may also be out for weeks with a hamstring injury."
    item = _item("Goal One news", body)
    sigs = extract_signals_rule(item, idx)
    mine = [s for s in sigs if s["player_id"] == 1 and s["category"] == "injury"]
    assert len(mine) == 1
    assert mine[0]["confidence"] == max(s["confidence"] for s in mine)


def test_official_news_cold_start_seeds_cache(db_path):
    players = dbmod.query(
        "SELECT id, web_name, status, news, news_added, chance_of_playing_next_round FROM players"
    )
    assert ingest_official_news(players) == []  # cold start: no emission
    cached = dbmod.query("SELECT COUNT(*) AS n FROM official_news_cache")
    assert cached[0]["n"] == len(players)


def test_official_news_status_change(db_path):
    players = dbmod.query(
        "SELECT id, web_name, status, news, news_added, chance_of_playing_next_round FROM players"
    )
    ingest_official_news(players)  # seed cache
    # now player 3 goes injured
    changed = [dict(p) for p in players]
    for p in changed:
        if p["id"] == 3:
            p["status"] = "i"
            p["news"] = "Hamstring injury, likely to miss several weeks."
    sigs = ingest_official_news(changed)
    mine = [s for s in sigs if s["player_id"] == 3]
    assert mine, "expected an official injury signal"
    s = next(x for x in mine if x["category"] == "injury")
    assert s["confidence"] == 1.0
    assert s["source"] == "fpl-official"
    assert s["sentiment"] == "negative"


def test_official_news_return(db_path):
    players = dbmod.query(
        "SELECT id, web_name, status, news, news_added, chance_of_playing_next_round FROM players"
    )
    ingest_official_news(players)  # seed cache
    # player 5 was seeded as 'a'; simulate prior injury then return
    dbmod.execute(
        "UPDATE official_news_cache SET status = 'i', news = 'Injury' WHERE player_id = 5"
    )
    sigs = ingest_official_news(players)
    mine = [s for s in sigs if s["player_id"] == 5]
    assert any(s["category"] == "return" and s["sentiment"] == "positive" for s in mine)


def test_official_news_no_change_no_signals(db_path):
    players = dbmod.query(
        "SELECT id, web_name, status, news, news_added, chance_of_playing_next_round FROM players"
    )
    ingest_official_news(players)
    assert ingest_official_news(players) == []


def test_official_news_chance_zero(db_path):
    players = dbmod.query(
        "SELECT id, web_name, status, news, news_added, chance_of_playing_next_round FROM players"
    )
    ingest_official_news(players)  # seed (all chance=100)
    changed = [dict(p) for p in players]
    for p in changed:
        if p["id"] == 7:
            p["chance_of_playing_next_round"] = 0
    sigs = ingest_official_news(changed)
    mine = [s for s in sigs if s["player_id"] == 7]
    assert any(s["category"] == "selection" and s["sentiment"] == "negative" for s in mine)