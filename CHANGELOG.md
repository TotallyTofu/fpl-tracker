# Changelog

## Unreleased

### Keep players in lineup

- **Keep** on My team: mark any players (no limit) that the plans must never sell.
  They show as grey shirts with a lock, on the pitch, bench and Squad table, and
  the plan pitch greys them too. The flag is saved per team with **Save team**.
- A hard rule in the solver, for all three plans and every chip, **including
  Wildcard and Free Hit**, and for the Wildcard/Free Hit rebuild estimate behind
  the chip advice. Plans say which players were kept.
- A kept player who is unavailable stays in the squad, projected 0, benched, with
  no forced-transfer hit.
- New column `lineup_players.keep` (added automatically on start); `diff.kept_ids`
  on each plan records what it was made with. See `ADD-FEATURE-KEEP.MD`.

## 1.1.0 — 2026-10-08

Projection accuracy: calibrated injury flags, a minutes model and a start
probability, all from a backtest on 2025-26 data (`ADD-FEATURES.MD`). The
backtest shows more accurate projections, **not** better picks: the points of
the top 30 players did not measurably improve (+0.12, interval −0.06 to +0.32).

### Projections

- **Calibrated injury flags.** FPL's flags are optimistic (flagged 75% played
  54% of the time). The projection now undoes FPL's own flag scaling of
  `ep_next` and applies a configurable curve: 75% → 0.60, 50% → 0.50,
  25% → 0.05, below 25% → 0. A 75%-flagged player projects about 20% lower
  than in 1.0; fit players are unchanged. The Safe plan's availability map is
  unchanged and is now described as extra caution on top.
- **Minutes model**, blended 50/50 with the existing projection. It estimates
  the share of each match a player plays and his points per 90 from the
  per-gameweek history. Backtest: error −0.114 points (95% interval −0.138 to
  −0.092), rank correlation +0.036. Players with no history keep the 1.0
  projection. Early in the season it pulls hot streaks toward the position
  average hard (everyone has about 450 minutes of history at gameweek 6), so
  high-form players drop 10-25%; this is by design, not rotation. Also applied to
  gameweeks after the next one, which was not backtested.
- **Start probability** ("starts ~X%"): from starts in the last five matches and
  the last match, cut down for injury flags. Shown on the pitch below 60%
  (without an injury flag), in a new My team column, and on transfer-in rows
  below 80%. Display only; the solver does not use it.

### Data

- **Per-gameweek history** (`player_gw_history`): minutes, starts and points for
  every player and finished gameweek, from FPL's `event/{gw}/live`. The first
  start of a season backfills every finished gameweek (one call each); after
  that a refresh makes no history calls, or one while the latest gameweek waits
  for FPL's `data_checked`. Wiped on season rollover.
- **Projection log** (`projection_log`) and `backend/scripts/scorecard.py`: the
  app records what it projected before each deadline (next to the 1.0 formula)
  and the scorecard compares both with what players scored, for all and for
  flagged players, plus a start-probability calibration table. It re-checks the
  curve, the weight and the table on this season's data; it changes nothing.
- **News is measured too.** The log also stores each player's projection without
  news, the news adjustment, and a leave-one-out projection per source (BBC, ESPN,
  Reddit, YouTube), with every signal's category, sentiment and confidence. The
  scorecard shows whether news, and each source separately, lowers the error. The
  news signals' coefficients were hand-set and never backtested; this is the first
  way to check them. An existing `projection_log` table gets the new columns on
  start.

### Settings

- New "Injury-flag calibration" and "Minutes model" blocks (0-1, validated by the
  server). The Safe plan block gains the missing 75% and 25% inputs.
- Rejected saves now say which field is wrong.
- An invalid `config.json` is copied to `config.json.invalid` before the
  defaults replace it (it used to be overwritten without a trace).

## 1.0.0 — 2026-10-05

First public release. Earlier milestones (M1–M4, v1.0.x, audit revisions 2–3)
were private iterations; see `README.md` history in git and `FIX.MD`.

### FPL rules

- **Differential plan could make 14 transfers for −48 points.** Its EP cut-off
  removed most of the user's own players, no plan fitted the free transfers, and
  the fallback switched the transfer limit off. The cut-off now keeps the user's
  players, and the fallback still charges −4 per extra transfer.
- **Holding a Wildcard or Free Hit no longer turns every plan into a rebuild.**
  A chip is only assumed when it is picked on the Transfer plans page.
- **One chip per gameweek**, enforced in chip advice (at most one "use"), when
  applying a plan, and in the chip log (409 for a second chip in a gameweek).
- **Money in the bank.** Plans spend bank + sell values (purchase price + half
  the rise). A real squad worth over £100m is a warning, not an error.
- Free transfers 0–5, rolled forward +1 per passed deadline.
- Goalkeeper sub in his own bench slot.
- The captain is the best pick for the plan, not the result of random moves.
- Bench Boost and Triple Captain are scored when played.

### Projections

- Availability is no longer counted up to four times: FPL's `ep_next` already
  includes chance of playing. Official news no longer moves the projection.
- Blend with the season points-per-game average instead of a second copy of
  form; fixture difficulty is a multiplier; blank gameweeks score 0; no more
  ~16% shrink on every projection.
- Steepest-ascent swap search: "Best projected" can no longer be out-projected
  by another plan.

### News and LLM

- LLM "thinking" switched off by default. On the user's model, two of four real
  articles failed after 70 s of reasoning; with thinking off all four succeeded
  in 3–6 s.
- Keyword rules only run when the LLM is unavailable or failed (they used to
  sit next to, and contradict, the LLM's reading). Items wait while the LLM is
  failing instead of being marked done; a re-read endpoint recovers the 169
  items older versions skipped.
- Keyword precision: proper nouns only, no other people's full names ("Steve
  Clarke" is not Clarke), no questions, Reddit post only.
- Reddit OAuth dates parsed (float epochs); a thread is stored once, not again
  each time its comments change.
- ESPN on by default, with full story text.
- YouTube transcripts read up to 60k characters (was 12k, mostly the intro).
- Signals expire from their publish time, at least until the next deadline.
- LLM prompt: national-team, price and ownership noise are not signals.

### Security

- Host and Origin checks (DNS rebinding, cross-site requests).
- Reddit secret redacted from the settings API; "Test connection" never sends
  the saved key to a different URL.

### UI

- Redesigned app: This gameweek, Transfer plans, My team, News & signals.
  Light and dark themes, phone layout, bundled fonts.
