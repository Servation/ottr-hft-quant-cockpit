"""
Events feed clients and parsers: FRED's CSV (and its HTTP 200 maintenance page), Alpha
Vantage's earnings calendar (errors hidden in 200 responses, a key that must never leak),
the SEC lists and the operating-company test, and Alpaca's corporate actions (parsing,
classification, paging). Everything runs against fakes in tests/fake_feeds.py and
tests/fake_alpaca.py that serve the formats the real services sent on 2026-10-21.
"""

from datetime import date

import httpx
import pytest

from ottr.events.corporate import CorporateAction, parse_actions
from ottr.events.earnings import AlphaVantage, AlphaVantageError, parse_calendar
from ottr.events.fred import Fred, FredError, parse_fredgraph
from ottr.events.sec import Company, Listing, Sec, SecError, operating_company, user_agent_problem
from ottr.market import http
from ottr.market.alpaca import Alpaca
from ottr.market.http import RateLimiter
from tests.fake_alpaca import CREDS, FakeAlpaca
from tests.fake_feeds import AV_HEADER, AV_KEY, MAINTENANCE_PAGE, FakeAlphaVantage, FakeFred, FakeSec, standard_sec

UA = "Jane Doe jane@example.com"


# -- shared text classify --------------------------------------------------------------------
def test_a_web_page_in_place_of_csv_is_a_retryable_outage():
    resp = httpx.Response(200, text=MAINTENANCE_PAGE)
    assert http.classify_text(resp) == (None, "a web page instead of data (site maintenance?)", True)
    assert http.classify_text(httpx.Response(404, text="nope"))[2] is False
    assert http.classify_text(httpx.Response(200, text="a,b\n1,2\n")) == ("a,b\n1,2\n", None, False)


# -- FRED --------------------------------------------------------------------------------------
def test_fredgraph_parses_both_headers_and_skips_missing_days():
    text = "observation_date,DGS10\n2026-10-19,4.01\n2026-10-20,\n2026-10-21,.\n2026-10-22,4.05\n"
    assert parse_fredgraph(text, "DGS10") == [
        parse_fredgraph("DATE,DGS10\n2026-10-19,4.01\n", "DGS10")[0],
        parse_fredgraph("observation_date,DGS10\n2026-10-22,4.05\n", "DGS10")[0]]
    assert [o.value for o in parse_fredgraph(text, "DGS10")] == [4.01, 4.05]


@pytest.mark.parametrize("text,needle", [
    ("observation_date,DGS2\n2026-10-19,4.01\n", "unexpected header"),       # a different series
    ("observation_date,DGS10\n2026-10-19,abc\n", "bad value"),
    ("observation_date,DGS10\n19/10/2026,4.0\n", "bad date"),
    ("observation_date,DGS10\n2026-10-19,nan\n", "non-finite"),
    ("", "empty"),
])
def test_fredgraph_refuses_anything_else(text, needle):
    with pytest.raises(FredError, match=needle):
        parse_fredgraph(text, "DGS10")


async def test_fred_rides_out_its_maintenance_page():
    f = FakeFred()
    f.add("VIXCLS", {date(2026, 10, 20): 17.5, date(2026, 10, 21): 18.25})
    f.maintenance = 1
    obs = await Fred(client=f.client(), backoff=0).series("VIXCLS", date(2026, 10, 1))
    assert [(o.day, o.value) for o in obs] == [(date(2026, 10, 20), 17.5), (date(2026, 10, 21), 18.25)]
    assert len(f.requests) == 2 and f.requests[1].url.params["cosd"] == "2026-10-01"


async def test_fred_maintenance_that_outlasts_the_retries_is_a_transient_error():
    f = FakeFred()
    f.add("VIXCLS", {date(2026, 10, 21): 18.0})
    f.maintenance = 10
    with pytest.raises(FredError, match="web page") as e:
        await Fred(client=f.client(), retries=2, backoff=0).series("VIXCLS", date(2026, 10, 1))
    assert len(f.requests) == 3 and e.value.transient
    f.maintenance = 0
    with pytest.raises(FredError) as e:                    # a refusal is not an outage
        await Fred(client=f.client(), retries=2, backoff=0).series("RETIRED", date(2026, 10, 1))
    assert not e.value.transient


