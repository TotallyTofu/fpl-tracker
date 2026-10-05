"""Fetcher tests against captured live fixtures (PLAN-3 T2.2–T2.5 / T2.12).

No network: http.get_text / get_json are monkeypatched to serve the fixture
files; the YouTube transcript provider is faked.
"""
from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

import httpx
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


def test_bbc_lone_common_word_player_term_dropped(monkeypatch):
    """A9.2: a lone common English word that happens to be a player's name
    ("White", "Barnes") must not make almost any article PL-relevant;
    multi-word names ("Ben White") stay specific enough."""
    rows = [
        {"web_name": "White", "known_name": "Ben White"},
        {"web_name": "Barnes", "known_name": "Anthony Barnes"},
        {"web_name": "Groß", "known_name": "Pascal Groß"},
    ]
    monkeypatch.setattr(bbc, "query", lambda sql: rows)
    terms = bbc._player_terms()
    assert "White" not in terms and "Barnes" not in terms
    assert "Ben White" in terms and "Pascal Groß" in terms
    # the filter path: lone common word alone → not relevant; full name → relevant
    assert not bbc.is_pl_relevant("White scores a late winner", "", [], None)
    assert bbc.is_pl_relevant("Ben White scores a late winner", "", [], None)


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
        # FIX N16: the PL feed serves an empty feed here — this test only
        # exercises the generic-feed path (see test_refresh_bbc_pl_feed).
        if url == bbc.PL_FEED_URL:
            return '<?xml version="1.0"?><rss><channel></channel></rss>'
        assert url == bbc.FEED_URL
        return _read("sample_bbc.xml")

    # Body auto-fetch off: this test only exercises the feed path (article URLs
    # from the live fixture would otherwise hit the fake get_text).
    cfg = ConfigFile()
    cfg.sources.bbc.fetch_bodies = False
    monkeypatch.setattr(bbc, "load_config", lambda: cfg)
    monkeypatch.setattr(http, "get_text", fake_get_text)
    out = asyncio.run(bbc.refresh_bbc())
    assert out["status"] == "ok"
    assert out["rows"] > 0
    assert out["pl_feed_ok"] is True
    # ≤ 60: in-feed duplicates are deduped on ingest (neither stored nor filtered)
    assert out["rows"] + out["filtered_out"] <= 60
    n = dbmod.query("SELECT COUNT(*) AS n FROM raw_items WHERE source = 'bbc'")[0]["n"]
    assert n == out["rows"]
    poll = dbmod.query_one("SELECT * FROM poll_log WHERE source = 'bbc'")
    assert poll["status"] == "ok"


def test_refresh_bbc_pl_feed_unfiltered(db_path, monkeypatch):
    """FIX N16: PL-feed items are PL by definition — one that would be dropped
    by the generic feed's relevance filter is ingested via the PL feed."""
    generic_xml = (
        '<?xml version="1.0"?><rss><channel><item>'
        "<title>Women's final: champions crowned</title>"
        "<description>A non-PL item that the filter drops.</description>"
        "<link>https://www.bbc.co.uk/sport/football/articles/generic1</link>"
        "<guid>generic1</guid>"
        "</item></channel></rss>"
    )
    pl_xml = (
        '<?xml version="1.0"?><rss><channel><item>'
        "<title>Brentford boss on the fitness latest</title>"
        "<description>No club name in the description either.</description>"
        "<link>https://www.bbc.co.uk/sport/football/articles/pl1</link>"
        "<guid>pl1</guid>"
        "</item></channel></rss>"
    )

    async def fake_get_text(url, **kw):
        return pl_xml if url == bbc.PL_FEED_URL else generic_xml

    cfg = ConfigFile()
    cfg.sources.bbc.fetch_bodies = False
    monkeypatch.setattr(bbc, "load_config", lambda: cfg)
    monkeypatch.setattr(http, "get_text", fake_get_text)
    out = asyncio.run(bbc.refresh_bbc())
    assert out["status"] == "ok" and out["pl_feed_ok"] is True
    assert out["filtered_out"] == 1  # only the generic-feed item was filtered
    titles = {r["title"] for r in dbmod.query("SELECT title FROM raw_items WHERE source = 'bbc'")}
    assert "Brentford boss on the fitness latest" in titles  # PL feed stored unfiltered
    assert "Women's final: champions crowned" not in titles


