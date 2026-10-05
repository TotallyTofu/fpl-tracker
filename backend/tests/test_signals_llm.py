"""LLM extractor tests (PLAN-3 T2.8 / T2.12 + FIX.MD Part 2 §18).

Async functions are driven with asyncio.run (no pytest-asyncio dependency).
The fake httpx client supports both the streaming path (``stream()`` with
``aiter_lines``) and the non-streaming fallback (``post``).

FIX N10 contract: hard errors → ``extract_signals_llm`` returns None (logged
to poll_log); ``[]`` means "processed, no signals".
"""
from __future__ import annotations

import asyncio
import json
import logging
import types

import pytest

from app import db as dbmod
from app.signals import llm_extractor as llm
from app.signals.llm_extractor import (
    _chunk,
    _parse_sse_line,
    _salvage_signals,
    _strip_fences,
    _validate,
    LLMResponse,
    extract_signals_llm,
)

PLAYERS = [
    {"id": 1, "first_name": "First", "second_name": "One", "team_code": "ALP"},
    {"id": 3, "first_name": "Def", "second_name": "A One", "team_code": "ALP"},
]


def _llm_cfg(**kw):
    d = dict(
        batch_chars=8000, timeout_sec=300, max_tokens=2048,
        player_list_mode="filtered",
        stream_idle_timeout_sec=90, chunk_wallclock_sec=300, retries=0,
        fallback_full_list=True, truncate_chars=8000, extract_timebox_sec=480,
    )
    d.update(kw)
    return types.SimpleNamespace(**d)


def _settings(ready=True, base_url="http://llm.test/v1", **llm_kw):
    return types.SimpleNamespace(
        llm_ready=ready,
        llm_base_url=base_url,
        llm_api_key="k" if ready else "",
        llm_model="test-model" if ready else "",
        config=types.SimpleNamespace(
            llm=_llm_cfg(**llm_kw),
            sources=types.SimpleNamespace(
                youtube=types.SimpleNamespace(llm_truncate_chars=12000)
            ),
        ),
    )


def _sse(*chunks, finish=None):
    """SSE line list the streaming fake serves."""
    lines = []
    for c in chunks:
        lines.append("data: " + json.dumps({"choices": [{"delta": {"content": c}}]}))
    if finish:
        lines.append("data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": finish}]}))
    lines.append("data: [DONE]")
    return lines


class _FakeStreamResponse:
    def __init__(self, lines, status_code=200, text=""):
        self._lines = lines
        self.status_code = status_code
        self.text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln


class _FakeClient:
    """Stands in for httpx.AsyncClient: stream() serves SSE lines, post() the
    non-streaming fallback. Class-level state is configurable per test."""

    lines: list[str] = _sse('{"signals": []}')
    status_code = 200
    post_content: str | None = '{"signals": []}'
    post_status = 200
    calls: list[dict] = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, json=None, headers=None):
        _FakeClient.calls.append({"kind": "stream", "url": url, "json": json, "headers": headers})
        return _FakeStreamResponse(_FakeClient.lines, _FakeClient.status_code,
                                   text="response_format unsupported")

    async def post(self, url, json=None, headers=None, timeout=None):
        _FakeClient.calls.append({"kind": "post", "url": url, "json": json, "headers": headers})
        return types.SimpleNamespace(
            status_code=_FakeClient.post_status, text=_FakeClient.post_content or "",
            json=lambda: {"choices": [{"message": {"content": _FakeClient.post_content}}]},
        )


@pytest.fixture(autouse=True)
def _patch_http(monkeypatch):
    # Reset class-level state: earlier tests set status/lines (e.g. the 401
    # test) and would otherwise leak into later tests.
    _FakeClient.calls = []
    _FakeClient.lines = _sse('{"signals": []}')
    _FakeClient.status_code = 200
    _FakeClient.post_content = '{"signals": []}'
    _FakeClient.post_status = 200
    llm._json_mode_ok = None  # FIX N4: reset the capability cache
    monkeypatch.setattr(llm.httpx, "AsyncClient", _FakeClient)


def test_strip_fences():
    assert _strip_fences('```json\n{"signals": []}\n```') == '{"signals": []}'
    assert _strip_fences('{"signals": []}') == '{"signals": []}'