# -- Alpha Vantage -----------------------------------------------------------------------------
def test_earnings_calendar_parses_the_real_columns():
    text = "\r\n".join([
        AV_HEADER,
        'BWMX,"BETTERWARE DE MEXICO, SOCIEDAD ANONIMA",2026-10-22,2026-09-30,0.72,USD,post-market',
        "BRK.B,BERKSHIRE HATHAWAY INCORPORATED,2026-11-06,2026-09-30,5.91,USD,",
        "BCTX,BRIACELL THERAPEUTICS CORPORATION,2026-10-21,2026-07-31,,USD,",
        "ABT,ABBOTT LABORATORIES,2026-10-21,2026-09-30,1.43,USD,pre-market",
        "WEIRD-SYMBOL-X,ODD LISTING,2026-10-30,2026-09-30,1,USD,",  # not a US ticker: skipped
    ]) + "\r\n"
    got = {d.symbol: d for d in parse_calendar(text)}
    assert set(got) == {"BWMX", "BRK.B", "BCTX", "ABT"}
    assert got["BWMX"].time_of_day == "post-market" and got["ABT"].time_of_day == "pre-market"
    assert got["BRK.B"].day == date(2026, 11, 6) and got["BRK.B"].time_of_day == ""
    assert got["BCTX"].estimate is None and got["ABT"].estimate == 1.43
    assert got["ABT"].fiscal_end == date(2026, 9, 30)


def test_an_error_split_into_csv_cells_is_not_a_calendar():
    with pytest.raises(AlphaVantageError, match="no usable calendar rows"):
        parse_calendar(f"{AV_HEADER}\r\nI,n,f,o,r,m,a\r\n")
    with pytest.raises(AlphaVantageError, match="unexpected calendar columns"):
        parse_calendar("a,b\n1,2\n")


async def test_the_calendar_request_and_its_result():
    f = FakeAlphaVantage()
    f.add("NVDA", date(2026, 11, 18), tod="")
    got = await AlphaVantage(AV_KEY, client=f.client(), backoff=0).earnings_calendar("6month")
    assert [(d.symbol, d.day) for d in got] == [("NVDA", date(2026, 11, 18))]
    p = f.requests[0].url.params
    assert (p["function"], p["horizon"], p["apikey"]) == ("EARNINGS_CALENDAR", "6month", AV_KEY)


async def test_a_rate_limit_message_is_refused_once_and_never_shows_the_key():
    f = FakeAlphaVantage()
    f.fail = "json"
    av = AlphaVantage(AV_KEY, client=f.client(), backoff=0)
    with pytest.raises(AlphaVantageError) as e:
        await av.earnings_calendar()
    assert "25 requests per day" in str(e.value) and AV_KEY not in str(e.value)
    assert len(f.requests) == 1        # a retry would spend another of the 25 daily calls
    assert AV_KEY not in repr(av)


async def test_a_garbled_calendar_is_refused():
    f = FakeAlphaVantage()
    f.fail = "garbled"
    with pytest.raises(AlphaVantageError, match="daily limit, or a bad key"):
        await AlphaVantage(AV_KEY, client=f.client(), backoff=0).earnings_calendar()


async def test_alpha_vantage_key_comes_from_the_environment(monkeypatch):
    assert AlphaVantage.from_env() is None
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", " k1 ")
    assert AlphaVantage.from_env()._key == "k1"
    with pytest.raises(ValueError, match="horizon"):
        await AlphaVantage("k").earnings_calendar("1week")


# -- SEC ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("ua,problem", [
    ("", "not set"), ("ottr bot", "contact email"), ("jane@example.com", "name before"),
    ("Jane Doe jane@example.com", None), ("Acme Research admin@acme.co", None),
])
def test_the_sec_user_agent_must_name_someone_and_an_email(ua, problem):
    got = user_agent_problem(ua)
    assert (got is None) if problem is None else (problem in got)


def test_sec_client_needs_a_proper_user_agent(monkeypatch):
    with pytest.raises(ValueError):
        Sec("ottr")
    monkeypatch.setenv("SEC_USER_AGENT", "ottr")
    assert Sec.from_env() is None
    monkeypatch.setenv("SEC_USER_AGENT", UA)
    assert Sec.from_env().user_agent == UA


async def test_sec_lists_and_profiles():
    f = standard_sec()
    sec = Sec(UA, client=f.client(), limiter=RateLimiter(1e6), backoff=0)
    listings = await sec.listings()
    assert listings["BRK.B"] == Listing("BRK.B", 1067983, "BERKSHIRE HATHAWAY INC", "NYSE")
    assert "NONE." not in listings and "NONE" not in listings
    funds = await sec.fund_symbols()
    assert {"QQQ", "SGOV", "TLT", "TQQQ"} <= funds
    co = await sec.company(320193)
    assert (co.entity_type, co.sic, co.tickers) == ("operating", "3571", ("AAPL",))
    assert all(r.headers["user-agent"] == UA for r in f.requests)
    assert f.requests[-1].url.path == "/submissions/CIK0000320193.json"


