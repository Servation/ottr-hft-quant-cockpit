"""
Screener tests on synthetic candles and a fake Kraken: which pairs are eligible (and why
not), what qualifies (every horizon up, ahead of BTC, near the 30-day high), the flags that
halve a score (pulling back, overextended), the rank-based score, the full run, the digest,
and the engine's daily task (stored, audited, posted, never fatal). Nothing touches the
network.
"""

import asyncio
import dataclasses
import json
import time

import pytest

from ottr import config as cfgmod
from ottr import health
from ottr.config import ConfigError
from ottr.engine import engine as engmod
from ottr.engine.engine import Engine
from ottr.market import screen
from ottr.market.kraken import KrakenError
from ottr.market.screen import Row, measure
from ottr.market.types import MarketDataError
from ottr.notify import Alerts
from tests.fakes import DAY, PRICES, FakeMarket, sample_screen, sample_screen_result, trend_rows


@pytest.fixture
def scfg(cfg):
    return dataclasses.replace(cfg, screen=dataclasses.replace(cfg.screen, enabled=True))


BTC = trend_rows(drift=0.001, noise=0.02)


def pump_then_dump():
    rows = trend_rows(n=170, drift=0.001, noise=0.01)
    px, ts = rows[-1][1], rows[-1][0]
    for i in range(23):    # +150% in about three weeks
        px *= 1.041
        ts += DAY
        rows.append((ts, px, 5e6))
    for i in range(7):     # then -35% in a week
        px *= 0.94
        ts += DAY
        rows.append((ts, px, 5e6))
    return rows


# -- eligibility -----------------------------------------------------------------------------
def test_listings_keep_tradable_usd_spot_pairs():
    pairs = {
        "XXBTZUSD": {"wsname": "XBT/USD", "altname": "XBTUSD", "status": "online"},
        "NEARUSD": {"wsname": "NEAR/USD", "altname": "NEARUSD", "status": "online"},
        "NEARUSD.d": {"wsname": "NEAR/USD", "altname": "NEARUSD.d", "status": "online"},
        "NEAREUR": {"wsname": "NEAR/EUR", "altname": "NEAREUR", "status": "online"},
        "USDTZUSD": {"wsname": "USDT/USD", "altname": "USDTZUSD", "status": "online"},
        "ZEURZUSD": {"wsname": "EUR/USD", "altname": "ZEURZUSD", "status": "online"},
        "OLDUSD": {"wsname": "OLD/USD", "altname": "OLDUSD", "status": "cancel_only"},
    }
    found, dropped = screen.listings(pairs)
    assert set(found) == {"BTC", "NEAR"} and found["BTC"].pair == "XBTUSD" and found["BTC"].key == "XXBTZUSD"
    assert dropped == {"stablecoin or fiat": 2, "not trading (cancel or post only)": 1}


def tick(bid, ask, base_vol, vwap):
    return {"b": [str(bid), "1", "1"], "a": [str(ask), "1", "1"], "v": ["0", str(base_vol)], "p": ["0", str(vwap)]}


def test_prefilter_keeps_the_liquid_tight_pairs_most_liquid_first(scfg):
    cfg = dataclasses.replace(scfg, screen=dataclasses.replace(scfg.screen, max_pairs=2))
    found = {s: screen.Listing(s, f"{s}USD", f"{s}USD") for s in ("AAA", "BBB", "CCC", "WIDE", "THIN", "BTC")}
    tickers = {"AAAUSD": tick(10, 10.01, 3e5, 10), "BBBUSD": tick(5, 5.001, 2e5, 5),
               "CCCUSD": tick(1, 1.0005, 9e6, 1), "WIDEUSD": tick(1, 1.02, 9e6, 1),
               "THINUSD": tick(1, 1.001, 1e4, 1), "BTCUSD": tick(1, 1, 1e9, 1)}
    keep, dropped = screen.prefilter(found, tickers, cfg, skip=["BTC"])
    assert [k.symbol for k in keep] == ["CCC", "AAA"]  # by 24h USD volume, capped at max_pairs
    assert dropped == {"spread too wide": 1, "too illiquid": 1, "beyond the scan limit (max_pairs)": 1}


