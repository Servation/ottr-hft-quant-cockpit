"""
Engine operations tests: the market heartbeat, persisted risk-blind state, watchdog
alarms reaching Discord and the audit log, sleep/suspend detection, crash-only exit
codes, the stop-request file, and the API's lifecycle inside `Engine.run`.
"""

import asyncio
import dataclasses
import socket
import time

import aiohttp
import pytest

from ottr.config import Alerts as Limits
from ottr.control import request_stop, stop_path
from ottr.engine.engine import Engine
from ottr.market.types import MarketDataError
from ottr.notify import Alerts
from tests.fakes import PRICES, FakeMarket, eth_buy_council


class Recorder:
    def __init__(self):
        self.posts = []

    async def __call__(self, text):
        self.posts.append(text)
        return True


@pytest.fixture
def market(cfg):
    return FakeMarket(cfg, PRICES)


@pytest.fixture
def rec():
    return Recorder()


@pytest.fixture
def eng(cfg, store, ledger, market, rec):
    return Engine(cfg, store, market, council=eth_buy_council(cfg), alerts=Alerts(rec, Limits()))


def drain(alerts):
    out = []
    while not alerts.queue.empty():
        out.append(alerts.queue.get_nowait())
        alerts.queue.task_done()
    return out


async def test_market_tick_passes_on_fresh_data(eng):
    await eng.market_tick()


async def test_market_tick_fails_on_stale_quotes_or_candles(eng, market):
    market.age = 600
    with pytest.raises(MarketDataError, match="BTC quote 10m old"):
        await eng.market_tick()
    market.age = 0
    market.candle_rows["SOL"] = market.candle_rows["SOL"][:-5]  # closes stopped 5 days ago
    with pytest.raises(MarketDataError, match="SOL last daily close"):
        await eng.market_tick()


async def _hold_eth(eng):
    await eng.meeting(execute=True)
    await eng.executor.drain()
    assert "ETH" in eng.ledger.positions()


async def test_risk_blind_is_persisted_and_cleared(eng, market, store):
    await _hold_eth(eng)
    market.age = 600
    await eng.risk_tick()
    since = store.get_state("risk_blind")["ETH"]
    await eng.risk_tick()
    assert store.get_state("risk_blind")["ETH"] == since  # "blind since" doesn't move
    market.age = 0
    await eng.risk_tick()
    assert store.get_state("risk_blind") == {}


async def test_total_outage_marks_every_holding_blind(eng, market, store):
    await _hold_eth(eng)
    market.fail = True
    with pytest.raises(MarketDataError):
        await eng.risk_tick()
    assert set(store.get_state("risk_blind")) == {"ETH"}


def beat_at(store, task, ts, ok=True, detail=""):
    """A heartbeat stamped at simulated time `ts` (store.heartbeat uses the real clock)."""
    store.conn.execute("INSERT INTO heartbeats(task,ts,ok,detail,ok_ts) VALUES(?,?,?,?,?) "
                       "ON CONFLICT(task) DO UPDATE SET ts=excluded.ts, ok=excluded.ok, detail=excluded.detail, "
                       "ok_ts=COALESCE(excluded.ok_ts, heartbeats.ok_ts)",
                       (task, ts, int(ok), detail, ts if ok else None))


async def test_watchdog_alarm_reaches_audit_and_discord_then_resolves(eng, store):
    now = time.time()
    eng.epoch, eng._last_watch = now - 3600, now - 60
    for t in ("market", "equity", "council"):
        beat_at(store, t, now)
    beat_at(store, "risk", now, ok=False, detail="MarketDataError: no quotes")  # never succeeded this run
    eng.watchdog_tick(now)
    assert set(eng.watchdog.active) == {"task:risk"}
    assert store.audits("alarm_raised")[0]["key"] == "task:risk"
    (post,) = drain(eng.alerts)
    assert post.startswith("**ALARM** (critical): risk task failing for 60m: MarketDataError")

    for step in range(1, 7):  # the feed recovers; one tick a minute
        t = now + 60 * step
        for task in ("market", "risk"):
            beat_at(store, task, t)
        eng.watchdog_tick(t)
        if step < 5:  # clean, but inside the 300s hysteresis
            assert "task:risk" in eng.watchdog.active and drain(eng.alerts) == []
    assert eng.watchdog.active == {}
    assert drain(eng.alerts) == ["**Resolved**: risk task failing for 60m: MarketDataError: no quotes"]
    assert store.audits("alarm_cleared")[0]["key"] == "task:risk"
    assert store.heartbeats()["watchdog"]["ok"]


