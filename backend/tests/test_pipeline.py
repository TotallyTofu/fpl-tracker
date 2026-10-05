"""Pipeline tests (FIX.MD Part 2 §18.6/§18.7): N8 timebox, N9 breaker,
N10 retry contract, and the raw_items.extract_attempts migration."""
from __future__ import annotations

import asyncio
import sqlite3
import types

import pytest

from app import db as dbmod
from app.signals import ingest, pipeline


def _settings():
    return types.SimpleNamespace(llm_ready=True, llm_model="m",
                                 config=types.SimpleNamespace(
                                     llm=types.SimpleNamespace(extract_timebox_sec=480)))


def _item(ext: str, body_suffix: str = "") -> int:
    return ingest.ingest_item(
        source="bbc", external_id=ext, kind="article",
        title="Goal One ruled out",
        url=f"http://example.com/{ext}", published_at=dbmod.now_utc(),
        body=f"Goal One is ruled out with a hamstring injury.{body_suffix}",
    )


def _row(item_id: int) -> dict:
    return dbmod.query_one("SELECT * FROM raw_items WHERE id = ?", (item_id,))


def test_llm_none_requeues_then_falls_back_to_rules(db_path, monkeypatch):
    """§18.6 (FIX N10), v1.0: a hard LLM failure (None) increments
    extract_attempts and leaves the item pending WITHOUT keyword signals (the
    next attempt may still read it properly). After MAX_EXTRACT_ATTEMPTS the
    keyword fallback runs and the item is marked processed."""
    item_id = _item("retry1")

    async def failing_llm(*a, **k):
        return None

    monkeypatch.setattr(pipeline, "extract_signals_llm", failing_llm)
    n = asyncio.run(pipeline.process_item(_row(item_id), _settings()))
    assert n == 0
    assert dbmod.query_one("SELECT * FROM signals WHERE player_id = 1") is None
    row = _row(item_id)
    assert row["extract_attempts"] == 1
    assert row["processed"] == 0        # requeued, not lost

    for _ in range(pipeline.MAX_EXTRACT_ATTEMPTS - 1):
        asyncio.run(pipeline.process_item(_row(item_id), _settings()))
    row = _row(item_id)
    assert row["extract_attempts"] == pipeline.MAX_EXTRACT_ATTEMPTS
    assert row["processed"] == 1        # gave up — but never silently lost
    sig = dbmod.query_one("SELECT * FROM signals WHERE player_id = 1 AND category = 'injury'")
    assert sig is not None and sig["model"] == "rules"
    assert "Keyword match only" in (row["takeaways"] or "")


def test_llm_success_stores_no_keyword_signals(db_path, monkeypatch):
    """v1.0: when the LLM reads the item, only its signals are stored — the
    keyword extractor used to add contradicting signals from the same text."""
    item_id = _item("llm1")

    async def ok_llm(*a, **k):
        return [{"player_id": 1, "category": "return", "sentiment": "positive",
                 "confidence": 0.8, "summary": "Goal One is back."}]

    monkeypatch.setattr(pipeline, "extract_signals_llm", ok_llm)
    asyncio.run(pipeline.process_item(_row(item_id), _settings()))
    models = {r["model"] for r in dbmod.query("SELECT model FROM signals WHERE player_id = 1")}
    assert models and all(m.startswith("llm:") for m in models)


def test_llm_none_marks_processed_when_attempts_exhausted(db_path, monkeypatch):
    """FIX N10: an item that already burned its attempts is not requeued."""
    item_id = _item("retry2")
    dbmod.execute("UPDATE raw_items SET extract_attempts = 3 WHERE id = ?", (item_id,))

    async def failing_llm(*a, **k):
        return None

    monkeypatch.setattr(pipeline, "extract_signals_llm", failing_llm)
    asyncio.run(pipeline.process_item(_row(item_id), _settings()))
    row = _row(item_id)
    assert row["processed"] == 1


def test_llm_success_marks_processed(db_path, monkeypatch):
    """FIX N10: a normal LLM result (list, even empty) completes the item."""
    item_id = _item("ok1")

    async def ok_llm(*a, **k):
        return []

    monkeypatch.setattr(pipeline, "extract_signals_llm", ok_llm)
    asyncio.run(pipeline.process_item(_row(item_id), _settings()))
    row = _row(item_id)
    assert row["processed"] == 1
    assert row["extract_attempts"] == 0


def test_llm_breaker_makes_recent_items_wait(db_path, monkeypatch):
    """v1.0: breaker open (≥2 recent llm errors, errors > oks) → the LLM is
    skipped and a RECENT item waits for it (it used to be marked done with
    keyword signals only, and never reached the LLM)."""
    item_id = _item("brk0")
    for _ in range(2):
        dbmod.execute(
            "INSERT INTO poll_log (source, status, rows, error, started_at, finished_at) "
            "VALUES ('llm', 'error', NULL, 'boom', ?, ?)",
            (dbmod.now_utc(), dbmod.now_utc()),
        )

    async def must_not_run(*a, **k):
        raise AssertionError("LLM must be skipped while the breaker is open")

    monkeypatch.setattr(pipeline, "extract_signals_llm", must_not_run)
    assert asyncio.run(pipeline.process_item(_row(item_id), _settings())) == -1
    assert _row(item_id)["processed"] == 0


