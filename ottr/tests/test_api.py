"""
API tests against a real aiohttp server on an ephemeral loopback port: every read
endpoint, input validation, the DNS-rebinding Host check, the read-only connection, the
kill switch (auth, persistence, effect on the executor, config-wins rule), and the owner's
meeting call (auth, and the watcher's limits: refused with code's reason, queued once,
run as a called meeting, counted against the day).

The engine behind it runs on the market rig (the real MarketData over FakeAlpaca, on a
mid-session Wednesday), after a council meeting that bought TLT.
"""

import asyncio
import dataclasses
import json
import sqlite3
import time

import aiohttp
import pytest

from ottr.api import ApiServer
from ottr.engine import watcher
from ottr.engine.engine import Engine
from ottr.council.models import ModelError
from ottr.engine.killswitch import KillSwitch
from ottr.market.types import MarketDataError
from ottr.risk.types import Intent
from tests.fakes import OPEN, ScriptedModel, buy_council, et, market_rig, no_corporate_actions

KEY = "test-api-key-0123456789"


async def make_engine(cfg, store):
    no_corporate_actions(store, cfg.core)
    rig = await market_rig(cfg, store).ready()
    eng = Engine(cfg, store, rig.market, council=buy_council(cfg, "TLT"), clock=rig.clock)
    await eng.refresh_members()
    return eng, rig


@pytest.fixture
async def api(cfg, store, ledger):
    cfg = dataclasses.replace(cfg, api_key=KEY)
    eng, rig = await make_engine(cfg, store)
    rec = await eng.meeting(execute=True)
    await eng.executor.drain()
    for i in range(30):
        store.add_equity(1_000_000 + i * 900, 10000 + i, 9000, 1000 + i, 10000 + 2 * i, 10000 - i, {})
    tuesday = rig.market.adjusted("SPY")[-1][0]
    store.set_state("bench_base", {"day": tuesday.isoformat(), "equity": 10000.0})
    server = ApiServer(eng, "127.0.0.1", 0)
    await server.start()
    eng.api = server  # as engine.run would
    async with aiohttp.ClientSession(f"http://127.0.0.1:{server.port}") as session:
        yield eng, session, rec
    await server.stop()


async def get(session, path, status=200, **kw):
    async with session.get(path, **kw) as r:
        assert r.status == status, await r.text()
        assert r.headers["Cache-Control"] == "no-store"
        return await r.json()


async def test_index_lists_endpoints(api):
    _, s, _ = api
    d = await get(s, "/api")
    assert d["read_only"] and any("/api/book" in e for e in d["endpoints"])
    assert any("/api/universe" in e for e in d["endpoints"])


async def test_book_matches_the_ledger(api):
    eng, s, _ = api
    d = await get(s, "/api/book")
    v = eng.ledger.value(await eng.market.marks())
    assert d["marked"] and d["equity"] == pytest.approx(v.equity)
    assert d["positions"] == 1 and d["cash"] == str(eng.ledger.cash())  # exact Decimal as a string
    spy, _ = await eng.market.quote("SPY")
    tuesday_close = eng.market.adjusted("SPY")[-1][1]
    assert d["benchmarks"]["spy_hold"] == pytest.approx(10000.0 * spy.mid / tuesday_close)
    assert d["benchmarks"]["sixty_forty"] is not None
    assert d["market"]["open"] is True and d["market"]["session"]["day"] == "2026-10-21"
    assert d["kill_switch"]["effective_dry_run"] is False
    assert d["last_equity_point"]["equity"] == 10029 and d["last_equity_point"]["bench_spy"] == 10058


async def test_positions_carry_marks_and_stop_levels(api):
    eng, s, _ = api
    d = await get(s, "/api/positions")
    (p,) = d["positions"]
    assert p["asset"] == "TLT" and p["class"] == "bond_etf" and float(p["qty"]) > 0
    assert p["bid"] < p["ask"] and p["price_basis"] == "live"
    from ottr.risk.monitor import entry_stop_pct
    vol, _ = await eng.market.vol_regime("TLT")
    assert p["stop_pct"] == pytest.approx(entry_stop_pct(eng.cfg.risk, vol))      # set by the opening BUY
    assert p["trailing_stop"] == pytest.approx(p["high_water"] * (1 - p["stop_pct"]))
    assert p["cap_pct"] == 25.0 and p["risk_blind_since"] is None


