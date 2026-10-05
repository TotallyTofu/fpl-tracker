"""LLM signal extraction (PLAN-3 T2.8) — OpenAI-compatible local endpoint (D5/D12).

High-quality structured signals from item text (incl. YouTube transcripts).

FIX N1–N7 (2026-09-21): streaming completions — bytes flow while generating,
so timeouts are inactivity guards instead of total-duration ceilings (a wedged
server now fails in ≤ ~5 min instead of blocking 2.5 h); sane output budgets;
per-chunk error isolation; truncated-JSON salvage; JSON mode with graceful
fallback + capability cache; ok-logging for latency/health; full-list fallback
when no player name resolves; per-source truncation.

Contract (FIX N10): ``extract_signals_llm`` returns **None** on hard error
(every chunk failed / transport failure) — the pipeline treats None as
"retryable" (raw_items.extract_attempts). ``[]`` means "processed, no signals".
Any failure is logged to ``poll_log(source='llm', status='error')``; the rule
extractor (T2.7) remains the backstop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

import httpx
from pydantic import BaseModel, Field, ValidationError

from ..db import log_poll, now_utc
from .names import get_name_index, resolve_candidates

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


MAX_LIST_PLAYERS = 40


def _relevant_players(text: str, player_list: list[dict]) -> list[dict]:
    """Players the content actually names (fuzzy + misspelling-tolerant), capped.

    Empty when no name resolves — with ``fallback_full_list`` (N6) the caller
    then sends the full list instead of skipping the LLM call.

    A23: when the cap bites, keep the fantasy-relevant names (highest
    selected_by_percent) and log the truncation — previously the last N named
    players were dropped silently and could never be signalled.
    """
    idx = get_name_index()
    hit_ids = {h["player_id"] for h in resolve_candidates(text, idx)}
    if not hit_ids:
        return []
    hits = [p for p in player_list if p["id"] in hit_ids]
    if len(hits) > MAX_LIST_PLAYERS:
        hits.sort(key=lambda p: float(p.get("selected_by_percent") or 0.0), reverse=True)
        log.warning(
            "LLM player list truncated: %d named players, keeping top %d by selected_by_percent",
            len(hits), MAX_LIST_PLAYERS,
        )
        hits = hits[:MAX_LIST_PLAYERS]
    return hits


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


# --- FIX N1: streaming transport -------------------------------------------------

# FIX N4: capability cache — remember when the server rejects JSON mode so
# later calls skip the rejected field instead of paying the 400 round-trip.
_json_mode_ok: bool | None = None


def _llm_timeout(cfg) -> httpx.Timeout:
    """FIX N1: connect/pool stay tight; read = "no tokens received for this
    long = failure". During active generation deltas arrive constantly, so
    even a 30-minute generation completes under a 90 s read timeout."""
    return httpx.Timeout(connect=10.0, read=float(cfg.stream_idle_timeout_sec),
                         write=30.0, pool=10.0)


def _parse_sse_line(line: str) -> dict | None:
    """One SSE ``data:`` line → its JSON payload, else None.

    Extracted for testability (§18.2): comment/blank lines, ``[DONE]`` and
    malformed JSON all yield None instead of raising.
    """
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if not data or data == "[DONE]":
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


def _chat_body(settings, messages: list[dict], max_tokens: int,
               json_mode: bool, stream: bool) -> dict:
    body = {
        "model": settings.llm_model,
        "temperature": 0,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}   # FIX N4
    if stream:
        body["stream"] = True
    return body


async def _chat_once_plain(settings, client, body: dict, headers: dict) -> tuple[str, str | None]:
    """FIX N1: non-streaming fallback — read timeout = llm.timeout_sec
    (wall-clock), ``llm.retries`` retries with 5 s backoff on connect errors
    and timeouts. Returns (content, finish_reason)."""
    cfg = settings.config.llm
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    timeout = httpx.Timeout(connect=10.0, read=float(cfg.timeout_sec), write=30.0, pool=10.0)
    retries = int(getattr(cfg, "retries", 2) or 0)
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = await client.post(url, json=body, headers=headers, timeout=timeout)
            if r.status_code != 200:
                raise RuntimeError(f"LLM HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
            choice = (data.get("choices") or [{}])[0]
            content = (choice.get("message") or {}).get("content")
            finish = choice.get("finish_reason")
            log.debug("llm (plain) finish_reason=%s usage=%s", finish, data.get("usage"))
            if not content:
                raise RuntimeError(
                    f"LLM returned empty content (finish_reason={finish}, "
                    f"usage={data.get('usage')}) — check llm.max_tokens / the model's "
                    "context window"
                )
            return content, finish
        except (httpx.HTTPError, RuntimeError) as e:
            last_exc = e
            if attempt < retries:
                await asyncio.sleep(5.0)
    raise last_exc  # type: ignore[misc]


async def _chat_once(client, settings, messages: list[dict],
                     max_tokens: int) -> tuple[str, str | None]:
    """One chat completion — streaming first (FIX N1), graceful fallbacks:

    - 400 mentioning ``response_format`` → remember the server lacks JSON mode
      (capability cache) and retry once without it via the plain path (N4);
    - no SSE deltas at all (server ignored ``stream: True``) → plain path.

    Returns (content, finish_reason).
    """
    global _json_mode_ok
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    body = _chat_body(settings, messages, max_tokens, _json_mode_ok is not False, True)
    plain_body = _chat_body(settings, messages, max_tokens, False, False)

    parts: list[str] = []
    reasoning: list[str] = []  # A5: thinking models stream the answer here
    finish: str | None = None
    saw_stream = False
    async with client.stream("POST", url, json=body, headers=headers) as r:
        if r.status_code == 400 and "response_format" in (r.text or "").lower():
            _json_mode_ok = False
            log.info("LLM server rejected response_format=json_object — retrying without it")
            return await _chat_once_plain(settings, client, plain_body, headers)
        r.raise_for_status()
        async for line in r.aiter_lines():
            payload = _parse_sse_line(line)
            if payload is None:
                continue
            saw_stream = True
            choice = (payload.get("choices") or [{}])[0]
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
            if payload.get("usage"):
                log.debug("llm usage=%s", payload["usage"])
            delta = choice.get("delta") or {}
            piece = delta.get("content")
            if piece:
                parts.append(piece)
            rpiece = delta.get("reasoning_content")
            if rpiece:
                reasoning.append(rpiece)
    if not saw_stream:
        # server answered a normal JSON body despite stream:True → plain path
        return await _chat_once_plain(settings, client, plain_body, headers)
    if not parts and reasoning:
        # A5: distinct, explicitly-logged failure — the poll-log should read
        # "empty content (model used its budget on reasoning)", not a bare
        # JSON-parse RuntimeError from the empty string.
        n = len("".join(reasoning))
        log.warning("LLM returned empty content but %d chars of reasoning_content "
                    "(finish_reason=%s) — the model spent its budget on reasoning; "
                    "raise llm.max_tokens", n, finish)
        raise RuntimeError(
            f"LLM returned empty content (finish_reason={finish}); the model spent its budget on "
            f"reasoning_content ({n} chars) — raise llm.max_tokens")
    return "".join(parts), finish


# --- FIX N3: salvage -------------------------------------------------------------


def _salvage_signals(content: str) -> list[dict]:
    """Pull complete flat signal objects out of truncated/prose-wrapped output.

    Flat signal objects contain no nested braces, so a regex over ``{...}``
    islands plus a schema check recovers the usable signals from a
    ``finish_reason == "length"`` truncation (FIX N3).
    """
    objs = []
    for m in re.finditer(r"\{[^{}]*\}", content or ""):
        try:
            o = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if {"player_id", "category", "sentiment", "confidence", "summary"} <= set(o):
            objs.append(o)
    return objs


def _signals_from_content(content: str, finish: str | None,
                          valid_ids: set[int]) -> list[dict]:
    """Parse → validate; on parse failure, best-effort salvage (FIX N3).

    Raises RuntimeError when nothing usable remains (the caller logs it as a
    chunk error).
    """
    text = _strip_fences(content)
    try:
        payload = LLMResponse.model_validate(json.loads(text))
        return _validate(payload, valid_ids)
    except (json.JSONDecodeError, ValidationError):
        salvaged = _salvage_signals(text)
        if not salvaged:
            raise RuntimeError(
                f"LLM output not parseable (finish_reason={finish}): {text[:120]!r}")
        log.info("salvaged %d signal object(s) from truncated/wrapped LLM output",
                 len(salvaged))
        payload = LLMResponse.model_validate({"signals": salvaged})
        return _validate(payload, valid_ids)


async def extract_signals_llm(
    text: str,
    source: str,
    published_at: str | None,
    player_list: list[dict],
    settings,
) -> list[dict] | None:
    """Call the LLM endpoint and return validated signal dicts (T2.8).

    settings: config.Settings (llm_base_url / llm_api_key / llm_model / config).

    FIX N10 contract: returns **None** on hard error — every chunk failed
    (each failure is logged per-chunk with its index, N2). ``[]`` means the
    item was processed successfully and simply has no signals. Skips (no
    text, LLM not ready, no valid ids, no names + fallback disabled) also
    return ``[]`` — they are not errors.
    """
    if not text or not text.strip():
        return []
    if not settings.llm_ready:
        return []
    valid_ids = {p["id"] for p in player_list}
    if not valid_ids:
        return []

    cfg = settings.config.llm
    # FIX N7: per-source truncation — YouTube keeps its larger override
    # (transcripts carry chapter noise), every other source uses llm.truncate_chars.
    truncate = (settings.config.sources.youtube.llm_truncate_chars
                if source == "youtube" else cfg.truncate_chars)
    batch = cfg.batch_chars
    chunks = _chunk(text[:truncate], batch)

    # FIX N6: filtered mode with no resolved names used to silently skip the
    # LLM (the scan regex misses community name forms — "Mo Salah"-style
    # nicknames). With fallback_full_list (default) and a substantive text,
    # send the full player list instead of skipping.
    if cfg.player_list_mode == "full":
        plist = render_player_list(player_list)
    else:
        relevant = _relevant_players(text, player_list)
        if not relevant:
            if not (getattr(cfg, "fallback_full_list", True) and len(text) >= 200):
                return []  # nothing to extract — a skip, not an error
            plist = render_player_list(player_list)
        else:
            plist = render_player_list(relevant)

    merged: dict[tuple[int, str], dict] = {}
    failed: list[int] = []
    started = now_utc()

    async with httpx.AsyncClient(timeout=_llm_timeout(cfg)) as client:
        for ci, chunk in enumerate(chunks):
            messages = [
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
            ]
            t0 = time.monotonic()
            try:
                content, finish = await asyncio.wait_for(
                    _chat_once(client, settings, messages, cfg.max_tokens),
                    timeout=cfg.chunk_wallclock_sec,   # FIX N1: wall-clock ceiling
                )
                signals = _signals_from_content(content, finish, valid_ids)
                if finish == "length":
                    # A6: truncated even though it parsed — flag it on the
                    # successful path too (previously only mentioned inside
                    # the salvage/error messages).
                    log.warning("LLM output truncated (finish_reason=length) — %d signal(s) "
                                "recovered; consider raising llm.max_tokens", len(signals))
            except asyncio.CancelledError:
                # FIX N1: shutdown/cancellation is not a chunk error — log
                # distinctly and re-raise so the event loop can exit cleanly.
                log_poll("llm", "error", error="cancelled", started_at=started)
                raise
            except (httpx.HTTPError, asyncio.TimeoutError, KeyError, IndexError,
                    json.JSONDecodeError, ValidationError, RuntimeError) as e:
                # FIX N2: one bad chunk must not destroy the item's other
                # chunks — log per-chunk (with its index) and continue.
                dur = time.monotonic() - t0
                failed.append(ci)
                log_poll("llm", "error",
                         error=f"chunk {ci + 1}/{len(chunks)} after {dur:.1f}s: "
                               f"{type(e).__name__}: {e}"[:500],
                         started_at=started)
                continue
            for s in signals:
                k = (s["player_id"], s["category"])
                if k not in merged or s["confidence"] > merged[k]["confidence"]:
                    merged[k] = s

    if failed and len(failed) == len(chunks):
        return None   # FIX N2/N10: every chunk failed → hard error → retryable
    # FIX N5: successful LLM calls are logged too (latency/health for the
    # Settings poll-log UI; also drives the N9 circuit breaker).
    log_poll("llm", "ok", rows=len(merged), started_at=started)
    return list(merged.values())


def _probe_timeout(settings) -> float:
    """A20: the probe's timeout is the configured llm.timeout_sec (falling
    back to stream_idle_timeout_sec), not a hard-coded literal — a cold local
    model can take >15 s to answer a healthy 2-token reply while the real
    extraction path is allowed llm.timeout_sec (default 300)."""
    llm = getattr(getattr(settings, "config", None), "llm", None)
    for name in ("timeout_sec", "stream_idle_timeout_sec"):
        try:
            t = float(getattr(llm, name, 0) or 0)
        except (TypeError, ValueError):
            t = 0.0
        if t > 0:
            return t
    return 300.0


async def test_llm_connection(settings, *, base_url=None, api_key=None, model=None) -> dict:
    """Trivial chat call for the Settings 'Test Connection' button (T2.13).

    Returns {ok, status, detail, finish_reason, usage} — never raises.
    A20: keyword overrides let the Settings endpoint probe the *draft* values
    (A4) through this same helper, so the two probes cannot drift; the probe
    budget is 256 tokens (an 8-token budget is consumed by
    reasoning_content before the answer starts on thinking models, A5/A6);
    finish_reason/usage ride along so an empty reply is diagnosable (A5).
    """
    eff_base = (base_url or settings.llm_base_url or "").strip()
    eff_key = (api_key or settings.llm_api_key or "").strip()
    eff_model = (model or settings.llm_model or "").strip()
    if not settings.llm_ready and not (eff_key and eff_model):
        return {
            "ok": False,
            "status": "not_configured",
            "detail": "Set the LLM model name and API key (or .env LLM_MODEL / LLM_API_KEY).",
            "finish_reason": None,
            "usage": None,
        }
    url = eff_base.rstrip("/") + "/chat/completions"
    body = {
        "model": eff_model,
        "temperature": 0,
        "max_tokens": 256,   # A20: thinking models burn 8 tokens on reasoning
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
    }
    # A20 rev 2: keep connect/pool tight — a bare float timeout applies to every
    # phase, so a blackholed host would hang the Settings button for the full
    # read budget (llm.timeout_sec, 300 s default) with no feedback. Only the
    # read phase needs the generous budget (a cold local model can be slow).
    timeout = httpx.Timeout(connect=10.0, read=_probe_timeout(settings),
                            write=30.0, pool=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {eff_key}"},
            )
        if r.status_code == 200:
            data = r.json()
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message", {})
            content = msg.get("content", "")
            finish = choice.get("finish_reason")
            usage = data.get("usage")
            if not content:
                # A5: a 200 with empty content is not a healthy connection —
                # the model spent its budget on reasoning_content.
                reasoning = msg.get("reasoning_content") or ""
                detail = ("HTTP 200 but empty content"
                          + (f" — model returned {len(reasoning)} chars of reasoning_content; "
                             "raise llm.max_tokens" if reasoning else
                             " — check llm.max_tokens / the model's context window"))
                if finish:
                    detail += f" (finish_reason={finish})"
                return {"ok": False, "status": "empty_content", "detail": detail,
                        "finish_reason": finish, "usage": usage}
            return {"ok": True, "status": "ok",
                    "detail": f"model={eff_model} replied: {content[:60]!r}",
                    "model": eff_model,
                    "reply": content[:100],
                    "finish_reason": finish, "usage": usage}
        return {"ok": False, "status": f"http_{r.status_code}", "detail": r.text[:200],
                "finish_reason": None, "usage": None}
    except Exception as e:
        # A20 rev 2: catch-all, not just httpx.HTTPError — a 200 whose body is
        # not JSON (a proxy returning an HTML error page) used to escape as
        # ValueError and 500 the /settings/test-llm endpoint. "Never raises"
        # has to include that.
        return {"ok": False, "status": "error", "detail": f"{type(e).__name__}: {e}"[:200],
                "finish_reason": None, "usage": None}