def test_normalize_bbc_url():
    from app.fetchers.bbc import normalize_bbc_url
    assert normalize_bbc_url(
        "https://www.bbc.co.uk/sport/football/articles/cqrl6pe56348o?at_medium=RSS&at_campaign=rss"
    ) == "https://www.bbc.co.uk/sport/football/articles/cqrl6pe56348o"
    assert normalize_bbc_url("https://www.bbc.co.uk/news/articles/c34gdjk1ne8yo#comments") == \
        "https://www.bbc.co.uk/news/articles/c34gdjk1ne8yo"
    assert normalize_bbc_url("") == ""


def test_parse_feed_strips_query_params():
    from app.fetchers.bbc import parse_feed
    xml = (
        '<?xml version="1.0"?><rss><channel><item>'
        "<title>T</title><description>D</description>"
        "<link>https://www.bbc.co.uk/sport/football/articles/abc123?at_medium=RSS&amp;at_campaign=rss</link>"
        "<guid>https://www.bbc.co.uk/sport/football/articles/abc123</guid>"
        "</item></channel></rss>"
    )
    items = parse_feed(xml)
    assert items[0]["url"] == "https://www.bbc.co.uk/sport/football/articles/abc123"


def test_refresh_bbc_auto_fetches_bodies(db_path, monkeypatch):
    """Bug 1 fix: new PL-relevant items get their full article body fetched and
    stored (re-queued for extraction), capped by max_bodies_per_poll."""
    feed_xml = (
        '<?xml version="1.0"?><rss><channel><item>'
        "<title>Premier League injury news</title>"
        "<description>A short RSS blurb.</description>"
        "<link>https://www.bbc.co.uk/sport/football/articles/abc123?at_medium=RSS&amp;at_campaign=rss</link>"
        "<guid>https://www.bbc.co.uk/sport/football/articles/abc123</guid>"
        "</item></channel></rss>"
    )
    article_html = (
        "<html><body><article><h1>Premier League injury news</h1>"
        "<p>First paragraph of the full article body, with enough text to be meaningful for the extractors working off the stored item.</p>"
        "<p>Second paragraph adds more detail about the injury itself, the medical scan, and the expected return window for the coming gameweeks.</p>"
        "<p>Third paragraph carries additional context and analysis from the club, the manager, and the network's correspondents covering the match.</p>"
        "</article></body></html>"
    )
    urls: list[str] = []

    async def fake_get_text(url, **kw):
        urls.append(url)
        if "feeds.bbci.co.uk" in url:
            return feed_xml
        return article_html

    cfg = ConfigFile()  # fetch_bodies=True, max_bodies_per_poll=10 by default
    monkeypatch.setattr(bbc, "load_config", lambda: cfg)
    monkeypatch.setattr(http, "get_text", fake_get_text)
    out = asyncio.run(bbc.refresh_bbc())
    assert out["status"] == "ok"
    assert out["rows"] == 1
    assert out["bodies"] == 1
    # the article page was fetched (via the normalised, param-free URL)
    assert "https://www.bbc.co.uk/sport/football/articles/abc123" in urls
    item = dbmod.query_one("SELECT * FROM raw_items WHERE source = 'bbc'")
    assert "First paragraph" in item["body"]
    assert "Third paragraph" in item["body"]
    assert item["processed"] == 0  # re-queued for extraction


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
        # FIX N11: /new serves an empty feed here — this test only exercises
        # the hot-feed path (see test_refresh_reddit_merges_new).
        if url == reddit.NEW_RSS:
            return '<?xml version="1.0"?><rss><channel></channel></rss>'
        assert url == reddit.HOT_RSS
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


