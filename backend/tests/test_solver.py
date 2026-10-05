"""Solver tests: structural validity + convergence guard (PLAN-2 T1.16, T4.1)."""
import pytest

from app.optimizer.rules import validate_lineup
from app.optimizer.solver import (
    PROFILES,
    SolvedLineup,
    SolveParams,
    _try_low_own_swap,
    _try_second_captain,
    dedupe_profiles,
    lineup_key,
    solve,
)


def test_all_profiles_return_valid_lineups(db_path, cfg):
    for profile in PROFILES:
        s = solve(SolveParams(
            current_squad=[], bank=5, chips={}, target_gw=6, profile=profile, cfg=cfg,
        ))
        check = validate_lineup(s.squad, bank=5, strict=True)
        assert check.valid, (profile, check.errors)
        assert len(s.squad) == 15
        assert len(s.xi) == 11
        assert len(s.bench) == 4
        # bench 1 = strongest sub: bench order must be EP-descending
        from app.db import query
        eps = {r["id"]: r["ep_next"] for r in query("SELECT id, ep_next FROM players")}
        bench_eps = [eps[i] for i in s.bench]
        assert bench_eps == sorted(bench_eps, reverse=True)
        assert s.captain in s.xi
        assert s.vice_captain in s.xi
        assert s.captain != s.vice_captain
        total = sum(p["now_cost"] for p in s.squad)
        assert total <= 10000
        clubs = {}
        for p in s.squad:
            clubs[p["team"]] = clubs.get(p["team"], 0) + 1
        assert max(clubs.values()) <= 3
        # Bug 3: objective now includes the transfer penalty. With an empty
        # current squad every candidate pays a constant 4*(30-5) = 100, so the
        # meaningful check is that the base value is positive.
        assert s.objective > -100
        assert s.projected_points["adjusted"] > 0
        assert s.variant_of is None


def test_solver_deterministic_with_seed(db_path, cfg):
    a = solve(SolveParams(current_squad=[], bank=5, chips={}, target_gw=6,
                          profile="max_ep", cfg=cfg, rng_seed=42))
    b = solve(SolveParams(current_squad=[], bank=5, chips={}, target_gw=6,
                          profile="max_ep", cfg=cfg, rng_seed=42))
    assert lineup_key(a) == lineup_key(b)
    assert a.objective == b.objective


def test_differential_profile_uses_floor(db_path, cfg):
    s = solve(SolveParams(current_squad=[], bank=5, chips={}, target_gw=6,
                          profile="differential", cfg=cfg))
    check = validate_lineup(s.squad, bank=5, strict=True)
    assert check.valid, check.errors


def test_dedupe_marks_converged_profile(db_path, cfg):
    # M1: no signals → 'safe' (ep*rel, rel=1) converges with 'max_ep' (ep)
    results = {}
    for profile in ("max_ep", "safe"):
        results[profile] = solve(SolveParams(
            current_squad=[], bank=5, chips={}, target_gw=6, profile=profile, cfg=cfg,
        ))
    out = dedupe_profiles(results)
    if lineup_key(results["safe"]) == lineup_key(results["max_ep"]):
        assert out["safe"].variant_of == "max_ep"
    else:
        assert out["safe"].variant_of is None


def test_solver_excludes_unavailable(db_path, cfg):
    from app.db import execute
    execute("UPDATE players SET status = 'u' WHERE id = 23")  # top EP fwd
    s = solve(SolveParams(current_squad=[], bank=5, chips={}, target_gw=6,
                          profile="max_ep", cfg=cfg))
    assert 23 not in {p["player_id"] for p in s.squad}


def test_solver_fails_on_empty_universe(db_path, cfg):
    from app.db import execute
    execute("UPDATE players SET can_select = 0")
    with pytest.raises(ValueError):
        solve(SolveParams(current_squad=[], bank=5, chips={}, target_gw=6,
                          profile="max_ep", cfg=cfg))


def test_universe_excludes_chance_zero_when_active(db_path, cfg):
    """T2.10: chance_of_playing_next_round = 0 is excluded only when
    optimizer.availability.active is True."""
    from app.db import execute
    from app.optimizer.solver import build_universe

    execute("UPDATE players SET chance_of_playing_next_round = 0 WHERE id = 23")
    ids_inactive = {p["id"] for p in build_universe(6, cfg)}  # cfg fixture: active=False
    assert 23 in ids_inactive

    cfg.optimizer.availability.active = True
    ids_active = {p["id"] for p in build_universe(6, cfg)}
    assert 23 not in ids_active