def test_chunk_splits_on_paragraphs():
    text = "\n".join(f"para {i} " + "x" * 50 for i in range(10))
    chunks = _chunk(text, 300)
    assert len(chunks) > 1
    assert all(len(c) <= 300 + 60 for c in chunks)  # a paragraph may slightly overshoot
    assert "".join(c.replace("\n", " ") for c in chunks).count("para ") == 10


def test_parse_sse_line():
    """§18.2: SSE line parsing is a pure helper."""
    assert _parse_sse_line('data: {"a": 1}') == {"a": 1}
    assert _parse_sse_line("data: [DONE]") is None
    assert _parse_sse_line(": comment") is None
    assert _parse_sse_line("") is None
    assert _parse_sse_line("data: not json") is None


def test_validate_rejects_bad_player_id():
    payload = LLMResponse.model_validate(
        {"signals": [
            {"player_id": 999, "category": "injury", "sentiment": "negative",
             "confidence": 0.9, "summary": "nope"},
            {"player_id": 1, "category": "injury", "sentiment": "negative",
             "confidence": 0.9, "summary": "ok"},
        ]}
    )
    out = _validate(payload, {1, 3})
    assert len(out) == 1
    assert out[0]["player_id"] == 1


def test_validate_rejects_bad_category_and_sentiment():
    payload = LLMResponse.model_validate(
        {"signals": [
            {"player_id": 1, "category": "weather", "sentiment": "negative",
             "confidence": 0.5, "summary": "x"},
            {"player_id": 1, "category": "injury", "sentiment": "upset",
             "confidence": 0.5, "summary": "x"},
        ]}
    )
    assert _validate(payload, {1}) == []


def test_validate_clamps_confidence_and_summary():
    payload = LLMResponse.model_validate(
        {"signals": [
            {"player_id": 1, "category": "injury", "sentiment": "negative",
             "confidence": 0.9, "summary": "s" * 500},
        ]}
    )
    out = _validate(payload, {1})
    assert len(out[0]["summary"]) == 200


def test_happy_path(db_path):
    _FakeClient.lines = _sse(json.dumps(
        {"signals": [
            {"player_id": 1, "category": "injury", "sentiment": "negative",
             "confidence": 0.8, "summary": "Goal One ruled out with hamstring injury."},
        ]}))
    out = asyncio.run(extract_signals_llm(
        "Goal One is ruled out with a hamstring injury.",
        "bbc", "2026-09-19T12:00:00Z", PLAYERS, _settings(),
    ))
    assert len(out) == 1
    assert out[0]["player_id"] == 1
    call = _FakeClient.calls[0]
    assert call["url"] == "http://llm.test/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer k"
    body = call["json"]
    assert body["temperature"] == 0
    assert body["stream"] is True                       # FIX N1: streaming
    assert body["max_tokens"] == 2048                   # FIX N1 default
    assert "1 | First One | ALP" in body["messages"][1]["content"]
    # FIX N5: successful calls are logged too
    oks = dbmod.query("SELECT * FROM poll_log WHERE source = 'llm' AND status = 'ok'")
    assert oks and oks[0]["rows"] == 1


def test_response_format_requested(db_path):
    """§18.2 (FIX N4): the request asks for JSON mode."""
    _FakeClient.lines = _sse('{"signals": []}')
    asyncio.run(extract_signals_llm("Goal One injured.", "bbc", None, PLAYERS, _settings()))
    assert _FakeClient.calls[0]["json"]["response_format"] == {"type": "json_object"}


def test_response_format_400_falls_back_to_plain(db_path):
    """§18.2 (FIX N4): a 400 mentioning response_format → one retry without it
    via the plain path, and the capability cache remembers."""
    _FakeClient.status_code = 400
    # post_content is the CONTENT string the plain response carries
    _FakeClient.post_content = json.dumps(
        {"signals": [{"player_id": 1, "category": "injury", "sentiment": "negative",
                      "confidence": 0.8, "summary": "doubt"}]})
    out = asyncio.run(extract_signals_llm("Goal One injured.", "bbc", None, PLAYERS, _settings()))
    assert out and out[0]["player_id"] == 1
    kinds = [c["kind"] for c in _FakeClient.calls]
    assert kinds == ["stream", "post"]                  # streaming 400 → plain retry
    assert llm._json_mode_ok is False
    assert "response_format" not in _FakeClient.calls[1]["json"]
    # later calls skip the rejected field entirely
    _FakeClient.calls = []
    _FakeClient.status_code = 200
    _FakeClient.lines = _sse('{"signals": []}')
    asyncio.run(extract_signals_llm("Goal One injured.", "bbc", None, PLAYERS, _settings()))
    assert "response_format" not in _FakeClient.calls[0]["json"]


