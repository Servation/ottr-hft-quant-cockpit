"""
Out-of-band control: the stop-request file shared by the engine, supervisor and service.

What it does: `request_stop` drops `<data_dir>/stop.request`; a running engine sees it
within a second and shuts down gracefully (drains queued orders), and the supervisor
then exits instead of restarting it. Why a file and not a signal: on Windows a process
launched hidden by Task Scheduler has no console to receive Ctrl+C, and TerminateProcess
can't be caught, so a file is the one graceful channel that works for every launcher.

A request only applies to processes started BEFORE it (compared by mtime), so a stale
file left by a crash can never stop the next engine the moment it starts.

Also here: installing stop signals (SIGINT/SIGTERM, plus Ctrl+Break on Windows) on the
running loop, with an undo so a finished run never leaves a handler aimed at a closed loop.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path
from typing import Callable, List, Tuple

STOP_FILE = "stop.request"


def stop_path(data_dir: Path) -> Path:
    return Path(data_dir) / STOP_FILE


def request_stop(data_dir: Path, why: str = "operator") -> Path:
    p = stop_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{time.time():.3f} pid={os.getpid()} {why}\n", encoding="utf-8")
    return p


def stop_requested(data_dir: Path, since: float) -> bool:
    """True if a stop was requested at or after `since` (a process's start time)."""
    try:
        return stop_path(data_dir).stat().st_mtime >= since - 1.0  # 1s for coarse mtimes
    except FileNotFoundError:
        return False


def clear_stop(data_dir: Path) -> None:
    try:
        stop_path(data_dir).unlink()
    except FileNotFoundError:
        pass


def clear_stale_stop(data_dir: Path, since: float) -> None:
    """Remove a request older than `since` (left over from an earlier run)."""
    try:
        if stop_path(data_dir).stat().st_mtime < since - 1.0:
            clear_stop(data_dir)
    except FileNotFoundError:
        pass


def install_stop_signals(callback: Callable[[], None]) -> List[Tuple]:
    """Call `callback` on the running loop when a stop signal arrives. Returns the undo."""
    loop = asyncio.get_running_loop()
    undo: List[Tuple] = []
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, callback)
            undo.append(("loop", sig, None))
        except (NotImplementedError, RuntimeError):  # Windows event loops
            try:
                old = signal.signal(sig, lambda s, f: loop.call_soon_threadsafe(callback))
                undo.append(("signal", sig, old))
            except (ValueError, OSError):  # not on the main thread
                pass
    return undo


def restore_signals(undo: List[Tuple]) -> None:
    loop = asyncio.get_running_loop()
    for kind, sig, old in undo:
        if kind == "loop":
            loop.remove_signal_handler(sig)
        else:  # None: the old handler wasn't set from Python, so restore the default
            signal.signal(sig, old if old is not None else signal.SIG_DFL)
