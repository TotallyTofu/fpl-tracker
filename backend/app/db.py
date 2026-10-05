"""SQLite layer: schema (PLAN.MD §7) + access helpers.

Per-call connections, WAL mode, foreign keys on. All timestamps are UTC strings
(YYYY-MM-DDTHH:MM:SSZ).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DB_PATH

log = logging.getLogger("fpl.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS players (
  id INTEGER PRIMARY KEY,
  web_name TEXT NOT NULL,
  known_name TEXT,
  first_name TEXT,
  second_name TEXT,
  element_type INTEGER NOT NULL,          -- 1 GK, 2 DEF, 3 MID, 4 FWD
  team INTEGER NOT NULL,
  team_code INTEGER,
  now_cost INTEGER NOT NULL,              -- tenths of a million (1050 = £10.5m)
  cost_change_start INTEGER,
  cost_change_event INTEGER,
  price_change_percent REAL,
  price_change_hourly_rate REAL,
  price_change_projections TEXT,          -- JSON list
  price_change_locked_until TEXT,
  total_points INTEGER,
  points_per_game REAL,
  form REAL,
  event_points INTEGER,
  minutes INTEGER,
  starts INTEGER,
  goals_scored INTEGER,
  assists INTEGER,
  clean_sheets INTEGER,
  goals_conceded INTEGER,
  own_goals INTEGER,
  penalties_saved INTEGER,
  penalties_missed INTEGER,
  yellow_cards INTEGER,
  red_cards INTEGER,
  saves INTEGER,
  bonus INTEGER,
  bps INTEGER,
  expected_goals REAL,
  expected_assists REAL,
  expected_goal_involvements REAL,
  expected_goals_per_90 REAL,
  expected_assists_per_90 REAL,
  expected_goal_involvements_per_90 REAL,
  expected_goals_conceded REAL,
  expected_goals_conceded_per_90 REAL,
  goals_conceded_per_90 REAL,
  saves_per_90 REAL,
  ep_next REAL,
  ep_this REAL,
  selected_by_percent REAL,
  transfers_in INTEGER,
  transfers_out INTEGER,
  transfers_in_event INTEGER,
  transfers_out_event INTEGER,
  influence REAL,
  creativity REAL,
  threat REAL,
  ict_index REAL,
  influence_per_90 REAL,
  creativity_per_90 REAL,
  threat_per_90 REAL,
  ict_index_per_90 REAL,
  influence_rank INTEGER,
  influence_rank_type INTEGER,
  creativity_rank INTEGER,
  creativity_rank_type INTEGER,
  threat_rank INTEGER,
  threat_rank_type INTEGER,
  ict_index_rank INTEGER,
  ict_index_rank_type INTEGER,
  clearances_blocks_interceptions INTEGER,
  recoveries INTEGER,
  tackles INTEGER,
  defensive_contribution INTEGER,
  status TEXT,                            -- a / d / u / s / i / n
  news TEXT,
  news_added TEXT,
  chance_of_playing_this_round INTEGER,   -- 0/50/100 or NULL
  chance_of_playing_next_round INTEGER,
  can_transact INTEGER,
  can_select INTEGER,
  removed INTEGER,
  penalties_order INTEGER,
  penalties_text TEXT,
  corners_and_indirect_freekicks_order INTEGER,
  corners_and_indirect_freekicks_text TEXT,
  direct_freekicks_order INTEGER,
  direct_freekicks_text TEXT,
  value_form REAL,
  value_season REAL,
  dreamteam_count INTEGER,
  in_dreamteam INTEGER,
  now_cost_rank INTEGER,
  opta_code INTEGER,
  region INTEGER,
  birth_date TEXT,
  team_join_date TEXT,
  raw_json TEXT,
  fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_players_team ON players(team);
CREATE INDEX IF NOT EXISTS idx_players_type ON players(element_type);
CREATE INDEX IF NOT EXISTS idx_players_status ON players(status);

CREATE TABLE IF NOT EXISTS teams (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  short_name TEXT,
  code INTEGER,
  fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fixtures (
  id INTEGER PRIMARY KEY,
  event INTEGER NOT NULL,
  home_team INTEGER NOT NULL,
  away_team INTEGER NOT NULL,
  kickoff_time TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'not_started',  -- not_started | in_play | finished
  minutes INTEGER,
  score_home INTEGER,
  score_away INTEGER,
  difficulty_home INTEGER,                      -- 1 easy .. 3 hard (home team's view)
  difficulty_away INTEGER,
  raw_json TEXT,
  fetched_at TEXT NOT NULL,
  UNIQUE(event, home_team, away_team)
);
CREATE INDEX IF NOT EXISTS idx_fixtures_event ON fixtures(event);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  name TEXT,
  deadline_time TEXT NOT NULL,
  average_entry_score REAL,
  highest_score INTEGER,
  highest_scoring_player INTEGER,
  is_current INTEGER NOT NULL DEFAULT 0,
  is_next INTEGER NOT NULL DEFAULT 0,
  finished INTEGER NOT NULL DEFAULT 0,
  data_checked INTEGER NOT NULL DEFAULT 0,
  released INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chips (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  number INTEGER,
  start_event INTEGER NOT NULL,
  stop_event INTEGER NOT NULL,
  chip_type TEXT,
  set_index INTEGER NOT NULL DEFAULT 1,
  raw_json TEXT,
  fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lineups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  transfer_bank INTEGER NOT NULL DEFAULT 1,
  chips TEXT NOT NULL DEFAULT '{}',            -- JSON {chip_name: sets_remaining}
  is_current INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL DEFAULT 'current',        -- T4.3: current | test
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lineup_players (
  lineup_id INTEGER NOT NULL REFERENCES lineups(id) ON DELETE CASCADE,
  player_id INTEGER NOT NULL REFERENCES players(id),
  role TEXT NOT NULL,                          -- starter | bench
  bench_order INTEGER,                         -- 1..4, NULL for starters
  is_captain INTEGER NOT NULL DEFAULT 0,
  is_vice_captain INTEGER NOT NULL DEFAULT 0,
  bought_cost INTEGER,                         -- snapshot of now_cost at add time
  PRIMARY KEY (lineup_id, player_id)
);

CREATE TABLE IF NOT EXISTS suggestions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lineup_id INTEGER REFERENCES lineups(id) ON DELETE SET NULL,
  profile TEXT NOT NULL,                       -- max_ep | differential | safe
  variant_of TEXT,
  generated_at TEXT NOT NULL,
  target_gw INTEGER NOT NULL,
  projected_points TEXT NOT NULL,              -- JSON {baseline, adjusted, with_captain}
  objective REAL,
  diff TEXT NOT NULL,                          -- JSON (see PLAN.MD §8.6)
  chip_advice TEXT NOT NULL,                   -- JSON list
  rationale TEXT NOT NULL,                     -- JSON {per_player: {}, notes: []}
  raw_lineup TEXT NOT NULL,                    -- JSON full lineup (audit snapshot)
  applied_at TEXT                              -- T4.2: set when the user marks it applied
);

CREATE TABLE IF NOT EXISTS raw_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,                        -- fpl-official | bbc | espn | reddit | youtube
  external_id TEXT NOT NULL,
  kind TEXT NOT NULL,                          -- article | thread | video | official-news
  title TEXT,
  url TEXT,
  published_at TEXT,
  body TEXT,
  content_hash TEXT NOT NULL,                  -- sha256(source|title|body[:2000])
  takeaways TEXT,                              -- JSON list of one-line takeaways
  processed INTEGER NOT NULL DEFAULT 0,        -- 1 = signal extraction completed
  extract_attempts INTEGER NOT NULL DEFAULT 0, -- FIX N10/N13: LLM/transcript retry counter
  retrieved_at TEXT NOT NULL,
  UNIQUE(source, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_items_source ON raw_items(source, retrieved_at);
CREATE INDEX IF NOT EXISTS idx_raw_items_pending ON raw_items(processed, retrieved_at);

CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  player_id INTEGER NOT NULL,
  category TEXT NOT NULL,                      -- injury | suspension | selection | rotation | return | transfer | other
  sentiment TEXT NOT NULL,                     -- negative | positive | neutral
  confidence REAL NOT NULL,                    -- 0.0-1.0
  summary TEXT NOT NULL,                       -- one-line takeaway
  source TEXT NOT NULL,                        -- fpl-official | bbc:<url> | espn:<id> | reddit:<thread_id> | youtube:<video_id>
  url TEXT,
  published_at TEXT,
  retrieved_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,                    -- TTL (default 72 h; official = until replaced)
  raw_item_id INTEGER,
  model TEXT NOT NULL DEFAULT 'rules'          -- 'rules' | 'llm:{model}' (provenance)
);
CREATE INDEX IF NOT EXISTS idx_signals_player ON signals(player_id, expires_at);

CREATE TABLE IF NOT EXISTS official_news_cache (
  player_id INTEGER PRIMARY KEY REFERENCES players(id),
  status TEXT,
  news TEXT,
  chance INTEGER,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS live_matches (
  fixture_id INTEGER PRIMARY KEY REFERENCES fixtures(id),
  event INTEGER NOT NULL,
  kickoff_time TEXT NOT NULL,
  status TEXT NOT NULL,                        -- pre | in | post
  minute INTEGER,
  score_home INTEGER,
  score_away INTEGER,
  home_name TEXT,
  away_name TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS live_player_points (
  player_id INTEGER PRIMARY KEY REFERENCES players(id),
  points INTEGER NOT NULL DEFAULT 0,
  bonus INTEGER,
  minutes INTEGER,
  started INTEGER,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chip_plays_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lineup_id INTEGER REFERENCES lineups(id) ON DELETE SET NULL,
  gw INTEGER NOT NULL,
  chip TEXT NOT NULL,
  played_at TEXT NOT NULL,
  UNIQUE(gw, chip)
);

CREATE TABLE IF NOT EXISTS poll_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  status TEXT NOT NULL,                        -- ok | error
  rows INTEGER,
  error TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL
);
"""

