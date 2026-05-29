import logging
from typing import Tuple, Optional
from app.config import settings, trading_config, translate

logger = logging.getLogger(__name__)

class RiskAuditorAgent:
    def audit_trade(
        self,
        symbol: str,
        action: str,
        quantity: float,
        price: float,
        portfolio_value: float,
        current_drawdown: float,
        estimated_slippage: float,
        current_holding: float = 0.0,
        strategy: str = "DD90/10",
        usd_cash: Optional[float] = None,
        volatility: Optional[float] = None
    ) -> Tuple[bool, str]:
        """
        Vets a proposed trade against risk metrics:
        - Portfolio Cap (default 5% of total portfolio value)
        - Drawdown Limit (default 4.5% drawdown)
        - Slippage Limit (default 0.08% slippage)
        - Spot Holdings Limit (rejects shorting on SELL orders)
        - Sufficient Cash check (rejects BUY orders exceeding cash balance)
        - Target Allocation Profile weight limits (with +5% soft buffer)
        """
        # 1. Check for sufficient holdings on SELL actions
        if action == "SELL" and quantity > current_holding:
            limit_str = (
                f"Insufficient Holdings. Proposed sell quantity {quantity:.6f} exceeds current holdings of {current_holding:.6f} {symbol}"
                if settings.locale == "en" else
                f"Недостаточно активов. Объем продажи {quantity:.6f} превышает текущие запасы {current_holding:.6f} {symbol}"
            )
            reason = translate(
                "risk_limit_exceeded",
                limit=limit_str
            )
            logger.warning(translate("trade_vetoed", reason=reason))
            return False, reason

        # 1.5. Check for sufficient cash balance on BUY actions
        if action == "BUY" and usd_cash is not None:
            order_value = quantity * price
            if order_value > usd_cash + 1e-2:
                limit_str = (
                    f"Insufficient Cash. Proposed buy order value {order_value:.2f} USD exceeds available cash balance of {usd_cash:.2f} USD"
                    if settings.locale == "en" else
                    f"Недостаточно свободных средств. Стоимость покупки {order_value:.2f} USD превышает доступный баланс {usd_cash:.2f} USD"
                )
                reason = translate(
                    "risk_limit_exceeded",
                    limit=limit_str
                )
                logger.warning(translate("trade_vetoed", reason=reason))
                return False, reason

        # 2. Check strategy target allocation limits on BUY orders (with a +5% soft buffer)
        if action == "BUY":
            is_dd = strategy == "DD90/10"
            if is_dd:
                if symbol == "BTCUSDT":
                    target_pct = 0.818
                elif symbol == "ETHUSDT":
                    target_pct = 0.064
                else:
                    target_pct = 0.027  # Altcoins Cap
            else:
                if symbol == "BTCUSDT":
                    target_pct = 0.545
                elif symbol == "ETHUSDT":
                    target_pct = 0.227
                else:
                    target_pct = 0.136  # Altcoins Cap
            
            # Allow a +5% soft buffer (0.05) on top of the target percentage
            max_allowed_pct = target_pct + 0.05
            
            current_value = current_holding * price
            proposed_value = quantity * price
            total_proposed_value = current_value + proposed_value
            
            proposed_percentage = total_proposed_value / portfolio_value if portfolio_value > 0 else 0.0
            
            if proposed_percentage > max_allowed_pct:
                limit_str = (
                    f"Target Allocation Profile Exceeded. Proposed purchase of {symbol} would push its weight to {proposed_percentage * 100:.1f}%, "
                    f"exceeding the target {target_pct * 100:.1f}% + 5% soft buffer limit ({max_allowed_pct * 100:.1f}%)"
                    if settings.locale == "en" else
                    f"Превышен профиль целевой аллокации. Покупка {symbol} увеличит долю актива до {proposed_percentage * 100:.1f}%, "
                    f"что превышает целевой показатель {target_pct * 100:.1f}% + 5% мягкого буфера ({max_allowed_pct * 100:.1f}%)"
                )
                reason = translate("risk_limit_exceeded", limit=limit_str)
                logger.warning(translate("trade_vetoed", reason=reason))
                return False, reason

        # 3. Check Portfolio Cap
        order_value = quantity * price
        cap_fraction = trading_config.portfolio_cap
        
        # Volatility-based portfolio cap scaling
        if volatility is not None and volatility > 0.015:
            scale_factor = 0.015 / volatility
            scale_factor = max(0.2, min(1.0, scale_factor))
            cap_fraction *= scale_factor
            logger.info(f"High volatility detected ({volatility * 100:.2f}%). Scaling maximum trade portfolio cap fraction to {cap_fraction * 100:.2f}% (Scale factor: {scale_factor:.2f})")

        max_order_value = portfolio_value * cap_fraction
        
        # Add a tiny float buffer
        if order_value > max_order_value + 1e-5:
            reason = translate(
                "risk_limit_exceeded",
                limit=f"Portfolio Cap. Proposed trade value {order_value:.2f} USD exceeds the maximum allowed {max_order_value:.2f} USD ({cap_fraction * 100:.1f}%)"
            )
            logger.warning(translate("trade_vetoed", reason=reason))
            return False, reason

        # 4. Check Portfolio Drawdown
        drawdown_limit = trading_config.drawdown_limit
        if current_drawdown > drawdown_limit:
            reason = translate(
                "risk_limit_exceeded",
                limit=f"Drawdown Limit. Current drawdown {current_drawdown * 100:.2f}% exceeds the limit {drawdown_limit * 100:.2f}%"
            )
            logger.warning(translate("trade_vetoed", reason=reason))
            return False, reason

        # 5. Check Slippage Limit
        slippage_limit = trading_config.slippage_limit
        if estimated_slippage > slippage_limit:
            reason = translate(
                "risk_limit_exceeded",
                limit=f"Slippage Limit. Estimated slippage {estimated_slippage * 100:.4f}% exceeds the limit {slippage_limit * 100:.4f}%"
            )
            logger.warning(translate("trade_vetoed", reason=reason))
            return False, reason

        # All checks passed
        details = f"{action} {quantity:.6f} {symbol} @ {price:.2f} (Value: {order_value:.2f} USD, Slippage: {estimated_slippage*100:.3f}%)"
        logger.info(translate("trade_approved", details=details))
        return True, "Approved"

risk_auditor = RiskAuditorAgent()
