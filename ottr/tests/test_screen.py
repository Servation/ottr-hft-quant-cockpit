"""
The stock screener (ottr/universe): the measures and gates, the ranking, the membership
rules with their hysteresis, and a full run against FakeAlpaca and a fake SEC through the
engine: satellites promoted, tracked and traded as stocks, demoted ones sold off and
retired, the digest in Discord, the stored screen behind /api/screen and /screen, the
`ottr screen` report and the weekly schedule's health.
"""

import asyncio
import dataclasses
import math
import random
from datetime import date, timedelta

import aiohttp
import pytest

from ottr import health
from ottr.api import ApiServer
from ottr.chat import render
from ottr.cli import screen_report
from ottr.config import Alerts as Limits
from ottr.engine.engine import Engine
from ottr.events import db
from ottr.events.earnings import EarningsDate
from ottr.events.refresh import Sources
from ottr.events.sec import Listing, Sec
from ottr.events.view import FEED_EARNINGS
from ottr.ledger.book import trade_day
from ottr.market.http import RateLimiter
from ottr.notify import Alerts, format_event
from ottr.universe import last_slot
from ottr.universe import db as udb
from ottr.universe.db import Satellite
from ottr.universe.run import ScreenError
from ottr.market import quality
from ottr.universe.screen import Row, measure, percentiles, score
from ottr.universe.select import decide
from tests.fake_feeds import FakeSec
from tests.fakes import et, market_rig, no_corporate_actions
from tests.nyse import sessions_2026

SAT = et(2026, 10, 24, 8)                 # the weekly slot: Saturday 08:00 New York
FRIDAY = date(2026, 10, 23)


def trading_days(last: date, n: int):
    """n trading days ending at `last`: the 2026 calendar, then weekdays before it."""
    out = [s.day for s in sessions_2026() if s.day <= last]
    d = out[0] - timedelta(days=1)
    while len(out) < n:
        if d.weekday() < 5:
            out.insert(0, d)
        d -= timedelta(days=1)
    return out[-n:]


def walk(days, start, drift, vol, seed):
    """A seeded random walk: `drift` and `vol` a year."""
    rng, p, out = random.Random(seed), start, {}
    for d in days:
        out[d] = round(p, 4)
        p *= math.exp(drift / 252 + vol / math.sqrt(252) * rng.gauss(0, 1))
    return out


DAYS = trading_days(FRIDAY, 300)


# -- measuring ------------------------------------------------------------------------------
def daily(closes, volume=2_000_000):
    return [(d, c, volume) for d, c in sorted(closes.items())]


def spy_rows():
    return sorted(walk(DAYS, 500, 0.10, 0.15, 1).items())


def recent():
    return DAYS[-60:]


def test_a_steady_uptrend_passes_and_its_measures_add_up(cfg):
    r = measure(cfg, "UP", daily(walk(DAYS, 50, 0.50, 0.20, 7)), spy_rows(), recent(), 20)
    assert r.passed and r.sessions == 300 and r.price > 10 and r.dollar_volume > 50e6
    assert 0.08 < r.vol < 0.40 and r.momentum > 0 and r.momentum_return > 0 and r.near_high <= 1
    assert r.sizable == pytest.approx(min(0.015 / max(r.vol, 0.15) * (1 if r.regime == "TRENDING" else 0.25), 0.08))
    assert r.entry_problem() == ""


@pytest.mark.parametrize("setup, gate", [
    (lambda c: dict(list(c.items())[-200:]), "200 sessions of history (needs 273)"),
    (lambda c: {d: v for d, v in c.items() if d != DAYS[-10]}, "no bar for 1 of the last 60 sessions"),
    (lambda c: {d: v / 10 for d, v in c.items()}, "price $"),
    (lambda c: {d: v for d, v in c.items()}, "a day traded"),
])
def test_hard_gates(cfg, setup, gate):
    closes = setup(walk(DAYS, 50, 0.50, 0.20, 7))
    volume = 100_000 if gate == "a day traded" else 2_000_000
    r = measure(cfg, "X", daily(closes, volume), spy_rows(), recent(), 20)
    assert gate in r.gate


