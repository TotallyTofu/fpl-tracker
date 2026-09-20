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
        assert s.objective > 0
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