def test_signal_applied_note_in_rationale(db_path, cfg):
    """T2.10: active signals on an XI player produce a 'Signal applied' note.

    Player 1 (top-EP GK) is always in the XI, so the note is guaranteed.
    """
    from app.signals import store

    # conf 0.2: drops EP 5.87 vs the other GK's 5.65 → player 1 stays in the XI
    sig = {
        "player_id": 1, "category": "injury", "sentiment": "negative",
        "confidence": 0.2, "summary": "Goal One hamstring knock, to be assessed.",
        "source": "bbc:x1", "url": "http://x", "published_at": "2026-09-19T12:00:00Z",
        "raw_item_id": None, "model": "rules",
    }
    store.save_signals([dict(sig)])
    s = solve(SolveParams(current_squad=[], bank=5, chips={}, target_gw=6,
                          profile="max_ep", cfg=cfg,
                          signals_by_player={1: [dict(sig)]}))
    assert 1 in s.xi
    assert any(n.startswith("Signal applied: Goal One") for n in s.notes)


# ---------------------------------------------------------------------------
# T4.1 — differential tuning: floor, convergence attempts, rationale lines
# ---------------------------------------------------------------------------

def test_differential_floor_excludes_low_ep(db_path, cfg):
    """The EP floor keeps only players at/above floor × max EP."""
    from app.optimizer.solver import _apply_ep_floor, build_universe

    uni = build_universe(6, cfg)
    for p in uni:
        p["ep"] = float(p["ep_next"])  # stand-in for ep_final
    cfg.optimizer.differential_ep_floor = 0.6
    filtered = _apply_ep_floor(uni, cfg)
    max_ep = max(p["ep"] for p in uni)
    assert len(filtered) < len(uni)
    assert all(p["ep"] >= 0.6 * max_ep for p in filtered)
    kept = {p["id"] for p in filtered}
    assert all(p["id"] in kept for p in uni if p["ep"] >= 0.6 * max_ep)
    assert _apply_ep_floor([], cfg) == []


def test_differential_rationale_lines_present(db_path, cfg):
    """Every XI player owned by <10% gets a per-player differential note."""
    from app.db import execute

    execute("UPDATE players SET selected_by_percent = 1.0")
    s = solve(SolveParams(current_squad=[], bank=5, chips={}, target_gw=6,
                          profile="differential", cfg=cfg))
    lines = [n for n in s.notes if n.startswith("Differential: ") and "owned by" in n]
    assert lines, "expected per-player differential rationale lines"
    assert all("(top-50 EP)" in n for n in lines)
    assert len(lines) <= 5


def _mk_lineup(squad, universe):
    """Hand-built SolvedLineup from a valid_squad() entry list (no solve needed)."""
    xi = [e["player_id"] for e in squad if e["role"] == "starter"]
    return SolvedLineup(
        squad=[dict(e) for e in squad],
        xi=xi,
        captain=next(e["player_id"] for e in squad if e["is_captain"]),
        vice_captain=next(e["player_id"] for e in squad if e["is_vice_captain"]),
        bench=[e["player_id"] for e in sorted(
            (e for e in squad if e["role"] == "bench"), key=lambda e: e["bench_order"] or 0)],
        objective=1.0,
        projected_points={"adjusted": 1.0, "baseline": 1.0},
        universe=universe,
    )


def _converged_pair(db_path, cfg, eps):
    """Two key-identical lineups (same squad/XI/captain) over a shared universe."""
    from conftest import valid_squad

    squad = valid_squad()
    universe = [
        {"id": e["player_id"], "ep": eps.get(e["player_id"], 8.0),
         "selected_by_percent": 99.0, "element_type": e["element_type"],
         "team": e["team"], "now_cost": e["now_cost"], "web_name": e["web_name"],
         "status": "a", "can_select": 1}
        for e in squad
    ]
    a = _mk_lineup(squad, universe)
    b = _mk_lineup(squad, universe)
    assert lineup_key(a) == lineup_key(b)
    return a, b


