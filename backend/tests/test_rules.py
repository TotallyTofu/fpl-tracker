"""Rules validator unit tests (PLAN-2 T1.16)."""
from app.optimizer.rules import validate_lineup

from conftest import mkplayer, valid_squad


def codes(check):
    return {e["code"] for e in check.errors}


def test_valid_squad_passes():
    c = validate_lineup(valid_squad(), bank=3)
    assert c.valid, c.errors
    assert c.errors == []


def test_squad_size_14():
    s = valid_squad()[:-1]
    c = validate_lineup(s, bank=3)
    assert not c.valid
    assert "SQUAD_SIZE" in codes(c)


def test_duplicate_player_rejected():
    s = valid_squad()
    s[10] = dict(s[10], player_id=s[0]["player_id"])  # duplicate id
    c = validate_lineup(s, bank=3)
    assert not c.valid
    assert "SQUAD_SIZE" in codes(c)


def test_composition_3_gk():
    s = valid_squad()
    s[2] = mkplayer(3, 1, 1, 400)  # DEF -> GK, now 3 GK
    c = validate_lineup(s, bank=3)
    assert "SQUAD_COMPOSITION" in codes(c)


def test_budget_exceeded():
    s = [dict(p, now_cost=p["now_cost"] + 100) for p in valid_squad()]  # 805 → 2305
    c = validate_lineup(s, bank=3)
    assert "BUDGET_EXCEEDED" in codes(c)


def test_club_limit_4_from_one_club():
    s = valid_squad()
    s[1] = mkplayer(2, 1, 1, 450, role="bench", bench_order=1)  # GK2 -> club 1 (now 4 in club 1)
    c = validate_lineup(s, bank=3)
    assert "CLUB_LIMIT" in codes(c)


def test_xi_position_min_only_2_def():
    s = valid_squad()
    s[4] = mkplayer(7, 3, 3, 450)  # XI DEF -> MID
    s[5] = mkplayer(9, 3, 4, 450)  # XI DEF -> MID → only 2 DEF in XI
    c = validate_lineup(s, bank=3)
    assert "XI_POSITION_MIN" in codes(c)


def test_bench_order_invalid():
    s = valid_squad()
    s[1] = dict(s[1], bench_order=5)
    c = validate_lineup(s, bank=3)
    assert "BENCH_ORDER" in codes(c)


def test_missing_captain():
    s = valid_squad()
    s[0] = dict(s[0], is_captain=False)
    c = validate_lineup(s, bank=3)
    assert "CAPTAIN_NOT_IN_XI" in codes(c)


def test_captain_on_bench():
    s = valid_squad()
    s[0] = dict(s[0], is_captain=False)
    s[1] = dict(s[1], is_captain=True)  # bench GK becomes captain
    c = validate_lineup(s, bank=3)
    assert "CAPTAIN_NOT_IN_XI" in codes(c)


def test_vice_captain_on_bench():
    s = valid_squad()
    s[5] = dict(s[5], is_vice_captain=False)
    s[11] = dict(s[11], is_vice_captain=True)  # bench MID becomes VC
    c = validate_lineup(s, bank=3)
    assert "VICE_CAPTAIN_NOT_IN_XI" in codes(c)


def test_captain_and_vc_same_player():
    s = valid_squad()
    s[7] = dict(s[7], is_vice_captain=False)  # original VC (MID t2)
    s[0] = dict(s[0], is_vice_captain=True)   # captain also becomes VC
    c = validate_lineup(s, bank=3)
    assert "CAPTAIN_VC_SAME" in codes(c)


def test_bank_range():
    c = validate_lineup(valid_squad(), bank=0)
    assert "BANK_RANGE" in codes(c)
    c = validate_lineup(valid_squad(), bank=6)
    assert "BANK_RANGE" in codes(c)


def test_chip_range():
    c = validate_lineup(valid_squad(), bank=3, chips={"wildcard": 3})
    assert "CHIP_RANGE" in codes(c)


def test_unavailable_player_strict_vs_nonstrict():
    s = valid_squad()
    s[3] = dict(s[3], status="u")
    strict = validate_lineup(s, bank=3, strict=True)
    assert not strict.valid
    assert "PLAYER_UNAVAILABLE" in codes(strict)
    loose = validate_lineup(s, bank=3, strict=False)
    assert loose.valid  # warning only
    assert any(e["code"] == "PLAYER_UNAVAILABLE" and e["severity"] == "warning" for e in loose.errors)


def test_can_select_zero_unavailable():
    s = valid_squad()
    s[3] = dict(s[3], can_select=0)
    c = validate_lineup(s, bank=3, strict=True)
    assert "PLAYER_UNAVAILABLE" in codes(c)