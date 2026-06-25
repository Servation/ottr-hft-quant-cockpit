"""Shared pytest fixtures for the discord-bridge suite."""
import pytest
from unittest.mock import AsyncMock


@pytest.fixture(autouse=True)
def _isolate_data_writers(tmp_path, monkeypatch):
    """Redirect EVERY data/-writing module to a per-test temp dir, so no test can mutate the
    live paper-trading state.

    This is the single authoritative place to isolate persistence. Every historical leak
    (the audit log's fake `order_placed ord124`, 70+ orphaned `portfolio_*.tmp`, meeting-memory
    writing into the live vault) came from a writer that the piecemeal per-fixture isolation had
    missed. Listing them all here, plus the `_no_live_data_writes` tripwire below as the
    backstop, makes a miss loud instead of silent. Each module resolves its path from a module
    global read at call time, so monkeypatching the global redirects the singleton and any new
    instance for the duration of the test.
    """
    d = tmp_path
    import bot.portfolio as _pf
    monkeypatch.setattr(_pf, "_DATA_DIR", d)
    monkeypatch.setattr(_pf, "_PORTFOLIO_FILE", d / "portfolio_state.json")

    import bot.equity as _equity
    monkeypatch.setattr(_equity, "_DATA_DIR", d)
    monkeypatch.setattr(_equity, "_EQUITY_FILE", d / "equity_curve.jsonl")

    import bot.risk_state as _risk_state
    monkeypatch.setattr(_risk_state, "_DATA_DIR", d)
    monkeypatch.setattr(_risk_state, "_STATE_FILE", d / "risk_state.json")

    import bot.memory as _memory
    monkeypatch.setattr(_memory, "DATA_DIR", d)
    monkeypatch.setattr(_memory, "LOG_PATH", d / "meeting_log.json")
    monkeypatch.setattr(_memory, "VAULT_DIR", d / "vesper_vault")
    monkeypatch.setattr(_memory, "INDEX_PATH", d / "embeddings_index.json")

    import bot.knowledge_graph as _kg
    monkeypatch.setattr(_kg, "DATA_DIR", d)
    monkeypatch.setattr(_kg, "GRAPH_PATH", d / "agent_reputation_graph.json")

    import bot.meetings as _meetings
    monkeypatch.setattr(_meetings, "ROTATION_STATE_PATH", d / "rotation_state.json")

    import bot.main as _main
    monkeypatch.setattr(_main, "LAST_STARTUP_MEETING_FILE", d / "last_startup_meeting.txt")

    # Audit log is env-driven: bot.audit._audit_file() reads AUDIT_LOG_FILE at call time.
    monkeypatch.setenv("AUDIT_LOG_FILE", str(d / "audit_log.jsonl"))


@pytest.fixture(autouse=True)
def _no_real_api_server(mocker):
    """Stop on_ready() tests from binding the real aiohttp port (:8001).

    Several tests exercise TradingFloorBot.on_ready(), which calls
    start_api_server() and binds a real socket. Across a full run the socket
    isn't freed between tests, causing intermittent 'address already in use'
    failures. on_ready tests care about channel resolution, not the live server,
    so mock it out everywhere.
    """
    try:
        mocker.patch("bot.main.start_api_server", new_callable=AsyncMock)
    except (ImportError, AttributeError):
        pass


# ── Live-data tripwire ────────────────────────────────────────────────────────
# The backstop that makes test -> live-data leaks impossible to miss. _isolate_data_writers
# (above) redirects every KNOWN writer, but a writer added later could be missed. If any write
# reaches live data/, a test silently mutates live paper-trading state -- exactly how the audit
# log and 70+ orphaned portfolio temp files crept in. This fails the suite (a blocking CI gate)
# the instant any test touches live data/, and restores the dir so a leak neither persists nor
# cascades into later tests.

def _data_manifest(d):
    """relpath -> (size, mtime_ns) for every file under the live data dir (cheap stat)."""
    out = {}
    if d.exists():
        for p in d.rglob("*"):
            if p.is_file():
                st = p.stat()
                out[str(p.relative_to(d))] = (st.st_size, st.st_mtime_ns)
    return out


def _restore_dir(live, backup):
    """Make live match the backup snapshot: delete files a test added, copy the rest back."""
    import shutil
    if live.exists():
        for p in list(live.rglob("*")):
            if p.is_file() and not (backup / p.relative_to(live)).exists():
                p.unlink()
    shutil.copytree(backup, live, dirs_exist_ok=True)


@pytest.fixture(scope="session")
def _live_data_dir():
    import bot
    return bot.PROJECT_ROOT / "data"


@pytest.fixture(scope="session", autouse=True)
def _live_data_backup(_live_data_dir, tmp_path_factory):
    """Snapshot the live data/ dir once per session so the tripwire can restore it."""
    import shutil
    backup = tmp_path_factory.mktemp("live_data_backup")
    if _live_data_dir.exists():
        shutil.copytree(_live_data_dir, backup, dirs_exist_ok=True)
    return backup


@pytest.fixture(autouse=True)
def _no_live_data_writes(_live_data_dir, _live_data_backup):
    """Fail (and restore) if a test creates / modifies / deletes any file under live data/.
    Tests must isolate every writer (the equity / risk / audit / etc. fixtures above, or
    eval_utils.isolated_* in evals). This is the writer-agnostic guarantee that a future
    writer can't silently leak into live paper-trading state."""
    before = _data_manifest(_live_data_dir)
    yield
    after = _data_manifest(_live_data_dir)
    if before != after:
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        modified = sorted(k for k in set(before) & set(after) if before[k] != after[k])
        _restore_dir(_live_data_dir, _live_data_backup)
        pytest.fail(
            "Test wrote to the live data/ dir (it must be isolated). "
            f"added={added} modified={modified} removed={removed}"
        )