# --- players -----------------------------------------------------------------

# (db column, json key) — identical names unless the API drifted (D18: parse defensively).
PLAYER_FIELDS: list[tuple[str, str]] = [
    ("web_name", "web_name"),
    ("known_name", "known_name"),
    ("first_name", "first_name"),
    ("second_name", "second_name"),
    ("element_type", "element_type"),
    ("team", "team"),
    ("team_code", "team_code"),
    ("now_cost", "now_cost"),
    ("cost_change_start", "cost_change_start"),
    ("cost_change_event", "cost_change_event"),
    ("price_change_percent", "price_change_percent"),
    ("price_change_hourly_rate", "price_change_hourly_rate"),
    ("price_change_projections", "price_change_projections"),
    ("price_change_locked_until", "price_change_locked_until"),
    ("total_points", "total_points"),
    ("points_per_game", "points_per_game"),
    ("form", "form"),
    ("event_points", "event_points"),
    ("minutes", "minutes"),
    ("starts", "starts"),
    ("goals_scored", "goals_scored"),
    ("assists", "assists"),
    ("clean_sheets", "clean_sheets"),
    ("goals_conceded", "goals_conceded"),
    ("own_goals", "own_goals"),
    ("penalties_saved", "penalties_saved"),
    ("penalties_missed", "penalties_missed"),
    ("yellow_cards", "yellow_cards"),
    ("red_cards", "red_cards"),
    ("saves", "saves"),
    ("bonus", "bonus"),
    ("bps", "bps"),
    ("expected_goals", "expected_goals"),
    ("expected_assists", "expected_assists"),
    ("expected_goal_involvements", "expected_goal_involvements"),
    ("expected_goals_per_90", "expected_goals_per_90"),
    ("expected_assists_per_90", "expected_assists_per_90"),
    ("expected_goal_involvements_per_90", "expected_goal_involvements_per_90"),
    ("expected_goals_conceded", "expected_goals_conceded"),
    ("expected_goals_conceded_per_90", "expected_goals_conceded_per_90"),
    ("goals_conceded_per_90", "goals_conceded_per_90"),
    ("saves_per_90", "saves_per_90"),
    ("ep_next", "ep_next"),
    ("ep_this", "ep_this"),
    ("selected_by_percent", "selected_by_percent"),
    ("transfers_in", "transfers_in"),
    ("transfers_out", "transfers_out"),
    ("transfers_in_event", "transfers_in_event"),
    ("transfers_out_event", "transfers_out_event"),
    ("influence", "influence"),
    ("creativity", "creativity"),
    ("threat", "threat"),
    ("ict_index", "ict_index"),
    ("influence_per_90", "influence_per_90"),
    ("creativity_per_90", "creativity_per_90"),
    ("threat_per_90", "threat_per_90"),
    ("ict_index_per_90", "ict_index_per_90"),
    ("influence_rank", "influence_rank"),
    ("influence_rank_type", "influence_rank_type"),
    ("creativity_rank", "creativity_rank"),
    ("creativity_rank_type", "creativity_rank_type"),
    ("threat_rank", "threat_rank"),
    ("threat_rank_type", "threat_rank_type"),
    ("ict_index_rank", "ict_index_rank"),
    ("ict_index_rank_type", "ict_index_rank_type"),
    ("clearances_blocks_interceptions", "clearances_blocks_interceptions"),
    ("recoveries", "recoveries"),
    ("tackles", "tackles"),
    ("defensive_contribution", "defensive_contribution"),
    ("status", "status"),
    ("news", "news"),
    ("news_added", "news_added"),
    ("chance_of_playing_this_round", "chance_of_playing_this_round"),
    ("chance_of_playing_next_round", "chance_of_playing_next_round"),
    ("can_transact", "can_transact"),
    ("can_select", "can_select"),
    ("removed", "removed"),
    ("penalties_order", "penalties_order"),
    ("penalties_text", "penalties_text"),
    ("corners_and_indirect_freekicks_order", "corners_and_indirect_freekicks_order"),
    ("corners_and_indirect_freekicks_text", "corners_and_indirect_freekicks_text"),
    ("direct_freekicks_order", "direct_freekicks_order"),
    ("direct_freekicks_text", "direct_freekicks_text"),
    ("value_form", "value_form"),
    ("value_season", "value_season"),
    ("dreamteam_count", "dreamteam_count"),
    ("in_dreamteam", "in_dreamteam"),
    ("now_cost_rank", "now_cost_rank"),
    ("opta_code", "opta_code"),
    ("region", "region"),
    ("birth_date", "birth_date"),
    ("team_join_date", "team_join_date"),
]

