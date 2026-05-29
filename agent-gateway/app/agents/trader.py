import logging
import time
import httpx
from typing import Dict, Any, List, Optional
from app.config import settings, translate, trading_config

logger = logging.getLogger(__name__)

# Global execution logs list
execution_logs: List[Dict[str, Any]] = []

class TraderAgent:
    async def get_portfolio_value(self) -> float:
        """
        Retrieves current total portfolio value from Java engine or fallback.
        """
        try:
            url = f"{settings.java_engine_url}/api/v1/portfolio/snapshot"
            async with httpx.AsyncClient(timeout=3.0) as client:
                res = await client.get(url)
                if res.status_code == 200:
                    data = res.json()
                    return float(data.get("total_value", 100000.0))
        except Exception:
            pass
        
        try:
            from app.agents.portfolio_manager import portfolio_manager
            return portfolio_manager.mock_portfolio_value
        except Exception:
            return 100000.0  # Default fallback portfolio value

    async def estimate_execution_slippage(self, symbol: str, side: str, quantity: float, mid_price: float) -> float:
        """
        Simulates filling a market order of the specified side and quantity against
        the Java matching engine's order book depth to estimate slippage percentage.
        Returns slippage as a decimal (e.g. 0.0005 for 0.05% slippage).
        """
        try:
            url = f"{settings.java_engine_url}/api/v1/orderbook/{symbol}"
            async with httpx.AsyncClient(timeout=2.0) as client:
                res = await client.get(url)
                if res.status_code == 200:
                    data = res.json()
                    levels = data.get("asks" if side == "BUY" else "bids", [])
                    if not levels:
                        return 0.0005 # Default baseline 0.05%
                    
                    remaining = quantity
                    filled_val = 0.0
                    for level in levels:
                        l_price = float(level.get("price", 0))
                        l_qty = float(level.get("quantity", 0))
                        
                        take = min(remaining, l_qty)
                        filled_val += take * l_price
                        remaining -= take
                        if remaining <= 1e-6:
                            break
                            
                    if remaining > 0.0:
                        # Reached end of book, add simulated slippage for unfilled part
                        filled_val += remaining * (mid_price * (1.05 if side == "BUY" else 0.95))
                    
                    avg_price = filled_val / quantity
                    slippage = abs(avg_price - mid_price) / mid_price
                    return slippage
        except Exception as e:
            logger.warning(f"Failed to estimate execution slippage from matching engine order book: {e}")
        
        # Fallback simulated slippage based on size
        est_base = 0.0005
        if quantity * mid_price > 5000:
            est_base += 0.0010
        return est_base

    def select_execution_path(self, confidence: float, order_value_usd: float, estimated_slippage: float) -> str:
        """
        Selects execution path: DIRECT for high confidence and low slippage,
        VWAP for medium confidence or medium slippage, and TWAP for high slippage.
        """
        if estimated_slippage < 0.0005 and confidence >= 0.8 and order_value_usd < 3000:
            return "DIRECT"
        elif estimated_slippage < 0.0015 and confidence >= 0.7:
            return "VWAP"
        else:
            return "TWAP"

    async def execute_trade(
        self,
        symbol: str,
        action: str,
        current_price: float,
        confidence: float,
        inference_delay_ms: int,
        quantity: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Formulates order size, selects path, and calls Java engine's /execute endpoint.
        """
        start_time = time.perf_counter()
        portfolio_value = await self.get_portfolio_value()
        
        # Formulate order size in USD: portfolio_value * cap * confidence
        if quantity is None:
            cap = trading_config.portfolio_cap
            order_value_usd = portfolio_value * cap * confidence
            quantity = order_value_usd / current_price
        else:
            order_value_usd = quantity * current_price

        # Estimate execution slippage before routing
        est_slippage = await self.estimate_execution_slippage(symbol, action, quantity, current_price)

        # Select execution path
        path = self.select_execution_path(confidence, order_value_usd, est_slippage)

        # Call Java engine /api/v1/execute
        url = f"{settings.java_engine_url}/api/v1/execute"
        payload = {
            "symbol": symbol,
            "side": action,
            "quantity": quantity,
            "price": current_price,
            "executionPath": path
        }
        headers = {
            "X-Inference-Delay-Ms": str(inference_delay_ms),
            "Content-Type": "application/json"
        }

        execution_status = "FAILED"
        error_msg = ""
        latency_ms = 0.0
        resp_data = None

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(url, json=payload, headers=headers)
                latency_ms = (time.perf_counter() - start_time) * 1000
                if response.status_code in (200, 201):
                    resp_data = response.json()
                    actual_status = resp_data.get("status", "SUCCESS")
                    if actual_status == "REJECTED":
                        execution_status = "FAILED"
                        error_msg = "Order rejected by matching engine (insufficient liquidity)"
                        logger.error(translate("trade_execution_failed", error=error_msg))
                    else:
                        execution_status = actual_status
                        logger.info(translate(
                            "trade_executed",
                            path=path,
                            latency=latency_ms,
                            size=f"{quantity:.6f} {symbol}"
                        ))
                else:
                    error_msg = f"Java engine returned status {response.status_code}: {response.text}"
                    logger.error(translate("trade_execution_failed", error=error_msg))
        except Exception as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            error_msg = str(e)
            # Simulated Execution Fallback (in case Java engine is not active yet)
            execution_status = "SIMULATED"
            logger.warning(f"Java engine not available ({e}). Simulating execution.")
            logger.info(translate(
                "trade_executed",
                path=path,
                latency=latency_ms,
                size=f"{quantity:.6f} {symbol} (Simulated)"
            ))

        # Build execution logs parameters with actual or simulated values
        vwap_fill_price = current_price
        slippage_pct = 0.05
        fee_usd = 0.0
        order_id = f"FILL-{int(time.time() * 1000) % 9000 + 1000}"

        if execution_status in ("FILLED", "PARTIALLY_FILLED", "SUCCESS") and resp_data:
            vwap_fill_price = resp_data.get("vwapPrice", current_price)
            slippage_pct = resp_data.get("slippage", 0.0005) * 100
            fee_usd = resp_data.get("feeDeducted", 0.0)
            order_id = resp_data.get("orderId", order_id)
            latency_ms = resp_data.get("actualLatencyMs", latency_ms)
            quantity = resp_data.get("filledQuantity", quantity)
        elif execution_status == "SIMULATED":
            # 0.05% simulated slippage
            est_slippage = 0.0005
            slippage_pct = est_slippage * 100
            if action == "BUY":
                vwap_fill_price = current_price * (1.0 + est_slippage)
            else:
                vwap_fill_price = current_price * (1.0 - est_slippage)
            # 0.1% transaction fee
            fee_usd = order_value_usd * 0.001

        log_entry = {
            "id": order_id,
            "timestamp": time.time(),
            "symbol": symbol,
            "action": action,
            "quantity": quantity,
            "price": current_price,
            "vwap_fill_price": vwap_fill_price,
            "slippage_pct": slippage_pct,
            "fee_usd": fee_usd,
            "execution_value_usd": quantity * vwap_fill_price,
            "execution_path": path,
            "status": execution_status,
            "error": error_msg,
            "inference_delay_ms": inference_delay_ms,
            "execution_latency_ms": latency_ms
        }
        
        execution_logs.append(log_entry)
        # Cap logs list at 100 entries
        if len(execution_logs) > 100:
            execution_logs.pop(0)

        return log_entry

trader = TraderAgent()
