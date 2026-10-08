"""EP model tests: deterministic, config-driven (PLAN-2 T1.16)."""
import types

import pytest

from app.optimizer.scoring import (
    availability_multiplier,
    ep_final,
    ep_baseline,
    ep_next_unflagged,
    play_factor,
    score_lineup,
    signal_adjustment,
)


def p(**kw):
    base = {"id": 1, "ep_next": 10.0, "status": "a", "can_select": 1, "element_type": 3,
            "form": None, "chance_of_playing_next_round": None}
    base.update(kw)
    return base


def test_baseline_missing_ep_is_zero():
    assert ep_baseline(p(ep_next=None)) == 0.0
    assert ep_baseline(p(ep_next=12.5)) == 12.5


def test_availability_hard_gates():
    assert availability_multiplier(p(status="u"), cfg=_cfg()) == 0.0
    assert availability_multiplier(p(status="s"), cfg=_cfg()) == 0.0
    assert availability_multiplier(p(can_select=0), cfg=_cfg()) == 0.0
    assert availability_multiplier(p(), cfg=_cfg()) == 1.0


def test_availability_inactive_in_m1():
    # M1: availability.active=False → pass-through even for doubt/50%
    assert availability_multiplier(p(status="d"), cfg=_cfg()) == 1.0
    assert availability_multiplier(p(chance_of_playing_next_round=50), cfg=_cfg()) == 1.0


def test_availability_active_maps():
    cfg = _cfg(active=True)
    # doubt × chance (both maps multiply)
    assert availability_multiplier(p(status="d", chance_of_playing_next_round=100), cfg=cfg) == 0.5
    assert availability_multiplier(p(chance_of_playing_next_round=100), cfg=cfg) == 1.0
    assert availability_multiplier(p(chance_of_playing_next_round=50), cfg=cfg) == 0.5
    assert availability_multiplier(p(chance_of_playing_next_round=0), cfg=cfg) == 0.0
    assert availability_multiplier(p(chance_of_playing_next_round=None), cfg=cfg) == 0.9


@pytest.mark.parametrize("chance,expected", [
    (None, 0.9),   # chance_null
    (100, 1.0),    # chance_100
    (75, 0.85),    # FIX T2: ≥75 bucket (getattr fallback default)
    (50, 0.5),     # chance_50
    (25, 0.35),    # FIX T2: ≥25 bucket (getattr fallback default)
    (10, 0.0),     # FIX T2: 0 < chance < 25 → effectively out
    (0, 0.0),      # chance_0
])
def test_availability_chance_buckets(chance, expected):
    """FIX T2: FPL also emits 25/75 and intermediate chance values — the old
    exact-match chain (==100/==50/==0) priced them as fully available."""
    cfg = _cfg(active=True)
    assert availability_multiplier(p(chance_of_playing_next_round=chance), cfg=cfg) == expected


def test_availability_chance_buckets_from_config():
    """FIX T2: the 75/25 buckets come from config keys when present."""
    cfg = _cfg(active=True)
    cfg.optimizer.availability.chance_75 = 0.8
    cfg.optimizer.availability.chance_25 = 0.3
    assert availability_multiplier(p(chance_of_playing_next_round=75), cfg=cfg) == 0.8
    assert availability_multiplier(p(chance_of_playing_next_round=25), cfg=cfg) == 0.3
    assert availability_multiplier(p(chance_of_playing_next_round=60), cfg=cfg) == 0.5  # still the 50 bucket
    assert availability_multiplier(p(chance_of_playing_next_round=30), cfg=cfg) == 0.3


def test_signal_adjustment_stacking_and_caps():
    cfg = _cfg()
    assert signal_adjustment(p(), [], cfg) == 0.0
    assert signal_adjustment(p(), [{"sentiment": "negative", "confidence": 0.8}], cfg) == -0.4
    # two strong negatives cap at -0.6
    sigs = [{"sentiment": "negative", "confidence": 0.9}, {"sentiment": "negative", "confidence": 0.9}]
    assert signal_adjustment(p(), sigs, cfg) == -0.6
    # positives cap at +0.2 (3 × 0.9 × 0.1 = 0.27 → capped)
    pos = [{"sentiment": "positive", "confidence": 0.9}] * 3
    assert signal_adjustment(p(), pos, cfg) == 0.2


