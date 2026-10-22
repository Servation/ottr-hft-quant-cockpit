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
