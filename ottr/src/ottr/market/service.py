"""
MarketData service + MarketSnapshot: the one view of the market everything reads.

What it does: caches quotes (Kraken, then Coinbase for any asset Kraken can't price
freshly) and closed daily candles (Kraken, archived locally), and builds an immutable
snapshot with indicators and explicit data-quality flags. Why: the council, the risk
monitor and the broker must agree on the same numbers, and "unknown" must stay visibly
unknown instead of becoming a 0 that downstream math trusts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Dict, List, Optional, Protocol, Tuple

import httpx

from ottr.config import Config
from ottr.market import coinbase, indicators as ind
from ottr.market.kraken import Kraken, KrakenError
from ottr.market.types import MarketDataError, Quote

logger = logging.getLogger(__name__)

DAY = 86400
MIN_HISTORY = 60  # closes needed before EMA50/MACD/vol are trustworthy


class CandleArchive(Protocol):
    def load_candles(self, asset: str) -> List[Tuple[int, float]]: ...
    def save_candles(self, asset: str, rows: List[Tuple[int, float]]) -> None: ...


@dataclass(frozen=True)
class AssetView:
    asset: str
    quote: Optional[Quote]
    closes: Tuple[float, ...]
    last_close_ts: Optional[int]
    ema20: Optional[float] = None
    ema50: Optional[float] = None
    rsi14: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    er: Optional[float] = None
    regime: str = "UNKNOWN"
    vol: Optional[float] = None
    chg_1d: Optional[float] = None
    chg_7d: Optional[float] = None
    chg_30d: Optional[float] = None
    hi_90d: Optional[float] = None
    lo_90d: Optional[float] = None
    corr_btc: Optional[float] = None
    flags: Tuple[str, ...] = ()


@dataclass(frozen=True)
class MarketSnapshot:
    as_of: float
    assets: Dict[str, AssetView]
    fear_greed: Optional[Tuple[int, str]] = None
    warnings: Tuple[str, ...] = ()

    def quote(self, asset: str) -> Optional[Quote]:
        v = self.assets.get(asset)
        return v.quote if v else None

    def packet(self) -> str:
        """Deterministic text block for the models, anchored in time."""
        ts = datetime.fromtimestamp(self.as_of, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            f"AS OF: {ts}. Judge ONLY the numbers below. Anything you remember about crypto "
            "prices from training is out of date and must not be used.",
            "Indicators use CLOSED daily candles (Kraken). Price is the live mid. "
            "vol = 30d annualized; ER = efficiency ratio (>=0.30 TRENDING, else CHOPPY); "
            "corrBTC = 30d daily-return correlation with BTC. 'n/a' means unknown, not zero.",
            "",
        ]
        for a, v in self.assets.items():
            q = v.quote
            px = f"${q.mid:,.6g}" if q else "n/a"
            lines.append(
                f"{a}: price {px} | 1d {_pct(v.chg_1d)} 7d {_pct(v.chg_7d)} 30d {_pct(v.chg_30d)} | "
                f"90d range {_num(v.lo_90d)}-{_num(v.hi_90d)} | EMA20 {_num(v.ema20)} EMA50 {_num(v.ema50)} | "
                f"RSI {_num(v.rsi14, 1)} | MACD {_num(v.macd)} vs signal {_num(v.macd_signal)} | "
                f"ER {_num(v.er, 2)} {v.regime} | vol {_pct(v.vol, 0)} | corrBTC {_num(v.corr_btc, 2)}"
                + (f" | FLAGS: {', '.join(v.flags)}" if v.flags else "")
            )
        if self.fear_greed:
            lines.append(f"\nCrypto Fear & Greed index: {self.fear_greed[0]} ({self.fear_greed[1]})")
        if self.warnings:
            lines.append("DATA WARNINGS: " + "; ".join(self.warnings))
        return "\n".join(lines)


def _pct(x: Optional[float], digits: int = 1) -> str:
    return "n/a" if x is None else f"{x * 100:+.{digits}f}%"


def _num(x: Optional[float], digits: int = 4) -> str:
    return "n/a" if x is None else f"{x:,.{digits}g}" if abs(x) < 1000 else f"{x:,.0f}"


def build_view(asset: str, quote: Optional[Quote], rows: List[Tuple[int, float]],
               btc_closes: Optional[List[float]], cfg: Config) -> AssetView:
    closes = [c for _, c in rows]
    flags: List[str] = []
    if len(closes) < MIN_HISTORY:
        flags.append("insufficient_history")
    if any(b - a != DAY for (a, _), (b, _) in zip(rows[-91:], rows[-90:])):
        flags.append("candle_gap")
    if quote and quote.source != "kraken":
        flags.append(f"quote_from_{quote.source}")
    if quote is None:
        flags.append("no_live_quote")
    warm = len(closes) >= MIN_HISTORY
    m = ind.macd(closes) if warm else None
    er = ind.efficiency_ratio(closes, cfg.market.regime_er_window)
    ref = quote.mid if quote else (closes[-1] if closes else None)

    def chg(days: int) -> Optional[float]:
        if ref is None or len(closes) < days:
            return None
        base = closes[-days]
        return ref / base - 1 if base > 0 else None

    window = closes[-90:]
    return AssetView(
        asset=asset, quote=quote, closes=tuple(closes),
        last_close_ts=rows[-1][0] if rows else None,
        ema20=ind.ema(closes, 20) if warm else None,
        ema50=ind.ema(closes, 50) if warm else None,
        rsi14=ind.rsi(closes, 14) if warm else None,
        macd=m[0] if m else None, macd_signal=m[1] if m else None,
        er=er, regime=ind.regime(er, cfg.market.regime_er_threshold),
        vol=ind.annualized_vol(closes, cfg.market.vol_window_days),
        chg_1d=chg(1), chg_7d=chg(7), chg_30d=chg(30),
        hi_90d=max(window) if window else None, lo_90d=min(window) if window else None,
        corr_btc=(None if asset == "BTC" or not btc_closes
                  else ind.return_correlation(closes, btc_closes, cfg.market.vol_window_days)),
        flags=tuple(flags),
    )


class MarketData:
    def __init__(self, cfg: Config, kraken: Optional[Kraken] = None,
                 fallback: Callable[..., Awaitable[Dict[str, Quote]]] = coinbase.quotes,
                 archive: Optional[CandleArchive] = None,
                 fear_greed: Optional[Callable[[], Awaitable[Optional[Tuple[int, str]]]]] = None) -> None:
        self.cfg = cfg
        self.kraken = kraken or Kraken()
        self.fallback = fallback
        self.archive = archive
        self._fear_greed_fn = fear_greed or fetch_fear_greed
        self._quotes: Dict[str, Quote] = {}
        self._quotes_ts = 0.0
        self._candles: Dict[str, List[Tuple[int, float]]] = {}
        self._candles_ts = 0.0
        self._fng: Optional[Tuple[int, str]] = None
        self._fng_ts = 0.0
        self._lock = asyncio.Lock()

    async def quotes(self, force: bool = False) -> Dict[str, Quote]:
        """Freshest quote per asset. Kraken first; Coinbase fills in stale/missing assets."""
        async with self._lock:
            now = time.time()
            if not force and self._quotes and now - self._quotes_ts < self.cfg.market.spot_ttl_sec:
                return dict(self._quotes)
            pairs = {a: p for a in self.cfg.universe if (p := self.cfg.kraken_pair(a))}
            fresh: Dict[str, Quote] = {}
            try:
                fresh = await self.kraken.quotes(pairs)
            except KrakenError as e:
                logger.warning("Kraken quotes failed: %s", e)
            missing = [a for a in self.cfg.universe if a not in fresh]
            if missing:
                try:
                    fresh.update(await self.fallback(missing))
                except Exception as e:  # fallback is best-effort by design
                    logger.warning("Fallback quotes failed: %s", e)
            for a, q in fresh.items():
                self._quotes[a] = q
            self._quotes_ts = now
            if not self._quotes:
                raise MarketDataError("no quotes from Kraken or Coinbase and no cache")
            return dict(self._quotes)

    async def candles(self, force: bool = False) -> Dict[str, List[Tuple[int, float]]]:
        now = time.time()
        if not force and self._candles and now - self._candles_ts < self.cfg.market.candles_ttl_sec:
            return dict(self._candles)
        for asset in self.cfg.universe:
            pair = self.cfg.kraken_pair(asset)
            if not pair:
                continue
            rows = self.archive.load_candles(asset) if self.archive and asset not in self._candles else self._candles.get(asset, [])
            try:
                new = await self.kraken.daily_closes(pair)
                merged = dict(rows)
                merged.update(dict(new))
                rows = sorted(merged.items())
                if self.archive:
                    self.archive.save_candles(asset, new)
            except KrakenError as e:
                logger.warning("Candles for %s failed (%s); using %d archived rows", asset, e, len(rows))
            self._candles[asset] = rows
            await asyncio.sleep(0.4)  # Kraken public limit is ~1 req/s
        self._candles_ts = now
        return dict(self._candles)

    async def fear_greed(self) -> Optional[Tuple[int, str]]:
        if time.time() - self._fng_ts < 3600:
            return self._fng
        try:
            self._fng = await self._fear_greed_fn()
        except Exception as e:
            logger.info("Fear & Greed unavailable: %s", e)
            self._fng = None
        self._fng_ts = time.time()
        return self._fng

    async def snapshot(self) -> MarketSnapshot:
        quotes = await self.quotes()
        candles = await self.candles()
        fng = await self.fear_greed()
        btc = [c for _, c in candles.get("BTC", [])]
        views = {a: build_view(a, quotes.get(a), candles.get(a, []), btc, self.cfg)
                 for a in self.cfg.universe}
        warnings = []
        now = time.time()
        for a, v in views.items():
            if v.quote and v.quote.age(now) > self.cfg.trading.max_data_age_sec:
                warnings.append(f"{a} quote is {v.quote.age(now):.0f}s old")
        return MarketSnapshot(as_of=now, assets=views, fear_greed=fng, warnings=tuple(warnings))


async def fetch_fear_greed() -> Optional[Tuple[int, str]]:
    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.get("https://api.alternative.me/fng/")
        r.raise_for_status()
        d = (r.json().get("data") or [None])[0]
        return (int(d["value"]), str(d["value_classification"])) if d else None
