# Acceptance — M4 (v1.0.0)

Executed 2026-09-20 against the live 2026/27 season. Method per criterion:
**PASS** / **FAIL** / **SKIPPED** + evidence. Criteria AC1–AC8 are from
`PLAN.MD` §1; AC6 is adjusted to the revised M3 scope (live-match polling was
dropped per project decision, 2026-09-20); AC7 is skipped per user decision
(group-rank card not in v1).

## AC1 — Fresh clone → prod + dev

**PASS.**

- **Prod**: fresh-clone simulation on 2026-09-20 — wiped `.venv/`,
  `frontend/node_modules/`, `frontend/dist/`, `data/fpl.db` → `start.ps1 -Prod`
  → Python found via `py -3.11` (3.11), venv created, `pip install
  --no-cache-dir -r backend/requirements.txt`, `npm install` (71 packages),
  `npm run build` → `dist/assets/index-BY1zgIW5.js` (234.42 kB) → uvicorn on
  `http://127.0.0.1:8000` serving API + built UI. Live data after bootstrap:
  667 players, 20 teams, 38 events, 8 chip rows. Startup news refresh ran in
  the background (bbc/reddit/youtube ok). SPA deep-links verified
  (`/suggestions`, `/team` render; `GET /api/nonexistent-xyz` → 404 JSON).
- **Dev**: 2026-09-20 — `start.ps1 -Dev` → vite on `http://localhost:5173`
  (HTTP 200) + API on `:8000`; proxy verified: `GET
  http://localhost:5173/api/meta/season` → `season=2026/27 current_gw=5
  next_gw=6`.

## AC2 — Team entry (paste + picker), rule violations, lineups

**PASS.**

- Paste-a-list with 1 deliberate typo: matcher resolved all 15 (typo → fuzzy
  surname match flagged for confirmation; ✅/🔍/❌ states shown) — verified in
  browser during M1 and re-exercised during M4 testing.
- Picker-only path (search + position/team filters) verified in M1.
- Rule violations: the validator emits 13 distinct error codes (16 check
  sites): `SQUAD_SIZE`, `SQUAD_COMPOSITION`, `BUDGET_EXCEEDED`, `CLUB_LIMIT`,
  `XI_SIZE`, `BENCH_SIZE`, `BENCH_ORDER`, `CAPTAIN_NOT_IN_XI`,
  `VICE_CAPTAIN_NOT_IN_XI`, `CAPTAIN_VC_SAME`, `BANK_RANGE`, `CHIP_RANGE`,
  `PLAYER_UNAVAILABLE` — each has a specific message and is covered by
  `backend/tests/test_rules.py` (part of the 139-test green suite); the UI
  validation panel was verified to show per-rule messages in the browser.
- Two lineups saved (real + test duplicate), current flag switched via the
  ⋯ menu — verified in browser during M4 (T4.3).

## AC3 — Generate → 3 cards, every constraint checked

**PASS.**

- Generate returns 3 profiles (max_ep / differential / safe), each a fully
  validated lineup: 15 players 2/5/5/3, ≤£100.0m, ≤3/club, XI = 11 with
  1 GK / ≥3 DEF / ≥1 FWD, captain + VC in XI and distinct, bench ordered
  1–4 — enforced by `validate_lineup` on every solve.
- Today's `scripts/dev_check.py` run (live data): all 3 profiles solved;
  e.g. max_ep objective 76.374, C=Groß VC=Tarkowski (VC no multiplier),
  transfer diff with penalty math and bank-after; differential notes
  low-ownership picks (Bogle 6.5%, Maitland-Niles 1.2%, …).
- Cards show pitch view, transfer diff (in/out, sell values, cost delta,
  free transfers used, bank after, penalty), chip advice (4 chips, use /
  consider / skip + reason), rationale notes, and the manual apply checklist
  — verified in browser (M1 + M4).

## AC4 — Unavailable players, VC math, transfer math

**PASS.**

- `status='u'`/`'s'` players are hard-excluded from the solver universe
  (`PLAYER_UNAVAILABLE` + universe filter); covered by `test_ep_model.py`
  (A(p) gate) and `test_solver.py` — never suggested.
- VC math: 2026/27 rules re-verified today on the live rules page — "Your
  captain's score will be doubled. If your captain plays no minutes … the
  captain will be changed to the vice-captain." VC has **no multiplier** of
  its own; app's scoring + dev_check output ("VC no multiplier") match.
- Transfer math spot-check: live rules page example "purchased £7.5m,
  increased to £7.8m → selling price £7.6m" matches the app's half-increase
  rule (75→78 sells at 76) covered in `test_transfers.py`; −4 per extra
  transfer, 20-transfer cap, bank 1–5 all re-verified against the live rules
  page today (see Season rollover dry pass, item 1).

## AC5 — LLM on / off

**PASS.**

