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

# A19: single-token name patterns that are ordinary English words (measured
# against the live 2026/27 pool — 26 of the surname-tier tokens). A scan hit on
# one of these counts only when the name is used as a PROPER NOUN in the raw
# text ("Pope out for a month" yes, "a white shirt" no). A same-sentence club /
# "Premier League" anchor is required on top of that only when 2+ players share
# the word ("James" → Reece James / Daniel James), where the anchor is what
# disambiguates — a uniquely-owned surname needs no club mention, because every
# source in this app is already PL/FPL-specific and dropping those hits cost
# real signals ("Pope out for a month with a knee injury" → no match).
_COMMON_WORDS = frozenset({
    "alex", "barnes", "burns", "cash", "cook", "dunk", "forster", "giles",
    "gray", "hall", "hill", "hume", "jordan", "keane", "lewis", "moore",
    "murphy", "pope", "reed", "scott", "shaw", "taylor", "thompson",
    "watson", "white", "wood",
})

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
}

# A8: patterns whose target is NOT in this season's pool (verified
# 2026-10-02 against the live data/fpl.db: no Salah, no Leeds United).
# Clearly labelled and kept out of the scan index — they were inert anyway
# (name_index now logs any live-pattern entry whose target is missing or
# ambiguous), but this block makes the mechanism visible instead of letting
# dead entries masquerade as live coverage. backend/scripts/validate_aliases.py
# re-checks these against the pool.
DORMANT_PATTERNS: dict[str, str] = {
    "leads united": "Leeds United",  # team name — excluded from the player index
    "leedz": "Leeds United",
    "mo salah": "Salah",
    "mohamed salah": "Salah",
}

# FIX N6: community nicknames → canonical web_name, folded into the scan
# index exactly like MISSPELLINGS (score 0.9). Aliases go stale season to
# season — each entry below was validated 2026-09-21 against the live
# players table (backend/scripts/validate_aliases.py): the target web_name
# exists and is unambiguous, and the alias string collides with no other
# player's name pattern. Entries whose target left the pool live in
# DORMANT_PATTERNS (A8).
ALIASES: dict[str, str] = {
    "mo caicedo": "Caicedo",
    "big gabriel": "Gabriel",
    "gabi jesus": "G.Jesus",
    "gabby jesus": "G.Jesus",
    "micky van de ven": "Van de Ven",
    "victor gyokeres": "Gyokeres",
    "ollie watkins": "Watkins",
}


# Letters that are distinct characters (not NFKD-decomposable accents) must be
# TRANSLITERATED, not deleted — `re.sub(r"[^a-z ]", "", ...)` turns "Groß" into
# the unusable 3-char stub "gro" and "Ødegaard" into "degaard".
_FOLD = {
    "ß": "ss", "æ": "ae", "œ": "oe", "ø": "o", "đ": "d", "ð": "d", "þ": "th",
    "ł": "l", "ı": "i", "ħ": "h", "ŋ": "n", "ĸ": "k", "ƒ": "f",
    "Æ": "AE", "Œ": "OE", "Ø": "O", "Đ": "D", "Ð": "D", "Þ": "TH",
    "Ł": "L", "İ": "I", "Ŋ": "N",
}


def _norm_scan(s: str) -> str:
    """The ONE normaliser for both index patterns and scanned text.

    NFKD + strip combining marks (é→e), fold non-decomposable letters
    (ß→ss, ø→o), lowercase, then drop every remaining non-[a-z ] char so
    hyphens/periods/apostrophes vanish from BOTH sides
    ('calvert-lewin' → 'calvertlewin', "o'riley" → 'oriley').
    Whitespace (incl. newlines) is collapsed to single spaces BEFORE the
    strip — deleting a newline outright would fuse the names on adjacent
    lines ('…One\\nDef A One…' → '…onedef a one…') and break the scan.
    """
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = "".join(_FOLD.get(c, c) for c in s)
    s = s.lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^a-z ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _norm(s: str) -> str:
    return _norm_scan(s)


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


def _flat(s: str) -> str:
    """Lowercase, de-accented, punctuation-stripped, single-spaced.

    Alias of ``_norm_scan`` (A2): the ONE normaliser for both index patterns
    and scanned text. Kept under this name so ``name_index``, ``resolve_name``
    and ``scripts/validate_aliases.py`` keep working.
    """
    return _norm_scan(s)


def _surname_tokens(s: str) -> list[str]:
    """Every non-stop token of a name (used by ``resolve_name``'s input side)."""
    return [t for t in _flat(s).split(" ") if t and t not in _STOP]


def _surname_token(s: str) -> str:
    """The SURNAME token of a name — the last non-stop token.

    A19.3 (rev 2): ``_surname_tokens`` returns *every* token, so building the
    surname tier from it put first names in too — ``known_name="Alex Tóth"``
    contributed ``"alex"``, which made "Alex is a doubt for the weekend"
    resolve to Tóth.A. Only the final token is a surname.
    """
    toks = _surname_tokens(s)
    return toks[-1] if toks else ""


