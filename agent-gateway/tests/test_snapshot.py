"""Gateway portfolio snapshot: reads the configured authoritative path and
never leaks secrets (Phase 1/4 regression guard)."""
import json
import pytest
from fastapi.testclient import TestClient

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
