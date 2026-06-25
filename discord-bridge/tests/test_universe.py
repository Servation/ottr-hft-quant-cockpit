"""
Tests for the tradeable-universe single source of truth (bot/universe.py) and the
invariant that the price feed fetches exactly the universe — no more (BNB, which has
no Kraken pair, must not leak back in), no less (every alt must be fetchable).
"""

from bot import settings
from bot.universe import tradeable_universe, TRADEABLE_UNIVERSE
from bot.price_feed import (
    _GECKO_ID_MAP,
    _COINCAP_ID_MAP,
    _KRAKEN_PAIR_MAP,
    COINGECKO_URL,
    COINCAP_URL,
)

EXPECTED = {"BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LINK", "AVAX"}


def test_universe_is_the_eight_indicator_coins():
    assert set(TRADEABLE_UNIVERSE) == EXPECTED
    assert tradeable_universe() == TRADEABLE_UNIVERSE  # snapshot matches the accessor


def test_bnb_is_gone_from_the_universe():
    """BNB has no Kraken USD pair -> no indicators/signals -> it must not be tradeable."""
    assert "BNB" not in TRADEABLE_UNIVERSE


def test_universe_dedupes_and_uppercases(monkeypatch):
    monkeypatch.setitem(settings, "universe", ["btc", "ETH", "btc", " sol "])
    assert tradeable_universe() == ["BTC", "ETH", "SOL"]


def test_price_feed_maps_match_the_universe():
    """The fetch maps must equal the universe exactly, so an asset can't be fetched
    without being tradeable (or tradeable without being fetchable)."""
    assert set(_GECKO_ID_MAP.values()) == EXPECTED
    assert set(_COINCAP_ID_MAP.values()) == EXPECTED
    assert set(_KRAKEN_PAIR_MAP.keys()) == EXPECTED


def test_bnb_not_fetched_anywhere():
    assert "BNB" not in _GECKO_ID_MAP.values()
    assert "BNB" not in _COINCAP_ID_MAP.values()
    assert "binancecoin" not in COINGECKO_URL
    assert "binance-coin" not in COINCAP_URL


def test_coincap_fallback_ids_resolve_alts():
    """CoinCap uses different ids than CoinGecko for XRP/AVAX; the fallback map must
    carry them so a CoinGecko outage doesn't silently drop those assets."""
    assert _COINCAP_ID_MAP.get("xrp") == "XRP"
    assert _COINCAP_ID_MAP.get("avalanche") == "AVAX"
    assert "xrp" in COINCAP_URL and "avalanche" in COINCAP_URL


def test_per_asset_exposure_caps_cover_the_alts():
    """Every newly-active alt has a concentration cap so the buy gate + Tier 3 trim
    bound its weight. BTC/ETH ride the general default; SOL keeps its tighter cap."""
    thresholds = settings.get("thresholds", {})
    assert thresholds.get("max_asset_exposure_pct") == 35.0
    assert thresholds.get("max_sol_exposure_pct") == 20.0
    for alt in ("xrp", "ada", "doge", "link", "avax"):
        cap = thresholds.get(f"max_{alt}_exposure_pct")
        assert cap is not None, f"{alt.upper()} has no exposure cap"
        assert 0 < cap <= 20.0
