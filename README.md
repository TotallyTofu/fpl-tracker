# FPL Team Optimizer

A local web app that helps you improve your Fantasy Premier League team. It pulls
live FPL data (players, fixtures, game settings), news signals (BBC, Reddit,
YouTube), and your current squad — then solves for 2–3 alternative lineups
(max-EP / differential / safe) with the full transfer math, rule validation,
and a click-by-click apply checklist.

Built per `OUTLINE.MD` + `PLAN.MD` (M1–M4). Backend: FastAPI + SQLite.
Frontend: React 18 + Vite + TypeScript.

## Quick start

Prereqs: Python 3.11+, Node 18+.

```powershell
# Windows
.\start.ps1            # dev: API :8000 + UI http://localhost:5173
.\start.ps1 -Prod      # prod: everything on http://127.0.0.1:8000
```

```bash
# Linux / macOS / WSL
./start.sh             # dev
./start.sh --prod      # prod
```

The launcher is idempotent: it creates `.venv`, installs `backend/requirements.txt`
(`pip --no-cache-dir`), runs `npm install` in `frontend/`, and runs `vite build`
— each only if missing. On first start the app fetches the full FPL dataset
(bootstrap: elements, fixtures, game settings, bootstrap-transfers) so the UI
has data immediately.

> Restricted/sandboxed environments: if `npm install` fails on a postinstall
> spawn (esbuild), use `npm install --no-audit --no-fund --ignore-scripts`
> — the esbuild binary ships in the `@esbuild/win32-x64` package regardless.

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
   - Save the lineup (you can keep several; mark one as **current**).
2. **Suggestions** — pick a lineup + target gameweek → **Generate**. The solver
   (multi-start hill climbing, seed 42, deterministic) returns up to 3 profiles:
   - **max_ep** — highest projected points next GW.
   - **differential** — max EP with low-ownership picks (λ=3, EP floor 0.4×max).
   - **safe** — EP weighted by reliability; in M1 it converges with max_ep and
     is shown as a *variant* — it diverges once news signals / availability
     activate (M2+).
   Each card shows the pitch, the transfer diff (in/out with sell values,
   cost delta, free transfers used, bank after, penalty points), chip advice,
   rationale notes, and a manual apply checklist (sell first → buy → bench →
   XI → C/VC → bench order → chip).
3. **Dashboard** — season state (current/next GW, deadline countdown, live
   window), quick refresh of all sources, your current team strip, next-GW
   fixtures.
4. **News** — M2: fetched articles/transcripts, extracted player signals
   (sentiment + confidence), and which signals are currently applied.
5. **Settings** — LLM endpoint config (base URL, **model name**, **API key**)
   with a **Test connection** button; read-only view of `config.json` (M4 adds
   the editor).

## Data sources

| Source | What | How | Status |
|---|---|---|---|
| fantasy.premierleague.com | players, fixtures, game settings, bootstrap | official public API | ✅ M1 |
| BBC Sport (football) | news articles | RSS | ✅ M2 |
| Reddit r/FantasyPL | community threads | RSS (public feed) | ✅ M2 |
| YouTube @PlanetFPL | "The Weekender" etc. | yt-dlp transcript download + LLM extraction | ✅ M2 |
| ESPN | news | disabled (403 from this network) | ❌ intentionally off |

### Reddit OAuth upgrade path (M2 uses RSS only)

The Reddit fetcher ships in **RSS mode** (no credentials needed):
`https://www.reddit.com/r/FantasyPL/hot/.rss` etc. If you want full Reddit API
access (higher rate limits, search, comments):

1. Create an app at <https://www.reddit.com/prefs/apps> (script type).
2. Put the client id/secret in `config.json` → `sources.reddit.oauth_client_id`
   / `oauth_client_secret` (or use `reddit.mode: "oauth"`).
3. The fetcher exchanges them for a bearer token and uses `oauth.reddit.com`.

RSS mode is deliberately the default so the app works with zero credentials.

## FPL rules encoded (2026/27)

Verified against the live `game_settings` API:

- Budget **£100.0m** (unit = £0.1m), squad **15** = 2 GK / 5 DEF / 5 MID / 3 FWD.
- **≤3 players per club**.
- XI = 11 starters: **1 GK, ≥3 DEF, ≥1 FWD**; bench = 4 (orders 1–4).
- Exactly **1 captain + 1 vice-captain**, both starters, distinct.
- **Vice-captain has NO multiplier in 2026/27** (only steps in if the captain
  scores 0). Captain doubles.
- Transfer bank 1–5, cap 20 total, **sell-on fee = 0.5×** the amount a player
  increased since purchase (only the increase is half-refunded).
- Hard-unavailable players (`can_select=0`, status `u`/`s`) can never be
  selected; doubt / chance-of-playing are *soft* gates (M2, via A(p)).

## Projected-points model

```
ep_final = w.ep · (EP_next · A(p) · (1 + S(p))) + w.form · form_adj + w.fixture · fixture_adj
```

- `w = {ep: 0.7, form: 0.15, fixture: 0.15}` (configurable).
- `A(p)` availability multiplier — hard gates in M1 (unavailable excluded);
  the doubt/chance map activates in M2 (`optimizer.availability.active`).
- `S(p)` news signal adjustment — 0 in M1; M2: negative signals −0.5 each
  (conf-weighted, floor −0.6), positive +0.1 (cap +0.2).
- `form_adj` / `fixture_adj` — per-position form and next-GW fixture difficulty.

## Architecture

```
backend/
  app/
    config.py         config.json + .env loader
    db.py             SQLite (WAL), per-call connections, schema
    httpclient.py     shared httpx.AsyncClient
    scheduler.py      APScheduler (M1 stub → M3 job table)
    season.py         season state, chip windows, next-GW fixtures
    fetchers/         fpl.py (M1) · bbc.py reddit.py youtube.py (M2)
    signals/          names.py (name matcher) · extract.py (M2 LLM)
    optimizer/        rules.py · scoring.py · solver.py · transfers.py
    api/              meta · players · lineups · suggestions · settings
  scripts/dev_check.py
  tests/              56 tests (rules, EP model, transfers, solver, names, API)
frontend/
  src/
    pages/            Dashboard · MyTeam · Suggestions · News · Settings
    components/       PitchView · PlayerPicker · PasteBox · ValidationPanel ·
                      MetaPanel · LineupTabs · DiffTable · ApplyChecklist ·
                      SuggestionCard · Countdown · TeamStrip
    api.ts · types.ts · rules.ts (client mirror) · hooks/
data/                 fpl.db (gitignored)
```

## Development

```powershell
cd backend
..\.venv\Scripts\python -m pytest tests -q     # 56 tests, no network needed
..\.venv\Scripts\python scripts\dev_check.py   # live end-to-end smoke check
```

Frontend: `cd frontend && npx tsc --noEmit && npx vite build`.

Design decisions (full rationale in `PLAN.MD` §9): D14 single-port prod
(FastAPI serves `frontend/dist`), D16 no frontend test framework (type-check +
build gate instead), deterministic solver (seed 42) for reproducible
suggestions, SQLite for zero-ops local storage.

## Notes & caveats

- M1 has **no news, no LLM, no live polling** — the hooks exist but are inert
  (`availability.active=false`, no signals). That's why `safe` ≡ `max_ep`
  in M1 (shown as a variant, not "fixed").
- Suggestions are **projections, not guarantees** — the model weights are
  configurable defaults, not a claim of optimality.
- The app stores no credentials except what you type into Settings (LLM key,
  optional Reddit OAuth) — everything stays in `config.json` / `.env` locally.