def test_stream_ignored_falls_back_to_plain(db_path):
    """FIX N1: a server that ignores stream:True (no SSE deltas) → plain path."""
    _FakeClient.lines = []  # body with no data: lines
    _FakeClient.post_content = json.dumps(
        {"signals": [{"player_id": 3, "category": "selection", "sentiment": "positive",
                      "confidence": 0.6, "summary": "starts"}]})
    out = asyncio.run(extract_signals_llm("Def A One starts.", "bbc", None, PLAYERS, _settings()))
    assert out and out[0]["player_id"] == 3
    assert [c["kind"] for c in _FakeClient.calls] == ["stream", "post"]


def test_fenced_json_is_stripped(db_path):
    _FakeClient.lines = _sse(
        '```json\n{"signals": [{"player_id": 3, "category": "selection", ',
        '"sentiment": "positive", "confidence": 0.6, "summary": "confirmed to start"}]}\n```',
    )
    out = asyncio.run(extract_signals_llm(
        "Def A One confirmed to start.", "bbc", None, PLAYERS, _settings()
    ))
    assert len(out) == 1
    assert out[0]["player_id"] == 3


def test_http_error_returns_none_and_logs(db_path):
    """§18.1 (FIX N10): hard error → None (retryable), still logged."""
    _FakeClient.status_code = 401
    out = asyncio.run(extract_signals_llm("Goal One injured.", "bbc", None, PLAYERS, _settings()))
    assert out is None
    errs = dbmod.query("SELECT * FROM poll_log WHERE source = 'llm' AND status = 'error'")
    assert errs and "401" in (errs[0]["error"] or "")


def test_malformed_json_returns_none(db_path):
    _FakeClient.lines = _sse("I cannot help with that.")
    out = asyncio.run(extract_signals_llm("Goal One injured.", "bbc", None, PLAYERS, _settings()))
    assert out is None


def test_salvage_truncated_json(db_path):
    """§18.2 (FIX N3): complete flat signal objects are salvaged from
    truncated/prose-wrapped output."""
    truncated = 'Here you go: {"signals": [{"player_id": 1, "category": "injury", ' \
        '"sentiment": "negative", "confidence": 0.9, "summary": "hamstring injury"}, {"player_id": 3, "c'
    salvaged = _salvage_signals(truncated)
    assert len(salvaged) == 1
    assert salvaged[0]["player_id"] == 1
    # end-to-end: salvage runs inside the extractor and yields the signal
    _FakeClient.lines = _sse(truncated, finish="length")
    out = asyncio.run(extract_signals_llm("Goal One injured.", "bbc", None, PLAYERS, _settings()))
    assert out and out[0]["player_id"] == 1


def test_chunk_error_isolation(db_path, monkeypatch):
    """§18.2 (FIX N2): chunk 1 valid, chunk 2 malformed → chunk 1's signals
    survive; the per-chunk failure is logged with its index."""
    s = _settings(batch_chars=50)
    text = ("First One is injured and out. " * 3) + "\n" + ("GARBAGE CHUNK " * 20)
    good = _sse('{"signals": [{"player_id": 1, "category": "injury", '
                '"sentiment": "negative", "confidence": 0.9, "summary": "out injured"}]}')
    seen: list[str] = []

    class _TwoPhaseClient(_FakeClient):
        def stream(self, method, url, json=None, headers=None):
            seen.append(url)
            if len(seen) == 1:
                return _FakeStreamResponse(good, 200)
            # valid SSE frame, but prose instead of JSON → chunk error (N2)
            bad = _sse("I cannot help with that.")
            return _FakeStreamResponse(bad, 200)

    monkeypatch.setattr(llm.httpx, "AsyncClient", _TwoPhaseClient)
    out = asyncio.run(extract_signals_llm(text, "bbc", None, PLAYERS, s))
    assert out and out[0]["player_id"] == 1             # chunk 1 survived
    errs = dbmod.query("SELECT * FROM poll_log WHERE source = 'llm' AND status = 'error'")
    assert errs and errs[0]["error"].startswith("chunk 2/")   # logged with the index


