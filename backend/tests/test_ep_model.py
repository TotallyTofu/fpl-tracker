"""EP model tests: deterministic, config-driven (PLAN-2 T1.16)."""
import pytest

from app.optimizer.scoring import (
    availability_multiplier,
    ep_final,
    ep_baseline,
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


def test_ep_final_weights():
    cfg = _cfg()
    pl = p(ep_next=10.0, form=10.0)
    # 0.7*10 + 0.15*(min(10,12)*0.9) + 0.15*0
    assert ep_final(pl, [], cfg) == 0.7 * 10.0 + 0.15 * 9.0


def test_ep_final_fixture_adjustment_by_position():
    cfg = _cfg()
    gk = p(element_type=1, ep_next=0.0, form=None)
    fwd = p(element_type=4, ep_next=0.0, form=None)
    # difficulty 1 (easy): GK/DEF +1.0, FWD +0.5 (×0.15 weight)
    assert ep_final(gk, [], cfg, fixture_difficulty=1) == 0.15 * 1.0
    assert ep_final(fwd, [], cfg, fixture_difficulty=1) == 0.15 * 0.5
    # difficulty 3 (hard): 0
    assert ep_final(gk, [], cfg, fixture_difficulty=3) == 0.0


def test_score_lineup_captain_doubles_vc_no_multiplier():
    cfg = _cfg()
    cap = p(id=1, ep_next=10.0)
    vc = p(id=2, ep_next=8.0)
    other = p(id=3, ep_next=6.0)
    xi = [cap, vc, other]
    squad = xi
    out = score_lineup(squad, xi, captain_id=1, vc_id=2, signals_by_player={}, cfg=cfg)
    # baseline: XI EP + captain EP = 10+8+6+10 = 34
    assert out["baseline"] == 34.0
    # adjusted: 0.7*(10+8+6) + 0.7*10 (captain again) = 0.7*34 = 23.8 (VC NOT doubled)
    assert out["adjusted"] == round(0.7 * 34.0, 1)
    assert out["with_captain"] == out["adjusted"]


def _cfg(active=False):
    import types
    return types.SimpleNamespace(
        optimizer=types.SimpleNamespace(
            weights=types.SimpleNamespace(ep=0.7, form=0.15, fixture=0.15),
            availability=types.SimpleNamespace(
                active=active, doubt=0.5, chance_null=0.9, chance_100=1.0, chance_50=0.5, chance_0=0.0
            ),
            signal=types.SimpleNamespace(neg_per=-0.5, neg_cap=-0.6, pos_per=0.1, pos_cap=0.2),
        )
    )