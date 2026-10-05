"""r/FantasyPL fetcher (PLAN-3 T2.4).

Handles the V5 gotchas: pinned moderator megathreads (skipped), image-only
posts (stored title-only when the text is < 40 chars), and best-effort
thread-body fetches for headline posts (max 5 per poll).

Two modes (config.sources.reddit.mode):
- "rss" (default): hot.rss + per-thread .rss comment fetches. Zero credentials.
- "oauth": client-credentials (app-only) flow — a free "script" app from
  reddit.com/prefs/apps, no redirect URI, no user login. Fetches hot.json and
  per-thread comments JSON via oauth.reddit.com with a bearer token (cached
  in memory until ~5 min before expiry). Falls back to RSS with a warning
  when credentials are missing, the token request fails, or the JSON fetch
  is rejected (e.g. 403).
"""
from __future__ import annotations

import base64
import html as html_mod
import logging
import re
import time

import feedparser
import httpx

from ..db import log_poll, now_utc
from ..httpclient import http
from ..signals import ingest

log = logging.getLogger("fpl.fetch.reddit")

HOT_RSS = "https://www.reddit.com/r/FantasyPL/hot.rss"
NEW_RSS = "https://www.reddit.com/r/FantasyPL/new.rss"      # FIX N11
# JSON endpoints go via oauth.reddit.com: www.reddit.com/.json is bot-gated
# and 403s even with a valid bearer token (verified 2026-09-21).
HOT_JSON = "https://oauth.reddit.com/r/FantasyPL/hot.json"
NEW_JSON = "https://oauth.reddit.com/r/FantasyPL/new.json"  # FIX N11
COMMENTS_JSON = "https://oauth.reddit.com/r/FantasyPL/comments/{post_id}.json"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
SOURCE = "reddit"
MODERATOR = "fplmoderator"
# In-memory OAuth token cache: {"token": str | None, "expires_at": epoch float}
_oauth_cache: dict = {"token": None, "expires_at": 0.0}
_HOT_UA = {
    "User-Agent": "fpl-tracker/0.2 (local personal FPL assistant; contact: local)",
}
# FIX N12: the old regex (TRANSFER|INJURY|CONFIRMED|DOUBT|LINEUP|START) missed
# common team-news titles — presser quotes, fitness flags, predicted lineups.
# A9.3 (noted, left as-is — cost/precision trade-off, not a correctness bug):
# LATEST and \bFLAG\b fire on generic titles ("Latest news on X") and cost
# thread-body fetches for posts with no team news; conversely "pre-match
# presser" is missed because PRESSER is whole-word-only.
_HEADLINE_RE = re.compile(
    r"TRANSFER|INJURY|CONFIRMED|DOUBT|LINEUP|START"
    r"|FITNESS|\bFIT\b|RETURN|BENCHED|DROPPED|ROTATION"
    r"|PRESSER|PRESS CONFERENCE|TEAM NEWS|PREDICTED|LATEST|\bFLAG\b",
    re.I,
)
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


async def _oauth_token(cfg) -> str | None:
    """Client-credentials (app-only) token, cached until ~5 min before expiry.

    Never logs the client secret or the access token. Returns None when
    credentials are missing or the token request fails.
    """
    client_id = (cfg.sources.reddit.oauth_client_id or "").strip()
    secret = (cfg.sources.reddit.oauth_client_secret or "").strip()
    if not client_id or not secret:
        return None
    if _oauth_cache["token"] and time.time() < _oauth_cache["expires_at"]:
        return _oauth_cache["token"]
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    try:
        status, r = await http.post_form(
            TOKEN_URL,
            data={"grant_type": "client_credentials", "scope": "read"},
            headers={"Authorization": f"Basic {basic}", "User-Agent": _HOT_UA["User-Agent"]},
        )
    except Exception as e:
        log.warning("reddit oauth token request failed: %s", e)
        return None
    if status != 200:
        log.warning("reddit oauth token request failed: HTTP %s", status)
        return None
    data = r.json()
    _oauth_cache["token"] = data.get("access_token")
    _oauth_cache["expires_at"] = time.time() + int(data.get("expires_in", 3600)) - 300
    return _oauth_cache["token"]


def _items_from_json(data: dict) -> tuple[list[dict], int]:
    """listing JSON (hot or new) → same item shape as parse_feed().

    ingest.parse_published accepts epoch-seconds strings for published_at.
    FIX N12: ``link_flair_text`` is appended to the text so "Team News" /
    "Injury" flair reaches the extractors for free.
    """
    out: list[dict] = []
    skipped = 0
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        if d.get("stickied") or str(d.get("author", "")).lower() == MODERATOR:
            skipped += 1  # pinned megathreads — same rule as the RSS path
            continue
        out.append(
            {
                "post_id": d.get("id", ""),
                "external_id": d.get("id", ""),
                "title": (d.get("title") or "").strip(),
                "text": " ".join(x for x in [d.get("title", ""), d.get("selftext", ""),
                                             d.get("link_flair_text") or ""] if x).strip(),
                "url": "https://www.reddit.com" + (d.get("permalink") or ""),
                "published_at": str(d.get("created_utc", "")) or None,
            }
        )
    return out, skipped


def _merge_items(streams: list[list[dict]]) -> list[dict]:
    """FIX N11: merge the hot and new listings, dropping within-poll
    duplicates (a post listed in both) so thread bodies are not fetched twice.
    ingest still dedupes across polls via content_hash."""
    seen: set[str] = set()
    out: list[dict] = []
    for items in streams:
        for it in items:
            k = it.get("external_id") or it.get("post_id")
            if not k or k in seen:
                continue
            seen.add(k)
            out.append(it)
    return out


