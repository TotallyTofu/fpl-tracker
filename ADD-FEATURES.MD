# FPL Tracker — Add Features (v1.1): flag calibration, minutes model, start probability

> **Written** 2026-10-08 (2026/27 season, before the GW6 deadline) from a backtest on
> 2025-26 Premier League data. Method and caveats: Appendix A. Builds on v1.0
> (`backend/app/optimizer/scoring.py`, see its module docstring).
> **Tasks** T1–T7. Do them in order; each ends with green tests
> (`cd backend; ..\.venv\Scripts\python -m pytest tests -q`, then
> `cd frontend; npx tsc --noEmit; npx vite build`).
> **Not in this plan:** the multi-week planner (the user is still deciding) and
> team strength / Elo (decided against, §6).

---

## 1. Why: what the backtest showed

Forecasting each gameweek from data available before its deadline (2025-26,
GW12–38, ~9,900 player-gameweeks; 95% CIs by bootstrap over gameweeks):

| Change | Overall effect | Where it matters |
|---|---|---|
| **Calibrated injury flags** (T1) | Overall error unchanged (MAE −0.003): only 1–3% of players are flagged | For 75%-flagged players MAE falls 1.89 → 1.72 and bias +0.36 → −0.02; for 25%-flagged, 0.41 → 0.13. Changes captain/transfer calls that involve doubtful players |
| **Minutes model**, 50/50 with today's blend (T2, T3) | MAE −0.114 [−0.138, −0.092], rank correlation +0.036 [+0.030, +0.042] | Every position. Also held early in 2026-27 with only 2–4 GWs of history (MAE 2.54 → 2.41) |
| **Start probability** from recent starts + last match (T4) | Start-prediction Brier 0.156 → 0.146 (skill 0.378 → 0.419) | A display / risk signal ("starts ~40%") |

**Be honest in the UI and docs about what this does NOT show.** None of these
changes measurably improved the *points of the top picks* (top-30 picks +0.12
points, CI −0.06 to +0.32). They make projections more accurate and flag
rotation/injury risk; they are not proven to pick better teams.

FPL's flags are badly optimistic. Regular starters flagged **75%** played **54%**
of the time (108 players); flagged 50% → 20%; 25% → 10%; 0% → 0%. FPL's own
`ep_next` is exactly `form × chance/100`, so today's projection is linear in the flag.

---

## 2. Decisions already made (do not relitigate)

