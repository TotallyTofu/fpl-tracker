"""Capture live source feeds as test fixtures (M2 T2.12) + network reality check."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "backend" / "tests" / "fixtures"
FIX.mkdir(parents=True, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FPL-Tracker/0.2"}

TARGETS = {
    "sample_bbc.xml": "https://feeds.bbci.co.uk/sport/football/rss.xml",
    "sample_reddit.xml": "https://www.reddit.com/r/FantasyPL/hot.rss",
    "sample_youtube.xml": "https://www.youtube.com/feeds/videos.xml?channel_id=UC8043oOKTB4uP8Nq15Kz6bg",
    "sample_espn_news.json": "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/news",
}


def main() -> None:
    with httpx.Client(timeout=30, headers=UA, follow_redirects=True) as client:
        for fname, url in TARGETS.items():
            try:
                r = client.get(url)
                status = r.status_code
                (FIX / fname).write_bytes(r.content)
                print(f"{fname}: HTTP {status}, {len(r.content)} bytes")
            except Exception as e:
                print(f"{fname}: ERROR {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()