def test_convergence_attempt_second_captain(db_path, cfg):
    """Attempt (1): captain moves to the 2nd-best-EP starter; no variant label."""
    # captain (id 1) is the top EP → 2nd-best (id 3) becomes captain
    a, b = _converged_pair(db_path, cfg, {1: 10.0, 3: 9.0})
    out = dedupe_profiles({"max_ep": a, "safe": b})
    assert out["safe"].variant_of is None
    assert out["safe"].captain == 3
    assert out["safe"].vice_captain != 3
    assert out["safe"].vice_captain in set(out["safe"].xi)
    assert lineup_key(out["safe"]) != lineup_key(out["max_ep"])


def test_convergence_variant_labeled_when_attempts_exhausted(db_path, cfg):
    """Both attempts fail (captain already 2nd-best EP, no <25% candidates)
    → the profile is labeled a variant of the first one."""
    # captain (id 1) is 2nd-best (id 3 is top) → attempt (1) is a no-op;
    # all universe players are 99%-owned → attempt (2) has no candidates.
    a, b = _converged_pair(db_path, cfg, {3: 10.0, 1: 9.0})
    out = dedupe_profiles({"max_ep": a, "safe": b})
    assert out["safe"].variant_of == "max_ep"
    assert lineup_key(out["safe"]) == lineup_key(out["max_ep"])


def test_convergence_attempt_low_own_swap(db_path, cfg):
    """Attempt (2): swaps in the best-EP player owned by <25% when the captain
    attempt is a no-op; budget and club cap must hold."""
    from conftest import valid_squad

    squad = valid_squad()
    universe = [
        {"id": e["player_id"], "ep": {3: 10.0, 1: 9.0}.get(e["player_id"], 8.0),
         "selected_by_percent": 99.0, "element_type": e["element_type"],
         "team": e["team"], "now_cost": e["now_cost"], "web_name": e["web_name"],
         "status": "a", "can_select": 1}
        for e in squad
    ]
    # new team 6, cheap FWD, low ownership, best EP in the universe
    universe.append({"id": 30, "ep": 50.0, "selected_by_percent": 5.0,
                     "element_type": 4, "team": 6, "now_cost": 40,
                     "web_name": "Diff Fwd", "status": "a", "can_select": 1})
    a = _mk_lineup(squad, universe)
    b = _mk_lineup(squad, universe)
    assert lineup_key(a) == lineup_key(b)
    out = dedupe_profiles({"max_ep": a, "safe": b})
    ids = {e["player_id"] for e in out["safe"].squad}
    assert 30 in ids and 19 not in ids  # cheapest FWD (id 19) replaced
    assert 30 in out["safe"].xi
    assert out["safe"].variant_of is None
    assert sum(e["now_cost"] for e in out["safe"].squad) <= 1000

# ---------------------------------------------------------------------------
# Bug 3 — transfer-aware objective + current-squad seed (FIX.MD 2.7)
# ---------------------------------------------------------------------------

VALID_IDS = (1, 2, 3, 5, 7, 9, 8, 13, 15, 17, 18, 16, 19, 23, 24)  # conftest.valid_squad()


def _current_squad(ids=VALID_IDS):
    from app.db import query
    rows = {r["id"]: r for r in query("SELECT id, web_name, now_cost FROM players")}
    return [{"player_id": i, "web_name": rows[i]["web_name"],
             "now_cost": rows[i]["now_cost"], "bought_cost": rows[i]["now_cost"]}
            for i in ids]


def test_transfer_ctx_penalty_math():
    """Regression for the bad_suggestion.png scenario: 13 in / 13 out (13
    transfers — a swap is one transfer), bank 3, no chip → 40 penalty points
    in the objective."""
    from app.optimizer.solver import _TransferCtx
    cur = frozenset(range(1, 16))
    tctx = _TransferCtx(cur_ids=cur, bank=3, chip_covers=False, profile="safe")
    kept_all = [{"id": i} for i in range(1, 16)]
    assert tctx.transfers(kept_all) == 0
    assert tctx.penalty_points(kept_all) == 0.0
    # FIX.MD deviation: the spec's `range(100, 115)` is 15 brand-new players
    # (15 transfers); a 13-swap scenario keeps 2 of the current players.
    swap13 = [{"id": i} for i in (1, 2)] + [{"id": i} for i in range(100, 113)]
    assert tctx.transfers(swap13) == 13
    assert tctx.penalty_points(swap13) == 40.0  # 4 × (13 − 3)
    tctx_chip = _TransferCtx(cur_ids=cur, bank=3, chip_covers=True, profile="safe")
    assert tctx_chip.penalty_points(swap13) == 0.0


