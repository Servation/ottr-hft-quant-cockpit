"""
Live smoke tests for the desk as a whole (`pytest -m live`): with the real keys, `ottr init`'s
seeding loads the calendar, bars and feeds into a fresh book, and the stock screener ranks
real stocks end to end on a small pool. Skips without the Alpaca keys or SEC_USER_AGENT.
About two minutes; it reads only public market data and the SEC, and writes only to a
temporary book.
"""

import dataclasses

import pytest

from ottr import cli
from ottr.events.sec import Sec
from ottr.ledger.book import Ledger
from ottr.ledger.store import Store
from ottr.market.alpaca import Alpaca, Credentials
from ottr.universe import db as udb

pytestmark = pytest.mark.live


async def test_live_seed_and_first_screen(cfg, tmp_path):
    creds, sec = Credentials.from_env(), Sec.from_env()
    if creds is None or sec is None:
        pytest.skip("needs APCA_API_KEY_ID / APCA_API_SECRET_KEY and SEC_USER_AGENT")
    small = dataclasses.replace(cfg, screen=dataclasses.replace(cfg.screen, pool_max=40))
    store = Store(tmp_path / "ottr.db", writer=True)
    try:
        Ledger(store).initialize(10000)
        alpaca = Alpaca(creds)
        lines = []
        await cli.seed(small, store, alpaca, cli._sources(alpaca), say=lines.append)
        text = "\n".join(lines)
        assert "market data: the calendar through" in text and "sec_lists: " in text, text
        screen = udb.last_screen(store)
        assert screen and screen["kind"] == "init", text
        counts = screen["result"]["counts"]
        assert counts["listed"] > 3000 and counts["pool"] == 40 and counts["ranked"] > 0, counts
        assert len(udb.satellites(store)) <= small.universe.satellites_max
        assert Ledger(store).check() == []
    finally:
        store.close()
