"""
SQLite store: the only persistence layer, with an enforced single writer.

What it does: owns the schema, transactions, and small helpers (audit, heartbeats,
key/value risk state, candles, equity, meetings). The writer holds an OS-level lock file
for its whole life; any other process can only open the DB read-only. Why: v1 kept state
in JSON files that any importer could rewrite (and did), and a corrupt file was silently
replaced by a fresh book. SQLite transactions make a fill + lots + position + cash + order
status + audit row commit together or not at all.

Schema v3 is the US-markets book: FIFO tax lots (`lots`, `lot_closures`), cash in cents,
and fills that carry their trade date, fee breakdown and quote snapshot. v1 and v2 were
the crypto book, which is archived at the cutover rather than converted: its positions,
precision and fee model don't map onto equities. A v3 store refuses them, and it reads
the version BEFORE creating any table, so refusing a file never changes it.

Until the cutover no v3 database exists outside tests, so the PRs leading up to it extend
SCHEMA directly (each table arrives with the code that uses it). After the cutover every
change ships as a numbered MIGRATION so the live book upgrades in place.
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

SCHEMA_VERSION = 3
FIRST_SUPPORTED_VERSION = 3   # v1/v2 were the crypto book: archived, never converted

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cash TEXT NOT NULL, realized_pnl TEXT NOT NULL, fees TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cash_events (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL,
    asset TEXT, qty TEXT, amount TEXT NOT NULL, note TEXT);
-- positions: per-asset summary of the open lots (qty, cost), plus the risk monitor's
-- high-water mark. Kept in step with `lots` inside every fill's transaction.
CREATE TABLE IF NOT EXISTS positions (
    asset TEXT PRIMARY KEY, qty TEXT NOT NULL, cost TEXT NOT NULL,
    high_water REAL NOT NULL, opened_ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, day TEXT NOT NULL, idem_key TEXT NOT NULL UNIQUE,
    asset TEXT NOT NULL, side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    qty TEXT NOT NULL, price TEXT NOT NULL, notional TEXT NOT NULL, fee TEXT NOT NULL,
    fee_detail TEXT NOT NULL, realized_pnl TEXT NOT NULL, quote_source TEXT NOT NULL,
    quote TEXT, origin TEXT NOT NULL, ref TEXT, reason TEXT);
-- One lot per BUY fill (open_fill_id) or per holding brought in at `ottr init` (NULL).
-- qty/cost are as opened; the *_remaining columns shrink as SELLs consume lots oldest
-- first (FIFO).
CREATE TABLE IF NOT EXISTS lots (
    id INTEGER PRIMARY KEY, asset TEXT NOT NULL, open_fill_id INTEGER REFERENCES fills(id),
    opened_ts REAL NOT NULL, opened_day TEXT NOT NULL,
    qty TEXT NOT NULL, cost TEXT NOT NULL, qty_remaining TEXT NOT NULL,
    cost_remaining TEXT NOT NULL, closed_ts REAL);
CREATE INDEX IF NOT EXISTS lots_asset ON lots (asset, id);
-- The part of one lot that one SELL consumed, with the proceeds and cost allocated to it.
CREATE TABLE IF NOT EXISTS lot_closures (
    id INTEGER PRIMARY KEY, lot_id INTEGER NOT NULL REFERENCES lots(id),
    fill_id INTEGER NOT NULL REFERENCES fills(id), day TEXT NOT NULL,
    qty TEXT NOT NULL, proceeds TEXT NOT NULL, cost TEXT NOT NULL, realized TEXT NOT NULL,
    term TEXT NOT NULL CHECK (term IN ('short','long')));
CREATE INDEX IF NOT EXISTS lot_closures_fill ON lot_closures (fill_id);
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
CREATE TABLE IF NOT EXISTS screens (
    id TEXT PRIMARY KEY, ts REAL NOT NULL, result TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS screens_ts ON screens (ts);
"""

# version -> statements that upgrade a database from version-1. SCHEMA above is always the
# latest shape, so a fresh database is created current and skips these. Empty until the
# first post-cutover change (see the module docstring).
MIGRATIONS: Dict[int, Tuple[str, ...]] = {}


class LockError(RuntimeError):
    """Another process already holds the writer lock."""


class SchemaError(RuntimeError):
    """The file isn't a book this code can open: a newer version, an archived crypto book
    (v1/v2), or not an OTTR database at all."""


def read_version(conn: sqlite3.Connection, path: Path) -> Optional[int]:
    """The file's schema version, or None for an empty (new) database. Reads only."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not tables:
        return None
    if "meta" not in tables:
        raise SchemaError(f"{path} is not an OTTR database (it has tables but no meta table)")
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        raise SchemaError(f"{path} has no schema_version: not an OTTR book")
    return int(row[0])


def check_version(have: Optional[int], path: Path) -> None:
    if have is None or FIRST_SUPPORTED_VERSION <= have <= SCHEMA_VERSION:
        return
    if have > SCHEMA_VERSION:
        raise SchemaError(f"{path} is schema v{have}; this OTTR only knows up to v{SCHEMA_VERSION}")
    raise SchemaError(
        f"{path} is a schema v{have} book (the crypto desk). The US desk keeps a fresh v3 book: "
        "archive this file and run `ottr init`. The file was not modified.")


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
            try:
                # Version first: a file this code can't open must leave exactly as it came
                # (no new tables, no WAL switch), so it can still be archived or rolled back.
                have = read_version(self.conn, self.path)
                check_version(have, self.path)
            except BaseException:
                self.close()
                raise
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=FULL")  # a fill must survive a power cut
            self.conn.executescript(SCHEMA)
            self._migrate(have)
        else:
            if not self.path.exists():
                raise FileNotFoundError(f"no OTTR database at {self.path} (run `ottr init`)")
            self.conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True,
                                        isolation_level=None)
            try:
                check_version(read_version(self.conn, self.path), self.path)
            except BaseException:
                self.conn.close()
                raise
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")

    def _migrate(self, have: Optional[int]) -> None:
        if have is None:  # fresh database: SCHEMA already created the latest shape
            self.set_meta("schema_version", str(SCHEMA_VERSION))
            return
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
            self._lock = None

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

    # -- screener (advisory; kept for history so P3 can grade the picks) -----------------
    def save_screen(self, result: Dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO screens(id,ts,result) VALUES(?,?,?)",
                      (result["id"], result["ts"], dumps(result)))

    def latest_screen(self) -> Optional[Dict[str, Any]]:
        r = self.conn.execute("SELECT result FROM screens ORDER BY ts DESC LIMIT 1").fetchone()
        return json.loads(r[0]) if r else None
