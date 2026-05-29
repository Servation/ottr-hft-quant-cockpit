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
from app.agents.trader import execution_logs
from app.agents.portfolio_manager import portfolio_manager
from app.agents.performance_optimizer import performance_optimizer

logger = logging.getLogger(__name__)
router = APIRouter()

# Request Models
class LLMConfigureRequest(BaseModel):
    llm_base_url: str = Field(..., description="OpenAI-compatible Base URL")
    llm_api_key: str = Field(..., description="API Key for the provider")
    llm_model_id: str = Field(..., description="Model Identifier")
    locale: Optional[str] = Field(None, description="Output language locale ('en' or 'ru')")
    llm_fallback_base_url: Optional[str] = Field(None, description="Fallback OpenAI-compatible Base URL")
    llm_fallback_api_key: Optional[str] = Field(None, description="Fallback API Key")
    llm_fallback_model_id: Optional[str] = Field(None, description="Fallback Model Identifier")
    llm_fallback_active: Optional[bool] = Field(None, description="Whether fallback routing is enabled")

class TradingConfigRequest(BaseModel):
    portfolio_cap: Optional[float] = Field(None, description="Max percentage of portfolio value per trade (e.g. 0.05)")
    drawdown_limit: Optional[float] = Field(None, description="Max acceptable drawdown before trade veto (e.g. 0.045)")
    slippage_limit: Optional[float] = Field(None, description="Max acceptable estimated slippage before trade veto (e.g. 0.0008)")
    symbols: Optional[List[str]] = Field(None, description="List of trade symbols (e.g. ['BTCUSDT'])")
    interval_seconds: Optional[int] = Field(None, description="Consensus evaluation frequency in seconds")
    stop_loss_limit: Optional[float] = Field(None, description="Max acceptable drop below average purchase price (e.g. 0.07)")
    strategy: Optional[str] = Field(None, description="Active strategy style (e.g. 'DD90/10')")

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

@router.get("/market-news")
async def get_market_news():
    try:
        news = await market_proxy.get_news()
        logger.info(translate("news_fetch_success"))
        return news
    except Exception as e:
        logger.error(translate("news_fetch_failed", error=str(e)))
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/events/stream")
async def events_stream():
    """
    Server-Sent Events endpoint streaming real-time broker execution logic.
    """
    return EventSourceResponse(sse_manager.subscribe())

@router.get("/portfolio/snapshot")
async def get_portfolio_snapshot():
    await portfolio_manager.get_current_portfolio_value()
    current_prices = await portfolio_manager.get_current_prices()
    return {
        "total_value": portfolio_manager.mock_portfolio_value,
        "drawdown": portfolio_manager.mock_drawdown,
        "portfolio_cap": trading_config.portfolio_cap,
        "drawdown_limit": trading_config.drawdown_limit,
        "slippage_limit": trading_config.slippage_limit,
        "stop_loss_limit": trading_config.stop_loss_limit,
        "symbols": trading_config.symbols,
        "interval_seconds": trading_config.interval_seconds,
        "trading_active": config.trading_active,
        "holdings": portfolio_manager.holdings,
        "purchase_prices": portfolio_manager.purchase_prices,
        "usd_cash": portfolio_manager.usd_cash,
        "current_prices": current_prices,
        "llm_fallback_base_url": settings.llm_fallback_base_url,
        "llm_fallback_api_key": settings.llm_fallback_api_key,
        "llm_fallback_model_id": settings.llm_fallback_model_id,
        "llm_fallback_active": settings.llm_fallback_active
    }

@router.get("/execution-logs")
async def get_execution_logs():
    return execution_logs

@router.post("/trading/config")
async def update_trading_config(req: TradingConfigRequest):
    if req.portfolio_cap is not None:
        trading_config.portfolio_cap = req.portfolio_cap
    if req.drawdown_limit is not None:
        trading_config.drawdown_limit = req.drawdown_limit
    if req.slippage_limit is not None:
        trading_config.slippage_limit = req.slippage_limit
    if req.symbols is not None:
        trading_config.symbols = req.symbols
    if req.interval_seconds is not None:
        trading_config.interval_seconds = req.interval_seconds
    if req.stop_loss_limit is not None:
        trading_config.stop_loss_limit = req.stop_loss_limit
    if req.strategy is not None:
        trading_config.strategy = req.strategy

    logger.info(translate("config_updated"))
    return {
        "status": "SUCCESS",
        "message": translate("config_updated"),
        "config": trading_config.dict()
    }

