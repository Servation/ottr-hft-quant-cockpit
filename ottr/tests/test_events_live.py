"""
Live smoke tests for the events feeds (`pytest -m live`): the real FRED, Alpha Vantage,
SEC and Alpaca corporate-actions endpoints still answer in the formats the parsers expect.

Each skips when its credential isn't available (ALPHAVANTAGE_API_KEY, SEC_USER_AGENT,
APCA_API_KEY_ID / APCA_API_SECRET_KEY, exported or in ottr/.env). FRED needs none, but
skips during its maintenance windows. The Alpha Vantage test spends one of the free key's
25 daily calls.
"""

import time
from datetime import timedelta

import pytest

from ottr.events.corporate import parse_actions
from ottr.events.earnings import AlphaVantage
from ottr.events.fred import Fred, FredError
from ottr.events.refresh import MIN_EARNINGS_ROWS, MIN_SEC_FUNDS, MIN_SEC_LISTINGS
from ottr.events.sec import Sec, operating_company
from ottr.ledger.book import trade_day
from ottr.market.alpaca import Alpaca, Credentials

pytestmark = pytest.mark.live


async def test_live_fred_series():
    today = trade_day(time.time())
    try:
        obs = await Fred().series("DGS10", today - timedelta(days=30))
    except FredError as e:
        if "web page" in str(e):
            pytest.skip(f"FRED is in maintenance: {e}")
        raise
    assert obs and 0 < obs[-1].value < 20 and (today - obs[-1].day).days <= 7


async def test_live_earnings_calendar_reaches_six_months():
    av = AlphaVantage.from_env()
    if av is None:
        pytest.skip("ALPHAVANTAGE_API_KEY not set")
    today = trade_day(time.time())
    dates = await av.earnings_calendar("6month")
    upcoming = [d for d in dates if d.day >= today]
    assert len(upcoming) >= MIN_EARNINGS_ROWS
    # The point of the 6-month horizon: dates well past 3 months out are included.
    assert max(d.day for d in upcoming) > today + timedelta(days=100)


async def test_live_sec_lists_and_classifier():
    sec = Sec.from_env()
    if sec is None:
        pytest.skip("SEC_USER_AGENT not set")
    listings, funds = await sec.listings(), await sec.fund_symbols()
    assert len(listings) >= MIN_SEC_LISTINGS and len(funds) >= MIN_SEC_FUNDS
    for sym, expected in (("AAPL", True), ("SPY", False), ("GLD", False), ("QQQ", False)):
        listing = listings.get(sym)
        co = await sec.company(listing.cik) if listing and sym not in funds else None
        ok, why = operating_company(sym, listing, funds, co)
        assert ok is expected, f"{sym}: {why}"


async def test_live_alpaca_corporate_actions():
    creds = Credentials.from_env()
    if creds is None:
        pytest.skip("no Alpaca keys in the environment")
    today = trade_day(time.time())
    payload = await Alpaca(creds).corporate_actions(["SPY", "TLT"], today - timedelta(days=120), today)
    actions, problems = parse_actions(payload)
    assert problems == []
    divs = [a for a in actions if a.kind == "cash_dividend"]
    assert {a.symbol for a in divs} == {"SPY", "TLT"}      # SPY pays quarterly, TLT monthly
    assert all(a.ex and a.pay and a.ex <= a.pay for a in divs)
