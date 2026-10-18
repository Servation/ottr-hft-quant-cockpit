"""
SQLite store: the only persistence layer, with an enforced single writer.

What it does: owns the schema, transactions, and small helpers (audit, heartbeats,
key/value risk state, candles, equity, meetings). The writer holds an OS-level lock file
for its whole life; any other process can only open the DB read-only. Why: v1 kept state
in JSON files that any importer could rewrite (and did), and a corrupt file was silently
replaced by a fresh book. SQLite transactions make a fill + position + cash + order
status + audit row commit together or not at all.

Schema changes ship as numbered MIGRATIONS so an existing book upgrades in place (the
live ledger is never recreated), and a reader tolerates a not-yet-migrated file so
`ottr status` keeps working while an older engine still runs.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cash TEXT NOT NULL, realized_pnl TEXT NOT NULL, fees TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cash_events (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL,
    asset TEXT, qty TEXT, amount TEXT NOT NULL, note TEXT);
CREATE TABLE IF NOT EXISTS positions (
    asset TEXT PRIMARY KEY, qty TEXT NOT NULL, cost TEXT NOT NULL,
    high_water REAL NOT NULL, opened_ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, idem_key TEXT NOT NULL UNIQUE,
    asset TEXT NOT NULL, side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    qty TEXT NOT NULL, price TEXT NOT NULL, notional TEXT NOT NULL, fee TEXT NOT NULL,
    realized_pnl TEXT NOT NULL, quote_source TEXT NOT NULL, origin TEXT NOT NULL,
    ref TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS orders (
    idem_key TEXT PRIMARY KEY, ts REAL NOT NULL, origin TEXT NOT NULL, asset TEXT NOT NULL,
    side TEXT NOT NULL, intent TEXT NOT NULL, status TEXT NOT NULL,
    verdict TEXT, updated_ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY, ts REAL NOT NULL, status TEXT NOT NULL,
    packet TEXT NOT NULL, record TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stances (
    meeting_id TEXT NOT NULL, analyst TEXT NOT NULL, asset TEXT NOT NULL,
    stance TEXT NOT NULL, conviction REAL NOT NULL, price REAL, ts REAL NOT NULL,
    PRIMARY KEY (meeting_id, analyst, asset));
CREATE TABLE IF NOT EXISTS equity (
    ts REAL PRIMARY KEY, equity REAL NOT NULL, cash REAL NOT NULL, invested REAL NOT NULL,
    bench_btc REAL, bench_ew REAL, detail TEXT);
CREATE TABLE IF NOT EXISTS risk_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS audit_kind_ts ON audit (kind, ts);
CREATE TABLE IF NOT EXISTS candles (
    asset TEXT NOT NULL, ts INTEGER NOT NULL, close REAL NOT NULL, PRIMARY KEY (asset, ts));
CREATE TABLE IF NOT EXISTS heartbeats (
    task TEXT PRIMARY KEY, ts REAL NOT NULL, ok INTEGER NOT NULL, detail TEXT, ok_ts REAL);
"""

# version -> statements that upgrade a database from version-1. SCHEMA above is always
# the latest shape, so a fresh database is created current and skips these.
MIGRATIONS: Dict[int, Tuple[str, ...]] = {
    # ok_ts = last time the task SUCCEEDED. `ts` alone can't tell "running but failing
    # every tick" from healthy, because a failing tick still writes a heartbeat.
    2: ("ALTER TABLE heartbeats ADD COLUMN ok_ts REAL",
        "UPDATE heartbeats SET ok_ts = ts WHERE ok = 1"),
}


class LockError(RuntimeError):
    """Another process already holds the writer lock."""


class SchemaError(RuntimeError):
    """The database was written by a newer OTTR than this code understands."""


