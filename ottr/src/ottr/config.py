"""
Typed configuration: config.toml plus a few env overrides.

What it does: parses config.toml into frozen dataclasses and validates ranges once at
startup. Why: v1 read loose dict keys all over the code, so a typo silently fell back
to a default. Here a bad or missing value fails at load time with a clear message.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]  # .../ottr
DEFAULT_CONFIG = ROOT / "config.toml"


@dataclass(frozen=True)
class Trading:
    dry_run: bool = False
    starting_cash: float = 10000.0
    slippage_pct: float = 0.05
    fee_pct: float = 0.40
    min_trade_usd: float = 100.0
    max_trade_usd: float = 2500.0
    max_gross_pct: float = 90.0
    cash_reserve_pct: float = 2.0
    max_trades_per_meeting: int = 3
    max_data_age_sec: float = 180.0
    decision_ttl_sec: float = 900.0


@dataclass(frozen=True)
class Sizing:
    target_risk_pct: float = 2.0
    choppy_regime_factor: float = 0.25
    max_position_pct: float = 50.0
    missing_vol: float = 1.5
    min_vol: float = 0.05


@dataclass(frozen=True)
class Risk:
    enabled: bool = True
    trailing_stop_pct: float = 10.0
    drawdown_halt_pct: float = 15.0
    drawdown_resume_pct: float = 10.0
    drawdown_derisk: bool = True
    drawdown_derisk_pct: float = 25.0
    halt_max_hours: float = 72.0
    trim_band_pct: float = 5.0
    cooldown_sec: float = 900.0
    min_equity_points: int = 24


@dataclass(frozen=True)
class Market:
    vol_window_days: int = 30
    regime_er_window: int = 20
    regime_er_threshold: float = 0.30
    spot_ttl_sec: float = 60.0
    candles_ttl_sec: float = 3600.0


@dataclass(frozen=True)
class Council:
    analysts: List[str] = field(default_factory=lambda: ["claude", "gpt", "gemini"])
    chair: str = "claude"
    min_analysts: int = 2
    timeout_sec: float = 300.0
    interval_hours: float = 4.0
    claude_model: str = "opus"
    gemini_model: str = "gemini-3.8-flash-high"
    reasoning_effort: str = "high"


@dataclass(frozen=True)
class Engine:
    risk_interval_sec: float = 60.0
    equity_interval_sec: float = 900.0
    watchdog_interval_sec: float = 60.0
    alarm_clear_sec: float = 300.0     # an alarm must stay clear this long to resolve


@dataclass(frozen=True)
class Alerts:
    burst: int = 10                    # notifications that may go out back to back
    per_hour: float = 30.0             # sustained rate after the burst is spent
    alarm_repeat_sec: float = 1800.0   # a flapping alarm re-posts at most this often
    alarm_remind_sec: float = 21600.0  # reminder while an alarm stays active
    live_meeting: bool = True          # one message, edited as a council meeting runs
    live_edit_sec: float = 3.0         # at least this long between edits of a live message


LOOPBACK = ("127.0.0.1", "localhost", "::1")


@dataclass(frozen=True)
class Api:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765


# Reasoning-effort levels each model CLI accepts (claude --effort, codex
# model_reasoning_effort, agy --effort). Checked at load so a typo can't fail every /ask.
MODEL_EFFORTS: Dict[str, Tuple[str, ...]] = {
    "claude": ("low", "medium", "high", "xhigh", "max"),
    "gpt": ("minimal", "low", "medium", "high", "xhigh"),
    "gemini": ("low", "medium", "high", "xhigh", "max"),
}


@dataclass(frozen=True)
class Chat:
    enabled: bool = True               # runs only once DISCORD_BOT_TOKEN and CEO_DISCORD_ID are set
    model: str = "claude"              # the one adapter that answers /ask
    effort: str = "low"                # faster than the council's "high": chat should feel live
    per_hour: int = 20                 # /ask model calls in any rolling hour
    history_turns: int = 6             # earlier /ask exchanges in the channel replayed into the prompt
    timeout_sec: float = 180.0         # Discord keeps a deferred reply open for 15 min


@dataclass(frozen=True)
class Screen:
    enabled: bool = True               # daily scan of other Kraken USD pairs; advisory, never traded
    interval_hours: float = 24.0       # daily candles change once a day
    min_usd_volume: float = 1_000_000  # 30-day median daily volume, in USD
    max_spread_pct: float = 0.30       # live bid/ask spread
    min_history_days: int = 120        # seasoning: skips new listings and their launch pumps
    max_pairs: int = 60                # most liquid pairs scored after the prefilter (Kraken's rate limit)
    top_n: int = 5                     # candidates shown to the owner
    post_to_discord: bool = True       # a short digest after each scan


@dataclass(frozen=True)
class Config:
    universe: List[str]
    trading: Trading
    sizing: Sizing
    caps: Dict[str, float]
    risk: Risk
    market: Market
    council: Council
    engine: Engine
    data_dir: Path
    alerts: Alerts = field(default_factory=Alerts)
    api: Api = field(default_factory=Api)
    chat: Chat = field(default_factory=Chat)
    screen: Screen = field(default_factory=Screen)
    pairs: Dict[str, str] = field(default_factory=dict)  # [pairs]: Kraken pair per extra universe asset
    # Secrets stay out of repr() so a logged or printed Config can't leak them.
    discord_webhook_url: Optional[str] = field(default=None, repr=False)
    api_key: Optional[str] = field(default=None, repr=False)  # OTTR_API_KEY: guards the kill switch
    discord_bot_token: Optional[str] = field(default=None, repr=False)  # DISCORD_BOT_TOKEN: `ottr chat`
    chat_owner_id: Optional[int] = None          # CEO_DISCORD_ID: the one user chat obeys fully
    chat_allowed_ids: Tuple[int, ...] = ()       # OTTR_CHAT_ALLOWED_IDS: may read, /ask and /kill
    chat_guild_id: Optional[int] = None          # DISCORD_GUILD_ID: register commands in one server

    def cap_pct(self, asset: str) -> float:
        return self.caps.get(asset.upper(), self.caps.get("default", 35.0))

    def kraken_pair(self, asset: str) -> Optional[str]:
        """The Kraken pair for `asset`: [pairs] first, then the built-in table."""
        from ottr.market.kraken import PAIRS
        return self.pairs.get(asset) or PAIRS.get(asset)


class ConfigError(ValueError):
    pass


def _section(cls, raw: dict, name: str):
    known = set(cls.__dataclass_fields__)
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"[{name}] unknown keys: {sorted(unknown)}")
    return cls(**raw)


def _env_bool(name: str) -> Optional[bool]:
    v = os.getenv(name)
    if v is None or v == "":
        return None
    return v.strip().lower() in ("1", "true", "yes", "on")


def _discord_id(name: str, raw: str) -> int:
    raw = raw.strip()
    if not raw.isdigit() or not 15 <= len(raw) <= 21:
        raise ConfigError(f"{name} must be a Discord user/server ID (15-21 digits), got {raw!r}")
    return int(raw)


def _env_id(name: str) -> Optional[int]:
    raw = os.getenv(name)
    return _discord_id(name, raw) if raw and raw.strip() else None


def _env_ids(name: str) -> Tuple[int, ...]:
    raw = os.getenv(name) or ""
    return tuple(_discord_id(name, part) for part in raw.replace(",", " ").split())


def load_env_file(path: Path) -> List[str]:
    """Read KEY=VALUE lines into os.environ without overriding variables already set.

    Why: the scheduled task gets the user's registry environment, not a shell's, so
    secrets (DISCORD_WEBHOOK_URL, OTTR_API_KEY) live in a gitignored ottr/.env instead.
    Only the CLI entry point calls this; tests and library code never read the file.
    Returns the keys it set.
    """
    if not path.is_file():
        return []
    set_keys = []
    for n, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key.replace("_", "").isalnum():
            raise ConfigError(f"{path}:{n}: expected KEY=VALUE")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key not in os.environ:
            os.environ[key] = value
            set_keys.append(key)
    return set_keys


def load(path: Path = DEFAULT_CONFIG, data_dir: Optional[Path] = None) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    trading_raw = dict(raw.get("trading", {}))
    dry = _env_bool("OTTR_DRY_RUN")
    if dry is not None:
        trading_raw["dry_run"] = dry
    if os.getenv("MAX_TRADE_USD"):
        trading_raw["max_trade_usd"] = float(os.environ["MAX_TRADE_USD"])

    universe = [str(a).upper().strip() for a in raw.get("universe", [])]
    if not universe or len(set(universe)) != len(universe):
        raise ConfigError("universe must be a non-empty list without duplicates")

    caps = {str(k).upper() if k != "default" else "default": float(v)
            for k, v in raw.get("caps", {}).items()}
    for k, v in caps.items():
        if not 0 < v <= 100:
            raise ConfigError(f"caps.{k} must be in (0, 100]")

    cfg = Config(
        universe=universe,
        trading=_section(Trading, trading_raw, "trading"),
        sizing=_section(Sizing, raw.get("sizing", {}), "sizing"),
        caps=caps,
        risk=_section(Risk, raw.get("risk", {}), "risk"),
        market=_section(Market, raw.get("market", {}), "market"),
        council=_section(Council, raw.get("council", {}), "council"),
        engine=_section(Engine, raw.get("engine", {}), "engine"),
        alerts=_section(Alerts, raw.get("alerts", {}), "alerts"),
        api=_section(Api, raw.get("api", {}), "api"),
        chat=_section(Chat, raw.get("chat", {}), "chat"),
        screen=_section(Screen, raw.get("screen", {}), "screen"),
        pairs={str(k).upper().strip(): str(v).strip() for k, v in raw.get("pairs", {}).items()},
        data_dir=Path(data_dir or os.getenv("OTTR_DATA_DIR") or (ROOT / "data")),
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL") or None,
        api_key=os.getenv("OTTR_API_KEY") or None,
        discord_bot_token=(os.getenv("DISCORD_BOT_TOKEN") or "").strip() or None,
        chat_owner_id=_env_id("CEO_DISCORD_ID"),
        chat_allowed_ids=_env_ids("OTTR_CHAT_ALLOWED_IDS"),
        chat_guild_id=_env_id("DISCORD_GUILD_ID"),
    )
    if cfg.api.host not in LOOPBACK:
        # The API has no TLS and its reads are unauthenticated: it must never face a network.
        raise ConfigError(f"api.host must be a loopback address {LOOPBACK}, got {cfg.api.host!r}")
    if not 0 <= cfg.api.port <= 65535:
        raise ConfigError("api.port must be in 0..65535")
    if cfg.api_key is not None and len(cfg.api_key) < 16:
        raise ConfigError("OTTR_API_KEY must be at least 16 characters")
    for name in ("risk_interval_sec", "equity_interval_sec", "watchdog_interval_sec"):
        if getattr(cfg.engine, name) <= 0:
            raise ConfigError(f"engine.{name} must be positive")
    if cfg.alerts.burst < 1 or cfg.alerts.per_hour <= 0:
        raise ConfigError("alerts.burst must be >= 1 and alerts.per_hour > 0")
    if not 1.0 <= cfg.alerts.live_edit_sec <= 60.0:
        # Discord allows a webhook about 5 requests per 2s, shared with every other post.
        raise ConfigError("alerts.live_edit_sec must be in [1, 60]")
    ch = cfg.chat
    if ch.model not in MODEL_EFFORTS:
        raise ConfigError(f"chat.model must be one of {sorted(MODEL_EFFORTS)}, got {ch.model!r}")
    if ch.effort not in MODEL_EFFORTS[ch.model]:
        raise ConfigError(f"chat.effort for {ch.model} must be one of {MODEL_EFFORTS[ch.model]}, got {ch.effort!r}")
    if ch.per_hour < 1 or not 0 <= ch.history_turns <= 50:
        raise ConfigError("chat.per_hour must be >= 1 and chat.history_turns in 0..50")
    if not 0 < ch.timeout_sec <= 600:
        # A deferred Discord reply expires after 15 minutes; leave room to post the answer.
        raise ConfigError("chat.timeout_sec must be in (0, 600]")
    if ch.enabled and not cfg.api.enabled:
        raise ConfigError("chat.enabled needs api.enabled: the chat bot reads the book through the API")
    sc = cfg.screen
    if sc.interval_hours <= 0 or sc.min_usd_volume < 0 or not 0 < sc.max_spread_pct <= 5:
        raise ConfigError("screen.interval_hours must be > 0, min_usd_volume >= 0, max_spread_pct in (0, 5]")
    if sc.min_history_days < 91 or not 1 <= sc.top_n <= 20 or not 1 <= sc.max_pairs <= 200:
        # 91: the 90-day trend and correlation need 90 daily returns.
        raise ConfigError("screen.min_history_days must be >= 91, top_n in 1..20, max_pairs in 1..200")
    for k, v in cfg.pairs.items():
        if not k.isalnum() or not v.isalnum():
            raise ConfigError(f"[pairs] {k} = {v!r}: expected a symbol and a Kraken pair name like NEARUSD")
    unpriced = [a for a in cfg.universe if not cfg.kraken_pair(a)]
    if unpriced:
        # Without a pair an asset would never get a quote: say so here, not as a market alarm.
        raise ConfigError(f"no Kraken pair for {unpriced}: add them under [pairs], e.g. "
                          f'{unpriced[0]} = "{unpriced[0]}USD"')
    if cfg.risk.drawdown_resume_pct >= cfg.risk.drawdown_halt_pct:
        raise ConfigError("risk.drawdown_resume_pct must be below drawdown_halt_pct")
    if cfg.council.min_analysts < 1 or cfg.council.min_analysts > len(cfg.council.analysts):
        raise ConfigError("council.min_analysts must be between 1 and the analyst count")
    return cfg
