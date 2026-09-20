"""Ingest dedupe + store TTL/dedupe tests (PLAN-3 T2.1 / T2.9 / T2.12)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import db as dbmod
from app.signals import ingest, store

NOW = "2026-09-19T12:00:00Z"


def test_parse_published_formats():
    assert ingest.parse_published("2026-09-19T12:00:00Z") == "2026-09-19T12:00:00Z"
    assert ingest.parse_published("2026-09-19T14:00:00+02:00") == "2026-09-19T12:00:00Z"
    assert ingest.parse_published("1758288000") == "2025-09-19T13:20:00Z"
    import time

    st = time.struct_time((2026, 9, 19, 12, 0, 0, 5, 262, 0))
    assert ingest.parse_published(st) == "2026-09-19T12:00:00Z"
    assert ingest.parse_published(None) is None
    assert ingest.parse_published("not a date") is None


def test_ingest_dedupe_on_content_hash(db_path):
    a = ingest.ingest_item("bbc", "x1", "article", "Goal One injury", "http://a", NOW, "body one")
    b = ingest.ingest_item("bbc", "x2", "article", "Goal One injury", "http://b", NOW, "body one")
    assert a is not None
    assert b is None  # same source+title+body → duplicate despite new URL
    c = ingest.ingest_item("bbc", "x3", "article", "Goal One injury", "http://c", NOW, "body two")
    assert c is not None  # body changed → new item
    n = dbmod.query("SELECT COUNT(*) AS n FROM raw_items")[0]["n"]
    assert n == 2


def test_ingest_processed_flag_and_queue(db_path):
    a = ingest.ingest_item("bbc", "x1", "article", "A", "http://a", NOW, "b1")
    b = ingest.ingest_item("reddit", "x2", "thread", "B", "http://b", NOW, "b2")
    pending = ingest.pending_items()
    assert [i["id"] for i in pending] == [a, b]
    ingest.mark_processed(a, ["takeaway"])
    pending = ingest.pending_items()
    assert [i["id"] for i in pending] == [b]
    row = dbmod.query_one("SELECT * FROM raw_items WHERE id = ?", (a,))
    assert row["processed"] == 1
    assert row["takeaways"] == '["takeaway"]'


def test_update_body_requeues(db_path):
    a = ingest.ingest_item("bbc", "x1", "article", "A", "http://a", NOW, "short")
    ingest.mark_processed(a)
    ingest.update_body(a, "much longer body", mark_pending=True)
    row = dbmod.query_one("SELECT * FROM raw_items WHERE id = ?", (a,))
    assert row["processed"] == 0
    assert row["body"] == "much longer body"


def _sig(pid=1, cat="injury", sent="negative", conf=0.5, src="bbc:x1",
         summary="s", expires_in_h=72):
    exp = (datetime.now(timezone.utc) + timedelta(hours=expires_in_h)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return {
        "player_id": pid, "category": cat, "sentiment": sent, "confidence": conf,
        "summary": summary, "source": src, "url": "http://x", "published_at": NOW,
        "raw_item_id": 1, "model": "rules",
    }


def test_store_dedupe_higher_confidence_wins(db_path):
    assert store.save_signals([_sig(conf=0.4, summary="low")]) == 1
    assert store.save_signals([_sig(conf=0.6, summary="high")]) == 1  # update, not insert
    rows = dbmod.query("SELECT * FROM signals")
    assert len(rows) == 1
    assert rows[0]["confidence"] == 0.6
    assert rows[0]["summary"] == "high"


def test_store_lower_confidence_keeps_existing(db_path):
    store.save_signals([_sig(conf=0.6, summary="high")])
    store.save_signals([_sig(conf=0.3, summary="low")])
    rows = dbmod.query("SELECT * FROM signals")
    assert len(rows) == 1
    assert rows[0]["confidence"] == 0.6
    assert rows[0]["summary"] == "high"


def test_store_different_category_or_source_inserts(db_path):
    store.save_signals([_sig(cat="injury", src="bbc:x1"), _sig(cat="selection", src="bbc:x1"),
                        _sig(cat="injury", src="espn:9")])
    assert len(dbmod.query("SELECT * FROM signals")) == 3


def test_store_official_ttl_14_days(db_path):
    store.save_signals([_sig(src="fpl-official")])
    store.save_signals([_sig(src="bbc:x1")])
    rows = {r["source"]: r for r in dbmod.query("SELECT * FROM signals")}
    for name, days in (("fpl-official", 14), ("bbc:x1", 3)):
        exp = datetime.fromisoformat(rows[name]["expires_at"].replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = (exp - now).total_seconds() / 86400
        assert abs(delta - days) < 0.2, (name, delta)


def test_active_signals_filters(db_path):
    store.save_signals([
        _sig(pid=1, cat="injury", src="bbc:a"),
        _sig(pid=3, cat="selection", src="espn:b"),
    ])
    assert len(store.active_signals()) == 2
    assert len(store.active_signals(player_id=1)) == 1
    assert len(store.active_signals(category="selection")) == 1
    # expired signal is excluded (force the TTL into the past)
    store.save_signals([_sig(pid=1, cat="return", src="old:x")])
    dbmod.execute("UPDATE signals SET expires_at = '2026-01-01T00:00:00Z' WHERE source = 'old:x'")
    assert len(store.active_signals(player_id=1)) == 1
    # expire_stale deletes it
    assert store.expire_stale() == 1
    assert len(dbmod.query("SELECT * FROM signals WHERE player_id = 1")) == 1


def test_signal_row_shape_for_api(db_path):
    store.save_signals([_sig(pid=1, cat="injury", src="bbc:a")])
    rows = store.active_signals(player_id=1)
    r = rows[0]
    for key in ("player_id", "category", "sentiment", "confidence", "summary",
                "source", "expires_at", "web_name", "team_code"):
        assert key in r
    assert r["web_name"] == "Goal One"