async def test_book_survives_a_market_outage(api):
    eng, s, _ = api

    async def outage():
        raise MarketDataError("scripted outage")

    eng.market.marks = outage
    d = await get(s, "/api/book")
    assert d["marked"] is False and d["equity"] is None and "scripted outage" in d["prices_error"]


async def test_a_slow_feed_answers_unmarked_instead_of_hanging(api, monkeypatch):
    eng, s, _ = api
    monkeypatch.setattr("ottr.api.PRICES_TIMEOUT", 0.1)

    async def slow():
        await asyncio.sleep(2.0)
        return {}

    eng.market.marks = slow
    t0 = time.time()
    d = await get(s, "/api/book")
    assert time.time() - t0 < 1.5 and d["marked"] is False and "longer than" in d["prices_error"]


async def test_equity_curve_downsamples_and_keeps_the_last_point(api):
    _, s, _ = api
    full = await get(s, "/api/equity")
    assert full["count"] == 30 and full["returned"] == 30
    assert set(full["points"][0]) == {"ts", "equity", "cash", "invested", "bench_spy", "bench_60_40", "after_tax"}
    d = await get(s, "/api/equity?max_points=7")
    assert d["returned"] <= 7 and d["points"][-1]["ts"] == full["points"][-1]["ts"]
    d = await get(s, f"/api/equity?since={1_000_000 + 900 * 25}")
    assert d["count"] == 5
    await get(s, "/api/equity?max_points=1", status=400)
    await get(s, "/api/equity?since=nan", status=400)


async def test_fills_paginate(api):
    eng, s, _ = api
    with eng.store.tx():
        eng.ledger.apply_fill(idem_key="x", asset="TLT", side="SELL", quantity="1", price="90",
                              quote_source="alpaca", origin="operator", now=OPEN + 60)
    d = await get(s, "/api/fills?limit=1")
    assert len(d["fills"]) == 1 and d["fills"][0]["side"] == "SELL" and d["next_before"]
    d2 = await get(s, f"/api/fills?limit=1&before={d['next_before']}")
    assert d2["fills"][0]["side"] == "BUY" and d2["fills"][0]["origin"] == "council"
    await get(s, "/api/fills?limit=0", status=400)


async def test_meetings_show_every_analyst_stance_and_the_gate_result(api):
    eng, s, rec = api
    d = await get(s, "/api/meetings")
    (m,) = d["meetings"]
    assert m["id"] == rec["id"] and m["summary"] == "buy tlt"
    assert set(m["analysts"]) == {"claude", "gpt", "gemini"}
    claude = {x["asset"]: x for x in m["analysts"]["claude"]["stances"]}
    assert claude["TLT"]["stance"] == "BUY" and claude["TLT"]["conviction"] == 0.8
    tlt, _ = await eng.market.quote("TLT")
    assert claude["TLT"]["price"] == pytest.approx(tlt.mid)
    assert m["analysts"]["claude"]["label"] in ("A", "B", "C")
    (order,) = m["orders"]
    assert (order["asset"], order["side"], order["status"]) == ("TLT", "BUY", "filled")
    assert "packet" not in m  # the list stays light
    full = await get(s, f"/api/meetings/{rec['id']}")
    assert full["packet"].startswith("AS OF Wed 2026-10-21 11:00 ET.") and full["decision"]["trades"]
    await get(s, "/api/meetings/m-nope", status=404)
    await get(s, "/api/meetings/bad%20id%27", status=400)


async def test_audit_filters_by_kind(api):
    _, s, _ = api
    d = await get(s, "/api/audit?kind=meeting")
    assert d["audit"] and all(a["kind"] == "meeting" for a in d["audit"])
    await get(s, "/api/audit?kind=DROP%20TABLE", status=400)


async def test_health_and_heartbeats(api):
    eng, s, _ = api
    eng.store.heartbeat("risk", True)
    d = await get(s, "/api/health")
    assert {"alarms", "tasks", "active_alarms", "kill_switch", "exit_code"} <= set(d)
    council = next(t for t in d["tasks"] if t["task"] == "council")
    assert council["expect"] == "at 10:15 and 15:15 ET on trading days" and council["stale_after_sec"] is None
    hb = await get(s, "/api/heartbeats")
    risk = next(t for t in hb["tasks"] if t["task"] == "risk")
    assert risk["ok"] is True and risk["stale_after_sec"] == 180