def test_ineligible_pairs_say_why(scfg):
    def why(rows, **kw):
        return measure("X", "XUSD", rows, BTC, scfg, in_universe=kw.get("in_universe", False)).reason

    assert why(trend_rows(n=100)).startswith("new listing (100 days")
    gappy = trend_rows()
    gappy = gappy[:-20] + [(ts + DAY, c, v) for ts, c, v in gappy[-20:]]
    assert why(gappy) == "gaps in its daily candles"
    assert why(trend_rows(drift=0.0, noise=0.001, start=1.0)).startswith("pegged")
    assert why(trend_rows(usd=2e5)).startswith("too illiquid ($200,000/day")
    assert why(trend_rows(usd=2e5), in_universe=True) == ""  # the desk already trades it


# -- what qualifies --------------------------------------------------------------------------
def test_a_steady_trend_ahead_of_btc_qualifies(scfg):
    r = measure("NEAR", "NEARUSD", trend_rows(drift=0.006, noise=0.01, phase=1), BTC, scfg, in_universe=False)
    assert r.eligible and r.qualifies and r.regime == "TRENDING" and not r.flags
    assert all(z > 0 for z in r.trend_by_horizon) and r.vs_btc > 0 and r.pullback > -0.2
    assert 0 < r.sizable <= scfg.cap_pct("NEAR") / 100 and screen.why_not(dataclasses.asdict(r)) == ""


def test_a_pump_that_is_reversing_is_flagged_not_promoted(scfg):
    r = measure("PUMP", "PUMPUSD", pump_then_dump(), BTC, scfg, in_universe=False)
    assert r.eligible and r.chg_30d > 0.5  # still up a lot on the month...
    assert "pulling back" in r.flags and not r.qualifies  # ...but 35% off its high
    assert screen.why_not(dataclasses.asdict(r)).startswith("pulling back (-")


def test_a_fresh_dip_in_a_long_uptrend_does_not_qualify(scfg):
    rows = trend_rows(n=186, drift=0.006, noise=0.01)
    px, ts = rows[-1][1], rows[-1][0]
    for _ in range(14):    # two soft weeks: about -8%, not a 20% pullback
        px *= 0.994
        ts += DAY
        rows.append((ts, px, 5e6))
    r = measure("DIP", "DIPUSD", rows, BTC, scfg, in_universe=False)
    assert r.trend_by_horizon[0] < 0 < r.trend_by_horizon[2] and "pulling back" not in r.flags
    assert not r.qualifies and screen.why_not(dataclasses.asdict(r)).startswith("not up over 14d")


def test_a_spike_is_flagged_overextended(scfg):
    rows = trend_rows(n=193, drift=0.0, noise=0.01)
    px, ts = rows[-1][1], rows[-1][0]
    for _ in range(7):
        px *= 1.06
        ts += DAY
        rows.append((ts, px, 5e6))
    assert "overextended" in measure("HOT", "HOTUSD", rows, BTC, scfg, in_universe=False).flags


def test_btc_is_its_own_benchmark(scfg):
    r = measure("BTC", "XBTUSD", BTC, BTC, scfg, in_universe=True)
    assert r.vs_btc == 0.0 and r.corr_btc == 1.0 and not r.qualifies


# -- score -----------------------------------------------------------------------------------
def test_percentiles_handle_ties_and_unknowns():
    assert screen._percentiles([1.0]) == [0.5]
    assert screen._percentiles([3.0, 1.0, 2.0]) == [1.0, 0.0, 0.5]
    assert screen._percentiles([1.0, 1.0, None]) == [0.75, 0.75, 0.0]  # unknown ranks below known
    assert screen._percentiles([None, 2.0, None]) == [0.25, 1.0, 0.25]


