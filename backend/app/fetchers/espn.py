"""ESPN PL news fetcher (PLAN-3 T2.3).

Disabled by default (V4: 403 from some networks without a browser UA). The
fetcher works when enabled — ESPN's CDN rejects the default client UA, so a
browser UA is sent. Headlines JSON → PL-relevant items in raw_items.
"""
from __future__ import annotations

import logging

from ..db import log_poll, now_utc
from ..fetchers.bbc import is_pl_relevant
from ..httpclient import http
from ..signals import ingest

log = logging.getLogger("fpl.fetch.espn")

NEWS_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/news"
SOURCE = "espn"
_BROWSER_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json",
}


def parse_news(payload: dict) -> list[dict]:
    """articles[] → candidate item dicts (empty-safe)."""
    out = []
    for a in payload.get("articles") or []:
        links = a.get("links") or {}
        web = (links.get("web") or {}).get("href") or ""
        out.append(
            {
                "external_id": str(a.get("id") or web),
                "title": (a.get("headline") or "").strip(),
                "description": (a.get("description") or "").strip(),
                "url": web,
                "published_at": a.get("published") or a.get("lastModified"),
            }
        )
    return out


async def refresh_espn() -> dict:
    """Fetch ESPN PL news headlines, filter for PL relevance, ingest."""
    started = now_utc()
    try:
        payload = await http.get_json(NEWS_URL, headers=_BROWSER_UA)
        stored = 0
        filtered_out = 0
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
        log_poll(SOURCE, "ok", rows=stored, started_at=started)
        return {"status": "ok", "rows": stored, "filtered_out": filtered_out}
    except Exception as e:
        log_poll(SOURCE, "error", error=f"{type(e).__name__}: {e}"[:500], started_at=started)
        log.exception("espn refresh failed")
        return {"status": "error", "rows": 0, "error": str(e)[:300]}