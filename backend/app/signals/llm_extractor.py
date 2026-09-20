"""LLM signal extraction (PLAN-3 T2.8) — OpenAI-compatible local endpoint (D5/D12).

High-quality structured signals from item text (incl. YouTube transcripts).
Never crashes the pipeline: any failure (401/timeout/parse/schema) logs
``poll_log(source='llm', status='error')`` and returns ``[]``; the rule
extractor (T2.7) remains the backstop.
"""
from __future__ import annotations

import json
import logging
import re

import httpx
from pydantic import BaseModel, Field, ValidationError

from ..db import log_poll

log = logging.getLogger("fpl.signals.llm")

CATEGORIES = ("injury", "suspension", "selection", "rotation", "return", "transfer", "other")
SENTIMENTS = ("negative", "positive", "neutral")

SYSTEM_PROMPT = """You are a Fantasy Premier League (FPL) news analyst. You extract structured player signals from sports content.

INPUT:
- A list of valid FPL players, formatted: player_id | full name | club code.
- Content from a source (news article, Reddit thread, or YouTube video transcript) with a publication timestamp.

TASK:
Extract every concrete, decision-relevant statement about a specific player that could affect FPL selection over the next 1-2 gameweeks.

CATEGORIES (use exactly one per signal):
- injury, suspension, selection, rotation, return, transfer, other

RULES:
1. Only output players that appear in the provided player list. The content may contain typos or
   mis-transcriptions (for example "Calbertt Lewing" may mean Calvert-Lewin, "Leads" may mean Leeds).
   Use the player list as the reference and map such names to the correct player_id.
2. If a statement is vague, speculative, or clearly about the distant future (beyond ~2 gameweeks), skip it.
3. sentiment: "negative" (injury, suspension, rotation out, transfer out, doubt),
   "positive" (confirmed to start, return, clear to play, transfer in), "neutral" (context, mixed).
4. confidence 0.0-1.0: official confirmation 0.9-1.0; reputable reporting 0.6-0.8;
   speculation/rumor 0.3-0.5; vague context 0.2.
5. summary: one factual sentence (max 25 words), no commentary, no hedging beyond what the source says.
6. Do not invent information that is not in the content. Prefer fewer, accurate signals over many weak ones.

OUTPUT:
Strict JSON only. No markdown, no code fences, no commentary. Exactly this shape:
{"signals": [{"player_id": <int>, "category": "<category>", "sentiment": "<sentiment>",
              "confidence": <float>, "summary": "<one sentence>"}]}
If there are no signals: {"signals": []}
"""

USER_TEMPLATE = """Player list (player_id | full name | club code):
{player_list}

Source: {source}
Published: {published_at}

Content:
{text}
"""


class PlayerSignal(BaseModel):
    player_id: int
    category: str
    sentiment: str
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str


class LLMResponse(BaseModel):
    signals: list[PlayerSignal] = Field(default_factory=list)


def render_player_list(players: list[dict]) -> str:
    """player_id | full name | club code — one line per player."""
    lines = []
    for p in players:
        full = " ".join(x for x in (p.get("first_name"), p.get("second_name")) if x).strip()
        if not full:
            full = p.get("web_name") or ""
        lines.append(f"{p['id']} | {full} | {p.get('team_code') or ''}")
    return "\n".join(lines)


def _strip_fences(content: str) -> str:
    s = (content or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _chunk(text: str, limit: int) -> list[str]:
    """Split into ~limit-char chunks at paragraph boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    cur = ""
    for para in text.split("\n"):
        if len(cur) + len(para) + 1 > limit and cur:
            chunks.append(cur)
            cur = para
        else:
            cur = f"{cur}\n{para}" if cur else para
    if cur:
        chunks.append(cur)
    return chunks


def _validate(payload: LLMResponse, valid_ids: set[int]) -> list[dict]:
    out = []
    for s in payload.signals:
        if s.player_id not in valid_ids:
            continue
        if s.category not in CATEGORIES:
            continue
        if s.sentiment not in SENTIMENTS:
            continue
        out.append(
            {
                "player_id": s.player_id,
                "category": s.category,
                "sentiment": s.sentiment,
                "confidence": max(0.0, min(1.0, s.confidence)),
                "summary": s.summary[:200],
            }
        )
    return out


async def extract_signals_llm(
    text: str,
    source: str,
    published_at: str | None,
    player_list: list[dict],
    settings,
) -> list[dict]:
    """Call the LLM endpoint and return validated signal dicts (T2.8).

    settings: config.Settings (llm_base_url / llm_api_key / llm_model / config).
    Returns [] on any failure (logged to poll_log).
    """
    if not text or not text.strip():
        return []
    if not settings.llm_ready:
        return []
    valid_ids = {p["id"] for p in player_list}
    if not valid_ids:
        return []

    truncate = (
        settings.config.sources.youtube.llm_truncate_chars
        if source == "youtube"
        else 8000
    )
    batch = settings.config.llm.batch_chars
    chunks = _chunk(text[:truncate], batch)
    plist = render_player_list(player_list)

    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    merged: dict[tuple[int, str], dict] = {}

    try:
        async with httpx.AsyncClient(timeout=settings.config.llm.timeout_sec) as client:
            for chunk in chunks:
                body = {
                    "model": settings.llm_model,
                    "temperature": 0,
                    "max_tokens": 2000,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": USER_TEMPLATE.format(
                                player_list=plist,
                                source=source,
                                published_at=published_at or "unknown",
                                text=chunk,
                            ),
                        },
                    ],
                }
                r = await client.post(url, json=body, headers=headers)
                if r.status_code != 200:
                    raise RuntimeError(f"LLM HTTP {r.status_code}: {r.text[:200]}")
                data = r.json()
                content = data["choices"][0]["message"]["content"]
                payload = LLMResponse.model_validate(json.loads(_strip_fences(content)))
                for s in _validate(payload, valid_ids):
                    k = (s["player_id"], s["category"])
                    if k not in merged or s["confidence"] > merged[k]["confidence"]:
                        merged[k] = s
    except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError,
            ValidationError, RuntimeError) as e:
        log_poll("llm", "error", error=f"{type(e).__name__}: {e}"[:500])
        return []

    return list(merged.values())


async def test_llm_connection(settings) -> dict:
    """Trivial chat call for the Settings 'Test Connection' button (T2.13).

    Returns {ok, status, detail} — never raises.
    """
    if not settings.llm_ready:
        return {
            "ok": False,
            "status": "not_configured",
            "detail": "Set the LLM model name and API key (or .env LLM_MODEL / LLM_API_KEY).",
        }
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": settings.llm_model,
        "temperature": 0,
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            )
        if r.status_code == 200:
            data = r.json()
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
            return {"ok": True, "status": "ok", "detail": f"model={settings.llm_model} replied: {content[:60]!r}"}
        return {"ok": False, "status": f"http_{r.status_code}", "detail": r.text[:200]}
    except httpx.HTTPError as e:
        return {"ok": False, "status": "error", "detail": f"{type(e).__name__}: {e}"[:200]}