def test_refresh_reddit_merges_new(db_path, monkeypatch):
    """FIX N11: /new posts are merged with /hot — including one post that is
    already trending in hot (within-poll dedupe) — and the megathread skip
    applies to both listings."""
    hot_xml = (
        '<?xml version="1.0"?><rss><channel>'
        "<item><title>Pinned megathread</title><author>fplmoderator@reddit.com</author>"
        "<id>t3_mega1</id><link href='https://www.reddit.com/r/FantasyPL/comments/mega1/'/></item>"
        "<item><title>Transfer news: confirmed starter announced</title>"
        "<id>t3_hot1</id><link href='https://www.reddit.com/r/FantasyPL/comments/hot1/'/>"
        "<content type='html'>Full details of the transfer situation in this post, with enough body text.</content></item>"
        "</channel></rss>"
    )
    new_xml = (
        '<?xml version="1.0"?><rss><channel>'
        "<item><title>Team news: presser quotes from the manager</title>"
        "<id>t3_hot1</id><link href='https://www.reddit.com/r/FantasyPL/comments/hot1/'/>"
        "<content type='html'>The same post already trending in hot.</content></item>"
        "<item><title>Fitness latest on the doubting striker</title>"
        "<id>t3_new1</id><link href='https://www.reddit.com/r/FantasyPL/comments/new1/'/>"
        "<content type='html'>He is a doubt for the weekend, per the press conference.</content></item>"
        "</channel></rss>"
    )

    async def fake_get_text(url, **kw):
        if url == reddit.NEW_RSS:
            return new_xml
        return hot_xml

    async def no_thread(post_id):
        return None

    monkeypatch.setattr(http, "get_text", fake_get_text)
    monkeypatch.setattr(reddit, "_fetch_thread_body", no_thread)
    out = asyncio.run(reddit.refresh_reddit(ConfigFile()))
    assert out["status"] == "ok"
    assert out["megathreads_skipped"] == 1  # hot's pinned megathread
    assert out["new_listing_items"] == 2
    ext_ids = {r["external_id"] for r in dbmod.query(
        "SELECT external_id FROM raw_items WHERE source = 'reddit'")}
    assert ext_ids == {"hot1", "new1"}  # within-poll duplicate hot1 stored once


def test_reddit_widened_headline_regex():
    """FIX N12: common team-news titles now match the thread-body selector."""
    for t in ("Haaland fitness latest", "Saka fit to start?",
              "Who returns this weekend?", "Palace predicted XI",
              "Latest on the Foden transfer saga", "Team news & injury flags",
              "Robinson benched after the presser"):
        assert reddit._HEADLINE_RE.search(t), t
    assert not reddit._HEADLINE_RE.search("A quiet day in football")


# --- Reddit OAuth -----------------------------------------------------------------

HOT_JSON_SAMPLE = {
    "data": {
        "children": [
            {
                "data": {
                    "id": "mega01",
                    "stickied": True,
                    "author": "FPLModerator",
                    "title": "Pinned megathread",
                    "selftext": "",
                    "permalink": "/r/FantasyPL/comments/mega01/",
                    "created_utc": 1750000000,
                }
            },
            {
                "data": {
                    "id": "hot01",
                    "author": "someuser",
                    "title": "Transfer news: confirmed starter announced",
                    "selftext": "Full details of the transfer and injury situation in this post.",
                    "permalink": "/r/FantasyPL/comments/hot01/",
                    "created_utc": 1750000100,
                }
            },
        ]
    }
}

COMMENTS_JSON_SAMPLE = [
    {"data": {"children": []}},  # [0] = post listing
    {
        "data": {
            "children": [
                {"kind": "t1", "data": {"body": "Top comment one"}},
                {"kind": "t1", "data": {"body": "Top comment two"}},
            ]
        }
    },  # [1] = comments listing
]


def test_reddit_items_from_json():
    items, skipped = reddit._items_from_json(HOT_JSON_SAMPLE)
    assert skipped == 1  # the stickied megathread
    assert len(items) == 1
    it = items[0]
    assert it["post_id"] == "hot01"
    assert it["external_id"] == "hot01"
    assert it["title"].startswith("Transfer news")
    assert "Full details" in it["text"]
    assert it["url"] == "https://www.reddit.com/r/FantasyPL/comments/hot01/"
    assert it["published_at"] == "1750000100"  # epoch-seconds string


def test_refresh_reddit_oauth_mode(db_path, monkeypatch):
    cfg = ConfigFile()
    cfg.sources.reddit.mode = "oauth"
    cfg.sources.reddit.oauth_client_id = "dummy-id"
    cfg.sources.reddit.oauth_client_secret = "dummy-secret"

    async def fake_post_form(url, **kw):
        assert url == reddit.TOKEN_URL
        return 200, types.SimpleNamespace(
            json=lambda: {"access_token": "t-fake", "expires_in": 3600}
        )

    async def fake_get_json(url, **kw):
        assert kw.get("headers", {}).get("Authorization") == "Bearer t-fake"
        if "hot.json" in url:
            return HOT_JSON_SAMPLE
        if "/comments/" in url:
            return COMMENTS_JSON_SAMPLE
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(http, "post_form", fake_post_form)
    monkeypatch.setattr(http, "get_json", fake_get_json)
    monkeypatch.setattr(reddit, "_oauth_cache", {"token": None, "expires_at": 0.0})
    out = asyncio.run(reddit.refresh_reddit(cfg))
    assert out["status"] == "ok"
    assert out["mode"] == "oauth"
    assert out["oauth_fallback"] is False
    assert out["megathreads_skipped"] == 1
    assert out["rows"] == 1
    assert out["thread_bodies_fetched"] == 1  # title matches _HEADLINE_RE, text ≥ 50
    item = dbmod.query_one("SELECT * FROM raw_items WHERE source = 'reddit'")
    assert "TOP COMMENTS" in (item["body"] or "")
    assert "Top comment one" in item["body"]