def _load_player_rows() -> list[dict]:
    return query(
        """SELECT p.id, p.web_name, p.known_name, p.first_name, p.second_name,
                  p.team, t.name AS team_name
           FROM players p LEFT JOIN teams t ON t.id = p.team
           WHERE p.removed = 0
            ORDER BY p.id"""
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
    # A9.1: flat name → [ids]. The old comment said "longest wins" but the
    # code was setdefault (first row wins) — and it silently picked an
    # arbitrary player for the 20 web_names shared by 2+ players. Now every
    # owner is kept; >1 owner means the token is ambiguous.
    full: dict[str, list[int]] = {}
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
            if pid not in full.setdefault(f, []):  # A9.1: list, not scalar
                full[f].append(pid)
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
        # A19.3: surname tokens only (the last token of web_name / known_name) —
        # see _surname_token. Deduped because both forms usually share the surname.
        for tok in {_surname_token(web), _surname_token(known)}:
            # dedupe: the same player may contribute a token twice
            # (web_name + known_name) — one player per token occurrence
            if len(tok) >= 3 and pid not in by_surname.setdefault(tok, []):
                by_surname[tok].append(pid)

    # misspelling/alias lookups (RAW flat keys — re.escape would escape the
# spaces and the dict lookup in resolve_candidates would always miss) and
# their escaped forms for the scan regex.
    missp: list[tuple[str, int]] = []
    missp_patterns: list[str] = []
    # A8 (rev 2 note): log targets that are dead (not in the pool) or
    # ambiguous instead of silently dropping them.
    for bad, good in {**MISSPELLINGS, **ALIASES}.items():
        fb = _flat(bad)
        fg = _flat(good)
        if not fb:
            continue
        pids = full.get(fg) or []
        if len(pids) == 1:
            missp.append((fb, pids[0]))
            missp_patterns.append(re.escape(fb))
        elif len(pids) > 1:
            log.warning("name pattern %r → %r is ambiguous (%s) — not folded in",
                        bad, good, pids)
        else:
            log.debug("name pattern %r → %r: target not in the pool (dormant)",
                      bad, good)
    # FIX N6: unambiguous surname tokens (exactly one player) join the scan
    # regex at score 0.8 — mirrors resolve_name's surname tier; multi-player
    # surnames stay excluded (ambiguity guard).
    surnames: dict[str, int] = {}
    for tok, pids in by_surname.items():
        if len(pids) == 1:
            surnames[tok] = next(iter(pids))

    combined = sorted(set(patterns) | set(missp_patterns) | set(surnames),
                      key=len, reverse=True)
    regex = re.compile(r"\b(" + "|".join(combined) + r")\b") if combined else None

    # A19: same-sentence anchors for bare common-word names — any club in the
    # pool plus "Premier League" (normalised, word-boundary matched).
    team_anchors = sorted(
        {_norm_scan(t) for t in (i["team_name"] for i in info.values())
         if t and _norm_scan(t)}
    )
    if "premier league" not in team_anchors:
        team_anchors.append("premier league")

    return {
        "by_web": by_web,
        "by_known": by_known,
        "by_firstlast": by_firstlast,
        "by_lastfirst": by_lastfirst,
        "by_surname": by_surname,
        "full": full,
        # A9.1: flat names shared by 2+ players — ambiguous, never guess.
        "ambiguous": {k for k, v in full.items() if len(v) > 1},
        "info": info,
        "missp": dict(missp),
        "surnames": surnames,
        "team_anchors": team_anchors,
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
        for full_name, pids in idx["full"].items():  # A9.1: list-valued
            d = _lev(n, full_name)
            if d <= 2:
                score = max(0.0, 1.0 - d / max(len(n), len(full_name)))
                for pid in pids:
                    found[pid] = max(found.get(pid, 0.0), score)
    if not found:
        for tok in _surname_tokens(name):
            if len(tok) < 4:
                continue
            for pid in idx["by_surname"].get(tok, []):
                found[pid] = max(found.get(pid, 0.0), 0.8)
    return sorted(((pid, round(s, 3)) for pid, s in found.items() if s >= 0.6),
                  key=lambda x: (-x[1], x[0]))


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def _proper_noun(text: str, token: str) -> bool:
    """A19 (rev 2): does ``token`` occur in ``text`` as a proper noun?

    Proper-noun use = the occurrence is not entirely lowercase, so "Pope",
    "POPE" (all-caps headline) and "McWhite" count, while "the white shirt"
    does not. ``token`` is the normalised (lowercase, accent-folded) form, so
    a case-insensitive search with a per-occurrence case test is exact.
    """
    for m in re.finditer(rf"\b{re.escape(token)}\b", text or "", re.I):
        if not m.group(0).islower():
            return True
    return False


def _sentence_anchored(text: str, token: str, anchors: list[str]) -> bool:
    """A19: does some sentence contain BOTH a capitalised ``token`` AND a
    club name / "Premier League" anchor? (Only required for a token shared by
    2+ players, where the anchor is what disambiguates.)"""
    cap = re.compile(rf"\b{re.escape(token.title())}\b")
    for sent in _SENT_SPLIT.split(text):
        if not cap.search(sent):
            continue
        ns = _norm_scan(sent)
        if any(re.search(rf"\b{re.escape(a)}\b", ns) for a in anchors):
            return True
    return False


def _disambiguate_full(norm: str, m: re.Match, pids: list[int], idx: dict) -> int | None:
    """A9.1: a flat name shared by 2+ players. Resolve only when the
    surrounding text disambiguates — a neighbouring first name, the full
    'first last' form, or a club that belongs to exactly one candidate.
    Otherwise: no match, never guess (the old code picked the first row)."""
    info = idx["info"]
    # 1. the word immediately before the hit is a candidate's first name
    prev = norm[: m.start()].split()
    if prev:
        hits = [p for p in pids
                if _flat(info[p]["first"]) and _flat(info[p]["first"]) == prev[-1]]
        if len(hits) == 1:
            return hits[0]
    # 2. the full 'first last' form appears in the text
    hits = []
    for p in pids:
        ff = _flat(f"{info[p]['first']} {info[p]['last']}".strip())
        if ff and " " in ff and re.search(rf"\b{re.escape(ff)}\b", norm):
            hits.append(p)
    if len(hits) == 1:
        return hits[0]
    # 3. a club name in the text that exactly one candidate plays for
    for team in {info[p]["team_name"] for p in pids}:
        if not team:
            continue
        if re.search(rf"\b{re.escape(_norm_scan(team))}\b", norm):
            owners = [p for p in pids if info[p]["team_name"] == team]
            if len(owners) == 1:
                return owners[0]
    return None


def resolve_candidates(text: str, idx: dict) -> list[dict]:
    """Scan text for player-name occurrences (word-boundary, case/de-accent-insensitive).

    Returns [{name_in_text, player_id, score, span}] sorted by span. Misspelling
    hits score 0.9; canonical-name hits score 1.0. Team-name-only matches are
    excluded (the index contains players only).

    A2: matching runs on ``_norm_scan(text)`` (hyphens/periods/apostrophes and
    accents folded on BOTH sides), so ``span`` holds offsets into the NORMALISED
    string, not the raw text. ``name_in_text`` is therefore the matched
    player's canonical ``web_name`` (recovered from the index) — the raw
    substring cannot be sliced reliably from normalised offsets.

    A19: a bare common-word name (``_COMMON_WORDS``) must be used as a proper
    noun in the raw text (capitalised, or ALL-CAPS in a headline) — lowercase
    prose never matches — and a same-sentence club / "Premier League" anchor is
    additionally required only when 2+ players share the word (``full[token]``
    has >1 owner), where the anchor is what disambiguates. The whole-text==token
    carve-out keeps bare-name input working (the §0.2 self-consistency probe).
    A9.1: a flat name shared by 2+ players resolves only when the surrounding
    text disambiguates (``_disambiguate_full``); otherwise it is a no-match.
    """
    if not text or idx["regex"] is None:
        return []
    out: list[dict] = []
    surnames = idx.get("surnames") or {}
    anchors = idx.get("team_anchors") or []
    norm = _norm_scan(text)
    for m in idx["regex"].finditer(norm):
        token = m.group(1)
        if " " not in token and token in _COMMON_WORDS and norm != token:
            # A19 rev 2: a common English word must be used as a PROPER NOUN
            # ("Pope out for a month" counts, "the white shirt" does not).
            # A same-sentence club / "Premier League" anchor is required only
            # when 2+ players share the word — there the anchor is what
            # disambiguates, and in a PL/FPL-only corpus a uniquely-owned
            # surname needs no club mention to be the right player.
            if not _proper_noun(text, token):
                continue
            if (len(idx["full"].get(token) or []) > 1
                    and not _sentence_anchored(text, token, anchors)):
                continue
        pid = idx["missp"].get(token)
        score = 0.9 if pid is not None else 1.0
        if pid is None:
            pids = idx["full"].get(token) or []  # A9.1: list-valued
            if len(pids) == 1:
                pid = pids[0]
            elif len(pids) > 1:
                pid = _disambiguate_full(norm, m, pids, idx)
        if pid is None:
            # FIX N6: unambiguous-surname tier (single-player surnames only)
            pid = surnames.get(token)
            score = 0.8
            if pid is None:
                continue
        elif token in surnames and surnames[token] != pid:
            # surname tier disagrees with a canonical/misspelling hit → ambiguous
            continue
        out.append(
            {
                "name_in_text": idx["info"][pid]["web_name"],
                "player_id": pid,
                "score": score,
                "span": (m.start(), m.end()),
            }
        )
    out.sort(key=lambda c: c["span"][0])
    return out