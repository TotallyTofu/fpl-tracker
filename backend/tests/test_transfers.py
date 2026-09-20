"""Transfer math + chip advice tests (PLAN-2 T1.16)."""
from app.optimizer.transfers import chip_advice_v1, compute_diff, sell_value


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