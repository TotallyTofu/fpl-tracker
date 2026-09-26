# FPL Team Optimizer — Part 4: M3 Real-Time (live pollers, SSE, live dashboard)

> **Prereqs**: M2 complete. This part covers tasks **T3.1–T3.9**.
> **M3 exit criteria** (outline §10): during a live match, the Dashboard updates scores + the user's live points within 60 s without manual refresh (SSE), deadline countdown accurate, auto-bench simulation visible (AC6).
> **Design**: pollers write to `live_matches` / `live_player_points` and push events into the in-process SSE hub; the hub broadcasts to connected `EventSource` clients; `GET /api/live/state` serves the same data one-shot (and as the `hello` snapshot).

---

## T3.1 — Live-window detection (wire into scheduler)

**Goal**: a single source for "are we live?" that gates the fast pollers.

**Files**: `backend/app/season.py` (extend T1.4), `backend/app/scheduler.py` (real implementation now).

**Spec**:
- `live_window()` (T1.4) is computed from `fixtures` of the **current GW** (fixtures whose `event == current_gw.id` and kickoff within [now−2h, now+2h] — outline §5.2: "any match started within ±2 h").
- `GET /api/meta/season` now returns `live_mode` + `live_window` (T1.4 shape) — M1 already exposes the fields; M3 makes them drive behavior.
- Scheduler pattern: jobs are registered **once** at startup with their *slow* cadence; a "conductor" job (every 60 s) flips live-mode state: when entering a live window it (re)starts the fast jobs (ESPN 30 s, FPL-players 60 s) and when leaving, reschedules them back off (they become no-ops). Simpler equivalent: fast jobs always run but early-return when `not live_mode()` — **use this** (no dynamic rescheduling bugs).
- Overlap protection on every job: `coalesce=True, max_instances=1, misfire_grace_time=30`.

**Acceptance**: unit test: fixtures seeded around now → `live_mode` True in window, False 3 h after last kickoff; fast poller no-ops outside the window (verified by `poll_log` having no new rows when idle).

---

## T3.2 — ESPN live poller (30 s in live windows)

**Goal**: live scores into `live_matches` + `fixtures` (status/minutes/score), V4 field mapping.

**Files**: `backend/app/fetchers/espn.py` (extend), `backend/app/live/poller.py`.

