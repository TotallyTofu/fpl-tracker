"""Transfer math + chip advice tests (PLAN-2 T1.16, M4 T4.2)."""
from types import SimpleNamespace

from app.db import execute
from app.optimizer.transfers import chip_advice_v1, chip_advice_v2, compute_diff, sell_value


def _solved(cap_ep=9.0, xi_ep=8.0, bench_eps=(6.0, 6.0, 6.0, 6.0)):
    """Mock SolvedLineup: captain id 100, XI 100..110, bench 201..204."""
    universe = [{"id": 100, "ep": cap_ep}]
    xi = list(range(100, 111))
    for i in xi[1:]:
        universe.append({"id": i, "ep": xi_ep})
    bench = [201, 202, 203, 204]
    for i, e in zip(bench, bench_eps):
        universe.append({"id": i, "ep": e})
    return SimpleNamespace(universe=universe, xi=xi, captain=100, bench=bench)


def _big_diff(bank=1):
    cur = [{"player_id": i, "web_name": f"P{i}", "now_cost": 500, "bought_cost": 500} for i in range(1, 4)]
    new = [{"player_id": i, "web_name": f"N{i}", "now_cost": 500} for i in range(10, 13)]
    return compute_diff(cur, new, bank=bank)


def test_sell_value_half_increase():
    assert sell_value(750, 780) == 765  # +30 → +15
    assert sell_value(750, 770) == 760  # +20 → +10
    assert sell_value(750, 700) == 700  # fall → current
    assert sell_value(750, 750) == 750
    assert sell_value(None, 750) == 750  # no purchase price → current


def test_compute_diff_swap_is_two_transfers():
    cur = [
        {"player_id": 1, "web_name": "A", "now_cost": 50, "bought_cost": 50},
        {"player_id": 2, "web_name": "B", "now_cost": 60, "bought_cost": 58},
    ]
    new = [
        {"player_id": 1, "web_name": "A", "now_cost": 50},
        {"player_id": 3, "web_name": "C", "now_cost": 55},
    ]
    d = compute_diff(cur, new, bank=2)
    assert len(d["transfers_in"]) == 1
    assert len(d["transfers_out"]) == 1
    assert d["transfers_out"][0]["player_id"] == 2
    assert d["transfers_out"][0]["sell_value"] == 59  # 58 + (60-58)//2
    assert d["cost_delta"] == 55 - 59
    assert d["free_transfers_used"] == 2
    assert d["bank_after"] == 0
    assert d["penalty_points"] == 0


def test_compute_diff_penalty_over_bank():
    cur = [{"player_id": i, "web_name": f"P{i}", "now_cost": 500, "bought_cost": 500} for i in range(1, 4)]
    new = [{"player_id": i, "web_name": f"N{i}", "now_cost": 500} for i in range(10, 13)]
    d = compute_diff(cur, new, bank=2)
    # 3 in + 3 out = 6 transfers, bank 2 → 4 over → 16 pts
    assert d["penalty_points"] == 16
    assert d["bank_after"] == 0
    assert d["free_transfers_used"] == 2


def test_compute_diff_no_change():
    cur = [{"player_id": 1, "web_name": "A", "now_cost": 500, "bought_cost": 500}]
    d = compute_diff(cur, [{"player_id": 1, "web_name": "A", "now_cost": 500}], bank=5)
    assert d["transfers_in"] == []
    assert d["transfers_out"] == []
    assert d["penalty_points"] == 0
    assert d["bank_after"] == 5


def test_chip_advice_wildcard_used_when_over_bank(db_path, cfg):
    cur = [{"player_id": i, "web_name": f"P{i}", "now_cost": 500, "bought_cost": 500} for i in range(1, 4)]
    new = [{"player_id": i, "web_name": f"N{i}", "now_cost": 500} for i in range(10, 13)]
    d = compute_diff(cur, new, bank=1)
    advice = chip_advice_v1(d, {"wildcard": 1, "freehit": 1, "bboost": 1, "triple_captain": 1},
                            current_gw=5, next_gw=6, cfg=cfg)
    by_chip = {a["chip"]: a for a in advice}
    assert by_chip["wildcard"]["recommendation"] == "use"
    assert by_chip["freehit"]["recommendation"] == "consider"
    assert by_chip["triple_captain"]["recommendation"] == "consider"
    assert by_chip["bboost"]["recommendation"] == "consider"


def test_chip_advice_skip_when_no_window(db_path, cfg):
    d = compute_diff([], [], bank=5)
    advice = chip_advice_v1(d, {"wildcard": 0, "freehit": 0, "bboost": 0, "triple_captain": 0},
                            current_gw=5, next_gw=6, cfg=cfg)
    by_chip = {a["chip"]: a for a in advice}
    assert by_chip["wildcard"]["recommendation"] == "skip"
    assert "sets remaining" in by_chip["wildcard"]["reason"]


