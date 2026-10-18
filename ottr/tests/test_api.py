"""
API tests against a real aiohttp server on an ephemeral loopback port: every read
endpoint, input validation, the DNS-rebinding Host check, the read-only connection, and
the kill switch (auth, persistence, effect on the executor, config-wins rule).
"""

import dataclasses
import sqlite3
import time

import aiohttp
import pytest

from ottr.api import ApiServer
from ottr.engine.engine import Engine
from ottr.engine.killswitch import KillSwitch
from ottr.risk.types import Intent
from tests.fakes import PRICES, FakeMarket, eth_buy_council

KEY = "test-api-key-0123456789"


@pytest.fixture
async def api(cfg, store, ledger):
    cfg = dataclasses.replace(cfg, api_key=KEY)
    market = FakeMarket(cfg, PRICES)
    eng = Engine(cfg, store, market, council=eth_buy_council(cfg))
    rec = await eng.meeting(execute=True)
    await eng.executor.drain()
    for i in range(30):
        store.add_equity(1_000_000 + i * 900, 10000 + i, 9000, 1000 + i, 10000 + 2 * i, 10000 - i, {})
    store.set_state("bench_base", {"ts": 1, "equity": 10000.0, "prices": dict(PRICES)})
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


async def test_book_matches_the_ledger(api):
    eng, s, _ = api
    d = await get(s, "/api/book")
    v = eng.ledger.value(await eng.market.quotes())
    assert d["marked"] and d["equity"] == pytest.approx(v.equity)
    assert d["positions"] == 1 and d["cash"] == str(eng.ledger.cash())  # exact Decimal as a string
    assert d["benchmarks"]["btc_hold"] == pytest.approx(10000.0, rel=1e-3)
    assert d["kill_switch"]["effective_dry_run"] is False
    assert d["last_equity_point"]["equity"] == 10029


async def test_positions_carry_marks_and_stop_levels(api):
    eng, s, _ = api
    d = await get(s, "/api/positions")
    (p,) = d["positions"]
    assert p["asset"] == "ETH" and float(p["qty"]) > 0
    assert p["bid"] < p["ask"] and p["trailing_stop"] == pytest.approx(p["high_water"] * 0.9)
    assert p["cap_pct"] == 35.0 and p["risk_blind_since"] is None


async def test_book_survives_a_market_outage(api):
    eng, s, _ = api
    eng.market.fail = True
    d = await get(s, "/api/book")
    assert d["marked"] is False and d["equity"] is None and "scripted outage" in d["quotes_error"]


async def test_slow_exchange_answers_unmarked_instead_of_hanging(api, monkeypatch):
    eng, s, _ = api
    monkeypatch.setattr("ottr.api.QUOTES_TIMEOUT", 0.1)
    eng.market.delay = 2.0
    t0 = time.time()
    d = await get(s, "/api/book")
    assert time.time() - t0 < 1.5 and d["marked"] is False and "longer than" in d["quotes_error"]


async def test_equity_curve_downsamples_and_keeps_the_last_point(api):
    _, s, _ = api
    full = await get(s, "/api/equity")
    assert full["count"] == 30 and full["returned"] == 30
    assert set(full["points"][0]) == {"ts", "equity", "cash", "invested", "bench_btc", "bench_ew"}
    d = await get(s, "/api/equity?max_points=7")
    assert d["returned"] <= 7 and d["points"][-1]["ts"] == full["points"][-1]["ts"]
    d = await get(s, f"/api/equity?since={1_000_000 + 900 * 25}")
    assert d["count"] == 5
    await get(s, "/api/equity?max_points=1", status=400)
    await get(s, "/api/equity?since=nan", status=400)


async def test_fills_paginate(api):
    eng, s, _ = api
    with eng.store.tx():
        eng.ledger.apply_fill(idem_key="x", asset="ETH", side="SELL", quantity="0.01", price="2400",
                              fee_pct=0.4, quote_source="kraken", origin="operator")
    d = await get(s, "/api/fills?limit=1")
    assert len(d["fills"]) == 1 and d["fills"][0]["side"] == "SELL" and d["next_before"]
    d2 = await get(s, f"/api/fills?limit=1&before={d['next_before']}")
    assert d2["fills"][0]["side"] == "BUY" and d2["fills"][0]["origin"] == "council"
    await get(s, "/api/fills?limit=0", status=400)


async def test_meetings_show_every_analyst_stance_and_the_gate_result(api):
    _, s, rec = api
    d = await get(s, "/api/meetings")
    (m,) = d["meetings"]
    assert m["id"] == rec["id"] and m["summary"] == "buy eth"
    assert set(m["analysts"]) == {"claude", "gpt", "gemini"}
    claude = {x["asset"]: x for x in m["analysts"]["claude"]["stances"]}
    assert claude["ETH"]["stance"] == "BUY" and claude["ETH"]["conviction"] == 0.8
    assert claude["ETH"]["price"] == pytest.approx(PRICES["ETH"])
    assert m["analysts"]["claude"]["label"] in ("A", "B", "C")
    (order,) = m["orders"]
    assert (order["asset"], order["side"], order["status"]) == ("ETH", "BUY", "filled")
    assert "packet" not in m  # the list stays light
    full = await get(s, f"/api/meetings/{rec['id']}")
    assert full["packet"].startswith("AS OF:") and full["decision"]["trades"]
    await get(s, "/api/meetings/m-nope", status=404)
    await get(s, "/api/meetings/bad%20id%27", status=400)


async def test_audit_filters_by_kind(api):
    _, s, _ = api
    d = await get(s, "/api/audit?kind=fill")
    assert d["audit"] and all(a["kind"] == "fill" for a in d["audit"])
    await get(s, "/api/audit?kind=DROP%20TABLE", status=400)


async def test_health_and_heartbeats(api):
    eng, s, _ = api
    eng.store.heartbeat("risk", True)
    d = await get(s, "/api/health")
    assert {"alarms", "tasks", "active_alarms", "kill_switch", "exit_code"} <= set(d)
    hb = await get(s, "/api/heartbeats")
    risk = next(t for t in hb["tasks"] if t["task"] == "risk")
    assert risk["ok"] is True and risk["stale_after_sec"] == 180


async def test_unknown_route_is_json_404(api):
    _, s, _ = api
    d = await get(s, "/api/nope", status=404)
    assert d["status"] == 404


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
    v = await eng.executor.process(Intent(origin="council", asset="SOL", side="BUY", conviction=1.0,
                                          idem_key="after-kill", created_ts=time.time()))
    assert v.approved and "SOL" not in eng.ledger.positions()  # verdict recorded, nothing filled
    assert KillSwitch(cfg, store).engaged  # a restarted engine comes back engaged
    assert store.audits("kill_switch")[0]["by"] == "api"
    d = await post_kill(s, {"engaged": False, "reason": "back to normal"})
    assert d["effective_dry_run"] is False
    v = await eng.executor.process(Intent(origin="council", asset="SOL", side="BUY", conviction=1.0,
                                          idem_key="after-release", created_ts=time.time()))
    assert v.approved and "SOL" in eng.ledger.positions()


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
    eng = Engine(cfg, store, FakeMarket(cfg, PRICES), council=eth_buy_council(cfg))
    server = ApiServer(eng, "127.0.0.1", 0)
    await server.start()
    try:
        async with aiohttp.ClientSession(f"http://127.0.0.1:{server.port}") as s:
            d = await post_kill(s, key="anything-anything", status=503)
            assert "OTTR_API_KEY" in d["error"]
    finally:
        await server.stop()
