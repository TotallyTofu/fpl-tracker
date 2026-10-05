"""Entry API + fetcher tests (T4.4, PLAN.MD §8.8) — TestClient, no network."""
from __future__ import annotations

import asyncio
import types

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import entry as entry_api
from app.fetchers import fpl as fpl_mod
from app.main import create_app

SAMPLE = {
    "id": 42,
    "name": "Test Team",
    "summary_overall_points": 412,
    "summary_overall_rank": 154321,
    "leagues": {
        "classic": [
            {"id": 14, "name": "Global", "class": None, "league_type": "s",
             "rank": 154321, "rank_count": 1297720, "total": 412,
             "entry_percentile_rank": 12, "active_phases": []},
            {"id": 999, "name": "Work Mini", "class": None, "league_type": "x",
             "rank": 2, "rank_count": 20, "total": 412,
             "entry_percentile_rank": 10, "active_phases": []},
        ],
        "h2h": [],
        "cup": {"matches": []},
    },
}


@pytest.fixture(autouse=True)
def _clean_entry_cache():
    fpl_mod.clear_entry_cache()
    yield
    fpl_mod.clear_entry_cache()


def _settings_cfg(entry_id: str = ""):
    return types.SimpleNamespace(group=types.SimpleNamespace(fpl_entry_id=entry_id))


@pytest.fixture()
def client():
    return TestClient(create_app())


# ---- API layer --------------------------------------------------------------


def test_entry_unset_returns_400(client, monkeypatch):
    monkeypatch.setattr(entry_api, "load_settings",
                        lambda: types.SimpleNamespace(config=_settings_cfg("")))
    r = client.get("/api/entry")
    assert r.status_code == 400
    assert "not set" in r.json()["detail"]


def test_entry_non_numeric_returns_400(client, monkeypatch):
    monkeypatch.setattr(entry_api, "load_settings",
                        lambda: types.SimpleNamespace(config=_settings_cfg("abc")))
    r = client.get("/api/entry")
    assert r.status_code == 400
    assert "numeric" in r.json()["detail"]


def test_entry_ok(client, monkeypatch):
    calls: list[str] = []

    async def fake_fetch(entry_id, force=False):
        calls.append(entry_id)
        return fpl_mod._normalize_entry(SAMPLE)

    monkeypatch.setattr(entry_api, "load_settings",
                        lambda: types.SimpleNamespace(config=_settings_cfg("42")))
    monkeypatch.setattr(fpl_mod, "fetch_entry", fake_fetch)
    r = client.get("/api/entry")
    assert r.status_code == 200
    body = r.json()
    assert body["entry_id"] == 42
    assert body["overall_points"] == 412
    assert body["overall_rank"] == 154321
    assert body["overall_rank_out_of"] == 1297720
    assert body["overall_percentile"] == round(154321 / 1297720 * 100, 1)
    assert [lg["league_type"] for lg in body["leagues"]] == ["s", "x"]
    assert calls == ["42"]


def test_entry_not_found_returns_400(client, monkeypatch):
    async def fake_fetch(entry_id, force=False):
        return None

    monkeypatch.setattr(entry_api, "load_settings",
                        lambda: types.SimpleNamespace(config=_settings_cfg("999999999999")))
    monkeypatch.setattr(fpl_mod, "fetch_entry", fake_fetch)
    r = client.get("/api/entry")
    assert r.status_code == 400
    assert "not found" in r.json()["detail"]


def test_entry_upstream_error_returns_502(client, monkeypatch):
    async def fake_fetch(entry_id, force=False):
        raise RuntimeError("boom")

    monkeypatch.setattr(entry_api, "load_settings",
                        lambda: types.SimpleNamespace(config=_settings_cfg("42")))
    monkeypatch.setattr(fpl_mod, "fetch_entry", fake_fetch)
    r = client.get("/api/entry")
    assert r.status_code == 502


# ---- fetcher level ----------------------------------------------------------


def test_fetch_entry_normalizes_and_caches(monkeypatch, db_path):
    from app import db as dbmod

    calls: list[str] = []

    async def fake_get_json(url, **kw):
        calls.append(url)
        return SAMPLE

    monkeypatch.setattr(fpl_mod.http, "get_json", fake_get_json)
    first = asyncio.run(fpl_mod.fetch_entry("42"))
    second = asyncio.run(fpl_mod.fetch_entry("42"))
    assert first is second
    assert calls == ["https://fantasy.premierleague.com/api/entry/42/"]
    assert first["entry_id"] == 42
    polls = dbmod.query("SELECT status FROM poll_log WHERE source = 'entry'")
    assert [p["status"] for p in polls] == ["ok"]


def test_fetch_entry_404_returns_none(monkeypatch, db_path):
    async def fake_get_json(url, **kw):
        req = httpx.Request("GET", url)
        resp = httpx.Response(404, request=req, json={"detail": "No Entry matches the given query."})
        raise httpx.HTTPStatusError("404", request=req, response=resp)

    monkeypatch.setattr(fpl_mod.http, "get_json", fake_get_json)
    assert asyncio.run(fpl_mod.fetch_entry("999999999999")) is None


def test_fetch_entry_force_bypasses_cache(monkeypatch, db_path):
    calls: list[str] = []

    async def fake_get_json(url, **kw):
        calls.append(url)
        return SAMPLE

    monkeypatch.setattr(fpl_mod.http, "get_json", fake_get_json)
    asyncio.run(fpl_mod.fetch_entry("42"))
    asyncio.run(fpl_mod.fetch_entry("42", force=True))
    assert len(calls) == 2


# ---- scheduler ---------------------------------------------------------------


def test_scheduler_registers_entry_rank_job():
    from app import scheduler as sched_mod

    sched = sched_mod.create_scheduler(app=None)
    job_ids = {j.id for j in sched.get_jobs()}
    assert "entry_rank" in job_ids


def test_entry_rank_job_noop_without_id(monkeypatch):
    from app import scheduler as sched_mod

    called: list[str] = []

    async def fake_fetch(entry_id, force=False):
        called.append(entry_id)

    monkeypatch.setattr(sched_mod, "load_settings",
                        lambda: types.SimpleNamespace(config=_settings_cfg("")))
    monkeypatch.setattr(fpl_mod, "fetch_entry", fake_fetch)
    asyncio.run(sched_mod._job_entry_rank())
    assert called == []