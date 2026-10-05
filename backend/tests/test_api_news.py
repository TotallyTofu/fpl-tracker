"""News/signals API tests (PLAN-3 T2.11 / T2.12) — TestClient, no network."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.main import create_app
from app.signals import ingest, store

NOW = "2026-09-19T12:00:00Z"


@pytest.fixture()
def client(db_path):
    return TestClient(create_app())


def _seed(client):
    item_id = ingest.ingest_item(
        "bbc", "x1", "article", "Goal One ruled out", "http://example.com/a", NOW,
        "Goal One is ruled out with a hamstring injury.",
    )
    store.save_signals([
        {
            "player_id": 1, "category": "injury", "sentiment": "negative",
            "confidence": 0.5, "summary": "Goal One ruled out with hamstring injury.",
            "source": "bbc:x1", "url": "http://example.com/a", "published_at": NOW,
            "raw_item_id": item_id, "model": "rules",
        }
    ])
    return item_id


def test_signals_endpoint_empty(client):
    r = client.get("/api/signals")
    assert r.status_code == 200
    d = r.json()
    assert d == {"signals": [], "count": 0}


def test_signals_endpoint_returns_stored(client):
    _seed(client)
    r = client.get("/api/signals")
    assert r.status_code == 200
    d = r.json()
    assert d["count"] == 1
    s = d["signals"][0]
    assert s["player_id"] == 1
    assert s["web_name"] == "Goal One"
    assert s["category"] == "injury"
    assert s["expires_at"] > NOW


def test_signals_filters(client):
    _seed(client)
    store.save_signals([
        {"player_id": 3, "category": "selection", "sentiment": "negative",
         "confidence": 0.4, "summary": "Def A One a doubt.", "source": "espn:9",
         "url": None, "published_at": NOW, "raw_item_id": None, "model": "rules"},
    ])
    assert client.get("/api/signals", params={"player_id": 1}).json()["count"] == 1
    assert client.get("/api/signals", params={"category": "selection"}).json()["count"] == 1
    assert client.get("/api/signals", params={"player_id": 999}).json()["count"] == 0


def test_signals_inactive_team_filter(client):
    """A11: active=false + team filter used to build "WHERE  AND" → HTTP 500."""
    dbmod.execute("UPDATE teams SET short_name = 'ALP' WHERE id = 1")
    dbmod.execute("UPDATE teams SET short_name = 'BET' WHERE id = 2")
    store.save_signals([
        {"player_id": 1, "category": "injury", "sentiment": "negative",
         "confidence": 0.5, "summary": "Goal One ruled out.", "source": "bbc:x1",
         "url": None, "published_at": NOW, "raw_item_id": None, "model": "rules"},
        {"player_id": 5, "category": "selection", "sentiment": "negative",
         "confidence": 0.4, "summary": "Def B One a doubt.", "source": "espn:9",
         "url": None, "published_at": NOW, "raw_item_id": None, "model": "rules"},
    ])
    r = client.get("/api/signals", params={"active": False, "team": "ALP"})
    assert r.status_code == 200
    d = r.json()
    assert d["count"] == 1
    assert d["signals"][0]["player_id"] == 1
    # team + player_id + category combined
    r = client.get("/api/signals", params={"active": False, "team": "ALP",
                                           "player_id": 1, "category": "injury"})
    assert r.status_code == 200
    assert r.json()["count"] == 1
    # team + non-matching category → empty, still 200
    r = client.get("/api/signals", params={"active": False, "team": "ALP",
                                           "category": "selection"})
    assert r.status_code == 200
    assert r.json()["count"] == 0


def test_items_endpoint(client):
    _seed(client)
    r = client.get("/api/items")
    assert r.status_code == 200
    d = r.json()
    assert d["count"] == 1
    it = d["items"][0]
    assert it["source"] == "bbc"
    assert it["kind"] == "article"
    assert it["processed"] in (0, 1)
    assert isinstance(it["takeaways"], list)
    # source/kind filters
    assert client.get("/api/items", params={"source": "bbc"}).json()["count"] == 1
    assert client.get("/api/items", params={"source": "espn"}).json()["count"] == 0
    assert client.get("/api/items", params={"kind": "video"}).json()["count"] == 0


def test_fetch_body_unknown_item_404(client):
    r = client.post("/api/items/9999/fetch-body")
    assert r.status_code == 404


def test_fetch_body_non_bbc_404(client):
    ingest.ingest_item("reddit", "t3_x", "thread", "A thread", "http://r", NOW, "body")
    item_id = dbmod.query_one("SELECT id FROM raw_items WHERE source = 'reddit'")["id"]
    r = client.post(f"/api/items/{item_id}/fetch-body")
    assert r.status_code == 404


def test_fetch_body_no_url_404(client):
    item_id = ingest.ingest_item("bbc", "x2", "article", "No URL item", None, NOW, "b")
    r = client.post(f"/api/items/{item_id}/fetch-body")
    assert r.status_code == 404


def test_refresh_bbc_runs_pipeline(client, monkeypatch):
    """refresh/bbc calls the (patched) fetcher, then the signal pipeline."""
    from app.fetchers import bbc as bbc_mod
    from app.signals import pipeline as pipeline_mod

    async def fake_refresh():
        return {"status": "ok", "rows": 0, "filtered_out": 0}

    async def fake_pipeline(limit=50):
        fake_pipeline.called = True
        return 0

    fake_pipeline.called = False
    monkeypatch.setattr(bbc_mod, "refresh_bbc", fake_refresh)
    monkeypatch.setattr(pipeline_mod, "process_pending_items", fake_pipeline)
    r = client.post("/api/refresh/bbc")
    assert r.status_code == 200
    d = r.json()
    assert d["results"]["bbc"] == {"status": "ok", "rows": 0, "filtered_out": 0}
    assert fake_pipeline.called


def test_refresh_unknown_source_404(client):
    assert client.post("/api/refresh/nope").status_code == 404


def test_refresh_espn_disabled_skip(client, monkeypatch):
    """ESPN disabled in settings → refresh skips it without fetching."""
    import types

    from app.api import meta as meta_mod
    from app.config import ConfigFile

    monkeypatch.setattr(
        meta_mod, "load_settings",
        lambda: types.SimpleNamespace(config=ConfigFile()),  # espn.enabled=False default
    )
    r = client.post("/api/refresh/espn")
    assert r.status_code == 200
    assert "disabled" in str(r.json()["results"]["espn"])