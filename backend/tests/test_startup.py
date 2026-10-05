"""Startup full news refresh (M3 revised): state machine, resilience, endpoint.

No network: every fetcher + the signal pipeline are monkeypatched.
"""
from __future__ import annotations

import asyncio
import json
import types

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app import startup


def _settings(bbc: bool = True, espn: bool = False, reddit: bool = True, youtube: bool = True):
    return types.SimpleNamespace(
        config=types.SimpleNamespace(
            sources=types.SimpleNamespace(
                bbc=types.SimpleNamespace(enabled=bbc),
                espn=types.SimpleNamespace(enabled=espn),
                reddit=types.SimpleNamespace(enabled=reddit),
                youtube=types.SimpleNamespace(enabled=youtube, channels=[]),
            ),
            # FIX N8: the startup pass passes the extraction timebox through.
            llm=types.SimpleNamespace(extract_timebox_sec=480),
        )
    )


def _mock_fetchers(monkeypatch, results: dict, errors: dict | None = None):
    """Patch refresh_bbc/refresh_reddit/refresh_youtube to canned results/errors."""
    errors = errors or {}

    def make(name, res, err):
        async def _fn(*a, **k):
            if err:
                raise err
            return res

        _fn.__name__ = name
        return _fn

    import app.fetchers.bbc as bbc_mod
    import app.fetchers.reddit as reddit_mod
    import app.fetchers.youtube as yt_mod

    monkeypatch.setattr(bbc_mod, "refresh_bbc", make("refresh_bbc", results.get("bbc"), errors.get("bbc")))
    monkeypatch.setattr(reddit_mod, "refresh_reddit", make("refresh_reddit", results.get("reddit"), errors.get("reddit")))
    monkeypatch.setattr(yt_mod, "refresh_youtube", make("refresh_youtube", results.get("youtube"), errors.get("youtube")))


def test_idle_state_when_no_run(db_path):
    assert startup.get_startup_state() == {"status": "idle"}


def test_full_refresh_runs_enabled_sources_and_persists(db_path, monkeypatch):
    monkeypatch.setattr(startup, "load_settings", lambda: _settings())
    _mock_fetchers(
        monkeypatch,
        {"bbc": {"rows": 5, "filtered": 2}, "reddit": {"rows": 3}, "youtube": {"rows": 2}},
    )
    import app.signals.pipeline as pipe

    monkeypatch.setattr(pipe, "process_pending_items", _fake_pipeline(4))

    state = asyncio.run(startup.run_full_refresh())

    assert state["status"] == "done"
    assert state["started_at"] and state["finished_at"]
    assert state["results"]["bbc"] == {"rows": 5, "filtered": 2}
    assert state["results"]["reddit"] == {"rows": 3}
    assert state["results"]["youtube"] == {"rows": 2}
    assert state["results"]["espn"] == "skipped (espn disabled in settings)"
    assert state["signals_stored"] == 4
    # persisted in meta so the UI endpoint can read it
    assert startup.get_startup_state() == state


def test_refresh_continues_after_source_error(db_path, monkeypatch):
    monkeypatch.setattr(startup, "load_settings", lambda: _settings())
    _mock_fetchers(
        monkeypatch,
        {"bbc": {"rows": 1}, "reddit": {"rows": 3}, "youtube": {"rows": 2}},
        errors={"bbc": RuntimeError("boom")},
    )
    import app.signals.pipeline as pipe

    monkeypatch.setattr(pipe, "process_pending_items", _fake_pipeline(0))

    state = asyncio.run(startup.run_full_refresh())

    assert state["status"] == "done"
    assert state["results"]["bbc"] == "error: boom"
    assert state["results"]["reddit"] == {"rows": 3}  # others still ran
    assert state["results"]["youtube"] == {"rows": 2}


def test_disabled_sources_are_skipped(db_path, monkeypatch):
    monkeypatch.setattr(startup, "load_settings", lambda: _settings(bbc=False, reddit=False))
    _mock_fetchers(monkeypatch, {"youtube": {"rows": 1}})
    import app.signals.pipeline as pipe

    monkeypatch.setattr(pipe, "process_pending_items", _fake_pipeline(0))

    state = asyncio.run(startup.run_full_refresh())

    assert state["results"]["bbc"] == "skipped (disabled)"
    assert state["results"]["reddit"] == "skipped (disabled)"
    assert state["results"]["youtube"] == {"rows": 1}


def test_refresh_one_unknown_source(db_path):
    cfg = _settings().config
    assert asyncio.run(startup.refresh_one("nope", cfg)) == "unknown source 'nope'"


def test_endpoint_returns_persisted_state(db_path):
    from app.main import app

    state = {
        "status": "done",
        "started_at": "2026-09-20T10:00:00Z",
        "finished_at": "2026-09-20T10:01:30Z",
        "results": {"bbc": {"rows": 5}},
        "signals_stored": 2,
    }
    dbmod.set_meta(startup.META_KEY, json.dumps(state))

    client = TestClient(app)  # no `with` → lifespan (and any network) not run
    r = client.get("/api/meta/startup-refresh")
    assert r.status_code == 200
    assert r.json() == state


def test_endpoint_idle_by_default(db_path):
    from app.main import app

    client = TestClient(app)
    r = client.get("/api/meta/startup-refresh")
    assert r.status_code == 200
    assert r.json() == {"status": "idle"}


def _fake_pipeline(n: int):
    async def _fn(limit: int = 50, timebox_sec=None):  # FIX N8: accepts the timebox
        return n

    return _fn


@pytest.mark.parametrize("key", ["started_at", "finished_at"])
def test_state_shape_keys(db_path, monkeypatch, key):
    monkeypatch.setattr(startup, "load_settings", lambda: _settings(bbc=False, reddit=False, youtube=False))
    import app.signals.pipeline as pipe

    monkeypatch.setattr(pipe, "process_pending_items", _fake_pipeline(0))
    state = asyncio.run(startup.run_full_refresh())
    assert state[key] is not None