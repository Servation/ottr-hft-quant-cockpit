"""
Screener: ranks Kraken USD pairs outside the universe, so the owner can see what else
is worth trading. Advisory only: nothing here can place an order, and a candidate becomes
tradable only when the owner adds it to `universe` (with a cap and, if needed, [pairs]).

What it measures, and why. The desk is long-only spot, sizes BUYs by inverse volatility
and gives CHOPPY assets a quarter size, so a useful candidate is one this desk could
actually hold with conviction: a steady uptrend, ahead of simply holding BTC, at a size
the risk model would allow, liquid, seasoned, and adding some diversification:

  trend 30%      mean over 14, 30 and 90 days of log return / (daily vol * sqrt(days)):
                 a volatility-adjusted trend (a t-stat of the drift), so a quiet steady
                 climb outranks a violent one of the same size
  vs_btc 20%     the same measure on the price ratio to BTC: alts move with BTC (about
                 0.9 correlation here), so an alt earns a slot only if it beats BTC itself
  sizable 20%    the risk model's own target weight at full conviction (risk/gate.py):
                 what the desk could really hold, lower for wild volatility and a quarter
                 in a choppy regime
  er 15%         Kaufman efficiency ratio (20d), the desk's regime measure: smoothness
  diversification 10%  1 - |correlation of daily returns with BTC| over 90 days
  participation 5%     median USD volume of the last 14 days / the last 90

The score (0-100) is a weighted mean of percentile ranks among the eligible pairs and the
universe together, so units and one outlier can't dominate, and a candidate is judged
against what the desk already trades. A candidate only *qualifies* when every horizon
agrees (up over 14, 30 and 90 days), it beats BTC, and it is within 20% of its 30-day
high. Two flags halve the score: "pulling back" (more than 20% off that high) and
"overextended" (a 7-day move above 3 sigma, or RSI above 80). The first live run showed
why: averaging horizons alone ranked coins that had pumped 120-220% in a month and were
falling 20-40% in the week above steady trends the desk could size. When nothing
qualifies, the screen says so instead of promoting the least bad pair.

Eligibility comes first, in code: online USD spot pairs only, no stablecoins, fiat or
pegs (a 90-day volatility under 10%), at least `min_history_days` of daily candles with
no gaps (skips fresh listings and their launch pumps), a 30-day median volume of at least
`min_usd_volume` and a live spread under `max_spread_pct`.

Why not "trending" (search or social attention): it measures attention, not return. It
tends to peak with the price, it is dominated by memecoins and fresh listings the filters
above exclude anyway, and it needs a second data source (v1's CoinGecko calls hit rate
limits every refresh). Everything here comes from Kraken, the venue the desk trades on.
"""

from __future__ import annotations

import asyncio
import logging
import math
import statistics
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ottr.config import Config
from ottr.market import indicators as ind
from ottr.market.kraken import Kraken, KrakenError
from ottr.market.types import MarketDataError
from ottr.risk.gate import target_weight

logger = logging.getLogger(__name__)

DAY = 86400
HORIZONS = (14, 30, 90)
WEIGHTS = {"trend": 0.30, "vs_btc": 0.20, "sizable": 0.20, "er": 0.15, "diversification": 0.10,
           "participation": 0.05}
MAX_PULLBACK = 0.20       # more than this below the 30-day high is a reversal, not a trend
PEG_VOL = 0.10            # annualized vol under this is a peg (stablecoin, fiat proxy), not a trade
OVEREXTENDED_Z = 3.0      # a 7-day move above this many sigma
OVEREXTENDED_RSI = 80.0
PREFILTER_FACTOR = 0.3    # 24h volume is noisy: prefilter loosely, judge on the 30-day median
PAUSE = 1.1               # seconds between candle requests (Kraken's public limit is ~1/s)
ALIASES = {"XBT": "BTC", "XDG": "DOGE"}  # Kraken's legacy codes -> the usual tickers
FIAT = frozenset({"USD", "EUR", "GBP", "AUD", "CAD", "JPY", "CHF", "AED", "NZD", "SGD", "HKD",
                  "SEK", "NOK", "DKK", "PLN", "CZK", "ZAR", "TRY", "MXN", "BRL"})
# Known USD/EUR stablecoins. Any peg missing here is still caught by PEG_VOL.
STABLES = frozenset({"USDT", "USDC", "DAI", "PYUSD", "USDG", "EURC", "EURT", "EURQ", "EURR", "TUSD",
                     "USDS", "USDE", "FDUSD", "RLUSD", "USD1", "USDQ", "USDR", "UST", "USTC", "GUSD",
                     "PAX", "USDP", "BUSD", "LUSD", "FRAX", "GHO", "USDD", "CRVUSD", "AUSD", "USDF"})


