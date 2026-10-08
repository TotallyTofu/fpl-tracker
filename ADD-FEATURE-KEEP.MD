# FPL Tracker — Add Feature: Keep players in lineup

> **Written** 2026-10-08 (before the GW6 deadline) for the agent that builds it.
> **Builds on the working tree as it is now.** v1.1 (`ADD-FEATURES.MD`) is built but
> **not committed**. Do not revert, stash or "clean up" those changes; build on top.
> **Tasks** K1–K6, in order. Each ends with green tests:
> `cd backend; ..\.venv\Scripts\python -m pytest tests -q`, then
> `cd frontend; npx tsc --noEmit; npx vite build`.
> **UI name:** "Keep players in lineup". Short label on buttons and columns: **Keep**.

---

## 1. What the user asked for

- In **My Team**, mark any players the plans must not transfer out.
- Marked players are shown **greyed out**.
- The optimizer can **never** sell a marked player: in every plan, with or without a
  chip, **including Wildcard and Free Hit**.
- **No limit** on how many players can be marked.

---

## 2. Decisions already made (do not relitigate)

1. **Keep means "stays in the 15-player squad".** It does not force the player into
   the starting XI or the captaincy. The solver still picks XI, bench order, captain
   and vice-captain. ("Always start" is an open question, §9.)
2. **Hard constraint, not a penalty.** It applies everywhere the solver can sell a
   player:
   - all three profiles (Best projected, Differential, Safe);
   - no chip, Wildcard, Free Hit, Bench Boost, Triple Captain;
   - the greedy-seed restarts (they only run when the transfer cap is lifted, i.e.
     exactly under Wildcard/Free Hit), steepest ascent, the random hill climb;
   - the convergence "low-ownership swap" in `dedupe_profiles`;
   - the no-feasible-plan fallback context;
   - **the extra Wildcard/Free Hit solve behind the chip advice** (`_rebuild_gain`).
     Without it the advice would promise points that require selling a kept player.
3. **Stored per lineup and player** (`lineup_players.keep`). A sandbox copy can keep
   different players. Duplicating a team copies the flags. Applying a plan keeps the
   flags (kept players are in the applied squad by construction). Season rollover
   already wipes `lineup_players`.
4. **Part of the My Team draft**, saved with **Save team**, like captain and bench.
   Plans read the saved lineup. My Team shows an "unsaved" hint while the keep flags
   differ from the saved team.
5. **A kept player who is unavailable** (status `u`/`s`, `can_select = 0` or chance
   0, the players `build_universe` excludes today) **stays in the squad**. He projects
   0 (`play_factor` already returns 0), goes to the bench like any 0-point player,
   does **not** count as a forced transfer (no −4 for him), and the plan says so.
   FPL itself lets you hold such a player. The strict validator must accept him, for
   kept ids only.
6. **Keep binds the optimizer only.** Manual edits in My Team are not blocked (My
   Team mirrors the real FPL team; the user may have sold the player on the FPL site).
   Swapping out a kept player in My Team gives a replacement that is **not** kept.
7. **Visual: greyed shirt plus a lock badge.** Grey alone reads as "unavailable" or
   "disabled"; the lock says why. Kept players stay clickable and draggable (captain,
   bench moves and swaps still work).
8. **No projection, objective or config changes.** No new settings.
9. **All 15 kept** is valid: the plans make no transfers and only choose XI, bench and
   captain, with a note saying so.

---

## 3. Order of work

| Task | What | Depends on |
|---|---|---|
| K1 | `lineup_players.keep` column + lineups API | — |
| K2 | Solver: locked players | — |
| K3 | Suggestions API: pass locks, rebuild-gain solve, `diff.kept_ids`, apply | K1, K2 |
| K4 | My Team UI (toggle, table column, greyed pitch tokens) | K1 |
| K5 | Plans UI (Transfer plans page, plan pitch) | K3, K4 |
| K6 | README, CHANGELOG | all |

New test file for everything: `backend/tests/test_keep.py`.

---

## K1 — Storage and lineups API

