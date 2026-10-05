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


def _decrement_chips(lineup_id: int, chips_used: list[str]) -> None:
    """FIX T3: subtract played chips from the lineup's in-hand sets.

    Silently skips chips the lineup does not hold (never below 0). MyTeam's
    steppers stay user-editable; this keeps them honest instead of silently
    wrong after a chip is played.
    """
    row = query_one("SELECT chips FROM lineups WHERE id = ?", (lineup_id,))
    if not row:
        return
    chips = json.loads(row["chips"])
    for c in chips_used:
        if chips.get(c, 0) > 0:
            chips[c] -= 1
    execute("UPDATE lineups SET chips = ? WHERE id = ?", (json.dumps(chips), lineup_id))


class LineupPlayerIn(BaseModel):
    player_id: int
    role: Literal["starter", "bench"]
    bench_order: int | None = Field(None, ge=1, le=4)
    is_captain: bool = False
    is_vice_captain: bool = False
    bought_cost: int | None = Field(None, ge=0)  # A15: real purchase price (None = keep/derive)


class LineupIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    transfer_bank: int = Field(1, ge=1, le=5)
    chips: dict[str, int] = Field(
        default_factory=lambda: {"wildcard": 2, "freehit": 2, "bboost": 2, "triple_captain": 2}
    )
    kind: Literal["current", "test"] | None = None  # A16: None on update = keep the stored kind
    players: list[LineupPlayerIn]


class ChipPlayIn(BaseModel):
    gw: int = Field(ge=1, le=99)
    chip: Literal["wildcard", "freehit", "bboost", "triple_captain"]


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
    rows = query("SELECT id, name, transfer_bank, chips, is_current, kind, created_at, updated_at FROM lineups ORDER BY id")
    for r in rows:
        r["chips"] = json.loads(r["chips"])
    return {"lineups": rows}


@router.get("/lineups/chip-plays")
async def list_chip_plays(gw: int | None = None) -> dict:
    """T4.2/T4.3: chips the user played, per GW (drives the Free-Hit ban)."""
    if gw is not None:
        rows = query("SELECT gw, chip, played_at FROM chip_plays_log WHERE gw = ? ORDER BY chip", (gw,))
    else:
        rows = query("SELECT gw, chip, played_at FROM chip_plays_log ORDER BY gw DESC, chip")
    return {"chip_plays": rows}


@router.post("/lineups/chip-play", status_code=201)
async def log_chip_play(body: ChipPlayIn) -> dict:
    """Record that a chip was played in a GW (drives the Free-Hit ban).

    Deliberately does NOT decrement lineups.chips: the checkbox is a toggle, so a
    decrement here double-counts (see FIX.MD A14). Edit the in-hand counts with
    the MetaPanel steppers; `apply_suggestion` is the only automatic decrement,
    and it is guarded by suggestions.applied_at.
    """
    existed = query_one("SELECT 1 AS x FROM chip_plays_log WHERE gw = ? AND chip = ?",
                        (body.gw, body.chip))
    if not existed:
        execute(
            "INSERT INTO chip_plays_log (lineup_id, gw, chip, played_at) VALUES (NULL,?,?,?)",
            (body.gw, body.chip, now_utc()),
        )
    return {"logged": {"gw": body.gw, "chip": body.chip}, "already": bool(existed)}


@router.delete("/lineups/chip-play")
async def unlog_chip_play(gw: int, chip: str) -> dict:
    if chip not in ("wildcard", "freehit", "bboost", "triple_captain"):
        raise HTTPException(422, "unknown chip")
    existed = query_one("SELECT 1 AS x FROM chip_plays_log WHERE gw = ? AND chip = ?", (gw, chip))
    execute("DELETE FROM chip_plays_log WHERE gw = ? AND chip = ?", (gw, chip))
    return {"deleted": bool(existed)}


@router.post("/lineups", status_code=201)
async def create_lineup(body: LineupIn) -> dict:
    _validate_in(body)
    ts = now_utc()
    lid = execute(
        "INSERT INTO lineups (name, transfer_bank, chips, is_current, kind, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (body.name, body.transfer_bank, json.dumps(body.chips), 0, body.kind or "current", ts, ts),
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
    row = query_one("SELECT kind FROM lineups WHERE id = ?", (lid,))
    if not row:
        raise HTTPException(404, "lineup not found")
    _validate_in(body)
    execute(
        "UPDATE lineups SET name = ?, transfer_bank = ?, chips = ?, kind = ?, updated_at = ? WHERE id = ?",
        (body.name, body.transfer_bank, json.dumps(body.chips), body.kind or row["kind"], now_utc(), lid),
    )
    _save_players(lid, body)  # A15: reads the old rows before deleting them
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


@router.post("/lineups/{lid}/duplicate", status_code=201)
async def duplicate_lineup(lid: int, body: dict | None = None) -> dict:
    """T4.3: sandbox copy of a lineup — same squad/bank/chips, kind='test',
    not current. Suggestions run against it without touching the real team."""
    src = _load_lineup(lid)
    if not src:
        raise HTTPException(404, "lineup not found")
    name = (body or {}).get("name") or f"{src['name']} (test)"
    ts = now_utc()
    new_id = execute(
        "INSERT INTO lineups (name, transfer_bank, chips, is_current, kind, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (name[:100], src["transfer_bank"], json.dumps(src["chips"]), 0, "test", ts, ts),
    )
    _save_players(
        new_id,
        LineupIn(
            name=name[:100],
            transfer_bank=src["transfer_bank"],
            chips=src["chips"],
            kind="test",
            players=[
                LineupPlayerIn(
                    player_id=p["player_id"], role=p["role"], bench_order=p["bench_order"],
                    is_captain=p["is_captain"], is_vice_captain=p["is_vice_captain"],
                )
                for p in src["players"]
            ],
        ),
    )
    return _load_lineup(new_id)


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
    """Persist squad rows. bought_cost (A15): an explicit value wins, else the
    previously stored value survives the DELETE-and-reinsert, else the current
    price is the snapshot for a brand-new row."""
    costs = {r["id"]: r["now_cost"] for r in query("SELECT id, now_cost FROM players")}
    prev = {r["player_id"]: r["bought_cost"] for r in query(
        "SELECT player_id, bought_cost FROM lineup_players WHERE lineup_id = ?", (lid,))}
    execute("DELETE FROM lineup_players WHERE lineup_id = ?", (lid,))  # after the read above
    rows = []
    for p in body.players:
        paid = p.bought_cost if p.bought_cost is not None else prev.get(p.player_id)
        if paid is None:
            paid = costs.get(p.player_id)
        rows.append(
            (lid, p.player_id, p.role, p.bench_order, 1 if p.is_captain else 0,
             1 if p.is_vice_captain else 0, paid)
        )
    from ..db import execute_many
    execute_many(
        "INSERT INTO lineup_players (lineup_id, player_id, role, bench_order, is_captain, is_vice_captain, bought_cost) "
        "VALUES (?,?,?,?,?,?,?)",
        rows,
    )