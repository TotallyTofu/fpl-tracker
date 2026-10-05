# FPL Team Optimizer — Master Implementation Plan (Part 1 of 5)

> **Audience**: the builder agent implementing the FPL Team Optimizer.
> **Companion documents**: `OUTLINE.MD` (decisions & rules source of truth) and this plan set:
> | Part | File | Contents |
> |---|---|---|
> | 1 | `PLAN.MD` (this file) | Mission, decisions, verified facts, FPL rules, architecture, DB schema, API spec, SSE protocol, config, test strategy, milestones |
> | 2 | `PLAN-2-M1-CORE.MD` | M1 task-by-task build spec (T1.1–T1.16) |
> | 3 | `PLAN-3-M2-NEWS.MD` | M2 task-by-task build spec (T2.1–T2.13) |
> | 4 | `PLAN-4-M3-REALTIME.MD` | M3 task-by-task build spec (T3.1–T3.9) |
> | 5 | `PLAN-5-M4-POLISH.MD` | M4 task-by-task build spec (T4.1–T4.10) + final acceptance + season rollover |
>
> **Reading order**: `OUTLINE.MD` → this file → M1 → M2 → M3 → M4. Build in milestone order; do not start M(n+1) until M(n)'s exit criteria pass.
>
> **Conflict rule**: where this plan's verified facts (V1–V11, §4) contradict `OUTLINE.MD`, the verified fact wins (each contradiction is flagged).

---

## 1. Mission & success criteria

