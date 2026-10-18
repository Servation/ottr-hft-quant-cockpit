"""
Supervisor: keeps `ottr run` alive, restarts it with backoff, and says so on Discord.

What it does: runs the engine as a child process (hidden, in a kill-on-close Job
Object, output appended to logs/engine.out.log), waits for it, and restarts it after
any exit that wasn't requested: 5s, then doubling to 5 min while it keeps crashing,
back to 5s once a run lasted 10 min. A stop request (`ottr service stop`, or Ctrl+C
here) reaches the engine through the stop file, so it drains its order queue and exits
cleanly; after a grace period the supervisor kills it instead.

Why a supervisor process when Task Scheduler has "restart on failure": that setting
only covers a task that fails to launch, not a program that starts and later exits
with an error, so an engine crash would stay down. Task Scheduler's job is reduced to
"keep one supervisor running" (logon trigger + a 5 min keepalive repeat), and the
restart policy lives here where it is deterministic and tested. A lock file keeps a
second supervisor from starting a second engine.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from ottr.control import (clear_stale_stop, clear_stop, install_stop_signals, request_stop,
                          restore_signals, stop_requested)
from ottr.health import fmt_age
from ottr.ledger.store import LockError, _FileLock
from ottr.notify import Alerts
from ottr.proc import NO_WINDOW, JobObject, kill_tree

logger = logging.getLogger(__name__)

OUT_LOG_MAX = 5 * 1024 * 1024


@dataclass(frozen=True)
class Backoff:
    initial: float = 5.0
    maximum: float = 300.0
    stable_after: float = 600.0   # a run this long counts as healthy: reset the delay

    def next(self, prev: Optional[float], runtime: float) -> float:
        if prev is None or runtime >= self.stable_after:
            return self.initial
        return min(self.maximum, prev * 2)


def _rotate(path: Path, max_bytes: int = OUT_LOG_MAX) -> None:
    try:
        if path.stat().st_size > max_bytes:
            os.replace(path, path.with_name(path.name + ".1"))
    except FileNotFoundError:
        pass


def _last_line(path: Path, offset: int) -> str:
    """Last non-empty line written since `offset` (the child's crash message, usually)."""
    try:
        with open(path, "rb") as f:
            f.seek(max(offset, f.seek(0, os.SEEK_END) - 4096))
            lines = [ln.strip() for ln in f.read().decode("utf-8", "replace").splitlines() if ln.strip()]
    except OSError:
        return ""
    return lines[-1][:300] if lines else ""


class Supervisor:
    def __init__(self, argv: Sequence[str], data_dir: Path, alerts: Optional[Alerts] = None, *,
                 backoff: Backoff = Backoff(), poll: float = 1.0, grace: float = 60.0,
                 out_log: Optional[Path] = None) -> None:
        self.argv: List[str] = list(argv)
        self.data_dir = Path(data_dir)
        self.alerts = alerts or Alerts(None)
        self.backoff, self.poll, self.grace = backoff, poll, grace
        self.out_log = out_log or self.data_dir / "logs" / "engine.out.log"
        self.started = 0.0
        self.spawns = 0
        self.restarts = 0
        self._signalled = False
        self._crashes = 0  # since the last stable run

    # -- stop ------------------------------------------------------------------------------
    def _stop_wanted(self) -> bool:
        return self._signalled or stop_requested(self.data_dir, self.started)

    def _on_signal(self) -> None:
        if not self._signalled:
            logger.info("supervisor signalled: asking the engine to stop")
            self._signalled = True
            request_stop(self.data_dir, "supervisor signal")

    async def _sleep(self, seconds: float) -> bool:
        """Sleep, waking early on a stop request. Returns True if a stop was requested."""
        end = time.time() + seconds
        while time.time() < end:
            if self._stop_wanted():
                return True
            await asyncio.sleep(min(self.poll, max(0.0, end - time.time())))
        return self._stop_wanted()

    # -- child -----------------------------------------------------------------------------
    async def _spawn(self, job: JobObject):
        self.out_log.parent.mkdir(parents=True, exist_ok=True)
        _rotate(self.out_log)
        with open(self.out_log, "ab") as out:
            out.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} supervisor starting engine: "
                      f"{subprocess.list2cmdline(self.argv)}\n".encode("utf-8"))
            out.flush()
            offset = out.tell()  # after our header: the crash tail is the engine's own output
            proc = await asyncio.create_subprocess_exec(
                *self.argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                creationflags=NO_WINDOW)
        job.assign(proc.pid)
        self.spawns += 1
        logger.info("engine started (pid %s)", proc.pid)
        return proc, offset

    async def _wait(self, proc, t0: float) -> int:
        waiter = asyncio.ensure_future(proc.wait())
        stop_seen: Optional[float] = None
        while True:
            done, _ = await asyncio.wait({waiter}, timeout=self.poll)
            if done:
                return waiter.result()
            now = time.time()
            if stop_seen is None and self._stop_wanted():
                stop_seen = now
                logger.info("stop requested; waiting up to %.0fs for a clean engine exit", self.grace)
            if stop_seen is not None and now - stop_seen > self.grace:
                logger.warning("engine did not stop within %.0fs; killing it", self.grace)
                kill_tree(proc)
                stop_seen = float("inf")  # kill once
            if self._crashes and now - t0 >= self.backoff.stable_after:
                logger.info("engine stable again after %d crash(es)", self._crashes)
                self.alerts.event("engine_stable", {"uptime": fmt_age(now - t0), "crashes": self._crashes})
                self._crashes = 0

    async def run(self) -> int:
        lock = _FileLock(self.data_dir / "supervisor.lock")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            lock.acquire()
        except LockError:
            logger.error("another supervisor is already running for %s", self.data_dir)
            return 2
        job = JobObject()
        self.started = time.time()
        clear_stale_stop(self.data_dir, self.started)
        undo = install_stop_signals(self._on_signal)
        delay: Optional[float] = None
        try:
            async with self.alerts.running():
                while not self._stop_wanted():
                    t0 = time.time()
                    error, offset, rc = "", None, None
                    try:
                        proc, offset = await self._spawn(job)
                    except OSError as e:  # e.g. the venv's python.exe vanished: back off and retry
                        logger.exception("could not start the engine")
                        error = f"{type(e).__name__}: {e}"
                    else:
                        rc = await self._wait(proc, t0)
                    runtime = time.time() - t0
                    if self._stop_wanted():
                        logger.info("engine exited (code %s) on request; supervisor done", rc)
                        break
                    delay = self.backoff.next(delay, runtime)
                    self._crashes = 0 if runtime >= self.backoff.stable_after else self._crashes
                    self._crashes += 1
                    self.restarts += 1
                    tail = _last_line(self.out_log, offset) if offset is not None else ""
                    logger.error("engine %s; restarting in %s. Last output: %s",
                                 f"failed to start ({error})" if error else
                                 f"exited with code {rc} after {fmt_age(runtime)}",
                                 fmt_age(delay), tail or "(none)")
                    self.alerts.event("engine_crash", {"rc": rc, "error": error, "runtime": fmt_age(runtime),
                                                       "delay": fmt_age(delay), "tail": tail},
                                      key="engine_crash")
                    if await self._sleep(delay):
                        break
        finally:
            restore_signals(undo)
            clear_stop(self.data_dir)
            job.close()
            lock.release()
        return 0
