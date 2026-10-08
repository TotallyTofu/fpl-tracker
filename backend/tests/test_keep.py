"""Keep players in lineup (ADD-FEATURE-KEEP.MD): storage, solver locks, suggestions API."""
import random
import sqlite3

import pytest
from conftest import mkplayer, valid_squad
from fastapi.testclient import TestClient

from app import db as dbmod
from app.main import create_app
from app.optimizer.rules import SQUAD_COMP, validate_lineup
from app.optimizer.solver import (
    PROFILES,
    SolveParams,
    _greedy_seed,
    _precompute,
    _TransferCtx,
    _unavailable,
    build_universe,
    dedupe_profiles,
    solve,
)

VALID_IDS = (1, 2, 3, 5, 7, 9, 8, 13, 15, 17, 18, 16, 19, 23, 24)   # conftest.valid_squad()


@pytest.fixture()
def client(db_path):
    return TestClient(create_app())


def _current_squad(ids=VALID_IDS):
    rows = {r["id"]: r for r in dbmod.query("SELECT id, web_name, now_cost FROM players")}
    return [{"player_id": i, "web_name": rows[i]["web_name"],
             "now_cost": rows[i]["now_cost"], "bought_cost": rows[i]["now_cost"]}
            for i in ids]


def _body(keep_ids=(), name="Keep XI", **extra):
    """POST /api/lineups payload for the conftest valid squad."""
    layout = [
        (1, "starter", None, True, False), (2, "bench", 1, False, False),
        (3, "starter", None, False, False), (5, "starter", None, False, False),
        (7, "starter", None, False, False), (9, "starter", None, False, False),
        (8, "bench", 2, False, False), (13, "starter", None, False, True),
        (15, "starter", None, False, False), (17, "starter", None, False, False),
        (18, "bench", 3, False, False), (16, "bench", 4, False, False),
        (19, "starter", None, False, False), (23, "starter", None, False, False),
        (24, "starter", None, False, False),
    ]
    players = []
    for pid, role, order, cap, vc in layout:
        p = {"player_id": pid, "role": role, "is_captain": cap, "is_vice_captain": vc}
        if order:
            p["bench_order"] = order
        if pid in keep_ids:
            p["keep"] = True
        players.append(p)
    return {"name": name, "transfer_bank": 1,
            "chips": {"wildcard": 1, "freehit": 1, "bboost": 1, "triple_captain": 1},
            "players": players, **extra}


def _kept(lineup):
    return {p["player_id"] for p in lineup["players"] if p["keep"]}


# ----------------------------------------------------------------- K1: storage


def test_keep_column_migration(tmp_path):
    """init_db adds lineup_players.keep to a database made before the feature."""
    p = tmp_path / "old.db"
    conn = sqlite3.connect(p)
    conn.execute(
        """CREATE TABLE lineup_players (
             lineup_id INTEGER NOT NULL, player_id INTEGER NOT NULL, role TEXT NOT NULL,
             bench_order INTEGER, is_captain INTEGER NOT NULL DEFAULT 0,
             is_vice_captain INTEGER NOT NULL DEFAULT 0, bought_cost INTEGER,
             PRIMARY KEY (lineup_id, player_id))"""
    )
    conn.execute("INSERT INTO lineup_players (lineup_id, player_id, role) VALUES (1, 1, 'starter')")
    conn.commit()
    conn.close()

    dbmod.init_db(p)
    conn = sqlite3.connect(p)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(lineup_players)")}
    assert "keep" in cols
    assert conn.execute("SELECT keep FROM lineup_players").fetchone()[0] == 0
    conn.close()


def test_fresh_db_has_keep_column(db_path):
    cols = {r[1] for r in sqlite3.connect(str(db_path)).execute("PRAGMA table_info(lineup_players)")}
    assert "keep" in cols


def test_keep_roundtrip_as_booleans(client):
    r = client.post("/api/lineups", json=_body(keep_ids=(1, 15)))
    assert r.status_code == 201, r.text
    lineup = client.get(f"/api/lineups/{r.json()['id']}").json()
    flags = {p["player_id"]: p["keep"] for p in lineup["players"]}
    assert all(type(v) is bool for v in flags.values())          # JSON booleans, not 0/1
    assert {pid for pid, v in flags.items() if v} == {1, 15}