def test_llm_breaker_skips_llm_and_notes_it(db_path, monkeypatch):
    """FIX N9 / v1.0: an item that has waited longer than llm.llm_wait_hours
    while the breaker is open falls back to keyword matches, noted in the
    takeaways."""
    item_id = _item("brk1")
    for _ in range(2):
        dbmod.execute(
            "INSERT INTO poll_log (source, status, rows, error, started_at, finished_at) "
            "VALUES ('llm', 'error', NULL, 'boom', ?, ?)",
            (dbmod.now_utc(), dbmod.now_utc()),
        )
    calls: list[int] = []

    async def must_not_run(*a, **k):
        calls.append(1)
        return []

    monkeypatch.setattr(pipeline, "extract_signals_llm", must_not_run)
    dbmod.execute("UPDATE raw_items SET retrieved_at = '2026-01-01T00:00:00Z' WHERE id = ?",
                  (item_id,))
    asyncio.run(pipeline.process_item(_row(item_id), _settings()))
    assert calls == []                              # the LLM was skipped
    row = _row(item_id)
    assert row["processed"] == 1                    # not retryable — never tried
    assert "circuit breaker" in (row["takeaways"] or "")


def test_timebox_stops_between_items(db_path, monkeypatch):
    """§18.6 (FIX N8): a tiny timebox stops the pass between items — the
    second item stays pending for the next pass."""
    id1 = _item("tb1", " (first)")
    id2 = _item("tb2", " (second)")
    processed: list[int] = []

    async def slow_item(item, settings=None):
        processed.append(item["id"])
        await asyncio.sleep(0.05)
        ingest.mark_processed(item["id"], None)
        return 0

    monkeypatch.setattr(pipeline, "process_item", slow_item)
    total = asyncio.run(pipeline.process_pending_items(limit=10, timebox_sec=0.02,
                                                       settings=_settings()))
    assert total == 0
    assert processed == [id1]                       # first started, second didn't
    pending = dbmod.query("SELECT id FROM raw_items WHERE processed = 0 ORDER BY id")
    assert [r["id"] for r in pending] == [id2]


def test_no_timebox_drains_the_queue(db_path, monkeypatch):
    _item("dr1", " (a)")
    _item("dr2", " (b)")

    async def fast_item(item, settings=None):
        ingest.mark_processed(item["id"], None)
        return 0

    monkeypatch.setattr(pipeline, "process_item", fast_item)
    asyncio.run(pipeline.process_pending_items(limit=10, timebox_sec=60, settings=_settings()))
    assert dbmod.query("SELECT id FROM raw_items WHERE processed = 0") == []


def test_pending_items_priority_short_first(db_path):
    """FIX N10: YouTube rows can never starve BBC/Reddit headlines."""
    yt = ingest.ingest_item(
        source="youtube", external_id="v1", kind="video", title="Video",
        url="https://www.youtube.com/watch?v=v1", published_at=dbmod.now_utc(),
        body="old description",
    )
    dbmod.execute("UPDATE raw_items SET retrieved_at = '2026-09-01T00:00:00Z' WHERE id = ?", (yt,))
    bbc_id = _item("prio1")
    rows = ingest.pending_items(10)
    ids = [r["id"] for r in rows]
    assert ids.index(bbc_id) < ids.index(yt)        # short item first despite age


def test_extract_attempts_migration(tmp_path):
    """§18.7: init_db adds raw_items.extract_attempts to a pre-migration DB."""
    p = tmp_path / "old.db"
    conn = sqlite3.connect(p)
    conn.execute(
        """CREATE TABLE raw_items (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             source TEXT NOT NULL, external_id TEXT NOT NULL, kind TEXT NOT NULL,
             title TEXT, url TEXT, published_at TEXT, body TEXT,
             content_hash TEXT NOT NULL, takeaways TEXT,
             processed INTEGER NOT NULL DEFAULT 0, retrieved_at TEXT NOT NULL,
             UNIQUE(source, content_hash))"""
    )
    conn.commit()
    conn.close()

    dbmod.init_db(p)
    cols = {r[1] for r in sqlite3.connect(p).execute("PRAGMA table_info(raw_items)")}
    assert "extract_attempts" in cols


def test_fresh_db_has_extract_attempts(db_path):
    cols = {r[1] for r in sqlite3.connect(str(db_path)).execute("PRAGMA table_info(raw_items)")}
    assert "extract_attempts" in cols