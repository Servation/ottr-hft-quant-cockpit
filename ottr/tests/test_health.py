"""
Health tests: heartbeat staleness (incl. "fresh but failing"), engine-down detection,
risk-blind and ledger alarms, exit codes, watchdog hysteresis, and the schema migration
that added heartbeats.ok_ts to an existing book.
"""

import sqlite3
import time

import pytest

from ottr import health
from ottr.health import Alarm, Watchdog, task_health
from ottr.ledger.book import Ledger
from ottr.ledger.store import SCHEMA_VERSION, SchemaError, Store

NOW = 1_800_000_000.0


def hb(ts, ok=True, ok_ts=None, detail=""):
    return {"ts": ts, "ok": ok, "ok_ts": ts if ok and ok_ts is None else ok_ts, "detail": detail}


def test_task_health_states():
    iv = 60.0
    assert task_health("risk", iv, hb(NOW - 30), NOW, epoch=NOW - 3600).healthy
    stalled = task_health("risk", iv, hb(NOW - 400), NOW, epoch=NOW - 3600)
    assert not stalled.healthy and "stalled" in stalled.problem
    # Writes a heartbeat every tick, but every tick fails: the case `ts` alone would miss.
    failing = task_health("risk", iv, hb(NOW - 10, ok=False, ok_ts=NOW - 900, detail="boom"), NOW, epoch=NOW - 3600)
    assert not failing.healthy and "failing for 15m: boom" in failing.problem
    never = task_health("council", 4 * 3600, None, NOW, epoch=NOW - 13 * 3600)
    assert not never.healthy and "has not reported since the engine started" in never.problem


def test_restart_gets_a_fresh_grace_period():
    # The previous run's heartbeat is a day old, but this run started 2 minutes ago.
    t = task_health("council", 4 * 3600, hb(NOW - 86400), NOW, epoch=NOW - 120)
    assert t.healthy
    t = task_health("risk", 60, hb(NOW - 86400), NOW, epoch=NOW - 120)
    assert t.healthy  # 120s < 3 x 60s
    assert not task_health("risk", 60, hb(NOW - 86400), NOW, epoch=NOW - 200).healthy


def _engine_running(store, epoch):
    store.set_state("engine", {"running": True, "epoch": epoch, "started_at": epoch})


def _all_fresh(store):
    for t in ("watchdog", "market", "risk", "equity", "council"):
        store.heartbeat(t, True)


def test_outside_view_healthy_engine_exits_0(cfg, store, ledger):
    _engine_running(store, time.time() - 30)
    _all_fresh(store)
    rep = health.evaluate(cfg, store, inside_engine=False)
    assert rep.alarms == [] and rep.engine_alive and rep.exit_code == health.EXIT_OK


def test_engine_down_exit_3_and_task_noise_suppressed(cfg, store, ledger):
    _engine_running(store, time.time() - 7200)
    _all_fresh(store)
    store.conn.execute("UPDATE heartbeats SET ts = ts - 7000, ok_ts = ok_ts - 7000")
    rep = health.evaluate(cfg, store, inside_engine=False)
    assert rep.exit_code == health.EXIT_DOWN
    assert [a.key for a in rep.alarms] == ["engine:down"]  # not five more alarms saying the same
    assert "last heartbeat" in rep.alarms[0].message


def test_clean_stop_reads_as_stopped_not_crashed(cfg, store, ledger):
    store.set_state("engine", {"running": False, "epoch": time.time() - 60})
    rep = health.evaluate(cfg, store, inside_engine=False)
    assert rep.exit_code == health.EXIT_DOWN and "stopped cleanly" in rep.alarms[0].message


