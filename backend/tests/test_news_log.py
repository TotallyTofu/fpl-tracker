"""v1.1 T6+: logging and scoring the news adjustment (does news help? does Reddit?).

Scorecard fixture, GW7 with final history; ep_nonews = projection without any news,
ep_final = with it:

    player  actual  ep_nonews  ep_final  chance  news
    1        4       6.0        4.5      None    reddit (neg)
    2        0       5.0        2.5      75      bbc (neg)
    3        7       5.0        5.5      None    reddit x2 (pos)
    4        3       3.0        3.0      None    none

any news (players 1-3): MAE 9/3 = 3.0 without vs 4.5/3 = 1.5 with; bias +1.67 vs +0.50;
  rank correlation 0.0 vs 1.0.   net neg (1, 2): MAE 3.5 vs 1.5.   net pos (3): 2.0 vs 1.5.
  flagged (2): 5.0 vs 2.5.
reddit (1, 3): ep_without 6.0 / 5.0 vs 4.5 / 5.5 -> MAE 2.0 vs 1.0.   bbc (2): 5.0 vs 2.5.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "scripts"))
import scorecard  # noqa: E402

from app import db as dbmod  # noqa: E402
from app.config import ConfigFile  # noqa: E402
from app.optimizer import projlog  # noqa: E402

BEFORE = "2026-09-26T12:00:00Z"        # the shared fixture's GW6 deadline is 2026-09-27T10:00:00Z


def _rows(gw=6):
    return {r["player_id"]: r for r in
            dbmod.query("SELECT * FROM projection_log WHERE gw = ? ORDER BY player_id", (gw,))}


def _signal(pid, source, sentiment, conf, category="injury"):
    dbmod.execute(
        "INSERT INTO signals (player_id, category, sentiment, confidence, summary, source, "
        "retrieved_at, expires_at) VALUES (?,?,?,?,'t',?,?,?)",
        (pid, category, sentiment, conf, source, BEFORE, "2999-01-01T00:00:00Z"))


# --- logging -------------------------------------------------------------------------------


def test_news_is_logged_per_source(db_path):
    _signal(11, "bbc:http://x/1", "negative", 0.4)          # this source alone: -0.5 x 0.4 = -0.2
    _signal(11, "reddit:abc", "negative", 0.4, "rotation")  # alone: -0.2
    _signal(11, "fpl-official", "negative", 1.0)            # never priced, never logged
    projlog.log_projections(ConfigFile(), now=BEFORE)
    r = _rows()[11]
    assert r["news_adj"] == pytest.approx(-0.4)
    assert r["ep_final"] == pytest.approx(r["ep_nonews"] * 0.6)
    detail = json.loads(r["news_detail"])
    assert set(detail) == {"bbc", "reddit"}                  # official news is not a source here
    assert detail["bbc"]["n"] == 1 and detail["bbc"]["adj"] == pytest.approx(-0.2)
    assert detail["bbc"]["signals"] == [["injury", "negative", 0.4]]
    assert detail["reddit"]["signals"] == [["rotation", "negative", 0.4]]
    # leave-one-out: without BBC only Reddit's -0.2 remains, and vice versa
    assert detail["bbc"]["ep_without"] == pytest.approx(r["ep_nonews"] * 0.8)
    assert detail["reddit"]["ep_without"] == pytest.approx(r["ep_nonews"] * 0.8)


def test_leave_one_out_respects_the_stacking_cap(db_path):
    """Two strong negatives would be -0.9 but cap at -0.6. Each source alone is -0.45,
    so removing one leaves the other's -0.45 (not -0.6 + 0.45)."""
    _signal(13, "bbc:1", "negative", 0.9)
    _signal(13, "reddit:2", "negative", 0.9)
    projlog.log_projections(ConfigFile(), now=BEFORE)
    r = _rows()[13]
    assert r["news_adj"] == pytest.approx(-0.6)
    detail = json.loads(r["news_detail"])
    assert detail["reddit"]["adj"] == pytest.approx(-0.45)
    assert detail["reddit"]["ep_without"] == pytest.approx(r["ep_nonews"] * 0.55)   # BBC alone


def test_positive_news_and_players_without_news(db_path):
    _signal(12, "youtube:v1", "positive", 0.9)               # +0.1 x 0.9 = +0.09
    projlog.log_projections(ConfigFile(), now=BEFORE)
    rows = _rows()
    assert rows[12]["news_adj"] == pytest.approx(0.09)
    assert json.loads(rows[12]["news_detail"])["youtube"]["adj"] == pytest.approx(0.09)
    quiet = rows[1]                                          # no signals at all
    assert quiet["news_adj"] == 0.0 and quiet["news_detail"] is None
    assert quiet["ep_nonews"] == pytest.approx(quiet["ep_final"])


def test_only_official_news_leaves_no_news_detail(db_path):
    _signal(11, "fpl-official", "negative", 1.0)
    projlog.log_projections(ConfigFile(), now=BEFORE)
    r = _rows()[11]
    assert r["news_detail"] is None and r["ep_nonews"] == pytest.approx(r["ep_final"])


