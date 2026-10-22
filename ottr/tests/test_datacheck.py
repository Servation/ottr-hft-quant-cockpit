"""
`ottr data --check` tests: the report and exit code for a healthy feed, a closed market,
a live key, a gap in history, a clock/calendar disagreement, bad keys, and no keys.
"""

from datetime import date, datetime

import pytest

from ottr import cli
from ottr.clock import FakeClock
from ottr.market import datacheck
from ottr.market.alpaca import Alpaca, Credentials
from ottr.market.calendar import ET
from ottr.market.http import RateLimiter
from tests.fake_alpaca import CREDS, FakeAlpaca

SYMS = ["SPY", "SGOV", "BRK.B"]


def et(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=ET).timestamp()


def healthy(now: float, skip_day: date = None) -> FakeAlpaca:
    f = FakeAlpaca(now)
    today = datetime.fromtimestamp(now, tz=ET).date()
    for i, s in enumerate(SYMS):
        f.add_asset(s, fractionable=(s != "BRK.B"))
        f.add_quote(s, 100 + i, 100.02 + i, age=2)
        f.add_daily_bars(s, {x.day: 100.0 + i for x in f.sessions
                             if x.day < today and x.day != skip_day})
    return f


async def run(fake, creds=CREDS, now=None):
    lines = []
    a = Alpaca(creds, client=fake.client(), limiter=RateLimiter(1e6), backoff=0, clock=FakeClock(fake.now))
    code = await datacheck.run(a, SYMS, now or fake.now, out=lines.append)
    return code, "\n".join(lines)


async def test_a_healthy_feed_during_the_session():
    code, text = await run(healthy(et(2026, 10, 21, 11, 0)))
    assert code == 0, text
    assert "market OPEN" in text and "calendar agrees" in text
    assert "BRK.B" in text and "whole shares only" in text and "IEX 100.00 x 100.02" in text
    assert "Alpaca data OK" in text


async def test_closed_market_quotes_are_informational():
    f = healthy(et(2026, 10, 24, 12, 0))  # Saturday
    for s in SYMS:
        f.add_quote(s, 100, 100.02, age=86400)
    code, text = await run(f)
    assert code == 0, text
    assert "quotes: market closed, so not judged (3 stale)" in text
    assert "SPY: stale" not in text  # one summary line, not one line per symbol


async def test_stale_quotes_during_the_session_degrade():
    f = healthy(et(2026, 10, 21, 11, 0))
    f.add_quote("SGOV", 100, 100.02, age=900)
    code, text = await run(f)
    assert code == 1 and "SGOV: no usable quote (stale" in text


async def test_a_missing_session_in_history_is_flagged():
    code, text = await run(healthy(et(2026, 10, 21, 11, 0), skip_day=date(2026, 10, 14)))
    assert code == 1 and "1 missing (2026-10-14)" in text


async def test_a_live_key_is_a_warning():
    code, text = await run(healthy(et(2026, 10, 21, 11, 0)), creds=Credentials("AKLIVE", CREDS.secret))
    assert "LIVE account key" in text  # (the fake then refuses it: wrong key, exit 2)


async def test_clock_and_calendar_disagreeing_fails():
    f = healthy(et(2026, 10, 24, 11, 0))  # Saturday: the calendar says closed...
    f.clock_says_open = True               # ...but the clock says open
    code, text = await run(f)
    assert code == 2 and "the desk would treat the market as closed" in text


async def test_bad_keys_stop_the_check():
    code, text = await run(healthy(et(2026, 10, 21, 11, 0)), creds=Credentials("PKWRONG", "x"))
    assert code == 2 and "rejected the keys" in text


def test_long_fund_names_are_trimmed_at_a_word():
    name = "iShares iBoxx $ Investment Grade Corporate Bond ETF"
    assert datacheck._short(name) == "iShares iBoxx $ Investment Grade..."
    assert datacheck._short("SPDR Gold Trust") == "SPDR Gold Trust"


def test_cli_without_keys_says_how_to_add_them(capsys):
    assert cli.main(["data", "--check"]) == 3
    assert "APCA_API_KEY_ID" in capsys.readouterr().out


def test_cli_rejects_a_bad_symbol(monkeypatch, capsys):
    monkeypatch.setenv("APCA_API_KEY_ID", "PKX")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "y")
    assert cli.main(["data", "--check", "--symbols", "SPY,NOT A TICKER"]) == 2


@pytest.mark.live
async def test_live_alpaca_feed():
    import time
    creds = Credentials.from_env()
    if creds is None:
        pytest.skip("no Alpaca keys in the environment")
    lines = []
    code = await datacheck.run(Alpaca(creds), ["SPY", "SGOV", "BRK.B"], time.time(), out=lines.append)
    assert code in (0, 1), "\n".join(lines)
