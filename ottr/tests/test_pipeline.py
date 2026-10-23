"""
Pipeline tests: council -> executor -> ledger on the US engine, with scripted models and
the real MarketData over FakeAlpaca (a mid-session Wednesday on the 2026 NYSE calendar).

Covers the invariants end to end: code-sized BUYs; the chair's rules (no BUY without an
analyst's support, no dollar sizes, a blocked BUY dropped with its reason); quorum;
duplicate execution; a bad quote deferring the order until the jobs task retries it; an
outage; decision expiry; dry run; a protective exit jumping the queue; the risk monitor's
trim and its idle nights; the daily jobs (sweep, fee charge); equity points with the
benchmarks; and a clean stop.
"""

import asyncio
import dataclasses
import json
from datetime import date
from decimal import Decimal

import pytest

from ottr.council.council import Council
from ottr.council.models import ModelError
from ottr.engine import engine as engmod
from ottr.engine.engine import Engine
from ottr.ledger.book import Ledger
from ottr.risk.types import Intent
from tests.fakes import (OPEN, ScriptedModel, analyst_reply, buy_council, council_for, et, market_rig,
                         no_corporate_actions)

SAT = et(2026, 10, 24, 12)


@pytest.fixture
async def rig(cfg, store):
    no_corporate_actions(store, cfg.core)
    return await market_rig(cfg, store).ready()


def engine(cfg, store, rig, council=None) -> Engine:
    return Engine(cfg, store, rig.market, council=council or Council({}, ScriptedModel("c", []), list(cfg.core)),
                  clock=rig.clock)


def buy(asset="TLT", key="k1", conviction=1.0, created=OPEN):
    return Intent(origin="council", asset=asset, side="BUY", conviction=conviction, idem_key=key, created_ts=created)


def fills(store):
    return [tuple(r) for r in store.conn.execute("SELECT side, asset, origin FROM fills ORDER BY id")]


# -- meetings ------------------------------------------------------------------------------------
async def test_meeting_executes_a_code_sized_buy(cfg, store, ledger, rig):
    eng = engine(cfg, store, rig, buy_council(cfg, "TLT"))
    rec = await eng.meeting(execute=True)
    await eng.executor.drain()
    assert rec["status"] == "ok"
    pos = ledger.positions()["TLT"]
    # Code sized it: the vol target x 0.7 conviction, capped well under the 25% class cap.
    assert pos.qty > 0 and float(pos.cost) < 0.25 * 10000
    assert ledger.check() == []
    assert store.conn.execute("SELECT COUNT(*) FROM stances").fetchone()[0] == 3 * len(cfg.core)
    packet = store.conn.execute("SELECT packet FROM meetings").fetchone()[0]
    assert "The US market is OPEN" in packet and "DESK BOOK (paper; margin_1x" in packet


async def test_a_buy_no_analyst_supports_is_dropped(cfg, store, ledger, rig):
    u = list(cfg.core)
    chair = {"summary": "s", "trades": [{"action": "BUY", "asset": "GLD", "conviction": 0.9, "reasoning": "vibes"}]}
    eng = engine(cfg, store, rig, council_for(cfg, [analyst_reply(u)] * 3, chair))
    rec = await eng.meeting(execute=True)
    await eng.executor.drain()
    assert rec["trades"] == [] and rec["dropped"][0]["why"] == "no analyst rated it BUY"
    assert ledger.positions() == {}


async def test_a_blocked_buy_is_dropped_with_its_reason(cfg, store, ledger, rig):
    store.conn.execute("DELETE FROM corporate_action_checks WHERE symbol='TLT'")
    eng = engine(cfg, store, rig, buy_council(cfg, "TLT"))
    rec = await eng.meeting(execute=True)
    assert rec["trades"] == [] and rec["dropped"][0]["why"] == "corporate actions unknown: never checked"
    assert "BUYS BLOCKED (code drops these): TLT (corporate actions unknown: never checked)" in rec["context"]


async def test_the_chair_cannot_send_dollar_sizes(cfg, store, ledger, rig):
    u = list(cfg.core)
    bad = {"summary": "s", "trades": [{"action": "BUY", "asset": "TLT", "conviction": 0.9, "usd_amount": 9000,
                                       "reasoning": "r"}]}
    analysts = {n: ScriptedModel(n, [analyst_reply(u, {"TLT": ("BUY", 0.9)})]) for n in ("claude", "gpt", "gemini")}
    rec = await engine(cfg, store, rig, Council(analysts, ScriptedModel("chair", [bad, bad]), u)).meeting()
    assert rec["status"].startswith("abstain: chair failed")