_COLS = [c for c, _ in PLAYER_FIELDS]


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def init_db(path: str | Path | None = None) -> None:
    p = Path(path) if path else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        # M2 migration: signals / official_news_cache were pre-shaped in M1 with a
        # different column set. Both are empty before M2 (no writer existed), so a
        # drop-and-recreate to the PLAN.MD §7 shape is safe.
        sig = _table_columns(conn, "signals")
        if sig and not {"summary", "retrieved_at", "expires_at"} <= sig:
            conn.execute("DROP TABLE signals")
        cache = _table_columns(conn, "official_news_cache")
        if cache and not {"chance", "updated_at"} <= cache:
            conn.execute("DROP TABLE official_news_cache")
        # M4 migration: suggestions.applied_at (additive, safe on existing data)
        sugg = _table_columns(conn, "suggestions")
        if sugg and "applied_at" not in sugg:
            conn.execute("ALTER TABLE suggestions ADD COLUMN applied_at TEXT")
        # M4 migration: lineups.kind (additive; existing rows default to 'current')
        lup = _table_columns(conn, "lineups")
        if lup and "kind" not in lup:
            conn.execute("ALTER TABLE lineups ADD COLUMN kind TEXT NOT NULL DEFAULT 'current'")
        # FIX N10 migration: raw_items.extract_attempts (additive, retry counter)
        ri = _table_columns(conn, "raw_items")
        if ri and "extract_attempts" not in ri:
            conn.execute("ALTER TABLE raw_items ADD COLUMN extract_attempts INTEGER NOT NULL DEFAULT 0")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def get_conn(path: str | Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path else DB_PATH
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def query(sql: str, params: tuple | list = ()) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def query_one(sql: str, params: tuple | list = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple | list = ()) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def execute_many(sql: str, seq: list[tuple]) -> None:
    conn = get_conn()
    try:
        conn.executemany(sql, seq)
        conn.commit()
    finally:
        conn.close()


def executemany(sql: str, seq: list[tuple]) -> None:
    return execute_many(sql, seq)


# --- upserts ------------------------------------------------------------------


def upsert_players(elements: list[dict]) -> int:
    """Insert/update players from bootstrap elements. Returns row count."""
    ts = now_utc()
    rows = []
    for el in elements:
        pid = el.get("id")
        if pid is None:
            continue
        row = {"id": pid, "fetched_at": ts, "raw_json": json.dumps(el)}
        for col, key in PLAYER_FIELDS:
            v = el.get(key)
            if col == "price_change_projections" and v is not None:
                v = json.dumps(v)
            row[col] = v
        rows.append(row)
    cols = ["id", "fetched_at", "raw_json"] + _COLS
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "id")
    sql = (
        f"INSERT INTO players ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}"
    )
    conn = get_conn()
    try:
        conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def upsert_teams(teams: list[dict]) -> int:
    ts = now_utc()
    sql = (
        "INSERT INTO teams (id, name, short_name, code, fetched_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, short_name=excluded.short_name, "
        "code=excluded.code, fetched_at=excluded.fetched_at"
    )
    rows = [
        (t.get("id"), t.get("name"), t.get("short_name"), t.get("code"), ts)
        for t in teams
        if t.get("id") is not None
    ]
    execute_many(sql, rows)
    return len(rows)


