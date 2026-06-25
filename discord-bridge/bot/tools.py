import logging
import os
from bot.price_feed import price_feed
from bot.portfolio import portfolio
from bot.scheduler import meeting_scheduler
from bot.audit import audit_event
from bot import settings
from bot import sizing
from bot import webhooks

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Kill-switch / dry-run safety
# ---------------------------------------------------------------------------
# State-mutating tools that are blocked when TRADING_DRY_RUN is enabled. Read
# tools (prices, portfolio summary, volatility) are always allowed.
DRY_RUN_BLOCKED_TOOLS = {"execute_trade", "update_parameter", "cancel_orders", "place_limit_order"}


def dry_run_enabled() -> bool:
    """True when the TRADING_DRY_RUN kill-switch is on."""
    return os.getenv("TRADING_DRY_RUN", "0").strip().lower() in ("1", "true", "yes", "on")

READ_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_asset_price",
            "description": "Fetch the current price of a specific asset (e.g. BTC, ETH, SOL).",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "The ticker symbol of the asset (e.g. BTC)"}
                },
                "required": ["symbol"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_portfolio_summary",
            "description": "Fetch the current portfolio holdings, cash balance, and total PnL.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_historical_volatility",
            "description": "Fetch the 14-day historical volatility for major assets.",
            "parameters": {"type": "object", "properties": {}}
        }
    }
]

ACTION_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "execute_trade",
            "description": "Execute a market BUY or SELL order.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["BUY", "SELL"], "description": "The action to perform"},
                    "asset": {"type": "string", "description": "The ticker symbol (e.g. BTC)"},
                    "amount": {"type": "number", "description": "The amount in USD for BUY, or quantity for SELL."},
                    "reasoning": {"type": "string", "description": "A brief one-sentence justification for the trade (the signal/consensus behind it). Recorded in the audit log and shown in the dashboard's decision feed."}
                },
                "required": ["action", "asset", "amount"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_parameter",
            "description": "Update a system parameter.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": ["min_trade_usd", "risk_halt"], "description": "The parameter to update"},
                    "value": {"type": "number", "description": "The new value"}
                },
                "required": ["name", "value"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_meeting",
            "description": "Schedule an early follow-up meeting before the next automatic 4-hour slot. Use only when a specific event warrants reconvening sooner — e.g. a limit order fills, a major price swing triggers a stop, or an urgent CEO directive arrives. Do NOT use this to duplicate the next scheduled meeting.",
            "parameters": {
                "type": "object",
                "properties": {
                    "minutes": {"type": "integer", "description": "Minutes from now to hold the follow-up meeting"}
                },
                "required": ["minutes"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "start_meeting_now",
            "description": "Immediately start a full team consensus meeting. Use this if the CEO requests to meet right now.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_orders",
            "description": "Cancel all pending limit/stop orders for an asset.",
            "parameters": {
                "type": "object",
                "properties": {
                    "asset": {"type": "string", "description": "The ticker symbol (e.g. BTC)"}
                },
                "required": ["asset"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "place_limit_order",
            "description": (
                "Place a persistent resting order at a key price level, checked every 60 "
                "seconds and surviving between meetings. order_type LIMIT (default): a BUY "
                "triggers when price falls to/below target, a SELL when price rises to/above "
                "target. order_type STOP: a protective SELL (stop-loss) that triggers when "
                "price falls to/below target. order_type TAKE_PROFIT: a SELL that triggers "
                "when price rises to/above target. STOP and TAKE_PROFIT are SELL-side only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["BUY", "SELL"],
                        "description": "BUY at support or SELL at resistance."
                    },
                    "asset": {
                        "type": "string",
                        "description": "Ticker symbol (e.g. BTC, ETH, SOL)."
                    },
                    "amount": {
                        "type": "number",
                        "description": "USD amount for BUY, or coin quantity for SELL."
                    },
                    "target_price": {
                        "type": "number",
                        "description": "Price level that triggers the order."
                    },
                    "order_type": {
                        "type": "string",
                        "enum": ["LIMIT", "STOP", "TAKE_PROFIT"],
                        "description": "Defaults to LIMIT. STOP/TAKE_PROFIT are SELL-side protective exits."
                    }
                },
                "required": ["action", "asset", "amount", "target_price"]
            }
        }
    }
]

