"""
Events in the book: refresh jobs store each feed (or refuse a bad snapshot and keep the
old one), the gate-facing reader turns data into sessions-to-earnings and BUY blocks
(and stale or missing data into "unknown"), the macro block, and when each feed is due.

Real store, fake feeds, the fake 2026 NYSE calendar and explicit timestamps.
"""

from datetime import date, datetime, timedelta

import pytest

from ottr.config import load
from ottr.events import db
from ottr.events.earnings import AlphaVantage
from ottr.events.fred import Fred
from ottr.events.refresh import (RETRY_SEC, Sources, company_profile, due, refresh_corporate_actions,
                                 refresh_due, refresh_earnings, refresh_macro, refresh_sec)
from ottr.events.sec import Sec
from ottr.events.view import (FEED_CORPORATE, FEED_EARNINGS, FEED_MACRO, FEED_SEC, StoredEvents,
                              macro_readings, sessions_until)
from ottr.market.alpaca import Alpaca
from ottr.market.calendar import ET
from ottr.market.http import RateLimiter
from tests.fake_alpaca import CREDS, FakeAlpaca
from tests.fake_feeds import AV_KEY, FakeAlphaVantage, FakeFred, standard_sec
from tests.nyse import calendar_2026

CAL = calendar_2026()
H = 3600


def et(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=ET).timestamp()


NOW = et(2026, 10, 21, 9, 0)          # Wednesday, before the open
TODAY = date(2026, 10, 21)


@pytest.fixture
def ucfg(tmp_path):
    return load(data_dir=tmp_path)


def events(store, max_age_h=72):
    return StoredEvents(store, lambda: CAL, max_age_h * H)


def av_with(*rows, filler=600):
    f = FakeAlphaVantage()
    for sym, day, tod in rows:
        f.add(sym, day, tod)
    f.many(filler, date(2027, 1, 20))
    return f, AlphaVantage(AV_KEY, client=f.client(), backoff=0)


def alpaca(f):
    return Alpaca(CREDS, client=f.client(), limiter=RateLimiter(1e6), backoff=0)


# -- sessions to a report -------------------------------------------------------------------------
@pytest.mark.parametrize("report,expect", [
    (date(2026, 10, 21), 0),     # today
    (date(2026, 10, 22), 1),     # the next session
    (date(2026, 10, 24), 2),     # a Saturday: Thu and Fri
    (date(2026, 10, 28), 5),     # still inside the 5-session blackout
    (date(2026, 10, 29), 6),     # first day outside it
    (date(2026, 11, 30), 27),    # across Thanksgiving (no session) and Black Friday (a short one)
])
def test_sessions_until_a_report(report, expect):
    assert sessions_until(CAL, TODAY, report) == expect


def test_sessions_past_the_calendar_are_a_lower_bound():
    short = type(CAL)([s for s in CAL.sessions if s.day <= date(2026, 10, 23)], last_day=date(2026, 10, 23))
    assert sessions_until(short, TODAY, date(2026, 11, 20)) == 3     # Thu, Fri, and "at least one more"


# -- earnings -------------------------------------------------------------------------------------
async def test_earnings_refresh_and_the_blackout_it_feeds(store):
    _, av = av_with(("NVDA", date(2026, 10, 27), "post-market"), ("AAPL", date(2026, 11, 2), "post-market"),
                    ("MSFT", date(2026, 10, 20), "post-market"))       # yesterday: not upcoming
    r = await refresh_earnings(store, av, "6month", TODAY, NOW)
    assert r.ok and r.rows == 602 and "through 2027-01-20" in r.detail
    ev = events(store)
    assert ev.earnings_sessions("NVDA", NOW) == 4          # inside the blackout
    assert ev.earnings_sessions("AAPL", NOW) == 8
    assert ev.earnings_sessions("MSFT", NOW) is None       # no upcoming date: unknown
    assert ev.earnings_sessions("SPY", NOW) is None        # an ETF has none (the gate doesn't ask)


async def test_a_new_snapshot_replaces_upcoming_dates_and_keeps_the_past(store):
    store.conn.execute("INSERT INTO earnings(symbol,day,time_of_day,fetched_ts) VALUES('NVDA','2026-08-26','',0)")
    _, av = av_with(("NVDA", date(2026, 11, 18), ""))
    await refresh_earnings(store, av, "6month", TODAY, NOW)
    _, av2 = av_with(("NVDA", date(2026, 11, 19), ""))        # the estimate moved a day
    await refresh_earnings(store, av2, "6month", TODAY, NOW + H)
    days = [r[0] for r in store.conn.execute("SELECT day FROM earnings WHERE symbol='NVDA' ORDER BY day")]
    assert days == ["2026-08-26", "2026-11-19"]


