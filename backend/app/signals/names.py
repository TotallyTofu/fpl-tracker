"""Paste-a-list name matching (PLAN-2 T1.9; M2 reuses for signal extraction).

Exact `web_name` → case-insensitive → known_name → Levenshtein ≤ 2 on surname.
Ambiguous (≥2 candidates within threshold) → all returned; no match → empty.
"""
from __future__ import annotations

import re

from ..db import query

_STOP = {"the", "de", "da", "del", "van", "von", "di", "la", "le", "do", "dos", "das"}


def _norm(s: str) -> str:
    s = re.sub(r"[^a-z ]", "", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _surname(s: str) -> str:
    parts = [p for p in _norm(s).split(" ") if p and p not in _STOP]
    return parts[-1] if parts else _norm(s)


def _lev(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _entry(r: dict, confidence: float) -> dict:
    return {
        "player_id": r["id"],
        "web_name": r["web_name"],
        "confidence": confidence,
        "element_type": r["element_type"],
        "team": r["team"],
        "team_name": r["team_name"],
        "now_cost": r["now_cost"],
        "status": r["status"],
        "can_select": r["can_select"],
        "ep_next": r["ep_next"],
        "selected_by_percent": r["selected_by_percent"],
        "chance_of_playing_next_round": r["chance_of_playing_next_round"],
    }


def match_names(names: list[str]) -> list[dict]:
    """Match a list of free-text names against the players table.

    Returns one entry per input name:
      {input, matched: MatchedPlayer | None, candidates: [MatchedPlayer, ...]}
    where MatchedPlayer = {player_id, web_name, confidence, element_type, team,
    team_name, now_cost, status, can_select, ep_next, selected_by_percent,
    chance_of_playing_next_round}.
    """
    rows = query(
        """SELECT p.id, p.web_name, p.known_name, p.first_name, p.second_name, p.element_type,
                  p.team, p.now_cost, p.status, p.can_select, p.ep_next, p.selected_by_percent,
                  p.chance_of_playing_next_round, t.name AS team_name
           FROM players p LEFT JOIN teams t ON t.id = p.team
           WHERE p.removed = 0"""
    )
    results = []
    for raw in names:
        raw = raw.strip()
        if not raw:
            continue
        n = _norm(raw)
        matched = None
        candidates: list[dict] = []

        # 1. exact web_name (case-insensitive)
        for r in rows:
            if _norm(r["web_name"]) == n:
                matched = _entry(r, 1.0)
                break
        # 2. known_name
        if not matched:
            for r in rows:
                if r.get("known_name") and _norm(r["known_name"]) == n:
                    matched = _entry(r, 0.95)
                    break
        # 3. Levenshtein ≤ 2 on surname (full-name distance as tiebreak)
        if not matched:
            target_surname = _surname(raw)
            for r in rows:
                d = _lev(target_surname, _surname(r["web_name"]))
                if d <= 2:
                    full_d = _lev(n, _norm(r["web_name"]))
                    candidates.append(
                        _entry(r, round(max(0.0, 1.0 - 0.1 * d - 0.01 * full_d), 3))
                    )
            candidates.sort(key=lambda c: (-c["confidence"], c["web_name"]))
            if len(candidates) == 1:
                matched = candidates[0]
            elif len(candidates) > 1:
                # only auto-match when the best is clearly ahead
                if candidates[0]["confidence"] - candidates[1]["confidence"] >= 0.15:
                    matched = candidates[0]

        results.append({"input": raw, "matched": matched, "candidates": candidates[:5]})
    return results