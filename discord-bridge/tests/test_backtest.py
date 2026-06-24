"""Unit tests for the backtest engine (bot.backtest). Pure + deterministic."""

from bot.backtest import (
    BuyAndHold, SmaCross, RsiMeanReversion, SignalStrategy, RegimeStrategy, Strategy,
    run_backtest, compare, default_strategies, synth_candles,
)


def _candles(closes):
    return [
        {"ts": 1_600_000_000 + i * 86400, "open": c, "high": c, "low": c, "close": c, "volume": 1.0}
        for i, c in enumerate(closes)
    ]


class _Flat(Strategy):
    name = "flat"

    def target_weight(self, i, closes):
        return 0.0


def test_flat_strategy_holds_cash():
    res = run_backtest(_candles([100, 110, 90, 120]), _Flat(), starting_cash=10000.0)
    assert res["final_value"] == 10000.0           # never traded
    assert res["metrics"]["total_return"] == 0.0
    assert len(res["equity_curve"]) == 4


def test_buy_and_hold_tracks_asset_minus_costs():
    closes = [100, 105, 98, 120, 130]
    res = run_backtest(_candles(closes), BuyAndHold(), starting_cash=10000.0)
    asset_return = closes[-1] / closes[0] - 1.0  # +30%

    assert res["final_value"] > 10000.0
    tr = res["metrics"]["total_return"]
    # Tracks the asset, but slightly under it due to the one-time entry fee+slippage.
    assert tr <= asset_return + 1e-9
    assert tr > asset_return - 0.02
    assert len(res["equity_curve"]) == len(closes)


def test_engine_is_deterministic():
    candles = synth_candles(200)
    r1 = compare(candles, default_strategies())
    r2 = compare(candles, default_strategies())
    assert [r["final_value"] for r in r1] == [r["final_value"] for r in r2]


def test_sma_cross_warmup_and_signal():
    s = SmaCross(2, 4)
    rising = list(range(100, 120))
    assert s.target_weight(1, rising) == 0.0   # before the slow window fills
    assert s.target_weight(10, rising) == 1.0  # fast MA above slow MA on an uptrend

    falling = list(range(120, 100, -1))
    assert SmaCross(2, 4).target_weight(10, falling) == 0.0


def test_rsi_enters_oversold_exits_overbought():
    falling = [100 - i for i in range(30)]
    assert RsiMeanReversion(14, 30, 70).target_weight(20, falling) == 1.0  # RSI low -> long

    rising = [100 + i for i in range(30)]
    assert RsiMeanReversion(14, 30, 70).target_weight(20, rising) == 0.0   # RSI high -> flat


def test_signal_strategy_runs_and_is_measurable():
    candles = synth_candles(150)
    res = run_backtest(candles, SignalStrategy(), starting_cash=10000.0)
    assert res["final_value"] > 0
    assert len(res["equity_curve"]) == len(candles)
    assert res["metrics"]["max_drawdown"] is not None


def test_compare_includes_signals_and_alpha_vs_hold():
    rows = compare(synth_candles(150), default_strategies())
    names = {r["strategy"] for r in rows}
    assert "Buy & Hold" in names
    assert "Signals (default)" in names
    assert any(n.startswith("Regime") for n in names)
    # Buy & Hold's alpha vs itself is ~0; every row has an alpha figure.
    for r in rows:
        assert r["alpha_vs_hold"] is not None
    bh = next(r for r in rows if r["strategy"] == "Buy & Hold")
    assert abs(bh["alpha_vs_hold"]) < 1e-9


def test_regime_strategy_runs():
    res = run_backtest(synth_candles(150), RegimeStrategy(), starting_cash=10000.0)
    assert res["final_value"] > 0
    assert len(res["equity_curve"]) == 150
    assert res["metrics"]["max_drawdown"] is not None
