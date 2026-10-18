"""
The chat bot's own record: every exchange (including refusals) in <data_dir>/chat.db.

What it does: one row per command. A /ask row is written as "pending" the moment its
model call starts and completed afterwards, with the full prompt, the reply, latency and
outcome. The same table answers the two questions the bot asks of its past: "what was
said in this channel" (history replayed into the next prompt, because the model CLIs are
stateless) and "how many model calls in the last hour" (the budget).

Why SQLite and not a log file: both questions are queries, and the budget must survive a
restart, or a crash loop would reset it. A row inserted before the call means a call cut
off by a crash still counts. Why its own file and not the ledger: the bot never opens the
ledger at all (the engine is its single writer); chat.db is written only by the one chat
process (chat.lock), and `ottr status` or a person can read it alongside.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS exchanges (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, channel_id TEXT, user_id TEXT NOT NULL,
    user_name TEXT, command TEXT NOT NULL, input TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending','ok','refused','error')),
    detail TEXT, reply TEXT, model TEXT, effort TEXT,
    model_call INTEGER NOT NULL DEFAULT 0, prompt TEXT, latency_sec REAL, done_ts REAL);
CREATE INDEX IF NOT EXISTS exchanges_history ON exchanges (channel_id, command, status, id);
CREATE INDEX IF NOT EXISTS exchanges_calls ON exchanges (model_call, ts);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


@dataclass(frozen=True)
class Asker:
    """Who sent a command, and where."""
    user_id: int
    name: str
    channel_id: Optional[int]


@dataclass(frozen=True)
class Turn:
    ts: float
    user_name: str
    question: str
    answer: str


class ChatLog:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)  # autocommit; one row per write
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"{self.path} was written by a newer OTTR (schema {version})")
        self.conn.executescript(SCHEMA)
        self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        # A call still "pending" from an earlier run was cut off (crash, kill or restart).
        # It keeps counting against the budget; it just isn't an answer.
        self.conn.execute("UPDATE exchanges SET status='error', detail='interrupted: the chat bot stopped', "
                          "done_ts=? WHERE status='pending'", (self.clock(),))

    def close(self) -> None:
        self.conn.close()

    # -- writes ----------------------------------------------------------------------------
    def _insert(self, asker: Asker, command: str, input: Optional[str], status: str, **cols: Any) -> int:
        cols = {"ts": self.clock(), "channel_id": str(asker.channel_id) if asker.channel_id else None,
                "user_id": str(asker.user_id), "user_name": asker.name, "command": command,
                "input": input, "status": status, **cols}
        if status != "pending":
            cols.setdefault("done_ts", cols["ts"])
        names = ",".join(cols)
        cur = self.conn.execute(f"INSERT INTO exchanges({names}) VALUES({','.join('?' * len(cols))})",
                                tuple(cols.values()))
        return int(cur.lastrowid)

    def record(self, asker: Asker, command: str, input: Optional[str], status: str,
               reply: Optional[str] = None, detail: Optional[str] = None) -> int:
        """A finished exchange that made no model call (a read, a kill, a refusal)."""
        return self._insert(asker, command, input, status, reply=reply, detail=detail)

    def start_call(self, asker: Asker, command: str, input: str, *, model: str, effort: str,
                   prompt: str) -> int:
        """A model call is about to start: it counts against the budget from now on."""
        return self._insert(asker, command, input, "pending", model=model, effort=effort,
                            model_call=1, prompt=prompt)

    def finish(self, row_id: int, status: str, *, reply: Optional[str] = None,
               detail: Optional[str] = None, latency: Optional[float] = None) -> None:
        self.conn.execute("UPDATE exchanges SET status=?, reply=?, detail=?, latency_sec=?, done_ts=? "
                          "WHERE id=?", (status, reply, detail, latency, self.clock(), row_id))

    # -- reads -----------------------------------------------------------------------------
    def calls_since(self, since: float) -> List[float]:
        """Start times of model calls at or after `since`, oldest first."""
        return [r[0] for r in self.conn.execute(
            "SELECT ts FROM exchanges WHERE model_call=1 AND ts>=? ORDER BY ts", (since,))]

    def history(self, channel_id: Optional[int], limit: int) -> List[Turn]:
        """The last `limit` answered /ask exchanges in this channel, oldest first."""
        if limit <= 0 or channel_id is None:
            return []
        rows = self.conn.execute(
            "SELECT ts, user_name, input, reply FROM exchanges WHERE channel_id=? AND command='ask' "
            "AND status='ok' ORDER BY id DESC LIMIT ?", (str(channel_id), limit)).fetchall()
        return [Turn(ts, name or "?", q or "", a or "") for ts, name, q, a in reversed(rows)]

    def recent(self, limit: int = 20) -> List[dict]:
        cur = self.conn.execute("SELECT * FROM exchanges ORDER BY id DESC LIMIT ?", (limit,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # -- small key/value state (e.g. the last command sync) ---------------------------------
    def get_state(self, key: str) -> Any:
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_state(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO state(key, value) VALUES(?, ?)",
                          (key, json.dumps(value, sort_keys=True)))
