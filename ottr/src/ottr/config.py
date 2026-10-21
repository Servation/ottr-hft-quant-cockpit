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
        market=_section(Market, raw.get("market", {}), "market"),
        screen=_section(Screen, raw.get("screen", {}), "screen"),
        pairs={str(k).upper().strip(): str(v).strip() for k, v in raw.get("pairs", {}).items()},
        **_shared_sections(raw),
        **_runtime(data_dir),
    )
    _validate_shared(cfg)
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
    return cfg


def _shared_sections(raw: dict) -> dict:
    """The sections both desks share, parsed the same way."""
    return dict(
        risk=_section(Risk, raw.get("risk", {}), "risk"),
        council=_section(Council, raw.get("council", {}), "council"),
        engine=_section(Engine, raw.get("engine", {}), "engine"),
        alerts=_section(Alerts, raw.get("alerts", {}), "alerts"),
        api=_section(Api, raw.get("api", {}), "api"),
        chat=_section(Chat, raw.get("chat", {}), "chat"),
    )


def _runtime(data_dir: Optional[Path]) -> dict:
    """Where data lives, plus the secrets and IDs that only ever come from the environment."""
    return dict(
        data_dir=Path(data_dir or os.getenv("OTTR_DATA_DIR") or (ROOT / "data")),
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL") or None,
        api_key=os.getenv("OTTR_API_KEY") or None,
        discord_bot_token=(os.getenv("DISCORD_BOT_TOKEN") or "").strip() or None,
        chat_owner_id=_env_id("CEO_DISCORD_ID"),
        chat_allowed_ids=_env_ids("OTTR_CHAT_ALLOWED_IDS"),
        chat_guild_id=_env_id("DISCORD_GUILD_ID"),
    )


def _validate_shared(cfg) -> None:
    """Checks on the shared sections, for either desk's config."""
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
    if cfg.risk.drawdown_resume_pct >= cfg.risk.drawdown_halt_pct:
        raise ConfigError("risk.drawdown_resume_pct must be below drawdown_halt_pct")
    if cfg.council.min_analysts < 1 or cfg.council.min_analysts > len(cfg.council.analysts):
        raise ConfigError("council.min_analysts must be between 1 and the analyst count")


# -- US-markets desk (config.us.toml) -------------------------------------------------------
# Loaded by load_us(). At the cutover this becomes the only config and the crypto Config
# above is retired; until then the two live side by side so the running engine is unaffected.

US_CONFIG = ROOT / "config.us.toml"
ACCOUNT_TYPES = ("margin_1x", "cash")
US_CLASSES = ("equity_etf", "bond_etf", "cash_etf", "commodity_etf", "real_estate_etf", "stock")
FEEDS = ("iex", "sip")
# The screener needs 273 sessions of history, about 380 calendar days.
MIN_HISTORY_DAYS = 380


@dataclass(frozen=True)
class Account:
    type: str = "margin_1x"
    starting_cash: float = 10000.0


@dataclass(frozen=True)
class UsUniverse:
    core: Dict[str, str]                 # symbol -> asset class
    reference: Tuple[str, ...] = ()      # data only, never traded
    cash_symbol: str = "SGOV"
    satellites_max: int = 6
    sleeve_pct: float = 40.0


@dataclass(frozen=True)
class UsTrading:
    dry_run: bool = False
    min_trade_usd: float = 100.0
    max_trade_usd: float = 2500.0
    max_gross_pct: float = 90.0
    cash_reserve_pct: float = 2.0
    max_trades_per_meeting: int = 3
    decision_ttl_sec: float = 900.0
    slippage_pct: float = 0.0
    no_buy_last_min: float = 10.0


@dataclass(frozen=True)
class UsSizing:
    target_risk_pct: float = 1.5
    choppy_regime_factor: float = 0.25
    max_position_pct: float = 25.0
    missing_vol: float = 0.80
    vol_floor: Dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class UsCaps:
    by_class: Dict[str, float]
    by_symbol: Dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Schedule:
    meeting_times: Tuple[str, ...] = ("10:15", "15:15")
    min_before_close_min: int = 45


@dataclass(frozen=True)
class Data:
    feed: str = "iex"
    history_days: int = 400
    max_spread_pct: float = 1.0
    max_quote_age_sec: float = 120.0
    max_trade_gap_pct: float = 2.0
    max_close_gap_pct: float = 25.0

    def quote_limits(self):
        from ottr.market.alpaca import QuoteLimits
        return QuoteLimits(max_spread_pct=self.max_spread_pct, max_age_sec=self.max_quote_age_sec,
                           max_trade_gap_pct=self.max_trade_gap_pct, max_close_gap_pct=self.max_close_gap_pct)


