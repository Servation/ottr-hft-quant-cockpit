"""
Cutover tooling: `ottr archive` (a verified copy of a book this version can't open, set
aside so a new one can be opened) and the seeding `ottr init` does (market data, events
feeds and the first stock screen) so the engine's first session starts with them.
"""

import sqlite3

import pytest

from ottr import cli
from ottr.archive import ArchiveError, archive
from ottr.events.refresh import Sources
from ottr.ledger.book import Ledger
from ottr.ledger.store import Store, _FileLock
from ottr.universe import db as udb
from tests.test_screen import screen_rig


def crypto_book(data_dir):
    """A stand-in for the v2 crypto book: a schema this version refuses, with history."""
    data_dir.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(data_dir / "ottr.db")
    c.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema_version', '2');
        CREATE TABLE equity (ts REAL PRIMARY KEY, equity REAL NOT NULL);
        INSERT INTO equity VALUES (1.0, 10000.0), (2.0, 10412.37);
        CREATE TABLE fills (id INTEGER PRIMARY KEY, asset TEXT);
        INSERT INTO fills (asset) VALUES ('BTC'), ('ETH'), ('SOL');""")
    c.commit()
    c.close()
    (data_dir / "logs").mkdir(exist_ok=True)
    (data_dir / "logs" / "engine.log").write_text("the last crypto run\n", encoding="utf-8")


def test_archive_copies_verifies_and_sets_the_book_aside(tmp_path, monkeypatch, capsys):
    data, dest = tmp_path / "data", tmp_path / "archive"
    crypto_book(data)
    old_config = tmp_path / "crypto.toml"
    old_config.write_text("[universe]\nBTC = 'crypto'\n", encoding="utf-8")
    a = archive(data, dest, old_config, retire=True)
    assert (a.schema_version, a.tables, a.last_equity) == ("2", {"equity": 2, "fills": 3, "meta": 1}, 10412.37)
    copy = sqlite3.connect(dest / "ottr.db")
    assert copy.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 3
    copy.close()
    assert (dest / "crypto.toml").read_text(encoding="utf-8").startswith("[universe]")
    assert (dest / "logs" / "engine.log").exists()
    note = (dest / "NOTE.md").read_text(encoding="utf-8")
    assert "- Schema version: 2" in note and "- Last equity point: $10,412.37" in note and a.sha256 in note
    assert not (data / "ottr.db").exists() and a.retired.exists()
    # The data directory is free: a new US book opens there.
    monkeypatch.setenv("OTTR_DATA_DIR", str(data))
    assert cli.main(["init", "--no-seed"]) == 0
    s = Store(data / "ottr.db", writer=False)
    try:
        assert Ledger(s).cash() == 10000
    finally:
        s.close()


def test_archive_refuses_to_overwrite_or_to_run_beside_the_engine(tmp_path):
    data, dest = tmp_path / "data", tmp_path / "archive"
    crypto_book(data)
    dest.mkdir()
    (dest / "something").write_text("x", encoding="utf-8")
    with pytest.raises(ArchiveError, match="never overwritten"):
        archive(data, dest, tmp_path / "none.toml")
    held = _FileLock(data / "ottr.lock")
    held.acquire()
    try:
        with pytest.raises(ArchiveError, match="holds the writer lock"):
            archive(data, tmp_path / "other", tmp_path / "none.toml", retire=True)
    finally:
        held.release()
    assert (data / "ottr.db").exists()                       # nothing was set aside
    with pytest.raises(ArchiveError, match="no database"):
        archive(tmp_path / "empty", tmp_path / "third", tmp_path / "none.toml")


async def test_init_seeds_the_market_data_feeds_and_first_screen(cfg, store, ledger):
    eng, rig, _ = await screen_rig(cfg, store)
    lines = []
    # On the rig's clock: before it took one, this test read the wall clock and began
    # failing once real time passed the fake market's last session (2026-10-23 16:00 ET).
    ok = await cli.seed(cfg, store, rig.market.alpaca, Sources(sec=eng.sources.sec, alpaca=rig.market.alpaca),
                        say=lines.append, clock=rig.clock)
    assert not ok                                            # no FRED or Alpha Vantage client in this test
    text = "\n".join(lines)
    assert "market data: the calendar through" in text and "corporate_actions: " in text
    assert "macro: no FRED client" in text and "first screen: 13 measured, 10 ranked; promoted" in text
    sats = udb.satellites(store)
    assert len(sats) == 6 and udb.last_screen(store)["kind"] == "init"
    assert all(len(rig.market.adjusted(s)) > 60 for s in sats)                 # their history is in
    from ottr.events import db
    assert all(db.corporate_actions_checked(store, s) for s in sats)            # and their corporate actions
