import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
import httpx
import time

from bot.price_feed import PriceFeed, _GECKO_ID_MAP, _KRAKEN_PAIR_MAP, COINCAP_URL

@pytest.fixture
def price_feed_instance():
    # Provide a fresh instance for each test so caching doesn't interfere
    feed = PriceFeed()
    return feed

@pytest.mark.asyncio
async def test_get_prices_coingecko_fallback(price_feed_instance, mocker):
    mock_get = mocker.patch("httpx.AsyncClient.get", new_callable=AsyncMock)
    
    # First call to CoinGecko fails
    def mock_get_side_effect(url, *args, **kwargs):
        if "coingecko" in url and "simple/price" in url:
            raise httpx.RequestError("CoinGecko Down")
        elif url == COINCAP_URL:
            # CoinCap succeeds
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = {
                "data": [
                    {"id": "bitcoin", "priceUsd": "100000.0", "changePercent24Hr": "5.0"},
                    {"id": "unknown_asset", "priceUsd": "1.0", "changePercent24Hr": "0.0"}
                ]
            }
            return mock_resp
        return MagicMock()

    mock_get.side_effect = mock_get_side_effect
    
    prices = await price_feed_instance.get_prices()
    
    assert "BTC" in prices
    assert prices["BTC"]["price"] == 100000.0
    assert prices["BTC"]["change_24h"] == 5.0

@pytest.mark.asyncio
async def test_get_prices_both_fail_fallback_to_cache(price_feed_instance, mocker):
    # Set up some cache
    price_feed_instance._cached_prices = {"BTC": {"price": 50000.0, "change_24h": 0.0}}
    # make it stale
    price_feed_instance._cache_timestamp = time.time() - 1000
    
    mock_get = mocker.patch("httpx.AsyncClient.get", new_callable=AsyncMock)
    mock_get.side_effect = httpx.RequestError("Both Down")
    
    prices = await price_feed_instance.get_prices()
    assert prices["BTC"]["price"] == 50000.0

@pytest.mark.asyncio
async def test_get_prices_both_fail_no_cache(price_feed_instance, mocker):
    mock_get = mocker.patch("httpx.AsyncClient.get", new_callable=AsyncMock)
    mock_get.side_effect = httpx.RequestError("Both Down")
    
    # Both APIs down with no cache: get_prices must RAISE rather than return fake
    # $0.00 prices (which could drive catastrophic trades).
    with pytest.raises(Exception, match="Critical Market Data Unavailable"):
        await price_feed_instance.get_prices()
    assert price_feed_instance._cached_prices is None


@pytest.mark.asyncio
async def test_get_prices_both_fail_uses_stale_cache(price_feed_instance, mocker):
    # If both APIs fail but a cached snapshot exists, fall back to the stale cache.
    price_feed_instance._cached_prices = {"BTC": {"price": 123.0}, "ETH": {"price": 9.0}}
    price_feed_instance._cache_timestamp = 0  # force "expired" so it tries to refetch
    mock_get = mocker.patch("httpx.AsyncClient.get", new_callable=AsyncMock)
    mock_get.side_effect = httpx.RequestError("Both Down")

    prices = await price_feed_instance.get_prices()
    assert prices["BTC"]["price"] == 123.0

def test_sanitize_prices_substitutes_outlier_and_zero(price_feed_instance):
    """The outlier guard replaces an outlier print or a dropped (zero) asset with the
    last known-good value, so a glitch tick can't reach valuation/risk math; a normal
    move passes through. Regression for the $100k BTC tick that drove a bogus trim."""
    feed = price_feed_instance
    feed._max_deviation_pct = 50.0
    feed._cached_prices = {
        "BTC": {"price": 64000.0, "change_24h": 1.0},
        "ETH": {"price": 1700.0, "change_24h": 0.5},
        "SOL": {"price": 70.0, "change_24h": -2.0},
    }
    fresh = {
        "BTC": {"price": 100000.0, "change_24h": 5.0},  # +56% outlier -> substituted
        "ETH": {"price": 1734.0, "change_24h": 0.6},    # +2% normal -> kept
        "SOL": {"price": 0.0, "change_24h": 0.0},       # dropped to $0 -> substituted
    }
    clean = feed._sanitize_prices(fresh)
    assert clean["BTC"]["price"] == 64000.0   # outlier rejected, last-good used
    assert clean["ETH"]["price"] == 1734.0    # normal move kept
    assert clean["SOL"]["price"] == 70.0      # zero rejected, last-good used


def test_sanitize_prices_passthrough_on_cold_start(price_feed_instance):
    """With no reference cache yet, the guard can't validate, so values pass through
    unchanged — a cold start must never blank the book."""
    feed = price_feed_instance
    feed._cached_prices = None
    fresh = {"BTC": {"price": 100000.0, "change_24h": 0.0}}
    assert feed._sanitize_prices(fresh)["BTC"]["price"] == 100000.0


@pytest.mark.asyncio
async def test_get_prices_applies_guard_against_outlier(price_feed_instance, mocker):
    """End to end: a refetch returning a BTC outlier is sanitized against the prior
    cached tick before being returned and re-cached."""
    feed = price_feed_instance
    feed._max_deviation_pct = 50.0
    feed._cached_prices = {"BTC": {"price": 64000.0, "change_24h": 0.0}}  # prior good tick
    feed._cache_timestamp = 0  # expired -> force a refetch

    def gecko_side_effect(url, *args, **kwargs):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"bitcoin": {"usd": 100000.0, "usd_24h_change": 0.0}}
        return resp
    mocker.patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=gecko_side_effect)

    prices = await feed.get_prices()
    assert prices["BTC"]["price"] == 64000.0  # guard kept last-good, not the $100k glitch


