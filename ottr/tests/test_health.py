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


def _crypto_book(path, version):
    """A database as the crypto desk left it (schema v1 or v2)."""
    c = sqlite3.connect(path)
    c.executescript(f"""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema_version', '{version}');
        CREATE TABLE account (id INTEGER PRIMARY KEY, cash TEXT, realized_pnl TEXT, fees TEXT);
        INSERT INTO account VALUES (1, '9876.54000000', '0', '0');
    """)
    c.commit()
    c.close()


@pytest.mark.parametrize("version", [1, 2])
def test_a_crypto_book_is_refused_and_left_untouched(tmp_path, version):
    # The US desk starts a fresh v3 book; the crypto book is archived, never converted.
    # The version must be read before any table is created, so the file comes out unchanged.
    db = tmp_path / "ottr.db"
    _crypto_book(db, version)
    before = db.read_bytes()
    with pytest.raises(SchemaError, match=f"schema v{version} book"):
        Store(db, writer=True)
    with pytest.raises(SchemaError, match="was not modified"):
        Store(db, writer=False)
    assert db.read_bytes() == before
    tables = {r[0] for r in sqlite3.connect(db).execute("SELECT name FROM sqlite_master")}
    assert "lots" not in tables and "positions" not in tables
    # The refused open released the writer lock: a fresh book can still be made beside it.
    Store(tmp_path / "fresh.db", writer=True).close()
    Store(db.with_name("other.db"), writer=True).close()


def test_a_non_ottr_database_is_refused(tmp_path):
    db = tmp_path / "ottr.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE something (x)")
    c.commit()
    c.close()
    with pytest.raises(SchemaError, match="not an OTTR database"):
        Store(db, writer=True)


def test_a_fresh_book_is_created_at_the_current_version(tmp_path):
    s = Store(tmp_path / "ottr.db", writer=True)
    try:
        assert s.meta("schema_version") == str(SCHEMA_VERSION) == "4"
        Ledger(s).initialize(10000)
    finally:
        s.close()
    Store(tmp_path / "ottr.db", writer=True).close()  # reopening a current book is a no-op


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


def test_a_v3_book_is_migrated_in_place(tmp_path):
    """The live book was created at v3: opening it with this code adds the quote_quality
    table, keeps every row, and says so in the audit log."""
    db = tmp_path / "ottr.db"
    s = Store(db, writer=True)
    Ledger(s).initialize(10000)
    s.conn.execute("DROP TABLE quote_quality")
    s.conn.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
    s.close()
    ro = Store(db, writer=False)                                        # readers can still open it
    ro.close()
    s = Store(db, writer=True)
    try:
        assert s.meta("schema_version") == "4" and Ledger(s).cash() == 10000 and Ledger(s).check() == []
        assert s.conn.execute("SELECT COUNT(*) FROM quote_quality").fetchone()[0] == 0
        (m,) = s.audits("schema_migrated")
        assert (m["from_version"], m["to_version"]) == (3, 4)
    finally:
        s.close()