def test_solver_keeps_squad_when_swaps_not_worth_penalty(db_path, cfg):
    """bank=1: a single swap is now legal (1 transfer, no penalty). The
    conftest squad's only positive-EP swap is 24→25 (+1.0); with 25
    unavailable no swap improves the objective → 0 transfers against the
    current squad (blocked by objective economics, not the cap)."""
    from app.db import execute
    execute("UPDATE players SET status = 'u' WHERE id = 25")
    s = solve(SolveParams(current_squad=_current_squad(), bank=1, chips={},
                          target_gw=6, profile="max_ep", cfg=cfg))
    kept = {p["player_id"] for p in s.squad} & set(VALID_IDS)
    assert len(kept) == 15


def _insert_edge_keeper():
    """Player 99: t1 GK, ep_next 11.0 → replaces player 1 (t1 GK, ep_next 8.0),
    ≈ +2.1 EP — the bigger of the two positive swaps in this universe. A GK is
    the most findable swap for the hill climber (only 2 GK candidates).

    The smaller positive swap is conftest player 25 ('Saka Junior', t5 FWD,
    ep_next 12.5) replacing player 24 (t5 FWD, ep_next 11.5), ≈ +1.0 EP.
    """
    from app.db import execute
    execute("INSERT INTO players (id, web_name, element_type, team, now_cost, ep_next, "
            "selected_by_percent, status, can_select, removed, chance_of_playing_next_round, "
            "form, fetched_at) VALUES (99, 'Edge Keeper', 1, 1, 45, 11.0, 10.0, 'a', 1, 0, 100, "
            "5.0, '2026-09-19T12:00:00Z')")


def test_penalty_blocks_small_gain_swap(db_path, cfg):
    """An over-bank swap is not taken, even with a positive gain (bank=1, no
    chip).

    (Old semantics: the swap was 2 transfers vs bank 1 → a 4-pt penalty blocked
    a +2.1 gain. New semantics: the first swap is free, so the blocking case is
    the SECOND positive swap — transfer #2 > bank 1, rejected by the hard cap
    before any penalty could be charged.)

    FIX.MD deviation: the spec inserts one player (99) as a team-2 MID, but the
    test current squad has every club at the 3-player cap, so a t2 MID could
    only replace player 13 (ep_final 8.06 > 99's 7.15) — illegal club-cap-wise
    and negative in EP. Redesign: 99 (t1 GK, +2.1) vs conftest player 25 (t5
    FWD, +1.0); the solver must take the best free swap and block the second.
    """
    _insert_edge_keeper()
    s = solve(SolveParams(current_squad=_current_squad(), bank=1, chips={},
                          target_gw=6, profile="max_ep", cfg=cfg))
    ids = {p["player_id"] for p in s.squad}
    assert 99 in ids     # best free swap taken (1 transfer = bank)
    assert 25 not in ids  # would be transfer #2 > bank 1 → blocked by the cap


def test_chip_covers_allows_small_gain_swap(db_path, cfg):
    """Same setup with a playable wildcard → the cap is lifted and both
    positive swaps are taken (penalty-free).

    FIX.MD deviation: same redesign as test_penalty_blocks_small_gain_swap.
    """
    _insert_edge_keeper()
    s = solve(SolveParams(current_squad=_current_squad(), bank=1,
                          chips={"wildcard": 1}, target_gw=6, profile="max_ep", cfg=cfg))
    ids = {p["player_id"] for p in s.squad}
    assert 99 in ids and 25 in ids


# ---------------------------------------------------------------------------
# FIX.MD — hard free-transfer cap on all profiles (D1/D2)
# ---------------------------------------------------------------------------

