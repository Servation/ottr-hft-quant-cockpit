"""Unit tests for the deterministic signal layer (bot.signals)."""

from bot import signals
from bot.signals import signal_from_indicators, Signal


def _ind(ema20, ema50, rsi, macd, macd_sig):
    return {"EMA_20": ema20, "EMA_50": ema50, "RSI_14": rsi, "MACD": macd, "MACD_signal": macd_sig}


def test_all_bullish():
    # uptrend EMA, oversold RSI (mean-reversion +), bullish MACD -> max bullish
    s = signal_from_indicators(_ind(110, 100, 25, 1.0, 0.5))
    assert s.direction == "BULLISH"
    assert s.score == 1.0


def test_all_bearish():
    s = signal_from_indicators(_ind(100, 110, 80, 0.5, 1.0))
    assert s.direction == "BEARISH"
    assert s.score == -1.0


def test_conflicting_is_neutral():
    # EMA up (+1), RSI mid (0), MACD down (-1) -> net 0
    s = signal_from_indicators(_ind(110, 100, 50, 0.5, 1.0))
    assert s.direction == "NEUTRAL"
    assert s.score == 0.0


def test_funding_extreme_is_contrarian_bearish():
    # Only RSI (mid, 0) + crowded-long funding -> net bearish.
    s = signal_from_indicators({"RSI_14": 50}, funding=0.001)
    assert s.direction == "BEARISH"
    assert any("funding" in r for r in s.reasons)


def test_fear_and_greed_extreme_fear_is_contrarian_bullish():
    s = signal_from_indicators({}, fng=10)
    assert s.direction == "BULLISH"
    assert any("fear" in r.lower() for r in s.reasons)


def test_no_inputs_is_neutral_zero():
    s = signal_from_indicators({})
    assert s.direction == "NEUTRAL"
    assert s.score == 0.0
    assert s.reasons == []


def test_signals_for_assets_maps_each():
    inds = {
        "BTC": _ind(110, 100, 25, 1.0, 0.5),   # bullish
        "ETH": _ind(100, 110, 80, 0.5, 1.0),   # bearish
    }
    out = signals.signals_for_assets(inds)
    assert out["BTC"].direction == "BULLISH"
    assert out["ETH"].direction == "BEARISH"


def test_consensus_maps_directions():
    sig = {"BTC": Signal("BULLISH", 1.0, 1.0), "ETH": Signal("BEARISH", -1.0, 1.0),
           "SOL": Signal("NEUTRAL", 0.0, 0.0)}
    c = signals.consensus_from_signals(sig)
    assert c == {"BTC": "BUY", "ETH": "SELL", "SOL": "HOLD"}


def test_format_signals_handles_empty():
    assert "No deterministic signals" in signals.format_signals({})
    out = signals.format_signals({"BTC": Signal("BULLISH", 1.0, 1.0, ["EMA20>EMA50 (uptrend)"])})
    assert "BTC" in out and "BULLISH" in out


def test_indicator_series_is_causal_and_filled():
    closes = [100.0 + i * 0.5 + (i % 5) for i in range(60)]
    series = signals.indicator_series_from_closes(closes)
    assert len(series) == 60
    assert series[0] is None              # not enough history early
    assert series[-1] is not None         # enough history at the end
    assert set(series[-1]) >= {"EMA_20", "EMA_50", "RSI_14", "MACD", "MACD_signal"}


def test_indicator_series_short_input():
    assert all(x is None for x in signals.indicator_series_from_closes([1.0, 2.0, 3.0]))