@pytest.mark.asyncio
async def test_get_price_history(price_feed_instance):
    price_feed_instance._history = [(123456789.0, {"BTC": {"price": 1.0}})]
    hist = price_feed_instance.get_price_history()
    assert len(hist) == 1
    assert hist[0]["timestamp"] == 123456789.0
    assert hist[0]["prices"]["BTC"]["price"] == 1.0

@pytest.mark.asyncio
async def test_fetch_historical_data_success_and_exception(price_feed_instance, mocker):
    mock_get = mocker.patch("httpx.AsyncClient.get", new_callable=AsyncMock)
    
    def mock_get_side_effect(url, *args, **kwargs):
        if "bitcoin" in url:
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = {
                "prices": [
                    [1000, 50000.0],
                    [2000, 55000.0],
                    [3000, 52000.0]
                ]
            }
            return mock_resp
        else:
            raise Exception("Failed ETH")
            
    mock_get.side_effect = mock_get_side_effect
    
    # speed up sleep
    mocker.patch("asyncio.sleep", new_callable=AsyncMock)
    
    vol = await price_feed_instance.get_volatility()
    assert "BTC" in vol
    assert vol["BTC"] > 0
    assert "ETH" in vol
    assert vol["ETH"] == 0.0

def test_correlation(price_feed_instance):
    # Empty prices
    assert price_feed_instance.get_correlation() == 0.0
    
    # Only 1 price
    price_feed_instance._cached_historical_prices = {"BTC": [100.0], "ETH": [200.0]}
    assert price_feed_instance.get_correlation() == 0.0
    
    # Same trend
    price_feed_instance._cached_historical_prices = {"BTC": [100.0, 110.0, 120.0], "ETH": [200.0, 220.0, 240.0]}
    corr = price_feed_instance.get_correlation()
    assert corr > 0.99
    
    # Zero variance
    price_feed_instance._cached_historical_prices = {"BTC": [100.0, 100.0, 100.0], "ETH": [200.0, 220.0, 240.0]}
    assert price_feed_instance.get_correlation() == 0.0

@pytest.mark.asyncio
async def test_fetch_defi_yields(price_feed_instance, mocker):
    mock_get = mocker.patch("httpx.AsyncClient.get", new_callable=AsyncMock)
    
    # Success
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "data": [
            {"symbol": "USDC", "tvlUsd": 20000000, "apy": 5.0},
            {"symbol": "USDT", "tvlUsd": 20000000, "apy": 10.0},
            {"symbol": "BTC", "tvlUsd": 20000000, "apy": 100.0}, # ignored
            {"symbol": "DAI", "tvlUsd": 1000, "apy": 20.0} # ignored tvl
        ]
    }
    mock_get.return_value = mock_resp
    
    yield_val = await price_feed_instance.get_yield()
    assert yield_val == 7.5
    
    # Failure
    mock_get.side_effect = Exception("Fail")
    price_feed_instance._cached_yield = None
    price_feed_instance._yield_timestamp = 0
    yield_val = await price_feed_instance.get_yield()
    assert yield_val == 0.0

@pytest.mark.asyncio
async def test_fetch_derivatives(price_feed_instance, mocker):
    mock_get = mocker.patch("httpx.AsyncClient.get", new_callable=AsyncMock)
    
    # Success: _fetch_derivatives queries OKX once per symbol and parses
    # {"code": "0", "data": [{"fundingRate": ...}]}.
    def okx_side_effect(url, *args, **kwargs):
        rate = "0.01" if "BTC-USDT-SWAP" in url else "0.02" if "ETH-USDT-SWAP" in url else "0.0"
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"code": "0", "data": [{"fundingRate": rate}]}
        return resp
    mock_get.side_effect = okx_side_effect

    rates = await price_feed_instance.get_funding_rates()
    assert rates["BTC"] == 0.01
    assert rates["ETH"] == 0.02

    # Failure: requests raise -> defaults to 0.0 (no stale cache available).
    mock_get.side_effect = Exception("Fail")
    price_feed_instance._cached_funding_rates = {}
    price_feed_instance._funding_timestamp = 0
    rates = await price_feed_instance.get_funding_rates()
    assert rates["BTC"] == 0.0


@pytest.mark.asyncio
async def test_technical_indicators_cover_all_mapped_assets(price_feed_instance, mocker):
    """Indicators are computed for every Kraken-mapped asset (not just BTC/ETH);
    SOL — held and voted but previously skipped — now gets EMA/RSI/MACD."""
    mocker.patch("asyncio.sleep", new_callable=AsyncMock)

    # 60 daily candles with mild variation so pandas-ta can compute EMA50/RSI/MACD.
    rows = []
    for i in range(60):
        c = 100.0 + i * 0.5 + (i % 5)
        rows.append([1000 + i, str(c), str(c + 1), str(c - 1), str(c), str(c), "10", 5])

    def kraken_side_effect(url, *args, **kwargs):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if "kraken.com" in url and "OHLC" in url:
            resp.json.return_value = {"error": [], "result": {"PAIR": rows, "last": 0}}
        else:
            resp.json.return_value = {}
        return resp

    mocker.patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=kraken_side_effect)

    indicators = await price_feed_instance.get_technical_indicators()

    # SOL is the asset that used to be structurally excluded.
    assert "SOL" in indicators
    assert set(indicators["SOL"]) >= {"EMA_20", "EMA_50", "RSI_14", "MACD"}
    # Regime (efficiency ratio) is attached for the live agent context.
    assert "regime" in indicators["SOL"] and "ER" in indicators["SOL"]
    assert "BTC" in indicators
    # BNB has no Kraken USD pair, so it's never fetched and never faked.
    assert "BNB" not in _KRAKEN_PAIR_MAP
    assert "BNB" not in indicators