async def test_the_universe_lists_every_member(api):
    _, s, _ = api
    d = await get(s, "/api/universe")
    m = d["members"]
    assert len(m) == 16 and d["cash_symbol"] == "SGOV" and "RSP" in d["reference"]
    assert m["SGOV"]["cap_pct"] is None and m["EEM"]["cap_pct"] == 15.0 and m["SPY"]["cap_pct"] == 25.0
    assert m["PDBC"]["fractionable"] is False and m["SPY"]["fractionable"] is True
    assert all(x["buyable"] for x in m.values())


async def test_unknown_route_is_json_404(api):
    _, s, _ = api
    d = await get(s, "/api/nope", status=404)
    assert d["status"] == 404
    # /api/screen is the US stock screen now (the crypto screener's route is gone); before
    # the first screen it answers with an empty membership, not an error.
    d = await get(s, "/api/screen")
    assert (d["screen"], d["satellites"], d["schedule"]) == (None, [], "Sat 08:00 ET weekly")


async def test_foreign_host_header_is_refused(api):
    # DNS rebinding: evil.example resolves to 127.0.0.1 and the browser sends its Host.
    _, s, _ = api
    d = await get(s, "/api/book", status=403, headers={"Host": "evil.example:8765"})
    assert "loopback" in d["error"]
    await get(s, "/api/book", headers={"Host": "localhost"})


async def test_api_reads_through_a_read_only_connection(api):
    eng, _, _ = api
    reader = eng.api._reader
    assert reader.writer is False and reader.conn is not eng.store.conn
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.conn.execute("DELETE FROM fills")


async def post_kill(session, body=None, key=KEY, status=200):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with session.post("/api/kill-switch", json=body, headers=headers) as r:
        assert r.status == status, await r.text()
        return await r.json()


async def test_kill_switch_requires_the_key(api):
    _, s, _ = api
    await post_kill(s, key=None, status=401)
    await post_kill(s, key="wrong-key-wrong-key", status=401)
    async with s.post("/api/kill-switch?key=" + KEY) as r:  # never accepted in a URL
        assert r.status == 401


async def test_kill_switch_stops_fills_and_persists(api, cfg, store):
    eng, s, _ = api
    d = await post_kill(s)  # empty body = panic button: engage
    assert d["effective_dry_run"] and d["operator_engaged"] and d["by"] == "api"
    v = await eng.executor.process(Intent(origin="council", asset="GLD", side="BUY", conviction=1.0,
                                          idem_key="after-kill", created_ts=OPEN))
    assert v.approved and "GLD" not in eng.ledger.positions()  # verdict recorded, nothing filled
    assert KillSwitch(cfg, store).engaged  # a restarted engine comes back engaged
    assert store.audits("kill_switch")[0]["by"] == "api"
    d = await post_kill(s, {"engaged": False, "reason": "back to normal"})
    assert d["effective_dry_run"] is False
    v = await eng.executor.process(Intent(origin="council", asset="GLD", side="BUY", conviction=1.0,
                                          idem_key="after-release", created_ts=OPEN))
    assert v.approved and "GLD" in eng.ledger.positions()


async def test_kill_switch_cannot_release_a_config_dry_run(api):
    eng, s, _ = api
    eng.kill.cfg = dataclasses.replace(eng.cfg, trading=dataclasses.replace(eng.cfg.trading, dry_run=True))
    d = await post_kill(s, {"engaged": False})
    assert d["operator_engaged"] is False and d["effective_dry_run"] is True


async def test_kill_switch_validates_the_body(api):
    _, s, _ = api
    await post_kill(s, {"engaged": "yes"}, status=400)
    await post_kill(s, ["engaged"], status=400)
    await post_kill(s, {"engaged": True, "reason": "x" * 501}, status=400)
    async with s.post("/api/kill-switch", data=b"{not json",
                      headers={"Authorization": f"Bearer {KEY}"}) as r:
        assert r.status == 400