def test_all_profiles_within_bank(db_path, cfg):
    """D1: every profile stays within the free-transfer bank (no chip)."""
    from app.optimizer.transfers import compute_diff
    cur = _current_squad()
    for profile in PROFILES:
        s = solve(SolveParams(current_squad=cur, bank=3, chips={},
                              target_gw=6, profile=profile, cfg=cfg))
        diff = compute_diff(cur, s.squad, 3)
        # a swap is one transfer: bank 3 → ≤3 swaps → ≤6 movements
        assert len(diff["transfers_in"]) + len(diff["transfers_out"]) <= 6, profile
    for profile in PROFILES:
        s = solve(SolveParams(current_squad=cur, bank=1, chips={},
                              target_gw=6, profile=profile, cfg=cfg))
        diff = compute_diff(cur, s.squad, 1)
        # a single swap is now legal at bank 1: at most 1 swap, no penalty
        assert diff["penalty_points"] == 0, profile
        assert len(diff["transfers_in"]) + len(diff["transfers_out"]) <= 2, profile


def test_forced_transfers_only_overrun(db_path, cfg):
    """D2: unavailable current players force exactly their replacements —
    no voluntary extras beyond the forced minimum."""
    from app.db import execute
    from app.optimizer.transfers import compute_diff
    execute("UPDATE players SET status = 'u' WHERE id IN (23, 24)")
    s = solve(SolveParams(current_squad=_current_squad(), bank=1, chips={},
                          target_gw=6, profile="max_ep", cfg=cfg))
    diff = compute_diff(_current_squad(), s.squad, 1)
    n = len(diff["transfers_in"]) + len(diff["transfers_out"])
    assert n == 4  # 2 forced swaps = 2 transfers; cap = max(1, 2) = 2
    # 2 transfers over bank 1 → 4 × (2 − 1) = 4 pts
    assert diff["penalty_points"] == 4


def test_no_voluntary_overrun_at_cap(db_path, cfg):
    """A tempting high-EP insert must not push transfers over the bank."""
    from app.db import execute
    from app.optimizer.transfers import compute_diff
    execute("INSERT INTO players (id, web_name, element_type, team, now_cost, ep_next, "
            "selected_by_percent, status, can_select, removed, chance_of_playing_next_round, "
            "form, fetched_at) VALUES (99, 'Edge Keeper', 1, 1, 45, 11.0, 10.0, 'a', 1, 0, 100, "
            "5.0, '2026-09-19T12:00:00Z')")
    for profile in PROFILES:
        s = solve(SolveParams(current_squad=_current_squad(), bank=3, chips={},
                              target_gw=6, profile=profile, cfg=cfg))
        diff = compute_diff(_current_squad(), s.squad, 3)
        # bank 3 → ≤3 swaps → ≤6 movements
        assert len(diff["transfers_in"]) + len(diff["transfers_out"]) <= 6, profile


def test_dedupe_swap_respects_cap(db_path, cfg):
    """The convergence low-own swap must respect the free-transfer cap: the
    baseline already sits at the cap (1 non-current player), so the swap would
    be transfer #2 > 1 → rejected and the profile is labeled a variant."""
    from conftest import valid_squad
    from app.optimizer.solver import _TransferCtx

    squad = valid_squad()
    universe = [
        {"id": e["player_id"], "ep": {3: 10.0, 1: 9.0}.get(e["player_id"], 8.0),
         "selected_by_percent": 99.0, "element_type": e["element_type"],
         "team": e["team"], "now_cost": e["now_cost"], "web_name": e["web_name"],
         "status": "a", "can_select": 1}
        for e in squad
    ]
    # <25%-owned, best-EP candidate — the unconstrained swap would take it
    universe.append({"id": 30, "ep": 50.0, "selected_by_percent": 5.0,
                     "element_type": 4, "team": 6, "now_cost": 40,
                     "web_name": "Diff Fwd", "status": "a", "can_select": 1})
    a = _mk_lineup(squad, universe)
    b = _mk_lineup(squad, universe)
    assert lineup_key(a) == lineup_key(b)
    # one squad player is NOT current → baseline is already at the cap
    # (1 transfer, bank=1); the low-own swap would be transfer #2 > 1
    squad_ids = frozenset(e["player_id"] for e in squad)
    tctx = _TransferCtx(cur_ids=squad_ids - {squad[0]["player_id"]},
                        bank=1, chip_covers=False, profile="safe")
    out = dedupe_profiles({"max_ep": a, "safe": b}, tctx=tctx)
    ids = {e["player_id"] for e in out["safe"].squad}
    assert 30 not in ids
    assert out["safe"].variant_of == "max_ep"