def test_score_ranks_the_better_pair_first_and_halves_flagged_ones():
    def row(sym, v, flags=()):
        return Row(symbol=sym, pair=sym, in_universe=False, eligible=True, trend=v, vs_btc=v, sizable=v, er=v,
                   corr_btc=1 - v, participation=v, flags=flags)

    ranked = screen.score([row("LOW", 0.1), row("HIGH", 0.9), row("MID", 0.5),
                           Row(symbol="OUT", pair="OUT", in_universe=False, eligible=False)])
    assert [r.symbol for r in ranked] == ["HIGH", "MID", "LOW"] and [r.rank for r in ranked] == [1, 2, 3]
    assert ranked[0].score == 100.0 and ranked[-1].score == 0.0
    flagged = screen.score([row("A", 0.9, ("pulling back",)), row("B", 0.7), row("C", 0.5), row("D", 0.1)])
    assert [r.symbol for r in flagged] == ["B", "A", "C", "D"]  # best on every measure, but halved
    assert next(r for r in flagged if r.symbol == "A").score == 50.0


# -- the run ---------------------------------------------------------------------------------
class FakeKraken:
    def __init__(self, candles, fail_pairs=(), fail_listing=False):
        self.candles, self.fail_pairs, self.fail_listing = candles, set(fail_pairs), fail_listing
        self.ohlc_calls = []

    async def asset_pairs(self):
        if self.fail_listing:
            raise KrakenError("AssetPairs failed: HTTP 503")
        pairs = {"XXBTZUSD": {"wsname": "XBT/USD", "altname": "XBTUSD", "status": "online"},
                 "USDCUSD": {"wsname": "USDC/USD", "altname": "USDCUSD", "status": "online"}}
        for a in self.candles:
            if a != "XBTUSD":
                pairs[a] = {"wsname": f"{a[:-3]}/USD", "altname": a, "status": "online"}
        return pairs

    async def tickers(self):
        return {("XXBTZUSD" if a == "XBTUSD" else a): tick(1, 1.0005, 1e8, 1) for a in self.candles}

    async def daily_ohlc(self, pair):
        self.ohlc_calls.append(pair)
        if pair in self.fail_pairs or pair not in self.candles:
            raise KrakenError(f"OHLC failed for {pair}")
        return self.candles[pair]


async def test_a_full_run_ranks_candidates_against_the_universe(scfg):
    cfg = dataclasses.replace(scfg, universe=["BTC", "ETH"])
    k = FakeKraken({"XBTUSD": BTC, "ETHUSD": trend_rows(drift=-0.002, noise=0.025),
                    "NEARUSD": trend_rows(drift=0.006, noise=0.01, phase=1),
                    "PUMPUSD": pump_then_dump(), "NEWUSD": trend_rows(n=60), "BADUSD": BTC},
                   fail_pairs={"BADUSD"})
    res = await screen.run(k, cfg, pause=0)
    by = {r.symbol: r for r in res.rows}
    assert set(by) == {"BTC", "ETH", "NEAR", "PUMP"} and by["BTC"].in_universe and not by["NEAR"].in_universe
    assert [r.symbol for r in res.candidates(5)] == ["NEAR"]
    assert [r.symbol for r in res.rejected] == ["NEW"] and res.rejected[0].reason.startswith("new listing")
    assert res.errors == ("BAD: OHLC failed for BADUSD",)
    assert res.excluded == {"stablecoin or fiat": 1} and res.listed == 6
    assert k.ohlc_calls.count("XBTUSD") == 1  # BTC is fetched once and reused as the benchmark
    d = res.to_dict()
    assert json.loads(json.dumps(d))["rows"][0]["symbol"] == res.rows[0].symbol  # storable as JSON


async def test_a_run_without_the_pair_list_fails_cleanly(scfg):
    with pytest.raises(MarketDataError, match="could not list Kraken pairs"):
        await screen.run(FakeKraken({}, fail_listing=True), scfg, pause=0)


