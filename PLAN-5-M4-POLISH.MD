# FPL Team Optimizer — Part 5: M4 Polish, Packaging & Final Acceptance

> **Prereqs**: M3 complete. This part covers tasks **T4.1–T4.10**.
> **M4 exit criteria** (outline §10): all acceptance criteria AC1–AC8 (PLAN.MD §1) pass — executed as T4.9.
> M4 is where the app becomes *the user's daily tool*: sharp differential advice, full chip logic, complete settings, production packaging, final docs.

---

## T4.1 — Differential profile tuning

**Goal**: make the Differential profile actually useful for mini-league climbing (D8).

**Files**: `backend/app/optimizer/solver.py` (extend `objective_differential`), config already has `differential_lambda`.

**Spec**:
- Objective (refined): `max_ep + λ · Σ_XI (1 − selected_by_percent/100)` — keep, but add:
  - **Eligibility floor**: differential candidates must have `ep_final ≥ 0.4 × max_ep_final_in_universe` (prevents "differential" degenerating into low-EP lottery picks); floor configurable (`optimizer.differential_ep_floor`, default 0.4).
  - **Convergence handling** (formalize T1.7.5): if the differential solution's player set equals Max EP's → try: (1) captain = 2nd-best ep in XI; (2) swap in the best-EP player not owned by >25% (hard constraint for the variant); (3) still identical → label `variant_of: "max_ep"` and say so in the card subtitle ("variant of Max EP — same best team found").
  - **Rationale enrichment**: each XI player with `selected_by_percent < 10` gets a rationale line: `f"Differential: {name} owned by {own:.1f}% (top-50 EP)"`.
- **UI**: Differential card gets a "mini-league edge" one-liner: how many of its XI players are owned by <10% of the FPL public (a proxy for group separation).

**Acceptance**: `test_solver.py` additions: floor excludes a low-EP differential; convergence → labeled variant path exercised (crafted universe where all profiles converge); rationale lines present.

---

## T4.2 — Chip advice v2

**Goal**: full chip logic per §5.8 (windows, consecutive-Free-Hit ban, 3XC/BB candidates).

**Files**: `backend/app/optimizer/transfers.py` (replace `chip_advice_v1` with `chip_advice_v2`; keep v1 as a fallback when chip data is missing).

**Spec**:
```python
def chip_advice_v2(diff, chips, lineup, next_gw, cfg) -> list[dict]:
    """Evaluate ALL four chips; return advice for each (recommendation: use|consider|skip + reason).
    1. WINDOWS: a chip is only recommendable if an active window (active_chip_windows) covers next_gw
       AND the user has that chip remaining (chips dict) — else recommendation 'skip', reason 'window closed'.
    2. WILDCARD: diff.transfers > bank → 'use' (reason: makes N transfers free, retains bank).
       Also 'consider' when bank ≤ 1 and diff.transfers ≤ bank but ≥ 4 (wildcard frees future options).
    3. FREE HIT: same trigger as wildcard; ADD: cannot recommend if the user played Free Hit in the
       previous GW (consecutive ban, §5.8) — reason 'cannot follow a Free Hit GW'.
       Data: store played chips per GW — add table chip_plays_log (lineup_id NULL, gw INTEGER, chip TEXT,
       played_at TEXT; UNIQUE(gw, chip)) — user confirms plays via a small "chips used this GW" toggle on
       the My Team page (M4 UI, T4.3) OR the app infers from diff when the user marks a suggestion 'applied'
       (add POST /api/suggestions/{id}/apply → logs transfers+chip, sets bank_after on the lineup).
    4. TRIPLE CAPTAIN: 'consider' when captain ep_final ≥ 7.0 (next GW); 'use' when ≥ 8.5 or when
       captain is a confirmed-starter signal (conf ≥ 0.7) with ep_final ≥ 7.0; 'skip' otherwise.
       (3XC + Wildcard in the same GW is legal but rarely optimal — note in reason when both triggered.)
    5. BENCH BOOST: 'consider' when Σ bench players' ep_final ≥ 0.5 × XI avg ep_final (bench is strong)
       AND a user match that GW is a multi-match GW; 'skip' otherwise. Estimate: "bench adds ~{sum:.1f} pts".
    Every recommendation passes the CHIP_WINDOW guard (§8.9) — invalid advice is replaced by 'skip'."""
```
**API**: SuggestionObject `chip_advice` uses v2 output; `POST /api/suggestions/{id}/apply` (new endpoint): marks the suggestion applied, logs `chip_plays_log` for the chosen chip (body: `{chip: "wildcard" | null}`), updates the lineup's `transfer_bank` to `bank_after` and swaps the squad to the suggested one (with confirmation — it rewrites the user's saved lineup; UI double-confirms with a typed "REPLACE" prompt).

