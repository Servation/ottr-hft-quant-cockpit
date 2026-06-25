"""Gateway health aggregation (Tier 4 / O2): /api/v1/health/detailed proxies the bridge's
/api/health, adds bridge reachability, and rolls up to OK | DEGRADED | DOWN. Always answers,
even when the bridge is down."""

from fastapi.testclient import TestClient

import app.routers.api as api_router
from app.main import app

client = TestClient(app)


class _FakeResp:
    def __init__(self, body):
        self.status_code = 200
        self._body = body

    def json(self):
        return self._body


def _fake_client(get_impl):
    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, timeout=3.0):
            return await get_impl(url)
    return lambda *a, **k: _Client()


def test_health_detailed_rolls_up_bridge(monkeypatch):
    # Bridge reports a STALE price feed -> overall DEGRADED; bridge itself reachable (OK).
    async def _get(url):
        return _FakeResp({"status": "DEGRADED",
                          "components": {"llm": {"status": "OK"}, "price_feed": {"status": "STALE"}}})
    monkeypatch.setattr(api_router.httpx, "AsyncClient", _fake_client(_get))

    data = client.get("/api/v1/health/detailed").json()
    assert data["components"]["bridge"]["status"] == "OK"
    assert data["components"]["price_feed"]["status"] == "STALE"
    assert data["status"] == "DEGRADED"


def test_health_detailed_bridge_down(monkeypatch):
    async def _get(url):
        raise Exception("connection refused")
    monkeypatch.setattr(api_router.httpx, "AsyncClient", _fake_client(_get))

    data = client.get("/api/v1/health/detailed").json()
    assert data["components"]["bridge"]["status"] == "DOWN"
    assert data["status"] == "DOWN"      # bridge is the only component -> DOWN
