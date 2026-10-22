"""
Shared fixtures: an isolated config + writer store in a temp dir, and quote helpers.

Every test gets its own database, so no test can touch real data (the v1 suite needed a
tripwire for exactly that).
"""

import dataclasses
import logging
import os
import time

import pytest

from ottr import config as cfgmod
from ottr.ledger.book import Ledger
from ottr.ledger.store import Store
from ottr.market.types import Quote


# Market-data and events feed credentials: they can only read.
LIVE_READ_ONLY_VARS = ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "ALPHAVANTAGE_API_KEY", "SEC_USER_AGENT")


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch, request):
    """No test may read the developer's ottr/.env or real secrets (a webhook would post).

    Exception: tests marked `live` keep the read-only feed credentials, since checking the
    real feeds is their whole point (`pytest -m live`, exported or in ottr/.env).
    """
    live = request.node.get_closest_marker("live") is not None
    for var in ("OTTR_DRY_RUN", "MAX_TRADE_USD", "OTTR_DATA_DIR", "DISCORD_WEBHOOK_URL", "OTTR_API_KEY",
                "DISCORD_BOT_TOKEN", "CEO_DISCORD_ID", "OTTR_CHAT_ALLOWED_IDS", "DISCORD_GUILD_ID",
                *(() if live else LIVE_READ_ONLY_VARS)):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OTTR_ENV_FILE", str(tmp_path / "no-such.env"))
    env_file = cfgmod.ROOT / ".env"
    if live and env_file.exists():
        # Only the read-only keys: the file's other secrets (a webhook) stay out of tests.
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() in LIVE_READ_ONLY_VARS and value.strip() and key.strip() not in os.environ:
                monkeypatch.setenv(key.strip(), value.strip())


@pytest.fixture(autouse=True)
def restore_logging():
    """`cli.main()` installs root log handlers bound to the test's captured stderr. Left in
    place, the next test's warning hits that closed stream and prints "--- Logging error
    ---". Remove what a test added (pytest's own capture handlers manage themselves)."""
    root = logging.getLogger()
    before, level = set(root.handlers), root.level
    yield
    for h in list(root.handlers):
        if h not in before and type(h).__module__.startswith("logging"):
            root.removeHandler(h)
            h.close()
    root.setLevel(level)


@pytest.fixture
def cfg(tmp_path):
    c = cfgmod.load(data_dir=tmp_path)
    # Engine tests must never bind the real dashboard port (API tests opt in with port 0)
    # or reach Kraken through the screener (screener tests pass a fake one).
    return dataclasses.replace(c, api=dataclasses.replace(c.api, enabled=False, port=0),
                               screen=dataclasses.replace(c.screen, enabled=False))


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