def upsert_fixtures(rows: list[dict]) -> int:
    """Base fixture rows from /api/fixtures/ (no difficulty)."""
    ts = now_utc()
    sql = (
        "INSERT INTO fixtures (id, event, home_team, away_team, kickoff_time, status, minutes, "
        "score_home, score_away, raw_json, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(event, home_team, away_team) DO UPDATE SET "
        "id=excluded.id, kickoff_time=excluded.kickoff_time, status=excluded.status, "
        "minutes=excluded.minutes, score_home=excluded.score_home, score_away=excluded.score_away, "
        "raw_json=excluded.raw_json, fetched_at=excluded.fetched_at"
    )
    full = [
        (
            r["id"], r["event"], r["home_team"], r["away_team"], r["kickoff_time"],
            r.get("status") or "not_started", r.get("minutes"), r.get("score_home"),
            r.get("score_away"), r.get("raw_json"), ts,
        )
        for r in rows
        if r.get("id") is not None
    ]
    execute_many(sql, full)
    return len(full)


def merge_fixture_difficulties(fixtures: list[dict]) -> int:
    """Merge per-player fixture difficulty (element-summary) into fixtures by fixture id."""
    n = 0
    for fx in fixtures:
        fid = fx.get("id")
        diff = fx.get("difficulty")
        if fid is None or diff is None:
            continue
        if fx.get("is_home"):
            execute("UPDATE fixtures SET difficulty_home = ? WHERE id = ?", (diff, fid))
        else:
            execute("UPDATE fixtures SET difficulty_away = ? WHERE id = ?", (diff, fid))
        n += 1
    return n


