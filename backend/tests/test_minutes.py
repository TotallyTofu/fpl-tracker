"""v1.1 T3/T4: minutes model, start probability, and their place in ep_final."""
from __future__ import annotations

import types

import pytest

from app import db as dbmod
from app.config import ConfigFile
from app.optimizer import minutes as mm
from app.optimizer.scoring import ep_final, minutes_ep
from app.optimizer.solver import build_universe


def rows(minutes, points=None, starts=None, tm=None):
    """History rows for one player, gw 1..n."""
    n = len(minutes)
    points = points or [0] * n
    starts = starts if starts is not None else [1 if m >= 60 else 0 for m in minutes]
    tm = tm or [1] * n
    return [{"player_id": 1, "gw": i + 1, "team_matches": tm[i], "minutes": minutes[i],
             "starts": starts[i], "total_points": points[i]} for i in range(n)]


def _cfg(enabled=True, weight=0.5):
    return types.SimpleNamespace(
        optimizer=types.SimpleNamespace(
            weights=types.SimpleNamespace(ep=0.7, form=0.15, fixture=0.15),
            signal=types.SimpleNamespace(neg_per=-0.5, neg_cap=-0.6, pos_per=0.1, pos_cap=0.2),
            minutes_model=types.SimpleNamespace(enabled=enabled, weight=weight),
        )
    )


def pl(**kw):
    base = {"id": 1, "ep_next": 6.0, "points_per_game": 5.0, "status": "a", "can_select": 1,
            "element_type": 3, "form": None, "chance_of_playing_next_round": None,
            "mm_share": None, "mm_rate90": None}
    base.update(kw)
    return base


# --- profile ----------------------------------------------------------------------


def test_profile_hand_computed():
    # minutes 90,90,0,90,45,90 → all 405 of 6 matches; last 5 → 315 of 5 matches
    # share  = 0.6·315/(90·5) + 0.4·405/(90·6) = 0.42 + 0.30
    # rate90 = (22 + 4.4·450/90) / ((405 + 450)/90) = 44 / 9.5
    # starts (last 5: 1,0,1,0,1) = 3 of 5
    r = rows([90, 90, 0, 90, 45, 90], points=[6, 2, 0, 8, 1, 5], starts=[1, 1, 0, 1, 0, 1])
    prof = mm.profile(r, 3)
    assert abs(prof.share - 0.72) < 1e-12
    assert abs(prof.rate90 - 44 / 9.5) < 1e-12
    assert abs(prof.start_rate5 - 0.6) < 1e-12
    assert prof.last_match == "started"
    assert prof.matches == 6


def test_profile_uses_position_prior():
    zero = rows([0, 0, 0])
    assert abs(mm.profile(zero, 1).rate90 - 3.4) < 1e-12       # no minutes → the prior
    assert abs(mm.profile(zero, 4).rate90 - 5.0) < 1e-12


def test_profile_share_is_capped_and_double_gw_counts_two_matches():
    assert mm.profile(rows([200]), 3).share == 1.0               # more than 90 → capped
    dgw = mm.profile(rows([180], tm=[2]), 3)                     # 180 minutes over 2 matches
    assert dgw.share == 1.0
    half = mm.profile(rows([90, 90], tm=[2, 2]), 3)              # 90 of 2 matches each → 0.5
    assert abs(half.share - 0.5) < 1e-12
    assert mm.profile(rows([180, 180], tm=[2, 2], starts=[2, 2]), 3).start_rate5 == 1.0


def test_profile_last_match_states():
    assert mm.profile(rows([90, 90, 90]), 3).last_match == "started"
    assert mm.profile(rows([90, 90, 25], starts=[1, 1, 0]), 3).last_match == "came_on"
    assert mm.profile(rows([90, 90, 0], starts=[1, 1, 0]), 3).last_match == "no_minutes"


def test_profile_null_starts_and_empty_history():
    assert mm.profile([], 3) is None
    assert mm.profile(rows([0], tm=[0]), 3) is None              # only blank gameweeks
    r = rows([90, 90])
    r[1]["starts"] = None
    prof = mm.profile(r, 3)
    assert prof.start_rate5 is None and prof.share == 1.0


def test_profile_recent_window_is_last_five_matches():
    # 5 old full games then 5 DNPs: season share .5, recent share 0 → 0.4·0.5
    prof = mm.profile(rows([90] * 5 + [0] * 5), 3)
    assert abs(prof.share - 0.2) < 1e-12


# --- load_history -----------------------------------------------------------------


