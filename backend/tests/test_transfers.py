"""Transfer math + chip advice tests (PLAN-2 T1.16, M4 T4.2)."""
import json
from types import SimpleNamespace

from app.db import execute, query_one, upsert_chips
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


def test_compute_diff_swap_is_one_transfer():
    """A swap (one in + one out) is ONE transfer — FPL charges per player out."""
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
    assert d["free_transfers_used"] == 1
    assert d["bank_after"] == 1  # v1.0: free transfers LEFT this gameweek (2 − 1)
    assert d["penalty_points"] == 0


def test_compute_diff_penalty_over_bank():
    cur = [{"player_id": i, "web_name": f"P{i}", "now_cost": 500, "bought_cost": 500} for i in range(1, 4)]
    new = [{"player_id": i, "web_name": f"N{i}", "now_cost": 500} for i in range(10, 13)]
    d = compute_diff(cur, new, bank=2)
    # 3 swaps = 3 transfers, bank 2 → 1 over → 4 pts
    assert d["penalty_points"] == 4
    assert d["bank_after"] == 0  # v1.0: none left this GW; +1 when the deadline passes
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


def test_v2_one_chip_per_gameweek(db_path, cfg):
    """3 transfers vs 1 free → wildcard would save 8 points; captain projects
    9.0 → triple captain is worth more. FPL allows one chip per GW: exactly one
    "use", the other is downgraded with the reason."""
    d = _big_diff(bank=1)
    advice = chip_advice_v2(d, ALL_CHIPS, _solved(cap_ep=9.0, bench_eps=(1, 1, 1, 1)), 5, 6, cfg)
    by_chip = {a["chip"]: a for a in advice}
    assert [a["chip"] for a in advice if a["recommendation"] == "use"] == ["triple_captain"]
    assert by_chip["wildcard"]["recommendation"] == "consider"
    assert "only one chip per gameweek" in by_chip["wildcard"]["reason"]


def test_v2_chosen_chip_is_the_only_one(db_path, cfg):
    d = compute_diff([], [], bank=1, chip_played="bboost")
    advice = chip_advice_v2(d, ALL_CHIPS, _solved(cap_ep=12.0), 5, 6, cfg)
    by_chip = {a["chip"]: a for a in advice}
    assert by_chip["bboost"]["recommendation"] == "use"
    assert all(by_chip[c]["recommendation"] == "skip"
               for c in ("wildcard", "freehit", "triple_captain"))


def test_v2_logged_chip_blocks_others(db_path, cfg):
    execute("INSERT INTO chip_plays_log (lineup_id, gw, chip, played_at) "
            "VALUES (NULL, 6, 'wildcard', '2026-09-19T12:00:00Z')")
    advice = chip_advice_v2(_big_diff(bank=1), ALL_CHIPS, _solved(cap_ep=12.0), 5, 6, cfg)
    assert [a["chip"] for a in advice if a["recommendation"] == "use"] == ["wildcard"]


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
    d = _big_diff(bank=3)    # transfers fit the bank → no competing wildcard "use"
    advice = chip_advice_v2(d, ALL_CHIPS, _solved(cap_ep=7.5, bench_eps=(1, 1, 1, 1)), 5, 6, cfg,
                            signals={100: [sig]})
    by_chip = {a["chip"]: a for a in advice}
    assert by_chip["triple_captain"]["recommendation"] == "use"
    assert "confirmed-starter" in by_chip["triple_captain"]["reason"]
    # and without the signal it is only 'consider'
    advice2 = chip_advice_v2(d, ALL_CHIPS, _solved(cap_ep=7.5, bench_eps=(1, 1, 1, 1)), 5, 6, cfg)
    assert {a["chip"]: a for a in advice2}["triple_captain"]["recommendation"] == "consider"