def test_all_chunks_failed_returns_none(db_path):
    """FIX N2/N10: every chunk failing is a hard error → None."""
    s = _settings(batch_chars=20)
    text = "Goal One is out.\nBBBBBBBBBB"    # 2 chunks, name resolves (DB index)
    _FakeClient.status_code = 500
    out = asyncio.run(extract_signals_llm(text, "bbc", None, PLAYERS, s))
    assert out is None
    errs = dbmod.query("SELECT * FROM poll_log WHERE source = 'llm' AND status = 'error'")
    assert len(errs) == 2                               # one per chunk
    assert all(e["error"].startswith("chunk ") for e in errs)


def test_max_tokens_comes_from_config(db_path):
    _FakeClient.lines = _sse('{"signals": []}')
    s = _settings(max_tokens=12345)
    asyncio.run(extract_signals_llm("Goal One injured.", "bbc", None, PLAYERS, s))
    assert _FakeClient.calls[0]["json"]["max_tokens"] == 12345


def test_max_tokens_config_default():
    """§18.2: the shipped default is the sane 2048, not 80 000."""
    from app.config import LLMConfig
    assert LLMConfig().max_tokens == 2048
    assert LLMConfig().timeout_sec == 300
    assert LLMConfig().stream_idle_timeout_sec == 90
    assert LLMConfig().chunk_wallclock_sec == 300
    assert LLMConfig().retries == 2
    assert LLMConfig().fallback_full_list is True
    assert LLMConfig().truncate_chars == 8000


def test_filtered_mode_sends_only_named_players(db_path):
    _FakeClient.lines = _sse('{"signals": []}')
    asyncio.run(extract_signals_llm("Goal One is ruled out.", "bbc", None, PLAYERS, _settings()))
    content = _FakeClient.calls[0]["json"]["messages"][1]["content"]
    assert "1 | First One | ALP" in content
    assert "3 | Def A One | ALP" not in content


def test_filtered_mode_skips_call_when_no_names(db_path):
    """§18.1: no names resolve AND fallback_full_list=False → the call is
    skipped (not an error, no poll_log row)."""
    _FakeClient.lines = _sse('{"signals": []}')
    out = asyncio.run(extract_signals_llm("The weather was fine today.", "bbc", None,
                                          PLAYERS, _settings(fallback_full_list=False)))
    assert out == []
    assert _FakeClient.calls == []  # no HTTP call at all
    assert dbmod.query("SELECT * FROM poll_log WHERE source = 'llm'") == []


def test_fallback_full_list(db_path):
    """§18.1 (FIX N6): no names resolve + fallback_full_list=True + a
    substantive text → the HTTP call happens with the FULL list in the prompt."""
    _FakeClient.lines = _sse('{"signals": []}')
    long_text = ("The medical team confirmed that the unnamed midfielder will "
                 "undergo a scan tomorrow, and the manager will hold a press "
                 "conference later today to discuss the squad situation ahead "
                 "of the weekend fixture, with all the latest fitness updates "
                 "and expected return dates for the players involved.")  # > 200 chars
    out = asyncio.run(extract_signals_llm(long_text, "bbc", None, PLAYERS, _settings()))
    assert out == []                                    # processed, no signals
    assert len(_FakeClient.calls) == 1                  # the LLM was NOT skipped
    content = _FakeClient.calls[0]["json"]["messages"][1]["content"]
    assert "1 | First One | ALP" in content
    assert "3 | Def A One | ALP" in content


def test_full_mode_sends_entire_list(db_path):
    _FakeClient.lines = _sse('{"signals": []}')
    s = _settings()
    s.config.llm.player_list_mode = "full"
    asyncio.run(extract_signals_llm("Goal One is ruled out.", "bbc", None, PLAYERS, s))
    content = _FakeClient.calls[0]["json"]["messages"][1]["content"]
    assert "1 | First One | ALP" in content
    assert "3 | Def A One | ALP" in content


def test_not_ready_returns_empty_silently(db_path):
    out = asyncio.run(extract_signals_llm(
        "Goal One injured.", "bbc", None, PLAYERS, _settings(ready=False)
    ))
    assert out == []
    assert _FakeClient.calls == []  # no HTTP call at all
    assert dbmod.query("SELECT * FROM poll_log WHERE source = 'llm'") == []


