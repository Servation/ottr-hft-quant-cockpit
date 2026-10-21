"""
Alpaca client tests against FakeAlpaca: parsing, paging, free-plan limits, quote sanity,
auth and rate-limit handling, and the GET-only / paper-host-only guarantees.
"""

from datetime import date, datetime

import pytest

from ottr.clock import FakeClock
from ottr.market.alpaca import (TRADING_PAPER, Alpaca, AlpacaError, Credentials, QuoteLimits,
                                bar_day, check_quote, completed_bars, parse_ts)
from ottr.market.calendar import ET
from ottr.market.http import RateLimiter
from ottr.market.types import Bar
from tests.fake_alpaca import CREDS, FakeAlpaca
from tests.nyse import calendar_2026


def et(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=ET).timestamp()


NOW = et(2026, 10, 21, 11, 0)  # a Wednesday, market open


@pytest.fixture
def fake():
    return FakeAlpaca(NOW)


def client(fake, **kw):
    # A generous limiter so tests don't wait; one that throttles is tested separately.
    return Alpaca(CREDS, client=fake.client(), limiter=RateLimiter(per_minute=1e6), backoff=0,
                  clock=FakeClock(fake.now), **kw)


# -- parsing -------------------------------------------------------------------------------

@pytest.mark.parametrize("s", ["2026-10-21T15:00:00Z", "2026-10-21T15:00:00.123456789Z",
                               "2026-10-21T11:00:00-04:00", "2026-10-21T11:00:00.5-04:00"])
def test_parse_ts_handles_nanoseconds_and_offsets(s):
    assert parse_ts(s) == pytest.approx(NOW, abs=1)


def test_daily_bar_day_is_read_in_new_york_time():
    assert bar_day("2026-10-21T04:00:00Z") == date(2026, 10, 21)   # EDT midnight
    assert bar_day("2026-12-01T05:00:00Z") == date(2026, 12, 1)    # EST midnight


def test_credentials_never_print_the_secret_and_know_their_kind(monkeypatch):
    c = Credentials("PKABC123", "topsecret")
    assert "topsecret" not in repr(c) and c.kind == "paper"
    assert Credentials("AKXYZ", "s").kind == "live"
    monkeypatch.setenv("APCA_API_KEY_ID", "PK1")
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    assert Credentials.from_env() is None  # half a pair is no pair


# -- trading API -----------------------------------------------------------------------------

async def test_clock_and_calendar(fake):
    a = client(fake)
    clk = await a.clock()
    assert clk.is_open and clk.next_close == pytest.approx(et(2026, 10, 21, 16, 0), abs=1)
    cal = await a.calendar(date(2026, 11, 20), date(2026, 11, 30))
    bf = cal.session_on(date(2026, 11, 27))
    assert bf.early_close and cal.session_on(date(2026, 11, 26)) is None  # Thanksgiving


async def test_assets_skip_symbols_ottr_cannot_hold(fake):
    for s in ("SPY", "BRK.B", "ABCDEFG"):
        fake.add_asset(s, fractionable=(s != "BRK.B"))
    got = await client(fake).assets()
    assert set(got) == {"SPY", "BRK.B"} and not got["BRK.B"].fractionable


async def test_one_asset_by_any_spelling(fake):
    fake.add_asset("BRK.B")
    assert (await client(fake).asset("brk-b")).symbol == "BRK.B"


# -- data API --------------------------------------------------------------------------------