@pytest.mark.parametrize("fail", ["json", "garbled", "short", "nokey"])
async def test_a_bad_calendar_keeps_the_previous_one(store, fail):
    _, av = av_with(("NVDA", date(2026, 11, 18), ""))
    await refresh_earnings(store, av, "6month", TODAY, NOW)
    f, av2 = av_with(("NVDA", date(2026, 10, 22), ""), filler=600 if fail != "short" else 10)
    f.fail = fail if fail in ("json", "garbled") else None
    r = await refresh_earnings(store, None if fail == "nokey" else av2, "6month", TODAY, NOW + H)
    assert not r.ok
    assert db.next_earnings(store, "NVDA", TODAY).day == date(2026, 11, 18)
    st = db.feed(store, FEED_EARNINGS)
    assert not st.ok and st.ok_ts == NOW and st.rows == 601   # last success kept on record
    if fail == "nokey":
        assert "ALPHAVANTAGE_API_KEY" in st.detail


async def test_a_stale_calendar_means_unknown(store):
    _, av = av_with(("NVDA", date(2026, 11, 18), ""))
    await refresh_earnings(store, av, "6month", TODAY, NOW)
    later = et(2026, 10, 24, 9, 1)                      # 72h and a minute on
    assert events(store).earnings_sessions("NVDA", later - 120) is not None
    assert events(store).earnings_sessions("NVDA", later) is None


# -- corporate actions ---------------------------------------------------------------------------
def scripted_actions():
    f = FakeAlpaca(NOW)
    f.add_action("cash_dividends", id="div", symbol="SPY", rate=1.888834, special=False, foreign=False,
                 ex_date="2026-09-18", payable_date="2026-10-30", process_date="2026-10-30")
    f.add_action("forward_splits", id="split", symbol="GLD", old_rate=1, new_rate=4,
                 ex_date="2026-11-05", process_date="2026-11-04")
    f.add_action("stock_mergers", id="merge", acquirer_symbol="BIGCO", acquiree_symbol="TLT", acquirer_rate=1.2,
                 acquiree_rate=1, effective_date="2026-11-02", process_date="2026-11-02")
    f.add_action("spin_offs", id="spin", source_symbol="BIGCO", new_symbol="SPUN", source_rate=1, new_rate=0.5,
                 ex_date="2026-11-09", process_date="2026-11-09")       # BIGCO isn't ours
    return f


async def test_corporate_actions_refresh_and_what_blocks_a_buy(store):
    f = scripted_actions()
    syms = ["SPY", "GLD", "TLT", "IEF"]
    r = await refresh_corporate_actions(store, alpaca(f), syms, TODAY, NOW, 60, 120)
    assert r.ok and r.rows == 3 and "3 new" in r.detail
    assert db.actions_for(store, "BIGCO") == []             # only the asked-about symbols are stored
    ev = events(store)
    assert ev.corp_action("SPY", NOW) is None               # a dividend owed: trades on
    assert ev.corp_action("GLD", NOW) is None               # a split: modeled
    assert ev.corp_action("TLT", NOW) == "stock merger effective 2026-11-02"
    assert ev.corp_action("IEF", NOW) is None               # checked, nothing pending
    assert ev.corp_action("QQQ", NOW) == "corporate actions unknown: never checked"
    assert "unknown: last checked 73h ago" in ev.corp_action("SPY", NOW + 73 * H)
    audit = store.audits("corporate_actions_changed")
    assert len(audit) == 1 and any("TLT stock merger" in x for x in audit[0]["new"])


async def test_a_cancelled_deal_is_withdrawn_and_stops_blocking(store):
    f = scripted_actions()
    await refresh_corporate_actions(store, alpaca(f), ["TLT"], TODAY, NOW, 60, 120)
    f.actions["stock_mergers"].clear()
    r = await refresh_corporate_actions(store, alpaca(f), ["TLT"], TODAY, NOW + H, 60, 120)
    assert "1 withdrawn" in r.detail
    assert events(store).corp_action("TLT", NOW + H) is None
    row = store.conn.execute("SELECT withdrawn_ts FROM corporate_actions WHERE id='merge'").fetchone()
    assert row[0] == NOW + H                              # kept, marked, not deleted