def test_refresh_reddit_oauth_missing_credentials_fallback(db_path, monkeypatch):
    cfg = ConfigFile()
    cfg.sources.reddit.mode = "oauth"  # but no client id/secret

    async def fake_get_text(url, **kw):
        assert "reddit.com" in url
        return _read("sample_reddit.xml")

    async def no_thread(post_id):
        return None

    async def no_post_form(url, **kw):
        raise AssertionError("token endpoint must not be called without credentials")

    monkeypatch.setattr(http, "get_text", fake_get_text)
    monkeypatch.setattr(http, "post_form", no_post_form)
    monkeypatch.setattr(reddit, "_fetch_thread_body", no_thread)
    monkeypatch.setattr(reddit, "_oauth_cache", {"token": None, "expires_at": 0.0})
    out = asyncio.run(reddit.refresh_reddit(cfg))
    assert out["status"] == "ok"
    assert out["mode"] == "oauth"
    assert out["oauth_fallback"] is True
    assert out["rows"] == 23


def test_refresh_reddit_oauth_fetch_failure_fallback(db_path, monkeypatch):
    """403 from the JSON endpoint (bot-gate) → degrade to RSS, don't lose the source."""
    cfg = ConfigFile()
    cfg.sources.reddit.mode = "oauth"
    cfg.sources.reddit.oauth_client_id = "dummy-id"
    cfg.sources.reddit.oauth_client_secret = "dummy-secret"

    async def fake_post_form(url, **kw):
        return 200, types.SimpleNamespace(
            json=lambda: {"access_token": "t-fake", "expires_in": 3600}
        )

    async def forbidden_get_json(url, **kw):
        raise httpx.HTTPStatusError(
            "403 Forbidden",
            request=httpx.Request("GET", url),
            response=httpx.Response(403),
        )

    rss_urls: list[str] = []

    async def fake_get_text(url, **kw):
        rss_urls.append(url)
        assert "hot.rss" in url
        return _read("sample_reddit.xml")

    async def no_thread(post_id):
        return None

    monkeypatch.setattr(http, "post_form", fake_post_form)
    monkeypatch.setattr(http, "get_json", forbidden_get_json)
    monkeypatch.setattr(http, "get_text", fake_get_text)
    monkeypatch.setattr(reddit, "_fetch_thread_body", no_thread)
    monkeypatch.setattr(reddit, "_oauth_cache", {"token": None, "expires_at": 0.0})
    out = asyncio.run(reddit.refresh_reddit(cfg))
    assert out["status"] == "ok"
    assert out["mode"] == "oauth"
    assert out["oauth_fallback"] is True
    assert out["rows"] == 23
    assert rss_urls  # the RSS path was actually used


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
    # FIX N14: keywords match title OR description
    prio = [i for i in items if not youtube.is_short(i["url"])
            and youtube.priority(i["title"], i["description"], kw) == 1]
    assert len(prio) == 4  # Deadline Stream, Weekender ep.48, 2× CotC
    # description-only matches count (N14): title misses, description hits
    assert youtube.priority("Some generic title",
                            "This week the lads review the Weekender action", kw) == 1
    assert youtube.priority("Some generic title", "Nothing relevant here", kw) == 0


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
    # FIX N13: the backlog pass only targets videos whose extraction already
    # completed (processed = 1) — nothing here qualifies yet (no pipeline ran),
    # so no transcript re-fetch for dupes. The retry itself is covered by
    # test_youtube_backlog_retry_fetches.
    assert out2["backlog_transcripts"] == 0
    assert len(provider.fetched) == 3


# --- FIX N13/N15: transcript backlog retry + provider fallback -------------------


def _yt_backlog_cfg():
    cfg = ConfigFile()
    cfg.sources.youtube.channels = []           # skip the RSS stage entirely
    cfg.sources.youtube.transcript_keywords = ["weekender"]
    cfg.sources.youtube.max_transcripts_per_poll = 3
    cfg.sources.youtube.llm_truncate_chars = 12000
    return cfg


class _ScriptedProvider:
    def __init__(self, text=None):
        self.text = text
        self.calls: list[str] = []

    def fetch(self, video_id):
        self.calls.append(video_id)
        return self.text


