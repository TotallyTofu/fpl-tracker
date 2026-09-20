"""Paste-a-list name matching (PLAN-2 T1.9) + extraction-grade helpers (M2 T2.6).

Exact `web_name` → case-insensitive → known_name → Levenshtein ≤ 2 on surname.
Ambiguous (≥2 candidates within threshold) → all returned; no match → empty.

M2 (T2.6): ``name_index`` / ``resolve_name`` / ``resolve_candidates`` for the
signal extractors, tolerant of auto-caption mis-transcriptions (V9). Unmatched
names are logged, never guessed (rule-based signals for unmatched names are
dropped).
"""
from __future__ import annotations

import logging
import re
import unicodedata

from ..db import query

log = logging.getLogger("fpl.signals.names")

_STOP = {"the", "de", "da", "del", "van", "von", "di", "la", "le", "do", "dos", "das"}

# Known auto-caption / typo patterns (V9) → canonical player name (or team name,
# which must NOT resolve to a player). Extendable.
MISSPELLINGS: dict[str, str] = {
    "calbertt lewing": "Calvert-Lewin",
    "calbert lewin": "Calvert-Lewin",
    "calvert lewin": "Calvert-Lewin",
    "lewis calvert": "Calvert-Lewin",
    "joao pedro": "João Pedro",
    "joao pedro": "João Pedro",
    "tarkowski": "Tarkowski",
    "de cuyer": "De Cuyper",
    "de cuyper": "De Cuyper",
    "maitland niles": "Maitland-Niles",
    "mautland niles": "Maitland-Niles",
    "verbruggen": "Verbruggen",
    "tzolakis": "Tzolakis",
    "tsolakis": "Tzolakis",
    "belloumi": "Belloumi",
    "janelt": "Janelt",
    "groß": "Groß",
    "gros": "Groß",
    "groth": "Groß",
    "emersonn": "Emersonn",
    "saka": "Saka",
    "barnes": "Barnes",
    "isak": "Isak",
    "rogers": "Rogers",
    "tavener": "Tavernier",
    "tavernier": "Tavernier",
    "schade": "Schade",
    "haverts": "Havertz",
    "kai havertz": "Havertz",
    "k havertz": "Havertz",
    "leads united": "Leeds United",  # team name — excluded from the player index
    "leedz": "Leeds United",
}


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


# --- M2 T2.6: extraction-grade helpers ----------------------------------------