def test_safe_profile_within_bank_with_fh_ban(db_path, cfg):
    """End-to-end screenshot repro: bank=3, freehit in hand but already played
    in GW5 (the previous GW) → the ban keeps chip_covers False and every
    profile within the bank."""
    from app.db import execute
    from app.optimizer.transfers import chip_covers_transfers, compute_diff
    execute("INSERT INTO chip_plays_log (lineup_id, gw, chip, played_at) "
            "VALUES (NULL, 5, 'freehit', '2026-09-19T12:00:00Z')")
    chips = {"freehit": 1}
    assert chip_covers_transfers(chips, 6) is False
    cur = _current_squad()
    for profile in PROFILES:
        s = solve(SolveParams(current_squad=cur, bank=3, chips=chips,
                              target_gw=6, profile=profile, cfg=cfg))
        diff = compute_diff(cur, s.squad, 3, chip_covers=False)
        assert len(diff["transfers_in"]) + len(diff["transfers_out"]) <= 6, profile
        assert diff["penalty_points"] == 0, profile


def test_forced_overrun_shows_penalty(db_path, cfg):
    """bank=1, two current players unavailable → exactly 2 forced swaps
    (in+out == 4), penalty 4 × (2 − 1) = 4, and the API rationale note
    attributes the penalty to the forced replacements."""
    from fastapi.testclient import TestClient
    from app.db import execute
    from app.main import create_app
    from app.optimizer.transfers import compute_diff
    execute("UPDATE players SET status = 'u' WHERE id IN (23, 24)")
    cur = _current_squad()
    s = solve(SolveParams(current_squad=cur, bank=1, chips={},
                          target_gw=6, profile="max_ep", cfg=cfg))
    diff = compute_diff(cur, s.squad, 1)
    assert len(diff["transfers_in"]) + len(diff["transfers_out"]) == 4
    assert diff["penalty_points"] == 4

    # API level: the same scenario's rationale note must mention the forced
    # replacements (mirrors suggestions.py Step 8 text)
    body = {
        "name": "Forced XI",
        "transfer_bank": 1,
        "chips": {"wildcard": 0, "freehit": 0, "bboost": 2, "triple_captain": 2},
        "players": [
            {"player_id": 1, "role": "starter", "is_captain": True},
            {"player_id": 2, "role": "bench", "bench_order": 1},
            {"player_id": 3, "role": "starter"},
            {"player_id": 5, "role": "starter"},
            {"player_id": 7, "role": "starter"},
            {"player_id": 9, "role": "starter"},
            {"player_id": 8, "role": "bench", "bench_order": 2},
            {"player_id": 13, "role": "starter", "is_vice_captain": True},
            {"player_id": 15, "role": "starter"},
            {"player_id": 17, "role": "starter"},
            {"player_id": 18, "role": "bench", "bench_order": 3},
            {"player_id": 16, "role": "bench", "bench_order": 4},
            {"player_id": 19, "role": "starter"},
            {"player_id": 23, "role": "starter"},
            {"player_id": 24, "role": "starter"},
        ],
    }
    client = TestClient(create_app())
    lid = client.post("/api/lineups", json=body).json()["id"]
    r = client.post("/api/suggestions/generate", json={"lineup_id": lid})
    assert r.status_code == 200, r.text
    sug = next(x for x in r.json()["suggestions"] if x["profile"] == "max_ep")
    assert any(n.startswith("2 forced replacements exceed your bank of 1")
               for n in sug["rationale"]["notes"])


# ---------------------------------------------------------------------------
# FIX.MD Part 3 — T1 fee-aware affordability, T4 per-profile dedupe ctx, T5 moves
# ---------------------------------------------------------------------------

# id: (element_type, team, cost) — 15-man squad, 2/5/5/3, all costs in [62, 74],
# distinct clubs. Total = 990, so with in-cost 60 every fee-blind route fits
# (990 + 60 − c ≤ 1000 for all c) and every fee (bought 25 → ≈ half the rise)
# pushes fee-aware routes over (990 + 60 − c + fee(c) > 1000 for all c).
_FEE_SQUAD = {
    101: (1, 50, 62), 102: (1, 51, 63),
    111: (2, 52, 65), 112: (2, 53, 65), 113: (2, 54, 65), 114: (2, 55, 65), 115: (2, 56, 65),
    121: (3, 57, 66), 122: (3, 58, 65), 123: (3, 59, 65), 124: (3, 60, 65), 125: (3, 61, 65),
    131: (4, 62, 70), 132: (4, 63, 70), 133: (4, 64, 74),
}
# Tempting same-EP swap-ins, one per position (ep_next 50 vs squad's 5), cost 60.
_FEE_EDGE = [(200, 1, 96, 60), (201, 2, 97, 60), (202, 3, 98, 60), (203, 4, 99, 60)]