async def test_sec_403_says_to_set_the_user_agent_and_is_not_retried():
    f = FakeSec()
    sec = Sec(UA, client=f.client(), limiter=RateLimiter(1e6), backoff=0)
    sec.user_agent = "no contact"      # as if the SEC disliked it
    with pytest.raises(SecError, match="SEC_USER_AGENT"):
        await sec.listings()
    assert len(f.requests) == 1


async def test_an_empty_sec_list_is_refused():
    f = FakeSec()
    sec = Sec(UA, client=f.client(), limiter=RateLimiter(1e6), backoff=0)
    with pytest.raises(SecError, match="no usable rows"):
        await sec.listings()


def _co(cik, entity="operating", sic="3571", desc="Electronic Computers"):
    return Company(cik, "X", entity, sic, desc, (), ())


@pytest.mark.parametrize("sym,listing,company,ok,why", [
    ("AAPL", Listing("AAPL", 1, "Apple", "Nasdaq"), _co(1), True, "Electronic Computers"),
    ("SPY", Listing("SPY", 2, "SPDR S&P 500 ETF TRUST", "NYSE"), _co(2, "other", ""), False, "entity type other"),
    ("GLD", Listing("GLD", 3, "SPDR GOLD TRUST", "NYSE"), _co(3, sic="6221"), False, "commodity trust"),
    ("SPAC", Listing("SPAC", 4, "X ACQUISITION", "Nasdaq"), _co(4, sic="6770"), False, "SPAC"),
    ("QQQ", None, None, False, "fund series"),
    ("PINK", Listing("PINK", 5, "OTC CO", "OTC"), _co(5), False, "listed on OTC"),
    ("NEWCO", Listing("NEWCO", 6, "New", "NYSE"), None, False, "no SEC company profile"),
    ("ODD", Listing("ODD", 7, "Odd", "NYSE"), _co(8), False, "CIK 8"),
    ("GONE", None, None, False, "not in the SEC"),
])
def test_operating_company_test(sym, listing, company, ok, why):
    got_ok, got_why = operating_company(sym, listing, {"QQQ"}, company)
    assert got_ok is ok and why in got_why


# -- corporate actions ---------------------------------------------------------------------------
# Shapes copied from Alpaca's live answers on 2026-10-21 (ids shortened).
PAYLOAD = {
    "cash_dividends": [{"id": "d1", "symbol": "SPY", "cusip": "78462F103", "rate": 1.888834, "special": False,
                        "foreign": False, "ex_date": "2026-09-18", "record_date": "2026-09-18",
                        "payable_date": "2026-10-30", "process_date": "2026-10-30"}],
    "forward_splits": [{"id": "f1", "symbol": "MTUM", "cusip": "46432F396", "new_rate": 4, "old_rate": 1,
                        "ex_date": "2026-11-05", "record_date": "2026-11-03", "payable_date": "2026-11-04",
                        "process_date": "2026-11-04"}],
    "reverse_splits": [{"id": "r1", "symbol": "AXNX", "old_cusip": "G6146G117", "new_cusip": "G6146G125",
                        "new_rate": 1, "old_rate": 2.5, "ex_date": "2026-10-22", "process_date": "2026-10-22"}],
    "spin_offs": [{"id": "s1", "source_symbol": "MSGS", "source_cusip": "x", "source_rate": 1, "new_symbol": "MSGR.WI",
                   "new_cusip": "y", "new_rate": 0.5, "ex_date": "2026-10-27", "record_date": "2026-10-20",
                   "payable_date": "2026-10-26", "process_date": "2026-10-27"}],
    "cash_mergers": [{"id": "c1", "acquiree_symbol": "BZH", "acquiree_cusip": "07556Q881", "rate": 33.5,
                      "effective_date": "2026-10-20", "payable_date": "2026-10-21", "process_date": "2026-10-21"}],
    "stock_mergers": [{"id": "m1", "acquirer_symbol": "SAFX", "acquirer_cusip": "a", "acquirer_rate": 1.1919,
                       "acquiree_symbol": "DEVSF", "acquiree_cusip": "b", "acquiree_rate": 1,
                       "effective_date": "2026-10-16", "payable_date": "2026-10-21", "process_date": "2026-10-21"}],
    "name_changes": [{"id": "n1", "old_symbol": "OLDN", "old_cusip": "a", "new_symbol": "NEWN", "new_cusip": "b",
                      "process_date": "2026-11-02"}],
    "special_payouts": [{"id": "u1", "symbol": "QQQ", "process_date": "2026-11-10"}],       # a type added later
    "worthless_removals": [{"id": "w1", "symbol": "ABCDEFGH", "process_date": "2026-10-21"},  # not a US ticker
                           {"symbol": "XYZ", "process_date": "2026-10-21"}],                 # no id
}


