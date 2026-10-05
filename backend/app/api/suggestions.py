"""Suggestions: generate 2–3 rule-valid lineups with diffs, transfer math, chip advice.

v1.0:
- ``chip`` (optional) in the generate body is the chip the user plans to play
  in the target GW. Only then does a Wildcard/Free Hit lift the transfer
  limit, Bench Boost score the bench, or Triple Captain triple the captain.
  Holding a chip changes nothing.
- When a Wildcard or Free Hit is playable but not chosen, one extra quick
  solve estimates what a rebuild would gain this GW; the chip advice uses it.
- Applying a suggestion updates the saved team to the suggested squad, sets
  the free transfers left and the money in the bank, and logs only the chip
  that was actually planned.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException

from .. import season as season_svc
from ..config import load_settings
from ..db import execute, execute_many, now_utc, query, query_one
from ..optimizer import transfers as tmath
from ..optimizer.rules import TRANSFER_CAP
from ..optimizer.scoring import score_lineup
from ..optimizer.solver import PROFILES, SolveParams, _team_fixtures, dedupe_profiles, solve
from .lineups import _decrement_chips, roll_bank

log = logging.getLogger("fpl.api.suggestions")
router = APIRouter()


def _current_signals() -> dict[int, list[dict]]:
    """Active (non-expired) signals grouped by player."""
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


def _current_projection(lid: int, solved, chip: str | None) -> float | None:
    """Projected points if the user keeps the saved team as it is (same model,
    same captain/XI), for the "+X vs no changes" line. Unavailable players
    count 0. None when the lineup has no XI/captain."""
    if solved is None or not solved.scoring_ctx:
        return None
    rows = query("SELECT player_id, role, is_captain, is_vice_captain FROM lineup_players "
                 "WHERE lineup_id = ?", (lid,))
    by_id = {p["id"]: p for p in solved.universe}
    squad = [by_id[r["player_id"]] for r in rows if r["player_id"] in by_id]
    xi = [by_id[r["player_id"]] for r in rows if r["role"] == "starter" and r["player_id"] in by_id]
    cap = next((r["player_id"] for r in rows if r["is_captain"]), None)
    vc = next((r["player_id"] for r in rows if r["is_vice_captain"]), None)
    if not xi or cap is None:
        return None
    signals, cfg, diff_map, _chip, use_ep_next = (tuple(solved.scoring_ctx) + (None, True))[:5]
    score_chip = chip if chip in ("bboost", "triple_captain") else None
    out = score_lineup(squad, xi, cap, vc, signals, cfg,
                       {p["id"]: _team_fixtures(diff_map, p["team"]) for p in squad},
                       chip=score_chip, use_ep_next=use_ep_next)
    return out["adjusted"]


def _rebuild_gain(params: SolveParams, base_adjusted: float, chips: dict,
                  target_gw: int) -> dict[str, float]:
    """Projected extra points this GW from a Wildcard/Free Hit rebuild (one
    quick max_ep solve with the chip). {} when neither chip is playable."""
    playable = [c for c in tmath.TRANSFER_CHIPS
                if tmath.chip_play_problem(c, chips, target_gw) is None]
    if not playable:
        return {}
    cfg = params.cfg.model_copy(deep=True) if hasattr(params.cfg, "model_copy") else params.cfg
    try:
        cfg.optimizer.solver.timebox_sec = min(4, cfg.optimizer.solver.timebox_sec)
    except AttributeError:
        pass
    try:
        rebuilt = solve(SolveParams(
            current_squad=params.current_squad, bank=params.bank, chips=params.chips,
            target_gw=params.target_gw, profile="max_ep", cfg=cfg,
            signals_by_player=params.signals_by_player, chip_played=playable[0],
            bank_money=params.bank_money, use_ep_next=params.use_ep_next,
        ))
    except Exception:
        log.exception("rebuild-gain estimate failed (chip advice falls back)")
        return {}
    gain = round(rebuilt.projected_points["adjusted"] - base_adjusted, 1)
    return {c: gain for c in playable}


@router.post("/suggestions/generate")
async def generate(body: dict) -> dict:
    lid = body.get("lineup_id")
    lineup = query_one("SELECT * FROM lineups WHERE id = ?", (lid,)) if lid else None
    if not lineup:
        raise HTTPException(422, "lineup_id missing or not found")
    requested = body.get("target_gw")
    season = season_svc.current_season()
    target_gw = requested or season["next_gw"] or season["current_gw"]
    if not target_gw:
        raise HTTPException(503, "no season data yet — refresh FPL first")
    if requested is not None:
        total = season.get("events_total") or 38
        if not (1 <= int(requested) <= total):
            raise HTTPException(422, f"target_gw must be 1–{total}")
    target_gw = int(target_gw)

    settings = load_settings()
    chips = json.loads(lineup["chips"])
    chip = body.get("chip") or None
    if chip is not None:
        problem = tmath.chip_play_problem(chip, chips, target_gw)
        if problem:
            raise HTTPException(422, f"can't play {tmath.CHIP_LABELS.get(chip, chip)} in "
                                     f"GW{target_gw}: {problem}")
    bank = roll_bank(lineup)
    bank_money = lineup.get("bank_money")
    current_squad = _current_squad_for_diff(lid)
    signals = _current_signals()
    chip_covers = tmath.chip_covers_transfers(chips, target_gw, chip)
    use_ep_next = season["next_gw"] is None or target_gw == season["next_gw"]

    results = {}
    params_by_profile = {}
    for profile in PROFILES:
        params = SolveParams(
            current_squad=current_squad,
            bank=bank,
            chips=chips,
            target_gw=target_gw,
            profile=profile,
            cfg=settings.config,
            signals_by_player=signals,
            lineup_id=lid,
            chip_played=chip,
            bank_money=bank_money,
            use_ep_next=use_ep_next,
        )
        params_by_profile[profile] = params
        try:
            results[profile] = solve(params)
        except Exception:
            log.exception("profile %s failed", profile)
            results[profile] = None

    if not any(results.values()):
        raise HTTPException(500, "all profiles failed to solve — check FPL data freshness")

    dedupe_profiles({k: v for k, v in results.items() if v},
                    tctx=next(v.tctx for v in results.values() if v))

    chip_gains: dict[str, float] = {}
    if chip is None and results.get("max_ep") is not None and len(current_squad) == 15:
        chip_gains = _rebuild_gain(params_by_profile["max_ep"],
                                   results["max_ep"].projected_points["adjusted"],
                                   chips, target_gw)

    current_proj = _current_projection(lid, next((v for v in results.values() if v), None), chip)

    out = []
    for profile in PROFILES:
        s = results.get(profile)
        if s is None:
            continue
        diff = tmath.compute_diff(
            current_squad, s.squad, bank, chips,
            chip_covers=chip_covers,
            ep_by_player={p["id"]: p["ep"] for p in s.universe},
            bank_money=bank_money, chip_played=chip,
        )
        transfers_n = max(len(diff["transfers_in"]), len(diff["transfers_out"]))
        cap_ok = transfers_n <= TRANSFER_CAP or chip_covers
        diff["transfer_cap_exceeded"] = not cap_ok
        if not cap_ok:
            log.warning("profile %s: %d transfers exceed the %d-transfer cap without a chip — flagged",
                        profile, transfers_n, TRANSFER_CAP)
        # D3: the headline score must show the transfer penalty honestly.
        projected = dict(s.projected_points)          # {baseline, adjusted, with_captain}
        projected["penalty_points"] = diff["penalty_points"]
        projected["net_after_transfers"] = round(s.projected_points["adjusted"] - diff["penalty_points"], 1)
        projected["current_team"] = current_proj
        try:
            advice = tmath.chip_advice_v2(diff, chips, s, season["current_gw"],
                                          target_gw, settings.config, signals, chip_gains)
        except Exception:
            log.exception("chip_advice_v2 failed — falling back to v1")
            advice = tmath.chip_advice_v1(diff, chips, season["current_gw"],
                                          target_gw, settings.config)
        rationale = {
            "per_player": {},
            "notes": list(s.notes),
        }
        if not cap_ok:
            rationale["notes"].insert(0, (
                f"WARNING: {transfers_n} transfers exceeds the {TRANSFER_CAP}-transfer hard cap "
                "and no chip covers this GW — invalid in FPL, do not apply"
            ))
        if diff["penalty_points"] > 0:
            sctx = getattr(s, "tctx", None)
            forced = sctx.forced_transfers if sctx is not None else 0
            if forced and transfers_n <= max(bank, forced):
                msg = (f"{forced} unavailable player(s) must be replaced and that exceeds your "
                       f"{bank} free transfer(s): −{diff['penalty_points']} points.")
            else:
                msg = (f"{transfers_n} transfers is {transfers_n - bank} more than your {bank} free: "
                       f"−{diff['penalty_points']} points.")
            rationale["notes"].insert(0, msg)
        if not diff["money_known"]:
            rationale["notes"].append("Money in the bank is not set, so it is estimated as "
                                      "£100m minus your squad's price. Add it in My Team.")
        ts = now_utc()
        raw = {"squad": s.squad, "xi": s.xi, "captain": s.captain,
               "vice_captain": s.vice_captain, "bench": s.bench}
        sid = execute(
            """INSERT INTO suggestions (lineup_id, profile, variant_of, generated_at, target_gw,
               projected_points, objective, diff, chip_advice, rationale, raw_lineup)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                lid, profile, s.variant_of, ts, target_gw,
                json.dumps(projected), s.objective, json.dumps(diff),
                json.dumps(advice), json.dumps(rationale), json.dumps(raw),
            ),
        )
        out.append(
            {
                "id": sid,
                "profile": profile,
                "variant_of": s.variant_of,
                "generated_at": ts,
                "target_gw": target_gw,
                "projected_points": projected,
                "objective": s.objective,
                "diff": diff,
                "chip_advice": advice,
                "rationale": rationale,
                "lineup": raw,
            }
        )
    return {"suggestions": out, "target_gw": target_gw, "chip": chip}


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
        raw = r.pop("raw_lineup")
        # New rows store the full lineup payload; legacy rows store a bare squad list.
        if isinstance(raw, list):
            r["lineup"] = {"squad": raw, "xi": [], "captain": None, "vice_captain": None, "bench": []}
        else:
            r["lineup"] = raw
    return {"suggestions": rows}


