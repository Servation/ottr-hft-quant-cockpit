"""
Shared HTTP helper tests: retries, Retry-After, non-retryable failures, secrets kept out of
error text, and the token-bucket rate limiter. No network: httpx.MockTransport.
"""

import httpx
import pytest

from ottr.market import http


class Sleeps:
    def __init__(self):
        self.calls = []

    async def __call__(self, seconds):
        self.calls.append(seconds)


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_retries_transient_failures_with_backoff_then_succeeds():
    replies = iter([httpx.Response(503), httpx.Response(429), httpx.Response(200, json={"ok": 1})])
    sleeps = Sleeps()
    async with client(lambda r: next(replies)) as c:
        assert await http.get_json(c, "https://x/y", backoff=1.0, sleep=sleeps) == {"ok": 1}
    assert sleeps.calls == [1.0, 2.0]


async def test_retry_after_is_honored_but_capped():
    replies = iter([httpx.Response(429, headers={"Retry-After": "7"}),
                    httpx.Response(429, headers={"Retry-After": "9999"}),
                    httpx.Response(200, json={})])
    sleeps = Sleeps()
    async with client(lambda r: next(replies)) as c:
        await http.get_json(c, "https://x", backoff=0.5, sleep=sleeps)
    assert sleeps.calls == [7.0, http.MAX_RETRY_AFTER]


async def test_client_errors_fail_at_once():
    calls = []

    def handler(r):
        calls.append(r)
        return httpx.Response(404)
    async with client(handler) as c:
        with pytest.raises(http.HttpError, match="HTTP 404"):
            await http.get_json(c, "https://x", sleep=Sleeps())
    assert len(calls) == 1


async def test_out_of_retries_raises_the_callers_error_type():
    class FeedError(http.HttpError):
        pass
    async with client(lambda r: httpx.Response(502)) as c:
        with pytest.raises(FeedError, match="feed failed: HTTP 502"):
            await http.get_json(c, "https://x", retries=2, name="feed", error=FeedError, sleep=Sleeps())


async def test_non_json_body_is_treated_as_an_outage():
    replies = iter([httpx.Response(200, text="<html>bad gateway</html>"), httpx.Response(200, json=[1])])
    async with client(lambda r: next(replies)) as c:
        assert await http.get_json(c, "https://x", sleep=Sleeps()) == [1]


async def test_transport_errors_never_leak_the_url():
    def boom(r):
        raise httpx.ConnectError("failed to connect to https://x/q?apikey=SECRET123")
    async with client(boom) as c:
        with pytest.raises(http.HttpError) as e:
            await http.get_json(c, "https://x/q", params={"apikey": "SECRET123"}, retries=1, sleep=Sleeps())
    assert "SECRET123" not in str(e.value) and "ConnectError" in str(e.value)


async def test_rate_limiter_spaces_requests_after_the_burst():
    t = {"now": 0.0}

    async def sleep(s):
        t["now"] += s
    lim = http.RateLimiter(per_minute=60, capacity=2, now=lambda: t["now"], sleep=sleep)
    for _ in range(5):
        await lim.acquire()
    # 2 at once from the burst, then one per second for the other 3.
    assert t["now"] == pytest.approx(3.0)


async def test_rate_limiter_gates_get_json():
    t = {"now": 0.0}

    async def sleep(s):
        t["now"] += s
    lim = http.RateLimiter(per_minute=120, capacity=1, now=lambda: t["now"], sleep=sleep)
    async with client(lambda r: httpx.Response(200, json={})) as c:
        for _ in range(3):
            await http.get_json(c, "https://x", limiter=lim)
    assert t["now"] == pytest.approx(1.0)  # 2 waits of 0.5s at 120/min