Build a **local web app** (FastAPI + React + SQLite, user's PC only) that:

1. Automatically pulls FPL data + football news from: official FPL API, ESPN, BBC Sport, Reddit r/FantasyPL, YouTube (Planet FPL, extensible).
2. Accepts the user's current team **manually** (picker + paste-a-list; no FPL login, ever).
3. Generates **2–3 rule-valid suggested lineups** (squad + XI + captain/VC + bench order) projected to maximize FPL points, each with a different risk profile, diff vs current team, transfer/penalty math, and chip advice.
4. Updates in **real time**: live scores, live player points, bonus race, auto-bench simulation, deadline countdown, auto-refreshing news/signal feed.

**Success criteria** (from `OUTLINE.MD` §12 — the final acceptance run in Part 5 executes these one by one):

- [ ] **AC1 — Runs locally**: with `python` + `node` installed, one launcher script starts everything; UI at `http://localhost:8000` (production mode) or `:5173` (dev); no cloud, no external keys required.
- [ ] **AC2 — Team entry**: valid 15-player squad (2/5/5/3, ≤£100.0m, ≤3/club) enterable via picker **and** pasted names; invalid input blocked with specific messages; multiple named lineups savable/switchable.
- [ ] **AC3 — Suggestions**: "Generate" returns 2–3 lineups, each passing the full rule validator; each shows projected points (baseline + news-adjusted), diff (transfers, cost delta, free transfers used, −4 penalty), captain/VC, bench order, chip advice, rationale.
- [ ] **AC4 — Rule fidelity**: no `status=u`/`s` or `can_select=false` player ever suggested; VC has **no** multiplier (2026/27); transfer math matches §3.5/§3.6 of the rules below; chip advice respects chip windows.
- [ ] **AC5 — News pipeline**: with LLM configured, new items produce per-player signals (JSON-validated, deduped, expiring); with LLM off, rule-based fallback still produces signals (official FPL news/status + keywords).
- [ ] **AC6 — Real-time**: during a live match, Dashboard updates scores + user's live points within 60 s via SSE; deadline countdown accurate; auto-bench simulation visible.
- [ ] **AC7 — Group context (optional)**: with an entry ID set, shows overall rank/percentile + per-league ranks from public `/api/entry/{id}/`.
- [ ] **AC8 — Docs**: README covers setup, config, sources + ToS posture, how to add sources/channels.

**Non-goals (v1)**: no writes to FPL (no official write API exists; user copies suggestions manually — the app provides an apply checklist); no multi-user/accounts/cloud; Classic FPL only (no Draft/Challenge); no betting/odds.

---

## 2. Fixed decisions

### 2.1 From `OUTLINE.MD` §2 (final, do not revisit)

| # | Decision | Value |
|---|----------|-------|
| D1 | App form | Local web app (browser UI on user's PC) |
| D2 | Tech stack | Python (FastAPI) backend + React (Vite) frontend, SQLite storage |
| D3 | Hosting | Local only |
| D4 | Team input | Manual only (picker + paste). No FPL login/cookies/import — also enables hypothetical test lineups |
| D5 | News analysis | LLM-powered with rule-based fallback. LLM = user's OpenAI-compatible endpoint `http://localhost:8888/v1` (user supplies key; model configurable). App must fully work with LLM off |
| D6 | Real-time | Required (live scores, live points, news refresh, deadline countdown) |
| D7 | Sources | Official FPL, BBC, r/FantasyPL, YouTube (Planet FPL), ESPN, Yahoo (optional) |
| D8 | Group context | Differential/mini-league profile + optional public rank display |

### 2.2 Supplemented by this plan (locked, do not revisit)

| # | Decision |
|---|----------|
| D9 | **Deadlines & countdown always come from the API** `events[].deadline_time` (V2 proved the outline's table is wrong). All times stored as UTC ISO-8601; UI renders local time. |
| D10 | Planet FPL default channel: handle `@PlanetFPL`, channel ID **`UC8043oOKTB4uP8Nq15Kz6bg`** (V7). User can add channels in Settings (handle or ID; handles resolved via yt-dlp on first fetch). |
| D11 | Transcript fetching: **yt-dlp subprocess is primary** (V8: installed & verified on user's machine). `youtube-transcript-api` is the documented alternative behind the same adapter interface. |
| D12 | LLM client: plain `httpx` POST to `{base_url}/chat/completions`. **No `openai` SDK dependency.** |
| D13 | BBC v1: RSS title+description only (sufficient for signals). Full-article fetch = on-demand UI button, never scheduled. **Superseded (2026-09-21, FIX.MD Task 3): new PL-relevant items auto-fetch full article text (`sources.bbc.fetch_bodies`, capped by `max_bodies_per_poll`); the on-demand button remains as fallback.** |
| D14 | Ports: backend `8000`, Vite dev `5173`. Two run modes: **dev** (two processes) and **production** (`npm run build` → FastAPI serves `frontend/dist`; single process). |
| D15 | Launchers: `start.ps1` (primary — user is on Windows) + `start.sh`. |
| D16 | Tests: backend pytest with golden fixtures (required); frontend v1 has **no** test framework (manual E2E checklist in README). |
| D17 | HTTP policy: one shared `httpx.AsyncClient`; timeout 15 s; descriptive static User-Agent `fpl-tracker/1.0 (local personal use)`; retries 2× with 2 s backoff on 5xx/timeout only; global semaphore **max 20 concurrent** requests; polite intervals per §4.6. |
| D18 | Schema-drift guard: every bootstrap fetch checks a required-field list; any missing → `poll_log` error + non-fatal UI banner on Dashboard/Settings. |
| D19 | SSE `/api/live/stream` sends a `hello` event with a full state snapshot on connect (seamless reconnects); client uses `EventSource` auto-reconnect. |
| D20 | Paste-a-list fuzzy matching: exact `web_name`/`known_name` → case-insensitive full match → token-sort + Levenshtein distance ≤ 2 → ambiguous candidates shown in a confirmation table (never auto-guess). |

---

## 3. How to use this plan set (builder operating rules)

1. **Work milestone by milestone** (M1 → M2 → M3 → M4). Each part file lists tasks with: goal, files, key functions (signatures), logic spec, acceptance criteria, tests. Implement in task order within a milestone.
2. **Commit after each milestone's exit criteria pass** (git init the repo in T1.1).
3. **Stop-the-line rules**:
   - Never call any FPL write endpoint or store FPL credentials. The app is read-only + local.
   - Never block a milestone on an *optional* source (Yahoo, Reddit OAuth, ESPN standings). Log and move on.
   - If a live API returns an unexpected shape: don't crash — defensive parse, log raw JSON to `poll_log`, surface a non-fatal banner, continue with last-good data.
   - Ask the user only the questions marked **ASK** (LLM model name at M2 start; Reddit OAuth preference at M4). Everything else is decided.
4. **Every milestone ends with its test suite green** and its exit criteria demonstrably met (run the manual checklist in a real browser).
5. **Season context today**: 2026/27, GW5 just finished, GW6 deadline `2026-10-10T10:00:00Z` (V2). The app must be season-agnostic: events, deadlines, chips, fixtures are read from the API at runtime.

---

## 4. Verified source facts (live-verified 2026-09-19)

⚠️ items from `OUTLINE.MD` §11, resolved by live checks. The builder re-verifies only the items marked "re-verify".

| # | Fact | Build implication |
|---|------|-------------------|
| V1 | FPL `bootstrap-static` ✅ 200. `chips[]` = 8 entries: `wildcard`/`freehit`/`bboost`/`3xc` × 2 sets, each with `start_event`/`stop_event` (sets split 1–19 / 20–38). `events[]` carries `deadline_time`, `is_current` (GW5), `is_next` (GW6), `chip_plays`, `most_captained`, `most_vice_captained`, `most_selected`, `most_transferred_in`, `top_element`, `average_entry_score`, `highest_score`. | Chip windows are read from `chips[]` at runtime — never hardcoded. |
| V2 | **GW6 deadline per API = `2026-10-10T10:00:00Z`**; GW5 = `2026-09-18T17:30:00Z`. The outline's deadline table (03:00 / 10:30 UK) does **not** match the API. | **Contradiction flagged**: API is the single source of truth (D9). Outline table is reference-only and wrong. |
| V3 | Verified `elements[]` field set (beyond outline's list): `ep_this`, `scout_risks`, `scout_news_link`, `value_form`, `value_season`, `price_change_locked_until`, `now_cost_rank`, `opta_code`, `dreamteam_count`, `in_dreamteam`, `region`, `birth_date`, `team_join_date`, `starts_per_90`, `clean_sheets_per_90`, full per-90 xG family, ICT influence/creativity/threat rank fields. | Full DDL in §7 stores the complete verified set; parser is defensive (missing optional field → NULL). |
| V4 | ESPN scoreboard ✅ 200 with live in-play data. Shape: `events[].competitions[0]` → `competitors[]` (`homeAway`, `score`, `team.{id,displayName,abbreviation}`, `form`), `status` (`state`: `pre`/`in`/`post`, `displayClock`, `clock`, `type.detail`), `date`, `venue`. | Exact mapping spec in Part 4 (T3.2). |
| V5 | Reddit `hot.rss` ✅ 200 — **Atom** format (not RSS). Entries: `title`, `link`, `published`, `updated`, `content` (HTML). Many posts are **image-only** (content = HTML table with `<img>`). A pinned moderator megathread is always present. | Parser must: use `title` + `<img alt>` text; skip the pinned megathread; thread bodies optional via `comments/{id}.rss`. |
| V6 | BBC RSS ✅ 200 but includes **non-PL football** (WSL, EFL, Scotland, international news). Items: `title`, `description`, `link`, `pubDate`, `media:thumbnail`. | PL relevance filter required before storing (club names + "Premier League"/"PL" keywords; see Part 3 T2.2). |
| V7 | **Planet FPL channel ID = `UC8043oOKTB4uP8Nq15Kz6bg`** (resolved via yt-dlp). Channel RSS ✅ 200: 15 entries with `yt:videoId`, `title`, `published`, and **full `media:description`** — descriptions contain chapter lists and player names (e.g. CotC episodes list Fernandes, Mbeumo, Maguire, Dewsbury-Hall, Garner, Enciso…). | Two-stage YouTube pipeline (Part 3 T2.5): (a) RSS title+description → cheap signals every poll; (b) transcript fetch only for priority videos. |
| V8 | yt-dlp **installed on user's machine** (v2026.03.17, pip). Auto-captions download works (Weekender ep.48: no manual subs, auto `en` present). Warnings: "no supported JavaScript runtime" (deno expected) and version >90 days old. | **Contradiction flagged**: outline preferred `youtube-transcript-api`; verified evidence supports yt-dlp as primary (D11). Command must include `--js-runtimes node` (Node is installed for the frontend anyway). README: recommend `pip install -U yt-dlp`. |
| V9 | Test transcript saved at `tmp-video-test\The Weekender ep. 48  Planet FPL 202627.txt` (2,057 lines, ~75 KB, user wants it kept). Auto-captions mis-transcribe real names/teams: Calvert-Lewin → "Calbertt Lewing", Leeds → "Leads", Strasbourg → "Stradarmas". | Copied (not moved) to `backend/tests/fixtures/weekender_ep48.txt` at M2 (T2.12) as golden fixture. Proves: LLM extraction with the FPL player list as reference is required; rule-based extractor must be conservative on transcripts (title/description first). |
| V10 | Local LLM at `http://localhost:8888/v1` is **UP**; `GET /v1/models` returns **401 without API key** (endpoint is key-gated). | Endpoint reachable & key-gated confirmed. `LLM_MODEL` unknown → **ASK user at M2 start** (T2.13); app must run fully without it (D5). |
| V11 | Yahoo `sportsengine.api.yahoo.com` unresolvable from the dev environment (outline §4.6). | Yahoo stays optional/droppable: one verification attempt at M4 (T4.8), drop silently on failure. ESPN covers live scores. |

### 4.1 Per-source access specs

| Source | Endpoint(s) | Auth | Format | Politeness / gotchas |
|---|---|---|---|---|
| **FPL** | `GET https://fantasy.premierleague.com/api/bootstrap-static/` · `GET /api/element-summary/{player_id}/` · `GET /api/event/{gw}/` · `GET /api/game-settings/` · `GET /api/entry/{entry_id}/` (public, optional) | None | JSON | bootstrap 15 min cadence; element-summary 60 s **only** for the user's 15 selected players during live windows; no `/api/draft/*` (404 without session — D4) |
| **ESPN** | `GET https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard` · `.../eng.1/news` · `.../eng.1/standings` (optional) | None | JSON | scoreboard 30 s **only in live windows**; news 30 min |
| **BBC** | `GET https://feeds.bbci.co.uk/sport/football/rss.xml` (+ on-demand article page fetch) | None | RSS 2.0 XML | 30 min cadence (15 min in GW-eve window); PL filter (V6); article fetch: auto for new PL-relevant items (≤ `max_bodies_per_poll` pages/poll) + on-demand button (D13 superseded, FIX.MD Task 3) |
| **Reddit** | RSS: `GET https://www.reddit.com/r/FantasyPL/hot.rss` (also `new.rss`, `top.rss?t=week`); thread bodies `GET /r/FantasyPL/comments/{id}.rss` · OAuth (v1.0.1): `hot.json` + `/comments/{id}.json` | None (OAuth client-credentials optional, implemented in v1.0.1) | **Atom** XML (RSS) or JSON (OAuth) | 30 min cadence; `hot.json` is bot-blocked for anonymous access, and `www.reddit.com/*.json` 403s even with a valid token — the OAuth-mode path goes via `oauth.reddit.com` (verified 2026-09-21); filter pinned megathread (V5); static descriptive UA |
| **YouTube** | Channel RSS `GET https://www.youtube.com/feeds/videos.xml?channel_id={id}`; transcripts via yt-dlp subprocess (D11) | None | Atom XML + VTT | RSS 1 h cadence; max 3 transcript fetches per poll; transcript only for priority videos (V7/V8) |
| **LLM** | `POST {LLM_BASE_URL}/chat/completions` (default `http://localhost:8888/v1`) | User's API key (401 without, V10) | JSON | 60 s timeout; batch ≤ ~8k input chars/call; **ASK** model name at M2 start |
| **Yahoo** (optional) | TBA — verify at M4 (V11) | None | JSON | Drop silently if it fails |

### 4.2 ToS posture (keep)

Light, polite polling (intervals above; ≤20 concurrent requests); no scraping of paywalled/protected content; Reddit via RSS (OAuth optional); BBC via official RSS; YouTube via official channel RSS + public transcripts; **no FPL account access at all**; no writes to FPL; LLM data boundary = fetched public content + player names only; everything stored locally in SQLite; no telemetry.

---

## 5. FPL rules — 2026/27 (optimizer constraint engine, self-contained)

Source: `OUTLINE.MD` §3 (extracted verbatim from the official rules page 2026-09-19). The optimizer must implement these **exactly**. ⚠️ Re-verify at each season start (rules changed in 2026/27 and can change again — see Part 5, T4.10).

### 5.1 Squad
- Exactly **15 players**: **2 GK, 5 DEF, 5 MID, 3 FWD**.
- Total squad value ≤ **£100.0m**. API unit: `now_cost` is in £0.1m → **budget = 1000 units**.
- **Max 3 players from a single club** (squad-level).

### 5.2 Starting XI
- Pick **11 of 15** by the GW deadline.
- **Any formation allowed, provided that 1 GK, ≥3 DEF and ≥1 FWD are selected at all times** (including after automatic substitutions).
- No explicit MID minimum (squad 2/5/5/3 + 11 slots imply ≥2 MID in practice).
- **Bench = remaining 4**, in user-set priority order. No official minimum mix on the bench, but the optimizer heuristically prefers balanced bench coverage (≥1 DEF, ≥1 MID, ≥1 FWD when affordable) and must always keep the XI valid after auto-substitution.

### 5.3 Automatic substitutions (simulate in real time, Part 4 T3.4)
- Starting **GK** doesn't play → replaced by the benched GK (if he played).
- Starting **outfield** player doesn't play → replaced by the **highest-priority outfield substitute who played and doesn't break the formation rules** (position-matched: with 3 DEF in the XI, a DEF can only be replaced by a DEF).
- "Playing" = making an appearance **or** receiving a yellow/red card.

### 5.4 Captaincy (⚠️ 2026/27 rule change — implement exactly this)
- Nominated from the **starting XI only**: 1 captain + 1 vice-captain, distinct players.
- **Captain's score ×2.**
- **Vice-captain has NO multiplier in 2026/27** — pure backup: if the captain plays **zero minutes**, the ×2 moves to the VC. If both play zero minutes, no doubling.
- (Older seasons used 1.5× VC — do **not** implement that.)

### 5.5 Transfers (new system in 2026/27)
- Unlimited free transfers **before the first deadline**.
- After the first deadline: **1 free transfer per GW**.
- Each **additional** transfer in the same GW deducts **4 points** (deducted at the start of the next GW).
- Unused free transfers **roll over**; **max bank = 5**.
- Hard cap: **20 transfers in a single GW** (not while playing Wildcard/Free Hit).
- The app stores the user's **free-transfer bank (1–5)** and computes per suggestion: transfers needed, free transfers consumed, −4 penalty points.

### 5.6 Player prices
- Prices change mid-season by transfer-market popularity.
- **Half-increase sell rule**: if a player's price rose after purchase, on sale you keep only half the increase, rounded **down** to £0.1m (bought £7.5m, now £7.8m → sells for £7.6m). The optimizer must use this in budget deltas for risen players.
- API: `now_cost`, `cost_change_start`, `price_change_projections` (use for "likely to rise/fall" hints).

### 5.7 Deadlines
- All changes (XI, transfers, captain, bench order) by the **GW deadline = 90 min before the first kickoff** of the GW. A deadline won't change within 24 h of its scheduled time.
- **Runtime reads `events[].deadline_time` from the API (D9/V2).** The outline's 38-GW table is reference-only and contains errors.

### 5.8 Chips (2026/27: two sets, split at the GW19 deadline)
- **Only one chip per GW.**
- API `chips[]` confirms windows (V1): each chip exists in two versions — set 1 active events 1/2→19, set 2 active 20→38.

| Chip | Effect | Availability 2026/27 | Cancellable? |
|------|--------|---------------------|--------------|
| **Wildcard** (×2) | All transfers in the GW (incl. already made) are free | #1: after GW1 → GW19 deadline; #2: after GW19 → season end | No, once played |
| **Free Hit** (×2) | Unlimited free transfers for one GW; squad reverts at next deadline | #1: after GW1 → GW19 deadline; #2: after GW19 | No, once confirmed; **cannot be played in consecutive GWs** |
| **Bench Boost** (×2) | Bench players' points count in the total | Set 1: season start → GW19 deadline; Set 2: after | Yes, before deadline |
| **Triple Captain** (×2) | Captain ×3 instead of ×2 | Set 1: season start → GW19 deadline; Set 2: after | Yes, before deadline |

- Playing Wildcard/Free Hit **retains saved free transfers** (bank unchanged for the following GW).
- The app stores chips remaining per type and validates advice against active windows (e.g. never suggest Free Hit in GW20 if played in GW19).

### 5.9 Scoring (2026/27)

| Action | Points |
|--------|--------|
| Playing up to 60 minutes | 1 |
| Playing 60+ minutes (excl. stoppage) | 2 |
| Goal — GK | 10 |
| Goal — DEF | 6 |
| Goal — MID | 5 |
| Goal — FWD | 4 |
| Goal assist | 3 |
| Clean sheet — GK/DEF | 4 |
| Clean sheet — MID | 1 |
| Every 3 shot saves (GK) | 1 |
| DEF: ≥10 clearances+blocks+interceptions+tackles | 2 |
| MID/FWD: ≥12 clearances+blocks+interceptions+tackles+recoveries | 2 |
| Penalty save | 5 |
| Penalty miss | −2 |
| Bonus points (best players per match) | 1–3 |
| Every 2 goals conceded (GK/DEF) | −1 |
| Yellow card | −1 |
| Red card | −3 |
| Own goal | −2 |

### 5.10 BPS (Bonus Points System) table
BPS drives the 1–3 bonus. Full table (verified in outline §3.10):

| Action | BPS | Action | BPS |
|--------|-----|--------|-----|
| Playing 1–60 min | 3 | Conceding a goal (GK/DEF) | −4 |
| Playing over 60 min | 6 | Conceding a penalty | −3 |
| Goal from penalty | 12 | Missing a penalty | −6 |
| Goal (GK/DEF, non-pen) | 12 | Yellow card | −3 |
| Goal (MID, non-pen) | 18 | Red card | −9 |
| Goal (FWD, non-pen) | 24 | Own goal | −6 |
| Assist | 9 | Missing a big chance | −3 |
| Clean sheet (GK/DEF) | 12 | Error leading to goal | −3 |
| Save | 2 | Error leading to attempt | −1 |
| Save from shot inside box | 1 | Conceding a foul | −1 |
| Save from big chance | 1 | Being caught offside | −1 |
| Penalty save | 7 | Shot off target | −1 |
| Every 3 clearances/blocks/interceptions | 1 | 70–79% pass completion (≥30 att.) | 2 |
| Every 3 recoveries | 1 | 80–89% pass completion (≥30 att.) | 4 |
| Creating a chance | 1 | 90%+ pass completion (≥30 att.) | 6 |
| Creating a big chance | 3 | Successful open-play cross | 1 |
| Successful tackle | 2 | Successful dribble | 1 |
| Scoring the match-winning goal | 3 | Goalline clearance | 9 |
| Foul won | 1 | Shot on target | 2 |

BPS is supplied by Opta and final once the GW is marked final.

---

## 6. Architecture

### 6.1 Stack
- **Backend**: Python 3.11+, **FastAPI**, **APScheduler** (AsyncIOScheduler), **SQLite** (stdlib `sqlite3` + thin async wrapper; no ORM), **httpx** (async), **feedparser** (RSS/Atom), **yt-dlp** (subprocess, D11), **python-dotenv**, **pydantic** (v2, request/response models).
- **Frontend**: React 18 + Vite + **TypeScript**, React Router (5 routes), plain CSS (single `app.css`, dark theme default), native `EventSource` for SSE. No UI framework, no state library (React state + a small `useApi`/`useSSE` hook layer).
- **Process model**:
  - **Dev**: FastAPI (uvicorn, port 8000, `--reload`) + Vite dev server (port 5173, proxy `/api` → 8000).
  - **Production**: `npm run build` → `frontend/dist/` mounted by FastAPI at `/` (single process, port 8000).
  - One backend process owns: API + schedulers + SSE.
- **Launcher**: `start.ps1` / `start.sh` (D15): create venv if missing, `pip install -r requirements.txt`, `npm install` if missing, build frontend if missing, run (dev or `--prod` flag).

### 6.2 Directory layout (from outline §9.2, kept verbatim + additions)

```
fpl-tracker/                      ← this workspace root
├── backend/
│   ├── app/
│   │   ├── main.py               # FastAPI app factory, CORS, static mount, lifespan (scheduler start/stop)
│   │   ├── config.py             # .env + config.json loading, pydantic Settings model
│   │   ├── db.py                 # connection, init (DDL from §7), row helpers, WAL
│   │   ├── scheduler.py          # APScheduler jobs (§4.1 intervals, GW-eve bump)
│   │   ├── httpclient.py         # shared httpx.AsyncClient, UA, retries, semaphore (D17)
│   │   ├── fetchers/
│   │   │   ├── fpl.py            # bootstrap-static, element-summary, event, game-settings, entry, fixtures builder
│   │   │   ├── espn.py           # scoreboard, news, standings
│   │   │   ├── reddit.py         # Atom RSS (hot/new/top + thread bodies)
│   │   │   ├── bbc.py            # RSS + on-demand article fetch
│   │   │   └── youtube.py        # channel RSS listing + yt-dlp transcript subprocess
│   │   ├── signals/
│   │   │   ├── schema.py         # PlayerSignal pydantic model
│   │   │   ├── llm_extractor.py  # httpx chat/completions client (D12)
│   │   │   ├── rule_extractor.py # keyword fallback + official-news ingest
│   │   │   └── names.py          # player name matching (D20, mis-transcription tolerant)
│   │   ├── optimizer/
│   │   │   ├── rules.py          # constraint validator (§5, single source of truth)
│   │   │   ├── scoring.py        # EP model (§7.2 of outline; Part 2 T1.6)
│   │   │   ├── transfers.py      # transfer math + chip advice
│   │   │   └── solver.py         # multi-start hill climbing
│   │   ├── live/
│   │   │   ├── compute.py        # live totals, auto-bench sim, bonus race, countdown
│   │   │   └── sse.py            # SSE hub (event bus, hello snapshot, keepalive)
│   │   └── api/
│   │       ├── meta.py           # season state, health, refresh endpoints, settings
│   │       ├── players.py        # player search/filter
│   │       ├── lineups.py        # saved lineups CRUD + match-names
│   │       ├── suggestions.py    # generate/list/delete
│   │       ├── news.py           # signals + raw items + on-demand body fetch
│   │       ├── live.py           # live state + SSE stream
│   │       └── entry.py          # optional group-rank card
│   ├── tests/
│   │   ├── conftest.py           # fixture loaders, db session, API test client
│   │   ├── fixtures/             # weekender_ep48.txt (copied from tmp-video-test/), sample JSON/XML
│   │   ├── test_rules.py
│   │   ├── test_ep_model.py
│   │   ├── test_transfers.py
│   │   ├── test_solver.py
│   │   ├── test_match_names.py
│   │   ├── test_extractors.py
│   │   ├── test_auto_bench.py
│   │   └── test_api.py
│   └── requirements.txt
├── frontend/                     # Vite + React (pages: Dashboard, MyTeam, Suggestions, News, Settings)
│   └── src/
│       ├── main.tsx, App.tsx, app.css
│       ├── api.ts                # typed API client
│       ├── hooks/useSSE.ts       # EventSource wrapper (D19)
│       ├── types.ts              # shared TS types mirroring §8 schemas
│       └── pages/                # Dashboard.tsx, MyTeam.tsx, Suggestions.tsx, News.tsx, Settings.tsx
│           └── components/       # PitchView, PlayerPicker, SignalsTable, LiveScores, Countdown, ...
├── data/                         # fpl.db (SQLite), created at runtime
├── .env.example                  # LLM_BASE_URL, LLM_API_KEY, LLM_MODEL
├── .env                          # user-filled (gitignored)
├── config.json                   # user-tunable (defaults in §10)
├── start.ps1 / start.sh
└── README.md
```

### 6.3 Module responsibilities

| Module | Owns | Must NOT |
|---|---|---|
| `fetchers/*` | HTTP/RSS/subprocess I/O, raw→table writes, `poll_log` entries | Business logic, signal extraction |
| `signals/*` | Name matching, LLM/rule extraction, signal store/dedupe/expiry | HTTP to sources (consumes `raw_items`) |
| `optimizer/*` | Rules validation, EP model, transfer math, solving | I/O (pure functions over in-memory data + read-only DB queries) |
| `live/*` | Live-window detection, live computation, SSE broadcast | Scheduling (scheduler triggers pollers; pollers push into `live.sse` bus) |
| `api/*` | Request validation (pydantic), routing, response shaping | Query logic beyond thin service calls |
| `scheduler.py` | Job registration, intervals, GW-eve bump, overlap protection (`coalesce=True, max_instances=1`) | Fetch logic |

---

## 7. SQLite schema (full DDL)

`backend/app/db.py` executes this on startup (`CREATE TABLE IF NOT EXISTS`); `PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;`. All timestamps stored as UTC ISO-8601 strings (`YYYY-MM-DDTHH:MM:SSZ`).

```sql
-- players: one row per FPL player, upserted on every bootstrap fetch
CREATE TABLE IF NOT EXISTS players (
  id INTEGER PRIMARY KEY,                 -- FPL player id
  season TEXT NOT NULL,                   -- e.g. '2026/27' (informational; wipe on rollover)
  web_name TEXT NOT NULL,
  known_name TEXT,
  first_name TEXT, second_name TEXT,
  element_type INTEGER NOT NULL,          -- 1 GK, 2 DEF, 3 MID, 4 FWD
  team INTEGER NOT NULL,                  -- FPL team id
  team_code TEXT,
  now_cost INTEGER NOT NULL,              -- £0.1m units (63 = £6.3m)
  cost_change_start INTEGER,
  cost_change_event INTEGER,
  price_change_percent INTEGER,
  price_change_hourly_rate INTEGER,
  price_change_projections TEXT,          -- JSON array (raw)
  price_change_locked_until TEXT,
  total_points INTEGER, points_per_game REAL, form REAL,
  event_points INTEGER, minutes INTEGER, starts INTEGER,
  goals_scored INTEGER, assists INTEGER, clean_sheets INTEGER,
  goals_conceded INTEGER, own_goals INTEGER,
  penalties_saved INTEGER, penalties_missed INTEGER,
  yellow_cards INTEGER, red_cards INTEGER, saves INTEGER,
  bonus INTEGER, bps INTEGER,
  expected_goals REAL, expected_assists REAL, expected_goal_involvements REAL,
  expected_goals_per_90 REAL, expected_assists_per_90 REAL,
  expected_goal_involvements_per_90 REAL,
  expected_goals_conceded REAL, expected_goals_conceded_per_90 REAL,
  goals_conceded_per_90 REAL, saves_per_90 REAL,
  ep_next REAL, ep_this REAL,
  selected_by_percent REAL,
  transfers_in INTEGER, transfers_out INTEGER,
  transfers_in_event INTEGER, transfers_out_event INTEGER,
  influence REAL, creativity REAL, threat REAL, ict_index REAL,
  influence_per_90 REAL, creativity_per_90 REAL, threat_per_90 REAL, ict_index_per_90 REAL,
  influence_rank INTEGER, influence_rank_type INTEGER,
  creativity_rank INTEGER, creativity_rank_type INTEGER,
  threat_rank INTEGER, threat_rank_type INTEGER,
  ict_index_rank INTEGER, ict_index_rank_type INTEGER,
  clearances_blocks_interceptions INTEGER, recoveries INTEGER, tackles INTEGER,
  defensive_contribution INTEGER,
  status TEXT,                            -- a|d|i|s|u (may be NULL = available)
  news TEXT, news_added TEXT,
  chance_of_playing_this_round INTEGER,   -- NULL|0|50|100
  chance_of_playing_next_round INTEGER,
  can_transact INTEGER, can_select INTEGER, removed INTEGER,
  penalties_order TEXT, penalties_text TEXT,
  corners_and_indirect_freekicks_order TEXT, corners_and_indirect_freekicks_text TEXT,
  direct_freekicks_order TEXT, direct_freekicks_text TEXT,
  value_form REAL, value_season REAL,
  dreamteam_count INTEGER, in_dreamteam INTEGER,
  now_cost_rank INTEGER,
  opta_code TEXT, region TEXT, birth_date TEXT, team_join_date TEXT,
  raw_json TEXT,                          -- full element JSON (drift forensics)
  fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_players_team ON players(team);
CREATE INDEX IF NOT EXISTS idx_players_type ON players(element_type);

-- fixtures: team-level fixtures, derived from per-player element-summary fixtures (T1.3)
CREATE TABLE IF NOT EXISTS fixtures (
  id INTEGER PRIMARY KEY,                 -- FPL fixture id
  event INTEGER NOT NULL,
  home_team INTEGER NOT NULL,
  away_team INTEGER NOT NULL,
  kickoff_time TEXT NOT NULL,             -- UTC ISO
  difficulty_home INTEGER,                -- 1–5 (home team's perspective)
  difficulty_away INTEGER,                -- 1–5 (away team's perspective)
  status TEXT,                            -- not_started|in_play|finished
  minutes INTEGER,
  score_home INTEGER, score_away INTEGER,
  raw_json TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(event, home_team, away_team)
);
CREATE INDEX IF NOT EXISTS idx_fixtures_event ON fixtures(event);

-- events: all 38 GWs from bootstrap
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  deadline_time TEXT NOT NULL,            -- UTC ISO (D9: single source of truth)
  is_current INTEGER, is_next INTEGER, finished INTEGER,
  average_entry_score INTEGER, highest_score INTEGER,
  most_captained INTEGER, most_vice_captained INTEGER,
  most_selected INTEGER, most_transferred_in INTEGER,
  top_element INTEGER,
  chip_plays TEXT,                        -- JSON
  raw_json TEXT, fetched_at TEXT NOT NULL
);

-- chips: from bootstrap chips[] (two sets per chip)
CREATE TABLE IF NOT EXISTS chips (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,                     -- wildcard|freehit|bboost|3xc
  chip_type TEXT NOT NULL,                -- transfer|team
  set_index INTEGER NOT NULL,             -- 1 or 2
  start_event INTEGER NOT NULL,
  stop_event INTEGER NOT NULL,
  fetched_at TEXT NOT NULL,
  UNIQUE(name, set_index)
);

-- raw_items: every ingested article/thread/video/official-news change
CREATE TABLE IF NOT EXISTS raw_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,                   -- fpl-official|bbc|espn|reddit|youtube
  external_id TEXT NOT NULL,              -- native id or url
  kind TEXT NOT NULL,                     -- article|thread|video|official-news
  title TEXT,
  url TEXT,
  published_at TEXT,
  body TEXT,                              -- full text (transcript/article) or NULL
  content_hash TEXT NOT NULL,             -- sha256(normalized source+title+body[:2000])
  takeaways TEXT,                         -- JSON array of one-line takeaways (LLM output)
  processed INTEGER NOT NULL DEFAULT 0,   -- 1 = signal extraction completed
  retrieved_at TEXT NOT NULL,
  UNIQUE(source, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_items_source ON raw_items(source, retrieved_at);

-- signals: extracted per-player signals
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  player_id INTEGER NOT NULL,
  category TEXT NOT NULL,                 -- injury|suspension|selection|rotation|return|transfer|other
  sentiment TEXT NOT NULL,                -- negative|positive|neutral
  confidence REAL NOT NULL,               -- 0.0–1.0
  summary TEXT NOT NULL,                  -- one-line takeaway
  source TEXT NOT NULL,                   -- fpl-official|bbc:<url>|espn:<id>|reddit:<thread_id>|youtube:<video_id>
  url TEXT,
  published_at TEXT,
  retrieved_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,               -- TTL (default 72 h; official = until replaced)
  raw_item_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_signals_player ON signals(player_id, expires_at);

-- lineups: user-saved lineups (real team + hypothetical test lineups)
CREATE TABLE IF NOT EXISTS lineups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  transfer_bank INTEGER NOT NULL DEFAULT 1,   -- 1–5
  chips_json TEXT NOT NULL DEFAULT '{"wildcard":2,"freehit":2,"bboost":2,"triple_captain":2}',
  is_current INTEGER NOT NULL DEFAULT 0,
  raw_json TEXT
);

CREATE TABLE IF NOT EXISTS lineup_players (
  lineup_id INTEGER NOT NULL REFERENCES lineups(id) ON DELETE CASCADE,
  player_id INTEGER NOT NULL,
  role TEXT NOT NULL,                   -- starter|bench
  bench_order INTEGER,                  -- 1–4, NULL for starters
  is_captain INTEGER NOT NULL DEFAULT 0,
  is_vice_captain INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (lineup_id, player_id)
);

-- suggestions: generated suggestion history
CREATE TABLE IF NOT EXISTS suggestions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lineup_id INTEGER NOT NULL REFERENCES lineups(id) ON DELETE CASCADE,
  profile TEXT NOT NULL,                -- max_ep|differential|safe
  target_gw INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  result_json TEXT NOT NULL             -- full SuggestionObject (§8.6)
);
CREATE INDEX IF NOT EXISTS idx_suggestions_lineup ON suggestions(lineup_id, created_at);

-- live state (M3)
CREATE TABLE IF NOT EXISTS live_matches (
  fixture_id INTEGER PRIMARY KEY REFERENCES fixtures(id) ON DELETE CASCADE,
  score_h INTEGER, score_a INTEGER,
  minute INTEGER, status TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS live_player_points (
  player_id INTEGER NOT NULL,
  event INTEGER NOT NULL,
  points INTEGER, minutes INTEGER, bps INTEGER,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (player_id, event)
);

-- poll_log: every fetch attempt (health + drift forensics)
CREATE TABLE IF NOT EXISTS poll_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  status TEXT NOT NULL,                 -- ok|error|skipped
  rows INTEGER,
  error TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_poll_source ON poll_log(source, started_at);
```

**Season rollover**: on bootstrap fetch, if `events[0].deadline_time` year differs from the stored `players.season` (or `game_settings` season marker changes), wipe `players, fixtures, events, chips, signals, live_matches, live_player_points` (keep `lineups, lineup_players, suggestions, raw_items, poll_log`) and log a `poll_log` row `source='season'`.

---

## 8. REST API spec

Base: `http://localhost:8000/api`. All responses JSON. Errors: `422 {errors: [{code, message}]}` for validation, `502 {error}` for upstream fetch failures, `404 {error}` for missing ids. Times are UTC ISO-8601.

### 8.1 Meta

**`GET /api/meta/season`**
```json
{
  "season": "2026/27",
  "current_gw": {"id": 5, "name": "Gameweek 5", "deadline_time": "2026-09-18T17:30:00Z", "finished": true},
  "next_gw":   {"id": 6, "name": "Gameweek 6", "deadline_time": "2026-10-10T10:00:00Z", "finished": false},
  "deadline_is_past": false,
  "live_mode": false,
  "live_window": {"start": null, "end": null},
  "chip_windows": [
    {"chip": "wildcard", "set": 1, "start_event": 2, "stop_event": 19},
    {"chip": "wildcard", "set": 2, "start_event": 20, "stop_event": 38}
  ],
  "schema_drift": []
}
```

**`GET /api/meta/health`** → `{db_ok: true, last_polls: {fpl: {status, finished_at, error}, ...}, schema_drift: [..]}`

**`POST /api/refresh/{source}`** (`source` ∈ `fpl|espn|bbc|reddit|youtube|all`) → `{status: "ok", rows: 1234}` (runs the fetcher immediately, bypassing the schedule)

**`GET /api/settings`** → the full `config.json` object (§10).
**`PUT /api/settings`** → partial update; validated (e.g. `optimizer.differential_lambda` numeric 0–10; intervals ≥ min bounds); returns the new full config. Writes `config.json` to disk.

### 8.2 Players

**`GET /api/players?search=&pos=&team=&min_cost=&max_cost=&has_signal=`**
Query params all optional; `pos` ∈ `GK|DEF|MID|FWD`; `team` = team_code; costs in £0.1m.
```json
[
  {"id": 115, "web_name": "K. Havertz", "pos": "FWD", "team_code": "ARS", "now_cost": 105,
   "form": 12.5, "total_points": 34, "ppge": 6.8, "ep_next": 6.2,
   "selected_by_percent": 8.4, "status": null, "news": "",
   "signals": [{"category": "selection", "sentiment": "positive", "confidence": 0.7, "summary": "Confirmed to start vs X"}]}
]
```
(`signals` = active signals only, max 3, shortest summaries.)

### 8.3 Lineups

**`GET /api/lineups`** → `[{id, name, created_at, updated_at, transfer_bank, chips, is_current, player_count: 15}]`

**`POST /api/lineups`**
```json
{
  "name": "My real team",
  "transfer_bank": 2,
  "chips": {"wildcard": 2, "freehit": 1, "bboost": 2, "triple_captain": 2},
  "players": [
    {"player_id": 1, "role": "starter", "is_captain": true},
    {"player_id": 2, "role": "starter", "is_vice_captain": true},
    {"player_id": 3, "role": "bench", "bench_order": 1}
  ]
}
```
→ `201` full lineup object (below) or `422 {errors: [{code, message}]}` using the §8.9 error catalog.

**`GET /api/lineups/{id}`**
```json
{
  "id": 1, "name": "My real team", "transfer_bank": 2,
  "chips": {"wildcard": 2, "freehit": 1, "bboost": 2, "triple_captain": 2},
  "is_current": true,
  "squad": [
    {"player_id": 1, "web_name": "David Raya", "pos": "GK", "team_code": "ARS", "now_cost": 50,
     "role": "starter", "bench_order": null, "is_captain": false, "is_vice_captain": false,
     "form": 8.0, "total_points": 21, "ep_next": 4.1, "status": null, "news": ""}
  ],
  "budget_remaining": 0,
  "validation": {"valid": true, "errors": []}
}
```

**`PUT /api/lineups/{id}`** — same body as POST → updated object.
**`DELETE /api/lineups/{id}`** → `204`.
**`POST /api/lineups/{id}/set-current`** → `200` (clears other `is_current` flags).

**`POST /api/lineups/match-names`** (paste-a-list, D20)
```json
{"names": ["Raya", "Calvert-Lewin", "Brtwo", ...]}
```
→
```json
{
  "matches": [
    {"name": "Raya", "candidates": [{"player_id": 143, "web_name": "D. Raya", "pos": "GK", "team_code": "ARS", "now_cost": 50, "score": 1.0}], "status": "exact"},
    {"name": "Brtwo", "candidates": [{"player_id": 9, "web_name": "B. Fernandes", "pos": "MID", "team_code": "MUN", "now_cost": 100, "score": 0.82}], "status": "fuzzy"},
    {"name": "Xyz", "candidates": [], "status": "no_match"}
  ]
}
```
Frontend shows a confirmation table; user confirms/picks candidates, then POSTs the lineup.

### 8.4 Suggestions

**`POST /api/suggestions/generate`**
```json
{"lineup_id": 1, "target_gw": 6, "profiles": ["max_ep", "differential", "safe"]}
```
(`target_gw` defaults to next GW; `profiles` defaults to all three.)
→ `200 {suggestions: [SuggestionObject, ...]}` (one per profile; persisted to `suggestions`).

**`GET /api/suggestions?lineup_id=1&limit=20`** → `[SuggestionObject with id/created_at]`
**`DELETE /api/suggestions/{id}`** → `204`.

### 8.5 News & signals

**`GET /api/signals?player_id=&team=&category=&active=`** (`active` default `true`)
```json
[
  {"id": 77, "player_id": 115, "web_name": "K. Havertz", "team_code": "ARS",
   "category": "injury", "sentiment": "negative", "confidence": 0.8,
   "summary": "Out 3–4 weeks with a hamstring injury",
   "source": "bbc:https://www.bbc.co.uk/sport/football/articles/...",
   "url": "https://...", "published_at": "2026-09-18T15:00:00Z",
   "retrieved_at": "2026-09-18T15:30:00Z", "expires_at": "2026-09-21T15:30:00Z"}
]
```

**`GET /api/items?source=&kind=&limit=&offset=`**
```json
[{"id": 501, "source": "youtube", "kind": "video", "title": "The Weekender ep. 48 | Planet FPL 2026/27",
  "url": "https://www.youtube.com/watch?v=kU_mDgO3urA", "published_at": "2026-09-18T12:30:06Z",
  "takeaways": ["Joao Pedro saga continues", "Free hit & wildcard drafts covered"], "processed": true}]
```

**`POST /api/items/{id}/fetch-body`** (BBC on-demand article fallback; new items are auto-fetched — D13 superseded, FIX.MD Task 3) → `{body: "..."}` (404 if item has no fetchable URL).

### 8.6 SuggestionObject (the core response shape)

```json
{
  "id": 12,
  "profile": "max_ep",
  "target_gw": 6,
  "created_at": "2026-09-19T20:00:00Z",
  "projected_points": {"baseline": 78.4, "adjusted": 81.2, "with_captain": 96.1},
  "squad": [
    {"player_id": 115, "web_name": "K. Havertz", "pos": "FWD", "team_code": "ARS",
     "now_cost": 105, "ep": 6.2, "origin": "kept"}
  ],
  "xi": [{"player_id": 115, "slot": "FWD"}],
  "captain": 115,
  "vice_captain": 120,
  "bench": [{"player_id": 201, "order": 1}],
  "diff": {
    "transfers_in":  [{"player_id": 115, "web_name": "K. Havertz", "cost": 105}],
    "transfers_out": [{"player_id": 310, "web_name": "Old Guy", "sell_value": 63}],
    "cost_delta": 0,
    "total_cost_after": 1000,
    "free_transfers_used": 2,
    "bank_after": 0,
    "penalty_points": 4
  },
  "chip_advice": [
    {"chip": "wildcard", "recommendation": "use", "reason": "5 changes vs bank of 1 — wildcard makes them free"},
    {"chip": "triple_captain", "recommendation": "consider", "reason": "Captain Havertz ep_next 6.2, GW has 2 matches"}
  ],
  "rationale": [
    "Top EP: Havertz (6.2 adj, fixture difficulty 2 vs AVL)",
    "Differential: Havertz owned by only 8.4% — 5th-best EP in pool",
    "Signal applied: 'Confirmed to start' (bbc, conf 0.7) → +0.07"
  ]
}
```
`squad` has exactly 15 (2/5/5/3, ≤1000 units, ≤3/club — guaranteed by validator); `xi` exactly 11; `bench` exactly 4 with orders 1–4; `captain`/`vice_captain` ∈ `xi`, distinct. `projected_points.baseline` = Σ ep_next (XI + captain ×2, no signals); `adjusted` = Σ EP_final (§7.2 formula) for XI + captain ×2; `with_captain` clarifies captain doubling is included in both (field kept for UI clarity).

### 8.7 Live (M3)

**`GET /api/live/state`**
```json
{
  "live_mode": true,
  "matches": [
    {"fixture_id": 501, "event": 6, "home": {"id": 3, "code": "AVL", "name": "Aston Villa"},
     "away": {"id": 1, "code": "TOT", "name": "Tottenham"},
     "score_h": 2, "score_a": 1, "minute": 78, "status": "in_play"}
  ],
  "player_points": [
    {"player_id": 115, "web_name": "K. Havertz", "points": 9, "minutes": 78, "bps": 42, "in_xi": true, "auto_subbed": false}
  ],
  "team_total": 31,
  "bonus_race": [
    {"player_id": 115, "match_fixture_id": 501, "points": 9, "bps": 42, "rank_by_bps": 2, "rank_by_points": 1, "bonus_outcome": "likely 2"}
  ],
  "countdown": {"deadline_time": "2026-10-10T10:00:00Z", "seconds_remaining": 1728000}
}
```

**`GET /api/live/stream`** — SSE (§9).

### 8.8 Entry (optional group context)

**`GET /api/entry`** (only meaningful when `config.group.fpl_entry_id` set; else `400 {error: "fpl_entry_id not set"}`)
```json
{
  "entry_id": 12345678,
  "overall_points": 412, "overall_rank": 154321, "overall_percentile": 98.3,
  "leagues": [
    {"league_id": 999, "name": "Work Mini", "class": "standard", "league_type": "x", "rank": 2, "size": 20}
  ]
}
```
Source: public `GET https://fantasy.premierleague.com/api/entry/{id}/` (poll 15 min, cached).

### 8.9 Error code catalog (rules validator — single source of truth in `optimizer/rules.py`)

| Code | Meaning |
|---|---|
| `SQUAD_SIZE` | Squad must be exactly 15 |
| `SQUAD_COMPOSITION` | Must be 2 GK / 5 DEF / 5 MID / 3 FWD |
| `BUDGET_EXCEEDED` | Total cost > 1000 units |
| `CLUB_LIMIT` | More than 3 players from one club |
| `XI_SIZE` | XI must be exactly 11 |
| `XI_POSITION_MIN` | XI must have 1 GK, ≥3 DEF, ≥1 FWD |
| `BENCH_SIZE` | Bench must be exactly 4 |
| `BENCH_ORDER` | Bench orders must be 1–4, unique |
| `CAPTAIN_NOT_IN_XI` | Captain must be a starter |
| `VICE_CAPTAIN_NOT_IN_XI` | VC must be a starter |
| `CAPTAIN_VC_SAME` | Captain and VC must be distinct |
| `PLAYER_UNAVAILABLE` | Player `status=u`/`s` or `can_select=false` (hard gate for suggestions; warning for user lineups) |
| `BANK_RANGE` | Transfer bank must be 1–5 |
| `CHIP_RANGE` | Chip counts must be 0–2 |
| `CHIP_WINDOW` | Chip advice references a chip outside its active window |
| `TRANSFER_CAP` | More than 20 transfers in one GW (no chip) |

---

## 9. SSE protocol (`GET /api/live/stream`)

Transport: `text/event-stream`; each frame `event: <type>\ndata: <json>\n\n`. Keepalive: `: ping` comment every 30 s. Client: `EventSource` with native auto-reconnect; on (re)connect the server first sends `hello` with a full snapshot (D19).

| Event | Payload |
|---|---|
| `hello` | Same shape as `GET /api/live/state` (§8.7) |
| `score_update` | `{"fixture_id": 501, "score_h": 2, "score_a": 1, "minute": 78, "status": "in_play"}` |
| `player_points` | `{"player_id": 115, "event": 6, "points": 9, "minutes": 78, "bps": 42}` |
| `bonus_race` | `{"bonus_race": [ …same array as /api/live/state… ]}` |
| `deadline_countdown` | `{"deadline_time": "2026-10-10T10:00:00Z", "seconds_remaining": 1727940}` (sent every 60 s in LIVE mode, every 5 min otherwise) |
| `new_signal` | Single signal object (§8.5 shape) |
| `price_change` | `{"player_id": 115, "old_cost": 105, "new_cost": 106}` |

Server-side: a process-wide async pub/sub hub (`live/sse.py`); pollers/compute push events; each connected client gets its own queue (maxsize 100, drop-oldest on overflow). Clients without a stored current lineup still receive `score_update`/`deadline_countdown`; player-specific events are computed against the lineup flagged `is_current`.

---

## 10. Configuration

### 10.1 `.env` (user-filled; `.env.example` committed)
```
LLM_BASE_URL=http://localhost:8888/v1
LLM_API_KEY=
LLM_MODEL=
```
Empty `LLM_API_KEY` or `LLM_MODEL` → LLM disabled (rule-based mode, D5). Key never leaves the machine; never logged.

### 10.2 `config.json` (defaults; user-tunable via Settings page / `PUT /api/settings`)
```json
{
  "sources": {
    "fpl": {"enabled": true, "bootstrap_interval_min": 15, "live_players_interval_sec": 60},
    "espn": {"enabled": true, "news_interval_min": 30, "live_interval_sec": 30},
    "bbc": {"enabled": true, "interval_min": 30},
    "reddit": {"enabled": true, "interval_min": 30, "mode": "rss", "oauth_client_id": "", "oauth_client_secret": ""},
    "youtube": {
      "enabled": true, "interval_min": 60,
      "channels": [{"handle": "@PlanetFPL", "channel_id": "UC8043oOKTB4uP8Nq15Kz6bg"}],
      "transcript_keywords": ["weekender", "deadline stream", "cotc", "clash of the correspondents", "review"],
      "max_transcripts_per_poll": 3,
      "llm_truncate_chars": 12000
    }
  },
  "llm": {"enabled": true, "base_url": "http://localhost:8888/v1", "model": "", "timeout_sec": 60, "batch_chars": 8000},
  "optimizer": {
    "weights": {"ep": 0.7, "form": 0.15, "fixture": 0.15},
    "availability": {"doubt": 0.7, "chance_null": 1.0, "chance_100": 1.0, "chance_50": 0.65, "chance_0": 0.0},
    "signal": {"neg_per": -0.5, "neg_cap": -0.6, "pos_per": 0.1, "pos_cap": 0.2},
    "differential_lambda": 3.0,
    "solver": {"restarts": 20, "timebox_sec": 8, "seed": 42}
  },
  "group": {"fpl_entry_id": ""},
  "ui": {"theme": "dark"}
}
```
`llm.model` mirrors `.env` `LLM_MODEL` (env wins if set; Settings page writes `config.json`).

---

## 11. Test strategy

### 11.1 Backend (pytest, required — green at every milestone exit)
Layout: `backend/tests/` (files listed in §6.2). `conftest.py` provides: in-memory/temp-file SQLite via the real `db.py` init, a `client` (FastAPI `TestClient` with scheduler disabled), and fixture loaders.

**Fixtures** (`backend/tests/fixtures/`):
- `weekender_ep48.txt` — copied from `tmp-video-test\The Weekender ep. 48  Planet FPL 202627.txt` at M2 (T2.12). Original file stays untouched.
- `sample_bootstrap.json` — real `bootstrap-static` snapshot captured during M1 dev (trimmed: keep chips, 3 events, ~60 elements covering all 4 positions and several clubs).
- `sample_scoreboard.json` — real ESPN scoreboard snapshot (pre + in-play + post matches).
- `sample_bbc.xml`, `sample_reddit.xml` — real RSS/Atom snapshots (Reddit: include one image-only post + the pinned megathread).
- `sample_element_summary.json` — one player's fixtures+history.

**Golden test suites**:
| File | Must cover |
|---|---|
| `test_rules.py` | A valid squad/XI passes; each of the 16 error codes in §8.9 triggered individually and reported with the exact code; validator is the same function used for user input and solver output |
| `test_ep_model.py` | M1: EP = ep_next only; M2: `A(p)` gates (u/s/can_select=false excluded; d→0.7; chance map 1.0/1.0/0.65/0.0); `S(p)` stacking + caps (−0.6 / +0.2); weight blend 0.7/0.15/0.15; captain ×2, VC ×1 |
| `test_transfers.py` | Bank consumption; −4 = 4 × max(0, transfers − bank); half-increase sell rule (75→78 sells at 76; 75→77 sells at 76; fall 75→70 sells at 70); 20-transfer cap; wildcard/free-hit neutralize penalties |
| `test_solver.py` | On a synthetic 60-player universe (deterministic): output always passes the validator; budget ≤ 1000; ≤3/club; XI 1/3/1 minimums; deterministic with fixed seed; 3 profiles return distinct lineups (or labeled variants); timebox respected |
| `test_match_names.py` | Exact `web_name`; case-insensitive; known_name; typo within Levenshtein 2; ambiguous name returns ≥2 candidates; no-match returns empty |
| `test_extractors.py` | Rule extractor: keyword dictionary cases (injury/doubt/suspension/selection/rotation/return/transfer) → correct category+sentiment+confidence; official-news changes → conf 1.0 signals; LLM extractor (mocked HTTP): valid JSON parsed; invalid JSON → discarded + logged, no crash; name normalization on weekender fixture: "Calbertt Lewing" → Calvert-Lewin id (LLM path mocked with canned response; rule path must NOT false-positive on it) |
| `test_auto_bench.py` | GK sub; position-matched outfield sub; no sub available (0-min player stays); VC handoff when captain 0 min; both 0 min → no doubling; auto-sub keeps XI valid |
| `test_api.py` | Smoke: `/api/meta/season`, players search, lineups CRUD (valid + each 422 case), match-names, suggestions generate (against seeded DB), signals list, settings round-trip |

### 11.2 Frontend (v1: no framework — D16)
Manual E2E checklist lives in README §"Manual test checklist" (executed at each milestone exit in a real browser):
1. Dev mode: `start.ps1` → both ports up → UI loads at :5173, API proxied.
2. Paste-a-list: paste 15 names (include 1 typo) → confirmation table → save → validation panel green.
3. Invalid input: break each rule once → specific error message appears.
4. Generate suggestions → 3 cards, pitch views, diffs, apply checklist.
5. (M2) Refresh each source → items appear; signals table filters work; LLM-off mode produces official-news signals.
6. (M3) During a live match: LIVE badge, scores update ≤60 s without refresh, team strip points tick, bonus race visible, countdown accurate.
7. (M4) Production mode: `start.ps1 --prod` → single port 8000 serves UI + API.

---

## 12. Milestones & acceptance

| Phase | Scope | Exit criteria (from outline §10) |
|---|---|---|
| **M1 — Core** | FPL fetchers + schema + manual team entry (picker + paste) + rules validator + optimizer on `ep_next` + Suggestions page + static Dashboard + README | User enters a real squad → gets 2–3 rule-valid suggestions with diffs, transfer math, captain pick |
| **M2 — News layer** | BBC/ESPN/Reddit/YouTube fetchers + raw_items + LLM extractor + rule fallback + signals page + optimizer integration (A(p) + S(p)) | News refresh produces per-player signals; suggestions visibly change with signals; works with LLM off |
| **M3 — Real-time** | Live pollers (ESPN 30 s, FPL 60 s in windows) + SSE + live Dashboard (scores, live points, auto-bench sim, bonus race, countdown) | During a live match, Dashboard updates ≤60 s without manual refresh |
| **M4 — Polish** | Differential tuning, chip advice v2, saved test lineups, optional entry-ID rank card, weights UI, packaging, final README | All AC1–AC8 pass (Part 5, T4.9) |

Task-level detail: Parts 2–5. Season-rollover checklist: Part 5 (T4.10).

---

## 13. Open items for the builder (only these may involve the user)

1. **ASK (M2 start, T2.13)**: LLM model name for `http://localhost:8888/v1` (endpoint verified up + key-gated, V10). If the user has no key/model → confirm rule-based mode is the working config; do not block.
2. **ASK (M4, T4.8)**: whether to document/set up Reddit OAuth (free app) for reliable thread bodies. Default: RSS only.
3. **Verify once (M4, T4.8)**: current Yahoo Sports JSON endpoint (V11). Drop silently on failure.
4. **Re-verify at season start (T4.10)**: official rules page (transfers/chips/scoring), scoring-table appearance points, chip windows (app reads chips from API — only validator constants need human re-check).