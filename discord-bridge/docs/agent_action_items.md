# Agent Action Items — Paper Trading Implementation

Three standing action items are now enforced in the paper trading layer.
They activate automatically on every 60-second monitor tick and on every trade
execution — no meeting vote required for the enforcement logic itself, though
agents still use meeting votes to decide *whether* to place an order.

---

## Mercury: Limit Orders at Key Support/Resistance

**What it does**
Athena (meeting chair) can call `place_limit_order` during any `trade_execution`
meeting. The order is persisted in `portfolio_state.json` and checked every 60 s
by the alert monitor. When the price condition is met the trade executes
automatically, exactly like a market order, with slippage applied.

| Order type | Triggers when |
|---|---|
| LIMIT BUY | current price ≤ target price |
| LIMIT SELL | current price ≥ target price |

**Tool schema** (available to Athena in `ACTION_TOOLS`)
```json
{
  "name": "place_limit_order",
  "parameters": {
    "action":       "BUY" | "SELL",
    "asset":        "BTC" | "ETH" | "SOL" | …,
    "amount":       <USD for BUY, coin qty for SELL>,
    "target_price": <trigger price>
  }
}
```

**Filled-order notifications**
When an order fills, `_notify_executed_orders` in `alerts.py` posts to the
trading-floor channel.  A STOP-type fill also triggers an emergency meeting.

**Cancellation**
Athena can call `cancel_orders` with an asset symbol to wipe all pending
orders for that asset.

**Dry-run safety**
`place_limit_order` is in `DRY_RUN_BLOCKED_TOOLS` — setting
`TRADING_DRY_RUN=1` prevents it from writing any order.

**Key files**
- [`bot/tools.py`](../bot/tools.py) — `place_limit_order` handler, `ACTION_TOOLS` entry
- [`bot/portfolio.py`](../bot/portfolio.py) — `place_order()`, `check_orders()`
- [`bot/alerts.py`](../bot/alerts.py) — `_monitor_loop()` calls `check_orders` every 60 s

---

## Midas / Zephyr: SOL Exposure Cap

**What it does**
Any BUY on SOL is blocked at the `execute_trade` gate if it would push SOL's
share of the total portfolio value above `max_sol_exposure_pct`.  The check
runs *before* slippage or minimum-trade validation, so the capital never moves.

**Configuration** (`config/settings.yaml`)
```yaml
thresholds:
  max_sol_exposure_pct: 20.0   # 0 = disabled
```
Change this value to tighten or loosen the cap without touching code.

**How the cap is calculated**
```
projected_sol_value = (current_sol_qty × current_price) + buy_amount_usd
projected_total     = current_portfolio_value + buy_amount_usd
projected_pct       = projected_sol_value / projected_total × 100
```
If `projected_pct > max_sol_exposure_pct` the trade is refused and an audit
event `trade_blocked / sol_exposure_cap` is emitted.

**What agents see**
The blocked message is returned to Athena:
> 🚫 **SOL exposure cap:** buying $3,000.00 would push SOL to **23.1%** of the
> portfolio, exceeding the **20%** cap. Reduce position size or wait for rebalancing.

**Key files**
- [`bot/tools.py`](../bot/tools.py) — SOL cap guard inside `execute_trade` handler
- [`config/settings.yaml`](../config/settings.yaml) — `thresholds.max_sol_exposure_pct`

---

## Atlas: MACD Crossover Alert → Entry Confirmation Meeting

**What it does**
The alert monitor calls `check_macd_flip()` every 60 s.  When the MACD
histogram (MACD line − signal line) changes sign for BTC or ETH, it is treated
as an entry-confirmation signal.  A message is posted to the trading-floor
channel and an emergency meeting is scheduled so the full team can vote on
whether to act.

| Crossover | Histogram change | Meaning |
|---|---|---|
| Bullish | negative → positive | MACD crosses above signal line |
| Bearish | positive → negative | MACD crosses below signal line |

**Cooldown**
4 hours between MACD alerts (matches the technical-indicator cache TTL in
`price_feed.py`).  This prevents a noisy re-alert while the same candle is
cached.

**Data source**
`price_feed.get_technical_indicators()` fetches 100 daily candles from Kraken
and runs `pandas_ta.macd(fast=12, slow=26, signal=9)`.  Both `MACD` (line) and
`MACD_signal` (signal line) are now returned in the indicators dict.

**Emergency meeting context**
The alert data passed to `schedule_emergency` uses the direction string
`MACD_BULLISH` or `MACD_BEARISH`, so agents in the meeting know it is an
Atlas-triggered entry signal rather than a price crash.

**Key files**
- [`bot/alerts.py`](../bot/alerts.py) — `check_macd_flip()`, `_trigger_macd_alert()`, `_macd_cooldown_elapsed()`
- [`bot/price_feed.py`](../bot/price_feed.py) — `get_technical_indicators()` now includes `MACD_signal`

---

## Tests

All three features are covered in
[`tests/test_action_items.py`](../tests/test_action_items.py) (16 tests, zero
real-API calls).

```
TestPlaceLimitOrder  (5 tests)
  test_buy_limit_order_stored              — tool stores order, returns ID
  test_sell_limit_order_stored             — SELL uses ≥ direction symbol
  test_limit_order_blocked_by_dry_run      — DRY_RUN=1 prevents placement
  test_limit_order_triggers_at_target      — check_orders fires at price ≤ target
  test_limit_order_does_not_trigger_above  — order stays pending above target

TestSolExposureCap  (4 tests)
  test_sol_buy_blocked_over_cap            — 23% > 20% cap → blocked
  test_sol_buy_allowed_under_cap           — 9% < 20% cap → executes
  test_sol_cap_not_applied_to_other_assets — BTC buy passes through
  test_sol_cap_disabled_when_zero          — cap=0 means unrestricted

TestMacdFlip  (7 tests)
  test_no_flip_on_first_reading            — seeds histogram, no alert
  test_bullish_crossover_detected          — negative → positive triggers BULLISH
  test_bearish_crossover_detected          — positive → negative triggers BEARISH
  test_no_flip_when_sign_unchanged         — same sign, no alert
  test_macd_flip_triggers_emergency_meeting — posts message + schedules meeting
  test_macd_flip_ignored_during_cooldown   — 4h cooldown respected
  test_macd_flip_resilient_to_api_error    — Kraken timeout → empty list, no crash
```

Run with:
```bash
cd discord-bridge
PYTHONPATH=. pytest tests/test_action_items.py -v
```
