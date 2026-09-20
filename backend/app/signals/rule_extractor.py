"""Rule-based signal extraction (PLAN-3 T2.7) — the LLM-off fallback (D5).

Two inputs:
(a) raw-item text via a keyword dictionary, scoped to sentences that also
    contain an EXACTLY resolved player name (conservative — no fuzzy, V9);
(b) official FPL news/status changes from the bootstrap fetch (confidence 1.0).

Rule signals never exceed confidence 0.6. Items older than 7 days are skipped.
YouTube items use title + description only on the rule path (auto-caption
noise is LLM-only material).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from ..db import execute_many, now_utc, query
from . import names as names_mod

log = logging.getLogger("fpl.signals.rules")

CATEGORIES = ("injury", "suspension", "selection", "rotation", "return", "transfer", "other")
SENTIMENTS = ("negative", "positive", "neutral")

# category → [(pattern (case-insensitive regex), sentiment, confidence)]
KEYWORDS: dict[str, list[tuple[str, str, float]]] = {
    "injury": [
        (r"\b(injur\w+|hamstring|ankle|knee|calf|thigh|quad|groin|ruled out|out for \d+|surgery|rehab)\b",
         "negative", 0.4),
        (r"\b(doubt|uncertain|question mark|50-50|chance of playing)\b", "negative", 0.35),
    ],
    "suspension": [
        (r"\b(suspend\w+|sent off|red card|serving a (ban|bans?)\b|banned)\b", "negative", 0.5),
    ],
    "selection": [
        (r"\b(confirmed to start|expected to start|to start|in the (starting )?(lineup|xi)|starts? (for|vs|against))\b",
         "positive", 0.5),
        (r"\b(bench\w*|left out|out of the (squad|team)|rest\w*|rotat\w*)\b", "negative", 0.4),
    ],
    "return": [
        (r"\b(back (in|for)|return\w*|available again|fit to play|cleared)\b", "positive", 0.5),
    ],
    "transfer": [
        (r"\b(transfer\w*|signing|joined|permanent deal|loan\w*|released)\b", "neutral", 0.5),
    ],
}
_COMPILED = {cat: [(re.compile(p, re.IGNORECASE), s, c) for p, s, c in pats]
             for cat, pats in KEYWORDS.items()}

MAX_RULE_CONFIDENCE = 0.6
MAX_ITEM_AGE_DAYS = 7
_SUMMARY_LIMIT = 200


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    return [p.strip() for p in parts if p.strip()]


def item_age_days(published_at: str | None, now: datetime | None = None) -> float | None:
    if not published_at:
        return None
    try:
        dt = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - dt).total_seconds() / 86400.0


def extract_signals_rule(item: dict, idx: dict) -> list[dict]:
    """Rule extraction for one raw item (T2.7a).

    item: raw_items row (dict) with title, body, source, url, published_at, id.
    idx:  name index from names.get_name_index().
    Returns signal dicts ready for store.save_signals (model='rules').
    """
    src = item.get("source") or ""
    title = item.get("title") or ""
    body = item.get("body") or ""
    # YouTube: rule path on title + description only — strip any transcript part.
    if src == "youtube" and body:
        body = body.split("--- TRANSCRIPT ---")[0]
    age = item_age_days(item.get("published_at"))
    if age is not None and age > MAX_ITEM_AGE_DAYS:
        return []

    text = f"{title}\n{body}"
    found = names_mod.resolve_candidates(text, idx)
    if not found:
        return []
    by_player: dict[int, int] = {}
    for c in found:
        by_player[c["player_id"]] = max(by_player.get(c["player_id"], 0), int(c["score"] * 100))

    signals: list[dict] = []
    for sent in _sentences(text):
        sent_flat = names_mod._flat(sent)
        # exact-match only: a sentence counts for a player when a canonical
        # occurrence (score 1.0) or a misspelling occurrence (0.9) is inside it
        players_in_sent: set[int] = set()
        for c in found:
            s, e = c["span"]
            if s < len(text) and text[s:e] in sent or sent_flat and names_mod._flat(text[s:e]) in sent_flat:
                players_in_sent.add(c["player_id"])
        if not players_in_sent:
            continue
        for cat, pats in _COMPILED.items():
            best = None
            for rx, sentiment, conf in pats:
                if rx.search(sent):
                    if best is None or conf > best[1]:
                        best = (sentiment, conf)
            if best is None:
                continue
            sentiment, conf = best
            conf = min(conf, MAX_RULE_CONFIDENCE)
            summary = sent[:_SUMMARY_LIMIT]
            for pid in sorted(players_in_sent):
                signals.append(
                    {
                        "player_id": pid,
                        "category": cat,
                        "sentiment": sentiment,
                        "confidence": conf,
                        "summary": summary,
                        "source": _source_ref(item),
                        "url": item.get("url"),
                        "published_at": item.get("published_at"),
                        "raw_item_id": item.get("id"),
                        "model": "rules",
                    }
                )
    # one signal per (player, category, item) — keep max confidence
    dedup: dict[tuple[int, str], dict] = {}
    for s in signals:
        k = (s["player_id"], s["category"])
        if k not in dedup or s["confidence"] > dedup[k]["confidence"]:
            dedup[k] = s
    return list(dedup.values())


def _source_ref(item: dict) -> str:
    src = item.get("source") or "unknown"
    ext = item.get("external_id") or item.get("url") or ""
    return f"{src}:{ext}" if ext else src


# --- (b) official FPL news ------------------------------------------------------


def _snapshot(players: list[dict]) -> dict[int, dict]:
    return {
        p["id"]: {
            "status": p.get("status"),
            "news": p.get("news"),
            "chance": p.get("chance_of_playing_next_round"),
        }
        for p in players
    }


def ingest_official_news(players: list[dict]) -> list[dict]:
    """Diff official FPL status/news/chance against the last-seen snapshot (T2.7b).

    players: current bootstrap rows (id, status, news, news_added,
    chance_of_playing_next_round). Emits signals for changed players, then
    refreshes official_news_cache. Source 'fpl-official', confidence 1.0 for
    hard states.
    """
    prev_rows = query("SELECT * FROM official_news_cache")
    prev = {r["player_id"]: r for r in prev_rows}
    ts = now_utc()

    # Cold start: seed the cache without emitting (every player would otherwise
    # "change" at once and flood the signals UI with first-run noise).
    if not prev:
        rows = [
            (p["id"], p.get("status"), p.get("news"), p.get("chance_of_playing_next_round"), ts)
            for p in players
        ]
        if rows:
            execute_many(
                """INSERT INTO official_news_cache (player_id, status, news, chance, updated_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(player_id) DO UPDATE SET
                     status=excluded.status, news=excluded.news,
                     chance=excluded.chance, updated_at=excluded.updated_at""",
                rows,
            )
        return []

    signals: list[dict] = []

    for p in players:
        pid = p["id"]
        status = p.get("status")
        news = p.get("news") or ""
        chance = p.get("chance_of_playing_next_round")
        pub = p.get("news_added") or ts
        old = prev.get(pid)
        changed = old is None or (
            old.get("status") != status or old.get("news") != news or old.get("chance") != chance
        )
        if not changed:
            continue

        def emit(category: str, sentiment: str, confidence: float, summary: str) -> None:
            signals.append(
                {
                    "player_id": pid,
                    "category": category,
                    "sentiment": sentiment,
                    "confidence": confidence,
                    "summary": (summary or news or "Status change (official FPL)")[:_SUMMARY_LIMIT],
                    "source": "fpl-official",
                    "url": None,
                    "published_at": pub,
                    "raw_item_id": None,
                    "model": "rules",
                }
            )

        old_status = old.get("status") if old else None
        if status in ("i", "s", "u"):
            cat = "injury" if status == "i" else ("suspension" if status == "s" else "selection")
            emit(cat, "negative", 1.0, news)
        elif status == "d":
            emit("selection", "negative", 0.7, news or "A doubt for selection")
        elif status in (None, "a") and old_status in ("i", "s", "u"):
            emit("return", "positive", 0.9, news or "Back to the squad / available")
        if "doubt" in news.lower():
            emit("selection", "negative", 0.7, news)
        if chance == 0:
            emit("selection", "negative", 0.8, news or "0% chance of playing next round")
        elif chance == 50:
            emit("selection", "negative", 0.5, news or "50% chance of playing next round")
        elif chance == 100 and (old is None or (old.get("chance") or 0) < 100):
            emit("selection", "positive", 0.6, news or "100% chance of playing next round")

    # refresh the snapshot cache (only changed rows are written)
    rows = []
    for p in players:
        pid = p["id"]
        if pid in prev:
            r = prev[pid]
            if (
                r.get("status") == p.get("status")
                and r.get("news") == p.get("news")
                and r.get("chance") == p.get("chance_of_playing_next_round")
            ):
                continue
        rows.append(
            (pid, p.get("status"), p.get("news"), p.get("chance_of_playing_next_round"), ts)
        )
    if rows:
        execute_many(
            """INSERT INTO official_news_cache (player_id, status, news, chance, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(player_id) DO UPDATE SET
                 status=excluded.status, news=excluded.news,
                 chance=excluded.chance, updated_at=excluded.updated_at""",
            rows,
        )
    return signals