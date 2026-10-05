"""Configuration: config.json (user-editable, incl. via Settings UI) + .env overrides.

Precedence for LLM values: .env wins when non-empty, else config.json.
The API key is stored locally only (config.json or .env), never logged, and only ever
sent to the configured LLM endpoint.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "fpl.db"
CONFIG_PATH = ROOT / "config.json"
ENV_PATH = ROOT / ".env"

load_dotenv(ENV_PATH)


class YouTubeChannel(BaseModel):
    handle: str = ""
    channel_id: str = ""


class FplSource(BaseModel):
    enabled: bool = True
    bootstrap_interval_min: int = 15
    live_players_interval_sec: int = 60
    live_matches_interval_sec: int = 30


class EspnSource(BaseModel):
    # Disabled by default: ESPN's API returned 403 from the user's network (verified
    # 2026-09-19). FPL /api/fixtures/ is the primary live-score source; ESPN is an
    # optional add-on (news + live) the user can enable in Settings.
    enabled: bool = False
    news_interval_min: int = 30
    live_interval_sec: int = 30


class BbcSource(BaseModel):
    enabled: bool = True
    interval_min: int = 30
    fetch_bodies: bool = True        # auto-fetch full article text for new items
    max_bodies_per_poll: int = 10    # politeness cap per refresh


class RedditSource(BaseModel):
    enabled: bool = True
    interval_min: int = 30
    mode: str = "rss"  # "rss" | "oauth" (client-credentials, app-only; falls back to RSS)
    oauth_client_id: str = ""
    oauth_client_secret: str = ""


class YouTubeSource(BaseModel):
    enabled: bool = True
    interval_min: int = 60
    channels: list[YouTubeChannel] = Field(
        default_factory=lambda: [
            YouTubeChannel(handle="@PlanetFPL", channel_id="UC8043oOKTB4uP8Nq15Kz6bg")
        ]
    )
    transcript_keywords: list[str] = Field(
        default_factory=lambda: [
            "weekender",
            "deadline stream",
            "cotc",
            "clash of the correspondents",
            "review",
        ]
    )
    max_transcripts_per_poll: int = 3
    llm_truncate_chars: int = 12000


class SourcesConfig(BaseModel):
    fpl: FplSource = Field(default_factory=FplSource)
    espn: EspnSource = Field(default_factory=EspnSource)
    bbc: BbcSource = Field(default_factory=BbcSource)
    reddit: RedditSource = Field(default_factory=RedditSource)
    youtube: YouTubeSource = Field(default_factory=YouTubeSource)


class LLMConfig(BaseModel):
    enabled: bool = True
    base_url: str = "http://localhost:8888/v1"
    api_key: str = ""
    model: str = ""
    # FIX N1: wall-clock cap for the non-streaming fallback (and the overall
    # per-chunk ceiling) — a wedged server must fail in ≤ ~5 min so the
    # extraction queue keeps moving. NOT a read-timeout workaround.
    timeout_sec: int = 300
    batch_chars: int = 8000
    # FIX N1: output cap matches the expected payload (a few hundred tokens).
    # Gemma is not a thinking model — the old "thinking + answer" rationale was
    # wrong; a huge cap only lets degenerate generations wander for minutes.
    max_tokens: int = 2048
    player_list_mode: str = "filtered"  # "filtered" (named players only) | "full" (whole universe)
    # FIX N1: streaming budgets — bytes flow while generating, so timeouts
    # become inactivity guards instead of total-duration ceilings.
    stream_idle_timeout_sec: int = 90   # SSE read timeout: no tokens for this long = failure
    chunk_wallclock_sec: int = 300      # asyncio.wait_for belt-and-suspenders per chunk
    # FIX N1: connect/timeout retries for the non-streaming fallback.
    retries: int = 2
    # FIX N6: when the filtered player list is empty (no name resolved) and the
    # text is long enough, send the full list instead of silently skipping.
    fallback_full_list: bool = True
    # FIX N7: truncation for non-YouTube sources (YouTube keeps its override).
    truncate_chars: int = 8000
    # FIX N8: time-budget for one extraction pass (items stay pending for the
    # next pass when exceeded; the scheduler job coalesces).
    extract_timebox_sec: int = 480


class OptimizerWeights(BaseModel):
    ep: float = 0.7
    form: float = 0.15
    fixture: float = 0.15


class AvailabilityConfig(BaseModel):
    # M1: active=False → A(p) is pass-through except hard gates (u/s/can_select=0).
    # M2 (T2.10) flips this to True to activate the doubt/chance map.
    active: bool = False
    doubt: float = 0.7
    chance_null: float = 1.0
    chance_100: float = 1.0
    chance_75: float = 0.85   # FIX T2: bucket for 75 ≤ chance < 100
    chance_50: float = 0.65
    chance_25: float = 0.35   # FIX T2: bucket for 25 ≤ chance < 50
    chance_0: float = 0.0     # FIX T2: also used for 0 < chance < 25 (effectively out)


class SignalConfig(BaseModel):
    neg_per: float = -0.5
    neg_cap: float = -0.6
    pos_per: float = 0.1
    pos_cap: float = 0.2


class SolverConfig(BaseModel):
    restarts: int = 20
    timebox_sec: int = 8
    seed: int = 42
    # FIX T7: hard move budget per restart (0 = disabled). Makes hill-climb
    # results reproducible across machines; the timebox stays the ceiling.
    max_moves_per_restart: int = 2000


class OptimizerConfig(BaseModel):
    weights: OptimizerWeights = Field(default_factory=OptimizerWeights)
    availability: AvailabilityConfig = Field(default_factory=AvailabilityConfig)
    signal: SignalConfig = Field(default_factory=SignalConfig)
    differential_lambda: float = 3.0
    differential_ep_floor: float = 0.4
    solver: SolverConfig = Field(default_factory=SolverConfig)


class GroupConfig(BaseModel):
    fpl_entry_id: str = ""


class UIConfig(BaseModel):
    theme: str = "dark"


class ConfigFile(BaseModel):
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    optimizer: OptimizerConfig = Field(default_factory=OptimizerConfig)
    group: GroupConfig = Field(default_factory=GroupConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    maps: dict[str, Any] = Field(default_factory=dict)


def default_config() -> ConfigFile:
    return ConfigFile()


def load_config() -> ConfigFile:
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return ConfigFile.model_validate(raw)
        except Exception:
            pass  # corrupt config → defaults (file kept for inspection)
    cfg = default_config()
    save_config(cfg)
    return cfg


def save_config(cfg: ConfigFile) -> None:
    CONFIG_PATH.write_text(
        json.dumps(cfg.model_dump(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


class Settings(BaseModel):
    """Runtime settings: config.json + .env overrides for the LLM (env wins when set)."""

    config: ConfigFile
    llm_base_url: str
    llm_api_key: str
    llm_model: str

    @property
    def llm_ready(self) -> bool:
        return bool(self.config.llm.enabled and self.llm_api_key and self.llm_model)


def load_settings() -> Settings:
    cfg = load_config()
    base_url = os.getenv("LLM_BASE_URL", "").strip() or cfg.llm.base_url
    api_key = os.getenv("LLM_API_KEY", "").strip() or cfg.llm.api_key
    model = os.getenv("LLM_MODEL", "").strip() or cfg.llm.model
    return Settings(config=cfg, llm_base_url=base_url, llm_api_key=api_key, llm_model=model)