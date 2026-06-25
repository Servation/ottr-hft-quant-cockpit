"""Unit tests for regime/volatility position sizing (bot.sizing)."""

from bot import sizing


def test_vol_targeting_smaller_for_higher_vol():
    hi_vol = sizing.max_buy_notional(10000, 1.6, "TRENDING")
    lo_vol = sizing.max_buy_notional(10000, 0.4, "TRENDING")
    assert lo_vol > hi_vol
    # 2% risk budget * 10000 / 0.8 vol = $250
    assert abs(sizing.max_buy_notional(10000, 0.8, "TRENDING") - 250.0) < 1.0


def test_choppy_regime_shrinks_size():
    trending = sizing.max_buy_notional(10000, 0.8, "TRENDING")
    choppy = sizing.max_buy_notional(10000, 0.8, "CHOPPY")
    assert abs(choppy - trending * 0.25) < 1e-6


def test_conviction_scales_size():
    full = sizing.max_buy_notional(10000, 0.8, "TRENDING")
    half = sizing.max_buy_notional(10000, 0.8, "TRENDING", conviction=0.5)
    assert abs(half - full * 0.5) < 1e-6


def test_size_never_exceeds_per_trade_cap():
    # Even a tiny volatility can't push a single BUY past max_position_pct (50%).
    for vol in (0.005, 0.05, 0.2, 1.0):
        assert sizing.max_buy_notional(10000, vol, "TRENDING") <= 5000.0 + 1e-6


def test_zero_total_value_is_zero():
    assert sizing.max_buy_notional(0, 0.8, "TRENDING") == 0.0


def test_missing_inputs_are_handled():
    # None vol -> uses the floor; None regime -> no shrink. Must not raise.
    assert sizing.max_buy_notional(10000, None, None) > 0
