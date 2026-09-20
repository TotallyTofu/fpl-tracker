"""Shared async HTTP client (D17): UA, 15 s timeout, 2 retries w/ 2 s backoff,
semaphore(20) concurrency cap.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

import httpx

log = logging.getLogger("fpl.http")

USER_AGENT = "fpl-tracker/1.0 (local personal use)"
TIMEOUT = 15.0
MAX_CONCURRENT = 20
RETRIES = 2
BACKOFF = 2.0


class HttpClient:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._sem = asyncio.Semaphore(MAX_CONCURRENT)

    async def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=TIMEOUT,
                headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
                follow_redirects=True,
            )
        return self._client

    async def get_json(self, url: str, *, params: dict | None = None,
                       timeout: float | None = None, headers: dict | None = None) -> Any:
        return await self._get(url, params=params, parse=json.loads, timeout=timeout,
                               headers=headers)

    async def get_text(self, url: str, *, params: dict | None = None,
                       timeout: float | None = None, headers: dict | None = None) -> str:
        return await self._get(url, params=params, parse=lambda b: b.decode("utf-8", "ignore"),
                               timeout=timeout, headers=headers)

    async def post_json(self, url: str, *, payload: dict | None = None,
                        headers: dict | None = None, timeout: float | None = None) -> Any:
        c = await self.client()
        last_exc: Exception | None = None
        for attempt in range(RETRIES + 1):
            try:
                async with self._sem:
                    r = await c.post(url, json=payload, headers=headers,
                                     timeout=timeout or TIMEOUT)
                if r.status_code >= 500:
                    raise httpx.HTTPStatusError(f"5xx {r.status_code}", request=r.request,
                                                response=r)
                return r.status_code, r
            except (httpx.TimeoutException, httpx.HTTPStatusError) as e:
                last_exc = e
                if attempt < RETRIES:
                    await asyncio.sleep(BACKOFF * (attempt + 1))
        raise last_exc  # type: ignore[misc]

    async def _get(self, url: str, *, params: dict | None,
                   parse: Callable[[bytes], Any], timeout: float | None,
                   headers: dict | None = None) -> Any:
        c = await self.client()
        last_exc: Exception | None = None
        for attempt in range(RETRIES + 1):
            try:
                async with self._sem:
                    r = await c.get(url, params=params, timeout=timeout or TIMEOUT,
                                    headers=headers)
                if r.status_code >= 500:
                    raise httpx.HTTPStatusError(f"5xx {r.status_code}", request=r.request,
                                                response=r)
                r.raise_for_status()
                return parse(r.content)
            except (httpx.TimeoutException, httpx.HTTPStatusError) as e:
                last_exc = e
                if attempt < RETRIES:
                    await asyncio.sleep(BACKOFF * (attempt + 1))
        raise last_exc  # type: ignore[misc]

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()


http = HttpClient()  # module-level singleton