#!/usr/bin/env python3
"""Risk-overlay eval (Tier 3 / R4).

For each cached fixture (BTC/ETH/SOL), run a few base strategies with vs without the
Tier 3 stop-loss + drawdown-halt overlay, print the comparison, and verify the engine
stays sane + deterministic. The point is to *measure* whether the controls cut drawdown
without giving back all the return (they can whipsaw) — so this prints the delta rather
than asserting the controls always help. Offline + deterministic (no LLM), so it runs as
a non-LLM gate in run_evals.py / CI.
"""

import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot import settings  # noqa: E402
from bot.backtest import (  # noqa: E402
    load_candles_csv, run_backtest, BuyAndHold, SmaCross, RegimeStrategy,
)

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "tests", "fixtures")


def _pct(v):
    return "n/a" if v is None else f"{v * 100:+.2f}%"


def _risk_cfg():
    rl = settings.get("risk_limits", {})
    return {
        "stop_loss_pct": float(rl.get("stop_loss_pct", 10.0)),
        "max_drawdown_halt_pct": float(rl.get("max_drawdown_halt_pct", 15.0)),
        "drawdown_resume_pct": float(rl.get("drawdown_resume_pct", 10.0)),
    }


def _check_asset(name, candles):
    risk = _risk_cfg()
    print(f"\n[{name}] base vs +risk overlay ({len(candles)} candles):")
    print(f"{'Strategy':<24}{'Return':>11}{'Return+R':>11}{'MaxDD':>10}{'MaxDD+R':>10}")
    print("-" * 66)
    for make in (lambda: BuyAndHold(), lambda: SmaCross(20, 50), lambda: RegimeStrategy()):
        base = run_backtest(candles, make())
        over = run_backtest(candles, make(), risk=risk)
        bm, om = base["metrics"], over["metrics"]
        print(f"{base['strategy']:<24}"
              f"{_pct(bm.get('total_return')):>11}{_pct(om.get('total_return')):>11}"
              f"{_pct(bm.get('max_drawdown')):>10}{_pct(om.get('max_drawdown')):>10}")

        for res in (base, over):
            assert res["final_value"] > 0, f"{name}/{res['strategy']} blew up"
            assert res["metrics"]["max_drawdown"] is not None, f"{name}/{res['strategy']} no drawdown"
        # Determinism: identical inputs -> identical outputs.
        again = run_backtest(candles, make(), risk=risk)
        assert again["final_value"] == over["final_value"], f"{name}: non-deterministic overlay"


def main():
    fixtures = sorted(glob.glob(os.path.join(FIXTURE_DIR, "*_daily.csv")))
    assert fixtures, "no *_daily.csv fixtures found"
    for path in fixtures:
        name = os.path.basename(path).replace("_daily.csv", "").upper()
        candles = load_candles_csv(path)
        assert len(candles) > 60, f"{name} fixture too small: {len(candles)}"
        _check_asset(name, candles)

    print("\nRISK OVERLAY EVAL PASSED")
    print("=== Risk Overlay Eval Completed Successfully ===")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        print("EVALUATION FAILED")
        sys.exit(1)