async def test_kill_switch_disabled_without_a_key(cfg, store, ledger):
    eng, _ = await make_engine(cfg, store)
    server = ApiServer(eng, "127.0.0.1", 0)
    await server.start()
    try:
        async with aiohttp.ClientSession(f"http://127.0.0.1:{server.port}") as s:
            d = await post_kill(s, key="anything-anything", status=503)
            assert "OTTR_API_KEY" in d["error"]
    finally:
        await server.stop()


async def post_call(session, body=None, key=KEY, status=200):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with session.post("/api/meeting-call", json=body, headers=headers) as r:
        assert r.status == status, await r.text()
        return await r.json()


@pytest.fixture
async def call_api(cfg, store, ledger):
    """The API after a council meeting at 11:00, like `api`, with the market rig in hand (a
    called meeting needs quotes as fresh as the moved clock) and the review a called meeting
    starts afterwards failing quietly."""
    cfg = dataclasses.replace(cfg, api_key=KEY)
    eng, rig = await make_engine(cfg, store)
    await eng.meeting(execute=True)
    await eng.executor.drain()
    eng.watcher_model = ScriptedModel("watcher", [ModelError("no review in this test")])
    server = ApiServer(eng, "127.0.0.1", 0)
    await server.start()
    async with aiohttp.ClientSession(f"http://127.0.0.1:{server.port}") as session:
        yield eng, rig, session
    await server.stop()


def nothing_called(eng, store):
    return eng.watch_state().pending is None and not eng._call.is_set() and store.audits("meeting_called") == []


async def test_meeting_call_requires_the_key(api, store):
    eng, s, _ = api
    eng.clock.advance(46 * 60)                              # past the gap, so only the key is in the way
    await post_call(s, {"why": "TLT broke"}, key=None, status=401)
    await post_call(s, {"why": "TLT broke"}, key="wrong-key-wrong-key", status=401)
    async with s.post("/api/meeting-call?key=" + KEY, json={"why": "TLT broke"}) as r:  # never in a URL
        assert r.status == 401
    assert nothing_called(eng, store)


async def test_meeting_call_validates_the_body(api, store):
    eng, s, _ = api
    eng.clock.advance(46 * 60)
    for body in (None, {}, {"why": ""}, {"why": "   "}, {"why": 7}, {"why": "x" * 401}, ["why"],
                 {"why": "ok", "by": "not allowed!"}):
        await post_call(s, body, status=400)
    async with s.post("/api/meeting-call", data=b"{not json", headers={"Authorization": f"Bearer {KEY}"}) as r:
        assert r.status == 400
    assert nothing_called(eng, store)


async def test_meeting_call_disabled_without_a_key(cfg, store, ledger):
    eng, _ = await make_engine(cfg, store)
    server = ApiServer(eng, "127.0.0.1", 0)
    await server.start()
    try:
        async with aiohttp.ClientSession(f"http://127.0.0.1:{server.port}") as s:
            d = await post_call(s, {"why": "TLT broke"}, key="anything-anything", status=503)
            assert "OTTR_API_KEY" in d["error"]
            assert (await get(s, "/api/meeting-call"))["allowed"] is True   # the read needs no key
    finally:
        await server.stop()
    assert nothing_called(eng, store)


