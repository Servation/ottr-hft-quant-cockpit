"""
Market layer tests: indicator math, Kraken parsing/retries, fallback, snapshot flags.
Network is replaced with httpx.MockTransport; nothing leaves the machine.
"""

import math

import httpx
import pytest

from ottr.market import indicators as ind
from ottr.market.kraken import PAIRS, Kraken, KrakenError, symbol_for_key
from ottr.market.service import MarketData, build_view
from ottr.market.types import MarketDataError, Quote
from tests.fakes import trending_closes


# ---- indicators ----------------------------------------------------------------------

def test_ema_matches_textbook_recursion():
    vals = [1, 2, 3, 4, 5, 6]
    k = 2 / 4
    e = 2.0  # SMA of first 3
    for v in (4, 5, 6):
        e = v * k + e * (1 - k)
    assert ind.ema(vals, 3) == pytest.approx(e)
    assert ind.ema([1, 2], 3) is None


def test_rsi_extremes_and_flat():
    assert ind.rsi(list(range(1, 30))) == 100.0
    assert ind.rsi(list(range(30, 1, -1))) == pytest.approx(0.0)
    assert ind.rsi([5.0] * 30) == 50.0
    assert ind.rsi([1, 2, 3]) is None


def test_macd_sign_follows_trend():
    up = [100 * 1.01 ** i for i in range(60)]
    m, s, h = ind.macd(up)
    assert m > 0
    assert ind.macd(up[:30]) is None


def test_efficiency_ratio_and_regime():
    assert ind.efficiency_ratio(list(range(30)), 20) == pytest.approx(1.0)
    chop = [100 + (1 if i % 2 else -1) for i in range(30)]
    assert ind.efficiency_ratio(chop, 20) == pytest.approx(0.0)
    assert ind.regime(0.31) == "TRENDING" and ind.regime(0.1) == "CHOPPY"
    assert ind.regime(None) == "UNKNOWN"


def test_vol_and_correlation_are_none_when_unknown():
    assert ind.annualized_vol([100.0, 101.0]) is None
    assert ind.annualized_vol([100.0] * 40) is None
    rets = [0.01, -0.02, 0.015]
    closes = [100.0]
    for r in rets:
        closes.append(closes[-1] * math.exp(r))
    m = sum(rets) / 3
    expect = math.sqrt(sum((r - m) ** 2 for r in rets) / 2) * math.sqrt(365)
    assert ind.annualized_vol(closes) == pytest.approx(expect)


def test_correlation_uses_returns_not_levels():
    a = [100, 104, 106, 110, 112, 116]
    b = [100, 101, 105, 106, 110, 111]  # also trending, but zig-zags against a
    assert ind.return_correlation(a, b) < 0


# ---- kraken --------------------------------------------------------------------------

def kraken_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_legacy_kraken_keys_map_back():
    assert symbol_for_key("XXBTZUSD") == "BTC" and symbol_for_key("XETHZUSD") == "ETH"
    assert symbol_for_key("XXRPZUSD") == "XRP"
    assert all(symbol_for_key(p) == s for s, p in PAIRS.items())


async def test_quotes_parse_bid_ask_and_24h_high():
    def handler(req):
        return httpx.Response(200, json={"error": [], "result": {
            "XXBTZUSD": {"a": ["80010", "1", "1"], "b": ["79990", "1", "1"], "c": ["80000", "0.1"],
                         "h": ["81000", "82000"], "l": ["79000", "78000"], "o": "79500"}}})
    async with kraken_client(handler) as c:
        q = (await Kraken(c).quotes({"BTC": "XBTUSD"}))["BTC"]
    assert (q.bid, q.ask, q.high_24h) == (79990.0, 80010.0, 82000.0)


async def test_daily_closes_drop_the_unfinished_candle():
    rows = [[1000, "1", "1", "1", "10", "1", "1", 1], [87400, "1", "1", "1", "11", "1", "1", 1],
            [173800, "1", "1", "1", "12", "1", "1", 1]]
    async with kraken_client(lambda r: httpx.Response(200, json={"error": [], "result": {"X": rows, "last": 1}})) as c:
        closes = await Kraken(c).daily_closes("XBTUSD")
    assert closes == [(1000, 10.0), (87400, 11.0)]


