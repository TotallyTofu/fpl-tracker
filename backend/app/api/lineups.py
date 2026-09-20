"""Lineups CRUD + paste-a-list name matching (PLAN-2 T1.9)."""
from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..db import execute, now_utc, query, query_one
from ..optimizer.rules import validate_lineup
from ..signals.names import match_names

router = APIRouter()


class LineupPlayerIn(BaseModel):
    player_id: int
    role: Literal["starter", "bench"]
    bench_order: int | None = Field(None, ge=1, le=4)
    is_captain: bool = False
    is_vice_captain: bool = False


class LineupIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    transfer_bank: int = Field(1, ge=1, le=5)
    chips: dict[str, int] = Field(
        default_factory=lambda: {"wildcard": 2, "freehit": 2, "bboost": 2, "triple_captain": 2}
    )
    players: list[LineupPlayerIn]


def _load_lineup(lid: int) -> dict | None:
    row = query_one("SELECT * FROM lineups WHERE id = ?", (lid,))
    if not row:
        return None
    row["chips"] = json.loads(row["chips"])
    pps = query(
        """SELECT lp.*, p.web_name, p.element_type, p.team, p.now_cost, p.status, p.can_select,
                  p.ep_next, p.selected_by_percent, p.chance_of_playing_next_round,
                  t.name AS team_name
           FROM lineup_players lp
           JOIN players p ON p.id = lp.player_id
           LEFT JOIN teams t ON t.id = p.team
           WHERE lp.lineup_id = ?
           ORDER BY (lp.role = 'bench'), lp.bench_order, p.ep_next DESC""",
        (lid,),
    )
    row["players"] = pps
    squad = [
        {
            "player_id": p["player_id"],
            "web_name": p["web_name"],
            "element_type": p["element_type"],
            "team": p["team"],
            "team_name": p["team_name"],
            "now_cost": p["now_cost"],
            "status": p["status"],
            "can_select": p["can_select"],
            "ep_next": p["ep_next"],
            "selected_by_percent": p["selected_by_percent"],
            "chance_of_playing_next_round": p["chance_of_playing_next_round"],
            "role": p["role"],
            "bench_order": p["bench_order"],
            "is_captain": bool(p["is_captain"]),
            "is_vice_captain": bool(p["is_vice_captain"]),
            "bought_cost": p["bought_cost"],
        }
        for p in pps
    ]
    check = validate_lineup(squad, row["transfer_bank"], row["chips"], strict=False)
    row["validation"] = {"valid": check.valid, "errors": check.errors}
    row["budget_remaining"] = 1000 - sum(p["now_cost"] for p in squad)
    return row


@router.get("/lineups")
async def list_lineups() -> dict:
    rows = query("SELECT id, name, transfer_bank, chips, is_current, created_at, updated_at FROM lineups ORDER BY id")
    for r in rows:
        r["chips"] = json.loads(r["chips"])
    return {"lineups": rows}


@router.post("/lineups", status_code=201)
async def create_lineup(body: LineupIn) -> dict:
    _validate_in(body)
    ts = now_utc()
    lid = execute(
        "INSERT INTO lineups (name, transfer_bank, chips, is_current, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (body.name, body.transfer_bank, json.dumps(body.chips), 0, ts, ts),
    )
    _save_players(lid, body)
    out = _load_lineup(lid)
    if out and not out["players"]:
        execute("DELETE FROM lineups WHERE id = ?", (lid,))
        raise HTTPException(422, {"detail": "no players saved"})
    return out


@router.get("/lineups/{lid}")
async def get_lineup(lid: int) -> dict:
    out = _load_lineup(lid)
    if not out:
        raise HTTPException(404, "lineup not found")
    return out


@router.put("/lineups/{lid}")
async def update_lineup(lid: int, body: LineupIn) -> dict:
    if not query_one("SELECT id FROM lineups WHERE id = ?", (lid,)):
        raise HTTPException(404, "lineup not found")
    _validate_in(body)
    execute(
        "UPDATE lineups SET name = ?, transfer_bank = ?, chips = ?, updated_at = ? WHERE id = ?",
        (body.name, body.transfer_bank, json.dumps(body.chips), now_utc(), lid),
    )
    execute("DELETE FROM lineup_players WHERE lineup_id = ?", (lid,))
    _save_players(lid, body)
    return _load_lineup(lid)


@router.delete("/lineups/{lid}")
async def delete_lineup(lid: int) -> dict:
    if not query_one("SELECT id FROM lineups WHERE id = ?", (lid,)):
        raise HTTPException(404, "lineup not found")
    execute("DELETE FROM lineups WHERE id = ?", (lid,))
    return {"deleted": lid}


@router.post("/lineups/{lid}/set-current")
async def set_current(lid: int) -> dict:
    if not query_one("SELECT id FROM lineups WHERE id = ?", (lid,)):
        raise HTTPException(404, "lineup not found")
    conn = execute("UPDATE lineups SET is_current = 0 WHERE is_current = 1")
    execute("UPDATE lineups SET is_current = 1 WHERE id = ?", (lid,))
    return {"current": lid}


@router.post("/lineups/match-names")
async def match_names_endpoint(body: dict) -> dict:
    names = body.get("names", [])
    if not isinstance(names, list) or not names:
        raise HTTPException(422, "names: non-empty list required")
    return {"matches": match_names([str(n) for n in names])}


def _validate_in(body: LineupIn) -> None:
    """Pre-check the payload against the rules validator; 422 with the exact errors."""
    ids = [p.player_id for p in body.players]
    if len(set(ids)) != len(ids):
        raise HTTPException(422, detail=[
            {"code": "SQUAD_SIZE", "message": "duplicate player_id in payload", "severity": "error"}
        ])
    known = {r["id"]: r for r in query("SELECT id, web_name, element_type, team, now_cost, status, can_select FROM players")}
    squad = []
    for p in body.players:
        row = known.get(p.player_id)
        if not row:
            raise HTTPException(422, detail=[
                {"code": "PLAYER_NOT_FOUND", "message": f"player {p.player_id} not in current season data",
                 "severity": "error"}
            ])
        squad.append({
            "player_id": p.player_id,
            "web_name": row["web_name"],
            "element_type": row["element_type"],
            "team": row["team"],
            "now_cost": row["now_cost"],
            "status": row["status"],
            "can_select": row["can_select"],
            "role": p.role,
            "bench_order": p.bench_order,
            "is_captain": p.is_captain,
            "is_vice_captain": p.is_vice_captain,
        })
    check = validate_lineup(squad, body.transfer_bank, body.chips, strict=False)
    if not check.valid:
        raise HTTPException(422, detail=check.errors)


def _save_players(lid: int, body: LineupIn) -> None:
    costs = {r["id"]: r["now_cost"] for r in query("SELECT id, now_cost FROM players")}
    rows = []
    for p in body.players:
        rows.append(
            (lid, p.player_id, p.role, p.bench_order, 1 if p.is_captain else 0,
             1 if p.is_vice_captain else 0, costs.get(p.player_id))
        )
    from ..db import execute_many
    execute_many(
        "INSERT INTO lineup_players (lineup_id, player_id, role, bench_order, is_captain, is_vice_captain, bought_cost) "
        "VALUES (?,?,?,?,?,?,?)",
        rows,
    )