async def test_meeting_call_is_held_to_the_watchers_limits(call_api, store):
    eng, rig, s = call_api
    # Right after the fixture's meeting: refused, with code's reason, and recorded as refused.
    st = await get(s, "/api/meeting-call")
    assert st["allowed"] is False and st["blocked"] == "the last meeting began 0 minutes ago (at least 45 apart)"
    assert (st["called_today"], st["max_called_per_day"], st["min_gap_min"]) == (0, 4, 45)
    d = await post_call(s, {"why": "TLT broke support", "by": "discord"}, status=409)
    assert d["queued"] is False and d["blocked"] == st["blocked"] and d["error"] == "no meeting: " + st["blocked"]
    (refused,) = store.audits("meeting_call_refused")
    assert (refused["by"], refused["via"], refused["blocked"]) == ("owner", "discord", st["blocked"])
    assert nothing_called(eng, store)

    # 46 minutes on: queued exactly like a watcher's call.
    rig.at(OPEN + 46 * 60)
    assert (await get(s, "/api/meeting-call"))["allowed"] is True
    d = await post_call(s, {"why": "  TLT broke\n support ", "by": "discord"})
    assert d["queued"] is True and d["why"] == "TLT broke support" and d["called_today"] == 0
    assert d["pending"]["by"] == watcher.OWNER and eng._call.is_set()
    (called,) = store.audits("meeting_called")
    assert (called["by"], called["via"], called["why"]) == ("owner", "discord", "TLT broke support")
    # A second call before it starts is refused, and doesn't replace the first.
    d = await post_call(s, {"why": "again"}, status=409)
    assert d["blocked"] == "a meeting called by the owner is about to start"
    assert eng.watch_state().pending["why"] == "TLT broke support"

    # The council loop runs it as a called meeting, and the day's count includes it.
    eng.council = buy_council(eng.cfg, "GLD")               # the 11:00 meeting used the first script
    eng._call.clear()
    await eng._called_meeting()
    if eng._review_task is not None:
        await asyncio.gather(eng._review_task, return_exceptions=True)
    rec = json.loads(store.conn.execute("SELECT record FROM meetings ORDER BY ts DESC LIMIT 1").fetchone()[0])
    assert rec["called"] == {"by": "owner", "why": "TLT broke support"}
    assert rec["context"].startswith("WHY THIS MEETING: called at 11:46 ET, outside the schedule, by the desk "
                                     "owner, from chat: TLT broke support")
    st = await get(s, "/api/meeting-call")
    assert st["called_today"] == 1 and st["pending"] is None
    assert st["blocked"] == "the last meeting began 0 minutes ago (at least 45 apart)"


async def test_meeting_call_shares_the_daily_cap_and_the_session(api, store):
    eng, s, _ = api
    eng.clock.advance(46 * 60)
    st = eng.watch_state()
    st.called = [{"ts": eng.clock.time() - 600 - i, "ran": True, "by": "watcher", "why": "x"} for i in range(4)]
    eng._save_watch(st)
    d = await post_call(s, {"why": "one more"}, status=409)
    assert d["blocked"] == "today's 4 called meetings are used" and d["called_today"] == 4
    eng._save_watch(watcher.WatchState())
    eng.clock.set(et(2026, 10, 21, 15, 40))                 # 20 minutes to the close
    assert "minutes to the close" in (await post_call(s, {"why": "late"}, status=409))["blocked"]
    eng.clock.set(et(2026, 10, 21, 17))
    assert (await post_call(s, {"why": "after hours"}, status=409))["blocked"] == "the market is closed"
    eng.clock.set(OPEN + 2 * 3600)
    eng.meeting_started = eng.clock.time()                  # a meeting under way
    assert (await post_call(s, {"why": "now"}, status=409))["blocked"] == "a meeting is running"
    eng.meeting_started = None
    assert nothing_called(eng, store) and len(store.audits("meeting_call_refused")) == 4


async def test_council_says_when_a_meeting_is_running(api):
    eng, s, rec = api
    d = await get(s, "/api/council")
    assert d["running"] is False and d["started_at"] is None and d["meeting_times_et"] == ["10:15", "15:15"]
    assert d["last_meeting"]["id"] == rec["id"] and d["last_meeting"]["status"] == "ok"
    eng.meeting_started = time.time() - 30
    d = await get(s, "/api/council")
    assert d["running"] is True and d["running_sec"] >= 30


async def test_a_meeting_flags_itself_only_while_it_runs(cfg, store, ledger):
    eng, _ = await make_engine(cfg, store)
    seen = []
    real = eng.council.run

    async def spy(*a, **kw):
        seen.append(eng.meeting_started)
        return await real(*a, **kw)

    eng.council.run = spy
    await eng.meeting(execute=False)
    assert seen and seen[0] is not None and eng.meeting_started is None

    async def outage(*a, **kw):
        raise MarketDataError("scripted outage")

    eng.market.prices = outage
    with pytest.raises(MarketDataError):
        await eng.meeting(execute=False)
    assert eng.meeting_started is None  # cleared on failure too


async def test_kill_switch_records_who_asked(api, store):
    _, s, _ = api
    d = await post_kill(s, {"engaged": True, "reason": "bed", "by": "discord"})
    assert d["by"] == "discord" and store.audits("kill_switch")[0]["by"] == "discord"
    await post_kill(s, {"engaged": True, "by": "not allowed!"}, status=400)
    await post_kill(s, {"engaged": True, "by": 7}, status=400)