@router.post("/llm/configure")
async def configure_llm(req: LLMConfigureRequest):
    settings.llm_base_url = clean_base_url(req.llm_base_url)
    settings.llm_api_key = req.llm_api_key
    settings.llm_model_id = req.llm_model_id
    settings.llm_fallback_base_url = clean_base_url(req.llm_fallback_base_url) if req.llm_fallback_base_url else None
    settings.llm_fallback_api_key = req.llm_fallback_api_key if req.llm_fallback_api_key else None
    settings.llm_fallback_model_id = req.llm_fallback_model_id if req.llm_fallback_model_id else None
    if req.llm_fallback_active is not None:
        settings.llm_fallback_active = req.llm_fallback_active
    
    if req.locale:
        if req.locale in ("en", "ru"):
            settings.locale = req.locale
        else:
            logger.warning(translate("invalid_locale"))
            
    logger.info(translate("llm_configured"))
    return {
        "status": "SUCCESS",
        "message": translate("llm_configured"),
        "locale": settings.locale
    }

@router.post("/llm/test-connection")
async def test_llm(req: LLMConfigureRequest):
    success, res, latency = await test_llm_connection(
        base_url=req.llm_base_url,
        api_key=req.llm_api_key,
        model_id=req.llm_model_id
    )
    if success:
        msg = translate("llm_test_success", latency=latency, response=res)
        logger.info(msg)
        return {
            "status": "SUCCESS",
            "message": msg,
            "latency_seconds": latency,
            "response": res
        }
    else:
        msg = translate("llm_test_failed", error=res)
        logger.error(msg)
        return {
            "status": "FAILED",
            "message": msg
        }

@router.post("/llm/test-fallback-connection")
async def test_fallback_llm(req: LLMConfigureRequest):
    success, res, latency = await test_llm_connection(
        base_url=req.llm_fallback_base_url,
        api_key=req.llm_fallback_api_key,
        model_id=req.llm_fallback_model_id
    )
    if success:
        msg = translate("llm_test_success", latency=latency, response=res)
        logger.info(f"Fallback LLM test success: {msg}")
        return {
            "status": "SUCCESS",
            "message": f"Fallback Success ({latency:.2f}s): {res}",
            "latency_seconds": latency,
            "response": res
        }
    else:
        msg = translate("llm_test_failed", error=res)
        logger.error(f"Fallback LLM test failed: {msg}")
        return {
            "status": "FAILED",
            "message": f"Fallback Failed: {res}"
        }

