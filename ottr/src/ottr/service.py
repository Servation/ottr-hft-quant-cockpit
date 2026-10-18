"""
Windows Task Scheduler integration: `ottr service install|uninstall|status|start|stop|restart`.

What it does: registers one task, "OTTR Engine", that runs `pythonw -m ottr.cli supervise`
as the current user. It fires at that user's logon and then every 5 minutes; the repeat
is a keepalive, because "if already running: do nothing" turns it into "start the
supervisor if it died". The supervisor (ottr/supervisor.py) owns the engine restarts.

Why these settings (each is a way v1-style silent death could come back):
- Runs as the logged-on user (InteractiveToken, no stored password): the claude, codex
  and agy CLIs are signed in under this profile, and some keep credentials where a
  password-less background logon can't decrypt them. The cost: it starts at logon, not
  at boot, so turn on Windows automatic sign-in if the PC must recover from a reboot.
- ExecutionTimeLimit PT0S: the default 72h limit would kill the engine every 3 days.
- Battery settings off: a laptop on battery must not stop the risk monitor.
- pythonw.exe: no console window on the desktop; logs go to <data_dir>/logs.

Every schtasks/PowerShell call goes through a `run` function so tests can check the
exact commands without touching the real scheduler.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional
from xml.sax.saxutils import escape

from ottr.config import ROOT
from ottr.control import request_stop
from ottr.proc import NO_WINDOW

DEFAULT_NAME = "OTTR Engine"
NAME_RE = re.compile(r"^[A-Za-z0-9 _.-]{1,64}$")
KEEPALIVE_MIN = 5

# Task Scheduler "Last Run Result" codes worth naming.
RESULTS = {0: "success", 0x41300: "ready", 0x41301: "currently running", 0x41302: "disabled",
           0x41303: "has not run yet", 0x41306: "terminated by user",
           0x8004131F: "already running (instance ignored)", 0x800710E0: "refused (operator or policy)",
           1: "exited with code 1", 2: "exited with code 2"}

Runner = Callable[[List[str]], subprocess.CompletedProcess]
Out = Callable[[str], None]


def run_cmd(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace", creationflags=NO_WINDOW)


def current_user() -> str:
    # getpass, not os.getlogin(): the latter raises without a controlling terminal.
    user = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{user}" if domain else user


@dataclass(frozen=True)
class TaskSpec:
    name: str
    command: str        # pythonw.exe
    arguments: str
    workdir: str
    user: str
    keepalive_min: int = KEEPALIVE_MIN


def default_spec(config_path: Path, name: str = DEFAULT_NAME,
                 python: Optional[Path] = None) -> TaskSpec:
    python = Path(python or sys.executable)
    pyw = python.with_name("pythonw.exe")
    exe = pyw if pyw.exists() else python
    args = subprocess.list2cmdline(["-m", "ottr.cli", "--config", str(Path(config_path).resolve()), "supervise"])
    return TaskSpec(name=name, command=str(exe), arguments=args, workdir=str(ROOT), user=current_user())


def task_xml(spec: TaskSpec, now: Optional[datetime] = None) -> str:
    """Task Scheduler 1.2 XML, elements in the order Windows itself exports them."""
    e = escape
    date = (now or datetime.now()).replace(microsecond=0).isoformat()
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Date>{date}</Date>
    <Author>{e(spec.user)}</Author>
    <Description>OTTR v2 paper-trading engine: runs the supervisor, which keeps `ottr run` alive. Managed by `ottr service`.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Repetition>
        <Interval>PT{spec.keepalive_min}M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <Enabled>true</Enabled>
      <UserId>{e(spec.user)}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{e(spec.user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{e(spec.command)}</Command>
      <Arguments>{e(spec.arguments)}</Arguments>
      <WorkingDirectory>{e(spec.workdir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _check_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise ValueError(f"task name must match {NAME_RE.pattern}")
    return name


# -- preflight --------------------------------------------------------------------------------
def registry_path() -> Optional[str]:
    """The PATH a Task Scheduler process gets (user + machine, from the registry), which
    can differ from this shell's PATH if a profile script extended it."""
    if os.name != "nt":
        return None
    import winreg
    parts = []
    for root, key in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                      (winreg.HKEY_CURRENT_USER, r"Environment")):
        try:
            with winreg.OpenKey(root, key) as k:
                parts.append(os.path.expandvars(winreg.QueryValueEx(k, "Path")[0]))
        except OSError:
            pass
    return os.pathsep.join(parts) if parts else None


def preflight(analysts: List[str], webhook: Optional[str], path: Optional[str] = None) -> List[str]:
    """Warnings about things that would make the scheduled engine misbehave."""
    from ottr.council.models import CodexCLI
    warns = []
    path = path if path is not None else registry_path()
    exes = {"claude": "claude", "gemini": "agy"}
    for name in analysts:
        if name == "gpt":
            if not CodexCLI._find():
                warns.append("codex CLI not found: the GPT analyst will drop out of every meeting")
        elif name in exes and path is not None and not shutil.which(exes[name], path=path):
            warns.append(f"`{exes[name]}` is not on your user/system PATH (registry), so the scheduled "
                         f"engine won't find it: the {name} analyst would drop out")
    if not webhook:
        warns.append("DISCORD_WEBHOOK_URL is not set (env or ottr/.env): alarms will only reach the log")
    return warns