- **LLM on** (M2, user's local endpoint `http://localhost:8888/v1`): refresh
  all → signals with LLM summaries + confidence bands; a strong negative
  signal on an XI player → regenerated suggestions dropped the player /
  `adjusted` EP dropped.
- **LLM off** (empty key, current state): refresh → rule-based signals still
  appear (confidence ≤ 0.6); suggestions still valid (139 tests green;
  dev_check run today with no LLM).

## AC6 — (adjusted) startup refresh banner

**PASS (adjusted scope).** Original live-match criterion (≤60 s updates,
auto-bench, bonus race) was dropped with the M3 scope revision; the revised
criterion is:

- Restart the app → top-right banner shows the news refresh running →
  completion summary with per-source row counts — verified in browser across
  multiple restarts today.
- Fresh page load within 15 min of a completed run still shows the
  "done" banner (state persisted in `meta`) — verified.
- Today's 22:30 startup refresh: bbc 0 new / reddit 1 new / youtube 0 new
  (dedupe on content hash confirmed).

## AC7 — Group rank card

**SKIPPED (user decision).** The group-rank card and entry-API work (T4.4)
are out of scope for v1; the spec is preserved in `PLAN-5-M4-POLISH.MD` for
a future milestone. `group.fpl_entry_id` remains a config knob for when it
ships.

## AC8 — README review (every command run as written)

**PASS.** Every command in the final README was executed on 2026-09-20:

| Command | Result |
|---|---|
| `start.ps1 -Prod` (fresh clone) | PASS (AC1) |
| `start.ps1 -Dev` | PASS (AC1) |
| `start.ps1 -Prod -Port N` / `-Rebuild`, `start.sh --*` | flags implemented + reviewed; port override exercised in dev smoke |
| `cd backend; ..\.venv\Scripts\python -m pytest tests -q` | 139 passed |
| `..\.venv\Scripts\python scripts\dev_check.py` | `DEV CHECK PASSED` |
| `cd frontend && npx tsc --noEmit && npx vite build` | clean build, 234.42 kB |

## Season rollover dry pass (T4.10)

Executed 2026-09-20 against the live 2026/27 season. All 9 items **PASS**.

1. **Rules page** (fetched live via browser, all accordions expanded):
   - Transfers: 1 free transfer/GW after GW1, **−4 pts per extra**, max
     **5 stored**, **20-transfer cap** (excl. wildcard/free hit), sell-on
     **half the increase, rounded down** (7.5→7.8 → 7.6) — all match
     `optimizer/rules.py` + `PLAN.MD` §5.5/§5.6.
   - Captaincy: captain ×2; **VC has no multiplier**; VC takes over the
     doubling only if the captain plays 0 minutes — matches app.
   - Chips: 2 sets per chip; set 1 until GW19 deadline (Fri 1 Jan 10:30),
     set 2 after; **Free Hit cannot be played in consecutive GWs**
     (GW19 → earliest GW21); saved free transfers retained when playing a
     chip — all match the app's chip logic + `chips` table.
2. **Chip windows**: `chips` table has 8 rows = 2 sets × 4 chips
   (wildcard/freehit GW2–19 & GW20–38; bboost/3xc GW1–19 & GW20–38) —
   matches the rules page text.
3. **Scoring table**: `PLAN.MD` §5.9 vs live "How are points scored?" —
   every row matches (appearance 1/2, goals 10/6/5/4, assist 3, clean sheet
   4/1, saves, defensive contributions 10/12, penalty save 5 / miss −2,
   bonus 1–3, goals conceded −1/2, yellow −1, red −3, own goal −2).
4. **BPS table**: `PLAN.MD` §5.10 vs live "How is the BPS score
   calculated?" — every row matches (appearance 3/6, goals 12/12/18/24,
   assist 9, clean sheet 12, saves 2/1/1, penalty save 7, chance creation
   1/3, crosses/tackles/dribbles, match-winner 3, goalline 9, pass
   completion 2/4/6, all negatives −4…−1).
5. **Club list**: 20 teams in DB; name map auto-rebuilds at first run
   (verified at each fetch).
6. **Player universe**: 667 players — GK 73 / DEF 217 / MID 298 / FWD 79;
   prices £3.9m–£15.6m (avg £5.15m); `meta.season = 2026/27`; old-season
   signals wiped by rollover logic.
7. **LLM model**: user-managed local endpoint (`http://localhost:8888/v1`,
   key-gated, verified 401 without key); no model change required this
   season.
8. **yt-dlp**: installed **2026.8.19** (installed 2026-08-19, ~1 month old)
   — current; run `pip install -U yt-dlp` at the next rollover.
9. **First-deadline check**: API GW1 deadline = `2026-08-21T17:30:00Z`;
   the app's countdown uses the API value by design (the rules-page table
   shows "Fri 21 Aug 10:30", which lags final scheduling — API wins, per
   spec). Current state: GW5 current, GW6 deadline `2026-10-10T10:00:00Z`.

## Verdict

AC1–AC5, AC6 (adjusted), AC8 **PASS**; AC7 **SKIPPED** (user decision).
Season rollover dry pass **9/9 PASS**. → **v1.0.0** (2026-09-20).