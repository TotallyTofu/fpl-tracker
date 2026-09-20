"""News & signals endpoints (PLAN-3 T2.11 / PLAN.MD §8.5).

GET  /api/signals?player_id=&team=&category=&active=
GET  /api/items?source=&kind=&limit=&offset=
POST /api/items/{id}/fetch-body   (BBC on-demand article, D13)
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException

from ..db import execute, now_utc, query, query_one
from ..fetchers.bbc import fetch_article_body
from ..signals import ingest, store

log = logging.getLogger("fpl.api.news")
router = APIRouter()


@router.get("/signals")
async def list_signals(player_id: int | None = None, team: str | None = None,
                       category: str | None = None, active: bool = True) -> dict:
    if active:
        rows = store.active_signals(player_id=player_id, team=team, category=category)
    else:
        sql = (
            """SELECT s.*, p.web_name, t.short_name AS team_code
               FROM signals s
               JOIN players p ON p.id = s.player_id
               LEFT JOIN teams t ON t.id = p.team"""
        )
        params: list = []
        if player_id is not None:
            sql += " WHERE s.player_id = ?"
            params.append(player_id)
        if team:
            sql += ("" if player_id is not None else " WHERE ") + " AND t.short_name = ?"
            params.append(team.upper())
        if category:
            sql += " AND s.category = ?"
            params.append(category)
        sql += " ORDER BY s.retrieved_at DESC LIMIT 500"
        rows = query(sql, params)
    return {"signals": rows, "count": len(rows)}


@router.get("/items")
async def list_items(source: str | None = None, kind: str | None = None,
                     limit: int = 50, offset: int = 0) -> dict:
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    sql = "SELECT * FROM raw_items"
    where: list[str] = []
    params: list = []
    if source:
        where.append("source = ?")
        params.append(source)
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY COALESCE(published_at, retrieved_at) DESC, id DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    rows = query(sql, params)
    for r in rows:
        r["takeaways"] = json.loads(r["takeaways"]) if r.get("takeaways") else []
        r["processed"] = bool(r["processed"])
        r["body"] = (r.get("body") or "")[:1000]  # preview; full body via fetch-body UI
    return {"items": rows, "count": len(rows)}


@router.post("/signals/clear")
async def clear_signals() -> dict:
    """Delete all extracted signals (Settings → Data). Items are kept."""
    n = query_one("SELECT COUNT(*) AS n FROM signals")["n"]
    execute("DELETE FROM signals")
    return {"cleared": n}


@router.post("/items/{item_id}/fetch-body")
async def fetch_body(item_id: int) -> dict:
    """On-demand full-article fetch (BBC, D13). Updates body + re-queues extraction."""
    item = query_one("SELECT * FROM raw_items WHERE id = ?", (item_id,))
    if not item:
        raise HTTPException(404, "item not found")
    if item["source"] != "bbc" or not item.get("url"):
        raise HTTPException(404, "item has no fetchable URL")
    body = await fetch_article_body(item)
    if body is None:
        raise HTTPException(502, "article fetch failed (non-fatal — title-level text kept)")
    ingest.update_body(item_id, body, mark_pending=True)
    return {"body": body[:2000], "truncated": len(body) > 2000}