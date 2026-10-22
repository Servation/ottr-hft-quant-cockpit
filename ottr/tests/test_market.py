"""
US market data (PR 7a): the stored calendar and bars, the session rules for marking the
book, the SPY and 60/40 benchmarks, and MarketData end to end against FakeAlpaca:
caching and outages, final-only daily bars, marks across the day, vol and regime, and the
council packet.
"""

from datetime import date, datetime

import pytest

from ottr.clock import FakeClock
from ottr.config import load
from ottr.events.view import MacroReading
from ottr.ledger.book import Ledger
from ottr.ledger.marks import mark
from ottr.ledger.store import Store
from ottr.market import benchmarks as bm
from ottr.market import history
from ottr.market.alpaca import Alpaca
from ottr.market.calendar import ET, TradingCalendar
from ottr.market.http import RateLimiter
from ottr.market.types import BASIS_CLOSE, BASIS_DELAYED, BASIS_LIVE, Bar, MarketDataError, Quote
from ottr.market.service import MarketData
from tests.fake_alpaca import CREDS, FakeAlpaca
from tests.nyse import calendar_2026

CAL = calendar_2026()


def et(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=ET).timestamp()


WED_11 = et(2026, 10, 21, 11)          # mid-session
WED_8 = et(2026, 10, 21, 8)            # before the open
WED_1605 = et(2026, 10, 21, 16, 5)     # closed, today's bar not final yet
WED_1620 = et(2026, 10, 21, 16, 20)    # closed, today's bar final
SAT = et(2026, 10, 24, 12)


@pytest.fixture
def ucfg(tmp_path):
    return load(data_dir=tmp_path)


def bar(day, close):
    return Bar(day, close, close, close, close, 1000.0)


# -- stored history --------------------------------------------------------------------------
def test_calendar_round_trips_and_extends_its_range(store):
    a = TradingCalendar([s for s in CAL.sessions if s.day <= date(2026, 6, 30)], date(2026, 1, 1), date(2026, 6, 30))
    b = TradingCalendar([s for s in CAL.sessions if s.day >= date(2026, 6, 1)], date(2026, 6, 1), date(2026, 12, 31))
    history.save_calendar(store, a)
    history.save_calendar(store, b)
    got = history.load_calendar(store)
    assert (got.first_day, got.last_day) == (date(2026, 1, 1), date(2026, 12, 31))
    assert got.sessions == CAL.sessions                       # early closes and settlement dates too
    assert got.session_on(date(2026, 11, 26)) is None         # Thanksgiving: a holiday, in range


def test_a_disjoint_calendar_replaces_the_stored_one(store):
    history.save_calendar(store, TradingCalendar([s for s in CAL.sessions if s.day.month == 1],
                                                 date(2026, 1, 1), date(2026, 1, 31)))
    history.save_calendar(store, TradingCalendar([s for s in CAL.sessions if s.day.month == 10],
                                                 date(2026, 10, 1), date(2026, 10, 31)))
    got = history.load_calendar(store)
    assert got.first_day == date(2026, 10, 1) and all(s.day.month == 10 for s in got.sessions)


def test_raw_bars_upsert_and_adjusted_closes_replace_whole(store):
    d1, d2 = date(2026, 10, 19), date(2026, 10, 20)
    history.save_bars(store, {"SPY": [bar(d1, 100), bar(d2, 101)]})
    history.save_bars(store, {"SPY": [bar(d2, 101.5)]})                    # a corrected bar
    assert [b.close for b in history.raw_bars(store, "SPY")] == [100, 101.5]
    history.replace_adjusted(store, {"SPY": [bar(d1, 99), bar(d2, 101)]})
    history.replace_adjusted(store, {"SPY": [bar(d2, 101)]})               # a new dividend: history rewritten
    assert history.adjusted_closes(store, "SPY") == [(d2, 101)]
    history.replace_adjusted(store, {"SPY": []})                           # an outage: keep what we had
    assert history.adjusted_closes(store, "SPY") == [(d2, 101)]


