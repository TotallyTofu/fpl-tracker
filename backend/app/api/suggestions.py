"""Suggestions: generate 2–3 rule-valid lineups with diffs, transfer math, chip advice."""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException

from .. import season as season_svc
from ..config import load_settings
from ..db import execute, now_utc, query, query_one
from ..optimizer import transfers as tmath
from ..optimizer.solver import PROFILES, SolveParams, dedupe_profiles, solve

log = logging.getLogger("fpl.api.suggestions")
router = APIRouter()


def _current_signals() -> dict[int, list[dict]]:
    """Active (non-expired) signals grouped by player. M1: empty; M2: live."""
    rows = query(
        "SELECT * FROM signals WHERE (expires_at IS NULL OR expires_at > ?) ORDER BY retrieved_at DESC",
        (now_utc(),),
    )
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(r["player_id"], []).append(dict(r))
    return out


def _current_squad_for_diff(lid: int) -> list[dict]:
    return query(
        """SELECT lp.player_id, p.web_name, p.now_cost, lp.bought_cost
           FROM lineup_players lp JOIN players p ON p.id = lp.player_id
           WHERE lp.lineup_id = ?""",
        (lid,),
    )


@router.post("/suggestions/generate")
async def generate(body: dict) -> dict:
    lid = body.get("lineup_id")
    lineup = query_one("SELECT * FROM lineups WHERE id = ?", (lid,)) if lid else None
    if not lineup:
        raise HTTPException(422, "lineup_id missing or not found")
    season = season_svc.current_season()
    target_gw = season["next_gw"] or season["current_gw"]
    if not target_gw:
        raise HTTPException(503, "no season data yet — refresh FPL first")

    settings = load_settings()
    chips = json.loads(lineup["chips"])
    current_squad = _current_squad_for_diff(lid)
    signals = _current_signals()

    results = {}
    for profile in PROFILES:
        params = SolveParams(
            current_squad=current_squad,
            bank=lineup["transfer_bank"],
            chips=chips,
            target_gw=target_gw,
            profile=profile,
            cfg=settings.config,
            signals_by_player=signals,
        )
        try:
            results[profile] = solve(params)
        except Exception as e:
            log.exception("profile %s failed", profile)
            results[profile] = None

    if not any(results.values()):
        raise HTTPException(500, "all profiles failed to solve — check FPL data freshness")

    dedupe_profiles({k: v for k, v in results.items() if v})

    out = []
    for profile in PROFILES:
        s = results.get(profile)
        if s is None:
            continue
        diff = tmath.compute_diff(current_squad, s.squad, lineup["transfer_bank"], chips)
        advice = tmath.chip_advice_v1(diff, chips, season["current_gw"], season["next_gw"],
                                      settings.config)
        rationale = {
            "per_player": {},
            "notes": s.notes,
        }
        if s.variant_of:
            rationale["notes"].insert(0, f"variant of {s.variant_of} — same best team found")
        ts = now_utc()
        sid = execute(
            """INSERT INTO suggestions (lineup_id, profile, variant_of, generated_at, target_gw,
               projected_points, objective, diff, chip_advice, rationale, raw_lineup)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                lid, profile, s.variant_of, ts, target_gw,
                json.dumps(s.projected_points), s.objective, json.dumps(diff),
                json.dumps(advice), json.dumps(rationale), json.dumps(s.squad),
            ),
        )
        out.append(
            {
                "id": sid,
                "profile": profile,
                "variant_of": s.variant_of,
                "generated_at": ts,
                "target_gw": target_gw,
                "projected_points": s.projected_points,
                "objective": s.objective,
                "diff": diff,
                "chip_advice": advice,
                "rationale": rationale,
                "lineup": {
                    "squad": s.squad,
                    "xi": s.xi,
                    "captain": s.captain,
                    "vice_captain": s.vice_captain,
                    "bench": s.bench,
                },
            }
        )
    return {"suggestions": out, "target_gw": target_gw}


@router.get("/suggestions")
async def list_suggestions(lineup_id: int | None = None) -> dict:
    sql = "SELECT * FROM suggestions"
    params: list = []
    if lineup_id is not None:
        sql += " WHERE lineup_id = ?"
        params.append(lineup_id)
    sql += " ORDER BY id DESC LIMIT 50"
    rows = query(sql, params)
    for r in rows:
        for k in ("projected_points", "diff", "chip_advice", "rationale", "raw_lineup"):
            r[k] = json.loads(r[k])
    return {"suggestions": rows}


@router.delete("/suggestions/{sid}")
async def delete_suggestion(sid: int) -> dict:
    if not query_one("SELECT id FROM suggestions WHERE id = ?", (sid,)):
        raise HTTPException(404, "suggestion not found")
    execute("DELETE FROM suggestions WHERE id = ?", (sid,))
    return {"deleted": sid}