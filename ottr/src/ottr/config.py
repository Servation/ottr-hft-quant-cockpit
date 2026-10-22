"""
Typed configuration: config.toml plus a few env overrides.

What it does: parses config.toml (the US-markets desk: account, universe, sizing, caps,
fees, schedule, data and events feeds, plus the operational sections) into frozen
dataclasses and validates ranges once at startup. Why: v1 read loose dict keys all over
the code, so a typo silently fell back to a default. Here a bad or missing value fails at
load time with a clear message. Secrets and IDs only ever come from the environment (or
ottr/.env).
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ottr.broker.fees import FeeSchedule, FeeScheduleError
from ottr.broker.fees import from_rows as fee_schedule_from_rows

ROOT = Path(__file__).resolve().parents[2]  # .../ottr
DEFAULT_CONFIG = ROOT / "config.toml"


@dataclass(frozen=True)
class Risk:
    enabled: bool = True
    # Trailing stop, set once when a position opens: stop_vol_mult x the move the asset's
    # volatility implies over stop_horizon_sessions, clamped to [stop_min_pct, stop_max_pct].
    stop_vol_mult: float = 2.0
    stop_horizon_sessions: int = 10
    stop_min_pct: float = 3.0
    stop_max_pct: float = 15.0
    stop_unknown_vol_pct: float = 10.0    # when the volatility is unknown
    open_delay_min: float = 5.0           # stops and trims wait this long after the open
    drawdown_halt_pct: float = 15.0
    drawdown_resume_pct: float = 10.0
    drawdown_derisk: bool = True
    drawdown_derisk_pct: float = 25.0
    halt_max_hours: float = 72.0
    trim_band_pct: float = 5.0
    cooldown_sec: float = 900.0
    min_equity_points: int = 24


@dataclass(frozen=True)
class Council:
    analysts: List[str] = field(default_factory=lambda: ["claude", "gpt", "gemini"])
    chair: str = "claude"
    min_analysts: int = 2
    timeout_sec: float = 300.0
    claude_model: str = "opus"
    gemini_model: str = "gemini-3.8-flash-high"
    reasoning_effort: str = "high"


@dataclass(frozen=True)
class Watch:
    """The desk watcher (`engine/watcher.py`): a model that reviews the market between
    meetings, sets conditions for code to watch, and may call a meeting."""
    enabled: bool = True
    model: str = "claude"                  # claude | gpt | gemini (claude runs council.claude_model)
    effort: str = ""                       # "" = the council's reasoning effort
    check_every_min: float = 30.0          # a review this often in session (and after every meeting)
    max_triggers: int = 8
    max_called_per_day: int = 4
    min_gap_min: float = 45.0              # between meetings, called or scheduled
    timeout_sec: float = 300.0


@dataclass(frozen=True)
class Engine:
    market_interval_sec: float = 60.0  # market data upkeep (calendar, bars, quote health)
    jobs_interval_sec: float = 60.0    # scheduled jobs: fees, sweep, events feeds, deferred orders
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


def read_env_file(path: Path) -> Dict[str, str]:
    """KEY -> VALUE from a .env file, or {} if there is none. Blank lines and # comments
    are skipped, an `export ` prefix is allowed, and matching quotes around a value are
    removed; spaces inside a value are kept. The first line for a key wins."""
    if not path.is_file():
        return {}
    out: Dict[str, str] = {}
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
        out.setdefault(key, value)
    return out


def load_env_file(path: Path) -> List[str]:
    """Read KEY=VALUE lines into os.environ without overriding variables already set.

    Why: the scheduled task gets the user's registry environment, not a shell's, so
    secrets (DISCORD_WEBHOOK_URL, OTTR_API_KEY) live in a gitignored ottr/.env instead.
    Only the CLI entry point calls this (live tests read the feed keys with
    `read_env_file`). Returns the keys it set.
    """
    set_keys = []
    for key, value in read_env_file(path).items():
        if key not in os.environ:
            os.environ[key] = value
            set_keys.append(key)
    return set_keys


def _shared_sections(raw: dict) -> dict:
    """Operational sections: risk, council, engine, alerts, api, chat."""
    return dict(
        risk=_section(Risk, raw.get("risk", {}), "risk"),
        council=_section(Council, raw.get("council", {}), "council"),
        watch=_section(Watch, raw.get("watch", {}), "watch"),
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
    """Checks on the operational sections."""
    if cfg.api.host not in LOOPBACK:
        # The API has no TLS and its reads are unauthenticated: it must never face a network.
        raise ConfigError(f"api.host must be a loopback address {LOOPBACK}, got {cfg.api.host!r}")
    if not 0 <= cfg.api.port <= 65535:
        raise ConfigError("api.port must be in 0..65535")
    if cfg.api_key is not None and len(cfg.api_key) < 16:
        raise ConfigError("OTTR_API_KEY must be at least 16 characters")
    for name in ("market_interval_sec", "jobs_interval_sec", "risk_interval_sec", "equity_interval_sec",
                 "watchdog_interval_sec"):
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
    r = cfg.risk
    if r.drawdown_resume_pct >= r.drawdown_halt_pct:
        raise ConfigError("risk.drawdown_resume_pct must be below drawdown_halt_pct")
    if not 0 < r.stop_min_pct <= r.stop_unknown_vol_pct <= r.stop_max_pct <= 50:
        raise ConfigError("risk: 0 < stop_min_pct <= stop_unknown_vol_pct <= stop_max_pct <= 50")
    if r.stop_vol_mult <= 0 or not 1 <= r.stop_horizon_sessions <= 63 or not 0 <= r.open_delay_min <= 60:
        raise ConfigError("risk: stop_vol_mult > 0, stop_horizon_sessions in 1..63, open_delay_min in 0..60")
    if cfg.council.min_analysts < 1 or cfg.council.min_analysts > len(cfg.council.analysts):
        raise ConfigError("council.min_analysts must be between 1 and the analyst count")


REQUIRED_FEES = ("sec", "taf", "cat")   # the pass-through fees every US equity fill can owe
ACCOUNT_TYPES = ("margin_1x", "cash")
ASSET_CLASSES = ("equity_etf", "bond_etf", "cash_etf", "commodity_etf", "real_estate_etf", "stock")
FEEDS = ("iex", "sip")
# The screener needs 273 sessions of history, about 380 calendar days.
MIN_HISTORY_DAYS = 380


@dataclass(frozen=True)
class Account:
    type: str = "margin_1x"
    starting_cash: float = 10000.0


@dataclass(frozen=True)
class Universe:
    core: Dict[str, str]                 # symbol -> asset class
    reference: Tuple[str, ...] = ()      # data only, never traded
    sectors: Tuple[str, ...] = ()        # breadth: how many sit above their 50-day average
    cash_symbol: str = "SGOV"
    satellites_max: int = 6
    sleeve_pct: float = 40.0


@dataclass(frozen=True)
class Trading:
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
class Sizing:
    target_risk_pct: float = 1.5
    choppy_regime_factor: float = 0.25
    max_position_pct: float = 25.0
    missing_vol: float = 0.80
    vol_floor: Dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Caps:
    by_class: Dict[str, float]
    by_symbol: Dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Schedule:
    meeting_times: Tuple[str, ...] = ("10:15", "15:15")
    min_before_close_min: int = 45


@dataclass(frozen=True)
class Screen:
    """The weekly stock screener (ottr/universe): which stocks may fill the satellite
    sleeve. Deterministic code ranks them; no model can add a symbol."""
    enabled: bool = True
    weekday: str = "Sat"              # when it runs, New York time
    at: str = "08:00"
    min_price: float = 10.0
    min_dollar_volume: float = 50_000_000.0   # 20-session median, dollars a day
    pool_max: int = 1200              # the most liquid stocks kept for the full measurement
    history_days: int = 430           # calendar days of daily bars per stock (>= min_sessions)
    min_sessions: int = 273           # 12-1 month momentum needs 252 sessions plus a month
    vol_min_pct: float = 8.0
    vol_max_pct: float = 70.0
    keep_rank: int = 12               # a satellite keeps its place while it ranks this high
    min_tenure_days: int = 14         # and can't be ranked out before this
    max_changes: int = 2              # rank-outs and swaps a week
    challenger_margin: float = 15.0   # score points a challenger must beat the weakest by
    max_correlation: float = 0.75     # with any member: the candidate is passed over
    cooldown_days: int = 28           # a demoted stock waits this long to return


@dataclass(frozen=True)
class Tax:
    """Rates for the tax ESTIMATES (`ledger/tax.py`): set them to your own. Not tax advice."""
    federal_ordinary_pct: float = 24.0        # marginal rate on ordinary income and short-term gains
    federal_long_term_pct: float = 15.0       # long-term gains and qualified dividends
    collectibles_max_pct: float = 28.0        # long-term gains on collectibles, at most this
    state_pct: float = 0.0
    niit_pct: float = 0.0                     # net investment income tax (3.8 above its threshold)
    collectibles: Tuple[str, ...] = ("GLD", "SLV")
    qualified_dividend_classes: Tuple[str, ...] = ("equity_etf", "stock")


@dataclass(frozen=True)
class Sweep:
    enabled: bool = True
    at: str = "15:45"             # New York time; an early close moves it back the same distance
    keep_cash_pct: float = 5.0    # buying power above this % of equity goes into the cash ETF


@dataclass(frozen=True)
class Data:
    feed: str = "iex"
    history_days: int = 400
    max_spread_pct: float = 1.0
    max_quote_age_sec: float = 120.0
    max_trade_gap_pct: float = 2.0
    max_close_gap_pct: float = 25.0
    quote_ttl_sec: float = 30.0       # snapshots are re-fetched after this (an order re-fetches after 10s)
    vol_sessions: int = 60            # realized vol for sizing: sessions of daily returns, annualized by 252
    er_sessions: int = 20             # efficiency-ratio window for the trending/choppy regime
    er_threshold: float = 0.30

    def quote_limits(self):
        from ottr.market.alpaca import QuoteLimits
        return QuoteLimits(max_spread_pct=self.max_spread_pct, max_age_sec=self.max_quote_age_sec,
                           max_trade_gap_pct=self.max_trade_gap_pct, max_close_gap_pct=self.max_close_gap_pct)


@dataclass(frozen=True)
class EventFeeds:
    """The events and macro feeds (ottr/events): what to fetch, how often, and when data
    is too old to trust. Keys and contact details come from the environment, not here."""
    macro: Dict[str, str]                       # FRED series id -> label shown to the council
    earnings_horizon: str = "6month"
    macro_every_hours: float = 12.0
    earnings_every_hours: float = 24.0
    corporate_actions_every_hours: float = 4.0
    sec_every_days: float = 7.0
    max_age_hours: float = 72.0                 # older earnings / corporate actions count as unknown
    corporate_actions_lookback_days: int = 60
    corporate_actions_ahead_days: int = 120
    macro_history_days: int = 400
    macro_stale_days: int = 5


@dataclass(frozen=True)
class Config:
    account: Account
    universe: Universe
    trading: Trading
    sizing: Sizing
    caps: Caps
    risk: Risk
    schedule: Schedule
    data: Data
    fees: FeeSchedule
    sweep: Sweep
    events: EventFeeds
    council: Council
    engine: Engine
    data_dir: Path
    alerts: Alerts = field(default_factory=Alerts)
    api: Api = field(default_factory=Api)
    chat: Chat = field(default_factory=Chat)
    screen: Screen = field(default_factory=Screen)
    tax: Tax = field(default_factory=Tax)
    watch: Watch = field(default_factory=Watch)
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


def load(path: Path = DEFAULT_CONFIG, data_dir: Optional[Path] = None) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    uni_raw = dict(raw.get("universe", {}))
    core_raw = uni_raw.pop("core", {})
    core = {_symbol(k, "universe.core"): str(v) for k, v in core_raw.items()}
    reference = tuple(_symbol(s, "universe.reference") for s in uni_raw.pop("reference", []))
    sectors = tuple(_symbol(s, "universe.sectors") for s in uni_raw.pop("sectors", []))
    universe = _section(Universe, {**uni_raw, "core": core, "reference": reference, "sectors": sectors},
                        "universe")

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
    fees_raw = dict(raw.get("fees", {}))
    rate_rows = fees_raw.pop("rate", [])
    if fees_raw:
        raise ConfigError(f"[fees] unknown keys: {sorted(fees_raw)} (rates go in [[fees.rate]] tables)")
    try:
        fees = fee_schedule_from_rows(rate_rows)
    except FeeScheduleError as e:
        raise ConfigError(f"fees: {e}")
    sweep_raw = dict(raw.get("sweep", {}))
    if "at" in sweep_raw:
        sweep_raw["at"] = _hhmm(sweep_raw["at"], "sweep.at")
    sched_raw = dict(raw.get("schedule", {}))
    if "meeting_times" in sched_raw:
        sched_raw["meeting_times"] = tuple(_hhmm(t, "schedule.meeting_times") for t in sched_raw["meeting_times"])
    tax_raw = dict(raw.get("tax", {}))
    if "collectibles" in tax_raw:
        tax_raw["collectibles"] = tuple(_symbol(s, "tax.collectibles") for s in tax_raw["collectibles"])
    if "qualified_dividend_classes" in tax_raw:
        tax_raw["qualified_dividend_classes"] = tuple(str(x) for x in tax_raw["qualified_dividend_classes"])
    events_raw = dict(raw.get("events", {}))
    events_raw["macro"] = {str(k): str(v) for k, v in events_raw.get("macro", {}).items()}

    cfg = Config(
        account=_section(Account, raw.get("account", {}), "account"),
        universe=universe,
        trading=_section(Trading, trading_raw, "trading"),
        sizing=_section(Sizing, sizing_raw, "sizing"),
        caps=Caps(by_class={str(k): float(v) for k, v in caps_raw.items()}, by_symbol=by_symbol),
        schedule=_section(Schedule, sched_raw, "schedule"),
        data=_section(Data, raw.get("data", {}), "data"),
        fees=fees,
        sweep=_section(Sweep, sweep_raw, "sweep"),
        events=_section(EventFeeds, events_raw, "events"),
        screen=_section(Screen, raw.get("screen", {}), "screen"),
        tax=_section(Tax, tax_raw, "tax"),
        **_shared_sections(raw),
        **_runtime(data_dir),
    )
    unknown = set(raw) - {"account", "universe", "trading", "sizing", "caps", "risk", "schedule", "data",
                          "fees", "sweep", "events", "screen", "tax", "watch", "council", "engine", "alerts", "api", "chat"}
    if unknown:
        raise ConfigError(f"unknown sections {sorted(unknown)}")
    _validate_shared(cfg)
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    a, u, t, s, c = cfg.account, cfg.universe, cfg.trading, cfg.sizing, cfg.caps
    if a.type not in ACCOUNT_TYPES:
        raise ConfigError(f"account.type must be one of {ACCOUNT_TYPES}, got {a.type!r}")
    if a.starting_cash <= 0:
        raise ConfigError("account.starting_cash must be positive")
    if not u.core:
        raise ConfigError("universe.core can't be empty")
    for sym, cls in u.core.items():
        if cls not in ASSET_CLASSES:
            raise ConfigError(f"universe.core.{sym}: class must be one of {ASSET_CLASSES}, got {cls!r}")
    if u.core.get(u.cash_symbol) != "cash_etf":
        raise ConfigError(f"universe.cash_symbol {u.cash_symbol!r} must be in universe.core as cash_etf")
    if set(u.reference) & set(u.core):
        raise ConfigError(f"universe.reference repeats traded symbols {sorted(set(u.reference) & set(u.core))}")
    unknown_sectors = set(u.sectors) - set(u.reference) - set(u.core)
    if unknown_sectors:
        raise ConfigError(f"universe.sectors must be core or reference symbols (not {sorted(unknown_sectors)})")
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
    missing = [k for k in ASSET_CLASSES if s.vol_floor.get(k, 0) <= 0]
    if missing or set(s.vol_floor) - set(ASSET_CLASSES):
        raise ConfigError(f"sizing.vol_floor needs a positive floor for each of {ASSET_CLASSES} "
                          f"(missing or bad: {missing}, unknown: {sorted(set(s.vol_floor) - set(ASSET_CLASSES))})")
    uncapped = [k for k in ASSET_CLASSES if k != "cash_etf" and k not in c.by_class]
    if uncapped or set(c.by_class) - set(ASSET_CLASSES):
        raise ConfigError(f"caps needs a cap for every class except cash_etf (missing: {uncapped}, "
                          f"unknown: {sorted(set(c.by_class) - set(ASSET_CLASSES))})")
    for k, v in {**c.by_class, **c.by_symbol}.items():
        if not 0 < v <= 100:
            raise ConfigError(f"caps.{k} must be in (0, 100]")
    times = cfg.schedule.meeting_times
    if not times or list(times) != sorted(set(times)):
        raise ConfigError("schedule.meeting_times must be distinct and in order")
    if not 0 <= cfg.schedule.min_before_close_min <= 120:
        raise ConfigError("schedule.min_before_close_min must be in 0..120")
    missing_fees = [f for f in REQUIRED_FEES if f not in cfg.fees.fees]
    if missing_fees:
        raise ConfigError(f"fees: no [[fees.rate]] for {missing_fees}: a missing fee would silently be $0")
    if not 0 <= cfg.sweep.keep_cash_pct < 100 or cfg.sweep.keep_cash_pct < t.cash_reserve_pct:
        # The sweep would otherwise try to invest the reserve the gate keeps back.
        raise ConfigError("sweep.keep_cash_pct must be in [trading.cash_reserve_pct, 100)")
    d = cfg.data
    if d.feed not in FEEDS:
        raise ConfigError(f"data.feed must be one of {FEEDS}, got {d.feed!r}")
    if d.history_days < MIN_HISTORY_DAYS:
        raise ConfigError(f"data.history_days must be >= {MIN_HISTORY_DAYS} (the screener needs 273 sessions)")
    if min(d.max_spread_pct, d.max_quote_age_sec, d.max_trade_gap_pct, d.max_close_gap_pct) <= 0:
        raise ConfigError("data: quote limits must all be positive")
    if not 5 <= d.quote_ttl_sec <= d.max_quote_age_sec:
        # A cached snapshot older than the quote age limit could only ever be judged stale.
        raise ConfigError("data.quote_ttl_sec must be in 5..max_quote_age_sec")
    if not 10 <= d.vol_sessions <= 252 or not 5 <= d.er_sessions <= 120 or not 0 < d.er_threshold < 1:
        raise ConfigError("data: vol_sessions in 10..252, er_sessions in 5..120, er_threshold in (0, 1)")
    _validate_events(cfg.events)
    _validate_screen(cfg.screen)
    w = cfg.watch
    if w.model not in ("claude", "gpt", "gemini"):
        raise ConfigError(f"watch.model must be claude, gpt or gemini, got {w.model!r}")
    if not 5 <= w.check_every_min <= 240 or not 0 <= w.max_triggers <= 20:
        raise ConfigError("watch: check_every_min in 5..240 and max_triggers in 0..20")
    if not 0 <= w.max_called_per_day <= 10 or not 15 <= w.min_gap_min <= 240 or not 30 <= w.timeout_sec <= 900:
        raise ConfigError("watch: max_called_per_day in 0..10, min_gap_min in 15..240, timeout_sec in 30..900")
    tx = cfg.tax
    rates = (tx.federal_ordinary_pct, tx.federal_long_term_pct, tx.collectibles_max_pct, tx.state_pct, tx.niit_pct)
    if not all(0 <= r <= 60 for r in rates):
        raise ConfigError("tax: every rate must be a percent in 0..60")
    if set(tx.qualified_dividend_classes) - set(ASSET_CLASSES):
        raise ConfigError(f"tax.qualified_dividend_classes must be asset classes {ASSET_CLASSES}")


def _validate_screen(s: Screen) -> None:
    from ottr.universe import WEEKDAYS
    if s.weekday not in WEEKDAYS:
        raise ConfigError(f"screen.weekday must be one of {WEEKDAYS}, got {s.weekday!r}")
    try:
        h, m = (int(x) for x in s.at.split(":"))
    except ValueError:
        raise ConfigError(f"screen.at: {s.at!r} is not HH:MM")
    if not (0 <= h < 24 and 0 <= m < 60) or len(s.at) != 5:
        raise ConfigError(f"screen.at: {s.at!r} is not HH:MM")
    if s.min_price <= 0 or s.min_dollar_volume <= 0 or s.pool_max < 10:
        raise ConfigError("screen: min_price > 0, min_dollar_volume > 0, pool_max >= 10")
    if s.min_sessions < 273:
        raise ConfigError("screen.min_sessions must be >= 273 (12-1 month momentum needs 252 + 21)")
    if s.history_days * 250 / 365 < s.min_sessions + 5:
        # About 250 sessions a year: fewer calendar days could never reach min_sessions.
        raise ConfigError(f"screen.history_days {s.history_days} is too short for {s.min_sessions} sessions")
    if not 0 < s.vol_min_pct < s.vol_max_pct <= 300:
        raise ConfigError("screen: 0 < vol_min_pct < vol_max_pct <= 300")
    if s.keep_rank < 1 or s.min_tenure_days < 0 or s.max_changes < 0 or s.cooldown_days < 0:
        raise ConfigError("screen: keep_rank >= 1; min_tenure_days, max_changes and cooldown_days >= 0")
    if not 0 <= s.challenger_margin <= 100 or not 0 < s.max_correlation <= 1:
        raise ConfigError("screen: challenger_margin in 0..100 and max_correlation in (0, 1]")


def _validate_events(e: EventFeeds) -> None:
    from ottr.events.earnings import HORIZONS
    if e.earnings_horizon not in HORIZONS:
        raise ConfigError(f"events.earnings_horizon must be one of {HORIZONS}, got {e.earnings_horizon!r}")
    if min(e.macro_every_hours, e.earnings_every_hours, e.corporate_actions_every_hours, e.sec_every_days) <= 0:
        raise ConfigError("events: every *_every_* interval must be positive")
    if e.max_age_hours <= max(e.earnings_every_hours, e.corporate_actions_every_hours):
        # Otherwise data would count as unknown between two on-time refreshes.
        raise ConfigError("events.max_age_hours must be longer than the earnings and corporate-actions intervals")
    if not 7 <= e.corporate_actions_lookback_days <= 365 or not 14 <= e.corporate_actions_ahead_days <= 365:
        raise ConfigError("events: corporate_actions_lookback_days in 7..365 and corporate_actions_ahead_days in 14..365")
    if e.macro_history_days < 30 or e.macro_stale_days < 1:
        raise ConfigError("events: macro_history_days >= 30 and macro_stale_days >= 1")
    if not e.macro:
        raise ConfigError("events.macro needs at least one FRED series")
    for sid, label in e.macro.items():
        if not re.fullmatch(r"[A-Z0-9_]{2,40}", sid) or not label.strip():
            raise ConfigError(f"events.macro.{sid}: needs an upper-case FRED series id and a label")
