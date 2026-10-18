"""
Task Scheduler tests with a fake command runner: nothing here registers a real task.
Checks the generated XML (settings that keep the engine alive), the exact schtasks /
PowerShell commands, the graceful stop sequence, and the install preflight.
"""

import json
import subprocess
import xml.etree.ElementTree as ET

import pytest

from ottr import service
from ottr.control import stop_path
from ottr.service import TaskSpec, task_xml

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
SPEC = TaskSpec(name="OTTR Engine", command=r"C:\venv\Scripts\pythonw.exe",
                arguments=r'-m ottr.cli --config "D:\R&D bots\ottr\config.toml" supervise',
                workdir=r"D:\R&D bots\ottr", user=r"DESKTOP-1\Jeffrey Saelee")


class FakeRunner:
    """Scripted schtasks/powershell: `states` is what successive queries report."""

    def __init__(self, states=("Ready",), fail=()):
        self.calls, self.states, self.fail = [], list(states), set(fail)

    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd[0] == "powershell":
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            out = "null" if state is None else json.dumps(
                {"State": state, "LastRunTime": "2026-10-18T03:00:00", "LastTaskResult": 0x41301,
                 "NextRunTime": None})
            return subprocess.CompletedProcess(cmd, 0, out + "\n", "")
        rc = 1 if cmd[1] in self.fail else 0
        return subprocess.CompletedProcess(cmd, rc, "SUCCESS" if not rc else "", "ERROR: denied" if rc else "")

    def schtasks(self):
        return [c[1:] for c in self.calls if c[0] == "schtasks"]


def text(root, path):
    return root.find(path, NS).text


def test_task_xml_keeps_the_engine_alive():
    root = ET.fromstring(task_xml(SPEC).split("\n", 1)[1])  # ET rejects the UTF-16 declaration on a str
    assert text(root, "t:Settings/t:ExecutionTimeLimit") == "PT0S"  # default would kill it after 72h
    assert text(root, "t:Settings/t:MultipleInstancesPolicy") == "IgnoreNew"
    assert text(root, "t:Settings/t:DisallowStartIfOnBatteries") == "false"
    assert text(root, "t:Settings/t:StopIfGoingOnBatteries") == "false"
    assert text(root, "t:Settings/t:IdleSettings/t:StopOnIdleEnd") == "false"
    assert text(root, "t:Settings/t:StartWhenAvailable") == "true"
    assert text(root, "t:Triggers/t:LogonTrigger/t:UserId") == SPEC.user
    assert text(root, "t:Triggers/t:LogonTrigger/t:Repetition/t:Interval") == "PT5M"
    assert text(root, "t:Triggers/t:LogonTrigger/t:Repetition/t:StopAtDurationEnd") == "false"
    assert text(root, "t:Principals/t:Principal/t:LogonType") == "InteractiveToken"  # no stored password
    assert text(root, "t:Principals/t:Principal/t:RunLevel") == "LeastPrivilege"
    assert text(root, "t:Actions/t:Exec/t:Command").endswith("pythonw.exe")  # no console window
    # Paths with spaces and '&' survive escaping intact.
    assert text(root, "t:Actions/t:Exec/t:Arguments") == SPEC.arguments
    assert text(root, "t:Actions/t:Exec/t:WorkingDirectory") == SPEC.workdir


def test_default_spec_prefers_pythonw_and_quotes_the_config(tmp_path):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "python.exe").write_bytes(b"")
    (scripts / "pythonw.exe").write_bytes(b"")
    cfg = tmp_path / "my config.toml"
    spec = service.default_spec(cfg, python=scripts / "python.exe")
    assert spec.command == str(scripts / "pythonw.exe")
    assert spec.arguments == f'-m ottr.cli --config "{cfg.resolve()}" supervise'


def test_install_dry_run_registers_nothing(tmp_path):
    run, lines = FakeRunner(), []
    assert service.install(SPEC, tmp_path, run=run, dry_run=True, out=lines.append) == 0
    assert run.calls == [] and not (tmp_path / "ottr-task.xml").exists()
    assert "<Task version=" in lines[0] and lines[1].startswith("would run: schtasks /Create")