def test_a_downtrend_is_under_its_average_and_a_wild_stock_outside_the_vol_band(cfg):
    down = measure(cfg, "DN", daily(walk(DAYS, 80, -0.40, 0.20, 3)), spy_rows(), recent(), 20)
    assert "under its 200-session average" in down.gate
    wild = measure(cfg, "WILD", daily(walk(DAYS, 50, 0.9, 1.2, 4)), spy_rows(), recent(), 20)
    assert "outside 8-70%" in wild.gate


def test_entry_checks_are_not_gates(cfg):
    r = measure(cfg, "UP", daily(walk(DAYS, 50, 0.50, 0.20, 7)), spy_rows(), recent(), 3)
    assert r.passed and r.entry_problem() == "reports in 3 sessions"
    assert dataclasses.replace(r, earnings_sessions=None).entry_problem() == "no known report date"
    assert dataclasses.replace(r, flags=("overextended",)).entry_problem() == "overextended"


def test_a_spike_is_overextended_and_a_drop_is_pulling_back(cfg):
    closes = walk(DAYS, 50, 0.40, 0.20, 7)
    spiked = dict(closes)
    for d in DAYS[-3:]:
        spiked[d] = spiked[d] * 1.25
    assert "overextended" in measure(cfg, "S", daily(spiked), spy_rows(), recent(), 20).flags
    dropped = dict(closes)
    for i, d in enumerate(DAYS[-6:]):
        dropped[d] = dropped[d] * (1 - 0.04 * (i + 1))      # a slide of 24% over six sessions
    assert "pulling back" in measure(cfg, "D", daily(dropped), spy_rows(), recent(), 20).flags


def test_percentiles_share_ties_and_rank_unknowns_last():
    assert percentiles([3.0, 1.0, None, 2.0, 2.0]) == [1.0, 0.25, 0.0, 0.625, 0.625]
    assert percentiles([5.0]) == [0.5]


def test_the_score_ranks_the_passers_and_flags_halve_it():
    rows = [Row("A", momentum=3, residual=2, efficiency=0.5, near_high=0.99, sizable=0.08, corr_spy=0.2,
                participation=1.2),
            Row("B", momentum=1, residual=0.5, efficiency=0.3, near_high=0.9, sizable=0.05, corr_spy=0.6,
                participation=1.0),
            Row("C", momentum=3, residual=2, efficiency=0.5, near_high=0.99, sizable=0.08, corr_spy=0.2,
                participation=1.2, flags=("overextended",)),
            Row("GATED", momentum=9, gate="below")]
    ranked = score(rows)
    assert [(r.symbol, r.rank) for r in ranked] == [("A", 1), ("C", 2), ("B", 3)]
    assert ranked[1].score == pytest.approx(ranked[0].score / 2)


# -- the rules -----------------------------------------------------------------------------
TODAY = date(2026, 10, 24)


def sat(symbol, days_ago=60, state="active", demoted=None):
    return Satellite(symbol, state, 0.0, TODAY - timedelta(days=days_ago), None,
                     TODAY - timedelta(days=demoted) if demoted is not None else None, 50.0, "test")


def ranked_rows(*symbols, earnings=20):
    rows = [Row(s, momentum=1.0, earnings_sessions=earnings) for s in symbols]
    return [dataclasses.replace(r, score=round(100 - 5 * i, 1), rank=i + 1) for i, r in enumerate(rows)]


def run_decide(cfg, ranked, current=(), cooling=(), corr=None, unfit=None, core=("SPY",)):
    measured = {r.symbol: r for r in ranked}
    return decide(cfg, ranked, measured, {s.symbol: s for s in current}, {s.symbol: s for s in cooling}, TODAY,
                  corr or (lambda a, b: 0.1), list(core), unfit or {})


