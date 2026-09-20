"""EP model (PLAN-2 T1.6; M2 activates A(p)/S(p) fully per PLAN-3 T2.10).

M1 behaviour: A(p) is pass-through except hard gates (u/s/can_select=0 → 0.0);
S(p) is 0.0 (no signals). The code paths for doubt/chance maps and signal
stacking exist and are config-gated (optimizer.availability.active).
"""
from __future__ import annotations


def ep_baseline(p: dict) -> float:
    v = p.get("ep_next")
    return float(v) if v is not None else 0.0


def availability_multiplier(p: dict, cfg) -> float:
    """A(p): 0 for hard gates; M2 map when cfg.optimizer.availability.active."""
    if p.get("status") in ("u", "s") or p.get("can_select") == 0:
        return 0.0
    av = cfg.optimizer.availability
    if not av.active:
        return 1.0
    a = av.doubt if p.get("status") == "d" else 1.0
    chance = p.get("chance_of_playing_next_round")
    if chance is None:
        a *= av.chance_null
    elif chance == 100:
        a *= av.chance_100
    elif chance == 50:
        a *= av.chance_50
    elif chance == 0:
        a *= av.chance_0
    return a


def signal_adjustment(p: dict, signals: list[dict], cfg) -> float:
    """S(p): negative −0.5×conf each (stack cap −0.6); positive +0.1×conf each (cap +0.2)."""
    sc = cfg.optimizer.signal
    neg = sum(float(s["confidence"]) for s in signals if s["sentiment"] == "negative")
    pos = sum(float(s["confidence"]) for s in signals if s["sentiment"] == "positive")
    s_neg = max(sc.neg_per * neg, sc.neg_cap)  # neg_per is negative
    s_pos = min(sc.pos_per * pos, sc.pos_cap)
    return s_neg + s_pos


def _form_adj(p: dict) -> float:
    form = p.get("form")
    if form is None:
        return 0.0
    return max(0.0, min(12.0, float(form))) * 0.9


def _fixture_adj(p: dict, difficulty: int | None) -> float:
    if difficulty is None:
        return 0.0
    if p["element_type"] in (1, 2):
        return (3 - difficulty) * 0.5
    return (3 - difficulty) * 0.25


def ep_final(p: dict, signals: list[dict], cfg, fixture_difficulty: int | None = None) -> float:
    """w·[EP_next·A(p)·(1+S(p))] + w·form_adj + w·fixture_adj"""
    w = cfg.optimizer.weights
    base = ep_baseline(p) * availability_multiplier(p, cfg) * (1 + signal_adjustment(p, signals, cfg))
    return (
        w.ep * base
        + w.form * _form_adj(p)
        + w.fixture * _fixture_adj(p, fixture_difficulty)
    )


def score_lineup(squad: list[dict], xi: list[dict], captain_id: int, vc_id: int,
                 signals_by_player: dict[int, list[dict]], cfg,
                 difficulty_by_player: dict[int, int | None] | None = None) -> dict:
    """Projected points for a lineup. VC gets NO multiplier (2026/27 rule, V5)."""
    diff_map = difficulty_by_player or {}
    cap = next((p for p in squad if p["id"] == captain_id), None)
    baseline = sum(ep_baseline(p) for p in xi) + (ep_baseline(cap) if cap else 0.0)
    adjusted = sum(
        ep_final(p, signals_by_player.get(p["id"], []), cfg, diff_map.get(p["id"]))
        for p in xi
    )
    if cap is not None:
        adjusted += ep_final(cap, signals_by_player.get(cap["id"], []), cfg,
                             diff_map.get(cap["id"]))
    return {
        "baseline": round(baseline, 1),
        "adjusted": round(adjusted, 1),
        "with_captain": round(adjusted, 1),
    }