async def handle_tool_call(tool_name: str, arguments: dict, audit_log_fn=None, post_message_fn=None) -> str:
    try:
        # Kill-switch: block state-mutating tools without touching any state.
        if dry_run_enabled() and tool_name in DRY_RUN_BLOCKED_TOOLS:
            msg = (
                f"🧪 **[DRY-RUN]** `{tool_name}` was blocked by the kill-switch "
                f"(TRADING_DRY_RUN). Requested args: `{arguments}`. No state changed."
            )
            logger.warning("DRY-RUN blocked %s with args %s", tool_name, arguments)
            audit_event("tool_blocked", tool=tool_name, reason="dry_run", args=arguments)
            if audit_log_fn:
                await audit_log_fn(msg)
            return msg

        if tool_name == "get_asset_price":
            symbol = arguments.get("symbol", "").upper()
            prices = await price_feed.get_prices()
            if symbol in prices:
                return f"{symbol} Price: ${prices[symbol]['price']:.2f} (24h: {prices[symbol]['change_24h']}%)"
            return f"Price for {symbol} not found."
            
        elif tool_name == "get_portfolio_summary":
            prices = await price_feed.get_prices()
            summary = portfolio.get_summary(prices)
            return str(summary)
            
        elif tool_name == "get_historical_volatility":
            vols = await price_feed.get_volatility()
            return str(vols)
            
        elif tool_name == "execute_trade":
            action = arguments.get("action", "").upper()
            asset = arguments.get("asset", "").upper()
            amount = float(arguments.get("amount", 0))
            reasoning = str(arguments.get("reasoning", "")).strip()[:500]

            prices = await price_feed.get_prices()
            if asset not in prices:
                return f"Error: Price for {asset} not found."
            price = prices[asset]["price"]

            # Drawdown circuit breaker (Tier 3 R2): while the breaker is latched, block
            # new BUYs (SELLs stay allowed so the desk can de-risk). Dark unless
            # risk_limits.enabled, so this is a no-op until enforcement is activated.
            if action == "BUY" and settings.get("risk_limits", {}).get("enabled", False):
                from bot import risk_state
                if risk_state.load().get("halted"):
                    msg = (
                        "🚦 **Trading halted:** the portfolio drawdown breaker is active, so "
                        "new BUYs are blocked until it resumes. De-risking SELLs are still allowed."
                    )
                    logger.warning("Drawdown halt blocked BUY %s $%.2f", asset, amount)
                    audit_event("trade_blocked", reason="drawdown_halt", action=action,
                                asset=asset, notional=amount)
                    if audit_log_fn:
                        await audit_log_fn(msg)
                    return msg

            # Authorization gate: cap the notional size of any single trade so a
            # runaway or prompt-injected order cannot exceed a safe limit. BUY
            # `amount` is USD; SELL `amount` is quantity (notional = qty * price).
            max_trade = float(os.getenv("MAX_TRADE_USD", "0") or 0)
            notional = amount if action == "BUY" else amount * price
            if max_trade > 0 and notional > max_trade:
                msg = (
                    f"🚫 **Trade blocked:** {action} {asset} notional "
                    f"${notional:,.2f} exceeds the MAX_TRADE_USD cap of ${max_trade:,.2f}."
                )
                logger.warning(
                    "Trade authorization gate blocked oversized %s %s (notional $%.2f > cap $%.2f)",
                    action, asset, notional, max_trade,
                )
                audit_event("trade_blocked", reason="max_trade_usd", action=action,
                            asset=asset, notional=notional, cap=max_trade)
                if audit_log_fn:
                    await audit_log_fn(msg)
                return msg

            # Concentration cap: block a BUY that would push ANY asset above its
            # portfolio-% limit. A per-asset override (e.g. thresholds.max_sol_exposure_pct)
            # beats the general thresholds.max_asset_exposure_pct, so SOL stays stricter.
            if action == "BUY":
                _caps = settings.get("thresholds", {})
                asset_cap = (
                    float(_caps.get(f"max_{asset.lower()}_exposure_pct", 0) or 0)
                    or float(_caps.get("max_asset_exposure_pct", 0) or 0)
                )
                if asset_cap > 0:
                    total_value = portfolio.get_total_value(prices)
                    held_qty = portfolio._state["holdings"].get(asset, {}).get("quantity", 0.0)
                    projected_value = held_qty * price + amount
                    projected_total = total_value + amount
                    if projected_total > 0:
                        projected_pct = (projected_value / projected_total) * 100
                        if projected_pct > asset_cap:
                            msg = (
                                f"🚫 **{asset} exposure cap:** buying ${amount:,.2f} would push {asset} to "
                                f"**{projected_pct:.1f}%** of the portfolio, exceeding the "
                                f"**{asset_cap:.0f}%** cap. Reduce position size or wait for rebalancing."
                            )
                            logger.warning(
                                "Exposure cap blocked BUY %s $%.2f (projected %.1f%% > cap %.1f%%)",
                                asset, amount, projected_pct, asset_cap,
                            )
                            audit_event(
                                "trade_blocked", reason="exposure_cap",
                                action=action, asset=asset, notional=amount,
                                projected_pct=projected_pct, cap=asset_cap,
                            )
                            if audit_log_fn:
                                await audit_log_fn(msg)
                            return msg

            # Regime/volatility position sizing (runs AFTER the safety caps, so an
            # injected oversized request is still blocked, not silently resized).
            # Resizes a BUY to a vol-targeted, regime-appropriate amount; in a CHOPPY
            # regime the size shrinks hard — often below the minimum, which
            # deterministically blocks trend-entries where the edge doesn't pay.
            if action == "BUY" and amount > 0:
                regime = None
                try:
                    vols = await price_feed.get_volatility()
                    tech = await price_feed.get_technical_indicators()
                    regime = tech.get(asset, {}).get("regime")
                    sized = sizing.max_buy_notional(
                        portfolio.get_total_value(prices), vols.get(asset), regime
                    )
                except Exception:
                    logger.exception("Position sizing failed; using requested amount")
                    sized = amount
                if 0 < sized < amount:
                    if sized < portfolio.min_trade_usd:
                        msg = (
                            f"🚫 **Sized out:** {asset} is in a **{regime or 'high-volatility'}** "
                            f"regime; vol/regime sizing reduces a safe entry to ~${sized:,.2f}, below "
                            f"the ${portfolio.min_trade_usd:,.2f} minimum — no trade."
                        )
                        logger.info(
                            "Sizing blocked BUY %s: sized $%.2f < min $%.2f (regime=%s)",
                            asset, sized, portfolio.min_trade_usd, regime,
                        )
                        audit_event("trade_blocked", reason="position_sizing", asset=asset,
                                    requested=amount, sized=sized, regime=regime)
                        if audit_log_fn:
                            await audit_log_fn(msg)
                        return msg
                    logger.info("Sizing resized BUY %s from $%.2f to $%.2f (regime=%s)",
                                asset, amount, sized, regime)
                    audit_event("trade_resized", asset=asset, requested=amount,
                                sized=sized, regime=regime)
                    amount = sized

            if action == "BUY":
                trade = portfolio.buy(asset, amount, price, reasoning=reasoning)
                audit_event("trade", action="BUY", asset=asset, usd_amount=amount,
                            quantity=trade["quantity"], fill_price=trade["fill_price"],
                            reasoning=reasoning)
                webhooks.publish_trade(trade, portfolio, prices)  # live SSE feed (O0)
                msg = f"💰 **Trade Executed:** **BUY** {trade['quantity']:.8f} {asset} @ **${trade['fill_price']:,.2f}**"
                if post_message_fn:
                    await post_message_fn("portfolio_manager", msg)
                if audit_log_fn:
                    await audit_log_fn(msg)
                return msg
            elif action == "SELL":
                trade = portfolio.sell(asset, amount, price, reasoning=reasoning)
                audit_event("trade", action="SELL", asset=asset, quantity=trade["quantity"],
                            fill_price=trade["fill_price"], reasoning=reasoning)
                webhooks.publish_trade(trade, portfolio, prices)  # live SSE feed (O0)
                msg = f"💰 **Trade Executed:** **SELL** {trade['quantity']:.8f} {asset} @ **${trade['fill_price']:,.2f}**"
                if post_message_fn:
                    await post_message_fn("portfolio_manager", msg)
                if audit_log_fn:
                    await audit_log_fn(msg)
                return msg
            return f"Error: Invalid action {action}"
            
        elif tool_name == "update_parameter":
            name = arguments.get("name")
            value = float(arguments.get("value", 0))
            if name == "min_trade_usd":
                old_value = portfolio.min_trade_usd
                portfolio.min_trade_usd = value
                audit_event("param_change", name="min_trade_usd", old=old_value, new=value)
                msg = f"⚙️ **Parameter Updated:** `min_trade_usd` set to **${value:.2f}**"
                if post_message_fn:
                    await post_message_fn("portfolio_manager", msg)
                if audit_log_fn:
                    await audit_log_fn(msg)
                return msg
            if name == "risk_halt":
                # Manual operator override of the Tier 3 drawdown breaker: value 0 clears
                # the halt (resume), any nonzero value latches it. Audited either way.
                from bot import risk_state
                state = risk_state.load()
                old_halt = bool(state.get("halted", False))
                state["halted"] = bool(value)
                if not state["halted"]:
                    state["halted_since"] = None
                risk_state.save(state)
                audit_event("param_change", name="risk_halt", old=old_halt, new=state["halted"])
                msg = f"🚦 **Risk halt {'SET' if state['halted'] else 'CLEARED'}** (drawdown breaker)."
                if post_message_fn:
                    await post_message_fn("portfolio_manager", msg)
                if audit_log_fn:
                    await audit_log_fn(msg)
                return msg
            return f"Error: Unknown parameter {name}"
            
        elif tool_name == "schedule_meeting":
            minutes = int(arguments.get("minutes", 0))
            meeting_scheduler.schedule_dynamic_meeting(minutes)
            msg = f"⏱️ **Dynamic Meeting Scheduled:** We will reconvene in **{minutes}** minutes."
            if post_message_fn:
                await post_message_fn("meeting_chair", msg)
            if audit_log_fn:
                await audit_log_fn(msg)
            return msg
            
        elif tool_name == "start_meeting_now":
            import asyncio
            asyncio.create_task(
                meeting_scheduler.schedule_emergency([
                    {"reason": "CEO invoked meeting via tool", "directive": "Meeting started immediately."}
                ])
            )
            msg = "🚨 **Meeting Initiated:** Waking up the full team immediately..."
            if post_message_fn:
                await post_message_fn("meeting_chair", msg)
            if audit_log_fn:
                await audit_log_fn(msg)
            return msg
            
        elif tool_name == "cancel_orders":
            asset = arguments.get("asset", "").upper()
            count = portfolio.cancel_all_orders(asset)
            audit_event("cancel_orders", asset=asset, count=count)
            msg = f"🗑️ **Orders Canceled:** Canceled {count} pending orders for **{asset}**."
            if post_message_fn:
                await post_message_fn("portfolio_manager", msg)
            if audit_log_fn:
                await audit_log_fn(msg)
            return msg

        elif tool_name == "place_limit_order":
            action = arguments.get("action", "").upper()
            asset = arguments.get("asset", "").upper()
            amount = float(arguments.get("amount", 0))
            target_price = float(arguments.get("target_price", 0))
            order_type = str(arguments.get("order_type", "LIMIT")).upper()
            if order_type not in ("LIMIT", "STOP", "TAKE_PROFIT"):
                return f"Error: invalid order_type '{order_type}' (LIMIT, STOP, or TAKE_PROFIT)."
            # STOP/TAKE_PROFIT are protective exits, SELL-side only: check_orders has no
            # BUY-side handler for them, so a BUY stop would just never trigger.
            if order_type in ("STOP", "TAKE_PROFIT") and action != "SELL":
                return f"Error: {order_type} orders are SELL-side only (protective exits)."

            order_id = portfolio.place_order(order_type, action, asset, amount, target_price)
            audit_event(
                "order_placed", order_type=order_type, action=action,
                asset=asset, amount=amount, target_price=target_price, order_id=order_id,
            )
            # ≤ triggers: LIMIT BUY (buy the dip) and STOP SELL (stop-loss).
            # ≥ triggers: LIMIT SELL (sell the rip) and TAKE_PROFIT SELL.
            triggers_below = (order_type == "LIMIT" and action == "BUY") or order_type == "STOP"
            direction_sym = "≤" if triggers_below else "≥"
            label = {"LIMIT": "Limit Order", "STOP": "Stop-Loss Order",
                     "TAKE_PROFIT": "Take-Profit Order"}[order_type]
            msg = (
                f"📝 **{label} Placed:** **{action}** {asset} triggers when price "
                f"{direction_sym} **${target_price:,.2f}** | Amount: {amount} | ID: `{order_id}`"
            )
            if post_message_fn:
                await post_message_fn("portfolio_manager", msg)
            if audit_log_fn:
                await audit_log_fn(msg)
            return msg

        else:
            return f"Error: Tool {tool_name} not found."
            
    except Exception as e:
        logger.exception(f"Error executing tool {tool_name}")
        return f"Error executing {tool_name}: {str(e)}"