def test_empty_slots_fill_with_the_best_candidates_free_of_the_change_limit(cfg):
    d = run_decide(cfg, ranked_rows(*"ABCDEFGH"))
    assert [s for s, _ in d.promote] == list("ABCDEF") and d.changes == 0
    assert d.passed[0] == ("G", "no slot: every satellite is in its first weeks")


def test_a_satellite_is_kept_in_its_tenure_and_while_it_ranks_in_the_top_12(cfg):
    rows = ranked_rows(*"ABCDEFGHIJKLMN")
    patient = dataclasses.replace(cfg, screen=dataclasses.replace(cfg.screen, challenger_margin=100))
    d = run_decide(patient, rows, current=[sat("N", days_ago=5), sat("L"), sat("C")])
    keep = dict(d.keep)
    assert keep["N"] == "#14, score 35; in its first 14 days" and keep["L"] == "#12, score 45" and "C" in keep
    assert d.demote == [] and [s for s, _ in d.promote] == ["A", "B", "D"]
    assert d.passed[0] == ("E", "beats the weakest satellite L by 35 points (needs 100)")
    # Without the patience, a strong enough challenger takes the weakest tenured place.
    d = run_decide(cfg, rows, current=[sat("N", days_ago=5), sat("L"), sat("C")])
    assert dict(d.demote)["L"].startswith("replaced by E") and d.changes == 1


def test_rank_outs_are_limited_to_two_a_week_worst_first(cfg):
    rows = ranked_rows(*[f"S{i:02d}" for i in range(20)])
    current = [sat(s) for s in ("S19", "S18", "S17", "S01")]
    d = run_decide(cfg, rows, current=current)
    assert [s for s, _ in d.demote] == ["S19", "S18"] and d.changes == 2
    assert "this week's changes are used" in dict(d.keep)["S17"]
    assert [s for s, _ in d.promote] == ["S00", "S02", "S03", "S04"]     # the freed slots refill


def test_a_hard_gate_or_unfitness_demotes_at_once_without_using_a_change(cfg):
    rows = ranked_rows(*"ABCDEF")
    measured_gate = dataclasses.replace(Row("G"), gate="at or under its 200-session average (1 vs 2)")
    d = decide(cfg, rows, {**{r.symbol: r for r in rows}, "G": measured_gate},
               {"G": sat("G"), "H": sat("H", days_ago=3)}, {}, TODAY, lambda a, b: 0.1, ["SPY"],
               {"H": "not tradable at Alpaca"})
    assert dict(d.demote) == {"G": "hard gate: at or under its 200-session average (1 vs 2)",
                              "H": "hard gate: not tradable at Alpaca"}
    assert d.changes == 0 and len(d.promote) == 6


def test_a_challenger_needs_the_margin_and_a_free_change(cfg):
    current = [sat(s) for s in "BCDEFG"]
    rows = ranked_rows("A", "B", "C", "D", "E", "F", "G")       # A 100 vs the weakest G 70
    d = run_decide(cfg, rows, current=current)
    assert ("A", "ranks #1, score 100: replaces G") in d.promote
    assert dict(d.demote)["G"] == "replaced by A (#1, score 100 vs #7, score 70)" and d.changes == 1
    close = [dataclasses.replace(r, score=75.0 if r.symbol == "A" else r.score) for r in rows]
    d = run_decide(cfg, close, current=current)
    assert d.promote == [] and dict(d.passed)["A"] == "beats the weakest satellite G by 5 points (needs 15)"