def _insert_fee_players():
    from app.db import execute
    # conftest players are CHEAP (~40) — as in-candidates they would free money
    # and fund the Edge swap after one money-freeing swap. Make them
    # unavailable so the universe is exactly the fee squad + the 4 edges.
    execute("UPDATE players SET status = 'u' WHERE id < 100")
    rows = [(pid, f"Fee{pid}", et, team, cost, 5.0)
            for pid, (et, team, cost) in _FEE_SQUAD.items()]
    rows += [(pid, f"Edge{pid}", et, team, cost, 50.0) for pid, et, team, cost in _FEE_EDGE]
    for pid, name, et, team, cost, ep in rows:
        execute(
            "INSERT INTO players (id, web_name, element_type, team, now_cost, ep_next, "
            "selected_by_percent, status, can_select, removed, chance_of_playing_next_round, "
            "form, fetched_at) VALUES (?,?,?,?,?,?,10.0,'a',1,0,100,5.0,'2026-09-19T12:00:00Z')",
            (pid, name, et, team, cost, ep),
        )


def _fee_squad(bought: int | None) -> list[dict]:
    """current_squad entries {player_id, web_name, now_cost, bought_cost};
    bought=None → bought at the current price (fee-free sales)."""
    from app.db import query
    idlist = ",".join(str(i) for i in _FEE_SQUAD)
    rows = {r["id"]: r for r in query(
        f"SELECT id, web_name, now_cost FROM players WHERE id IN ({idlist})")}
    out = []
    for pid, (_et, _team, _cost) in _FEE_SQUAD.items():
        bc = rows[pid]["now_cost"] if bought is None else bought
        out.append({"player_id": pid, "web_name": rows[pid]["web_name"],
                    "now_cost": rows[pid]["now_cost"], "bought_cost": bc})
    return out


def test_solver_fee_aware_affordability(db_path, cfg):
    """FIX T1 (§25.1): a fee-bearing sale frees the SELL value, not the current
    price — with money exactly borderline the tempting swap is rejected when
    the sale carries a fee and accepted when the same-EP sale is fee-free.

    Design note (deviation from the spec's single fee-bearing player): a
    same-position route that frees MORE money stays affordable, so a lone
    fee-bearing player cannot make the swap uniquely blocked. The fee is
    therefore applied to every squad player (bought_cost 25 → fee ≈ half the
    price rise), making ALL routes fee-blocked; the fee-free twin
    (bought_cost = now_cost) then accepts the swap.
    """
    _insert_fee_players()
    edge_ids = {200, 201, 202, 203}

    s_fee = solve(SolveParams(current_squad=_fee_squad(bought=25), bank=1, chips={},
                              target_gw=6, profile="max_ep", cfg=cfg))
    fee_ids = {e["player_id"] for e in s_fee.squad}
    assert fee_ids & edge_ids == set()  # fee-bearing sale cannot fund the swap

    s_free = solve(SolveParams(current_squad=_fee_squad(bought=None), bank=1, chips={},
                               target_gw=6, profile="max_ep", cfg=cfg))
    free_ids = {e["player_id"] for e in s_free.squad}
    assert free_ids & edge_ids          # fee-free sale of the same EP funds it


