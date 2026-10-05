"""Transfer math + chip rules and advice (PLAN-2 T1.8; M4 T4.2; v1.0 rules pass).

FPL transfer counting: a swap (one player out + one player in) is ONE transfer.
Penalty = 4 pts per transfer over the free transfers available.

FPL hard cap: 20 transfers in one GW (lifted by Wildcard/Free Hit).

v1.0:
- A chip is only "played" when the user picks it for the target GW
  (``chip`` / ``chip_played``). Holding a Wildcard no longer turns every
  suggestion into a rebuild.
- One chip per gameweek: the advice recommends "use" for at most one chip,
  and a chip already logged for the GW blocks the others.
- Money: ``budget_before``/``budget_after`` are the money in the bank before
  and after the transfers (the user's figure when known, else the
  £100m − squad-price estimate, flagged by ``money_known``).
- ``bank_after`` is the free transfers LEFT this gameweek (0 allowed). The
  weekly +1 is added when the deadline passes (lineups API, ``bank_gw``).
"""
from __future__ import annotations

from ..db import query, query_one
from .rules import BUDGET as BUDGET_TOTAL

CHIP_NAMES = ("wildcard", "freehit", "bboost", "triple_captain")
TRANSFER_CHIPS = ("wildcard", "freehit")
CHIP_LABELS = {"wildcard": "Wildcard", "freehit": "Free Hit",
               "bboost": "Bench Boost", "triple_captain": "Triple Captain"}

# Chip-advice thresholds, in projected points.
TC_USE_EP = 8.5            # captain EP for "use" (7.0 with a confirmed-starter signal)
TC_CONSIDER_EP = 7.0
BB_USE_BENCH = 14.0        # bench projection for "use" (double-gameweek territory)
BB_CONSIDER_BENCH = 10.0
WC_CONSIDER_GAIN = 10.0    # extra points a full rebuild projects THIS gameweek
FH_CONSIDER_GAIN = 15.0


def sell_value(bought_cost: int | None, now_cost: int) -> int:
    """Half-increase sell rule: sells at purchase price + 50% of any increase,
    rounded down. 75→78 sells at 76; 75→77 sells at 76; fall 75→70 sells at 70.
    (bought_cost missing → assume bought at current price.)
    """
    bought = bought_cost if bought_cost is not None else now_cost
    if now_cost > bought:
        return bought + (now_cost - bought) // 2
    return now_cost


def bank_money_estimate(current_squad: list[dict]) -> int:
    """The pre-v1.0 assumption when the user has not entered their bank:
    £100.0m minus what the current squad costs today (never negative)."""
    if not current_squad:
        return BUDGET_TOTAL
    return max(0, BUDGET_TOTAL - sum(p["now_cost"] for p in current_squad))


def compute_diff(current_squad: list[dict], new_squad: list[dict], bank: int,
                 chips: dict | None = None, chip_covers: bool = False,
                 ep_by_player: dict[int, float] | None = None,
                 bank_money: int | None = None, chip_played: str | None = None) -> dict:
    """Diff a suggested squad against the user's current squad (PLAN.MD §8.6).

    current_squad entries: {player_id, web_name, now_cost, bought_cost}
    new_squad entries:      {player_id, web_name, now_cost}

    ``chip_covers`` (a Wildcard/Free Hit is being played): all transfers are
    free and the free-transfer count is kept. ``bank_money`` is the money in
    the bank in £0.1m (None = estimate). ``ep_by_player`` adds each player's
    projected points to the in/out rows.
    """
    cur_by_id = {p["player_id"]: p for p in current_squad}
    new_by_id = {p["player_id"]: p for p in new_squad}
    out_ids = [pid for pid in cur_by_id if pid not in new_by_id]
    in_ids = [pid for pid in new_by_id if pid not in cur_by_id]

    def _ep(pid: int):
        if ep_by_player is not None and pid in ep_by_player:
            return round(float(ep_by_player[pid]), 2)
        return None

    transfers_out = [
        {
            "player_id": pid,
            "web_name": cur_by_id[pid].get("web_name"),
            "sell_value": sell_value(cur_by_id[pid].get("bought_cost"), cur_by_id[pid]["now_cost"]),
            "ep": _ep(pid),
        }
        for pid in out_ids
    ]
    transfers_in = [
        {"player_id": pid, "web_name": new_by_id[pid].get("web_name"),
         "cost": new_by_id[pid]["now_cost"], "ep": _ep(pid)}
        for pid in in_ids
    ]
    cost_delta = sum(t["cost"] for t in transfers_in) - sum(t["sell_value"] for t in transfers_out)
    total_cost_after = sum(p["now_cost"] for p in new_squad)
    transfers = max(len(in_ids), len(out_ids))
    if chip_covers:
        free_used = 0            # wildcard/free hit does not consume free transfers
        bank_after = bank        # and keeps the ones saved
        penalty = 0
    else:
        free_used = min(transfers, bank)
        bank_after = max(0, bank - transfers)   # left this gameweek
        penalty = 4 * max(0, transfers - bank)
    money_known = bank_money is not None
    money_before = bank_money if money_known else bank_money_estimate(current_squad)
    money_after = money_before - cost_delta
    return {
        "transfers_in": transfers_in,
        "transfers_out": transfers_out,
        "cost_delta": cost_delta,
        "total_cost_after": total_cost_after,
        "free_transfers_used": free_used,
        "bank_after": bank_after,
        "penalty_points": penalty,
        "chip_covers": bool(chip_covers),
        "chip_played": chip_played,
        "bank_before": bank,
        "budget_before": money_before,
        "budget_after": money_after,
        "money_known": money_known,
    }


