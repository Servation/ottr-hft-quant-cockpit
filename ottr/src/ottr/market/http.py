"""
Shared JSON-over-HTTP GET with bounded retries and an optional rate limiter.

What it does: `get_json` makes one logical request and retries transient failures
(transport errors, 429, 5xx, a provider's own "busy" errors) with exponential backoff,
honoring a server's Retry-After. Each provider supplies a `classify` that turns a response
into (result, error, retryable). `RateLimiter` is an async token bucket that keeps a client
under a provider's budget (Alpaca's free tier allows 200 requests a minute).

Why shared: OTTR talks to several data providers (Kraken now; Alpaca, FRED, Alpha Vantage
and the SEC next). One retry policy means every feed fails the same way, loudly and
bounded, instead of each client growing its own almost-identical loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional, Tuple, Type

import httpx

logger = logging.getLogger(__name__)

Classified = Tuple[Any, Optional[str], bool]   # (result, error, retryable)
MAX_RETRY_AFTER = 60.0                         # never sleep longer than this on a server's say-so


class HttpError(RuntimeError):
    """A request failed for good (non-retryable, or out of retries)."""


def classify_json(resp: httpx.Response) -> Classified:
    """Default: 2xx with a JSON body succeeds; 429/5xx and non-JSON retry; other 4xx fail."""
    if resp.status_code == 429 or resp.status_code >= 500:
        return None, f"HTTP {resp.status_code}", True
    if resp.status_code >= 400:
        return None, f"HTTP {resp.status_code}", False
    try:
        return resp.json(), None, False
    except ValueError:  # an HTML error page from a proxy/CDN: an outage, not a crash
        return None, "non-JSON response", True


def retry_after(resp: Optional[httpx.Response]) -> Optional[float]:
    """Seconds a server asked us to wait (Retry-After in seconds), if it said."""
    if resp is None:
        return None
    try:
        return max(0.0, float(resp.headers.get("retry-after", "")))
    except ValueError:
        return None


class RateLimiter:
    """Async token bucket: up to `capacity` requests at once, refilled at `per_minute`.

    `acquire()` waits until a token is free. Callers on one event loop share it safely:
    the lock serializes waiters so a burst can't overdraw the bucket.
    """

    def __init__(self, per_minute: float, capacity: Optional[float] = None,
                 now: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        if per_minute <= 0:
            raise ValueError("per_minute must be positive")
        self.rate = per_minute / 60.0
        self.capacity = float(capacity if capacity is not None else per_minute)
        self.tokens = self.capacity
        self._now, self._sleep = now, sleep
        self._ts = now()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        t = self._now()
        self.tokens = min(self.capacity, self.tokens + (t - self._ts) * self.rate)
        self._ts = t

    async def acquire(self) -> None:
        async with self._lock:
            self._refill()
            while self.tokens < 1.0:
                await self._sleep((1.0 - self.tokens) / self.rate)
                self._refill()
            self.tokens -= 1.0


async def get_json(client: httpx.AsyncClient, url: str, params: Optional[dict] = None, *,
                   classify: Callable[[httpx.Response], Classified] = classify_json,
                   retries: int = 3, backoff: float = 1.0, limiter: Optional[RateLimiter] = None,
                   headers: Optional[dict] = None, name: str = "request",
                   error: Type[HttpError] = HttpError,
                   sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> Any:
    """GET `url` and return the classified result, or raise `error` once retries run out."""
    delay = backoff
    for attempt in range(retries + 1):
        if limiter is not None:
            await limiter.acquire()
        resp: Optional[httpx.Response] = None
        try:
            resp = await client.get(url, params=params, headers=headers)
            result, err, retryable = classify(resp)
        except httpx.TransportError as e:
            # Type name only: an httpx message embeds the URL, and some providers (Alpha
            # Vantage) take their API key as a query parameter.
            result, err, retryable = None, f"transport: {type(e).__name__}", True
        if err is None:
            return result
        if not retryable or attempt >= retries:
            raise error(f"{name} failed: {err}")
        wait = delay
        hinted = retry_after(resp)
        if hinted is not None:
            wait = max(wait, min(hinted, MAX_RETRY_AFTER))
        logger.warning("%s attempt %d failed (%s); retrying in %.1fs", name, attempt + 1, err, wait)
        await sleep(wait)
        delay *= 2
    raise error(f"{name} failed")  # unreachable