def test_existing_projection_log_table_gains_the_news_columns(tmp_path):
    """The table shipped without the news columns; init_db adds them in place."""
    path = tmp_path / "old.db"
    dbmod.init_db(path)
    conn = dbmod.get_conn(path)
    conn.execute("DROP TABLE projection_log")
    conn.execute(OLD_LOG_TABLE)
    conn.execute("INSERT INTO projection_log (gw, player_id, ep_v10, ep_final, logged_at) "
                 "VALUES (6, 1, 3.0, 2.5, 'x')")
    conn.commit()
    conn.close()
    dbmod.init_db(path)
    conn = dbmod.get_conn(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(projection_log)")}
    row = dict(conn.execute("SELECT * FROM projection_log").fetchone())
    conn.close()
    assert {"ep_nonews", "news_adj", "news_detail"} <= cols
    assert row["ep_final"] == 2.5 and row["ep_nonews"] is None       # old row kept, new fields empty


OLD_LOG_TABLE = (
    "CREATE TABLE projection_log (gw INTEGER NOT NULL, player_id INTEGER NOT NULL, "
    "status TEXT, chance INTEGER, ep_next REAL, ep_v10 REAL, ep_final REAL, "
    "minutes_ep REAL, p_start REAL, logged_at TEXT NOT NULL, PRIMARY KEY (gw, player_id))")


# --- scorecard -----------------------------------------------------------------------------


@pytest.fixture()
def news_db(tmp_path):
    path = tmp_path / "news.db"
    dbmod.init_db(path)
    conn = sqlite3.connect(path)
    log = [  # player, chance, ep_nonews, ep_final, news_adj, news_detail
        (1, None, 6.0, 4.5, -0.25,
         '{"reddit":{"n":1,"adj":-0.25,"ep_without":6.0,"signals":[["injury","negative",0.5]]}}'),
        (2, 75, 5.0, 2.5, -0.5,
         '{"bbc":{"n":1,"adj":-0.5,"ep_without":5.0,"signals":[["injury","negative",1.0]]}}'),
        (3, None, 5.0, 5.5, 0.1,
         '{"reddit":{"n":2,"adj":0.1,"ep_without":5.0,'
         '"signals":[["return","positive",0.5],["other","positive",0.5]]}}'),
        (4, None, 3.0, 3.0, 0.0, None),
    ]
    conn.executemany(
        "INSERT INTO projection_log (gw, player_id, chance, ep_v10, ep_final, logged_at, ep_nonews, "
        "news_adj, news_detail) VALUES (7,?,?,?,?, 'x',?,?,?)",
        [(pid, ch, fin, fin, nonews, adj, det) for pid, ch, nonews, fin, adj, det in log])
    actual = {1: 4, 2: 0, 3: 7, 4: 3}
    conn.executemany(
        "INSERT INTO player_gw_history (player_id, gw, team_matches, minutes, starts, total_points, "
        "final, fetched_at) VALUES (?,7,1,90,1,?,1,'x')", list(actual.items()))
    conn.commit()
    conn.close()
    return path


def _news_section(capsys, path):
    assert scorecard.main(["--db", str(path), "--min-minutes", "0"]) == 0
    return capsys.readouterr().out.split("News: projection without news")[1]


def _line(section, label):
    return next(ln for ln in section.splitlines() if ln.strip().startswith(label)).split()


def test_news_table_all_sources(news_db, capsys):
    news = _news_section(capsys, news_db)
    any_news = _line(news, "any news")
    assert any_news[2:] == ["3", "3.000", "1.500", "-1.500", "+1.67", "+0.50", "0.000", "1.000"]
    neg = _line(news, "net neg")
    assert neg[2:5] == ["2", "3.500", "1.500"] and neg[-1] == "n/a"      # two rows: no rank correlation
    assert _line(news, "net pos")[2:5] == ["1", "2.000", "1.500"]
    assert _line(news, "flagged")[1:4] == ["1", "5.000", "2.500"]


def test_news_table_per_source_answers_does_reddit_help(news_db, capsys):
    by_source = _news_section(capsys, news_db).split("By source")[1]
    assert _line(by_source, "reddit")[1:4] == ["2", "2.000", "1.000"]    # 2.0 without vs 1.0 with: helps
    assert _line(by_source, "bbc")[1:4] == ["1", "5.000", "2.500"]
    assert "bbc 1 (1 players), reddit 3 (2 players)" in by_source


def test_scorecard_without_news_columns_still_runs(tmp_path, capsys):
    """A log written before news logging: no news columns, no crash."""
    path = tmp_path / "old.db"
    dbmod.init_db(path)
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE projection_log")
    conn.execute(OLD_LOG_TABLE)
    conn.execute("INSERT INTO projection_log (gw, player_id, ep_v10, ep_final, logged_at) "
                 "VALUES (7, 1, 3.0, 2.5, 'x')")
    conn.execute("INSERT INTO player_gw_history (player_id, gw, team_matches, minutes, starts, "
                 "total_points, final, fetched_at) VALUES (1, 7, 1, 90, 1, 2, 1, 'x')")
    conn.commit()
    conn.close()
    assert scorecard.main(["--db", str(path), "--min-minutes", "0"]) == 0
    out = capsys.readouterr().out
    assert "1 player-gameweeks" in out
    assert "no scored player had a news signal" in out
