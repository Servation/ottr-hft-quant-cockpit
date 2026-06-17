import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
import httpx
import time

from bot.price_feed import PriceFeed, _GECKO_ID_MAP, COINCAP_URL

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
    
    prices = await price_feed_instance.get_prices()
    assert prices["BTC"]["price"] == 0.0
    assert prices["ETH"]["price"] == 0.0

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
    
    # Success
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = [
        {"symbol": "BTCUSDT", "market": "Binance", "funding_rate": 0.01},
        {"symbol": "ETHUSDT", "market": "binance", "funding_rate": 0.02},
        {"symbol": "DOGEUSDT", "market": "binance", "funding_rate": 0.05}
    ]
    mock_get.return_value = mock_resp
    
    rates = await price_feed_instance.get_funding_rates()
    assert rates["BTC"] == 0.01
    assert rates["ETH"] == 0.02
    
    # Failure
    mock_get.side_effect = Exception("Fail")
    price_feed_instance._cached_funding_rates = None
    price_feed_instance._funding_rates_timestamp = 0
    rates = await price_feed_instance.get_funding_rates()
    assert rates["BTC"] == 0.0
