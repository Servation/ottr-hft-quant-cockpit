#!/usr/bin/env python3
"""Backtest eval: replay cached BTC daily candles through the baseline strategies,
print a vs-buy-and-hold comparison, and verify the engine is sane + deterministic.

Offline and deterministic (reads tests/fixtures/btc_daily.csv) so it runs as a
non-LLM gate in run_evals.py / CI.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.backtest import (  # noqa: E402
    load_candles_csv, compare, format_table, default_strategies, BUY_AND_HOLD_NAME,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "tests", "fixtures", "btc_daily.csv")


def main():
    candles = load_candles_csv(FIXTURE)
    assert len(candles) > 60, f"fixture too small: {len(candles)} candles"

    rows = compare(candles, default_strategies())
    print("\nBaseline strategies vs buy-and-hold (cached BTC daily candles):")
    print(format_table(rows))
    print()

    by_name = {r["strategy"]: r for r in rows}
    bh = by_name[BUY_AND_HOLD_NAME]
    asset_return = candles[-1]["close"] / candles[0]["close"] - 1.0

    # Buy & hold should track the asset, slightly under it from the entry cost.
    assert bh["total_return"] is not None
    assert bh["total_return"] <= asset_return + 1e-9, "buy&hold beat the asset (impossible with costs)"
    assert bh["total_return"] > asset_return - 0.03, "buy&hold drag unexpectedly large"

    # Every strategy produces a full, finite equity curve and computable metrics.
    for r in rows:
        assert r["final_value"] > 0, f"{r['strategy']} blew up the account"
        assert r["max_drawdown"] is not None, f"{r['strategy']} missing drawdown"
        assert r["alpha_vs_hold"] is not None, f"{r['strategy']} missing alpha"

    # Determinism: identical inputs -> identical outputs.
    rows2 = compare(candles, default_strategies())
    assert [r["final_value"] for r in rows] == [r["final_value"] for r in rows2], "non-deterministic backtest"

    print("BACKTEST EVAL PASSED")
    print("=== Backtest Eval Completed Successfully ===")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        print("EVALUATION FAILED")
        sys.exit(1)
