"""LLM extractor tests (PLAN-3 T2.8 / T2.12): validation, fence-strip,
bad player_id rejection, HTTP error → [] + poll_log, not-ready → [].

Async functions are driven with asyncio.run (no pytest-asyncio dependency).
"""
from __future__ import annotations

import asyncio
import json
import types

import pytest

from app import db as dbmod
from app.signals import llm_extractor as llm
from app.signals.llm_extractor import (
    _chunk,
    _strip_fences,
    _validate,
    LLMResponse,
    extract_signals_llm,
)

PLAYERS = [
    {"id": 1, "first_name": "First", "second_name": "One", "team_code": "ALP"},
    {"id": 3, "first_name": "Def", "second_name": "A One", "team_code": "ALP"},
]


def _settings(ready=True, base_url="http://llm.test/v1"):
    return types.SimpleNamespace(
        llm_ready=ready,
        llm_base_url=base_url,
        llm_api_key="k" if ready else "",
        llm_model="test-model" if ready else "",
        config=types.SimpleNamespace(
            llm=types.SimpleNamespace(batch_chars=8000, timeout_sec=5),
            sources=types.SimpleNamespace(
                youtube=types.SimpleNamespace(llm_truncate_chars=12000)
            ),
        ),
    )


class _FakeResponse:
    def __init__(self, content, status_code=200):
        self._content = content
        self.status_code = status_code
        self.text = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class _FakeClient:
    """Stands in for httpx.AsyncClient: post() returns canned content."""

    content = '{"signals": []}'
    status_code = 200
    calls: list[dict] = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeClient.calls.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(_FakeClient.content, _FakeClient.status_code)


@pytest.fixture(autouse=True)
def _patch_http(monkeypatch):
    _FakeClient.calls = []
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
    _FakeClient.content = json.dumps(
        {"signals": [
            {"player_id": 1, "category": "injury", "sentiment": "negative",
             "confidence": 0.8, "summary": "Goal One ruled out with hamstring injury."},
        ]}
    )
    out = asyncio.run(extract_signals_llm(
        "Goal One is ruled out with a hamstring injury.",
        "bbc", "2026-09-19T12:00:00Z", PLAYERS, _settings(),
    ))
    assert len(out) == 1
    assert out[0]["player_id"] == 1
    assert _FakeClient.calls[0]["url"] == "http://llm.test/v1/chat/completions"
    assert _FakeClient.calls[0]["headers"]["Authorization"] == "Bearer k"
    body = _FakeClient.calls[0]["json"]
    assert body["temperature"] == 0
    assert "1 | First One | ALP" in body["messages"][1]["content"]


def test_fenced_json_is_stripped(db_path):
    _FakeClient.content = (
        '```json\n{"signals": [{"player_id": 3, "category": "selection", '
        '"sentiment": "positive", "confidence": 0.6, "summary": "confirmed to start"}]}\n```'
    )
    out = asyncio.run(extract_signals_llm(
        "Def A One confirmed to start.", "bbc", None, PLAYERS, _settings()
    ))
    assert len(out) == 1
    assert out[0]["player_id"] == 3


def test_http_error_returns_empty_and_logs(db_path):
    _FakeClient.status_code = 401
    out = asyncio.run(extract_signals_llm("Goal One injured.", "bbc", None, PLAYERS, _settings()))
    assert out == []
    errs = dbmod.query("SELECT * FROM poll_log WHERE source = 'llm' AND status = 'error'")
    assert errs and "401" in (errs[0]["error"] or "")


def test_malformed_json_returns_empty(db_path):
    _FakeClient.content = "I cannot help with that."
    out = asyncio.run(extract_signals_llm("Goal One injured.", "bbc", None, PLAYERS, _settings()))
    assert out == []


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