def test_one_blind_alarm_for_all_held_assets(cfg, store, ledger):
    now = time.time()
    _engine_running(store, now - 30)
    _all_fresh(store)
    with store.tx():
        for asset, px in (("ETH", "2000"), ("BTC", "80000")):
            ledger.apply_fill(idem_key=f"b-{asset}", asset=asset, side="BUY", quantity="0.01", price=px,
                              fee_pct=0.4, quote_source="kraken", origin="test")
    # SOL isn't held: a stale entry for it is not news.
    store.set_state("risk_blind", {"ETH": now - 300, "BTC": now - 60, "SOL": now - 300})
    rep = health.evaluate(cfg, store, inside_engine=True, epoch=now - 30)
    assert [a.key for a in rep.alarms] == ["risk:blind"] and rep.exit_code == health.EXIT_DEGRADED
    assert rep.alarms[0].message.startswith("risk monitor blind on BTC, ETH for up to 5m")
    assert "trailing stops" in rep.alarms[0].message and rep.blind.keys() == {"BTC", "ETH"}


def test_ledger_problem_wins_exit_2(cfg, store, ledger):
    _engine_running(store, time.time() - 30)
    _all_fresh(store)
    store.conn.execute("UPDATE account SET cash = '999999' WHERE id = 1")  # tampered book
    rep = health.evaluate(cfg, store, inside_engine=False)
    assert rep.exit_code == health.EXIT_LEDGER
    assert any(a.key == "ledger" and "cash" in a.message for a in rep.alarms)


def test_watchdog_raises_at_once_and_clears_with_hysteresis():
    wd = Watchdog(clear_after=300)
    a = Alarm("task:market", "critical", "market failing")
    raised, cleared = wd.update([a], 0)
    assert raised == [a] and cleared == []
    assert wd.update([], 100) == ([], [])          # clean for 100s: still active
    assert wd.update([a], 150) == ([], [])          # came back: the clean clock resets
    assert wd.update([], 200) == ([], [])
    assert wd.update([], 499) == ([], [])
    raised, cleared = wd.update([], 500)            # clean for 300s
    assert cleared == [a] and wd.active == {}


def test_heartbeat_ok_ts_survives_failures(store):
    store.heartbeat("risk", True)
    ok_ts = store.heartbeats()["risk"]["ok_ts"]
    time.sleep(0.01)
    store.heartbeat("risk", False, "MarketDataError: down")
    h = store.heartbeats()["risk"]
    assert h["ok"] is False and h["ok_ts"] == ok_ts and h["ts"] > ok_ts


def _v1_db(path):
    """A database exactly as P1 (schema v1) left it."""
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema_version', '1');
        CREATE TABLE heartbeats (task TEXT PRIMARY KEY, ts REAL NOT NULL, ok INTEGER NOT NULL, detail TEXT);
        INSERT INTO heartbeats VALUES ('risk', 100.0, 1, ''), ('council', 200.0, 0, 'abstain');
    """)
    c.commit()
    c.close()


def test_v1_database_migrates_in_place(tmp_path):
    db = tmp_path / "ottr.db"
    _v1_db(db)
    ro = Store(db, writer=False)  # an older engine still owns it: readers must cope
    assert ro.heartbeats()["risk"]["ok_ts"] == 100.0 and ro.heartbeats()["council"]["ok_ts"] is None
    ro.close()
    s = Store(db, writer=True)
    try:
        assert s.meta("schema_version") == str(SCHEMA_VERSION)
        h = s.heartbeats()
        assert h["risk"]["ok_ts"] == 100.0 and h["council"]["ok_ts"] is None
        assert s.audits("schema_migrated")[0]["from_version"] == 1
        Ledger(s).initialize(10000)  # the rest of the schema was created alongside
    finally:
        s.close()
    s = Store(db, writer=True)  # reopening is a no-op
    assert len(s.audits("schema_migrated")) == 1
    s.close()


def test_newer_schema_is_refused_not_downgraded(tmp_path):
    db = tmp_path / "ottr.db"
    Store(db, writer=True).close()
    c = sqlite3.connect(db)
    c.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    c.commit()
    c.close()
    with pytest.raises(SchemaError):
        Store(db, writer=True)
    # The failed open released the writer lock, so a correct version could still open it.
    c = sqlite3.connect(db)
    c.execute(f"UPDATE meta SET value='{SCHEMA_VERSION}' WHERE key='schema_version'")
    c.commit()
    c.close()
    Store(db, writer=True).close()
