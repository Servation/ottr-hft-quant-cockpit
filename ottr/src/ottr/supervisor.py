"""
Supervisor: keeps `ottr run` alive (and `ottr chat`, when it is set up), restarts each
with backoff, and says so on Discord.

What it does: runs the engine, plus optional companions such as the chat bot, as child
processes (hidden, in one kill-on-close Job Object, output appended to
logs/<name>.out.log), waits for them, and restarts each one after any exit that wasn't
requested: 5s, then doubling to 5 min while it keeps crashing, back to 5s once a run
lasted 10 min. A stop request (`ottr service stop`, or Ctrl+C here) reaches every child
through the stop file, so the engine drains its order queue and exits cleanly; after a
grace period the supervisor kills a child instead.

Why a supervisor process when Task Scheduler has "restart on failure": that setting
only covers a task that fails to launch, not a program that starts and later exits
with an error, so an engine crash would stay down. Task Scheduler's job is reduced to
"keep one supervisor running" (logon trigger + a 5 min keepalive repeat), and the
restart policy lives here where it is deterministic and tested. A lock file keeps a
second supervisor from starting a second engine.

Why the chat bot is a second child here rather than its own scheduled task or a thread
of the engine: each child has its own restart loop, backoff and crash alerts, so a chat
crash (or a crash loop) never restarts or slows the engine, while one service still
starts, stops and cleans up everything. A child can name exit codes that mean "a restart
can't fix this" (the chat bot's bad-token exit): the supervisor then stops restarting
that child, says so once, and keeps the engine running.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Sequence

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


@dataclass(frozen=True)
class Child:
    """One supervised process. `name` labels its logs and alerts ("engine", "chat")."""
    name: str
    argv: Sequence[str]
    out_log: Path
    give_up_codes: FrozenSet[int] = frozenset()   # exits a restart can't fix (bad config)


@dataclass
class ChildState:
    spawns: int = 0
    restarts: int = 0
    crashes: int = 0              # since the last stable run
    delay: Optional[float] = None
    gave_up: bool = False
    pids: List[int] = field(default_factory=list)


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
                 out_log: Optional[Path] = None, companions: Sequence[Child] = ()) -> None:
        self.data_dir = Path(data_dir)
        engine = Child("engine", list(argv), out_log or self.data_dir / "logs" / "engine.out.log")
        self.children: List[Child] = [engine, *companions]
        names = [c.name for c in self.children]
        if len(set(names)) != len(names):
            raise ValueError(f"child names must be unique: {names}")
        self.state: Dict[str, ChildState] = {c.name: ChildState() for c in self.children}
        self.alerts = alerts or Alerts(None)
        self.backoff, self.poll, self.grace = backoff, poll, grace
        self.started = 0.0
        self._signalled = False

    # The engine's counters, as before companions existed.
    @property
    def spawns(self) -> int:
        return self.state["engine"].spawns

    @property
    def restarts(self) -> int:
        return self.state["engine"].restarts

    # -- stop ------------------------------------------------------------------------------
    def _stop_wanted(self) -> bool:
        return self._signalled or stop_requested(self.data_dir, self.started)

    def _on_signal(self) -> None:
        if not self._signalled:
            logger.info("supervisor signalled: asking every child to stop")
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

    # -- one child -------------------------------------------------------------------------
    async def _spawn(self, child: Child, job: JobObject):
        child.out_log.parent.mkdir(parents=True, exist_ok=True)
        _rotate(child.out_log)
        with open(child.out_log, "ab") as out:
            out.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} supervisor starting {child.name}: "
                      f"{subprocess.list2cmdline(list(child.argv))}\n".encode("utf-8"))
            out.flush()
            offset = out.tell()  # after our header: the crash tail is the child's own output
            proc = await asyncio.create_subprocess_exec(
                *child.argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                creationflags=NO_WINDOW)
        job.assign(proc.pid)
        st = self.state[child.name]
        st.spawns += 1
        st.pids.append(proc.pid)
        logger.info("%s started (pid %s)", child.name, proc.pid)
        return proc, offset

    async def _wait(self, child: Child, proc, t0: float) -> int:
        st = self.state[child.name]
        waiter = asyncio.ensure_future(proc.wait())
        stop_seen: Optional[float] = None
        while True:
            done, _ = await asyncio.wait({waiter}, timeout=self.poll)
            if done:
                return waiter.result()
            now = time.time()
            if stop_seen is None and self._stop_wanted():
                stop_seen = now
                logger.info("stop requested; waiting up to %.0fs for a clean %s exit", self.grace, child.name)
            if stop_seen is not None and now - stop_seen > self.grace:
                logger.warning("%s did not stop within %.0fs; killing it", child.name, self.grace)
                kill_tree(proc)
                stop_seen = float("inf")  # kill once
            if st.crashes and now - t0 >= self.backoff.stable_after:
                logger.info("%s stable again after %d crash(es)", child.name, st.crashes)
                self.alerts.event(f"{child.name}_stable", {"uptime": fmt_age(now - t0), "crashes": st.crashes})
                st.crashes = 0

    async def _keep(self, child: Child, job: JobObject) -> None:
        """Run `child` until a stop is requested, restarting it with backoff."""
        st = self.state[child.name]
        while not self._stop_wanted():
            t0 = time.time()
            error, offset, rc = "", None, None
            try:
                proc, offset = await self._spawn(child, job)
            except OSError as e:  # e.g. the venv's python.exe vanished: back off and retry
                logger.exception("could not start %s", child.name)
                error = f"{type(e).__name__}: {e}"
            else:
                rc = await self._wait(child, proc, t0)
            runtime = time.time() - t0
            if self._stop_wanted():
                logger.info("%s exited (code %s) on request", child.name, rc)
                return
            tail = _last_line(child.out_log, offset) if offset is not None else ""
            if rc is not None and rc in child.give_up_codes:
                st.gave_up = True
                logger.error("%s exited with code %s, which a restart can't fix; not restarting it. "
                             "Last output: %s", child.name, rc, tail or "(none)")
                self.alerts.event(f"{child.name}_gave_up", {"rc": rc, "tail": tail})
                return
            st.delay = self.backoff.next(st.delay, runtime)
            st.crashes = 0 if runtime >= self.backoff.stable_after else st.crashes
            st.crashes += 1
            st.restarts += 1
            logger.error("%s %s; restarting in %s. Last output: %s", child.name,
                         f"failed to start ({error})" if error else
                         f"exited with code {rc} after {fmt_age(runtime)}",
                         fmt_age(st.delay), tail or "(none)")
            self.alerts.event(f"{child.name}_crash", {"rc": rc, "error": error, "runtime": fmt_age(runtime),
                                                      "delay": fmt_age(st.delay), "tail": tail},
                              key=f"{child.name}_crash")
            if await self._sleep(st.delay):
                return

    async def _keep_companion(self, child: Child, job: JobObject) -> None:
        # A bug in a companion's loop must not take the supervisor (and so the engine) down.
        try:
            await self._keep(child, job)
        except Exception:
            logger.exception("supervising %s failed; it is no longer restarted", child.name)
            self.state[child.name].gave_up = True

    # -- all children ----------------------------------------------------------------------
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
        try:
            async with self.alerts.running():
                engine, *companions = self.children
                tasks = [asyncio.create_task(self._keep(engine, job), name="keep-engine")]
                tasks += [asyncio.create_task(self._keep_companion(c, job), name=f"keep-{c.name}")
                          for c in companions]
                try:
                    await asyncio.gather(*tasks)
                finally:  # an engine-loop failure ends the supervisor: stop the others first
                    for t in tasks:
                        t.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            logger.info("supervisor done")
        finally:
            restore_signals(undo)
            clear_stop(self.data_dir)
            job.close()
            lock.release()
        return 0