async def test_an_amended_action_is_updated_in_place(store):
    f = scripted_actions()
    await refresh_corporate_actions(store, alpaca(f), ["TLT"], TODAY, NOW, 60, 120)
    f.actions["stock_mergers"][0]["effective_date"] = "2026-11-09"
    f.actions["stock_mergers"][0]["process_date"] = "2026-11-09"
    r = await refresh_corporate_actions(store, alpaca(f), ["TLT"], TODAY, NOW + H, 60, 120)
    assert "1 changed" in r.detail
    assert events(store).corp_action("TLT", NOW + H) == "stock merger effective 2026-11-09"
    r = await refresh_corporate_actions(store, alpaca(f), ["TLT"], TODAY, NOW + 2 * H, 60, 120)
    assert "0 new, 0 changed, 0 withdrawn" in r.detail


async def test_a_merger_in_effect_but_not_yet_booked_still_blocks(store):
    f = scripted_actions()
    f.actions["stock_mergers"][0]["process_date"] = "2026-11-04"     # effective 11-02, booked 11-04
    await refresh_corporate_actions(store, alpaca(f), ["TLT"], TODAY, NOW, 60, 120)
    for day, blocked in ((3, True), (4, True), (5, False)):
        ts = et(2026, 11, day, 12)
        store.conn.execute("UPDATE corporate_action_checks SET ts=?", (ts,))   # as if just re-checked
        assert (events(store).corp_action("TLT", ts) is not None) is blocked, day


async def test_an_alpaca_failure_keeps_what_was_known(store):
    f = scripted_actions()
    await refresh_corporate_actions(store, alpaca(f), ["TLT"], TODAY, NOW, 60, 120)
    f.fail_429["/v1/corporate-actions"] = 99
    r = await refresh_corporate_actions(store, alpaca(f), ["TLT"], TODAY, NOW + H, 60, 120)
    assert not r.ok and "429" in r.detail
    assert events(store).corp_action("TLT", NOW + H) == "stock merger effective 2026-11-02"
    r = await refresh_corporate_actions(store, None, ["TLT"], TODAY, NOW + H, 60, 120)
    assert not r.ok and "Alpaca keys" in r.detail


# -- macro ----------------------------------------------------------------------------------------
async def test_macro_refresh_replaces_its_window_and_reads_back(store):
    f = FakeFred()
    days = [TODAY - timedelta(days=i) for i in range(45)]
    f.add("DGS10", {d: 4.0 + 0.01 * (TODAY - d).days * -1 for d in days if d.weekday() < 5})
    f.add("VIXCLS", {date(2026, 9, 1): 15.0})           # stopped publishing in September
    r = await refresh_macro(store, Fred(client=f.client(), backoff=0), {"DGS10": "10y", "VIXCLS": "VIX",
                                                                         "NOPE": "gone"}, TODAY, 400, NOW)
    assert not r.ok and "2/3 series" in r.detail and "NOPE" in r.detail
    read = {m.series: m for m in macro_readings(store, {"DGS10": "10y", "VIXCLS": "VIX", "NOPE": "gone"}, TODAY, 5)}
    assert read["DGS10"].value == pytest.approx(4.0) and read["DGS10"].day == TODAY and not read["DGS10"].stale
    assert read["DGS10"].change_1w == pytest.approx(0.07) and read["DGS10"].change_1m == pytest.approx(0.30)
    assert read["VIXCLS"].stale and read["VIXCLS"].day == date(2026, 9, 1)   # shown, flagged stale
    assert read["NOPE"].value is None and read["NOPE"].stale
    # FRED revises: a value withdrawn upstream disappears here too.
    f.add("DGS10", {TODAY: 4.5})
    await refresh_macro(store, Fred(client=f.client(), backoff=0), {"DGS10": "10y"}, TODAY, 400, NOW + H)
    assert [(o.day, o.value) for o in db.macro_series(store, "DGS10")] == [(TODAY, 4.5)]