def test_actions_name_the_affected_symbol_and_the_day_it_takes_effect():
    actions, problems = parse_actions(PAYLOAD)
    by = {a.id: a for a in actions}
    assert set(by) == {"d1", "f1", "r1", "s1", "c1", "m1", "n1", "u1"}
    assert (by["d1"].symbol, by["d1"].effective, by["d1"].pay) == ("SPY", date(2026, 9, 18), date(2026, 10, 30))
    assert (by["s1"].symbol, by["s1"].effective) == ("MSGS", date(2026, 10, 27))
    assert (by["c1"].symbol, by["c1"].effective) == ("BZH", date(2026, 10, 20))
    assert by["m1"].symbol == "DEVSF"                 # the target's holders are affected, not the acquirer's
    assert (by["n1"].symbol, by["n1"].effective) == ("OLDN", date(2026, 11, 2))
    assert (by["u1"].kind, by["u1"].symbol) == ("special_payout", "QQQ")
    assert any("special_payouts" in p for p in problems) and any("no id" in p for p in problems)
    assert by["d1"].detail["rate"] == 1.888834


def test_what_is_modeled_and_what_blocks_a_buy():
    actions, _ = parse_actions(PAYLOAD)
    by = {a.id: a for a in actions}
    assert [k for k in ("d1", "f1", "r1") if by[k].blocks_buy] == []           # income and splits trade on
    assert all(by[k].modeled for k in ("d1", "f1", "r1", "c1"))
    assert by["c1"].blocks_buy                                                  # price pinned to the deal
    for k in ("s1", "m1", "n1", "u1"):                                          # not modeled: block
        assert by[k].blocks_buy and not by[k].modeled


def test_pending_until_in_effect_and_booked():
    a = CorporateAction("x", "stock_merger", "XYZ", effective=date(2026, 10, 20), process=date(2026, 10, 23))
    assert a.pending(date(2026, 10, 19)) and a.pending(date(2026, 10, 22))     # effective, not yet booked
    assert not a.pending(date(2026, 10, 24))
    assert CorporateAction("y", "stock_merger", "XYZ", effective=None, process=None).pending(date(2030, 1, 1))
    assert "unknown date" in CorporateAction("y", "spin_off", "XYZ", None, None).describe()


async def test_alpaca_corporate_actions_follow_pages_and_dedupe_across_chunks(monkeypatch):
    from ottr.market import alpaca as alp
    f = FakeAlpaca(0.0)
    for i in range(5):
        f.add_action("cash_dividends", symbol="SPY", rate=0.1 * (i + 1), ex_date=f"2026-0{i + 1}-15",
                     process_date=f"2026-0{i + 1}-20")
    f.add_action("stock_mergers", acquirer_symbol="SPY", acquiree_symbol="TLT", effective_date="2026-03-01",
                 process_date="2026-03-02")
    f.add_action("cash_dividends", symbol="SPY", rate=9, ex_date="2025-12-01", process_date="2025-12-05")  # too old
    monkeypatch.setattr(alp, "CORP_ACTIONS_PAGE_LIMIT", 2)
    monkeypatch.setattr(alp, "SNAPSHOT_CHUNK", 1)          # SPY and TLT in separate requests
    a = Alpaca(CREDS, client=f.client(), limiter=RateLimiter(1e6), backoff=0)
    got = await a.corporate_actions(["SPY", "TLT"], date(2026, 1, 1), date(2026, 12, 31))
    assert len(got["cash_dividends"]) == 5 and len(got["stock_mergers"]) == 1   # the merger came twice
    reqs = [r for r in f.requests if r.url.path == "/v1/corporate-actions"]
    assert {r.url.params["symbols"] for r in reqs} == {"SPY", "TLT"} and len(reqs) == 4
    assert reqs[0].url.params["start"] == "2026-01-01" and reqs[0].url.params["end"] == "2026-12-31"
