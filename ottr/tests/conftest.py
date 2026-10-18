"""
Shared fixtures: an isolated config + writer store in a temp dir, and quote helpers.

Every test gets its own database, so no test can touch real data (the v1 suite needed a
tripwire for exactly that).
"""

import dataclasses
import time

import pytest

from ottr import config as cfgmod
from ottr.ledger.book import Ledger
from ottr.ledger.store import Store
from ottr.market.types import Quote


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """No test may read the developer's ottr/.env or real secrets (a webhook would post)."""
    for var in ("OTTR_DRY_RUN", "MAX_TRADE_USD", "OTTR_DATA_DIR", "DISCORD_WEBHOOK_URL", "OTTR_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OTTR_ENV_FILE", str(tmp_path / "no-such.env"))


@pytest.fixture
def cfg(tmp_path):
    c = cfgmod.load(data_dir=tmp_path)
    # Engine tests must never bind the real dashboard port; API tests opt in with port 0.
    return dataclasses.replace(c, api=dataclasses.replace(c.api, enabled=False, port=0))


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "ottr.db", writer=True)
    yield s
    s.close()


@pytest.fixture
def ledger(store):
    lg = Ledger(store)
    lg.initialize(10000)
    return lg


def quote(asset, price, spread_pct=0.1, age=0.0, source="kraken", high=None):
    half = price * spread_pct / 200
    return Quote(asset=asset, bid=price - half, ask=price + half, last=price,
                 ts=time.time() - age, source=source, high_24h=high, low_24h=None)
