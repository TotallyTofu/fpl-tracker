"""Lineups CRUD + paste-a-list name matching (PLAN-2 T1.9; v1.0 rules pass).

v1.0:
- ``bank_money``: the money in the bank (£0.1m). Plans then only spend money
  the user has. NULL = unknown (the solver falls back to £100m − squad price).
- ``transfer_bank`` is the free transfers available for gameweek ``bank_gw``
  (0–5). When deadlines pass it rolls forward by +1 per gameweek, capped at 5,
  so a team saved with 0 left has 1 again next week.
- Bench: the substitute goalkeeper always takes bench slot 1 (FPL keeps him
  in his own slot); outfield subs keep their relative order in slots 2–4.
- ``keep`` ("Keep players in lineup"): a per-player flag the plans honour — a
  kept player is never sold, whatever the chip. A save that omits the field
  (``None``) leaves the stored flag alone, like ``bought_cost``.
"""
from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..db import execute, now_utc, query, query_one
from ..optimizer.minutes import attach_start_info
from ..optimizer.rules import BUDGET, validate_lineup
from ..signals.names import match_names

router = APIRouter()

MAX_BANK = 5


def _decrement_chips(lineup_id: int, chips_used: list[str]) -> None:
    """FIX T3: subtract played chips from the lineup's in-hand sets (never below 0)."""
    row = query_one("SELECT chips FROM lineups WHERE id = ?", (lineup_id,))
    if not row:
        return
    chips = json.loads(row["chips"])
    for c in chips_used:
        if chips.get(c, 0) > 0:
            chips[c] -= 1
    execute("UPDATE lineups SET chips = ? WHERE id = ?", (json.dumps(chips), lineup_id))


def _next_gw() -> int | None:
    row = query_one("SELECT id FROM events WHERE is_next = 1")
    if row:
        return row["id"]
    cur = query_one("SELECT id FROM events WHERE is_current = 1")
    return cur["id"] + 1 if cur else None


def roll_bank(lineup: dict) -> int:
    """Free transfers for the upcoming deadline: the stored count plus one per
    deadline passed since ``bank_gw`` (max 5). Persists the rolled value."""
    bank = int(lineup["transfer_bank"])
    since = lineup.get("bank_gw")
    nxt = _next_gw()
    if since is None or nxt is None or nxt <= since:
        if since is None and nxt is not None:
            execute("UPDATE lineups SET bank_gw = ? WHERE id = ?", (nxt, lineup["id"]))
        return bank
    rolled = min(MAX_BANK, bank + (nxt - since))
    execute("UPDATE lineups SET transfer_bank = ?, bank_gw = ? WHERE id = ?",
            (rolled, nxt, lineup["id"]))
    lineup["transfer_bank"] = rolled
    lineup["bank_gw"] = nxt
    return rolled


class LineupPlayerIn(BaseModel):
    player_id: int
    role: Literal["starter", "bench"]
    bench_order: int | None = Field(None, ge=1, le=4)
    is_captain: bool = False
    is_vice_captain: bool = False
    bought_cost: int | None = Field(None, ge=0)  # A15: real purchase price (None = keep/derive)
    keep: bool | None = None                     # None = leave the stored flag as it is


class LineupIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    transfer_bank: int = Field(1, ge=0, le=MAX_BANK)
    bank_money: int | None = Field(None, ge=0, le=2000)   # £0.1m; None = unknown
    chips: dict[str, int] = Field(
        default_factory=lambda: {"wildcard": 2, "freehit": 2, "bboost": 2, "triple_captain": 2}
    )
    kind: Literal["current", "test"] | None = None  # A16: None on update = keep the stored kind
    players: list[LineupPlayerIn]


class ChipPlayIn(BaseModel):
    gw: int = Field(ge=1, le=99)
    chip: Literal["wildcard", "freehit", "bboost", "triple_captain"]


def _normalize_bench(body: LineupIn) -> None:
    """GK sub → bench slot 1; outfield subs keep their order in 2–4."""
    bench = [p for p in body.players if p.role == "bench"]
    if len(bench) != 4:
        return
    types = {r["id"]: r["element_type"] for r in query(
        "SELECT id, element_type FROM players WHERE id IN (%s)" % ",".join("?" * len(bench)),
        [p.player_id for p in bench])}
    gks = [p for p in bench if types.get(p.player_id) == 1]
    if len(gks) != 1:
        return
    outfield = sorted((p for p in bench if p is not gks[0]),
                      key=lambda p: p.bench_order if p.bench_order is not None else 9)
    gks[0].bench_order = 1
    for i, p in enumerate(outfield, start=2):
        p.bench_order = i