@router.delete("/suggestions/{sid}")
async def delete_suggestion(sid: int) -> dict:
    if not query_one("SELECT id FROM suggestions WHERE id = ?", (sid,)):
        raise HTTPException(404, "suggestion not found")
    execute("DELETE FROM suggestions WHERE id = ?", (sid,))
    return {"deleted": sid}


def _replace_squad(lid: int, squad: list[dict]) -> None:
    """Make the saved team the applied suggestion: roles, bench order and
    captaincy from the suggestion; purchase prices kept for players already
    owned, today's price for new signings."""
    prev = {r["player_id"]: r["bought_cost"] for r in query(
        "SELECT player_id, bought_cost FROM lineup_players WHERE lineup_id = ?", (lid,))}
    rows = [
        (lid, e["player_id"], e["role"], e.get("bench_order"),
         1 if e.get("is_captain") else 0, 1 if e.get("is_vice_captain") else 0,
         prev.get(e["player_id"], e.get("now_cost")))
        for e in squad
    ]
    execute("DELETE FROM lineup_players WHERE lineup_id = ?", (lid,))
    execute_many(
        "INSERT INTO lineup_players (lineup_id, player_id, role, bench_order, is_captain, "
        "is_vice_captain, bought_cost) VALUES (?,?,?,?,?,?,?)",
        rows,
    )