# -- commands ---------------------------------------------------------------------------------
def query(name: str, run: Runner = run_cmd) -> Optional[dict]:
    """{State, LastRunTime, LastTaskResult, NextRunTime} or None if not registered."""
    script = (
        f"$t = Get-ScheduledTask -TaskName '{_check_name(name)}' -ErrorAction SilentlyContinue; "
        "if (-not $t) { 'null'; exit 0 }; $i = $t | Get-ScheduledTaskInfo; "
        "[pscustomobject]@{State=[string]$t.State; "
        "LastRunTime=$(if ($i.LastRunTime) { $i.LastRunTime.ToString('s') } else { $null }); "
        "LastTaskResult=[int64]$i.LastTaskResult; "
        "NextRunTime=$(if ($i.NextRunTime) { $i.NextRunTime.ToString('s') } else { $null })} "
        "| ConvertTo-Json -Compress"
    )
    r = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
    if r.returncode != 0:
        raise RuntimeError(f"could not query Task Scheduler: {(r.stderr or r.stdout).strip()}")
    out = (r.stdout or "").strip()
    return None if out in ("", "null") else json.loads(out)


def describe_result(code: Optional[int]) -> str:
    if code is None:
        return "n/a"
    code &= 0xFFFFFFFF
    return f"0x{code:X} ({RESULTS.get(code, 'see Task Scheduler docs')})"


def install(spec: TaskSpec, data_dir: Path, *, run: Runner = run_cmd, dry_run: bool = False,
            warnings: List[str] = (), out: Out = print) -> int:
    xml = task_xml(spec)
    path = Path(data_dir) / "ottr-task.xml"
    cmd = ["schtasks", "/Create", "/TN", _check_name(spec.name), "/XML", str(path), "/F"]
    for w in warnings:
        out(f"warning: {w}")
    if dry_run:
        out(xml)
        out("would run: " + subprocess.list2cmdline(cmd))
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(xml, encoding="utf-16")  # schtasks /XML wants UTF-16 with a BOM
    r = run(cmd)
    if r.returncode != 0:
        out(f"schtasks failed ({r.returncode}): {(r.stderr or r.stdout).strip()}")
        return 1
    out(f"Registered scheduled task '{spec.name}' for {spec.user}: starts at logon, "
        f"re-checked every {spec.keepalive_min} min. Start it now with `ottr service start`.")
    return 0


def start(name: str, *, run: Runner = run_cmd, out: Out = print) -> int:
    for cmd in (["schtasks", "/Change", "/TN", _check_name(name), "/ENABLE"],
                ["schtasks", "/Run", "/TN", name]):
        r = run(cmd)
        if r.returncode != 0:
            out(f"{' '.join(cmd[:2])} failed: {(r.stderr or r.stdout).strip()}")
            return 1
    out(f"'{name}' started; `ottr status` shows engine health in about a minute")
    return 0


def stop(name: str, data_dir: Path, *, run: Runner = run_cmd, timeout: float = 90.0,
         sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
         out: Out = print) -> int:
    """Disable the keepalive, ask the engine to stop cleanly, end the task if it won't."""
    info = query(name, run)
    if info is not None:
        # Disable first, or the 5-minute keepalive would start it right back up.
        run(["schtasks", "/Change", "/TN", _check_name(name), "/DISABLE"])
    request_stop(data_dir, "ottr service stop")
    if info is None:
        out("task not registered; dropped a stop request for any engine that is running")
        return 0
    deadline = clock() + timeout  # by the clock: each PowerShell query itself takes ~1s
    while clock() < deadline:
        info = query(name, run)
        if not info or info.get("State") != "Running":
            out(f"'{name}' stopped and disabled (`ottr service start` re-enables it)")
            return 0
        sleep(1.0)
    out(f"engine did not stop within {timeout:.0f}s; ending the task (the ledger is crash-safe)")
    r = run(["schtasks", "/End", "/TN", name])
    return 0 if r.returncode == 0 else 1


def uninstall(name: str, data_dir: Path, *, run: Runner = run_cmd, out: Out = print, **kw) -> int:
    if query(name, run) is None:
        out(f"no scheduled task named '{name}'")
        return 0
    stop(name, data_dir, run=run, out=out, **kw)
    r = run(["schtasks", "/Delete", "/TN", _check_name(name), "/F"])
    if r.returncode != 0:
        out(f"schtasks /Delete failed: {(r.stderr or r.stdout).strip()}")
        return 1
    out(f"removed scheduled task '{name}'")
    return 0


def status(name: str, *, run: Runner = run_cmd, out: Out = print) -> int:
    info = query(name, run)
    if info is None:
        out(f"scheduled task '{name}': not installed (`ottr service install`)")
        return 1
    out(f"scheduled task '{name}': {info.get('State')}  last run {info.get('LastRunTime') or 'never'} "
        f"result {describe_result(info.get('LastTaskResult'))}  next {info.get('NextRunTime') or 'n/a'}")
    return 0 if info.get("State") == "Running" else 1