class _FileLock:
    """Exclusive, non-blocking OS lock that is released automatically if the process dies."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    def acquire(self) -> None:
        fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            fh.close()
            raise LockError(f"another OTTR writer holds {self.path}") from e
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self._fh.close()
        self._fh = None


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=_json_default, sort_keys=True)


class Store:
    def __init__(self, path: Path, writer: bool) -> None:
        self.path = Path(path)
        self.writer = writer
        self._lock: Optional[_FileLock] = None
        self._depth = 0
        if writer:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._lock = _FileLock(self.path.with_suffix(".lock"))
            self._lock.acquire()
            self.conn = sqlite3.connect(self.path, isolation_level=None)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=FULL")  # a fill must survive a power cut
            self.conn.executescript(SCHEMA)
            self._migrate()
        else:
            if not self.path.exists():
                raise FileNotFoundError(f"no OTTR database at {self.path} (run `ottr init`)")
            self.conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True,
                                        isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")

    def _migrate(self) -> None:
        raw = self.meta("schema_version")
        if raw is None:  # fresh database: SCHEMA already created the latest shape
            self.set_meta("schema_version", str(SCHEMA_VERSION))
            return
        have = int(raw)
        if have > SCHEMA_VERSION:
            self.close()
            raise SchemaError(f"{self.path} is schema v{have}; this OTTR only knows v{SCHEMA_VERSION}")
        if have == SCHEMA_VERSION:
            return
        with self.tx() as c:
            for v in range(have + 1, SCHEMA_VERSION + 1):
                for stmt in MIGRATIONS.get(v, ()):
                    c.execute(stmt)
            c.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))
        self.audit("schema_migrated", from_version=have, to_version=SCHEMA_VERSION)

    def close(self) -> None:
        self.conn.close()
        if self._lock:
            self._lock.release()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- transactions -----------------------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Atomic unit of work. Nested calls join the outer transaction."""
        if not self.writer:
            raise PermissionError("read-only store")
        if self._depth:
            self._depth += 1
            try:
                yield self.conn
            finally:
                self._depth -= 1
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        finally:
            self._depth = 0

    @contextmanager
    def snapshot(self) -> Iterator[sqlite3.Connection]:
        """Consistent multi-query read on a read-only connection (one WAL snapshot).

        Without it each SELECT sees the latest commit, so e.g. cash and positions read a
        moment apart could straddle a fill and show a book that never existed.
        """
        if self.writer:
            raise PermissionError("snapshot() is for read-only stores; the writer uses tx()")
        self.conn.execute("BEGIN")
        try:
            yield self.conn
        finally:
            self.conn.execute("COMMIT")

    # -- small helpers ------------------------------------------------------------------
    def meta(self, key: str) -> Optional[str]:
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def set_meta(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, value))

    def audit(self, kind: str, **data: Any) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO audit(ts,kind,data) VALUES(?,?,?)", (time.time(), kind, dumps(data)))

    def audits(self, kind: Optional[str] = None, since: float = 0.0, limit: int = 100) -> List[Dict[str, Any]]:
        q = "SELECT ts,kind,data FROM audit WHERE ts>=?"
        args: list = [since]
        if kind:
            q += " AND kind=?"
            args.append(kind)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [{"ts": r[0], "kind": r[1], **json.loads(r[2])} for r in self.conn.execute(q, args)]

    def get_state(self, key: str, default: Any = None) -> Any:
        r = self.conn.execute("SELECT value FROM risk_state WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_state(self, key: str, value: Any) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO risk_state(key,value) VALUES(?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, dumps(value)))

    def heartbeat(self, task: str, ok: bool = True, detail: str = "") -> None:
        now = time.time()
        with self.tx() as c:
            c.execute("INSERT INTO heartbeats(task,ts,ok,detail,ok_ts) VALUES(?,?,?,?,?) "
                      "ON CONFLICT(task) DO UPDATE SET ts=excluded.ts, ok=excluded.ok, "
                      "detail=excluded.detail, ok_ts=COALESCE(excluded.ok_ts, heartbeats.ok_ts)",
                      (task, now, int(ok), detail[:500], now if ok else None))

    def heartbeats(self) -> Dict[str, Dict[str, Any]]:
        """task -> {ts (last report), ok, detail, ok_ts (last success, None if never)}."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(heartbeats)")}
        if "ok_ts" not in cols:  # a v1 file an older engine still holds: best effort
            return {r[0]: {"ts": r[1], "ok": bool(r[2]), "detail": r[3], "ok_ts": r[1] if r[2] else None}
                    for r in self.conn.execute("SELECT task,ts,ok,detail FROM heartbeats")}
        return {r[0]: {"ts": r[1], "ok": bool(r[2]), "detail": r[3], "ok_ts": r[4]}
                for r in self.conn.execute("SELECT task,ts,ok,detail,ok_ts FROM heartbeats")}

    # -- candles (CandleArchive) ----------------------------------------------------------
    def load_candles(self, asset: str) -> List[Tuple[int, float]]:
        return [(r[0], r[1]) for r in self.conn.execute(
            "SELECT ts,close FROM candles WHERE asset=? ORDER BY ts", (asset,))]

    def save_candles(self, asset: str, rows: List[Tuple[int, float]]) -> None:
        if not self.writer:
            return
        with self.tx() as c:
            c.executemany("INSERT INTO candles(asset,ts,close) VALUES(?,?,?) "
                          "ON CONFLICT(asset,ts) DO UPDATE SET close=excluded.close",
                          [(asset, ts, close) for ts, close in rows])

    # -- equity ---------------------------------------------------------------------------
    def add_equity(self, ts: float, equity: float, cash: float, invested: float,
                   bench_btc: Optional[float], bench_ew: Optional[float], detail: Dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO equity(ts,equity,cash,invested,bench_btc,bench_ew,detail) "
                      "VALUES(?,?,?,?,?,?,?)", (ts, equity, cash, invested, bench_btc, bench_ew, dumps(detail)))

    def equity_series(self, limit: int = 5000) -> List[Tuple[float, float]]:
        rows = self.conn.execute("SELECT ts,equity FROM equity ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [(r[0], r[1]) for r in reversed(rows)]
