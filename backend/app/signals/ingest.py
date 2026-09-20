"""Raw-item ingestion: dedupe-and-store (PLAN-3 T2.1).

Every fetcher goes through ``ingest_item`` — no direct ``raw_items`` writes
elsewhere. Dedupe key is (source, content_hash) where content_hash covers
source + normalized title + first 2000 chars of body, so the same story with
a new URL still dedupes (title-only stability) while a title change counts as
a new item.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import time
from datetime import datetime, timezone

from ..db import execute, now_utc, query

_BODY_LIMIT = 200_000  # transcripts ~75 KB are fine; hard cap at storage


def content_hash(source: str, title: str | None, body: str | None) -> str:
    """sha256 of f"{source}|{title.strip().lower()}|{(body or '')[:2000].strip().lower()}"."""
    norm = f"{source}|{(title or '').strip().lower()}|{(body or '')[:2000].strip().lower()}"
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def parse_published(raw: str | None) -> str | None:
    """Normalize a published timestamp to UTC ISO ``YYYY-MM-DDTHH:MM:SSZ``.

    Accepts ISO-8601 (with/without Z, with offsets) and epoch seconds;
    returns None when unparseable (stored as NULL per T2.1).
    """
    if not raw:
        return None
    # feedparser *_parsed fields are time.struct_time (UTC)
    if isinstance(raw, time.struct_time):
        return datetime.fromtimestamp(calendar.timegm(raw), tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    s = str(raw).strip()
    try:
        if s.isdigit():
            dt = datetime.fromtimestamp(int(s), tz=timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OverflowError, OSError):
        return None


def ingest_item(
    source: str,
    external_id: str,
    kind: str,
    title: str | None,
    url: str | None,
    published_at: str | None,
    body: str | None = None,
    takeaways: list[str] | None = None,
) -> int | None:
    """Insert a raw item; returns the new row id, or None when it is a duplicate.

    New items are stored with ``processed=0`` (extraction queue).
    """
    pub = parse_published(published_at)
    stored_body = body[:_BODY_LIMIT] if body else None
    h = content_hash(source, title, body)
    rowid = execute(
        """INSERT INTO raw_items
             (source, external_id, kind, title, url, published_at, body,
              content_hash, takeaways, retrieved_at, processed)
           VALUES (?,?,?,?,?,?,?,?,?,?,0)
           ON CONFLICT(source, content_hash) DO NOTHING""",
        (
            source,
            external_id,
            kind,
            title,
            url,
            pub,
            stored_body,
            h,
            json.dumps(takeaways) if takeaways else None,
            now_utc(),
        ),
    )
    return rowid or None


def update_body(item_id: int, body: str | None, mark_pending: bool = True) -> None:
    """Replace an item's body (on-demand article fetch, transcript fetch).

    ``mark_pending`` re-queues the item for extraction (processed=0).
    """
    execute(
        "UPDATE raw_items SET body = ?, processed = ? WHERE id = ?",
        (body[:_BODY_LIMIT] if body else None, 0 if mark_pending else 1, item_id),
    )


def set_takeaways(item_id: int, takeaways: list[str]) -> None:
    execute("UPDATE raw_items SET takeaways = ? WHERE id = ?", (json.dumps(takeaways), item_id))


def pending_items(limit: int = 50) -> list[dict]:
    """Unprocessed items, oldest first (extraction queue)."""
    return query(
        "SELECT * FROM raw_items WHERE processed = 0 ORDER BY retrieved_at ASC, id ASC LIMIT ?",
        (limit,),
    )


def mark_processed(item_id: int, takeaways: list[str] | None = None) -> None:
    execute(
        "UPDATE raw_items SET processed = 1, takeaways = COALESCE(?, takeaways) WHERE id = ?",
        (json.dumps(takeaways) if takeaways else None, item_id),
    )