async def test_snapshots_are_chunked_and_keyed_by_canonical_symbol(fake):
    syms = [chr(65 + i // 26) + chr(65 + i % 26) + "Q" for i in range(250)]  # AAQ, ABQ, ...
    for s in syms[:3]:
        fake.add_quote(s, 10, 10.01)
    snaps = await client(fake).snapshots(syms)
    assert set(snaps) == set(syms[:3])
    assert fake.paths().count("/v2/stocks/snapshots") == 3  # 250 symbols, 100 per request


async def test_daily_bars_follow_pages_across_symbols(fake):
    days = [date(2026, 10, d) for d in (14, 15, 16, 19, 20)]
    fake.add_daily_bars("SPY", {d: 500 + i for i, d in enumerate(days)})
    fake.add_daily_bars("QQQ", {d: 400 + i for i, d in enumerate(days)})
    fake.page_size = 3  # forces 4 pages; the first holds only SPY
    bars = await client(fake).daily_bars(["SPY", "QQQ"], date(2026, 10, 1))
    assert [b.close for b in bars["SPY"]] == [500, 501, 502, 503, 504]
    assert [b.day for b in bars["QQQ"]] == days
    assert fake.paths().count("/v2/stocks/bars") == 4


async def test_daily_bars_never_ask_for_recent_sip_data(fake):
    # The fake refuses SIP data newer than 15 minutes, as the free plan does.
    fake.add_daily_bars("SPY", {date(2026, 10, 20): 500})
    bars = await client(fake).daily_bars(["SPY"], date(2026, 10, 1), end=date(2026, 10, 21), now=NOW)
    assert bars["SPY"][0].close == 500
    end = parse_ts(dict(fake.requests[-1].url.params)["end"])
    assert end <= NOW - 16 * 60 + 1


async def test_symbols_without_bars_come_back_empty(fake):
    assert (await client(fake).daily_bars(["SPY"], date(2026, 10, 1)))["SPY"] == []


def test_todays_bar_counts_only_after_close_plus_sip_delay():
    cal = calendar_2026()
    bars = [Bar(date(2026, 10, 20), 1, 1, 1, 1, 1), Bar(date(2026, 10, 21), 1, 1, 1, 1, 1)]
    assert [b.day.day for b in completed_bars(bars, cal, et(2026, 10, 21, 15, 0))] == [20]
    assert [b.day.day for b in completed_bars(bars, cal, et(2026, 10, 21, 16, 5))] == [20]
    assert [b.day.day for b in completed_bars(bars, cal, et(2026, 10, 21, 16, 20))] == [20, 21]
    # Old history before the calendar's range is kept, not silently dropped.
    old = [Bar(date(2025, 6, 2), 1, 1, 1, 1, 1)]
    assert completed_bars(old, cal, et(2026, 10, 21, 12, 0)) == old


# -- failure handling --------------------------------------------------------------------------

async def test_a_plan_refusal_is_not_reported_as_bad_keys(fake):
    fake.add_daily_bars("SPY", {date(2026, 10, 20): 500})
    a = Alpaca(CREDS, client=fake.client(), limiter=RateLimiter(1e6), backoff=0, clock=FakeClock(NOW + 3600))
    with pytest.raises(AlpacaError, match="recent SIP data") as e:  # our clock ran ahead of Alpaca's
        await a.daily_bars(["SPY"], date(2026, 10, 1))
    assert "keys" not in str(e.value)


async def test_bad_keys_fail_at_once_with_a_clear_message(fake):
    a = Alpaca(Credentials("PKWRONG", "nope"), client=fake.client(), limiter=RateLimiter(1e6), backoff=0)
    with pytest.raises(AlpacaError, match="rejected the keys"):
        await a.clock()
    assert len(fake.requests) == 1


async def test_rate_limited_requests_wait_for_the_reset_and_retry(fake, monkeypatch):
    waits = []

    async def sleep(s):
        waits.append(s)
    import ottr.market.http as http
    monkeypatch.setattr(http.asyncio, "sleep", sleep)
    monkeypatch.setattr(http.time, "time", lambda: NOW)
    fake.fail_429["/v2/clock"] = 2
    assert (await client(fake).clock()).is_open
    assert waits == [pytest.approx(2.0), pytest.approx(2.0)]  # the X-RateLimit-Reset window


async def test_unknown_asset_is_an_error_not_a_retry(fake):
    with pytest.raises(AlpacaError, match="404"):
        await client(fake).asset("ZZZZ")
    assert len(fake.requests) == 1


async def test_client_only_ever_sends_gets(fake):
    a = client(fake)
    fake.add_quote("SPY", 500, 500.05)
    fake.add_daily_bars("SPY", {date(2026, 10, 20): 500})
    await a.clock()
    await a.calendar(date(2026, 10, 19), date(2026, 10, 23))
    await a.snapshots(["SPY"])
    await a.daily_bars(["SPY"], date(2026, 10, 1))
    assert {r.method for r in fake.requests} == {"GET"}


def test_refuses_any_trading_host_but_paper():
    with pytest.raises(ValueError, match="paper host only"):
        Alpaca(CREDS, trading_base="https://api.alpaca.markets")
    assert Alpaca(CREDS).trading_base == TRADING_PAPER


# -- quote sanity ------------------------------------------------------------------------------

def snap(fake, sym="SPY", **kw):
    fake.add_quote(sym, **kw)
    return fake.snaps[sym]


@pytest.mark.parametrize("kw,reason", [
    (dict(bid=0, ask=500.1), "one_sided"),
    (dict(bid=500.2, ask=500.1), "crossed"),
    (dict(bid=495, ask=505), "too_wide"),
    (dict(bid=500, ask=500.1, age=600), "stale"),
    (dict(bid=500, ask=500.1, last=520, trade_age=10), "off_market"),       # vs a fresh trade
    (dict(bid=500, ask=500.1, last=520, trade_age=3600, prev_close=300), "off_market"),  # vs prev close
])
def test_bad_quotes_are_rejected_with_a_reason(fake, kw, reason):
    q, why = check_quote("SPY", snap(fake, **kw), NOW)
    assert q is None and why.startswith(reason)


def test_a_sane_quote_becomes_a_live_quote_stamped_with_market_time(fake):
    s = snap(fake, bid=500, ask=500.10, age=3, last=500.02, trade_age=10)
    q, why = check_quote("spy", s, NOW)
    assert why == "ok" and q.asset == "SPY" and q.source == "alpaca" and q.basis == "live"
    assert q.ts == pytest.approx(NOW - 3, abs=0.2) and q.last == 500.02  # fake adds .123456789s


def test_an_old_trade_does_not_veto_a_good_quote(fake):
    s = snap(fake, bid=500, ask=500.1, last=470, trade_age=3600)  # stale print, fresh quote
    assert check_quote("SPY", s, NOW)[1] == "ok"


def test_missing_pieces(fake):
    assert check_quote("SPY", None, NOW)[1] == "no_snapshot"
    assert check_quote("SPY", {"latestTrade": {}}, NOW)[1] == "no_quote"


def test_limits_are_tunable(fake):
    s = snap(fake, bid=500, ask=503)  # 0.6% spread
    assert check_quote("SPY", s, NOW)[1] == "ok"
    assert check_quote("SPY", s, NOW, QuoteLimits(max_spread_pct=0.5))[1].startswith("too_wide")


async def test_rate_limiter_is_shared_across_calls(fake):
    t = {"now": 0.0}

    async def sleep(s):
        t["now"] += s
    lim = RateLimiter(per_minute=60, capacity=1, now=lambda: t["now"], sleep=sleep)
    a = Alpaca(CREDS, client=fake.client(), limiter=lim, backoff=0)
    for _ in range(3):
        await a.clock()
    assert t["now"] == pytest.approx(2.0)


def test_fake_calendar_matches_the_test_calendar():
    # Guards the fixture itself: the fake serves the same sessions the calendar tests use.
    assert FakeAlpaca(NOW).sessions[0].day == calendar_2026().sessions[0].day