# -- marks -------------------------------------------------------------------------------------
LIVE = Quote("SPY", 600.0, 600.1, 600.05, WED_11, "alpaca")
BARS = [bar(date(2026, 10, 19), 590.0), bar(date(2026, 10, 20), 595.0)]


@pytest.mark.parametrize("now,live,delayed,bars,expect", [
    (WED_11, LIVE, None, BARS, ("live", 600.0)),
    (WED_11, None, (et(2026, 10, 21, 10, 44), 598.5), BARS, (BASIS_DELAYED, 598.5)),
    (WED_11, None, (et(2026, 10, 21, 9, 15), 597.0), BARS, None),        # a pre-market print isn't a session price
    (WED_11, None, None, BARS, None),                                    # unknown, never yesterday's close
    (WED_8, LIVE, None, BARS, (BASIS_CLOSE, 595.0)),                     # before the open: yesterday's close
    (WED_1605, None, (et(2026, 10, 21, 15, 49), 601.0), BARS, (BASIS_DELAYED, 601.0)),
    (WED_1620, None, (et(2026, 10, 21, 15, 59), 601.0), BARS + [bar(date(2026, 10, 21), 602.0)], (BASIS_CLOSE, 602.0)),
    (WED_1620, None, (et(2026, 10, 21, 16, 4), 603.0), BARS, None),      # an after-hours print isn't the close
    (SAT, None, None, BARS + [bar(date(2026, 10, 21), 602), bar(date(2026, 10, 23), 604)], (BASIS_CLOSE, 604.0)),
])
def test_marks_follow_the_session(now, live, delayed, bars, expect):
    m = mark("SPY", CAL.state_at(now), live, delayed, bars)
    if expect is None:
        assert m is None
    else:
        assert (m.basis, m.bid) == expect
        if m.basis != BASIS_LIVE:
            assert m.bid == m.ask == m.last                  # one price, no invented spread


# -- benchmarks --------------------------------------------------------------------------------
def test_spy_and_60_40_from_day_0():
    days = [date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 2)]
    closes = {"SPY": list(zip(days, [100.0, 110.0, 99.0, 121.0])), "IEF": list(zip(days, [50.0, 50.0, 55.0, 55.0]))}
    base = {"day": "2026-09-29", "equity": 10000.0}
    got = bm.benchmark_values(base, closes, {"SPY": 0.0, "IEF": 0.0})
    assert got["spy"] == pytest.approx(12100.0)
    # 60/40 by hand: 60 SPY units + 80 IEF units; Oct 1 is the new month's first session:
    # 60*99 + 80*55 = 10340, rebalanced to 62.6667 SPY + 75.2 IEF; Oct 2: 62.6667*121 + 75.2*55.
    assert got["60_40"] == pytest.approx(10340 * 0.6 / 99 * 121 + 10340 * 0.4)
    moved = bm.benchmark_values(base, closes, {"SPY": 0.01, "IEF": 0.0})
    assert moved["spy"] == pytest.approx(12100.0 * 1.01)
    w_spy = 10340 * 0.6 / 99 * 121 / got["60_40"]
    assert moved["60_40"] == pytest.approx(got["60_40"] * (1 + 0.01 * w_spy))


def test_benchmarks_are_unknown_rather_than_guessed():
    closes = {"SPY": [(date(2026, 10, 1), 100.0)], "IEF": [(date(2026, 10, 1), 50.0)]}
    assert bm.benchmark_values(None, closes, {"SPY": 0, "IEF": 0}) == {"spy": None, "60_40": None}
    later = {"day": "2026-09-30", "equity": 1.0}                          # no close on day 0
    assert bm.benchmark_values(later, closes, {"SPY": 0, "IEF": 0}) == {"spy": None, "60_40": None}
    base = {"day": "2026-10-01", "equity": 1.0}
    assert bm.benchmark_values(base, closes, {"SPY": 0.0, "IEF": None}) == {"spy": 1.0, "60_40": None}


