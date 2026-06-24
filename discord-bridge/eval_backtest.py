#!/usr/bin/env python3
"""Backtest eval: replay cached daily candles through the baseline + signal
strategies for every committed fixture (BTC/ETH/SOL), print a vs-buy-and-hold
comparison per asset, and verify the engine is sane + deterministic.

Cross-asset on purpose: a strategy that only wins on one asset/window is curve-fit.
Offline + deterministic (reads tests/fixtures/*_daily.csv) so it runs as a non-LLM
gate in run_evals.py / CI.
"""

import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.backtest import (  # noqa: E402
    load_candles_csv, compare, format_table, default_strategies, BUY_AND_HOLD_NAME,
)

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "tests", "fixtures")


def _check_asset(name, candles):
    rows = compare(candles, default_strategies())
    print(f"\n[{name}] strategies vs buy-and-hold ({len(candles)} daily candles):")
    print(format_table(rows))

    by_name = {r["strategy"]: r for r in rows}
    bh = by_name[BUY_AND_HOLD_NAME]
    asset_return = candles[-1]["close"] / candles[0]["close"] - 1.0

    # Buy & hold tracks the asset, slightly under it from the one-time entry cost.
    assert bh["total_return"] is not None
    assert bh["total_return"] <= asset_return + 1e-9, f"{name}: buy&hold beat the asset"
    assert bh["total_return"] > asset_return - 0.03, f"{name}: buy&hold drag too large"

    for r in rows:
        assert r["final_value"] > 0, f"{name}/{r['strategy']} blew up"
        assert r["max_drawdown"] is not None, f"{name}/{r['strategy']} missing drawdown"
        assert r["alpha_vs_hold"] is not None, f"{name}/{r['strategy']} missing alpha"

    # Determinism: identical inputs -> identical outputs.
    rows2 = compare(candles, default_strategies())
    assert [r["final_value"] for r in rows] == [r["final_value"] for r in rows2], \
        f"{name}: non-deterministic backtest"


def main():
    fixtures = sorted(glob.glob(os.path.join(FIXTURE_DIR, "*_daily.csv")))
    assert fixtures, "no *_daily.csv fixtures found"
    for path in fixtures:
        name = os.path.basename(path).replace("_daily.csv", "").upper()
        candles = load_candles_csv(path)
        assert len(candles) > 60, f"{name} fixture too small: {len(candles)}"
        _check_asset(name, candles)

    print("\nBACKTEST EVAL PASSED")
    print("=== Backtest Eval Completed Successfully ===")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        print("EVALUATION FAILED")
        sys.exit(1)
