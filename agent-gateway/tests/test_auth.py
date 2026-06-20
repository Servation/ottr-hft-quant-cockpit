"""Phase 1 auth tests for the agent-gateway state-changing routes.

Run from the agent-gateway dir: `python -m pytest tests/`.
(Not yet wired into CI — Phase 5 adds the gateway CI job.)
"""
import os
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

MUTATING = [
    ("post", "/api/v1/trading/start", None),
    ("post", "/api/v1/trading/stop", None),
    ("post", "/api/v1/llm/configure", {"base_url": "x", "model_id": "y"}),
    ("post", "/api/v1/portfolio/reset-balance", {"balance": 1000}),
]


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("OTTR_API_KEY", "secret")


@pytest.mark.parametrize("method,path,body", MUTATING)
def test_mutation_requires_key(method, path, body):
    r = getattr(client, method)(path, json=body)
    assert r.status_code == 401


@pytest.mark.parametrize("method,path,body", MUTATING)
def test_mutation_rejects_wrong_key(method, path, body):
    r = getattr(client, method)(path, json=body, headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


def test_mutation_accepts_valid_key():
    r = client.post("/api/v1/trading/start", headers={"X-API-Key": "secret"})
    assert r.status_code == 200


def test_reads_stay_open():
    # Read-only endpoints must not require the key (EventSource can't send headers).
    assert client.get("/api/v1/health").status_code == 200


def test_fail_closed_when_unconfigured(monkeypatch):
    monkeypatch.delenv("OTTR_API_KEY", raising=False)
    r = client.post("/api/v1/trading/start", headers={"X-API-Key": "secret"})
    assert r.status_code == 503


def test_rate_limit_returns_429(monkeypatch, mocker):
    monkeypatch.setenv("OTTR_API_KEY", "secret")
    import app.routers.api as api
    mocker.patch.object(api._gw_rate, "max_calls", 1)
    api._gw_rate._hits.clear()
    h = {"X-API-Key": "secret"}
    r1 = client.post("/api/v1/trading/start", headers=h)
    r2 = client.post("/api/v1/trading/start", headers=h)
    assert r1.status_code == 200
    assert r2.status_code == 429
