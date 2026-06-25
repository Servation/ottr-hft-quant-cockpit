"""
Deterministic backtest harness.

Why this exists: forward paper-trading at a 4h cadence makes strategy iteration
effectively zero-speed, and there was no way to ask "would this rule have beaten
just holding BTC?" This replays historical daily candles through a pluggable
strategy, reusing the same slippage + fee model as the live portfolio, and scores
the result with `bot.metrics`. Pure and offline — no LLM, no live network in the
hot path (data is loaded from a cached CSV fixture for tests/evals).

A Strategy returns a *target weight* in [0, 1] (fraction of equity to hold in the
asset) for each bar, using only data up to that bar (no look-ahead). The engine
rebalances toward that target. Buy-and-hold is the benchmark the others must beat.
"""

from __future__ import annotations

import csv
import math
from typing import Dict, List, Optional, Sequence, Tuple

from bot import settings
from bot import metrics
from bot import signals

# Reuse the same cost model as the live portfolio (single source of truth for the
# rates) without importing the disk-backed Portfolio singleton.
_pcfg = settings.get("portfolio", {})
_SLIPPAGE_FRAC = float(_pcfg.get("slippage_pct", 0.1)) / 100.0
_FEE_FRAC = float(_pcfg.get("fee_pct", 0.0)) / 100.0
_STARTING_CASH = float(_pcfg.get("starting_balance", 10000.0))

# Don't churn on negligible rebalances (avoids float dust + needless fees).
_MIN_REBALANCE_USD = 1.0

BUY_AND_HOLD_NAME = "Buy & Hold"


# --- indicators (pure, deterministic) --------------------------------------

def _sma(closes: Sequence[float], end_idx: int, n: int) -> Optional[float]:
    if end_idx + 1 < n:
        return None
    window = closes[end_idx - n + 1: end_idx + 1]
    return sum(window) / n