def test_cooldown_correlation_and_entry_checks_pass_a_candidate_over(cfg):
    rows = ranked_rows("COOL", "TWIN", "SOON", "OK") + []
    rows[2] = dataclasses.replace(rows[2], earnings_sessions=2)
    corr = lambda a, b: 0.9 if {a, b} == {"TWIN", "SPY"} else 0.1
    d = run_decide(cfg, rows, cooling=[sat("COOL", state="removed", demoted=10)], corr=corr)
    assert dict(d.passed) == {"COOL": f"cooling off since its demotion on {TODAY - timedelta(days=10)}",
                              "TWIN": "moves with SPY (correlation 0.90)", "SOON": "reports in 2 sessions"}
    assert [s for s, _ in d.promote] == ["OK"]


# -- a full run through the engine ------------------------------------------------------------
STOCKS = {   # symbol: (start price, drift, vol, seed)
    "AAPL": (150, 0.55, 0.22, 11), "MSFT": (300, 0.45, 0.20, 12), "NVDA": (90, 0.80, 0.35, 13),
    "AMZN": (120, 0.40, 0.25, 14), "META": (280, 0.60, 0.28, 15), "AVGO": (900, 0.50, 0.25, 16),
    "COST": (600, 0.35, 0.18, 17), "LLY": (500, 0.42, 0.22, 18), "SOON": (60, 0.70, 0.22, 19),
    "DOWN": (80, -0.45, 0.22, 20), "NEWCO": (40, 0.6, 0.3, 21), "THIN": (40, 0.5, 0.2, 22),
    "PENY": (6, 0.5, 0.2, 23), "ETFX": (40, 0.5, 0.2, 24), "FUNDY": (40, 0.5, 0.2, 25), "TRST": (40, 0.5, 0.2, 26),
}


class Recorder:
    def __init__(self):
        self.posts = []

    async def __call__(self, text):
        self.posts.append(text)
        return True


async def screen_rig(cfg, store, now=SAT):
    no_corporate_actions(store, cfg.core, now)
    rig = market_rig(cfg, store, now=now)
    f = rig.fake
    spy = walk(DAYS, 500, 0.12, 0.15, 1)
    f.add_daily_bars("SPY", spy)
    sec, listings = FakeSec(), {}
    for i, (sym, (p0, drift, vol, seed)) in enumerate(STOCKS.items()):
        closes = walk(DAYS[-100:] if sym == "NEWCO" else DAYS, p0, drift, vol, seed)
        f.add_daily_bars(sym, closes, volume=20_000 if sym == "THIN" else 3_000_000)
        f.add_asset(sym, exchange="NASDAQ", name="Leveraged 2X ETF" if sym == "ETFX" else f"{sym} Inc.")
        cik = 1000 + i
        listings[sym] = Listing(sym, cik, f"{sym} INC", "Nasdaq")
        sec.add_company(sym, cik, f"{sym} INC", sic="6221" if sym == "TRST" else "3571")
    twin = {d: round(c * (1 + 0.0005 * ((i % 3) - 1)), 4) for i, (d, c) in
            enumerate(walk(DAYS, 150, 0.55, 0.22, 11).items())}     # AAPL plus a hair of noise
    f.add_daily_bars("TWIN", twin, volume=3_000_000)
    f.add_asset("TWIN", exchange="NYSE", name="Twin Corp")
    listings["TWIN"] = Listing("TWIN", 2000, "TWIN CORP", "NYSE")
    sec.add_company("TWIN", 2000, "TWIN CORP", "NYSE")
    db.save_sec_lists(store, listings, {"QQQ", "SGOV", "FUNDY"})
    today = trade_day(now)
    reports = [EarningsDate(s, today + timedelta(days=3 if s == "SOON" else 40)) for s in [*STOCKS, "TWIN"]]
    db.save_earnings(store, reports, today, now)
    db.record_feed(store, FEED_EARNINGS, True, "test", now)
    quotes_seen(store, [*STOCKS, "TWIN"])
    rig.requote()
    await rig.ready()
    client = Sec("Test Person test@example.com", client=sec.client(), limiter=RateLimiter(1e6), backoff=0)
    rec = Recorder()
    eng = Engine(cfg, store, rig.market, sources=Sources(sec=client), alerts=Alerts(rec, Limits()), clock=rig.clock)
    return eng, rig, rec


