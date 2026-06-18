import logging
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Query, Body
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.config import settings, trading_config, translate
import app.config as config
from app.llm_connector import test_llm_connection, clean_base_url, generate_chat_completion
from app.services.market_proxy import market_proxy
from app.services.sse_manager import sse_manager
import json
import os
import uuid
import httpx

logger = logging.getLogger(__name__)
router = APIRouter()

# Request Models


class ChatEntry(BaseModel):
    sender: str
    text: str

class AgentChatRequest(BaseModel):
    message: str
    history: List[ChatEntry]

# Endpoints
@router.get("/health")
async def health():
    return {
        "status": "OK",
        "message": translate("health_ok")
    }

@router.get("/market-data")
async def get_market_data(symbols: Optional[str] = Query(None)):
    if symbols:
        target_symbols = [s.strip() for s in symbols.split(",") if s.strip()]
    else:
        target_symbols = [s.replace("USDT", "") for s in trading_config.symbols]

    try:
        results = {}
        for sym in target_symbols:
            price = await market_proxy.get_ticker(sym)
            results[sym] = {
                "symbol": sym,
                "price": price
            }
        logger.info(translate("ticker_fetch_success"))
        return results
    except Exception as e:
        logger.error(translate("ticker_fetch_failed", error=str(e)))
        raise HTTPException(status_code=500, detail=str(e))



@router.get("/events/stream")
async def events_stream():
    """
    Server-Sent Events endpoint streaming real-time broker execution logic.
    """
    return EventSourceResponse(sse_manager.subscribe())

@router.get("/portfolio/snapshot")
async def get_portfolio_snapshot():
    # Read from discord-bridge
    state_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../discord-bridge/data/portfolio_state.json"))
    portfolio_data = {"cash": 0.0, "holdings": {}, "total_pnl": 0.0, "trade_history": [], "min_trade_usd": 100.0}
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                portfolio_data = json.load(f)
        except Exception as e:
            logger.error(f"Failed to read discord portfolio state: {e}")

    # Fetch current prices for active holdings
    current_prices = {}
    total_holdings_value = 0.0
    purchase_prices = {}
    
    for sym, data in portfolio_data.get("holdings", {}).items():
        try:
            price = await market_proxy.get_ticker(sym)
            current_prices[sym] = price
            total_holdings_value += price * data.get("quantity", 0)
            purchase_prices[sym] = data.get("avg_cost", 0)
        except Exception:
            current_prices[sym] = data.get("avg_cost", 0)
            total_holdings_value += data.get("avg_cost", 0) * data.get("quantity", 0)
            purchase_prices[sym] = data.get("avg_cost", 0)

    total_value = portfolio_data.get("cash", 0) + total_holdings_value

    return {
        "total_value": total_value,
        "drawdown": 0.0,
        "portfolio_cap": trading_config.portfolio_cap,
        "drawdown_limit": trading_config.drawdown_limit,
        "slippage_limit": trading_config.slippage_limit,
        "stop_loss_limit": trading_config.stop_loss_limit,
        "symbols": trading_config.symbols,
        "interval_seconds": trading_config.interval_seconds,
        "trading_active": True,
        "holdings": portfolio_data.get("holdings", {}),
        "purchase_prices": purchase_prices,
        "usd_cash": portfolio_data.get("cash", 0),
        "current_prices": current_prices,
        "llm_fallback_base_url": settings.llm_fallback_base_url,
        "llm_fallback_api_key": settings.llm_fallback_api_key,
        "llm_fallback_model_id": settings.llm_fallback_model_id,
        "llm_fallback_active": settings.llm_fallback_active
    }

@router.get("/execution-logs")
async def get_execution_logs():
    state_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../discord-bridge/data/portfolio_state.json"))
    logs = []
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                portfolio_data = json.load(f)
                
            for i, trade in enumerate(portfolio_data.get("trade_history", [])):
                logs.append({
                    "id": f"trade-{i}-{trade.get('timestamp')}",
                    "timestamp": trade.get("timestamp"),
                    "symbol": trade.get("asset"),
                    "action": trade.get("action"),
                    "quantity": trade.get("quantity"),
                    "price": trade.get("fill_price"),
                    "vwap_fill_price": trade.get("fill_price"),
                    "slippage_pct": trade.get("slippage_pct", 0),
                    "fee_usd": trade.get("fee_usd", 0),
                    "reasoning": trade.get("reasoning", "Executed via Discord Meeting consensus.")
                })
        except Exception as e:
            logger.error(f"Failed to read discord execution logs: {e}")
            
    # Sort descending by timestamp
    logs.sort(key=lambda x: x["timestamp"], reverse=True)
    return logs


@router.post("/trading/start")
async def start_trading():
    return {"status": "SUCCESS"}

@router.post("/trading/stop")
async def stop_trading():
    return {"status": "SUCCESS"}

class TradingConfigRequest(BaseModel):
    strategy: str
    ticksPerMinute: int
    activeCryptos: List[str]
    stopLossLimit: float

@router.post("/trading/config")
async def set_trading_config(req: TradingConfigRequest):
    return {"status": "SUCCESS"}

@router.get("/optimizer/history")
async def get_optimizer_history():
    return []

class LLMConfigRequest(BaseModel):
    base_url: str
    api_key: str = ""
    model_id: str
    fallback_base_url: str = ""
    fallback_api_key: str = ""
    fallback_model_id: str = ""
    fallback_active: bool = True

@router.post("/llm/configure")
async def configure_llm(req: LLMConfigRequest):
    return {"status": "SUCCESS"}

class ResetBalanceRequest(BaseModel):
    balance: float = Field(..., ge=1.0, description="New starting balance value")

@router.post("/portfolio/reset-balance")
async def reset_balance(req: ResetBalanceRequest):
    return {"status": "SUCCESS"}



@router.post("/agent/chat")
async def agent_chat(req: AgentChatRequest):
    discord_api_url = "http://discord-bridge:8001/api/directive"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(discord_api_url, json={"message": req.message}, timeout=5.0)
            if response.status_code == 200:
                return {
                    "text": "CEO Directive broadcasted to the Discord Trading Floor successfully.",
                    "executed_commands": []
                }
            else:
                return {
                    "text": f"Failed to send directive. Discord bot returned {response.status_code}.",
                    "executed_commands": []
                }
    except Exception as e:
        logger.error(f"Failed to forward chat to discord bot: {e}")
        return {
            "text": f"Error: Failed to reach Discord Bot integration. Details: {str(e)}",
            "executed_commands": []
        }
