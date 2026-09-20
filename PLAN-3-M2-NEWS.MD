# FPL Team Optimizer — Part 3: M2 News Layer (fetchers → signals → optimizer)

> **Prereqs**: M1 complete (Part 2 exit run done). This part covers tasks **T2.1–T2.13**.
> **M2 exit criteria** (outline §10): news refresh produces per-player signals; suggestions visibly change with signals; works with LLM off (rule-based fallback).
> **Data flow**: `fetchers/*` → `raw_items` (deduped) → `signals/*` (name match → extract → validate → store) → `optimizer/scoring.py` (`A(p)` + `S(p)` live) → UI (News & Signals page).

---

## T2.1 — Raw-item ingestion layer

**Goal**: one dedupe-and-store function every fetcher uses.

**Files**: `backend/app/signals/ingest.py`.

**Key functions**:
```python
def content_hash(source: str, title: str, body: str | None) -> str:
    """sha256 of f"{source}|{title.strip().lower()}|{(body or '')[:2000].strip().lower()}" —
    title-only dedupe works for articles (same story, same title, new URL) and transcripts."""

def ingest_item(source: str, external_id: str, kind: str, title: str, url: str,
                published_at: str | None, body: str | None = None, takeaways: list[str] | None = None) -> int | None:
    """INSERT ... ON CONFLICT(source, content_hash) DO NOTHING; returns row id if new, None if duplicate.
    Sets processed=0 for new items (extraction queue)."""

def pending_items(limit: int = 50) -> list[dict]   # WHERE processed=0 ORDER BY retrieved_at ASC
def mark_processed(item_id: int, takeaways: list[str] | None = None) -> None
```
**Logic**: `published_at` parsed to UTC ISO; keep as-is if unparseable (NULL). `body` truncated to 200k chars at storage (transcripts ~75 KB are fine). All fetchers must go through `ingest_item` — no direct `raw_items` writes elsewhere.

**Acceptance**: unit test — double-ingest of the same (source, title, body) → second returns None; title change → new row.

---

## T2.2 — BBC fetcher

**Goal**: RSS → filtered PL items in `raw_items` (V6: feed contains non-PL football).

**Files**: `backend/app/fetchers/bbc.py`.

**Spec**:
- Source: `https://feeds.bbci.co.uk/sport/football/rss.xml` (feedparser).
- **PL relevance filter** (item stored only if it passes): title+description matches any of:
  - "Premier League" / "PL " / "Premier" (careful: "Premier" alone can hit EFL — require "Premier League" or a club name),
  - any of the 20 current PL club names (built from the `teams` in bootstrap — keep a static fallback list of the 2026/27 clubs in code, refreshed from API team names at runtime: FPL `teams[]` has `short_name`/`name` — use those, mapped loosely: match on short_name and the two common aliases kept in a small dict, e.g. Spurs/Tottenham, Utd suffixes),
  - player web_names from the current season (strong signal: article about a specific FPL player is PL-relevant).
- Store: `source='bbc'`, `kind='article'`, `external_id`=guid/link, `title`, `url`=link, `published_at`=pubDate, `body`=description (RSS description is the summary — sufficient for signals, D13).
- **On-demand full article**: `POST /api/items/{id}/fetch-body` → if `source='bbc'` and URL fetchable: GET the article page, extract main text (strip tags; take `<article>`/`main` text if present, else first 8k chars of body text), update `raw_items.body`, mark for re-extraction (`processed=0`). Non-fatal on failure.
- Cadence: 30 min (15 min in GW-eve window — scheduler wiring in M3 T3.8; the interval lives in config now).

**Acceptance**: `test_extractors.py`/`test_api.py` with `sample_bbc.xml` fixture: PL items stored, WSL/EFL/Scotland items filtered out; on-demand fetch updates body (mocked HTTP).

---

## T2.3 — ESPN news fetcher

**Goal**: ESPN PL news headlines → `raw_items`.

**Files**: `backend/app/fetchers/espn.py`.

**Spec**:
- Source: `GET https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/news` → `articles[]` (fields: `headline`, `description`, `links.web.href`, `published`).
- Store: `source='espn'`, `kind='article'`, `external_id`=article id, title=headline, body=description, url=web href.
- (Standings `.../eng.1/standings` — **not** fetched in M2; optional M4 add-on, outline §4.6.)
- Cadence: 30 min.

**Acceptance**: fixture `sample_espn_news.json` → items stored with correct fields; no crash on empty `articles`.