def quotes_seen(store, symbols, samples=30, usable=30, day=FRIDAY):
    """A week's IEX quote samples (here all on Friday) for `symbols`."""
    with store.tx() as c:
        c.executemany("INSERT OR REPLACE INTO quote_quality(symbol,day,samples,usable) VALUES(?,?,?,?)",
                      [(s, day.isoformat(), samples, usable) for s in symbols])


def drain(alerts):
    out = []
    while not alerts.queue.empty():
        out.append(alerts.queue.get_nowait())
    return out


async def test_the_weekly_screen_promotes_satellites_the_desk_then_trades_as_stocks(cfg, store, ledger):
    eng, rig, rec = await screen_rig(cfg, store)
    res = await eng.screen(apply=True)
    k = res.counts
    assert (k["listed"], k["liquid"], k["measured"], k["passed"], k["ranked"]) == (15, 13, 13, 11, 10)
    assert "TRST" not in res.rows                   # a commodity trust: never ranked as a company
    assert {r.symbol for r in res.rows.values() if not r.passed} == {"DOWN", "NEWCO"}
    promoted = [s for s, _ in res.decision.promote]
    assert len(promoted) == 6 and "SOON" not in promoted and len({"AAPL", "TWIN"} & set(promoted)) == 1
    passed = dict(res.decision.passed)
    assert passed["SOON"] == "reports in 2 sessions"
    twin = ({"AAPL", "TWIN"} - set(promoted)).pop()
    assert passed[twin].startswith("moves with ")
    # Members, market data and the council see them as stocks.
    assert set(promoted) <= set(eng.members) and all(eng.members[s].asset_class == "stock" for s in promoted)
    assert set(promoted) <= set(rig.market.tracked())
    assert all(len(rig.market.adjusted(s)) > 60 for s in promoted)     # history loaded for Monday's meeting
    for s in promoted:
        assert (await rig.market.vol_regime(s))[0] is not None
    assert set(udb.satellites(store)) == set(promoted)
    # Stored for /screen and the API, and the digest is in Discord.
    last = udb.last_screen(store)
    assert last["applied"] and last["result"]["decision"]["promote"][0][0] == promoted[0]
    (post,) = drain(eng.alerts)
    assert post.startswith(f"**Weekly stock screen** {FRIDAY + timedelta(days=1)}: 10 of 13 measured stocks "
                           "passed the gates as operating companies (13 liquid).")
    assert f"- Promoted {promoted[0]} (12-1 " in post and "- Passed over SOON: reports in 2 sessions" in post
    assert "—" not in post
    assert screen_report(res, 5).count("\n") > 5


async def test_a_satellite_that_fails_a_gate_is_sold_off_then_leaves(cfg, store, ledger):
    eng, rig, _ = await screen_rig(cfg, store)
    udb.promote(store, "DOWN", 50.0, "test", SAT - 30 * 86400)
    with store.tx():
        ledger.apply_fill(idem_key="b", asset="DOWN", side="BUY", quantity="5", price="40", quote_source="test",
                          origin="test", now=SAT - 29 * 86400)
    res = await eng.screen(apply=True)
    assert dict(res.decision.demote)["DOWN"].startswith("hard gate: at or under its 200-session average")
    assert udb.satellites(store)["DOWN"].state == "sunset"
    assert not eng.members["DOWN"].buyable                      # SELL or HOLD only
    with store.tx():
        ledger.apply_fill(idem_key="s", asset="DOWN", side="SELL", quantity="5", price="30", quote_source="test",
                          origin="test", now=SAT)
    await eng.refresh_members()
    assert "DOWN" not in eng.members and udb.satellites(store, ("removed",))["DOWN"].state == "removed"
    assert [e["change"] for e in udb.events(store) if e["symbol"] == "DOWN"] == ["removed", "demoted", "promoted"]