def freehit_played_in(gw: int | None) -> bool:
    """True when a Free Hit was played in gameweek ``gw`` (chip_plays_log).

    Team-level fact, deliberately NOT scoped to a lineup id.
    """
    if gw is None:
        return False
    return query_one(
        "SELECT 1 AS x FROM chip_plays_log WHERE gw = ? AND chip = 'freehit'", (gw,)
    ) is not None


def chips_logged_in(gw: int | None) -> list[str]:
    """Chips recorded as played in gameweek ``gw``."""
    if gw is None:
        return []
    return [r["chip"] for r in query("SELECT chip FROM chip_plays_log WHERE gw = ? ORDER BY id", (gw,))]


def chip_window_covers(chip: str, gw: int | None) -> bool:
    """True when one of the chip's sets (from the API's chips[]) is playable in ``gw``."""
    if gw is None:
        return False
    return query_one(
        "SELECT 1 AS x FROM chips WHERE name = ? AND start_event <= ? AND stop_event >= ?",
        (chip, gw, gw),
    ) is not None


def chip_play_problem(chip: str, chips: dict | None, gw: int | None) -> str | None:
    """Why ``chip`` cannot be played in ``gw`` (None = it can)."""
    chips = chips or {}
    label = CHIP_LABELS.get(chip)
    if label is None:
        return f"unknown chip {chip!r}"
    if int(chips.get(chip, 0) or 0) <= 0:
        return f"no {label} sets remaining"
    if not chip_window_covers(chip, gw):
        return f"no {label} window covers GW{gw}"
    if chip == "freehit" and gw is not None and freehit_played_in(gw - 1):
        return f"a Free Hit cannot follow a Free Hit GW (one was played in GW{gw - 1})"
    other = [c for c in chips_logged_in(gw) if c != chip]
    if other:
        return (f"{CHIP_LABELS.get(other[0], other[0])} is already logged for GW{gw}: "
                "only one chip per gameweek")
    return None


def chip_covers_transfers(chips: dict | None, target_gw: int | None,
                          chip: str | None = None) -> bool:
    """True when the user plays a Wildcard or Free Hit (``chip``) in the target
    GW and is allowed to: transfers are then unlimited (no penalty, no cap).

    Holding a chip is not playing it — without ``chip`` this is False.
    """
    if chip not in TRANSFER_CHIPS:
        return False
    return chip_play_problem(chip, chips, target_gw) is None


def _enforce_one_chip(advice: list[dict], values: dict[str, float]) -> list[dict]:
    """FPL allows one chip per gameweek: keep the single most valuable "use"
    and turn any other "use" into "consider"."""
    uses = [a for a in advice if a["recommendation"] == "use"]
    if len(uses) <= 1:
        return advice
    keep = max(uses, key=lambda a: values.get(a["chip"], 0.0))
    for a in uses:
        if a is not keep:
            a["recommendation"] = "consider"
            a["reason"] += f" (only one chip per gameweek; {CHIP_LABELS[keep['chip']]} is worth more)"
    return advice


def _committed(advice: list[dict], chip: str, gw: int | None, why: str) -> list[dict]:
    """A chip is planned/logged for the GW: it is the one chip; others skip."""
    label = CHIP_LABELS.get(chip, chip)
    out = []
    for a in advice:
        if a["chip"] == chip:
            out.append({"chip": chip, "recommendation": "use", "reason": why})
        else:
            out.append({"chip": a["chip"], "recommendation": "skip",
                        "reason": f"one chip per gameweek: {label} is planned for GW{gw}"})
    return out


