import logging
import os
from bot.price_feed import price_feed
from bot.portfolio import portfolio
from bot.scheduler import meeting_scheduler
from bot.audit import audit_event
from bot import settings

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
                    "amount": {"type": "number", "description": "The amount in USD for BUY, or quantity for SELL."}
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
                    "name": {"type": "string", "enum": ["min_trade_usd"], "description": "The parameter to update"},
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
                "Place a persistent limit order at a key price level. "
                "A BUY triggers when price falls to or below the target; "
                "a SELL triggers when price rises to or above the target. "
                "Orders are checked every 60 seconds and survive between meetings."
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
            
            prices = await price_feed.get_prices()
            if asset not in prices:
                return f"Error: Price for {asset} not found."
            price = prices[asset]["price"]

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

            # SOL exposure cap: block BUY if it would push SOL above the portfolio % limit.
            # Midas/Zephyr set this via thresholds.max_sol_exposure_pct in settings.yaml.
            if action == "BUY" and asset == "SOL":
                max_sol_pct = float(settings.get("thresholds", {}).get("max_sol_exposure_pct", 0) or 0)
                if max_sol_pct > 0:
                    total_value = portfolio.get_total_value(prices)
                    sol_qty = portfolio._state["holdings"].get("SOL", {}).get("quantity", 0.0)
                    current_sol_value = sol_qty * price
                    projected_sol_value = current_sol_value + amount
                    projected_total = total_value + amount
                    if projected_total > 0:
                        projected_pct = (projected_sol_value / projected_total) * 100
                        if projected_pct > max_sol_pct:
                            msg = (
                                f"🚫 **SOL exposure cap:** buying ${amount:,.2f} would push SOL to "
                                f"**{projected_pct:.1f}%** of the portfolio, exceeding the "
                                f"**{max_sol_pct:.0f}%** cap. Reduce position size or wait for rebalancing."
                            )
                            logger.warning(
                                "SOL exposure cap blocked BUY SOL $%.2f "
                                "(projected %.1f%% > cap %.1f%%)",
                                amount, projected_pct, max_sol_pct,
                            )
                            audit_event(
                                "trade_blocked", reason="sol_exposure_cap",
                                action=action, asset=asset, notional=amount,
                                projected_pct=projected_pct, cap=max_sol_pct,
                            )
                            if audit_log_fn:
                                await audit_log_fn(msg)
                            return msg

            if action == "BUY":
                trade = portfolio.buy(asset, amount, price)
                audit_event("trade", action="BUY", asset=asset, usd_amount=amount,
                            quantity=trade["quantity"], fill_price=trade["fill_price"])
                msg = f"💰 **Trade Executed:** **BUY** {trade['quantity']:.8f} {asset} @ **${trade['fill_price']:,.2f}**"
                if post_message_fn:
                    await post_message_fn("portfolio_manager", msg)
                if audit_log_fn:
                    await audit_log_fn(msg)
                return msg
            elif action == "SELL":
                trade = portfolio.sell(asset, amount, price)
                audit_event("trade", action="SELL", asset=asset, quantity=trade["quantity"],
                            fill_price=trade["fill_price"])
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

            order_id = portfolio.place_order("LIMIT", action, asset, amount, target_price)
            audit_event(
                "order_placed", order_type="LIMIT", action=action,
                asset=asset, amount=amount, target_price=target_price, order_id=order_id,
            )
            direction_sym = "≤" if action == "BUY" else "≥"
            msg = (
                f"📝 **Limit Order Placed:** **{action}** {asset} triggers when price "
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