def test_v2_bench_boost_strength(db_path, cfg):
    d = _big_diff(bank=1)
    # v1.0 thresholds: bench projection ≥ 14 → use, ≥ 10 → consider
    strong = chip_advice_v2(d, ALL_CHIPS, _solved(cap_ep=5.0, bench_eps=(6.0, 6.0, 6.0, 6.0)), 5, 6, cfg)
    fair = chip_advice_v2(d, ALL_CHIPS, _solved(cap_ep=5.0, bench_eps=(3.0, 3.0, 3.0, 2.0)), 5, 6, cfg)
    weak = chip_advice_v2(d, ALL_CHIPS, _solved(cap_ep=5.0, bench_eps=(0.5, 0.5, 0.5, 0.5)), 5, 6, cfg)
    assert {a["chip"]: a for a in strong}["bboost"]["recommendation"] == "use"
    assert {a["chip"]: a for a in fair}["bboost"]["recommendation"] == "consider"
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


# ---------------------------------------------------------------------------
# Bug 3 — shared chip-covers-transfers rule (FIX.MD 2.7)
# ---------------------------------------------------------------------------

def test_chip_covers_transfers_basic(db_path, cfg):
    from app.optimizer.transfers import chip_covers_transfers
    # conftest: wildcard+freehit windows cover GW6 (the next GW)
    assert chip_covers_transfers({}, 6) is False
    # v1.0: holding a chip is not playing it
    assert chip_covers_transfers({"wildcard": 1}, 6) is False
    assert chip_covers_transfers({"wildcard": 1}, 6, "wildcard") is True
    assert chip_covers_transfers({"freehit": 1}, 6, "freehit") is True
    assert chip_covers_transfers({"wildcard": 0}, 6, "wildcard") is False   # none left
    assert chip_covers_transfers({"bboost": 1}, 6, "bboost") is False  # bboost doesn't free transfers


def test_chip_covers_transfers_freehit_consecutive_ban(db_path, cfg):
    from app.db import execute
    from app.optimizer.transfers import chip_covers_transfers
    # The real-world path: a played chip logs lineup_id = NULL (gw-scoped ban).
    execute("INSERT INTO chip_plays_log (lineup_id, gw, chip, played_at) "
            "VALUES (NULL, 5, 'freehit', '2026-09-19T12:00:00Z')")
    # free-hit played in GW5 → not playable in GW6 → only wildcard covers
    assert chip_covers_transfers({"freehit": 1}, 6, "freehit") is False
    assert chip_covers_transfers({"freehit": 1, "wildcard": 1}, 6, "wildcard") is True
    # after deleting the row, free-hit covers again
    execute("DELETE FROM chip_plays_log WHERE gw = 5 AND chip = 'freehit'")
    assert chip_covers_transfers({"freehit": 1}, 6, "freehit") is True


def test_chip_covers_transfers_ban_row_with_any_lineup_id(db_path, cfg):
    """Both a NULL-lineup_id row and a row with an unrelated lineup_id (999) must
    block free-hit coverage — regression test for the screenshot scenario."""
    from app.db import execute
    from app.optimizer.transfers import chip_covers_transfers
    execute("INSERT INTO chip_plays_log (lineup_id, gw, chip, played_at) "
            "VALUES (NULL, 5, 'freehit', '2026-09-19T12:00:00Z')")
    assert chip_covers_transfers({"freehit": 1}, 6, "freehit") is False
    execute("DELETE FROM chip_plays_log WHERE lineup_id IS NULL")
    execute("INSERT INTO lineups (id, name, created_at, updated_at) "
            "VALUES (999, 't', '2026-09-19T12:00:00Z', '2026-09-19T12:00:00Z')")
    execute("INSERT INTO chip_plays_log (lineup_id, gw, chip, played_at) "
            "VALUES (999, 5, 'freehit', '2026-09-19T12:00:00Z')")
    assert chip_covers_transfers({"freehit": 1}, 6, "freehit") is False


# ---------------------------------------------------------------------------
# FIX.MD — chip-aware compute_diff + bank floor (S1/S2)
# ---------------------------------------------------------------------------