def test_install_writes_utf16_xml_and_registers(tmp_path):
    run, lines = FakeRunner(), []
    assert service.install(SPEC, tmp_path, run=run, warnings=["codex missing"], out=lines.append) == 0
    path = tmp_path / "ottr-task.xml"
    assert path.read_bytes()[:2] == b"\xff\xfe"  # UTF-16 LE BOM, as schtasks expects
    assert "ExecutionTimeLimit" in path.read_text(encoding="utf-16")
    assert run.schtasks() == [["/Create", "/TN", "OTTR Engine", "/XML", str(path), "/F"]]
    assert lines[0] == "warning: codex missing"


def test_install_failure_is_reported(tmp_path):
    lines = []
    assert service.install(SPEC, tmp_path, run=FakeRunner(fail={"/Create"}), out=lines.append) == 1
    assert "denied" in lines[-1]


def test_start_enables_then_runs():
    run = FakeRunner()
    assert service.start("OTTR Engine", run=run, out=lambda s: None) == 0
    assert run.schtasks() == [["/Change", "/TN", "OTTR Engine", "/ENABLE"], ["/Run", "/TN", "OTTR Engine"]]


def test_stop_disables_keepalive_then_asks_the_engine_to_stop(tmp_path):
    run = FakeRunner(states=["Running", "Running", "Running", "Ready"])
    assert service.stop("OTTR Engine", tmp_path, run=run, sleep=lambda s: None, out=lambda s: None) == 0
    assert run.schtasks() == [["/Change", "/TN", "OTTR Engine", "/DISABLE"]]  # no hard /End needed
    assert stop_path(tmp_path).exists()


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_stop_ends_the_task_if_the_engine_hangs(tmp_path):
    run, clock = FakeRunner(states=["Running"]), FakeClock()
    assert service.stop("OTTR Engine", tmp_path, run=run, timeout=3, sleep=clock.sleep, clock=clock,
                        out=lambda s: None) == 0
    assert run.schtasks()[-1] == ["/End", "/TN", "OTTR Engine"]
    assert clock.t == 3.0


def test_stop_without_a_task_still_stops_a_manual_engine(tmp_path):
    run = FakeRunner(states=[None])
    assert service.stop("OTTR Engine", tmp_path, run=run, out=lambda s: None) == 0
    assert run.schtasks() == [] and stop_path(tmp_path).exists()


def test_uninstall_stops_then_deletes(tmp_path):
    run = FakeRunner(states=["Ready"])
    assert service.uninstall("OTTR Engine", tmp_path, run=run, out=lambda s: None, sleep=lambda s: None) == 0
    assert run.schtasks()[-1] == ["/Delete", "/TN", "OTTR Engine", "/F"]


def test_status_reports_state_and_result_code():
    lines = []
    assert service.status("OTTR Engine", run=FakeRunner(states=["Running"]), out=lines.append) == 0
    assert "Running" in lines[0] and "0x41301 (currently running)" in lines[0]
    assert service.status("OTTR Engine", run=FakeRunner(states=[None]), out=lines.append) == 1
    assert "not installed" in lines[-1]


def test_query_raises_on_powershell_failure():
    def broken(cmd):
        return subprocess.CompletedProcess(cmd, 1, "", "Get-ScheduledTask : access denied")
    with pytest.raises(RuntimeError, match="access denied"):
        service.query("OTTR Engine", broken)


def test_task_name_is_validated_before_it_reaches_a_shell():
    with pytest.raises(ValueError):
        service.query("x'; Remove-Item -Recurse C:\\; '", FakeRunner())


def test_preflight_warns_about_missing_clis_and_webhook(tmp_path, monkeypatch):
    from ottr.council import models
    monkeypatch.setattr(models.CodexCLI, "_find", staticmethod(lambda: None))
    warns = service.preflight(["claude", "gpt", "gemini"], webhook=None, path=str(tmp_path))
    assert any("`claude` is not on your user/system PATH" in w for w in warns)
    assert any("`agy`" in w for w in warns)
    assert any("codex CLI not found" in w for w in warns)
    assert any("DISCORD_WEBHOOK_URL" in w for w in warns)
    (tmp_path / "claude.exe").write_bytes(b"")
    (tmp_path / "agy.exe").write_bytes(b"")
    monkeypatch.setattr(models.CodexCLI, "_find", staticmethod(lambda: "codex.exe"))
    monkeypatch.setenv("PATHEXT", ".EXE")
    assert service.preflight(["claude", "gpt", "gemini"], webhook="https://x", path=str(tmp_path)) == []


def test_xml_declares_utf16_for_schtasks():
    assert task_xml(SPEC).startswith('<?xml version="1.0" encoding="UTF-16"?>')
