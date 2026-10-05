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
from .transfers import chip_covers_transfers, sell_value

PROFILES = ("max_ep", "differential", "safe")

# FIX T5: no "bench_reorder" — as_lineup() always re-derives bench order by
# EP-descending, so permuting state.bench could never change the produced
# lineup; the move only burned 1/6 of the move budget. Module-level constant
# so tests can pin the move list (§25.5).
_HILL_CLIMB_MOVES = ("squad_swap", "squad_swap", "xi_bench", "captain", "vc")


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
    tctx: _TransferCtx | None = None  # transfer ctx used to solve (cap guard for dedupe)


def build_universe(target_gw: int, cfg) -> list[dict]:
    """Eligible players: can_select=1, not u/s, not removed (hard gates).

    M2 (T2.10): with optimizer.availability.active, chance_of_playing_next_round
    = 0 is also excluded (the A(p) map would price them at 0 anyway).
    """
    # A23: team_name comes from the teams join so as_lineup() can emit the
    # full LineupPlayer display fields, not just the solver's own columns.
    # N3 (rev 2): t.name, not t.short_name — every other producer of
    # LineupPlayer.team_name (lineups._load_lineup, signals.names) uses the
    # full club name, so the suggested-squad payload must match that contract.
    sql = (
        """SELECT p.*, t.name AS team_name
           FROM players p
           LEFT JOIN teams t ON t.id = p.team
           WHERE p.can_select = 1 AND p.removed = 0
             AND (p.status IS NULL OR p.status NOT IN ('u', 's'))"""
    )
    if cfg.optimizer.availability.active:
        sql += " AND (p.chance_of_playing_next_round IS NULL OR p.chance_of_playing_next_round <> 0)"
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
    q["ep"] = ep_final(p, signals_by_player.get(p["id"], []), cfg, diff_map.get(p["team"]))
    # reliability for the 'safe' profile (M1: A≈1, no signals → 1.0)
    a = availability_multiplier(p, cfg)
    neg = any(s["sentiment"] == "negative" for s in signals_by_player.get(p["id"], []))
    conf = any(s["sentiment"] == "positive" and s["category"] == "selection"
               and s["confidence"] >= 0.7 for s in signals_by_player.get(p["id"], []))
    q["rel"] = a * (1 - 0.5 * neg) * (1 + 0.1 * conf)
    q["own"] = float(p.get("selected_by_percent") or 0.0)
    return q


def _objective(value_fn, xi: list[dict], captain: dict, captain_fn=None) -> float:
    """Σ value_fn(xi) + captain term.

    FIX T8 (explicit decision): for `differential` the captain term doubles EP
    (captains score double) but counts the low-ownership λ bonus only ONCE —
    `captain_fn` implements that split. Passing captain_fn=None (max_ep/safe)
    keeps the legacy "count the captain twice" behaviour, which is exactly
    right when value_fn has no extra bonus term.
    """
    if captain_fn is None:
        captain_fn = value_fn
    return sum(value_fn(p) for p in xi) + captain_fn(captain)


def _value_fn(profile: str, cfg) -> tuple:
    """Returns (value_fn, captain_fn) — see _objective for the T8 split."""
    if profile == "max_ep":
        f = lambda p: p["ep"]
        return f, None
    if profile == "differential":
        lam = cfg.optimizer.differential_lambda

        def f(p: dict) -> float:
            return p["ep"] + lam * (1 - p["own"] / 100.0)

        def captain_fn(p: dict) -> float:
            # EP doubles, λ(1−own) counts once (FIX T8).
            return 2 * p["ep"] + lam * (1 - p["own"] / 100.0)

        return f, captain_fn
    # safe
    f = lambda p: p["ep"] * p["rel"]
    return f, None


@dataclass
class _TransferCtx:
    """Transfer-aware objective context.

    transfers = 15 − kept, kept = |squad ∩ current| (a swap — one in + one out —
    is one transfer).
    Transfers over the bank are a HARD constraint (D1): hard_cap bounds every
    accepted state, with exceptions only when a wildcard/free-hit covers the
    target GW (unlimited) or for forced replacements of unavailable players
    (D2: cap = max(bank, forced_transfers)). The 4-pt penalty term remains in
    the objective only for the forced-overrun portion and for legacy
    no-current-squad solves; 'safe's churn is a within-limit tie-breaker.
    """
    cur_ids: frozenset
    bank: int
    chip_covers: bool
    profile: str
    forced_transfers: int = 0   # min transfers any valid squad must make (D2)
    has_current: bool = True    # False only for empty/degenerate current squads
    # FIX T1: player_id → money fee owed when selling (now_cost − sell_value).
    # 0/absent for players bought at (or below) their current price.
    sell_fee: dict = field(default_factory=dict)

    @property
    def hard_cap(self) -> int | None:
        """Max transfers a suggestion may use; None = unlimited (chip covers the GW,
        or no real current squad to measure against)."""
        if self.chip_covers or not self.has_current:
            return None
        return max(self.bank, self.forced_transfers)

    def within_limit(self, squad: list[dict]) -> bool:
        cap = self.hard_cap
        return cap is None or self.transfers(squad) <= cap

    def transfers(self, squad: list[dict]) -> int:
        kept = len({p["id"] for p in squad} & self.cur_ids)
        return 15 - kept

    def penalty_points(self, squad: list[dict]) -> float:
        if self.chip_covers:
            return 0.0
        return 4.0 * max(0, self.transfers(squad) - self.bank)

    def total(self, value_fn, xi: list[dict], captain: dict, squad: list[dict],
              captain_fn=None) -> float:
        churn = 0.1 * self.transfers(squad) if self.profile == "safe" else 0.0
        return (_objective(value_fn, xi, captain, captain_fn)
                - self.penalty_points(squad) - churn)


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