async def test_a_signed_out_chair_still_trades_through_the_fallback(cfg, store, ledger, rig):
    u = list(cfg.core)
    analysts = {"claude": ScriptedModel("claude", [ModelError("claude exit 1: Not logged in")]),
                "gpt": ScriptedModel("gpt", [analyst_reply(u, {"TLT": ("BUY", 0.8)})]),
                "gemini": ScriptedModel("gemini", [analyst_reply(u)])}
    buy_tlt = {"summary": "buy tlt", "trades": [{"action": "BUY", "asset": "TLT", "conviction": 0.7,
                                                 "reasoning": "trend"}]}
    council = Council(analysts, ScriptedModel("claude", [ModelError("claude exit 1: Not logged in")]), u,
                      fallback=ScriptedModel("gpt", [buy_tlt]))
    eng = engine(cfg, store, rig, council)
    rec = await eng.meeting(execute=True)
    await eng.executor.drain()
    assert rec["status"] == "ok" and rec["errors"] == {"claude": "claude exit 1: Not logged in"}
    assert fills(store) == [("BUY", "TLT", "council")]
    stored = json.loads(store.conn.execute("SELECT record FROM meetings").fetchone()[0])
    assert stored["chair"]["name"] == "gpt" and stored["chair"]["replaced"] == "claude"


async def test_quorum_failure_means_no_trades(cfg, store, ledger, rig):
    u = list(cfg.core)
    reps = [analyst_reply(u), ModelError("down"), ModelError("down")]
    rec = await engine(cfg, store, rig, council_for(cfg, reps, {"summary": "s", "trades": []})).meeting()
    assert rec["status"].startswith("abstain") and rec["trades"] == []


# -- the executor through the real market ---------------------------------------------------------
async def test_a_duplicate_intent_executes_once(cfg, store, ledger, rig):
    ex = engine(cfg, store, rig).executor
    assert (await ex.process(buy())).approved
    assert (await ex.process(buy())).reason == "duplicate:filled"
    assert len(fills(store)) == 1


async def test_a_wide_quote_defers_until_the_jobs_task_retries_it(cfg, store, ledger, rig, monkeypatch):
    eng = engine(cfg, store, rig)
    p = rig.price("TLT")
    rig.fake.add_quote("TLT", p * 0.97, p * 1.03, age=2)            # 6% wide: not a market
    v = await eng.executor.process(buy())
    assert v.reason == "no_quote" and "too_wide" in v.notes[0]
    assert store.conn.execute("SELECT status FROM orders").fetchone()[0] == "deferred"
    rig.at(OPEN + 120)                                               # a sane quote again
    monkeypatch.setattr(engmod, "DEFERRED_RETRY_SEC", 0.0)
    assert "1 deferred order(s) retried" in await eng.jobs_tick()
    await eng.executor.drain()
    assert fills(store) == [("BUY", "TLT", "council")]


async def test_an_outage_rejects_without_trading(cfg, store, ledger):
    rig = market_rig(cfg, store)
    await rig.market.refresh_calendar()
    rig.fake.fail_429["/v2/stocks/snapshots"] = 99
    v = await engine(cfg, store, rig).executor.process(buy())
    assert v.reason in ("market_data_error", "no_quote") and ledger.positions() == {}


async def test_an_expired_decision_is_not_executed(cfg, store, ledger, rig):
    v = await engine(cfg, store, rig).executor.process(buy(created=OPEN - 3600))
    assert v.reason == "decision_expired"


async def test_dry_run_records_and_never_fills_then_real_execution_still_runs(cfg, store, ledger, rig):
    dry = dataclasses.replace(cfg, trading=dataclasses.replace(cfg.trading, dry_run=True))
    assert (await engine(dry, store, rig).executor.process(buy(key="k"))).approved
    assert ledger.positions() == {} and store.audits("would_fill")
    assert (await engine(dry, store, rig).executor.process(buy(key="k"))).reason == "duplicate:dry_run"
    assert (await engine(cfg, store, rig).executor.process(buy(key="k"))).approved
    assert ledger.positions()["TLT"].qty > 0


async def test_a_protective_exit_jumps_the_queue_and_blocks_the_rebuy(cfg, store, ledger, rig):
    ex = engine(cfg, store, rig).executor
    await ex.process(buy("TLT", key="open"))
    await ex.submit(buy("TLT", key="rebuy"))
    await ex.submit(Intent(origin="stop", asset="TLT", side="SELL", fraction=1.0, idem_key="stop1", created_ts=OPEN))
    await ex.drain()
    assert [s for s, _, _ in fills(store)] == ["BUY", "SELL"]  # the stop ran first; the stale re-buy was refused
    reason = store.conn.execute("SELECT verdict FROM orders WHERE idem_key='rebuy'").fetchone()[0]
    assert "cooldown" in reason or "risk_event_during_decision" in reason
    assert ledger.check() == []


