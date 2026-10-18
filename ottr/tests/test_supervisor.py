"""
Supervisor tests with real child processes: restart with backoff, coalesced crash
alerts, graceful stop through the stop file, kill after the grace period, the
single-supervisor lock, and (on Windows) the Job Object killing children when the
supervisor itself is killed.
"""

import asyncio
import os
import subprocess
import sys
import textwrap
import time

import pytest

from ottr.config import Alerts as Limits
from ottr.control import request_stop, stop_path
from ottr.notify import Alerts
from ottr.supervisor import Backoff, Supervisor, _last_line, _rotate

FAST = Backoff(initial=0.05, maximum=0.2, stable_after=30.0)


class Recorder:
    def __init__(self):
        self.posts = []

    async def __call__(self, text):
        self.posts.append(text)
        return True


def py(code: str) -> list:
    return [sys.executable, "-c", textwrap.dedent(code)]


def test_backoff_doubles_to_a_cap_and_resets_after_a_stable_run():
    b = Backoff(initial=5, maximum=300, stable_after=600)
    seq, d = [], None
    for _ in range(9):
        d = b.next(d, runtime=3)
        seq.append(d)
    assert seq == [5, 10, 20, 40, 80, 160, 300, 300, 300]
    assert b.next(300, runtime=601) == 5


async def stop_when(cond, data_dir, timeout=20):
    t0 = time.time()
    while not cond():
        assert time.time() - t0 < timeout, "condition never met"
        await asyncio.sleep(0.05)
    request_stop(data_dir, "test")


async def test_crashing_engine_is_restarted_and_alerts_coalesce(tmp_path):
    rec = Recorder()
    sup = Supervisor(py("import sys; print('boom: ledger not initialized'); sys.exit(3)"), tmp_path,
                     Alerts(rec, Limits()), backoff=FAST, poll=0.05)
    asyncio.get_running_loop().create_task(stop_when(lambda: sup.restarts >= 3, tmp_path))
    assert await asyncio.wait_for(sup.run(), timeout=30) == 0
    assert sup.restarts >= 3
    crashes = [p for p in rec.posts if p.startswith("**Engine exited**")]
    assert len(crashes) == 1  # one post per 30 min, the rest are counted
    assert "code 3" in crashes[0] and "boom: ledger not initialized" in crashes[0]
    assert not stop_path(tmp_path).exists()  # the request was consumed
    log = (tmp_path / "logs" / "engine.out.log").read_text()
    assert log.count("supervisor starting engine") == sup.spawns >= 3


async def test_stop_request_lets_the_engine_exit_cleanly(tmp_path):
    marker = tmp_path / "clean-exit"
    child = py(f"""
        import os, sys, time
        start = time.time()
        while True:  # mimics the engine: watch the stop file, then shut down cleanly
            p = {str(tmp_path / 'stop.request')!r}
            if os.path.exists(p) and os.path.getmtime(p) >= start - 1:
                open({str(marker)!r}, 'w').write('ok'); sys.exit(0)
            time.sleep(0.05)
    """)
    sup = Supervisor(child, tmp_path, backoff=FAST, poll=0.05, grace=10)

    async def later():
        await asyncio.sleep(1.0)
        request_stop(tmp_path)

    asyncio.get_running_loop().create_task(later())
    assert await asyncio.wait_for(sup.run(), timeout=30) == 0
    assert marker.read_text() == "ok" and sup.restarts == 0


async def test_engine_that_ignores_the_stop_is_killed_after_grace(tmp_path):
    sup = Supervisor(py("import time; time.sleep(60)"), tmp_path, backoff=FAST, poll=0.05, grace=0.5)

    async def later():
        await asyncio.sleep(0.5)
        request_stop(tmp_path)

    asyncio.get_running_loop().create_task(later())
    t0 = time.time()
    assert await asyncio.wait_for(sup.run(), timeout=30) == 0
    assert time.time() - t0 < 15


