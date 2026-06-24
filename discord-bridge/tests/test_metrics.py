"""Unit tests for the pure performance-metric functions (bot.metrics)."""

from bot import metrics
from bot.metrics import _SECONDS_PER_YEAR


def test_total_return_basic():
    assert metrics.total_return([100, 110, 121]) == 121 / 100 - 1.0


def test_total_return_insufficient_data_is_none():
    assert metrics.total_return([100]) is None
    assert metrics.total_return([]) is None


def test_max_drawdown_peak_to_trough():
    # Peak 120 -> trough 90 = 25% drawdown, even though it recovers to 150.
    assert metrics.max_drawdown([100, 120, 90, 150]) == 0.25


def test_max_drawdown_monotonic_up_is_zero_not_none():
    # A real 0.0 must be distinguishable from "insufficient data" (None).
    assert metrics.max_drawdown([100, 110, 120]) == 0.0
    assert metrics.max_drawdown([100]) is None


def test_volatility_zero_for_constant_returns():
    # Identical period returns -> ~zero dispersion (modulo float rounding).
    assert metrics.annualized_volatility([0.1, 0.1, 0.1]) < 1e-9


def test_sharpe_none_when_no_dispersion():
    # Zero stdev -> Sharpe undefined, not infinity.
    assert metrics.sharpe_ratio([0.05, 0.05, 0.05]) is None


def test_sharpe_positive_for_steady_gains():
    s = metrics.sharpe_ratio([0.02, 0.01, 0.03, 0.015])
    assert s is not None and s > 0


def test_sortino_none_without_downside():
    # All non-negative excess returns -> no downside deviation.
    assert metrics.sortino_ratio([0.01, 0.02, 0.0]) is None


def test_cagr_doubling_in_one_year():
    points = [(0.0, 100.0), (_SECONDS_PER_YEAR, 200.0)]
    c = metrics.cagr(points)
    assert c is not None and abs(c - 1.0) < 1e-9


def test_cagr_none_for_tiny_span_no_overflow():
    # A 2% move over 2 seconds would annualize to an overflowing/absurd number;
    # it must come back None, not raise or return inf.
    points = [(1_000_000.0, 100.0), (1_000_002.0, 102.0)]
    assert metrics.cagr(points) is None


def test_summarize_short_span_is_safe():
    # Several points within an hour with a big swing: total_return / max_drawdown
    # are still computable, but CAGR is suppressed and nothing explodes.
    base = 1_000_000.0
    points = [(base, 100.0), (base + 600, 80.0), (base + 1800, 130.0)]
    out = metrics.summarize(points)
    assert out["cagr"] is None
    assert out["total_return"] == metrics.total_return([100.0, 80.0, 130.0])
    assert out["max_drawdown"] == 0.2  # 100 -> 80


def test_calmar_ratio():
    assert metrics.calmar_ratio(0.5, 0.25) == 2.0
    assert metrics.calmar_ratio(0.5, 0.0) is None  # no drawdown -> undefined
    assert metrics.calmar_ratio(None, 0.25) is None


def test_summarize_insufficient_data():
    out = metrics.summarize([(0.0, 100.0)])
    assert out["insufficient_data"] is True
    assert out["total_return"] is None
    assert out["sharpe"] is None


def test_summarize_full_series_with_benchmark():
    day = 86400.0
    # 5 daily points; strategy ends +5%, BTC ends +10% over the same window.
    points = [(i * day, v) for i, v in enumerate([100, 101, 102, 99, 105])]
    btc = [20000, 20200, 20800, 20400, 22000]  # +10%

    out = metrics.summarize(points, btc_prices=btc)

    assert out["insufficient_data"] is False
    assert out["num_points"] == 5
    assert abs(out["total_return"] - 0.05) < 1e-9
    assert abs(out["period_days"] - 4.0) < 1e-9
    # Daily returns exist, so the ratio metrics are computable.
    assert out["sharpe"] is not None
    assert out["max_drawdown"] is not None
    # Benchmark = BTC's return; alpha = strategy - benchmark (we trailed BTC).
    assert abs(out["benchmark_return"] - 0.10) < 1e-9
    assert abs(out["alpha"] - (0.05 - 0.10)) < 1e-9


def test_summarize_benchmark_skipped_when_btc_missing():
    points = [(0.0, 100.0), (86400.0, 110.0)]
    out = metrics.summarize(points, btc_prices=[None, None])
    assert out["benchmark_return"] is None
    assert out["alpha"] is None
