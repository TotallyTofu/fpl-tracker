# FPL Team Optimizer — Part 2: M1 Core (end-to-end usable app)

> **Prereqs**: read `OUTLINE.MD` + `PLAN.MD` (all of it). This part covers tasks **T1.1–T1.16**.
> **M1 exit criteria** (outline §10): user enters a real squad → gets 2–3 rule-valid suggestions with diffs, transfer math, captain pick. FPL fetchers + schema + manual team entry (picker + paste) + rules validator + optimizer on `ep_next` only (no news yet) + Suggestions page + static Dashboard + README.
> **Scope note**: M1 has **no** BBC/ESPN/Reddit/YouTube fetchers, **no** LLM, **no** live polling. The optimizer uses `ep_next` only; `A(p)`/`S(p)` hooks exist but are inert (default pass-through) so M2 slots in without refactoring.

---

## T1.1 — Repo scaffold

**Goal**: project skeleton that runs (empty API + empty frontend) and is git-initialized.

**Files**:
- `git init` at workspace root; `.gitignore`: `.env`, `data/`, `__pycache__/`, `*.pyc`, `node_modules/`, `frontend/dist/`, `.venv/`, `tmp-video-test/` (keep the transcript visible but untracked? — **no**: `tmp-video-test/` is a local artifact; ignore it; the fixture copy is tracked in `backend/tests/fixtures/`).
- `backend/requirements.txt` (pin exact versions at install time; minimum set):
  ```
  fastapi>=0.115
  uvicorn[standard]>=0.30
  apscheduler>=3.10
  httpx>=0.27
  feedparser>=6.0
  python-dotenv>=1.0
  pydantic>=2.7
  pytest>=8.0
  # optional, M4 exact solver:
  # pulp>=2.8
  ```
  (yt-dlp is a **system** dependency, not a pip dep of the backend — it's invoked as a subprocess and is already installed on the user's machine; README documents `pip install -U yt-dlp`.)
- `backend/app/__init__.py`, `backend/app/main.py` (FastAPI app factory: CORS allow `http://localhost:5173`, `/api` router mount, static mount of `frontend/dist` if it exists, lifespan hook that starts/stops the scheduler — scheduler file is a stub in M1), `backend/app/config.py`, `backend/app/db.py`, `backend/app/httpclient.py`, `backend/app/scheduler.py` (stub: `create_scheduler(app) -> AsyncIOScheduler` with no jobs yet).
- `frontend/`: `npm create vite@latest frontend -- --template react-ts` (Node 18+); add `react-router-dom`; `vite.config.ts` with dev proxy: `server.proxy['/api'] = 'http://localhost:8000'`.
- `.env.example` (per PLAN.MD §10.1), `config.json` (defaults per PLAN.MD §10.2), `data/.gitkeep`.
- `start.ps1` / `start.sh` (see T1.15).
- `README.md` stub (expanded in T1.15/T4.8).

**Key functions**:
```python
# config.py
class Settings(BaseModel):
    llm_base_url: str = "http://localhost:8888/v1"
    llm_api_key: str = ""
    llm_model: str = ""
    config: ConfigFile          # pydantic model mirroring config.json (§10.2)
def load_settings() -> Settings   # .env (python-dotenv) + config.json; missing config.json → defaults written to disk
# db.py
def init_db(path: str = "data/fpl.db") -> None   # executes PLAN.MD §7 DDL, WAL, foreign_keys
def get_conn() -> sqlite3.Connection              # per-call connection, row_factory=sqlite3.Row
# httpclient.py
class HttpClient:            # singleton
    async def get_json(self, url: str, *, params: dict | None = None) -> Any
    async def get_text(self, url: str) -> str
    # internals: one httpx.AsyncClient(timeout=15, headers={"User-Agent": "fpl-tracker/1.0 (local personal use)"}),
    # retry 2x backoff 2s on 5xx/timeout, semaphore(20)
```

**Acceptance**: `uvicorn app.main:app` boots; `GET /api/meta/season` returns `501 {error: "no data yet"}` (or empty-shape 200 — pick 200 with `season: null` and log); frontend scaffold renders a "FPL Tracker" shell at :5173; `pytest` runs (0 tests, green).

---

## T1.2 — DB layer

