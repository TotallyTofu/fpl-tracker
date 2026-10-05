"""Projected-points model (v1.0).

    ep_final = blend × fixture_mult × (1 + S)

    blend        = (w.ep · ep_next  +  w.form · ppg · chance · n_fixtures) / (w.ep + w.form)
    fixture_mult = 1 + w.fixture · k_pos · (3 − difficulty)   (averaged over the GW's fixtures)
    S            = news-signal adjustment (official FPL news excluded, see below)

Why this shape (verified against the live API on 2026-10-05):

- FPL's ``ep_next`` is the 30-day form average × chance of playing: 615 of
  667 players have ``ep_next == form``, and Rice (form 4.0, 75%) has 3.0.
  Availability is therefore already priced in. The old model multiplied it
  in again (doubt × chance bucket) and then once more through the official
  news signal, so a 75% player was projected at ~30% of his form.
- Blending ``ep_next`` with ``form`` blended a number with itself. The
  season points-per-game average is the independent, steadier estimate.
- Fixture difficulty used to move a player by at most ±0.15 points; it is
  now a multiplier (±24% for GK/DEF, ±15% for MID/FWD at difficulty 1 vs 5
  with the default weight), and the weights no longer shrink every
  projection by ~16%.

``availability_multiplier`` still exists: it drives the ``safe`` profile's
reliability weight and the hard gates (u/s/can_select=0 → 0).
"""
from __future__ import annotations

# Fixture sensitivity per position: clean sheets make GK/DEF swing more.
_FIXTURE_K = {1: 0.8, 2: 0.8, 3: 0.5, 4: 0.5}

# Signals with this source are FPL's own news/status — already inside ep_next.
OFFICIAL_SOURCE = "fpl-official"


def ep_baseline(p: dict) -> float:
    v = p.get("ep_next")
    return float(v) if v is not None else 0.0


def _hard_gated(p: dict) -> bool:
    return p.get("status") in ("u", "s") or p.get("can_select") == 0


def availability_multiplier(p: dict, cfg) -> float:
    """A(p) for the safe profile's reliability: 0 for hard gates, else the
    doubt/chance map when ``optimizer.availability.active``."""
    if _hard_gated(p):
        return 0.0
    av = cfg.optimizer.availability
    if not av.active:
        return 1.0
    a = av.doubt if p.get("status") == "d" else 1.0
    chance = p.get("chance_of_playing_next_round")
    # FIX T2: threshold buckets, not exact matches — FPL also emits 25/75 and
    # intermediate values. getattr fallbacks keep hand-built test cfgs working.
    if chance is None:
        a *= av.chance_null
    elif chance == 100:
        a *= av.chance_100
    elif chance >= 75:
        a *= getattr(av, "chance_75", 0.85)
    elif chance >= 50:
        a *= av.chance_50
    elif chance >= 25:
        a *= getattr(av, "chance_25", 0.35)
    else:                      # 0 < chance < 25 — effectively out
        a *= av.chance_0
    return a


def pricing_signals(signals: list[dict]) -> list[dict]:
    """Signals that may move projected points: everything except FPL's own
    news, which ep_next already reflects."""
    return [s for s in signals if not str(s.get("source") or "").startswith(OFFICIAL_SOURCE)]


def signal_adjustment(p: dict, signals: list[dict], cfg) -> float:
    """S(p): negative −0.5×conf each (stack cap −0.6); positive +0.1×conf each (cap +0.2).
    Official FPL news is skipped (already priced into ep_next)."""
    sc = cfg.optimizer.signal
    sigs = pricing_signals(signals)
    neg = sum(float(s["confidence"]) for s in sigs if s["sentiment"] == "negative")
    pos = sum(float(s["confidence"]) for s in sigs if s["sentiment"] == "positive")
    s_neg = max(sc.neg_per * neg, sc.neg_cap)  # neg_per is negative
    s_pos = min(sc.pos_per * pos, sc.pos_cap)
    return s_neg + s_pos


def _difficulties(fixture_difficulty) -> list[int] | None:
    """Normalise the fixture argument: None = no fixture data (neutral),
    [] = blank gameweek, int or list = the GW's difficulties."""
    if fixture_difficulty is None:
        return None
    if isinstance(fixture_difficulty, (list, tuple)):
        return [int(d) for d in fixture_difficulty if d is not None]
    return [int(fixture_difficulty)]


def fixture_multiplier(p: dict, fixture_difficulty, cfg) -> float:
    diffs = _difficulties(fixture_difficulty)
    if not diffs:
        return 1.0
    k = _FIXTURE_K.get(p.get("element_type"), 0.5)
    w = cfg.optimizer.weights.fixture
    return sum(1 + w * k * (3 - d) for d in diffs) / len(diffs)


def _chance(p: dict) -> float:
    c = p.get("chance_of_playing_next_round")
    return 1.0 if c is None else max(0.0, min(100.0, float(c))) / 100.0


def ep_final(p: dict, signals: list[dict], cfg, fixture_difficulty=None,
             use_ep_next: bool = True) -> float:
    """Projected points for one player in the target GW (see module doc).

    ``use_ep_next=False`` is for gameweeks after the next one: FPL only
    publishes expected points for the next GW, so later GWs use the season
    average alone.
    """
    if _hard_gated(p):
        return 0.0
    diffs = _difficulties(fixture_difficulty)
    if diffs is not None and not diffs:
        return 0.0                              # blank gameweek
    n_fix = len(diffs) if diffs else 1
    w = cfg.optimizer.weights
    ppg_term = float(p.get("points_per_game") or p.get("form") or 0.0) * _chance(p) * n_fix
    if use_ep_next:
        w_ep, w_form = w.ep, w.form
        total = w_ep + w_form
        blend = ((w_ep * ep_baseline(p) + w_form * ppg_term) / total) if total > 0 else ep_baseline(p)
    else:
        blend = ppg_term
    return blend * fixture_multiplier(p, diffs, cfg) * (1 + signal_adjustment(p, signals, cfg))


def score_lineup(squad: list[dict], xi: list[dict], captain_id: int, vc_id: int,
                 signals_by_player: dict[int, list[dict]], cfg,
                 difficulty_by_player: dict | None = None,
                 chip: str | None = None, use_ep_next: bool = True) -> dict:
    """Projected points for a lineup. VC gets NO multiplier (2026/27 rule).

    ``chip``: "bboost" adds the bench, "triple_captain" counts the captain
    three times instead of twice.
    """
    diff_map = difficulty_by_player or {}
    cap = next((p for p in squad if p["id"] == captain_id), None)
    cap_mult = 3 if chip == "triple_captain" else 2
    counted = list(xi)
    if chip == "bboost":
        xi_ids = {p["id"] for p in xi}
        counted += [p for p in squad if p["id"] not in xi_ids]

    def fin(p: dict) -> float:
        return ep_final(p, signals_by_player.get(p["id"], []), cfg, diff_map.get(p["id"]),
                        use_ep_next=use_ep_next)

    baseline = sum(ep_baseline(p) for p in counted)
    adjusted = sum(fin(p) for p in counted)
    if cap is not None:
        baseline += (cap_mult - 1) * ep_baseline(cap)
        adjusted += (cap_mult - 1) * fin(cap)
    # with_captain is identical to adjusted (kept for API compatibility).
    return {
        "baseline": round(baseline, 1),
        "adjusted": round(adjusted, 1),
        "with_captain": round(adjusted, 1),
    }