@dataclass(frozen=True)
class UsConfig:
    account: Account
    universe: UsUniverse
    trading: UsTrading
    sizing: UsSizing
    caps: UsCaps
    risk: Risk
    schedule: Schedule
    data: Data
    council: Council
    engine: Engine
    data_dir: Path
    alerts: Alerts = field(default_factory=Alerts)
    api: Api = field(default_factory=Api)
    chat: Chat = field(default_factory=Chat)
    discord_webhook_url: Optional[str] = field(default=None, repr=False)
    api_key: Optional[str] = field(default=None, repr=False)
    discord_bot_token: Optional[str] = field(default=None, repr=False)
    chat_owner_id: Optional[int] = None
    chat_allowed_ids: Tuple[int, ...] = ()
    chat_guild_id: Optional[int] = None

    @property
    def core(self) -> Tuple[str, ...]:
        return tuple(self.universe.core)

    def asset_class(self, symbol: str) -> Optional[str]:
        """A core symbol's class. Satellites are stocks; their membership lives in the DB."""
        return self.universe.core.get(symbol)

    def cap_pct(self, symbol: str, asset_class: Optional[str] = None) -> Optional[float]:
        """Max % of equity in one position: a symbol override, else its class cap. None means
        uncapped (the cash ETF, which only ever holds swept cash)."""
        if symbol in self.caps.by_symbol:
            return self.caps.by_symbol[symbol]
        cls = asset_class or self.asset_class(symbol)
        if cls is None:
            raise KeyError(f"{symbol}: no asset class (not in the core universe)")
        return self.caps.by_class.get(cls)

    def vol_floor(self, asset_class: str) -> float:
        return self.sizing.vol_floor[asset_class]


def _symbol(raw: str, where: str) -> str:
    from ottr.market.symbols import normalize
    try:
        sym = normalize(raw)
    except ValueError as e:
        raise ConfigError(f"{where}: {e}")
    if sym != raw:
        # Strict on purpose: one spelling everywhere, or two entries could mean one stock.
        raise ConfigError(f"{where}: write {raw!r} as {sym!r}")
    return sym


def _hhmm(raw: str, where: str) -> str:
    try:
        h, m = (int(x) for x in str(raw).split(":"))
    except ValueError:
        raise ConfigError(f"{where}: {raw!r} is not HH:MM")
    if not (9, 30) <= (h, m) < (16, 0) or len(str(raw)) != 5:
        raise ConfigError(f"{where}: {raw!r} must be HH:MM within the session (09:30 to 15:59)")
    return str(raw)


def load_us(path: Path = US_CONFIG, data_dir: Optional[Path] = None) -> UsConfig:
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    uni_raw = dict(raw.get("universe", {}))
    core_raw = uni_raw.pop("core", {})
    core = {_symbol(k, "universe.core"): str(v) for k, v in core_raw.items()}
    reference = tuple(_symbol(s, "universe.reference") for s in uni_raw.pop("reference", []))
    universe = _section(UsUniverse, {**uni_raw, "core": core, "reference": reference}, "universe")

    trading_raw = dict(raw.get("trading", {}))
    dry = _env_bool("OTTR_DRY_RUN")
    if dry is not None:
        trading_raw["dry_run"] = dry
    if os.getenv("MAX_TRADE_USD"):
        trading_raw["max_trade_usd"] = float(os.environ["MAX_TRADE_USD"])

    sizing_raw = dict(raw.get("sizing", {}))
    sizing_raw["vol_floor"] = {str(k): float(v) for k, v in sizing_raw.get("vol_floor", {}).items()}
    caps_raw = dict(raw.get("caps", {}))
    by_symbol = {_symbol(k, "caps.symbol"): float(v) for k, v in caps_raw.pop("symbol", {}).items()}
    sched_raw = dict(raw.get("schedule", {}))
    if "meeting_times" in sched_raw:
        sched_raw["meeting_times"] = tuple(_hhmm(t, "schedule.meeting_times") for t in sched_raw["meeting_times"])

    cfg = UsConfig(
        account=_section(Account, raw.get("account", {}), "account"),
        universe=universe,
        trading=_section(UsTrading, trading_raw, "trading"),
        sizing=_section(UsSizing, sizing_raw, "sizing"),
        caps=UsCaps(by_class={str(k): float(v) for k, v in caps_raw.items()}, by_symbol=by_symbol),
        schedule=_section(Schedule, sched_raw, "schedule"),
        data=_section(Data, raw.get("data", {}), "data"),
        **_shared_sections(raw),
        **_runtime(data_dir),
    )
    unknown = set(raw) - {"account", "universe", "trading", "sizing", "caps", "risk", "schedule", "data",
                          "council", "engine", "alerts", "api", "chat"}
    if unknown:
        raise ConfigError(f"unknown sections {sorted(unknown)}")
    _validate_shared(cfg)
    _validate_us(cfg)
    return cfg


