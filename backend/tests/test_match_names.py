"""Name-matching tests (PLAN-2 T1.16 + FIX.MD Part 2 §18.4)."""
import pytest

from app.db import execute
from app.signals import names as names_mod
from app.signals.names import match_names, name_index, resolve_candidates


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


# --- FIX N6: aliases + unambiguous-surname tier ----------------------------------


def _idx(db_path) -> dict:
    return name_index(
        execute_fresh_rows(db_path)
    )


def execute_fresh_rows(db_path):
    from app.db import query
    return query(
        """SELECT p.id, p.web_name, p.known_name, p.first_name, p.second_name,
                  p.team, t.name AS team_name
           FROM players p LEFT JOIN teams t ON t.id = p.team
           WHERE p.removed = 0"""
    )


def test_alias_resolves_in_scan_index(db_path, monkeypatch):
    """FIX N6: community nickname forms resolve via the ALIASES map (score
    0.9, same tier as misspellings) instead of producing zero candidates."""
    monkeypatch.setitem(names_mod.ALIASES, "goal gem", "Goal One")
    monkeypatch.setattr(names_mod, "_index_cache", {"key": None, "idx": None})
    idx = names_mod.get_name_index()
    hits = resolve_candidates("Goal gem is injured and out.", idx)
    assert hits and hits[0]["player_id"] == 1
    assert hits[0]["score"] == 0.9


def test_ambiguous_surname_yields_no_candidates(db_path, monkeypatch):
    """FIX N6 ambiguity guard: a surname shared by 2+ players is excluded
    from the scan regex's surname tier."""
    monkeypatch.setattr(names_mod, "_index_cache", {"key": None, "idx": None})
    idx = names_mod.get_name_index()
    # 'one' is the surname token of many conftest players ('Goal One', 'Def A
# One', …) — 2+ players → excluded from the surname tier
    assert len(idx["by_surname"].get("one", [])) >= 2
    assert "one" not in idx["surnames"]                 # excluded from the tier
    assert resolve_candidates("One is a doubt for the match.", idx) == []


def test_unambiguous_surname_resolves_at_lower_score(db_path, monkeypatch):
    """FIX N6: a surname mapping to exactly ONE player joins the scan tier at
    score 0.8 — e.g. 'junior' (only 'Saka Junior' carries it)."""
    monkeypatch.setattr(names_mod, "_index_cache", {"key": None, "idx": None})
    idx = names_mod.get_name_index()
    assert idx["surnames"].get("junior") == 25
    hits = resolve_candidates("Junior is expected to start.", idx)
    assert hits and hits[0]["player_id"] == 25
    assert hits[0]["score"] == 0.8


def test_alias_fold_in_is_a_noop_for_missing_targets(db_path, monkeypatch):
    """FIX N6: aliases pointing at players not in this season's list (the
    spec's dormant 'mo salah' example) simply create no pattern."""
    monkeypatch.setattr(names_mod, "_index_cache", {"key": None, "idx": None})
    idx = names_mod.get_name_index()
    assert resolve_candidates("Mo Salah is injured.", idx) == []


# --- FIX.MD A2 / §10.2: one normaliser for both sides ---------------------------

# Synthetic players mirroring the live-pool spellings the old normaliser broke
# (ids match the live data/fpl.db so the §10.2 table stays copy-pasteable).
# Fixture player 15 ("Mid C One") is displaced by Ødegaard to mirror the live id.
A2_PLAYERS = [
    # (id, web_name, known_name, element_type, team, now_cost, ep_next, selected_by_percent, status)
    (124, "Groß", "Pascal Groß", 3, 3, 55, 10.0, 40.0, "a"),
    (15, "Ødegaard", "Martin Ødegaard", 3, 1, 60, 11.0, 40.0, "a"),
    (346, "Calvert-Lewin", "Dominic Calvert-Lewin", 4, 1, 50, 8.0, 30.0, "a"),
    (426, "B.Fernandes", "Bruno Fernandes", 3, 2, 55, 9.0, 35.0, "a"),
    (126, "O'Riley", "Matt O'Riley", 3, 1, 45, 7.0, 20.0, "a"),
    (400, "João Pedro", "João Pedro", 4, 1, 70, 10.0, 25.0, "a"),
]