# ---------------------------------------------------------------------------
# T4.2 — chip advice v2
# ---------------------------------------------------------------------------

ALL_CHIPS = {"wildcard": 1, "freehit": 1, "bboost": 1, "triple_captain": 1}


def test_v2_wildcard_use_and_3xc_note(db_path, cfg):
    """6 transfers vs bank 1 → wildcard 'use'; captain EP 9.0 → 3XC 'use' with
    the 3XC+WC interplay note."""
    d = _big_diff(bank=1)
    advice = chip_advice_v2(d, ALL_CHIPS, _solved(cap_ep=9.0), 5, 6, cfg)
    by_chip = {a["chip"]: a for a in advice}
    assert by_chip["wildcard"]["recommendation"] == "use"
    assert by_chip["triple_captain"]["recommendation"] == "use"
    assert "3XC + Wildcard" in by_chip["triple_captain"]["reason"]


def test_v2_freehit_consecutive_ban(db_path, cfg):
    """A Free Hit played in the previous GW (GW5) blocks the recommendation."""
    d = _big_diff(bank=1)
    execute("INSERT INTO chip_plays_log (lineup_id, gw, chip, played_at) "
            "VALUES (NULL, 5, 'freehit', '2026-09-19T12:00:00Z')")
    advice = chip_advice_v2(d, ALL_CHIPS, _solved(), 5, 6, cfg)
    by_chip = {a["chip"]: a for a in advice}
    assert by_chip["freehit"]["recommendation"] == "skip"
    assert "cannot follow a Free Hit GW" in by_chip["freehit"]["reason"]
    # without the ban it would be 'consider'
    execute("DELETE FROM chip_plays_log WHERE gw = 5 AND chip = 'freehit'")
    advice2 = chip_advice_v2(d, ALL_CHIPS, _solved(), 5, 6, cfg)
    assert {a["chip"]: a for a in advice2}["freehit"]["recommendation"] == "consider"


def test_v2_3xc_confirmed_starter_signal(db_path, cfg):
    """Captain EP 7.5 (< 8.5) + confirmed-starter signal (conf ≥ 0.7) → 'use'."""
    d = _big_diff(bank=1)
    sig = {"sentiment": "positive", "category": "selection", "confidence": 0.8,
           "summary": "set to start", "source": "fpl-official:1"}
    advice = chip_advice_v2(d, ALL_CHIPS, _solved(cap_ep=7.5), 5, 6, cfg,
                            signals={100: [sig]})
    by_chip = {a["chip"]: a for a in advice}
    assert by_chip["triple_captain"]["recommendation"] == "use"
    assert "confirmed-starter" in by_chip["triple_captain"]["reason"]
    # and without the signal it is only 'consider'
    advice2 = chip_advice_v2(d, ALL_CHIPS, _solved(cap_ep=7.5), 5, 6, cfg)
    assert {a["chip"]: a for a in advice2}["triple_captain"]["recommendation"] == "consider"


def test_v2_bench_boost_strength(db_path, cfg):
    d = _big_diff(bank=1)
    strong = chip_advice_v2(d, ALL_CHIPS, _solved(bench_eps=(6.0, 6.0, 6.0, 6.0)), 5, 6, cfg)
    weak = chip_advice_v2(d, ALL_CHIPS, _solved(bench_eps=(0.5, 0.5, 0.5, 0.5)), 5, 6, cfg)
    assert {a["chip"]: a for a in strong}["bboost"]["recommendation"] == "consider"
    assert {a["chip"]: a for a in weak}["bboost"]["recommendation"] == "skip"


def test_v2_window_closed(db_path, cfg):
    """A chip whose window does not cover the next GW is skipped as closed."""
    execute("UPDATE chips SET stop_event = 5 WHERE name = 'bboost'")  # covers GW5 only
    d = _big_diff(bank=1)
    advice = chip_advice_v2(d, ALL_CHIPS, _solved(), 5, 6, cfg)
    by_chip = {a["chip"]: a for a in advice}
    assert by_chip["bboost"]["recommendation"] == "skip"
    assert "window" in by_chip["bboost"]["reason"]


def test_v2_no_sets_remaining(db_path, cfg):
    d = _big_diff(bank=1)
    advice = chip_advice_v2(d, {"wildcard": 0, "freehit": 1, "bboost": 1, "triple_captain": 1},
                            _solved(), 5, 6, cfg)
    by_chip = {a["chip"]: a for a in advice}
    assert by_chip["wildcard"]["recommendation"] == "skip"
    assert "sets remaining" in by_chip["wildcard"]["reason"]