def test_dedupe_judges_swap_by_profile_own_ctx():
    """FIX T4 (§25.4): dedupe's low-ownership swap must be judged by EACH
    profile's own transfer context — the differential EP floor can exclude
    current players, giving a different cap than the shared ctx generate()
    passes in. Here the shared ctx (cap 2) would allow the swap; the
    differential profile's own ctx (cap 1) must block it."""
    import copy

    from app.optimizer.solver import SolvedLineup, _TransferCtx

    et = {1: 1, 2: 1, 3: 2, 4: 2, 5: 2, 6: 2, 7: 2, 8: 3, 9: 3, 10: 3, 11: 3, 12: 3,
          13: 4, 14: 4, 15: 4}
    universe = []
    squad = []
    for i in range(1, 16):
        universe.append({"id": i, "web_name": f"P{i}", "element_type": et[i],
                         "team": 100 + i, "now_cost": 50,
                         "ep": 9.0 if i == 1 else 5.0, "selected_by_percent": 50.0})
        squad.append({"player_id": i, "web_name": f"P{i}", "element_type": et[i],
                      "team": 100 + i, "now_cost": 50})
    # low-ownership candidate: MID, own club, huge EP, 10% owned
    universe.append({"id": 16, "web_name": "LowOwn", "element_type": 3, "team": 999,
                     "now_cost": 50, "ep": 99.0, "selected_by_percent": 10.0})

    cur_ids = frozenset(range(1, 15))   # player 15 already replaced → transfers = 1
    # Hand-built caps that exhibit the mechanism (§25.4: "two key-identical
    # lineups whose tctx caps differ"): the shared ctx (what generate() would
    # pass — an arbitrary profile's) is the LOOSER cap 2; the differential
    # profile's own ctx is the tighter cap 1. The low-own swap costs transfer
    # #2, so it must pass under the shared ctx and fail under the own ctx.
    shared = _TransferCtx(cur_ids=cur_ids, bank=2, chip_covers=False,
                          profile="max_ep", forced_transfers=1)     # cap 2
    own = _TransferCtx(cur_ids=cur_ids, bank=1, chip_covers=False,
                       profile="differential", forced_transfers=0)  # cap 1

    def build(tctx):
        return SolvedLineup(
            squad=copy.deepcopy(squad), xi=list(range(1, 12)),
            captain=2, vice_captain=1, bench=[12, 13, 14, 15],
            objective=0.0, projected_points={}, universe=universe,
            scoring_ctx=None, tctx=tctx,
        )

    s_a, s_b = build(shared), build(own)
    assert lineup_key(s_a) == lineup_key(s_b)
    # sanity: under the shared cap-2 ctx the swap WOULD go through
    probe = build(shared)
    assert _try_low_own_swap(probe, shared) is True

    out = dedupe_profiles({"max_ep": s_a, "differential": s_b}, tctx=shared)
    # the differential profile's own cap 1 blocks the swap → labelled a variant
    assert out["differential"].variant_of == "max_ep"
    assert 16 not in {e["player_id"] for e in out["differential"].squad}
    assert out["max_ep"].variant_of is None


def test_hill_climb_moves_drop_bench_reorder():
    """FIX T5 (§25.5): bench_reorder was a no-op (as_lineup re-derives bench
    order by EP-descending) — it must be gone from the move list."""
    from app.optimizer.solver import _HILL_CLIMB_MOVES
    assert "bench_reorder" not in _HILL_CLIMB_MOVES
    assert set(_HILL_CLIMB_MOVES) == {"squad_swap", "xi_bench", "captain", "vc"}
    assert _HILL_CLIMB_MOVES.count("squad_swap") == 2  # extra weight preserved


def test_as_lineup_emits_full_display_fields(db_path, cfg):
    """A23: a suggested squad carries the display fields the frontend type
    promises (team_name / ep_next / chance_of_playing_next_round) and does NOT
    fabricate a bought_cost — purchase prices only exist for stored lineups."""
    from app import db as dbmod
    from app.db import query

    # Give one team a name so the join is exercised with a real value.
    conn = dbmod.get_conn(db_path)
    conn.execute("UPDATE teams SET name = 'Alpha FC', short_name = 'ALP' WHERE id = 1")
    conn.commit()
    conn.close()

    s = solve(SolveParams(current_squad=[], bank=5, chips={}, target_gw=6,
                          profile="max_ep", cfg=cfg))
    by_id = {r["id"]: r for r in query(
        "SELECT id, ep_next, chance_of_playing_next_round FROM players")}
    # N3 (rev 2): the full club name, matching lineups._load_lineup /
    # signals.names — not short_name.
    team_names = {r["id"]: r["name"] for r in query("SELECT id, name FROM teams")}
    for p in s.squad:
        src = by_id[p["player_id"]]
        assert p["ep_next"] == src["ep_next"]
        assert p["chance_of_playing_next_round"] == src["chance_of_playing_next_round"]
        assert p["team_name"] == team_names.get(p["team"])
        assert "bought_cost" not in p
    # the team-1 players must show the real club name, not null
    assert any(p["team_name"] == "Alpha FC" for p in s.squad if p["team"] == 1)