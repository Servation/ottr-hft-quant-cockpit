"""
Prototype: run one council meeting on live market data and PRINT the proposal.

What it does: builds a market packet from the price feed + a read-only portfolio snapshot,
runs the three-model council, prints analysts, tally and the chair's proposed trades.
Why: C0 of COUNCIL_PLAN.md. It is propose-only: it never calls execute_trade and never
saves portfolio state, so it is safe to run on the host next to the live container.

Usage: python run_council.py [--out council_last.json] [--only claude,gpt,gemini]
"""

import argparse
import asyncio
import json
import os
import sys
import time

from bot.council.council import Council
from bot.council.models import ClaudeCLI, CodexCLI, GeminiCLI
from bot.portfolio import portfolio
from bot.price_feed import price_feed
from bot.universe import tradeable_universe


async def build_packet() -> str:
    prices = await price_feed.get_prices()  # raises if no data: fail loud, never trade blind
    market = await price_feed.get_market_state_summary()
    summary = portfolio.get_summary(prices)
    held = ", ".join(
        f"{a}: {h['quantity']:.6f} (avg ${h['avg_cost']:,.2f}, now ${h['current_price']:,.2f})"
        for a, h in summary["holdings"].items()
    ) or "none"
    return (
        f"MARKET:\n{market}\n\n"
        f"PORTFOLIO: cash ${summary['cash']:,.2f}, total ${summary['total_portfolio_value']:,.2f}, "
        f"net P&L ${summary['net_pnl']:,.2f}\nHOLDINGS: {held}\n"
        f"MIN TRADE: ${summary['min_trade_usd']:,.2f}"
    )


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    ap.add_argument("--only", default="claude,gpt,gemini")
    args = ap.parse_args()

    everyone = {"claude": ClaudeCLI(), "gpt": CodexCLI(), "gemini": GeminiCLI()}
    analysts = {k: v for k, v in everyone.items() if k in args.only.split(",")}
    council = Council(
        analysts=analysts,
        chair=ClaudeCLI(),
        universe=tradeable_universe(),
        max_trade_usd=float(os.getenv("MAX_TRADE_USD", "0") or 0),
    )
    packet = await build_packet()
    t0 = time.time()
    record = await council.run(packet)
    record["elapsed_sec"] = round(time.time() - t0, 1)

    print(f"status: {record['status']}  ({record['elapsed_sec']}s)")
    for name, err in record["errors"].items():
        print(f"  ! {name} dropped: {err}")
    if record["tally"]:
        from bot.council.council import format_tally
        print(format_tally(record["tally"]))
    if record["decision"]:
        d = record["decision"]
        print(f"\nCHAIR: {d['summary']}\nDISSENT: {d['dissent_notes']}")
        for t in record["trades"]:
            print(f"  PROPOSE {t}")
        if not record["trades"]:
            print("  PROPOSE no trades")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