def test_load_history_excludes_blank_gws_and_future(with_history):
    h = mm.load_history(6)
    assert [r["gw"] for r in h[19]] == [1, 3, 4, 5]              # GW2 blank row excluded
    assert [r["gw"] for r in mm.load_history(4)[11]] == [1, 2, 3]  # gw < before_gw
    assert 1 not in h                                            # no history, no key


# --- ep_final ---------------------------------------------------------------------


def test_ep_final_no_history_equals_v1():
    p = pl()
    assert ep_final(p, [], _cfg()) == ep_final(p, [], _cfg(enabled=False))
    assert abs(ep_final(p, [], _cfg()) - (0.7 * 6.0 + 0.15 * 5.0) / 0.85) < 1e-12


def test_ep_final_weight_zero_is_v1_and_weight_one_is_minutes_only():
    p = pl(mm_share=0.5, mm_rate90=5.0)                          # minutes_ep = 2.5
    v1 = ep_final(p, [], _cfg(enabled=False))
    assert ep_final(p, [], _cfg(weight=0.0)) == v1
    assert abs(ep_final(p, [], _cfg(weight=1.0)) - 2.5) < 1e-12
    assert abs(ep_final(p, [], _cfg(weight=0.5)) - (0.5 * v1 + 0.5 * 2.5)) < 1e-12


def test_ep_final_weight_one_applies_fixture_and_news_multipliers():
    p = pl(element_type=2, mm_share=1.0, mm_rate90=4.0)
    sigs = [{"sentiment": "negative", "confidence": 0.4, "source": "bbc:x"}]   # S = -0.2
    got = ep_final(p, sigs, _cfg(weight=1.0), fixture_difficulty=1)
    assert abs(got - 4.0 * 1.24 * 0.8) < 1e-12                   # DEF easy fixture: 1 + .15·.8·2


def test_flag_scales_minutes_estimate_and_double_gw_doubles_it():
    fit = pl(mm_share=0.8, mm_rate90=5.0)
    flagged = pl(mm_share=0.8, mm_rate90=5.0, chance_of_playing_next_round=75, ep_next=4.5)
    assert abs(ep_final(fit, [], _cfg(weight=1.0)) - 4.0) < 1e-12
    assert abs(ep_final(flagged, [], _cfg(weight=1.0)) - 4.0 * 0.6) < 1e-12
    assert abs(ep_final(fit, [], _cfg(weight=1.0), fixture_difficulty=[3, 3]) - 8.0) < 1e-12
    assert minutes_ep(pl(), _cfg()) is None                       # no history → no estimate


def test_minutes_model_also_applies_beyond_next_gw():
    """use_ep_next=False: season-average projection, blended with the minutes model
    (not backtested — the README says so)."""
    p = pl(mm_share=0.5, mm_rate90=4.0)                          # minutes_ep = 2.0
    got = ep_final(p, [], _cfg(weight=0.5), use_ep_next=False)
    assert abs(got - (0.5 * 5.0 + 0.5 * 2.0)) < 1e-12


def test_hard_gates_and_blank_gw_stay_zero_with_history():
    p = pl(mm_share=1.0, mm_rate90=5.0)
    assert ep_final(dict(p, status="u"), [], _cfg()) == 0.0
    assert ep_final(p, [], _cfg(), fixture_difficulty=[]) == 0.0


def test_default_config_enables_the_minutes_model():
    c = ConfigFile()
    assert c.optimizer.minutes_model.enabled is True
    assert c.optimizer.minutes_model.weight == 0.5
    assert ConfigFile.model_validate({"optimizer": {}}).optimizer.minutes_model.weight == 0.5


def test_config_rejects_out_of_range_values():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ConfigFile.model_validate({"optimizer": {"minutes_model": {"weight": 1.5}}})
    with pytest.raises(ValidationError):
        ConfigFile.model_validate({"optimizer": {"availability_curve": {"play_75": -0.1}}})


# --- build_universe ---------------------------------------------------------------


def test_build_universe_attaches_minutes_fields(with_history):
    uni = {p["id"]: p for p in build_universe(6, _cfg())}
    nailed, rotation, fwd = uni[11], uni[12], uni[19]
    assert nailed["mm_share"] == 1.0
    assert abs(nailed["mm_rate90"] - 5.4) < 1e-12                # (32 + 22) / 10
    assert nailed["start_rate5"] == 1.0 and nailed["last_match"] == "started"
    assert abs(rotation["mm_share"] - 0.3) < 1e-12               # 135 min of 5 matches
    assert abs(rotation["mm_rate90"] - 4.0) < 1e-12              # (4 + 22) / 6.5
    assert abs(rotation["start_rate5"] - 0.2) < 1e-12 and rotation["last_match"] == "no_minutes"
    assert fwd["start_rate5"] == 1.0                             # 5 starts over 5 matches (capped)
    # everyone else has no history → v1 projection
    other = uni[1]
    assert other["mm_share"] is None and other["mm_rate90"] is None
    assert other["start_rate5"] is None and other["last_match"] is None and other["p_start"] is None