def test_compute_diff_chip_covers():
    """A Wildcard/Free Hit covering the target GW makes all transfers free:
    no penalty, and the bank carries over unchanged."""
    cur = [{"player_id": i, "web_name": f"P{i}", "now_cost": 500, "bought_cost": 500} for i in range(1, 4)]
    new = [{"player_id": i, "web_name": f"N{i}", "now_cost": 500} for i in range(10, 13)]
    d = compute_diff(cur, new, bank=2, chip_covers=True)
    assert d["penalty_points"] == 0
    assert d["bank_after"] == 2
    assert d["free_transfers_used"] == 0


def test_compute_diff_bank_floor():
    """No chip, overdrawn: 0 free transfers left this gameweek (the +1 for
    the next one is added when the deadline passes — lineups.roll_bank)."""
    cur = [{"player_id": i, "web_name": f"P{i}", "now_cost": 500, "bought_cost": 500} for i in range(1, 4)]
    new = [{"player_id": i, "web_name": f"N{i}", "now_cost": 500} for i in range(10, 13)]
    d = compute_diff(cur, new, bank=2)
    assert d["bank_after"] == 0
    assert d["penalty_points"] == 4  # 3 swaps = 3 transfers, bank 2 → 1 over


def test_compute_diff_reports_chip_covers_and_bank_before():
    """D4: compute_diff reports chip_covers and bank_before in its diff payload."""
    cur = [{"player_id": i, "web_name": f"P{i}", "now_cost": 500, "bought_cost": 500} for i in range(1, 4)]
    new = [{"player_id": i, "web_name": f"N{i}", "now_cost": 500} for i in range(10, 13)]
    d = compute_diff(cur, new, bank=3, chip_covers=True)
    assert d["chip_covers"] is True
    assert d["bank_before"] == 3
    assert d["penalty_points"] == 0
    assert d["free_transfers_used"] == 0
    assert d["bank_after"] == 3


def test_chip_advice_requires_bank_before(db_path, cfg):
    """A22: a diff without bank_before (legacy row) must raise KeyError —
    the old fallback guessed '2' for a bank of 1 with one transfer."""
    import pytest

    legacy = {
        "transfers_in": [{"player_id": 10}],
        "transfers_out": [{"player_id": 1}],
        "free_transfers_used": 1,
        "bank_after": 1,
    }
    with pytest.raises(KeyError):
        chip_advice_v1(legacy, {}, 5, 6, cfg)
    with pytest.raises(KeyError):
        chip_advice_v2(legacy, {}, _solved(), 5, 6, cfg)


def test_compute_diff_budget_after():
    """FIX T1 (§25.1): honest money — budget_before/budget_after.

    Out player bought 50 / now 52 → sells 51 → the 1-unit fee is DESTROYED
    value: budget_after == 1000 − Σ new_n − 1. (The FIX.MD snippet wrote
    "+1", but its own feasibility math — "a strictly tighter constraint" and
    the solver check `total + in − out + fee ≤ 1000` — requires minus: a
    fee-bearing sale must leave LESS money than the fee-blind view, not more.
    Selling at 51 after buying at 50 cannot add a unit to the bank.)
    """
    cur = [
        {"player_id": 1, "web_name": "A", "now_cost": 52, "bought_cost": 50},
        {"player_id": 2, "web_name": "B", "now_cost": 60, "bought_cost": 60},
    ]
    new = [
        {"player_id": 3, "web_name": "C", "now_cost": 55},
        {"player_id": 2, "web_name": "B", "now_cost": 60},
    ]
    d = compute_diff(cur, new, bank=2)
    assert d["transfers_out"][0]["sell_value"] == 51
    assert d["budget_before"] == 1000 - 112        # 888
    assert d["budget_after"] == 1000 - 115 - 1     # 884 — fee destroyed
    assert d["budget_after"] == d["budget_before"] - 55 + 51