def _seed_from_current(current_squad: list[dict], universe: list[dict], cfg,
                       rng: random.Random) -> list[dict]:
    """Restart-0 seed: the user's actual squad. Players no longer in the
    universe (u/s, can_select=0, chance 0) are replaced by the best-EP
    same-position universe player (club cap + budget respected).

    Returns a 15-player list when possible; otherwise a short list, which the
    caller skips (``len(squad) != 15``) and falls back to greedy restarts.
    ``current_squad`` entries are {player_id, web_name, now_cost, bought_cost}
    (no element_type) — missing positions are looked up in the players table.
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
    # backfill missing positions with best-EP eligible players
    picked = {p["id"] for p in squad}
    club_count: dict[int, int] = {}
    total = 0
    for p in squad:
        club_count[p["team"]] = club_count.get(p["team"], 0) + 1
        total += p["now_cost"]
    cands = sorted(universe, key=lambda p: -p["ep"])
    for et in missing_pos:
        for c in cands:
            if c["id"] in picked or c["element_type"] != et:
                continue
            if club_count.get(c["team"], 0) >= CLUB_LIMIT:
                continue
            if total + c["now_cost"] > BUDGET:
                continue
            squad.append(c)
            picked.add(c["id"])
            club_count[c["team"]] = club_count.get(c["team"], 0) + 1
            total += c["now_cost"]
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
                    # A23: complete the display fields the frontend type
                    # promises (team_name / ep_next / chance_of_playing_next_round
                    # come straight from the universe row). No bought_cost — a
                    # suggested squad has no purchase prices (SquadPlayer type).
                    "team_name": p.get("team_name"),
                    "now_cost": p["now_cost"],
                    "status": p.get("status"),
                    "can_select": p.get("can_select"),
                    "ep_next": p.get("ep_next"),
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
    best = (tctx.total(value_fn, state.xi, state.captain, state.squad, captain_fn)
            if (state.valid() and tctx.within_limit(state.squad)) else float("-inf"))
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
        # FIX T5: see _HILL_CLIMB_MOVES — no-op bench permutations removed.
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
                elif (state.total_cost + in_p["now_cost"] - out_p["now_cost"]
                      + tctx.sell_fee.get(out_p["id"], 0) > BUDGET):
                    # FIX T1: selling frees the sell value, not the current
                    # price — a fee-bearing sale tightens the money budget.
                    ok = False
                else:
                    state.squad[i] = in_p
                    state.club_count[out_p["team"]] -= 1
                    state.club_count[in_p["team"]] = state.club_count.get(in_p["team"], 0) + 1
                    state.total_cost += in_p["now_cost"] - out_p["now_cost"]
                    if out_p in state.xi:
                        state.xi[state.xi.index(out_p)] = in_p
                    elif out_p in state.bench:
                        # FIX T6: unreachable "promote" branch deleted — xi and
                        # bench partition the squad, so out_p is always found by
                        # one of the two branches above.
                        state.bench[state.bench.index(out_p)] = in_p
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

        if ok and state.valid():
            if not tctx.within_limit(state.squad):
                # Out-of-limit moves are treated exactly like invalid moves.
                no_improve += 1
                revert()
            else:
                obj = tctx.total(value_fn, state.xi, state.captain, state.squad, captain_fn)
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


def _run_restarts(params: SolveParams, universe: list[dict], value_fn,
                  tctx: _TransferCtx, cfg, rng: random.Random,
                  deadline: float, captain_fn=None) -> tuple[_State | None, float]:
    """Hill climbing over up to `cfg.optimizer.solver.restarts` seeds.

    Restart 0 seeds from the user's actual squad; later restarts use greedy
    seeds only when the transfer cap is inactive (a fresh ~30-transfer squad
    can never satisfy the cap). So with a real squad and no covering chip the
    everyday case is a single local search from the user's squad, not a
    multi-start search (A21). Returns (best_state, best_objective).
    """
    best_state: _State | None = None
    best_obj = float("-inf")
    for r in range(cfg.optimizer.solver.restarts):
        if time.monotonic() > deadline:
            break
        rrng = random.Random(params.rng_seed + r * 7919)
        if r == 0 and params.current_squad:
            squad = _seed_from_current(params.current_squad, universe, cfg, rrng)
        elif tctx.hard_cap is None:
            squad = _greedy_seed(universe, cfg, rrng)
        else:
            continue  # a fresh ~30-transfer squad can never satisfy the cap
        if len(squad) != 15:
            continue
        state = _State(squad, cfg)
        if not state.valid():
            continue
        # A21: _hill_climb gets the outer `rng`, not the per-restart `rrng`
        # (rrng is for seed variety only) — intentional, so a given seed's
        # climb replays deterministically regardless of which restart ran it.
        obj = _hill_climb(state, universe, value_fn, cfg, rng, deadline, tctx, captain_fn)
        if obj > best_obj:
            best_obj = obj
            best_state = state
    return best_state, best_obj


def solve(params: SolveParams) -> SolvedLineup:
    cfg = params.cfg
    rng = random.Random(params.rng_seed)
    deadline = time.monotonic() + cfg.optimizer.solver.timebox_sec
    diff_map = _difficulty_map(params.target_gw)
    signals = params.signals_by_player

    universe_raw = [_precompute(p, cfg, signals, diff_map) for p in build_universe(params.target_gw, cfg)]
    # Forced replacements (D2) are measured against the pre-floor universe: a
    # suspended player must be replaced regardless of the differential EP floor.
    uni_ids = {p["id"] for p in universe_raw}
    if params.profile == "differential":
        universe = _apply_ep_floor(universe_raw, cfg)
    else:
        universe = universe_raw
    if not universe:
        raise ValueError("empty player universe — is the FPL data fresh?")

    value_fn, captain_fn = _value_fn(params.profile, cfg)
    cur_ids = frozenset(p["player_id"] for p in params.current_squad)
    # FIX T1: selling a player frees its sell value, not its current price —
    # build the per-player fee (now_cost − sell_value) once, upfront.
    sell_fee = {
        p["player_id"]: p["now_cost"] - sell_value(p.get("bought_cost"), p["now_cost"])
        for p in params.current_squad
    }
    tctx = _TransferCtx(
        cur_ids=cur_ids,
        bank=params.bank,
        chip_covers=chip_covers_transfers(params.chips, params.target_gw),
        profile=params.profile,
        forced_transfers=len(cur_ids - uni_ids),
        has_current=len(params.current_squad) == 15,
        sell_fee=sell_fee,
    )

    best_state, best_obj = _run_restarts(params, universe, value_fn, tctx, cfg, rng,
                                         deadline, captain_fn)
    active_ctx = tctx
    if best_state is None and tctx.hard_cap is not None:
        # Degenerate no-feasible-seed case (e.g. an unavailable player with no
        # eligible replacement within budget/club cap): retry once with the
        # legacy soft-penalty ctx (cap disabled) instead of raising.
        active_ctx = _TransferCtx(cur_ids=cur_ids, bank=params.bank,
                                  chip_covers=True, profile=params.profile,
                                  sell_fee=sell_fee)
        best_state, best_obj = _run_restarts(params, universe, value_fn, active_ctx, cfg, rng,
                                             deadline, captain_fn)

    if best_state is None:
        raise ValueError("solver failed to build a valid lineup")

    # Defense in depth: the cap must hold for the ctx that produced the state.
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
        tctx=active_ctx,
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


def _try_low_own_swap(s: SolvedLineup, tctx: _TransferCtx | None = None) -> bool:
    """Convergence attempt (2): swap in the best-EP player owned by <25% of the
    public (replacing the weakest same-position squadmate); budget + club cap
    must hold. When ``tctx`` is given, a candidate whose swap would push
    transfers over the free-transfer cap is skipped (try the next candidate).
    Returns True if the squad changed."""
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
        old_id = cheapest["player_id"]  # capture before the in-place update
        fee = tctx.sell_fee.get(old_id, 0) if tctx is not None else 0
        if total_cost - cheapest["now_cost"] + c["now_cost"] + fee > BUDGET:
            # FIX T1: a fee-bearing sale frees less money — budget check uses it.
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


def dedupe_profiles(results: dict[str, SolvedLineup],
                    tctx: _TransferCtx | None = None) -> dict[str, SolvedLineup]:
    """Convergence guard (T1.7.5, formalized in T4.1): if a profile equals the
    first one, try (1) 2nd-best-EP captain, (2) swap in a <25%-owned player;
    if still identical, label it a variant of the first profile.

    When ``tctx`` is given, attempt (2) must also respect the free-transfer cap
    (a low-ownership swap that would push transfers over the cap is skipped);
    ``tctx=None`` keeps the legacy unconstrained behavior."""
    ordered = list(results.items())
    for i in range(1, len(ordered)):
        name, s = ordered[i]
        prev_name, prev = ordered[0]
        if lineup_key(s) == lineup_key(prev):
            # FIX T4: judge the swap by THIS profile's own transfer context —
            # the differential EP floor can exclude current players, giving a
            # different cap than the shared ctx generate() passes in.
            effective = s.tctx if getattr(s, "tctx", None) is not None else tctx
            changed = _try_second_captain(s) or _try_low_own_swap(s, effective)
            if changed:
                _rescore(s)
            if lineup_key(s) == lineup_key(prev):
                s.variant_of = prev_name
    return results