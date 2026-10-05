"""Multi-start hill-climbing solver (PLAN-2 T1.7; v1.0 rules pass).

Deterministic with a fixed seed; 3 risk profiles; hard constraints (2/5/5/3,
money, ≤3/club, XI 1/3/1 minimums, captain/VC in XI) are enforced by move
construction + the rules validator on the result.

v1.0 changes (see README "How suggestions work"):
- Money is real FPL money: current players are valued at their SELL value,
  new players at their price, against bank + Σ sell values (the user's
  bank when known, else the old £100m − squad-price estimate).
- The differential profile's EP floor never removes the user's own players,
  so a plan within the free transfers always exists.
- When no plan fits the transfer limit, the fallback still charges −4 per
  extra transfer in the objective (it used to switch the limit off and
  return 14-transfer rebuilds).
- A chip is only assumed when the user picks one to play (``chip_played``):
  wildcard/free hit lift the transfer limit, bench boost scores the bench,
  triple captain makes the captain count three times.
- The captain is set to the best pick for the profile after the climb, and
  the bench is GK first, then outfield subs by projected points.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from ..db import query
from .rules import SQUAD_COMP, BUDGET, CLUB_LIMIT, validate_lineup
from .scoring import availability_multiplier, ep_final, pricing_signals, score_lineup
from .transfers import chip_covers_transfers, sell_value

PROFILES = ("max_ep", "differential", "safe")

# FIX T5: no "bench_reorder" — as_lineup() always re-derives bench order, so
# permuting state.bench could never change the produced lineup. Module-level
# constant so tests can pin the move list (§25.5).
_HILL_CLIMB_MOVES = ("squad_swap", "squad_swap", "xi_bench", "captain", "vc")

SCORING_CHIPS = ("bboost", "triple_captain")


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
    lineup_id: int | None = None
    # v1.0: the chip the user plans to play in target_gw (None = no chip).
    chip_played: str | None = None
    # v1.0: money in the bank in £0.1m (None = unknown → £100m − squad price).
    bank_money: int | None = None
    # False for GWs after the next one (FPL only publishes next-GW ep_next).
    use_ep_next: bool = True


@dataclass
class SolvedLineup:
    squad: list[dict]
    xi: list[int]
    captain: int
    vice_captain: int
    bench: list[int]  # ordered 1..4 (1 = GK sub)
    objective: float
    projected_points: dict
    variant_of: str | None = None
    notes: list[str] = field(default_factory=list)
    # T4.1: kept in-memory (never serialized) so the convergence guard can
    # differentiate a profile and re-score it after a captain/swap change.
    universe: list[dict] = field(default_factory=list)
    scoring_ctx: tuple | None = None  # (signals_by_player, cfg, diff_map, chip, use_ep_next)
    tctx: _TransferCtx | None = None  # transfer ctx used to solve (cap guard for dedupe)


def build_universe(target_gw: int, cfg) -> list[dict]:
    """Eligible players: can_select=1, not u/s, not removed (hard gates);
    chance_of_playing_next_round = 0 is excluded too (FPL prices them at 0)."""
    # N3 (rev 2): t.name (full club name) — the LineupPlayer.team_name contract.
    sql = (
        """SELECT p.*, t.name AS team_name
           FROM players p
           LEFT JOIN teams t ON t.id = p.team
           WHERE p.can_select = 1 AND p.removed = 0
             AND (p.status IS NULL OR p.status NOT IN ('u', 's'))
             AND (p.chance_of_playing_next_round IS NULL OR p.chance_of_playing_next_round <> 0)"""
    )
    return [dict(p) for p in query(sql)]


def _difficulty_map(target_gw: int) -> dict[int, list[int]]:
    """team → difficulties of its fixtures in the target GW (a list: blank
    gameweek = team absent, double gameweek = two entries). Empty dict when
    the fixtures table has nothing for the GW (no data → neutral)."""
    out: dict[int, list[int]] = {}
    rows = query(
        "SELECT home_team, away_team, difficulty_home, difficulty_away FROM fixtures WHERE event = ?",
        (target_gw,),
    )
    for r in rows:
        out.setdefault(r["home_team"], []).append(r["difficulty_home"] or 3)
        out.setdefault(r["away_team"], []).append(r["difficulty_away"] or 3)
    return out


def _team_fixtures(diff_map: dict, team: int):
    """Fixture argument for scoring: None (no data), [] (blank) or the list."""
    if not diff_map:
        return None
    return diff_map.get(team, [])


def _precompute(p: dict, cfg, signals_by_player: dict[int, list[dict]],
                diff_map: dict, use_ep_next: bool = True) -> dict:
    """Add precomputed ep / rel / ownership helpers to a player dict (copy)."""
    q = dict(p)
    sigs = signals_by_player.get(p["id"], [])
    q["ep"] = ep_final(p, sigs, cfg, _team_fixtures(diff_map, p["team"]), use_ep_next=use_ep_next)
    # reliability for the 'safe' profile: availability map + any negative news
    # (official news counts here — it is a risk signal even when priced in).
    a = availability_multiplier(p, cfg)
    neg = any(s["sentiment"] == "negative" for s in sigs)
    conf = any(s["sentiment"] == "positive" and s["category"] == "selection"
               and s["confidence"] >= 0.7 for s in sigs)
    q["rel"] = a * (1 - 0.5 * neg) * (1 + 0.1 * conf)
    q["own"] = float(p.get("selected_by_percent") or 0.0)
    return q


def _value_fn(profile: str, cfg) -> tuple:
    """Returns (value_fn, captain_base_fn).

    value_fn(p) is a player's contribution to the objective. The captain adds
    (multiplier − 1) × captain_base_fn(p) on top: for `differential` the
    low-ownership bonus counts once (FIX T8), so the base is plain EP.
    """
    if profile == "max_ep":
        f = lambda p: p["ep"]
        return f, f
    if profile == "differential":
        lam = cfg.optimizer.differential_lambda

        def f(p: dict) -> float:
            return p["ep"] + lam * (1 - p["own"] / 100.0)

        return f, (lambda p: p["ep"])
    # safe
    f = lambda p: p["ep"] * p["rel"]
    return f, f


def _objective(value_fn, xi: list[dict], captain: dict, captain_fn=None,
               bench: list[dict] | None = None, cap_mult: int = 2) -> float:
    """Σ value_fn(XI) [+ Σ value_fn(bench) under Bench Boost] + captain extra.

    ``captain_fn`` is the captain's base value (defaults to value_fn); the
    captain adds (cap_mult − 1) × captain_fn(captain).
    """
    if captain_fn is None:
        captain_fn = value_fn
    total = sum(value_fn(p) for p in xi)
    if bench:
        total += sum(value_fn(p) for p in bench)
    if captain is not None:
        total += (cap_mult - 1) * captain_fn(captain)
    return total


@dataclass
class _TransferCtx:
    """Transfer- and money-aware objective context.

    transfers = 15 − kept (a swap — one in + one out — is one transfer).
    The free-transfer limit is a HARD constraint: hard_cap bounds every
    accepted state, except when a wildcard/free hit covers the GW
    (chip_covers) or for forced replacements of unavailable players
    (cap = max(bank, forced_transfers)). has_current=False lifts the cap but
    keeps the −4 penalty in the objective (fresh builds and the no-feasible-
    plan fallback), so extra transfers are only taken when they pay.

    Money: spend(squad) = Σ cost_basis.get(id, now_cost) must stay ≤ budget,
    where cost_basis holds current players at their sell value and budget =
    bank money + Σ sell values (defaults reproduce the plain £100m rule).
    """
    cur_ids: frozenset
    bank: int
    chip_covers: bool
    profile: str
    forced_transfers: int = 0   # min transfers any valid squad must make
    has_current: bool = True    # False: no real current squad / fallback (no cap)
    # legacy (pre-v1.0) per-player fee map; kept so older callers still work.
    sell_fee: dict = field(default_factory=dict)
    budget: int = BUDGET
    cost_basis: dict = field(default_factory=dict)
    cap_mult: int = 2           # 3 under Triple Captain
    bench_counts: bool = False  # True under Bench Boost

    @property
    def hard_cap(self) -> int | None:
        """Max transfers a suggestion may use; None = no hard cap."""
        if self.chip_covers or not self.has_current:
            return None
        return max(self.bank, self.forced_transfers)

    def within_limit(self, squad: list[dict]) -> bool:
        cap = self.hard_cap
        return cap is None or self.transfers(squad) <= cap

    def transfers(self, squad: list[dict]) -> int:
        kept = len({p["id"] for p in squad} & self.cur_ids)
        return 15 - kept

    def cost(self, p: dict) -> int:
        pid = p.get("id", p.get("player_id"))
        if pid in self.cost_basis:
            return self.cost_basis[pid]
        return p["now_cost"]

    def spend(self, squad: list[dict]) -> int:
        return sum(self.cost(p) for p in squad)

    def sale_fee(self, p: dict) -> int:
        """Legacy fee owed when selling ``p`` (only used without cost_basis)."""
        if self.cost_basis:
            return 0
        return self.sell_fee.get(p.get("id", p.get("player_id")), 0)

    def penalty_points(self, squad: list[dict]) -> float:
        if self.chip_covers:
            return 0.0
        return 4.0 * max(0, self.transfers(squad) - self.bank)

    def total(self, value_fn, xi: list[dict], captain: dict, squad: list[dict],
              captain_fn=None) -> float:
        churn = 0.1 * self.transfers(squad) if self.profile == "safe" else 0.0
        bench = None
        if self.bench_counts:
            xi_ids = {p["id"] for p in xi}
            bench = [p for p in squad if p["id"] not in xi_ids]
        return (_objective(value_fn, xi, captain, captain_fn, bench, self.cap_mult)
                - self.penalty_points(squad) - churn)


def _greedy_seed(universe: list[dict], cfg, rng: random.Random,
                 budget: int = BUDGET) -> list[dict]:
    """Greedy 2/5/5/3 squad by ep/price with club cap + budget; noisy for restart diversity."""
    squad: list[dict] = []
    picked: set[int] = set()
    club_count: dict[int, int] = {}
    left = budget
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
            if p["now_cost"] > left:
                continue
            # restart diversity: occasionally take the 2nd best
            pick = p
            if rng.random() < 0.25 and idx + 1 < len(cands):
                alt = cands[idx + 1]
                if (alt["id"] not in picked and club_count.get(alt["team"], 0) < CLUB_LIMIT
                        and alt["now_cost"] <= left):
                    pick = alt
            squad.append(pick)
            picked.add(pick["id"])
            club_count[pick["team"]] = club_count.get(pick["team"], 0) + 1
            left -= pick["now_cost"]
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
            if p["now_cost"] > left:
                continue
            squad.append(p)
            picked.add(p["id"])
            club_count[p["team"]] = club_count.get(p["team"], 0) + 1
            left -= p["now_cost"]
            need -= 1
    return squad


def _seed_from_current(current_squad: list[dict], universe: list[dict], cfg,
                       rng: random.Random, tctx: _TransferCtx | None = None) -> list[dict]:
    """Restart-0 seed: the user's actual squad. Players no longer in the
    universe (u/s, can_select=0, chance 0) are replaced by the best-EP
    same-position universe player (club cap + money respected).

    Returns a 15-player list when possible; otherwise a short list, which the
    caller skips (``len(squad) != 15``) and falls back to greedy restarts.
    """
    uni_by_id = {p["id"]: p for p in universe}
    squad = [uni_by_id[p["player_id"]] for p in current_squad if p["player_id"] in uni_by_id]
    missing = [p["player_id"] for p in current_squad if p["player_id"] not in uni_by_id]
    missing_pos: list[int] = []
    if missing:
        rows = query(
            "SELECT id, element_type FROM players WHERE id IN (%s)"
            % ",".join("?" * len(missing)),
            missing,
        )
        missing_pos = [r["element_type"] for r in rows]
    cost = tctx.cost if tctx is not None else (lambda p: p["now_cost"])
    budget = tctx.budget if tctx is not None else BUDGET
    picked = {p["id"] for p in squad}
    club_count: dict[int, int] = {}
    total = 0
    for p in squad:
        club_count[p["team"]] = club_count.get(p["team"], 0) + 1
        total += cost(p)
    cands = sorted(universe, key=lambda p: -p["ep"])
    for et in missing_pos:
        for c in cands:
            if c["id"] in picked or c["element_type"] != et:
                continue
            if club_count.get(c["team"], 0) >= CLUB_LIMIT:
                continue
            if total + cost(c) > budget:
                continue
            squad.append(c)
            picked.add(c["id"])
            club_count[c["team"]] = club_count.get(c["team"], 0) + 1
            total += cost(c)
            break
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

    def __init__(self, squad: list[dict], cfg, tctx: _TransferCtx | None = None):
        self.squad = squad
        self.tctx = tctx
        self.club_count: dict[int, int] = {}
        for p in squad:
            self.club_count[p["team"]] = self.club_count.get(p["team"], 0) + 1
        self.budget = tctx.budget if tctx is not None else BUDGET
        self.total_cost = tctx.spend(squad) if tctx is not None else sum(p["now_cost"] for p in squad)
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

    def cost(self, p: dict) -> int:
        return self.tctx.cost(p) if self.tctx is not None else p["now_cost"]

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
        if self.total_cost > self.budget:
            return False
        if any(c > CLUB_LIMIT for c in self.club_count.values()):
            return False
        return True

    def set_best_captain(self, captain_fn, cap_mult: int = 2) -> None:
        """Captain = the starter whose extra (cap_mult − 1) × captain_fn is
        largest; vice = the runner-up (the VC has no multiplier — he is the
        backup if the captain does not play)."""
        if len(self.xi) < 2:
            return
        ranked = sorted(self.xi, key=lambda p: (captain_fn(p), p["ep"], -p["id"]), reverse=True)
        self.captain, self.vice = ranked[0], ranked[1]

    def as_lineup(self) -> list[dict]:
        xi_ids = {p["id"] for p in self.xi}
        # FPL bench: the GK sub has his own slot (order 1); outfield subs are
        # ranked by projected points (2 = first to come on).
        gk_bench = [p for p in self.bench if p["element_type"] == 1]
        outfield = sorted((p for p in self.bench if p["element_type"] != 1),
                          key=lambda p: p["ep"], reverse=True)
        bench_sorted = gk_bench + outfield
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
                    # A23: full LineupPlayer display fields; no bought_cost — a
                    # suggested squad has no purchase prices (SquadPlayer type).
                    "team_name": p.get("team_name"),
                    "now_cost": p["now_cost"],
                    "status": p.get("status"),
                    "can_select": p.get("can_select"),
                    "ep_next": p.get("ep_next"),
                    "ep": round(p.get("ep", 0.0), 2),
                    "selected_by_percent": p.get("selected_by_percent"),
                    "chance_of_playing_next_round": p.get("chance_of_playing_next_round"),
                    "role": role,
                    "bench_order": order,
                    "is_captain": p is self.captain,
                    "is_vice_captain": p is self.vice,
                }
            )
        return out


def _hill_climb(state: _State, universe: list[dict], value_fn, cfg,
                rng: random.Random, deadline: float, tctx: _TransferCtx,
                captain_fn=None) -> float:
    cap_fn = captain_fn or value_fn

    def score() -> float:
        return tctx.total(value_fn, state.xi, state.captain, state.squad, cap_fn)

    if state.valid():
        state.set_best_captain(cap_fn, tctx.cap_mult)
    best = score() if (state.valid() and tctx.within_limit(state.squad)) else float("-inf")
    no_improve = 0
    moves = 0
    # FIX T7: hard move budget per restart (0/absent = disabled) — deterministic
    # replay depends only on seed + data; the timebox stays the ceiling.
    max_moves = getattr(cfg.optimizer.solver, "max_moves_per_restart", 0) or 0
    by_pos: dict[int, list[dict]] = {}
    for p in universe:
        by_pos.setdefault(p["element_type"], []).append(p)

    while no_improve < 400:
        moves += 1
        if max_moves and moves > max_moves:
            break
        if moves % 100 == 0 and time.monotonic() > deadline:
            break
        kind = rng.choice(_HILL_CLIMB_MOVES)
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
                elif (state.total_cost + state.cost(in_p) - state.cost(out_p)
                      + tctx.sale_fee(out_p) > state.budget):
                    ok = False
                else:
                    state.squad[i] = in_p
                    state.club_count[out_p["team"]] -= 1
                    state.club_count[in_p["team"]] = state.club_count.get(in_p["team"], 0) + 1
                    state.total_cost += state.cost(in_p) - state.cost(out_p)
                    if out_p in state.xi:
                        state.xi[state.xi.index(out_p)] = in_p
                    elif out_p in state.bench:
                        state.bench[state.bench.index(out_p)] = in_p
                    if state.captain is out_p:
                        state.captain = in_p
                    if state.vice is out_p:
                        state.vice = in_p
        elif kind == "xi_bench":
            s = rng.choice(state.xi)
            cands = [b for b in state.bench if b["element_type"] == s["element_type"]]
            if not cands:
                ok = False
            else:
                b = rng.choice(cands)
                state.xi[state.xi.index(s)] = b
                state.bench[state.bench.index(b)] = s
                state.set_best_captain(cap_fn, tctx.cap_mult)
        elif kind == "captain":
            cands = [p for p in state.xi if p is not state.captain]
            if not cands:
                ok = False
            else:
                state.captain = rng.choice(cands)
                if state.vice is state.captain:
                    state.vice = next(p for p in state.xi if p is not state.captain)
        elif kind == "vc":
            cands = [p for p in state.xi if p is not state.captain and p is not state.vice]
            if not cands:
                ok = False
            else:
                state.vice = rng.choice(cands)

        if ok and state.valid():
            if not tctx.within_limit(state.squad):
                no_improve += 1
                revert()
            else:
                if kind == "squad_swap":
                    state.set_best_captain(cap_fn, tctx.cap_mult)
                obj = score()
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
    # The captain is a deterministic choice, not something to stumble on.
    if state.valid() and tctx.within_limit(state.squad):
        state.set_best_captain(cap_fn, tctx.cap_mult)
        best = max(best, score())
    return best


def _apply_ep_floor(universe: list[dict], cfg, keep_ids: frozenset = frozenset()) -> list[dict]:
    """T4.1: differential eligibility floor — candidates must have
    ep_final ≥ optimizer.differential_ep_floor × max ep_final in the universe
    (prevents low-EP lottery picks). ``keep_ids`` (the user's own players)
    always stay: removing them made every plan exceed the free transfers."""
    if not universe:
        return universe
    max_ep = max(p["ep"] for p in universe)
    floor = cfg.optimizer.differential_ep_floor * max_ep
    return [p for p in universe if p["ep"] >= floor or p["id"] in keep_ids]


def _run_restarts(params: SolveParams, universe: list[dict], value_fn,
                  tctx: _TransferCtx, cfg, rng: random.Random,
                  deadline: float, captain_fn=None) -> tuple[_State | None, float]:
    """Hill climbing over up to `cfg.optimizer.solver.restarts` seeds.

    Restart 0 seeds from the user's actual squad; later restarts use greedy
    seeds only when the transfer cap is inactive (a fresh ~15-transfer squad
    can never satisfy the cap). So with a real squad and no covering chip the
    everyday case is a single local search from the user's squad (A21).
    """
    best_state: _State | None = None
    best_obj = float("-inf")
    for r in range(cfg.optimizer.solver.restarts):
        if time.monotonic() > deadline:
            break
        rrng = random.Random(params.rng_seed + r * 7919)
        if r == 0 and params.current_squad:
            squad = _seed_from_current(params.current_squad, universe, cfg, rrng, tctx)
        elif tctx.hard_cap is None:
            # sell values never exceed prices, so a squad that fits the budget at
            # current prices also fits it under the cost basis.
            squad = _greedy_seed(universe, cfg, rrng, tctx.budget)
        else:
            continue  # a fresh squad can never satisfy the cap
        if len(squad) != 15:
            continue
        state = _State(squad, cfg, tctx)
        if not state.valid():
            continue
        # A21: _hill_climb gets the outer `rng`, not the per-restart `rrng`
        # (rrng is for seed variety only) — deterministic replay.
        obj = _hill_climb(state, universe, value_fn, cfg, rng, deadline, tctx, captain_fn)
        if obj > best_obj:
            best_obj = obj
            best_state = state
    return best_state, best_obj


def _money_ctx(params: SolveParams) -> tuple[int, dict]:
    """(budget, cost_basis) in £0.1m.

    Current players are held at their sell value; the budget is the money in
    the bank plus everything the squad sells for. Unknown bank money falls
    back to £100m − Σ current price (the pre-v1.0 assumption, identical to
    the old "Σ price + sale fees ≤ £100m" rule).
    """
    cur = params.current_squad
    if len(cur) != 15:
        return (params.bank_money if params.bank_money is not None else BUDGET), {}
    basis = {p["player_id"]: sell_value(p.get("bought_cost"), p["now_cost"]) for p in cur}
    bank_money = params.bank_money
    if bank_money is None:
        bank_money = max(0, BUDGET - sum(p["now_cost"] for p in cur))
    return bank_money + sum(basis.values()), basis


def _signal_note(name: str, s: dict) -> str | None:
    """Rationale line for a signal that moved a starter's projection."""
    if s["sentiment"] not in ("negative", "positive"):
        return None
    conf = float(s["confidence"])
    pct = round((50 if s["sentiment"] == "negative" else 10) * conf)
    sign = "−" if s["sentiment"] == "negative" else "+"
    src = str(s.get("source") or "").split(":")[0]
    return f"{name}: \"{s['summary'][:90]}\" ({src}, {round(conf * 100)}% sure) → {sign}{pct}%"


def solve(params: SolveParams) -> SolvedLineup:
    cfg = params.cfg
    rng = random.Random(params.rng_seed)
    deadline = time.monotonic() + cfg.optimizer.solver.timebox_sec
    diff_map = _difficulty_map(params.target_gw)
    signals = params.signals_by_player
    chip = params.chip_played

    universe_raw = [_precompute(p, cfg, signals, diff_map, params.use_ep_next)
                    for p in build_universe(params.target_gw, cfg)]
    uni_ids = {p["id"] for p in universe_raw}
    cur_ids = frozenset(p["player_id"] for p in params.current_squad)
    if params.profile == "differential":
        universe = _apply_ep_floor(universe_raw, cfg, keep_ids=cur_ids)
    else:
        universe = universe_raw
    if not universe:
        raise ValueError("empty player universe — is the FPL data fresh?")

    value_fn, captain_fn = _value_fn(params.profile, cfg)
    budget, basis = _money_ctx(params)
    covers = chip_covers_transfers(params.chips, params.target_gw, chip)
    common = dict(bank=params.bank, profile=params.profile, budget=budget, cost_basis=basis,
                  cap_mult=3 if chip == "triple_captain" else 2,
                  bench_counts=chip == "bboost")
    tctx = _TransferCtx(
        cur_ids=cur_ids,
        chip_covers=covers,
        forced_transfers=len(cur_ids - uni_ids),
        has_current=len(params.current_squad) == 15,
        **common,
    )

    best_state, best_obj = _run_restarts(params, universe, value_fn, tctx, cfg, rng,
                                         deadline, captain_fn)
    if best_state is None and universe is not universe_raw:
        # The differential EP floor can leave too few players (or clubs) to
        # build 15 within the 3-per-club rule — use the full pool instead.
        universe = universe_raw
        best_state, best_obj = _run_restarts(params, universe, value_fn, tctx, cfg, rng,
                                             deadline, captain_fn)
    active_ctx = tctx
    fallback = False
    if best_state is None and tctx.hard_cap is not None:
        # Degenerate case (e.g. an unavailable player with no affordable
        # replacement): drop the hard cap but KEEP the −4 per extra transfer
        # in the objective, so extra transfers are only taken when they pay.
        fallback = True
        active_ctx = _TransferCtx(cur_ids=cur_ids, chip_covers=False, has_current=False,
                                  forced_transfers=tctx.forced_transfers, **common)
        best_state, best_obj = _run_restarts(params, universe, value_fn, active_ctx, cfg, rng,
                                             deadline, captain_fn)

    if best_state is None:
        raise ValueError("solver failed to build a valid lineup")
    if not active_ctx.within_limit(best_state.squad):
        raise ValueError("solver produced a lineup that exceeds the free-transfer bank")

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

    diff_by_player = {p["id"]: _team_fixtures(diff_map, p["team"]) for p in squad_full}
    score_chip = chip if chip in SCORING_CHIPS else None
    projected = score_lineup(squad_full, xi_players, cap, vc, signals, cfg, diff_by_player,
                             chip=score_chip, use_ep_next=params.use_ep_next)

    notes = []
    if fallback:
        notes.append("No plan fits your free transfers (an unavailable player has no "
                     "affordable replacement), so extra transfers were allowed at −4 points each.")
    top3 = sorted(xi_players, key=lambda p: p["ep"], reverse=True)[:3]
    notes.append("Top projected starters: " + ", ".join(
        f"{p.get('web_name', '?')} ({p['ep']:.1f})" for p in top3))
    for p in xi_players:
        for s in pricing_signals(signals.get(p["id"], [])):
            line = _signal_note(p.get("web_name", "?"), s)
            if line:
                notes.append("News: " + line)
    if params.profile == "differential":
        diff_players = [p for p in xi_players if p["own"] < 10]
        if diff_players:
            notes.append(f"{len(diff_players)} of 11 starters are owned by under 10% of managers: "
                         + ", ".join(f"{p.get('web_name', '?')} ({p['own']:.1f}%)"
                                     for p in diff_players[:5])
                         + (" …" if len(diff_players) > 5 else ""))
    if params.profile == "safe":
        risky = [p for p in xi_players if p.get("status") == "d"
                 or (p.get("chance_of_playing_next_round") or 100) < 100]
        if risky:
            notes.append("Injury doubts still in the XI: " + ", ".join(p.get("web_name", "?") for p in risky))
    if not params.use_ep_next:
        notes.append(f"GW{params.target_gw} is beyond the next gameweek, so projections use "
                     "season averages (FPL only publishes expected points for the next GW).")

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
        scoring_ctx=(signals, cfg, diff_map, score_chip, params.use_ep_next),
        tctx=active_ctx,
    )


