"""Transfer math + chip advice (PLAN-2 T1.8; v2 in M4 T4.2).

FPL transfer counting: every player in AND every player out is one transfer
(a swap = 2 transfers). Penalty = 4 pts per transfer over the free bank.
"""
from __future__ import annotations

from .. import season as season_svc
from ..db import query

CHIP_NAMES = ("wildcard", "freehit", "bboost", "triple_captain")


def sell_value(bought_cost: int | None, now_cost: int) -> int:
    """Half-increase sell rule: sells at purchase price + 50% of any increase.

    75→78 sells at 76; 75→77 sells at 76; fall 75→70 sells at 70.
    (bought_cost missing → assume bought at current price.)
    """
    bought = bought_cost if bought_cost is not None else now_cost
    if now_cost > bought:
        return bought + (now_cost - bought) // 2
    return now_cost


def compute_diff(current_squad: list[dict], new_squad: list[dict], bank: int,
                 chips: dict | None = None) -> dict:
    """Diff a suggested squad against the user's current squad (PLAN.MD §8.6).

    current_squad entries: {player_id, web_name, now_cost, bought_cost}
    new_squad entries:      {player_id, web_name, now_cost}
    """
    cur_by_id = {p["player_id"]: p for p in current_squad}
    new_by_id = {p["player_id"]: p for p in new_squad}
    out_ids = [pid for pid in cur_by_id if pid not in new_by_id]
    in_ids = [pid for pid in new_by_id if pid not in cur_by_id]

    transfers_out = [
        {
            "player_id": pid,
            "web_name": cur_by_id[pid].get("web_name"),
            "sell_value": sell_value(cur_by_id[pid].get("bought_cost"), cur_by_id[pid]["now_cost"]),
        }
        for pid in out_ids
    ]
    transfers_in = [
        {"player_id": pid, "web_name": new_by_id[pid].get("web_name"),
         "cost": new_by_id[pid]["now_cost"]}
        for pid in in_ids
    ]
    cost_delta = sum(t["cost"] for t in transfers_in) - sum(t["sell_value"] for t in transfers_out)
    total_cost_after = sum(p["now_cost"] for p in new_squad)
    transfers = len(in_ids) + len(out_ids)
    free_used = min(transfers, bank)
    bank_after = max(0, bank - transfers)
    penalty = 4 * max(0, transfers - bank)
    return {
        "transfers_in": transfers_in,
        "transfers_out": transfers_out,
        "cost_delta": cost_delta,
        "total_cost_after": total_cost_after,
        "free_transfers_used": free_used,
        "bank_after": bank_after,
        "penalty_points": penalty,
    }


def chip_advice_v1(diff: dict, chips: dict | None, current_gw: int | None,
                   next_gw: int | None, cfg) -> list[dict]:
    """Chip advice v1 (M1): window guard + wildcard/free-hit by transfer count,
    triple-captain by captain EP. v2 (M4 T4.2) adds Free-Hit consecutive ban,
    BB strength, 3XC+WC interplay.
    """
    advice: list[dict] = []
    chips = chips or {}
    windows = {w["chip"]: w for w in season_svc.active_chip_windows()}
    transfers = len(diff["transfers_in"]) + len(diff["transfers_out"])
    bank_before = _bank_before(diff)

    def window_open(chip: str) -> bool:
        w = windows.get(chip)
        return bool(w and w.get("playable_next_gw"))

    # WILDCARD
    w_left = chips.get("wildcard", 0)
    if not window_open("wildcard"):
        advice.append({"chip": "wildcard", "recommendation": "skip",
                       "reason": "no active wildcard window covers the next GW"})
    elif w_left <= 0:
        advice.append({"chip": "wildcard", "recommendation": "skip",
                       "reason": "no wildcard sets remaining"})
    elif transfers > bank_before:
        advice.append({"chip": "wildcard", "recommendation": "use",
                       "reason": f"{transfers} transfers vs bank of {bank_before} — wildcard makes them all free"})
    else:
        advice.append({"chip": "wildcard", "recommendation": "skip",
                       "reason": f"{transfers} transfers fit in the bank of {bank_before}"})

    # FREE HIT
    f_left = chips.get("freehit", 0)
    if not window_open("freehit"):
        advice.append({"chip": "freehit", "recommendation": "skip",
                       "reason": "no active free-hit window covers the next GW"})
    elif f_left <= 0:
        advice.append({"chip": "freehit", "recommendation": "skip",
                       "reason": "no free-hit sets remaining"})
    elif transfers > bank_before:
        advice.append({"chip": "freehit", "recommendation": "consider",
                       "reason": f"{transfers} transfers vs bank of {bank_before} — free hit avoids the "
                                 f"-{4 * max(0, transfers - bank_before)} pt penalty (cannot follow a Free Hit GW)"})
    else:
        advice.append({"chip": "freehit", "recommendation": "skip",
                       "reason": f"{transfers} transfers fit in the bank of {bank_before}"})

    # TRIPLE CAPTAIN
    t_left = chips.get("triple_captain", 0)
    if not window_open("triple_captain"):
        advice.append({"chip": "triple_captain", "recommendation": "skip",
                       "reason": "no active triple-captain window covers the next GW"})
    elif t_left <= 0:
        advice.append({"chip": "triple_captain", "recommendation": "skip",
                       "reason": "no triple-captain sets remaining"})
    else:
        advice.append({"chip": "triple_captain", "recommendation": "consider",
                       "reason": "evaluate captain choice — 3XC doubles a strong captain (see card)"})

    # BENCH BOOST
    b_left = chips.get("bboost", 0)
    if not window_open("bboost"):
        advice.append({"chip": "bboost", "recommendation": "skip",
                       "reason": "no active bench-boost window covers the next GW"})
    elif b_left <= 0:
        advice.append({"chip": "bboost", "recommendation": "skip",
                       "reason": "no bench-boost sets remaining"})
    else:
        advice.append({"chip": "bboost", "recommendation": "consider",
                       "reason": "bench boost pays 1 pt per appearance — use on a GW with deep bench minutes"})

    return advice


def _bank_before(diff: dict) -> int:
    """Recover the pre-diff bank: bank_after = max(0, bank − transfers),
    free_used = min(transfers, bank) → bank = bank_after + free_used."""
    return diff["bank_after"] + diff["free_transfers_used"]