def test_the_digest_lists_candidates_or_says_none_qualify(scfg):
    yes = screen.digest(sample_screen(scfg), 5)
    assert yes.startswith("**Screener**: 2 candidate(s)") and "**NEAR**" in yes and "Advisory" in yes
    no = screen.digest(sample_screen(scfg, qualifying=False), 5)
    assert "no pair outside the universe qualifies" in no and "Closest: NEAR" in no
    assert "\u2014" not in yes + no
    assert screen.promote_hint({"symbol": "NEAR", "pair": "NEARUSD"}) == (
        'To trade NEAR: add it to `universe`, `NEAR = 10.0` under [caps] and `NEAR = "NEARUSD"` under '
        "[pairs] in config.toml, then `ottr service restart`.")


# -- the engine's daily task -----------------------------------------------------------------
class Recorder:
    def __init__(self):
        self.posts = []

    async def __call__(self, text):
        self.posts.append(text)
        return True


async def test_the_engine_stores_audits_and_posts_each_screen(scfg, store, ledger):
    async def screener():
        return sample_screen_result(scfg)

    rec = Recorder()
    eng = Engine(scfg, store, FakeMarket(scfg, PRICES), alerts=Alerts(rec), screener=screener)
    async with eng.alerts.running():
        await eng.screen_tick()
    assert store.latest_screen()["id"] == "s-test"
    (audit,) = store.audits("screen")
    assert audit["candidates"] == ["NEAR", "XMR"]
    assert rec.posts and rec.posts[0].startswith("**Screener**: 2 candidate(s)")


async def test_a_failing_screen_marks_its_heartbeat_and_keeps_the_engine_going(scfg, store, ledger, monkeypatch):
    monkeypatch.setattr(engmod, "SCREEN_FIRST_DELAY", 0)
    calls = []

    async def broken():
        calls.append(time.time())
        raise MarketDataError("screen: could not list Kraken pairs: HTTP 503")

    eng = Engine(scfg, store, FakeMarket(scfg, PRICES), screener=broken)
    task = asyncio.create_task(eng._screen_loop())
    for _ in range(100):
        if store.heartbeats().get("screen"):
            break
        await asyncio.sleep(0.02)
    hb = store.heartbeats()["screen"]
    assert hb["ok"] is False and "HTTP 503" in hb["detail"] and not task.done()
    await asyncio.sleep(0.2)
    assert len(calls) == 1  # retried after SCREEN_RETRY_SEC, not in a tight loop
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_the_screen_heartbeat_is_a_warning_only_when_enabled(cfg, scfg):
    assert "screen" not in health.intervals(cfg)
    assert health.intervals(scfg)["screen"] == 24 * 3600 and health.SEVERITY["screen"] == health.WARNING


# -- config ----------------------------------------------------------------------------------
def _toml(tmp_path, old, new):
    p = tmp_path / "c.toml"
    text = cfgmod.DEFAULT_CONFIG.read_text(encoding="utf-8")
    assert old in text
    p.write_text(text.replace(old, new), encoding="utf-8")
    return p


def test_a_universe_asset_needs_a_kraken_pair(tmp_path):
    p = _toml(tmp_path, '"LINK", "AVAX"]', '"LINK", "AVAX", "NEAR"]')
    with pytest.raises(ConfigError, match=r"no Kraken pair for \['NEAR'\].*NEAR = \"NEARUSD\""):
        cfgmod.load(p, data_dir=tmp_path)
    p.write_text(p.read_text(encoding="utf-8") + '\n[pairs]\nnear = "NEARUSD"\n', encoding="utf-8")
    c = cfgmod.load(p, data_dir=tmp_path)
    assert c.kraken_pair("NEAR") == "NEARUSD" and c.kraken_pair("BTC") == "XBTUSD"


@pytest.mark.parametrize("old,new,match", [
    ("min_history_days = 120", "min_history_days = 30", "min_history_days"),
    ("top_n = 5", "top_n = 0", "top_n"),
    ("max_spread_pct = 0.30", "max_spread_pct = 0", "max_spread_pct"),
    ("max_pairs = 60", "max_pairz = 60", r"\[screen\] unknown keys"),
])
def test_bad_screen_config_fails_loudly(tmp_path, old, new, match):
    with pytest.raises(ConfigError, match=match):
        cfgmod.load(_toml(tmp_path, old, new), data_dir=tmp_path)
