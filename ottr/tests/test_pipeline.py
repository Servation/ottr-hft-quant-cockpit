"""
Pipeline tests: council -> executor -> ledger, with fake models and a fake market.

Covers the review's must-haves: duplicate execution, crash safety, stale data,
protective-exit priority, decision expiry, dry run, and accounting invariants.
"""

import asyncio
import time
from decimal import Decimal

import pytest

from ottr.council.council import Council
from ottr.council.models import ModelError
from ottr.engine.engine import Engine
from ottr.engine.executor import Executor
from ottr.ledger.book import Ledger
from ottr.risk.types import Intent
from tests.fakes import FakeMarket, ScriptedModel, analyst_reply

PRICES = {"BTC": 80000.0, "ETH": 2400.0, "SOL": 100.0, "XRP": 1.3, "ADA": 0.22,
          "DOGE": 0.08, "LINK": 12.0, "AVAX": 9.8}


@pytest.fixture
def market(cfg):
    return FakeMarket(cfg, PRICES)


def council_for(cfg, analyst_replies, chair_reply):
    analysts = {n: ScriptedModel(n, [r]) for n, r in zip(("claude", "gpt", "gemini"), analyst_replies)}
    return Council(analysts, ScriptedModel("chair", [chair_reply]), cfg.universe,
                   min_analysts=2, max_trades=3)


def engine(cfg, store, market, council=None):
    return Engine(cfg, store, market, council=council or Council({}, ScriptedModel("c", []), cfg.universe))


def buy_intent(asset="ETH", key="k1", conviction=1.0, created=None):
    return Intent(origin="council", asset=asset, side="BUY", conviction=conviction,
                  idem_key=key, created_ts=created or time.time())


async def test_meeting_executes_code_sized_buy(cfg, store, ledger, market):
    u = cfg.universe
    reps = [analyst_reply(u, {"ETH": ("BUY", 0.8)}), analyst_reply(u, {"ETH": ("BUY", 0.6)}),
            analyst_reply(u)]
    chair = {"summary": "buy eth", "trades": [{"action": "BUY", "asset": "ETH", "conviction": 0.7,
                                               "reasoning": "trend"}]}
    eng = engine(cfg, store, market, council_for(cfg, reps, chair))
    rec = await eng.meeting(execute=True)
    await eng.executor.drain()
    assert rec["status"] == "ok"
    pos = ledger.positions()["ETH"]
    assert pos.qty > 0
    # Code sized it: 2% / vol * 0.7 of equity, far below anything a model "asked" for.
    assert float(pos.cost) < 0.1 * 10000
    assert ledger.check() == []
    stances = store.conn.execute("SELECT COUNT(*) FROM stances").fetchone()[0]
    assert stances == 3 * len(u)


async def test_chair_buy_without_analyst_support_is_dropped(cfg, store, ledger, market):
    u = cfg.universe
    chair = {"summary": "s", "trades": [{"action": "BUY", "asset": "DOGE", "conviction": 0.9,
                                         "reasoning": "vibes"}]}
    eng = engine(cfg, store, market, council_for(cfg, [analyst_reply(u)] * 3, chair))
    rec = await eng.meeting(execute=True)
    await eng.executor.drain()
    assert rec["trades"] == [] and rec["dropped"][0]["why"] == "no analyst rated it BUY"
    assert ledger.positions() == {}


async def test_chair_cannot_send_dollar_sizes(cfg, store, ledger, market):
    u = cfg.universe
    bad = {"summary": "s", "trades": [{"action": "BUY", "asset": "ETH", "conviction": 0.9,
                                       "usd_amount": 9000, "reasoning": "r"}]}
    reps = [analyst_reply(u, {"ETH": ("BUY", 0.9)})] * 3
    analysts = {n: ScriptedModel(n, [r]) for n, r in zip(("claude", "gpt", "gemini"), reps)}
    council = Council(analysts, ScriptedModel("chair", [bad, bad]), u)
    rec = await engine(cfg, store, market, council).meeting(execute=True)
    assert rec["status"].startswith("abstain: chair failed")


async def test_quorum_failure_means_no_trades(cfg, store, ledger, market):
    u = cfg.universe
    reps = [analyst_reply(u), ModelError("down"), ModelError("down")]
    chair = {"summary": "s", "trades": []}
    rec = await engine(cfg, store, market, council_for(cfg, reps, chair)).meeting()
    assert rec["status"].startswith("abstain") and rec["trades"] == []


async def test_duplicate_intent_executes_once(cfg, store, ledger, market):
    ex = Executor(cfg, store, ledger, market)
    v1 = await ex.process(buy_intent())
    v2 = await ex.process(buy_intent())
    assert v1.approved and v2.reason == "duplicate:filled"
    assert store.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1


