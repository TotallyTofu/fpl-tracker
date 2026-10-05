"""API integration tests against a temp DB (no network — lifespan not entered)."""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app

VALID_BODY = {
    "name": "Test XI",
    "transfer_bank": 3,
    "chips": {"wildcard": 2, "freehit": 2, "bboost": 2, "triple_captain": 2},
    "players": [
        {"player_id": 1, "role": "starter", "is_captain": True},
        {"player_id": 2, "role": "bench", "bench_order": 1},
        {"player_id": 3, "role": "starter"},
        {"player_id": 5, "role": "starter"},
        {"player_id": 7, "role": "starter"},
        {"player_id": 9, "role": "starter"},
        {"player_id": 8, "role": "bench", "bench_order": 2},
        {"player_id": 13, "role": "starter", "is_vice_captain": True},
        {"player_id": 15, "role": "starter"},
        {"player_id": 17, "role": "starter"},
        {"player_id": 18, "role": "bench", "bench_order": 3},
        {"player_id": 16, "role": "bench", "bench_order": 4},
        {"player_id": 19, "role": "starter"},
        {"player_id": 23, "role": "starter"},
        {"player_id": 24, "role": "starter"},
    ],
}


@pytest.fixture()
def client(db_path):
    return TestClient(create_app())


def test_meta_season(client):
    r = client.get("/api/meta/season")
    assert r.status_code == 200
    d = r.json()
    assert d["current_gw"] == 5
    assert d["next_gw"] == 6
    assert d["deadline"] == "2026-09-27T10:00:00Z"
    assert len(d["fixtures_next_gw"]) == 2
    assert d["fixtures_next_gw"][0]["home_name"] == "Alpha FC"
    chips = {w["chip"] for w in d["chip_windows"]}
    assert {"wildcard", "freehit", "bboost", "triple_captain"} <= chips


def test_meta_health(client):
    r = client.get("/api/meta/health")
    assert r.status_code == 200
    d = r.json()
    assert d["db_ok"] is True
    assert "last_polls" in d
    assert "schema_drift" in d


def test_players_search(client):
    r = client.get("/api/players", params={"search": "goal"})
    assert r.status_code == 200
    names = {p["web_name"] for p in r.json()["players"]}
    assert {"Goal One", "Goal Two"} <= names


def test_players_filters(client):
    r = client.get("/api/players", params={"pos": 4, "team": 3})
    assert r.status_code == 200
    for p in r.json()["players"]:
        assert p["element_type"] == 4
        assert p["team"] == 3


def test_players_has_signal_filter(client):
    """A12: has_signal=true must match unexpired signals — expires_at is NOT
    NULL, so the old `expires_at IS NULL` filter could never match."""
    from app import db as dbmod
    from app.signals import store

    store.save_signals([
        {"player_id": 1, "category": "injury", "sentiment": "negative",
         "confidence": 0.5, "summary": "Goal One ruled out.", "source": "bbc:x1",
         "url": None, "published_at": "2026-09-19T12:00:00Z", "raw_item_id": None,
         "model": "rules"},
    ])
    dbmod.execute(
        "INSERT INTO signals (player_id, category, sentiment, confidence, summary, source, "
        "published_at, retrieved_at, expires_at, raw_item_id, model) "
        "VALUES (2, 'injury', 'negative', 0.5, 'expired', 'bbc:x2', "
        "'2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z', '2026-09-05T00:00:00Z', NULL, 'rules')"
    )
    r = client.get("/api/players", params={"has_signal": True})
    assert r.status_code == 200
    ids = {p["id"] for p in r.json()["players"]}
    assert ids == {1}


def test_create_and_get_lineup(client):
    r = client.post("/api/lineups", json=VALID_BODY)
    assert r.status_code == 201, r.text
    lid = r.json()["id"]
    assert r.json()["validation"]["valid"] is True

    r = client.get(f"/api/lineups/{lid}")
    assert r.status_code == 200
    d = r.json()
    assert len(d["players"]) == 15
    assert d["budget_remaining"] == 1000 - 805
    assert any(p["is_captain"] for p in d["players"])


def test_list_and_set_current(client):
    lid = client.post("/api/lineups", json=VALID_BODY).json()["id"]
    r = client.post(f"/api/lineups/{lid}/set-current")
    assert r.status_code == 200
    r = client.get("/api/lineups")
    assert any(l["id"] == lid and l["is_current"] for l in r.json()["lineups"])