def test_rotation_player_projects_lower_with_history(with_history):
    cfg = ConfigFile()
    uni = {p["id"]: p for p in build_universe(6, cfg)}
    with_mm = ep_final(uni[12], [], cfg)
    cfg.optimizer.minutes_model.enabled = False
    assert with_mm < ep_final(uni[12], [], cfg) * 0.8             # a clear drop
    cfg.optimizer.minutes_model.enabled = True
    # a nailed starter whose FPL numbers agree with his history barely moves
    nailed = dict(uni[11], ep_next=5.6, points_per_game=5.4)
    off = ConfigFile()
    off.optimizer.minutes_model.enabled = False
    assert abs(ep_final(nailed, [], cfg) / ep_final(nailed, [], off) - 1) < 0.05


def test_build_universe_survives_missing_history_table(db_path):
    dbmod.execute("DROP TABLE player_gw_history")
    uni = build_universe(6, _cfg())
    assert uni and all(p["mm_share"] is None for p in uni)


# --- start_probability (T4) -------------------------------------------------------


def _prof(start_rate5, last):
    return mm.MinutesProfile(share=0.8, rate90=4.0, start_rate5=start_rate5,
                             last_match=last, matches=5)


@pytest.mark.parametrize("rate,bucket", [(0.0, 0), (0.19, 0), (0.2, 1), (0.4, 2), (0.6, 3),
                                          (0.8, 4), (1.0, 4)])
def test_start_probability_buckets(rate, bucket):
    for col, last in enumerate(("started", "came_on", "no_minutes")):
        assert mm.start_probability(pl(), _prof(rate, last)) == mm.START_TABLE[bucket][col]


def test_start_probability_documented_cells():
    p = pl()
    assert mm.start_probability(p, _prof(1.0, "started")) == 0.88
    assert mm.start_probability(p, _prof(0.0, "no_minutes")) == 0.14
    assert mm.start_probability(p, _prof(0.4, "came_on")) == 0.39


def test_start_probability_flag_factors():
    prof = _prof(1.0, "started")                                  # 0.88 unflagged
    assert abs(mm.start_probability(pl(chance_of_playing_next_round=99), prof) - 0.88 * 0.60) < 1e-12
    assert abs(mm.start_probability(pl(chance_of_playing_next_round=75), prof) - 0.88 * 0.60) < 1e-12
    assert abs(mm.start_probability(pl(chance_of_playing_next_round=74), prof) - 0.88 * 0.25) < 1e-12
    assert abs(mm.start_probability(pl(chance_of_playing_next_round=50), prof) - 0.88 * 0.25) < 1e-12
    assert abs(mm.start_probability(pl(chance_of_playing_next_round=25), prof) - 0.88 * 0.05) < 1e-12
    assert mm.start_probability(pl(chance_of_playing_next_round=24), prof) == 0.0
    assert mm.start_probability(pl(chance_of_playing_next_round=100), prof) == 0.88


def test_start_probability_none_and_zero_paths():
    prof = _prof(1.0, "started")
    assert mm.start_probability(pl(), None) is None               # no history
    assert mm.start_probability(pl(), _prof(None, "started")) is None   # no starts data
    assert mm.start_probability(pl(), mm.MinutesProfile(0.5, 4.0, 0.5, None, 3)) is None
    assert mm.start_probability(pl(status="u"), prof) == 0.0      # hard gates
    assert mm.start_probability(pl(status="s"), prof) == 0.0
    assert mm.start_probability(pl(can_select=0), prof) == 0.0
    assert mm.start_probability(pl(status="u"), None) == 0.0      # even without history
    assert mm.start_probability(pl(chance_of_playing_next_round=0), None) == 0.0


def test_build_universe_attaches_p_start(with_history):
    uni = {p["id"]: p for p in build_universe(6, _cfg())}
    assert uni[11]["p_start"] == 0.88                             # nailed, started last match
    assert uni[12]["p_start"] == mm.START_TABLE[1][2]             # 20% start rate, no minutes last
    assert uni[1]["p_start"] is None