# -- the service --------------------------------------------------------------------------------
def trend(days, start, step):
    return {d: round(start * (1 + step) ** i * (1 + (0.004 if i % 2 else -0.004)), 4) for i, d in enumerate(days)}


def fake_market(ucfg, now, through=None):
    """Alpaca with 2026 history for every core and reference symbol through `through`
    (default: the last session before `now`'s New York date)."""
    f = FakeAlpaca(now)
    today = datetime.fromtimestamp(now, tz=ET).date()
    days = [s.day for s in f.sessions if s.day < (through or today)]
    syms = list(ucfg.universe.core) + list(ucfg.universe.reference)
    for i, s in enumerate(syms):
        step = -0.0015 if s in ("XLU", "XLP", "XLV", "XLRE") else 0.001 + 0.0001 * i
        f.add_daily_bars(s, trend(days, 50.0 + 10 * i, step))
        f.add_asset(s)
    return f


def service(ucfg, store, fake, now):
    clock = FakeClock(now)
    a = Alpaca(CREDS, client=fake.client(), limiter=RateLimiter(1e6), backoff=0, clock=clock)
    return MarketData(ucfg, a, store, clock), clock


def snaps_for(fake, syms, age=2.0):
    for s in syms:
        last = fake.bars[s][-1]["c"]
        fake.add_quote(s, last * 0.9999, last * 1.0001, age=age)


async def test_the_calendar_is_kept_for_a_restart_without_alpaca(ucfg, tmp_path):
    store = Store(tmp_path / "a.db", writer=True)
    f = fake_market(ucfg, WED_11)
    svc, _ = service(ucfg, store, f, WED_11)
    with pytest.raises(MarketDataError, match="no trading calendar"):
        svc.calendar
    await svc.refresh_calendar()
    await svc.refresh_calendar()                                       # once a day
    assert [r.url.path for r in f.requests].count("/v2/calendar") == 1
    store.close()
    store = Store(tmp_path / "a.db", writer=True)
    down = FakeAlpaca(WED_11)
    down.fail_429["/v2/calendar"] = 99
    svc2, _ = service(ucfg, store, down, WED_11)
    assert svc2.calendar.state_at(WED_11).is_open                      # known before Alpaca answers
    assert (await svc2.refresh_calendar(force=True)).state_at(WED_11).is_open   # an outage keeps it
    store.close()


async def test_snapshots_are_cached_and_an_outage_serves_quotes_judged_by_age(ucfg, store):
    f = fake_market(ucfg, WED_11)
    snaps_for(f, ucfg.universe.core)
    svc, clock = service(ucfg, store, f, WED_11)
    await svc.refresh_calendar()
    q, why = await svc.quote("SPY")
    assert q is not None and why == "ok"
    n = len(f.requests)
    await svc.snapshots()
    assert len(f.requests) == n                                         # within quote_ttl_sec
    clock.advance(300)
    f.now += 300
    f.fail_429["/v2/stocks/snapshots"] = 99
    q, why = await svc.quote("SPY")                                     # the old snapshot, now 5 min stale
    assert q is None and why.startswith("stale")


async def test_an_order_quote_is_refetched_when_the_cache_is_10s_old(ucfg, store):
    f = fake_market(ucfg, WED_11)
    snaps_for(f, ucfg.universe.core)
    svc, clock = service(ucfg, store, f, WED_11)
    await svc.refresh_calendar()
    await svc.quote("SPY")
    n = len(f.requests)
    clock.advance(11)
    await svc.quote("SPY")
    assert len(f.requests) == n + 1


