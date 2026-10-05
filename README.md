# FPL Tracker

A local web app that helps you plan your Fantasy Premier League team. It reads the
official FPL data plus football news (BBC Sport, ESPN, Reddit r/FantasyPL and
YouTube), turns the news into per-player signals with an optional local LLM, and
suggests three transfer plans for your team that follow the FPL rules.

- **No FPL login.** You type your team in. The app never touches your FPL account.
- **Runs on your computer.** Everything is stored locally in SQLite. The server
  only answers on `127.0.0.1`.
- **Works without an LLM.** A keyword matcher is the fallback. With a local
  OpenAI-compatible model the news signals are much better.

Version **1.0.0**, built for the 2026/27 season rules. See [CHANGELOG.md](CHANGELOG.md).

## What you get

| Page | What it does |
|---|---|
| **This gameweek** | The deadline countdown and a four-step checklist: injury flags in your squad, the best transfer plan, captain, chips. News on your players, the fixtures (with how many of your players are in each), and the health of every news source. |
| **Transfer plans** | Three plans: **Best projected**, **Differential** (low-ownership picks) and **Safe** (weights each pick by how likely he is to play). Each card's headline is projected points *after* any transfer hit. Pick at most one chip to play. Open a plan to see the pitch, the transfers, why, and step-by-step instructions for the FPL site. |
| **My team** | Your squad on a pitch (drag and drop, captaincy, bench order with the FPL goalkeeper-sub slot), free transfers, money in the bank, chips, purchase prices, next three fixtures and a rule check. Paste a list of names or search for players. Keep several teams, and sandbox copies to experiment with. |
| **News & signals** | Every source with its status, every live signal with where it came from (official FPL, read by the LLM, or a keyword match) and how much it moves the projection. Dismiss wrong matches. |
| **Settings** | LLM, sources, optimizer weights, group/entry ID, data tools. |

Light and dark themes (button in the header), and the layout works on a phone.

## Quick start

Needs Python 3.11+ and Node 18+ (Node only for the first build or dev mode).

```powershell
# Windows
.\start.cmd                    # same as start.ps1, bypasses script-signing policies
.\start.ps1 -Prod              # everything on http://127.0.0.1:8000
.\start.ps1 -Dev               # API :8000 + UI http://localhost:5173 with hot reload
.\start.ps1 -Prod -Rebuild     # rebuild the frontend first
```

```bash
# Linux / macOS / WSL
./start.sh --prod
./start.sh --dev
```

The launcher creates `.venv`, installs `backend/requirements.txt`, installs and
builds the frontend if needed, and starts the server. The first start fetches the
FPL data (about 30 seconds), then refreshes the news in the background.

Then:

1. **My team**: paste your 15 players (one per line) or search for them. Set
   your free transfers, **money in the bank** (the "Bank" figure on the FPL
   Transfers page) and which chips you have used. Save.
2. **Transfer plans**: press **Make plans**. Pick a chip only if you mean to play
   it this week.
3. Make the changes on the FPL site, then press **I've made these changes in
   FPL** so the app updates your saved team.

## Configuration

Two local files, both gitignored. Neither is needed to start.

**`config.json`** holds every setting (the Settings page edits it). It is
created with defaults on first run. [`config.example.json`](config.example.json)
shows the defaults.

**`.env`** can hold the LLM values instead (it wins over `config.json`). See
[`.env.example`](.env.example):

```
LLM_BASE_URL=http://localhost:8888/v1
LLM_API_KEY=
LLM_MODEL=
```

### Local LLM (recommended)

Any OpenAI-compatible `chat/completions` endpoint works (llama.cpp server,
Unsloth Studio, LM Studio, vLLM, Ollama's OpenAI API…).

1. Settings → LLM: base URL, model name, API key → Save → **Test connection**.
2. Leave **turn off the model's "thinking"** on. Thinking models (Gemma 4,
   Qwen 3) otherwise reason for minutes on one article and run out of output
   budget before answering. The app sends
   `chat_template_kwargs: {"enable_thinking": false}` and retries without it if
   your server rejects the field.
3. `max_tokens` 2048 is plenty with thinking off.

