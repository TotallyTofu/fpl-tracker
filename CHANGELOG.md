# Changelog

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