def test_put_without_keep_leaves_flags_alone(client):
    lid = client.post("/api/lineups", json=_body(keep_ids=(1, 15))).json()["id"]
    r = client.put(f"/api/lineups/{lid}", json=_body(name="Renamed"))   # no keep fields at all
    assert r.status_code == 200, r.text
    assert _kept(r.json()) == {1, 15}


def test_put_with_keep_false_clears_one(client):
    lid = client.post("/api/lineups", json=_body(keep_ids=(1, 15))).json()["id"]
    body = _body(keep_ids=(1,))
    for p in body["players"]:
        if p["player_id"] == 15:
            p["keep"] = False
    r = client.put(f"/api/lineups/{lid}", json=body)
    assert _kept(r.json()) == {1}


def test_duplicate_copies_keep(client):
    lid = client.post("/api/lineups", json=_body(keep_ids=(2, 8))).json()["id"]
    dup = client.post(f"/api/lineups/{lid}/duplicate")
    assert dup.status_code == 201, dup.text
    assert _kept(dup.json()) == {2, 8}
    assert dup.json()["id"] != lid


# ------------------------------------------------------------------ K2: solver


def _solve(profile="max_ep", locked=frozenset(), chip=None, bank=1, cfg=None, **kw):
    return solve(SolveParams(
        current_squad=_current_squad(), bank=bank,
        chips={"wildcard": 1, "freehit": 1}, target_gw=6, profile=profile, cfg=cfg,
        chip_played=chip, locked_ids=frozenset(locked), **kw))


def _insert_edge_keeper():
    """Player 99: t1 GK, ep 11.0 → +2.1 over player 1 (see test_solver)."""
    dbmod.execute(
        "INSERT INTO players (id, web_name, element_type, team, now_cost, ep_next, "
        "selected_by_percent, status, can_select, removed, chance_of_playing_next_round, "
        "form, fetched_at) VALUES (99, 'Edge Keeper', 1, 1, 45, 11.0, 10.0, 'a', 1, 0, 100, "
        "5.0, '2026-09-19T12:00:00Z')")


def test_locked_player_not_sold_for_better_swap(db_path, cfg):
    _insert_edge_keeper()
    free = _solve(cfg=cfg)
    assert 99 in {p["player_id"] for p in free.squad}          # control: the swap is worth it
    held = _solve(locked={1}, cfg=cfg)
    ids = {p["player_id"] for p in held.squad}
    assert 1 in ids and 99 not in ids


@pytest.mark.parametrize("chip", ["wildcard", "freehit"])
@pytest.mark.parametrize("profile", PROFILES)
def test_locks_hold_under_transfer_chips(db_path, cfg, chip, profile):
    """Lock exactly the players an unlocked rebuild sells: they must all stay."""
    free = _solve(profile=profile, chip=chip, bank=1, cfg=cfg)
    sold = set(VALID_IDS) - {p["player_id"] for p in free.squad}
    assert sold, "the unlocked rebuild should sell someone, or this test proves nothing"
    held = _solve(profile=profile, chip=chip, bank=1, cfg=cfg, locked=sold)
    assert sold <= {p["player_id"] for p in held.squad}
    assert len(held.squad) == 15 and len(held.xi) == 11


def test_all_15_locked_makes_no_transfers(db_path, cfg):
    s = _solve(chip="wildcard", locked=VALID_IDS, cfg=cfg)
    assert {p["player_id"] for p in s.squad} == set(VALID_IDS)
    assert any(n.startswith("You kept all 15 players") for n in s.notes)


def test_kept_note_lists_names(db_path, cfg):
    s = _solve(locked={1, 3}, cfg=cfg)
    assert any(n.startswith("Kept by you, never sold: ") and "Goal One" in n and "Def A One" in n
               for n in s.notes)
    assert not _solve(cfg=cfg).notes[0].startswith("Kept by you")      # nothing kept, no note


