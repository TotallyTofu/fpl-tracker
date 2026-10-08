# FIX.MD — Audit findings (rev 3, 2026-10-03)

> **STATUS: CLOSED. Nothing in this file is outstanding.** A1–A23 and the §13 doc drift were
> implemented and verified in rev 2; the four warnings that survived that round (W1–W4) plus eight
> nits were fixed in rev 3. See **§0.0 Post-audit close-out** for the evidence, the deliberate
> behaviour decisions, and the two items left as documented limitations. §1–§13 below are the
> original findings, kept verbatim as the audit record — read them as *history*, not as a to-do list.

Baseline at the time of writing (re-verified after rev 3):

| Check | Command | Result |
|---|---|---|
| Tests | `cd backend && ..\.venv\Scripts\python -m pytest tests -q` | **280 passed** |
| Types | `cd frontend && npx tsc --noEmit` | **exit 0** |
| Name resolution | `cd backend && ..\.venv\Scripts\python scripts\check_name_resolution.py` | **667 players: 0 wrong, 0 missing** |
| Live chip names | `SELECT DISTINCT name FROM chips` | `wildcard, freehit, bboost, triple_captain` |

---

## 0.0 Post-audit close-out (rev 3)

### What rev 2 shipped, and what the follow-up audit found

Rev 2 implemented every finding. A follow-up audit re-verified all of it against the live DB and
found **no blockers**, but four warnings and nine nits. Rev 3 fixes those. Everything below is
verified by the harness above plus the live probes described inline.

