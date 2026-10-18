"""
Process helpers for Windows-hosted subprocesses: no console windows, whole-tree kills,
and a kill-on-close Job Object.

What it does: `NO_WINDOW` keeps console children (the engine, the model CLIs) from
popping windows on the desktop when OTTR runs hidden under Task Scheduler. `kill_tree`
ends a process and everything it spawned. `JobObject` ties children to the supervisor's
lifetime: when the supervisor dies for any reason, even TerminateProcess from Task
Scheduler's "End", Windows closes its handle and kills the engine and its model CLIs,
so no orphaned engine keeps the writer lock while a new one fails to start.
"""

from __future__ import annotations

import logging
import os
import subprocess

logger = logging.getLogger(__name__)

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def kill_tree(proc) -> None:
    """Kill `proc` (asyncio or subprocess.Popen) and its descendants. Idempotent."""
    if getattr(proc, "returncode", None) is not None:
        return
    if os.name == "nt":
        # Launchers (venv python.exe, .cmd shims) run the real work in a child that
        # proc.kill() would leave behind. taskkill /T walks the tree.
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


class JobObject:
    """A Windows Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. No-op elsewhere."""

    def __init__(self) -> None:
        self.handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        k32.CloseHandle.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._k32, self._ctypes = k32, ctypes

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        handle = k32.CreateJobObjectW(None, None)
        if not handle:
            logger.warning("CreateJobObject failed (%s); children won't die with the supervisor",
                           ctypes.get_last_error())
            return
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            logger.warning("SetInformationJobObject failed (%s)", ctypes.get_last_error())
            k32.CloseHandle(handle)
            return
        self.handle = handle

    def assign(self, pid: int) -> bool:
        if self.handle is None:
            return False
        PROCESS_SET_QUOTA, PROCESS_TERMINATE = 0x0100, 0x0001
        h = self._k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not h:
            logger.warning("OpenProcess(%s) failed (%s)", pid, self._ctypes.get_last_error())
            return False
        try:
            if not self._k32.AssignProcessToJobObject(self.handle, h):
                logger.warning("AssignProcessToJobObject(%s) failed (%s)", pid, self._ctypes.get_last_error())
                return False
            return True
        finally:
            self._k32.CloseHandle(h)

    def close(self) -> None:
        """Closing the last handle kills every process still in the job."""
        if self.handle is not None:
            self._k32.CloseHandle(self.handle)
            self.handle = None