**Field mapping** (from the verified V4 shape):
```
espn response.events[]
  └─ competitions[0]
       ├─ date            → kickoff (UTC)
       ├─ status.state    → 'pre' | 'in' | 'post'
       ├─ status.displayClock (e.g. "78'", "HT", "FT") → parse minute (int before ', else 0/45/90)
       ├─ status.type.detail → 'FT' | 'AET' | 'PEN' | ...
       └─ competitors[]   → for each: homeAway ('home'|'away'), score (str→int),
                            team.id (ESPN team id), team.displayName, team.abbreviation, team.form
```
**Match to FPL fixtures**: `fixtures` rows have `home_team`/`away_team` as **FPL team ids** — build the ESPN-id ↔ FPL-id map once per season: ESPN scoreboard teams have `team.id`; FPL `teams[]` (bootstrap) has `id` + `name`. Map by normalized name (strip "FC"/"United" suffix handling: use a small alias dict, e.g. `Tottenham Hotspur → Tottenham`, `Manchester United → Man Utd` — build the alias dict from FPL `teams[].short_name` ↔ ESPN `displayName` at first run and persist in `config.json` under `maps.espn_team_ids` (one-time interactive check is NOT needed: match on `abbreviation` first — FPL `teams[].short_name` (3 chars, e.g. TOT) equals ESPN `team.abbreviation` (3 chars) — use that, with name-fallback + logged warning on mismatch).
**Upsert**: `live_matches` keyed by `fixture_id`; also update `fixtures.status/minutes/score_home/score_away` when state ∈ (in, post). `post` → keep last state (don't re-poll finished matches every 30 s: skip fixtures with `status='finished'` until next GW).
**Events pushed**: `score_update` per changed match (diff against previous values).

**Acceptance**: `test_api.py`/unit with `sample_scoreboard.json` (pre + in-play + post): mapping produces correct `live_matches` rows; minute parsing ("78'" → 78, "HT" → 45, "FT" → 90); team map built from abbreviations; only changed matches emit `score_update`.

---

## T3.3 — FPL live player poller (60 s in live windows)

**Goal**: live points/minutes/BPS for the user's selected players into `live_player_points`.

**Files**: `backend/app/fetchers/fpl.py` (extend), `backend/app/live/poller.py`.

**Spec**:
- Target set: the 15 players of the lineup flagged `is_current` (fallback: first saved lineup; none → skip with `poll_log` note).
- For each: `GET /api/element-summary/{pid}/` → `history[]` entry where `event == current_gw.id` → `points`, `minutes`, `bps` (bps may be absent mid-match → NULL).
- 15 calls × 60 s cadence — well within D17 limits (sequential, 15 s timeout each; run in one job, ~10–20 s total).
- Upsert `live_player_points (player_id, event)`; push `player_points` event per changed row.
- **Price-change watch** (cheap add-on): from the same bootstrap 15-min job (T1.3), diff `now_cost` per player vs previous → `price_change` SSE events + `poll_log` info. (Outline §5.2 lists `price_change` as an SSE event.)

**Acceptance**: unit test with `sample_element_summary.json`: current-GW history row extracted correctly; upsert idempotent; only changed rows emit events.

---

## T3.4 — Live computation (totals, auto-bench sim, bonus race, countdown)

**Goal**: `live/compute.py` — the M3 brain. Pure functions over DB rows (unit-testable).

**Files**: `backend/app/live/compute.py`.

**Key functions**:
```python
def team_live_total(lineup: Lineup, live: dict[int, LivePointRow]) -> int:
    """Σ over XI: points(p) where points(p) = live_points(p) * (2 if p == effective_captain else 1)
    + (bench points only if Bench Boost is being played this GW — M3: ignore BB, note in README;
    BB simulation lands with M4 chip advice v2 if time permits).
    effective_captain: captain if live_minutes(captain) > 0 else (VC if live_minutes(VC) > 0 else None)
    (2026/27 VC handoff, §5.4 — VC gets ×2, not ×1.5, when he is the effective captain)."""

def auto_bench_simulation(xi: list[PlayerRow], bench: list[PlayerRow], live: dict[int, LivePointRow]) -> dict:
    """§5.3 simulation. bench ordered by bench_order. Returns {applied: [{out: pid, in: pid, reason}],
    final_xi: [pid...], notes: [str]}:
    1. GK: if starting GK has minutes == 0 and a benched GK has minutes > 0 → sub in that GK.
       (If benched GK also 0 min → no sub; note "GK slot stays empty-scoring".)
    2. Outfield: for each starting outfield player with minutes == 0 (process in XI order):
         candidate = first bench player (priority order) with minutes > 0, same element_type,
         not already used, whose entry keeps XI valid (position-matched sub always keeps
         1 GK/≥3 DEF/≥1 FWD — assert it).
         If found → sub; else note "no eligible sub for {name}".
    'Playing' = minutes > 0 OR yellow/red card received (live data: minutes > 0 covers the UI case;
    card-only appearances without minutes are rare — if FPL later exposes them in element-summary,
    extend: treat minutes==0 && (yellow_cards>0 || red_cards>0) as playing — keep a TODO marker)."""

def bonus_race(lineup: Lineup, live: dict[int, LivePointRow], match_players: dict[int, list[dict]]) -> list[dict]:
    """For each user player in a live/finished current-GW match:
    match_players[fixture_id] = all players in that match with live (points, bps) — built by the
    poller from the element-summary history of ALL players in the user's matches (cheap: 2 teams × ~22
    players per live match the user is involved in; poll at 3-min cadence, not 60 s).
    rank_by_bps = user player's rank by bps (NULL bps → rank by points+goals+assists proxy, flagged);
    rank_by_points = rank by live points;
    bonus_outcome = heuristic: rank 1 → 'likely 3' (if points gap ≥ 3: 'likely 3', else 'contested'),
    rank 2 → 'likely 2', rank 3 → 'likely 1', else 'unlikely'.
    (Display-only guidance — real bonus is assigned by FPL from final BPS; label the panel "estimates".)"""

def countdown_state(next_gw_deadline: str) -> dict:
    """{deadline_time, seconds_remaining} — exact from events table (D9)."""
```
**Events pushed**: `bonus_race` (recomputed on each poll when any user match is live), `deadline_countdown` (every 60 s in live mode / 5 min otherwise — the conductor job owns this).

**Acceptance**: `test_auto_bench.py` golden suite (PLAN.MD §11.1): GK sub; position-matched outfield sub; out-of-order bench priority respected; no eligible sub (note emitted); VC handoff (captain 0 min → VC ×2); both 0 min → no doubling; team_live_total exact on a crafted 15-man lineup; bonus_race ranks + outcomes on a crafted match.

---

## T3.5 — SSE endpoint + hub

**Goal**: `GET /api/live/stream` per PLAN.MD §9.

**Files**: `backend/app/live/sse.py`, `backend/app/api/live.py`.

**Spec**:
```python
class SSEHub:
    """Process-wide singleton. clients: dict[client_id, asyncio.Queue(maxsize=100)]
    async publish(event: str, payload: dict)   # non-blocking put_nowait; overflow → drop oldest (q.get_nowait)
    async stream() -> AsyncGenerator[str, None]  # FastAPI StreamingResponse:
        on connect: client_id = uuid; yield "event: hello\ndata: {live_state_snapshot}\n\n"
        loop: msg = await queue.get(); yield f"event: {msg.type}\ndata: {json.dumps(msg.payload)}\n\n"
        every 30 s idle: yield ": ping\n\n"
        on disconnect: remove client"""
```
- `live/state` snapshot = same builder as `GET /api/live/state` (single function `build_live_state()` shared — no drift between the two).
- `GET /api/live/state` → `build_live_state()` (works with zero connected SSE clients).
- Response headers: `Cache-Control: no-cache`, `X-Accel-Buffering: no`.
- Backpressure: as above (drop-oldest); a client can never stall pollers.

**Acceptance**: `test_api.py` SSE section (TestClient streaming): first frame is `hello` with valid snapshot shape; publish a `score_update` mid-stream → client receives it; keepalive comment appears after 30 s (test with injected clock / short keepalive for tests); disconnect cleans up the client registry.

---

## T3.6 — Dashboard live mode

**Goal**: the live Dashboard (AC6 UI) — scores, live points, bonus race, countdown, auto-bench visibility.

**Files**: `frontend/src/pages/Dashboard.tsx` (extend), `components/LiveScores.tsx`, `LiveTeamStrip.tsx`, `BonusRace.tsx`.

**Behavior spec**:
- **LIVE badge**: red pulsing when `live_mode` (from season poll / `hello`), grey "not live" otherwise.
- **LiveScores**: grid of current-GW matches: `TOT 2 – 1 AVL  78'` (minute from `displayClock`), FT matches dimmed; user's players' matches highlighted (border accent).
- **LiveTeamStrip**: the current lineup's 15, each card: name, pos, **live points** (big, ticking), minutes, "subbed" tag when auto-bench simulation moved them (tooltip: "auto-benched — no minutes"), captain badge shows "C×2" or (when handed off) "VC×2".
- **Team total**: big number `team_live_total` + "of projected {adjusted}" context.
- **BonusRace**: panel per user player in a live match: points, BPS (or "est."), rank-by-BPS chip (1st/2nd/3rd), outcome label ("likely 2", "contested"); panel footer: "estimates — final bonus is set by FPL from BPS".
- **Countdown**: `deadline_countdown` events drive it (no client timer drift): seconds → `Dd HH:MM:SS` local; red < 24 h, blinking < 1 h.
- **Data wiring**: `useSSE` (T3.7) updates local state on each event; `hello` re-syncs full state on (re)connect; no polling needed in live mode (keep the 60 s season poll for meta).

**Acceptance** (manual checklist item 6, executed during a real live match): LIVE badge on; scores update ≤60 s without refresh; team strip points tick; auto-bench tags appear when a starter stays on the bench; bonus race renders; countdown matches `deadline_time` exactly.

---

## T3.7 — Frontend SSE client

**Goal**: `hooks/useSSE.ts` — robust EventSource wrapper (D19).

**Files**: `frontend/src/hooks/useSSE.ts`.

**Spec**:
```ts
export function useSSE(handlers: Partial<Record<EventType, (payload: any) => void>>): {connected: boolean}
// - new EventSource('/api/live/stream'); onerror → browser auto-reconnects (native);
//   track 'connected' from onopen/onerror for the UI indicator (small green/red dot top bar).
// - on 'hello': replace live state wholesale (authoritative snapshot) then apply subsequent deltas.
// - per-event handlers dispatch to the dashboard's state reducer (single useReducer in Dashboard:
//   actions: HELLO, SCORE_UPDATE, PLAYER_POINTS, BONUS_RACE, DEADLINE, NEW_SIGNAL, PRICE_CHANGE).
// - NEW_SIGNAL: toast + update a "latest signals" mini-feed on the Dashboard (replaces the M1 placeholder panel).
// - PRICE_CHANGE: toast "X: £a.b → £c.d".
// - cleanup on unmount: es.close().
```
**Acceptance**: unit-ish manual test: kill the backend mid-session (Ctrl+C) → UI shows disconnected dot; restart → `hello` re-syncs state without a page reload; no duplicate state on reconnect (snapshot replace, not merge).

---

## T3.8 — Scheduler wiring (final)

**Goal**: all jobs registered with final cadences (outline §5.2 table) + GW-eve bump.

**Files**: `backend/app/scheduler.py` (complete).

**Job table** (all `coalesce=True, max_instances=1`):
| Job | Cadence | Notes |
|---|---|---|
| `fpl_bootstrap` | 15 min | T1.3 + official-news ingest (T2.9b) + price-change diff (T3.3) |
| `fpl_fixtures` | 15 min | element-summary fixtures builder (T1.3) |
| `espn_news` | 30 min | T2.3 |
| `espn_live` | 30 s | T3.2 — early-return when `not live_mode()` |
| `fpl_live_players` | 60 s | T3.3 — early-return when `not live_mode()` |
| `match_players_bps` | 3 min | T3.4 bonus-race inputs — early-return when `not live_mode()` |
| `bbc` | 30 min (15 min GW-eve) | T2.2 |
| `reddit` | 30 min (15 min GW-eve) | T2.4 |
| `youtube` | 1 h | T2.5 (RSS + transcript budget) |
| `signal_pipeline` | 30 min (15 min GW-eve) | T2.9 `process_pending_items` |
| `conductor` | 60 s | live-mode flip + `deadline_countdown` publish + `expire_stale()` signals (hourly tick) |
| `entry_rank` | 15 min | M4 feature — register now as no-op stub |

**GW-eve bump**: conductor checks `now > deadline − 48h` → sets a module-level `GW_EVE=True` flag that the interval-aware jobs read (they recompute their effective interval; APScheduler static intervals are kept — the bump is implemented as: when GW_EVE and the 30-min job hasn't run in 15 min, run it anyway (stale-check pattern) — simple, no rescheduling).

**Startup ordering**: lifespan → `init_db` → run `fpl_bootstrap` + `fpl_fixtures` **once synchronously** (await) before accepting traffic (UI should never show "no data" on a normal start) → start scheduler. First-run bootstrap may take ~30 s (20 element-summary calls) — acceptable; log progress.

**Acceptance**: unit test (frozen time): job table registered (names + intervals asserted); live jobs no-op outside window; GW-eve stale-check triggers early run; startup ordering (test: app startup with seeded HTTP mocks populates DB before first request).

---

## T3.9 — M3 tests + exit verification

**Goal**: M3 suite green; M3 exit criteria demonstrated on a real live match.

**Files**: `backend/tests/test_auto_bench.py`, `test_api.py` (SSE + live state), `test_scheduler.py` (new), `fixtures/sample_scoreboard.json` (real snapshot with in-play match).

**Exit run**:
1. `pytest backend/tests -q` → green.
2. **Live-match window** (if none happens to be live during dev: simulate — a `scripts/simulate_live.py` dev script that feeds `sample_scoreboard.json` + canned element-summary responses through the same poller code path; mark results "simulated" in the commit message):
   - Open Dashboard → LIVE badge, scores, team strip, bonus race, countdown all render.
   - Observe updates ≤60 s without refresh (real) or step through simulated polls (simulated).
   - Kill/restart backend → `hello` re-sync.
3. Git commit: `M3: real-time (live pollers, SSE, live dashboard, auto-bench sim)`.

**M3 is done when**: outline §10 M3 exit + AC6 demonstrably pass (real preferred, simulated acceptable if no live match overlaps dev time — note which in the commit).