def test_ep_final_blend_of_ep_next_and_season_average():
    """blend = (w.ep·ep_next + w.form·ppg·f) / (w.ep + w.form) — on the points
    scale (the old model shrank every projection by ~16%). ep_next has FPL's own
    flag scaling removed and the calibrated curve f applied once (v1.1)."""
    cfg = _cfg()
    assert ep_final(p(ep_next=10.0, points_per_game=10.0), [], cfg) == 10.0
    # ep_next 10, season average 6 → (7 + 0.9) / 0.85
    assert abs(ep_final(p(ep_next=10.0, points_per_game=6.0), [], cfg) - 7.9 / 0.85) < 1e-9
    # 75% flag: FPL's ep_next 7.5 is form 10 × 0.75 → unflagged 10.0, curve 0.60
    got = ep_final(p(ep_next=7.5, points_per_game=10.0, chance_of_playing_next_round=75), [], cfg)
    assert abs(got - (0.7 * 10.0 * 0.6 + 0.15 * 10.0 * 0.6) / 0.85) < 1e-9
    assert abs(got - 6.0) < 1e-9


def test_ep_final_applies_calibrated_curve_once():
    """FPL's ep_next is form × chance/100, so it is un-scaled first and the
    calibrated curve (75% → 0.60) is applied exactly once. Official FPL news
    is still not priced a second time."""
    cfg = _cfg(active=True)
    doubt = p(ep_next=3.0, points_per_game=4.0, status="d", chance_of_playing_next_round=75)
    official = [{"sentiment": "negative", "confidence": 0.7, "source": "fpl-official"}]
    # unflagged ep_next = 3.0 / 0.75 = 4.0
    expected = (0.7 * 4.0 * 0.6 + 0.15 * 4.0 * 0.6) / 0.85
    assert abs(expected - 2.4) < 1e-9
    assert abs(ep_final(doubt, official, cfg) - expected) < 1e-9


def test_ep_final_unflagged_without_chance():
    """No flag → ep_next is used as it is."""
    cfg = _cfg()
    pl = p(ep_next=6.0, points_per_game=6.0, chance_of_playing_next_round=None)
    assert ep_final(pl, [], cfg) == 6.0
    assert ep_next_unflagged(pl) == 6.0
    assert ep_next_unflagged(p(ep_next=6.0, chance_of_playing_next_round=100)) == 6.0


def test_ep_next_unflagged_undoes_fpl_scaling():
    assert ep_next_unflagged(p(ep_next=3.0, chance_of_playing_next_round=75)) == 4.0
    assert ep_next_unflagged(p(ep_next=2.0, chance_of_playing_next_round=50)) == 4.0
    assert ep_next_unflagged(p(ep_next=0.0, chance_of_playing_next_round=0)) == 0.0


@pytest.mark.parametrize("chance,expected", [
    (None, 1.0), (100, 1.0), (99, 0.60), (75, 0.60), (74, 0.50), (50, 0.50),
    (49, 0.05), (25, 0.05), (24, 0.0), (0, 0.0),
])
def test_play_factor_buckets(chance, expected):
    assert play_factor(p(chance_of_playing_next_round=chance), _cfg()) == expected


def test_play_factor_hard_gates():
    cfg = _cfg()
    assert play_factor(p(status="u"), cfg) == 0.0
    assert play_factor(p(status="s"), cfg) == 0.0
    assert play_factor(p(can_select=0), cfg) == 0.0


def test_play_factor_from_config():
    cfg = _cfg()
    cfg.optimizer.availability_curve = types.SimpleNamespace(play_75=0.7, play_50=0.4, play_25=0.1)
    assert play_factor(p(chance_of_playing_next_round=75), cfg) == 0.7
    assert play_factor(p(chance_of_playing_next_round=50), cfg) == 0.4
    assert play_factor(p(chance_of_playing_next_round=25), cfg) == 0.1


def test_default_config_carries_the_curve():
    from app.config import ConfigFile
    c = ConfigFile()
    assert (c.optimizer.availability_curve.play_75, c.optimizer.availability_curve.play_50,
            c.optimizer.availability_curve.play_25) == (0.60, 0.50, 0.05)
    # an existing config.json without the key loads with the defaults
    assert ConfigFile.model_validate({"optimizer": {}}).optimizer.availability_curve.play_75 == 0.60