@dataclass(frozen=True)
class Listing:
    symbol: str     # the usual ticker (BTC, not XBT)
    pair: str       # Kraken altname, e.g. NEARUSD (what OHLC and [pairs] take)
    key: str        # Kraken's result key (XXBTZUSD for BTC), how Ticker answers


@dataclass(frozen=True)
class Row:
    symbol: str
    pair: str
    in_universe: bool
    eligible: bool
    reason: str = ""                     # why not eligible
    history_days: int = 0
    price: Optional[float] = None        # last closed daily candle
    usd_volume: Optional[float] = None   # 30-day median daily volume
    spread_pct: Optional[float] = None
    vol: Optional[float] = None          # 90-day annualized
    trend: Optional[float] = None
    trend_by_horizon: Tuple[Optional[float], ...] = ()   # 14d, 30d, 90d
    vs_btc: Optional[float] = None
    sizable: Optional[float] = None      # risk-model target weight at conviction 1.0
    pullback: Optional[float] = None     # price vs its 30-day high (0 = at the high)
    er: Optional[float] = None
    regime: str = "UNKNOWN"
    corr_btc: Optional[float] = None
    participation: Optional[float] = None
    rsi: Optional[float] = None
    chg_7d: Optional[float] = None
    chg_30d: Optional[float] = None
    chg_90d: Optional[float] = None
    flags: Tuple[str, ...] = ()
    qualifies: bool = False              # up on every horizon, ahead of BTC, near its high
    score: Optional[float] = None
    rank: Optional[int] = None


@dataclass(frozen=True)
class ScreenResult:
    id: str
    ts: float
    rows: Tuple[Row, ...]                # scored rows (universe + eligible candidates), best first
    rejected: Tuple[Row, ...]            # measured but ineligible, with the reason
    excluded: Dict[str, int]             # counts of pairs dropped before measuring, by reason
    listed: int                          # online USD spot pairs Kraken lists
    errors: Tuple[str, ...] = ()
    settings: Dict[str, Any] = field(default_factory=dict)

    def candidates(self, n: int) -> List[Row]:
        return [r for r in self.rows if not r.in_universe and r.qualifies][:n]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["rows"] = [asdict(r) for r in self.rows]
        d["rejected"] = [asdict(r) for r in self.rejected]
        return d


# -- pure steps ------------------------------------------------------------------------------
def listings(asset_pairs: Mapping[str, dict]) -> Tuple[Dict[str, Listing], Dict[str, int]]:
    """symbol -> Listing for online USD spot pairs that could be a trade, plus what was dropped."""
    out: Dict[str, Listing] = {}
    dropped: Dict[str, int] = {}

    def drop(why: str) -> None:
        dropped[why] = dropped.get(why, 0) + 1

    for key, p in asset_pairs.items():
        base, _, quote = (p.get("wsname") or "").partition("/")
        if key.endswith(".d") or quote != "USD" or not base:
            continue  # dark pools and non-USD quotes are simply out of scope
        sym = ALIASES.get(base, base).upper()
        if p.get("status", "online") != "online":
            drop("not trading (cancel or post only)")
        elif sym in FIAT or sym in STABLES:
            drop("stablecoin or fiat")
        else:
            out[sym] = Listing(sym, p.get("altname") or key, key)
    return out, dropped


def spread_pct(ticker: Optional[dict]) -> Optional[float]:
    try:
        bid, ask = float(ticker["b"][0]), float(ticker["a"][0])
    except (TypeError, KeyError, IndexError, ValueError):
        return None
    mid = (bid + ask) / 2
    return (ask - bid) / mid * 100 if bid > 0 and ask > 0 else None


def usd_volume_24h(ticker: Optional[dict]) -> float:
    try:
        return float(ticker["v"][1]) * float(ticker["p"][1])  # 24h base volume x 24h VWAP
    except (TypeError, KeyError, IndexError, ValueError):
        return 0.0