@router.post("/suggestions/{sid}/apply")
async def apply_suggestion(sid: int) -> dict:
    """Mark a suggestion as done in FPL: the saved team becomes the suggested
    squad, the free transfers left / money in the bank are updated, and the
    planned chip (if any) is logged in chip_plays_log (Free-Hit ban) and
    taken off the chips in hand."""
    row = query_one("SELECT * FROM suggestions WHERE id = ?", (sid,))
    if not row:
        raise HTTPException(404, "suggestion not found")
    if row["applied_at"]:
        return {"applied": sid, "already": True,
                "bank_after": json.loads(row["diff"])["bank_after"], "chips_logged": []}
    # A16 (rev 2): applying is a TEAM-LEVEL act — never from a sandbox lineup,
    # nor from one that has since been deleted.
    lineup = (query_one("SELECT kind FROM lineups WHERE id = ?", (row["lineup_id"],))
              if row["lineup_id"] else None)
    if lineup is None:
        raise HTTPException(409, "the lineup this suggestion was generated from no longer "
                                 "exists — regenerate the suggestion")
    if lineup["kind"] == "test":
        raise HTTPException(422, "this suggestion was generated from a test (sandbox) lineup — "
                                 "apply is disabled; generate from your real lineup instead")
    diff = json.loads(row["diff"])
    if diff.get("transfer_cap_exceeded"):
        raise HTTPException(
            422, "suggestion exceeds the 20-transfer cap and no chip covers this GW — "
                 "invalid in FPL, do not apply")
    ts = now_utc()
    raw = json.loads(row["raw_lineup"])
    if isinstance(raw, dict) and len(raw.get("squad") or []) == 15:
        _replace_squad(row["lineup_id"], raw["squad"])
    execute("UPDATE lineups SET transfer_bank = ?, bank_gw = ?, updated_at = ? WHERE id = ?",
            (diff["bank_after"], row["target_gw"], ts, row["lineup_id"]))
    if diff.get("money_known") and diff.get("budget_after") is not None:
        execute("UPDATE lineups SET bank_money = ? WHERE id = ?",
                (max(0, int(diff["budget_after"])), row["lineup_id"]))
    logged: list[str] = []
    chip = diff.get("chip_played")
    if chip:
        execute(
            "INSERT OR IGNORE INTO chip_plays_log (lineup_id, gw, chip, played_at) "
            "VALUES (?,?,?,?)",
            (row["lineup_id"], row["target_gw"], chip, ts),
        )
        logged.append(chip)
        _decrement_chips(row["lineup_id"], logged)
    execute("UPDATE suggestions SET applied_at = ? WHERE id = ?", (ts, sid))
    return {"applied": sid, "bank_after": diff["bank_after"], "chips_logged": logged}