def _insert_backlog_video(external_id="vid1", attempts=0, marker=False):
    return dbmod.execute(
        """INSERT INTO raw_items (source, external_id, kind, title, url, published_at,
           body, content_hash, processed, extract_attempts, retrieved_at)
           VALUES ('youtube', ?, 'video', 'Weekender ep.50',
                   'https://www.youtube.com/watch?v=' || ?, '2026-09-19T12:00:00Z',
                   ?, 'hash-' || ?, 1, ?, ?)""",
        (external_id, external_id,
         "The pre-show description" + (" --- TRANSCRIPT --- x" if marker else ""),
         external_id, attempts, dbmod.now_utc()),
    )


def test_youtube_backlog_retry_fetches(db_path):
    """FIX N13: a processed priority video whose transcript failed once is
    retried on a later poll — body updated with the marker, attempt counted,
    item re-queued for extraction."""
    item_id = _insert_backlog_video()
    provider = _ScriptedProvider("FULL TRANSCRIPT TEXT")
    out = asyncio.run(youtube.refresh_youtube(_yt_backlog_cfg(), provider=provider))
    assert out["status"] == "ok"
    assert out["backlog_transcripts"] == 1
    assert provider.calls == ["vid1"]
    row = dbmod.query_one("SELECT * FROM raw_items WHERE id = ?", (item_id,))
    assert row["extract_attempts"] == 1          # the attempt was counted
    assert row["processed"] == 0                 # re-queued for extraction
    assert "--- TRANSCRIPT ---" in row["body"]
    assert "FULL TRANSCRIPT TEXT" in row["body"]


def test_youtube_backlog_retry_failing_provider_bumps_attempts(db_path):
    item_id = _insert_backlog_video(attempts=1)
    provider = _ScriptedProvider(None)           # yt-dlp style: no captions
    out = asyncio.run(youtube.refresh_youtube(_yt_backlog_cfg(), provider=provider))
    assert out["backlog_transcripts"] == 0
    row = dbmod.query_one("SELECT * FROM raw_items WHERE id = ?", (item_id,))
    assert row["extract_attempts"] == 2          # attempts bump even on failure
    assert row["processed"] == 1                 # still processed (no requeue here)


def test_youtube_backlog_retry_stops_after_three_attempts(db_path):
    _insert_backlog_video(attempts=3)
    provider = _ScriptedProvider("should not be called")
    out = asyncio.run(youtube.refresh_youtube(_yt_backlog_cfg(), provider=provider))
    assert provider.calls == []
    assert out["backlog_transcripts"] == 0


def test_youtube_backlog_retry_needs_priority(db_path):
    """Non-priority videos stay untouched by the backlog pass."""
    item_id = dbmod.execute(
        """INSERT INTO raw_items (source, external_id, kind, title, url, published_at,
           body, content_hash, processed, extract_attempts, retrieved_at)
           VALUES ('youtube', 'vid2', 'video', 'A random highlight clip',
                   'https://www.youtube.com/watch?v=vid2', '2026-09-19T12:00:00Z',
                   'Description without keywords', 'hash2', 1, 0, ?)""",
        (dbmod.now_utc(),),
    )
    provider = _ScriptedProvider("nope")
    asyncio.run(youtube.refresh_youtube(_yt_backlog_cfg(), provider=provider))
    assert provider.calls == []
    row = dbmod.query_one("SELECT * FROM raw_items WHERE id = ?", (item_id,))
    assert row["extract_attempts"] == 0


def test_youtube_backlog_uses_api_provider_on_retry(db_path, monkeypatch):
    """FIX N15: on the second attempt (extract_attempts ≥ 1) the
    youtube-transcript-api backend gets a shot instead of yt-dlp."""
    import sys
    import types as _t

    _insert_backlog_video(attempts=1)
    monkeypatch.setitem(sys.modules, "youtube_transcript_api", _t.SimpleNamespace())

    class FakeApiProvider(_ScriptedProvider):
        pass

    api = FakeApiProvider("API TRANSCRIPT")
    monkeypatch.setattr(youtube, "TranscriptApiProvider", lambda: api)
    base = _ScriptedProvider("YT-DLP TEXT")
    out = asyncio.run(youtube.refresh_youtube(_yt_backlog_cfg(), provider=base))
    assert out["backlog_transcripts"] == 1
    assert base.calls == []                       # yt-dlp skipped on retry
    assert api.calls == ["vid1"]


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