async def test_system_sleep_does_not_raise_a_storm_of_alarms(eng, store):
    now = time.time()
    eng.epoch = eng._last_watch = now - 60
    for t in ("market", "risk", "equity", "council"):
        store.heartbeat(t, True)
    eng.watchdog_tick(now)
    # The laptop slept for two hours; every heartbeat is now 2h old by the wall clock.
    later = now + 7200
    rep = eng.watchdog_tick(later)
    assert rep.alarms == [] and eng.epoch == later
    assert store.audits("engine_resumed")[0]["gap_sec"] == 7200
    assert any("suspended for 2.0h" in p for p in drain(eng.alerts))
    # But a task that still doesn't report after the new grace period does alarm.
    assert eng.watchdog_tick(later + 60).alarms == []
    assert eng.watchdog_tick(later + 120).alarms == []
    assert any(a.key == "task:risk" for a in eng.watchdog_tick(later + 190).alarms)


async def _run_briefly(eng, seconds=0.5, **kw):
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(seconds, stop.set)
    return await asyncio.wait_for(eng.run(stop, **kw), timeout=20)


async def test_run_reports_an_unclean_previous_exit(eng, store, rec):
    store.set_state("engine", {"running": True, "pid": 1})  # last run died without stopping
    store.set_state("next_meeting_at", time.time() + 3600)
    assert await _run_briefly(eng) == 0
    start = store.audits("engine_start")[0]
    assert start["unclean_previous_exit"] is True
    assert any("did not shut down cleanly" in p for p in rec.posts)
    assert store.get_state("engine")["running"] is False  # this run did stop cleanly
    assert any(p.startswith("**Engine stopped** cleanly") for p in rec.posts)


async def test_a_dead_core_task_exits_1_for_the_supervisor(eng, store):
    async def broken():
        raise RuntimeError("council loop bug")

    eng._council_loop = broken
    code = await asyncio.wait_for(eng.run(asyncio.Event()), timeout=20)
    assert code == 1
    stop = store.audits("engine_stop")[0]
    assert stop["code"] == 1 and "council loop bug" in stop["why"]
    assert store.get_state("engine")["exit_code"] == 1


async def test_stop_file_stops_the_engine_but_a_stale_one_does_not(eng, cfg, store):
    store.set_state("next_meeting_at", time.time() + 3600)
    request_stop(cfg.data_dir)
    import os
    old = time.time() - 600
    os.utime(stop_path(cfg.data_dir), (old, old))  # left over from a crash 10 min ago

    async def drop_request():
        await asyncio.sleep(1.5)
        request_stop(cfg.data_dir, "test")

    asyncio.get_running_loop().create_task(drop_request())
    t0 = time.time()
    code = await asyncio.wait_for(eng.run(asyncio.Event()), timeout=20)
    assert code == 0 and 1.5 <= time.time() - t0 < 6
    assert store.audits("stop_requested")[0]["source"] == "stop_file"


async def test_api_runs_inside_the_engine(eng, store):
    store.set_state("next_meeting_at", time.time() + 3600)
    eng.cfg = dataclasses.replace(eng.cfg, api=dataclasses.replace(eng.cfg.api, port=0))
    stop = asyncio.Event()
    seen = {}

    async def probe():
        for _ in range(100):
            if eng.api is not None:
                break
            await asyncio.sleep(0.05)
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{eng.api.port}/api/health") as r:
                seen["status"] = r.status
        seen["port"] = eng.api.port
        stop.set()

    asyncio.get_running_loop().create_task(probe())
    assert await asyncio.wait_for(eng.run(stop, serve_api=True), timeout=20) == 0
    assert seen["status"] == 200 and eng.api is None
    with pytest.raises(OSError):  # the port was released on shutdown
        socket.create_connection(("127.0.0.1", seen["port"]), timeout=1).close()


async def test_taken_api_port_is_an_alarm_not_a_crash(eng, store):
    store.set_state("next_meeting_at", time.time() + 3600)
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    try:
        port = blocker.getsockname()[1]
        eng.cfg = dataclasses.replace(eng.cfg, api=dataclasses.replace(eng.cfg.api, port=port))
        assert await _run_briefly(eng, serve_api=True) == 0
        assert "api" in eng.problems and f":{port}" in eng.problems["api"].message
        assert any(a.key == "api" for a in eng.health_report().alarms)
    finally:
        blocker.close()
    await eng._start_api()  # the watchdog's retry once the port is free again
    try:
        assert eng.api is not None and "api" not in eng.problems
    finally:
        await eng._stop_api()


async def test_restart_names_the_alarms_it_lost_track_of(eng, store, rec):
    now = time.time()
    eng.epoch, eng._last_watch = now - 3600, now - 60
    beat_at(store, "risk", now, ok=False, detail="down")
    for t in ("market", "equity", "council"):
        beat_at(store, t, now)
    eng.watchdog_tick(now)
    assert set(store.get_state("alarms")) == {"task:risk"}
    store.set_state("next_meeting_at", time.time() + 3600)
    assert await _run_briefly(eng) == 0
    start = next(p for p in rec.posts if p.startswith("**Engine started**"))
    assert "re-checked from scratch and re-raised if they persist: task:risk" in start
    assert store.audits("engine_start")[0]["previous_alarms"] == ["task:risk"]
