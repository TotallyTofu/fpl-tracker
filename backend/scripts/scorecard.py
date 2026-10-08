"""Scorecard: how good were the projections the app logged before each deadline?

Joins ``projection_log`` (what the app projected) with ``player_gw_history``
(what players actually did) and prints, for each logged gameweek whose history
is final and pooled over all of them:

  * MAE, bias (projection − actual; positive = over-projects) and Spearman rank
    correlation for ``ep_v10`` (the v1.0 formula) vs ``ep_final`` (what the app
    uses now);
  * the same for injury-flagged players only;
  * a calibration table for the start probability: deciles of ``p_start`` vs the
    share of those players who actually started;
  * news: for players with at least one news signal, the projection without any
    news vs with it (all sources, net negative, net positive, injury-flagged), and
    for each source (bbc, espn, reddit, youtube) the projection without that one
    source vs with it. Official FPL news is already inside ``ep_next`` and is not
    part of this. "with" better than "without" means the source helps.

Reads the app database only and never writes. It does not re-tune anything: if
the numbers say the flag curve or the minutes weight is off, change
``config.json`` by hand (Settings → Optimizer).

    python backend/scripts/scorecard.py [--db PATH] [--min-minutes 270]

``--min-minutes`` keeps players with at least that many minutes before the
gameweek (270 = the backtest's rule; 0 = everyone logged).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "data" / "fpl.db"


def ranks(values: list[float]) -> list[float]:
    """Average ranks (ties share the mean of their positions)."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return out


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx == 0 or syy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(rx, ry)) / (sxx * syy) ** 0.5


def metrics(pred: list[float], actual: list[float]) -> dict:
    n = len(pred)
    if n == 0:
        return {"n": 0, "mae": None, "bias": None, "rho": None}
    errs = [p - a for p, a in zip(pred, actual)]
    return {"n": n, "mae": sum(abs(e) for e in errs) / n, "bias": sum(errs) / n,
            "rho": spearman(pred, actual)}


def load_rows(conn: sqlite3.Connection, min_minutes: int) -> list[dict]:
    """Logged projections joined with the actual result, for gameweeks whose
    history is final and in which the player's team actually played. Older logs
    without the news columns still load (the news section is then empty)."""
    conn.row_factory = sqlite3.Row
    sql = """
      SELECT l.*, h.total_points AS actual, h.starts, h.minutes,
             COALESCE((SELECT SUM(x.minutes) FROM player_gw_history x
                       WHERE x.player_id = l.player_id AND x.gw < l.gw), 0) AS prior_min
      FROM projection_log l
      JOIN player_gw_history h ON h.gw = l.gw AND h.player_id = l.player_id
      WHERE h.team_matches > 0
        AND l.gw IN (SELECT gw FROM player_gw_history GROUP BY gw HAVING MIN(final) = 1)
        AND l.ep_v10 IS NOT NULL AND l.ep_final IS NOT NULL
      ORDER BY l.gw, l.player_id"""
    rows = []
    for r in conn.execute(sql):
        d = dict(r)
        if d["prior_min"] < min_minutes:
            continue
        raw = d.get("news_detail")
        try:
            d["news"] = json.loads(raw) if raw else {}
        except ValueError:
            d["news"] = {}
        rows.append(d)
    return rows


def _fmt(v: float | None, spec: str = "+.2f") -> str:
    return "  n/a" if v is None else format(v, spec)


def _table(title: str, groups: list[tuple[str, list[dict]]], old_key: str = "ep_v10",
           new_key: str = "ep_final", old_label: str = "v1.0", new_label: str = "now",
           gw_col: str = "GW", label_w: int = 8) -> list[str]:
    """MAE / bias / rank correlation of ``old_key`` vs ``new_key`` per group."""
    ow, nw = len(old_label) + 7, len(new_label) + 7          # room for "bias <label>" plus a gap
    lines = [title,
             f"  {gw_col:<{label_w}}{'n':>5}  {'MAE ' + old_label:>{ow}}{'MAE ' + new_label:>{nw}}{'diff':>8}  "
             f"{'bias ' + old_label:>{ow}}{'bias ' + new_label:>{nw}}  "
             f"{'rho ' + old_label:>{ow}}{'rho ' + new_label:>{nw}}"]
    for label, rows in groups:
        if not rows:
            continue
        actual = [r["actual"] for r in rows]
        old = metrics([r[old_key] for r in rows], actual)
        new = metrics([r[new_key] for r in rows], actual)
        lines.append(
            f"  {label:<{label_w}}{old['n']:>5}  {old['mae']:>{ow}.3f}{new['mae']:>{nw}.3f}"
            f"{new['mae'] - old['mae']:>+8.3f}  {old['bias']:>+{ow}.2f}{new['bias']:>+{nw}.2f}  "
            f"{_fmt(old['rho'], '.3f'):>{ow}}{_fmt(new['rho'], '.3f'):>{nw}}")
    if len(lines) == 2:
        lines.append("  (no rows)")
    return lines