def prefilter(found: Mapping[str, Listing], tickers: Mapping[str, dict], cfg: Config,
              skip: Sequence[str]) -> Tuple[List[Listing], Dict[str, int]]:
    """The most liquid listings worth fetching candles for (one request each)."""
    sc = cfg.screen
    keep: List[Tuple[float, Listing]] = []
    dropped: Dict[str, int] = {}

    def drop(why: str) -> None:
        dropped[why] = dropped.get(why, 0) + 1

    for sym, lst in found.items():
        if sym in skip:
            continue
        t = tickers.get(lst.key)
        sp = spread_pct(t)
        vol24 = usd_volume_24h(t)
        if t is None or sp is None:
            drop("no live quote")
        elif sp > sc.max_spread_pct:
            drop("spread too wide")
        elif vol24 < sc.min_usd_volume * PREFILTER_FACTOR:
            drop("too illiquid")
        else:
            keep.append((vol24, lst))
    keep.sort(key=lambda x: -x[0])
    if len(keep) > sc.max_pairs:
        dropped["beyond the scan limit (max_pairs)"] = len(keep) - sc.max_pairs
    return [lst for _, lst in keep[:sc.max_pairs]], dropped


def _drift_z(series: Sequence[float], sd: float, h: int) -> Optional[float]:
    if len(series) <= h or sd <= 0 or series[-1 - h] <= 0:
        return None
    return math.log(series[-1] / series[-1 - h]) / (sd * math.sqrt(h))


def _horizons(series: Sequence[float]) -> Tuple[Optional[float], ...]:
    """Volatility-adjusted drift per horizon in HORIZONS (Nones when too short)."""
    rets = ind.log_returns(list(series)[-(HORIZONS[-1] + 1):])
    if len(rets) < HORIZONS[-1]:
        return tuple(None for _ in HORIZONS)
    sd = statistics.stdev(rets)
    return tuple(_drift_z(series, sd, h) for h in HORIZONS)


def _mean(zs: Sequence[Optional[float]]) -> Optional[float]:
    return None if not zs or any(z is None for z in zs) else sum(zs) / len(zs)


def measure(symbol: str, pair: str, rows: Sequence[Tuple[int, float, float]],
            btc_rows: Sequence[Tuple[int, float, float]], cfg: Config, *, in_universe: bool,
            spread: Optional[float] = None) -> Row:
    """Every measure for one pair from its closed daily candles [(ts, close, usd_volume)]."""
    sc = cfg.screen
    base = Row(symbol=symbol, pair=pair, in_universe=in_universe, eligible=False,
               history_days=len(rows), spread_pct=spread)
    if len(rows) < max(sc.min_history_days, HORIZONS[-1] + 1):
        return replace(base, reason=f"new listing ({len(rows)} days of history)")
    recent = rows[-(HORIZONS[-1] + 1):]
    if any(b[0] - a[0] != DAY for a, b in zip(recent, recent[1:])):
        return replace(base, reason="gaps in its daily candles")
    closes = [r[1] for r in rows]
    vols = [r[2] for r in rows]
    rets = ind.log_returns(closes[-(HORIZONS[-1] + 1):])
    sd = statistics.stdev(rets)
    ann = sd * math.sqrt(365.0)
    liquidity = statistics.median(vols[-30:])
    out = replace(base, price=closes[-1], usd_volume=liquidity, vol=ann)
    if ann < PEG_VOL:
        return replace(out, reason=f"pegged (90-day volatility {ann * 100:.1f}%)")
    if not in_universe and liquidity < sc.min_usd_volume:
        return replace(out, reason=f"too illiquid (${liquidity:,.0f}/day over 30 days)")

    btc = {ts: c for ts, c, _ in btc_rows}
    common = [(c, btc[ts]) for ts, c, _ in rows if ts in btc and btc[ts] > 0]
    is_btc = symbol == "BTC"
    vs_btc = 0.0 if is_btc else _mean(_horizons([a / b for a, b in common]))
    corr = 1.0 if is_btc else ind.return_correlation([a for a, _ in common], [b for _, b in common],
                                                       window=HORIZONS[-1])
    er = ind.efficiency_ratio(closes, cfg.market.regime_er_window)
    long_vol = statistics.median(vols[-90:])
    rsi = ind.rsi(closes, 14)
    z7 = _drift_z(closes, sd, 7)
    by_h = _horizons(closes)
    trend = _mean(by_h)
    pullback = closes[-1] / max(closes[-30:]) - 1
    reg = ind.regime(er, cfg.market.regime_er_threshold)
    flags = []
    if (z7 is not None and z7 > OVEREXTENDED_Z) or (rsi is not None and rsi > OVEREXTENDED_RSI):
        flags.append("overextended")
    if pullback < -MAX_PULLBACK:
        flags.append("pulling back")

    def chg(h: int) -> Optional[float]:
        return closes[-1] / closes[-1 - h] - 1 if len(closes) > h and closes[-1 - h] > 0 else None

    agree = all(z is not None and z > 0 for z in by_h)
    vol30 = ind.annualized_vol(closes, cfg.market.vol_window_days)  # the window the sizer uses
    return replace(
        out, eligible=True, trend=trend, trend_by_horizon=by_h, vs_btc=vs_btc, er=er, regime=reg,
        sizable=target_weight(cfg, symbol, vol30, reg, 1.0), pullback=pullback, corr_btc=corr,
        participation=statistics.median(vols[-14:]) / long_vol if long_vol > 0 else None,
        rsi=rsi, chg_7d=chg(7), chg_30d=chg(30), chg_90d=chg(90), flags=tuple(flags),
        qualifies=bool(agree and vs_btc is not None and vs_btc > 0 and pullback >= -MAX_PULLBACK),
    )


