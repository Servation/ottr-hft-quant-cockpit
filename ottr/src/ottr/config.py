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
from typing import Dict, List, Optional

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


LOOPBACK = ("127.0.0.1", "localhost", "::1")


@dataclass(frozen=True)
class Api:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765


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
    discord_webhook_url: Optional[str] = None
    api_key: Optional[str] = None      # OTTR_API_KEY: guards the API's kill switch

    def cap_pct(self, asset: str) -> float:
        return self.caps.get(asset.upper(), self.caps.get("default", 35.0))


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
        data_dir=Path(data_dir or os.getenv("OTTR_DATA_DIR") or (ROOT / "data")),
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL") or None,
        api_key=os.getenv("OTTR_API_KEY") or None,
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
    if cfg.risk.drawdown_resume_pct >= cfg.risk.drawdown_halt_pct:
        raise ConfigError("risk.drawdown_resume_pct must be below drawdown_halt_pct")
    if cfg.council.min_analysts < 1 or cfg.council.min_analysts > len(cfg.council.analysts):
        raise ConfigError("council.min_analysts must be between 1 and the analyst count")
    return cfg
