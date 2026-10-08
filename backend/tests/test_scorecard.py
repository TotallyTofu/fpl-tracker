"""v1.1 T6: scripts/scorecard.py on a tiny synthetic database.

GW7 (final history), four players that played:

    player  ep_v10  ep_final  actual  chance  p_start  started
    1        5.0     5.5       6       None    0.90     yes
    2        3.0     1.0       0       75      0.15     no
    3        7.0     7.5       8       None    0.85     yes
    4        1.0     2.4       2       None    0.55     no

MAE v1.0 = (1 + 3 + 1 + 1) / 4 = 1.5     MAE now = (.5 + 1 + .5 + .4) / 4 = 0.6
bias v1.0 = (-1 + 3 - 1 - 1) / 4 = 0      bias now = (-.5 + 1 - .5 + .4) / 4 = 0.1
Spearman v1.0 = 1 - 6·2/(4·15) = 0.8      Spearman now = 1.0 (same order as the actuals)
"""
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "scripts"))
import scorecard  # noqa: E402

from app import db as dbmod  # noqa: E402

TS = "2026-10-10T12:00:00Z"


@pytest.fixture()
def score_db(tmp_path):
    path = tmp_path / "score.db"
    dbmod.init_db(path)
    conn = sqlite3.connect(path)
    log = [  # gw, player, chance, ep_v10, ep_final, p_start
        (7, 1, None, 5.0, 5.5, 0.90), (7, 2, 75, 3.0, 1.0, 0.15),
        (7, 3, None, 7.0, 7.5, 0.85), (7, 4, None, 1.0, 2.4, 0.55),
        (7, 5, None, 4.0, 4.0, 0.80),            # team has a blank GW7 → not scored
        (8, 1, None, 5.0, 5.0, 0.90),            # GW8 history is not final yet → not scored
    ]
    conn.executemany("INSERT INTO projection_log (gw, player_id, chance, ep_v10, ep_final, p_start, "
                     "logged_at) VALUES (?,?,?,?,?,?,?)", [r + (TS,) for r in log])
    hist = [  # player, gw, team_matches, minutes, starts, points, final
        (1, 7, 1, 90, 1, 6, 1), (2, 7, 1, 0, 0, 0, 1), (3, 7, 1, 90, 1, 8, 1),
        (4, 7, 1, 30, 0, 2, 1), (5, 7, 0, 0, 0, 0, 1),
        (1, 8, 1, 90, 1, 5, 0),
        (1, 6, 1, 300, 1, 4, 1),                 # player 1 has 300 minutes before GW7
    ]
    conn.executemany("INSERT INTO player_gw_history (player_id, gw, team_matches, minutes, starts, "
                     "total_points, final, fetched_at) VALUES (?,?,?,?,?,?,?,?)",
                     [h + (TS,) for h in hist])
    conn.commit()
    conn.close()
    return path


def _rows(path, min_minutes=0):
    conn = sqlite3.connect(path)
    try:
        return scorecard.load_rows(conn, min_minutes)
    finally:
        conn.close()


def test_ranks_average_ties():
    assert scorecard.ranks([10, 20, 20, 30]) == [1, 2.5, 2.5, 4]
    assert scorecard.ranks([3, 1, 2]) == [3, 1, 2]


def test_spearman():
    assert scorecard.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert scorecard.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert scorecard.spearman([1, 2], [1, 2]) is None            # too few
    assert scorecard.spearman([1, 1, 1], [1, 2, 3]) is None      # no variance


def test_only_final_gameweeks_where_the_team_played_are_scored(score_db):
    rows = _rows(score_db)
    assert {(r["gw"], r["player_id"]) for r in rows} == {(7, 1), (7, 2), (7, 3), (7, 4)}


def test_metrics_match_hand_computation(score_db):
    rows = _rows(score_db)
    actual = [r["actual"] for r in rows]
    old = scorecard.metrics([r["ep_v10"] for r in rows], actual)
    new = scorecard.metrics([r["ep_final"] for r in rows], actual)
    assert old["n"] == 4
    assert old["mae"] == pytest.approx(1.5) and new["mae"] == pytest.approx(0.6)
    assert old["bias"] == pytest.approx(0.0) and new["bias"] == pytest.approx(0.1)
    assert old["rho"] == pytest.approx(0.8) and new["rho"] == pytest.approx(1.0)
    assert scorecard.metrics([], [])["mae"] is None


def test_report_prints_all_three_sections(score_db, capsys):
    assert scorecard.main(["--db", str(score_db), "--min-minutes", "0"]) == 0
    out = capsys.readouterr().out
    assert "4 player-gameweeks over GW7-GW7" in out
    lines = out.splitlines()
    all_pooled = next(l for l in lines if l.strip().startswith("pooled"))
    for token in ("1.500", "0.600", "-0.900", "+0.00", "+0.10", "0.800", "1.000"):
        assert token in all_pooled, (token, all_pooled)
    # flagged-only section: just player 2 (MAE 3.0 → 1.0), too few rows for a rank correlation
    flagged = out.split("Injury-flagged players only")[1].split("Start probability")[0]
    assert "3.000" in flagged and "1.000" in flagged and "n/a" in flagged
    # calibration deciles: 0.9 → 1.00 (started), 0.85 → 1.00, 0.55 → 0.00, 0.15 → 0.00
    cal = out.split("Start probability calibration")[1]
    row = lambda start: next(l for l in cal.splitlines() if l.strip().startswith(start))
    assert row("0.9-1.00").split()[-3:] == ["1", "0.90", "1.00"]
    assert row("0.8-0.9").split()[-3:] == ["1", "0.85", "1.00"]
    assert row("0.5-0.6").split()[-3:] == ["1", "0.55", "0.00"]
    assert row("0.1-0.2").split()[-3:] == ["1", "0.15", "0.00"]


def test_min_minutes_filter_uses_history_before_the_gameweek(score_db, capsys):
    assert [r["player_id"] for r in _rows(score_db, 270)] == [1]         # 300 minutes in GW6
    assert scorecard.main(["--db", str(score_db)]) == 0                  # default is 270
    assert "1 player-gameweeks over GW7-GW7" in capsys.readouterr().out


def test_nothing_to_score_yet(tmp_path, capsys):
    path = tmp_path / "empty.db"
    dbmod.init_db(path)
    assert scorecard.main(["--db", str(path)]) == 0
    assert "nothing to score yet" in capsys.readouterr().out


def test_missing_database_is_an_error(tmp_path, capsys):
    assert scorecard.main(["--db", str(tmp_path / "nope.db")]) == 1
    assert "database not found" in capsys.readouterr().err


def test_scorecard_never_writes(score_db):
    before = score_db.read_bytes()
    scorecard.main(["--db", str(score_db), "--min-minutes", "0"])
    assert score_db.read_bytes() == before