# -- risk ----------------------------------------------------------------------------------------
async def test_the_risk_monitor_trims_an_overweight_position(cfg, store, rig):
    lg = Ledger(store)
    spy = rig.price("SPY")
    qty = Decimal(str(round(4000 / spy, 6)))                          # 40% of a $10k book vs a 25% cap
    lg.initialize(Decimal("6000"), holdings={"SPY": (qty, spy)}, now=OPEN - 7 * 86400)
    eng = engine(cfg, store, rig)
    res = await eng.risk_tick()
    await eng.executor.drain()
    assert [f"{i.origin}:{i.asset}" for i in res.intents] == ["trim:SPY"]
    v = lg.value(await rig.market.marks())
    assert v.weights["SPY"] == pytest.approx(0.25, abs=0.01)
    assert lg.check() == []


async def test_the_risk_monitor_rests_while_the_market_is_closed(cfg, store, ledger, rig):
    store.set_state("risk_blind", {"TLT": OPEN})
    rig.at(SAT)
    assert await engine(cfg, store, rig).risk_tick() == "market closed: stops wait for the open"
    assert store.get_state("risk_blind") == {}


# -- jobs and equity -----------------------------------------------------------------------------
async def test_the_sweep_runs_before_the_close_and_fees_are_charged_after_it(cfg, store, ledger, rig):
    eng = engine(cfg, store, rig)
    await eng.executor.process(buy())
    assert ledger.pending_fees() > 0                                  # CAT accrued on the fill
    rig.at(et(2026, 10, 21, 15, 46))
    assert "sweep submitted" in await eng.jobs_tick()
    await eng.executor.drain()
    assert ("BUY", "SGOV", "sweep") in fills(store)
    rig.at(et(2026, 10, 21, 16, 30))
    assert "fees for 2026-10-21 charged" in await eng.jobs_tick()
    assert ledger.pending_fees() == 0 and ledger.check() == []


async def test_equity_points_in_session_and_one_at_the_close_with_benchmarks(cfg, store, ledger):
    from datetime import date
    rig = await market_rig(cfg, store, through=date(2026, 10, 23)).ready()   # Alpaca will have Wed's bar
    no_corporate_actions(store, cfg.core)
    eng = engine(cfg, store, rig)
    assert (await eng.equity_tick()).startswith("equity $10,000.00")
    first = store.conn.execute("SELECT bench_spy, bench_60_40 FROM equity").fetchone()
    assert tuple(first) == (None, None)                                       # no close since the book opened
    rig.at(et(2026, 10, 21, 16, 20))
    await rig.market.refresh_bars()
    await eng.equity_tick()
    base = store.get_state("bench_base")
    assert base["day"] == "2026-10-21" and base["equity"] == pytest.approx(10000)
    assert await eng.equity_tick() == "market closed; the last close is recorded"
    rig.at(et(2026, 10, 22, 11))
    await eng.equity_tick()
    row = store.conn.execute("SELECT bench_spy, bench_60_40 FROM equity ORDER BY ts DESC").fetchone()
    wed_close = dict(rig.market.adjusted("SPY"))[date(2026, 10, 21)]
    spy, _ = await rig.market.quote("SPY")
    assert row[0] == pytest.approx(10000 * spy.mid / wed_close)        # SPY's move since the base close
    assert row[1] is not None and row[1] != row[0]
    assert store.conn.execute("SELECT COUNT(*) FROM equity").fetchone()[0] == 3


async def test_the_engine_stops_cleanly_on_a_stop_event(cfg, store, ledger, rig):
    eng = engine(cfg, store, rig)
    store.set_state("next_meeting_at", OPEN + 3600)
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.3, stop.set)
    assert await asyncio.wait_for(eng.run(stop), timeout=10) == 0
    assert "engine_stop" in [a["kind"] for a in store.audits()]


# -- PR 8: the protective monitor in the engine -----------------------------------------------------
async def test_stops_and_trims_wait_out_the_opening_auction(cfg, store, ledger, rig):
    rig.at(et(2026, 10, 21, 9, 32))
    assert await engine(cfg, store, rig).risk_tick() == "the open: stops wait until 09:35 ET"


async def test_the_opening_buy_sets_the_stop_and_an_add_keeps_it(cfg, store, ledger, rig):
    from ottr.risk.monitor import entry_stop_pct
    eng = engine(cfg, store, rig)
    await eng.refresh_members()                                         # Alpaca's fractional-share flags
    ex = eng.executor
    await ex.process(buy(conviction=0.4, key="open"))
    vol, _ = await rig.market.vol_regime("TLT")
    first = ledger.positions()["TLT"].stop_pct
    assert first == pytest.approx(entry_stop_pct(cfg.risk, vol)) and 0.03 <= first <= 0.15
    store.conn.execute("UPDATE adj_closes SET close = close * (1 + (rowid % 2) * 0.05) WHERE symbol='TLT'")
    await ex.process(buy(conviction=1.0, key="add"))                        # an add, at a different vol
    assert ledger.positions()["TLT"].stop_pct == first
    await ex.process(Intent(origin="sweep", asset="SGOV", side="BUY", usd=500.0, idem_key="sw", created_ts=OPEN))
    assert ledger.positions()["SGOV"].stop_pct is None                     # swept cash has no stop


