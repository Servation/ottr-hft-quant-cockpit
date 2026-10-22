"""
US desk config tests (config.toml via load): the shipped file is valid, lookups
resolve caps and floors as intended, env overrides apply, secrets stay out of repr, and
every validation rule rejects a broken file with a message naming the problem.
"""

import pytest

from ottr import config as cfgmod
from ottr.config import ConfigError, load

SHIPPED = cfgmod.DEFAULT_CONFIG.read_text(encoding="utf-8")


def variant(tmp_path, *edits):
    """The shipped config with (old, new) text replacements applied."""
    text = SHIPPED
    for old, new in edits:
        assert old in text, old
        text = text.replace(old, new, 1)
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    return load(p, data_dir=tmp_path)


def test_the_shipped_config_is_valid(tmp_path):
    c = load(data_dir=tmp_path)
    assert c.account.type == "margin_1x" and c.account.starting_cash == 10000
    assert len(c.core) == 16 and c.universe.cash_symbol == "SGOV"
    assert c.asset_class("TLT") == "bond_etf" and c.asset_class("NVDA") is None
    assert "XLE" in c.core and "XLE" not in c.universe.reference
    assert c.schedule.meeting_times == ("10:15", "15:15")
    assert c.data.feed == "iex"
    assert c.events.earnings_horizon == "6month" and {"VIXCLS", "DGS10", "BAMLH0A0HYM2"} <= set(c.events.macro)


def test_caps_resolve_override_then_class_and_cash_is_uncapped(tmp_path):
    c = load(data_dir=tmp_path)
    assert c.cap_pct("SPY") == 25.0            # class cap
    assert c.cap_pct("EEM") == 15.0            # symbol override beats its class
    assert c.cap_pct("SGOV") is None           # swept cash: uncapped
    assert c.cap_pct("NVDA", "stock") == 8.0   # a satellite, by its class
    with pytest.raises(KeyError):
        c.cap_pct("NVDA")                      # unknown symbol without a class: no guessing


def test_vol_floors_and_quote_limits(tmp_path):
    c = load(data_dir=tmp_path)
    assert c.vol_floor("cash_etf") < c.vol_floor("bond_etf") < c.vol_floor("equity_etf")
    q = c.data.quote_limits()
    assert (q.max_spread_pct, q.max_age_sec) == (1.0, 120)


def test_env_overrides_and_secrets_stay_out_of_repr(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTR_DRY_RUN", "1")
    monkeypatch.setenv("MAX_TRADE_USD", "500")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/SECRET-TOKEN")
    c = load(data_dir=tmp_path)
    assert c.trading.dry_run and c.trading.max_trade_usd == 500
    assert c.discord_webhook_url and "SECRET-TOKEN" not in repr(c)


@pytest.mark.parametrize("edits,needle", [
    ([('type = "margin_1x"', 'type = "portfolio_margin"')], "account.type"),
    ([("SPY = \"equity_etf\"", "brk-b = \"stock\"")], "write 'brk-b' as 'BRK.B'"),
    ([('QQQ = "equity_etf"', 'QQQ = "crypto"')], "universe.core.QQQ"),
    ([('SGOV = "cash_etf"', 'SGOV = "bond_etf"')], "cash_symbol"),
    ([('reference = ["RSP",', 'reference = ["SPY", "RSP",')], "repeats traded symbols"),
    ([("stock = 0.15", "")], "sizing.vol_floor"),
    ([("real_estate_etf = 20.0\nstock = 8.0", "stock = 8.0")], "caps needs a cap"),
    ([('meeting_times = ["10:15", "15:15"]', 'meeting_times = ["08:00", "15:15"]')], "within the session"),
    ([('meeting_times = ["10:15", "15:15"]', 'meeting_times = ["15:15", "10:15"]')], "in order"),
    ([('feed = "iex"', 'feed = "polygon"')], "data.feed"),
    ([("\nhistory_days = 400", "\nhistory_days = 200")], "data.history_days"),
    ([("max_gross_pct = 90.0", "max_gross_pct = 120.0")], "max_gross_pct"),
    ([("[api]", "[not_a_section]\nx = 1\n\n[api]")], "unknown sections"),
    ([("keep_cash_pct = 5.0", "keep_cash_pct = 1.0")], "sweep.keep_cash_pct"),   # below the 2% reserve
    ([('at = "15:45"', 'at = "17:00"')], "sweep.at"),
    ([('host = "127.0.0.1"', 'host = "0.0.0.0"')], "loopback"),          # shared rule applies too
    ([("min_analysts = 2", "min_analysts = 5")], "min_analysts"),
    ([('earnings_horizon = "6month"', 'earnings_horizon = "1year"')], "events.earnings_horizon"),
    ([("max_age_hours = 72", "max_age_hours = 24")], "events.max_age_hours"),    # stale between refreshes
    ([('VIXCLS = "VIX', 'vixcls = "VIX')], "events.macro.vixcls"),
    ([("corporate_actions_lookback_days = 60", "corporate_actions_lookback_days = 3")], "lookback"),
    ([("sec_every_days = 7", "sec_every_days = 7\nsec_key = 1")], r"\[events\] unknown keys"),
    ([('sectors = ["XLB",', 'sectors = ["XLX",')], "universe.sectors"),
    ([("quote_ttl_sec = 30", "quote_ttl_sec = 300")], "quote_ttl_sec"),     # longer than a quote may live
    ([("er_threshold = 0.30", "er_threshold = 1.5")], "er_threshold"),
])
def test_broken_configs_are_rejected(tmp_path, edits, needle):
    with pytest.raises(ConfigError, match=needle.replace("(", r"\(").replace(")", r"\)")):
        variant(tmp_path, *edits)


def test_unknown_keys_in_a_section_are_rejected(tmp_path):
    with pytest.raises(ConfigError, match=r"\[trading\] unknown keys"):
        variant(tmp_path, ("slippage_pct = 0.0", "slippage_pct = 0.0\nfee_pct = 0.4"))