**Goal**: full schema live, upsert helpers, poll_log helper.

**Files**: `backend/app/db.py` (extend T1.1 stub with the complete PLAN.MD §7 DDL).

**Key functions**:
```python
def upsert_players(rows: list[dict]) -> int          # INSERT ... ON CONFLICT(id) DO UPDATE (all columns except id); returns count
def upsert_fixtures(rows: list[dict]) -> int         # ON CONFLICT(event, home_team, away_team) DO UPDATE
def upsert_events(rows: list[dict]) -> int
def upsert_chips(rows: list[dict]) -> int
def log_poll(source: str, status: str, rows: int | None = None, error: str | None = None, started_at: str) -> None
def check_season_rollover(new_season_marker: str) -> bool   # wipe per PLAN.MD §7; returns True if wiped
```
**Logic**: all upserts in one transaction; `fetched_at` = now-UTC; JSON sub-fields (`price_change_projections`, `chip_plays`, `raw_json`) stored via `json.dumps`. `raw_json` keeps the full API object for drift forensics.

**Acceptance**: running `init_db` twice is idempotent; a unit test inserts a fake player row and re-inserts with changed cost → row updated, count 1.

---

## T1.3 — FPL fetcher

**Goal**: all M1-relevant FPL data in SQLite, with raw logging and schema-drift guard (D18).

**Files**: `backend/app/fetchers/fpl.py`.