def lineup_key(s: SolvedLineup) -> tuple:
    """Identity for the convergence guard: squad set + XI set + captain."""
    return (frozenset(p["player_id"] for p in s.squad), frozenset(s.xi), s.captain)


def _rescore(s: SolvedLineup) -> None:
    """Recompute projected points after a captain/roster change (T4.1)."""
    if not s.scoring_ctx:
        return
    signals, cfg, diff_map, chip, use_ep_next = (tuple(s.scoring_ctx) + (None, True))[:5]
    squad_full = [p for p in s.universe if p["id"] in {e["player_id"] for e in s.squad}]
    squad_by_id = {p["id"]: p for p in squad_full}
    xi_players = [squad_by_id[i] for i in s.xi]
    s.projected_points = score_lineup(
        squad_full, xi_players, s.captain, s.vice_captain, signals, cfg,
        {p["id"]: _team_fixtures(diff_map, p["team"]) for p in squad_full},
        chip=chip, use_ep_next=use_ep_next,
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


def _try_low_own_swap(s: SolvedLineup, tctx: _TransferCtx | None = None) -> bool:
    """Convergence attempt (2): swap in the best-EP player owned by <25% of the
    public (replacing the cheapest same-position squadmate); money + club cap
    must hold. When ``tctx`` is given, a candidate whose swap would push
    transfers over the free-transfer cap is skipped (try the next candidate).
    Returns True if the squad changed."""
    squad_ids = {e["player_id"] for e in s.squad}
    club_count: dict[int, int] = {}
    for e in s.squad:
        club_count[e["team"]] = club_count.get(e["team"], 0) + 1
    if tctx is not None:
        total_cost = tctx.spend(s.squad)
        budget = tctx.budget
    else:
        total_cost = sum(e["now_cost"] for e in s.squad)
        budget = BUDGET
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
        old_id = cheapest["player_id"]  # capture before the in-place update
        if tctx is not None:
            out_cost = tctx.cost(cheapest)
            in_cost = tctx.cost(c)
            fee = tctx.sale_fee(cheapest)
        else:
            out_cost, in_cost, fee = cheapest["now_cost"], c["now_cost"], 0
        if total_cost - out_cost + in_cost + fee > budget:
            continue
        if tctx is not None:
            cap = tctx.hard_cap
            if cap is not None:
                new_ids = (squad_ids - {old_id}) | {c["id"]}
                kept = len(new_ids & tctx.cur_ids)
                if (15 - kept) > cap:
                    continue  # swap would breach the transfer cap — try next candidate
        # apply the swap
        for e in s.squad:
            if e["player_id"] == old_id:
                e.update({
                    "player_id": c["id"],
                    "web_name": c.get("web_name"),
                    "element_type": c["element_type"],
                    "team": c["team"],
                    "team_name": c.get("team_name"),
                    "now_cost": c["now_cost"],
                    "status": c.get("status"),
                    "can_select": c.get("can_select"),
                    "ep_next": c.get("ep_next"),
                    "ep": round(c.get("ep", 0.0), 2),
                    "selected_by_percent": c.get("selected_by_percent"),
                    "chance_of_playing_next_round": c.get("chance_of_playing_next_round"),
                })
        if old_id in s.xi:
            s.xi = [c["id"] if i == old_id else i for i in s.xi]
        if s.captain == old_id:
            s.captain = c["id"]
        if s.vice_captain == old_id:
            s.vice_captain = c["id"]
        ep = {p["id"]: p.get("ep", 0.0) for p in s.universe}
        bench_entries = [e for e in s.squad if e["player_id"] not in set(s.xi)]
        bench_sorted = ([e for e in bench_entries if e["element_type"] == 1]
                        + sorted((e for e in bench_entries if e["element_type"] != 1),
                                 key=lambda e: ep.get(e["player_id"], 0.0), reverse=True))
        for e in s.squad:
            if e["player_id"] in set(s.xi):
                e["bench_order"] = None
            else:
                e["bench_order"] = bench_sorted.index(e) + 1
        s.bench = [e["player_id"] for e in bench_sorted]
        return True
    return False


def dedupe_profiles(results: dict[str, SolvedLineup],
                    tctx: _TransferCtx | None = None) -> dict[str, SolvedLineup]:
    """Convergence guard (T1.7.5, formalized in T4.1): if a profile equals the
    first one, try (1) 2nd-best-EP captain, (2) swap in a <25%-owned player.
    Either way the profile is labelled a variant of the first one, so the UI
    can say "same team as …, alternative captain" instead of presenting a
    deliberately weaker pick as an independent plan.

    When ``tctx`` is given, attempt (2) must also respect the free-transfer cap
    (judged by each profile's own ctx, FIX T4)."""
    ordered = list(results.items())
    for i in range(1, len(ordered)):
        name, s = ordered[i]
        prev_name, prev = ordered[0]
        if lineup_key(s) == lineup_key(prev):
            effective = s.tctx if getattr(s, "tctx", None) is not None else tctx
            changed = _try_second_captain(s) or _try_low_own_swap(s, effective)
            if changed:
                _rescore(s)
            s.variant_of = prev_name
            s.notes.insert(0, f"Same best team as {prev_name}"
                           + ("; shown with an alternative captain or one low-ownership swap."
                              if changed else "."))
    return results
