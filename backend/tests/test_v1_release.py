"""v1.0 release fixes: security, money, chips, bench, dedupe, expiry, precision.

Each test pins one finding from the v1.0 review so it cannot silently regress.
"""
from __future__ import annotations

import json

import pytest
from conftest import RECENT, valid_squad
from fastapi.testclient import TestClient

from app import db as dbmod
from app.main import create_app
from app.optimizer.rules import validate_lineup
from app.optimizer.solver import SolveParams, solve
from app.optimizer.transfers import compute_diff
from app.signals import ingest, store

BODY = {
    "name": "Real team",
    "transfer_bank": 2,
    "chips": {"wildcard": 1, "freehit": 1, "bboost": 1, "triple_captain": 1},
    "players": [
        {"player_id": 1, "role": "starter", "is_captain": True},
        {"player_id": 2, "role": "bench", "bench_order": 3},       # GK sub NOT in slot 1
        {"player_id": 3, "role": "starter"},
        {"player_id": 5, "role": "starter"},
        {"player_id": 7, "role": "starter"},
        {"player_id": 9, "role": "starter"},
        {"player_id": 8, "role": "bench", "bench_order": 1},
        {"player_id": 13, "role": "starter", "is_vice_captain": True},
        {"player_id": 15, "role": "starter"},
        {"player_id": 17, "role": "starter"},
        {"player_id": 18, "role": "bench", "bench_order": 2},
        {"player_id": 16, "role": "bench", "bench_order": 4},
        {"player_id": 19, "role": "starter"},
        {"player_id": 23, "role": "starter"},
        {"player_id": 24, "role": "starter"},
    ],
}


@pytest.fixture()
def client(db_path):
    return TestClient(create_app())


# --------------------------------------------------------------------- security

def test_guard_refuses_foreign_host(db_path, monkeypatch):
    monkeypatch.setenv("FPL_ALLOWED_HOSTS", "")
    c = TestClient(create_app(), base_url="http://evil.example")
    assert c.get("/api/meta/season").status_code == 403
    ok = TestClient(create_app(), base_url="http://127.0.0.1:8000")
    assert ok.get("/api/meta/season").status_code == 200