def _load_lineup(lid: int) -> dict | None:
    row = query_one("SELECT * FROM lineups WHERE id = ?", (lid,))
    if not row:
        return None
    roll_bank(row)
    row["chips"] = json.loads(row["chips"])
    pps = query(
        """SELECT lp.*, p.web_name, p.element_type, p.team, p.now_cost, p.status, p.can_select,
                  p.ep_next, p.form, p.points_per_game, p.selected_by_percent,
                  p.chance_of_playing_next_round, p.news,
                  t.name AS team_name, t.short_name AS team_short
           FROM lineup_players lp
           JOIN players p ON p.id = lp.player_id
           LEFT JOIN teams t ON t.id = p.team
           WHERE lp.lineup_id = ?
           ORDER BY (lp.role = 'bench'), lp.bench_order, p.ep_next DESC""",
        (lid,),
    )
    for p in pps:   # stored as 0/1 — the API contract (LineupPlayer) is boolean
        p["is_captain"] = bool(p["is_captain"])
        p["is_vice_captain"] = bool(p["is_vice_captain"])
        p["keep"] = bool(p["keep"])
    # v1.1: rotation risk for the next gameweek (display only)
    attach_start_info(pps, _next_gw(), id_key="player_id")
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
    # money in the bank: the user's figure, else the £100m − squad-price estimate
    est = max(0, BUDGET - sum(p["now_cost"] for p in squad))
    row["budget_remaining"] = row["bank_money"] if row.get("bank_money") is not None else est
    row["money_known"] = row.get("bank_money") is not None
    return row


@router.get("/lineups")
async def list_lineups() -> dict:
    rows = query("SELECT id, name, transfer_bank, bank_money, bank_gw, chips, is_current, kind, "
                 "created_at, updated_at FROM lineups ORDER BY id")
    for r in rows:
        roll_bank(r)
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

    FPL allows one chip per gameweek: logging a second, different chip for the
    same GW is refused (409). Deliberately does NOT decrement lineups.chips
    (the checkbox is a toggle — see FIX.MD A14).
    """
    existed = query_one("SELECT 1 AS x FROM chip_plays_log WHERE gw = ? AND chip = ?",
                        (body.gw, body.chip))
    if not existed:
        other = query_one("SELECT chip FROM chip_plays_log WHERE gw = ? AND chip <> ?",
                          (body.gw, body.chip))
        if other:
            raise HTTPException(409, f"{other['chip']} is already logged for GW{body.gw}: "
                                     "FPL allows one chip per gameweek")
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
    _normalize_bench(body)
    _validate_in(body)
    ts = now_utc()
    lid = execute(
        "INSERT INTO lineups (name, transfer_bank, bank_money, bank_gw, chips, is_current, kind, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (body.name, body.transfer_bank, body.bank_money, _next_gw(), json.dumps(body.chips), 0,
         body.kind or "current", ts, ts),
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
    _normalize_bench(body)
    _validate_in(body)
    execute(
        "UPDATE lineups SET name = ?, transfer_bank = ?, bank_money = ?, bank_gw = ?, chips = ?, "
        "kind = ?, updated_at = ? WHERE id = ?",
        (body.name, body.transfer_bank, body.bank_money, _next_gw(), json.dumps(body.chips),
         body.kind or row["kind"], now_utc(), lid),
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
    execute("UPDATE lineups SET is_current = 0 WHERE is_current = 1")
    execute("UPDATE lineups SET is_current = 1 WHERE id = ?", (lid,))
    return {"current": lid}


@router.post("/lineups/{lid}/duplicate", status_code=201)
async def duplicate_lineup(lid: int, body: dict | None = None) -> dict:
    """T4.3: sandbox copy of a lineup — same squad/bank/money/chips and
    purchase prices, kind='test', not current."""
    src = _load_lineup(lid)
    if not src:
        raise HTTPException(404, "lineup not found")
    name = (body or {}).get("name") or f"{src['name']} (test)"
    ts = now_utc()
    new_id = execute(
        "INSERT INTO lineups (name, transfer_bank, bank_money, bank_gw, chips, is_current, kind, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (name[:100], src["transfer_bank"], src.get("bank_money"), src.get("bank_gw"),
         json.dumps(src["chips"]), 0, "test", ts, ts),
    )
    _save_players(
        new_id,
        LineupIn(
            name=name[:100],
            transfer_bank=src["transfer_bank"],
            bank_money=src.get("bank_money"),
            chips=src["chips"],
            kind="test",
            players=[
                LineupPlayerIn(
                    player_id=p["player_id"], role=p["role"], bench_order=p["bench_order"],
                    is_captain=p["is_captain"], is_vice_captain=p["is_vice_captain"],
                    bought_cost=p["bought_cost"], keep=p["keep"],
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
    price is the snapshot for a brand-new row. ``keep`` works the same way: an
    explicit flag wins, else the stored flag survives, else 0."""
    costs = {r["id"]: r["now_cost"] for r in query("SELECT id, now_cost FROM players")}
    old = query("SELECT player_id, bought_cost, keep FROM lineup_players WHERE lineup_id = ?", (lid,))
    prev = {r["player_id"]: r["bought_cost"] for r in old}
    prev_keep = {r["player_id"]: bool(r["keep"]) for r in old}
    execute("DELETE FROM lineup_players WHERE lineup_id = ?", (lid,))  # after the read above
    rows = []
    for p in body.players:
        paid = p.bought_cost if p.bought_cost is not None else prev.get(p.player_id)
        if paid is None:
            paid = costs.get(p.player_id)
        keep = p.keep if p.keep is not None else prev_keep.get(p.player_id, False)
        rows.append(
            (lid, p.player_id, p.role, p.bench_order, 1 if p.is_captain else 0,
             1 if p.is_vice_captain else 0, paid, 1 if keep else 0)
        )
    from ..db import execute_many
    execute_many(
        "INSERT INTO lineup_players (lineup_id, player_id, role, bench_order, is_captain, "
        "is_vice_captain, bought_cost, keep) VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