| ID | Severity | Issue | Resolution (rev 3) |
|----|----------|-------|--------------------|
| **W1** | Warning | **A23.1 was inert in production.** `_relevant_players` sorts the 40-player prompt cap by `selected_by_percent`, but `pipeline._player_list()` never selected that column — all 667 sort keys were `0.0`, so truncation kept an arbitrary 40. The unit test passed a *synthetic* list that included the field, so it was green while production wasn't. | `pipeline._player_list()` now selects `selected_by_percent` (105 distinct sort keys on live data) + `test_truncation_keeps_top_owned_through_pipeline_player_list`, an integration test that goes through the real helper |
| **W2** | Warning | **A19's gate was over-tight.** The common-word stoplist AND the mandatory same-sentence club anchor were ANDed, so plain headlines lost real signals: "Pope out for a month with a knee injury", "White a doubt for the weekend", "[Team News] Barnes benched" all resolved to nothing — even though `White`/`Pope`/`Barnes` are each owned by exactly one player in the pool. | Recalibrated (see below): **proper-noun use is required; the club anchor is required only when 2+ players share the word.** All four misses resolve; all five false positives stay out |
| **W3** | Warning | **`apply_suggestion` had no sandbox guard.** Applying a suggestion generated from a `kind='test'` lineup returned 200 and wrote a **team-level** `chip_plays_log` row (which drives the Free-Hit ban and shows in My Team's "Chips used" boxes). A suggestion whose lineup had been deleted did the same, while its bank update silently no-opped on `WHERE id = NULL`. | 422 for a test lineup, 409 for an orphaned suggestion, before any write + `test_apply_refuses_test_lineup`, `test_apply_refuses_orphaned_suggestion` |
| **W4** | Warning | **Legacy diff rows rendered "1 / undefined".** 57 of the 91 stored suggestions predate `bank_before`; `DiffTable` interpolated it straight into a template literal. | `diff.bank_before != null ? … : String(used)` |
| N1 | Nit | `meta.test_llm` lost its catch-all: a `200` with a non-JSON body (proxy HTML) escaped `test_llm_connection` as `ValueError` and 500'd the endpoint. | `except Exception` in the probe; "never raises" is now true |
| N2 | Nit | A bare float timeout applied to **connect** as well as read, so a blackholed host hung the Test button for `llm.timeout_sec` (300 s) with no feedback. | `httpx.Timeout(connect=10.0, read=probe, write=30.0, pool=10.0)` |
| N3 | Nit | `build_universe` used `t.short_name AS team_name`; every other producer (`lineups._load_lineup`, `signals.names`) uses the full club name. | `t.name`, matching the `LineupPlayer.team_name` contract |
| N6 | Nit | The `chip_advice_v1` exception fallback passed `season["next_gw"]` while the primary path passes `target_gw` — the Free-Hit ban would be judged for the wrong GW. | Both paths get `target_gw` |
| N7 | Nit | `ACCEPTANCE.md` cited a test name that does not exist. | Corrected to `test_rollover_with_lineups_succeeds` |
| N8 | Nit | `DiffTable` paired `transfers_in[i]` with `transfers_out[i]` for a "Δ vs out" figure — the solver's swaps are not positional, so it compared unrelated players. | Delta removed; each side already shows its own EP |
| N9 | Nit | The rev-2 report's "baseline 242 → +33" did not reconcile (measured baseline was 228). | Recorded here; no code impact |
| N10 | Coverage | §10.3's `test_bootstrap_survives_rollover` guard was never written — only the direct `check_season_rollover` test existed. | Added `test_bootstrap_survives_rollover_with_saved_lineup` (drives the real `fetch_bootstrap()` with a saved lineup + a changed marker) |

Deliberately **not** changed:

- **N5 — `pulp_available` is a dead payload field.** `exact_ilp` was correctly removed (A18 option 2),
  so nothing consumes the flag. It is kept for API-shape stability (removing it means touching
  `meta.py`, `types.ts` and `test_api.py` for no behavioural gain). Revisit if the ILP ever lands.
- **`DiffTable`'s club abbreviation.** `PitchView` renders `team_name.slice(0, 3).toUpperCase()`,
  which invents an abbreviation from the full club name (`"Aston Villa"` → `AST`, FPL's is `AVL`).
  Pre-existing and cosmetic; the club label is hidden in the compact (suggestion) view. Fixing it
  properly means shipping `short_name` alongside `name` — a schema change, out of scope here.

### W2 — the calibration, and why

The rev-2 rule was `common word AND no club in the sentence → drop`. Both halves are needed in
principle, but ANDing them cost more than it saved. The rule now is:

1. A single-token common word (`_COMMON_WORDS`) must occur as a **proper noun** in the raw text —
   `"Pope"`, `"POPE"` (all-caps headline) or `"McWhite"` count; `"the white shirt"` does not.
   (`_proper_noun()`, case-insensitive search + a per-occurrence `not islower()` test.)
2. A same-sentence **club / "Premier League" anchor** is required **only when 2+ players share the
   word** — that is where the anchor does real work, feeding `_disambiguate_full`. A uniquely-owned
   surname needs no club mention, because every source in this app is already PL/FPL-specific.
3. The whole-text-equals-token carve-out stays, so bare-name input (the self-consistency probe)
   still works.

Supporting fix — **A19.3 was not actually "falling out naturally"**: `by_surname` was built from
`_surname_tokens()`, which returns *every* token, so `known_name="Alex Tóth"` contributed `"alex"` as
a surname and "Alex is a doubt for the weekend" resolved to Tóth.A. `by_surname` now uses
`_surname_token()` (the **last** token only); the live tier shrank 667 → 625 entries and `"alex"` is
gone while `"toth"` remains.

Measured on the live pool (`data/fpl.db`, 2026-10-03):

```
MUST RESOLVE (were misses)                     ->  result
  "Pope out for a month with a knee injury."       442  ✅ (rule extractor: injury)
  "White a doubt for the weekend"                   10  ✅ (injury)
  "[Team News] Barnes benched"                     453  ✅ (selection)
  "POPE OUT FOR A MONTH"                           442  ✅
  "Arsenal sweat on White"                          10  ✅
  "Harvey Barnes injury"                           453  ✅

MUST NOT RESOLVE (were the A19 false positives)
  "the white shirt is clean for Arsenal"            []  ✅
  "Alex is a doubt for the weekend."                []  ✅
  "This is a long read about the title race."        []  ✅
  "James is expected to return."     (2 owners)     []  ✅
  "The king of the box, Ward, scores." (2 owners)   []  ✅
```

**Residual, accepted:** a *shared* common word with no anchor still yields no match, which is the
conservative side of the trade. If it bites, extend `_disambiguate_full` rather than re-tightening
the gate. And a **first-name-only** mention ("Ben White") resolves only when the stored `first_name`
matches — the live row says `Benjamin`, so "Ben White injury update" stays a miss until someone adds
a nickname alias (see §3 "Also needed: the two genuinely different spellings").

### Verified end-to-end after rev 3

- **A1 in the live DB** — `chips` now reads `wildcard, freehit, bboost, triple_captain`;
  `active_chip_windows()` returns `triple_captain … playable_next_gw: True`.
- **A2/A9** — same probe as before, **667 / 0 / 0**; all nine §3 acceptance cases pass; the
  paste-a-list matcher returns exact 1.0 for `Groß`, `Gross`, `Ødegaard`, `Odegaard`,
  `Calvert-Lewin`, `B.Fernandes`, `O'Royle`→`O'Riley`-class forms.
- **A10 end-to-end** — with a saved lineup and a changed season marker, `fetch_bootstrap()` now
  completes, wipes the lineup tables, normalises the chip rows and writes the new marker, and the
  call site swallows a rollover failure so it can never abort the refresh again. Locked in by
  `tests/test_rollover.py::test_bootstrap_survives_rollover_with_saved_lineup`.
- **A3/A17** — `GET` and `PUT /api/settings` return the same key set, `llm.api_key` is `""` in both,
  and `llm_status`/`pulp_available` survive a save.

---

## 0. Original audit record (rev 2, 2026-10-02) — historical

> Everything from here to §13 is the **rev-2 audit as written**, kept verbatim so the reasoning and
> evidence stay reproducible. The findings were all open at the time; they are closed now (see §0.0).
> Line numbers refer to the code **as it was on 2026-10-02** and have drifted.

**Status at the time:** every finding below was **reproduced against this checkout** (live DB +
temp-DB probes); **none of them are implemented yet.**

Baseline at that time:

| Check | Command (from `backend/`) | Result |
|---|---|---|
| Tests | `..\.venv\Scripts\python -m pytest tests -q` | **228 passed** |
| Types | `cd frontend && npx tsc --noEmit` | **exit 0** |

**A-IDs are stable across revisions.** A1–A9 are unchanged from rev 1 (with two corrections and extra
evidence added in place — see the `rev 2` notes inside §1/§3/§7). **A10–A23 are new in rev 2.**

Rev-2 fix order (executed): **§1 (A1) → §2 (A10) → §3 (A2) → §4 (A11–A14) → §5 (A3/A4) → §6
(A15–A19) → §7 (A5–A9) → §8 (A20–A23)**, then the open questions in §9. §12 lists behaviour that was
already correct. §13 lists documentation drift.

> **Note for a smaller model reading the historical sections.** Every finding has the same four
> parts: **Defect** (what the code did), **Evidence** (a command and its observed output at the
> time), **Impact** (what the user saw), **Fix** (the edit that was made). Do not re-apply any of it —
> §0.0 is the current state. Do not "improve" anything outside a finding's Fix block.

---

## 0. TL;DR

Ordered by the recommended fix order, not by ID.

| # | ID | Severity | Bug | User-visible effect | Primary location |
|---|----|----------|-----|---------------------|------------------|
| 1 | **A1** | **Blocker** | `chips` table stores the FPL API's raw `name` (`"3xc"`), but all app code looks up `"triple_captain"` | Triple Captain is **permanently unrecommendable** — always "no active triple-captain window" | `backend/app/db.py:609-633` |
| 2 | **A10** | **Blocker** | `check_season_rollover` deletes FK *parents* before *children* → `IntegrityError` | First bootstrap of a new season **aborts the whole FPL refresh** (no players/fixtures) whenever the user has a saved lineup | `backend/app/db.py:666-686` |
| 3 | **A2** | **Blocker** | `_flat()` normalises the *pattern* differently from the scanned text, and deletes non-ASCII letters | **83 of 667 players (12.4%) never resolve from their own name** → no signals, no LLM coverage | `backend/app/signals/names.py:184-192, 369` |
| 4 | **A11** | High | `GET /api/signals?active=false&team=X` builds `" WHERE  AND …"` | HTTP **500** on a documented filter combination | `backend/app/api/news.py:39` |
| 5 | **A12** | High | `has_signal=true` filters `s.expires_at IS NULL`, but the column is `NOT NULL` | `GET /api/players?has_signal=true` **always returns `[]`** | `backend/app/api/players.py:39-41` |
| 6 | **A13** | High | `POST /api/suggestions/generate` never reads `body["target_gw"]` | The **Target GW dropdown does nothing**; suggestions are always solved for `next_gw` | `backend/app/api/suggestions.py:44-52` |
| 7 | **A14** | High | `log_chip_play` decrements unconditionally; `unlog_chip_play` never restores | Ticking/unticking a chip checkbox **silently burns both sets** → "no wildcard sets remaining" forever | `backend/app/api/lineups.py:119-140` |
| 8 | **A3** | High | `GET /api/settings` returns the **LLM API key in cleartext** | Secret exposed to anything that can read the endpoint | `backend/app/api/meta.py:109-126` |
| 9 | **A4** | High | "Test connection" tests the **saved** config, not the edited draft | Editing key/model then testing gives a false failure (the one users hit first) | `backend/app/api/meta.py:145-169`, `frontend/src/pages/Settings.tsx:112-123` |
| 10 | **A15** | Medium-High | `bought_cost` is reset to `now_cost` on **every** lineup save; no UI to set it | The sell-on fee is **always 0** → `budget_after` overstates money; the solver proposes unaffordable transfers | `backend/app/api/lineups.py:274-287` |
| 11 | **A16** | Medium | My Team's `save()` omits `kind`; the pydantic default is `"current"` | Saving a **test lineup silently promotes it to a real one** (apply becomes enabled) | `backend/app/api/lineups.py:48,173-176`, `frontend/src/pages/MyTeam.tsx:438-452` |
| 12 | **A17** | Medium | `PUT /api/settings` returns `body.model_dump()` — no `llm_status`, no `pulp_available` | After every Save the UI shows LLM **"not configured"** and greys out the PuLP checkbox | `backend/app/api/meta.py:142`, `frontend/src/pages/Settings.tsx:99-102` |
| 13 | **A18** | Medium | `solver.exact_ilp` is declared, rendered in the UI and documented — but **read by no code** | Ticking "exact ILP (PuLP)" does nothing at all | `backend/app/config.py:152`, `frontend/src/pages/Settings.tsx:659-673` |
| 14 | **A19** | Medium | The "unambiguous surname" scan tier matches **ordinary English words** (26 of them) | "White will start" → a signal on player 10; "Pope is fit again" → 442 | `backend/app/signals/names.py:283-293, 376-383` |
| 15 | **A5** | Medium | `_chat_once` treats a `200` with **empty content** as success (answer is in `reasoning_content`) | Silent zero-signal results; extraction "succeeds" while doing nothing | `backend/app/signals/llm_extractor.py:244-285` |
| 16 | **A6** | Medium | `max_tokens=2048` is consumed by reasoning before the JSON answer | Truncated/empty JSON → salvage path or hard failure | `config.json` / `backend/app/config.py:104` |
| 17 | **A7** | Medium | Reddit `/new.rss` returns **429** while OAuth `/new.json` works | RSS-fallback mode silently degrades to hot-only | `backend/app/fetchers/reddit.py:336-341` |
| 18 | **A8** | Low | `MISSPELLINGS`/`ALIASES` contain dead entries for players who don't exist | Maintenance confusion; masks the A2 root cause | `backend/app/signals/names.py:25-77` |
| 19 | **A9** | Low→Medium | Wrong-player matches: `_HEADLINE_RE`, BBC `_ALIASES`, and **37/667 `web_name`s resolve to another player** | A signal can land on the wrong player | `names.py:358-393`, `bbc.py:39-109` |
| 20 | **A20** | Low-Medium | Both "Test connection" paths hard-code a 15 s/20 s timeout and `max_tokens: 8` | A healthy but slow/thinking model reports "Failed" even after A4/A5 are fixed | `meta.py:153,160`, `llm_extractor.py:448,452` |
| 21 | **A21** | Low | `optimizer.solver.restarts` is skipped for every restart > 0 unless a chip covers the GW | The `restarts` knob is dead in the everyday case; README's "20 restarts" is misleading | `backend/app/optimizer/solver.py:521-530` |
| 22 | **A22** | Low | `_bank_before` fallback returns 2 for a bank of 1; same arithmetic duplicated in the UI | "Free transfers used 1 / 2" on legacy diff rows | `transfers.py:348-361`, `DiffTable.tsx:75` |
| 23 | **A23** | Nit | `MAX_LIST_PLAYERS=40` truncates the filtered prompt silently; two wrong TS type shapes | Players beyond the 40th named can never get an LLM signal | `llm_extractor.py:102,115`, `types.ts:3-7,61-78` |

> **Historical table.** Severities and locations are as of rev 2 (2026-10-02). All 23 are fixed now.
> One row reads differently after the rev-3 recalibration: **A19**'s "White will start → player 10"
> is no longer a false positive to suppress — with only one `White` in the pool and a PL-only corpus,
> a *proper-noun* mention of a uniquely-owned surname is the intended match. The false positive the
> rule still blocks is lowercase prose ("the white shirt"). See §0.0 → W2.

### 0.1 Rev-2 commit sequence (historical — all landed)

Do them as separate commits so a regression is bisectable:

1. `fix(db): delete FK children before parents in check_season_rollover` (**A10**, + **A22**'s rollover
   half: also clear `chip_plays_log`).
2. `fix(db): normalise chip names at ingest` (**A1**) — needs no migration (`upsert` is keyed on `id`).
3. `fix(signals): one normaliser for patterns and text` (**A2**) — the biggest single change; land it
   alone with the new tests, then re-measure the §0.2 probe before touching A9.1.
4. `fix(api): signals/players filter bugs` (**A11**, **A12**).
5. `fix(api): honour target_gw` (**A13**).
6. `fix(api): make chip-play idempotent` (**A14**).
7. `fix(api): redact + draft-test the LLM settings` (**A3**, **A4**, **A17**, **A20**).
8. `fix(api): preserve bought_cost and lineup kind` (**A15**, **A16**).
9. `fix(ui): remove or implement exact_ilp` (**A18**).
10. `fix(signals): tighten the surname scan tier + the ambiguous-name map` (**A19**, **A9.1**
    class A).
11. Everything else (A5–A8, A21, A23) as small commits.

### 0.2 Verification harness (current — run this after any change)

```powershell
# from the repo root
cd backend
..\.venv\Scripts\python -m pytest tests -q          # must stay 280 passed
..\.venv\Scripts\python scripts\check_name_resolution.py   # must print "667 players: 0 wrong, 0 missing"
cd ..\frontend
npx tsc --noEmit                                    # must stay exit 0
```

Quick manual probes that need no network. The ones below are written for the **rev-2 bugs** — today
they are the *regression checks*: each line's comment is the state you should now see. Run them from
`backend/` with `..\.venv\Scripts\python -X utf8 -c "<code>"`; call
`sys.stdout.reconfigure(encoding="utf-8")` first on Windows.

```python
# A10 — rollover must NOT crash with a saved lineup
from app import db
db.set_meta("season", "2025/26")
db.check_season_rollover("2026/27")      # now returns True (was IntegrityError)

# A1 — chip names in the live DB
from app.db import query
print([r["name"] for r in query("SELECT DISTINCT name FROM chips")])
# now ['wildcard', 'freehit', 'bboost', 'triple_captain']  (was ['3xc', ...])

# A2 / A9 — how many players resolve from their own name
from app.db import query
from app.signals import names as N
idx = N.get_name_index()
rows = query("SELECT id, web_name FROM players WHERE removed = 0")
bad = [(r["id"], r["web_name"], sorted({h["player_id"] for h in N.resolve_candidates(r["web_name"], idx)}))
       for r in rows if r["id"] not in {h["player_id"] for h in N.resolve_candidates(r["web_name"], idx)}]
print(len(bad), "of", len(rows))          # now 0 of 667 (was 83 of 667)

# A12 — no signal can ever be "active" under the current filter
print(query("SELECT COUNT(*) AS n FROM signals WHERE expires_at IS NULL")[0]["n"])   # 0
```

---

## 1. A1 (BLOCKER) — `3xc` vs `triple_captain` chip-name mismatch

### The defect

The FPL API's `bootstrap-static` `chips[]` array uses the name **`"3xc"`** (verified live,
2026-10-02). `upsert_chips` stores `c.get("name")` **verbatim**:

`backend/app/db.py:609-633`
```python
name = c.get("name")          # <-- "3xc" from the API
rows.append((c.get("id"), name, c.get("number"), ...))
```

Every consumer, however, uses **`"triple_captain"`**:

| Location | Code |
|---|---|
| `backend/app/api/lineups.py:46,54,136` | `{"wildcard":2,"freehit":2,"bboost":2,"triple_captain":2}` |
| `backend/app/season.py:11` | `CHIPS = ("wildcard","freehit","bboost","triple_captain")` |
| `backend/app/optimizer/transfers.py:17` | `CHIP_NAMES = (..., "triple_captain")` |
| `backend/app/optimizer/transfers.py:205-214, 311-329` | `chips.get("triple_captain", 0)`, `window_open("triple_captain")` |

`active_chip_windows()` (`backend/app/season.py:60-80`) builds its list from the `chips` **table**,
so the key it emits is `"3xc"`. Every later `windows.get("triple_captain")` therefore returns
`None`, and `window_open("triple_captain")` is always `False`.

### Verified evidence

Live DB (`data/fpl.db`), re-verified 2026-10-02:

```
SELECT DISTINCT name FROM chips  ->  ['wildcard', 'freehit', 'bboost', '3xc']
--- active_chip_windows() as the app sees them ---
{'chip': '3xc',    'set': 1, 'playable_next_gw': True}
{'chip': 'bboost', 'set': 1, 'playable_next_gw': True}
{'chip': 'freehit','set': 1, 'playable_next_gw': True}
{'chip': 'wildcard','set': 1,'playable_next_gw': True}

--- window lookup for the names the app actually uses ---
wildcard        -> playable_next_gw=True
freehit         -> playable_next_gw=True
bboost          -> playable_next_gw=True
triple_captain  -> None   playable_next_gw=False      <-- ALWAYS

--- chip_advice_v2 output for a user holding a triple captain ---
triple_captain   skip      no active triple-captain window covers the next GW
```

The live `chips` table holds 8 rows: `wildcard×2, freehit×2, bboost×2, 3xc×2` (only `3xc`, never
`triple_captain`). `frontend/src/components/MetaPanel.tsx:55-64` shows the same failure as
"no window" on the Triple Captain stepper.

### Why the tests don't catch it

`backend/tests/conftest.py:70` seeds the fixture with the **app's** spelling, not the API's:

```python
(4, "triple_captain", 1, 6, 6, "3x_captain", 1),   # <-- "triple_captain", not "3xc"
```

So the whole suite is green while production is broken. **The fixture must be changed to the real
API spelling** as part of this fix (see §10.1).

### Impact

- Triple Captain can **never** be recommended (`recommendation` is hard-coded `"skip"`).
- `chip_covers_transfers` is unaffected (it only reads `wildcard`/`freehit`), so the bank and
  penalty maths stay correct — see §12.

### Fix — normalise once, at ingest (recommended)

Add a canonical mapping and store the canonical form, keeping the raw value in `raw_json`:

`backend/app/db.py` (new module-level constant, near `upsert_chips`)
```python
# FPL's bootstrap-static chips[] uses "3xc"; the app's UI/config/advice all use
# "triple_captain". Normalise on ingest so `chips.name` is always the app's
# spelling — `raw_json` keeps the upstream value for forensics.
CHIP_NAME_MAP = {"3xc": "triple_captain", "3x_captain": "triple_captain",
                 "free_hit": "freehit", "bench_boost": "bboost",
                 "wildcard": "wildcard", "freehit": "freehit", "bboost": "bboost"}
```

then in `upsert_chips`:
```python
raw_name = c.get("name")
if not name:
    continue
name = CHIP_NAME_MAP.get(raw_name, raw_name)
if name != raw_name:
    log.warning("chips[] name drift: %r → %r", raw_name, name)
```

`db.py` does **not** currently import `logging` (checked: lines 6-14 import `json`, `sqlite3`,
`datetime`, `pathlib`, `typing`, `.config`) — add `import logging` and
`log = logging.getLogger("fpl.db")` at the top.

**Also** make `active_chip_windows()` defensive so a future rename cannot silently disable a chip —
prefer to skip-and-warn rather than emit an unknown key. Optionally add a `KNOWN_CHIPS` assertion in
`season.py` that logs when the `chips` table yields a name outside `CHIP_NAMES`.

### Migration for the existing DB

`upsert` is keyed on `id`, so the next `POST /api/refresh/fpl` rewrites all 8 rows with the
canonical name — **no manual migration needed** provided `CHIP_NAME_MAP` is in place first. Verify
with:

```sql
SELECT DISTINCT name FROM chips;   -- must NOT contain '3xc'
```

### Acceptance
`SELECT DISTINCT name FROM chips` contains `triple_captain` and not `3xc`; with a triple captain in
hand and an open window, `chip_advice_v2` returns `recommendation != "skip"` for it.

---

## 2. A10 (BLOCKER) — season rollover aborts the FPL refresh with an FK violation

### The defect

`check_season_rollover` (`backend/app/db.py:666-686`) deletes **parents before children**:

```python
for table in (
    "players", "fixtures", "events", "chips", "signals",
    "live_matches", "live_player_points", "official_news_cache",
):
    conn.execute(f"DELETE FROM {table}")
conn.execute("DELETE FROM lineup_players")
conn.execute("DELETE FROM lineups")
conn.execute("DELETE FROM suggestions")
```

`get_conn()` turns foreign keys **on** (`db.py:449`), and the schema declares:

| Child column | References | Declared at |
|---|---|---|
| `lineup_players.player_id` | `players(id)` | `db.py:188` |
| `live_player_points.player_id` | `players(id)` | `db.py:271` |
| `official_news_cache.player_id` | `players(id)` | `db.py:250` |
| `live_matches.fixture_id` | `fixtures(id)` | `db.py:258` |

`players` is deleted **first**, so the very first `DELETE` fails as soon as one `lineup_players` row
exists — i.e. whenever the user has ever saved a lineup. `fixtures` is deleted second and is blocked
by `live_matches` for the same reason.

### Verified evidence

Reproduced with a temp DB seeded from `tests/conftest.py` plus one saved lineup
(`POST /api/lineups` with the standard 15-man body):

```
[rollover] lineup_players rows: 15
[rollover] raised: IntegrityError FOREIGN KEY constraint failed

# and with only a live_matches row, no lineup:
[rollover-live] raised: IntegrityError FOREIGN KEY constraint failed

# with neither:
[rollover-nolineup] wiped: True
```

The failure propagates through the real refresh path
(`backend/app/fetchers/fpl.py:116` → `fetch_bootstrap()`):

```
[bootstrap] raised: IntegrityError FOREIGN KEY constraint failed
[bootstrap] players left: 28      # nothing wiped — sqlite rolls back on close
[bootstrap] lineups left: 1
```

### Impact

- The **first `bootstrap-static` fetch after the season marker changes never completes**. No new
  players, no new fixtures, no new events, no new chips.
- `main.lifespan` (`backend/app/main.py:37-41`) swallows it as a warning, so the app starts with
  **last season's** data and the UI looks fine.
- The 15-minute scheduler job (`scheduler.py:20-26`) logs an exception on **every** poll — forever,
  because the marker never gets written (`set_meta("season", …)` is after the failing block).
- Nothing is partially wiped (SQLite rolls back on `close()`), so the damage is "stuck on stale
  data", not corruption.

### Why the tests don't catch it

`grep -rn "check_season_rollover" backend` returns only `db.py` (definition) and `fpl.py:116`
(call). **There is no test.** `ACCEPTANCE.md:125-156` claims a "Season rollover dry pass … 9/9 PASS"
including "old-season signals wiped by rollover logic" — that pass must have run against a DB with
no lineups, so the claim is not reproducible today.

### Fix

Delete children first, then parents, then the season marker; and clear the chip-play log (A22):

`backend/app/db.py:666-686`
```python
def check_season_rollover(season_marker: str) -> bool:
    """Wipe season-scoped tables if the season marker changed. Returns True if wiped.

    PRAGMA foreign_keys=ON → children MUST be deleted before their parents
    (lineup_players/live_matches/live_player_points/official_news_cache all
    reference players or fixtures).
    """
    stored = get_meta("season")
    if stored and stored == season_marker:
        return False
    conn = get_conn()
    try:
        for table in (
            # children first
            "lineup_players", "suggestions", "live_matches", "live_player_points",
            "official_news_cache", "signals", "raw_items",
            # season-scoped logs: a stale GW row would keep the Free-Hit ban alive
            # into the new season and UNIQUE(gw, chip) would block re-logging.
            "chip_plays_log",
            # parents
            "players", "fixtures", "events", "chips",
            # lineups last (lineup_players/suggestions already gone)
            "lineups",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM meta WHERE key = 'season'")
        conn.commit()
    finally:
        conn.close()
    set_meta("season", season_marker)
    return True
```

Also make the call site non-fatal so a future schema change can never take down the whole refresh
(`backend/app/fetchers/fpl.py:114-117`):

```python
events = data.get("events", [])
marker = _season_marker(events)
if marker != "unknown":
    try:
        if check_season_rollover(marker):
            log.warning("season rollover detected → wiped season-scoped tables (marker=%s)", marker)
    except Exception:
        log.exception("season rollover failed — continuing on last-good data")
```

**Decide deliberately about `raw_items` and `poll_log`.** Recommended: wipe `raw_items` (last
season's articles would otherwise stay in the News page forever and could be re-extracted) and keep
`poll_log` (it is an append-only health log; `recent_polls` is read with `LIMIT`). If you keep
`raw_items`, at least delete the ones older than ~14 days.

### Acceptance
With a saved lineup and `meta.season = '2025/26'`, `POST /api/refresh/fpl` returns 200, logs
`season rollover detected`, repopulates `players`, leaves `lineups`/`lineup_players` empty and
`chip_plays_log` empty, and `SELECT value FROM meta WHERE key='season'` is the new marker.

---

## 3. A2 (BLOCKER) — name matching silently drops real players

This is the **root cause** of "the news sources are fine but the signals are thin/incomplete".
It is one bug with two independent halves; both must be fixed.

> **rev 2 additions:** the measured blast radius (§3.4), the apostrophe class, and a **correction**
> to rev 1's fix instructions (§3.3 step 1 was wrong — see the boxed note).

### Half 1 — pattern and text are normalised differently

`resolve_candidates` (`backend/app/signals/names.py:358-393`) scans the **raw lowercased,
de-accented text**:

```python
for m in idx["regex"].finditer(_deaccent(text.lower())):
```

but the regex patterns were built from `_flat(name)` (`names.py:238-244`), and `_flat` **deletes**
every non-`[a-z ]` character (`names.py:188-192`):

```python
def _flat(s: str) -> str:
    s = _deaccent(s).lower()
    s = re.sub(r"[^a-z ]", "", s)      # <-- deletes '-', '.', "'", and every non-ASCII letter
    return re.sub(r"\s+", " ", s).strip()
```

Consequences, all verified:

```
'Calvert-Lewin'  ->  pattern 'calvertlewin'   scanned text 'calvert-lewin ...'   NO MATCH
'B.Fernandes'    ->  pattern 'bfernandes'     scanned text 'b.fernandes ...'     NO MATCH
"O'Riley"        ->  pattern 'oriley'         scanned text "o'riley ..."         NO MATCH
```

The hyphen/period/**apostrophe** is removed from the **pattern** but left in the **text**, so the
`\b...\b` anchor can never line up. (rev 1 listed `-` and `.` only; `'` is the same class and
accounts for `O'Riley`, `O'Brien`, `O'Shea`, `O'Nien`.)

### Half 2 — `_flat()` deletes non-ASCII letters instead of transliterating

`_deaccent` uses NFKD, which splits *accented* letters (é→e, ü→u) but **not** letters that are
distinct characters rather than accented forms. `_flat`'s `[^a-z ]` filter then **deletes** them:

| Player | `_flat()` result | Correct |
|---|---|---|
| `Groß` (`web_name`, id 124) | `gro` | `gross` |
| `Ødegaard` (id 15) | `degaard` | `odegaard` |
| `Nørgaard` (id 21) | `nrgaard` | `norgaard` |
| `F.Kadıoğlu` (id 113) | `fkadoglu` | `fkadioglu` |
| `Aït-Nouri` (id 392) | `aitnouri` | `aitnouri` (ok) but the text keeps `ï`→ NFKD `i` + combining, so this one is a hyphen problem |

`gro` is a 3-character stub that can never equal the text `gross`, so **Pascal Groß is
unresolvable** — and he is currently the single most-transferred-in player in the game.

### Real-world evidence (from this DB's own `raw_items`)

```
[reddit] Pascal Groß is the most-transferred in player ahead of GW6.   resolved: []
[reddit] Pascal Groß For FPL GW 6: Is Brighton's Value Worth The Risk? resolved: []
[reddit] Hürzeler & Groß are next...                                   resolved: []
[bbc]    How Gross is proving all-time bargain                         resolved: []
[reddit] [Alex Vollo] ...compared: Gross, Stach, Tavernier & Szoboszlai resolved: [Stach, Tavernier, Szoboszlai]  (Gross missing)
[youtube] Calvert-Lewin: Keep or Sell?                                 resolved: []
```

and the resulting signal counts for high-ownership players (live DB, 2026-10-02):

```
Haaland        id=411  signals=0
Saka           id= 12  signals=0
Groß           id=124  signals=0
Calvert-Lewin  id=346  signals=0
Ødegaard       id= 15  signals=0
```

Because `llm.player_list_mode` is `"filtered"` (`config.json`), an unresolved name is **also removed
from the LLM prompt** (`llm_extractor._relevant_players`, `llm_extractor.py:105-115`). So the LLM is
never even asked about Groß, and `fallback_full_list` only triggers when *no* name resolves — a
mixed article like the "Gross, Stach, Tavernier & Szoboszlai" one sends a filtered list that omits
Groß. The recall loss compounds.

### Measured blast radius (rev 2 — the number to quote in the commit message)

Self-consistency probe over all 667 live players: does each player's own `web_name` resolve to that
player? (script in §0.2)

```
TOTAL players: 667
web_name resolves to itself:      584   (87.6%)
web_name resolves to NOTHING:      46   (punctuation/hyphen/period/apostrophe or non-ASCII)
web_name resolves to WRONG player: 37   (see A9 — this is a precision bug, not just recall)
```

The 46 no-match players, split by cause (the groups are disjoint):

```
non-ASCII letter ONLY (3) — the letter is deleted, the name becomes a stub:
    Ødegaard(15) Nørgaard(21) Groß(124)

punctuation/apostrophe ONLY (37) — the separator is stripped from the pattern but
kept in the scanned text, so the \b anchor never lines up:
    G.Jesus(27) M.Bizot(29) Gannon-Doak(72) Kroupi.Jr(78) Ji-soo(92) O.Dango(95)
    O'Riley(126) B.Badiashile(146) N.Jackson(166) Kesler-Hayden(177) Mason-Clark(186)
    J.Rak-Sakyi(219) O'Brien(232) J.Cuenca(255) Kusi-Asare(272) Lo-Tutala(276) O'Shea(304)
    Calvert-Lewin(346) C.Jones(373) O'Reilly(387) N.Gonzalez(405) J.Ramsey(456)
    N.Williams(469) Hudson-Odoi(482) O'Nien(539) Jocelin.T(550) N.Angulo(551)
    Gourna-Douath(589) Williams-Barnett(601) Wan-Bissaka(611) Cozier-Duberry(624)
    Harwood-Bellis(630) Fernandez-Pardo(634) Norton-Cuffy(638) Maitland-Niles(649)
    J.Angulo(651) Nicoll-Jazuli(662)

BOTH (6) — punctuation AND a non-decomposable letter:
    A.García(38) F.Kadıoğlu(113) Aït-Nouri(392) I.Sangaré(488) M.Sangaré(565) Hjertø-Dahl(574)
```

`_norm_scan` (§3 Fix) handles all three groups: NFKD strips the accents that *can* be
decomposed (`é`, `ï`, `á`), `_FOLD` transliterates the ones that cannot (`ß`, `ø`, `ı`), and the
final `[^a-z ]` strip removes `.`/`-`/`'` from **both** sides.

Full-name resolution is slightly better but still broken: **35 of 667** full names do not resolve to
their own player (`Martin Ødegaard`, `Pascal Groß`, `Dominic Calvert-Lewin`, `Matt O'Riley`,
`Christian Nørgaard`, `Kaine Kesler-Hayden`, …).

### Fix — one shared normaliser, used for both sides

Replace `_flat`/`_deaccent` with a single `_norm_scan()` that **transliterates** rather than
deletes, and use it for the compiled patterns **and** the scanned text.

`backend/app/signals/names.py`
```python
# Letters that are distinct characters (not NFKD-decomposable accents) must be
# TRANSLITERATED, not deleted — `re.sub(r"[^a-z ]", "", ...)` turns "Groß" into
# the unusable 3-char stub "gro" and "Ødegaard" into "degaard".
_FOLD = {
    "ß": "ss", "æ": "ae", "œ": "oe", "ø": "o", "đ": "d", "ð": "d", "þ": "th",
    "ł": "l", "ı": "i", "ħ": "h", "ŋ": "n", "ĸ": "k", "ƒ": "f",
    "Æ": "AE", "Œ": "OE", "Ø": "O", "Đ": "D", "Ð": "D", "Þ": "TH",
    "Ł": "L", "İ": "I", "Ŋ": "N",
}


def _norm_scan(s: str) -> str:
    """The ONE normaliser for both index patterns and scanned text.

    NFKD + strip combining marks (é→e), fold non-decomposable letters
    (ß→ss, ø→o), lowercase, then drop every remaining non-[a-z ] char so
    hyphens/periods/apostrophes vanish from BOTH sides
    ('calvert-lewin' → 'calvertlewin', "o'riley" → 'oriley').
    """
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = "".join(_FOLD.get(c, c) for c in s)
    s = s.lower()
    s = re.sub(r"[^a-z ]", "", s)
    return re.sub(r"\s+", " ", s).strip()
```

Then:

1. Point `_flat` at `_norm_scan` (keep the name `_flat` as an alias so `name_index`, `resolve_name`
   and `scripts/validate_aliases.py` keep working).

   > **rev 2 CORRECTION.** rev 1 said this also fixes `match_names`. **It does not.**
   > `match_names` (`names.py:123-178`) uses `_norm` (`names.py:80-82`) and `_surname`
   > (`names.py:85-87`) — a *second, independent* normaliser that also does
   > `re.sub(r"[^a-z ]", "", s.lower())` with no de-accenting at all. `_flat` is never called on
   > that path. If you want the paste-a-list matcher to behave, **also** point `_norm` at
   > `_norm_scan` (and leave `_surname` alone — it calls `_norm`). Today it degrades to fuzzy
   > matching for these names: `_surname("Gross")="gross"` vs `_surname("Groß")="gro"` → Levenshtein
   > distance 2 → it *does* surface as a low-confidence candidate rather than an exact match.

2. In `resolve_candidates`, scan `_norm_scan(text)` — **not** `_deaccent(text.lower())`
   (`names.py:369`).
3. Because `_norm_scan` removes hyphens from the text too, `name_in_text`/`span` offsets no longer
   map onto the original string. **Fix the span reporting**: scan the normalised string for
   *matching*, but recover the original substring by locating the matched player's canonical name in
   the raw text (or return normalised spans and document them). The current code does
   `text[m.start():m.end()]` (`names.py:386`), which would silently mis-slice after this change —
   `name_in_text` is used by `_relevant_players` only for filtering, so returning the canonical
   `web_name` is the simplest correct answer.
4. `_deaccent` can stay (it is only used by `_flat` today), but grep for other callers before
   deleting it.

Validated result of this approach (offline probe, 8/10 targeted cases; the 2 misses are genuinely
different spellings that belong in `ALIASES`, see below):

```
Calvert-Lewin is fit again.            pattern 'calvertlewin'  MATCH
Odegaard returned to Arsenal training. pattern 'odegaard'      MATCH
Pascal Groß is the most-transferred in player  pattern 'gross'  MATCH
B.Fernandes scored for Man United.     pattern 'bfernandes'    MATCH
João Pedro starts up front.            pattern 'joao pedro'    MATCH
```

**Acceptance for the whole fix:** re-run the §0.2 self-consistency probe. The three counts must
become `667 / 0 / 0`. The 37 "wrong player" cases split into two classes that need two different
fixes (class P = punctuation fallthrough, which A2 alone removes; class A = ambiguous `web_name`
resolved to an arbitrary row, which needs the `full`-map change described in §7/A9.1). **Do not
expect A2 alone to reach 0/0** — measure after A2, then fix the class-A residue. If a name still
resolves to nothing, check whether it is a genuinely different spelling (`"Calvert Lewin"` with a
space) before adding an `ALIASES` entry.

### Also needed: the two genuinely different spellings

`"Calvert Lewin"` (space) and `"Bruno Fernandes"` vs `web_name "B.Fernandes"` are **not**
normalisation bugs — they are distinct strings. Note `known_name` already carries
`"Bruno Fernandes"`, `by_known` is populated (`names.py:248`) and joined into `patterns`
(`names.py:238-244`), so `bruno fernandes` **is** in the index and does match; the form that
regressed is the `B.Fernandes` *text* spelling. Keep `ALIASES` free of guesses and record each new
entry only after checking it against the live `players` table with
`backend/scripts/validate_aliases.py`.

### Acceptance
`resolve_candidates("Pascal Groß is the most-transferred in player", get_name_index())` returns
player 124; same for `"Calvert-Lewin"` (346), `"Ødegaard"` (15), `"B.Fernandes"` (426). Add a test
per case (§10.2).

---

## 4. A11–A14 (High) — four API bugs that make documented features no-ops or 500s

These are independent and small. Each has a reproduction, so fix them one at a time.

### A11 — `GET /api/signals?active=false&team=X` → HTTP 500

`backend/app/api/news.py:26-45` builds the WHERE clause by string concatenation:

```python
if player_id is not None:
    sql += " WHERE s.player_id = ?"
    params.append(player_id)
if team:
    sql += ("" if player_id is not None else " WHERE ") + " AND t.short_name = ?"
    params.append(team.upper())
```

With `player_id=None` and `team` set, `"" if player_id is not None else " WHERE "` yields
`" WHERE "`, which is then concatenated with `" AND t.short_name = ?"` →
`"… LEFT JOIN teams t ON t.id = p.team WHERE  AND t.short_name = ?"` — a SQLite syntax error.

**Verified** (temp DB, two signals, two teams):

```
GET /api/signals?active=false                -> 200, 2 rows
GET /api/signals?active=false&team=ALP       -> 500 Internal Server Error
GET /api/signals?active=false&team=BET&category=injury -> 500
GET /api/signals?active=true&team=ALP        -> 200   (the active path is fine)
```

The active path (`store.active_signals`, `backend/app/signals/store.py:82-100`) builds its clause
correctly, which is why this went unnoticed.

**Impact:** any client combining "active only = off" with a team filter gets a 500. The shipped UI
happens to send only one of the two, so users don't hit it — but it is a documented query
(`PLAN.MD §8.5`) and it is a trivially reproducible server error.

**Fix** — build a list and join it (mirror `store.active_signals`):

```python
where: list[str] = []
params: list = []
if player_id is not None:
    where.append("s.player_id = ?")
    params.append(player_id)
if team:
    where.append("t.short_name = ?")
    params.append(team.upper())
if category:
    where.append("s.category = ?")
    params.append(category)
if where:
    sql += " WHERE " + " AND ".join(where)
sql += " ORDER BY s.retrieved_at DESC LIMIT 500"
```

**Acceptance:** `test_signals_inactive_team_filter` — `GET /api/signals?active=false&team=ALP`
returns 200 and only that team's signals; same with `player_id` and `category` combined.

### A12 — `GET /api/players?has_signal=true` always returns `[]`

`backend/app/api/players.py:39-41`:

```python
if has_signal:
    sql += (" AND EXISTS (SELECT 1 FROM signals s WHERE s.player_id = p.id "
            "AND s.expires_at IS NULL)")
```

`signals.expires_at` is declared `TEXT NOT NULL` (`db.py:243`) and every writer sets it
(`store.save_signals`, `store.py:42,59,72`). **Verified:** the live DB has
`SELECT COUNT(*) FROM signals WHERE expires_at IS NULL` → `0`, and a temp DB with one active +
one expired signal returns `[]` for `has_signal=true`.

**Impact:** the "has an active signal" player filter is dead. Nothing in the shipped UI sends it
(`PlayerPicker.tsx` doesn't), so it is an API-only defect today.

**Fix:**

```python
from ..db import now_utc, query
...
if has_signal:
    sql += (" AND EXISTS (SELECT 1 FROM signals s WHERE s.player_id = p.id "
            "AND s.expires_at > ?)")
    params.append(now_utc())
```

(`api/players.py` currently imports only `query` — add `now_utc`.)

**Acceptance:** `test_players_has_signal_filter` — seed one active and one expired signal; assert
only the active player is returned.

### A13 — `POST /api/suggestions/generate` ignores `target_gw`

`backend/app/api/suggestions.py:43-52`:

```python
@router.post("/suggestions/generate")
async def generate(body: dict) -> dict:
    lid = body.get("lineup_id")
    lineup = query_one("SELECT * FROM lineups WHERE id = ?", (lid,)) if lid else None
    if not lineup:
        raise HTTPException(422, "lineup_id missing or not found")
    season = season_svc.current_season()
    target_gw = season["next_gw"] or season["current_gw"]     # <-- body["target_gw"] never read
```

`body` is a bare `dict`, so FastAPI does not even validate the key. The frontend *does* send it
(`frontend/src/pages/Suggestions.tsx:54-57` → `frontend/src/api.ts:120-124`) and renders a
**Target GW** dropdown offering `current_gw`, `next_gw` and `next_gw + 1`
(`Suggestions.tsx:69-74, 94-104`).

**Verified:** `POST /api/suggestions/generate {"lineup_id": <id>, "target_gw": 5}` → response
`target_gw: 6` (the fixture's `next_gw`), and `suggestions.target_gw` is stored as 6.

**Impact:** the user picks a gameweek and silently gets a different one. The generated squad,
`projected_points`, the stored `target_gw`, the diff and the chip advice all belong to `next_gw`.
The "Apply checklist" even prints "Deadline for GW {s.target_gw}" (`ApplyChecklist.tsx:36`), which
is then the wrong deadline.

**Fix (do the whole thing — a partial fix makes it worse):**

```python
    requested = body.get("target_gw")
    season = season_svc.current_season()
    target_gw = requested or season["next_gw"] or season["current_gw"]
    if not target_gw:
        raise HTTPException(503, "no season data yet — refresh FPL first")
    if requested is not None:
        total = season.get("events_total") or 38
        if not (1 <= int(requested) <= total):
            raise HTTPException(422, f"target_gw must be 1–{total}")
```

and **pass the requested GW into the chip advice**, because `chip_advice_v2` derives the
Free-Hit ban GW from its `next_gw` argument (`transfers.py:291`):

```python
advice = tmath.chip_advice_v2(diff, chips, s, season["current_gw"],
                              target_gw,            # <-- was season["next_gw"]
                              settings.config, signals)
```

`chip_covers_transfers(chips, target_gw)` already takes the target GW, so it is correct as written.

Then tighten the UI: `Suggestions.tsx:69-74` offers `current_gw` and `next_gw + 1` as well; once the
backend honours the value, decide whether solving for a *past* GW (`current_gw` when the deadline
has passed) should be allowed. Recommended: offer only `next_gw` and `next_gw + 1`, and label the
default "GW {next_gw} (next)".

**Acceptance:** `test_generate_honours_target_gw` — POST with `target_gw: 5`; assert
`response["target_gw"] == 5`, that the inserted `suggestions.target_gw` is 5, and that
`chip_advice_v2`'s Free-Hit ban is evaluated for GW 4.

### A14 — chip counters drift down (double-decrement, and un-ticking never restores)

`backend/app/api/lineups.py:119-140`:

```python
@router.post("/lineups/chip-play", status_code=201)
async def log_chip_play(body: ChipPlayIn) -> dict:
    execute(
        "INSERT OR IGNORE INTO chip_plays_log (lineup_id, gw, chip, played_at) VALUES (NULL,?,?,?)",
        (body.gw, body.chip, now_utc()),
    )
    current_ids = query("SELECT id FROM lineups WHERE kind = 'current'")
    for r in current_ids:
        _decrement_chips(r["id"], [body.chip])      # <-- unconditional
    return {"logged": {"gw": body.gw, "chip": body.chip}}


@router.delete("/lineups/chip-play")
async def unlog_chip_play(gw: int, chip: str) -> dict:
    ...
    execute("DELETE FROM chip_plays_log WHERE gw = ? AND chip = ?", (gw, chip))
    return {"deleted": bool(existed)}               # <-- never increments back
```

`chip_plays_log` has `UNIQUE(gw, chip)` (`db.py:285`), so a repeat POST is a silent no-op at the DB
level — but the decrement still runs. `execute()` returns `lastrowid`, which is `0` for an ignored
insert, and the caller ignores it either way.

**Verified** (temp DB; a `kind='current'` lineup with all four chips at 2):

```
[chip] log #1            -> freehit 2 → 1
[chip] unlog             -> deleted: True, freehit stays 1     (should be 2)
[chip] log #2            -> freehit 1 → 0                      (should be 1)
[chip] redundant re-log  -> freehit stays 0                    (a no-op that still decrements)
```

`frontend/src/pages/MyTeam.tsx:147-153` is exactly this toggle (`toggleChip` → `unlogChipPlay` or
`logChipPlay`), so a user who ticks the box, unticks it, and ticks it again has **silently lost both
sets**.

**Impact:** `lineups.chips[chip]` reaches 0 without the chip being played. Knock-on effects:

- `chip_covers_transfers` (`transfers.py:134-154`) returns `False` → the solver stops treating the
  GW as chip-covered, enforces the bank cap, and can no longer propose the free-transfer rebuild the
  Wildcard is for.
- `chip_advice_v2` reports "no wildcard sets remaining" / "no free-hit sets remaining" forever.
- Because the counter is user-editable in `MetaPanel`, the user's fix is to bump the stepper back
  up — which masks the bug.

**Fix — pick option 1.** Option 2 is a smaller diff but changes the meaning of the checkbox.

**Option 1 (recommended): make the log a pure log.** The checkbox's real job is the Free-Hit ban
(`freehit_played_in`, `transfers.py:120-131`) plus a "I played this" note. The in-hand counts are
already editable in the MetaPanel steppers, so the endpoint should not touch them:

```python
@router.post("/lineups/chip-play", status_code=201)
async def log_chip_play(body: ChipPlayIn) -> dict:
    """Record that a chip was played in a GW (drives the Free-Hit ban).

    Deliberately does NOT decrement lineups.chips: the checkbox is a toggle, so a
    decrement here double-counts (see FIX.MD A14). Edit the in-hand counts with
    the MetaPanel steppers; `apply_suggestion` is the only automatic decrement,
    and it is guarded by suggestions.applied_at.
    """
    existed = query_one("SELECT 1 AS x FROM chip_plays_log WHERE gw = ? AND chip = ?",
                        (body.gw, body.chip))
    if not existed:
        execute(
            "INSERT INTO chip_plays_log (lineup_id, gw, chip, played_at) VALUES (NULL,?,?,?)",
            (body.gw, body.chip, now_utc()),
        )
    return {"logged": {"gw": body.gw, "chip": body.chip}, "already": bool(existed)}
```

**Option 2 (if you keep the decrement):** make it conditional on the actual state transition, and
restore symmetrically on unlog:

```python
    existed = query_one("SELECT 1 AS x FROM chip_plays_log WHERE gw = ? AND chip = ?", (body.gw, body.chip))
    if existed:
        return {"logged": {...}, "already": True}
    execute("INSERT INTO chip_plays_log …")
    for r in query("SELECT id FROM lineups WHERE kind = 'current'"):
        _decrement_chips(r["id"], [body.chip])

# unlog:
    if existed:
        for r in query("SELECT id FROM lineups WHERE kind = 'current'"):
            _restore_chips(r["id"], [chip])       # +1, capped at 2 (FPL max per season)
```

Note the ambiguity you must document in either case: the app cannot tell "the user un-ticked the
box" from "the user played the chip for real and is correcting the record", so restoring is
inherently a guess. That is the argument for option 1.

**Tests to update:** `backend/tests/test_api.py:332-341`
(`test_chip_play_decrements_current_kind_only`) pins the decrement. Under option 1 it becomes
`test_chip_play_does_not_touch_chips`: assert both lineups keep `wildcard == 2` and that
`chip_plays_log` has the row. `test_chip_play_endpoints` (`:177-195`) is unaffected.
`test_apply_decrements_used_chip` (`:306-318`) must keep passing — `apply_suggestion` keeps its
decrement.

**Acceptance (new tests):** log → unlog → log leaves `lineups.chips` untouched; a redundant POST
returns `already: True` and changes nothing; `freehit_played_in(gw)` is `True` after a log and
`False` after the unlog.

---

## 5. A3 / A4 (High) — LLM settings: secret disclosure + a false-negative Test button

### A3 — the API key is served back in cleartext

`backend/app/api/meta.py:109-126`
```python
out = s.config.model_dump()          # includes llm.api_key in full
out["llm_status"] = {...}
return out
```

`GET /api/settings` therefore returns the raw key. **Verified against this checkout:**

```
[settings] GET keys: ['group', 'llm', 'llm_status', 'maps', 'optimizer', 'pulp_available', 'sources', 'ui']
[settings] GET llm.api_key: 'sk-unsloth-…'      # the real key is redacted here on purpose:
                                                # this file is committed, config.json is not
```

> **Do not paste real secrets into this file.** It is tracked for the public repo; `config.json`
> (which holds the LLM key and the Reddit OAuth secret) is gitignored and must stay that way.
> If you need to record evidence, record its *shape* (`'sk-unsloth-…'`, length, prefix), never the
> value — a revoked-but-real key in a public commit is still a leak.

This is a **local single-user app** (`config.json` is gitignored — verified: `git ls-files
config.json` fails, `.gitignore` lists `config.json`), so the risk is bounded. It is still a real
weakness: any local process, a browser extension, or a screen-share can read the key, and it
contradicts the module's own promise ("The API key is stored locally only …, never logged").

**Fix (pick one, in order of preference):**
1. Redact on read and treat an unchanged/empty value as "keep":
   ```python
   out["llm"]["api_key"] = ""            # never echo the secret
   out["llm_status"]["key_set"] = bool(s.llm_api_key)
   ```
   then in `put_settings`:
   ```python
   if not body.llm.api_key:                       # empty = "leave as-is"
       body.llm.api_key = load_config().llm.api_key
   ```
   The Settings UI already renders this field as `type="password"` with a `key_set` badge
   (`Settings.tsx:184-185, 206-213`), so the UI needs only a placeholder change
   ("leave blank to keep the saved key").
2. Or return a masked tail (`"sk-…c438"`) and keep the same write-back rule.

Option 1 is preferred: sending an empty string is unambiguous and cannot leak a prefix.

**Watch out:** if `.env` sets `LLM_API_KEY`, `s.llm_api_key` comes from the environment, not from
`config.json`. `put_settings` writes `config.json`, so "keep" must preserve the *config.json* value
(`load_config().llm.api_key`), not the env-merged one — otherwise a PUT would copy the env key into
the file. Also extend `llm_status.note` to say which source won.

### A4 — the Settings "Test connection" button tests the wrong thing

`frontend/src/pages/Settings.tsx:112-123` calls `api.testLlm()` → `POST /api/settings/test-llm`,
which does `load_settings()` (`meta.py:148`) — i.e. it reads **config.json from disk**. The edited
`draft` in React state is never sent. So this sequence is expected behaviour, not a bad key:

> edit the API key in the UI → press **Test connection** *before* **Save** → the previously saved key
> is the one tested → "Failed", even though the edited key is valid.

**Fix:** send the draft. Change the endpoint to accept an optional body and prefer it:
```python
@router.post("/settings/test-llm")
async def test_llm(body: dict | None = None) -> dict:
    s = load_settings()
    b = body or {}
    base_url = (b.get("base_url") or s.llm_base_url).strip()
    api_key = (b.get("api_key") or s.llm_api_key).strip()
    model = (b.get("model") or s.llm_model).strip()
    if not api_key or not model:
        return {"ok": False, "error": "LLM model or API key not set ..."}
    ...
```
and in the UI pass the draft: `api.testLlm({ base_url: draft.llm.base_url, api_key: draft.llm.api_key, model: draft.llm.model })`.
Depending on A3's outcome, the "empty key" sentinel must fall back to the saved key so a redacted
field still tests correctly. `frontend/src/api.ts:138` currently declares `testLlm: () => …` — give
it the optional body parameter (the file map in §11 already lists this).

**Also (small UX bug):** `save()` returns early when weights don't sum to 1.0
(`Settings.tsx:87-94`) — so an LLM-key edit silently refuses to save if the weights are off. Either
scope the weight check to weight fields or show it as a blocking message near the Save button
(currently the user sees only the message, and the key is not written).

**Current live status (verified 2026-10-02):** the endpoint is up at `http://localhost:8888/v1` and
the key in `config.json` now authenticates — `POST /api/settings/test-llm` → `{'ok': True, ...}` and
a real extraction returns a correct signal. So A3/A4 are about the *mechanism*, not a live outage.
See **A20** for the remaining false-negative source in the same button (hard-coded 15 s timeout and
`max_tokens: 8`).

---

## 6. A15–A19 (Medium) — state that silently goes wrong

### A15 — `bought_cost` is reset to `now_cost` on every save, so the sell-on fee is always 0

`backend/app/api/lineups.py:274-287`:

```python
def _save_players(lid: int, body: LineupIn) -> None:
    costs = {r["id"]: r["now_cost"] for r in query("SELECT id, now_cost FROM players")}
    rows = []
    for p in body.players:
        rows.append((lid, p.player_id, p.role, p.bench_order, ...,
                     1 if p.is_vice_captain else 0, costs.get(p.player_id)))   # <-- bought_cost
```

and `update_lineup` (`lineups.py:173-178`) deletes every row first:

```python
execute("DELETE FROM lineup_players WHERE lineup_id = ?", (lid,))
_save_players(lid, body)
```

`LineupPlayerIn` (`lineups.py:34-39`) has **no** `bought_cost` field, so no client can supply one.
The frontend sets `bought_cost: p.now_cost` when adding/swapping/pasting
(`MyTeam.tsx:194, 215, 242`).

**Verified against the live DB** — the user's real team (`lineups.id = 3`, `is_current = 1`):

```
lineup 3 players: 15 | bought_cost != now_cost: 0
```

i.e. every player's recorded purchase price equals their current price, so
`sell_value(bought, now) == now` for all 15 and the fee is 0. (The test copy `lineups.id = 4` has
one stale row — `Barry` bought at 56, now 57 — which is exactly the snapshot behaviour: the value is
whatever the price was at the last save.)

**Impact:**

- `compute_diff`'s `fee_sum` (`transfers.py:104`) is always 0, so
  `budget_after = 1000 − Σ new now_cost` — it **overstates** the money the user will actually have.
  Selling a risen player frees less than their current price in real FPL.
- `_TransferCtx.sell_fee` (`solver.py:565-568`) is all zeros, so the solver's affordability check
  (`solver.py:437-441`, `727-729`) is too permissive: it can propose a squad the user cannot afford
  after the sell-on fees are applied.
- `DiffTable.tsx:60-67` will never show the "(sell-on fees applied)" hint, and README:317-318
  describes a rule that never fires. FIX.MD §12's "`sell_value` half-increase rounding (all four
  spec cases)" is about the *function*, which is correct — the *data feeding it* is not.

**Fix (both halves):**

1. Preserve the stored value on update — read the previous rows **before** the DELETE:
   ```python
   def _save_players(lid: int, body: LineupIn) -> None:
       costs = {r["id"]: r["now_cost"] for r in query("SELECT id, now_cost FROM players")}
       prev = {r["player_id"]: r["bought_cost"] for r in query(
           "SELECT player_id, bought_cost FROM lineup_players WHERE lineup_id = ?", (lid,))}
       rows = []
       for p in body.players:
           paid = p.bought_cost if p.bought_cost is not None else prev.get(p.player_id)
           if paid is None:
               paid = costs.get(p.player_id)
           rows.append((lid, p.player_id, p.role, p.bench_order,
                        1 if p.is_captain else 0, 1 if p.is_vice_captain else 0, paid))
   ```
2. Add `bought_cost: int | None = Field(None, ge=0)` to `LineupPlayerIn` and surface it in
   `MyTeam.tsx` (an editable "paid" value per row, defaulting to the current price) so the user can
   enter their real purchase prices. Include it in the `save()`/`renameLineup()` payloads
   (`MyTeam.tsx:442-448, 495-501`) — otherwise the first save after this change silently re-derives
   every price again.
3. Minimum viable alternative if the UI work is out of scope: keep the current behaviour, but
   **stop advertising the fee** — remove the "(sell-on fees applied)" hint and the README bullet, and
   add a comment in `_save_players` saying `bought_cost` is a price snapshot, not a purchase price.

**Acceptance:** `test_update_preserves_bought_cost` — add a player at 45, `UPDATE players SET
now_cost = 50`, PUT the lineup, assert `bought_cost == 45` and `sell_value(45, 50) == 47`; then
`compute_diff(...)["budget_after"]` is 3 lower per such player than the naive `1000 − Σ now_cost`.

### A16 — saving a lineup from My Team silently promotes a test lineup to `current`

`backend/app/api/lineups.py:48`:
```python
kind: Literal["current", "test"] = "current"   # T4.3: test lineups are sandbox copies
```
`update_lineup` (`:173-176`) writes `kind` from the body, and `MyTeam.tsx:438-452`'s `save()` builds
the body **without** `kind`:

```typescript
const body = {
  name: draft.name,
  transfer_bank: draft.bank,
  chips: draft.chips,
  players: draft.players.map(...),
};                       // <-- no `kind`; the server default is "current"
```

The `Draft` type (`MyTeam.tsx:15-21`) doesn't carry `kind` either, so the value is lost on load.

**Verified:** create `kind="test"` → `PUT /api/lineups/{id}` with the body My Team sends →
response `kind: "current"`.

**Impact:** the sandbox stops being a sandbox.

- `Suggestions.tsx:127` computes `canApply={lineups.find(l => l.id === lineupId)?.kind !== "test"}`,
  so **Apply becomes enabled** for what the user believes is a test copy.
- `_decrement_chips` (`lineups.py:128-130`) starts touching it, and `apply_suggestion` writes
  team-level `chip_plays_log` rows (`suggestions.py:224-229`) — so sandbox exploration leaks into the
  real Free-Hit ban.
- The "⚗" marker disappears from the tab (`LineupTabs.tsx:62`) with no explanation.

**Fix (backend, so it cannot regress from any client):**

```python
class LineupIn(BaseModel):
    ...
    kind: Literal["current", "test"] | None = None   # None on update = keep the stored kind
```

```python
# create_lineup
(body.kind or "current", ...)

# update_lineup
row = query_one("SELECT kind FROM lineups WHERE id = ?", (lid,))
...
(body.kind or row["kind"], ...)
```

and in the frontend, carry `kind` through `Draft`/`toDraft`/`emptyDraft` and send it — belt and
braces, and it makes the intent visible.

**Acceptance:** `test_put_lineup_preserves_kind` — create `kind="test"`, PUT without `kind`, assert
the stored kind is still `"test"`; `test_put_lineup_can_promote_kind` — PUT with
`kind="current"` promotes it. `test_lineup_kind_default_and_duplicate` (`test_api.py:198-209`) must
keep passing (create still defaults to `"current"`, duplicate still produces `"test"`).

### A17 — `PUT /api/settings` omits `llm_status` and `pulp_available`, so the UI blanks them

`backend/app/api/meta.py:129-142` ends with `return body.model_dump()`, which has no
`llm_status`/`pulp_available`. `Settings.tsx:98-104` destructures both out of the PUT response:

```typescript
const updated = await api.putSettings(draft);
const { llm_status, pulp_available, ...rest } = updated;
setDraft(rest);
setStatus(llm_status);              // undefined
setPulpAvailable(!!pulp_available); // false
```

**Verified:** `GET /api/settings` keys = `[group, llm, llm_status, maps, optimizer, pulp_available,
sources, ui]`; `PUT /api/settings` keys = `[group, llm, maps, optimizer, sources, ui]`.

**Impact:** immediately after **every** Save the LLM row renders "not configured" with no base URL,
model or key badge (`Settings.tsx:180-186`), and the "exact ILP (PuLP)" checkbox greys out with the
"pip install pulp" hint (`Settings.tsx:659-673`) even when PuLP is installed. A user who just typed a
valid key sees "not configured" and concludes the save failed. This is the same false-negative class
as A4 and is likely to be reported as "saving the key doesn't work".

**Fix (preferred):** factor the payload so GET and PUT cannot diverge:

```python
def _settings_payload(s) -> dict:
    out = s.config.model_dump()
    out["llm"]["api_key"] = ""            # A3
    out["llm_status"] = {...}
    try:
        import pulp  # noqa: F401
        out["pulp_available"] = True
    except ImportError:
        out["pulp_available"] = False
    return out

@router.get("/settings")
async def get_settings() -> dict:
    return _settings_payload(load_settings())

@router.put("/settings")
async def put_settings(body: ConfigFile) -> dict:
    ...validate...
    save_config(body)
    return _settings_payload(load_settings())
```

Returning `_settings_payload(load_settings())` (rather than `body.model_dump()`) also makes the PUT
response show the *effective* env-merged values, which is what the status row is for.

**Alternative (frontend-only):** after a successful save, `await api.getSettings()` and use that.

**Acceptance:** `test_put_settings_response_matches_get_shape` — the two key sets are equal and
`llm_status`/`pulp_available` are present in both.

### A18 — `solver.exact_ilp` is a dead setting (documented, rendered, never read)

```
$ grep -rn "exact_ilp" backend/app frontend/src
backend/app/config.py:152:    exact_ilp: bool = False
frontend/src/types.ts:277:  exact_ilp: boolean;
frontend/src/pages/Settings.tsx:662:  checked={o.solver.exact_ilp}
frontend/src/pages/Settings.tsx:664:  onChange={(e) => upSolver({ exact_ilp: e.target.checked })}
```

No solver code reads it. `backend/app/api/meta.py:120-125` only *reports* `pulp_available`, and
`backend/app/optimizer/solver.py` contains no `pulp` reference at all. README:136-137 ("solver
(restarts, timebox, seed, exact-ILP — greyed out until PuLP is installed)") and README:195
("`exact_ilp: false` (needs `pip install pulp`)") promise behaviour that does not exist.
`PLAN.MD:274` also says `solver.py # multi-start hill climbing (+ optional PuLP)`.

**Impact:** a user can `pip install pulp`, tick the box, and the solver still hill-climbs. A silent
no-op control is worse than a missing one — it costs trust and it wastes debugging time. (In the
current environment `pulp` is not installed, so `pulp_available: False` and the box is greyed out —
but that hides the problem rather than fixing it.)

**Fix — pick one and be explicit:**
1. **Implement it** (largest scope): in `solve()`, when `cfg.optimizer.solver.exact_ilp`, build the
   ILP with PuLP (15 binary vars per player, budget/club/position/XI/captain constraints, objective
   from the same `value_fn`), use it as the seed for `_hill_climb`, and fall back to the greedy seed
   on `ImportError`. Keep the transfer cap as a constraint so A13's diff stays valid.
2. **Remove it** (recommended if the ILP is not wanted): delete `exact_ilp` from `SolverConfig`,
   drop the checkbox and the `pulp_available` plumbing (`meta.py:120-125`,
   `types.ts:277,341`, `Settings.tsx:31,48,99,102,659-673`), and fix README:136-137,195 and
   `PLAN.MD:274`.

**Acceptance:** either a test that proves `exact_ilp=True` changes the produced squad on a fixture
where the hill climb is known-suboptimal, or zero references to `exact_ilp` in the tree.

### A19 — the "unambiguous surname" scan tier matches ordinary English words

`backend/app/signals/names.py:283-293` promotes **every** surname token with exactly one owner into
the scan regex:

```python
surnames: dict[str, int] = {}
for tok, pids in by_surname.items():
    if len(pids) == 1:
        surnames[tok] = next(iter(pids))
```

In the live index that is **667 entries** — essentially every player — and `resolve_candidates`
(`names.py:376-383`) resolves any `\b(tok)\b` hit at score 0.8.

**Verified against the live pool** (26 of those tokens are ordinary English words):

```
surname-tier tokens that are common words (26):
  alex barnes burns cash cook dunk forster giles gray hall hill hume jordan keane lewis
  moore murphy pope reed scott shaw taylor thompson watson white wood
```

and the resulting false positives:

```
"Alex is a doubt for the weekend."        -> [(76,  'Alex')]      # Tóth.A — a FIRST name
"The manager said White will start."      -> [(10,  'White')]
"Pope is fit again."                      -> [(442, 'Pope')]
"Barnes picked up a knock in training."   -> [(453, 'Barnes')]
"James is expected to return."            -> [(142, 'James')]
```

(Note: `king` and `smith` are **not** in this tier — two players share each name, so the
`len(pids) == 1` guard excludes them. They are still matched, through a *different* defect —
see A9.1's class A. Do not use them as examples for this finding.)

**Impact:** this is a *precision* bug in the same family as A9, and it is worse than A9.2 because it
lands on the signal path. `resolve_candidates` feeds both `_relevant_players`
(`llm_extractor.py:105-115`) and `rule_extractor.extract_signals_rule`
(`rule_extractor.py:101-114`), which creates a signal for **every** id it returns. So "White will
start" produces a positive *selection* signal for player 10 — even when the sentence is about a
different White, a different club, or a different sport. Those signals then move projected points via
`S(p)` (`scoring.py:42-49`).

**Fix (any combination; 1+2 is the cheapest meaningful change):**

1. Exclude tokens that are common English words — either a small stop-list, or require the token to
   appear **capitalised** in the original (un-normalised) text:
   `re.search(rf"\b{re.escape(tok).title()}\b", text)`. Cheap, no dictionary needed, and "White"
   mid-sentence still counts.
2. Require a second anchor in the same sentence before a bare surname counts (a club name, or
   "Premier League") — mirrors the A9.2 fix for BBC relevance.
3. Don't add `_surname_tokens(web)` for players whose `web_name` is a single token equal to their
   *first* name (e.g. `Alex` for `Tóth.A`, id 76). Those are first names, not surnames, and they are
   the highest-noise class.
4. Keep `by_surname` for `match_names`/`resolve_name` (where the input is user-typed and the
   ambiguity guard in `match_names:170-175` already applies) and only tighten the **scan** tier.

**Acceptance:** `resolve_candidates("White will start", idx) == []`;
`resolve_candidates("Ben White will start for Arsenal", idx)` returns 10;
`resolve_candidates("Pascal Groß is the most-transferred in player", idx)` still returns 124 (A2's
fix must not be undone).

---

## 7. A5–A9 (Medium & Low) — LLM transport, Reddit RSS, and matcher precision

> **rev 2 additions inside A9:** the measured wrong-player list (37 of 667) and a pointer to A19 for
> the surname-tier half.

### A5 — empty `content` on a `200` is treated as success

The live model returns the answer in a **separate field** and an empty `content`:

```
POST /v1/chat/completions  →  HTTP 200
{"choices":[{"message":{"content":"",
                        "reasoning_content":"The user wants a single"},
             "finish_reason":"length"}]}
```

`_chat_once` (`llm_extractor.py:244-285`) only accumulates `delta.content` and never inspects
`reasoning_content`; `_chat_once_plain` raises on empty content (`llm_extractor.py:230-235`) but the
**streaming** path returns `""` happily. `_signals_from_content` then raises `RuntimeError` on the
unparseable empty string (`llm_extractor.py:323-324`), which the caller counts as a chunk failure —
so it *does* eventually surface. Two fixes worth making:

1. In the SSE loop, capture `delta.get("reasoning_content")` too and log a warning when content is
   empty but reasoning is not — this is the diagnostic that names the real problem.
2. Treat "empty content, non-empty reasoning" as a distinct, explicitly-logged failure so the
   Settings poll-log reads `empty content (model used its budget on reasoning)` rather than a bare
   `RuntimeError` from JSON parsing.

For `test_llm_connection` (`llm_extractor.py:433-464`) and `meta.test_llm`, a `200` with empty
content currently reports **`ok: True` with `reply: ''`** — misleading. Require non-empty content
for `ok: True`, or add `"empty_content": True` to the payload and surface it in the UI.

**Implementation note:** `_chat_once` returns `(content, finish)`. The cleanest change is to keep
that signature and detect the case in `extract_signals_llm` — but then the reasoning text is lost.
Better: accumulate `reasoning` alongside `parts` and, when `not parts and reasoning`, raise
`RuntimeError(f"LLM returned empty content (finish_reason={finish}); the model spent its budget on "
f"reasoning_content ({len(reasoning)} chars) — raise llm.max_tokens")`.

### A6 — `max_tokens=2048` is too small when the model reasons first

Same root cause as A5. `config.json` sets `max_tokens: 2048`; the probe above shows
`finish_reason: "length"` after 8 tokens on a trivial prompt because reasoning consumed the budget.
Extraction prompts are far longer, so this will bite on real items. The Settings page copy already
warns about this (`Settings.tsx:259-263`: "thinking models spend it on reasoning before answering") —
so the **fix is config + a guard**, not new code:
- raise `llm.max_tokens` (4096–8192 is cheap for a local model), and
- keep `_salvage_signals` (`llm_extractor.py:291-306`) as the backstop, adding a log line when
  `finish_reason == "length"` on the **successful** path too (today it is only mentioned inside the
  salvage/error messages).

**rev 2 note:** the live `config.json` still has `max_tokens: 2048` and
`optimizer.availability` without `chance_75`/`chance_25` (the code's `getattr` fallbacks cover it,
but the Settings UI never writes them back until the first save). Also update
`config.example.json` to match.

### A7 — `/new.rss` is 429 while OAuth `/new.json` works

```
Reddit hot.rss           HTTP 200
Reddit new.rss           HTTP 429     <-- rate-limited
Reddit OAuth token       HTTP 200  (token acquired, hot.json 200, 5 posts)
```

`refresh_reddit` handles this correctly — a failed `/new` fetch is caught and logged
("hot-only this poll", `reddit.py:336-341`) and does not lose the poll. But because `mode` is
`"oauth"` in `config.json`, the **RSS path is the untested fallback**, and in that mode the extra
recall from polling `/new` is silently lost. Worth noting in the README as a known limitation of
RSS mode rather than a code change; if RSS mode matters, add `/top.rss?t=day` as a second fallback.
No fix required if OAuth stays the default — **but pin OAuth as the documented default**, and note
that README:204 already advertises `top.rss?t=week` which is **not implemented** (see §13).

### A8 — dead `MISSPELLINGS` / `ALIASES` entries

Verified against the live pool:

```
MISSPELLINGS: 2 dead of 31   'leads united'->'Leeds United', 'leedz'->'Leeds United'
ALIASES:      2 dead of  9   'mo salah'->'Salah', 'mohamed salah'->'Salah'
```

`Salah` and `Leeds United` genuinely do not exist in this 2026/27 pool (confirmed by query), so the
comments are accurate and the entries are inert. **The problem is that these entries look like the
mechanism that would have caught Groß/Ødegaard, which misdirects a maintainer away from A2.** After
fixing A2, either delete the dead entries or move them to a clearly-labelled "dormant — target not
in this season's pool" block, and add a startup/dev-check assertion that logs dead alias targets
(`validate_aliases.py` already has the right shape for this).

**rev 2 note:** `name_index` (`names.py:266-282`) silently drops an alias whose target is not in
`full` — that is why they are inert rather than harmful. A one-line `log.debug` there would have made
this obvious.

### A9 — wrong-player matches (precision, not recall)

Three distinct hazards. The first is now measured.

**A9.1 — a player's own `web_name` resolves to a different player (37 of 667).** Full list from the
§0.2 probe:

```
J.Timber(5)->644      Lewis-Skelly(7)->395   J.Araujo(65)->579    Lewis-Potter(86)->395
M.Sarr(150)->208      Wilson(172)->108       Dasilva(176)->103    Thomas-Asante(194)->173
Henderson(198)->101   Drakes-Thomas(221)->173 Dewsbury-Hall(236)->449  Wilson(260)->108
King(268)->228        Hughes(278)->212       Kamara(293)->47      Palmer(301)->154
Johnson(307)->209     James(343)->142        Gomez(359)->127      Munoz(377)->201
Phillips(408)->273    Martinez(419)->28      B.Fernandes(426)->525 J.Fletcher(434)->438
A.Murphy(451)->457    L.Miley(459)->659      O.Richards(476)->202 Gibbs-White(480)->10
Phillips(507)->273    Davies(508)->355       P.M.Sarr(518)->208   Patterson(530)->234
Sanchez(573)->140     Palacios(619)->570     Thomas(642)->173     Fofana(646)->145
Wright(667)->193
```

There are **two independent root causes** in this list. Split them before you fix them.

**Class P — punctuation fallthrough (~15 cases).** `_flat("B.Fernandes") == "bfernandes"` cannot
match the text `b.fernandes`, so the regex falls through to the shorter `fernandes` token, which
belongs to player 525. Same for `J.Timber`→644, `Lewis-Skelly`→395, `J.Araujo`→579,
`Lewis-Potter`→395, `M.Sarr`→208, `P.M.Sarr`→208, `Thomas-Asante`→173, `Drakes-Thomas`→173,
`Dewsbury-Hall`→449, `J.Fletcher`→438, `A.Murphy`→457, `L.Miley`→659, `O.Richards`→202,
`Gibbs-White`→10. **A2's `_norm_scan` fix removes the fallthrough** (the period/hyphen disappears
from the text too, so `bfernandes` matches), and the `\b` alternation already tries the longest
pattern first (`names.py:291-293` sorts by `len` descending). **Land A2 first, then re-run the
probe** — most of this class disappears on its own.

**Class A — ambiguous `web_name` resolved to an arbitrary first row (~22 cases).**
`names.py:221-244` builds `full` as a single-valued dict with `setdefault`:

```python
full: dict[str, int] = {}  # flat name → id (longest wins)      # <-- the comment is WRONG
...
full.setdefault(f, pid)                                          # first row wins, not longest
```

The live pool has **20 `web_name`s shared by 2+ players** (`by_web` has 645 keys, 20 ambiguous):

```
dasilva davies fofana gomez henderson hughes james johnson kamara king
martinez munoz palacios palmer patterson phillips sanchez thomas wilson wright
```

All 20 are still reachable through `full`, and `resolve_candidates` checks `full` **before** the
surname tier (`names.py:373-375`), so it returns whichever player the unordered
`SELECT … FROM players WHERE removed = 0` (`_load_player_rows`, `names.py:199-205` — no `ORDER BY`)
happened to yield first, at **score 1.0**. That is why `King(268)->228`, `Wilson(260)->108`,
`Palmer(301)->154`, `Phillips(507)->273`, `P.M.Sarr(518)->208` etc. Note `by_web` already records
the ambiguity (`by_web["king"] == [228, 268]`) — `resolve_candidates` simply never consults it, and
the guard at `names.py:381-383` only fires for tokens that are in `surnames` (ambiguous tokens are
excluded from `surnames` by construction), so nothing catches it.

**Fix for class A:** make `full` a `dict[str, list[int]]` (or keep the scalar map plus a
`set[str]` of ambiguous keys) and treat an ambiguous token as *no match* unless the surrounding text
disambiguates (a first name, a club, or the full `first last` form). Do **not** silently pick the
first row. Concretely:

```python
# in name_index
full.setdefault(f, []).append(pid)        # list, not scalar
...
"ambiguous": {k for k, v in full.items() if len(v) > 1},

# in resolve_candidates, replacing `pid = idx["full"].get(token)`
pids = idx["full"].get(token) or []
pid = pids[0] if len(pids) == 1 else None
if pid is None and len(pids) > 1:
    # try to disambiguate with a neighbouring first name / full-name pattern
    ...
    continue                               # otherwise: ambiguous → no match, do not guess
```

Also fix the misleading comment on `names.py:221`, and add `ORDER BY id` to `_load_player_rows` so
the index is at least deterministic while you are in there.

**Acceptance:** the §0.2 self-consistency probe reports `667 / 0 / 0`. If a residual case remains,
either it is a genuine duplicate name (then assert it resolves to *no* player) or it is an
`ALIASES` candidate.

**A9.2 — `_ALIASES` over-matching in BBC.** `bbc.is_pl_relevant` expands `Villa`→`Aston Villa` etc.
(`bbc.py:39-53`) with `\b...\b`, which is fine, but `_player_terms()` accepts any `web_name`/
`known_name` of length ≥ 4 (`bbc.py:72-83`) — so a common English word that happens to be a player's
name (e.g. `White`, `James`, `Barnes`, `Burns`) makes almost any football article "PL-relevant". That
inflates `filtered_out=0` and pushes non-PL noise into the pipeline. Consider requiring a **club or
"Premier League" anchor** in addition to a lone player name, or dropping the most common-word names
from `_player_terms`. (Low priority — the pipeline's extractors are conservative downstream — but it
costs LLM calls.) The same word list is the input to A19, so fix them together.

**A9.3 — `_HEADLINE_RE` over/under-matching.** `reddit.py:50-55` includes `LATEST` and `\bFLAG\b`,
which fire on generic titles ("Latest news on X") and so fetch thread bodies for posts with no team
news; and it misses common phrasings ("team news", "predicted XI" are covered but "presser" is only
matched as a whole word, not "pre-match presser"). This is a cost/precision trade-off, not a
correctness bug — note it and move on.

---

## 8. A20–A23 (Low & nit)

### A20 — both "Test connection" paths hard-code a short timeout and `max_tokens: 8`

`backend/app/api/meta.py:153` `httpx.AsyncClient(timeout=20)` and `:160` `"max_tokens": 8`;
`backend/app/signals/llm_extractor.py:452` `httpx.AsyncClient(timeout=15)` and `:448`
`"max_tokens": 8`.

**Impact:** with a thinking model, an 8-token budget is consumed by `reasoning_content` before the
answer starts (A5/A6), so `content` is empty and/or `finish_reason == "length"`. Independently, a
cold local model can take >15 s to answer even when perfectly healthy — `llm.timeout_sec` defaults
to **300**, so the probe is 20× stricter than the real extraction path. Both produce "Failed: …" for
a working configuration: the same false-negative class as A4, and the reason a user may still not
trust the button after A4 is fixed.

**Fix:**
- Raise the probe budget to ≥ 256 tokens (still trivial) and use the configured timeout
  (`settings.config.llm.timeout_sec`, or `stream_idle_timeout_sec`) instead of a literal.
- Route **both** probes through one shared helper — `llm_extractor.test_llm_connection` already
  returns `{ok, status, detail}` and never raises; make `meta.test_llm` call it (after applying
  A4's body override) so the two cannot drift again.
- Include `finish_reason` and `usage` in the returned payload so an empty reply is diagnosable
  (A5).

**Acceptance:** a mock transport that sleeps 20 s then answers → `ok: True`; a mock returning empty
content with `finish_reason="length"` → `ok: False` with a message naming `max_tokens`.

### A21 — `optimizer.solver.restarts` is effectively 1 in the everyday case

`backend/app/optimizer/solver.py:521-530`:

```python
for r in range(cfg.optimizer.solver.restarts):
    if time.monotonic() > deadline:
        break
    rrng = random.Random(params.rng_seed + r * 7919)
    if r == 0 and params.current_squad:
        squad = _seed_from_current(params.current_squad, universe, cfg, rrng)
    elif tctx.hard_cap is None:
        squad = _greedy_seed(universe, cfg, rrng)
    else:
        continue  # a fresh ~30-transfer squad can never satisfy the cap
```

`hard_cap` is `None` only when a Wildcard/Free Hit covers the target GW or there is no real current
squad (`solver.py:161-167`). So with a real 15-man squad and no covering chip, every restart after
the first is skipped.

**Verified** (valid 15-man current squad, `bank=1`, `chips={}`, `restarts=20`): `tctx.hard_cap == 1`,
`has_current == True`, and `_hill_climb` was invoked **once**. (With no current squad, the 8 s
timebox bounds it — ~9 runs at a 2 s test timebox.)

**Impact:** `restarts` (Settings → Optimizer) does nothing for the everyday case, and README:356's
"multi-start hill climbing, 20 restarts" is misleading — the solver is a *single* deterministic local
search from the user's squad. That is a defensible design (it is cap-safe and reproducible), but it
should not be advertised as multi-start.

**Fix:** either (a) label the knob honestly in `Settings.tsx` ("restarts — only used when a chip
covers the GW or no current squad is set") and correct README:356; or (b) make the extra restarts
useful under a cap by seeding from the current squad with `k` random *within-cap* swaps instead of
`continue`. Also note `rrng` is only used by the seed functions — `_hill_climb` is passed the outer
`rng` (`solver.py:536`), which is intentional (deterministic replay) but worth a comment.

### A22 — `_bank_before` fallback arithmetic is wrong at the floor

`backend/app/optimizer/transfers.py:348-361`:

```python
def _bank_before(diff: dict) -> int:
    t = max(len(diff["transfers_in"]), len(diff["transfers_out"]))
    used = diff["free_transfers_used"]
    if t == 0 or used == 0:
        return diff["bank_after"]
    if t > used:
        return used
    return diff["bank_after"] + used
```

With `bank == 1` and `transfers == 1`: `free_used = 1`, `bank_after = max(1, 0) = 1`, so the fallback
returns `2` — the floor in `compute_diff` (`transfers.py:96`) breaks the reconstruction. The same
arithmetic is duplicated in the UI: `frontend/src/components/DiffTable.tsx:75`
(`diff.bank_before ?? diff.bank_after + diff.free_transfers_used`).

**Impact:** low — `compute_diff` now always returns an explicit `bank_before` (`transfers.py:114`) and
both `chip_advice_v1`/`v2` prefer it (`transfers.py:167, 245`), so the fallback only fires for legacy
`suggestions` rows written before that key existed. Legacy history rows would display
"Free transfers used 1 / 2".

**Fix:** delete the fallback and require `bank_before` (raise/`KeyError` loudly instead of guessing),
or fix it as `min(5, diff["bank_after"] + used)` — note the cap of 5 makes it ambiguous at both ends,
which is the argument for deleting it.

### A23 — silent truncation and two wrong TS shapes (nits)

- **`MAX_LIST_PLAYERS = 40`** (`llm_extractor.py:102`) silently truncates the filtered prompt:
  `_relevant_players` returns `[p for p in player_list if p["id"] in hit_ids][:MAX_LIST_PLAYERS]`
  (`:115`), and `_validate` (`:143-161`) drops every `player_id` outside `valid_ids`, so the model
  can never signal a player who was cut. A long transcript naming 60 players loses 20 of them with no
  log line. Fix: raise the cap, or sort the hit list by `selected_by_percent`/EP before slicing so the
  truncation keeps the fantasy-relevant names, and `log.warning` when truncation happens.
- **`frontend/src/types.ts:3-7`** declares `ChipWindow { chip; event: number; playable_next_gw }`,
  but `season.active_chip_windows()` (`season.py:71-79`) returns
  `{chip, set, start_event, stop_event, playable_next_gw}` — no `event`. Harmless today (only `chip`
  and `playable_next_gw` are read, `MetaPanel.tsx:55-64`) but it will mislead the next change. Fix
  the type.
- **`frontend/src/types.ts:61-78`** `LineupPlayer` requires `ep_next`,
  `chance_of_playing_next_round`, `team_name` and `bought_cost`, but `_State.as_lineup()`
  (`solver.py:373-389`) emits only `player_id, web_name, element_type, team, now_cost, status,
  can_select, selected_by_percent, role, bench_order, is_captain, is_vice_captain`. So
  `SuggestedLineup.squad: LineupPlayer[]` (`types.ts:141`) is a lie: `PitchView.statusBadge`
  (`PitchView.tsx:18-24`) reads `p.chance_of_playing_next_round`, which is `undefined` for suggested
  squads (so a 50%-doubt player shows no ⚠️ badge), and `PlayerPicker`-style EP sorting would see
  `undefined`. Fix: add the missing fields to `as_lineup()` (it has `p` in scope and `universe`
  entries carry everything) or split the type into `SquadPlayer` (suggested) and `LineupPlayer`
  (stored).

---

## 9. Open questions for the next agent

1. **`chips` naming source of truth.** §1 normalises at ingest. The alternative is to make
   `season.CHIPS`, `transfers.CHIP_NAMES` and `lineups.LineupIn.chips` adopt the API spelling
   (`3xc`) instead. Not recommended: `triple_captain` is already the wire format in `config.json`,
   the REST API (`ChipPlayIn`) and the DB's `lineups.chips` JSON, so moving the boundary is a
   breaking API change for no gain.
2. **`name_in_text` after the A2 fix.** The intended contract needs a decision (see §3) — returning
   the canonical `web_name` is simplest, since no caller needs the raw span.
3. **`max_tokens` target.** 4096 vs 8192 depends on the real per-item token cost; measure one
   `newest` BBC article and one podcast transcript before choosing.
4. **Should `test_llm_connection` be `ok: False` on empty content?** Arguably yes (§7/A5), but it
   changes the Settings UX, so confirm first.
5. **ESPN.** `sources.espn.enabled` is `false` and the config comment says ESPN 403s from this
   network, yet the live endpoint returned **HTTP 200** during this audit. Either enable it, or
   update the comment — the current note looks stale.
6. **Chip-play semantics (A14).** Does the checkbox mean "record that I played this" (a pure log,
   option 1) or "remove one from my hand" (a state change, option 2)? The answer decides whether
   `lineups.chips` is user-owned or app-owned. Recommended: user-owned (option 1), with
   `apply_suggestion` as the only automatic decrement.
7. **`bought_cost` source (A15).** Manual entry per player, or drop the fee model entirely and always
   assume purchase-at-current-price? The second is honest but changes the displayed `budget_after`
   semantics.
8. **Rollover scope (A10).** Should `raw_items` and `poll_log` survive a season change? Recommended:
   wipe `raw_items`, keep `poll_log`.

---

## 10. Test plan

### 10.1 Update (A1) — this is the regression guard, and it is verified

`backend/tests/conftest.py:66-96` currently seeds the `chips` table with a **raw INSERT** using the
**app's** spelling. Two changes are needed, and **both** matter:

1. Use the **real API spelling** in the fixture data — a list of dicts shaped like the API payload:
   ```python
   CHIPS = [
       {"id": 1, "name": "wildcard", "number": 1, "start_event": 1,  "stop_event": 38, "chip_type": "wildcard"},
       {"id": 2, "name": "freehit",  "number": 1, "start_event": 1,  "stop_event": 38, "chip_type": "free_hit"},
       {"id": 3, "name": "bboost",   "number": 1, "start_event": 6,  "stop_event": 6,  "chip_type": "3x_points"},
       {"id": 4, "name": "3xc",      "number": 1, "start_event": 6,  "stop_event": 6,  "chip_type": "3x_captain"},
   ]
   ```
2. Insert them through the **real ingest path**, not a raw INSERT — replace the `executemany` block
   with:
   ```python
   conn.commit()          # release the fixture connection's write lock first!
   dbmod.upsert_chips(CHIPS)
   ```
   **The `conn.commit()` is mandatory.** `upsert_chips` opens its *own* connection
   (`execute_many` → `get_conn()`), and the fixture's `conn` is holding a write lock from the
   preceding `executemany` calls. Without the commit, every test blocks ~5 s on
   `sqlite3`'s lock timeout and the suite appears to hang (verified: a 228-test run exceeded a
   10-minute budget).

**Why the raw INSERT is not enough:** `CHIP_NAME_MAP` lives in `upsert_chips`, so a raw INSERT of
`"3xc"` bypasses the normalisation and keeps failing *after* the fix. Only routing through
`upsert_chips` makes the fixture exercise the same path production uses.

**Verified outcome** (I ran this exact experiment and reverted it):

| State | Result |
|---|---|
| Fixture changed, `CHIP_NAME_MAP` **not** implemented | **4 failed**, 224 passed |
| Fixture changed, `CHIP_NAME_MAP` implemented | **228 passed** |

The four that fail before the fix are exactly the chip-window guards:

```
FAILED tests/test_api.py::test_meta_season
FAILED tests/test_transfers.py::test_chip_advice_wildcard_used_when_over_bank
FAILED tests/test_transfers.py::test_v2_wildcard_use_and_3xc_note
FAILED tests/test_transfers.py::test_v2_3xc_confirmed_starter_signal
```

(Yes, the *wildcard* tests fail too: `test_meta_season`'s `chip_windows` assertion is the direct one;
the transfer tests go through `active_chip_windows()`/`chip_covers_transfers`, which consult the same
table.) So: **make the fixture change first, watch those four go red, then implement A1 and watch
them go green.** That is the whole point of the change.

Then add:
- `test_upsert_chips_normalises_names`: upsert `{"id": 9, "name": "3xc", ...}` → `SELECT name FROM
  chips WHERE id = 9` returns `triple_captain`.
- `test_chip_advice_triple_captain_window_open`: triple captain in hand + open window + high captain
  EP → `recommendation == "use"`.
- `test_chip_advice_triple_captain_no_window`: same, but a `chips` row whose window excludes the
  target GW → `recommendation == "skip"` with the *window* reason (not the sets-remaining reason).

### 10.2 New (A2)
`backend/tests/test_match_names.py` — parametrized, one case per class of breakage:
```python
@pytest.mark.parametrize("text, want_id", [
    ("Calvert-Lewin is fit again for Everton.", 346),
    ("Pascal Gross was excellent for Brighton.", 124),
    ("Pascal Groß is the most-transferred in player", 124),
    ("Odegaard returned to Arsenal training.", 15),
    ("Ødegaard returned to Arsenal training.", 15),
    ("B.Fernandes scored for Manchester United.", 426),
    ("O'Riley started for Brighton.", 126),         # apostrophe class (rev 2)
    ("João Pedro starts up front.", None),          # assert it does NOT become another player
])
def test_resolve_candidates_real_world_forms(db_path, text, want_id): ...
```
Plus a **precision** test for A9: `"B.Fernandes scored"` must resolve to 426, **not** 525.
Plus the **self-consistency** test — the strongest single guard for A2 + A9.1, and it needs no
hand-written cases:
```python
def test_every_player_resolves_from_own_web_name(db_path):
    """A player's own web_name must resolve to that player — or to nobody when the
    name is genuinely shared (class A). It must NEVER resolve to a different player."""
    idx = names.get_name_index()
    rows = names._load_player_rows()
    shared = {k for k, v in idx["by_web"].items() if len(v) > 1}
    wrong, missing = [], []
    for r in rows:
        hits = {h["player_id"] for h in names.resolve_candidates(r["web_name"], idx)}
        if hits == {r["id"]}:
            continue
        if hits and r["id"] not in hits:
            wrong.append((r["web_name"], sorted(hits)))
        elif not hits and names._flat(r["web_name"]) not in shared:
            missing.append(r["web_name"])
    assert wrong == []      # class A + class P: must be empty
    assert missing == []    # class P only: must be empty after A2
```
The `conftest` fixture pool is small and ASCII-only, so add 4-5 synthetic players for this test
(`Groß`, `Ødegaard`, `Calvert-Lewin`, `B.Fernandes`, `O'Riley`) plus a **duplicate pair** (two
players with `web_name = "King"`) to cover class A. Keep the live-pool probe as a script under
`backend/scripts/` (e.g. `check_name_resolution.py`) so it can be re-run against `data/fpl.db`.

### 10.3 New (A10)
`backend/tests/test_rollover.py`:
- `test_rollover_with_saved_lineup` — seed the standard lineup, `set_meta("season","2025/26")`,
  `check_season_rollover("2026/27")` returns `True` and does not raise; assert `lineups`,
  `lineup_players`, `suggestions`, `players`, `fixtures`, `chips`, `signals`, `chip_plays_log` are all
  empty and `meta.season` is the new marker.
- `test_rollover_with_live_matches` — insert a `live_matches` row, same assertions (covers the
  `fixtures` ordering).
- `test_bootstrap_survives_rollover` — monkeypatch `fpl_fetcher.http.get_json` to return a canned
  bootstrap payload with `events[0].deadline_time = "2026-08-14…"`; assert `fetch_bootstrap()`
  completes with a saved lineup present (this is the integration guard).

### 10.4 New (A11–A14, A16, A17)
- `test_signals_inactive_team_filter` (A11): `GET /api/signals?active=false&team=ALP` → 200, only
  that team; also `player_id + team + category` combined.
- `test_players_has_signal_filter` (A12): one active + one expired signal → only the active player.
- `test_generate_honours_target_gw` (A13): POST `target_gw: 5` → response and stored row are 5.
- `test_chip_play_is_idempotent` (A14): log → unlog → log leaves `lineups.chips` unchanged; the
  second log reports `already: True`.
- `test_put_lineup_preserves_kind` / `test_put_lineup_can_promote_kind` (A16).
- `test_put_settings_response_matches_get_shape` (A17): equal key sets, `llm_status` present.

### 10.5 New (A3/A4/A5/A20)
- `test_get_settings_redacts_api_key`: `GET /api/settings` → `llm.api_key == ""` and
  `llm_status.key_set is True`.
- `test_put_settings_empty_key_keeps_existing`: PUT with `api_key: ""` does not clear the stored key.
- `test_test_llm_uses_body_override`: `POST /api/settings/test-llm` with a body whose `base_url`
  points at a mock transport → the mock is called (proving the draft is used).
- `test_streaming_empty_content_is_failure`: SSE stream that emits only `reasoning_content` →
  `extract_signals_llm` returns `None` (retryable), and `test_llm_connection` reports `ok: False`.
- `test_test_llm_timeout_uses_config`: a mock that sleeps longer than the old 15 s literal but less
  than `llm.timeout_sec` → `ok: True` (A20).

### 10.6 New (A19)
`test_surname_tier_ignores_common_words`: `resolve_candidates("White will start", idx) == []` and
`resolve_candidates("Ben White will start for Arsenal", idx)` contains the White id;
`resolve_candidates("The king of the box scores", idx) == []`.

### 10.7 Manual verification
1. `POST /api/refresh/fpl` → `SELECT DISTINCT name FROM chips` shows `triple_captain`.
2. **Settings → Test connection** *before saving* an edited key → must test the edited values.
3. Refresh BBC/Reddit → the "Pascal Groß is the most-transferred in player" item yields a signal for
   player 124.
4. `Settings → Data → Refresh all` with the LLM up → `poll_log` shows `llm/ok` rows, not only errors.
5. Save Settings → the LLM status row must still read "ready" with the base URL and "key set".
6. My Team: duplicate a lineup as a test, press **Save changes** → the tab must keep the ⚗ marker and
   Suggestions must keep Apply disabled.
7. My Team: tick a chip checkbox, untick it, tick it again → `MetaPanel`'s chip stepper must not
   change.
8. My Team: note a player's price, change it in the DB (`UPDATE players SET now_cost = now_cost + 5
   WHERE id = ?`), press **Save changes**, reload → the player's sell value must reflect the old
   purchase price (A15), or the UI must not claim a fee it does not model.
9. Suggestions: pick "GW 7" in the Target GW dropdown → the card header/checklist must say GW 7.

---

## 11. File map

| File | Change |
|---|---|
| `backend/app/db.py` | **A10** reorder the rollover deletes (children → parents) + clear `chip_plays_log`; **A1** `CHIP_NAME_MAP` + normalise in `upsert_chips` + add `logging` |
| `backend/app/fetchers/fpl.py` | **A10** wrap the rollover call in `try/except` so it cannot abort the refresh |
| `backend/app/season.py` | **A1** optional: warn on unknown chip names in `active_chip_windows` |
| `backend/app/signals/names.py` | **A2** `_norm_scan`/`_FOLD`; use it for patterns **and** scanned text; fix `name_in_text`; **also point `_norm` at it** (rev 2); **A8** prune/annotate dead aliases; **A9.1** `full` → `dict[str, list[int]]` + treat an ambiguous token as no-match (class A); **A19** tighten the surname scan tier |
| `backend/app/api/news.py` | **A11** build the `active=false` WHERE clause as a list |
| `backend/app/api/players.py` | **A12** `s.expires_at > now_utc()` |
| `backend/app/api/suggestions.py` | **A13** honour `body["target_gw"]` (+ pass it to `chip_advice_v2`) |
| `backend/app/api/lineups.py` | **A14** idempotent `log_chip_play` (+ optional restore in `unlog_chip_play`); **A15** preserve `bought_cost` + accept it in `LineupPlayerIn`; **A16** `kind: … | None = None` preserved on update |
| `backend/app/api/meta.py` | **A3** redact `llm.api_key` on read; keep-on-empty in `put_settings`; **A4** accept a body in `test-llm`; **A17** shared `_settings_payload()` for GET/PUT; **A20** configured timeout + bigger probe budget |
| `backend/app/signals/llm_extractor.py` | **A5** capture/log `reasoning_content`; empty-content = failure; **A6** log `finish_reason == "length"` on success; **A20** one shared probe helper; **A23** truncation log |
| `backend/app/optimizer/solver.py` | **A18** implement or delete `exact_ilp`; **A21** restarts or honest labelling; **A23** complete `as_lineup()` fields |
| `backend/app/optimizer/transfers.py` | **A22** fix or delete the `_bank_before` fallback |
| `backend/app/config.py` | **A18** drop `exact_ilp` (if removing) |
| `backend/app/fetchers/bbc.py` | **A9.2** optional: require a club/PL anchor for player-name-only relevance |
| `frontend/src/pages/Settings.tsx` | **A4** send the draft to `testLlm`; key-field placeholder; **A3** "leave blank to keep" copy; weight-check scoping; **A17** don't trust the PUT response; **A18** drop the ILP checkbox |
| `frontend/src/pages/MyTeam.tsx` | **A16** carry and send `kind`; **A15** `bought_cost` field + payload |
| `frontend/src/pages/Suggestions.tsx` | **A13** restrict the Target GW options to `next_gw` / `next_gw + 1` |
| `frontend/src/api.ts` | **A4** `testLlm(body)` signature |
| `frontend/src/types.ts` | **A3/A5** `llm_status.key_set`, optional `empty_content`; **A18** drop `exact_ilp`; **A23** fix `ChipWindow`, split `SuggestedLineup`'s player type |
| `frontend/src/components/DiffTable.tsx` | **A22** drop the `bank_before` fallback; **A23** drop or fix the `transfers_in[i]`/`transfers_out[i]` pairing |
| `config.example.json` / `config.json` | **A6** raise `llm.max_tokens`; **A5** comment; **A18** drop `exact_ilp` |
| `README.md` | **A7/A18/A21** + §13 doc drift |
| `ACCEPTANCE.md` | §13: the rollover "9/9 PASS" claim is not reproducible until A10 is fixed |
| `backend/tests/conftest.py` | **A1** fixture `3xc`; **A2** add non-ASCII + hyphenated players |
| `backend/tests/*` | §10.1–10.6 |

---

## 12. What is already correct (do not "fix" these)

Confirmed against the code and the live DB while auditing, so you can skip re-deriving it:

- **Transfer accounting**: a swap is 1 transfer; `free_transfers_used=1`, `bank_after` respects the
  floor of 1 and the cap of 5 (the cap is enforced by `LineupIn.transfer_bank`, `lineups.py:44`);
  `penalty = 4 × max(0, transfers − bank)`.
- **`sell_value`** half-increase rounding (all four spec cases) — the *function* is right. The
  *input* is wrong: see A15.
- **Free-Hit consecutive-GW ban** is gw-scoped via `freehit_played_in`, and `chip_covers_transfers`
  and `chip_advice_v2` share it — they cannot disagree **provided the same target GW is passed to
  both** (see A13's warning).
- **Availability buckets** `chance_75`/`chance_25` exist and fire; `status u/s` and `can_select=0`
  still hard-gate to 0.0 and `build_universe` excludes them. (Note: A(p) multiplies `doubt` by the
  chance bucket for `status='d'`, giving 0.7 × 0.65 = 0.455 for a 50% doubt — arguably intended, but
  worth a deliberate comment rather than looking like an accident.)
- **`_decrement_chips`** is wired into both `apply_suggestion` and `POST /lineups/chip-play`, so
  playing a chip really does decrement `lineups.chips` and cannot re-open an unlimited-transfers hole
  on the Wildcard side. **But** the chip-play wiring double-counts (A14) — fix that without removing
  the `apply_suggestion` half.
- **LLM failure contract**: hard failure → `None` (retryable, attempts counted, item requeued);
  clean no-signal → `[]`. Verified against the live 401 and the live 200.
- **`db.execute` / `ingest_item` dedupe**: `execute()` returns `lastrowid`, which is `0` for an
  `ON CONFLICT DO NOTHING` no-op on a fresh connection — that is what makes `ingest_item`'s
  `rowid or None` dedupe detection work. Do not "simplify" it.
- **All five live sources reachable** on 2026-10-02: FPL bootstrap 200, FPL fixtures 200, BBC
  football RSS 200, BBC PL RSS 200, Reddit hot.rss 200 + OAuth 200, YouTube channel feed 200,
  ESPN news 200. (`/new.rss` 429 — see A7.)
- **`config.json` is gitignored** and untracked; the Reddit OAuth secret and LLM key are not in git.
- `npx tsc --noEmit` clean; `pytest` 228 passed (re-verified at the end of this audit).

---

## 13. Documentation drift (cheap fixes; do them while you are in the area)

None of these are code bugs, but each one will cost the next agent time. Verified line numbers as of
this revision.

| Claim | Reality | Where |
|---|---|---|
| "169 tests" (twice) | 228 | `README.md:533`, `README.md:551` |
| Architecture lists `api/ … settings` | there is no `api/settings.py`; settings live in `api/meta.py`. `api/entry.py` is missing from the list | `README.md:531` |
| "`players.season` bumped" | no such column exists in the schema | `README.md:506` |
| "group.fpl_entry_id — optional public FPL entry ID (rank card not in v1)" | the rank card exists and is documented 90 lines earlier | `README.md:196` vs `README.md:106-108` |
| LLM source row: "60 s timeout" | `llm.timeout_sec` defaults to **300**; the streaming idle guard is 90 s | `README.md:207` |
| Reddit row advertises `top.rss?t=week` | only `hot.rss`/`new.rss` (and the JSON equivalents) are implemented | `README.md:204` |
| FPL row lists `/api/event/{gw}/` and `/api/game-settings/` | never called; `fetchers/fpl.py` uses bootstrap-static, fixtures, element-summary, entry only | `README.md:202` |
| ESPN row lists `{scoreboard,news,standings}` | only `news` is implemented | `README.md:206` |
| "yt-dlp `--write-auto-sub`" | the provider tries `--write-auto-sub` then `--write-sub`, preferring human captions | `README.md:565` vs `youtube.py:87-90` |
| "exact-ILP — greyed out until PuLP is installed" / "`exact_ilp: false` (needs `pip install pulp`)" | the setting is never read (A18) | `README.md:136-137`, `README.md:195` |
| "multi-start hill climbing, 20 restarts" | 1 restart in the everyday case (A21) | `README.md:356` |
| Season rollover dry pass "9/9 PASS", "old-season signals wiped by rollover logic" | the rollover raises `IntegrityError` with a saved lineup (A10) | `ACCEPTANCE.md:125-156` |
| §8 "sell-on fee" described as a live rule | the fee is always 0 (A15) | `README.md:317-318` |

Also worth a note in the README: `Settings → Sources` interval changes need an app restart
(`create_scheduler` reads the config once at boot, `scheduler.py:94`), while the optimizer weights
are read per-generate. The Settings page does not say which is which.