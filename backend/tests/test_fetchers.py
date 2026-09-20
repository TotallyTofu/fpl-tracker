"""Fetcher tests against captured live fixtures (PLAN-3 T2.2–T2.5 / T2.12).

No network: http.get_text / get_json are monkeypatched to serve the fixture
files; the YouTube transcript provider is faked.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app import db as dbmod
from app.config import ConfigFile
from app.fetchers import bbc, espn, reddit, youtube
from app.httpclient import http

FIX = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# --- BBC ------------------------------------------------------------------------


def test_bbc_parse_feed():
    items = bbc.parse_feed(_read("sample_bbc.xml"))
    assert len(items) == 60
    it = items[0]
    assert it["title"] and it["url"] and it["external_id"]
    assert it["published_at"] is not None


def test_bbc_relevance_filter():
    clubs = ["Arsenal", "Chelsea"]
    assert bbc.is_pl_relevant("Arsenal beat Chelsea in Premier League", "", clubs, [])
    assert bbc.is_pl_relevant("Arsenal win at Chelsea", "", clubs, [])
    assert bbc.is_pl_relevant("Haaland scores for Manchester City", "", [], ["Haaland"])
    assert not bbc.is_pl_relevant("Women's final: champions crowned", "", clubs, [])


def test_bbc_extract_article_text():
    html = (
        "<html><body><article><h1>Title</h1><p>First para.</p>"
        "<p>Second para with &amp; entity.</p></article>"
        "<script>var x = 1;</script></body></html>"
    )
    text = bbc.extract_article_text(html)
    assert "First para." in text and "Second para with & entity." in text
    assert "var x" not in text


def test_refresh_bbc_uses_fixture(db_path, monkeypatch):
    async def fake_get_text(url, **kw):
        assert "feeds.bbci.co.uk" in url
        return _read("sample_bbc.xml")

    monkeypatch.setattr(http, "get_text", fake_get_text)
    out = asyncio.run(bbc.refresh_bbc())
    assert out["status"] == "ok"
    assert out["rows"] > 0
    # ≤ 60: in-feed duplicates are deduped on ingest (neither stored nor filtered)
    assert out["rows"] + out["filtered_out"] <= 60
    n = dbmod.query("SELECT COUNT(*) AS n FROM raw_items WHERE source = 'bbc'")[0]["n"]
    assert n == out["rows"]
    poll = dbmod.query_one("SELECT * FROM poll_log WHERE source = 'bbc'")
    assert poll["status"] == "ok"


# --- ESPN -----------------------------------------------------------------------


def test_espn_parse_news():
    items = espn.parse_news(json.loads(_read("sample_espn_news.json")))
    assert len(items) == 6
    it = items[0]
    assert it["title"] and it["url"].startswith("https://")
    assert it["external_id"]


def test_espn_parse_news_empty():
    assert espn.parse_news({}) == []
    assert espn.parse_news({"articles": None}) == []


def test_refresh_espn_uses_fixture(db_path, monkeypatch):
    async def fake_get_json(url, **kw):
        assert "site.api.espn.com" in url
        return json.loads(_read("sample_espn_news.json"))

    monkeypatch.setattr(http, "get_json", fake_get_json)
    out = asyncio.run(espn.refresh_espn())
    assert out["status"] == "ok"
    assert out["rows"] + out["filtered_out"] == 6


# --- Reddit ---------------------------------------------------------------------


def test_reddit_parse_feed_skips_megathreads():
    items, skipped = reddit.parse_feed(_read("sample_reddit.xml"))
    assert skipped == 2  # the two pinned u/FPLModerator megathreads
    assert len(items) == 23
    assert all("fplmoderator" not in (i["external_id"] or "") for i in items)
    # post ids extracted from t3_xxx
    assert all(i["post_id"] for i in items)


def test_reddit_image_only_post_title_only():
    items, _ = reddit.parse_feed(_read("sample_reddit.xml"))
    # at least one entry is image-only: its text is short (< 40 chars)
    shorts = [i for i in items if len(i["text"]) < 40]
    assert shorts, "expected at least one image-only post in the fixture"


def test_refresh_reddit_uses_fixture(db_path, monkeypatch):
    async def fake_get_text(url, **kw):
        assert "reddit.com" in url
        return _read("sample_reddit.xml")

    async def no_thread(post_id):
        return None

    monkeypatch.setattr(http, "get_text", fake_get_text)
    monkeypatch.setattr(reddit, "_fetch_thread_body", no_thread)
    out = asyncio.run(reddit.refresh_reddit(ConfigFile()))
    assert out["status"] == "ok"
    assert out["megathreads_skipped"] == 2
    assert out["rows"] == 23
    poll = dbmod.query_one("SELECT * FROM poll_log WHERE source = 'reddit'")
    assert poll["status"] == "ok"


# --- YouTube --------------------------------------------------------------------


def test_youtube_parse_feed():
    items = youtube.parse_feed(_read("sample_youtube.xml"))
    assert len(items) == 15
    assert all(i["video_id"] for i in items)
    assert all(i["url"].startswith("https://www.youtube.com/") for i in items)


def test_youtube_shorts_and_priority():
    items = youtube.parse_feed(_read("sample_youtube.xml"))
    shorts = [i for i in items if youtube.is_short(i["url"])]
    assert len(shorts) > 0
    kw = ["weekender", "deadline stream", "cotc", "clash of the correspondents", "review"]
    prio = [i for i in items if not youtube.is_short(i["url"])
            and youtube.priority(i["title"], kw) == 1]
    assert len(prio) == 4  # Deadline Stream, Weekender ep.48, 2× CotC


def test_vtt_to_text_dedupes():
    vtt = "\n".join([
        "WEBVTT",
        "Kind: captions",
        "Language: en",
        "",
        "00:00:00.000 --> 00:00:02.000",
        "Hello there",
        "Hello there",
        "",
        "00:00:02.000 --> 00:00:04.000",
        "Second <c>line</c> &amp; more",
        "",
    ])
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".vtt", delete=False, encoding="utf-8") as f:
        f.write(vtt)
        path = f.name
    try:
        text = youtube.vtt_to_text(path)
    finally:
        Path(path).unlink(missing_ok=True)
    lines = text.splitlines()
    assert lines == ["Hello there", "Second line & more"]


def test_refresh_youtube_uses_fixture(db_path, monkeypatch):
    class FakeProvider:
        def __init__(self):
            self.fetched: list[str] = []

        def fetch(self, video_id):
            self.fetched.append(video_id)
            return f"TRANSCRIPT TEXT for {video_id}"

    provider = FakeProvider()

    async def fake_get_text(url, **kw):
        assert "youtube.com/feeds/videos.xml" in url
        return _read("sample_youtube.xml")

    monkeypatch.setattr(http, "get_text", fake_get_text)
    out = asyncio.run(youtube.refresh_youtube(ConfigFile(), provider=provider))
    assert out["status"] == "ok"
    assert out["rows"] == 15
    assert out["transcripts_fetched"] == 3  # budget = max_transcripts_per_poll
    # the transcript is appended after the marker
    rows = dbmod.query("SELECT * FROM raw_items WHERE source = 'youtube' AND body LIKE '%--- TRANSCRIPT ---%'")
    assert len(rows) == 3
    assert all("TRANSCRIPT TEXT for" in r["body"] for r in rows)
    # re-ingest dedupes: second poll stores nothing new
    out2 = asyncio.run(youtube.refresh_youtube(ConfigFile(), provider=provider))
    assert out2["rows"] == 0
    assert len(provider.fetched) == 3  # no transcript re-fetch for dupes


# --- Official news hook (fpl bootstrap) ------------------------------------------


def test_official_news_hook_in_bootstrap(db_path, monkeypatch):
    """refresh_all_fpl runs process_official_news after the elements upsert."""
    from app.fetchers import fpl as fpl_fetcher

    bootstrap = {
        "elements": [
            {"id": 1, "web_name": "Goal One", "element_type": 1, "team": 1,
             "now_cost": 45, "ep_next": 8.0, "status": "a", "can_select": 1,
             "removed": 0, "selected_by_percent": 50.0,
             "chance_of_playing_next_round": 100,
             "news": None, "news_added": None},
            {"id": 3, "web_name": "Def A One", "element_type": 2, "team": 1,
             "now_cost": 40, "ep_next": 9.0, "status": "i", "can_select": 1,
             "removed": 0, "selected_by_percent": 60.0,
             "chance_of_playing_next_round": 0,
             "news": "Hamstring injury", "news_added": "2026-09-19T09:00:00Z"},
        ],
        "events": [{"id": 5, "deadline_time": "2026-09-20T10:00:00Z"},
                   {"id": 6, "deadline_time": "2026-09-27T10:00:00Z"}],
        "chips": [],
        "teams": [{"id": 1, "name": "Alpha FC", "short_name": "Alpha", "strength": 80}],
        "game_settings": {"max_total_squad_cost": 1000, "squad_size": 15},
    }
    async def fake_get_json(url, **kw):
        if "bootstrap-static" in url:
            return bootstrap
        return []  # /fixtures/ returns a bare JSON list

    monkeypatch.setattr(http, "get_json", fake_get_json)
    asyncio.run(fpl_fetcher.refresh_all_fpl())
    # cold start: cache seeded (all DB players), no signals yet
    assert dbmod.query("SELECT COUNT(*) AS n FROM official_news_cache")[0]["n"] == 28
    assert dbmod.query("SELECT COUNT(*) AS n FROM signals")[0]["n"] == 0
    # now player 3 recovers → a return signal
    bootstrap["elements"][1].update(status="a", chance_of_playing_next_round=100,
                                    news="Back in the squad")
    asyncio.run(fpl_fetcher.refresh_all_fpl())
    sigs = dbmod.query("SELECT * FROM signals WHERE player_id = 3")
    assert any(s["category"] == "return" and s["confidence"] == 0.9 for s in sigs)
    assert any(s["category"] == "selection" and s["sentiment"] == "positive" for s in sigs)