async def test_daily_bars_are_stored_once_final_and_never_partial(ucfg, store):
    f = fake_market(ucfg, WED_1605, through=date(2026, 10, 22))      # Alpaca already has Wed's partial bar
    svc, clock = service(ucfg, store, f, WED_1605)
    await svc.refresh_calendar()
    assert await svc.refresh_bars() > 0
    assert history.raw_bars(store, "SPY")[-1].day == date(2026, 10, 20)   # Tuesday: Wednesday isn't final
    assert await svc.refresh_bars() == 0                                    # nothing new to fetch
    clock.set(WED_1620)
    f.now = WED_1620
    await svc.refresh_bars()
    assert history.raw_bars(store, "SPY")[-1].day == date(2026, 10, 21)
    assert history.adjusted_closes(store, "SPY")[-1][0] == date(2026, 10, 21)


async def test_marks_for_holdings_through_the_day(ucfg, store):
    f = fake_market(ucfg, WED_11, through=date(2026, 10, 24))          # Alpaca will have Wed-Fri later
    svc, clock = service(ucfg, store, f, WED_11)
    lg = Ledger(store)
    lg.initialize(10000, now=WED_11 - 86400 * 7)
    with store.tx():
        for s in ("SPY", "PDBC"):
            lg.apply_fill(idem_key=f"b{s}", asset=s, side="BUY", quantity=1, price=50, quote_source="t",
                          origin="t", now=WED_11 - 86400)
    await svc.refresh_calendar()
    snaps_for(f, ["SPY"])
    pdbc = f.bars["PDBC"][-1]["c"]
    f.add_quote("PDBC", pdbc * 0.97, pdbc * 1.03, age=2)               # 6% wide: no live quote
    f.add_minute_bar("PDBC", et(2026, 10, 21, 10, 40), 51.25)
    marks = await svc.marks()
    assert marks["SPY"].basis == BASIS_LIVE
    assert (marks["PDBC"].basis, marks["PDBC"].bid) == (BASIS_DELAYED, 51.25)
    assert Ledger(store).value(marks) is not None                       # one thin ETF doesn't blind the book
    clock.set(SAT)
    f.now = SAT
    assert await svc.marks() == {}                 # Friday's close not fetched yet: unknown, not Tuesday's
    await svc.refresh_bars()
    night = await svc.marks()
    assert {m.basis for m in night.values()} == {BASIS_CLOSE}
    assert night["SPY"].bid == f.bars["SPY"][-1]["c"]                  # Friday's official close


async def test_vol_and_regime_come_from_adjusted_closes(ucfg, store):
    f = fake_market(ucfg, WED_1620, through=date(2026, 10, 22))
    svc, _ = service(ucfg, store, f, WED_1620)
    await svc.refresh_calendar()
    await svc.refresh_bars()
    vol, regime = await svc.vol_regime("SPY")
    # Alternating +-0.4% around a steady trend: daily stdev ~0.8%, about 12.7% a year.
    assert 0.10 < vol < 0.16 and regime in ("TRENDING", "CHOPPY")
    assert await svc.vol_regime("NOPE") == (None, "UNKNOWN")


async def test_the_council_packet(ucfg, store):
    f = fake_market(ucfg, WED_11)
    snaps_for(f, list(ucfg.universe.core) + list(ucfg.universe.reference))
    svc, clock = service(ucfg, store, f, WED_11 - 3600)               # bars as of Tuesday night
    await svc.refresh_calendar()
    clock.set(et(2026, 10, 20, 17))
    await svc.refresh_bars()
    clock.set(WED_11)

    class Events:
        def earnings_sessions(self, s, now):
            return None

        def corp_action(self, s, now):
            return "stock merger effective 2026-11-02" if s == "TLT" else None

    macro = [MacroReading("VIXCLS", "VIX", 17.31, date(2026, 10, 20), 1.2, -2.05, False),
             MacroReading("DGS10", "10y", None, None, None, None, True)]
    members = dict(ucfg.universe.core)
    members["NVDA"] = "stock"
    snap = await svc.snapshot(members, Events(), macro)
    text = snap.packet()
    assert text.startswith("AS OF Wed 2026-10-21 11:00 ET. The US market is OPEN: regular session 09:30-16:00 ET")
    assert "closing in 5h00m" in text
    assert "VIX: 17.31 on 2026-10-20 (1w +1.20, 1m -2.05)" in text and "10y: n/a" in text
    assert "BREADTH: 7 of 11 sector ETFs above their 50-day average" in text
    spy = next(line for line in text.splitlines() if line.startswith("SPY [equity_etf]"))
    assert " live |" in spy and "n/a" not in spy.split("|")[2]          # every return window known
    assert "TLT [bond_etf]" in text and "stock merger effective 2026-11-02" in text
    nvda = next(line for line in text.splitlines() if line.startswith("NVDA [stock]"))
    assert "n/a |" in nvda and "earnings date unknown" in nvda and "FLAGS: insufficient_history, no_price" in nvda
    assert "DATA WARNINGS: NVDA has no price" in text
    assert snap.quote("SPY").basis == BASIS_LIVE


