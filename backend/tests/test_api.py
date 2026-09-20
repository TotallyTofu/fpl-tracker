"""API integration tests against a temp DB (no network — lifespan not entered)."""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app

VALID_BODY = {
    "name": "Test XI",
    "transfer_bank": 3,
    "chips": {"wildcard": 2, "freehit": 2, "bboost": 2, "triple_captain": 2},
    "players": [
        {"player_id": 1, "role": "starter", "is_captain": True},
        {"player_id": 2, "role": "bench", "bench_order": 1},
        {"player_id": 3, "role": "starter"},
        {"player_id": 5, "role": "starter"},
        {"player_id": 7, "role": "starter"},
        {"player_id": 9, "role": "starter"},
        {"player_id": 8, "role": "bench", "bench_order": 2},
        {"player_id": 13, "role": "starter", "is_vice_captain": True},
        {"player_id": 15, "role": "starter"},
        {"player_id": 17, "role": "starter"},
        {"player_id": 18, "role": "bench", "bench_order": 3},
        {"player_id": 16, "role": "bench", "bench_order": 4},
        {"player_id": 19, "role": "starter"},
        {"player_id": 23, "role": "starter"},
        {"player_id": 24, "role": "starter"},
    ],
}


@pytest.fixture()
def client(db_path):
    return TestClient(create_app())


def test_meta_season(client):
    r = client.get("/api/meta/season")
    assert r.status_code == 200
    d = r.json()
    assert d["current_gw"] == 5
    assert d["next_gw"] == 6
    assert d["deadline"] == "2026-09-27T10:00:00Z"
    assert len(d["fixtures_next_gw"]) == 2
    assert d["fixtures_next_gw"][0]["home_name"] == "Alpha FC"
    chips = {w["chip"] for w in d["chip_windows"]}
    assert {"wildcard", "freehit", "bboost", "triple_captain"} <= chips


def test_meta_health(client):
    r = client.get("/api/meta/health")
    assert r.status_code == 200
    d = r.json()
    assert d["db_ok"] is True
    assert "last_polls" in d
    assert "schema_drift" in d


def test_players_search(client):
    r = client.get("/api/players", params={"search": "goal"})
    assert r.status_code == 200
    names = {p["web_name"] for p in r.json()["players"]}
    assert {"Goal One", "Goal Two"} <= names


def test_players_filters(client):
    r = client.get("/api/players", params={"pos": 4, "team": 3})
    assert r.status_code == 200
    for p in r.json()["players"]:
        assert p["element_type"] == 4
        assert p["team"] == 3


def test_create_and_get_lineup(client):
    r = client.post("/api/lineups", json=VALID_BODY)
    assert r.status_code == 201, r.text
    lid = r.json()["id"]
    assert r.json()["validation"]["valid"] is True

    r = client.get(f"/api/lineups/{lid}")
    assert r.status_code == 200
    d = r.json()
    assert len(d["players"]) == 15
    assert d["budget_remaining"] == 1000 - 805
    assert any(p["is_captain"] for p in d["players"])


def test_list_and_set_current(client):
    lid = client.post("/api/lineups", json=VALID_BODY).json()["id"]
    r = client.post(f"/api/lineups/{lid}/set-current")
    assert r.status_code == 200
    r = client.get("/api/lineups")
    assert any(l["id"] == lid and l["is_current"] for l in r.json()["lineups"])


def test_update_lineup(client):
    lid = client.post("/api/lineups", json=VALID_BODY).json()["id"]
    body = dict(VALID_BODY, name="Renamed")
    r = client.put(f"/api/lineups/{lid}", json=body)
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed"


def test_delete_lineup(client):
    lid = client.post("/api/lineups", json=VALID_BODY).json()["id"]
    r = client.delete(f"/api/lineups/{lid}")
    assert r.status_code == 200
    assert client.get(f"/api/lineups/{lid}").status_code == 404


def test_invalid_lineup_422_with_errors(client):
    body = dict(VALID_BODY)
    body["players"] = [p for p in VALID_BODY["players"] if p["player_id"] != 24]  # 14 players
    r = client.post("/api/lineups", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert any(e["code"] == "SQUAD_SIZE" for e in detail)


def test_unknown_player_422(client):
    body = dict(VALID_BODY)
    body["players"] = [dict(p, player_id=9999) for p in VALID_BODY["players"]]
    r = client.post("/api/lineups", json=body)
    assert r.status_code == 422


def test_match_names_endpoint(client):
    r = client.post("/api/lineups/match-names", json={"names": ["Goal One", "Zzz Qqq"]})
    assert r.status_code == 200
    matches = r.json()["matches"]
    assert matches[0]["matched"]["player_id"] == 1
    assert matches[1]["matched"] is None


def test_settings_get(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    d = r.json()
    assert "weights" in d["optimizer"] or "llm" in d


def test_refresh_unknown_source_404(client):
    r = client.post("/api/refresh/nope")
    assert r.status_code == 404