def test_kept_unavailable_player_stays_on_bench(db_path, cfg):
    from app.optimizer.transfers import compute_diff
    dbmod.execute("UPDATE players SET status = 'u' WHERE id = 23")
    # control: not kept → he is a forced replacement
    free = _solve(cfg=cfg)
    assert 23 not in {p["player_id"] for p in free.squad}
    # kept → he stays, projected 0, on the bench, and no hit is charged for him
    s = _solve(locked={23}, cfg=cfg)
    mine = next(p for p in s.squad if p["player_id"] == 23)
    assert mine["role"] == "bench" and mine["ep"] == 0
    assert compute_diff(_current_squad(), s.squad, 1)["penalty_points"] == 0
    assert "Fwd C One is kept although unavailable (projected 0)." in s.notes


def test_unavailable_helper_matches_universe(db_path, cfg):
    """_unavailable restates build_universe's WHERE clause: they must agree."""
    dbmod.execute("UPDATE players SET status = 'u' WHERE id = 23")
    dbmod.execute("UPDATE players SET status = 's' WHERE id = 24")
    dbmod.execute("UPDATE players SET can_select = 0 WHERE id = 25")
    dbmod.execute("UPDATE players SET removed = 1 WHERE id = 26")
    dbmod.execute("UPDATE players SET status = 'd', chance_of_playing_next_round = 0 WHERE id = 27")
    dbmod.execute("UPDATE players SET status = 'd', chance_of_playing_next_round = 50 WHERE id = 28")
    in_universe = {p["id"] for p in build_universe(6, cfg)}
    rows = dbmod.query("SELECT * FROM players")
    assert {r["id"] for r in rows if _unavailable(r)} == {23, 24, 25, 26, 27}
    assert all(_unavailable(r) == (r["id"] not in in_universe) for r in rows)


def test_build_universe_include_ids(db_path, cfg):
    dbmod.execute("UPDATE players SET status = 'u' WHERE id = 23")
    assert 23 not in {p["id"] for p in build_universe(6, cfg)}
    assert 23 in {p["id"] for p in build_universe(6, cfg, include_ids=frozenset({23}))}


def _precomputed_universe(cfg):
    return [_precompute(p, cfg, {}, {}, True) for p in build_universe(6, cfg)]


def test_greedy_seed_includes_locked(db_path, cfg):
    universe = _precomputed_universe(cfg)
    locked = frozenset({10})                                   # the lowest-EP defender
    seen_full = 0
    for seed in range(8):
        squad = _greedy_seed(universe, cfg, random.Random(seed), 1000, locked,
                             lambda p: p["now_cost"])
        assert 10 in {p["id"] for p in squad}
        assert len({p["id"] for p in squad}) == len(squad)
        if len(squad) == 15:
            seen_full += 1
            for pos, n in SQUAD_COMP.items():
                assert sum(1 for p in squad if p["element_type"] == pos) == n
            assert max(sum(1 for p in squad if p["team"] == t) for t in {p["team"] for p in squad}) <= 3
    assert seen_full, "no greedy seed produced a full squad"
    # unlocked default is unchanged
    assert _greedy_seed(universe, cfg, random.Random(3), 1000) == \
        _greedy_seed(universe, cfg, random.Random(3), 1000, frozenset(), None)


def test_low_own_swap_skips_locked(db_path, cfg):
    """The convergence swap replaces the cheapest same-position player; a kept
    one is skipped, so the next-cheapest goes instead."""
    from test_solver import _mk_lineup
    squad = valid_squad()
    universe = [
        {"id": e["player_id"], "ep": {3: 10.0, 1: 9.0}.get(e["player_id"], 8.0),
         "selected_by_percent": 99.0, "element_type": e["element_type"],
         "team": e["team"], "now_cost": e["now_cost"], "web_name": e["web_name"],
         "status": "a", "can_select": 1}
        for e in squad
    ]
    universe.append({"id": 30, "ep": 50.0, "selected_by_percent": 5.0, "element_type": 4,
                     "team": 6, "now_cost": 40, "web_name": "Diff Fwd", "status": "a",
                     "can_select": 1})
    ids = frozenset(e["player_id"] for e in squad)

    def run(locked):
        tctx = _TransferCtx(cur_ids=ids, bank=1, chip_covers=True, profile="safe",
                            locked_ids=frozenset(locked))
        out = dedupe_profiles({"max_ep": _mk_lineup(squad, universe),
                               "safe": _mk_lineup(squad, universe)}, tctx=tctx)
        return {e["player_id"] for e in out["safe"].squad}

    assert 30 in run(set()) and 19 not in run(set())           # control: 19 is the cheapest FWD
    held = run({19})
    assert 19 in held and 30 in held and 24 not in held


