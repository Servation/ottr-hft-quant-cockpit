"""Gateway read-route tests: market-data success + error sanitization."""
import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_open():
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] in ("OK", "ok", "healthy")


def test_market_data_success(mocker):
    mocker.patch("app.routers.api.market_proxy.get_ticker",
                 new=AsyncMock(return_value=12345.0))
    r = client.get("/api/v1/market-data?symbols=BTC,ETH")
    assert r.status_code == 200
    data = r.json()
    assert data["BTC"]["price"] == 12345.0
    assert data["ETH"]["price"] == 12345.0


def test_market_data_error_is_sanitized(mocker):
    mocker.patch("app.routers.api.market_proxy.get_ticker",
                 new=AsyncMock(side_effect=Exception("secret path /etc/keys leaked")))
    r = client.get("/api/v1/market-data?symbols=BTC")
    assert r.status_code == 500
    # The raw exception text must NOT be returned to the client.
    assert "secret path" not in r.text
    assert r.json()["detail"] == "Failed to fetch market data"
