# FPL Team Optimizer — Project Outline

> **Purpose of this document**: This is the single source of truth for a planning agent. Read it end-to-end, then produce a complete implementation plan (and build) for the **FPL Team Optimizer** — a local web app that pulls FPL data and football news automatically, accepts the user's current lineup manually, and returns 2–3 rule-valid suggested lineups projected to score the highest, in order to help the user perform better in their FPL group/mini-league.
>
> **Verification status**: Every endpoint, rule, and decision marked ✅ was verified live on **2026-09-19** (current season: **2026/27**, GW5 just finished, GW6 deadline 2026-10-10 03:00 UTC). Items marked ⚠️ must be re-verified during implementation.
>
> **Companion file**: `potential websites for FPL tracking.md` (same folder) is the user's original source inventory; this outline supersedes and extends it.

---

## 1. Project Overview & Goals

### 1.1 What we are building
A **local web application** (runs on the user's PC, opened in a browser) that:

1. **Automatically pulls** FPL data and football information from: the official FPL site/API, BBC Sport, ESPN, Reddit r/FantasyPL, and YouTube (Planet FPL channel, extensible list).
2. **Accepts the user's current team manually** (no FPL login — deliberate, see §2).
3. **Generates 2–3 suggested lineups** (squad + starting XI + captain/vice-captain + bench order) that follow the official FPL rules and are projected to give the **highest FPL score**, each with a different risk profile, a diff against the user's current team, transfer/penalty math, and chip advice.
4. **Updates in real time**: live match scores, live points for the user's players, bonus race, deadline countdown, and an auto-refreshing news/signal feed.

### 1.2 Core loop
```
┌────────────┐   ┌─────────────┐   ┌──────────────┐   ┌──────────────────┐
│ Fetchers   │ → │ Normalize   │ → │ Signal       │ → │ Optimizer        │
│ FPL/ESPN/  │   │ into SQLite │   │ extraction   │   │ (rules + EP      │
│ Reddit/BBC │   │             │   │ (LLM + rules)│   │ model + solver)  │
│ YouTube    │   └─────────────┘   └──────────────┘   └────────┬─────────┘
└────────────┘                                                  ▼
        ▲                                             ┌──────────────────┐
        │ (schedulers + on-demand)                    │ 2–3 suggestions  │
        │                                             │ + live dashboard │
        └─────────────────────────────────────────────┴──────────────────┘
```

### 1.3 Non-goals (v1)
- **No auto-apply / no writes to FPL.** There is no official write API and no login by design. The user copies suggestions into the official site manually (the app provides an apply checklist).
- No multi-user support, no accounts, no cloud deployment.
- Not for FPL Draft or FPL Challenge — **Classic FPL only**.
- No betting/odds integration.

### 1.4 Season context (as of 2026-09-19)
- Season **2026/27**, 38 gameweeks. GW1 started 2026-08-21.
- GW5 deadline passed 2026-09-18 17:30 UTC; **GW6 deadline: 2026-10-10 03:00 UTC** (full 38-GW deadline table in §3.7).
- The app must be season-agnostic: everything (events, deadlines, chips, fixtures) is read from the official API at runtime; the 2026/27 deadline table below is reference only.

---

## 2. User Requirements & Fixed Decisions

These decisions were made by the user and are **final** for v1:

| # | Decision | Value |
|---|----------|-------|
| D1 | App form | **Local web app** (browser UI on the user's PC) |
| D2 | Tech stack | **Python (FastAPI) backend + React (Vite) frontend**, SQLite storage |
| D3 | Hosting | **Local only** — no cloud, no server to manage |
| D4 | Team input | **Manual only.** The user enters their own lineup via the UI (picker and/or pasted list). **No FPL login, no session cookies, no team import** — explicitly for security, and so the user can load *different hypothetical lineups for testing* |
| D5 | News analysis | **LLM-powered with fallback.** LLM is the user's own OpenAI-compatible endpoint at `http://localhost:8888/v1` (user supplies API key; model name configurable). If no key is configured, a **rule-based keyword extractor** must still work |
| D6 | Real-time | **Required** — live scores, live player points, news refresh, deadline countdown (§5) |
| D7 | Sources | Official FPL site, BBC, r/FantasyPL, YouTube (Planet FPL), ESPN, Yahoo (optional) |
| D8 | Group context | User wants to "perform better in my group" → suggestions include a **differential/mini-league profile** and an optional public rank display (§5.4) |

### 2.1 Original user request (for context)
> "I want to build a program/UI to help me modify my Fantasy Premier League Team (FPL) to make me perform better in my group. I want the UI or program to automatically pull information from sources like sport news articles about FPL, subreddit about FPL like r/FantasyPL, or Youtube Channel dedicated to Fantasy Premier League… I want to be able to input my current team in and have the program give me 2 or 3 new suggested layout that it think would give me the highest score in FPL, following its rules (https://fantasy.premierleague.com/en/help/rules)."
>
> "The program should be updating realtime as well. Maybe other sports sites like ESPN or Yahoo sport?"

### 2.2 "Do I need an API or access?" — Answer
**No paid APIs, no FPL login, no keys are required for the core product.** All data sources are free:

| Source | Auth required | Cost |
|--------|--------------|------|
| Official FPL API | None (public JSON) | Free |
| ESPN JSON endpoints | None | Free |
| BBC Sport RSS | None | Free |
| Reddit RSS | None (official OAuth app optional, free) | Free |
| YouTube transcripts (`youtube-transcript-api`) | None | Free |
| YouTube channel video list (channel RSS feed) | None | Free |
| User's local LLM (`http://localhost:8888/v1`) | User's own key | User's existing setup |
| Yahoo Sports | None (endpoint unverified — optional) | Free |

The **only** external dependency is the user's local LLM endpoint, and the app must function fully without it (rule-based fallback).

---

## 3. FPL Rules — 2026/27 (the optimizer's constraint engine)

✅ Extracted verbatim from the official rules page (`https://fantasy.premierleague.com/en/help/rules`) on 2026-09-19. The optimizer must implement these exactly. ⚠️ Re-verify at each season start (rules can change between seasons — 2026/27 itself changed the transfer system).

### 3.1 Squad
- Squad = **exactly 15 players**: **2 Goalkeepers, 5 Defenders, 5 Midfielders, 3 Forwards**.
- Total squad value ≤ **£100.0m** (API unit: `now_cost` is in £0.1m, so budget = 1000 units).
- **Max 3 players from a single Premier League club** (squad-level rule).

### 3.2 Starting XI
- Pick **11 of your 15** by the GW deadline.
- **Any formation is allowed, provided that 1 goalkeeper, at least 3 defenders and at least 1 forward are selected at all times** ("at all times" includes after automatic substitutions).
- There is **no explicit minimum number of midfielders** in the official text (squad composition 2/5/5/3 plus the 11-slot limit imply ≥2 MID in practice).
- **Bench = the remaining 4 players**, in a user-set priority order. The official 2026/27 text does **not** mandate a minimum of one of each position on the bench — but the optimizer should *heuristic-wise* prefer balanced bench coverage (≥1 DEF, ≥1 MID, ≥1 FWD when affordable) and must always be able to keep the XI valid after auto-substitution.

### 3.3 Automatic substitutions (bench logic the app must simulate)
- If a starting **GK** doesn't play, he is replaced by the benched GK (if he played).
- If a starting **outfield player** doesn't play, they are replaced by the **highest-priority outfield substitute who played and doesn't break the formation rules** (e.g., with 3 defenders in the XI, a defender can only be replaced by another defender).
- "Playing" = making an appearance or receiving a yellow/red card.
- The live dashboard (§5) must simulate this in real time from live minutes data.

### 3.4 Captaincy (⚠️ rule change vs older seasons — implement exactly this)
- Nominated from the **starting XI only**: 1 captain, 1 vice-captain (distinct players).
- **Captain's score is doubled (×2).**
- **The vice-captain has NO multiplier in 2026/27** — the VC is a pure backup: if the captain plays **zero minutes**, the captaincy (×2) moves to the VC. If both play zero minutes, no player's score is doubled.
- (Older seasons used a 1.5× VC — do **not** implement that.)

### 3.5 Transfers (new system in 2026/27)
- Unlimited free transfers **before the first deadline**.
- After the first deadline: **1 free transfer per Gameweek**.
- Each **additional** transfer in the same GW deducts **4 points** from the total (deducted at the start of the next GW).
- Unused free transfers **roll over**; **maximum bank = 5**.
- Hard cap: **20 transfers in a single GW** (does not apply while playing a Wildcard or Free Hit).
- The app must ask for / store the user's **current free-transfer bank (1–5)** and compute, per suggestion: transfers needed, free transfers consumed, −4 penalty points.

### 3.6 Player prices
- Prices change during the season based on transfer-market popularity (not before season start).
- **If a player's price rises after purchase, on sale you keep only half of the increase**, rounded down to £0.1m (e.g., bought £7.5m, now £7.8m → sells for £7.6m). The optimizer must use this when computing budget deltas for players that have risen.
- The API provides `now_cost`, `cost_change_start`, `price_change_projections` (use for "likely to rise/fall" hints in suggestions).

### 3.7 Deadlines
- All changes (XI, transfers, captain, bench order) must be made **by the GW deadline** = **90 minutes before the first kickoff** of the GW. A deadline won't change within 24h of its scheduled time.
- 2026/27 deadline table (UK time, reference only — runtime reads `/api/bootstrap-static/` `events[].deadline_time`):

| GW | Deadline | GW | Deadline | GW | Deadline | GW | Deadline |
|----|----------|----|----------|----|----------|----|----------|
| 1 | Fri 21 Aug 10:30 | 10 | Sat 7 Nov 05:30 | 19 | Fri 1 Jan 10:30 | 28 | Wed 3 Mar 10:30 |
| 2 | Fri 28 Aug 10:30 | 11 | Sat 21 Nov 05:30 | 20 | Tue 5 Jan 10:00 | 29 | Sat 13 Mar 05:30 |
| 3 | Fri 4 Sep 10:30 | 12 | Sat 28 Nov 05:30 | 21 | Sat 16 Jan 05:30 | 30 | Sat 20 Mar 06:30 |
| 4 | Sat 12 Sep 05:30 | 13 | Wed 2 Dec 10:30 | 22 | Sat 23 Jan 05:30 | 31 | Sat 10 Apr 05:30 |
| 5 | Fri 18 Sep 10:30 | 14 | Sat 5 Dec 05:30 | 23 | Sat 30 Jan 05:30 | 32 | Sat 17 Apr 05:30 |
| 6 | Sat 10 Oct 03:00 | 15 | Sat 12 Dec 05:30 | 24 | Sat 6 Feb 05:30 | 33 | Sat 24 Apr 05:30 |
| 7 | Sat 17 Oct 03:00 | 16 | Sat 19 Dec 05:30 | 25 | Wed 10 Feb 10:30 | 34 | Sat 1 May 05:30 |
| 8 | Fri 23 Oct 10:30 | 17 | Sat 26 Dec 03:00 | 26 | Sat 20 Feb 05:30 | 35 | Sat 8 May 05:30 |
| 9 | Sat 31 Oct 04:00 | 18 | Tue 29 Dec 10:00 | 27 | Sat 27 Feb 05:30 | 36 | Sat 15 May 05:30 |
|    |          |    |          |    |          | 37 | Sun 23 May 05:30 |
|    |          |    |          |    |          | 38 | Sun 30 May 06:30 |

### 3.8 Chips (2026/27: two sets, split at the GW19 deadline)
- **Only one chip can be played in a single GW.**
- The API's `bootstrap-static` `chips[]` array confirms the windows for 2026/27: each chip exists in two versions — set 1 active events 1/2→19, set 2 active events 20→38.

| Chip | Effect | Availability 2026/27 | Cancellable? |
|------|--------|---------------------|--------------|
| **Wildcard** (×2) | All transfers in the GW (including those already made) are free | #1: after GW1 → GW19 deadline (Fri 1 Jan 10:30); #2: after GW19 → end of season | No, once played |
| **Free Hit** (×2) | Unlimited free transfers for one GW; squad reverts to pre-GW state at next deadline | #1: after GW1 → GW19 deadline; #2: after GW19 | No, once confirmed; **cannot be played in consecutive GWs** |
| **Bench Boost** (×2) | Points scored by bench players count in the total | Set 1: season start → GW19 deadline; Set 2: after | Yes, before deadline |
| **Triple Captain** (×2) | Captain's points ×3 instead of ×2 | Set 1: season start → GW19 deadline; Set 2: after | Yes, before deadline |

- Playing a Wildcard or Free Hit **retains saved free transfers** (bank is unchanged for the following GW).
- The app stores the user's **chips remaining** per type and validates chip advice against the active windows (e.g., never suggest Free Hit in GW20 if it was played in GW19).

### 3.9 Scoring (2026/27)

| Action | Points |
|--------|--------|
| Playing up to 60 minutes | 1 |
| Playing 60 minutes or more (excl. stoppage time) | 2 |
| Goal — Goalkeeper | 10 |
| Goal — Defender | 6 |
| Goal — Midfielder | 5 |
| Goal — Forward | 4 |
| Goal assist | 3 |
| Clean sheet — GK / DEF | 4 |
| Clean sheet — MID | 1 |
| Every 3 shot saves (GK) | 1 |
| DEF: ≥10 clearances + blocks + interceptions + tackles | 2 |
| MID/FWD: ≥12 clearances + blocks + interceptions + tackles + recoveries | 2 |
| Penalty save | 5 |
| Penalty miss | −2 |
| Bonus points (best players per match) | 1–3 |
| Every 2 goals conceded (GK/DEF) | −1 |
| Yellow card | −1 |
| Red card | −3 |
| Own goal | −2 |

### 3.10 BPS (Bonus Points System) table
BPS drives the 1–3 bonus points. Full table (verified):

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

BPS data is supplied by Opta and is final once the GW is marked final.

---

## 4. Data Sources & Access (all verified 2026-09-19 unless noted)

### 4.1 Official FPL API — `https://fantasy.premierleague.com/api/` ✅ (no auth, no key)
The backbone of the app. Long-stable unofficial API used by the official site itself.

| Endpoint | Status | Contents |
|----------|--------|----------|
| `GET /api/bootstrap-static/` | ✅ 200 | `chips[]` (chip windows), `events[]` (all 38 GWs: `deadline_time`, `is_current`/`is_next`, `most_captained`, `most_vice_captained`, `most_selected`, `most_transferred_in`, `top_element`, `chip_plays`), `teams[]`, `element_types[]`, `elements[]` — **every player** with the full field set below |
| `GET /api/element-summary/{player_id}/` | ✅ 200 | Per-player `fixtures[]` (with official **difficulty 1–5**, kickoff, home/away) + `history[]` per-GW stats — **`event_points` updates live during matches** (basis of live player points, §5) |
| `GET /api/event/{gw}/` | known | Per-GW player stats (results, top scorers) |
| `GET /api/fixtures/` | known | All season fixtures with status/minutes |
| `GET /api/game-settings/` | known | Budget, max transfers, scoring settings |
| `GET /api/entry/{entry_id}/` | ✅ 200 (public) | Public profile: overall/event points, overall rank + percentile, **every league membership with per-league rank** (basis of optional group-rank display, §5.4). No login needed |
| `GET /api/draft/team/{id}/` | 404 without session | **Not used** — team input is manual by design (D4) |

**Player field set from `bootstrap-static` `elements[]` (verified subset — all strings unless noted):**
- Identity: `id`, `first_name`, `second_name`, `web_name`, `known_name`, `element_type` (1=GK, 2=DEF, 3=MID, 4=FWD), `team`, `team_code`, `photo`
- Cost: `now_cost` (integer, £0.1m — 63 = £6.3m), `cost_change_start`, `cost_change_event`, `price_change_percent`, `price_change_hourly_rate`, `price_change_projections[]`
- Scoring: `total_points`, `points_per_game`, `form` (last 5 GWs), `event_points`, `minutes`, `goals_scored`, `assists`, `clean_sheets`, `goals_conceded`, `own_goals`, `penalties_saved`, `penalties_missed`, `yellow_cards`, `red_cards`, `saves`, `bonus`, `bps`, `starts`
- Expected (the gold for the optimizer): **`ep_next`**, **`ep_this`** (official expected points for next/current GW), `expected_goals`, `expected_assists`, `expected_goal_involvements` (+ `_per_90` variants)
- Ownership/flow: `selected_by_percent` (ownership %), `transfers_in`, `transfers_out`, `transfers_in_event`, `transfers_out_event`
- Availability: `status` (`a` available, `d` doubt, `i` injured, `s` suspended, `u` unavailable), `news` (official text, e.g. "Has joined Al Hilal permanently"), `news_added`, `chance_of_playing_this_round`, `chance_of_playing_next_round` (null/0/50/100), `can_transact`, `can_select`, `removed`
- Advanced: `influence`, `creativity`, `threat`, `ict_index` (+ per-90 + rank fields), `clearances_blocks_interceptions`, `recoveries`, `tackles`, `defensive_contribution`, `starts_per_90`, `clean_sheets_per_90`
- Set pieces: `penalties_order`/`penalties_text`, `corners_and_indirect_freekicks_order`, `direct_freekicks_order`

**Notes**
- The official `news` + `status` + `chance_of_playing` fields are a **free, authoritative baseline** for availability — community news sources refine, not replace, them.
- Treat the API as unofficial-but-stable: defensive parsing, log raw responses (`poll_log`), alert on schema drift.

### 4.2 ESPN ✅ (no key)
| Endpoint | Status | Use |
|----------|--------|-----|
| `GET https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard` | ✅ 200 (live in-play data verified) | **Live match scores** for the real-time dashboard |
| `GET https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/news` | ✅ 200 | Premier League **news article feed** (headlines, timestamps, links) |
| `GET .../eng.1/standings` | known pattern ⚠️ | League table (nice-to-have) |

Public documentation of these undocumented endpoints: `github.com/pseudo-r/Public-ESPN-API`.

### 4.3 BBC Sport
- `GET https://feeds.bbci.co.uk/sport/football/rss.xml` ✅ 200 — RSS with titles, timestamps, article URLs. Full article text requires fetching the article page (HTML scrape, light use).
- Use: general PL news (injuries, team news, selection articles).

### 4.4 Reddit r/FantasyPL
- `GET https://www.reddit.com/r/FantasyPL/hot.rss` ✅ 200 (also `new.rss`, `top.rss?t=week`) — thread titles/links/timestamps. The subreddit posts **weekly GW threads** (captain polls, predictions league, "how did they play"), which are valuable.
- `GET https://www.reddit.com/r/FantasyPL/hot.json` — ⚠️ bot-blocked (403/JS challenge) from datacenter IPs; may work from a residential IP with a descriptive static `User-Agent`.
- **Optional upgrade**: official Reddit OAuth app (free, 100 queries/min) via `reddit.com/prefs/apps` — most robust for thread bodies. Default for v1: **RSS only**; document the OAuth option in README.
- Thread bodies: `https://www.reddit.com/r/FantasyPL/comments/{id}.rss` (same access model).

### 4.5 YouTube (Planet FPL + extensible)
- **Video listing (keyless)**: YouTube channel RSS feed `https://www.youtube.com/feeds/videos.xml?channel_id={id}` — resolve the `@PlanetFPL` handle to a channel ID at implementation time (⚠️ verify). Planet FPL's "**The Weekender**" series (pre-GW team-setup content) is the target; new videos typically appear in the days before each GW.
- **Transcripts (keyless)**: Python package **`youtube-transcript-api`** (v1.2+) — fetches subtitles/auto-captions without an API key and handles YouTube's current PoToken/BotGuard requirement. Fallback: `yt-dlp`.
- **Optional**: YouTube Data API v3 (free key, 10,000 units/day) if the user wants comments or richer metadata — not required.
- Channel list is configurable in Settings (user can add other FPL channels).

### 4.6 Yahoo Sports (optional)
- ⚠️ The known JSON host (`sportsengine.api.yahoo.com`) **did not resolve from the development environment** on 2026-09-19. Treat Yahoo as an **optional secondary live-score source**: verify the current endpoint during implementation; if it fails, drop it silently — ESPN already covers live scores. Not a blocker.

### 4.7 LLM endpoint (user-provided, optional)
- OpenAI-compatible chat-completions API at **`http://localhost:8888/v1`** (configurable `base_url`).
- Config: `LLM_BASE_URL` (default above), `LLM_API_KEY` (user-provided), `LLM_MODEL` (⚠️ user to confirm model name; must be configurable since unknown).
- Used **only** for news→signal extraction (§7.3). App is fully functional with LLM disabled.

---

## 5. Real-Time Requirements

### 5.1 What "real-time" means in FPL
FPL decisions (transfers, XI, captain) only matter **before the GW deadline**. During a GW, the user can't change their team, so real-time serves: (a) live tracking of how the user's team is doing, (b) bonus-race and auto-bench awareness, (c) immediate news intake that affects the *next* deadline.

### 5.2 Live mode (during match windows)
- **Trigger**: any current-GW fixture with kickoff in `[now − 2h, now + 2h]` → app enters LIVE mode (badge in UI).
- **Pollers** (APScheduler, local):
  | What | Source | Interval |
  |------|--------|----------|
  | Match scores/status | ESPN scoreboard | 30 s |
  | User's 15 players' live points + minutes | FPL `element-summary` (per player) | 60 s |
  | Player prices + official news/status | FPL `bootstrap-static` | 15 min |
  | Article feeds (BBC/ESPN/Reddit RSS) | RSS | 30 min |
  | YouTube new videos | channel RSS | 1 h (or on demand) |
- **Computation**: user's live GW total = Σ live `event_points` (with captain ×2 applied live), **auto-bench simulation** applied as players' minutes come in (§3.3), **bonus race** indicator per user player (using live BPS if available, else proxy: points + goals/assists vs other players in the same match).
- **Push to UI**: Server-Sent Events (SSE) endpoint `/api/live/stream` with event types: `score_update`, `player_points`, `bonus_race`, `deadline_countdown`, `new_signal`, `price_change`. Frontend auto-refreshes the Dashboard in LIVE mode; no manual refresh needed.

### 5.3 Non-live mode
- Schedulers keep data fresh on the intervals above (lighter cadence: news 30 min, bootstrap 15 min).
- **On-demand**: "Refresh now" buttons per source on the News page and a global "Refresh all" on the Dashboard.
- **GW-eve behavior**: in the 48 h before a deadline, bump news cadence (15 min) and auto-run the signal pipeline on new items.

### 5.4 Group/mini-league context (optional, default OFF)
- Official league ranks only publish **after** a GW finishes. So "live group performance" = user's own live points (§5.2) vs the last published standings.
- Optional setting: user's **FPL entry ID** (the number in their public profile URL — public data, no login). The app then polls `GET /api/entry/{id}/` (public ✅) and displays: overall points, overall rank + percentile, and a table of every league the user is in with per-league rank (custom mini-leagues appear as `league_type: "x"` entries). Poll every 15 min; values update as GWs finalize.
- Because D4 is "no login", this is the only group-context feature; it is optional and clearly labeled.

---

## 6. Team Input Design (manual, per D4)

### 6.1 Inputs the user provides
1. **Squad**: 15 players — 2 GK / 5 DEF / 5 MID / 3 FWD, total ≤ £100.0m, ≤3 per club.
2. **Starting XI** (11 of the 15) + **formation is derived** (e.g., 4-3-3) — must satisfy 1 GK / ≥3 DEF / ≥1 FWD.
3. **Captain + vice-captain** (from the XI, distinct).
4. **Bench order** (4 players, priority order).
5. **Free-transfer bank**: 1–5 (integer).
6. **Chips remaining**: counts per type (WC/FH/BB/3XC), each 0–2; app validates against chip windows (§3.8).
7. (Optional) **FPL entry ID** for group-rank display (§5.4).

### 6.2 Entry methods (both required)
- **Interactive picker**: searchable player list (by name, club, position) fed by live `bootstrap-static` data — shows current price, form, points, ownership, news badge, and per-player signals. Click to add/swap; position tabs; budget bar; live validation panel (composition 2/5/5/3, ≤3 per club, £100m). XI view with drag/click assignment, captain/VC buttons, bench ordering.
- **Paste-a-list**: user pastes 15 player names (one per line, any casing). The app fuzzy-matches against the FPL player list (exact `web_name`/`known_name` first, then token/Levenshtein), shows a confirmation table of matches with ambiguity flags (e.g., "Grealish (MID, MCI)" vs other candidates), and loads the result. Position/cost come from the API, so the user only types names.
- **Saved lineups**: user can save multiple named lineups (e.g., "My real team", "Test A: all-Premier-differentials") and switch between them; suggestions always run against the selected lineup. This directly supports the user's testing use case (D4).

### 6.3 Validation rules (mirrors §3)
Block save/suggest with explicit messages on: wrong composition, budget over £100.0m, >3 per club, XI not 11, XI formation violation, captain/VC not in XI or same player, transfer bank out of 1–5, chips out of 0–2.

---

## 7. Optimization Engine

### 7.1 Problem statement
Given: current squad (15), transfer bank (1–5), chips, target GW (default: next), risk profile — produce **2–3 distinct complete lineups** (squad + XI + captain/VC + bench order) that are rule-valid (§3) and maximize projected FPL points, with an explanation and the transfer diff vs the current team.

### 7.2 Player expected-points model
For each candidate player `p` in the target GW:

```
EP(p) = ep_next(p)                                  # official FPL expected points (baseline)
       × A(p)                                        # availability multiplier
       × (1 + S(p))                                  # net news-signal adjustment
       blended with form & fixture factors (weights configurable)
```

- **Availability `A(p)`** (hard gates first):
  - `status = u` (unavailable) or `can_select = false` → **excluded** (except players already in the user's squad, who may be kept at A=0 if the user insists — flagged loudly).
  - `status = s` (suspended) → excluded (unless official `news` says ban served — rare; treat as excluded).
  - `status = d` (doubt) → A = 0.7 (configurable).
  - `chance_of_playing_next_round`: null → 1.0; 100 → 1.0; 50 → 0.65; 0 → 0.0 (configurable map).
- **News signal adjustment `S(p)`**: sum over active (non-expired) signals (§7.3): negative (injury/rotation/selection risk) → `−0.5 × confidence` each (stacking capped at −0.6); positive (confirmed/return/selection) → `+0.1 × confidence` each (capped +0.2). `S(p) ∈ [−0.6, +0.2]`.
- **Form & fixture blend** (configurable weights, defaults shown):
  - `EP_final = 0.7·EP + 0.15·form_adj + 0.15·fixture_adj` where `form_adj` scales `form` (last-5-GW points) into the EP range and `fixture_adj` scales official fixture difficulty (1–5) — easy fixture (1) gives a bonus, hard (5) a penalty, larger for DEF/GK (clean-sheet sensitive).
- **Captain multiplier**: when scoring a full lineup, captain contributes `2 × EP`, VC contributes `1 × EP` (2026/27 — no 1.5×, §3.4).
- All weights live in `config.json` (advanced settings page), so the user can tune.

### 7.3 Signal schema (produced by §7.4 extractors)
```
PlayerSignal {
  player_id: int
  category: injury | suspension | selection | rotation | return | transfer | other
  sentiment: negative | positive | neutral
  confidence: 0.0–1.0
  summary: str            # one-line takeaway, e.g. "Out 3–4 weeks with a hamstring injury"
  source: str             # "fpl-official" | "reddit:<thread_id>" | "bbc:<url>" | "espn:<id>" | "youtube:<video_id>"
  url: str
  published_at: datetime
  retrieved_at: datetime
  ttl_hours: int          # expiry (default 72h; official news: until replaced)
}
```
Only signals for players that exist in the current FPL player list are kept (name matching at extraction time).

### 7.4 Signal extraction pipeline
1. **Ingest**: fetchers store raw items (articles/threads/videos + transcripts) in `raw_items` with content hash (dedupe).
2. **Extract** (per new item):
   - **LLM mode** (when `LLM_API_KEY` set): send item text + current player name list → strict-JSON prompt → array of `PlayerSignal`. Enforce schema; discard + log on parse failure. Batch multiple items per call where size allows; cap tokens (truncate long transcripts, e.g., first ~12k chars of "The Weekender").
   - **Rule-based fallback** (always available): keyword/pattern dictionary (injury terms, "doubt", "to start", "benched", "return", "suspended", "fit to play", per-club rotation phrases) mapped to categories with fixed confidence (0.3–0.6). Official FPL `news`/`status` changes are ingested as signals with `source=fpl-official`, confidence 1.0.
3. **Store** in `signals`; the optimizer reads only non-expired signals.

### 7.5 Optimization profiles (the 2–3 suggested lineups)
| Profile | Objective | When it wins |
|---------|-----------|--------------|
| **1. Max EP** | Maximize Σ EP_final over squad + XI (with captain) | Default; pure expected points |
| **2. Differential (mini-league edge)** | Maximize Σ EP_final + λ·Σ(1 − ownership) over the XI, with a soft penalty on very high-ownership picks | Beating a small group: same points as everyone else but via players they don't have (λ configurable, default ≈ 3.0 pts per 100% ownership gap) |
| **3. Safe** | Maximize Σ EP_final × reliability, where reliability down-weights `d` status, negative signals, rotation risk, and rewards confirmed starters + clean-sheet chances | Low-variance weeks, must-not-lose weeks |

The app always returns all three (they naturally differ); if two converge to the same lineup, produce a variant with a different captain or a bench swap and label it.

### 7.6 Solver
- **Search space**: squad selection (15 from ~500 candidates) + XI assignment (11 of 15) + captain/VC + bench order.
- **Algorithm (default)**: multi-start hill climbing —
  1. Seed: greedy by EP_final/price respecting constraints.
  2. Moves: squad swap (in/out), XI↔bench move, captain/VC change, bench reorder.
  3. Accept if objective improves and the full constraint validator (§3) passes; simulated-annealing-style restarts (e.g., 20 restarts, 5–10 s timebox, deterministic seed option for reproducibility).
- **Constraint validator** (single source of truth, also used for user input §6.3): squad 2/5/5/3; cost ≤ 1000 units; ≤3 per club; XI = 11 with 1 GK/≥3 DEF/≥1 FWD; captain/VC in XI and distinct; transfers vs bank; budget delta computed with the half-increase sell rule (§3.6).
- **Optional exact mode**: integer linear program (PuLP) for squad selection — off by default (slower), available as a settings toggle.
- **Transfer diff per suggestion**: list of transfers in/out, cost delta, free transfers consumed, extra-transfer penalty (−4 × max(0, transfers − bank)), and **chip advice**: e.g., "Use Wildcard (4 changes > bank of 1)", "Free Hit not worth it (2 changes)", "3XC candidate: X (ep_next 9.2, GW has 2 home games)", "Bench Boost: your bench EP this GW is 8.1 — consider".

### 7.7 Edge cases
- **After the deadline**: no transfer suggestions; optimize XI/captain/bench only for the (still-in-progress) GW.
- **Players with price rises**: use half-increase sell value in budget math; show "likely to rise/fall" from `price_change_projections`.
- **Postponed matches / empty GWs**: fixture data drives it; flag "no matches" players (0 EP).
- **Squad contains an unavailable player**: keep allowed (it's the user's real team) but flag; suggestions prefer to transfer them out.
- **GW1 / fresh season**: no history — optimizer degrades gracefully to price + set-piece + team-form heuristics (form fields will be null/0).

---

## 8. UI Specification (React + Vite)

Five pages, single-page app, dark or light theme (keep simple):

### 8.1 Dashboard
- Header: current GW, **deadline countdown** (live), LIVE badge during match windows.
- **Your team strip**: 15 players with live points this GW (LIVE mode), team total, auto-bench status icons.
- **Live scores grid**: today's PL matches (ESPN), score, minute, status.
- **Bonus race**: user's players' live bonus standing in their matches.
- **Latest signals**: top 5 by confidence × recency, with source + link.
- Quick actions: **Get suggestions**, **Refresh all**.
- (Optional, §5.4) Group-rank card: overall rank/percentile + per-league table.

### 8.2 My Team
- Tabs: saved lineups ("My real team", test lineups…) + "New lineup".
- Squad table: name, pos, club, cost, form, total pts, ownership, official news badge, signal badges.
- **Pitch view**: XI in formation, captain (C) / VC badges, bench row with priority numbers.
- Edit mode: picker search, click-to-swap, drag optional.
- Meta panel: free-transfer bank (1–5), chips remaining (with window hints), budget bar.
- Validation panel: live rule checks (§6.3).

### 8.3 Suggestions
- Controls: target GW (default next), **Regenerate**, profile toggle (show all three by default).
- One card per suggestion: profile name, pitch view, **projected points** (baseline EP and news-adjusted, with/without captain), **diff table** (transfers in/out, cost delta, transfers used, −4 penalty), **chip advice**, expandable **rationale** (top 3 reasons, key signals used).
- **Apply checklist**: numbered manual steps for the official site (transfers first, then XI, captain, bench order, chip).

### 8.4 News & Signals
- **Signals tab**: per-player table (player, signal, category, sentiment, confidence, source, age, link); filter by player/club/category.
- **Sources tabs**: FPL official / BBC / ESPN / Reddit / YouTube — item lists with titles, timestamps, links, extracted takeaways; per-source "Refresh now".
- YouTube items show video title + link + extracted key takeaways (from transcript).

### 8.5 Settings
- **LLM**: enable toggle, `base_url` (default `http://localhost:8888/v1`), API key (stored in local `.env`), model name.
- **Sources**: per-source enable toggles; Reddit mode (RSS default; OAuth optional, document in README); YouTube channel list (default: Planet FPL).
- **Polling**: live-mode and normal-mode intervals (defaults from §5.2).
- **Optimizer (advanced)**: profile weights (λ differential, form/difficulty/availability weights), solver timebox, exact-ILP toggle.
- **Group context**: optional FPL entry ID.
- Data: "re-fetch all now", "clear signals", DB stats.

---

## 9. Architecture & Data Model

### 9.1 Stack
- **Backend**: Python 3.11+, **FastAPI**, APScheduler (AsyncIOScheduler), SQLite (via `sqlite3` or SQLAlchemy), `httpx` for HTTP, `feedparser` for RSS, `youtube-transcript-api`, `python-dotenv`. Optional: `PuLP` (exact solver), `openai` SDK (OpenAI-compatible client pointing at the user's local endpoint).
- **Frontend**: React 18 + Vite + TypeScript, React Router, plain CSS or a minimal UI kit (no heavy framework), `eventsource` for SSE.
- **Process model**: one backend process (API + schedulers + SSE) and the Vite dev server (or prebuilt static files served by FastAPI for one-command run). Provide a single launcher script (`start.ps1` / `start.sh` or `Makefile`) and a `README.md`.

### 9.2 Layout
```
fpl-tracker/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI app, CORS, static mount, SSE
│   │   ├── config.py            # .env + config.json
│   │   ├── db.py / models.py    # SQLite schema + access
│   │   ├── scheduler.py         # APScheduler jobs (§5.2)
│   │   ├── fetchers/
│   │   │   ├── fpl.py           # bootstrap-static, element-summary, event, fixtures, game-settings, entry
│   │   │   ├── espn.py          # scoreboard, news, standings
│   │   │   ├── reddit.py        # RSS (hot/new/top + thread bodies)
│   │   │   ├── bbc.py           # RSS + article fetch
│   │   │   └── youtube.py       # channel RSS listing + transcript fetch
│   │   ├── signals/
│   │   │   ├── schema.py        # PlayerSignal
│   │   │   ├── llm_extractor.py # OpenAI-compatible client (localhost:8888/v1)
│   │   │   └── rule_extractor.py# keyword fallback
│   │   ├── optimizer/
│   │   │   ├── rules.py         # constraint validator (§3, single source of truth)
│   │   │   ├── scoring.py       # EP model (§7.2)
│   │   │   └── solver.py        # hill climbing (+ optional PuLP)
│   │   └── api/
│   │       ├── lineups.py       # saved lineups CRUD
│   │       ├── suggestions.py   # generate/list
│   │       ├── news.py          # signals + raw items
│   │       ├── live.py          # SSE stream, live state
│   │       └── meta.py          # season state, refresh endpoints, settings
│   └── requirements.txt
├── frontend/                    # Vite + React (pages: Dashboard, MyTeam, Suggestions, News, Settings)
├── data/                        # fpl.db (SQLite), created at runtime
├── .env                         # LLM_BASE_URL, LLM_API_KEY, LLM_MODEL (user-filled)
├── config.json                  # weights, intervals, sources, channels
└── README.md
```

### 9.3 SQLite tables (core)
| Table | Key columns |
|-------|-------------|
| `players` | id (PK, FPL id), web_name, element_type, team, now_cost, form, total_points, ep_next, selected_by_percent, status, news, news_added, chance_of_playing_next_round, can_select, raw_json, fetched_at |
| `fixtures` | id, event, team_h, team_a, kickoff_time, difficulty, status, minutes, raw_json |
| `events` | id, name, deadline_time, is_current, is_next, finished, most_captained, most_transferred_in |
| `raw_items` | id (PK), source, external_id, title, url, published_at, content_hash (unique per source+hash), body (nullable), retrieved_at |
| `signals` | id, player_id, category, sentiment, confidence, summary, source, url, published_at, retrieved_at, expires_at |
| `lineups` | id, name, created_at, meta_json (transfer bank, chips), is_current |
| `lineup_players` | lineup_id, player_id, role (starter/bench), bench_order, is_captain, is_vice_captain |
| `suggestions` | id, lineup_id, profile, target_gw, created_at, result_json (full lineup + diff + advice + rationale) |
| `live_matches` | fixture_id, score_h, score_a, minute, status, updated_at |
| `live_player_points` | player_id, event, points, minutes, updated_at |
| `poll_log` | id, source, status, rows, error, started_at, finished_at |

### 9.4 Configuration
- `.env`: `LLM_BASE_URL=http://localhost:8888/v1`, `LLM_API_KEY=`, `LLM_MODEL=` (user fills; empty = rule-based mode).
- `config.json`: source toggles, YouTube channels, polling intervals, optimizer weights (§7.2/§7.5), optional `fpl_entry_id`.

---

## 10. Phases / Milestones

| Phase | Scope | Exit criteria |
|-------|-------|---------------|
| **M1 — Core (usable end-to-end)** | FPL fetchers + schema + manual team entry (picker + paste) + rules validator + optimizer on `ep_next` only (no news) + Suggestions page + static Dashboard + README | User enters a real squad → gets 2–3 rule-valid suggestions with diffs, transfer math, captain pick |
| **M2 — News layer** | BBC/ESPN/Reddit/YouTube fetchers + raw_items + LLM extractor (localhost:8888/v1) + rule fallback + signals page + optimizer integration (availability gates + S(p)) | News refresh produces per-player signals; suggestions visibly change with signals; works with LLM off |
| **M3 — Real-time** | Live pollers (ESPN 30 s, FPL 60 s during windows) + SSE + live Dashboard (scores, your live points, auto-bench sim, bonus race, deadline countdown) | During a live match, Dashboard updates ≤ 60 s without manual refresh |
| **M4 — Polish & differentiators** | Differential profile tuning, chip advice, saved test lineups, optional entry-ID rank card, weights UI, packaging (one-command start), final README | All acceptance criteria (§12) pass |

---

## 11. Open Questions / Verify at Implementation

1. ⚠️ **LLM model name** at `http://localhost:8888/v1` — ask the user; keep `LLM_MODEL` configurable (config + Settings page).
2. ⚠️ **Reddit**: RSS-only is the default. If the user wants thread bodies reliably, set up a free OAuth app (document in README) — do not block on this.
3. ⚠️ **Yahoo Sports**: try to verify the current JSON endpoint; if unresolvable, drop Yahoo (ESPN covers live scores).
4. ⚠️ **YouTube**: resolve `@PlanetFPL` → channel ID for the RSS feed; confirm transcript fetching works for recent "The Weekender" videos (auto-captions).
5. ⚠️ **FPL API stability**: pin the exact field usage behind defensive parsing; log raw JSON; add a schema-drift check that alerts in the UI (non-fatal).
6. ⚠️ **Season rollover**: at each new season, re-verify the rules page (transfers/chips changed in 2026/27 and could change again); the app reads chips/events/fixtures from the API so only the *validator constants* (squad composition, scoring table) need human re-check.
7. **Scoring-table changes**: the appearance points (1/2) and defensive-contribution thresholds were recently changed by the PL — re-verify at season start (§3.9).

---

## 12. Acceptance Criteria

1. **Runs locally**: with `python` + `node` installed, a single launcher script starts backend + frontend; UI reachable at `http://localhost:<port>`; no cloud, no external keys required.
2. **Team entry**: a valid 15-player squad (2/5/5/3, ≤£100.0m, ≤3 per club) can be entered via picker **and** via pasted name list; invalid input is blocked with clear, specific messages; multiple named test lineups can be saved and switched.
3. **Suggestions**: "Generate" returns 2–3 lineups, each passing the full rule validator; each shows projected points (baseline + news-adjusted), diff vs current team (transfers, cost delta, free transfers used, −4 penalty), captain/VC, bench order, chip advice, and rationale.
4. **Rule fidelity**: suggestions never include `status=u`/`s` or `can_select=false` players as picks; VC has no multiplier; transfer math matches §3.5/§3.6; chip advice respects §3.8 windows.
5. **News pipeline**: with LLM configured, new items from enabled sources produce per-player signals (JSON-validated, deduped, expiring); with LLM disabled, the rule-based fallback still produces signals from official FPL news/status plus keywords.
6. **Real-time**: during a live match, the Dashboard updates scores and the user's live points within 60 s via SSE; deadline countdown is accurate; auto-bench simulation is visible.
7. **Group context (optional)**: with an entry ID set, the app shows overall rank/percentile and per-league ranks from the public `/api/entry/{id}/` endpoint.
8. **Docs**: README covers setup, config, source list + ToS notes (light use, RSS), and how to add sources/channels.

---

## 13. Assumptions & Constraints

- **Single user**, English-language content, **Classic FPL only**, local network only.
- The official FPL API is **unofficial but long-stable** (used by the official site). Mitigation: defensive parsing, raw-JSON logging, non-fatal schema-drift alerts.
- **ToS posture**: light, polite polling (RSS ≥ 30 min, bootstrap 15 min, ESPN 30 s only during live windows, ≤ 20 concurrent requests); no scraping of paywalled/protected content; Reddit used via RSS (and optionally official OAuth); BBC via official RSS; YouTube via official channel RSS + public transcripts. No FPL account access at all.
- **No writes to FPL** by design (D4) — the app advises; the user acts on the official site.
- **LLM data boundary**: only fetched public content + player names are sent to the user's local LLM endpoint; no FPL credentials, no personal data.
- **Storage**: everything local in SQLite; no telemetry, no third-party services.
- **Time zone**: all deadlines shown in the user's local time; API times are UTC.

---

*End of outline. A planning agent should now be able to produce a full implementation plan (task breakdown, per-file design, test strategy) without making any of the decisions above — only the ⚠️ items in §11 remain open.*