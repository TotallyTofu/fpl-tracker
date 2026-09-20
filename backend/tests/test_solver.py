"""Solver tests: structural validity + convergence guard (PLAN-2 T1.16)."""
import pytest

from app.optimizer.rules import validate_lineup
from app.optimizer.solver import PROFILES, SolveParams, dedupe_profiles, lineup_key, solve


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