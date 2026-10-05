"""Planet FPL YouTube fetcher (PLAN-3 T2.5) — two-stage.

Stage A: channel RSS every poll → cheap signals from title + description
(V7: descriptions carry chapter lists & player names).
Stage B: transcripts for priority videos only (yt-dlp, D11), budgeted per
poll. TranscriptProvider protocol allows an API backend (env
YOUTUBE_TRANSCRIPT_BACKEND=api) — VTT files are intermediate artifacts and
are deleted after conversion.
"""
from __future__ import annotations

import html as html_mod
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

import feedparser

from ..config import ConfigFile
from ..db import log_poll, now_utc, query
from ..httpclient import http
from ..signals import ingest

log = logging.getLogger("fpl.fetch.youtube")

SOURCE = "youtube"
_TRANSCRIPT_MARKER = "--- TRANSCRIPT ---"


# --- Transcript providers (D11) --------------------------------------------------


class TranscriptProvider(Protocol):
    def fetch(self, video_id: str) -> str | None: ...


def vtt_to_text(vtt_path: str) -> str:
    """Dedupe VTT → plain text (V9-verified algorithm).

    Drop WEBVTT/Kind:/Language:/timestamp/empty lines, strip <...> tags,
    unescape &amp; &gt; &lt;, drop exact-duplicate lines preserving order.
    """
    out: list[str] = []
    seen: set[str] = set()
    for line in Path(vtt_path).read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith(("WEBVTT", "Kind:", "Language:", "NOTE", "STYLE", "<")):
            continue
        if "-->" in s or re.fullmatch(r"[\d:.]+", s):
            continue
        s = re.sub(r"<[^>]+>", "", s)
        s = (s.replace("&amp;", "&").replace("&gt;", ">").replace("&lt;", "<")
                .replace("&#39;", "'").replace("&quot;", '"'))
        s = s.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return "\n".join(out)


def _yt_dlp_cmd() -> list[str]:
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    return [sys.executable, "-m", "yt_dlp"]


class YtDlpProvider:
    """Default: yt-dlp auto-subs (one --write-sub retry on failure).

    FIX N15: prefer human/original captions ("en,en-orig") — auto-caps are
    noisier for the name matcher."""

    def fetch(self, video_id: str) -> str | None:
        tmp = Path(tempfile.mkdtemp(prefix="fpl-yt-"))
        try:
            for flags in (
                ["--write-auto-sub", "--sub-langs", "en,en-orig"],
                ["--write-sub", "--sub-langs", "en,en-orig"],
            ):
                cmd = [
                    *_yt_dlp_cmd(),
                    "--no-update", *flags,
                    "--skip-download", "--convert-subs", "vtt",
                    "--js-runtimes", "node",
                    "--output", str(tmp / "%(id)s"),
                    f"https://www.youtube.com/watch?v={video_id}",
                ]
                try:
                    subprocess.run(cmd, capture_output=True, timeout=180)
                except (subprocess.TimeoutExpired, OSError) as e:
                    log.warning("yt-dlp failed for %s: %s", video_id, e)
                    continue
                vtt = next(iter(sorted(tmp.glob(f"{video_id}.en*.vtt"))), None)
                if vtt is not None:
                    text = vtt_to_text(str(vtt))
                    vtt.unlink(missing_ok=True)  # VTT is an intermediate artifact
                    if text.strip():
                        return text
            return None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TranscriptApiProvider:
    """Optional: youtube-transcript-api (env YOUTUBE_TRANSCRIPT_BACKEND=api)."""

    def fetch(self, video_id: str) -> str | None:
        try:
            from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
        except ImportError:
            log.warning("YOUTUBE_TRANSCRIPT_BACKEND=api but youtube-transcript-api is not installed")
            return None
        try:
            t = YouTubeTranscriptApi().fetch(video_id, languages=["en"])
            return " ".join(sn.text for sn in t) or None
        except Exception as e:
            log.warning("transcript api failed for %s: %s", video_id, e)
            return None


def get_transcript_provider() -> TranscriptProvider:
    if os.getenv("YOUTUBE_TRANSCRIPT_BACKEND", "").strip().lower() == "api":
        return TranscriptApiProvider()
    return YtDlpProvider()


# --- Stage A: channel RSS --------------------------------------------------------