async def test_rate_limit_is_retried_but_bad_request_is_not():
    calls = {"n": 0}

    def flaky(req):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(200, json={"error": ["EAPI:Rate limit exceeded"]})
        return httpx.Response(200, json={"error": [], "result": {}})
    async with kraken_client(flaky) as c:
        assert await Kraken(c, backoff=0)._get("Ticker", {}) == {}
    assert calls["n"] == 3

    calls["n"] = 0

    def bad(req):
        calls["n"] += 1
        return httpx.Response(200, json={"error": ["EQuery:Unknown asset pair"]})
    async with kraken_client(bad) as c:
        with pytest.raises(KrakenError):
            await Kraken(c, backoff=0)._get("Ticker", {})
    assert calls["n"] == 1


# ---- service -------------------------------------------------------------------------

class StubKraken:
    def __init__(self, quotes=None, fail=False):
        self._q, self.fail = quotes or {}, fail

    async def quotes(self, pairs):
        if self.fail:
            raise KrakenError("down")
        return {a: q for a, q in self._q.items() if a in pairs}

    async def daily_closes(self, pair):
        raise KrakenError("no candles in this stub")


def q(asset, px, source="kraken"):
    import time
    return Quote(asset, px * 0.999, px * 1.001, px, time.time(), source)


async def test_coinbase_fills_in_assets_kraken_missed(cfg):
    got = {}

    async def fallback(assets):
        got["asked"] = list(assets)
        return {a: q(a, 1.0, "coinbase") for a in assets}
    md = MarketData(cfg, kraken=StubKraken({"BTC": q("BTC", 80000)}), fallback=fallback)
    quotes = await md.quotes()
    assert quotes["BTC"].source == "kraken" and quotes["ETH"].source == "coinbase"
    assert "BTC" not in got["asked"]


async def test_total_outage_without_cache_fails_loud(cfg):
    async def nothing(assets):
        return {}
    md = MarketData(cfg, kraken=StubKraken(fail=True), fallback=nothing)
    with pytest.raises(MarketDataError):
        await md.quotes()


async def test_outage_keeps_last_good_quotes(cfg):
    async def nothing(assets):
        return {}
    k = StubKraken({"BTC": q("BTC", 80000)})
    md = MarketData(cfg, kraken=k, fallback=nothing)
    await md.quotes()
    k.fail = True
    assert (await md.quotes(force=True))["BTC"].last == 80000  # stale but known; age shows it


def test_view_flags_short_history_gaps_and_fallback_quotes(cfg):
    rows = trending_closes(100, n=30)
    rows = rows[:10] + rows[11:]  # one missing day
    v = build_view("ETH", q("ETH", 150, "coinbase"), rows, None, cfg)
    assert {"insufficient_history", "candle_gap", "quote_from_coinbase"} <= set(v.flags)
    assert v.ema50 is None and v.macd is None  # no warm-up, no number


def test_packet_is_time_anchored_and_marks_unknowns(cfg):
    from ottr.market.service import MarketSnapshot
    v = build_view("ETH", None, [], None, cfg)
    text = MarketSnapshot(as_of=1_800_000_000, assets={"ETH": v}).packet()
    assert text.startswith("AS OF: 2027-01-15")
    assert "n/a" in text and "no_live_quote" in text


async def test_market_data_uses_the_configured_pair_for_an_extra_asset(cfg):
    import dataclasses as dc
    from ottr.market.service import MarketData
    seen = {}

    class Recording(StubKraken):
        async def quotes(self, pairs):
            seen.update(pairs)
            return {}

    c = dc.replace(cfg, universe=["BTC", "NEAR"], pairs={"NEAR": "NEARUSD"})

    async def no_fallback(missing):
        return {a: q(a, 5.0, "coinbase") for a in missing}

    await MarketData(c, kraken=Recording(), fallback=no_fallback).quotes(force=True)
    assert seen == {"BTC": "XBTUSD", "NEAR": "NEARUSD"}
