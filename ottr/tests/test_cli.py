"""
CLI tests through `main()`: `ottr status` exit codes and JSON, the env-file loader, the
config guards (loopback-only API, key length), and `service install --dry-run`.
"""

import json
import time

import pytest

from ottr import cli, health
from ottr import config as cfgmod
from ottr.config import ConfigError, load_env_file
from ottr.ledger.book import Ledger
from ottr.ledger.store import Store
from ottr.market.types import MarketDataError


@pytest.fixture
def book(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTR_DATA_DIR", str(tmp_path))
    s = Store(tmp_path / "ottr.db", writer=True)
    Ledger(s).initialize(10000)
    yield s
    s.close()


def status(*extra):
    return cli.main(["status", *extra])


def test_status_without_a_database_is_engine_down(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OTTR_DATA_DIR", str(tmp_path / "empty"))
    assert status("--json") == health.EXIT_DOWN
    assert json.loads(capsys.readouterr().out)["exit_code"] == 3


def test_status_json_engine_down(book, capsys):
    assert status("--json") == health.EXIT_DOWN
    d = json.loads(capsys.readouterr().out)
    assert d["engine_alive"] is False and d["alarms"][0]["key"] == "engine:down"
    assert d["kill_switch"]["effective_dry_run"] is False


def test_status_healthy_engine_exits_0(book, capsys, monkeypatch):
    book.set_state("engine", {"running": True, "epoch": time.time() - 30})
    for t in ("watchdog", "market", "risk", "equity", "council"):
        book.heartbeat(t, True)

    class NoNetwork:
        def __init__(self, cfg):
            pass

        async def quotes(self):
            raise MarketDataError("offline in tests")

    monkeypatch.setattr(cli, "MarketData", NoNetwork)
    assert status() == health.EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("OTTR OK (exit 0)") and "live quotes unavailable" in out
    assert "task risk     ok" in out


def test_status_ledger_problem_exits_2(book, capsys):
    book.conn.execute("UPDATE account SET cash='1' WHERE id=1")
    assert status("--json") == health.EXIT_LEDGER


def test_env_file_fills_only_unset_variables(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("# secrets\nexport OTTR_T1='quoted value'\nOTTR_T2=plain\n\nOTTR_T3=\"x=y\"\n", encoding="utf-8")
    monkeypatch.setenv("OTTR_T2", "from-shell")
    for k in ("OTTR_T1", "OTTR_T3"):
        monkeypatch.delenv(k, raising=False)
    assert sorted(load_env_file(f)) == ["OTTR_T1", "OTTR_T3"]
    import os
    assert os.environ["OTTR_T1"] == "quoted value" and os.environ["OTTR_T2"] == "from-shell"
    assert os.environ["OTTR_T3"] == "x=y"
    assert load_env_file(tmp_path / "missing.env") == []
    (tmp_path / "bad.env").write_text("NOT A PAIR\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="bad.env:1"):
        load_env_file(tmp_path / "bad.env")


def test_main_reads_the_env_file(book, tmp_path, monkeypatch, capsys):
    env = tmp_path / "test.env"
    env.write_text("OTTR_API_KEY=abcdefghijklmnop-from-file\n", encoding="utf-8")
    monkeypatch.setenv("OTTR_ENV_FILE", str(env))
    monkeypatch.delenv("OTTR_API_KEY", raising=False)
    status("--json")
    import os
    assert os.environ.pop("OTTR_API_KEY") == "abcdefghijklmnop-from-file"


def test_api_must_bind_loopback(tmp_path):
    p = tmp_path / "open.toml"
    p.write_text(cfgmod.DEFAULT_CONFIG.read_text(encoding="utf-8").replace('host = "127.0.0.1"', 'host = "0.0.0.0"'),
                 encoding="utf-8")
    with pytest.raises(ConfigError, match="loopback"):
        cfgmod.load(p, data_dir=tmp_path)


def test_short_api_key_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTR_API_KEY", "short")
    with pytest.raises(ConfigError, match="16 characters"):
        cfgmod.load(data_dir=tmp_path)


def test_typo_in_a_new_section_fails_loudly(tmp_path):
    p = tmp_path / "typo.toml"
    p.write_text(cfgmod.DEFAULT_CONFIG.read_text(encoding="utf-8").replace("burst = 10", "brust = 10"),
                 encoding="utf-8")
    with pytest.raises(ConfigError, match=r"\[alerts\] unknown keys: \['brust'\]"):
        cfgmod.load(p, data_dir=tmp_path)


def test_service_install_dry_run_prints_the_task(capsys):
    assert cli.main(["service", "install", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in out and "supervise" in out
    assert "would run: schtasks /Create" in out


def test_supervise_adds_the_chat_bot_only_when_it_can_run(tmp_path, monkeypatch):
    cfg = cfgmod.load(data_dir=tmp_path)
    engine, companions, notes = cli.supervised(cfg, cfgmod.DEFAULT_CONFIG)
    assert engine[-1] == "run" and companions == [] and "DISCORD_BOT_TOKEN" in notes[0]
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    monkeypatch.setenv("CEO_DISCORD_ID", "111111111111111111")
    cfg = cfgmod.load(data_dir=tmp_path)
    engine, (chat,), notes = cli.supervised(cfg, cfgmod.DEFAULT_CONFIG, verbose=True)
    # --standby: a bot started by hand keeps the token; the supervised one waits for it.
    assert notes == [] and chat.name == "chat" and chat.argv[-3:] == ["-v", "chat", "--standby"]
    assert str(tmp_path / "logs" / "chat.log") in chat.argv and chat.give_up_codes == {78}
    assert chat.out_log == tmp_path / "logs" / "chat.out.log"
    assert "tok" not in " ".join(chat.argv)  # the token reaches the child through ottr/.env, not argv


def _closed_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_chat_check_reports_without_connecting(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OTTR_DATA_DIR", str(tmp_path))
    p = tmp_path / "c.toml"  # a port nothing listens on, so a live engine on 8765 is never touched
    p.write_text(cfgmod.DEFAULT_CONFIG.read_text(encoding="utf-8").replace("port = 8765", f"port = {_closed_port()}"),
                 encoding="utf-8")
    assert cli.main(["--config", str(p), "chat", "--check"]) == 78
    out = capsys.readouterr().out
    assert "DISCORD_BOT_TOKEN: MISSING" in out and "Can't reach the OTTR engine" in out and "not ready" in out
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    monkeypatch.setenv("CEO_DISCORD_ID", "111111111111111111")
    assert cli.main(["--config", str(p), "chat", "--check"]) == 0
    out = capsys.readouterr().out
    assert "ready: `ottr chat` connects" in out and "tok" not in out.replace("token", "")


def test_chat_refuses_to_start_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTR_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    monkeypatch.setenv("CEO_DISCORD_ID", "111111111111111111")
    p = tmp_path / "off.toml"
    p.write_text(cfgmod.DEFAULT_CONFIG.read_text(encoding="utf-8").replace(
        "enabled = true             # `ottr supervise` runs it", "enabled = false            # `ottr supervise` runs it"),
        encoding="utf-8")
    assert cli.main(["--config", str(p), "chat"]) == 78