async def test_a_dry_run_changes_nothing_and_a_screen_without_sec_lists_refuses(cfg, store, ledger):
    eng, rig, _ = await screen_rig(cfg, store)
    res = await eng.screen(apply=False, kind="dry_run")
    assert res.decision.promote and udb.satellites(store) == {} and udb.last_applied_ts(store) is None
    assert udb.last_screen(store)["kind"] == "dry_run"
    assert "(dry run, nothing changed)" in drain(eng.alerts)[0]
    store.conn.execute("DELETE FROM sec_funds")
    with pytest.raises(ScreenError, match="lists aren't loaded"):
        await eng.screen()


async def test_the_api_and_chat_show_the_screen(cfg, store, ledger):
    eng, rig, _ = await screen_rig(cfg, store)
    await eng.screen(apply=True)
    server = ApiServer(eng, "127.0.0.1", 0)
    await server.start()
    try:
        async with aiohttp.ClientSession(f"http://127.0.0.1:{server.port}") as s:
            async with s.get("/api/screen") as r:
                d = await r.json()
            async with s.get("/api/universe") as r:
                u = await r.json()
    finally:
        await server.stop()
    assert d["schedule"] == "Sat 08:00 ET weekly" and len(d["satellites"]) == 6 and d["screen"]["applied"]
    assert {x["symbol"] for x in u["satellites"]} == {x["symbol"] for x in d["satellites"]}
    text = render.screen(d, SAT)
    assert text.startswith("**Satellites** (6 of 6, screened Sat 08:00 ET weekly):")
    assert "**Latest screen**" in text and "Passed over SOON: reports in 2 sessions" in text


async def test_the_weekly_schedule_and_its_health(cfg, store, ledger):
    sc = cfg.screen
    assert last_slot(sc, SAT) == SAT and last_slot(sc, SAT - 1) == SAT - 7 * 86400
    assert last_slot(sc, et(2026, 11, 2, 12)) == et(2026, 10, 31, 8)       # across the DST change
    eng, rig, _ = await screen_rig(cfg, store, now=SAT + 4 * 3600)
    now = SAT + 4 * 3600
    stale = health.screen_health(cfg, store, None, now, epoch=now - 5 * 3600)
    assert not stale.healthy and stale.problem == "screen missed the Sat 10-24 08:00 ET run (no screen was applied)"
    assert health.screen_health(cfg, store, None, now, epoch=now - 600).healthy      # a fresh start's grace
    loop = asyncio.create_task(eng._screen_loop())
    for _ in range(200):
        if udb.last_applied_ts(store):
            break
        await asyncio.sleep(0.05)
    loop.cancel()
    assert udb.last_applied_ts(store) == now
    assert health.screen_health(cfg, store, None, now, epoch=now - 5 * 3600).healthy
    off = dataclasses.replace(cfg, screen=dataclasses.replace(cfg.screen, enabled=False))
    assert health.screen_health(off, store, None, now + 30 * 86400, None).expect == "off"


def test_the_discord_digest():
    d = {"day": "2026-10-24", "applied": True, "counts": {"ranked": 3, "measured": 5, "liquid": 9},
         "promote": [["NVDA (12-1 +85%)", "ranks #1, score 91: fills an open slot"]], "demote": [], "keep": [],
         "passed": [], "errors": 0}
    assert format_event("screen", d) == ("**Weekly stock screen** 2026-10-24: 3 of 5 measured stocks passed the gates "
                                         "as operating companies (9 liquid).\n- Promoted NVDA (12-1 +85%): ranks #1, "
                                         "score 91: fills an open slot")