async def test_crash_after_claim_retries_cleanly(cfg, store, ledger, market, monkeypatch):
    ex = Executor(cfg, store, ledger, market)
    real = ledger.apply_fill

    def boom(**kw):
        real(**kw)
        raise RuntimeError("power cut before commit")

    monkeypatch.setattr(ledger, "apply_fill", boom)
    with pytest.raises(RuntimeError):
        await ex.process(buy_intent())
    assert ledger.positions() == {} and ledger.check() == []  # rolled back entirely
    monkeypatch.setattr(ledger, "apply_fill", real)
    v = await ex.process(buy_intent())  # pending (not terminal) -> retried once
    assert v.approved and store.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1


async def test_stale_data_defers_and_retries(cfg, store, ledger, market):
    ex = Executor(cfg, store, ledger, market)
    market.age = 10_000
    v = await ex.process(buy_intent())
    assert v.reason == "stale_quote"
    status = store.conn.execute("SELECT status FROM orders").fetchone()[0]
    assert status == "deferred"
    market.age = 0
    assert (await ex.process(buy_intent())).approved


async def test_market_outage_rejects_without_trading(cfg, store, ledger, market):
    market.fail = True
    v = await Executor(cfg, store, ledger, market).process(buy_intent())
    assert v.reason == "market_data_error" and ledger.positions() == {}


async def test_expired_decision_is_not_executed(cfg, store, ledger, market):
    v = await Executor(cfg, store, ledger, market).process(buy_intent(created=time.time() - 3600))
    assert v.reason == "decision_expired"


async def test_dry_run_records_verdict_and_never_fills(cfg, store, ledger, market):
    import dataclasses
    dry = dataclasses.replace(cfg, trading=dataclasses.replace(cfg.trading, dry_run=True))
    v = await Executor(dry, store, ledger, market).process(buy_intent())
    assert v.approved and ledger.positions() == {}
    assert store.audits("would_fill")


async def test_dry_run_verdict_does_not_block_real_execution_later(cfg, store, ledger, market):
    import dataclasses
    dry = dataclasses.replace(cfg, trading=dataclasses.replace(cfg.trading, dry_run=True))
    assert (await Executor(dry, store, ledger, market).process(buy_intent(key="stop-like"))).approved
    assert (await Executor(dry, store, ledger, market).process(buy_intent(key="stop-like"))).reason == "duplicate:dry_run"
    v = await Executor(cfg, store, ledger, market).process(buy_intent(key="stop-like"))
    assert v.approved and ledger.positions()["ETH"].qty > 0


async def test_protective_exit_jumps_queue_and_blocks_rebuy(cfg, store, ledger, market):
    ex = Executor(cfg, store, ledger, market)
    await ex.process(buy_intent("SOL", key="open"))
    t_decision = time.time()
    await ex.submit(buy_intent("SOL", key="rebuy", created=t_decision))
    await ex.submit(Intent(origin="stop", asset="SOL", side="SELL", fraction=1.0, idem_key="stop1"))
    await ex.drain()
    sides = [r[0] for r in store.conn.execute("SELECT side FROM fills ORDER BY id")]
    assert sides == ["BUY", "SELL"]  # the stop ran first; the stale re-buy was refused
    reason = store.conn.execute("SELECT verdict FROM orders WHERE idem_key='rebuy'").fetchone()[0]
    assert "cooldown" in reason or "risk_event_during_decision" in reason
    assert ledger.check() == []


async def test_risk_tick_trims_imported_overweight_position(cfg, store, market):
    lg = Ledger(store)
    # Mirrors the live v1 book: ADA ~21.6% of equity against a 15% cap.
    lg.initialize(Decimal("5898.76"), holdings={"ADA": (Decimal("10081.9"), 0.22),
                                               "DOGE": (Decimal("21988.9"), 0.08)})
    eng = engine(cfg, store, market)
    res = await eng.risk_tick()
    await eng.executor.drain()
    assert [i.origin + ":" + i.asset for i in res.intents] == ["trim:ADA"]
    v = lg.value(await market.quotes())
    assert v.weights["ADA"] == pytest.approx(0.15, abs=0.005)
    assert lg.check() == []


async def test_equity_tick_records_benchmarks(cfg, store, ledger, market):
    eng = engine(cfg, store, market)
    await eng.equity_tick()
    market.prices["BTC"] *= 1.1
    await eng.equity_tick()
    row = store.conn.execute("SELECT equity, bench_btc FROM equity ORDER BY ts DESC").fetchone()
    assert row[0] == pytest.approx(10000) and row[1] == pytest.approx(11000)


async def test_engine_stops_cleanly_on_stop_event(cfg, store, ledger, market):
    eng = engine(cfg, store, market)
    store.set_state("next_meeting_at", time.time() + 3600)
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.3, stop.set)
    await asyncio.wait_for(eng.run(stop), timeout=10)
    kinds = [a["kind"] for a in store.audits()]
    assert "engine_stop" in kinds