**Files**: `backend/app/db.py`, `backend/app/api/lineups.py`, `backend/tests/test_keep.py`.

**Spec**:

- `db.py` `SCHEMA`, table `lineup_players`, after `bought_cost`:
  ```sql
  keep INTEGER NOT NULL DEFAULT 0,             -- 1 = plans never sell this player (ADD-FEATURE-KEEP.MD)
  ```
- `db.py` `init_db`, next to the v1.0 `lineups` migrations (existing DBs already have
  the table, so `CREATE TABLE IF NOT EXISTS` won't add the column):
  ```python
  # keep-players migration: lineup_players.keep (additive; existing rows = not kept)
  lpl = _table_columns(conn, "lineup_players")
  if lpl and "keep" not in lpl:
      conn.execute("ALTER TABLE lineup_players ADD COLUMN keep INTEGER NOT NULL DEFAULT 0")
  ```
- `lineups.py`:
  - `LineupPlayerIn`: `keep: bool | None = None  # None = keep the stored flag (like bought_cost)`.
    **Why None, not False**: `MyTeam.renameLineup` and any other caller that rebuilds
    the player list from a GET without the field would otherwise clear every keep.
  - `_save_players`: read the previous flags in the **same** query as `bought_cost`
    (before the DELETE):
    `SELECT player_id, bought_cost, keep FROM lineup_players WHERE lineup_id = ?`.
    Flag to store: `p.keep if p.keep is not None else bool(prev_keep.get(p.player_id, 0))`.
    Add `keep` to the INSERT.
  - `_load_lineup`: in the existing 0/1 → bool loop add `p["keep"] = bool(p["keep"])`
    (`lp.*` already selects the column). The API contract is boolean, like `is_captain`.
  - `duplicate_lineup`: pass `keep=p["keep"]` in the `LineupPlayerIn(...)` list.
  - Module docstring: one bullet describing `keep`.

**Tests** (`test_keep.py`; `client` fixture as in `test_api.py`, squad from
`conftest.valid_squad()`):
- Migration: an old DB whose `lineup_players` has no `keep` column → `init_db` adds it;
  existing rows read 0. Copy the pattern of `test_pipeline.test_extract_attempts_migration`.
- POST a lineup with `keep: true` on two players → GET returns `keep: true` for those two
  and `false` for the rest (JSON booleans, not 0/1).
- PUT without the `keep` field keeps the stored flags; PUT with `keep: false` clears one.
- `POST /lineups/{id}/duplicate` copies the flags.

---

## K2 — Solver

**Files**: `backend/app/optimizer/solver.py`, `backend/app/optimizer/rules.py`,
`backend/tests/test_keep.py`.

**Naming**: call it `locked_ids` inside the optimizer. **Not** `keep_ids`:
`_apply_ep_floor(keep_ids=...)` already uses that name for "the user's own players".

**Determinism requirement**: with `locked_ids` empty, every code path must give the same
result as today, so the existing suite passes **unchanged**. In particular, do not change
the RNG call order on the unlocked path (step 5).

**Spec**:

1. `SolveParams`: `locked_ids: frozenset[int] = frozenset()` with a comment
   ("players the user marked Keep; never sold"). In `solve()`:
   `locked = frozenset(params.locked_ids) & cur_ids`. Ids outside the current squad are
   ignored with a `log.warning` (add a module logger, `logging.getLogger("fpl.solver")`).
2. `_TransferCtx`: field `locked_ids: frozenset = frozenset()` and
   ```python
   def can_sell(self, p: dict) -> bool:
       return p.get("id", p.get("player_id")) not in self.locked_ids
   ```
   Add `locked_ids=locked` to the `common` dict in `solve()`, so the fallback ctx
   carries it too. `dedupe_profiles` already receives each profile's ctx via `s.tctx`.
3. `build_universe(target_gw, cfg, include_ids: frozenset = frozenset())`: when
   `include_ids` is non-empty, the WHERE becomes `(<today's eligibility>) OR p.id IN (?,…)`.
   `solve()` passes `include_ids=locked`. Effects, all wanted:
   - a kept unavailable player is in the universe with `ep` 0 (via `play_factor`);
   - `uni_ids` includes him, so `forced_transfers = len(cur_ids - uni_ids)` no longer
     counts him and `_seed_from_current` keeps him;
   - the differential floor keeps him (`_apply_ep_floor(keep_ids=cur_ids)` rescues
     current players below the floor; no change needed there).
   `projlog.py` calls `build_universe(gw, cfg)` and must stay unchanged (default arg).
4. `_greedy_seed(universe, cfg, rng, budget=BUDGET, locked=frozenset(), cost=None)`:
   pre-seed the locked players before the position loop:
   ```python
   by_id = {p["id"]: p for p in universe}
   for pid in sorted(locked):            # sorted: deterministic
       p = by_id.get(pid)
       if p is None:
           continue
       squad.append(p)
       picked.add(pid)
       club_count[p["team"]] = club_count.get(p["team"], 0) + 1
       left -= cost(p) if cost else p["now_cost"]
   ```
   The position loop already stops a full position (its first statement is the
   `>= SQUAD_COMP[pos]` check → `break`) and skips `picked` ids; the backfill counts
   `have` the same way. Use `cost` (= `tctx.cost`, the sell value) **only** for the
   locked players and keep `now_cost` for everyone else, so the unlocked path is
   unchanged. `_run_restarts` calls
   `_greedy_seed(universe, cfg, rrng, tctx.budget, tctx.locked_ids, tctx.cost)`.
5. `_hill_climb`, `squad_swap` move:
   ```python
   if tctx.locked_ids:
       sellable = [j for j, p in enumerate(state.squad) if tctx.can_sell(p)]
       i = rng.choice(sellable) if sellable else None
   else:
       i = rng.randrange(len(state.squad))   # unchanged: seeded results must not move
   out_p = state.squad[i] if i is not None else None
   cands = by_pos.get(out_p["element_type"], []) if out_p is not None else []
   if not cands:
       ok = False
   else:
       ...  # existing body
   ```
   Picking among sellable players (rather than rejecting a locked pick) matters: with
   12 kept, 80% of swap moves would otherwise be wasted and count toward the
   400-no-improve stop.
6. `_greedy_improve`: first line of the `for i, out_p in enumerate(state.squad)` loop:
   `if not tctx.can_sell(out_p): continue`.
7. `_try_low_own_swap(s, tctx)`: when `tctx` is given, build `pos_mates` from sellable
   entries only (`tctx.can_sell(e)`; entries carry `player_id`, which `can_sell`
   handles). With `tctx=None` (legacy callers) nothing is locked.
8. Invariant in `solve()`, right after `lineup = best_state.as_lineup()`:
   ```python
   dropped = locked - {l["player_id"] for l in lineup}
   if dropped:
       raise ValueError(f"solver sold kept player(s) {sorted(dropped)}")
   ```
9. `rules.validate_lineup(..., strict=False, allow_unavailable: frozenset = frozenset())`:
   for ids in `allow_unavailable`, `PLAYER_UNAVAILABLE` is a **warning** even when
   `strict=True`. `solve()` passes `allow_unavailable=locked`. Update the rules module
   docstring (strict mode exception).
10. Notes. In `solve()`, insert before "Top projected starters" (after the fallback
    note):
    - when `locked`: `"Kept by you, never sold: A, B, C."` (web names in squad order);
    - when `len(locked) == 15`: `"You kept all 15 players, so this plan only picks the
      starting XI, bench and captain."` (instead of the line above);
    - for each kept player who is unavailable: `"X is kept although unavailable
      (projected 0)."` Decide "unavailable" with a small `_unavailable(p)` helper that
      mirrors the `build_universe` WHERE clause (status u/s, `can_select = 0`,
      `removed = 1`, chance 0), so the two never disagree.
11. Module docstring: one bullet ("Keep: players in `locked_ids` are never sold …").

**Tests** (`test_keep.py`; reuse `_current_squad`, `VALID_IDS` and the edge-keeper
insert from `test_solver.py`: import them or copy the helpers):
- `test_locked_player_not_sold_for_better_swap`: insert player 99 (t1 GK, +2.1 over
  player 1; see `_insert_edge_keeper`). `locked_ids={1}`, bank 1 → 1 in squad, 99 not.
  (Without the lock 99 comes in: `test_penalty_blocks_small_gain_swap` proves it.)
- `test_locks_hold_under_transfer_chips`, parametrized over chip ∈ {wildcard, freehit}
  × all `PROFILES`: `chips={"wildcard": 1, "freehit": 1}`, `chip_played=chip`,
  `locked_ids={1, 7, 15, 19, 23}` → all five in every squad. The conftest cfg has
  `restarts=3`, so greedy-seed restarts are exercised.
- `test_all_15_locked_makes_no_transfers`: Wildcard played, all `VALID_IDS` locked →
  squad ids == `VALID_IDS`; the "kept all 15" note is present.
- `test_kept_unavailable_player_stays_on_bench`: player 23 status `'u'`, locked,
  bank 1, no chip → 23 in squad with role bench and ep 0, no other transfer forced
  (`compute_diff(...)["penalty_points"] == 0`), the "kept although unavailable" note is
  present. Control: without the lock 23 is replaced (`test_forced_transfers_only_overrun`).
- `test_greedy_seed_includes_locked`: call `_greedy_seed` directly on a precomputed
  universe with a low-EP locked player → he is in the 15 and the squad is 2/5/5/3.
- `test_low_own_swap_skips_locked`: like `test_dedupe_swap_respects_cap`, but with a
  `_TransferCtx` (no cap: `chip_covers=True`) whose `locked_ids` holds the cheapest
  same-position player → the swap replaces a different player or none; the locked
  player is still there.
- `test_validate_strict_allows_kept_unavailable`: rules level; the same unavailable
  player is an error with `strict=True` and a warning when his id is in
  `allow_unavailable`.

---

## K3 — Suggestions API

**Files**: `backend/app/api/suggestions.py`, `backend/tests/test_keep.py`.

**Spec**:

- `_current_squad_for_diff`: add `lp.keep` to the SELECT.
- `generate()`: `locked = frozenset(p["player_id"] for p in current_squad if p["keep"])`;
  pass `locked_ids=locked` to every `SolveParams`.
- `_rebuild_gain`: pass `locked_ids=params.locked_ids` to its `SolveParams` (easy to
  miss: it builds its own).
- After `compute_diff(...)`: `diff["kept_ids"] = sorted(locked)`. The diff is stored as
  JSON, so no migration. The plan views use it (K5), so an older plan shows the keeps
  it was made with, not today's.
- `_replace_squad` (apply): read `player_id, bought_cost, keep` from the old rows and
  insert `keep` with the new rows (0 for new signings). Kept players are always in the
  applied squad (K2 invariant), so their flags survive.
- No new note here: the solver notes (K2 step 10) already reach `rationale.notes`.

**Tests**:
- API: create a lineup with `keep: true` on two players, generate with
  `chip: "wildcard"` → every suggestion's `lineup.squad` contains both;
  `diff.kept_ids` equals the two ids, sorted; a note starts with "Kept by you".
- Rebuild gain honours locks: monkeypatch `app.api.suggestions.solve` with a wrapper
  that records each `SolveParams` and calls the real `solve`; generate with no chip →
  the rebuild call (the one with `chip_played` set) has the same `locked_ids`.
- Apply: generate (no chip), apply `max_ep` → GET the lineup → the two kept players are
  still `keep: true`; any new signing is `keep: false`.

---

## K4 — My Team UI

**Files**: `frontend/src/types.ts`, `frontend/src/components/PitchView.tsx`,
`frontend/src/pages/MyTeam.tsx`, `frontend/src/app.css`.

**`types.ts`**:
- `LineupPlayer`: `keep?: boolean;  // "Keep players in lineup": plans never sell this player`.
  Optional, because `SquadPlayer` (suggested squads) derives from `LineupPlayer` and
  suggestions don't carry it.
- `Diff`: `kept_ids?: number[];`

**`PitchView.tsx`**:
- New prop, next to `newIds`: `keptIds?: Set<number>` ("players the plans never sell:
  greyed, with a lock").
- Export a small icon used here and in My Team:
  ```tsx
  export const LockIcon = () => (
    <svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor"
      strokeWidth="2" aria-hidden="true">
      <rect x="3" y="7" width="10" height="7" rx="1.5" />
      <path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2" />
    </svg>
  );
  ```
- Pitch token: add `keptIds?.has(p.player_id) ? "kept" : ""` to the class list; inside
  `.shirt` add `<span className="lock-badge" aria-label="Kept"><LockIcon /></span>`;
  append ` · kept: plans never sell this player` to the `title`.
- Bench card: same class on `.bench-card` and the same badge inside its `.shirt`.

**`app.css`**, next to the `.token.new .shirt` rule:
```css
/* Keep players in lineup: greyed shirt + lock (grey alone would read as "unavailable") */
.token.kept .shirt, .bench-card.kept .shirt { background: #b9c3c9; color: #2a3338; }
.token.kept .nameplate { background: #4a555c; }
.lock-badge {
  position: absolute; top: -7px; left: -10px; width: 22px; height: 22px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  background: #2a3338; color: #ffffff; box-shadow: 0 0 0 2px #ffffff;
}
tr.kept td { background: var(--surface-2); }
.legend .dot.kept { background: #b9c3c9; }
```
Badge positions on the shirt: armband top-right, injury/rotation flag bottom-left,
so the lock takes top-left. Check it doesn't collide on the 40px bench shirts; shrink
to 18px there if it does. The selected ring (`box-shadow` on `.shirt`) still shows on a
kept shirt.

**`MyTeam.tsx`**:
- `newPlayer(...)`: add `keep: false`. So `swapPlayer`'s replacement is not kept
  (decision 6); don't copy `old.keep`.
- New action:
  ```ts
  const toggleKeep = (id: number) => patch((d) => ({
    ...d, players: d.players.map((p) => (p.player_id === id ? { ...p, keep: !p.keep } : p)),
  }));
  ```
- `loadMatched` (paste a list): players already in the squad keep their flag:
  `keep: d.players.find((x) => x.player_id === m.player_id)?.keep ?? false` on each
  added player. Rename the local variable `keep` in that function to `others`: the old
  name now reads as the feature.
- `save()` body and `renameLineup` body: send `keep` per player (`p.keep ?? false` in
  `save`, `p.keep` in `renameLineup`).
- Unsaved hint: a `savedKeep` state holding the sorted kept ids as a string, set in
  `loadLineup` and after a successful `save`. When `draft.id` is set and the draft's
  key differs, show under the Save button:
  `<div className="alert warn">Keep changes aren't saved yet. Press Save team so plans use them.</div>`
- `keptIds = useMemo(() => new Set(draft.players.filter((p) => p.keep).map((p) => p.player_id)), [draft.players])`,
  passed to `<PitchView keptIds={keptIds} … />`.
- Selected-player card: after the Vice-captain button,
  ```tsx
  <button type="button" className="sm ghost" onClick={() => toggleKeep(selected.player_id)}
    aria-pressed={Boolean(selected.keep)}
    title="Plans never sell a kept player, even with a Wildcard or Free Hit">
    <LockIcon /> {selected.keep ? "Kept" : "Keep"}
  </button>
  ```
- Squad table: new first column **Keep** (move `paddingLeft: 20` from Pos to it). Cell:
  `onClick={(e) => e.stopPropagation()}` (as the Paid cell does) with
  `<input type="checkbox" checked={Boolean(p.keep)} onChange={() => toggleKeep(p.player_id)} aria-label={`Keep ${p.web_name}`} />`.
  Row class gets `kept` when kept. Empty-row `colSpan` 11 → 12; table `minWidth`
  960 → 1000. Card-head help: add "Tick Keep and plans will never sell that player."
- Squad facts card, after the chips field: when any are kept, a row
  "Kept: N player(s). Plans never sell them, even with a Wildcard or Free Hit." with a
  `sm ghost` **Clear** button that sets every `keep` to false.
- Under the pitch, when any are kept, one help line: "Grey shirt with a lock: kept.
  Plans never sell that player."

---

## K5 — Plans UI

**Files**: `frontend/src/pages/Suggestions.tsx`, `frontend/src/components/PlanDetail.tsx`.

- `Suggestions.tsx`: in the chip fieldset, under the existing help text, when the
  selected lineup has kept players:
  ```tsx
  <p className="help">Kept, never sold: {kept.map((p) => p.web_name).join(", ")}. <Link to="/team">Change in My team</Link></p>
  ```
  (`kept = lineup?.players.filter((p) => p.keep) ?? []`.) This is where the user picks
  Wildcard/Free Hit, so the constraint must be visible here.
- `PlanDetail.tsx`: `const keptIds = new Set(s.diff.kept_ids ?? [])`; pass
  `keptIds={keptIds}` to `PitchView`; in the legend, when `keptIds.size > 0`, add
  `<span><span className="dot kept" /> kept by you</span>`. Use the diff's ids (what
  the plan was made with), not the lineup's current flags. Plans made before this
  feature have no `kept_ids` and show nothing.

---

## K6 — Docs

- `README.md`, "How suggestions work": a new `### Keeping players` after "The three
  plans": what Keep does; it holds under every chip including Wildcard and Free Hit, and
  in the chip advice's rebuild estimate; a kept unavailable player stays on the bench at
  0 with no hit; Keep is saved with Save team and is per team (sandbox copies are
  separate); it doesn't force a player into the XI.
- `CHANGELOG.md`: a `## Unreleased` section above 1.1.0 with a "Keep players in
  lineup" entry. Don't renumber versions (1.1.0 is itself uncommitted).
- No `config.example.json` change.

---

## 7. Acceptance (manual, real app)

Restart the backend once (the migration runs at startup). Then:

1. My Team: tick Keep on three players, one of them expensive. They turn grey with a
   lock on the pitch/bench; their table rows are shaded; the facts card says "Kept: 3".
2. The unsaved hint appears. Save → it goes away. Reload → still kept.
3. Transfer plans: the kept list shows under the chip picker. Make plans with No chip,
   then Wildcard, then Free Hit. Every plan contains the three; on the plan pitch they're
   grey; "Why this plan" lists them.
4. Chip advice (No chip run) still shows a Wildcard/Free Hit rebuild gain. It now comes
   from a rebuild that holds the three (the K3 test proves the locks reach that solve;
   don't expect the number to always go down, because the base plan is constrained too).
5. "I've made these changes in FPL" → My Team still shows the three kept.
6. Keep a player flagged OUT: the plan holds him on the bench at 0, says why, and
   charges no −4 for him.
7. Duplicate the team: the sandbox has the same keeps; changing them there doesn't
   touch the real team.

---

## 8. Pitfalls

- **RNG order**: the unlocked path of `_hill_climb` must call `rng.randrange` exactly as
  today, or seeded results move and unrelated tests break.
- **`_rebuild_gain`** builds its own `SolveParams`; pass the locks there too.
- **Strict validation** rejects unavailable players; without `allow_unavailable` a kept
  injured player makes every plan fail with "solver produced invalid lineup".
- **`keep: None` on PUT** means "keep stored"; a plain `bool = False` default silently
  clears flags on rename.
- **Don't** add a `keep_ids` override to the `/suggestions/generate` body. The saved
  lineup is the single source of truth.
- **Don't** block manual swaps/removals of kept players in My Team.
- **Name clash**: `_apply_ep_floor(keep_ids=…)` means "current players". Use
  `locked_ids` in the solver.

---

## 9. Open questions for the user (not blocking)

1. **"Always start"**: should a kept player also be forced into the XI? Not built; Keep
   only protects him from being sold.
2. **Per-run override**: a "ignore my keeps" toggle on Transfer plans, to see what the
   locks cost. Not built. The chip advice gain already hints at it under Wildcard/Free Hit.
