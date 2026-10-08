"""Signal store: dedupe, TTL, expiry (PLAN-3 T2.9).

Dedupe: an ACTIVE signal with the same (player_id, category, source-ref)
within its TTL wins on higher confidence (summary updated when the newer
signal is at least as confident). Official FPL signals live 14 days and are
refreshed on each new official signal for the same player+category.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..db import execute, now_utc, query

DEFAULT_TTL_HOURS = 72
OFFICIAL_TTL_DAYS = 14


def _source_ref(source: str) -> str:
    """'fpl-official' stays; 'bbc:<url>' → 'bbc:<url>' (whole ref dedupes per article)."""
    return source or "unknown"


MAX_TTL_DAYS = 21
_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, _FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _next_deadline_after(ts: datetime) -> datetime | None:
    row = query("SELECT MIN(deadline_time) AS d FROM events WHERE deadline_time > ?",
                (ts.strftime(_FMT),))
    return _parse(row[0]["d"]) if row and row[0]["d"] else None


def _expires_for(source: str, now: datetime, published_at: str | None = None) -> str:
    """v1.0: news lives from its PUBLICATION time — 72 h, or until the next
    deadline after it was published if that is later (team news before an
    international break stays relevant until the break ends), capped at 21
    days. Official FPL news lives 14 days from now (it is refreshed on change).
    """
    if source == "fpl-official":
        return (now + timedelta(days=OFFICIAL_TTL_DAYS)).strftime(_FMT)
    base = _parse(published_at) or now
    if base > now:
        base = now
    end = base + timedelta(hours=DEFAULT_TTL_HOURS)
    deadline = _next_deadline_after(base)
    if deadline is not None and deadline > end:
        end = deadline
    end = min(end, base + timedelta(days=MAX_TTL_DAYS))
    return end.strftime(_FMT)


def save_signals(signals: list[dict]) -> int:
    """Store extracted signals with dedupe + TTL. Returns count inserted/updated.

    Each signal dict: player_id, category, sentiment, confidence, summary,
    source, url, published_at, raw_item_id, model.
    """
    if not signals:
        return 0
    now = datetime.now(timezone.utc)
    now_s = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    count = 0
    for s in signals:
        ref = _source_ref(s.get("source"))
        expires = _expires_for(s.get("source") or "", now, s.get("published_at"))
        if expires <= now_s:
            continue   # already stale when read (e.g. an old item re-analysed)
        existing = query(
            """SELECT id, confidence, summary, expires_at FROM signals
               WHERE player_id = ? AND category = ? AND source = ? AND expires_at > ?""",
            (s["player_id"], s["category"], ref, now_s),
        )
        if existing:
            best = max(existing, key=lambda r: r["confidence"])
            if s["confidence"] > best["confidence"]:
                execute(
                    """UPDATE signals SET confidence = ?, summary = ?, sentiment = ?,
                       url = COALESCE(?, url), published_at = COALESCE(?, published_at),
                       raw_item_id = COALESCE(?, raw_item_id), model = ?, expires_at = ?
                       WHERE id = ?""",
                    (
                        s["confidence"], s["summary"], s["sentiment"], s.get("url"),
                        s.get("published_at"), s.get("raw_item_id"), s.get("model", "rules"),
                        expires, best["id"],
                    ),
                )
            count += 1
        else:
            execute(
                """INSERT INTO signals
                     (player_id, category, sentiment, confidence, summary, source, url,
                      published_at, retrieved_at, expires_at, raw_item_id, model)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    s["player_id"], s["category"], s["sentiment"], s["confidence"],
                    s["summary"], ref, s.get("url"), s.get("published_at"), now_s,
                    expires, s.get("raw_item_id"), s.get("model", "rules"),
                ),
            )
            count += 1
    return count


def active_signals(player_id: int | None = None, team: str | None = None,
                   category: str | None = None, limit: int = 200) -> list[dict]:
    """Active (non-expired) signals joined with player/club context."""
    sql = (
        """SELECT s.*, p.web_name, p.known_name, t.short_name AS team_code
           FROM signals s
           JOIN players p ON p.id = s.player_id
           LEFT JOIN teams t ON t.id = p.team
           WHERE s.expires_at > ?"""
    )
    params: list = [now_utc()]
    if player_id is not None:
        sql += " AND s.player_id = ?"
        params.append(player_id)
    if team:
        sql += " AND t.short_name = ?"
        params.append(team.upper())
    if category:
        sql += " AND s.category = ?"
        params.append(category)
    sql += " ORDER BY s.confidence DESC, s.retrieved_at DESC LIMIT ?"
    params.append(limit)
    return query(sql, params)


def signals_by_player() -> dict[int, list[dict]]:
    """Active (non-expired) signals grouped by player, newest first. What the
    projection (suggestions and the projection log) prices in."""
    rows = query(
        "SELECT * FROM signals WHERE (expires_at IS NULL OR expires_at > ?) ORDER BY retrieved_at DESC",
        (now_utc(),),
    )
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(r["player_id"], []).append(dict(r))
    return out


def expire_stale() -> int:
    """Delete expired signals. Returns count deleted."""
    from ..db import get_conn

    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM signals WHERE expires_at <= ?", (now_utc(),))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()