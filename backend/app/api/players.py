"""Player browser endpoint (picker + search)."""
from __future__ import annotations

from fastapi import APIRouter, Query

from ..db import now_utc, query

router = APIRouter()


@router.get("/players")
async def list_players(
    search: str | None = Query(None, max_length=80),
    pos: int | None = Query(None, ge=1, le=4),
    team: int | None = Query(None, ge=1),
    min_cost: int | None = Query(None, ge=0),
    max_cost: int | None = Query(None, ge=0),
    has_signal: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    sql = "SELECT p.*, t.name AS team_name FROM players p LEFT JOIN teams t ON t.id = p.team WHERE 1=1"
    params: list = []
    if search:
        sql += " AND (p.web_name LIKE ? OR p.known_name LIKE ?)"
        like = f"%{search}%"
        params += [like, like]
    if pos:
        sql += " AND p.element_type = ?"
        params.append(pos)
    if team:
        sql += " AND p.team = ?"
        params.append(team)
    if min_cost is not None:
        sql += " AND p.now_cost >= ?"
        params.append(min_cost)
    if max_cost is not None:
        sql += " AND p.now_cost <= ?"
        params.append(max_cost)
    if has_signal:
        sql += (" AND EXISTS (SELECT 1 FROM signals s WHERE s.player_id = p.id "
                "AND s.expires_at > ?)")
        params.append(now_utc())
    sql += " ORDER BY p.ep_next DESC LIMIT ?"
    params.append(limit)
    rows = query(sql, params)
    for r in rows:
        if r.get("ep_next") is not None:
            r["ep_next"] = round(float(r["ep_next"]), 2)
    return {"players": rows, "count": len(rows)}