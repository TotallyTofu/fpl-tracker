"""BBC Sport football RSS fetcher (PLAN-3 T2.2).

RSS title+description (D13). The generic feed is all football (WSL, EFL,
Scotland, international…) — a PL relevance filter keeps only items that
mention "Premier League", a current PL club (aliases expanded, N17), or a
current FPL player name. FIX N16 adds the PL-specific feed — its items are
PL by definition and skip the filter (dedupe still applies).
Full-article fetch: automatic for new PL-relevant items (sources.bbc.fetch_bodies,
capped by max_bodies_per_poll) + on-demand (POST /api/items/{id}/fetch-body).
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit, urlunsplit

import feedparser
from bs4 import BeautifulSoup

from ..config import load_config
from ..db import log_poll, now_utc, query
from ..httpclient import http
from ..signals import ingest
from ..signals.names import _COMMON_WORDS

log = logging.getLogger("fpl.fetch.bbc")

FEED_URL = "https://feeds.bbci.co.uk/sport/football/rss.xml"
# FIX N16: PL-specific feed — verified live 2026-09-21 (HTTP 200).
PL_FEED_URL = "https://feeds.bbci.co.uk/sport/football/premier-league/rss.xml"
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

# FIX N17: alias ↔ canonical expansion map (alias + canonical lowercased).
_ALIAS_TERMS: dict[str, list[str]] = {}
for _a, _c in _ALIASES.items():
    _ALIAS_TERMS.setdefault(_c.lower(), []).append(_a.lower())
    _ALIAS_TERMS.setdefault(_a.lower(), []).append(_c.lower())


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
    """web_name + known_name terms for the current season (strong PL signal).

    A9.2: a lone common English word that happens to be a player's name
    ("White", "James", "Barnes", "Burns") makes almost any football article
    "PL-relevant" and costs LLM calls. Single-token terms from A19's
    common-word list are dropped here; multi-word names ("Ben White") stay —
    they are specific enough without a club anchor.
    """
    terms: set[str] = set()
    try:
        for p in query("SELECT web_name, known_name FROM players WHERE removed = 0"):
            for k in ("web_name", "known_name"):
                v = p.get(k)
                if v and len(v) >= 4:
                    t = v.strip()
                    if " " not in t and t.lower() in _COMMON_WORDS:
                        continue
                    terms.add(t)
    except Exception:
        pass
    return sorted(terms, key=len, reverse=True)


def is_pl_relevant(title: str, description: str,
                   clubs: list[str] | None = None,
                   players: list[str] | None = None) -> bool:
    """T2.2 filter: 'Premier League' phrase, a club name, or a player name.

    FIX N17 (wired instead of deleted): each club term is expanded through
    _ALIASES before the \\b regex checks, so "Spurs", "Man Utd" and friends
    count as club mentions (a cheap recall gain over short_name coverage).
    """
    text = f"{title or ''} {description or ''}"
    low = text.lower()
    if "premier league" in low:
        return True
    for club in (clubs if clubs is not None else _club_names()):
        if not club:
            continue
        low_club = club.lower()
        for term in [low_club] + _ALIAS_TERMS.get(low_club, []):
            if re.search(rf"\b{re.escape(term)}\b", low):
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


def normalize_bbc_url(url: str) -> str:
    """Drop tracking params (?at_medium=RSS&at_campaign=rss) and fragments.
    Yields the URL the article page actually serves."""
    parts = urlsplit(url or "")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def parse_feed(xml_text: str) -> list[dict]:
    """Parse BBC RSS → candidate item dicts (before relevance filter)."""
    feed = feedparser.parse(xml_text)
    out = []
    for e in feed.entries:
        out.append(
            {
                "title": (e.get("title") or "").strip(),
                "description": (e.get("summary") or e.get("description") or "").strip(),
                "url": normalize_bbc_url(e.get("link") or ""),
                "external_id": e.get("id") or normalize_bbc_url(e.get("link") or "") or "",
                "published_at": (e.get("published_parsed") or e.get("updated_parsed")
                           or e.get("published") or e.get("updated") or None),
            }
        )
    return out


async def refresh_bbc() -> dict:
    """Fetch both feeds (FIX N16: generic football + PL-specific), filter, ingest.

    The generic feed keeps the PL relevance filter (``filtered_out`` counts
    only there); PL-feed items are PL by definition and skip the filter.
    Dedupe still applies across both; the article auto-fetch budget is shared.
    """
    started = now_utc()
    try:
        clubs = _club_names()
        players = _player_terms()
        stored = 0
        filtered_out = 0
        bodies = 0
        pl_feed_ok: bool | None = None
        bbc_cfg = load_config().sources.bbc

        async def _ingest(items: list[dict], require_filter: bool) -> None:
            nonlocal stored, filtered_out, bodies
            for it in items:
                if require_filter and not is_pl_relevant(
                        it["title"], it["description"], clubs, players):
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
                    # Auto-fetch the full article (Bug 1 fix; supersedes D13 "on-demand only").
                    if (bbc_cfg.fetch_bodies and bodies < bbc_cfg.max_bodies_per_poll
                            and it["url"]):
                        full = await fetch_article_body({"url": it["url"]})
                        if full and len(full) > 300:
                            ingest.update_body(rowid, full, mark_pending=True)
                            bodies += 1

        xml = await http.get_text(FEED_URL)
        await _ingest(parse_feed(xml), require_filter=True)
        try:
            pl_xml = await http.get_text(PL_FEED_URL)
            await _ingest(parse_feed(pl_xml), require_filter=False)
            pl_feed_ok = True
        except Exception as e:
            # N16: the PL feed is additive — its failure must not lose the
            # generic feed's results for this poll.
            pl_feed_ok = False
            log.warning("bbc PL feed fetch failed (%s) — generic feed only this poll", e)
        log_poll(SOURCE, "ok", rows=stored, started_at=started)
        return {"status": "ok", "rows": stored, "filtered_out": filtered_out,
                "bodies": bodies, "pl_feed_ok": pl_feed_ok}
    except Exception as e:
        log_poll(SOURCE, "error", error=f"{type(e).__name__}: {e}"[:500], started_at=started)
        log.exception("bbc refresh failed")
        return {"status": "error", "rows": 0, "error": str(e)[:300]}


def extract_article_text(html: str) -> str:
    """Main text of a BBC article page: <article> if present, else <main>,
    else body. (BeautifulSoup — mirrors the user-verified BBC-news.py tool.)"""
    soup = BeautifulSoup(html, "html.parser")
    node = soup.find("article") or soup.find("main") or soup.body
    if node is None:
        return ""
    for tag in node.find_all(["script", "style", "noscript"]):
        tag.decompose()
    paras = [p.get_text(" ", strip=True) for p in node.find_all("p")]
    return re.sub(r"\s+", " ", " ".join(p for p in paras if p)).strip()[:8000]


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