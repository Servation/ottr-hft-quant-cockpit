import logging
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Query, Body, Header, Depends, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.config import trading_config, translate
import app.config as config
from app.llm_connector import test_llm_connection, clean_base_url, generate_chat_completion
from app.services.market_proxy import market_proxy
from app.services.sse_manager import sse_manager
import json
import os
import hmac
import uuid
import httpx


def require_api_key(x_api_key: str = Header(default="")):
    """Shared-secret auth for state-changing gateway routes.

    Fail-closed: if OTTR_API_KEY is not configured the route is unusable (503),
    because an unauthenticated mutation endpoint is the spoofing/EoP hole from
    threat_model.md. Read-only routes intentionally stay open (the SSE stream
    can't carry custom headers); CORS lockdown is handled separately (Phase 3).
    """
    expected = os.getenv("OTTR_API_KEY", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Server auth not configured")
    if not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


import time as _time


class _RateLimiter:
    """Sliding-window per-key limiter (in-memory, no deps)."""

    def __init__(self, max_calls: int, window: float):
        self.max_calls = max_calls
        self.window = window
        self._hits = {}

    def allow(self, key) -> bool:
        if self.max_calls <= 0:
            return True
        now = _time.monotonic()
        cutoff = now - self.window
        q = self._hits.setdefault(key, [])
        while q and q[0] < cutoff:
            q.pop(0)
        if len(q) >= self.max_calls:
            return False
        q.append(now)
        return True


_gw_rate = _RateLimiter(int(os.getenv("API_RATE_LIMIT", "60")), 60.0)


def rate_limit(request: Request):
    ip = request.client.host if request.client else "unknown"
    if not _gw_rate.allow(ip):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")


def _portfolio_state_path() -> str:
    """Path to the AUTHORITATIVE portfolio file written by discord-bridge.

    Configurable via PORTFOLIO_STATE_PATH so a Docker deploy can point at the
    shared volume (single source of truth) instead of a brittle relative path.
    """
    import os as _os
    return _os.getenv("PORTFOLIO_STATE_PATH") or _os.path.abspath(
        _os.path.join(_os.path.dirname(__file__), "../../../discord-bridge/data/portfolio_state.json")
    )


def _bridge_base_url() -> str:
    """Base URL of the discord-bridge API (same host the directive call uses)."""
    return os.getenv("DISCORD_BRIDGE_URL", "http://discord-bridge:8001")


async def _fetch_performance() -> Optional[dict]:
    """Best-effort fetch of computed performance metrics from the bridge.

    Returns None if the bridge is unreachable so /portfolio/snapshot still
    renders (the portfolio is read from the shared file independently). The
    metric *logic* lives on the bridge to avoid a second source of truth.
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(_bridge_base_url() + "/api/performance", timeout=3.0)
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        logger.warning(f"Performance metrics unavailable from bridge: {e}")
    return None

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
        raise HTTPException(status_code=500, detail="Failed to fetch market data")



@router.get("/events/stream")
async def events_stream():
    """
    Server-Sent Events endpoint streaming real-time broker execution logic.
    """
    return EventSourceResponse(sse_manager.subscribe())

@router.get("/portfolio/snapshot")
async def get_portfolio_snapshot():
    # Read from discord-bridge
    state_path = _portfolio_state_path()
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

    # Real performance metrics from the bridge (best-effort; None if it's down).
    perf = await _fetch_performance()
    metrics_data = (perf or {}).get("metrics") or {}
    max_dd = metrics_data.get("max_drawdown")

    return {
        "total_value": total_value,
        # Real peak-to-trough drawdown (fraction). Falls back to 0.0 only when the
        # bridge has no curve yet, so the cash-fallback math downstream still works.
        "drawdown": max_dd if max_dd is not None else 0.0,
        "performance": {
            "total_return": metrics_data.get("total_return"),
            "cagr": metrics_data.get("cagr"),
            "sharpe": metrics_data.get("sharpe"),
            "sortino": metrics_data.get("sortino"),
            "max_drawdown": max_dd,
            "benchmark_return": metrics_data.get("benchmark_return"),
            "alpha": metrics_data.get("alpha"),
            "num_points": (perf or {}).get("num_points", 0),
        },
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
        # NOTE: LLM fallback config (incl. api_key) intentionally NOT returned here.
        # Secrets must never be sent to the client; the portfolio snapshot is
        # portfolio data only. (See AUDIT_PLAN.md Phase 1.)
    }

@router.get("/execution-logs")
async def get_execution_logs():
    state_path = _portfolio_state_path()
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


@router.post("/trading/start", dependencies=[Depends(require_api_key), Depends(rate_limit)])
async def start_trading():
    return {"status": "SUCCESS"}

@router.post("/trading/stop", dependencies=[Depends(require_api_key), Depends(rate_limit)])
async def stop_trading():
    return {"status": "SUCCESS"}

class TradingConfigRequest(BaseModel):
    strategy: str
    ticksPerMinute: int
    activeCryptos: List[str]
    stopLossLimit: float

@router.post("/trading/config", dependencies=[Depends(require_api_key), Depends(rate_limit)])
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

@router.post("/llm/configure", dependencies=[Depends(require_api_key), Depends(rate_limit)])
async def configure_llm(req: LLMConfigRequest):
    return {"status": "SUCCESS"}

class ResetBalanceRequest(BaseModel):
    balance: float = Field(..., ge=1.0, description="New starting balance value")

@router.post("/portfolio/reset-balance", dependencies=[Depends(require_api_key), Depends(rate_limit)])
async def reset_balance(req: ResetBalanceRequest):
    return {"status": "SUCCESS"}



@router.post("/agent/chat", dependencies=[Depends(require_api_key), Depends(rate_limit)])
async def agent_chat(req: AgentChatRequest):
    discord_api_url = "http://discord-bridge:8001/api/directive"
    try:
        api_key = os.getenv("OTTR_API_KEY", "")
        fwd_headers = {"X-API-Key": api_key} if api_key else {}
        async with httpx.AsyncClient() as client:
            response = await client.post(discord_api_url, json={"message": req.message}, headers=fwd_headers, timeout=5.0)
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
            "text": "Error: Failed to reach the Discord Bot integration.",
            "executed_commands": []
        }