When the LLM fails repeatedly, new items wait for it (up to
`llm.llm_wait_hours`, default 6) before falling back to keyword matching. Items
skipped by older versions can be re-read from News → Local LLM → **Re-read
skipped**.

### Reddit OAuth (optional)

RSS works without credentials but Reddit rate-limits it. For OAuth, create a
free "script" app at <https://www.reddit.com/prefs/apps>, then in Settings →
Sources → Reddit set mode `oauth` and paste the client id and secret. The app
uses the client-credentials flow (no user login).

## Security and privacy

- The launchers bind the server to **127.0.0.1**, so other devices on your
  network cannot reach it. Do not change this to `0.0.0.0`.
- The server refuses requests whose `Host` is not a loopback name (blocks
  DNS-rebinding attacks) and state-changing requests from other websites' pages
  (blocks cross-site request forgery). To serve it under another host name on
  purpose, set `FPL_ALLOWED_HOSTS=name1,name2`.
- Your LLM API key and Reddit secret are stored only in `config.json` / `.env`.
  The settings API never returns them (blank field = keep the saved value), and
  **Test connection** only sends the saved key to the saved URL.
- Sent to outside services: requests to the FPL API, BBC, ESPN, Reddit and
  YouTube (public data only), and news text plus player names to *your* LLM
  endpoint. No telemetry. Fonts are bundled, not loaded from Google.
- `config.json`, `.env` and `data/` (the database) are gitignored. Check
  `git status` before you commit if you add other local files.

## How suggestions work

### Projected points

For each player in the target gameweek:

```
projected = blend × fixture × (1 + news)

blend   = (0.70 · FPL ep_next + 0.15 · season points-per-game × chance of playing) / 0.85
fixture = 1 + 0.15 · k · (3 − difficulty)      k = 0.8 for GK/DEF, 0.5 for MID/FWD
news    = −0.5 × confidence per negative signal (floor −0.6), +0.1 per positive (cap +0.2)
```

- FPL's `ep_next` is already "recent form × chance of playing", so injury
  doubts are **not** discounted again, and official FPL news does not move the
  number (it is already in `ep_next`).
- A club with no fixture in the gameweek scores 0; a double gameweek counts both
  fixtures.
- Gameweeks after the next one use the season average only (FPL only publishes
  expected points for the next gameweek).
- The weights are in Settings → Optimizer.

### The three plans

| Plan | Maximises |
|---|---|
| Best projected | projected points |
| Differential | projected points + a bonus for players few managers own (λ = 3) |
| Safe | projected points × reliability (doubts and negative news cost extra) |

The solver starts from your squad, tries every legal single swap and takes the
best one repeatedly (steepest ascent), then refines with a seeded random search.
It is deterministic: the same data gives the same plans. If two plans end up
identical, the second is labelled "same team as…".

### FPL rules enforced (2026/27)

- 15 players: 2 GK, 5 DEF, 5 MID, 3 FWD; at most 3 from one club.
- Starting XI: 1 GK, at least 3 DEF and 1 FWD. The bench has the goalkeeper sub
  in his own slot and three outfield subs in priority order.
- Captain ×2. The vice-captain has no multiplier; he only takes over if the
  captain does not play.
- Transfers: a swap is one transfer. Each transfer beyond your free ones costs
  4 points. Plans never take a hit voluntarily; one only appears when an
  unavailable player must be replaced. Free transfers: 0–5, +1 per deadline,
  max 5. At most 20 transfers in a gameweek without a Wildcard or Free Hit.
- Money: you sell at the purchase price plus half of any rise (rounded down).
  A plan only spends your money in the bank plus what you sell. Without a bank
  figure it is estimated as £100.0m minus your squad's current price. A squad
  worth more than £100m after price rises is allowed.
- Chips: two sets, one per half of the season (windows read from the FPL API).
  **One chip per gameweek.** A chip is only assumed when you pick it on the
  Transfer plans page. A Wildcard or Free Hit makes that week's transfers free
  and keeps your saved free transfers. A Free Hit cannot follow a Free Hit.
  Bench Boost counts the bench, Triple Captain triples the captain.

### Chip advice

Each plan rates every chip "use", "consider" or "skip", and at most one chip is
"use":