def test_compute_diff_ep_enrichment():
    """FIX T9 (optional, implemented): when an id→ep map is passed, each
    transfers_in/transfers_out entry carries the player's projected EP."""
    cur = [
        {"player_id": 1, "web_name": "A", "now_cost": 50, "bought_cost": 50},
        {"player_id": 2, "web_name": "B", "now_cost": 60, "bought_cost": 60},
    ]
    new = [
        {"player_id": 3, "web_name": "C", "now_cost": 55},
        {"player_id": 2, "web_name": "B", "now_cost": 60},
    ]
    d = compute_diff(cur, new, bank=2, ep_by_player={1: 6.275, 3: 8.375})
    assert d["transfers_out"][0]["ep"] == 6.28     # round(x, 2)
    assert d["transfers_in"][0]["ep"] == 8.38      # the swap's in player
    assert len(d["transfers_in"]) == 1             # kept player 2 → no entry
    # no map → ep present but None (legacy-tolerant shape)
    d2 = compute_diff(cur, new, bank=2)
    assert d2["transfers_out"][0]["ep"] is None


# ---------------------------------------------------------------------------
# FIX.MD A1 — chip name normalisation at ingest (regression guard)
# ---------------------------------------------------------------------------


def test_upsert_chips_normalises_names(db_path):
    """Raw API spelling '3xc' is stored as 'triple_captain'; raw value kept in raw_json."""
    upsert_chips([{"id": 9, "name": "3xc", "number": 1, "start_event": 6, "stop_event": 6,
                   "chip_type": "3x_captain"}])
    row = query_one("SELECT name, raw_json FROM chips WHERE id = 9")
    assert row["name"] == "triple_captain"
    assert json.loads(row["raw_json"])["name"] == "3xc"


def test_chip_advice_triple_captain_window_open(db_path, cfg):
    """In-hand triple captain + open window + high captain EP → 'use' (not the
    permanent 'skip' the raw '3xc' name used to cause)."""
    d = _big_diff(bank=1)
    advice = chip_advice_v2(d, ALL_CHIPS, _solved(cap_ep=9.0, bench_eps=(1, 1, 1, 1)), 5, 6, cfg)
    by_chip = {a["chip"]: a for a in advice}
    assert by_chip["triple_captain"]["recommendation"] == "use"


def test_chip_advice_triple_captain_no_window(db_path, cfg):
    """A closed window skips with the *window* reason, not the sets-remaining one."""
    execute("UPDATE chips SET start_event = 1, stop_event = 5 WHERE name = 'triple_captain'")
    d = _big_diff(bank=1)
    advice = chip_advice_v2(d, ALL_CHIPS, _solved(cap_ep=9.0), 5, 6, cfg)
    by_chip = {a["chip"]: a for a in advice}
    assert by_chip["triple_captain"]["recommendation"] == "skip"
    assert "window" in by_chip["triple_captain"]["reason"]
    assert "sets remaining" not in by_chip["triple_captain"]["reason"]

def test_compute_diff_money_with_known_bank():
    """v1.0: money in the bank = the user's figure + sells − buys."""
    cur = [{"player_id": 1, "web_name": "A", "now_cost": 80, "bought_cost": 70},
           {"player_id": 2, "web_name": "B", "now_cost": 50, "bought_cost": 50}]
    new = [{"player_id": 2, "web_name": "B", "now_cost": 50},
           {"player_id": 3, "web_name": "C", "now_cost": 78}]
    d = compute_diff(cur, new, bank=1, bank_money=5)
    assert d["transfers_out"][0]["sell_value"] == 75      # 70 + (80 − 70) // 2
    assert d["budget_before"] == 5 and d["money_known"] is True
    assert d["budget_after"] == 5 + 75 - 78
    est = compute_diff(cur, new, bank=1)
    assert est["money_known"] is False
    assert est["budget_before"] == 1000 - 130


def test_chip_play_problem_rules(db_path, cfg):
    from app.optimizer.transfers import chip_play_problem
    chips = {"wildcard": 1, "freehit": 1, "bboost": 1, "triple_captain": 0}
    assert chip_play_problem("wildcard", chips, 6) is None
    assert "sets remaining" in chip_play_problem("triple_captain", chips, 6)
    execute("INSERT INTO chip_plays_log (lineup_id, gw, chip, played_at) "
            "VALUES (NULL, 6, 'bboost', '2026-09-19T12:00:00Z')")
    assert "one chip per gameweek" in chip_play_problem("wildcard", chips, 6)
