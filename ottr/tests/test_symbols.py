"""
Ticker normalization tests: share classes in every spelling map to one canonical symbol.
"""

import pytest

from ottr.market import symbols


@pytest.mark.parametrize("raw", ["BRK.B", "brk.b", "BRK-B", "BRK/B", "BRK B", " brk_b "])
def test_share_class_spellings_collapse_to_one_symbol(raw):
    assert symbols.normalize(raw) == "BRK.B"


@pytest.mark.parametrize("raw", ["SPY", "qqq", "A", "GOOGL"])
def test_plain_tickers(raw):
    assert symbols.normalize(raw) == raw.upper()


@pytest.mark.parametrize("raw", ["", "TOOLONG", "BRK.", "BRK..B", "BRK.BCD", "SPY1", "^VIX", "BTC/USD/X", None, 7])
def test_non_us_tickers_are_rejected(raw):
    assert not symbols.is_valid(raw)
    with pytest.raises(ValueError):
        symbols.normalize(raw)


def test_provider_spellings():
    assert symbols.to_alpaca("brk-b") == "BRK.B"
    assert symbols.to_sec("BRK.B") == "BRK-B"
    assert symbols.to_sec("spy") == "SPY"