def _percentiles(values: Sequence[Optional[float]]) -> List[float]:
    """Each value's percentile rank in [0, 1] (ties share the average rank, None ranks last)."""
    n = len(values)
    if n == 1:
        return [0.5]
    known = sorted(v for v in values if v is not None)
    unknown = n - len(known)
    out = []
    for v in values:
        if v is None:  # the unknowns share the bottom ranks
            out.append(((unknown - 1) / 2) / (n - 1))
            continue
        below = unknown + sum(1 for k in known if k < v)
        equal = sum(1 for k in known if k == v)
        out.append((below + (equal - 1) / 2) / (n - 1))
    return out


def score(rows: Sequence[Row]) -> List[Row]:
    """Score and rank eligible rows together (universe included), best first."""
    pool = [r for r in rows if r.eligible]
    if not pool:
        return []
    feats = {
        "trend": [r.trend for r in pool],
        "vs_btc": [r.vs_btc for r in pool],
        "sizable": [r.sizable for r in pool],
        "er": [r.er for r in pool],
        "diversification": [None if r.corr_btc is None else 1 - abs(r.corr_btc) for r in pool],
        "participation": [r.participation for r in pool],
    }
    pct = {k: _percentiles(v) for k, v in feats.items()}
    scored = []
    for i, r in enumerate(pool):
        s = 100 * sum(w * pct[k][i] for k, w in WEIGHTS.items())
        for flag in ("overextended", "pulling back"):  # each halves it: chasing, or catching
            if flag in r.flags:
                s *= 0.5
        scored.append(replace(r, score=round(s, 1)))
    scored.sort(key=lambda r: (-(r.score or 0), r.symbol))
    return [replace(r, rank=i + 1) for i, r in enumerate(scored)]


# -- the run ---------------------------------------------------------------------------------
async def run(kraken: Kraken, cfg: Config, pause: float = PAUSE) -> ScreenResult:
    """One screen: list pairs, prefilter on the bulk ticker, fetch candles, measure, rank.

    About one candle request per shortlisted pair (paced for Kraken's public limit), so a
    run takes a minute or so; it is meant to run once a day.
    """
    t0 = time.time()
    try:
        asset_pairs = await kraken.asset_pairs()
        tickers = await kraken.tickers()
    except KrakenError as e:
        raise MarketDataError(f"screen: could not list Kraken pairs: {e}")
    found, excluded = listings(asset_pairs)
    universe = list(cfg.universe)
    shortlist, dropped = prefilter(found, tickers, cfg, skip=universe)
    for k, v in dropped.items():
        excluded[k] = excluded.get(k, 0) + v
    keys = {p.get("altname"): k for k, p in asset_pairs.items()}
    btc_pair = cfg.kraken_pair("BTC") or "XBTUSD"
    try:
        btc_rows = await kraken.daily_ohlc(btc_pair)
    except KrakenError as e:
        raise MarketDataError(f"screen: no BTC candles to compare against: {e}")
    targets = [(a, cfg.kraken_pair(a) or "", True) for a in universe if a != "BTC"]
    targets += [(lst.symbol, lst.pair, False) for lst in shortlist]
    measured = [measure("BTC", btc_pair, btc_rows, btc_rows, cfg, in_universe=True,
                        spread=spread_pct(tickers.get(keys.get(btc_pair, ""))))] if "BTC" in universe else []
    errors: List[str] = []
    for sym, pair, in_u in targets:
        await asyncio.sleep(pause)
        try:
            rows = await kraken.daily_ohlc(pair)
        except KrakenError as e:
            errors.append(f"{sym}: {e}")
            continue
        measured.append(measure(sym, pair, rows, btc_rows, cfg, in_universe=in_u,
                                spread=spread_pct(tickers.get(keys.get(pair, "")))))
    ranked = score(measured)
    rejected = tuple(sorted((r for r in measured if not r.eligible), key=lambda r: r.symbol))
    now = time.time()
    sc = cfg.screen
    logger.info("screen: %d listed, %d measured, %d ranked in %.0fs (%d error(s))",
                len(found), len(measured), len(ranked), now - t0, len(errors))
    return ScreenResult(
        id=time.strftime("s-%Y%m%dT%H%M%S", time.gmtime(now)) + "-" + uuid.uuid4().hex[:6],
        ts=now, rows=tuple(ranked), rejected=rejected, excluded=excluded, listed=len(found),
        errors=tuple(errors),
        settings={"min_usd_volume": sc.min_usd_volume, "max_spread_pct": sc.max_spread_pct,
                  "min_history_days": sc.min_history_days, "top_n": sc.top_n, "weights": WEIGHTS})