def calibration(rows: list[dict]) -> list[str]:
    """p_start deciles vs the share who actually started."""
    lines = ["Start probability calibration (p_start decile vs actual start rate)",
             f"  {'p_start':<11}{'n':>6}{'predicted':>11}{'actual':>9}"]
    bins: dict[int, list[dict]] = {}
    for r in rows:
        if r["p_start"] is None or r["starts"] is None:
            continue
        bins.setdefault(min(9, int(r["p_start"] * 10 + 1e-9)), []).append(r)
    for b in sorted(bins):
        rs = bins[b]
        pred = sum(r["p_start"] for r in rs) / len(rs)
        act = sum(1 for r in rs if r["starts"] >= 1) / len(rs)
        hi = "1.00" if b == 9 else f"{(b + 1) / 10:.1f}"
        lines.append(f"  {b / 10:.1f}-{hi:<7}{len(rs):>6}{pred:>11.2f}{act:>9.2f}")
    if not bins:
        lines.append("  (no rows with a start probability)")
    return lines


def news_report(rows: list[dict]) -> list[str]:
    """Does news help? Pooled over the scored gameweeks. Only players that had a
    (non-official) news signal when the projection was logged can differ."""
    with_news = [r for r in rows if r.get("news") and r.get("ep_nonews") is not None]
    if not with_news:
        return ["News signals: no scored player had a news signal when the projection was logged "
                "(or the log predates news logging)."]
    for r in with_news:
        r["_nonews"] = r["ep_nonews"]
    flagged = [r for r in with_news if r["chance"] is not None and r["chance"] < 100]
    groups = [("any news", with_news),
              ("net neg", [r for r in with_news if (r["news_adj"] or 0) < 0]),
              ("net pos", [r for r in with_news if (r["news_adj"] or 0) > 0]),
              ("flagged", flagged)]
    out = _table("News: projection without news vs with it (players with at least one news signal)",
                 groups, "_nonews", "ep_final", "no news", "with", gw_col="group", label_w=9)
    sources = sorted({src for r in with_news for src in r["news"]})
    per_source = []
    for src in sources:
        mine = []
        for r in with_news:
            if src in r["news"]:
                mine.append(dict(r, _without=r["news"][src]["ep_without"]))
        per_source.append((src, mine))
    out += ["", "By source: projection without that one source vs with it "
                "(players with at least one signal from it)"]
    out += _table("", per_source, "_without", "ep_final", "w/o src", "with",
                  gw_col="source", label_w=9)[1:]
    out.append("")
    out.append("  signals per source: " + ", ".join(
        f"{src} {sum(r['news'][src]['n'] for r in with_news if src in r['news'])}"
        f" ({sum(1 for r in with_news if src in r['news'])} players)" for src in sources))
    return out


def build_report(rows: list[dict]) -> str:
    gws = sorted({r["gw"] for r in rows})
    flagged = [r for r in rows if r["chance"] is not None and r["chance"] < 100]
    out = [f"Projection scorecard: {len(rows)} player-gameweeks over GW{gws[0]}-GW{gws[-1]}"
           if gws else "Projection scorecard: nothing to score yet", ""]
    if not gws:
        out.append("No logged gameweek has final history yet. The log is written before each "
                   "deadline; the history is final once FPL marks the gameweek data_checked.")
        return "\n".join(out)
    per_gw = [(f"GW{g}", [r for r in rows if r["gw"] == g]) for g in gws]
    out += _table("All players  (bias = projection - actual; rho = Spearman rank correlation)",
                  per_gw + [("pooled", rows)])
    out.append("")
    out += _table("Injury-flagged players only (chance of playing < 100)",
                  [(f"GW{g}", [r for r in flagged if r["gw"] == g]) for g in gws]
                  + [("pooled", flagged)])
    out.append("")
    out += calibration(rows)
    out.append("")
    out += news_report(rows)
    out.append("")
    out.append("Small samples are noisy: a few gameweeks of flagged players is a handful of rows. "
               "Treat differences under ~0.05 MAE as nothing until the pooled n is in the hundreds; per-source news rows are the smallest samples of all.")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=str(DEFAULT_DB), help="path to fpl.db")
    ap.add_argument("--min-minutes", type=int, default=270,
                    help="only players with this many minutes before the gameweek (default 270)")
    args = ap.parse_args(argv)
    db = Path(args.db)
    if not db.exists():
        print(f"database not found: {db}", file=sys.stderr)
        return 1
    # read-only: the scorecard never writes to the app database
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        try:
            rows = load_rows(conn, args.min_minutes)
        except sqlite3.OperationalError as e:
            print(f"cannot read the projection log ({e}). Start the app once so the tables exist.",
                  file=sys.stderr)
            return 1
    finally:
        conn.close()
    print(build_report(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