async def test_second_supervisor_refuses_to_start(tmp_path):
    first = Supervisor(py("import time; time.sleep(60)"), tmp_path, backoff=FAST, poll=0.05, grace=0.5)
    task = asyncio.get_running_loop().create_task(first.run())
    await asyncio.sleep(0.5)
    second = Supervisor(py("print('never')"), tmp_path, backoff=FAST, poll=0.05)
    assert await second.run() == 2
    request_stop(tmp_path)
    assert await asyncio.wait_for(task, timeout=30) == 0


async def test_stale_stop_request_does_not_stop_a_new_supervisor(tmp_path):
    request_stop(tmp_path)
    old = time.time() - 600
    os.utime(stop_path(tmp_path), (old, old))
    sup = Supervisor(py("import sys; sys.exit(1)"), tmp_path, backoff=FAST, poll=0.05)
    asyncio.get_running_loop().create_task(stop_when(lambda: sup.restarts >= 1, tmp_path))
    assert await asyncio.wait_for(sup.run(), timeout=30) == 0 and sup.restarts >= 1


def test_last_line_and_rotation(tmp_path):
    log = tmp_path / "out.log"
    log.write_bytes(b"old run\n")
    off = log.stat().st_size
    with open(log, "ab") as f:
        f.write(b"Traceback...\n  File x\nRuntimeError: boom\n\n")
    assert _last_line(log, off) == "RuntimeError: boom"
    assert _last_line(log, log.stat().st_size) == ""
    _rotate(log, max_bytes=10)
    assert not log.exists() and (tmp_path / "out.log.1").exists()


def _alive(pid: int) -> bool:
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    h = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not h:
        return False
    try:
        return k32.WaitForSingleObject(ctypes.c_void_p(h), 0) == 0x102  # WAIT_TIMEOUT: still running
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


@pytest.mark.skipif(os.name != "nt", reason="Job Objects are Windows-only")
def test_job_object_kills_children_when_the_supervisor_dies(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    holder = textwrap.dedent(f"""
        import subprocess, sys, time
        from ottr.proc import JobObject
        job = JobObject()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        assert job.assign(child.pid)
        open({str(pidfile)!r}, "w").write(str(child.pid))
        time.sleep(120)
    """)
    sup = subprocess.Popen([sys.executable, "-c", holder])
    try:
        for _ in range(200):
            if pidfile.exists() and pidfile.read_text():
                break
            time.sleep(0.05)
        grandchild = int(pidfile.read_text())
        assert _alive(grandchild)
        sup.kill()  # TerminateProcess: no cleanup code runs, like Task Scheduler's "End"
        sup.wait(10)
        for _ in range(100):
            if not _alive(grandchild):
                break
            time.sleep(0.05)
        assert not _alive(grandchild), "orphaned engine survived its supervisor"
    finally:
        if sup.poll() is None:
            sup.kill()


async def test_silent_crash_does_not_quote_the_supervisors_own_header(tmp_path):
    # Found in the E2E run: a killed engine prints nothing, and the alert used to quote
    # the supervisor's "starting engine: <argv>" line as if the engine had said it.
    rec = Recorder()
    sup = Supervisor(py("import sys; sys.exit(1)"), tmp_path, Alerts(rec, Limits()), backoff=FAST, poll=0.05)
    asyncio.get_running_loop().create_task(stop_when(lambda: sup.restarts >= 1, tmp_path))
    assert await asyncio.wait_for(sup.run(), timeout=30) == 0
    crash = next(p for p in rec.posts if p.startswith("**Engine exited**"))
    assert "supervisor starting engine" not in crash and "\n>" not in crash


async def test_spawn_failure_backs_off_instead_of_killing_the_supervisor(tmp_path):
    rec = Recorder()
    sup = Supervisor([str(tmp_path / "no-such-python.exe"), "-c", "pass"], tmp_path, Alerts(rec, Limits()),
                     backoff=FAST, poll=0.05)
    asyncio.get_running_loop().create_task(stop_when(lambda: sup.restarts >= 2, tmp_path))
    assert await asyncio.wait_for(sup.run(), timeout=30) == 0
    (post,) = [p for p in rec.posts if p.startswith("**Engine failed to start**")]
    assert "FileNotFoundError" in post and "retries in" in post