def test_empty_text_returns_empty(db_path):
    out = asyncio.run(extract_signals_llm("   ", "bbc", None, PLAYERS, _settings()))
    assert out == []
    assert _FakeClient.calls == []


def test_youtube_uses_its_own_truncate_chars(db_path):
    """FIX N7: YouTube keeps its override; other sources use llm.truncate_chars."""
    _FakeClient.lines = _sse('{"signals": []}')
    s = _settings(truncate_chars=100)
    long_text = "A" * 150 + "Z" * 350     # distinct markers: Z only past char 100
    asyncio.run(extract_signals_llm(long_text, "bbc", None, PLAYERS, s))
    chunk_sent = _FakeClient.calls[0]["json"]["messages"][1]["content"]
    assert "Z" not in chunk_sent                        # truncated to the first 100 chars
    # YouTube: sources.youtube.llm_truncate_chars wins (12000 → no cut)
    _FakeClient.calls = []
    asyncio.run(extract_signals_llm(long_text, "youtube", None, PLAYERS, s))
    chunk_sent = _FakeClient.calls[0]["json"]["messages"][1]["content"]
    assert "Z" in chunk_sent


# --- FIX.MD A5 / A6: empty content on reasoning + truncation guards -------------


def _sse_reasoning(*chunks, finish=None):
    """SSE lines carrying reasoning_content deltas (thinking models: the
    answer arrives in a separate field, content stays empty)."""
    lines = []
    for c in chunks:
        lines.append("data: " + json.dumps({"choices": [{"delta": {"reasoning_content": c}}]}))
    if finish:
        lines.append("data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": finish}]}))
    lines.append("data: [DONE]")
    return lines


def test_empty_content_with_reasoning_is_distinct_failure(db_path):
    """A5: the model spends its budget on reasoning_content → a named failure
    in the poll-log, not a bare JSON-parse RuntimeError."""
    _FakeClient.lines = _sse_reasoning("The user wants a single", finish="length")
    out = asyncio.run(extract_signals_llm("Goal One injured.", "bbc", None, PLAYERS, _settings()))
    assert out is None
    errs = dbmod.query("SELECT * FROM poll_log WHERE source = 'llm' AND status = 'error'")
    assert errs and "reasoning_content" in (errs[0]["error"] or "")
    assert "raise llm.max_tokens" in errs[0]["error"]


def test_empty_content_without_reasoning_keeps_parse_failure(db_path):
    """A5: a truly empty stream (no content, no reasoning) keeps the old
    'not parseable' chunk error."""
    _FakeClient.lines = _sse(finish="stop")
    out = asyncio.run(extract_signals_llm("Goal One injured.", "bbc", None, PLAYERS, _settings()))
    assert out is None
    errs = dbmod.query("SELECT * FROM poll_log WHERE source = 'llm' AND status = 'error'")
    assert errs and "not parseable" in (errs[0]["error"] or "")


def test_truncated_success_path_logs_length(db_path, caplog):
    """A6: finish_reason=length on a parsed success is flagged on the
    successful path too (previously only in the salvage/error messages)."""
    _FakeClient.lines = _sse('{"signals": []}', finish="length")
    with caplog.at_level(logging.WARNING, logger="fpl.signals.llm"):
        out = asyncio.run(extract_signals_llm("Goal One injured.", "bbc", None, PLAYERS, _settings()))
    assert out == []
    assert any("finish_reason=length" in rec.getMessage() for rec in caplog.records)


def test_test_llm_connection_empty_content_is_not_ok(db_path, monkeypatch):
    """A5: a 200 with empty content is not a healthy connection — ok: False
    with a detail that names the reasoning_content budget problem."""

    class _EmptyClient(_FakeClient):
        async def post(self, url, json=None, headers=None, timeout=None):
            return types.SimpleNamespace(
                status_code=200, text="",
                json=lambda: {"choices": [{"message": {
                    "content": "",
                    "reasoning_content": "The user wants a single " * 20,
                }}]},
            )

    monkeypatch.setattr(llm.httpx, "AsyncClient", _EmptyClient)
    out = asyncio.run(llm.test_llm_connection(_settings()))
    assert out["ok"] is False
    assert out["status"] == "empty_content"
    assert "reasoning_content" in out["detail"]
    assert "raise llm.max_tokens" in out["detail"]


