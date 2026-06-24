"""Unit tests for the equity-curve logger (bot.equity)."""

import pytest

import bot.equity as equity


class FakePortfolio:
    """Minimal stand-in exposing the get_summary() shape equity.log_snapshot uses."""

    def __init__(self, summary):
        self._summary = summary

    def get_summary(self, prices):
        return self._summary


@pytest.fixture
def temp_equity(tmp_path, monkeypatch):
    """Redirect the equity log to a temp file (mirrors the portfolio fixture)."""
    monkeypatch.setattr(equity, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(equity, "_EQUITY_FILE", tmp_path / "equity_curve.jsonl")
    return tmp_path


_PRICES = {"BTC": {"price": 60000.0, "symbol": "BTC"}, "SOL": {"price": 150.0}}


def test_log_snapshot_writes_row(temp_equity):
    pf = FakePortfolio({
        "cash": 4000.0,
        "total_portfolio_value": 10000.0,
        "realized_pnl": 100.0,
        "unrealized_pnl": -50.0,
    })

    row = equity.log_snapshot(pf, _PRICES, source="test")

    assert row is not None
    assert row["total_value"] == 10000.0
    assert row["cash"] == 4000.0
    assert row["holdings_value"] == 6000.0  # total - cash
    assert row["btc_price"] == 60000.0
    assert row["source"] == "test"

    curve = equity.load_curve()
    assert len(curve) == 1
    assert curve[0]["total_value"] == 10000.0


def test_log_snapshot_is_append_only(temp_equity):
    pf = FakePortfolio({"cash": 1.0, "total_portfolio_value": 2.0})
    equity.log_snapshot(pf, _PRICES)
    equity.log_snapshot(pf, _PRICES)
    equity.log_snapshot(pf, _PRICES)
    assert len(equity.load_curve()) == 3


def test_log_snapshot_skips_without_prices(temp_equity):
    pf = FakePortfolio({"cash": 1.0, "total_portfolio_value": 2.0})
    assert equity.log_snapshot(pf, {}) is None
    assert equity.load_curve() == []


def test_btc_price_none_when_absent(temp_equity):
    pf = FakePortfolio({"cash": 1.0, "total_portfolio_value": 2.0})
    row = equity.log_snapshot(pf, {"SOL": {"price": 150.0}})
    assert row is not None
    assert row["btc_price"] is None


def test_load_curve_tolerates_corrupt_line(temp_equity):
    pf = FakePortfolio({"cash": 1.0, "total_portfolio_value": 2.0})
    equity.log_snapshot(pf, _PRICES)
    with open(equity._EQUITY_FILE, "a", encoding="utf-8") as f:
        f.write("{ not valid json\n")
    equity.log_snapshot(pf, _PRICES)
    # The two good rows survive; the corrupt middle line is skipped.
    assert len(equity.load_curve()) == 2
