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


def _expires_for(source: str, now: datetime) -> str:
    if source == "fpl-official":
        return (now + timedelta(days=OFFICIAL_TTL_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (now + timedelta(hours=DEFAULT_TTL_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        expires = _expires_for(s.get("source") or "", now)
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