def _insert_a2_players(db_path):
    from app import db as dbmod

    dbmod.execute("DELETE FROM players WHERE id = 15")  # displaced by live Ødegaard id
    dbmod.execute_many(
        "INSERT INTO players (id, web_name, known_name, element_type, team, now_cost, ep_next, "
        "selected_by_percent, status, can_select, removed, chance_of_playing_next_round, form, "
        "fetched_at) VALUES (?,?,?,?,?,?,?,?,?,1,0,100,5.0,?)",
        [(p[0], p[1], p[2], p[3], p[4], p[5], p[6], p[7], p[8], "2026-09-19T12:00:00Z")
         for p in A2_PLAYERS],
    )


@pytest.mark.parametrize("text, want_id", [
    ("Calvert-Lewin is fit again for Everton.", 346),
    ("Pascal Gross was excellent for Brighton.", 124),
    ("Pascal Groß is the most-transferred in player", 124),
    ("Odegaard returned to Arsenal training.", 15),
    ("Ødegaard returned to Arsenal training.", 15),
    ("B.Fernandes scored for Manchester United.", 426),
    ("O'Riley started for Brighton.", 126),         # apostrophe class (rev 2)
    ("João Pedro starts up front.", None),          # assert it does NOT become another player
])
def test_resolve_candidates_real_world_forms(db_path, monkeypatch, text, want_id):
    _insert_a2_players(db_path)
    monkeypatch.setattr(names_mod, "_index_cache", {"key": None, "idx": None})
    idx = names_mod.get_name_index()
    hits = names_mod.resolve_candidates(text, idx)
    got = {h["player_id"] for h in hits}
    if want_id is None:
        assert got <= {400}  # the intended player at most — never someone else
    else:
        assert got == {want_id}


# --- FIX.MD A19 / A9.1: scan precision (common words + ambiguous full names) ----
# Synthetic pool mirroring the live-data cases from the spec (ids kept so the
# examples stay copy-pasteable). Built directly — no DB round-trip needed.

A19_PLAYERS = [
    {"id": 10, "web_name": "White", "known_name": "Ben White",
     "first_name": "Benjamin", "second_name": "White", "team_name": "Arsenal"},
    {"id": 76, "web_name": "Tóth.A", "known_name": "Alex Toth",
     "first_name": "Alex", "second_name": "Tóth", "team_name": "Bournemouth"},
    {"id": 108, "web_name": "Wilson", "known_name": "",
     "first_name": "Callum", "second_name": "Wilson", "team_name": "Brentford"},
    {"id": 172, "web_name": "Wilson", "known_name": "",
     "first_name": "Ben", "second_name": "Wilson", "team_name": "Coventry City"},
    {"id": 260, "web_name": "Wilson", "known_name": "",
     "first_name": "Harry", "second_name": "Wilson", "team_name": "Leeds"},
    {"id": 124, "web_name": "Groß", "known_name": "",
     "first_name": "Pascal", "second_name": "Groß", "team_name": "Brighton"},
    {"id": 379, "web_name": "Isak", "known_name": "",
     "first_name": "Alexander", "second_name": "Isak", "team_name": "Newcastle"},
    {"id": 442, "web_name": "Pope", "known_name": "",
     "first_name": "Nick", "second_name": "Pope", "team_name": "Newcastle"},
    # A19 rev 2: a common word SHARED by two players — the anchor case.
    {"id": 490, "web_name": "Wood", "known_name": "",
     "first_name": "Chris", "second_name": "Wood", "team_name": "Nottingham Forest"},
    {"id": 491, "web_name": "Wood", "known_name": "",
     "first_name": "Bobby", "second_name": "Wood", "team_name": "Leeds"},
]


