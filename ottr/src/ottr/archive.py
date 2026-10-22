"""
Archive a book: a verified copy of the database (any schema version) with the config, the
logs and a note, and optionally the original set aside so `ottr init` can open a new one.

What it does: takes the writer lock (so the engine must be stopped), copies `ottr.db` with
SQLite's online backup API (a consistent copy, the write-ahead log included), checks the
copy (`PRAGMA integrity_check`, the same tables and row counts as the original), copies
the config file and the logs, and writes NOTE.md: when, from where, the schema version,
the git commit, the last equity point and the copy's SHA-256. With `retire`, the original
database files are then renamed to `ottr.db.retired-<date>` (never deleted), which frees
the data directory for a new book.

Why raw sqlite3 and not the Store: the book being archived is usually one this version
refuses to open (the crypto v2 book at the US cutover). A copy needs no schema knowledge,
and opening it read-only can't change it.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from ottr.ledger.store import LockError, _FileLock


class ArchiveError(RuntimeError):
    pass


@dataclass(frozen=True)
class Archived:
    dest: Path
    schema_version: Optional[str]
    tables: Dict[str, int]
    last_equity: Optional[float]
    sha256: str
    retired: Optional[Path]


def _counts(conn: sqlite3.Connection) -> Dict[str, int]:
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}


def _one(conn: sqlite3.Connection, sql: str):
    try:
        r = conn.execute(sql).fetchone()
    except sqlite3.Error:
        return None
    return r[0] if r else None


def _commit(repo: Path) -> str:
    try:
        out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def archive(data_dir: Path, dest: Path, config_path: Path, retire: bool = False,
            now: Optional[datetime] = None) -> Archived:
    now = now or datetime.now().astimezone()
    db = data_dir / "ottr.db"
    if not db.exists():
        raise ArchiveError(f"no database at {db}")
    if dest.exists() and any(dest.iterdir()):
        raise ArchiveError(f"{dest} exists and isn't empty: an archive is never overwritten")
    lock = _FileLock(data_dir / "ottr.lock")
    try:
        lock.acquire()
    except LockError:
        raise ArchiveError("the engine (or another ottr command) holds the writer lock: stop it first "
                           "(`ottr service stop`)") from None
    try:
        dest.mkdir(parents=True, exist_ok=True)
        copy = dest / "ottr.db"
        src = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        dst = sqlite3.connect(copy)
        try:
            src.backup(dst)
            check = dst.execute("PRAGMA integrity_check").fetchone()[0]
            original, copied = _counts(src), _counts(dst)
            version = _one(dst, "SELECT value FROM meta WHERE key='schema_version'")
            equity = _one(dst, "SELECT equity FROM equity ORDER BY ts DESC LIMIT 1")
        finally:
            dst.close()
            src.close()
        if check != "ok" or original != copied:
            raise ArchiveError(f"the copy doesn't match the original (integrity: {check}; tables "
                               f"{original} vs {copied}); nothing was retired")
        digest = hashlib.sha256(copy.read_bytes()).hexdigest()
        if config_path.exists():
            shutil.copy2(config_path, dest / config_path.name)
        if (data_dir / "logs").is_dir():
            shutil.copytree(data_dir / "logs", dest / "logs")
        retired = None
        if retire:
            stamp = now.strftime("%Y-%m-%d")
            retired = data_dir / f"ottr.db.retired-{stamp}"
            for suffix in ("", "-wal", "-shm"):
                part = data_dir / f"ottr.db{suffix}"
                if part.exists():
                    part.rename(data_dir / f"ottr.db.retired-{stamp}{suffix}")
        (dest / "NOTE.md").write_text("\n".join([
            f"# OTTR book archived {now.isoformat(timespec='seconds')}",
            "",
            f"- From: {db}",
            f"- Schema version: {version or 'unknown'}",
            f"- Code commit: {_commit(data_dir.parent)}",
            f"- Last equity point: {f'${equity:,.2f}' if equity is not None else 'none'}",
            f"- Copy: ottr.db, SHA-256 {digest}, integrity check ok, {sum(copied.values()):,} rows in "
            f"{len(copied)} tables",
            f"- Original: {'renamed to ' + retired.name if retired else 'left in place'}",
            "",
            "Restore: stop the engine, copy ottr.db back into the data directory, and check out the",
            "commit above (its code is the one that reads this schema).",
            ""]), encoding="utf-8")
        return Archived(dest, version, copied, equity, digest, retired)
    finally:
        lock.release()
