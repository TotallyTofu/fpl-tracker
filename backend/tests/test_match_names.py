"""Name-matching tests (PLAN-2 T1.16)."""
from app.db import execute
from app.signals.names import match_names


def test_exact_web_name(db_path):
    r = match_names(["Goal One"])
    assert len(r) == 1
    assert r[0]["matched"] is not None
    assert r[0]["matched"]["player_id"] == 1
    assert r[0]["matched"]["confidence"] == 1.0


def test_case_insensitive(db_path):
    r = match_names(["goal one"])
    assert r[0]["matched"] is not None
    assert r[0]["matched"]["player_id"] == 1


def test_known_name(db_path):
    execute("UPDATE players SET known_name = 'Haaland' WHERE id = 19")
    r = match_names(["Haaland"])
    assert r[0]["matched"] is not None
    assert r[0]["matched"]["player_id"] == 19
    assert r[0]["matched"]["confidence"] == 0.95


def test_single_typo_candidate_auto_matches(db_path):
    # "Saka Junor" → surname "junor" vs "junior" (player 25) is the only near hit
    r = match_names(["Saka Junor"])
    assert r[0]["matched"] is not None
    assert r[0]["matched"]["player_id"] == 25
    assert 0 < r[0]["matched"]["confidence"] < 1.0


def test_ambiguous_no_auto_match(db_path):
    # "Def A On" is one char off "… One" for several players → ambiguous
    r = match_names(["Def A On"])
    assert r[0]["matched"] is None
    assert len(r[0]["candidates"]) >= 2


def test_no_match(db_path):
    r = match_names(["Zzz Qqq"])
    assert r[0]["matched"] is None
    assert r[0]["candidates"] == []


def test_multiple_inputs(db_path):
    r = match_names(["Goal One", "Fwd C One", "Nobody Here"])
    assert [m["matched"]["player_id"] if m["matched"] else None for m in r] == [1, 23, None]