# -- text (shared by the CLI, the Discord digest, /screen and the /ask prompt) --------------
def _fmt(x: Optional[float], spec: str) -> str:
    return "n/a" if x is None else format(x, spec)


def signed_pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x * 100:+.0f}%"


def money(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    return f"${x / 1e6:,.1f}M" if x >= 1e6 else f"${x / 1e3:,.0f}k"


def line(r: dict) -> str:
    """One row as plain text (from the stored/API dict form)."""
    flags = f" [{', '.join(r['flags'])}]" if r.get("flags") else ""
    vol = "n/a" if r.get("vol") is None else f"{r['vol'] * 100:.0f}%"
    size = "n/a" if r.get("sizable") is None else f"{r['sizable'] * 100:.1f}%"
    return (f"{r['symbol']}: score {_fmt(r.get('score'), '.0f')} | trend {_fmt(r.get('trend'), '+.2f')} | "
            f"vs BTC {_fmt(r.get('vs_btc'), '+.2f')} | sizable {size} | ER {_fmt(r.get('er'), '.2f')} "
            f"{r.get('regime')} | "
            f"corrBTC {_fmt(r.get('corr_btc'), '.2f')} | 7d {signed_pct(r.get('chg_7d'))} 30d {signed_pct(r.get('chg_30d'))} "
            f"90d {signed_pct(r.get('chg_90d'))} | vol {vol} | {money(r.get('usd_volume'))}/day{flags}")


def candidates(d: dict, n: int) -> List[dict]:
    return [r for r in d.get("rows", []) if not r["in_universe"] and r["qualifies"]][:n]


def closest(d: dict, n: int) -> List[dict]:
    return [r for r in d.get("rows", []) if not r["in_universe"]][:n]


def why_not(r: dict) -> str:
    """Why a ranked pair does not qualify (empty when it does)."""
    if r.get("qualifies"):
        return ""
    if "pulling back" in (r.get("flags") or []):
        return f"pulling back ({(r.get('pullback') or 0) * 100:.0f}% off its 30-day high)"
    down = [f"{h}d" for h, z in zip(HORIZONS, r.get("trend_by_horizon") or []) if z is None or z <= 0]
    if down:
        return f"not up over {', '.join(down)}"
    return "not ahead of BTC"


def promote_hint(r: dict) -> str:
    return (f"To trade {r['symbol']}: add it to `universe`, `{r['symbol']} = 10.0` under [caps] and "
            f"`{r['symbol']} = \"{r['pair']}\"` under [pairs] in config.toml, then `ottr service restart`.")


def digest(d: dict, n: int) -> str:
    """A short text for the Discord webhook after each run."""
    scored = sum(1 for r in d.get("rows", []) if not r["in_universe"])
    top = candidates(d, n)
    if not top:
        near = "; ".join(f"{r['symbol']} ({r['score']:.0f}, {why_not(r)})" for r in closest(d, 3)) or "none"
        return (f"**Screener**: no pair outside the universe qualifies (up over 14, 30 and 90 days, "
                f"ahead of BTC, near its high; {scored} liquid pairs scored). Closest: {near}. "
                "`/screen` for detail.")
    lines = [f"**Screener**: {len(top)} candidate(s) outside the universe ({scored} liquid pairs scored):"]
    for r in top:
        lines.append(f"- **{r['symbol']}** score {r['score']:.0f}: trend {r['trend']:+.2f}, vs BTC "
                     f"{r['vs_btc']:+.2f}, 30d {signed_pct(r['chg_30d'])}, {money(r['usd_volume'])}/day"
                     + (f" ({', '.join(r['flags'])})" if r.get("flags") else ""))
    lines.append("Trend and vs BTC are volatility-adjusted: 0 is flat, +1 a strong steady climb. Advisory: "
                 "none is traded unless you add it to the universe. `/screen` for detail.")
    return "\n".join(lines)
