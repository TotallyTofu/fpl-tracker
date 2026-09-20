# FPL Team Optimizer

A local web app that helps you improve your Fantasy Premier League team. It pulls
your FPL data (players, fixtures, game settings) from the official public API and
news signals from BBC Sport, Reddit r/FantasyPL, and YouTube (@PlanetFPL; ESPN
optional), then — from the squad you enter manually — solves for 2–3 alternative
lineups (max-EP / differential / safe) with the full transfer math, rule
validation, and a click-by-click apply checklist.

There is **no FPL login by design**: the app never touches your FPL account,
never writes to FPL, and stores nothing but what you see locally in SQLite.
You type your team in (paste a list or pick players), the app does the rest.
Built per `OUTLINE.MD` + `PLAN.MD` (M1–M4). Backend: FastAPI + SQLite.
Frontend: React 18 + Vite + TypeScript.

## Quick start

Prereqs: Python 3.11+, Node 18+ (Node is only needed for dev mode or the first
build — a fresh prod start with an existing `frontend/dist` runs on Python alone).

```powershell
# Windows
.\start.ps1                    # auto: prod if frontend\dist exists, else dev
.\start.ps1 -Prod              # prod: everything on http://127.0.0.1:8000
.\start.ps1 -Dev               # dev: API :8000 + UI http://localhost:5173 (HMR)
.\start.ps1 -Prod -Port 9000   # port override
.\start.ps1 -Prod -Rebuild     # force `npm run build` even if dist exists
```

```bash
# Linux / macOS / WSL
./start.sh                     # auto
./start.sh --prod              # prod
./start.sh --dev               # dev
./start.sh --prod --port 9000  # port override
./start.sh --prod --rebuild    # force rebuild
```

The launcher is idempotent: it detects your Python (`py -3.11` → `py -3` →
`python` → `python3`), creates `.venv`, installs `backend/requirements.txt`
(`pip --no-cache-dir`) only when the requirements file changed, runs
`npm install` in `frontend/` and `npm run build` only if missing (or with
`-Rebuild`/`--rebuild`). Missing Python/Node on Windows gets a `winget`
install hint, and the console stays open if the server crashes so you can read
the error.