- **Triple Captain**: use when the captain projects 8.5+ (7.0+ with a
  confirmed-starter signal), consider at 7.0+.
- **Bench Boost**: use when the bench projects 14+, consider at 10+.
- **Wildcard / Free Hit**: the app runs one extra solve with the chip and
  reports how many more points a rebuild projects this week (consider at +10 /
  +15).

Suggestions are projections, not guarantees. Check team news on the official
site before the deadline.

## News sources

| Source | How | Notes |
|---|---|---|
| FPL official | `fantasy.premierleague.com/api/bootstrap-static/` | Prices, fixtures, injury status and news. Every 15 min. |
| BBC Sport | Football and Premier League RSS + article pages | Full article text for new Premier League items (capped per poll). |
| ESPN | `site.api.espn.com/.../eng.1/news` + `content.core.api.espn.com` stories | Full story text (capped per poll). Sends a browser User-Agent; if your network is blocked the poll log shows the error and the rest carries on. |
| Reddit r/FantasyPL | RSS, or OAuth JSON | Post plus top comments for headline posts; the keyword fallback reads the post only. |
| YouTube (Planet FPL) | Channel RSS + transcripts via yt-dlp | Up to 3 transcripts per poll for priority videos (Weekender, deadline streams…); add channels in Settings. |

Polling is light (15–60 min) and only reads public pages. Yahoo Sports was
dropped: its API hosts no longer resolve.

## Troubleshooting

- **`start.ps1` is "not digitally signed"**: run `start.cmd` instead, or
  `powershell -ExecutionPolicy Bypass -File .\start.ps1`.
- **LLM "Test connection" fails with 401**: wrong or empty API key.
- **LLM calls time out or return empty content**: make sure "turn off the
  model's thinking" is on (Settings → LLM).
- **No YouTube transcripts**: `pip install -U yt-dlp` and make sure Node is
  installed.
- **Empty dashboard**: Settings → Data → "Re-fetch all now", and check your
  network can reach `fantasy.premierleague.com`.
- **Port 8000 in use**: `.\start.ps1 -Prod -Port 9000` / `./start.sh --prod --port 9000`.
- **Changed a source interval**: restart the app (intervals are read at start;
  optimizer weights apply immediately).

## New season checklist (each August)

The app reads gameweeks, deadlines, chips and fixtures from the API, but a few
things need a human check before GW1:

1. Re-read the rules page (<https://fantasy.premierleague.com/en/help/rules>):
   squad rules, transfers, captaincy, chips, scoring. Update
   `backend/app/optimizer/rules.py` and `PLAN.MD` §5 if anything changed.
2. Check `GET /api/meta/season` → `chip_windows` looks right.
3. First start of the season wipes old players, signals and saved teams
   (season rollover) — re-enter your team.
4. `pip install -U yt-dlp`.

## Development

```powershell
cd backend
..\.venv\Scripts\python -m pytest tests -q                  # 316 tests, no network
..\.venv\Scripts\python scripts\check_name_resolution.py    # every player resolves from his own name
cd ..\frontend
npx tsc --noEmit
npx vite build
```

```
backend/app/
  main.py            FastAPI app, local-only guard, static frontend
  config.py          config.json + .env
  db.py              SQLite schema and migrations
  season.py          gameweeks, deadlines, chip windows
  fetchers/          fpl, bbc, espn, reddit, youtube
  signals/           ingest (dedupe), names (player matching), llm_extractor,
                     rule_extractor (keyword fallback), store (expiry), pipeline
  optimizer/         rules (validator), scoring (projection), solver, transfers (diff + chips)
  api/               meta, entry, players, lineups, suggestions, news
frontend/src/
  pages/             Dashboard (This gameweek), MyTeam, Suggestions (Transfer plans), News, Settings
  components/        PitchView, PlanCard, PlanDetail, LineupTabs, PlayerPicker, PasteBox, …
```

Design and history: [`PLAN.MD`](PLAN.MD) (rules engine, schema, API spec),
[`FIX.MD`](FIX.MD) (the rev 2–3 audit), [`ACCEPTANCE.md`](ACCEPTANCE.md) and
[`archive/`](archive/) (original plans).