async def test_a_closed_market_reads_as_closed(ucfg, store):
    f = fake_market(ucfg, SAT)
    svc, _ = service(ucfg, store, f, SAT)
    await svc.refresh_calendar()
    await svc.refresh_bars()
    text = (await svc.snapshot(dict(ucfg.universe.core))).packet()
    assert "The US market is closed (no session today); next open Mon 2026-10-26 09:30 ET." in text
    spy = next(line for line in text.splitlines() if line.startswith("SPY [equity_etf]"))
    assert " close |" in spy


async def test_benchmarks_from_the_service(ucfg, store):
    f = fake_market(ucfg, SAT)
    svc, _ = service(ucfg, store, f, SAT)
    await svc.refresh_calendar()
    await svc.refresh_bars()
    spy = history.adjusted_closes(store, "SPY")
    day0, c0 = spy[-6]
    base = {"day": day0.isoformat(), "equity": 10000.0}
    got = await svc.benchmarks(base)
    assert got["spy"] == pytest.approx(10000 * spy[-1][1] / c0)       # closed: no move since the close
    assert got["60_40"] is not None and got["60_40"] != got["spy"]


def test_packet_numbers_read_cleanly():
    from ottr.market.packet import _fix, _pct
    assert _pct(-0.0001) == "0.0%" and _pct(0.012) == "+1.2%" and _pct(None) == "n/a"
    assert _fix(53.8123, 1) == "53.8" and _fix(0.0761, 2) == "0.08"


@pytest.mark.live
async def test_live_service_end_to_end(ucfg, tmp_path):
    """The real feed through the whole service: calendar, a year of bars, prices for every
    core ETF, a packet with every return known, and both benchmarks."""
    import time
    from ottr.market.alpaca import Credentials
    creds = Credentials.from_env()
    if creds is None:
        pytest.skip("no Alpaca keys in the environment")
    store = Store(tmp_path / "live.db", writer=True)
    svc = MarketData(ucfg, Alpaca(creds), store)
    await svc.refresh_calendar()
    assert await svc.refresh_bars() > 5000
    snap = await svc.snapshot(dict(ucfg.universe.core))
    missing = [s for s in ucfg.universe.core if snap.quote(s) is None]
    assert not missing, snap.packet()
    assert snap.breadth.above_50d is not None and snap.breadth.sectors == 11
    days = [d for d, _ in svc.adjusted("SPY")]
    got = await svc.benchmarks({"day": days[-20].isoformat(), "equity": 10000.0})
    assert all(v is not None and 8000 < v < 12000 for v in got.values()), got
    assert time.time() - svc.calendar.state_at(time.time()).ts < 5
    store.close()


def test_the_shipped_sectors_are_known_symbols(ucfg):
    assert len(ucfg.universe.sectors) == 11 and "XLE" in ucfg.universe.sectors
    assert set(ucfg.universe.sectors) <= set(ucfg.universe.reference) | set(ucfg.universe.core)