@router.post("/trading/start")
async def start_trading():
    try:
        portfolio_manager.start()
        return {
            "status": "SUCCESS",
            "message": translate("trading_started")
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/trading/stop")
async def stop_trading():
    try:
        portfolio_manager.stop()
        return {
            "status": "SUCCESS",
            "message": translate("trading_stopped")
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

class ResetBalanceRequest(BaseModel):
    balance: float = Field(..., ge=1.0, description="New starting balance value")

@router.post("/portfolio/reset-balance")
async def reset_balance(req: ResetBalanceRequest):
    await portfolio_manager.reset_portfolio(req.balance)
    return {
        "status": "SUCCESS",
        "message": f"Portfolio balance reset to ${req.balance:,.2f}"
    }

@router.get("/optimizer/history")
async def get_optimizer_history():
    return performance_optimizer.optimization_history

@router.post("/optimizer/run")
async def run_optimizer():
    try:
        await portfolio_manager.get_current_portfolio_value()
        opt_res = await performance_optimizer.optimize(
            portfolio_manager.mock_portfolio_value,
            portfolio_manager.mock_drawdown
        )
        # Broadcast updated history over SSE
        await sse_manager.broadcast("optimization_history", performance_optimizer.optimization_history)
        return {
            "status": "SUCCESS",
            "result": opt_res
        }
    except Exception as e:
        logger.error(f"Manual optimization run failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/agent/chat")
async def agent_chat(req: AgentChatRequest):
    """
    Hybrid chat interface endpoint. Answers queries about trades and executes config overrides.
    """
    from app.services.journal_manager import journal_manager
    import re
    import json
    
    # 1. Fetch current system state and recent trades across all symbols for context
    try:
        journal = journal_manager._load_journal()
        # Sort by timestamp descending to get newest first, then take last 15
        sorted_journal = sorted(journal, key=lambda x: x.get("timestamp", 0))
        recent_trades = sorted_journal[-15:]
        
        import time
        lines = []
        for t_entry in recent_trades:
            ts_val = t_entry.get("timestamp")
            ts_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts_val)) if isinstance(ts_val, (int, float)) else str(ts_val)
            lines.append(
                f"- [ID: {t_entry.get('id')}] [{ts_str}] {t_entry.get('symbol')} {t_entry.get('action')} "
                f"{t_entry.get('quantity', 0.0):.6f} @ {t_entry.get('price', 0.0):.2f} USD "
                f"(Status: {t_entry.get('status')}, Fee: ${t_entry.get('fee_usd', 0.0):.2f}, Slippage: {t_entry.get('slippage_pct', 0.0):.4f}%). "
                f"Reasoning: {t_entry.get('reasoning', 'N/A').replace(chr(10), ' ')}"
            )
        recent_journal = "\n".join(lines) if lines else "No trade history available yet."
    except Exception as e:
        logger.error(f"Failed to load recent journal context: {e}")
        recent_journal = "No recent logs available."

    # 1b. Scan message and history for specific transaction IDs (FILL-xxxx, JOURNAL-xxxx, or UUID)
    id_pattern = r"(FILL-\d+|JOURNAL-\d+|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    tx_ids = re.findall(id_pattern, req.message)
    for entry in req.history:
        tx_ids.extend(re.findall(id_pattern, entry.text))
    # Deduplicate
    tx_ids = list(set(tx_ids))

    specific_tx_context = ""
    if tx_ids:
        # Search memory execution logs
        found_tx = None
        for tx_id in tx_ids:
            for log in execution_logs:
                if str(log.get("id")) == tx_id:
                    found_tx = log
                    break
            if found_tx:
                break
            
            # Search database trade journal
            try:
                for entry in journal_manager._load_journal():
                    if str(entry.get("id")) == tx_id:
                        found_tx = entry
                        break
            except Exception:
                pass
            if found_tx:
                break

        if found_tx:
            ts_val = found_tx.get("timestamp")
            ts_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts_val)) if isinstance(ts_val, (int, float)) else str(ts_val)
            specific_tx_context = f"""
=== USER IS INQUIRING ABOUT THIS SPECIFIC TRANSACTION ===
- Transaction ID: {found_tx.get('id')}
- Timestamp: {ts_str}
- Symbol: {found_tx.get('symbol')}
- Action: {found_tx.get('action')}
- Quantity: {found_tx.get('quantity')}
- Price: {found_tx.get('price')} USD
- VWAP Fill Price: {found_tx.get('vwap_fill_price', found_tx.get('price'))} USD
- Slippage: {found_tx.get('slippage_pct')}%
- Fee: ${found_tx.get('fee_usd')}
- Status: {found_tx.get('status')}
- Execution Path: {found_tx.get('execution_path', 'N/A')}
- Execution Latency: {found_tx.get('execution_latency_ms', found_tx.get('latencyMs', 0.0))} ms
- AI Decision Chain-of-Thought (CoT) Reasoning behind this trade: {found_tx.get('reasoning', 'No reasoning recorded.')}
=========================================================
"""
        
    portfolio_snap = {
        "cash_balance_usd": portfolio_manager.usd_cash,
        "total_value_usd": portfolio_manager.mock_portfolio_value,
        "holdings": portfolio_manager.holdings,
        "purchase_prices": portfolio_manager.purchase_prices,
        "trading_active": config.trading_active,
        "current_config": {
            "symbols": config.trading_config.symbols,
            "stop_loss_limit": config.trading_config.stop_loss_limit,
            "portfolio_cap": config.trading_config.portfolio_cap,
            "drawdown_limit": config.trading_config.drawdown_limit,
            "slippage_limit": config.trading_config.slippage_limit,
            "interval_seconds": config.trading_config.interval_seconds
        }
    }
    
    # 2. Format Chat History
    history_str = ""
    for entry in req.history:
        role_label = "User" if entry.sender == "user" else "Assistant"
        history_str += f"{role_label}: {entry.text}\n"
 
    # 3. Construct System Prompt
    system_prompt = f"""You are the Supervisor Agent of a multi-agent crypto trading bot.
You communicate with the user, explain recent trade decisions, and adjust configuration settings when requested.

Current System State:
{json.dumps(portfolio_snap, indent=2)}

Recent Trade Journal History:
{recent_journal}
{specific_tx_context}

Your instructions:
1. Converse naturally and answer questions about the system state or past trades.
2. If the user asks to modify settings (e.g., set stop loss, add/remove assets, pause/resume trading, change limits, liquidate assets, reset balance), you must fulfill the request and append EXACTLY one JSON COMMAND block at the end of your response.
3. Keep your answers concise, clear, and quantitative.

Available Action Commands:
- Set stop loss limit: Change config.stop_loss_limit. (Value must be float e.g. 0.05 for 5%)
  JSON format:
  COMMAND: {{"action": "set_stop_loss", "limit": 0.05}}
- Set active symbols list: Overwrite watchlist. (Provide list of asset names like ["BTC", "ETH", "SOL"])
  JSON format:
  COMMAND: {{"action": "set_symbols", "symbols": ["BTC", "ETH", "SOL"]}}
- Toggle trading status: Pause or resume the consensus loop.
  JSON format:
  COMMAND: {{"action": "toggle_trading", "active": false}}
- Set portfolio cap limit: Change max percentage of portfolio value per trade (Value must be float e.g. 0.05 for 5%)
  JSON format:
  COMMAND: {{"action": "set_portfolio_cap", "limit": 0.05}}
- Set drawdown limit: Change max acceptable drawdown before trade veto (Value must be float e.g. 0.045 for 4.5%)
  JSON format:
  COMMAND: {{"action": "set_drawdown_limit", "limit": 0.045}}
- Set slippage limit: Change max acceptable estimated slippage before trade veto (Value must be float e.g. 0.0008 for 0.08%)
  JSON format:
  COMMAND: {{"action": "set_slippage_limit", "limit": 0.0008}}
- Set interval seconds: Change consensus evaluation interval (Value must be integer seconds e.g. 30)
  JSON format:
  COMMAND: {{"action": "set_interval", "seconds": 30}}
- Reset portfolio balance: Resets starting balance, holdings, and logs to a specific USD value (Value must be float e.g. 100000.0)
  JSON format:
  COMMAND: {{"action": "reset_portfolio", "balance": 100000.0}}
- Liquidate asset position: Sells all holdings of a specific crypto asset immediately (Value must be symbol string e.g. "ETH" or "SOL")
  JSON format:
  COMMAND: {{"action": "liquidate_position", "symbol": "ETH"}}

Example of a command response:
"I have updated the asset stop-loss limit to 5% as requested.
COMMAND: {{"action": "set_stop_loss", "limit": 0.05}}"
"""

    messages = [
        {"role": "system", "content": system_prompt}
    ]
    
    # Add history messages
    for entry in req.history:
        role = "user" if entry.sender == "user" else "assistant"
        messages.append({"role": role, "content": entry.text})
        
    # Append user latest query
    messages.append({"role": "user", "content": req.message})
    
    # 4. Generate LLM Completion
    try:
        raw_response, inference_delay = await generate_chat_completion(messages, temperature=0.3, max_tokens=800, timeout=15.0)
    except Exception as e:
        logger.error(f"Chat completion failed: {e}")
        return {
            "text": f"Error: Failed to query local model. Details: {str(e)}",
            "executed_commands": []
        }
        
    # 5. Parse and Execute Command Blocks
    executed = []
    cleaned_response = raw_response
    
    match = re.search(r"COMMAND:\s*(\{.*?\})", raw_response, re.DOTALL)
    if match:
        cmd_json_str = match.group(1)
        cleaned_response = raw_response.replace(match.group(0), "").strip()
        
        try:
            cmd = json.loads(cmd_json_str)
            action = cmd.get("action")
            
            if action == "set_stop_loss":
                limit_val = cmd.get("limit")
                if isinstance(limit_val, (int, float)):
                    await portfolio_manager.update_stop_loss(float(limit_val))
                    executed.append(cmd)
            elif action == "set_symbols":
                symbols_list = cmd.get("symbols")
                if isinstance(symbols_list, list):
                    await portfolio_manager.update_active_symbols(symbols_list)
                    executed.append(cmd)
            elif action == "toggle_trading":
                active_val = cmd.get("active")
                if isinstance(active_val, bool):
                    await portfolio_manager.set_trading_state(active_val)
                    executed.append(cmd)
            elif action == "set_portfolio_cap":
                limit_val = cmd.get("limit")
                if isinstance(limit_val, (int, float)):
                    await portfolio_manager.update_portfolio_cap(float(limit_val))
                    executed.append(cmd)
            elif action == "set_drawdown_limit":
                limit_val = cmd.get("limit")
                if isinstance(limit_val, (int, float)):
                    await portfolio_manager.update_drawdown_limit(float(limit_val))
                    executed.append(cmd)
            elif action == "set_slippage_limit":
                limit_val = cmd.get("limit")
                if isinstance(limit_val, (int, float)):
                    await portfolio_manager.update_slippage_limit(float(limit_val))
                    executed.append(cmd)
            elif action == "set_interval":
                secs = cmd.get("seconds")
                if isinstance(secs, int):
                    await portfolio_manager.update_interval(secs)
                    executed.append(cmd)
            elif action == "reset_portfolio":
                bal = cmd.get("balance")
                if isinstance(bal, (int, float)):
                    await portfolio_manager.reset_portfolio(float(bal))
                    executed.append(cmd)
            elif action == "liquidate_position":
                sym = cmd.get("symbol")
                if isinstance(sym, str):
                    res_msg = await portfolio_manager.liquidate_asset(sym)
                    executed.append(cmd)
                    cleaned_response += f"\n\n[Execution Info: {res_msg}]"
        except Exception as e:
            logger.error(f"Failed to execute command '{cmd_json_str}': {e}")
            cleaned_response += f"\n\n[System Alert: Failed to execute override command: {str(e)}]"

    return {
        "text": cleaned_response,
        "executed_commands": executed
    }