async def test_fred_maintenance_stops_the_run_and_keeps_the_old_values(store):
    f = FakeFred()
    for sid in ("VIXCLS", "DGS2", "DGS10"):
        f.add(sid, {TODAY: 1.0})
    fred = Fred(client=f.client(), retries=1, backoff=0)
    await refresh_macro(store, fred, {"VIXCLS": "a", "DGS2": "b", "DGS10": "c"}, TODAY, 400, NOW)
    f.maintenance = 99
    r = await refresh_macro(store, fred, {"VIXCLS": "a", "DGS2": "b", "DGS10": "c"}, TODAY, 400, NOW + H)
    assert not r.ok and "0/3 series" in r.detail and "skipped DGS2, DGS10 (FRED unavailable)" in r.detail
    assert len(f.requests) == 3 + 2                     # one series tried (with its retry), not three
    assert [o.value for o in db.macro_series(store, "DGS10")] == [1.0]


# -- SEC ------------------------------------------------------------------------------------------
async def test_sec_lists_are_stored_whole_and_a_short_one_is_refused(store):
    sec = Sec("Jane Doe jane@example.com", client=standard_sec().client(), limiter=RateLimiter(1e6), backoff=0)
    r = await refresh_sec(store, sec, NOW, min_listings=5, min_funds=3)
    assert r.ok and db.sec_listing(store, "BRK.B").cik == 1067983 and "TQQQ" in db.sec_funds(store)
    r = await refresh_sec(store, sec, NOW + H, min_listings=5000, min_funds=3)
    assert not r.ok and "kept the previous lists" in r.detail and db.sec_listing(store, "AAPL") is not None
    assert "SEC_USER_AGENT" in (await refresh_sec(store, None, NOW)).detail


async def test_company_profiles_are_cached(store):
    f = standard_sec()
    sec = Sec("Jane Doe jane@example.com", client=f.client(), limiter=RateLimiter(1e6), backoff=0)
    a = await company_profile(store, sec, 320193, NOW)
    b = await company_profile(store, sec, 320193, NOW + 30 * 86400)
    assert a == b and len(f.requests) == 1
    await company_profile(store, sec, 320193, NOW + 91 * 86400)
    assert len(f.requests) == 2


# -- scheduling -----------------------------------------------------------------------------------
def test_when_a_feed_is_due(store):
    assert due(store, FEED_EARNINGS, NOW, 24 * H)                   # never run
    db.record_feed(store, FEED_EARNINGS, True, "ok", NOW)
    assert not due(store, FEED_EARNINGS, NOW + 23 * H, 24 * H)
    assert due(store, FEED_EARNINGS, NOW + 24 * H, 24 * H)
    db.record_feed(store, FEED_EARNINGS, False, "boom", NOW + 24 * H)
    wait = RETRY_SEC[FEED_EARNINGS]                                  # each try costs a daily call
    assert not due(store, FEED_EARNINGS, NOW + 24 * H + wait - 1, 24 * H)
    assert due(store, FEED_EARNINGS, NOW + 24 * H + wait, 24 * H)


async def test_refresh_due_runs_what_is_due_and_a_new_symbol_at_once(store, ucfg):
    fake_av, av = av_with(("NVDA", date(2026, 11, 18), ""))
    fake_fred = FakeFred()
    for sid in ucfg.events.macro:
        fake_fred.add(sid, {TODAY: 1.0})
    fa = scripted_actions()
    sources = Sources(fred=Fred(client=fake_fred.client(), backoff=0), alpha_vantage=av,
                      sec=Sec("Jane Doe jane@example.com", client=standard_sec().client(),
                              limiter=RateLimiter(1e6), backoff=0),
                      alpaca=alpaca(fa))
    first = await refresh_due(ucfg, store, sources, ["SPY", "TLT"], NOW)
    assert {r.feed: r.ok for r in first} == {FEED_MACRO: True, FEED_EARNINGS: True, FEED_CORPORATE: True,
                                             FEED_SEC: False}         # the fake lists are far too short
    assert await refresh_due(ucfg, store, sources, ["SPY", "TLT"], NOW + 60) == []
    again = await refresh_due(ucfg, store, sources, ["SPY", "TLT", "IEF"], NOW + 120)
    assert [r.feed for r in again] == [FEED_CORPORATE]                 # IEF was never checked
    assert len(fake_av.requests) == 1


async def test_a_failed_feed_waits_before_retrying(store, ucfg):
    fa = scripted_actions()
    fa.fail_429["/v1/corporate-actions"] = 99
    sources = Sources(alpaca=alpaca(fa))
    await refresh_due(ucfg, store, sources, ["SPY"], NOW)
    n = len(fa.requests)
    await refresh_due(ucfg, store, sources, ["SPY", "IEF"], NOW + 60)   # a new symbol doesn't bypass the wait
    assert len(fa.requests) == n