def _rsi(closes: Sequence[float], period: int) -> Optional[float]:
    """Simple (non-Wilder) RSI over the last `period` price changes."""
    if len(closes) <= period:
        return None
    gains = 0.0
    losses = 0.0
    for k in range(len(closes) - period, len(closes)):
        change = closes[k] - closes[k - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


# --- strategies ------------------------------------------------------------

class Strategy:
    name = "strategy"

    def target_weight(self, i: int, closes: Sequence[float]) -> float:
        """Desired asset weight in [0,1] after bar i, using only closes[0..i]."""
        raise NotImplementedError


class BuyAndHold(Strategy):
    name = BUY_AND_HOLD_NAME

    def target_weight(self, i: int, closes: Sequence[float]) -> float:
        return 1.0


class SmaCross(Strategy):
    """Long while the fast SMA is above the slow SMA, flat otherwise."""

    def __init__(self, fast: int = 20, slow: int = 50):
        self.fast = fast
        self.slow = slow
        self.name = f"SMA {fast}/{slow}"

    def target_weight(self, i: int, closes: Sequence[float]) -> float:
        fast_ma = _sma(closes, i, self.fast)
        slow_ma = _sma(closes, i, self.slow)
        if fast_ma is None or slow_ma is None:
            return 0.0
        return 1.0 if fast_ma > slow_ma else 0.0


class RsiMeanReversion(Strategy):
    """Go long when RSI is oversold, exit when overbought, hold otherwise."""

    def __init__(self, period: int = 14, low: float = 30.0, high: float = 70.0):
        self.period = period
        self.low = low
        self.high = high
        self._held = 0.0
        self.name = f"RSI {period} ({int(low)}/{int(high)})"

    def target_weight(self, i: int, closes: Sequence[float]) -> float:
        rsi = _rsi(closes[: i + 1], self.period)
        if rsi is None:
            return self._held
        if rsi < self.low:
            self._held = 1.0
        elif rsi > self.high:
            self._held = 0.0
        return self._held


class SignalStrategy(Strategy):
    """Trades the deterministic signal layer (bot/signals). Long-only: the target
    weight is the signal's bullishness (its positive score), flat otherwise — so
    the backtest measures whether the same signals the agents now see have edge.

    `weights` selects the signal config (balanced vs trend); None = the live default.
    """

    def __init__(self, weights=None, name=None):
        self._series = None  # per-bar indicator dicts, computed once (causal)
        self._weights = weights
        self.name = name or "Signals (default)"

    def target_weight(self, i: int, closes: Sequence[float]) -> float:
        if self._series is None:
            self._series = signals.indicator_series_from_closes(closes)
        ind = self._series[i] if i < len(self._series) else None
        if not ind:
            return 0.0
        return max(0.0, signals.signal_from_indicators(ind, weights=self._weights).score)


class RegimeStrategy(Strategy):
    """Regime-aware: trend-follow when the market is actually trending, flat in chop.

    The data motivation: SMA trend-following has strong edge in trends (BTC/ETH) but
    gets chopped to pieces on ranging assets (SOL). The Kaufman efficiency ratio gates
    it — only trend-follow (long while fast SMA > slow SMA) when ER >= threshold;
    otherwise stay flat and avoid the chop. Long-only.
    """

    def __init__(self, fast=20, slow=50, er_window=20, er_threshold=0.30):
        self.fast = fast
        self.slow = slow
        self.er_window = er_window
        self.er_threshold = er_threshold
        self.name = f"Regime (ER>{er_threshold:g} trend-follow)"

    def target_weight(self, i: int, closes: Sequence[float]) -> float:
        er = signals.efficiency_ratio(closes, i, self.er_window)
        if er is None or er < self.er_threshold:
            return 0.0  # choppy / insufficient history -> defensive, stay flat
        fast_ma = _sma(closes, i, self.fast)
        slow_ma = _sma(closes, i, self.slow)
        if fast_ma is None or slow_ma is None:
            return 0.0
        return 1.0 if fast_ma > slow_ma else 0.0


# --- engine ----------------------------------------------------------------

def _apply_risk(target_w: float, price: float, equity: float, current_w: float,
                rstate: dict, risk: dict) -> float:
    """Overlay the Tier 3 controls on a base target weight, over backtest bars so the
    controls become measurable. `rstate` carries state across bars (mutated in place).

    Stop-loss: exit a long that's fallen `stop_loss_pct` below its reference and stay out
    until the base goes flat (no re-buying into the same downtrend). The reference is the
    entry price (`stop_mode` "fixed", the default) or the position's running high
    (`stop_mode` "trailing", which ratchets up to lock in gains). Drawdown halt: while
    equity is past `max_drawdown_halt_pct` off its peak, block *increases* in weight (like
    the live halt blocking new BUYs — holdings are kept, so the book can still recover),
    resuming below `drawdown_resume_pct`.
    """
    sl = risk.get("stop_loss_pct")
    if sl:
        trailing = risk.get("stop_mode") == "trailing"
        if target_w <= 0.0:
            rstate["entry"] = None
            rstate["high"] = None
            rstate["stopped"] = False
        elif rstate.get("stopped"):
            target_w = 0.0
        else:
            if rstate.get("entry") is None:
                rstate["entry"] = price
                rstate["high"] = price
            if price > rstate.get("high", price):
                rstate["high"] = price
            ref = rstate["high"] if trailing else rstate["entry"]
            if price <= ref * (1.0 - sl / 100.0):
                rstate["entry"] = None
                rstate["high"] = None
                rstate["stopped"] = True
                target_w = 0.0
    hp = risk.get("max_drawdown_halt_pct")
    if hp:
        peak = max(rstate.get("peak", equity), equity)
        rstate["peak"] = peak
        dd = (peak - equity) / peak if peak > 0 else 0.0
        if rstate.get("halted"):
            if dd <= risk.get("drawdown_resume_pct", 0.0) / 100.0:
                rstate["halted"] = False
        elif dd >= hp / 100.0:
            rstate["halted"] = True
        if rstate.get("halted"):
            target_w = min(target_w, current_w)  # block new buys, keep holdings
    return target_w


def run_backtest(candles: Sequence[dict], strategy: Strategy,
                 starting_cash: float = _STARTING_CASH,
                 risk: Optional[dict] = None) -> dict:
    """Replay candles through a strategy; return the equity curve + metrics.

    Fills execute at each bar's close with the live slippage + fee model. Returns
    a dict with `equity_curve` ([(ts, value)]), `final_value`, and `metrics`
    (from bot.metrics.summarize).

    When `risk` is given (a dict with optional `stop_loss_pct` / `max_drawdown_halt_pct`
    / `drawdown_resume_pct`), the Tier 3 controls are overlaid on the strategy so their
    effect (does a stop cut drawdown without giving back all the return, or whipsaw?) is
    measurable on the cached fixtures.
    """
    closes = [float(c["close"]) for c in candles]
    cash = float(starting_cash)
    qty = 0.0  # units of the asset held
    equity_curve: List[Tuple[float, float]] = []
    rstate: dict = {}

    for i, candle in enumerate(candles):
        price = closes[i]
        if price <= 0:
            equity_curve.append((float(candle["ts"]), cash + qty * price))
            continue

        target_w = max(0.0, min(1.0, strategy.target_weight(i, closes)))
        total_value = cash + qty * price
        if risk:
            current_w = (qty * price) / total_value if total_value > 0 else 0.0
            target_w = _apply_risk(target_w, price, total_value, current_w, rstate, risk)
        desired_asset_value = target_w * total_value
        diff = desired_asset_value - qty * price

        if diff > _MIN_REBALANCE_USD:
            # Buy: leave headroom so notional + fee fits in cash.
            spend = min(diff, cash / (1.0 + _FEE_FRAC))
            if spend > _MIN_REBALANCE_USD:
                fill = price * (1.0 + _SLIPPAGE_FRAC)
                fee = spend * _FEE_FRAC
                qty += spend / fill
                cash -= spend + fee
        elif diff < -_MIN_REBALANCE_USD:
            sell_value = -diff
            sell_qty = min(qty, sell_value / price)
            if sell_qty * price > _MIN_REBALANCE_USD:
                fill = price * (1.0 - _SLIPPAGE_FRAC)
                proceeds = sell_qty * fill
                fee = proceeds * _FEE_FRAC
                qty -= sell_qty
                cash += proceeds - fee

        equity_curve.append((float(candle["ts"]), cash + qty * price))

    summary = metrics.summarize(equity_curve)
    return {
        "strategy": strategy.name,
        "equity_curve": equity_curve,
        "final_value": equity_curve[-1][1] if equity_curve else starting_cash,
        "metrics": summary,
    }


def compare(candles: Sequence[dict], strategies: Sequence[Strategy],
            starting_cash: float = _STARTING_CASH) -> List[dict]:
    """Run each strategy on the same candles; return rows incl. alpha vs HODL."""
    results = {s.name: run_backtest(candles, s, starting_cash) for s in strategies}
    bh = results.get(BUY_AND_HOLD_NAME, {}).get("metrics", {})
    bh_return = bh.get("total_return")

    rows = []
    for name, res in results.items():
        m = res["metrics"]
        tr = m.get("total_return")
        alpha = (tr - bh_return) if (tr is not None and bh_return is not None) else None
        rows.append({
            "strategy": name,
            "final_value": res["final_value"],
            "total_return": tr,
            "cagr": m.get("cagr"),
            "sharpe": m.get("sharpe"),
            "max_drawdown": m.get("max_drawdown"),
            "alpha_vs_hold": alpha,
        })
    return rows


def format_table(rows: Sequence[dict]) -> str:
    """Render comparison rows as a fixed-width text table."""
    def pct(v):
        return "n/a" if v is None else f"{v * 100:+.2f}%"

    def ratio(v):
        return "n/a" if v is None else f"{v:.2f}"

    header = f"{'Strategy':<22}{'Return':>10}{'CAGR':>10}{'Sharpe':>9}{'MaxDD':>9}{'vs HODL':>11}"
    lines = [header, "-" * len(header)]
    for r in rows:
        lines.append(
            f"{r['strategy']:<22}"
            f"{pct(r['total_return']):>10}"
            f"{pct(r['cagr']):>10}"
            f"{ratio(r['sharpe']):>9}"
            f"{pct(r['max_drawdown']):>9}"
            f"{pct(r['alpha_vs_hold']):>11}"
        )
    return "\n".join(lines)


def default_strategies() -> List[Strategy]:
    return [BuyAndHold(), SmaCross(20, 50), RsiMeanReversion(14, 30, 70),
            SignalStrategy(), RegimeStrategy()]


# --- data: CSV fixture I/O + sources --------------------------------------

def load_candles_csv(path: str) -> List[dict]:
    """Load candles from a CSV with columns ts,open,high,low,close,volume."""
    out: List[dict] = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            out.append({
                "ts": float(row["ts"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 0.0) or 0.0),
            })
    return out


def save_candles_csv(candles: Sequence[dict], path: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        for c in candles:
            w.writerow([
                int(c["ts"]), c["open"], c["high"], c["low"], c["close"], c.get("volume", 0.0),
            ])


def synth_candles(n: int = 500, start: float = 20000.0, seed: int = 42) -> List[dict]:
    """Deterministic geometric-random-walk candles (offline fixture fallback)."""
    import random
    rng = random.Random(seed)
    price = start
    ts0 = 1_600_000_000  # fixed epoch so the fixture is reproducible
    out: List[dict] = []
    for i in range(n):
        price *= 1.0 + rng.uniform(-0.03, 0.035)  # slight upward drift + noise
        out.append({
            "ts": ts0 + i * 86400,
            "open": price,
            "high": price * 1.01,
            "low": price * 0.99,
            "close": price,
            "volume": 1.0,
        })
    return out


def fetch_kraken_daily(pair: str = "XBTUSD", count: int = 720) -> Optional[List[dict]]:
    """Fetch up to ~720 daily candles from Kraken (used only to (re)build the
    fixture; never on the eval hot path). Returns None on any failure."""
    try:
        import httpx
        url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=1440"
        with httpx.Client(timeout=10.0) as client:
            data = client.get(url).json()
        if data.get("error"):
            return None
        key = [k for k in data.get("result", {}) if k != "last"][0]
        rows = data["result"][key][-count:]
        # Kraken row: [time, open, high, low, close, vwap, volume, count]
        return [{
            "ts": float(r[0]), "open": float(r[1]), "high": float(r[2]),
            "low": float(r[3]), "close": float(r[4]), "volume": float(r[6]),
        } for r in rows]
    except Exception:
        return None