---

## T2.4 — Reddit fetcher

**Goal**: r/FantasyPL hot (Atom) → `raw_items`, with the V5 gotchas handled.

**Files**: `backend/app/fetchers/reddit.py`.

**Spec**:
- Source: `https://www.reddit.com/r/FantasyPL/hot.rss` (feedparser — handles Atom; `link` may be a dict → take `['href']`).
- **Skip the pinned moderator megathread**: entries whose author is `u/FPLModerator` (or whose link ends in a known pinned thread pattern) are skipped (log `poll_log` note, count them).
- **Image-only posts**: `content` HTML often contains only a `<table>` with `<img>` (e.g. "GW5 Lineups | Newcastle United Vs. Hull City"). Extract: `title` + all `<img alt>` text + any `<p>` text. If the resulting text is < 40 chars: store with `body`=title only and flag `kind='thread'` — the extractor treats it as title-only (no false signals from images alone).
- **Thread bodies (optional, best-effort)**: for posts with ≥50 chars of text and a title matching `TRANSFER|INJURY|CONFIRMED|DOUBT|LINEUP|START` (case-insensitive), fetch `https://www.reddit.com/r/FantasyPL/comments/{id}.rss` (top-level comments, first 30) and append as `body` (truncated 20k). Rate: max 5 thread fetches per poll. Failure → non-fatal, item stored with title-level text.
- Store: `source='reddit'`, `kind='thread'`, `external_id`=post id (from `t3_xxx`), url=permalink.
- OAuth mode (documented only, off by default): if `config.sources.reddit.oauth_client_id` set, use `www.reddit.com/r/FantasyPL/hot.json?limit=25` with the app credentials — same downstream path. **ASK at M4** (PLAN.MD §13.2); M2 implements RSS only + the code path stub.
- Cadence: 30 min.

**Acceptance**: `sample_reddit.xml` fixture (real snapshot incl. one image-only post + pinned megathread): megathread skipped, image-only post stored title-only, text post stored with content; thread-body fetch mocked.

---

## T2.5 — YouTube fetcher (two-stage, V7/V8)

**Goal**: channel RSS every poll (cheap signals from title+description) + transcripts for priority videos only (yt-dlp).

**Files**: `backend/app/fetchers/youtube.py`.

**Stage A — channel RSS**:
- Source: `https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}` per channel in `config.sources.youtube.channels` (default: `UC8043oOKTB4uP8Nq15Kz6bg`, D10).
- Handle resolution: if a configured channel has `handle` but no `channel_id`, resolve once via `yt-dlp --no-update --print "%(channel_id)s" "https://www.youtube.com/{handle}/videos"` (subprocess, 30 s timeout) and persist into `config.json`.
- Per entry: `yt:videoId`, `title`, `published`, `media:description` (full — contains chapter lists & player names, V7).
- Store: `source='youtube'`, `kind='video'`, `external_id`=videoId, `title`, `url`=`https://www.youtube.com/watch?v={id}`, `body`=description (NOT the transcript yet).
- **Priority classifier** (decides transcript fetch): `priority(title)` = 1 if title (lowercased) contains any `transcript_keywords` config entry (default: `weekender`, `deadline stream`, `cotc`, `clash of the correspondents`, `review`); else 0. **Skip Shorts** (link contains `/shorts/` → priority 0, description-only).
- **Transcript budget**: per poll, take priority-1 items that are new (`processed=0` or new row) and have no transcript yet, newest first, max `max_transcripts_per_poll` (default 3).

**Stage B — transcript fetch (yt-dlp, D11)**:
```python
def fetch_transcript(video_id: str, tmp_dir: str) -> str | None:
    """subprocess.run([
        yt-dlp, '--no-update',
        '--write-auto-sub', '--sub-langs', 'en',
        '--skip-download', '--convert-subs', 'vtt',
        '--js-runtimes', 'node',            # Node is installed for the frontend (V8)
        '--output', f'{tmp_dir}/%(id)s',
        f'https://www.youtube.com/watch?v={video_id}',
    ], capture_output=True, timeout=180)
    On success: read {tmp_dir}/{video_id}.en.vtt → vtt_to_text() → DELETE the .vtt (skill rule:
    VTT is an intermediate artifact) → return text.
    On failure (bot-check/geo): log poll_log error, return None (item stays description-only;
    if --write-auto-sub failed, ONE retry with --write-sub; still None → give up, non-fatal)."""

def vtt_to_text(vtt_path: str) -> str:
    """Dedupe algorithm (verified in V9 test): drop WEBVTT/Kind:/Language:/-->/empty lines,
    strip <...> tags, unescape &amp; &gt; &lt;, drop exact-duplicate lines preserving order."""
```
- On transcript: update `raw_items.body` = description + "\n\n--- TRANSCRIPT ---\n" + transcript (truncated to `llm_truncate_chars` default 12000 **for LLM input only** — full text kept in `body` for UI display).
- Adapter interface (D11): `class TranscriptProvider(Protocol): def fetch(self, video_id: str) -> str | None` — `YtDlpProvider` (default) and `TranscriptApiProvider` (optional `youtube-transcript-api` import, used only if env `YOUTUBE_TRANSCRIPT_BACKEND=api`).