**Endpoints & mappings** (all via `HttpClient`):
| Call | Writes | Notes |
|---|---|---|
| `GET /api/bootstrap-static/` | `players` (upsert all `elements[]`), `events` (all `events[]`), `chips` (all `chips[]`), `fixtures` **partial** (see below) | Parse every V3-verified field; missing optional field → NULL; `status` NULL when absent (means available) |
| `GET /api/element-summary/{pid}/` for **one player per team** (20 calls, lowest `id` per `team` from bootstrap) | `fixtures` (merge per-player `fixtures[]` into team-level rows: `difficulty_home`/`difficulty_away` set from the player's perspective) | 20 calls × 15-min cadence is fine (≤20 concurrent, D17); cache: skip re-fetch if fixture already has `difficulty_*` set and `event` > current_gw |
| `GET /api/game-settings/` | in-memory `Settings` cache (budget 1000, max_transfers 15, scoring table) | sanity-check budget == 1000; mismatch → `poll_log` error (non-fatal) |
| `GET /api/entry/{id}/` | in-memory cache | **only** if `config.group.fpl_entry_id` set (M4 feature; fetch stub now) |

**Required-field drift check** (D18): after each bootstrap parse, verify presence of: `elements[].{id, web_name, element_type, team, now_cost, ep_next, status, can_select, selected_by_percent}` and `events[].{id, deadline_time}` and `chips[].{name, start_event, stop_event}`. Any missing → `log_poll('fpl', 'error', error='schema drift: <fields>')` + include in `/api/meta/season.schema_drift`.

**Key functions**:
```python
async def fetch_bootstrap() -> dict          # returns {players, events, chips, drift: [...]}
async def fetch_fixtures() -> int            # 20 element-summary calls → upsert team-level fixtures
async def fetch_game_settings() -> dict
async def refresh_all_fpl() -> None          # orchestrates the above; called by scheduler (M3) + POST /api/refresh/fpl
```

**Acceptance**: `POST /api/refresh/fpl` (endpoint exists from T1.15 meta API, or a dev script) populates: 500+ players, 38 events (GW5 `is_current=1`, GW6 `is_next=1`), 8 chips, 20×~19 fixtures with both difficulty values; `poll_log` has `ok` rows; drift list empty.

---

## T1.4 — Season service

**Goal**: one place to answer "what GW are we in, when's the deadline, which chip windows are active, are we in a live window?"

**Files**: `backend/app/season.py`.

**Key functions**:
```python
def current_season() -> dict     # {season, current_gw, next_gw, deadline_is_past, live_mode, live_window, chip_windows, schema_drift}
def deadline_for(gw: int) -> str | None        # from events table (D9 — never a hardcoded table)
def active_chip_windows() -> list[dict]        # chips[] joined with current/next GW: which (chip, set) is playable now/next
def live_window(current_gw_fixtures: list[dict]) -> tuple[str|None, str|None]
    # LIVE mode when any current-GW fixture kickoff ∈ [now−2h, now+2h] (outline §5.2); M1: computed but unused by UI until M3
```
**Logic**: `live_window` = (min(kickoff of fixtures in window) − 2h, max(kickoff) + 2h) or (None, None). `deadline_is_past` = `now > next_gw.deadline_time`.

**Acceptance**: unit tests with seeded `events`/`fixtures` rows: correct current/next GW; countdown math exact; chip windows for GW6 (all set-1 chips active) and hypothetical GW20 (set-2 active); live window edges (kickoff exactly now−2h → in window).

---

## T1.5 — Rules validator (single source of truth)

**Goal**: `optimizer/rules.py` — validates any lineup (user input **and** solver output) against §5 of PLAN.MD, returning the §8.9 error catalog.

**Files**: `backend/app/optimizer/rules.py`.

**Key functions**:
```python
@dataclass
class SquadCheck:
    valid: bool
    errors: list[dict]        # [{"code": "BUDGET_EXCEEDED", "message": "Squad costs £100.1m (max £100.0m)"}]

def validate_lineup(players: list[PlayerRow], bank: int, chips: dict) -> SquadCheck
    # players: list of dicts {player_id, role, bench_order, is_captain, is_vice_captain}
    # PlayerRow: dict from DB with element_type, team, now_cost, status, can_select
```
**Check order** (all checks run; every violation reported, not just the first):
1. `SQUAD_SIZE` (≠15) → skip dependent checks gracefully.
2. `SQUAD_COMPOSITION` (counts per element_type ≠ 2/5/5/3).
3. `BUDGET_EXCEEDED` (Σ now_cost > 1000).
4. `CLUB_LIMIT` (any team count > 3).
5. `XI_SIZE` (starters ≠ 11), `BENCH_SIZE` (bench ≠ 4), `BENCH_ORDER` (orders not 1–4 unique).
6. `XI_POSITION_MIN` (starters: GK ≠ 1, DEF < 3, FWD < 1).
7. `CAPTAIN_NOT_IN_XI`, `VICE_CAPTAIN_NOT_IN_XI`, `CAPTAIN_VC_SAME` (exactly one captain and one VC must exist).
8. `BANK_RANGE` (not 1–5), `CHIP_RANGE` (any chip count not 0–2).
9. `PLAYER_UNAVAILABLE` — **severity depends on caller**: for suggestions (solver) it's a hard error; for user lineups it's a *warning* (code returned with `"severity": "warning"` in the dict — user's real team may contain injured players; suggestions should transfer them out). `validate_lineup(..., strict: bool)` controls this: `strict=True` (solver) → error; `strict=False` (user) → warning.
10. `TRANSFER_CAP` — invoked from transfer math (T1.8), not here.

**Acceptance**: `test_rules.py` golden suite (PLAN.MD §11.1) — 16 codes each triggered individually; a real valid squad passes clean; warnings don't fail user lineups.

---

## T1.6 — EP model

**Goal**: `optimizer/scoring.py` — the EP pipeline with M2 hooks inert.

**Files**: `backend/app/optimizer/scoring.py`.

**Key functions**:
```python
def ep_baseline(p: PlayerRow) -> float:
    """M1: return p.ep_next (0.0 if None)."""

def availability_multiplier(p: PlayerRow, cfg: OptimizerConfig) -> float:
    """M2-activated; M1 returns 1.0 for everyone EXCEPT hard gates:
    status in ('u','s') or can_select == 0 → 0.0 (excluded from suggestions).
    (doubt/chance map implemented in M2 T2.10 — code path exists, cfg-gated, defaults = pass-through in M1.)"""

def signal_adjustment(p: PlayerRow, signals: list[SignalRow], cfg: OptimizerConfig) -> float:
    """M2-activated; M1 returns 0.0. M2 formula (spec in Part 3 T2.10):
    S = clamp(sum(-0.5*conf for negative signals), -0.6) + clamp(sum(+0.1*conf for positive), +0.2)"""

def ep_final(p: PlayerRow, signals: list[SignalRow], cfg: OptimizerConfig, fixture_difficulty: int | None) -> float:
    """EP_final = (ep_baseline * A(p) * (1 + S(p))) blended:
    0.7 * that + 0.15 * form_adj + 0.15 * fixture_adj   (weights from config.optimizer.weights)
    form_adj: scale p.form (last-5-GW points) into EP range: form_adj = clamp(p.form, 0, 12) * 0.9  (M1: p.form often 0/None early season → contributes ~0, which is correct)
    fixture_adj: (3 - difficulty) * 0.5 for DEF/GK (clean-sheet sensitive), (3 - difficulty) * 0.25 for MID/FWD; None difficulty → 0
    difficulty = the player's team's fixture difficulty in the target GW (home or away value from fixtures table)"""

def score_lineup(squad: list[PlayerRow], xi: list[PlayerRow], captain_id: int, vc_id: int,
                 signals_by_player: dict, cfg) -> dict:
    """Return {baseline, adjusted, with_captain} per PLAN.MD §8.6:
    baseline = Σ ep_baseline(XI) + ep_baseline(captain)            # captain ×2
    adjusted = Σ ep_final(XI) + ep_final(captain)                   # captain ×2, VC ×1 (2026/27, §5.4)
    with_captain = adjusted (field kept for UI clarity)"""
```
**Invariants**: never raise on missing data (None → 0/neutral); deterministic (no randomness here).

**Acceptance**: `test_ep_model.py` M1 portion: ep_baseline passthrough; hard gates exclude u/s/can_select=false; captain doubling exact; VC not doubled; blend weights honored (unit tests with crafted rows).

---

## T1.7 — Solver

**Goal**: `optimizer/solver.py` — multi-start hill climbing producing one valid lineup per profile.

**Files**: `backend/app/optimizer/solver.py`.

**Key functions**:
```python
@dataclass
class SolveParams:
    current_squad: list[PlayerRow]      # user's 15 (with now_cost, origin='kept')
    bank: int
    chips: dict
    target_gw: int
    profile: str                        # 'max_ep' | 'differential' | 'safe'
    cfg: OptimizerConfig
    rng_seed: int = 42

def solve(params: SolveParams, universe: list[PlayerRow]) -> SolvedLineup
    """universe: all eligible players (hard gates applied: not u/s, can_select=1).
    Returns SolvedLineup {squad[15], xi[11], captain, vice_captain, bench[4 ordered], objective, projected_points}"""

def objective_max_ep(lineup, ...) -> float          # Σ ep_final(XI) + ep_final(captain)
def objective_differential(lineup, ...) -> float    # max_ep + λ * Σ_XI (1 - selected_by_percent/100)   (λ = cfg.differential_lambda, default 3.0)
def objective_safe(lineup, ...) -> float            # Σ ep_final * reliability
    # reliability(p) = A(p) (full M2 map) * (1 - 0.5*has_negative_signal) * (1 + 0.1*confirmed_starter)
    # M1: with S(p)=0 and A pass-through, safe ≈ max_ep * (confirmed-starter bonus) — acceptable per outline (degrades gracefully)
```
**Algorithm** (spec, deterministic):
1. **Seed**: greedy by `ep_final/price` respecting constraints (fill GK→DEF→MID→FWD in order 2/5/5/3, best ratio first, club cap 3, budget 1000); then pick XI = top 11 by ep_final (1 GK/≥3 DEF/≥1 FWD enforced by construction), captain = max ep_final in XI, VC = 2nd, bench = remaining 4 ordered by ep_final desc.
2. **Moves** (random order, `random.Random(seed + restart_index)`):
   - `squad_swap`: out = random squad member, in = random universe member of same position (club/budget feasible).
   - `xi_bench_move`: swap a starter with a bench player (position-feasible for XI validity).
   - `captain_change`: captain ← another XI member.
   - `vc_change`: VC ← another XI member (≠ captain).
   - `bench_reorder`: swap two bench orders.
3. **Accept** move iff `validate_lineup(strict=True)` passes **and** objective improves (hill climbing; no degradation accepted in v1).
4. **Restarts**: 20; keep best objective across restarts; total timebox `cfg.solver.timebox_sec` (default 8 s) checked between moves (cooperative — check every 200 moves); deterministic with fixed seed.
5. **Convergence guard** (profiles): if the solved lineup (as a player-id set + XI + captain) equals a previously solved profile's, vary: try a different captain (2nd-best ep), then a bench swap; if still identical, label the output `variant_of: <profile>` in the result (UI shows "variant of Max EP").
6. **Post-process**: transfer math (T1.8) against the user's current squad; assemble SuggestionObject (§8.6) including `origin` per squad member (`kept`/`transfer_in`).

**Optional exact mode** (M4): `pulp` ILP for squad selection behind `cfg.solver.exact_ilp` — stub function now (`def solve_ilp(...): raise NotImplementedError`), implemented in Part 5.

**Acceptance**: `test_solver.py` (PLAN.MD §11.1): synthetic 60-player universe (4 positions, 5 clubs, varied costs/ep/ownership, a few u/s players) → all invariants hold; deterministic (same seed → identical output); 3 profiles differ (or labeled variants); timebox respected (test with 1 s timebox).

---

## T1.8 — Transfer math & chip advice v1

**Goal**: `optimizer/transfers.py` — diff a solved squad against the user's squad with exact §5.5/§5.6 math + basic chip advice.

**Files**: `backend/app/optimizer/transfers.py`.

**Key functions**:
```python
def sell_value(bought_cost: int, now_cost: int) -> int:
    """Half-increase rule (§5.6): if now_cost > bought_cost: bought + (now_cost - bought_cost)//2  (floor to £0.1m)
    else now_cost.  Unit tests: (75,78)→76, (75,77)→76, (75,70)→70, (75,75)→75."""

def compute_diff(current_squad: list[PlayerRow], new_squad: list[PlayerRow], bank: int, chips: dict, cfg) -> dict:
    """transfers_in/out (with costs; out uses sell_value vs the user's bought cost —
    bought cost approximated by now_cost at lineup save time: store `bought_cost` per lineup_player in lineup_players
    (add column bought_cost INTEGER NULL; populated from players.now_cost at save), cost_delta = Σin - Σout,
    total_cost_after (must == 1000 ± 0 — assert ≤ 1000), free_transfers_used = min(transfers, bank),
    bank_after = max(0, bank - transfers) (if no chip), penalty_points = 4 * max(0, transfers - bank)
    (zero if a wildcard/freehit is recommended & used), TRANSFER_CAP check (>20 & no chip → error code TRANSFER_CAP)."""

def chip_advice_v1(diff: dict, chips: dict, current_gw: int, next_gw: int, cfg) -> list[dict]:
    """v1 rules (v2 with windows + consecutive-FH + 3XC/BB candidates in Part 5 T4.2):
    - transfers > bank and chips['wildcard'] > 0 → recommend wildcard ("use")
    - transfers > bank and chips['freehit'] > 0 and wildcard == 0 → recommend freehit ("use")
    - transfers <= bank → no transfer chip; if captain ep_next >= 7 and chips['triple_captain'] > 0 → ("consider", reason)
    Every recommendation must pass active_chip_windows() (T1.4) — else omit (CHIP_WINDOW guard)."""
```
**Acceptance**: `test_transfers.py` golden suite (PLAN.MD §11.1) — all sell-value cases, bank/penalty math, cap, chip advice windows.

---

## T1.9 — Lineups API

**Goal**: saved lineups CRUD + paste-a-list matching, per PLAN.MD §8.3.

**Files**: `backend/app/api/lineups.py`, `backend/app/signals/names.py`.

**Key functions**:
```python
# names.py
def match_names(names: list[str], players: list[PlayerRow]) -> list[dict]:
    """Per name: 1) exact web_name/known_name (case-insensitive) → status 'exact', score 1.0
    2) case-insensitive substring unique → 'fuzzy'
    3) token-sort + Levenshtein ≤ 2 on web_name & known_name → 'fuzzy', score = 1 - dist/len
    4) else 'no_match'. Return up to 5 candidates per name sorted by score desc."""
# lineups.py
POST /api/lineups        # validate via validate_lineup(strict=False); 422 with errors on invalid;
                         # on success: insert lineup + lineup_players (with bought_cost snapshot), clear is_current on others if name == 'My real team'? NO — is_current set only via set-current endpoint
GET/PUT/DELETE /api/lineups/{id}
POST /api/lineups/{id}/set-current
POST /api/lineups/match-names
```
**Logic**: PUT re-validates the full body (never patch). Deleting a lineup cascades its `lineup_players` and `suggestions` (FK cascade). `GET /api/lineups/{id}` joins players for the squad array + `budget_remaining = 1000 - Σcost` + `validation` (re-run live — a saved lineup can become invalid after a price change; UI shows it).

**Acceptance**: `test_api.py` lineups section: CRUD happy path; each 422 code from §8.9 via crafted bodies; match-names exact/fuzzy/no-match/ambiguous; set-current exclusivity.

---

## T1.10 — Suggestions API

**Goal**: `POST /api/suggestions/generate` orchestrates universe → solver ×3 profiles → transfer math → SuggestionObjects → persist.

**Files**: `backend/app/api/suggestions.py`.

**Key functions**:
```python
def build_universe(target_gw: int) -> list[PlayerRow]:
    """players WHERE can_select=1 AND status NOT IN ('u','s') AND removed=0,
    with ep_next, form, selected_by_percent, now_cost, team, element_type,
    price_change_projections (for UI hints) — M1 universe (M2 adds signal joins)."""

async def generate(lineup_id: int, target_gw: int | None, profiles: list[str]) -> list[dict]:
    """1. load lineup (404 if missing); 2. target_gw default = next GW (or current GW if deadline not past? —
       no: default = next_gw always; if user passes current_gw after deadline → 422 SUGGESTIONS_DEADLINE_PASSED:
       'XI/captain/bench-only optimization lands in M3; M1 requires a future GW');
       3. universe = build_universe; 4. for each profile: solve(); 5. convergence guard across profiles (T1.7.5);
       6. compute_diff + chip_advice_v1 per profile; 7. assemble SuggestionObject (§8.6) with rationale (top-3 reasons:
       highest-EP pick, best differential, strongest applied signal — M1: signal line omitted);
       8. persist all to suggestions; return."""
GET /api/suggestions?lineup_id=&limit=
DELETE /api/suggestions/{id}
```
**Rationale builder** (M1): `f"Top EP: {name} ({ep:.1f} adj, fixture difficulty {d} vs {opp})"`, `f"Differential: {name} owned by only {own:.1f}%"`, captain choice reason. (M2 adds the signal line.)

**Acceptance**: `test_api.py` suggestions section: generate on a seeded DB (real bootstrap snapshot) returns 3 SuggestionObjects, each passing `validate_lineup(strict=True)`; `GET` history works; deadline-past 422.

---

## T1.11 — Frontend scaffold

**Goal**: app shell, routing, typed API client, SSE hook stub.

**Files**: `frontend/src/` per PLAN.MD §6.2:
- `types.ts` — TS interfaces mirroring PLAN.MD §8 (Season, Player, Lineup, SuggestionObject, Signal, LiveState, Settings) — **copy the JSON shapes exactly**.
- `api.ts` — `const api = { getSeason(), getPlayers(params), listLineups(), createLineup(body), updateLineup(id, body), deleteLineup(id), setCurrent(id), matchNames(names), generateSuggestions(body), listSuggestions(lineupId), getSignals(params), getItems(params), getSettings(), putSettings(patch), refresh(source) }` — all `fetch('/api/...')` with typed returns + error surfacing (422 → parse `errors[]`).
- `hooks/useSSE.ts` — M1: stub `useSSE(onEvent)` that exports the interface only (real EventSource wiring in M3 T3.7).
- `App.tsx` — left nav (Dashboard / My Team / Suggestions / News / Settings), route switch, top bar with season + GW + deadline (from `useSeason()` polling `GET /api/meta/season` every 60 s in M1).
- `app.css` — dark theme: bg `#0e1116`, panel `#161b22`, accent `#3fb950` (FPL green), text `#e6edf3`; simple grid layout; no framework.
- Pages: `Dashboard.tsx` (T1.14), `MyTeam.tsx` (T1.12), `Suggestions.tsx` (T1.13), `News.tsx` (M1: "News arrives in M2" placeholder + season card), `Settings.tsx` (M1: read-only display of config.json + LLM status "not configured (M2)" — full editor in M4).

**Acceptance**: shell navigates all 5 routes; top bar shows live season data; API client type-checks (`tsc --noEmit` clean).

---

## T1.12 — My Team page

**Goal**: enter the team by picker **and** paste (AC2), live validation, pitch view, meta panel, saved lineups.

**Files**: `frontend/src/pages/MyTeam.tsx` + `components/`: `PlayerPicker.tsx`, `PasteBox.tsx`, `PitchView.tsx`, `ValidationPanel.tsx`, `MetaPanel.tsx`, `LineupTabs.tsx`.

**Behavior spec**:
- **LineupTabs**: chips for saved lineups + "+ New". Selecting loads via `GET /api/lineups/{id}`. "New" starts an empty draft (local state until saved).
- **PlayerPicker**: search box (debounced 300 ms → `GET /api/players?search=`), position tabs (GK/DEF/MID/FWD/All), row = name, club, cost, form, total pts, ownership, status badge (⚠️ doubt / ❌ out), active-signal badge (M2). Click a row → **add** (if squad has room for that position) or **swap** (replace selected squad member). Budget bar: `£{1000-Σcost}/10.0m` turns red at 0.
- **PasteBox**: textarea (15 lines) → "Match" button → `POST /api/lineups/match-names` → confirmation table: each name, best candidate (+ "2 more…" expand for ambiguous), status icon (✅ exact / 🔍 fuzzy / ❌ no match) → "Load matched" fills the draft (unmatched names stay flagged; user resolves via picker).
- **PitchView**: formation derived from XI positions (grid by row: GK / DEF / MID / FWD — no true tactical coordinates in v1); players show C / VC badges; bench row with ①②③④ priority; click player → select; buttons: "Set captain" / "Set VC" (only when selected player is a starter — else disabled with tooltip), "Bench order: 1..4" (click bench player, pick slot).
- **ValidationPanel**: calls `validate_lineup` result from the lineup object (server re-validates on every GET/PUT; draft state client-side mirrors the 16 rules in `validateClient()` in `rules.ts` — same codes — for instant feedback). Red list with exact messages; green "Valid" when clean.
- **MetaPanel**: transfer bank stepper (1–5), chips remaining (4 steppers 0–2 with window hints from `chip_windows`), budget bar, "Save lineup" (POST/PUT) + "Set as current" + "Generate suggestions" (jumps to Suggestions with this lineup preselected).
- **Squad table** (above pitch): sortable columns name/pos/club/cost/form/pts/ownership/news/signal badges; click row selects.

**Acceptance** (manual checklist items 2–3 of PLAN.MD §11.2): real squad enterable both ways; every rule break shows its specific message; save + reload round-trips; 2 saved lineups switchable.

---

## T1.13 — Suggestions page

**Goal**: generate + display 3 profile cards with diff, chip advice, rationale, apply checklist (AC3).

**Files**: `frontend/src/pages/Suggestions.tsx` + `components/SuggestionCard.tsx`, `DiffTable.tsx`, `ApplyChecklist.tsx`.

**Behavior spec**:
- **Controls**: lineup selector (defaults to `is_current`), target GW (default next, editable ±1 within season), profile toggles (all 3 on by default), **Generate** button (spinner; server timebox ~8 s × 3 profiles → show "optimizing…").
- **SuggestionCard** (one per profile, ordered Max EP / Differential / Safe):
  - Header: profile name + one-line description + (if `variant_of`: "variant of Max EP").
  - **Projected points**: `baseline 78.4 → adjusted 81.2 (captain ×2 incl.)` big number.
  - **PitchView** (reuse) of the suggested XI + bench.
  - **DiffTable**: transfers in (green, +cost), out (red, −sell_value), `cost_delta`, `free_transfers_used / bank`, `penalty_points` (−4 highlighted), `total_cost_after`.
  - **Chip advice** list (chip icon + recommendation badge use/consider + reason).
  - **Rationale**: numbered list (top 3).
  - **ApplyChecklist** (expandable): numbered manual steps for the official site:
    1. "Transfers: sell X, Y (before buying)" 2. "Buy A, B" 3. "Set XI: move Z to bench, start A" 4. "Captain: X — Vice: Y" 5. "Bench order: 1 A, 2 B, 3 C, 4 D" 6. "Chip: (play Wildcard / none)" — generated from the diff; note "do this before the GW6 deadline: {local time}".
- **History**: `GET /api/suggestions?lineup_id=` rendered as a collapsed list at the bottom (created_at, profile, adjusted points, view).

**Acceptance** (manual checklist item 4): generate on a real squad → 3 cards render, all diffs consistent (Σcost = 1000), checklist steps match the diff, regenerate works, history persists.

---

## T1.14 — Dashboard (static M1 version)

**Goal**: at-a-glance state; live elements stubbed for M3.

**Files**: `frontend/src/pages/Dashboard.tsx` + `components/Countdown.tsx`, `TeamStrip.tsx`, `LatestSignals.tsx`.

**Behavior spec**:
- **Header row**: season, current GW, next GW, **deadline countdown** (from `deadline_time`, ticking every second, local time, turns red < 24 h), LIVE badge slot (greyed "not live" in M1).
- **TeamStrip**: current lineup's 15 (compact cards: name, pos, club, cost; M3 adds live points).
- **Today's fixtures card**: current/next GW fixtures from `fixtures` table via `GET /api/meta/season` extension (`fixtures_next_gw: [{home, away, kickoff}]`) — M1 shows next GW's 10 matches with kickoffs (live scores arrive M3).
- **LatestSignals**: "No signals yet — news pipeline lands in M2" placeholder panel (M2 replaces with top-5 by confidence×recency).
- **Quick actions**: "Get suggestions" (→ Suggestions), "Refresh all" (`POST /api/refresh/all`).
- Poll `GET /api/meta/season` every 60 s.

**Acceptance**: countdown matches `deadline_time` exactly (verify against API value, not the outline table); fixtures render; refresh-all triggers poll_log rows (visible via `GET /api/meta/health`).

---

## T1.15 — Launchers + README (M1 section)

**Goal**: one-command start, dev and prod (D14/D15).

**Files**: `start.ps1`, `start.sh`, `README.md` (M1 section).

**`start.ps1` spec** (PowerShell 5.1+ compatible):
```
1. args: --prod (production mode), --port (default 8000)
2. if ! .venv: python -m venv .venv; .venv\Scripts\pip install -r backend\requirements.txt
3. if ! frontend\node_modules: npm --prefix frontend install
4. if --prod and ! frontend\dist: npm --prefix frontend run build
5. run:
   - dev:  Start-Process uvicorn (backend dir, app.main:app --port 8000 --reload) + npm --prefix frontend run dev
   - prod: .venv\Scripts\uvicorn app.main:app --app-dir backend --port 8000   (FastAPI serves frontend/dist)
6. print URLs + "press Ctrl+C to stop"
```
`start.sh` mirrors (bash, `venv/`, `trap` cleanup).

**README (M1 section)**: what it is, prerequisites (Python 3.11+, Node 18+), quick start (dev/prod), the 5 pages, config files, "M2–M4 coming" note, ToS posture paragraph (PLAN.MD §4.2).

**Acceptance** (manual checklist item 1 + prod preview): fresh clone → `start.ps1` → UI at :5173 with data; `start.ps1 --prod` → UI at :8000.

---

## T1.16 — M1 test suite + exit verification

**Goal**: all M1 golden tests green; M1 exit criteria demonstrated.

**Files**: `backend/tests/*` per PLAN.MD §11.1 (M1-relevant: `test_rules.py`, `test_ep_model.py` (M1 portion), `test_transfers.py`, `test_solver.py`, `test_match_names.py`, `test_api.py`, `conftest.py`, fixtures `sample_bootstrap.json` + `sample_element_summary.json` captured from the live API during dev).

**Exit run** (record in README "M1 verified" note):
1. `pytest backend/tests -q` → green.
2. Manual checklist items 1–4 (PLAN.MD §11.2) pass in a real browser.
3. Real-world check: enter the user's actual squad (user paste), generate, verify: 3 lineups, all valid, diffs sensible, no unavailable players suggested, VC not doubled (spot-check the math on one card).
4. Git commit: `M1: core end-to-end (fetch, entry, optimizer, suggestions, dashboard)`.

**M1 is done when**: outline §10 M1 exit criteria + AC1 (partial: dev mode), AC2, AC3, AC4 (M1 portion) demonstrably pass.