**Acceptance**: `test_transfers.py` v2 section: window gating (GW19 vs GW20 sets); consecutive-FH block; 3XC use/consider/skip thresholds; BB estimate math; `apply` endpoint updates bank + squad + chip log (API test).

---

## T4.3 — Saved test lineups UX polish

**Goal**: the D4 testing workflow (hypothetical lineups) becomes first-class.

**Files**: `frontend/src/pages/MyTeam.tsx` (extend), `components/LineupTabs.tsx` (extend).

**Spec**:
- LineupTabs: each tab shows a badge: ✓ current; ⚗ test (auto-labeled: any lineup not marked "real" — add `lineups.kind` column: `'real' | 'test'`, default 'real', settable on create/edit); per-tab menu: rename, duplicate, delete, "run suggestions" (jumps to Suggestions with this lineup selected), "set as current".
- "Duplicate" = copy squad+meta under a new name (kind 'test') — the one-click way to spin up a "what-if" from the real team.
- Suggestions page: when a **test** lineup is selected, the card header shows a ⚗ badge and the apply checklist is hidden (replaced by "Test lineup — apply checklist disabled; duplicate your real team to act on this").
- `apply` endpoint (T4.2) only available for kind='real' lineups (409 otherwise).

**Acceptance** (manual): duplicate real team → rename "Test A: all-differentials" → edit freely → generate → badges correct, no apply checklist; real team still applies normally.

---

## T4.4 — Optional entry-ID group-rank card (AC7) — SKIPPED

**Status: SKIPPED (user chose "skip" on 2026-09-20 — no public entry ID provided).**
The card stays hidden with `group.fpl_entry_id` empty (default); no errors, no UI surface.
AC7 is therefore out of scope for T4.9. Implementation below is kept for a future season if the user opts in.

**Goal**: public rank display (D8, outline §5.4) — no login, public endpoint only.

**Files**: `backend/app/api/entry.py` (implement), `backend/app/fetchers/fpl.py` (`fetch_entry`), `frontend/src/pages/Dashboard.tsx` (card), `Settings` (entry ID input — T4.5).

**Spec**:
- `fetch_entry()`: `GET https://fantasy.premierleague.com/api/entry/{id}/` → cache 15 min (in-memory + `poll_log`). Fields: `overall_points`, `rank`, `rank_out_of` (percentile = rank/rank_out_of), `leagues[]` (`id`, `name`, `class`, `league_type` — custom mini-leagues appear as `league_type: "x"`), per-league `rank`, `size` (from `league.size` or `rank_out_of` within league if present).
- `GET /api/entry` per PLAN.MD §8.8 (400 when `config.group.fpl_entry_id` empty).
- **Dashboard card** (only when entry ID set): "My group position": overall points, rank + percentile ("top 1.7%"), table of leagues (name, rank, size, type badge "mini-league" for `x`); footer: "updates as gameweeks finalize (official ranks publish after each GW)".
- Scheduler: `entry_rank` job (15 min, registered in M3 as stub) activates.