**Acceptance**: with the live channel (or a fixture `sample_youtube.xml` of the real feed): items stored with descriptions; classifier flags "The Weekender ep. 48" as priority-1 and a Short as 0; `fetch_transcript` on `kU_mDgO3urA` returns ~2k+ lines of text (or a clean logged failure); VTT deleted after; budget respected (≤3 per poll).

---

## T2.6 — Player name matching (extraction-grade)

**Goal**: robust name → FPL player-id resolution for extractors, tolerant of auto-caption mis-transcriptions (V9).

**Files**: `backend/app/signals/names.py` (extend T1.9's `match_names` with extraction helpers).

**Key functions**:
```python
def name_index(players: list[PlayerRow]) -> dict:
    """Build: exact web_name(lower) → ids, known_name(lower) → ids, first+last variants,
    token sets for fuzzy. Rebuilt per season (cache keyed by players.fetched_at)."""

def resolve_name(name: str, idx: dict) -> list[tuple[int, float]]:
    """1) exact web_name/known_name (case-insensitive) → [(id, 1.0)]
    2) Levenshtein ≤ 2 on web_name/known_name (token-aware: compare against both full name and
       'first last' / 'last first') → [(id, 1 - dist/max_len)]
    3) substring (last name ≥ 4 chars) → [(id, 0.8)]
    Returns all candidates with score ≥ 0.6, sorted desc."""

def resolve_candidates(text: str, idx: dict) -> list[dict]:
    """Scan text for any player name occurrence (word-boundary, case-insensitive) using the index
    (precompiled regex alternation over web_name + known_name + common misspellings list).
    Misspellings list: static dict of known auto-caption patterns observed (V9):
    {'calbertt lewing': 'Calvert-Lewin', 'leads united': 'Leeds United', ...} — extendable in code.
    Return [{name_in_text, player_id, score, span}] — only used by the LLM path's prompt builder
    (to pre-filter) and by the rule path (conservative: exact matches only)."""
```
**Rule**: unmatched names are **logged, never guessed** (a rule-based signal for an unmatched name is dropped).

**Acceptance**: `test_match_names.py` M2 additions: "Calvert-Lewin" exact; "Calvert-Lewine" fuzzy (dist 1); "Leeds" does NOT resolve to a player (team name — must be excluded: index contains players only); misspelling dict hits; resolve_candidates finds Havertz in a paragraph without false positives on "Havert".

---

## T2.7 — Rule-based extractor (fallback + official-news)

**Goal**: deterministic signals with LLM off (D5). Two inputs: (a) `raw_items` text via keyword dictionary; (b) official FPL news/status changes (highest value).

**Files**: `backend/app/signals/rule_extractor.py`.

**(a) Keyword dictionary** (apply to item title+body, per-player context = sentences containing a resolved player name):
```python
KEYWORDS: dict[str, list[tuple[str, str, float]]] = {
  # category: [(pattern (case-insensitive regex), sentiment, confidence)]
  "injury":     [(r"\b(injur\w+|hamstring|ankle|knee|calf|thigh|quad|groin|ruled out|out for \d+|surgery|rehab)\b", "negative", 0.4),
                 (r"\b(doubt|uncertain|question mark|50-50|chance of playing)\b", "negative", 0.35)],
  "suspension": [(r"\b(suspend\w+|sent off|red card|serving a (ban|ban\b)|banned)\b", "negative", 0.5)],
  "selection":  [(r"\b(confirmed to start|expected to start|to start|in the (starting )?(lineup|xi)|starts? (for|vs|against))\b", "positive", 0.5),
                 (r"\b(bench\w*|left out|out of the (squad|team)|rest\w*|rotat\w*)\b", "negative", 0.4)],
  "return":     [(r"\b(back (in|for)|return\w*|available again|fit to play|cleared)\b", "positive", 0.5)],
  "transfer":   [(r"\b(transfer\w*|signing|joined|permanent deal|loan\w*|released)\b", "neutral", 0.5)],
}
```
**Sentence scoping**: split item text into sentences; a keyword counts only in a sentence that also contains a resolved player name (exact match, not fuzzy — conservative per V9). One signal per (player, category, item) — take the max-confidence pattern; `summary` = the sentence (trimmed to 200 chars).
**Confidence policy**: rule signals never exceed 0.6 (LLM/official can go higher). Item age > 7 days → skip (stale).
**YouTube transcripts**: rule path applies **title + description only**, never the transcript body (auto-caption noise, V9) — the transcript is LLM-only material.

**(b) Official FPL news ingest** (runs on every bootstrap fetch, `fetchers/fpl.py` → this module):
```python
def ingest_official_news(players: list[PlayerRow], prev: dict[int, dict]) -> list[SignalRow]:
    """For each player whose (status, news, chance_of_playing_next_round) changed since prev:
    - status 'i' or 's' or 'u' → category injury/suspension/unavailable, sentiment negative, confidence 1.0,
      summary = news text (or status description)
    - status 'd' → selection, negative, 0.7
    - status back to null/'a' with previous 'i'/'s' → return, positive, 0.9
    - chance_of_playing_next_round: 0 → negative 0.8; 50 → negative 0.5; 100 (from lower) → positive 0.6
    - news contains "doubt" → selection negative 0.7
    Source = 'fpl-official', url = None, published_at = news_added or now.
    `prev` = last-seen snapshot kept in a small table (add: CREATE TABLE official_news_cache
    (player_id INTEGER PRIMARY KEY, status TEXT, news TEXT, chance INTEGER, updated_at TEXT)) —
    include this DDL in db.py at M2."""
```
**Acceptance**: `test_extractors.py` rule section: each dictionary category fires with the exact category/sentiment/confidence; a sentence with a keyword but no player name → no signal; "Calbertt Lewing ... injury" → **no** rule signal (fuzzy not allowed in rule path); official news: status null→i → injury conf 1.0; i→null → return conf 0.9; chance 100→0 → negative 0.8.

---

## T2.8 — LLM extractor

**Goal**: high-quality structured signals from item text (incl. transcripts) via the user's local OpenAI-compatible endpoint (D5/D12).

**Files**: `backend/app/signals/llm_extractor.py`.

**Client**:
```python
async def extract_signals_llm(text: str, source: str, published_at: str | None,
                              player_list: list[dict]) -> list[dict]:
    """POST {llm.base_url}/chat/completions via httpx (NOT the shared HttpClient — different timeout 60 s,
    Authorization: Bearer {llm_api_key}).
    model = config.llm.model (env LLM_MODEL wins).  temperature 0.  max_tokens 2000.
    messages = [system=SYSTEM_PROMPT, user=USER_TEMPLATE.format(player_list=rendered, source=source,
              published_at=published_at, text=text[:cfg.sources.youtube.llm_truncate_chars if source=='youtube' else 8000])]
    Response parsing: choices[0].message.content → strip code fences if present → json.loads →
    validate with pydantic (List[PlayerSignal]) → enforce: player_id ∈ player_list ids (drop others),
    confidence clamped 0–1, category/sentiment in allowed sets.
    Failures (401/timeout/parse/schema): log poll_log(source='llm', status='error', error=str(e)),
    return [] — NEVER crash the pipeline. Item still gets rule extraction (T2.7) as backstop."""
```

**SYSTEM_PROMPT** (exact, put in the module):
```
You are a Fantasy Premier League (FPL) news analyst. You extract structured player signals from sports content.

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
```

**USER_TEMPLATE**:
```
Player list (player_id | full name | club code):
{player_list}

Source: {source}
Published: {published_at}

Content:
{text}
```
**`player_list` rendering**: all current-season players (id | "First Last" | club code) — ~500 lines, ~15 KB; that's fine for a local LLM (V10). If `len(text) > batch_chars` (8000, non-YouTube): split into ~8k-char chunks at paragraph boundaries, call per chunk, merge + dedupe (same player+category → keep max confidence).

**Acceptance**: `test_extractors.py` LLM section (mocked httpx transport): valid JSON → parsed + filtered (unknown player_id dropped); code-fenced JSON → stripped + parsed; invalid JSON → [] + poll_log error; 401 → [] + error logged; transcript fixture (weekender_ep48.txt) with a canned LLM response → signals land in DB with correct player ids and no crash.

---

## T2.9 — Signal store, dedupe, expiry + pipeline runner

**Goal**: store extracted signals with dedupe + TTL; one pipeline function per new item.

**Files**: `backend/app/signals/store.py`, `backend/app/signals/pipeline.py`.

**Key functions**:
```python
def save_signals(signals: list[dict], source: str, url: str | None, published_at: str | None,
                 raw_item_id: int | None) -> int:
    """For each signal: dedupe — if an ACTIVE signal exists with same (player_id, category, source-prefix)
    (source-prefix = 'fpl-official' or the source host/url) within its TTL: keep the HIGHER confidence
    (update summary if newer), else INSERT. TTL: rule signals 72 h; LLM signals 72 h;
    official news: until replaced (expires_at = now + 14 d, refreshed on each new official signal for
    the same player+category). Returns count inserted/updated."""

def active_signals(player_id: int | None = None, team: str | None = None, category: str | None = None) -> list[dict]
    # WHERE expires_at > now (+ joins for web_name/team_code for the API)

def expire_stale() -> int    # called by scheduler; DELETE expires_at < now (keeps audit via raw_items)

async def process_item(item: dict) -> None:
    """1. candidates = resolve_candidates(item body/title, index)  [LLM path context]
    2. rule signals = rule_extract(item)
    3. llm signals = [] ; if llm enabled (key+model set): llm signals = await extract_signals_llm(...)
    4. merged = dedupe(rule + llm) (same player+category: keep max confidence, prefer llm summary)
    5. save_signals(merged, ...)
    6. takeaways (LLM side-call, best-effort): one 3-item list "what this item means for FPL" —
       only for LLM-enabled + kind in (article, video); stored on raw_items.takeaways
    7. mark_processed(item_id, takeaways)
    8. push SSE 'new_signal' events for any inserted signal (M3 wires the bus; M2: no-op hook)"""
```
**Scheduler hook**: `process_pending_items()` job (30 min cadence, after RSS fetches; also triggered by `POST /api/refresh/all`) — processes up to 50 pending items sequentially (LLM calls are the bottleneck; 50 × ~10 s worst case is acceptable offline).

**Acceptance**: `test_extractors.py` store section: dedupe keeps max confidence; TTL expiry math; official-news replacement refreshes expiry; `process_item` end-to-end on the weekender fixture (LLM mocked) → N signals in DB, takeaways set, item processed=1.

---

## T2.10 — Optimizer integration (A(p) + S(p) go live)

**Goal**: `optimizer/scoring.py` M2 mode — availability map + signal adjustment per outline §7.2.

**Files**: `backend/app/optimizer/scoring.py` (activate the M1-stubbed paths).

**Spec** (formula final):
```
A(p):
  status in ('u','s') or can_select == 0  → excluded from universe (hard gate, already M1)
  status == 'd'                            → 0.7
  chance_of_playing_next_round:
      NULL → 1.0        100 → 1.0        50 → 0.65        0 → 0.0 (effectively excluded)
  (map values from config.optimizer.availability)
S(p):
  neg = sum(confidence for active negative signals for p)
  pos = sum(confidence for active positive signals for p)
  S = clamp(-0.5 * neg, -0.6) + clamp(+0.1 * pos, +0.2)     (coefficients from config.optimizer.signal)
EP_final(p) = 0.7 * (ep_next * A(p) * (1 + S(p))) + 0.15 * form_adj(p) + 0.15 * fixture_adj(p)
  (form_adj / fixture_adj exactly as M1 T1.6)
score_lineup: baseline stays ep_next-only (no A/S) — the UI shows both so the user sees the news impact.
```
**Universe change**: `build_universe` (T1.10) adds the chance-0 exclusion and joins active signals per player (one query: `SELECT player_id, sentiment, confidence FROM signals WHERE expires_at > now GROUP BY player_id` aggregated in Python).
**Rationale change**: SuggestionObject `rationale` gains a line per applied signal on a XI player: `f"Signal: '{summary}' ({source}, conf {conf:.1f}) → {S:+.2f}"` (top 3 by |impact|).

**Acceptance**: `test_ep_model.py` M2 section: A(p) map exact (d→0.7, chance 50→0.65, 0→excluded); S(p) stacking (two negative conf 0.8 → -0.6 capped; three positive 0.5 → +0.2 capped); mixed neg+pos arithmetic; end-to-end: a player with a negative official signal drops out of a Max-EP suggestion that would otherwise include him (crafted universe).

---

## T2.11 — News & Signals page

**Goal**: the full News page (replaces the M1 placeholder) — signals table + source tabs + refresh (AC5 UI).

**Files**: `frontend/src/pages/News.tsx` + `components/SignalsTable.tsx`, `SourceTabs.tsx`, `ItemCard.tsx`.

**Behavior spec**:
- **Top: SignalsTable** (default view): columns player (name+club), category badge (color-coded: injury red, suspension orange, selection green, rotation yellow, return blue, transfer purple, other grey), sentiment icon (▼ negative / ▲ positive / • neutral), confidence bar, summary, source (truncated url, clickable), published (relative time), expires (relative). Filters: player search, club, category, "active only" (default on). Sort: confidence desc, then published desc.
- **Bottom: SourceTabs** (BBC / ESPN / Reddit / YouTube / FPL-official): each tab lists `GET /api/items?source=` (latest 50): ItemCard = title (link), published, takeaways (bullets, "LLM" tag when present), kind icon (📰 article / 💬 thread / 🎥 video), "Refresh now" button per tab (`POST /api/refresh/{source}`) + body preview for YouTube items (description + transcript excerpt, expandable).
- **LLM status chip** (top-right): "LLM: on (model X)" / "LLM: off — rule-based mode" from `GET /api/settings`.
- **Empty states**: "No signals yet — click Refresh now on a source tab."
- Poll signals every 5 min (M3 adds SSE `new_signal` live updates).

**Acceptance** (manual checklist item 5): refresh each source → items appear with correct filtering (BBC has no WSL items); signals table populates (rule path with LLM off; LLM path when configured); filters work; YouTube card shows description + (if fetched) transcript excerpt.

---

## T2.12 — M2 tests + fixtures

**Goal**: M2 golden tests green.

**Files**: extend `backend/tests/`:
- Copy fixture: `cp "tmp-video-test/The Weekender ep. 48  Planet FPL 202627.txt" backend/tests/fixtures/weekender_ep48.txt` (original stays untouched — user decision).
- Capture fixtures from live sources during dev: `sample_bbc.xml`, `sample_reddit.xml` (must include the pinned megathread + one image-only post), `sample_espn_news.json`, `sample_youtube.xml` (real Planet FPL feed), `sample_llm_response.json` (canned valid chat/completions response).
- `test_extractors.py` (full: T2.7/T2.8/T2.9 sections), `test_api.py` additions (`/api/signals`, `/api/items`, `POST /api/items/{id}/fetch-body`, `POST /api/refresh/{source}`), `test_ep_model.py` M2 section.

**Acceptance**: `pytest backend/tests -q` green; LLM-off e2e: with `LLM_API_KEY` empty, `POST /api/refresh/all` on a seeded DB produces ≥1 official-news signal and ≥1 keyword signal (seed a BBC item with "X is a doubt"); LLM-on e2e (mocked): transcript fixture yields ≥3 signals incl. one for a player named via the misspelling map.

---

## T2.13 — ASK: LLM model name (the only M2 user question)

**Goal**: configure the LLM path (V10: endpoint up, key-gated, model unknown).

**Steps**:
1. Ask the user (this is a marked ASK, PLAN.MD §13.1): "What model name should the app send to `http://localhost:8888/v1`? (and confirm the API key is in `.env` — LLM_API_KEY)."
2. Set `.env` `LLM_MODEL` (and/or `config.json` `llm.model`); verify with a real call: `POST /chat/completions` with a trivial prompt → expect 200 + JSON. On 401 → tell the user the key is missing/wrong; the app continues in rule-based mode (D5) — **do not block M2 exit on this**.
3. If the user declines/has no model: confirm rule-based mode is the working configuration; document it in README ("LLM optional").
4. Run the M2 exit criteria (outline §10): news refresh → per-player signals; suggestions visibly change (toggle a signal on/off in DB → regenerate → `adjusted` differs); LLM-off path demonstrated.
5. Git commit: `M2: news layer (fetchers, extractors, signals, optimizer integration)`.

**M2 is done when**: outline §10 M2 exit + AC5 demonstrably pass (both LLM modes).