1. **Flag curve** (share of a fit player's points a flagged player delivers):
   75% → **0.60**, 50% → **0.50** (unchanged: the estimate was unstable, 0.75 in one
   half of the season and 0.32 in the other, and 0.50 already had ~0 bias),
   25% → **0.05**, below 25% → 0. Configurable.
2. **Minutes model weight 0.5**, blended with the existing projection. The model alone
   was slightly more accurate late in the season but picked worse early; 0.5 is the
   robust choice. Configurable.
3. **Last-match state (started / came on / didn't play) is for the start-probability
   display only.** Feeding it into the points projection made it *worse*
   (MAE −0.073 vs −0.136 without it).
4. **Data source: FPL's `/api/event/{gw}/live/`** — one call per finished gameweek
   gives `minutes`, `starts`, `total_points` and `explain` (the team's fixtures, present
   even for players who did not play) for all ~667 players. Verified 2026-10-08.
   Not FPL-Core-Insights (third-party, schema drift, Elo already stopped), not
   `element-summary` (one call per player).
5. **No "bench vs not in squad" split.** It needs lineup data FPL doesn't publish and
   added only ~0.001 Brier.
6. **Safe profile's availability map stays, values unchanged**, re-labelled as
   *extra caution on top of the projection*. Once the projection carries the calibrated
   curve, the Safe plan automatically gets more cautious about doubts: with the user's
   config (`doubt` 0.7 × `chance_75` 0.85) a 75% doubt nets 0.75 × 0.595 = 0.45 today
   and 0.60 × 0.595 = 0.36 after T1.
7. **The solver objective does not change** except through `ep`. No new constraints.

---

## 3. Order of work

| Task | What | Depends on |
|---|---|---|
| T1 | Calibrated flag curve in the projection + Settings | — |
| T2 | Per-gameweek history table + fetcher (`event/{gw}/live`) | — |
| T3 | Minutes model blended into `ep_final` | T1, T2 |
| T4 | Start probability + rotation-risk display | T2 |
| T5 | Settings UI for T1/T3 (+ the missing `chance_75`/`chance_25` inputs) | T1, T3 |
| T6 | Projection log + scorecard script (the self-calibration loop) | T3, T4 |
| T7 | README, CHANGELOG, config.example.json | all |

---

## T1 — Calibrated availability curve

**Goal**: a flagged player's projection uses what flagged players actually deliver,
applied exactly once.

**Files**: `backend/app/config.py`, `backend/app/optimizer/scoring.py`,
`backend/tests/test_ep_model.py`.

**Spec**:

- `config.py` — new model, added to `OptimizerConfig` as
  `availability_curve: AvailabilityCurve = Field(default_factory=AvailabilityCurve)`.
  An existing `config.json` without the key loads with the defaults (pydantic).
  ```python
  class AvailabilityCurve(BaseModel):
      # v1.1: share of a fit player's points that a player flagged at this
      # chance of playing actually delivers (2025-26 backtest, ADD-FEATURES.MD §1).
      play_75: float = 0.60   # 75 <= chance < 100
      play_50: float = 0.50   # 50 <= chance < 75
      play_25: float = 0.05   # 25 <= chance < 50   (below 25 → 0)
  ```
- `scoring.py`:
  ```python
  def play_factor(p: dict, cfg) -> float:
      """Calibrated availability: 0 for hard gates, 1 for fit / no flag,
      else the curve bucket. Same thresholds as availability_multiplier."""
      if _hard_gated(p):
          return 0.0
      c = p.get("chance_of_playing_next_round")
      if c is None or c >= 100:
          return 1.0
      cur = getattr(cfg.optimizer, "availability_curve", None)  # hand-built test cfgs
      if c >= 75: return getattr(cur, "play_75", 0.60)
      if c >= 50: return getattr(cur, "play_50", 0.50)
      if c >= 25: return getattr(cur, "play_25", 0.05)
      return 0.0

  def ep_next_unflagged(p: dict) -> float:
      """FPL's ep_next is form × chance/100 (verified: ratio 0.75/0.50/0.26 for
      75/50/25 flags). Undo it so the calibrated curve is applied once."""
      ep = ep_baseline(p)
      c = p.get("chance_of_playing_next_round")
      return ep * 100.0 / c if c is not None and 0 < c < 100 else ep
  ```
- `ep_final`: replace `ep_baseline(p)` in the blend with
  `ep_next_unflagged(p) * f` and `_chance(p)` in `ppg_term` with `f`, where
  `f = play_factor(p, cfg)`. Delete `_chance` if nothing else uses it.
  Everything else (fixture multiplier, signals, blank/double GW, `use_ep_next=False`)
  is unchanged.
- `score_lineup`'s `baseline` keeps raw FPL `ep_next` (it is "what FPL says").
- `availability_multiplier` (Safe profile) is unchanged. Update its docstring and the
  `AvailabilityConfig` comment in `config.py`: "extra caution for the Safe plan, applied
  on top of the calibrated projection".
- Update the module docstring formula in `scoring.py`.

**Tests** (`test_ep_model.py`; the `_cfg()` helper has no `availability_curve`, so the
`getattr` defaults are exercised):
- Rewrite the third assertion of `test_ep_final_blend_of_ep_next_and_season_average`:
  `p(ep_next=7.5, points_per_game=10.0, chance=75)` → unflagged 10.0 →
  `(0.7·10·0.6 + 0.15·10·0.6) / 0.85 = 6.0`.
- Replace `test_ep_final_does_not_reapply_availability` with
  `test_ep_final_applies_calibrated_curve_once`: doubt `p(ep_next=3.0, ppg=4.0,
  status="d", chance=75)` plus an official negative signal →
  `(0.7·4.0·0.6 + 0.15·4.0·0.6) / 0.85 = 2.4` (official news still not priced).
- New parametrized `test_play_factor_buckets`: None→1, 100→1, 99→0.60, 75→0.60,
  74→0.50, 50→0.50, 49→0.05, 25→0.05, 24→0, 0→0; `status` u/s and `can_select=0` → 0.
- New `test_play_factor_from_config`: a cfg with `availability_curve.play_75=0.7` → 0.7.
- `test_ep_final_unflagged_without_chance`: chance None → `ep_next` used as is.

**Acceptance**: all tests green. On the live DB, flagged players' `ep` drops
(75% doubt ≈ 0.8× the v1.0 value); unflagged players are unchanged.

---

## T2 — Per-gameweek history store

**Goal**: every player's minutes, starts and points per finished gameweek, locally,
from one API call per gameweek.

**Files**: `backend/app/db.py`, `backend/app/fetchers/fpl.py`,
`backend/tests/fixtures/sample_event_live.json` (new),
`backend/tests/test_gw_history.py` (new).

**Spec**:

- Schema (add to `SCHEMA`; `CREATE TABLE IF NOT EXISTS`, so no migration needed):
  ```sql
  CREATE TABLE IF NOT EXISTS player_gw_history (
    player_id INTEGER NOT NULL,
    gw INTEGER NOT NULL,
    team_matches INTEGER NOT NULL,   -- len(explain): 0 = blank GW, 2 = double GW
    minutes INTEGER NOT NULL,
    starts INTEGER,                  -- NULL if the API stops sending it (drift)
    total_points INTEGER NOT NULL,
    final INTEGER NOT NULL DEFAULT 0, -- 1 = fetched after the GW's data_checked
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (player_id, gw)
  );
  CREATE INDEX IF NOT EXISTS idx_pgh_gw ON player_gw_history(gw);
  ```
  No foreign key to `players`. **Add `player_gw_history` to the wipe list in
  `check_season_rollover`**: FPL re-numbers player ids every season, so old rows
  would be attached to the wrong players.
- `db.py`: `upsert_gw_history(gw: int, rows: list[tuple], final: bool) -> int`.
- `fpl.py`:
  ```python
  async def fetch_gw_history(gw: int, final: bool) -> int:
      """GET /api/event/{gw}/live/ → player_gw_history rows for that GW."""
      data = await http.get_json(f"{BASE}/event/{gw}/live/")
      for el in data.get("elements", []):
          st = el.get("stats") or {}
          team_matches = len(el.get("explain") or [])
          # defensive (D18): minutes without explain should not happen; count 1
          if team_matches == 0 and (st.get("minutes") or 0) > 0:
              team_matches = 1
          row = (el["id"], gw, team_matches, int(st.get("minutes") or 0),
                 st.get("starts"), int(st.get("total_points") or 0))
      ...
  async def sync_gw_history() -> int:
      """Fetch every finished GW that has no rows yet, or whose rows are not
      final (fetched before data_checked). Sequential, politeness first."""
  ```
  - Work list: `SELECT id, data_checked FROM events WHERE finished = 1`; skip GWs
    whose rows all have `final = 1`; fetch the rest one after another and pass
    `final = bool(data_checked)`.
  - The first run of a season backfills every finished GW (≤ 38 calls, once). After
    that it's 0 calls, or 1 while the latest GW waits for `data_checked`.
  - Log drift once if `stats.starts` is missing (`_drift` list, like `_check_drift`).
  - Sanity check, logged and non-fatal: count players whose `team_matches` disagrees
    with the `fixtures` table count for their current team in that GW. Double-GW
    `explain` behaviour has **not** been observed yet this season; this check catches it.
- Call `sync_gw_history()` from `refresh_all_fpl()` after `fetch_fixtures()`, wrapped in
  try/except like the official-news step (a failure must not break the refresh).

**Tests** (network-free; monkeypatch `http.get_json` as `test_fetchers.py` does):
- Fixture JSON with: a 90-min starter, a 20-min sub, an unused player (0 min, 1 explain
  entry), a blank-GW player (no explain), a double-GW player (2 explain entries).
- Parse: rows and `team_matches` values are as expected.
- Sync: finished GWs with no rows are fetched; non-final GWs are re-fetched until
  `data_checked`; final GWs are not re-fetched; a fetch error leaves the other GWs intact.
- Rollover wipes `player_gw_history`.

**Acceptance**: on a fresh DB the first start stores rows for every finished GW
(GW1–5 now: ~667 rows each); a second refresh makes 0 history calls; one player's
minutes match the FPL site.

---

## T3 — Minutes model in the projection

**Goal**: blend a minutes-based estimate into `ep_final`, so rotation risk and
reduced roles lower the projection.

**Files**: `backend/app/optimizer/minutes.py` (new), `backend/app/optimizer/scoring.py`,
`backend/app/optimizer/solver.py`, `backend/app/config.py`,
`backend/tests/test_minutes.py` (new), `backend/tests/conftest.py`.

**Spec**:

- `config.py`:
  ```python
  class MinutesModelConfig(BaseModel):
      # v1.1: blend weight of the minutes-based estimate (0 = off).
      enabled: bool = True
      weight: float = 0.5
  ```
  Add as `OptimizerConfig.minutes_model`. Hand-built test cfgs lack it → treat as off.
- `minutes.py` (constants from the backtest, keep them here with a comment):
  ```python
  PRIOR_PTS90 = {1: 3.4, 2: 3.8, 3: 4.4, 4: 5.0}   # 2025-26 points per 90 by position
  RECENT_N = 5            # last 5 GWs in which the player's team played
  RECENT_W = 0.6          # weight of the recent window vs the season
  SHRINK_MIN = 450        # prior counts as 450 minutes (5 full matches)

  def load_history(before_gw: int) -> dict[int, list[dict]]:
      """One query: rows with gw < before_gw AND team_matches > 0, by player, ascending gw."""

  @dataclass
  class MinutesProfile:
      share: float          # expected share of 90 minutes per team match, ≤ 1
      rate90: float         # shrunk points per 90
      start_rate5: float | None
      last_match: str | None  # "started" | "came_on" | "no_minutes"
      matches: int          # team matches in history

  def profile(rows: list[dict], element_type: int) -> MinutesProfile | None:
      """None when the player has no team match in history."""
  ```
  - `rec` = the last `RECENT_N` rows; `tm` = `team_matches`.
  - `share = min(1, RECENT_W·Σmin_rec/(90·Σtm_rec) + (1−RECENT_W)·Σmin_all/(90·Σtm_all))`
  - `rate90 = (Σpts_all + PRIOR_PTS90[pos]·SHRINK_MIN/90) / ((Σmin_all + SHRINK_MIN)/90)`
  - `start_rate5 = min(1, Σstarts_rec / Σtm_rec)` (None if any `starts` is NULL)
  - `last_match` from the most recent row: starts ≥ 1 → started; minutes > 0 → came_on;
    else no_minutes.
- `solver.build_universe(target_gw, cfg)`: after the query, call
  `load_history(target_gw)` once and attach to each player dict
  `mm_share`, `mm_rate90`, `start_rate5`, `last_match` (all None without a profile).
  Every `ep_final` / `score_lineup` call already receives universe dicts
  (`solver.py:122,808,867`, `suggestions.py:72`), so no signature changes are needed.
- `scoring.ep_final`, after the T1 blend:
  ```python
  mm = cfg.optimizer.minutes_model (getattr; None → off)
  if mm and mm.enabled and p.get("mm_share") is not None:
      minutes_ep = play_factor(p, cfg) * p["mm_rate90"] * p["mm_share"] * n_fix
      blend = (1 - mm.weight) * blend + mm.weight * minutes_ep
  return blend * fixture_multiplier(...) * (1 + signal_adjustment(...))
  ```
  This applies to `use_ep_next=False` too (the minutes model doesn't use `ep_next`).
  That case was **not backtested**; say so in the README.

**Tests** (`test_minutes.py`):
- `profile` hand-computed: 6 GWs, minutes `[90, 90, 0, 90, 45, 90]`, `team_matches` all 1,
  known points, MID → exact `share`, `rate90`, `start_rate5`, `last_match`.
- Blank GW rows (tm = 0) are excluded by `load_history`; a double GW (tm = 2, 180 min) →
  share 1; share is capped at 1.
- No history → `ep_final` equals the T1 value exactly.
- `weight = 0` → T1 value; `weight = 1` → `minutes_ep × fixture × (1+S)`.
- A 75% flag scales `minutes_ep` by 0.60; a double GW doubles it.
- `build_universe` attaches the fields (add a few `player_gw_history` rows to
  `conftest.py` for 2–3 players; leave the rest without history).
- **Existing solver/suggestion tests must stay green unchanged**: players without
  history fall back to the T1 projection.

**Acceptance**: tests green. On live data, log the top 20 projections before/after:
a known rotation player drops, and nailed starters on a hot streak drop too. (The
original criterion "nailed starters move by under ~15%" was wrong and was never
tested: at GW6 everyone has ~450 minutes of history, so points per 90 are pulled
~50% toward the position mean and the top of the list falls 10-25% (Groß −25%,
Tarkowski −20%). That is the intended correction: last season's data with 3-9 GWs
of history gives top-30 projected vs actual 6.25 vs 3.95 with no shrinkage and
5.10 vs 4.04 with 450 minutes, and the GW3-5 top 30 of the old formula projected
7.56 against 4.42 actual. Do not lighten the shrinkage. Side effect: the Triple
Captain / Bench Boost thresholds in `transfers.py` were set against the old,
inflated projections and now fire less often; they were left unchanged.)

---

## T4 — Start probability and rotation-risk display

**Goal**: show "starts ~X%" so the user sees rotation risk. Display only; the solver is
untouched.

**Files**: `backend/app/optimizer/minutes.py`, `backend/app/api/lineups.py`,
`backend/app/api/suggestions.py` (squad payload), `frontend/src/types.ts`,
`frontend/src/components/PitchView.tsx`, `frontend/src/pages/MyTeam.tsx`,
`frontend/src/components/PlanDetail.tsx`, tests.

**Spec**:

- `minutes.py`:
  ```python
  # P(start next GW) for unflagged players, by start rate over the last 5 team
  # matches × what happened in the last match. 2025-26 GW12-38, n per cell in ADD-FEATURES.MD App. B.
  START_TABLE = {            # bucket: (started, came_on, no_minutes)
      0: (0.30, 0.31, 0.14),  # start rate 0-19%  ("started" cell n=25, rounded down)
      1: (0.67, 0.38, 0.25),  # 20-39%
      2: (0.69, 0.39, 0.29),  # 40-59%
      3: (0.77, 0.53, 0.29),  # 60-79%
      4: (0.88, 0.57, 0.51),  # 80-100%
  }
  # flagged players start far less often than they play (whole pool: fit 65% start;
  # 75% flag 39%, 50% flag 15%, 25% flag 2%)
  START_FLAG_FACTOR = {75: 0.60, 50: 0.25, 25: 0.05}

  def start_probability(p: dict, prof: MinutesProfile | None) -> float | None:
      """None without history or without starts data; 0 for hard gates / flag < 25."""
  ```
  Bucket = `min(4, int(start_rate5 * 5))`. Use the same flag thresholds as `play_factor`.
- Attach `p_start` in `build_universe` next to the T3 fields.
- API: add `p_start` (rounded to 2 dp) and `last_match` to each squad player in the
  suggestions response and to lineup players in `lineups.py` (around the
  `SELECT … p.ep_next …` at line ~118; compute with `load_history(next_gw)` +
  `profile` + `start_probability` for the 15 ids).
- `types.ts`: on `SquadPlayer` / `LineupPlayer`, add
  `p_start?: number | null; last_match?: "started" | "came_on" | "no_minutes" | null;`
- UI (one constant `ROTATION_RISK = 0.6` in `types.ts`):
  - `PitchView` token: if `flag(p)` is null and `p_start < ROTATION_RISK`, show a small
    muted chip `~45%` with title "Starts ~45% — started 2 of last 5; last match: came on".
    The FPL flag chip keeps precedence.
  - `MyTeam` squad table: a "Starts" column, amber below 60%, red below 30%, "—" if None.
  - `PlanDetail` transfers-in rows: append ` · starts ~X%` when `p_start < 0.8`.

**Tests**: `start_probability` per bucket and last-match state, flag factors, None paths;
API test that lineup and suggestion players carry `p_start`; `tsc` and `vite build`.

**Acceptance** (manual): My team shows a Starts column; a known rotation player has an
amber chip on the pitch; nailed starters show ~85–90%.

---

## T5 — Settings UI

**Files**: `frontend/src/types.ts`, `frontend/src/pages/Settings.tsx`.

**Spec**:
- `types.ts`: `AvailabilityConfig` is missing `chance_75` and `chance_25` (the backend
  has had them since FIX T2, and the user's `config.json` sets them). Add them. Add
  `AvailabilityCurve` and `MinutesModelConfig` to the optimizer config type.
- `Settings.tsx`, Optimizer section:
  - New block **"Injury-flag calibration"**: three number inputs (0–1, step 0.05) for
    `play_75 / play_50 / play_25`. Help text: "Flagged players score less than FPL's
    percentage suggests. 2025-26: players flagged 75% played 54% of the time."
  - New block **"Minutes model"**: enabled toggle + weight (0–1, step 0.05). Help:
    "Blends a minutes-based estimate with FPL's projection. Backtest: ~6% lower error."
  - Rename the availability block heading to **"Safe plan — extra caution for doubts"**
    and add the missing `chance_75` / `chance_25` inputs.
- Server-side validation: values must be in [0, 1] (pydantic `Field(ge=0, le=1)` on the new
  models; check how `PUT /api/settings` reports validation errors and follow that).

**Acceptance**: change each value, Save, reload → persisted in `config.json`; projections
change on the next "Make plans".

---

## T6 — Projection log and scorecard (self-calibration loop)

**Goal**: the app records what it projected before each deadline, so the curve, the
minutes weight and the start table can be re-checked on this season's data instead of
trusting 2025-26 forever.

**Files**: `backend/app/db.py`, `backend/app/fetchers/fpl.py` (or a new
`backend/app/optimizer/projlog.py`), `backend/scripts/scorecard.py` (new), tests.

**Spec**:
- Table (add to the rollover wipe list):
  ```sql
  CREATE TABLE IF NOT EXISTS projection_log (
    gw INTEGER NOT NULL,
    player_id INTEGER NOT NULL,
    status TEXT, chance INTEGER, ep_next REAL,
    ep_v10 REAL,        -- v1.0 formula: curve 0.75/0.50/0.25, minutes model off
    ep_final REAL,      -- what the app projected
    minutes_ep REAL, p_start REAL,
    logged_at TEXT NOT NULL,
    PRIMARY KEY (gw, player_id)
  );
  ```
- After each bootstrap, while `now < deadline(next_gw)`: build the universe for `next_gw`
  with the same signals and fixture map the suggestions use, and upsert one row per
  player. The last pre-deadline write wins; rows freeze once `next_gw` advances.
  `ep_v10` = `ep_final` with a cfg copy where `availability_curve = 0.75/0.50/0.25` and
  `minutes_model.enabled = False` (identical to v1.0 for 75/50/25 flags).
- `scripts/scorecard.py` (reads the app DB only; joins `projection_log` with
  `player_gw_history`): for each logged GW with final history, and pooled, print MAE,
  bias and rank correlation for `ep_v10` vs `ep_final`; the same for flagged players
  only; and a `p_start` calibration table (deciles of `p_start` vs actual start rate).
  No automatic re-tuning: the user/agent changes config by hand.

**Tests**: logging writes ~one row per eligible player for `next_gw` and is idempotent;
nothing is written after the deadline; the scorecard runs on a tiny synthetic DB and
prints the expected numbers.

**Acceptance**: after the GW6 deadline, `projection_log` has GW6 rows; once GW6 is
`data_checked`, `python scripts/scorecard.py` prints a table.

---

## T7 — Docs

- `README.md` → "How suggestions work → Projected points": the new formula:
  ```
  projected = blend × fixture × (1 + news)
  blend     = 0.5 × v1 + 0.5 × minutes          (minutes model, if the player has history)
  v1        = (0.70 · FPL ep_next* + 0.15 · season points-per-game × flag) / 0.85
  minutes   = flag × points per 90 (shrunk to the position average) × expected share of 90 × fixtures
  flag      = 1 fit · 0.60 at 75% · 0.50 at 50% · 0.05 at 25% · 0 below
  * ep_next with FPL's own flag scaling removed, so the flag counts once
  ```
  (0.70 / 0.15 are the default weights; the Settings page can change them.)
  Plus: the start % display and what it means; Safe = extra caution on top; gameweeks
  beyond next use the minutes model too (not backtested); the honest line from §1
  ("more accurate projections; not shown to pick better teams").
- README "Development": `scripts/scorecard.py`.
- `CHANGELOG.md`: v1.1.0 entry.
- `config.example.json`: the new `availability_curve` and `minutes_model` keys.

---

## 6. Not building: team strength / Elo

Checked and rejected on 2026-10-08:
- FPL no longer publishes continuous team strength: in 2026/27 `bootstrap-static` has
  `strength: null`, attack/defence strengths 0, and only 1–5 overall home/away values.
- ClubElo vs the official FDR on 2025-26 (team goals per match, 700 team-matches): Elo
  r = 0.254, FDR r = 0.214; difference +0.04, CI −0.02 to +0.10, so not a significant
  improvement. Rolling xG was worse (0.185).
- No live source: FPL-Core-Insights' Elo stopped in 2026/27 (none by GW5, none for upcoming
  fixtures), and ClubElo's API returned "Fixtures API deactivated" / an empty response.

Revisit only if FPL republishes continuous strengths, a maintained Elo feed appears, or
the multi-week planner needs per-fixture goal expectations. Then re-test against FDR on
two or more seasons.

---

## 7. Open questions for the user (not blocking)

1. **Safe plan and rotation risk**: should Safe also discount low `p_start`? No evidence
   either way (it's a risk preference). Recommendation: decide after seeing T4's chips for
   a few gameweeks.
2. **Minutes weight**: 0.5 now; revisit with T6's scorecard after ~5 gameweeks.
3. **50% flag**: kept at 0.50 because the data was unstable; the scorecard will say more.

---

## Appendix A — How the numbers were produced (and pitfalls)

- **Data**: FPL-Core-Insights (github.com/olbauday/FPL-Core-Insights, commit `e6616fd`,
  2026-10-08), `data/2025-2026/By Tournament/Premier League/GW*/` —
  `player_gameweek_stats.csv`, `playermatchstats.csv`, `lineups.csv`, plus the season's
  `team_history.csv`; 2026-27 GW1–5 for the early-season check; official FDR for 2025-26
  from vaastav/Fantasy-Premier-League `data/2025-26/fixtures.csv`.
- **Clean rows only.** In the 2025-26 files the pre-deadline snapshot columns
  (`ep_next`, `form`, `points_per_game`, status, flags) are unreliable for GW1–10. They were
  written weeks later (the GW10 file contains GW11's results), and GW2–10 have a
  `status='a'` + `chance=0.0` artefact. Per-gameweek minutes/starts/points are fine for all
  38 GWs (100% agreement with match-level data). Forecast inputs therefore come from GW11+
  rows; history uses all GWs.
- **Timing**: features from row t (24 of 27 clean files were last written before the
  next deadline, 3 a few hours after) → target = points in GW t+1. Pool: not `u`, ≥ 270
  minutes of history (≥ 90 for the 2026-27 check), team plays in GW t+1.
- **"App blend" in the backtest** = `(0.70·ep_next + 0.15·ppg·flag)/0.85`, **without** the
  fixture multiplier or news signals (the dataset has neither in usable form). The real
  app's gains may differ; T6 measures them.
- **The user's `config.json` uses non-default weights** (`ep` 0.59, `form` 0.24,
  `fixture` 0.17). Re-run with those weights, same picture: all rows MAE 1.89 → 1.80
  (T1 + T3); 75%-flagged 1.91 → 1.72 (bias +0.45 → +0.05); 25%-flagged 0.45 → 0.14;
  early 2026-27 2.55 → 2.41. T3's blend uses whatever weights are configured.
- **Metrics**: MAE; rank correlation (Spearman); mean actual points of each GW's top 30;
  share of the top 30 who played under 60 minutes. CIs: 95%, bootstrap over gameweeks.
- **Splits**: flag curve and lookup tables fitted on forecasts of GW12–25, tested on
  GW26–38.
- Other dataset gotchas: `matches.csv` `home_team`/`away_team` are team **codes** (the
  README says ids); 2025-26 `playermatchstats` holds only ~1/3 of unused subs
  (`lineups.csv` is complete); `teams.csv` in the GW1–25 folders is one copied snapshot.
- The research scripts lived in a temporary folder and were not committed; T6 is the
  in-app replacement.

## Appendix B — Reference numbers

Holdout (forecast GW26–38, 13 GWs, n = 5,035):

| Variant | MAE | Rank corr | Top-30 pts | Top-30 < 60 min |
|---|---|---|---|---|
| v1.0 app blend | 1.865 | 0.549 | 4.43 | 11.8% |
| + calibrated flags (T1) | 1.862 | 0.549 | 4.39 | 12.1% |
| minutes model alone | 1.730 | 0.585 | 4.62 | 10.0% |
| **T1 + minutes model 50/50 (T3)** | **1.751** | **0.585** | 4.54 | 10.8% |
| minutes model + last-match state | 1.792 | 0.591 | 4.53 | 13.8% |

Flag multiplier (actual points ÷ FPL form, relative to fit players):

| Flag | Forecast GW12–25 | Forecast GW26–38 | All (rows / players) |
|---|---|---|---|
| 75% | 0.64 | 0.59 | 0.62 (288 / 174) |
| 50% | 0.75 | 0.32 | 0.53 (67 / 56) |
| 25% | 0.01 | 0.06 | 0.03 (72 / 52) |

Start table cell counts (unflagged rows; started / came on / no minutes):
0–19%: 25 / 460 / 702 · 20–39%: 339 / 339 / 263 · 40–59%: 515 / 296 / 204 ·
60–79%: 668 / 221 / 145 · 80–100%: 3,816 / 171 / 110.

Position priors, points per 90: GK 3.37, DEF 3.80, MID 4.39, FWD 5.00 (whole 2025-26
season; GW1–10 alone gave 3.36 / 3.87 / 4.35 / 5.05).