def chip_advice_v1(diff: dict, chips: dict | None, current_gw: int | None,
                   next_gw: int | None, cfg) -> list[dict]:
    """Chip advice v1 (fallback when no solved lineup is available): window
    guard + wildcard/free-hit by transfer count. One chip per GW enforced."""
    advice: list[dict] = []
    chips = chips or {}
    gw = next_gw or current_gw
    transfers = max(len(diff["transfers_in"]), len(diff["transfers_out"]))
    bank_before = diff["bank_before"]   # A22: required — a legacy diff fails loudly

    for chip in TRANSFER_CHIPS:
        label = CHIP_LABELS[chip]
        if not chip_window_covers(chip, gw):
            advice.append({"chip": chip, "recommendation": "skip",
                           "reason": f"no active {label.lower()} window covers GW{gw}"})
        elif chips.get(chip, 0) <= 0:
            advice.append({"chip": chip, "recommendation": "skip",
                           "reason": f"no {label.lower()} sets remaining"})
        elif transfers > bank_before:
            advice.append({"chip": chip, "recommendation": "use" if chip == "wildcard" else "consider",
                           "reason": f"{transfers} transfers vs {bank_before} free — a {label} makes them all free"})
        else:
            advice.append({"chip": chip, "recommendation": "skip",
                           "reason": f"{transfers} transfers fit in your {bank_before} free"})
    for chip in ("triple_captain", "bboost"):
        label = CHIP_LABELS[chip]
        if not chip_window_covers(chip, gw):
            advice.append({"chip": chip, "recommendation": "skip",
                           "reason": f"no active {label.lower()} window covers GW{gw}"})
        elif chips.get(chip, 0) <= 0:
            advice.append({"chip": chip, "recommendation": "skip",
                           "reason": f"no {label.lower()} sets remaining"})
        else:
            advice.append({"chip": chip, "recommendation": "consider",
                           "reason": "no projection available — check the captain and bench"})
    logged = chips_logged_in(gw)
    played = diff.get("chip_played") or (logged[0] if logged else None)
    if played:
        return _committed(advice, played, gw, f"{CHIP_LABELS.get(played, played)} is planned for GW{gw}")
    return _enforce_one_chip(advice, {"wildcard": 4.0 * max(0, transfers - bank_before)})


