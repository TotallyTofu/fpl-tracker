"""ESPN PL news fetcher (PLAN-3 T2.3; v1.0: on by default + full stories).

The news endpoint answers with a browser User-Agent (re-verified 2026-10-05:
200, 50 stories, 45 Premier League). Headlines JSON → PL-relevant items in
raw_items; for new items the full story comes from ESPN's content API
(``content.core.api.espn.com/v1/sports/news/{id}``), capped per poll
(``sources.espn.max_bodies_per_poll``) — the headline blurb alone is ~150
characters, the injury round-ups are 5k+.
"""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from ..config import load_config
from ..db import log_poll, now_utc
from ..fetchers.bbc import is_pl_relevant
from ..httpclient import http
from ..signals import ingest

log = logging.getLogger("fpl.fetch.espn")

NEWS_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/news"
STORY_URL = "https://content.core.api.espn.com/v1/sports/news/{id}"
SOURCE = "espn"
_BROWSER_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json",
}
_STORY_LIMIT = 12000


def parse_news(payload: dict) -> list[dict]:
    """articles[] → candidate item dicts (empty-safe)."""
    out = []
    for a in payload.get("articles") or []:
        links = a.get("links") or {}
        web = (links.get("web") or {}).get("href") or ""
        out.append(
            {
                "external_id": str(a.get("id") or web),
                "story_id": a.get("id"),
                "title": (a.get("headline") or "").strip(),
                "description": (a.get("description") or "").strip(),
                "url": web,
                "published_at": a.get("published") or a.get("lastModified"),
            }
        )
    return out


def story_text(payload: dict) -> str:
    """Plain text of a content-API story (``headlines[0].story`` is HTML)."""
    heads = payload.get("headlines") or [payload]
    html = (heads[0] or {}).get("story") or ""
    if not html:
        return ""
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()[:_STORY_LIMIT]


async def fetch_story(story_id) -> str | None:
    """Full story text, or None (non-fatal)."""
    if not story_id:
        return None
    try:
        payload = await http.get_json(STORY_URL.format(id=story_id), headers=_BROWSER_UA)
        return story_text(payload) or None
    except Exception as e:
        log.warning("espn story fetch failed for %s: %s", story_id, e)
        return None


async def refresh_espn() -> dict:
    """Fetch ESPN PL news headlines, filter for PL relevance, ingest, and fetch
    full stories for new items (capped per poll)."""
    started = now_utc()
    try:
        cfg = load_config().sources.espn
        payload = await http.get_json(NEWS_URL, headers=_BROWSER_UA)
        stored = 0
        filtered_out = 0
        bodies = 0
        for it in parse_news(payload):
            if not it["title"]:
                continue
            if not is_pl_relevant(it["title"], it["description"]):
                filtered_out += 1
                continue
            rowid = ingest.ingest_item(
                source=SOURCE,
                external_id=it["external_id"],
                kind="article",
                title=it["title"],
                url=it["url"] or None,
                published_at=it["published_at"],
                body=it["description"] or None,
            )
            if rowid:
                stored += 1
                if (getattr(cfg, "fetch_bodies", True)
                        and bodies < getattr(cfg, "max_bodies_per_poll", 10)):
                    full = await fetch_story(it["story_id"])
                    if full and len(full) > 300:
                        body = f"{it['description']}\n\n{full}" if it["description"] else full
                        ingest.update_body(rowid, body, mark_pending=True)
                        bodies += 1
        log_poll(SOURCE, "ok", rows=stored, started_at=started)
        return {"status": "ok", "rows": stored, "filtered_out": filtered_out, "bodies": bodies}
    except Exception as e:
        log_poll(SOURCE, "error", error=f"{type(e).__name__}: {e}"[:500], started_at=started)
        log.exception("espn refresh failed")
        return {"status": "error", "rows": 0, "error": str(e)[:300]}
