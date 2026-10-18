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
    discord_webhook_url: Optional[str] = None

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
        data_dir=Path(data_dir or os.getenv("OTTR_DATA_DIR") or (ROOT / "data")),
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL") or None,
    )
    if cfg.risk.drawdown_resume_pct >= cfg.risk.drawdown_halt_pct:
        raise ConfigError("risk.drawdown_resume_pct must be below drawdown_halt_pct")
    if cfg.council.min_analysts < 1 or cfg.council.min_analysts > len(cfg.council.analysts):
        raise ConfigError("council.min_analysts must be between 1 and the analyst count")
    return cfg