def _resolve_channel_id(handle: str) -> str | None:
    """Resolve a /handle to a channel id via yt-dlp (one-time, 30 s timeout)."""
    cmd = [*_yt_dlp_cmd(), "--no-update", "--print", "%(channel_id)s",
           f"https://www.youtube.com/{handle}/videos"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        out = r.stdout.decode("utf-8", "ignore").strip().splitlines()
        return out[-1].strip() if out and out[-1].strip() else None
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("channel id resolution failed for %s: %s", handle, e)
        return None


def _entry(e) -> dict | None:
    # feedparser lowercases namespaced keys: yt:videoId → 'yt_videoid'
    video_id = e.get("yt_videoid") or e.get("yt_videoId") or ""
    if not video_id:
        m = re.search(r"yt:video:([A-Za-z0-9_-]+)", e.get("id") or "")
        video_id = m.group(1) if m else ""
    if not video_id:
        return None
    link = ""
    for l in e.get("links") or []:
        if l.get("rel") == "alternate":
            link = l.get("href") or ""
            break
    # media:description maps to 'summary' in feedparser; media_group as fallback
    desc = e.get("summary") or ""
    if not desc:
        group = e.get("media_group") or {}
        if isinstance(group, dict):
            d = group.get("media_description")
            if isinstance(d, list) and d:
                desc = d[0].get("#text") or ""
            elif isinstance(d, str):
                desc = d
    return {
        "video_id": video_id,
        "title": (e.get("title") or "").strip(),
        "url": link or f"https://www.youtube.com/watch?v={video_id}",
        "published_at": (e.get("published_parsed") or e.get("updated_parsed")
                           or e.get("published") or e.get("updated")),
        "description": html_mod.unescape(desc).strip(),
    }


def is_short(url: str) -> bool:
    return "/shorts/" in (url or "")


def priority(title: str, description: str, keywords: list[str]) -> int:
    """1 if the title OR description matches a transcript keyword, else 0.

    FIX N14: descriptions carry chapter lists and show names ("Weekender",
    "COTC" appear in the description far more often than in the title)."""
    hay = f"{(title or '').lower()} {(description or '').lower()}"
    return 1 if any(k.lower() in hay for k in keywords) else 0


def _provider_for(attempts: int, base: TranscriptProvider) -> TranscriptProvider:
    """FIX N15: after one failed yt-dlp attempt for a video (the N13 attempts
    counter), give the youtube-transcript-api backend a shot instead — yt-dlp
    fails wholesale when Node.js is missing, and the API backend is the
    documented alternative (env YOUTUBE_TRANSCRIPT_BACKEND=api)."""
    if attempts >= 1:
        try:
            import youtube_transcript_api  # noqa: F401
        except ImportError:
            return base
        return TranscriptApiProvider()
    return base


def parse_feed(xml_text: str) -> list[dict]:
    feed = feedparser.parse(xml_text)
    out = []
    for e in feed.entries:
        item = _entry(e)
        if item:
            out.append(item)
    return out


async def refresh_youtube(cfg: ConfigFile, provider: TranscriptProvider | None = None) -> dict:
    """Stage A (RSS) + Stage B (budgeted transcripts). Returns {status, rows, ...}."""
    started = now_utc()
    ycfg = cfg.sources.youtube
    provider = provider or get_transcript_provider()
    stored = 0
    transcripts = 0
    try:
        for ch in ycfg.channels:
            channel_id = ch.channel_id
            if not channel_id and ch.handle:
                channel_id = _resolve_channel_id(ch.handle)
                if channel_id:
                    ch.channel_id = channel_id  # persisted by the caller via save_config
            if not channel_id:
                continue
            url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
            xml = await http.get_text(url)
            entries = parse_feed(xml)
            for it in entries:
                if is_short(it["url"]):
                    prio = 0
                else:
                    # FIX N14: match keywords against title AND description
                    prio = priority(it["title"], it["description"], ycfg.transcript_keywords)
                rowid = ingest.ingest_item(
                    source=SOURCE,
                    external_id=it["video_id"],
                    kind="video",
                    title=it["title"],
                    url=it["url"],
                    published_at=it["published_at"],
                    body=it["description"] or None,
                )
                if rowid:
                    stored += 1
                # Stage B: budgeted transcript fetch for new priority videos
                if (
                    prio == 1
                    and rowid
                    and transcripts < ycfg.max_transcripts_per_poll
                    and _TRANSCRIPT_MARKER not in (it["description"] or "")
                ):
                    text = await _to_thread(provider.fetch, it["video_id"])
                    if text:
                        body = (
                            f"{it['description']}\n\n{_TRANSCRIPT_MARKER}\n{text}"
                            if it["description"]
                            else f"{_TRANSCRIPT_MARKER}\n{text}"
                        )
                        ingest.update_body(rowid, body, mark_pending=True)
                        transcripts += 1

        # FIX N13: transcript backlog retry — Stage B used to run on brand-new
        # rows only, so one failed yt-dlp attempt (429, caption missing, no
        # Node.js) lost the transcript forever. Retry recent priority videos
        # without the marker, budget-capped, counting attempts per video
        # (reuses raw_items.extract_attempts, the FIX N10 column).
        backlog_fetched = 0
        if transcripts < ycfg.max_transcripts_per_poll:
            three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            backlog = query(
                """SELECT id, title, body, external_id, extract_attempts FROM raw_items
                   WHERE source = 'youtube' AND kind = 'video' AND processed = 1
                     AND (body IS NULL OR body NOT LIKE '%--- TRANSCRIPT ---%')
                     AND retrieved_at >= ?
                   ORDER BY retrieved_at DESC""",
                (three_days_ago,),
            )
            for row in backlog:
                if transcripts >= ycfg.max_transcripts_per_poll:
                    break
                attempts = int(row.get("extract_attempts") or 0)
                if attempts >= 3:
                    continue  # stop chasing a video after 3 failed attempts
                if not priority(row.get("title") or "", row.get("body") or "",
                                ycfg.transcript_keywords):
                    continue
                if row.get("body") and _TRANSCRIPT_MARKER in row["body"]:
                    continue  # double-fetch guard
                text = await _to_thread(
                    _provider_for(attempts, provider).fetch, row["external_id"])
                ingest.bump_extract_attempts(row["id"])   # count every attempt
                if text:
                    body = (f"{row['body']}\n\n{_TRANSCRIPT_MARKER}\n{text}"
                            if row.get("body") else f"{_TRANSCRIPT_MARKER}\n{text}")
                    ingest.update_body(row["id"], body, mark_pending=True)
                    transcripts += 1
                    backlog_fetched += 1

        log_poll(SOURCE, "ok", rows=stored, started_at=started)
        return {"status": "ok", "rows": stored, "transcripts_fetched": transcripts,
                "backlog_transcripts": backlog_fetched}
    except Exception as e:
        log_poll(SOURCE, "error", error=f"{type(e).__name__}: {e}"[:500], started_at=started)
        log.exception("youtube refresh failed")
        return {"status": "error", "rows": 0, "error": str(e)[:300]}


async def _to_thread(fn, *args):
    """Run a blocking provider fetch off the event loop."""
    import asyncio

    return await asyncio.to_thread(fn, *args)