**Acceptance**: with a real public entry ID (user's own — no login needed): card renders with sane numbers; invalid ID → 400 with friendly message; unset → card hidden, no errors anywhere.

---

## T4.5 — Settings page (full) — DONE (2026-09-20)

> Status: complete. All six sections shipped in `Settings.tsx` (single-page; `SettingsSections.tsx` folded in as
> section blocks). Backend additions: `POST /api/signals/clear`, `GET /api/meta/db-stats`, `pulp_available` in
> `GET /settings`. Save round-trip browser-verified (edit → save → persists to `config.json` on disk → reload).
> Deviations: Group "Test" button omitted (T4.4 entry API skipped); "Re-fetch all now" uses existing
> `POST /api/refresh/all`; YouTube "resolve" is server-side (fetcher resolves handle→channel_id on first poll,
> so the UI only needs handle-or-ID rows).

**Goal**: every knob from `config.json` editable in the UI (PLAN.MD §10).

**Files**: `frontend/src/pages/Settings.tsx` (replace M1 read-only version) + `components/SettingsSections.tsx`.

**Sections** (all via `GET/PUT /api/settings`, server-side validation):
1. **LLM**: enabled toggle, base URL, API key (password input, masked, "stored locally only"), model, timeout. Live "Test connection" button → `POST /api/settings/test-llm` (new endpoint: trivial chat completion, returns model echo or the 401 message).
2. **Sources**: per-source enabled toggle + interval; Reddit mode (rss/oauth + client id/secret, disabled-look until ASK resolved); YouTube: channel list (add by handle or ID, resolve button, remove), transcript keywords (comma list), max transcripts/poll.
3. **Optimizer**: weights (ep/form/fixture, must sum to 1 — client validates), availability map, signal coefficients, differential λ + EP floor, solver (restarts, timebox, seed, exact-ILP toggle → greyed until PuLP installed).
4. **Group**: FPL entry ID (number) + "Test" button (calls `/api/entry`).
5. **Data**: "Re-fetch all now" (`POST /api/refresh/all`), "Clear all signals" (`POST /api/signals/clear` — new endpoint, `DELETE FROM signals`), DB stats (row counts per table, DB file size, last 10 poll_log errors).
6. **About**: version, sources + ToS summary, links to OUTLINE.MD/PLAN.MD.

**Acceptance** (manual): every field round-trips (edit → save → reload persists to `config.json` on disk); LLM test button shows 401 message when key empty; signals clear empties the table; validation rejects weights not summing to 1.

---

## T4.6 — Optimizer weights UI (advanced) — DONE (2026-09-20)

> Status: complete. `WeightSlider.tsx` ships 3 sliders + live sum indicator (✓/must-be-1.00), Auto-normalize
> (rounding-error absorbed by fixture so the sum is exactly 1.00), Reset to defaults, and per-weight "what this
> does" tooltips. Server-side sum validation (`PUT /settings` ±0.01) plus client pre-save check.

**Goal**: the §5 weights are visible/tunable without editing JSON (folded into T4.5 section 3 — this task adds the presentation + sanity UX).

**Files**: `frontend/src/components/WeightSlider.tsx`.

**Spec**: three sliders (EP / Form / Fixture) with a sum indicator (must equal 1.00 — auto-normalize button); a "what this does" tooltip per weight (EP = official FPL projection; Form = last-5-GW points; Fixture = opponent difficulty); "Reset to defaults" (0.7/0.15/0.15). Changes apply immediately to the next Generate (no rebuild).

**Acceptance** (manual): moving form weight to 0.4 and regenerating changes at least one suggestion on a real squad (sanity: form-weighted picks differ from EP-weighted); reset restores defaults.

---

## T4.7 — Packaging (production mode final) — DONE (2026-09-20)

> Status: complete. `start.ps1`/`start.sh` finalized: mode auto-detect (prod if
> `frontend/dist` exists, else dev — printed notice), `-Prod`/`-Dev`/`-Port`/`-Rebuild`
> (`--prod`/`--dev`/`--port`/`--rebuild`), Python detection chain (`py -3.11` → `py -3` →
> `python` → `python3`), pip install gated on a SHA-256 marker of `requirements.txt`
> (`.venv/.reqs.sha256`), npm install gated on `node_modules` presence with a
> project-local-cache retry for restricted environments, `npm run build` when dist
> missing or `-Rebuild`, winget hints for missing Python/Node, console kept open on
> crash (Read-Host). Dev-mode `--port` works via `FPL_API_TARGET` picked up by
> `vite.config.ts` proxy. `main.py` static mount was already spec-compliant (SPA
> fallback; unknown `/api/*` → JSON 404, verified live). README quick-start +
> Settings section updated. Fresh-clone simulation PASSED: wiped `.venv`,
> `node_modules`, `dist`, `data/fpl.db` → `start.ps1 -Prod` → venv+pip+npm+build all
> ran, server on :8000 with live FPL data (667 players) + startup news refresh
> (bbc/reddit/youtube ok) + working SPA deep-links; user data restored from backup
> afterwards. Both launchers are ASCII-only (Windows PowerShell 5.1 mis-parses
> UTF-8-no-BOM scripts with non-ASCII chars — first draft hit this).

**Goal**: AC1 — one command, one port, robust restarts.

**Files**: `start.ps1`/`start.sh` (finalize), `backend/app/main.py` (static mount final), `README.md` (setup section final).

**Spec**:
- `start.ps1 --prod`: venv check → pip install (if missing) → npm install (if missing) → `npm run build` (if `dist` missing or `--rebuild`) → single uvicorn on 8000 serving API + `frontend/dist` (SPA fallback: any non-`/api` GET → `index.html`).
- Dev mode stays two-process with the Vite proxy.
- Port override `--port`; `--dev`/`--prod` explicit (default: prod if `dist` exists, else dev — with a printed notice).
- Windows niceties: `start.ps1` detects the user's Python (`py -3.11` → `python` → `python3`), prints clear error + fix for missing Node (winget hint), keeps the console open on crash (Read-Host).
- `main.py` static mount: `app.mount("/", StaticFiles(directory=dist, html=True), name="static")` **after** `/api` routes; 404 for unknown `/api/*` must not fall through to the SPA.
- Smoke: fresh-clone simulation (delete `.venv`, `node_modules`, `dist`, `data/` → `start.ps1 --prod` → full app with live data).

**Acceptance** (manual checklist item 7): production mode single-port verified incl. SPA route deep-links (`/suggestions` reload works); fresh-clone simulation passes.

---

## T4.8 — Final README + open-item closure

**Goal**: AC8 + close PLAN.MD §13 open items.

**Files**: `README.md` (final), `backend/app/fetchers/yahoo.py` (optional stub).

**README sections** (final):
1. **What it is** (2 paragraphs: data sources, manual team input, 3 suggested lineups, real-time; no FPL login by design).
2. **Quick start** (prereqs: Python 3.11+, Node 18+; `start.ps1` / `start.sh`; dev vs prod; first-run bootstrap ~30 s).
3. **Configuration** (`.env` LLM keys — endpoint verified at `http://localhost:8888/v1`, key-gated; `config.json` tour; Settings page).
4. **Sources & ToS posture** (table from PLAN.MD §4.1 + §4.2: light polling, RSS only, no FPL account access, no writes; Reddit OAuth option: create a free "script" app at reddit.com/prefs/apps, paste client id/secret in Settings — **ASK the user whether to keep this section prominent or collapsed**; Yahoo: **verify once now** — try the current public endpoint, if it works add it as an optional source behind a config flag, if not document "unsupported in v1, ESPN covers live data" and delete the stub).
5. **How suggestions work** (EP model formula, A(p)/S(p), 3 profiles explained in plain language, transfer math incl. −4 and half-increase rule, chip advice rules, disclaimer: "projections, not guarantees; verify news on the official site before the deadline").
6. **Adding sources/channels** (how to add a YouTube channel: handle or ID in Settings; how a new fetcher plugs in: implement the `ingest_item` pattern + a fetcher module + scheduler job — 10-line guide).
7. **Troubleshooting** (LLM 401 → key; yt-dlp bot-check → `pip install -U yt-dlp`, ensure Node present; schema-drift banner → what it means, app keeps working on last-good data; "no data" → run `POST /api/refresh/all` / check network; ports in use → `--port`).
8. **Manual test checklist** (PLAN.MD §11.2, final version).
9. **Milestones & status** (M1–M4 done dates, simulated-vs-real note for M3).

**Acceptance**: README renders cleanly (no broken links, every command tested as written); Yahoo closure documented (either way); Reddit OAuth decision made (ASK).

---

## T4.9 — Full acceptance run (AC1–AC8)

**Goal**: execute PLAN.MD §1 success criteria one by one; record results in `ACCEPTANCE.md` (new file: criterion, method, evidence, pass/fail, date) and in the final commit message.

**Run order** (fresh state where cheap: reset `data/fpl.db` once at the start, keep saved lineups via export/import — or just rebuild the test lineups):
1. **AC1** — fresh clone → `start.ps1 --prod` → UI at :8000; also dev mode at :5173. Evidence: screenshots or curl of both.
2. **AC2** — enter a real 15-man squad via paste (include 1 deliberate typo → fuzzy confirmation), then re-enter via picker-only path; break each of the 16 rules once → specific message each; save 2 lineups (real + test), switch.
3. **AC3** — Generate → 3 cards; check every card: 15 players 2/5/5/3, ≤£100.0m, ≤3/club, XI 11 with 1 GK/≥3 DEF/≥1 FWD, captain+VC in XI distinct, bench ordered 1–4, diff sums (cost 1000, bank math, −4 correct), chip advice, rationale, apply checklist.
4. **AC4** — inject a `status='u'` player into the universe (DB edit or dev script) → regenerate → never suggested; hand-verify VC math on one card (captain 0-min scenario simulated); transfer math spot-check against §5.5/§5.6 (pick a player with a price rise → sell value = half-increase).
5. **AC5** — LLM on: refresh all → signals with LLM summaries; toggle a strong negative signal on a XI player → regenerate → he drops out / `adjusted` drops. LLM off (empty key): refresh → official-news + keyword signals still appear; suggestions still valid.
6. **AC6** — during a live match (real, or the M3 simulation script): ≤60 s updates, no manual refresh, countdown exact, auto-bench visible, bonus race renders.
7. **AC7** — set the user's real entry ID → rank card renders with plausible numbers vs the official site (user visually cross-checks one league rank).
8. **AC8** — README review: a fresh person could set up from it (checklist: every command run once as written).

**Gate**: all 8 pass → final commit `M4: polish, packaging, acceptance (AC1–AC8 pass)` → **project complete**. Any fail → fix and re-run that criterion only.

---

## T4.10 — Season-rollover checklist (for the user + future maintenance)

**Goal**: the app is season-agnostic (D9/API-driven), but a few human re-checks happen each August. Write this into README §"New season" and run it proactively in T4.9 as a dry pass.

**Checklist** (each item: what, where, why):
1. **Rules page** (`fantasy.premierleague.com/en/help/rules`): re-read §5 of PLAN.MD — transfers (bank cap, −4, 20-cap), captaincy (VC multiplier!), chips (windows, split point), scoring table. Update `optimizer/rules.py` constants + this plan doc if anything changed. (2026/27 already changed transfers + VC — this is why the checklist exists.)
2. **Chip windows**: app reads `chips[]` from the API — verify the 8 rows look right (two sets per chip) in `/api/meta/season.chip_windows`.
3. **Scoring table appearance points**: cross-check §5.9 against the rules page (points for plays/goals/clean sheets).
4. **BPS table**: cross-check §5.10 (drives bonus display estimates).
5. **Club list**: 20 clubs — the name-map (FPL `teams[]` ↔ ESPN abbreviations) auto-rebuilds at first run; watch `poll_log` for "team map mismatch" warnings in GW1.
6. **Player universe**: first bootstrap of the season → sanity: ~500 players, 4 positions, prices sum sensible; `players.season` bumped; old-season `signals`/`live_*` wiped (season-rollover logic, PLAN.MD §7).
7. **LLM model**: if the user's local model changed → update `.env`.
8. **yt-dlp**: `pip install -U yt-dlp` (YouTube extraction is moving targets).
9. **First-deadline check**: GW1 deadline from the API (never the outline table) — confirm countdown shows the right time.

**Acceptance**: `ACCEPTANCE.md` includes a "Season rollover dry pass" section with the 9 items checked against the live 2026/27 season; README §"New season" written.

---

## Final state

When T4.9 passes all 8 criteria and T4.10's dry pass is recorded:
- Repo: `backend/` (full app + green pytest suite), `frontend/` (buildable), `data/` (live DB), launchers, `.env.example`, `config.json`, `README.md`, `ACCEPTANCE.md`, `OUTLINE.MD`, `PLAN*.MD` (5 parts).
- The user's daily loop: open `http://localhost:8000` → Dashboard (live if in a window) → My Team (paste/pick, save, test lineups) → Suggestions (3 profiles, diffs, chips, apply checklist) → News (signals + sources) → Settings (any knob) → copy the checklist into the official FPL site before the deadline.