def test_test_llm_connection_ok_reply(db_path, monkeypatch):
    """A5 regression: a 200 with a real reply is still ok: True."""

    class _OkClient(_FakeClient):
        async def post(self, url, json=None, headers=None, timeout=None):
            return types.SimpleNamespace(
                status_code=200, text="",
                json=lambda: {"choices": [{"message": {"content": "ok"}}]},
            )

    monkeypatch.setattr(llm.httpx, "AsyncClient", _OkClient)
    out = asyncio.run(llm.test_llm_connection(_settings()))
    assert out["ok"] is True
    assert out["status"] == "ok"


def test_relevant_players_truncation_keeps_top_selected_and_warns(monkeypatch, caplog):
    """A23: when more players are named than MAX_LIST_PLAYERS, the cap keeps
    the highest selected_by_percent and the truncation is logged."""
    players = [
        {"id": i, "web_name": f"Player {i}", "selected_by_percent": float(i)}
        for i in range(1, 46)  # 45 named players > MAX_LIST_PLAYERS (40)
    ]
    monkeypatch.setattr(llm, "get_name_index", lambda: {})
    monkeypatch.setattr(
        llm, "resolve_candidates",
        lambda text, idx: [{"player_id": p["id"]} for p in players],
    )
    with caplog.at_level(logging.WARNING, logger="fpl.signals.llm"):
        out = llm._relevant_players(" ".join(p["web_name"] for p in players), players)
    assert len(out) == llm.MAX_LIST_PLAYERS
    assert {p["id"] for p in out} == set(range(6, 46))  # top 40 by selected_by_percent
    assert any("truncated" in r.getMessage() for r in caplog.records)


def test_relevant_players_under_cap_keeps_order_and_is_silent(monkeypatch, caplog):
    """A23: below the cap the original order is kept and nothing is logged."""
    players = [
        {"id": i, "web_name": f"Player {i}", "selected_by_percent": float(i)}
        for i in range(1, 11)
    ]
    monkeypatch.setattr(llm, "get_name_index", lambda: {})
    monkeypatch.setattr(
        llm, "resolve_candidates",
        lambda text, idx: [{"player_id": p["id"]} for p in players],
    )
    with caplog.at_level(logging.WARNING, logger="fpl.signals.llm"):
        out = llm._relevant_players("text", players)
    assert [p["id"] for p in out] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert not any("truncated" in r.getMessage() for r in caplog.records)


def test_truncation_keeps_top_owned_through_pipeline_player_list(db_path, monkeypatch, caplog):
    """W1 (rev 3) integration guard: the A23 truncation sorts by
    selected_by_percent, so the PRODUCTION player list must actually carry that
    column. It did not — pipeline._player_list() omitted it, every sort key was
    0.0, and the "top by ownership" promise was a silent no-op (the unit tests
    above pass a synthetic list that happens to include the field)."""
    from app import db as dbmod
    from app.signals import names as names_mod
    from app.signals import pipeline

    # 45 distinctly-named players with strictly descending ownership. (Letter
    # suffixes, not digits: the normaliser strips [^a-z ], so "Zed00" would
    # collapse to "zed" for all 45.)
    suffixes = [f"{chr(97 + i // 26)}{chr(97 + i % 26)}" for i in range(45)]
    dbmod.execute_many(
        "INSERT INTO players (id, web_name, element_type, team, now_cost, ep_next, "
        "selected_by_percent, status, can_select, removed, chance_of_playing_next_round, "
        "form, fetched_at) VALUES (?,?,3,1,50,5.0,?,'a',1,0,100,5.0,'2026-09-19T12:00:00Z')",
        [(900 + i, f"Zed{s}", float(45 - i)) for i, s in enumerate(suffixes)],
    )
    monkeypatch.setattr(names_mod, "_index_cache", {"key": None, "idx": None})

    plist = pipeline._player_list()
    assert plist and all("selected_by_percent" in r for r in plist)

    text = " ".join(f"Zed{s}" for s in suffixes)
    with caplog.at_level(logging.WARNING, logger="fpl.signals.llm"):
        out = llm._relevant_players(text, plist)

    assert len(out) == llm.MAX_LIST_PLAYERS
    # the 40 highest-owned (Zed00..Zed39, ownership 45..6) survive the cap
    assert {p["id"] for p in out} == {900 + i for i in range(40)}
    assert any("truncated" in r.getMessage() for r in caplog.records)