"""Dev smoke test: init DB → live FPL fetch → build a valid squad → run all 3 solver profiles.

Usage:  python backend/scripts/dev_check.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.fetchers import fpl  # noqa: E402
from app.optimizer.rules import SQUAD_COMP, validate_lineup  # noqa: E402
from app.optimizer.solver import PROFILES, SolveParams, solve  # noqa: E402
from app.optimizer.transfers import compute_diff  # noqa: E402


def build_valid_squad() -> list[dict]:
    """Greedy valid squad from live data (2/5/5/3, club cap, budget) — mirrors solver seed."""
    rows = db.query(
        "SELECT * FROM players WHERE can_select = 1 AND removed = 0 "
        "AND (status IS NULL OR status NOT IN ('u','s')) ORDER BY ep_next DESC"
    )
    squad, clubs, budget = [], {}, 1000
    for pos in (1, 2, 3, 4):
        for p in rows:
            if p["element_type"] != pos:
                continue
            if len([s for s in squad if s["element_type"] == pos]) >= SQUAD_COMP[pos]:
                break
            if clubs.get(p["team"], 0) >= 3 or p["now_cost"] > budget:
                continue
            squad.append(p)
            clubs[p["team"]] = clubs.get(p["team"], 0) + 1
            budget -= p["now_cost"]
    if len(squad) != 15:
        raise SystemExit(f"could not build 15-player squad (got {len(squad)})")
    return squad


def squad_to_lineup(squad: list[dict]) -> list[dict]:
    by_ep = sorted(squad, key=lambda p: p["ep_next"] or 0, reverse=True)
    xi = [p for p in by_ep[:11]]
    # enforce formation quickly
    gk = [p for p in xi if p["element_type"] == 1]
    if not gk:
        gk = [p for p in squad if p["element_type"] == 1][0]
        xi[-1] = gk
    def_cnt = sum(1 for p in xi if p["element_type"] == 2)
    fwd_cnt = sum(1 for p in xi if p["element_type"] == 4)
    if fwd_cnt < 1:
        cand = next(p for p in squad if p["element_type"] == 4 and p not in xi)
        xi[-1] = cand
    if def_cnt < 3:
        cand = next(p for p in squad if p["element_type"] == 2 and p not in xi)
        xi[-1] = cand
    bench = [p for p in squad if p not in xi]
    out = []
    for i, p in enumerate(xi):
        out.append({
            "player_id": p["id"], "web_name": p["web_name"], "element_type": p["element_type"],
            "team": p["team"], "now_cost": p["now_cost"], "status": p["status"],
            "can_select": p["can_select"], "role": "starter", "bench_order": None,
            "is_captain": p is xi[0], "is_vice_captain": p is xi[1],
        })
    for i, p in enumerate(bench):
        out.append({
            "player_id": p["id"], "web_name": p["web_name"], "element_type": p["element_type"],
            "team": p["team"], "now_cost": p["now_cost"], "status": p["status"],
            "can_select": p["can_select"], "role": "bench", "bench_order": i + 1,
            "is_captain": False, "is_vice_captain": False,
        })
    return out


async def main() -> None:
    db.init_db()
    print("== fetching FPL data ==")
    await fpl.refresh_all_fpl()
    n_players = db.query_one("SELECT COUNT(*) c FROM players")["c"]
    n_fixtures = db.query_one("SELECT COUNT(*) c FROM fixtures")["c"]
    n_diff = db.query_one("SELECT COUNT(*) c FROM fixtures WHERE difficulty_home IS NOT NULL")["c"]
    print(f"players={n_players} fixtures={n_fixtures} with_difficulty={n_diff}")
    print("game_settings:", {k: fpl.get_game_settings().get(k) for k in
          ("squad_total_spend", "transfers_cap", "max_extra_free_transfers", "sys_vice_captain_enabled")})

    print("\n== building test squad ==")
    squad = build_valid_squad()
    lineup = squad_to_lineup(squad)
    check = validate_lineup(lineup, 1, None, strict=False)
    print(f"validator: valid={check.valid} errors={check.errors}")
    total = sum(p["now_cost"] for p in squad)
    print(f"squad cost: {total}/1000")
    print("XI:", ", ".join(p["web_name"] for p in lineup if p["role"] == "starter"))

    cfg = load_settings().config
    print("\n== solving 3 profiles ==")
    cur_squad = [{"player_id": p["id"], "web_name": p["web_name"], "now_cost": p["now_cost"],
                  "bought_cost": p["now_cost"]} for p in squad]
    for profile in PROFILES:
        params = SolveParams(current_squad=cur_squad, bank=1, chips={}, target_gw=6,
                             profile=profile, cfg=cfg)
        s = solve(params)
        diff = compute_diff(cur_squad, s.squad, 1, {})
        cap = next(p for p in s.squad if p["is_captain"])
        vc = next(p for p in s.squad if p["is_vice_captain"])
        print(f"\n[{profile}] objective={s.objective} projected={s.projected_points}")
        print(f"  C={cap['web_name']}  VC={vc['web_name']}  (VC no multiplier)")
        print(f"  transfers: {len(diff['transfers_in'])} in / {len(diff['transfers_out'])} out, "
              f"penalty={diff['penalty_points']} bank_after={diff['bank_after']}")
        xi_names = [p["web_name"] for p in s.squad if p["role"] == "starter"]
        print(f"  XI: {', '.join(xi_names)}")
        for note in s.notes:
            print(f"  note: {note}")
    print("\nDEV CHECK PASSED")


if __name__ == "__main__":
    asyncio.run(main())