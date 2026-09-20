"""Multi-start hill-climbing solver (PLAN-2 T1.7).

Deterministic with a fixed seed; 3 risk profiles; hard constraints (2/5/5/3,
≤£100.0m, ≤3/club, XI 1/3/1 minimums, captain/VC in XI) are enforced by move
construction + the rules validator on every accepted move.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from ..db import query
from .rules import SQUAD_COMP, BUDGET, CLUB_LIMIT, validate_lineup
from .scoring import availability_multiplier, ep_final, score_lineup

PROFILES = ("max_ep", "differential", "safe")


@dataclass
class SolveParams:
    current_squad: list[dict]
    bank: int
    chips: dict
    target_gw: int
    profile: str
    cfg: object
    rng_seed: int = 42
    signals_by_player: dict[int, list[dict]] = field(default_factory=dict)


@dataclass
class SolvedLineup:
    squad: list[dict]
    xi: list[int]
    captain: int
    vice_captain: int
    bench: list[int]  # ordered 1..4
    objective: float
    projected_points: dict
    variant_of: str | None = None
    notes: list[str] = field(default_factory=list)
    # T4.1: kept in-memory (never serialized) so the convergence guard can
    # differentiate a profile and re-score it after a captain/swap change.
    universe: list[dict] = field(default_factory=list)
    scoring_ctx: tuple | None = None  # (signals_by_player, cfg, diff_map)


def build_universe(target_gw: int, cfg) -> list[dict]:
    """Eligible players: can_select=1, not u/s, not removed (hard gates).

    M2 (T2.10): with optimizer.availability.active, chance_of_playing_next_round
    = 0 is also excluded (the A(p) map would price them at 0 anyway).
    """
    sql = (
        """SELECT * FROM players
           WHERE can_select = 1 AND removed = 0
             AND (status IS NULL OR status NOT IN ('u', 's'))"""
    )
    if cfg.optimizer.availability.active:
        sql += " AND (chance_of_playing_next_round IS NULL OR chance_of_playing_next_round <> 0)"
    rows = query(sql)
    return [dict(p) for p in rows]


def _difficulty_map(target_gw: int) -> dict[int, int | None]:
    """(team, gw) → difficulty for the target GW, per team side."""
    out: dict[int, int | None] = {}
    rows = query(
        "SELECT home_team, away_team, difficulty_home, difficulty_away FROM fixtures WHERE event = ?",
        (target_gw,),
    )
    for r in rows:
        out[r["home_team"]] = r["difficulty_home"]
        out[r["away_team"]] = r["difficulty_away"]
    return out


def _precompute(p: dict, cfg, signals_by_player: dict[int, list[dict]],
                diff_map: dict[int, int | None]) -> dict:
    """Add precomputed ep / rel / ownership helpers to a player dict (copy)."""
    q = dict(p)
    q["id"] = p["id"]
    q["ep"] = ep_final(p, signals_by_player.get(p["id"], []), cfg, diff_map.get(p["team"]))
    # reliability for the 'safe' profile (M1: A≈1, no signals → 1.0)
    a = availability_multiplier(p, cfg)
    neg = any(s["sentiment"] == "negative" for s in signals_by_player.get(p["id"], []))
    conf = any(s["sentiment"] == "positive" and s["category"] == "selection"
               and s["confidence"] >= 0.7 for s in signals_by_player.get(p["id"], []))
    q["rel"] = a * (1 - 0.5 * neg) * (1 + 0.1 * conf)
    q["own"] = float(p.get("selected_by_percent") or 0.0)
    return q


def _objective_key(profile: str) -> str:
    return profile


def _objective(value_fn, xi: list[dict], captain: dict) -> float:
    return sum(value_fn(p) for p in xi) + value_fn(captain)


def _value_fn(profile: str, cfg):
    if profile == "max_ep":
        return lambda p: p["ep"]
    if profile == "differential":
        lam = cfg.optimizer.differential_lambda

        def f(p: dict) -> float:
            return p["ep"] + lam * (1 - p["own"] / 100.0)

        return f
    # safe
    return lambda p: p["ep"] * p["rel"]


def _greedy_seed(universe: list[dict], cfg, rng: random.Random) -> list[dict]:
    """Greedy 2/5/5/3 squad by ep/price with club cap + budget; noisy for restart diversity."""
    squad: list[dict] = []
    picked: set[int] = set()
    club_count: dict[int, int] = {}
    budget = BUDGET
    for pos in (1, 2, 3, 4):
        cands = sorted(
            (p for p in universe if p["element_type"] == pos),
            key=lambda p: p["ep"] / max(p["now_cost"], 1),
            reverse=True,
        )
        for idx, p in enumerate(cands):
            if len([s for s in squad if s["element_type"] == pos]) >= SQUAD_COMP[pos]:
                break
            if p["id"] in picked:
                continue
            if club_count.get(p["team"], 0) >= CLUB_LIMIT:
                continue
            if p["now_cost"] > budget:
                continue
            # restart diversity: occasionally take the 2nd best
            pick = p
            if rng.random() < 0.25 and idx + 1 < len(cands):
                alt = cands[idx + 1]
                if (alt["id"] not in picked and club_count.get(alt["team"], 0) < CLUB_LIMIT
                        and alt["now_cost"] <= budget):
                    pick = alt
            squad.append(pick)
            picked.add(pick["id"])
            club_count[pick["team"]] = club_count.get(pick["team"], 0) + 1
            budget -= pick["now_cost"]
    # backfill any shortfall with cheapest eligible (club/budget permitting)
    for pos in (1, 2, 3, 4):
        have = len([s for s in squad if s["element_type"] == pos])
        need = SQUAD_COMP[pos] - have
        if need <= 0:
            continue
        cands = sorted(
            (p for p in universe if p["element_type"] == pos and p["id"] not in picked),
            key=lambda p: p["now_cost"],
        )
        for p in cands:
            if need <= 0:
                break
            if club_count.get(p["team"], 0) >= CLUB_LIMIT:
                continue
            if p["now_cost"] > budget:
                continue
            squad.append(p)
            picked.add(p["id"])
            club_count[p["team"]] = club_count.get(p["team"], 0) + 1
            budget -= p["now_cost"]
            need -= 1
    return squad


def _pick_xi(squad: list[dict]) -> list[dict]:
    """Top-11 by ep with 1 GK, ≥3 DEF, ≥1 FWD (formation enforced by swaps)."""
    gks = [p for p in squad if p["element_type"] == 1]
    if not gks:
        return []
    gk = max(gks, key=lambda p: p["ep"])
    rest = [p for p in squad if p["element_type"] != 1]
    rest_sorted = sorted(rest, key=lambda p: p["ep"], reverse=True)
    xi = [gk] + rest_sorted[:10]

    def ensure(pos: int, count: int) -> None:
        have = sum(1 for p in xi if p["element_type"] == pos)
        while have < count:
            unselected = [p for p in rest if p["element_type"] == pos and p not in xi]
            if not unselected:
                return
            # never evict the GK or a player of the target position
            victims = [p for p in xi if p["element_type"] not in (pos, 1)]
            if not victims:
                return
            victim = min(victims, key=lambda p: p["ep"])
            best = max(unselected, key=lambda p: p["ep"])
            xi[xi.index(victim)] = best
            have += 1

    ensure(4, 1)  # FWD first (scarcest in squad)
    ensure(2, 3)  # then DEF
    return xi


class _State:
    """Mutable lineup state for hill climbing."""

    def __init__(self, squad: list[dict], cfg):
        self.squad = squad
        self.club_count: dict[int, int] = {}
        for p in squad:
            self.club_count[p["team"]] = self.club_count.get(p["team"], 0) + 1
        self.total_cost = sum(p["now_cost"] for p in squad)
        self.xi = _pick_xi(squad)
        if len(self.xi) != 11:
            self.xi = []
        if self.xi:
            by_ep = sorted(self.xi, key=lambda p: p["ep"], reverse=True)
            self.captain = by_ep[0]
            self.vice = by_ep[1] if len(by_ep) > 1 else None
        else:
            self.captain = None
            self.vice = None
        self.bench = [p for p in squad if p not in self.xi]
        self.cfg = cfg

    def valid(self) -> bool:
        if len(self.squad) != 15 or len(self.xi) != 11 or len(self.bench) != 4:
            return False
        if self.captain is None or self.vice is None or self.captain is self.vice:
            return False
        # XI composition: exactly 1 GK, ≥3 DEF, ≥1 FWD
        if sum(1 for p in self.xi if p["element_type"] == 1) != 1:
            return False
        if sum(1 for p in self.xi if p["element_type"] == 2) < 3:
            return False
        if sum(1 for p in self.xi if p["element_type"] == 4) < 1:
            return False
        if self.total_cost > BUDGET:
            return False
        if any(c > CLUB_LIMIT for c in self.club_count.values()):
            return False
        return True

    def as_lineup(self) -> list[dict]:
        xi_ids = {p["id"] for p in self.xi}
        # bench 1 = strongest sub (FPL convention): rank bench by EP desc
        bench_sorted = sorted(self.bench, key=lambda p: p["ep"], reverse=True)
        out = []
        for p in self.squad:
            if p["id"] in xi_ids:
                role, order = "starter", None
            else:
                role = "bench"
                order = bench_sorted.index(p) + 1
            out.append(
                {
                    "player_id": p["id"],
                    "web_name": p.get("web_name"),
                    "element_type": p["element_type"],
                    "team": p["team"],
                    "now_cost": p["now_cost"],
                    "status": p.get("status"),
                    "can_select": p.get("can_select"),
                    "selected_by_percent": p.get("selected_by_percent"),
                    "role": role,
                    "bench_order": order,
                    "is_captain": p is self.captain,
                    "is_vice_captain": p is self.vice,
                }
            )
        return out


def _hill_climb(state: _State, universe: list[dict], value_fn, cfg,
                rng: random.Random, deadline: float) -> float:
    best = _objective(value_fn, state.xi, state.captain) if state.valid() else float("-inf")
    no_improve = 0
    moves = 0
    by_pos: dict[int, list[dict]] = {}
    for p in universe:
        by_pos.setdefault(p["element_type"], []).append(p)

    while no_improve < 400:
        moves += 1
        if moves % 100 == 0 and time.monotonic() > deadline:
            break
        kind = rng.choice(["squad_swap", "squad_swap", "xi_bench", "captain", "vc", "bench_reorder"])
        if not state.valid():
            break
        before = (list(state.squad), list(state.xi), state.captain, state.vice,
                  list(state.bench), state.club_count.copy(), state.total_cost)

        def revert() -> None:
            state.squad, state.xi, state.captain, state.vice = before[0], before[1], before[2], before[3]
            state.bench, state.club_count, state.total_cost = before[4], before[5], before[6]

        ok = True

        if kind == "squad_swap":
            i = rng.randrange(len(state.squad))
            out_p = state.squad[i]
            cands = by_pos.get(out_p["element_type"], [])
            if not cands:
                ok = False
            else:
                in_p = rng.choice(cands)
                if in_p["id"] in {s["id"] for s in state.squad}:
                    ok = False
                elif state.club_count.get(in_p["team"], 0) >= CLUB_LIMIT and in_p["team"] != out_p["team"]:
                    ok = False
                elif state.total_cost + in_p["now_cost"] - out_p["now_cost"] > BUDGET:
                    ok = False
                else:
                    state.squad[i] = in_p
                    state.club_count[out_p["team"]] -= 1
                    state.club_count[in_p["team"]] = state.club_count.get(in_p["team"], 0) + 1
                    state.total_cost += in_p["now_cost"] - out_p["now_cost"]
                    if out_p in state.xi:
                        state.xi[state.xi.index(out_p)] = in_p
                    elif out_p in state.bench:
                        state.bench[state.bench.index(out_p)] = in_p
                    elif in_p["ep"] > min(p["ep"] for p in state.bench):
                        # promote: swap with the weakest bench player (keeps XI valid)
                        weakest = min(state.bench, key=lambda p: p["ep"])
                        state.bench[state.bench.index(weakest)] = out_p
                        state.xi.append(in_p)
                        # re-pick captain/vice from XI
                        by_ep = sorted(state.xi, key=lambda p: p["ep"], reverse=True)
                        state.captain, state.vice = by_ep[0], by_ep[1]
        elif kind == "xi_bench":
            s = rng.choice(state.xi)
            cands = [b for b in state.bench if b["element_type"] == s["element_type"]]
            if not cands:
                ok = False
            else:
                b = rng.choice(cands)
                state.xi[state.xi.index(s)] = b
                state.bench[state.bench.index(b)] = s
                by_ep = sorted(state.xi, key=lambda p: p["ep"], reverse=True)
                state.captain, state.vice = by_ep[0], by_ep[1]
        elif kind == "captain":
            cands = [p for p in state.xi if p is not state.captain]
            if not cands:
                ok = False
            else:
                state.captain = rng.choice(cands)
        elif kind == "vc":
            cands = [p for p in state.xi if p is not state.captain and p is not state.vice]
            if not cands:
                ok = False
            else:
                state.vice = rng.choice(cands)
        elif kind == "bench_reorder":
            if len(state.bench) >= 2:
                i, j = rng.sample(range(len(state.bench)), 2)
                state.bench[i], state.bench[j] = state.bench[j], state.bench[i]

        if ok and state.valid():
            obj = _objective(value_fn, state.xi, state.captain)
            if obj > best + 1e-9:
                best = obj
                no_improve = 0
            else:
                no_improve += 1
                revert()
        elif ok:
            no_improve += 1
            revert()
        else:
            no_improve += 1
    return best


def _apply_ep_floor(universe: list[dict], cfg) -> list[dict]:
    """T4.1: differential eligibility floor — candidates must have
    ep_final ≥ optimizer.differential_ep_floor × max ep_final in the universe
    (prevents the profile degenerating into low-EP lottery picks)."""
    if not universe:
        return universe
    max_ep = max(p["ep"] for p in universe)
    floor = cfg.optimizer.differential_ep_floor * max_ep
    return [p for p in universe if p["ep"] >= floor]


def solve(params: SolveParams) -> SolvedLineup:
    cfg = params.cfg
    rng = random.Random(params.rng_seed)
    deadline = time.monotonic() + cfg.optimizer.solver.timebox_sec
    diff_map = _difficulty_map(params.target_gw)
    signals = params.signals_by_player

    universe = [_precompute(p, cfg, signals, diff_map) for p in build_universe(params.target_gw, cfg)]
    if params.profile == "differential":
        universe = _apply_ep_floor(universe, cfg)
    if not universe:
        raise ValueError("empty player universe — is the FPL data fresh?")

    best_state: _State | None = None
    best_obj = float("-inf")
    value_fn = _value_fn(params.profile, cfg)

    for r in range(cfg.optimizer.solver.restarts):
        if time.monotonic() > deadline:
            break
        rrng = random.Random(params.rng_seed + r * 7919)
        squad = _greedy_seed(universe, cfg, rrng)
        if len(squad) != 15:
            continue
        state = _State(squad, cfg)
        if not state.valid():
            continue
        obj = _hill_climb(state, universe, value_fn, cfg, rng, deadline)
        if obj > best_obj:
            best_obj = obj
            best_state = state

    if best_state is None:
        raise ValueError("solver failed to build a valid lineup")

    lineup = best_state.as_lineup()
    check = validate_lineup(lineup, params.bank, params.chips, strict=True)
    if not check.valid:
        raise ValueError(f"solver produced invalid lineup: {check.errors}")

    squad_full = [p for p in universe if p["id"] in {l["player_id"] for l in lineup}]
    squad_by_id = {p["id"]: p for p in squad_full}
    xi_players = [squad_by_id[l["player_id"]] for l in lineup if l["role"] == "starter"]
    cap = next(l["player_id"] for l in lineup if l["is_captain"])
    vc = next(l["player_id"] for l in lineup if l["is_vice_captain"])
    bench = [l["player_id"] for l in sorted(
        [l for l in lineup if l["role"] == "bench"], key=lambda l: l["bench_order"] or 0
    )]

    diff_by_player = {p["id"]: diff_map.get(p["team"]) for p in squad_full}
    projected = score_lineup(squad_full, xi_players, cap, vc, signals, cfg, diff_by_player)

    notes = []
    top3 = sorted(xi_players, key=lambda p: p["ep"], reverse=True)[:3]
    notes.append("Top-EP starters: " + ", ".join(p.get("web_name", "?") for p in top3))
    # M2 (T2.10): signal transparency — show which active signals moved the numbers
    for p in xi_players:
        for s in signals.get(p["id"], []):
            delta = "+" if s["sentiment"] == "positive" else ("−" if s["sentiment"] == "negative" else "±")
            notes.append(
                f"Signal applied: {p.get('web_name', '?')} — "
                f"\"{s['summary'][:90]}\" ({s['source'].split(':')[0]}, conf {s['confidence']:.2f}) → {delta}{abs(s['confidence']):.2f}"
            )
    if params.profile == "differential":
        diff_players = [p for p in xi_players if p["own"] < 10]
        for p in diff_players[:5]:
            notes.append(
                f"Differential: {p.get('web_name', '?')} owned by {p['own']:.1f}% (top-50 EP)"
            )
        if len(diff_players) > 5:
            notes.append(f"…and {len(diff_players) - 5} more low-ownership starters")
    if params.profile == "safe":
        risky = [p for p in xi_players if p.get("status") == "d" or p.get("chance_of_playing_next_round") == 50]
        if risky:
            notes.append("Doubt/50% players in XI: " + ", ".join(p.get("web_name", "?") for p in risky))

    return SolvedLineup(
        squad=lineup,
        xi=[l["player_id"] for l in lineup if l["role"] == "starter"],
        captain=cap,
        vice_captain=vc,
        bench=bench,
        objective=round(best_obj, 3),
        projected_points=projected,
        notes=notes,
        universe=universe,
        scoring_ctx=(signals, cfg, diff_map),
    )


def lineup_key(s: SolvedLineup) -> tuple:
    """Identity for the convergence guard: squad set + XI set + captain."""
    return (frozenset(p["player_id"] for p in s.squad), frozenset(s.xi), s.captain)


def _rescore(s: SolvedLineup) -> None:
    """Recompute projected points after a captain/roster change (T4.1)."""
    if not s.scoring_ctx:
        return
    signals, cfg, diff_map = s.scoring_ctx
    squad_full = [p for p in s.universe if p["id"] in {e["player_id"] for e in s.squad}]
    squad_by_id = {p["id"]: p for p in squad_full}
    xi_players = [squad_by_id[i] for i in s.xi]
    s.projected_points = score_lineup(
        squad_full, xi_players, s.captain, s.vice_captain, signals, cfg,
        {p["id"]: diff_map.get(p["team"]) for p in squad_full},
    )


def _try_second_captain(s: SolvedLineup) -> bool:
    """Convergence attempt (1): captain = 2nd-best-EP starter (if different)."""
    ep = {p["id"]: p.get("ep", 0.0) for p in s.universe}
    xi = [e for e in s.squad if e["player_id"] in set(s.xi)]
    if len(xi) < 3:
        return False
    ranked = sorted(xi, key=lambda e: ep.get(e["player_id"], 0.0), reverse=True)
    second = ranked[1]["player_id"]
    if second == s.captain:
        return False
    if second == s.vice_captain:
        third = ranked[2]["player_id"]
        if third == s.captain:
            return False
        for e in s.squad:
            e["is_vice_captain"] = e["player_id"] == third
        s.vice_captain = third
    for e in s.squad:
        e["is_captain"] = e["player_id"] == second
    s.captain = second
    return True


def _try_low_own_swap(s: SolvedLineup) -> bool:
    """Convergence attempt (2): swap in the best-EP player owned by <25% of the
    public (replacing the weakest same-position squadmate); budget + club cap
    must hold. Returns True if the squad changed."""
    squad_ids = {e["player_id"] for e in s.squad}
    club_count: dict[int, int] = {}
    for e in s.squad:
        club_count[e["team"]] = club_count.get(e["team"], 0) + 1
    total_cost = sum(e["now_cost"] for e in s.squad)
    by_pos: dict[int, list[dict]] = {}
    for e in s.squad:
        by_pos.setdefault(e["element_type"], []).append(e)
    cands = sorted(
        (p for p in s.universe
         if p["id"] not in squad_ids and p.get("selected_by_percent", 100.0) < 25.0),
        key=lambda p: p.get("ep", 0.0),
        reverse=True,
    )
    for c in cands:
        pos_mates = sorted(by_pos.get(c["element_type"], []),
                           key=lambda e: e["now_cost"])
        if not pos_mates:
            continue
        if club_count.get(c["team"], 0) >= CLUB_LIMIT:
            continue
        cheapest = pos_mates[0]
        if total_cost - cheapest["now_cost"] + c["now_cost"] > BUDGET:
            continue
        old_id = cheapest["player_id"]  # capture before the in-place update
        # apply the swap
        for e in s.squad:
            if e["player_id"] == old_id:
                e.update({
                    "player_id": c["id"],
                    "web_name": c.get("web_name"),
                    "element_type": c["element_type"],
                    "team": c["team"],
                    "now_cost": c["now_cost"],
                    "status": c.get("status"),
                    "can_select": c.get("can_select"),
                    "selected_by_percent": c.get("selected_by_percent"),
                })
        if old_id in s.xi:
            s.xi = [c["id"] if i == old_id else i for i in s.xi]
        if s.captain == old_id:
            s.captain = c["id"]
        if s.vice_captain == old_id:
            s.vice_captain = c["id"]
        bench_sorted = sorted(
            (e for e in s.squad if e["player_id"] not in set(s.xi)),
            key=lambda e: {p["id"]: p.get("ep", 0.0) for p in s.universe}.get(e["player_id"], 0.0),
            reverse=True,
        )
        for e in s.squad:
            if e["player_id"] in set(s.xi):
                e["bench_order"] = None
            else:
                e["bench_order"] = bench_sorted.index(e) + 1
        s.bench = [e["player_id"] for e in bench_sorted]
        return True
    return False


def dedupe_profiles(results: dict[str, SolvedLineup]) -> dict[str, SolvedLineup]:
    """Convergence guard (T1.7.5, formalized in T4.1): if a profile equals the
    first one, try (1) 2nd-best-EP captain, (2) swap in a <25%-owned player;
    if still identical, label it a variant of the first profile."""
    ordered = list(results.items())
    for i in range(1, len(ordered)):
        name, s = ordered[i]
        prev_name, prev = ordered[0]
        if lineup_key(s) == lineup_key(prev):
            changed = _try_second_captain(s) or _try_low_own_swap(s)
            if changed:
                _rescore(s)
            if lineup_key(s) == lineup_key(prev):
                s.variant_of = prev_name
    return results