def upsert_events(events: list[dict]) -> int:
    rows = []
    for e in events:
        eid = e.get("id")
        if eid is None:
            continue
        rows.append(
            (
                eid, e.get("name"), e.get("deadline_time"), e.get("average_entry_score"),
                e.get("highest_score"), e.get("highest_scoring_player"),
                1 if e.get("is_current") else 0, 1 if e.get("is_next") else 0,
                1 if e.get("finished") else 0, 1 if e.get("data_checked") else 0,
                1 if e.get("released") else 0,
            )
        )
    sql = (
        "INSERT INTO events (id, name, deadline_time, average_entry_score, highest_score, "
        "highest_scoring_player, is_current, is_next, finished, data_checked, released) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, deadline_time=excluded.deadline_time, "
        "average_entry_score=excluded.average_entry_score, highest_score=excluded.highest_score, "
        "highest_scoring_player=excluded.highest_scoring_player, is_current=excluded.is_current, "
        "is_next=excluded.is_next, finished=excluded.finished, data_checked=excluded.data_checked, "
        "released=excluded.released"
    )
    execute_many(sql, rows)
    return len(rows)


# FPL's bootstrap-static chips[] uses "3xc"; the app's UI/config/advice all use
# "triple_captain". Normalise on ingest so `chips.name` is always the app's
# spelling — `raw_json` keeps the upstream value for forensics.
CHIP_NAME_MAP = {"3xc": "triple_captain", "3x_captain": "triple_captain",
                 "free_hit": "freehit", "bench_boost": "bboost",
                 "wildcard": "wildcard", "freehit": "freehit", "bboost": "bboost"}