def test_ask_sees_the_satellites_and_the_latest_screen():
    from ottr.chat.client import DeskContext
    from ottr.chat.prompt import desk_data
    ctx = DeskContext(screen={
        "schedule": "Sat 08:00 ET weekly", "satellites_max": 6,
        "satellites": [{"symbol": "NVDA", "state": "active", "promoted_day": "2026-10-24"},
                       {"symbol": "XYZ", "state": "sunset", "demoted_day": "2026-10-24", "why": "hard gate: thin"}],
        "screen": {"ts": SAT, "applied": True, "result": {"decision": {
            "promote": [["NVDA", "ranks #1, score 91: fills an open slot"]], "demote": [["XYZ", "hard gate: thin"]],
            "passed": [["MSFT", "moves with QQQ (correlation 0.81)"]]}}}})
    text = desk_data(ctx, SAT)
    assert ("SATELLITES (stocks the weekly screener picked, at most 6; screened Sat 08:00 ET weekly): NVDA since "
            "2026-10-24; XYZ being sold off (demoted 2026-10-24: hard gate: thin)") in text
    assert ("promoted NVDA (ranks #1, score 91: fills an open slot); demoted XYZ (hard gate: thin); passed over "
            "MSFT (moves with QQQ (correlation 0.81))") in text
    assert "SATELLITES: unavailable (down)" in desk_data(DeskContext(errors={"screen": "down"}), SAT)


async def test_without_the_sec_no_stock_is_assumed_to_be_a_company(cfg, store, ledger):
    eng, rig, _ = await screen_rig(cfg, store)
    eng.sources = Sources()                       # no SEC_USER_AGENT, nothing cached
    with pytest.raises(ScreenError, match="no operating company confirmed among 11 stocks that passed the gates"):
        await eng.screen()
    assert udb.satellites(store) == {}


async def test_stocks_whose_iex_quote_is_rarely_usable_are_skipped_or_demoted(cfg, store, ledger):
    eng, rig, _ = await screen_rig(cfg, store)
    first = await eng.screen(apply=True)
    held_in = [s for s, _ in first.decision.promote]
    poor_sat, poor_cand = held_in[0], ({"AAPL", "TWIN"} - set(held_in)).pop()
    quotes_seen(store, [poor_sat], samples=400, usable=120)            # a member: sampled every minute
    quotes_seen(store, [poor_cand], samples=26, usable=10)
    res = await eng.screen(apply=True)                                 # the same week, screened again
    assert dict(res.decision.demote)[poor_sat] == (
        "hard gate: IEX quote usable only 30% of the time since 2026-10-19 (needs 75%)")
    assert dict(res.decision.passed)[poor_cand] == "IEX quote usable only 38% of the time since 2026-10-19 (needs 75%)"
    assert res.rows[poor_sat].quote_usable == pytest.approx(0.3)
    assert res.counts["quote_ok"] == res.counts["ranked"] - 1
    assert " iex  flags" in screen_report(res, 3)


async def test_an_unmeasured_candidate_waits_a_week(cfg, store, ledger):
    eng, rig, _ = await screen_rig(cfg, store)
    store.conn.execute("DELETE FROM quote_quality")
    res = await eng.screen(apply=True)
    assert res.decision.promote == [] and res.counts["quote_ok"] == 0
    assert all(why.startswith("IEX quote not measured enough yet (0 samples since 2026-10-19")
               for s, why in res.decision.passed if s not in ("SOON",))
    # During the week the engine samples the screen's best ranked (untracked) stocks.
    rig.at(et(2026, 10, 26, 11))
    await eng.market_tick()
    seen = quality.usable(store, [r.symbol for r in res.ranked], date(2026, 10, 26))
    assert len(seen) == len(res.ranked) and all(n == 1 for n, _ in seen.values())
    await eng.market_tick()                                            # not again within 15 minutes
    assert quality.usable(store, ["AAPL"], date(2026, 10, 26))["AAPL"][0] == 1
    assert quality.usable(store, ["SPY"], date(2026, 10, 26))["SPY"] == (2, 2)   # members: every tick
