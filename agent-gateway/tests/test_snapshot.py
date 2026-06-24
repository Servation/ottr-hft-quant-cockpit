"""Gateway portfolio snapshot: reads the configured authoritative path and
never leaks secrets (Phase 1/4 regression guard); surfaces real performance
metrics from the bridge (M1)."""
import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import app.routers.api as api_router
from app.main import app

client = TestClient(app)


def test_snapshot_reads_configured_path(monkeypatch, tmp_path):
    f = tmp_path / "pf.json"
    f.write_text(json.dumps({
        "cash": 4242.0, "holdings": {}, "total_pnl": 0.0,
        "trade_history": [], "min_trade_usd": 100.0,
    }))
    monkeypatch.setenv("PORTFOLIO_STATE_PATH", str(f))

    r = client.get("/api/v1/portfolio/snapshot")
    assert r.status_code == 200
    data = r.json()
    assert data["usd_cash"] == 4242.0
    # Secrets must never appear in the snapshot.
    assert "llm_fallback_api_key" not in data


def test_snapshot_missing_file_defaults_gracefully(monkeypatch, tmp_path):
    monkeypatch.setenv("PORTFOLIO_STATE_PATH", str(tmp_path / "nope.json"))
    r = client.get("/api/v1/portfolio/snapshot")
    assert r.status_code == 200
    assert r.json()["usd_cash"] == 0


def test_snapshot_surfaces_real_metrics_when_bridge_up(monkeypatch, tmp_path):
    f = tmp_path / "pf.json"
    f.write_text(json.dumps({"cash": 1000.0, "holdings": {}, "total_pnl": 0.0}))
    monkeypatch.setenv("PORTFOLIO_STATE_PATH", str(f))
    monkeypatch.setattr(api_router, "_fetch_performance", AsyncMock(return_value={
        "metrics": {"max_drawdown": 0.25, "sharpe": 1.5, "total_return": 0.1,
                    "benchmark_return": 0.2, "alpha": -0.1},
        "num_points": 12,
    }))

    data = client.get("/api/v1/portfolio/snapshot").json()
    # The hardcoded drawdown: 0.0 is gone — real max drawdown flows through.
    assert data["drawdown"] == 0.25
    assert data["performance"]["sharpe"] == 1.5
    assert data["performance"]["alpha"] == -0.1
    assert data["performance"]["num_points"] == 12


def test_snapshot_degrades_when_bridge_down(monkeypatch, tmp_path):
    f = tmp_path / "pf.json"
    f.write_text(json.dumps({"cash": 1000.0, "holdings": {}, "total_pnl": 0.0}))
    monkeypatch.setenv("PORTFOLIO_STATE_PATH", str(f))
    monkeypatch.setattr(api_router, "_fetch_performance", AsyncMock(return_value=None))

    data = client.get("/api/v1/portfolio/snapshot").json()
    # No curve yet -> drawdown falls back to 0.0 and metrics are null (not faked).
    assert data["drawdown"] == 0.0
    assert data["performance"]["max_drawdown"] is None
    assert data["performance"]["num_points"] == 0