def _deaccent(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _flat(s: str) -> str:
    """Lowercase, de-accented, punctuation-stripped, single-spaced."""
    s = _deaccent(s).lower()
    s = re.sub(r"[^a-z ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _surname_tokens(s: str) -> list[str]:
    return [t for t in _flat(s).split(" ") if t and t not in _STOP]


def _load_player_rows() -> list[dict]:
    return query(
        """SELECT p.id, p.web_name, p.known_name, p.first_name, p.second_name,
                  p.team, t.name AS team_name
           FROM players p LEFT JOIN teams t ON t.id = p.team
           WHERE p.removed = 0"""
    )


def name_index(players: list[dict]) -> dict:
    """Build the extraction index (T2.6).

    players: rows with id, web_name, known_name, first_name, second_name (team_name optional).
    Returns a dict of lookup maps + a precompiled word-boundary regex (web_name,
    known_name, 'first last', 'last first' variants, de-accented) plus the
    misspelling patterns.
    """
    by_web: dict[str, list[int]] = {}
    by_known: dict[str, list[int]] = {}
    by_firstlast: dict[str, list[int]] = {}
    by_lastfirst: dict[str, list[int]] = {}
    by_surname: dict[str, list[int]] = {}
    full: dict[str, int] = {}  # flat name → id (longest wins)
    info: dict[int, dict] = {}
    patterns: list[str] = []

    for r in players:
        pid = r["id"]
        web = r.get("web_name") or ""
        known = r.get("known_name") or ""
        first = (r.get("first_name") or "").strip()
        last = (r.get("second_name") or "").strip()
        info[pid] = {
            "web_name": web,
            "known_name": known,
            "first": first,
            "last": last,
            "team_name": r.get("team_name") or "",
        }
        for name in {web, known, f"{first} {last}".strip(), f"{last} {first}".strip()}:
            f = _flat(name)
            if not f:
                continue
            full.setdefault(f, pid)
            if f not in patterns:
                patterns.append(re.escape(f))
        fw = _flat(web)
        if fw:
            by_web.setdefault(fw, []).append(pid)
        fk = _flat(known)
        if fk:
            by_known.setdefault(fk, []).append(pid)
        ff = _flat(f"{first} {last}".strip())
        if ff:
            by_firstlast.setdefault(ff, []).append(pid)
        fl = _flat(f"{last} {first}".strip())
        if fl:
            by_lastfirst.setdefault(fl, []).append(pid)
        for tok in _surname_tokens(web) + _surname_tokens(known):
            if len(tok) >= 3:
                by_surname.setdefault(tok, []).append(pid)

    # misspelling patterns (de-accented, escaped) that map to a known player
    missp: list[tuple[str, int]] = []
    for bad, good in MISSPELLINGS.items():
        fb = _flat(bad)
        fg = _flat(good)
        pid = full.get(fg)
        if pid is not None and fb:
            missp.append((re.escape(fb), pid))

    combined = sorted(set(patterns) | {p for p, _ in missp}, key=len, reverse=True)
    regex = re.compile(r"\b(" + "|".join(combined) + r")\b") if combined else None

    return {
        "by_web": by_web,
        "by_known": by_known,
        "by_firstlast": by_firstlast,
        "by_lastfirst": by_lastfirst,
        "by_surname": by_surname,
        "full": full,
        "info": info,
        "missp": dict(missp),
        "regex": regex,
    }


_index_cache: dict = {"key": None, "idx": None}


def get_name_index() -> dict:
    """Index for the current season, cached and rebuilt when players.fetched_at moves."""
    try:
        r = query("SELECT MAX(fetched_at) AS k FROM players")
        key = r[0]["k"] if r else None
    except Exception:
        key = None
    if _index_cache["key"] == key and _index_cache["idx"] is not None:
        return _index_cache["idx"]
    idx = name_index(_load_player_rows())
    _index_cache["key"] = key
    _index_cache["idx"] = idx
    return idx


def resolve_name(name: str, idx: dict) -> list[tuple[int, float]]:
    """Resolve one name against the index (T2.6).

    1) exact web_name/known_name/first-last/last-first (de-accented) → 1.0
    2) Levenshtein ≤ 2 on full names → 1 - dist/max_len
    3) surname substring (≥ 4 chars) → 0.8
    Returns all candidates with score ≥ 0.6, sorted desc by score.
    """
    n = _flat(name)
    if not n:
        return []
    found: dict[int, float] = {}
    for table in (idx["by_web"], idx["by_known"], idx["by_firstlast"], idx["by_lastfirst"]):
        for pid in table.get(n, []):
            found[pid] = max(found.get(pid, 0.0), 1.0)
    if not found:
        for full_name, pid in idx["full"].items():
            d = _lev(n, full_name)
            if d <= 2:
                score = max(0.0, 1.0 - d / max(len(n), len(full_name)))
                found[pid] = max(found.get(pid, 0.0), score)
    if not found:
        for tok in _surname_tokens(name):
            if len(tok) < 4:
                continue
            for pid in idx["by_surname"].get(tok, []):
                found[pid] = max(found.get(pid, 0.0), 0.8)
    return sorted(((pid, round(s, 3)) for pid, s in found.items() if s >= 0.6),
                  key=lambda x: (-x[1], x[0]))


def resolve_candidates(text: str, idx: dict) -> list[dict]:
    """Scan text for player-name occurrences (word-boundary, case/de-accent-insensitive).

    Returns [{name_in_text, player_id, score, span}] sorted by span. Misspelling
    hits score 0.9; canonical-name hits score 1.0. Team-name-only matches are
    excluded (the index contains players only).
    """
    if not text or idx["regex"] is None:
        return []
    out: list[dict] = []
    for m in idx["regex"].finditer(_deaccent(text.lower())):
        token = m.group(1)
        pid = idx["missp"].get(token)
        score = 0.9 if pid is not None else 1.0
        if pid is None:
            pid = idx["full"].get(token)
            if pid is None:
                continue
        out.append(
            {
                "name_in_text": text[m.start() : m.end()],
                "player_id": pid,
                "score": score,
                "span": (m.start(), m.end()),
            }
        )
    out.sort(key=lambda c: c["span"][0])
    return out