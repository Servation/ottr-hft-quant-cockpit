"""
The events half of `ottr data --check`: what it reports and its exit code with every feed
healthy, with no Alpha Vantage key or SEC contact, with corporate actions unavailable, and
with a pending action that blocks BUYs. Fakes only; the live run is in test_events_live.
"""

import dataclasses
from datetime import date, datetime

from ottr import cli
from ottr.config import load_us
from ottr.events import check
from ottr.events.earnings import AlphaVantage
from ottr.events.fred import Fred
from ottr.events.refresh import Sources
from ottr.events.sec import Sec
from ottr.market.alpaca import Alpaca
from ottr.market.calendar import ET
from ottr.market.http import RateLimiter
from tests.fake_alpaca import CREDS, FakeAlpaca
from tests.fake_feeds import AV_KEY, FakeAlphaVantage, FakeFred, standard_sec

NOW = datetime(2026, 10, 21, 18, 0, tzinfo=ET).timestamp()
TODAY = date(2026, 10, 21)
SYMS = ["SPY", "TLT", "GLD"]


def rig(tmp_path, av=True, sec=True, alpaca_ok=True):
    cfg = load_us(data_dir=tmp_path).events
    cfg = dataclasses.replace(cfg, macro={"VIXCLS": "VIX", "DGS10": "10y"})
    fred = FakeFred()
    fred.add("VIXCLS", {date(2026, 10, 20): 17.5})
    fred.add("DGS10", {date(2026, 10, 9): 4.1})                 # 12 days old: stale
    fav = FakeAlphaVantage()
    fav.add("AAPL", date(2026, 11, 2))
    fav.add("NVDA", date(2026, 11, 18), tod="")
    fav.many(600, date(2027, 1, 15))
    s = standard_sec()
    fa = FakeAlpaca(NOW)
    fa.add_action("cash_dividends", symbol="SPY", rate=1.888834, ex_date="2026-09-18", payable_date="2026-10-30",
                  process_date="2026-10-30")
    fa.add_action("stock_mergers", acquirer_symbol="BIGCO", acquiree_symbol="TLT", effective_date="2026-11-02",
                  process_date="2026-11-02")
    if not alpaca_ok:
        fa.fail_429["/v1/corporate-actions"] = 99
    sources = Sources(fred=Fred(client=fred.client(), backoff=0),
                      alpha_vantage=AlphaVantage(AV_KEY, client=fav.client(), backoff=0) if av else None,
                      sec=Sec("Jane Doe jane@example.com", client=s.client(), limiter=RateLimiter(1e6), backoff=0)
                      if sec else None,
                      alpaca=Alpaca(CREDS, client=fa.client(), limiter=RateLimiter(1e6), backoff=0))
    return sources, cfg


async def run(sources, cfg):
    lines = []
    code = await check.run(sources, cfg, SYMS, NOW, out=lines.append)
    return code, "\n".join(lines)


async def test_every_feed_answers(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "MIN_SEC_LISTINGS", 5)
    monkeypatch.setattr(check, "MIN_SEC_FUNDS", 3)
    code, text = await run(*rig(tmp_path))
    assert "ok    macro VIXCLS = 17.5 on 2026-10-20 (VIX)" in text
    assert "WARN  macro DGS10 = 4.1 on 2026-10-09" in text            # stale, so degraded
    assert "ok    earnings: 602 report dates, 2026-11-02 to 2027-01-15 (6month)" in text
    assert "next reports: AAPL 2026-11-02 post-market, NVDA 2026-11-18 time n/a" in text
    assert "ok    SEC AAPL: an operating company" in text
    assert "ok    SEC SPY: not a pick: SEC entity type other" in text
    assert "ok    SEC GLD: not a pick: commodity trust" in text
    assert "ok    SEC QQQ: not a pick: a fund series" in text
    assert "1 cash dividend, 1 stock merger" in text
    assert "TLT: stock merger effective 2026-11-02: BUYs blocked until it is done" in text
    assert "SPY: dividend 1.888834/share, ex 2026-09-18, paid 2026-10-30" in text
    assert code == 1 and "Events feeds usable, with problems above (exit 1)" in text
    assert AV_KEY not in text


async def test_missing_keys_say_what_they_cost_and_how_to_fix(tmp_path):
    code, text = await run(*rig(tmp_path, av=False, sec=False))
    assert "ALPHAVANTAGE_API_KEY is not set" in text and "single-stock BUYs are blocked" in text
    assert "alphavantage.co/support/#api-key" in text
    assert "SEC: SEC_USER_AGENT is not set" in text
    assert code == 1


async def test_fred_down_is_reported_once_not_waited_out_per_series(tmp_path):
    sources, cfg = rig(tmp_path)
    fred = FakeFred()
    fred.maintenance = 99
    sources.fred = Fred(client=fred.client(), retries=1, backoff=0)
    code, text = await run(sources, cfg)
    assert "WARN  macro VIXCLS: FRED VIXCLS failed: a web page instead of data" in text
    assert "skipped DGS10: FRED is unavailable; try again later" in text and "macro DGS10:" not in text
    assert len(fred.requests) == 2 and code == 1


async def test_counts_of_one_kind_read_as_plurals(tmp_path):
    sources, cfg = rig(tmp_path)
    fa = FakeAlpaca(NOW)
    for m in ("07", "09", "10"):
        fa.add_action("cash_dividends", symbol="TLT", rate=0.3, ex_date=f"2026-{m}-01", payable_date=f"2026-{m}-05",
                      process_date=f"2026-{m}-05")
    sources.alpaca = Alpaca(CREDS, client=fa.client(), limiter=RateLimiter(1e6), backoff=0)
    _, text = await run(sources, cfg)
    assert "by process date: 2 cash dividends" in text          # July's is outside the 60-day window


async def test_corporate_actions_down_is_a_failure(tmp_path):
    code, text = await run(*rig(tmp_path, alpaca_ok=False))
    assert "FAIL  corporate actions" in text and "every BUY would be blocked" in text
    assert code == 2 and "Events feeds NOT usable" in text


def test_cli_data_check_still_needs_alpaca_keys_first(capsys):
    assert cli.main(["data", "--check"]) == 3
    out = capsys.readouterr().out
    assert "APCA_API_KEY_ID" in out and "events" not in out