def _validate_us(cfg: UsConfig) -> None:
    a, u, t, s, c = cfg.account, cfg.universe, cfg.trading, cfg.sizing, cfg.caps
    if a.type not in ACCOUNT_TYPES:
        raise ConfigError(f"account.type must be one of {ACCOUNT_TYPES}, got {a.type!r}")
    if a.starting_cash <= 0:
        raise ConfigError("account.starting_cash must be positive")
    if not u.core:
        raise ConfigError("universe.core can't be empty")
    for sym, cls in u.core.items():
        if cls not in US_CLASSES:
            raise ConfigError(f"universe.core.{sym}: class must be one of {US_CLASSES}, got {cls!r}")
    if u.core.get(u.cash_symbol) != "cash_etf":
        raise ConfigError(f"universe.cash_symbol {u.cash_symbol!r} must be in universe.core as cash_etf")
    if set(u.reference) & set(u.core):
        raise ConfigError(f"universe.reference repeats traded symbols {sorted(set(u.reference) & set(u.core))}")
    if not 0 <= u.satellites_max <= 20 or not 0 < u.sleeve_pct <= 100:
        raise ConfigError("universe.satellites_max must be in 0..20 and sleeve_pct in (0, 100]")
    if t.min_trade_usd <= 0 or t.max_trade_usd < 0 or (t.max_trade_usd and t.max_trade_usd < t.min_trade_usd):
        raise ConfigError("trading: min_trade_usd > 0 and max_trade_usd 0 (none) or >= min_trade_usd")
    if not 0 < t.max_gross_pct <= 100 or not 0 <= t.cash_reserve_pct < 100 or t.slippage_pct < 0:
        raise ConfigError("trading: max_gross_pct in (0, 100], cash_reserve_pct in [0, 100), slippage_pct >= 0")
    if t.max_trades_per_meeting < 1 or t.decision_ttl_sec <= 0 or not 0 <= t.no_buy_last_min <= 60:
        raise ConfigError("trading: max_trades_per_meeting >= 1, decision_ttl_sec > 0, no_buy_last_min in 0..60")
    if s.target_risk_pct <= 0 or s.missing_vol <= 0 or not 0 < s.choppy_regime_factor <= 1:
        raise ConfigError("sizing: target_risk_pct > 0, missing_vol > 0, choppy_regime_factor in (0, 1]")
    if not 0 < s.max_position_pct <= 100:
        raise ConfigError("sizing.max_position_pct must be in (0, 100]")
    missing = [k for k in US_CLASSES if s.vol_floor.get(k, 0) <= 0]
    if missing or set(s.vol_floor) - set(US_CLASSES):
        raise ConfigError(f"sizing.vol_floor needs a positive floor for each of {US_CLASSES} "
                          f"(missing or bad: {missing}, unknown: {sorted(set(s.vol_floor) - set(US_CLASSES))})")
    uncapped = [k for k in US_CLASSES if k != "cash_etf" and k not in c.by_class]
    if uncapped or set(c.by_class) - set(US_CLASSES):
        raise ConfigError(f"caps needs a cap for every class except cash_etf (missing: {uncapped}, "
                          f"unknown: {sorted(set(c.by_class) - set(US_CLASSES))})")
    for k, v in {**c.by_class, **c.by_symbol}.items():
        if not 0 < v <= 100:
            raise ConfigError(f"caps.{k} must be in (0, 100]")
    times = cfg.schedule.meeting_times
    if not times or list(times) != sorted(set(times)):
        raise ConfigError("schedule.meeting_times must be distinct and in order")
    if not 0 <= cfg.schedule.min_before_close_min <= 120:
        raise ConfigError("schedule.min_before_close_min must be in 0..120")
    d = cfg.data
    if d.feed not in FEEDS:
        raise ConfigError(f"data.feed must be one of {FEEDS}, got {d.feed!r}")
    if d.history_days < MIN_HISTORY_DAYS:
        raise ConfigError(f"data.history_days must be >= {MIN_HISTORY_DAYS} (the screener needs 273 sessions)")
    if min(d.max_spread_pct, d.max_quote_age_sec, d.max_trade_gap_pct, d.max_close_gap_pct) <= 0:
        raise ConfigError("data: quote limits must all be positive")