async def _get_listing(cfg, url: str, params: dict, token: str) -> tuple[dict, str]:
    """One oauth JSON listing; on 401 re-auth once and retry (N11 helper).

    Returns (data, token) — the token may have been refreshed."""
    headers = {"Authorization": f"Bearer {token}", "User-Agent": _HOT_UA["User-Agent"]}
    try:
        data = await http.get_json(url, params=params, headers=headers)
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 401:
            raise
        _oauth_cache["token"] = None
        token = await _oauth_token(cfg)
        if not token:
            raise
        headers = {"Authorization": f"Bearer {token}", "User-Agent": _HOT_UA["User-Agent"]}
        data = await http.get_json(url, params=params, headers=headers)
    return data, token


async def _fetch_thread_body_oauth(post_id: str, token: str) -> str | None:
    """Top comments via the comments JSON endpoint (replaces per-thread RSS).

    Raises _RateLimited on a 429 so the caller can stop trying more threads.
    """
    url = COMMENTS_JSON.format(post_id=post_id)
    try:
        data = await http.get_json(
            url,
            params={"limit": 30, "sort": "top"},
            headers={"Authorization": f"Bearer {token}", "User-Agent": _HOT_UA["User-Agent"]},
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            log.warning("reddit rate-limited (429) on thread %s; stopping thread fetches", post_id)
            raise _RateLimited from e
        log.warning("reddit thread body fetch failed for %s: %s", post_id, e)
        return None
    except Exception as e:
        log.warning("reddit thread body fetch failed for %s: %s", post_id, e)
        return None
    # The comments endpoint returns [post_listing, comments_listing].
    listing = data[1] if isinstance(data, list) and len(data) > 1 else data
    parts: list[str] = []
    for child in listing.get("data", {}).get("children", []):
        if child.get("kind") != "t1":
            continue
        body = (child.get("data", {}).get("body") or "").strip()
        if body:
            parts.append(body)
    return "\n".join(parts)[:_THREAD_BODY_LIMIT] or None


async def refresh_reddit(cfg=None) -> dict:
    """Fetch r/FantasyPL hot, handle V5 gotchas, ingest. Returns {status, rows, ...}."""
    started = now_utc()
    mode = "rss"
    max_threads = _MAX_THREAD_FETCHES
    if cfg is not None:
        mode = cfg.sources.reddit.mode
        max_threads = _MAX_THREAD_FETCHES
    oauth_fallback = False
    try:
        # FIX N11: poll /new in addition to /hot — team-news posts break in
        # /new with few upvotes and can take hours to surface in /hot.
        token = None
        if mode == "oauth":
            token = await _oauth_token(cfg)
            if not token:
                log.warning(
                    "reddit oauth mode but no usable token (missing client id/secret or "
                    "token request failed) — falling back to RSS"
                )
                oauth_fallback = True

        items_new: list[dict] = []
        if token:
            try:
                data_hot, token = await _get_listing(cfg, HOT_JSON, {"limit": 25}, token)
            except Exception as e:
                # Any oauth fetch failure (403 bot-gate, 5xx, network) →
                # degrade to RSS for this poll instead of losing the source.
                log.warning("reddit oauth fetch failed (%s) — falling back to RSS", e)
                oauth_fallback = True
                token = None
        if token:
            items_hot, skipped_hot = _items_from_json(data_hot)
            skipped = skipped_hot
            try:
                data_new, token = await _get_listing(cfg, NEW_JSON, {"limit": 25}, token)
                items_new, skipped_new = _items_from_json(data_new)
                skipped += skipped_new
            except Exception as e:
                log.warning("reddit /new.json fetch failed (%s) — hot-only this poll", e)
            items = _merge_items([items_hot, items_new])
            tok = token
            thread_body = lambda pid: _fetch_thread_body_oauth(pid, tok)  # noqa: E731
        else:
            xml = await http.get_text(HOT_RSS, headers=_HOT_UA)
            items_hot, skipped_hot = parse_feed(xml)
            skipped = skipped_hot
            try:
                xml_new = await http.get_text(NEW_RSS, headers=_HOT_UA)
                items_new, skipped_new = parse_feed(xml_new)
                skipped += skipped_new
            except Exception as e:
                log.warning("reddit /new.rss fetch failed (%s) — hot-only this poll", e)
            items = _merge_items([items_hot, items_new])
            thread_body = _fetch_thread_body

        stored = 0
        thread_fetched = 0
        rate_limited = False
        for it in items:
            text = it["text"]
            # FIX N12 (cosmetic): text already starts with the title — don't
            # duplicate it in the <40-char branch.
            body = text if len(text) >= 40 else None
            if (
                not rate_limited
                and thread_fetched < max_threads
                and it["post_id"]
                and len(text) >= 50
                and _HEADLINE_RE.search(it["title"] or "")
            ):
                try:
                    extra = await thread_body(it["post_id"])
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
            "mode": mode,
            "oauth_fallback": oauth_fallback,
            "new_listing_items": len(items_new),
        }
    except Exception as e:
        log_poll(SOURCE, "error", error=f"{type(e).__name__}: {e}"[:500], started_at=started)
        log.exception("reddit refresh failed")
        return {"status": "error", "rows": 0, "error": str(e)[:300], "mode": mode}