def _a19_idx() -> dict:
    return name_index(A19_PLAYERS)


def test_a19_lowercase_common_word_never_matches():
    # not used as a proper noun → never a name, even with a club anchor present
    assert resolve_candidates("the white shirt is clean for Arsenal", _a19_idx()) == []


def test_a19_proper_noun_unique_owner_resolves_without_a_club():
    # A19 rev 2 calibration: this is the headline form that matters. Requiring a
    # club mention dropped "Pope out for a month with a knee injury" (and "White
    # a doubt for the weekend", "[Team News] Barnes benched") — real signals, in
    # a corpus that is already PL/FPL-only, for a name only one player owns.
    hits = resolve_candidates("Pope out for a month with a knee injury.", _a19_idx())
    assert {h["player_id"] for h in hits} == {442}


def test_a19_all_caps_headline_resolves():
    # Reddit/team-news headlines are often ALL CAPS — still a proper noun.
    hits = resolve_candidates("POPE OUT FOR A MONTH", _a19_idx())
    assert {h["player_id"] for h in hits} == {442}


def test_a19_common_word_with_club_anchor_matches():
    hits = resolve_candidates("Ben White will start for Arsenal", _a19_idx())
    assert {h["player_id"] for h in hits} == {10}


def test_a19_shared_common_word_needs_an_anchor():
    # 'Wood' is owned by two players: the bare form must not guess…
    assert resolve_candidates("Wood is a doubt for the weekend.", _a19_idx()) == []
    # …and a club in the same sentence picks the right one.
    hits = resolve_candidates("Wood is a doubt for Nottingham Forest.", _a19_idx())
    assert {h["player_id"] for h in hits} == {490}


def test_a19_first_name_token_is_not_in_the_surname_tier():
    # A19.3 (rev 2): 'alex' is Tóth.A's FIRST name (via known_name "Alex Toth").
    # It used to join the surname tier, so "Alex is a doubt" resolved to him.
    idx = _a19_idx()
    assert "alex" not in idx["by_surname"]
    assert idx["by_surname"].get("toth") == [76]
    assert resolve_candidates("Alex is a doubt for the weekend.", idx) == []


def test_a19_non_common_word_single_name_still_resolves():
    # the gate must not eat recall for ordinary single-token names
    hits = resolve_candidates("Isak scores a late winner.", _a19_idx())
    assert {h["player_id"] for h in hits} == {379}


def test_a91_ambiguous_full_name_is_no_match_without_context():
    # A9.1: three players are named Wilson — a bare "Wilson" must not guess
    idx = _a19_idx()
    assert "wilson" in idx["ambiguous"]
    assert "white" not in idx["ambiguous"]
    assert resolve_candidates("Wilson", idx) == []


def test_a91_neighbouring_first_name_disambiguates():
    hits = resolve_candidates("Callum Wilson", _a19_idx())
    assert {h["player_id"] for h in hits} == {108}


def test_a91_club_disambiguates():
    hits = resolve_candidates("Wilson scores for Brentford", _a19_idx())
    assert {h["player_id"] for h in hits} == {108}


def test_a91_probe_style_self_consistency():
    # the §0.2 probe rule on the synthetic pool: a player's own web_name
    # resolves to that player — or to nobody when the name is genuinely
    # shared — but NEVER to a different player.
    idx = _a19_idx()
    shared = {k for k, v in idx["by_web"].items() if len(v) > 1}
    for r in A19_PLAYERS:
        hits = {h["player_id"] for h in resolve_candidates(r["web_name"], idx)}
        if hits:
            assert hits == {r["id"]}, (r["web_name"], hits)
        else:
            assert names_mod._flat(r["web_name"]) in shared