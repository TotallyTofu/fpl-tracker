"""FPL rule validator — single source of truth (PLAN.MD §5, §8.9).

Used for BOTH user-entered lineups (strict=False: PLAYER_UNAVAILABLE is a warning)
and solver output (strict=True: every error is fatal).

Money: the £100.0m limit only binds a squad built from scratch. A real squad
can be worth more after price rises, and what it can afford depends on the
money in the bank, so for user lineups an over-£100m value is a warning; the
solver enforces money itself (sell values + bank, see solver._TransferCtx).

Bench: FPL keeps the substitute goalkeeper in his own slot; only the three
outfield subs have a priority order. Here that is bench_order 1 = GK, 2-4 =
outfield priority (the same numbering as FPL's picks 12-15).
"""
from __future__ import annotations

from dataclasses import dataclass, field

POS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
SQUAD_COMP = {1: 2, 2: 5, 3: 5, 4: 3}
BUDGET = 1000  # tenths of a million
CLUB_LIMIT = 3
TRANSFER_CAP = 20


@dataclass
class SquadCheck:
    valid: bool
    errors: list[dict] = field(default_factory=list)


def _err(code: str, message: str, severity: str = "error") -> dict:
    return {"code": code, "message": message, "severity": severity}


def validate_lineup(players: list[dict], bank: int, chips: dict | None = None,
                    strict: bool = False) -> SquadCheck:
    """Validate a 15-player lineup.

    players: list of dicts with at least:
      player_id, element_type, team, now_cost, status, can_select,
      role ('starter'|'bench'), bench_order (1-4 or None),
      is_captain, is_vice_captain, web_name (for messages)
    """
    errors: list[dict] = []
    n = len(players)
    ids = [p["player_id"] for p in players]
    if len(set(ids)) != n:
        errors.append(_err("SQUAD_SIZE", f"Squad must contain 15 distinct players (got {n}, duplicates present)"))
    starters = [p for p in players if p.get("role") == "starter"]
    bench = [p for p in players if p.get("role") == "bench"]

    if n != 15:
        errors.append(_err("SQUAD_SIZE", f"Squad must be exactly 15 players (got {n})"))

    counts: dict[int, int] = {}
    for p in players:
        counts[p["element_type"]] = counts.get(p["element_type"], 0) + 1
    comp_msgs = [f"{POS[et]} {counts.get(et, 0)}/{want}" for et, want in SQUAD_COMP.items()
                 if counts.get(et, 0) != want]
    if comp_msgs:
        errors.append(_err("SQUAD_COMPOSITION",
                           "Squad must be 2 GK / 5 DEF / 5 MID / 3 FWD (got "
                           + ", ".join(comp_msgs) + ")"))

    total = sum(p["now_cost"] for p in players)
    if total > BUDGET and not strict:
        errors.append(_err("BUDGET_EXCEEDED",
                           f"Squad is worth £{total / 10:.1f}m, over the £100.0m starting budget. "
                           "That is fine for a real team whose players have risen in price.",
                           severity="warning"))

    club_counts: dict[int, int] = {}
    for p in players:
        club_counts[p["team"]] = club_counts.get(p["team"], 0) + 1
    over = {t: c for t, c in club_counts.items() if c > CLUB_LIMIT}
    if over:
        errors.append(_err("CLUB_LIMIT", f"Max 3 players per club (violated: {over})"))

    if len(starters) != 11:
        errors.append(_err("XI_SIZE", f"Starting XI must be exactly 11 (got {len(starters)})"))
    if len(bench) != 4:
        errors.append(_err("BENCH_SIZE", f"Bench must be exactly 4 (got {len(bench)})"))
    orders = sorted(b.get("bench_order") for b in bench if b.get("bench_order") is not None)
    if len(bench) == 4 and orders != [1, 2, 3, 4]:
        errors.append(_err("BENCH_ORDER", "Bench orders must be 1–4, unique"))
    bench_gks = [b for b in bench if b["element_type"] == 1]
    if len(bench) == 4 and len(bench_gks) == 1 and bench_gks[0].get("bench_order") != 1:
        errors.append(_err("BENCH_GK_SLOT",
                           "The substitute goalkeeper must be in the first bench slot "
                           "(only the three outfield subs have a priority order)"))

    scounts: dict[int, int] = {}
    for p in starters:
        scounts[p["element_type"]] = scounts.get(p["element_type"], 0) + 1
    if scounts.get(1, 0) != 1 or scounts.get(2, 0) < 3 or scounts.get(4, 0) < 1:
        errors.append(_err(
            "XI_POSITION_MIN",
            f"XI must have 1 GK, ≥3 DEF, ≥1 FWD (got GK {scounts.get(1, 0)}, "
            f"DEF {scounts.get(2, 0)}, FWD {scounts.get(4, 0)})",
        ))

    caps = [p for p in players if p.get("is_captain")]
    vcs = [p for p in players if p.get("is_vice_captain")]
    if len(caps) != 1:
        errors.append(_err("CAPTAIN_NOT_IN_XI", f"Exactly one captain required (got {len(caps)})"))
    else:
        if caps[0].get("role") != "starter":
            errors.append(_err("CAPTAIN_NOT_IN_XI", "Captain must be a starter"))
    if len(vcs) != 1:
        errors.append(_err("VICE_CAPTAIN_NOT_IN_XI", f"Exactly one vice-captain required (got {len(vcs)})"))
    else:
        if vcs[0].get("role") != "starter":
            errors.append(_err("VICE_CAPTAIN_NOT_IN_XI", "Vice-captain must be a starter"))
        if caps and caps[0]["player_id"] == vcs[0]["player_id"]:
            errors.append(_err("CAPTAIN_VC_SAME", "Captain and vice-captain must be distinct players"))

    if not (0 <= bank <= 5):
        errors.append(_err("BANK_RANGE", f"Free transfers must be 0–5 (got {bank})"))

    for chip_name, cnt in (chips or {}).items():
        if not (0 <= cnt <= 2):
            errors.append(_err("CHIP_RANGE", f"Chip '{chip_name}' count must be 0–2 (got {cnt})"))

    for p in players:
        if p.get("status") in ("u", "s") or p.get("can_select") == 0:
            sev = "error" if strict else "warning"
            name = p.get("web_name") or f"player {p['player_id']}"
            errors.append(_err("PLAYER_UNAVAILABLE",
                               f"{name} is unavailable (status={p.get('status')})",
                               severity=sev))

    valid = not any(e["severity"] == "error" for e in errors)
    return SquadCheck(valid=valid, errors=errors)