def test_update_lineup(client):
    lid = client.post("/api/lineups", json=VALID_BODY).json()["id"]
    body = dict(VALID_BODY, name="Renamed")
    r = client.put(f"/api/lineups/{lid}", json=body)
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed"


def test_delete_lineup(client):
    lid = client.post("/api/lineups", json=VALID_BODY).json()["id"]
    r = client.delete(f"/api/lineups/{lid}")
    assert r.status_code == 200
    assert client.get(f"/api/lineups/{lid}").status_code == 404


def test_invalid_lineup_422_with_errors(client):
    body = dict(VALID_BODY)
    body["players"] = [p for p in VALID_BODY["players"] if p["player_id"] != 24]  # 14 players
    r = client.post("/api/lineups", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert any(e["code"] == "SQUAD_SIZE" for e in detail)


def test_unknown_player_422(client):
    body = dict(VALID_BODY)
    body["players"] = [dict(p, player_id=9999) for p in VALID_BODY["players"]]
    r = client.post("/api/lineups", json=body)
    assert r.status_code == 422


def test_match_names_endpoint(client):
    r = client.post("/api/lineups/match-names", json={"names": ["Goal One", "Zzz Qqq"]})
    assert r.status_code == 200
    matches = r.json()["matches"]
    assert matches[0]["matched"]["player_id"] == 1
    assert matches[1]["matched"] is None


def test_settings_get(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    d = r.json()
    assert "weights" in d["optimizer"] or "llm" in d


@pytest.fixture()
def hermetic_config(monkeypatch, tmp_path):
    """Point config.json at a temp file and clear LLM env overrides so the
    settings tests never touch the real config.json / .env values."""
    from app import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    for var in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    return cfgmod


def test_get_settings_redacts_api_key(client, hermetic_config):
    """A3: GET /api/settings returns llm.api_key == '' and key_set reflects
    the stored key."""
    cfg = hermetic_config.load_config()
    cfg.llm.api_key = "sk-saved-1"
    hermetic_config.save_config(cfg)
    d = client.get("/api/settings").json()
    assert d["llm"]["api_key"] == ""
    assert d["llm_status"]["key_set"] is True


def test_put_settings_empty_key_keeps_existing(client, hermetic_config):
    """A3: PUT with api_key '' means 'keep the saved key' — it must not clear
    it, and the response must not echo the secret either."""
    cfg = hermetic_config.load_config()
    cfg.llm.api_key = "sk-saved-2"
    hermetic_config.save_config(cfg)

    d = client.get("/api/settings").json()
    assert d["llm"]["api_key"] == ""
    d["llm"]["api_key"] = ""          # explicit "keep"
    r = client.put("/api/settings", json=d)
    assert r.status_code == 200
    assert r.json()["llm"]["api_key"] == ""
    assert hermetic_config.load_config().llm.api_key == "sk-saved-2"
    assert client.get("/api/settings").json()["llm_status"]["key_set"] is True


def test_put_settings_response_matches_get_shape(client, hermetic_config):
    """A17: the PUT response has exactly GET's shape — llm_status and
    pulp_available must not blank out after a save."""
    d = client.get("/api/settings").json()
    r = client.put("/api/settings", json=d)
    assert r.status_code == 200
    out = r.json()
    assert set(out) == set(d)
    assert "llm_status" in out and "pulp_available" in out
    assert out["llm"]["api_key"] == ""


def test_test_llm_uses_body_override(client, monkeypatch):
    """A4: POST /api/settings/test-llm honours the draft body — a base_url the
    saved config does not know must be the one hit. (A20: the endpoint now
    routes through llm_extractor.test_llm_connection, so the transport mock
    patches llm_extractor.httpx.)"""
    import httpx

    from app.signals import llm_extractor

    calls: list[httpx.Request] = []

    class _MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(
                200,
                json={"model": "mock-model", "choices": [{"message": {"content": "ok"}}]},
            )

    real_client = httpx.AsyncClient

    def factory(*a, **kw):
        kw.pop("timeout", None)
        return real_client(transport=_MockTransport())

    monkeypatch.setattr(llm_extractor.httpx, "AsyncClient", factory)

    r = client.post(
        "/api/settings/test-llm",
        json={"base_url": "http://mock-host/v1", "api_key": "sk-draft", "model": "mock-model"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert len(calls) == 1
    req = calls[0]
    assert req.url.host == "mock-host"
    assert req.headers["authorization"] == "Bearer sk-draft"
    import json as _json

    assert _json.loads(req.read())["model"] == "mock-model"


def test_test_llm_empty_content_is_not_ok(client, monkeypatch):
    """A5: a 200 with empty content (model spent its budget on
    reasoning_content) must not report ok: True."""
    import httpx

    from app.signals import llm_extractor

    class _MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"model": "mock-model", "choices": [{"message": {
                    "content": "",
                    "reasoning_content": "The user wants a single " * 20,
                }}]},
            )

    real_client = httpx.AsyncClient

    def factory(*a, **kw):
        kw.pop("timeout", None)
        return real_client(transport=_MockTransport())

    monkeypatch.setattr(llm_extractor.httpx, "AsyncClient", factory)

    r = client.post(
        "/api/settings/test-llm",
        json={"base_url": "http://mock-host/v1", "api_key": "sk-draft", "model": "mock-model"},
    )
    assert r.status_code == 200
    out = r.json()
    assert out["ok"] is False
    assert "empty content" in out["error"]
    assert "reasoning_content" in out["error"]
    assert "raise llm.max_tokens" in out["error"]


def test_test_llm_probe_uses_configured_timeout(client, monkeypatch, hermetic_config):
    """A20: the probe timeout is llm.timeout_sec (not the old hardcoded 20) —
    a slow endpoint that fits the configured budget still returns ok."""
    import httpx

    from app.signals import llm_extractor

    cfg = hermetic_config.load_config()
    cfg.llm.timeout_sec = 42
    hermetic_config.save_config(cfg)

    captured: dict = {}

    class _MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"model": "mock-model", "choices": [{"message": {"content": "ok"}}]},
            )

    real_client = httpx.AsyncClient

    def factory(*a, **kw):
        captured["timeout"] = kw.get("timeout")
        kw.pop("timeout", None)
        return real_client(transport=_MockTransport())

    monkeypatch.setattr(llm_extractor.httpx, "AsyncClient", factory)

    r = client.post(
        "/api/settings/test-llm",
        json={"base_url": "http://mock-host/v1", "api_key": "sk-draft", "model": "mock-model"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # A20 rev 2: the read phase carries the configured budget; connect stays
    # tight so a blackholed host fails fast instead of hanging the button.
    to = captured["timeout"]
    assert to.read == 42
    assert to.connect == 10.0


def test_test_llm_probe_sends_small_budget(client, monkeypatch):
    """A20: the probe requests a 256-token budget, not the old 8 — enough for
    a reasoning model to answer inside the budget."""
    import httpx
    import json as _json

    from app.signals import llm_extractor

    calls: list[httpx.Request] = []

    class _MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(
                200,
                json={"model": "mock-model", "choices": [{"message": {"content": "ok"}}]},
            )

    real_client = httpx.AsyncClient

    def factory(*a, **kw):
        kw.pop("timeout", None)
        return real_client(transport=_MockTransport())

    monkeypatch.setattr(llm_extractor.httpx, "AsyncClient", factory)

    r = client.post(
        "/api/settings/test-llm",
        json={"base_url": "http://mock-host/v1", "api_key": "sk-draft", "model": "mock-model"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert _json.loads(calls[0].read())["max_tokens"] == 256


def test_test_llm_empty_content_reports_finish_reason(client, monkeypatch):
    """A20: finish_reason is surfaced in the error — a truncated empty reply
    says so explicitly."""
    import httpx

    from app.signals import llm_extractor

    class _MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"model": "mock-model",
                      "choices": [{"message": {
                          "content": "",
                          "reasoning_content": "thinking..." * 50,
                      }, "finish_reason": "length"}]},
            )

    real_client = httpx.AsyncClient

    def factory(*a, **kw):
        kw.pop("timeout", None)
        return real_client(transport=_MockTransport())

    monkeypatch.setattr(llm_extractor.httpx, "AsyncClient", factory)

    r = client.post(
        "/api/settings/test-llm",
        json={"base_url": "http://mock-host/v1", "api_key": "sk-draft", "model": "mock-model"},
    )
    assert r.status_code == 200
    out = r.json()
    assert out["ok"] is False
    assert "finish_reason=length" in out["error"]


def test_refresh_unknown_source_404(client):
    r = client.post("/api/refresh/nope")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# M4 T4.2 / T4.3 — apply suggestion, chip-play log, lineup kind + duplicate
# ---------------------------------------------------------------------------

def test_suggestions_apply_endpoint(client):
    lid = client.post("/api/lineups", json=VALID_BODY).json()["id"]
    r = client.post("/api/suggestions/generate", json={"lineup_id": lid})
    assert r.status_code == 200, r.text
    sugs = r.json()["suggestions"]
    assert sugs
    sug = next(s for s in sugs if s["profile"] == "max_ep")
    bank_before = VALID_BODY["transfer_bank"]

    r = client.post(f"/api/suggestions/{sug['id']}/apply")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] == sug["id"]
    assert body["bank_after"] == sug["diff"]["bank_after"]

    # lineup bank updated
    r = client.get(f"/api/lineups/{lid}")
    assert r.json()["transfer_bank"] == sug["diff"]["bank_after"]

    # idempotent second call
    r = client.post(f"/api/suggestions/{sug['id']}/apply")
    assert r.status_code == 200
    assert r.json().get("already") is True

    assert bank_before >= 1  # sanity: bank was in range


def test_chip_play_endpoints(client):
    r = client.post("/api/lineups/chip-play", json={"gw": 5, "chip": "freehit"})
    assert r.status_code == 201, r.text

    r = client.get("/api/lineups/chip-plays", params={"gw": 5})
    assert r.status_code == 200
    assert any(p["gw"] == 5 and p["chip"] == "freehit" for p in r.json()["chip_plays"])

    r = client.delete("/api/lineups/chip-play", params={"gw": 5, "chip": "freehit"})
    assert r.status_code == 200
    assert r.json()["deleted"] is True

    # second delete: nothing left
    r = client.delete("/api/lineups/chip-play", params={"gw": 5, "chip": "freehit"})
    assert r.json()["deleted"] is False

    # unknown chip rejected
    r = client.post("/api/lineups/chip-play", json={"gw": 5, "chip": "time_travel"})
    assert r.status_code == 422


def test_lineup_kind_default_and_duplicate(client):
    lid = client.post("/api/lineups", json=VALID_BODY).json()["id"]
    r = client.get(f"/api/lineups/{lid}")
    assert r.json()["kind"] == "current"

    r = client.post(f"/api/lineups/{lid}/duplicate")
    assert r.status_code == 201, r.text
    dup = r.json()
    assert dup["kind"] == "test"
    assert dup["is_current"] == 0
    assert len(dup["players"]) == 15
    assert dup["transfer_bank"] == VALID_BODY["transfer_bank"]

    # explicit kind on create
    body = dict(VALID_BODY, name="Sandbox", kind="test")
    r = client.post("/api/lineups", json=body)
    assert r.status_code == 201
    assert r.json()["kind"] == "test"


def test_suggestion_payload_with_fh_ban(client):
    """§6.3: with a Free-Hit ban row (played in GW5), the generated payload
    must be honest: diff.chip_covers is false, diff.bank_before is present,
    projected_points carries penalty_points + net_after_transfers, chip_advice
    warns the Free Hit is banned for GW6, and the bank is never reduced by a
    penalty."""
    from app.db import execute
    execute("INSERT INTO chip_plays_log (lineup_id, gw, chip, played_at) "
            "VALUES (NULL, 5, 'freehit', '2026-09-19T12:00:00Z')")
    body = dict(VALID_BODY, chips={"freehit": 1, "bboost": 2, "triple_captain": 2})
    lid = client.post("/api/lineups", json=body).json()["id"]
    r = client.post("/api/suggestions/generate", json={"lineup_id": lid})
    assert r.status_code == 200, r.text
    sug = next(x for x in r.json()["suggestions"] if x["profile"] == "max_ep")
    diff, proj = sug["diff"], sug["projected_points"]
    assert diff["chip_covers"] is False
    assert diff["bank_before"] == 3
    assert "penalty_points" in proj and "net_after_transfers" in proj
    assert proj["net_after_transfers"] == round(proj["adjusted"] - proj["penalty_points"], 1)
    fh = next(a for a in sug["chip_advice"] if a["chip"] == "freehit")
    assert fh["recommendation"] == "skip"
    assert "Free Hit" in fh["reason"] and "GW 5" in fh["reason"]
    # bank drops only by transfers used — a penalty is never charged from it
    assert diff["bank_after"] == max(1, 3 - diff["free_transfers_used"])


def test_generate_honours_target_gw(client):
    """A13: an explicit target_gw is honoured end-to-end — response, stored
    row, and the Free-Hit ban (evaluated for target_gw - 1)."""
    from app.db import execute, query_one

    # fixture season only knows GW 5-6; extend it so GW 5 is a legal target
    for gw in (7, 8, 9, 10):
        execute(
            "INSERT INTO events (id, name, deadline_time, is_current, is_next, "
            "finished, data_checked, released) VALUES (?,?,?,0,0,0,0,1)",
            (gw, f"Gameweek {gw}", "2026-10-03T10:00:00Z"),
        )
    # A Free Hit played in GW 4 must ban the Free Hit when solving for GW 5.
    execute("INSERT INTO chip_plays_log (lineup_id, gw, chip, played_at) "
            "VALUES (NULL, 4, 'freehit', '2026-09-19T12:00:00Z')")
    lid = client.post("/api/lineups", json=VALID_BODY).json()["id"]
    r = client.post("/api/suggestions/generate", json={"lineup_id": lid, "target_gw": 5})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["target_gw"] == 5
    for s in d["suggestions"]:
        assert s["target_gw"] == 5
    row = query_one("SELECT target_gw FROM suggestions WHERE lineup_id = ? "
                    "ORDER BY id DESC LIMIT 1", (lid,))
    assert row["target_gw"] == 5
    # Free-Hit ban evaluated for GW 4 (target_gw - 1), not GW 5 (next_gw - 1)
    sug = next(s for s in d["suggestions"] if s["profile"] == "max_ep")
    fh = next(a for a in sug["chip_advice"] if a["chip"] == "freehit")
    assert fh["recommendation"] == "skip"
    assert "GW 4" in fh["reason"]


def test_apply_rejects_cap_exceeded(client):
    """§6.3 (D5): a suggestion whose diff exceeds the 20-transfer cap without a
    chip covering the GW is invalid in FPL — apply must 422 and leave the
    bank untouched."""
    import json
    from app.db import execute
    body = dict(VALID_BODY, chips={"wildcard": 0, "freehit": 0, "bboost": 2, "triple_captain": 2})
    lid = client.post("/api/lineups", json=body).json()["id"]
    diff = {
        "transfers_in": [{"player_id": 100 + i, "web_name": f"In{i}", "cost": 50} for i in range(21)],
        "transfers_out": [{"player_id": 300 + i, "web_name": f"Out{i}", "sell_value": 50} for i in range(21)],
        "cost_delta": 0,
        "total_cost_after": 800,
        "free_transfers_used": 20,
        "bank_after": 1,
        "penalty_points": 4,
        "chip_covers": False,
        "bank_before": 3,
        "transfer_cap_exceeded": True,
    }
    sid = execute(
        """INSERT INTO suggestions (lineup_id, profile, variant_of, generated_at, target_gw,
           projected_points, objective, diff, chip_advice, rationale, raw_lineup)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (lid, "max_ep", None, "2026-09-19T12:00:00Z", 6,
         json.dumps({"baseline": 90.0, "adjusted": 94.0, "with_captain": 108.0,
                     "penalty_points": 4, "net_after_transfers": 90.0}),
         94.0, json.dumps(diff), json.dumps([]),
         json.dumps({"per_player": {}, "notes": []}), json.dumps([])),
    )
    r = client.post(f"/api/suggestions/{sid}/apply")
    assert r.status_code == 422
    assert "cap" in r.json()["detail"]
    row = client.get(f"/api/lineups/{lid}").json()
    assert row["transfer_bank"] == 3


# ---------------------------------------------------------------------------
# FIX.MD Part 3 — T3: applied/played chips must leave the in-hand sets
# ---------------------------------------------------------------------------

_APPLY_DIFF = {
    "transfers_in": [], "transfers_out": [], "cost_delta": 0,
    "total_cost_after": 800, "free_transfers_used": 0, "bank_after": 3,
    "penalty_points": 0, "chip_covers": False, "bank_before": 3,
}


def _insert_suggestion(lid: int, advice: list[dict]) -> int:
    import json

    from app.db import execute
    return execute(
        """INSERT INTO suggestions (lineup_id, profile, variant_of, generated_at, target_gw,
           projected_points, objective, diff, chip_advice, rationale, raw_lineup)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (lid, "max_ep", None, "2026-09-19T12:00:00Z", 6,
         json.dumps({"baseline": 90.0, "adjusted": 94.0}), 94.0, json.dumps(_APPLY_DIFF),
         json.dumps(advice), json.dumps({"per_player": {}, "notes": []}), json.dumps([])),
    )


def test_apply_decrements_used_chip(client):
    """§25.3 (T3): applying a suggestion whose advice says 'use wildcard'
    decrements the lineup's in-hand wildcard set — otherwise the lineup keeps
    claiming a wildcard whenever a window is open and every later generate
    re-opens the unlimited-transfers hole (the Wildcard-side twin of B1)."""
    body = dict(VALID_BODY, chips={"wildcard": 1, "freehit": 2, "bboost": 2, "triple_captain": 2})
    lid = client.post("/api/lineups", json=body).json()["id"]
    advice = [{"chip": "wildcard", "recommendation": "use", "reason": "bank is thin"}]
    sid = _insert_suggestion(lid, advice)
    r = client.post(f"/api/suggestions/{sid}/apply")
    assert r.status_code == 200, r.text
    assert r.json()["chips_logged"] == ["wildcard"]
    assert client.get(f"/api/lineups/{lid}").json()["chips"]["wildcard"] == 0


def test_apply_refuses_test_lineup(client):
    """W3 (rev 3): applying is a TEAM-LEVEL act (it logs chips into
    chip_plays_log, which drives the Free-Hit ban, and rewrites the bank). A
    suggestion generated from a test (sandbox) lineup must never do that — the
    UI hid the button but the API allowed it."""
    body = dict(VALID_BODY, name="Sandbox", kind="test")
    lid = client.post("/api/lineups", json=body).json()["id"]
    sug = next(s for s in client.post(
        "/api/suggestions/generate", json={"lineup_id": lid}).json()["suggestions"]
        if s["profile"] == "max_ep")

    r = client.post(f"/api/suggestions/{sug['id']}/apply")
    assert r.status_code == 422, r.text
    assert "test (sandbox)" in r.json()["detail"]
    # nothing leaked into team-level state
    assert client.get("/api/lineups/chip-plays").json()["chip_plays"] == []
    assert client.get(f"/api/lineups/{lid}").json()["transfer_bank"] == body["transfer_bank"]
    assert client.get(f"/api/lineups/{lid}").json()["chips"]["wildcard"] == 2


def test_apply_refuses_orphaned_suggestion(client):
    """W3 (rev 3): a suggestion whose lineup has been deleted (lineup_id NULL
    via ON DELETE SET NULL) used to log team-level chips while the bank update
    silently no-opped on `WHERE id = NULL`."""
    lid = client.post("/api/lineups", json=VALID_BODY).json()["id"]
    advice = [{"chip": "wildcard", "recommendation": "use", "reason": "bank is thin"}]
    sid = _insert_suggestion(lid, advice)
    assert client.delete(f"/api/lineups/{lid}").status_code == 200

    r = client.post(f"/api/suggestions/{sid}/apply")
    assert r.status_code == 409, r.text
    assert "no longer exists" in r.json()["detail"]
    assert client.get("/api/lineups/chip-plays").json()["chip_plays"] == []


def test_apply_chip_decrement_never_negative(client):
    """§25.3 (T3): a chip the lineup does not hold is never decremented below 0."""
    body = dict(VALID_BODY, chips={"wildcard": 0, "freehit": 2, "bboost": 2, "triple_captain": 2})
    lid = client.post("/api/lineups", json=body).json()["id"]
    advice = [{"chip": "wildcard", "recommendation": "use", "reason": "stale advice"}]
    sid = _insert_suggestion(lid, advice)
    r = client.post(f"/api/suggestions/{sid}/apply")
    assert r.status_code == 200, r.text
    assert client.get(f"/api/lineups/{lid}").json()["chips"]["wildcard"] == 0


def test_chip_play_does_not_touch_chips(client):
    """A14 option 1: the manual chip-play endpoint is a pure log — it drives
    the Free-Hit ban but never touches lineups.chips (the checkbox is a
    toggle; in-hand counts are edited with the MetaPanel steppers)."""
    cur = client.post("/api/lineups", json=dict(VALID_BODY, name="Current XI")).json()["id"]
    test = client.post("/api/lineups", json=dict(VALID_BODY, name="Sandbox", kind="test")).json()["id"]
    r = client.post("/api/lineups/chip-play", json={"gw": 6, "chip": "wildcard"})
    assert r.status_code == 201
    assert client.get(f"/api/lineups/{cur}").json()["chips"]["wildcard"] == 2
    assert client.get(f"/api/lineups/{test}").json()["chips"]["wildcard"] == 2
    r = client.get("/api/lineups/chip-plays", params={"gw": 6})
    assert any(p["gw"] == 6 and p["chip"] == "wildcard" for p in r.json()["chip_plays"])


def test_chip_play_toggle_cycle(client):
    """A14 acceptance: log → unlog → log leaves lineups.chips untouched; a
    redundant POST returns already:True and changes nothing; freehit_played_in
    tracks the log."""
    from app.db import query_one
    from app.optimizer.transfers import freehit_played_in

    lid = client.post("/api/lineups", json=VALID_BODY).json()["id"]
    before = client.get(f"/api/lineups/{lid}").json()["chips"]

    r = client.post("/api/lineups/chip-play", json={"gw": 6, "chip": "freehit"})
    assert r.status_code == 201
    assert r.json()["already"] is False
    assert freehit_played_in(6) is True

    # redundant POST: already:True, nothing changes
    r = client.post("/api/lineups/chip-play", json={"gw": 6, "chip": "freehit"})
    assert r.status_code == 201
    assert r.json()["already"] is True
    assert query_one("SELECT COUNT(*) AS n FROM chip_plays_log "
                     "WHERE gw = 6 AND chip = 'freehit'")["n"] == 1

    r = client.delete("/api/lineups/chip-play", params={"gw": 6, "chip": "freehit"})
    assert r.json()["deleted"] is True
    assert freehit_played_in(6) is False

    r = client.post("/api/lineups/chip-play", json={"gw": 6, "chip": "freehit"})
    assert r.json()["already"] is False
    assert freehit_played_in(6) is True

    assert client.get(f"/api/lineups/{lid}").json()["chips"] == before


def test_update_preserves_bought_cost(client):
    """A15: the stored bought_cost survives a save's DELETE-and-reinsert, and
    the resulting sell-on fee lowers compute_diff's budget_after."""
    from app import db as dbmod
    from app.optimizer.transfers import compute_diff, sell_value

    lid = client.post("/api/lineups", json=VALID_BODY).json()["id"]
    saved = {p["player_id"]: p for p in client.get(f"/api/lineups/{lid}").json()["players"]}
    assert saved[1]["bought_cost"] == 45  # derived from now_cost at creation

    # The price rises before the next save; the save must not re-derive it.
    dbmod.execute("UPDATE players SET now_cost = 50 WHERE id = 1")
    r = client.put(f"/api/lineups/{lid}", json=VALID_BODY)
    assert r.status_code == 200
    saved = {p["player_id"]: p for p in r.json()["players"]}
    assert saved[1]["bought_cost"] == 45
    assert sell_value(45, 50) == 47

    # Selling the risen player: the fee (50 − 47 = 3) comes off the naive
    # 1000 − Σ now_cost view.
    cur = [{"player_id": p["player_id"], "web_name": p["web_name"],
            "now_cost": p["now_cost"], "bought_cost": p["bought_cost"]}
           for p in r.json()["players"]]
    new = [p for p in cur if p["player_id"] != 1]
    naive = 1000 - sum(p["now_cost"] for p in new)
    assert compute_diff(cur, new, bank=3)["budget_after"] == naive - 3


def test_put_lineup_preserves_kind(client):
    """A16: a PUT without kind keeps the stored kind — saving a test copy
    from My Team must not silently promote it to current."""
    lid = client.post("/api/lineups", json=dict(VALID_BODY, name="Sandbox", kind="test")).json()["id"]
    r = client.put(f"/api/lineups/{lid}", json=VALID_BODY)
    assert r.status_code == 200
    assert r.json()["kind"] == "test"
    assert client.get(f"/api/lineups/{lid}").json()["kind"] == "test"


def test_put_lineup_can_promote_kind(client):
    """A16: an explicit kind on the PUT body still wins (test → current)."""
    lid = client.post("/api/lineups", json=dict(VALID_BODY, name="Sandbox", kind="test")).json()["id"]
    r = client.put(f"/api/lineups/{lid}", json=dict(VALID_BODY, kind="current"))
    assert r.status_code == 200
    assert r.json()["kind"] == "current"