def chip_advice_v2(diff: dict, chips: dict | None, solved, current_gw: int | None,
                   next_gw: int | None, cfg, signals: dict | None = None,
                   chip_gains: dict | None = None) -> list[dict]:
    """Full chip logic (M4 T4.2; v1.0 rules pass): window gating, sets
    remaining, Free-Hit consecutive ban, triple captain by captain EP +
    confirmed starter, bench boost by bench projection, wildcard / free hit by
    the projected gain of a rebuild (``chip_gains``, from an extra solve), and
    at most one "use" per gameweek.

    `solved` is a SolvedLineup (carries universe EPs, xi, captain, bench).
    `signals` maps player_id → list of active signal dicts.
    """
    chips = chips or {}
    signals = signals or {}
    chip_gains = chip_gains or {}
    gw = next_gw or current_gw
    transfers = max(len(diff["transfers_in"]), len(diff["transfers_out"]))
    bank_before = diff["bank_before"]   # A22: required — a legacy diff fails loudly

    universe = getattr(solved, "universe", None) or []
    ep = {p["id"]: p.get("ep", 0.0) for p in universe}
    xi_ids = list(getattr(solved, "xi", []) or [])
    bench_ids = list(getattr(solved, "bench", []) or [])
    cap_id = getattr(solved, "captain", None)
    bench_sum = sum(ep.get(i, 0.0) for i in bench_ids)
    cap_ep = ep.get(cap_id, 0.0) if cap_id is not None else 0.0
    cap_confirmed = any(
        s.get("sentiment") == "positive" and s.get("category") == "selection"
        and s.get("confidence", 0) >= 0.7 for s in signals.get(cap_id, []) or []
    ) if cap_id is not None else False

    def gate(chip: str) -> dict | None:
        label = CHIP_LABELS[chip]
        if not chip_window_covers(chip, gw):
            return {"chip": chip, "recommendation": "skip",
                    "reason": f"no active {label.lower()} window covers GW{gw}"}
        if int(chips.get(chip, 0) or 0) <= 0:
            return {"chip": chip, "recommendation": "skip",
                    "reason": f"no {label.lower()} sets remaining"}
        return None

    advice: list[dict] = []
    values: dict[str, float] = {}

    # WILDCARD
    a = gate("wildcard")
    if a is None:
        gain = chip_gains.get("wildcard")
        if transfers > bank_before:
            values["wildcard"] = 4.0 * (transfers - bank_before)
            a = {"chip": "wildcard", "recommendation": "use",
                 "reason": f"{transfers} transfers vs {bank_before} free — a Wildcard makes them all "
                           f"free (saves {4 * (transfers - bank_before)} points) and keeps your free transfers"}
        elif gain is not None and gain >= WC_CONSIDER_GAIN:
            values["wildcard"] = gain
            a = {"chip": "wildcard", "recommendation": "consider",
                 "reason": f"a full rebuild projects +{gain:.1f} points this gameweek; "
                           "a Wildcard also sets up the weeks after"}
        elif gain is not None:
            a = {"chip": "wildcard", "recommendation": "skip",
                 "reason": f"a full rebuild projects only +{gain:.1f} points this gameweek — save it"}
        else:
            a = {"chip": "wildcard", "recommendation": "skip",
                 "reason": f"{transfers} transfers fit in your {bank_before} free"}
    advice.append(a)

    # FREE HIT (+ consecutive-GW ban)
    a = gate("freehit")
    if a is None:
        gain = chip_gains.get("freehit", chip_gains.get("wildcard"))
        if gw is not None and freehit_played_in(gw - 1):
            a = {"chip": "freehit", "recommendation": "skip",
                 "reason": f"cannot follow a Free Hit GW (one was played in GW {gw - 1})"}
        elif transfers > bank_before:
            a = {"chip": "freehit", "recommendation": "consider",
                 "reason": f"{transfers} transfers vs {bank_before} free — a Free Hit avoids the "
                           f"-{4 * (transfers - bank_before)} pt penalty (your squad returns next GW)"}
        elif gain is not None and gain >= FH_CONSIDER_GAIN:
            values["freehit"] = gain
            a = {"chip": "freehit", "recommendation": "consider",
                 "reason": f"a one-week rebuild projects +{gain:.1f} points; your squad returns next GW"}
        else:
            reason = (f"a one-week rebuild projects only +{gain:.1f} points — save it"
                      if gain is not None else f"{transfers} transfers fit in your {bank_before} free")
            a = {"chip": "freehit", "recommendation": "skip", "reason": reason}
    advice.append(a)

    # TRIPLE CAPTAIN
    a = gate("triple_captain")
    if a is None:
        values["triple_captain"] = cap_ep
        if cap_ep >= TC_USE_EP or (cap_confirmed and cap_ep >= TC_CONSIDER_EP):
            reason = f"captain projects {cap_ep:.1f}: Triple Captain adds another {cap_ep:.1f}"
            if cap_confirmed:
                reason += " (confirmed-starter signal)"
            a = {"chip": "triple_captain", "recommendation": "use", "reason": reason}
        elif cap_ep >= TC_CONSIDER_EP:
            a = {"chip": "triple_captain", "recommendation": "consider",
                 "reason": f"captain projects {cap_ep:.1f}: worth a look, below the {TC_USE_EP} bar"}
        else:
            a = {"chip": "triple_captain", "recommendation": "skip",
                 "reason": f"captain projects {cap_ep:.1f}, too low for Triple Captain"}
    advice.append(a)

    # BENCH BOOST
    a = gate("bboost")
    if a is None:
        values["bboost"] = bench_sum
        if bench_sum >= BB_USE_BENCH:
            a = {"chip": "bboost", "recommendation": "use",
                 "reason": f"your bench projects {bench_sum:.1f} points"}
        elif bench_sum >= BB_CONSIDER_BENCH:
            a = {"chip": "bboost", "recommendation": "consider",
                 "reason": f"your bench projects {bench_sum:.1f} points (use it at {BB_USE_BENCH:.0f}+)"}
        else:
            a = {"chip": "bboost", "recommendation": "skip",
                 "reason": f"your bench projects only {bench_sum:.1f} points"}
    advice.append(a)

    logged = chips_logged_in(gw)
    played = diff.get("chip_played")
    if played:
        why = {"wildcard": "unlimited free transfers this gameweek; your free transfers are kept",
               "freehit": "unlimited free transfers for one week; your squad returns next gameweek",
               "bboost": f"your bench scores too: +{bench_sum:.1f} projected",
               "triple_captain": f"captain counts three times: +{cap_ep:.1f} projected"}.get(played, "")
        return _committed(advice, played, gw, f"You chose to play {CHIP_LABELS[played]}: {why}")
    if logged:
        return _committed(advice, logged[0], gw,
                          f"{CHIP_LABELS.get(logged[0], logged[0])} is logged as played for GW{gw}")
    return _enforce_one_chip(advice, values)