async def test_a_breached_stop_sells_the_position_at_the_bid(cfg, store, ledger, rig):
    eng = engine(cfg, store, rig)
    await eng.executor.process(buy())
    stop = ledger.positions()["TLT"].stop_price
    p = round(stop * 0.99, 2)
    rig.fake.add_quote("TLT", p, p + 0.02, age=2)
    rig.market._snaps_ts = 0
    res = await eng.risk_tick()
    assert [i.origin for i in res.intents] == ["stop"] and res.summary() == "stop TLT"
    await eng.executor.drain()
    sell = store.conn.execute("SELECT price, origin FROM fills WHERE side='SELL'").fetchone()
    assert (float(sell[0]), sell[1]) == (p, "stop") and "TLT" not in ledger.positions()


async def test_a_gap_through_the_stop_is_flagged_audited_and_posted(cfg, store, ledger, rig):
    from ottr.config import Alerts as Limits
    from ottr.notify import Alerts
    posts = []

    async def hook(text):
        posts.append(text)
        return True

    eng = Engine(cfg, store, rig.market, alerts=Alerts(hook, Limits()), clock=rig.clock)
    await eng.executor.process(buy())
    stop = ledger.positions()["TLT"].stop_price
    rig.at(et(2026, 10, 22, 9, 36))                                      # next morning: Wed closed above the stop
    from ottr.market.types import Bar
    from ottr.market import history
    history.save_bars(store, {"TLT": [Bar(date(2026, 10, 21), stop * 1.03, stop * 1.04, stop * 1.02, stop * 1.03, 1)]})
    gapped = round(stop * 0.95, 2)
    rig.fake.add_quote("TLT", gapped, gapped + 0.02, age=2)
    rig.market._snaps_ts = 0
    await eng.risk_tick()
    gap = store.audits("risk_gap")
    assert gap and gap[0]["asset"] == "TLT" and gap[0]["close"] == pytest.approx(stop * 1.03)
    while not eng.alerts.queue.empty():
        posts.append(eng.alerts.queue.get_nowait())
    assert any(p.startswith("**TLT gapped through its stop**") for p in posts)


async def test_the_breakers_sale_survives_the_night(cfg, store, ledger, rig):
    eng = engine(cfg, store, rig)
    await eng.refresh_members()                                         # fractional TLT: a 25% sale is exact
    await eng.executor.process(buy())
    for i in range(cfg.risk.min_equity_points):
        store.add_equity(OPEN - 3600 + i, 10000.0, 0, 0, None, None, {})
    store.set_state("risk", {"peak_equity": 20000.0})                    # equity is half the peak
    p = rig.price("TLT")
    rig.fake.add_quote("TLT", p * 0.97, p * 1.03, age=2)                 # no usable quote: the sale defers...
    rig.fake.add_minute_bar("TLT", OPEN - 20 * 60, p)                    # ...but a delayed print still values the book
    rig.market._snaps_ts = 0
    await eng.risk_tick()
    await eng.executor.drain()
    assert store.get_state("risk")["halted"] and store.get_state("risk")["derisk"] == ["TLT"]
    key = store.conn.execute("SELECT idem_key, status FROM orders WHERE origin='derisk'").fetchone()
    assert key[1] == "deferred"
    qty_before = ledger.positions()["TLT"].qty
    rig.at(et(2026, 10, 22, 9, 40))                                      # the next morning, a sane quote
    await eng.risk_tick()
    await eng.executor.drain()
    assert store.conn.execute("SELECT status FROM orders WHERE idem_key=?", (key[0],)).fetchone()[0] == "filled"
    assert float(ledger.positions()["TLT"].qty) == pytest.approx(float(qty_before) * 0.75, rel=1e-6)
    await eng.risk_tick()
    await eng.executor.drain()                                           # re-issued, answered "duplicate"
    assert store.conn.execute("SELECT COUNT(*) FROM fills WHERE origin='derisk'").fetchone()[0] == 1


async def test_holdings_brought_in_at_init_get_their_stop_at_the_first_check(cfg, store, rig):
    lg = Ledger(store)
    lg.initialize(Decimal("9000"), holdings={"GLD": (Decimal("2"), rig.price("GLD"))}, now=OPEN - 7 * 86400)
    assert lg.positions()["GLD"].stop_pct is None
    await engine(cfg, store, rig).risk_tick()
    assert 0.03 <= lg.positions()["GLD"].stop_pct <= 0.15
