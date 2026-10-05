"""Diagnostic: verify Reddit OAuth credentials + JSON endpoint access.

Usage (from backend/):  ..\\.venv\\Scripts\\python scripts\\probe_reddit_token.py

Reads the client id/secret from config.json (same source as the Settings UI),
requests a client-credentials token, then fetches r/FantasyPL hot.json on
both www.reddit.com and oauth.reddit.com to show which host accepts the
token. Never prints the secret or the token.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import httpx

CFG = Path(__file__).resolve().parents[2] / "config.json"
UA = "fpl-tracker/0.2 (local personal FPL assistant; contact: local)"


def main() -> int:
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    r = cfg.get("sources", {}).get("reddit", {})
    cid = (r.get("oauth_client_id") or "").strip()
    sec = (r.get("oauth_client_secret") or "").strip()
    print(f"mode={r.get('mode')!r} client_id_len={len(cid)} secret_len={len(sec)}")
    if not cid or not sec:
        print("STATUS: no credentials in config.json — enter them in Settings → Sources → Reddit")
        return 1

    basic = base64.b64encode(f"{cid}:{sec}".encode()).decode()
    try:
        resp = httpx.post(
            "https://www.reddit.com/api/v1/access_token",
            data={"grant_type": "client_credentials", "scope": "read"},
            headers={"Authorization": f"Basic {basic}", "User-Agent": UA},
            timeout=20,
        )
    except Exception as e:
        print(f"STATUS: token request error: {e}")
        return 1
    print(f"token endpoint: HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(f"BODY: {resp.text[:300]}")
        return 1
    token = resp.json().get("access_token", "")
    print(f"token acquired (len={len(token)}, expires_in={resp.json().get('expires_in')})")

    for host in ("www.reddit.com", "oauth.reddit.com"):
        url = f"https://{host}/r/FantasyPL/hot.json"
        try:
            r2 = httpx.get(
                url,
                params={"limit": 5},
                headers={"Authorization": f"Bearer {token}", "User-Agent": UA},
                timeout=20,
            )
            extra = ""
            if r2.status_code == 200:
                children = r2.json().get("data", {}).get("children", [])
                extra = f" — {len(children)} posts"
            print(f"{host}: HTTP {r2.status_code}{extra}")
        except Exception as e:
            print(f"{host}: error: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())