**First run takes ~30–60 s**: the app first fetches the full FPL dataset
(bootstrap: elements, fixtures, game settings, bootstrap-transfers) so the UI
has data immediately, then runs a **full news refresh in the background**
(BBC + Reddit + YouTube + signal extraction; ESPN when enabled). A banner in
the top-right shows the refresh in progress and then a completion summary
("✅ News refresh complete — bbc: N rows · reddit: N rows · … · N signals
stored") so you know everything is up to date the moment you open the app.
Ongoing updates continue via the scheduler (FPL 15 min, BBC/Reddit 30 min,
YouTube 60 min, extraction 10 min) and the manual refresh buttons.

> Restricted/sandboxed environments: if `npm install` fails on a postinstall
> spawn (esbuild), the launcher automatically retries with a project-local
> cache (`--ignore-scripts`) — the esbuild binary ships in the
> `@esbuild/win32-x64` package regardless.

## Using the app

1. **My Team** — build your squad:
   - **Paste** a text list of player names (e.g. copied from your FPL lineup or
     a chat) → the matcher resolves each name (exact → known-name → fuzzy
     surname) and reports ✅ matched / 🔍 ambiguous / ❌ no match.
   - Or **pick** players one by one (search + position/team filters).
   - Set your **transfer bank** (1–5) and any **chips** in hand (wildcard,
     free hit, b. boost, triple captain) with window hints.
   - The panel shows live rule validation (squad size, 2/5/5/3, budget,
     ≤3/club, XI shape, bench order, captain/vice-captain).
   - Save the lineup (you can keep several; mark one as **current**; the ⋯ menu
     on each tab renames, duplicates-as-test, sets-current, or deletes it).
2. **Suggestions** — pick a lineup + target gameweek → **Generate**. The solver
   (multi-start hill climbing, seed 42, deterministic) returns up to 3 profiles:
   - **max_ep** — highest projected points next GW.
   - **differential** — max EP with low-ownership picks (λ=3, EP floor 0.4×max).
   - **safe** — EP weighted by reliability; it converges with max_ep when the
     squad is clean and diverges once availability doubt / negative signals
     price players differently.
   Each card shows the pitch, the transfer diff (in/out with sell values,
   cost delta, free transfers used, bank after, penalty points), chip advice,
   rationale notes, and a manual apply checklist (sell first → buy → bench →
   XI → C/VC → bench order → chip). **Apply** marks the suggestion as applied
   and records the chip advice in the chip-play log (used to block
   consecutive free hits). Test lineups (duplicates) can generate suggestions
   but cannot be applied.
3. **Dashboard** — season state (current/next GW, deadline countdown, live
   window), quick refresh of all sources, a live **Signals** panel (top 5 active
   signals, links to News), your current team strip, next-GW fixtures.
4. **News** — the signal pipeline:
   - **Refresh sources** — per-source buttons (fpl / bbc / espn / reddit /
     youtube / All + extract). Each fetch stores items, then the extraction
     pipeline runs: LLM first (when configured), rule-based keywords as the
     backstop. Results show rows fetched, filtered out, and signals stored.
   - **Player signals** — the per-player output the optimizer consumes:
     sentiment (green/red dot), category, confidence, summary, source ref,
     expiry. Active signals directly shift projected points (`S(p)`).
   - **Fetched items** — everything pulled (articles/threads/videos) with
     extraction status and takeaways. BBC items have a **Fetch full article**
     button (on-demand only — RSS title+description is the scheduled path).
5. **Settings** — every knob from `config.json`, organised in six sections:
   - **LLM** — enabled, base URL, model, API key (password input, stored
     locally only), timeout, batch size, live **Test connection**.
   - **Sources** — per-source enabled toggle + refresh interval (FPL, ESPN,
     BBC, Reddit with rss/oauth mode, YouTube with the channel list +
     transcript keywords).
   - **Optimizer** — EP/Form/Fixture weight sliders (sum must be 1.00,
     auto-normalise + reset), availability map, signal coefficients,
     differential λ + EP floor, solver (restarts, timebox, seed, exact-ILP —
     greyed out until PuLP is installed).
   - **Group** — FPL entry ID (optional; the group-rank card is not in v1).
   - **Data** — "Re-fetch all now", "Clear all signals", live DB stats
     (row counts, file size, recent poll errors).
   - **About** — version, sources + ToS posture, disclaimer.
   Save validates client-side (weights sum) and server-side, then writes
   `config.json` on disk. With no LLM configured the app still works —
   rule-based extraction (confidence ≤ 0.6) covers the basics.

## Configuration

Two files, both local, both optional:

**`.env`** (LLM credentials; `.env.example` is committed):

```
LLM_BASE_URL=http://localhost:8888/v1
LLM_API_KEY=
LLM_MODEL=
```

The endpoint `http://localhost:8888/v1` was verified live (key-gated:
`GET /v1/models` returns 401 without a key). An empty key or model → LLM
disabled (rule-based mode). The key never leaves the machine and is never
logged. **Settings → LLM** writes the same values into `config.json`
(`.env` wins if both are set).

**`config.json`** (all app behaviour; every field editable in the Settings
page):

- `sources.*` — per-source `enabled` + refresh intervals; `youtube.channels`
  (handle + resolved channel_id), `youtube.transcript_keywords`,
  `max_transcripts_per_poll`; `reddit.mode` (`rss` default / `oauth`).
- `llm` — `enabled`, `base_url`, `api_key`, `model`, `timeout_sec`,
  `batch_chars` (≤ ~8k input chars per call).
- `optimizer.weights` — `{ep: 0.7, form: 0.15, fixture: 0.15}` (must sum to 1).
- `optimizer.availability` — soft-gate multipliers (doubt ×0.7, chance-50
  ×0.65, chance-0 excluded); `active: true`.
- `optimizer.signal` — signal pricing (neg −0.5 each, floor −0.6; pos +0.1,
  cap +0.2).
- `optimizer.differential_lambda` / `differential_ep_floor` — the
  differential profile's ownership penalty and EP floor (0.4×max).
- `optimizer.solver` — `restarts: 20`, `timebox_sec: 8`, `seed: 42`,
  `exact_ilp: false` (needs `pip install pulp`).
- `group.fpl_entry_id` — optional public FPL entry ID (rank card not in v1).

## Sources & ToS posture

| Source | Endpoint(s) | Auth | Format | Politeness / gotchas |
|---|---|---|---|---|
| **FPL** | `fantasy.premierleague.com/api/bootstrap-static/` · `/api/element-summary/{id}/` · `/api/event/{gw}/` · `/api/game-settings/` | None | JSON | 15-min cadence; element-summary for the user's 15 players only; no `/api/draft/*` (account data — out of scope by design) |
| **BBC** | `feeds.bbci.co.uk/sport/football/rss.xml` (+ on-demand article fetch) | None | RSS 2.0 | 30-min cadence; PL relevance filter (feed includes WSL/EFL/int'l); full article on demand only |
| **Reddit** | `reddit.com/r/FantasyPL/hot.rss` (+ `new.rss`, `top.rss?t=week`); thread bodies `/comments/{id}.rss` | None (OAuth optional) | **Atom** | 30-min cadence; pinned megathread filtered; 429s → circuit breaker stops thread fetches for that poll |
| **YouTube** | Channel RSS `youtube.com/feeds/videos.xml?channel_id={id}`; transcripts via yt-dlp | None | Atom + VTT | 60-min cadence; ≤3 transcript fetches/poll, priority videos only (keyword match) |
| **ESPN** | `site.api.espn.com/apis/site/v2/sports/soccer/eng.1/{scoreboard,news,standings}` | None (browser UA) | JSON | **Disabled by default** — 403s a bare client from some networks; enable in Settings → Sources (fetcher sends a browser User-Agent, verified working) |
| **LLM** | `POST {base_url}/chat/completions` (default `http://localhost:8888/v1`) | User's API key | JSON | 60 s timeout; batches ≤ ~8k input chars; local endpoint verified key-gated |
| **Yahoo** | — | — | — | **Unsupported in v1**: the public JSON API hosts (`sportsengine.api.yahoo.com`, `sports.api.arcadia.yahoo.com`) do not resolve in DNS (verified 2026-09-20). ESPN covers live data; no Yahoo fetcher ships. |

**ToS posture**: light, polite polling (intervals above; ≤20 concurrent
requests); no scraping of paywalled/protected content; Reddit via RSS (OAuth
optional); BBC via official RSS; YouTube via official channel RSS + public
transcripts; **no FPL account access at all**; no writes to FPL; LLM data
boundary = fetched public content + player names only; everything stored
locally in SQLite; no telemetry.

### LLM setup (optional but recommended)

Extraction is LLM-first with a rule-based backstop. Point the app at any
OpenAI-compatible `chat/completions` endpoint:

1. **Settings → LLM** — enter **Base URL** (e.g. `http://localhost:8888/v1`),
   **Model name**, and **API key** → **Save** → **Test connection**.
2. The key is stored locally (`config.json`, or `.env` via `LLM_API_KEY` —
   env wins) and only ever sent to that endpoint. Never logged.
3. Until it's configured, the News page shows "LLM not configured —
   rule-based extraction only (confidence ≤ 0.6)" and suggestions still work.

## Reddit OAuth (optional upgrade)

The Reddit fetcher ships in **RSS mode** (zero credentials):
`https://www.reddit.com/r/FantasyPL/hot/.rss` etc. — and that's all you need
for normal use. If you want the full Reddit API (higher rate limits, search,
comments), upgrade in three steps:

1. Create a free **"script" app** at <https://www.reddit.com/prefs/apps>
   (no registration/approval needed — just a personal app entry).
2. Copy the **client id** and **client secret** into
   **Settings → Sources → Reddit** and set **Mode: oauth** → Save.
3. The fetcher exchanges them for a bearer token and polls
   `oauth.reddit.com` instead of the public RSS endpoints.

> Rate-limit note: even in RSS mode the fetcher pulls thread bodies (top
> comments) for up to 5 headline posts per poll, best-effort. Reddit
> rate-limits aggressively: on a 429 the fetcher stops trying further
> threads for that poll (items are stored with title+text only) and resumes
> on the next cycle. OAuth mode raises the limits substantially.

## How suggestions work

**Projected-points model** (per player, for the target gameweek):

```
ep_final = w.ep · (EP_next · A(p) · (1 + S(p))) + w.form · form_adj + w.fixture · fixture_adj
```

- `w = {ep: 0.7, form: 0.15, fixture: 0.15}` (configurable in Settings; must
  sum to 1.00).
- `EP_next` — the official FPL projection (`ep_next`) for the next GW.
- `A(p)` availability multiplier — hard gates always (unavailable /
  unselectable players are excluded from the universe); the doubt/chance map
  is a soft gate: doubt ×0.7, chance-null ×1.0, 100 ×1.0, 50 ×0.65, 0 →
  excluded.
- `S(p)` news signal adjustment — negative signals −0.5 each
  (confidence-weighted, floor −0.6), positive +0.1 (cap +0.2). Rule-based
  signals cap at 0.6 confidence; LLM signals follow source-reliability bands
  (official 0.9–1.0, reputable 0.6–0.8, rumor 0.3–0.5, vague 0.2).
- `form_adj` / `fixture_adj` — per-position last-5-GW form and next-GW
  fixture difficulty.

**The three profiles** (same validator, different objective):

- **max_ep** — highest total projected points.
- **differential** — max EP with a penalty for high-ownership picks
  (λ=3.0) and an EP floor of 0.4× the max-EP total, so it can't collapse into
  garbage; the "value" pick.
- **safe** — EP weighted by reliability (availability doubt and negative
  signals cost more); diverges from max_ep exactly when the squad or the news
  is not clean.

**Transfer math** (2026/27 system):

- Free transfers = `bank` (1–5). Each extra transfer costs **−4 points**:
  `penalty = 4 × max(0, transfers − bank)`.
- **Sell-on fee**: 0.5× the amount a player has *increased* since purchase
  (75→78 sells at 76; 75→77 sells at 76; a fall 75→70 sells at 70).
- 20-transfer cap per season; **wildcard / free hit neutralize penalties**
  for the GW they cover.
- Cost must stay ≤ £100.0m (budget 1000 in £0.1m units).

**Chip advice** — per suggestion, each of the four chips gets a
use / consider / skip recommendation with a reason, all gated on the active
chip window (read from the API's `chips[]`) and the sets you still have:

- **Wildcard** — "use" when the diff exceeds your bank (it makes them all
  free and retains the bank); "consider" when the bank is ≤1 and there are
  ≥4 transfers.
- **Free hit** — "consider" when the diff exceeds your bank (avoids the
  −4-per-excess penalty); never advised if a Free Hit was played the previous
  GW (consecutive ban).
- **Triple captain** — "use" when the captain's EP ≥ 8.5, or ≥ 7.0 with a
  high-confidence confirmed-starter signal; "consider" at EP ≥ 7.0.
- **Bench boost** — "consider" when the bench's total EP ≥ 0.5× the XI
  average (the 1-pt appearances become worthwhile).

Applying a suggestion logs any "use" recommendation in the chip-play log at
the target GW — which is what powers the free-hit consecutive-ban check above.

**Rules the solver enforces** (2026/27, verified against the live
`game_settings` API):

- Budget **£100.0m** (unit = £0.1m), squad **15** = 2 GK / 5 DEF / 5 MID /
  3 FWD, **≤3 players per club**.
- XI = 11 starters: **1 GK, ≥3 DEF, ≥1 FWD**; bench = 4 (orders 1–4).
- Exactly **1 captain + 1 vice-captain**, both starters, distinct.
- **Vice-captain has NO multiplier in 2026/27** (only steps in if the captain
  plays 0 minutes; then ×2 moves to the VC).
- Hard-unavailable players (`can_select=0`, status `u`/`s`) can never be
  selected; doubt / chance-of-playing are soft gates (via A(p)).

The solver is deterministic (seed 42, multi-start hill climbing, 20 restarts,
8 s timebox) — the same squad + data always produces the same suggestions.

> **Disclaimer**: suggestions are **projections, not guarantees** — the model
> weights are configurable defaults, not a claim of optimality. Verify news on
> the official site before the deadline.

## Adding sources / channels

**YouTube channel** (no code): Settings → Sources → YouTube → "+ Add channel"
→ paste a handle (`@SomeChannel`) or a channel ID. The fetcher resolves
handle → channel_id server-side on the first poll; remove rows the same way.
Transcript keywords (`transcript_keywords`) control which videos get a
transcript fetched (≤3 per poll).

**New news source** (fetcher plugin, ~10 lines of integration):

1. Create `backend/app/fetchers/<name>.py` with `async def fetch() -> None`
   that pulls the feed and calls, per item:
   `ingest.ingest_item(source="<name>", external_id=..., kind="article"|"thread"|"video",
   title=..., url=..., published_at=..., body=...)` — duplicates are dropped
   automatically (content hash on (source, title, body)).
2. Add a `sources.<name>` block (enabled + interval) to `config.json`
   defaults in `backend/app/config.py`.
3. Register a scheduler job in `backend/app/scheduler.py` (copy the
   `_job_bbc` pattern) and add `<name>` to the startup full-refresh list in
   `backend/app/startup.py`.
4. Frontend: add the source to the refresh buttons in `News.tsx` and a row in
   `Settings.tsx` → Sources.
5. Tests: a fixture file under `backend/tests/fixtures/` + a fetcher test
   (see `test_fetchers.py`). Extraction (rule + LLM) picks new items up
   automatically on the next extract cycle — no extractor changes needed.

## Troubleshooting

- **LLM 401 on Test connection** — the key is wrong/empty. Fix it in
  Settings → LLM (or `.env` `LLM_API_KEY`); the endpoint is key-gated.
- **yt-dlp "bot check" / no transcript** — `pip install -U yt-dlp` (the
  launcher keeps it updated only when `requirements.txt` changes), and make
  sure Node is installed (auto-caption extraction wants a JS runtime; the
  launcher's `--js-runtimes node` uses it).
- **Schema-drift banner** — the FPL API changed a field the parser expects.
  The app keeps working on last-good data; re-fetch (Settings → Data →
  "Re-fetch all now") and check Settings → Data → recent poll errors.
- **"No data" / empty dashboard** — run `POST /api/refresh/all` (or Settings
  → Data → "Re-fetch all now") and check your network; the bootstrap endpoint
  is `fantasy.premierleague.com`.
- **Port 8000 already in use** — `start.ps1 -Prod -Port 9000` (or
  `./start.sh --prod --port 9000`).
- **Reddit 429s in the poll log** — expected under RSS; the circuit breaker
  stops thread-body fetches for that poll and resumes next cycle. OAuth mode
  (Settings → Sources → Reddit) raises the limits.
- **npm install / esbuild failures in restricted environments** — the launcher
  retries with a project-local cache + `--ignore-scripts` automatically; the
  esbuild binary ships in `@esbuild/win32-x64` regardless.

## Manual test checklist

Executed in a real browser at each milestone exit (final version; M3 item
adjusted to the revised scope):

1. **Dev mode**: `start.ps1 -Dev` → both ports up → UI loads at :5173, API
   proxied.
2. **Paste-a-list**: paste 15 names (include 1 typo) → confirmation table →
   save → validation panel green.
3. **Invalid input**: break each rule once → specific error message appears.
4. **Generate suggestions** → 3 cards, pitch views, diffs, apply checklist.
5. **Refresh each source** → items appear; signals table filters work;
   LLM-off mode produces official-news signals.
6. **Startup refresh (M3 revised)**: restart the app → top-right banner shows
   the news refresh running → completion summary with per-source row counts;
   a fresh page load within 15 min still shows the completion banner.
7. **Production mode (M4)**: `start.ps1 -Prod` → single port 8000 serves UI +
   API; SPA deep-links survive a hard reload (`/suggestions`, `/team`);
   unknown `/api/*` returns JSON 404 (not the SPA).
8. **Test lineups + settings (M4)**: duplicate a lineup as a test → generate
   on it → apply is disabled; Settings round-trip (edit a weight → sum check,
   edit BBC interval → save) persists to `config.json` on disk.

## Milestones & status

| Phase | Scope | Status |
|---|---|---|
| **M1 — Core** (2026-09-19, `571d879`) | FPL fetchers + schema + manual team entry (picker + paste) + rules validator + optimizer on `ep_next` + Suggestions page + static Dashboard | ✅ done |
| **M2 — News layer** (2026-09-20, `f82e1c3`) | BBC/ESPN/Reddit/YouTube fetchers + raw_items + LLM extractor + rule fallback + signals page + optimizer integration (A(p) + S(p)) | ✅ done |
| **M3 — Real-time → revised** (2026-09-20, `098033e`) | Original scope (live pollers + SSE + live dashboard) **dropped per project decision**; replaced by a **full news refresh on every start** (background task after the synchronous FPL bootstrap) + completion banner. The season state still reports `live_mode` / `live_window` from the fixtures. | ✅ done (revised scope) |
| **M4 — Polish** (2026-09-20, `d5d8b0d` → `5074aef` + T4.8–T4.10) | Solver convergence, chip advice v2, test lineups, full Settings page, weight sliders, packaging (launchers + fresh-clone smoke), final README, v1.0.0 tag | ✅ done |

Note on "simulated vs real": all acceptance testing used **live** FPL/news
data (the only simulated parts are the pytest fixtures for the solver and
extractors, as required by the test strategy).

## New season (August rollover checklist)

The app is season-agnostic by design (all rules data comes from the live API),
but a few human re-checks are worth doing each August before GW1. The 2026/27
dry pass was executed on 2026-09-20 (see `ACCEPTANCE.md`); every item passed.

1. **Rules page** (<https://fantasy.premierleague.com/en/help/rules>):
   re-read transfers (bank cap, −4 per extra, 20-cap, sell-on fee),
   captaincy (does the VC have a multiplier?), chips (windows, split point,
   consecutive-free-hit ban), and the scoring table. If anything changed,
   update `backend/app/optimizer/rules.py` constants and `PLAN.MD` §5.
   (2026/27 changed transfers + VC multiplier — this is why the checklist
   exists.)
2. **Chip windows**: the app reads `chips[]` from the API — verify the rows
   look right (typically two sets per chip) via
   `GET /api/meta/season` → `chip_windows`.
3. **Scoring table**: cross-check `PLAN.MD` §5.9 against the rules page
   (appearance, goals, clean sheets, defensive contributions, cards).
4. **BPS table**: cross-check `PLAN.MD` §5.10 (drives bonus estimates).
5. **Club list**: 20 clubs — the name map (FPL `teams[]` ↔ ESPN
   abbreviations) auto-rebuilds at first run; watch
   Settings → Data → recent poll errors for "team map mismatch" in GW1.
6. **Player universe**: first bootstrap of the season → sanity: all 4
   positions present, prices sensible, `players.season` bumped, old-season
   signals wiped (season-rollover logic, `PLAN.MD` §7).
7. **LLM model**: if your local model changed → update `.env`
   (`LLM_MODEL`) or Settings → LLM.
8. **yt-dlp**: `pip install -U yt-dlp` (YouTube extraction is a moving
   target).
9. **First-deadline check**: the GW1 deadline comes from the API (never the
   rules-page table, which can lag scheduling) — confirm the Dashboard
   countdown shows the right time.

## Architecture

```
backend/
  app/
    config.py         config.json + .env loader
    db.py             SQLite (WAL), per-call connections, schema
    httpclient.py     shared httpx.AsyncClient
    scheduler.py      APScheduler (fpl 15m · bbc/espn/reddit 30m · youtube 60m · extract 10m)
    season.py         season state, chip windows, next-GW fixtures
    startup.py        boot-time full news refresh (background task + state)
    fetchers/         fpl.py · bbc.py · espn.py · reddit.py · youtube.py
    signals/          ingest.py (dedupe store) · names.py (matcher) ·
                      rule_extractor.py (keyword backstop) · llm_extractor.py ·
                      store.py (TTL) · pipeline.py (LLM→rules orchestration)
    optimizer/        rules.py · scoring.py · solver.py · transfers.py
    api/              meta · players · lineups · suggestions · settings · news
  scripts/dev_check.py
  tests/              139 tests (rules, EP, transfers, solver, names, API,
                      signals rule/llm/ingest, fetchers, news API, startup,
                      chip advice, chip-play endpoints)
frontend/
  src/
    pages/            Dashboard · MyTeam · Suggestions · News · Settings
    components/       PitchView · PlayerPicker · PasteBox · ValidationPanel ·
                      MetaPanel · LineupTabs · DiffTable · ApplyChecklist ·
                      SuggestionCard · Countdown · TeamStrip · StartupToast ·
                      WeightSlider
    api.ts · types.ts · rules.ts (client mirror) · hooks/
data/                 fpl.db (gitignored)
```

## Development

```powershell
cd backend
..\.venv\Scripts\python -m pytest tests -q     # 139 tests, no network needed
..\.venv\Scripts\python scripts\dev_check.py   # live end-to-end smoke check
```

Frontend: `cd frontend && npx tsc --noEmit && npx vite build`.

Design decisions (full rationale in `PLAN.MD` §9): D14 single-port prod
(FastAPI serves `frontend/dist`), D16 no frontend test framework (type-check +
build gate instead), deterministic solver (seed 42) for reproducible
suggestions, SQLite for zero-ops local storage.

## Notes & caveats

- LLM transcripts are **auto-captions only** (yt-dlp `--write-auto-sub`),
  budgeted to 3 videos per poll, and only used by the LLM path — the rule
  path reads YouTube title+description only (caption noise would confuse it).
- When a signal hits a player in the suggested XI, the rationale notes it
  ("Signal applied: …").
- The app stores no credentials except what you type into Settings (LLM key,
  optional Reddit OAuth) — everything stays in `config.json` / `.env` locally.