def test_ep_final_fixture_multiplier_by_position():
    cfg = _cfg()
    gk = p(element_type=1, ep_next=6.0, points_per_game=6.0)
    fwd = p(element_type=4, ep_next=6.0, points_per_game=6.0)
    # easy fixture (1): GK/DEF ×(1 + 0.15·0.8·2) = 1.24, MID/FWD ×(1 + 0.15·0.5·2) = 1.15
    assert abs(ep_final(gk, [], cfg, fixture_difficulty=1) - 6.0 * 1.24) < 1e-9
    assert abs(ep_final(fwd, [], cfg, fixture_difficulty=1) - 6.0 * 1.15) < 1e-9
    assert ep_final(gk, [], cfg, fixture_difficulty=3) == 6.0          # neutral
    assert abs(ep_final(gk, [], cfg, fixture_difficulty=5) - 6.0 * 0.76) < 1e-9
    assert ep_final(gk, [], cfg, fixture_difficulty=None) == 6.0       # no fixture data


def test_ep_final_blank_and_double_gameweek():
    cfg = _cfg()
    pl = p(ep_next=6.0, points_per_game=6.0)
    assert ep_final(pl, [], cfg, fixture_difficulty=[]) == 0.0         # blank GW
    # double GW: the season-average term counts both fixtures
    dgw = ep_final(pl, [], cfg, fixture_difficulty=[3, 3])
    assert abs(dgw - (0.7 * 6.0 + 0.15 * 12.0) / 0.85) < 1e-9


def test_ep_final_beyond_next_gw_uses_season_average():
    cfg = _cfg()
    pl = p(ep_next=9.0, points_per_game=5.0)
    assert ep_final(pl, [], cfg, use_ep_next=False) == 5.0


def test_signal_adjustment_skips_official_news():
    cfg = _cfg()
    sigs = [{"sentiment": "negative", "confidence": 1.0, "source": "fpl-official"},
            {"sentiment": "negative", "confidence": 0.4, "source": "bbc:x"}]
    assert signal_adjustment(p(), sigs, cfg) == -0.2


def test_score_lineup_captain_doubles_vc_no_multiplier():
    cfg = _cfg()
    cap = p(id=1, ep_next=10.0, points_per_game=10.0)
    vc = p(id=2, ep_next=8.0, points_per_game=8.0)
    other = p(id=3, ep_next=6.0, points_per_game=6.0)
    xi = [cap, vc, other]
    out = score_lineup(xi, xi, captain_id=1, vc_id=2, signals_by_player={}, cfg=cfg)
    # XI 10+8+6 plus the captain once more = 34 (VC NOT doubled)
    assert out["baseline"] == 34.0
    assert out["adjusted"] == 34.0
    assert out["with_captain"] == out["adjusted"]


def test_score_lineup_chips():
    cfg = _cfg()
    cap = p(id=1, ep_next=10.0, points_per_game=10.0)
    other = p(id=2, ep_next=6.0, points_per_game=6.0)
    bench = p(id=3, ep_next=2.0, points_per_game=2.0)
    squad = [cap, other, bench]
    xi = [cap, other]
    tc = score_lineup(squad, xi, 1, 2, {}, cfg, chip="triple_captain")
    assert tc["adjusted"] == 36.0                    # 10×3 + 6
    bb = score_lineup(squad, xi, 1, 2, {}, cfg, chip="bboost")
    assert bb["adjusted"] == 28.0                    # 10×2 + 6 + bench 2


def _cfg(active=False):
    return types.SimpleNamespace(
        optimizer=types.SimpleNamespace(
            weights=types.SimpleNamespace(ep=0.7, form=0.15, fixture=0.15),
            availability=types.SimpleNamespace(
                active=active, doubt=0.5, chance_null=0.9, chance_100=1.0, chance_50=0.5, chance_0=0.0
            ),
            signal=types.SimpleNamespace(neg_per=-0.5, neg_cap=-0.6, pos_per=0.1, pos_cap=0.2),
        )
    )