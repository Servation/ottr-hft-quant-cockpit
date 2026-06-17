import logging
from bot.price_feed import price_feed
from bot.portfolio import portfolio
from bot.scheduler import meeting_scheduler

logger = logging.getLogger(__name__)

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
            "description": "Schedule a dynamic follow-up meeting.",
            "parameters": {
                "type": "object",
                "properties": {
                    "minutes": {"type": "integer", "description": "Minutes from now to schedule the meeting"}
                },
                "required": ["minutes"]
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
    }
]

async def handle_tool_call(tool_name: str, arguments: dict, audit_log_fn=None, post_message_fn=None) -> str:
    try:
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
            
            if action == "BUY":
                trade = portfolio.buy(asset, amount, price)
                msg = f"💰 **Trade Executed:** **BUY** {trade['quantity']:.8f} {asset} @ **${trade['fill_price']:,.2f}**"
                if post_message_fn:
                    await post_message_fn("portfolio_manager", msg)
                if audit_log_fn:
                    await audit_log_fn(msg)
                return msg
            elif action == "SELL":
                trade = portfolio.sell(asset, amount, price)
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
                portfolio.min_trade_usd = value
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
            
        elif tool_name == "cancel_orders":
            asset = arguments.get("asset", "").upper()
            count = portfolio.cancel_all_orders(asset)
            msg = f"🗑️ **Orders Canceled:** Canceled {count} pending orders for **{asset}**."
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