def upsert_chips(chips: list[dict]) -> int:
    ts = now_utc()
    rows = []
    seen: dict[str, int] = {}
    for c in chips:
        raw_name = c.get("name")
        if not raw_name:
            continue
        name = CHIP_NAME_MAP.get(raw_name, raw_name)
        if name != raw_name:
            log.warning("chips[] name drift: %r → %r", raw_name, name)
        seen[name] = seen.get(name, 0) + 1
        rows.append(
            (
                c.get("id"), name, c.get("number"), c.get("start_event"), c.get("stop_event"),
                c.get("chip_type"), seen[name], json.dumps(c), ts,
            )
        )
    sql = (
        "INSERT INTO chips (id, name, number, start_event, stop_event, chip_type, set_index, raw_json, fetched_at) "
        "VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, number=excluded.number, "
        "start_event=excluded.start_event, stop_event=excluded.stop_event, "
        "chip_type=excluded.chip_type, set_index=excluded.set_index, raw_json=excluded.raw_json, "
        "fetched_at=excluded.fetched_at"
    )
    execute_many(sql, rows)
    return len(rows)


# --- poll log ------------------------------------------------------------------


def log_poll(source: str, status: str, rows: int | None = None, error: str | None = None,
             started_at: str | None = None) -> None:
    execute(
        "INSERT INTO poll_log (source, status, rows, error, started_at, finished_at) VALUES (?,?,?,?,?,?)",
        (source, status, rows, error, started_at or now_utc(), now_utc()),
    )


def recent_polls(limit: int = 50) -> list[dict]:
    return query("SELECT * FROM poll_log ORDER BY id DESC LIMIT ?", (limit,))


# --- meta / season rollover ------------------------------------------------------


def get_meta(key: str) -> str | None:
    row = query_one("SELECT value FROM meta WHERE key = ?", (key,))
    return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    execute(
        "INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def check_season_rollover(season_marker: str) -> bool:
    """Wipe season-scoped tables if the season marker changed. Returns True if wiped.

    PRAGMA foreign_keys=ON → children MUST be deleted before their parents
    (lineup_players/live_matches/live_player_points/official_news_cache all
    reference players or fixtures).
    """
    stored = get_meta("season")
    if stored and stored == season_marker:
        return False
    conn = get_conn()
    try:
        for table in (
            # children first
            "lineup_players", "suggestions", "live_matches", "live_player_points",
            "official_news_cache", "signals", "raw_items",
            # season-scoped logs: a stale GW row would keep the Free-Hit ban alive
            # into the new season and UNIQUE(gw, chip) would block re-logging.
            "chip_plays_log",
            # parents
            "players", "fixtures", "events", "chips",
            # lineups last (lineup_players/suggestions already gone)
            "lineups",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM meta WHERE key = 'season'")
        conn.commit()
    finally:
        conn.close()
    set_meta("season", season_marker)
    return True