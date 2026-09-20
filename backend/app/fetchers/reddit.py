"""r/FantasyPL RSS fetcher (PLAN-3 T2.4).

Handles the V5 gotchas: pinned moderator megathreads (skipped), image-only
posts (stored title-only when the text is < 40 chars), and best-effort
thread-body fetches for headline posts (max 5 per poll).

OAuth mode (config.sources.reddit.mode = "oauth") is a documented extension:
the code path stub is in place, RSS is the default.
"""
from __future__ import annotations

import html as html_mod
import logging
import re

import feedparser
import httpx

from ..db import log_poll, now_utc
from ..httpclient import http
from ..signals import ingest

log = logging.getLogger("fpl.fetch.reddit")

HOT_RSS = "https://www.reddit.com/r/FantasyPL/hot.rss"
SOURCE = "reddit"
MODERATOR = "fplmoderator"
_HOT_UA = {
    "User-Agent": "fpl-tracker/0.2 (local personal FPL assistant; contact: local)",
}
_HEADLINE_RE = re.compile(r"TRANSFER|INJURY|CONFIRMED|DOUBT|LINEUP|START", re.I)
_MAX_THREAD_FETCHES = 5
_THREAD_BODY_LIMIT = 20_000
_IMG_RE = re.compile(r"<img[^>]*\balt=[\"']([^\"']*)[\"']", re.I)
_P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _link(e) -> str:
    """feedparser link may be a str (Atom) or dict (RSS)."""
    link = e.get("link")
    if isinstance(link, dict):
        return link.get("href") or ""
    return link or ""


def _author(e) -> str:
    a = e.get("author") or ""
    if isinstance(a, dict):
        a = a.get("name") or ""
    return str(a).lower()


def _content_text(e) -> str:
    """Title + <img alt> + <p> text out of the Atom content HTML."""
    parts = [e.get("title") or ""]
    content = e.get("content")
    if isinstance(content, list):
        content = content[0].get("value") if content else ""
    elif not isinstance(content, str):
        content = ""
    if content:
        unescaped = html_mod.unescape(content)
        parts.extend(_IMG_RE.findall(unescaped))
        for p in _P_RE.findall(unescaped):
            parts.append(_TAG_RE.sub(" ", p))
    text = " ".join(x for x in parts if x)
    return re.sub(r"\s+", " ", text).strip()


def parse_feed(xml_text: str) -> tuple[list[dict], int]:
    """Parse the Atom feed → (candidate items, megathreads_skipped)."""
    feed = feedparser.parse(xml_text)
    out: list[dict] = []
    skipped = 0
    for e in feed.entries:
        if MODERATOR in _author(e):
            skipped += 1
            continue
        post_id = ""
        eid = e.get("id") or ""
        m = re.search(r"t3_([A-Za-z0-9]+)", eid)
        if m:
            post_id = m.group(1)
        out.append(
            {
                "post_id": post_id,
                "external_id": post_id or eid or _link(e),
                "title": (e.get("title") or "").strip(),
                "text": _content_text(e),
                "url": _link(e),
                "published_at": (e.get("published_parsed") or e.get("updated_parsed")
                           or e.get("published") or e.get("updated")),
            }
        )
    return out, skipped


class _RateLimited(Exception):
    """Raised when Reddit returns 429; caller should stop further thread fetches."""


async def _fetch_thread_body(post_id: str) -> str | None:
    """Top-level comments (first 30) as text. Best-effort, non-fatal.

    Raises _RateLimited on a 429 so the caller can stop trying more threads
    instead of burning the whole poll on backoff retries.
    """
    url = f"https://www.reddit.com/r/FantasyPL/comments/{post_id}.rss"
    try:
        xml = await http.get_text(url, headers=_HOT_UA)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            log.warning("reddit rate-limited (429) on thread %s; stopping thread fetches", post_id)
            raise _RateLimited from e
        log.warning("reddit thread body fetch failed for %s: %s", post_id, e)
        return None
    except Exception as e:
        log.warning("reddit thread body fetch failed for %s: %s", post_id, e)
        return None
    feed = feedparser.parse(xml)
    parts: list[str] = []
    for e in feed.entries[:30]:
        content = e.get("content")
        if isinstance(content, list):
            content = content[0].get("value") if content else ""
        elif not isinstance(content, str):
            content = ""
        if content:
            text = _TAG_RE.sub(" ", html_mod.unescape(content))
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                parts.append(text)
    return "\n".join(parts)[:_THREAD_BODY_LIMIT] or None


async def refresh_reddit(cfg=None) -> dict:
    """Fetch r/FantasyPL hot, handle V5 gotchas, ingest. Returns {status, rows, ...}."""
    started = now_utc()
    mode = "rss"
    max_threads = _MAX_THREAD_FETCHES
    if cfg is not None:
        mode = cfg.sources.reddit.mode
        max_threads = _MAX_THREAD_FETCHES
    if mode == "oauth":
        # Documented extension point (PLAN-3 T2.4): use app credentials against
        # https://www.reddit.com/r/FantasyPL/hot.json?limit=25 and feed the same
        # downstream path. Not implemented in M2 — fall back to RSS.
        log.info("reddit oauth mode requested; falling back to RSS (M2 implements RSS only)")
    try:
        xml = await http.get_text(HOT_RSS, headers=_HOT_UA)
        items, skipped = parse_feed(xml)
        stored = 0
        thread_fetched = 0
        rate_limited = False
        for it in items:
            text = it["text"]
            body = text if len(text) >= 40 else (it["title"] or None)
            if (
                not rate_limited
                and thread_fetched < max_threads
                and it["post_id"]
                and len(text) >= 50
                and _HEADLINE_RE.search(it["title"] or "")
            ):
                try:
                    extra = await _fetch_thread_body(it["post_id"])
                except _RateLimited:
                    rate_limited = True
                    extra = None
                if extra:
                    body = f"{text}\n\n--- TOP COMMENTS ---\n{extra}"
                    thread_fetched += 1
            rowid = ingest.ingest_item(
                source=SOURCE,
                external_id=it["external_id"],
                kind="thread",
                title=it["title"],
                url=it["url"] or None,
                published_at=it["published_at"],
                body=body,
            )
            if rowid:
                stored += 1
        log_poll(SOURCE, "ok", rows=stored, started_at=started)
        return {
            "status": "ok",
            "rows": stored,
            "megathreads_skipped": skipped,
            "thread_bodies_fetched": thread_fetched,
            "rate_limited": rate_limited,
        }
    except Exception as e:
        log_poll(SOURCE, "error", error=f"{type(e).__name__}: {e}"[:500], started_at=started)
        log.exception("reddit refresh failed")
        return {"status": "error", "rows": 0, "error": str(e)[:300]}