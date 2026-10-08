"""Projection log (v1.1): what the app projected before each deadline.

After every FPL refresh, while the next gameweek's deadline has not passed, the
projection for every eligible player is written to ``projection_log`` (the last
pre-deadline write wins; the rows freeze when ``next_gw`` advances). Each row
holds the projection the app uses (``ep_final``) next to the v1.0 formula
(``ep_v10``: the old linear flag curve, minutes model off), so
``scripts/scorecard.py`` can compare them with what players actually scored —
re-checking the calibration on this season's data instead of trusting 2025-26
forever.

News is logged too, because nobody has measured whether it helps (the 2025-26
backtest had no usable news). Per player: ``ep_nonews`` (the same projection with
every news signal removed), ``news_adj`` (the total adjustment, the ``S`` in
``blend × fixture × (1 + S)``) and ``news_detail``, a JSON object per source
(``bbc``, ``espn``, ``reddit``, ``youtube``: how many signals, what that source
alone would add, the signals' category / sentiment / confidence, and
``ep_without``: the projection with that one source removed). Official FPL news
is already inside ``ep_next`` and never priced, so it is not logged here. The
scorecard turns this into "does news help?" and "does Reddit help?" tables.
"""
from __future__ import annotations

import json
import logging

from ..db import get_conn, now_utc, query_one
from ..signals.store import signals_by_player
from .scoring import _difficulties, ep_final, minutes_ep, pricing_signals, signal_adjustment
from .solver import _difficulty_map, _team_fixtures, build_universe

log = logging.getLogger("fpl.projlog")


def v10_config(cfg):
    """A copy of ``cfg`` that reproduces the v1.0 projection: FPL's linear flag
    scaling (identical for 75/50/25 flags) and no minutes model."""
    old = cfg.model_copy(deep=True)
    curve = old.optimizer.availability_curve
    curve.play_75, curve.play_50, curve.play_25 = 0.75, 0.50, 0.25
    old.optimizer.minutes_model.enabled = False
    return old


def source_of(signal: dict) -> str:
    """'reddit:abc123' -> 'reddit' (the news source a signal came from)."""
    return str(signal.get("source") or "").split(":")[0] or "unknown"


def news_breakdown(p: dict, sigs: list[dict], cfg, fx) -> tuple[float, float, str | None]:
    """(ep_nonews, news_adj, news_detail JSON or None) for one player.

    Only signals that move the projection count (official FPL news does not).
    ``ep_without`` per source is a leave-one-out projection, because the stacking
    caps make the sources' own adjustments non-additive."""
    priced = pricing_signals(sigs)
    ep_nonews = ep_final(p, [], cfg, fx)
    adj = signal_adjustment(p, priced, cfg) if priced else 0.0
    if not priced:
        return ep_nonews, adj, None
    detail: dict[str, dict] = {}
    for src in sorted({source_of(s) for s in priced}):
        mine = [s for s in priced if source_of(s) == src]
        detail[src] = {
            "n": len(mine),
            "adj": round(signal_adjustment(p, mine, cfg), 4),
            "ep_without": ep_final(p, [s for s in priced if source_of(s) != src], cfg, fx),
            "signals": [[s.get("category"), s.get("sentiment"), s.get("confidence")] for s in mine],
        }
    return ep_nonews, adj, json.dumps(detail, separators=(",", ":"))


def log_projections(cfg=None, now: str | None = None) -> int:
    """Write the projection log for the next gameweek. Returns the rows written,
    or 0 when there is no next gameweek or its deadline has passed."""
    nxt = query_one("SELECT id, deadline_time FROM events WHERE is_next = 1")
    if not nxt or not nxt["deadline_time"]:
        return 0
    ts = now or now_utc()
    if ts >= nxt["deadline_time"]:                 # both 'YYYY-MM-DDTHH:MM:SSZ'
        return 0
    gw = nxt["id"]
    if cfg is None:
        from ..config import load_settings

        cfg = load_settings().config
    old_cfg = v10_config(cfg)

    diff_map = _difficulty_map(gw)
    signals = signals_by_player()
    rows = []
    for p in build_universe(gw, cfg):
        fx = _team_fixtures(diff_map, p["team"])
        sigs = signals.get(p["id"], [])
        n_fix = len(_difficulties(fx) or []) or 1
        m_ep = minutes_ep(p, cfg, n_fix)
        ep_nonews, news_adj, news_detail = news_breakdown(p, sigs, cfg, fx)
        rows.append((
            gw, p["id"], p.get("status"), p.get("chance_of_playing_next_round"), p.get("ep_next"),
            ep_final(p, sigs, old_cfg, fx), ep_final(p, sigs, cfg, fx),
            m_ep, p.get("p_start"), ts, ep_nonews, news_adj, news_detail,
        ))
    conn = get_conn()
    try:
        # replace the whole gameweek, so a player who has since become
        # unavailable does not keep a stale projection
        conn.execute("DELETE FROM projection_log WHERE gw = ?", (gw,))
        conn.executemany(
            "INSERT INTO projection_log (gw, player_id, status, chance, ep_next, ep_v10, ep_final, "
            "minutes_ep, p_start, logged_at, ep_nonews, news_adj, news_detail) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
    finally:
        conn.close()
    return len(rows)
