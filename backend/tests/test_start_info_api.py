"""v1.1 T4: p_start / last_match / start_rate5 in the lineup and suggestion payloads."""
import pytest
from fastapi.testclient import TestClient
from test_api import VALID_BODY

from app import db as dbmod
from app.main import create_app


@pytest.fixture()
def client(db_path):
    return TestClient(create_app())


@pytest.fixture()
def client_hist(with_history):
    return TestClient(create_app())


def _players(lineup):
    return {p["player_id"]: p for p in lineup["players"]}


def test_lineup_players_carry_start_info(client_hist):
    lid = client_hist.post("/api/lineups", json=VALID_BODY).json()["id"]
    got = _players(client_hist.get(f"/api/lineups/{lid}").json())
    # player 19 (Fwd A One): started every one of his last matches, started last match
    assert got[19]["p_start"] == 0.88
    assert got[19]["last_match"] == "started"
    assert got[19]["start_rate5"] == 1.0
    # players without history: the keys exist and are None
    assert got[1]["p_start"] is None and got[1]["last_match"] is None
    assert got[1]["start_rate5"] is None


def test_lineup_without_any_history_still_loads(client):
    lid = client.post("/api/lineups", json=VALID_BODY).json()["id"]
    got = client.get(f"/api/lineups/{lid}").json()
    assert all(p["p_start"] is None for p in got["players"])


def test_lineup_flagged_and_unavailable_players(client_hist):
    dbmod.execute("UPDATE players SET chance_of_playing_next_round = 75, status = 'd' WHERE id = 19")
    dbmod.execute("UPDATE players SET status = 'u', can_select = 0 WHERE id = 23")
    lid = client_hist.post("/api/lineups", json=VALID_BODY).json()["id"]
    got = _players(client_hist.get(f"/api/lineups/{lid}").json())
    assert got[19]["p_start"] == round(0.88 * 0.60, 2)           # flag factor at 75%
    assert got[23]["p_start"] == 0.0                              # hard gate, no history needed


def test_suggestion_squad_players_carry_start_info(client_hist):
    lid = client_hist.post("/api/lineups", json=VALID_BODY).json()["id"]
    r = client_hist.post("/api/suggestions/generate", json={"lineup_id": lid})
    assert r.status_code == 200, r.text
    for s in r.json()["suggestions"]:
        for p in s["lineup"]["squad"]:
            assert {"p_start", "last_match", "start_rate5"} <= set(p)
            if p["player_id"] == 19:
                assert p["p_start"] == 0.88 and p["last_match"] == "started"
            if p["player_id"] not in (11, 12, 19):
                assert p["p_start"] is None
    # the stored copy (GET /suggestions) has them too
    stored = client_hist.get("/api/suggestions", params={"lineup_id": lid}).json()["suggestions"]
    assert all("p_start" in p for p in stored[0]["lineup"]["squad"])


def test_generate_without_history_matches_v1(client):
    """No history at all → every projection is the T1 value (minutes model idle)."""
    lid = client.post("/api/lineups", json=VALID_BODY).json()["id"]
    r = client.post("/api/suggestions/generate", json={"lineup_id": lid})
    assert r.status_code == 200, r.text
    for s in r.json()["suggestions"]:
        assert all(p["p_start"] is None for p in s["lineup"]["squad"])
