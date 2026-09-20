"""BBC Sport football RSS fetcher (PLAN-3 T2.2).

RSS title+description only (D13). The feed is general football (WSL, EFL,
Scotland, international…) — a PL relevance filter keeps only items that
mention "Premier League", a current PL club, or a current FPL player name.
Full-article fetch is on-demand only (POST /api/items/{id}/fetch-body).
"""
from __future__ import annotations

import logging
import re

import feedparser

from ..db import log_poll, now_utc, query
from ..httpclient import http
from ..signals import ingest

log = logging.getLogger("fpl.fetch.bbc")

FEED_URL = "https://feeds.bbci.co.uk/sport/football/rss.xml"
SOURCE = "bbc"

# Static fallback: 2026/27 clubs (refreshed from the teams table at runtime).
_FALLBACK_CLUBS = [
    "Arsenal", "Aston Villa", "Bournemouth", "Brentford", "Brighton", "Burnley",
    "Chelsea", "Crystal Palace", "Everton", "Fulham", "Leeds", "Liverpool",
    "Manchester City", "Manchester United", "Newcastle", "Nottingham Forest",
    "Sunderland", "Tottenham", "West Ham", "Ipswich",
]
_ALIASES = {
    "Spurs": "Tottenham",
    "Man City": "Manchester City",
    "Man Utd": "Manchester United",
    "Man United": "Manchester United",
    "Forest": "Nottingham Forest",
    "Villa": "Aston Villa",
    "Boro": "Middlesbrough",
}


def _club_names() -> list[str]:
    """Current PL club names + short names from the teams table, with fallback."""
    names: set[str] = set()
    try:
        for t in query("SELECT name, short_name FROM teams"):
            if t.get("name"):
                names.add(t["name"])
            if t.get("short_name"):
                names.add(t["short_name"])
    except Exception:
        pass
    if len(names) < 10:
        names.update(_FALLBACK_CLUBS)
    return sorted(names, key=len, reverse=True)


def _player_terms() -> list[str]:
    """web_name + known_name terms for the current season (strong PL signal)."""
    terms: set[str] = set()
    try:
        for p in query("SELECT web_name, known_name FROM players WHERE removed = 0"):
            for k in ("web_name", "known_name"):
                v = p.get(k)
                if v and len(v) >= 4:
                    terms.add(v)
    except Exception:
        pass
    return sorted(terms, key=len, reverse=True)


def is_pl_relevant(title: str, description: str,
                   clubs: list[str] | None = None,
                   players: list[str] | None = None) -> bool:
    """T2.2 filter: 'Premier League' phrase, a club name, or a player name."""
    text = f"{title or ''} {description or ''}"
    low = text.lower()
    if "premier league" in low:
        return True
    for club in (clubs if clubs is not None else _club_names()):
        if club and re.search(rf"\b{re.escape(club.lower())}\b", low):
            return True
    for player in (players if players is not None else _player_terms()):
        if player and player.lower() in low:
            return True
    return False


def _entry_text(e) -> str:
    """HTML-escaped text out of a feed entry (title + description, tags stripped)."""
    parts = []
    for key in ("title", "summary", "description"):
        v = e.get(key)
        if v:
            parts.append(v)
    text = " ".join(parts)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_feed(xml_text: str) -> list[dict]:
    """Parse BBC RSS → candidate item dicts (before relevance filter)."""
    feed = feedparser.parse(xml_text)
    out = []
    for e in feed.entries:
        out.append(
            {
                "title": (e.get("title") or "").strip(),
                "description": (e.get("summary") or e.get("description") or "").strip(),
                "url": e.get("link") or "",
                "external_id": e.get("id") or e.get("link") or "",
                "published_at": (e.get("published_parsed") or e.get("updated_parsed")
                           or e.get("published") or e.get("updated") or None),
            }
        )
    return out


async def refresh_bbc() -> dict:
    """Fetch the feed, filter, ingest. Returns {status, rows, stored, filtered_out}."""
    started = now_utc()
    try:
        xml = await http.get_text(FEED_URL)
        clubs = _club_names()
        players = _player_terms()
        stored = 0
        filtered_out = 0
        for it in parse_feed(xml):
            if not is_pl_relevant(it["title"], it["description"], clubs, players):
                filtered_out += 1
                continue
            body = _entry_text({"title": it["title"], "summary": it["description"]})
            rowid = ingest.ingest_item(
                source=SOURCE,
                external_id=it["external_id"],
                kind="article",
                title=it["title"],
                url=it["url"] or None,
                published_at=it["published_at"],
                body=body or None,
            )
            if rowid:
                stored += 1
        log_poll(SOURCE, "ok", rows=stored, started_at=started)
        return {"status": "ok", "rows": stored, "filtered_out": filtered_out}
    except Exception as e:
        log_poll(SOURCE, "error", error=f"{type(e).__name__}: {e}"[:500], started_at=started)
        log.exception("bbc refresh failed")
        return {"status": "error", "rows": 0, "error": str(e)[:300]}


_TAG_RE = re.compile(r"<[^>]+>")


def extract_article_text(html: str) -> str:
    """Main text of a BBC article page: <article>/<main> if present, else body text."""
    m = re.search(r"<(?:article|main)[^>]*>(.*?)</(?:article|main)>", html, re.S | re.I)
    chunk = m.group(1) if m else html
    chunk = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", chunk, flags=re.S | re.I)
    text = _TAG_RE.sub(" ", chunk)
    text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&#x27;", "'").replace("&quot;", '"').replace("&nbsp;", " "))
    return re.sub(r"\s+", " ", text).strip()[:8000]


async def fetch_article_body(item: dict) -> str | None:
    """On-demand full-article fetch (D13). Non-fatal; returns None on failure."""
    url = item.get("url")
    if not url:
        return None
    try:
        html = await http.get_text(url)
        return extract_article_text(html) or None
    except Exception as e:
        log.warning("bbc article fetch failed for %s: %s", url, e)
        return None