def test_guard_refuses_cross_site_writes(client):
    r = client.post("/api/signals/clear", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/signals/clear", headers={"Origin": "null"})
    assert r.status_code == 403
    # the app's own pages (prod :8000, dev :5173) and tools without Origin pass
    assert client.post("/api/signals/clear", headers={"Origin": "http://127.0.0.1:8000"}).status_code == 200
    assert client.post("/api/signals/clear", headers={"Origin": "http://localhost:5173"}).status_code == 200
    assert client.post("/api/signals/clear").status_code == 200
    # reads are not origin-checked (CORS already hides the response)
    assert client.get("/api/meta/season", headers={"Origin": "https://evil.example"}).status_code == 200


def _write_config(tmp_path, monkeypatch, data: dict):
    from app import config as cfgmod
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", path)
    for k in ("LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    return path


def test_settings_never_echo_secrets_and_keep_them_on_save(client, tmp_path, monkeypatch):
    path = _write_config(tmp_path, monkeypatch, {
        "llm": {"api_key": "sk-local-secret", "model": "m"},
        "sources": {"reddit": {"mode": "oauth", "oauth_client_id": "cid",
                               "oauth_client_secret": "reddit-secret"}},
    })
    got = client.get("/api/settings").json()
    assert "sk-local-secret" not in json.dumps(got)
    assert "reddit-secret" not in json.dumps(got)
    assert got["reddit_status"]["secret_set"] is True
    assert got["llm_status"]["key_set"] is True
    # saving the redacted payload back keeps both secrets on disk
    body = {k: v for k, v in got.items() if k not in ("llm_status", "reddit_status", "pulp_available")}
    assert client.put("/api/settings", json=body).status_code == 200
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["llm"]["api_key"] == "sk-local-secret"
    assert on_disk["sources"]["reddit"]["oauth_client_secret"] == "reddit-secret"


def test_test_llm_never_sends_saved_key_to_another_url(client, tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {
        "llm": {"api_key": "sk-local-secret", "model": "m", "base_url": "http://localhost:8888/v1"}})
    from app.signals import llm_extractor

    async def boom(*a, **k):
        raise AssertionError("the saved key must not be sent to a draft URL")

    monkeypatch.setattr(llm_extractor, "test_llm_connection", boom)
    r = client.post("/api/settings/test-llm", json={"base_url": "https://attacker.example/v1"})
    assert r.status_code == 200
    assert r.json()["ok"] is False and "API key" in r.json()["error"]


# ------------------------------------------------------------------ lineups API

def test_lineup_bench_gk_slot_bank_zero_and_money(client):
    body = dict(BODY, transfer_bank=0, bank_money=15)
    r = client.post("/api/lineups", json=body)
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["transfer_bank"] == 0
    assert d["bank_money"] == 15 and d["budget_remaining"] == 15 and d["money_known"] is True
    bench = {p["player_id"]: p["bench_order"] for p in d["players"] if p["role"] == "bench"}
    assert bench[2] == 1                       # GK sub moved to slot 1
    assert [bench[8], bench[18], bench[16]] == [2, 3, 4]   # outfield order kept
    assert d["validation"]["valid"] is True


def test_free_transfers_roll_forward_at_the_deadline(client):
    lid = client.post("/api/lineups", json=dict(BODY, transfer_bank=0)).json()["id"]
    assert dbmod.query_one("SELECT bank_gw FROM lineups WHERE id = ?", (lid,))["bank_gw"] == 6
    # GW6's deadline passes: GW7 becomes next → +1 free transfer
    dbmod.execute("UPDATE events SET is_next = 0")
    dbmod.execute("INSERT INTO events (id, name, deadline_time, is_current, is_next, finished, "
                  "data_checked, released) VALUES (7, 'Gameweek 7', '2026-10-04T10:00:00Z', 0, 1, 0, 0, 1)")
    assert client.get(f"/api/lineups/{lid}").json()["transfer_bank"] == 1


def test_duplicate_keeps_purchase_prices_and_money(client):
    players = [dict(p, bought_cost=30) if p["player_id"] == 3 else p for p in BODY["players"]]
    lid = client.post("/api/lineups", json=dict(BODY, players=players, bank_money=7)).json()["id"]
    dup = client.post(f"/api/lineups/{lid}/duplicate").json()
    paid = {p["player_id"]: p["bought_cost"] for p in dup["players"]}
    assert paid[3] == 30 and dup["bank_money"] == 7


def test_only_one_chip_can_be_logged_per_gameweek(client):
    assert client.post("/api/lineups/chip-play", json={"gw": 6, "chip": "wildcard"}).status_code == 201
    r = client.post("/api/lineups/chip-play", json={"gw": 6, "chip": "triple_captain"})
    assert r.status_code == 409 and "one chip per gameweek" in r.text
    assert client.post("/api/lineups/chip-play", json={"gw": 7, "chip": "triple_captain"}).status_code == 201


def test_squad_over_100m_is_a_warning_for_a_real_team():
    squad = valid_squad()
    for p in squad:
        p["now_cost"] = 70             # 15 × £7.0m = £105m after price rises
    check = validate_lineup(squad, bank=1)
    assert check.valid
    assert any(e["code"] == "BUDGET_EXCEEDED" and e["severity"] == "warning" for e in check.errors)


# ----------------------------------------------------------------- suggestions

def test_holding_a_wildcard_does_not_rebuild_the_team(client):
    lid = client.post("/api/lineups", json=BODY).json()["id"]
    r = client.post("/api/suggestions/generate", json={"lineup_id": lid})
    assert r.status_code == 200, r.text
    for s in r.json()["suggestions"]:
        assert len(s["diff"]["transfers_in"]) <= 2, s["profile"]      # 2 free transfers
        assert s["diff"]["penalty_points"] == 0
        assert s["diff"]["chip_played"] is None
        assert sum(a["recommendation"] == "use" for a in s["chip_advice"]) <= 1


def test_differential_stays_within_free_transfers_when_floor_excludes_squad(db_path, cfg):
    """The v1.0 regression: the EP floor removed most of the user's squad, no
    plan fit the free transfers, and the fallback switched the limit off —
    14 transfers, −48 points. The floor now keeps the user's own players."""
    from conftest import valid_squad as vs
    cur_ids = [p["player_id"] for p in vs()]
    dbmod.execute("UPDATE players SET ep_next = 1.0 WHERE id IN (%s)" % ",".join(map(str, cur_ids[2:])))
    rows = {r["id"]: r for r in dbmod.query("SELECT id, web_name, now_cost FROM players")}
    current = [{"player_id": i, "web_name": rows[i]["web_name"], "now_cost": rows[i]["now_cost"],
                "bought_cost": rows[i]["now_cost"]} for i in cur_ids]
    s = solve(SolveParams(current_squad=current, bank=2, chips={}, target_gw=6,
                          profile="differential", cfg=cfg))
    d = compute_diff(current, s.squad, 2)
    assert len(d["transfers_in"]) <= 2
    assert d["penalty_points"] == 0


def test_solver_spends_only_money_in_the_bank(db_path, cfg):
    """With bank money known, a transfer may not cost more than bank + sale."""
    from conftest import valid_squad as vs
    cur = vs()
    rows = {r["id"]: r for r in dbmod.query("SELECT id, web_name, now_cost FROM players")}
    current = [{"player_id": p["player_id"], "web_name": rows[p["player_id"]]["web_name"],
                "now_cost": rows[p["player_id"]]["now_cost"],
                "bought_cost": rows[p["player_id"]]["now_cost"]} for p in cur]
    for bank_money in (0, 25):
        s = solve(SolveParams(current_squad=current, bank=5, chips={}, target_gw=6,
                              profile="max_ep", cfg=cfg, bank_money=bank_money))
        d = compute_diff(current, s.squad, 5, bank_money=bank_money)
        assert d["budget_after"] >= 0, (bank_money, d["budget_after"])


# ----------------------------------------------------------------- news ingest

def test_reddit_float_epoch_is_parsed():
    assert ingest.parse_published("1791209205.0") == "2026-10-05T14:06:45Z"
    assert ingest.parse_published("1791209205") == "2026-10-05T14:06:45Z"


def test_same_post_is_stored_once_and_gets_its_date(db_path):
    first = ingest.ingest_item("reddit", "abc", "thread", "Saliba back", "u", None, "post")
    again = ingest.ingest_item("reddit", "abc", "thread", "Saliba back", "u", "1791209205.0",
                               "post\n--- TOP COMMENTS ---\nnew comment")
    assert first and again is None
    rows = dbmod.query("SELECT published_at FROM raw_items WHERE source = 'reddit'")
    assert len(rows) == 1 and rows[0]["published_at"] == "2026-10-05T14:06:45Z"


def test_old_news_is_not_resurrected(db_path):
    n = store.save_signals([{
        "player_id": 1, "category": "injury", "sentiment": "negative", "confidence": 0.8,
        "summary": "old", "source": "bbc:old", "url": None,
        "published_at": "2025-01-01T00:00:00Z", "raw_item_id": None, "model": "llm:x"}])
    assert n == 0
    n = store.save_signals([{
        "player_id": 1, "category": "injury", "sentiment": "negative", "confidence": 0.8,
        "summary": "new", "source": "bbc:new", "url": None,
        "published_at": RECENT, "raw_item_id": None, "model": "llm:x"}])
    assert n == 1


def test_requeue_skipped_items(db_path):
    from app.signals.pipeline import requeue_skipped
    item = ingest.ingest_item("bbc", "x1", "article", "t", None, RECENT, "body")
    ingest.mark_processed(item, ["LLM skipped this pass: circuit breaker open (≥2 failures)"])
    store.save_signals([{
        "player_id": 1, "category": "injury", "sentiment": "negative", "confidence": 0.4,
        "summary": "kw", "source": "bbc:x1", "url": None, "published_at": RECENT,
        "raw_item_id": item, "model": "rules"}])
    out = requeue_skipped(7)
    assert out == {"requeued": 1, "signals_removed": 1}
    assert dbmod.query_one("SELECT processed FROM raw_items WHERE id = ?", (item,))["processed"] == 0


def test_dismiss_signal(client):
    store.save_signals([{
        "player_id": 1, "category": "injury", "sentiment": "negative", "confidence": 0.4,
        "summary": "wrong player", "source": "bbc:z", "url": None, "published_at": RECENT,
        "raw_item_id": None, "model": "rules"}])
    sid = dbmod.query_one("SELECT id FROM signals")["id"]
    assert client.delete(f"/api/signals/{sid}").status_code == 200
    assert dbmod.query_one("SELECT id FROM signals") is None


def test_sources_health_endpoint(client):
    d = client.get("/api/meta/sources").json()
    assert {s["source"] for s in d["sources"]} == {"fpl-official", "bbc", "espn", "reddit", "youtube"}
    assert "disable_thinking" in d["llm"]