def test_validate_strict_allows_kept_unavailable():
    squad = valid_squad()
    squad[2] = mkplayer(3, 2, 1, 40, status="u")
    strict = validate_lineup(squad, 1, strict=True)
    assert not strict.valid and any(e["code"] == "PLAYER_UNAVAILABLE" for e in strict.errors)
    kept = validate_lineup(squad, 1, strict=True, allow_unavailable=frozenset({3}))
    assert kept.valid
    assert any(e["code"] == "PLAYER_UNAVAILABLE" and e["severity"] == "warning" for e in kept.errors)
    # a different player's allowance does not cover him
    assert not validate_lineup(squad, 1, strict=True, allow_unavailable=frozenset({1})).valid


def test_locks_outside_the_squad_are_ignored(db_path, cfg):
    s = _solve(locked={1, 28}, cfg=cfg)           # 28 is not in the current squad
    assert 1 in {p["player_id"] for p in s.squad}


# ------------------------------------------------------- K3: suggestions API


def test_generate_with_wildcard_keeps_players(client):
    lid = client.post("/api/lineups", json=_body(keep_ids=(2, 8))).json()["id"]
    r = client.post("/api/suggestions/generate", json={"lineup_id": lid, "chip": "wildcard"})
    assert r.status_code == 200, r.text
    suggestions = r.json()["suggestions"]
    assert suggestions
    for s in suggestions:
        assert {2, 8} <= {p["player_id"] for p in s["lineup"]["squad"]}, s["profile"]
        assert s["diff"]["kept_ids"] == [2, 8]
        assert any(n.startswith("Kept by you") for n in s["rationale"]["notes"])


def test_generate_without_keeps_has_empty_kept_ids(client):
    lid = client.post("/api/lineups", json=_body()).json()["id"]
    r = client.post("/api/suggestions/generate", json={"lineup_id": lid})
    assert r.status_code == 200, r.text
    for s in r.json()["suggestions"]:
        assert s["diff"]["kept_ids"] == []
        assert not any(n.startswith("Kept by you") for n in s["rationale"]["notes"])


def test_rebuild_gain_solve_honours_locks(client, monkeypatch):
    import app.api.suggestions as sug
    real, calls = sug.solve, []

    def spy(params):
        calls.append(params)
        return real(params)

    monkeypatch.setattr(sug, "solve", spy)
    lid = client.post("/api/lineups", json=_body(keep_ids=(2, 8))).json()["id"]
    r = client.post("/api/suggestions/generate", json={"lineup_id": lid})   # no chip
    assert r.status_code == 200, r.text
    rebuilds = [c for c in calls if c.chip_played]
    assert rebuilds, "a playable wildcard/free hit should trigger the rebuild-gain solve"
    assert len(calls) > len(rebuilds)
    assert all(c.locked_ids == frozenset({2, 8}) for c in calls)


def test_apply_keeps_flags(client):
    lid = client.post("/api/lineups", json=_body(keep_ids=(2, 8))).json()["id"]
    gen = client.post("/api/suggestions/generate", json={"lineup_id": lid}).json()
    sid = next(s["id"] for s in gen["suggestions"] if s["profile"] == "max_ep")
    assert client.post(f"/api/suggestions/{sid}/apply").status_code == 200
    after = client.get(f"/api/lineups/{lid}").json()
    assert _kept(after) == {2, 8}
    assert len(after["players"]) == 15
