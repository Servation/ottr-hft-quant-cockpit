import asyncio
import logging
import time
from typing import Dict, Any, Optional, List
from app.config import settings, translate, trading_config, trading_active
import app.config as config  # to allow modifying global variables
from app.services.market_proxy import market_proxy
from app.services.sse_manager import sse_manager
from app.services.journal_manager import journal_manager
from app.agents.technical_analyst import technical_analyst
from app.agents.sentiment_analyst import sentiment_analyst
from app.agents.risk_auditor import risk_auditor
from app.agents.trader import trader
from app.agents.performance_optimizer import performance_optimizer

logger = logging.getLogger(__name__)

# Track active background task
_consensus_task: Optional[asyncio.Task] = None

class PortfolioManagerAgent:
    def __init__(self):
        self.mock_portfolio_value = 100000.0
        self.usd_cash = 100000.0
        self.holdings = {}  # symbol -> quantity
        self.purchase_prices = {}  # symbol -> weighted average cost
        self.mock_drawdown = 0.012  # 1.2% drawdown
        self.screener_cycle_counter = 0
        self.liquidated_on_startup = False
        self.load_state()
        self.agent_states = {
            "altcoin_screener": {
                "role": "altcoin_screener",
                "status": "IDLE",
                "message_en": "IDLE: Waiting to screen next cycle...",
                "message_ru": "ОЖИДАНИЕ: Ожидание анализа следующего цикла...",
                "last_updated": ""
            },
            "technical_analyst": {
                "role": "technical_analyst",
                "status": "IDLE",
                "message_en": "IDLE: Awaiting next price frame computation...",
                "message_ru": "ОЖИДАНИЕ: Ожидание вычисления следующего тика цен...",
                "last_updated": ""
            },
            "sentiment_analyst": {
                "role": "sentiment_analyst",
                "status": "IDLE",
                "message_en": "IDLE: Polling social indexes and order books...",
                "message_ru": "ОЖИДАНИЕ: Опрос социальных индексов и стаканов заявок...",
                "last_updated": ""
            },
            "trader": {
                "role": "trader",
                "status": "IDLE",
                "message_en": "IDLE: Position sizing pipeline in standby...",
                "message_ru": "ОЖИДАНИЕ: Конвейер сайзинга позиций в режиме ожидания...",
                "last_updated": ""
            },
            "risk_auditor": {
                "role": "risk_auditor",
                "status": "IDLE",
                "message_en": "IDLE: Compliance monitors at zero utilization...",
                "message_ru": "ОЖИДАНИЕ: Мониторы комплаенса не загружены...",
                "last_updated": ""
            },
            "performance_optimizer": {
                "role": "performance_optimizer",
                "status": "IDLE",
                "message_en": "IDLE: Awaiting next performance evaluation window...",
                "message_ru": "ОЖИДАНИЕ: Ожидание следующего окна оценки эффективности...",
                "last_updated": ""
            }
        }

    def get_initial_message_en(self, role: str) -> str:
        if role == "altcoin_screener":
            return "IDLE: Waiting to screen next cycle..."
        elif role == "technical_analyst":
            return "IDLE: Awaiting next price frame computation..."
        elif role == "sentiment_analyst":
            return "IDLE: Polling social indexes and order books..."
        elif role == "trader":
            return "IDLE: Position sizing pipeline in standby..."
        elif role == "risk_auditor":
            return "IDLE: Compliance monitors at zero utilization..."
        elif role == "performance_optimizer":
            return "IDLE: Awaiting next performance evaluation window..."
        return "IDLE"

    def get_initial_message_ru(self, role: str) -> str:
        if role == "altcoin_screener":
            return "ОЖИДАНИЕ: Ожидание анализа следующего цикла..."
        elif role == "technical_analyst":
            return "ОЖИДАНИЕ: Ожидание вычисления следующего тика цен..."
        elif role == "sentiment_analyst":
            return "ОЖИДАНИЕ: Опрос социальных индексов и стаканов заявок..."
        elif role == "trader":
            return "ОЖИДАНИЕ: Конвейер сайзинга позиций в режиме ожидания..."
        elif role == "risk_auditor":
            return "ОЖИДАНИЕ: Мониторы комплаенса не загружены..."
        elif role == "performance_optimizer":
            return "ОЖИДАНИЕ: Ожидание следующего окна оценки эффективности..."
        return "ОЖИДАНИЕ"


    def load_state(self):
        import os
        import json
        state_file = os.path.join(os.getcwd(), "portfolio_state.json")
        if os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.mock_portfolio_value = data.get("mock_portfolio_value", 100000.0)
                    self.usd_cash = data.get("usd_cash", 100000.0)
                    self.holdings = data.get("holdings", {})
                    self.purchase_prices = data.get("purchase_prices", {})
                    self.mock_drawdown = data.get("mock_drawdown", 0.012)
                    logger.info(f"Loaded persistent portfolio state: cash={self.usd_cash:.2f}, holdings={self.holdings}")
            except Exception as e:
                logger.error(f"Failed to load persistent portfolio state: {e}")

    def save_state(self):
        import os
        import json
        state_file = os.path.join(os.getcwd(), "portfolio_state.json")
        try:
            data = {
                "mock_portfolio_value": self.mock_portfolio_value,
                "usd_cash": self.usd_cash,
                "holdings": self.holdings,
                "purchase_prices": self.purchase_prices,
                "mock_drawdown": self.mock_drawdown
            }
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            logger.info("Saved persistent portfolio state.")
        except Exception as e:
            logger.error(f"Failed to save portfolio state: {e}")

    def record_trade(self, symbol: str, action: str, quantity: float, vwap_fill_price: float, fee_usd: float, status: str):
        if status not in ("SUCCESS", "SIMULATED", "FILLED", "PARTIALLY_FILLED"):
            return
        
        if symbol not in self.holdings:
            self.holdings[symbol] = 0.0
            
        if action == "BUY":
            total_cost = quantity * vwap_fill_price + fee_usd
            self.usd_cash -= total_cost
            
            old_qty = self.holdings.get(symbol, 0.0)
            old_price = self.purchase_prices.get(symbol, 0.0)
            new_qty = old_qty + quantity
            if new_qty > 0.0:
                self.purchase_prices[symbol] = (old_price * old_qty + vwap_fill_price * quantity) / new_qty
            else:
                self.purchase_prices[symbol] = vwap_fill_price
                
            self.holdings[symbol] += quantity
            logger.info(f"Updated portfolio: BUY {quantity:.6f} {symbol} at {vwap_fill_price:.2f}. Fee: {fee_usd:.2f}. New cash: {self.usd_cash:.2f}, holding: {self.holdings[symbol]:.6f}, avg cost: {self.purchase_prices[symbol]:.2f}")
        elif action == "SELL":
            total_revenue = quantity * vwap_fill_price - fee_usd
            self.usd_cash += total_revenue
            self.holdings[symbol] -= quantity
            if self.holdings[symbol] <= 1e-6:
                self.holdings[symbol] = 0.0
                self.purchase_prices[symbol] = 0.0
            logger.info(f"Updated portfolio: SELL {quantity:.6f} {symbol} at {vwap_fill_price:.2f}. Fee: {fee_usd:.2f}. New cash: {self.usd_cash:.2f}, holding: {self.holdings[symbol]:.6f}")
        self.save_state()

    async def get_current_portfolio_value(self) -> float:
        """
        Calculates the total portfolio value (cash + symbol holdings * latest prices).
        """
        if not self.liquidated_on_startup:
            self.liquidated_on_startup = True
            total_holdings_value = 0.0
            for symbol, qty in list(self.holdings.items()):
                if qty > 0.0:
                    try:
                        price = await market_proxy.get_ticker(symbol)
                        total_holdings_value += qty * price
                        logger.info(f"Startup liquidation: pretending to sell {qty:.6f} {symbol} at {price:.2f} USD")
                    except Exception as e:
                        price = self.purchase_prices.get(symbol, 0.0)
                        total_holdings_value += qty * price
                        logger.error(f"Startup liquidation: ticker failed for {symbol}, using purchase average price {price:.2f} USD: {e}")
            if total_holdings_value > 0.0:
                self.usd_cash += total_holdings_value
                self.holdings = {}
                self.purchase_prices = {}
                logger.info(f"Startup liquidation completed. Restored net asset value as cash: {self.usd_cash:.2f} USD.")
                self.save_state()
        total_holdings_value = 0.0
        for symbol, qty in list(self.holdings.items()):
            if qty != 0.0:
                try:
                    price = await market_proxy.get_ticker(symbol)
                    total_holdings_value += qty * price
                except Exception as e:
                    logger.error(f"Error fetching ticker price for {symbol} while valuing portfolio: {e}")
                    pass
        val = self.usd_cash + total_holdings_value
        self.mock_portfolio_value = val
        return val

    async def update_agent(self, role: str, status: str, message_en: str, message_ru: str):
        from datetime import datetime
        self.agent_states[role].update({
            "status": status,
            "message_en": message_en,
            "message_ru": message_ru,
            "last_updated": datetime.utcnow().isoformat() + "Z"
        })
        await sse_manager.broadcast("agent_state", list(self.agent_states.values()))

    async def run_consensus_loop(self):
        """
        Background consensus loop that periodically evaluates the configured symbols.
        """
        logger.info(translate("trading_started"))
        await sse_manager.broadcast("status", {"active": True, "message": translate("trading_started")})

        # Initialize/reset statuses to idle on startup
        for role in self.agent_states:
            await self.update_agent(
                role,
                "IDLE",
                self.get_initial_message_en(role),
                self.get_initial_message_ru(role)
            )

        try:
            while config.trading_active:
                # 1. Run Altcoin Screener every 10 cycles (including first cycle)
                if self.screener_cycle_counter % 10 == 0:
                    await self.update_agent(
                        "altcoin_screener",
                        "EXECUTING",
                        "EXECUTING: Screening altcoin trends and momentum...",
                        "ВЫПОЛНЕНИЕ: Анализ трендов и импульса альткоинов..."
                    )
                    try:
                        from app.agents.altcoin_screener import altcoin_screener
                        screened_alts = await altcoin_screener.screen_altcoins()
                        config.trading_config.symbols = ["BTCUSDT", "ETHUSDT"] + screened_alts
                        await self.update_agent(
                            "altcoin_screener",
                            "COMPLETED",
                            f"COMPLETED: Screener updated active symbols: {config.trading_config.symbols}",
                            f"УСПЕШНО: Обновлены активные символы: {config.trading_config.symbols}"
                        )
                    except Exception as e:
                        logger.error(f"Error in Altcoin Screener: {e}")
                        await self.update_agent(
                            "altcoin_screener",
                            "IDLE",
                            f"IDLE: Screener failed: {str(e)}",
                            f"ОЖИДАНИЕ: Ошибка скринера: {str(e)}"
                        )

                # 1.1 Run Performance Optimizer every 30 cycles (including first cycle, after screener)
                if self.screener_cycle_counter % 30 == 0:
                    await self.update_agent(
                        "performance_optimizer",
                        "EXECUTING",
                        "EXECUTING: Retrospective performance logging & parameter optimization...",
                        "ВЫПОЛНЕНИЕ: Ретроспективный анализ логов и оптимизация параметров..."
                    )
                    try:
                        await self.get_current_portfolio_value()
                        opt_res = await performance_optimizer.optimize(self.mock_portfolio_value, self.mock_drawdown)
                        
                        # Broadcast optimization history logs
                        await sse_manager.broadcast("optimization_history", performance_optimizer.optimization_history)
                        
                        prop_param = opt_res.get("proposed_param", "None")
                        prop_val = opt_res.get("proposed_value", 0.0)
                        
                        if prop_param != "None":
                            msg_en = f"COMPLETED: Self-tuned parameter '{prop_param}' to {prop_val:.4f}."
                            msg_ru = f"УСПЕШНО: Настроен параметр '{prop_param}' до {prop_val:.4f}."
                        else:
                            actions = opt_res.get("attribution_actions", [])
                            if actions:
                                msg_en = f"COMPLETED: Performance checks completed. Actions: {', '.join(actions)}"
                                msg_ru = f"УСПЕШНО: Проверка эффективности завершена. Действия: {', '.join(actions)}"
                            else:
                                msg_en = "COMPLETED: No parameter tuning needed at this time."
                                msg_ru = "УСПЕШНО: Настройка параметров на данный момент не требуется."
                        
                        await self.update_agent(
                            "performance_optimizer",
                            "COMPLETED",
                            msg_en,
                            msg_ru
                        )
                    except Exception as e:
                        logger.error(f"Error in Performance Optimizer: {e}")
                        await self.update_agent(
                            "performance_optimizer",
                            "IDLE",
                            f"IDLE: Optimizer failed: {str(e)}",
                            f"ОЖИДАНИЕ: Ошибка оптимизатора: {str(e)}"
                        )
                
                self.screener_cycle_counter += 1

                for symbol in config.trading_config.symbols:
                    try:
                        # 1. Fetch current price
                        current_price = await market_proxy.get_ticker(symbol)
                        
                        # Seed Java engine's order book so there is fresh liquidity around the current price
                        try:
                            import httpx
                            # Estimate ATR as 2% of price for synthetic depth seeding
                            atr = current_price * 0.02
                            url = f"{settings.java_engine_url}/api/v1/orderbook/{symbol}/seed?midPrice={current_price}&atr={atr}"
                            async with httpx.AsyncClient(timeout=2.0) as client:
                                await client.post(url)
                        except Exception as e:
                            logger.warning(f"Failed to seed order book on Java engine: {e}")

                        await self.get_current_portfolio_value()
                        current_prices = await self.get_current_prices()
                        await sse_manager.broadcast("portfolio", {
                            "total_value": self.mock_portfolio_value,
                            "drawdown": self.mock_drawdown,
                            "holdings": self.holdings,
                            "purchase_prices": self.purchase_prices,
                            "usd_cash": self.usd_cash,
                            "current_prices": current_prices
                        })
                        await sse_manager.broadcast("agent_state_legacy", {
                            "symbol": symbol,
                            "price": current_price,
                            "state": "ANALYZING"
                        })

                        # Stop Loss check
                        current_holding = self.holdings.get(symbol, 0.0)
                        average_cost = self.purchase_prices.get(symbol, 0.0)
                        is_stop_loss = False
                        
                        if current_holding > 0.0 and average_cost > 0.0:
                            stop_loss_pct = config.trading_config.stop_loss_limit
                            threshold_price = average_cost * (1.0 - stop_loss_pct)
                            if current_price < threshold_price:
                                is_stop_loss = True
                                consensus_action = "SELL"
                                consensus_confidence = 1.0
                                msg = translate(
                                    "stop_loss_triggered",
                                    symbol=symbol,
                                    price=current_price,
                                    cost=average_cost,
                                    limit_pct=stop_loss_pct * 100
                                )
                                logger.warning(msg)
                                await sse_manager.broadcast("consensus", {
                                    "symbol": symbol,
                                    "action": "SELL",
                                    "score": -1.0,
                                    "confidence": 1.0,
                                    "reason": "STOP_LOSS"
                                })

                        if not is_stop_loss:
                            # Retrieve recent performance journal context for prompt injection
                            try:
                                performance_history = journal_manager.get_recent_performance(symbol)
                            except Exception as e:
                                logger.error(f"Error fetching performance history for {symbol}: {e}")
                                performance_history = ""

                            # Mark analyst agents as executing
                            await self.update_agent(
                                "technical_analyst",
                                "EXECUTING",
                                f"EXECUTING: Mapping moving averages, RSI divergence, and MACD for {symbol}...",
                                f"ВЫПОЛНЕНИЕ: Технический анализ (EMA, RSI, MACD) для {symbol}..."
                            )
                            await self.update_agent(
                                "sentiment_analyst",
                                "EXECUTING",
                                f"EXECUTING: Analyzing market sentiment and news feed for {symbol}...",
                                f"ВЫПОЛНЕНИЕ: Анализ настроений рынка и новостной ленты для {symbol}..."
                            )

                            # 2. Run Analysts in parallel
                            tech_task = asyncio.create_task(technical_analyst.analyze(symbol, current_price, performance_history))
                            sent_task = asyncio.create_task(sentiment_analyst.analyze(symbol, performance_history))
                            
                            tech_res, sent_res = await asyncio.gather(tech_task, sent_task)
                            
                            # Broadcast analysis results
                            await sse_manager.broadcast("analysis", {
                                "symbol": symbol,
                                "technical": {
                                    "signal": tech_res["signal"],
                                    "confidence": tech_res["confidence"],
                                    "inference_time_ms": tech_res["inference_time_ms"],
                                    "indicators": tech_res["indicators"]
                                },
                                "sentiment": {
                                    "signal": sent_res["signal"],
                                    "confidence": sent_res["confidence"],
                                    "inference_time_ms": sent_res["inference_time_ms"],
                                    "sopr": sent_res["sopr"]
                                }
                            })

                            # Update analyst statuses to completed
                            tech_sig = tech_res["signal"]
                            tech_conf = tech_res["confidence"]
                            sent_sig = sent_res["signal"]
                            sent_conf = sent_res["confidence"]

                            await self.update_agent(
                                "technical_analyst",
                                "COMPLETED",
                                f"COMPLETED: Signal {tech_sig} calculated (confidence: {tech_conf:.2f}).",
                                f"УСПЕШНО: Расчитан сигнал {tech_sig} с уверенностью {tech_conf:.2f}."
                            )
                            await self.update_agent(
                                "sentiment_analyst",
                                "COMPLETED",
                                f"COMPLETED: Sentiment signal {sent_sig} (confidence: {sent_conf:.2f}).",
                                f"УСПЕШНО: Сигнал настроений {sent_sig} с уверенностью {sent_conf:.2f}."
                            )

                            # 3. Consensus Logic
                            sig_weights = {"BUY": 1, "SELL": -1, "HOLD": 0}
                            w_tech = sig_weights.get(tech_sig, 0) * tech_conf
                            w_sent = sig_weights.get(sent_sig, 0) * sent_conf
                            score = w_tech + w_sent

                            consensus_action = "HOLD"
                            consensus_confidence = 0.5

                            if score >= 0.4:
                                consensus_action = "BUY"
                                consensus_confidence = min(1.0, score)
                            elif score <= -0.4:
                                consensus_action = "SELL"
                                consensus_confidence = min(1.0, abs(score))

                            # Log and broadcast consensus
                            msg = translate("consensus_action", action=consensus_action, symbol=symbol) if consensus_action != "HOLD" else translate("consensus_no_action")
                            logger.info(msg)
                            await sse_manager.broadcast("consensus", {
                                "symbol": symbol,
                                "action": consensus_action,
                                "score": score,
                                "confidence": consensus_confidence
                            })

                        # 4. If action is BUY or SELL, perform risk audit & trade execution
                        if consensus_action in ("BUY", "SELL"):
                            import random
                            estimated_slippage = 0.0004 + (random.random() * 0.0002)

                            # Calculate volatility for dynamic risk assessment
                            try:
                                vol_prices = await technical_analyst.fetch_historical_prices(symbol, interval="15m", limit=96)
                                if len(vol_prices) >= 2:
                                    returns = [(vol_prices[i] - vol_prices[i-1]) / vol_prices[i-1] for i in range(1, len(vol_prices))]
                                    mean_ret = sum(returns) / len(returns)
                                    volatility = (sum((r - mean_ret) ** 2 for r in returns) / len(returns)) ** 0.5
                                else:
                                    volatility = None
                            except Exception as e:
                                logger.warning(f"Failed to calculate volatility: {e}")
                                volatility = None

                            # Sizing
                            if is_stop_loss:
                                proposed_qty = current_holding
                            else:
                                proposed_usd_size = self.mock_portfolio_value * config.trading_config.portfolio_cap * consensus_confidence
                                proposed_qty = proposed_usd_size / current_price
                                if consensus_action == "SELL":
                                    if current_holding <= 0.0:
                                        logger.info(f"Consensus proposed SELL on {symbol}, but holdings are zero. Skipping execution.")
                                        await self.update_agent(
                                            "risk_auditor",
                                            "IDLE",
                                            "IDLE: Sell proposed but holdings are zero. Standby.",
                                            "ОЖИДАНИЕ: Предложена продажа, но баланс равен нулю."
                                        )
                                        await self.update_agent(
                                            "trader",
                                            "IDLE",
                                            "IDLE: Sell proposed but holdings are zero. Standby.",
                                            "ОЖИДАНИЕ: Предложена продажа, но баланс равен нулю."
                                        )
                                        await asyncio.sleep(1.5)
                                        continue
                                    else:
                                        proposed_qty = min(proposed_qty, current_holding)

                            # Set risk auditor and trader to executing
                            if is_stop_loss:
                                await self.update_agent(
                                    "risk_auditor",
                                    "EXECUTING",
                                    f"EXECUTING: Auditing forced STOP LOSS liquidation for {symbol}...",
                                    f"ВЫПОЛНЕНИЕ: Экспертиза принудительной ликвидации стоп-лосса для {symbol}..."
                                )
                            else:
                                await self.update_agent(
                                    "risk_auditor",
                                    "EXECUTING",
                                    f"EXECUTING: Auditing proposed {consensus_action} trade for {symbol}...",
                                    f"ВЫПОЛнЕНИЕ: Экспертиза лимитов и рисков сделки {consensus_action} для {symbol}..."
                                )

                            await self.update_agent(
                                "trader",
                                "EXECUTING",
                                f"EXECUTING: Sizing and routing order blocks for {symbol}...",
                                f"ВЫПОЛНЕНИЕ: Спецификация блоков ордеров для {symbol}..."
                            )

                            # Audit
                            if is_stop_loss:
                                approved = True
                                reason = translate("stop_loss_liquidation", details=f"Liquidating {proposed_qty:.6f} {symbol} @ {current_price:.2f}")
                                logger.info(translate("trade_approved", details=reason))
                            else:
                                approved, reason = risk_auditor.audit_trade(
                                    symbol=symbol,
                                    action=consensus_action,
                                    quantity=proposed_qty,
                                    price=current_price,
                                    portfolio_value=self.mock_portfolio_value,
                                    current_drawdown=self.mock_drawdown,
                                    estimated_slippage=estimated_slippage,
                                    current_holding=self.holdings.get(symbol, 0.0),
                                    strategy=trading_config.strategy,
                                    usd_cash=self.usd_cash,
                                    volatility=volatility
                                )

                            await sse_manager.broadcast("risk_audit", {
                                "symbol": symbol,
                                "approved": approved,
                                "reason": reason
                            })

                            if approved:
                                # Risk Auditor approved
                                if is_stop_loss:
                                    await self.update_agent(
                                        "risk_auditor",
                                        "COMPLETED",
                                        f"COMPLETED: Stop Loss liquidation approved.",
                                        f"УСПЕШНО: Ликвидация по стоп-лоссу одобрена."
                                    )
                                else:
                                    await self.update_agent(
                                        "risk_auditor",
                                        "COMPLETED",
                                        "COMPLETED: Trade approved. Compliance metrics within limits.",
                                        "УСПЕШНО: Сделка одобрена. Пределы рыночных рисков не нарушены."
                                    )

                                # Execute
                                if is_stop_loss:
                                    exec_log = await trader.execute_trade(
                                        symbol=symbol,
                                        action="SELL",
                                        current_price=current_price,
                                        confidence=1.0,
                                        inference_delay_ms=50,
                                        quantity=proposed_qty
                                    )
                                    exec_log["notes"] = f"Stop Loss exit triggered. Average cost was: {average_cost:.2f}"
                                else:
                                    total_inference_ms = tech_res["inference_time_ms"] + sent_res["inference_time_ms"]
                                    exec_log = await trader.execute_trade(
                                        symbol=symbol,
                                        action=consensus_action,
                                        current_price=current_price,
                                        confidence=consensus_confidence,
                                        inference_delay_ms=total_inference_ms,
                                        quantity=proposed_qty
                                    )

                                # Compile reasoning string from both analysts or stop loss event
                                if is_stop_loss:
                                    sl_pct = config.trading_config.stop_loss_limit * 100
                                    combined_reasoning = f"Forced Stop Loss liquidation. Average purchase price was: {average_cost:.2f} USD. Current price of {current_price:.2f} USD breached the {sl_pct:.1f}% limit."
                                else:
                                    combined_reasoning = (
                                        f"Technical Analyst: {tech_res.get('analysis', 'N/A')}\n\n"
                                        f"Sentiment Analyst: {sent_res.get('analysis', 'N/A')}"
                                    )
                                
                                # Add reasoning to the exec_log so it's broadcasted to SSE and saved in memory execution logs
                                exec_log["reasoning"] = combined_reasoning

                                # Record to trade journal database file
                                try:
                                    journal_manager.record_journal_entry(
                                        symbol=symbol,
                                        action=consensus_action if not is_stop_loss else "SELL",
                                        price=exec_log["vwap_fill_price"],
                                        quantity=exec_log["quantity"],
                                        status=exec_log["status"],
                                        fee_usd=exec_log["fee_usd"],
                                        slippage_pct=exec_log["slippage_pct"],
                                        reasoning=combined_reasoning,
                                        entry_id=exec_log["id"]
                                    )
                                except Exception as e:
                                    logger.error(f"Failed to record journal entry: {e}")

                                # Record the trade to update holdings/cash
                                self.record_trade(
                                    symbol=symbol,
                                    action=consensus_action,
                                    quantity=exec_log["quantity"],
                                    vwap_fill_price=exec_log["vwap_fill_price"],
                                    fee_usd=exec_log["fee_usd"],
                                    status=exec_log["status"]
                                )

                                # Recalculate portfolio value
                                await self.get_current_portfolio_value()

                                # Update mock drawdown
                                if consensus_action == "BUY":
                                    self.mock_drawdown = max(0.0, self.mock_drawdown - 0.001)
                                else:
                                    self.mock_drawdown = min(0.044, self.mock_drawdown - 0.002)

                                await sse_manager.broadcast("execution", exec_log)
                                current_prices = await self.get_current_prices()
                                await sse_manager.broadcast("portfolio", {
                                    "total_value": self.mock_portfolio_value,
                                    "drawdown": self.mock_drawdown,
                                    "holdings": self.holdings,
                                    "purchase_prices": self.purchase_prices,
                                    "usd_cash": self.usd_cash,
                                    "current_prices": current_prices
                                })

                                # Trader completed
                                await self.update_agent(
                                    "trader",
                                    "COMPLETED",
                                    f"COMPLETED: Order executed successfully via {exec_log.get('execution_path', 'DIRECT')}.",
                                    f"УСПЕШНО: Ордер успешно выполнен через {exec_log.get('execution_path', 'DIRECT')}."
                                )
                            else:
                                # Risk Auditor vetoed
                                await self.update_agent(
                                    "risk_auditor",
                                    "VETOED",
                                    f"VETOED: Trade rejected: {reason}",
                                    f"ОТКЛОНЕНО: Сделка отклонена: {reason}"
                                )
                                await self.update_agent(
                                    "trader",
                                    "VETOED",
                                    "VETOED: Execution halted by Risk Auditor veto.",
                                    "ОТМЕНЕНО: Выполнение заблокировано по вето Риск-аудитора."
                                )
                        else:
                            # Consensus was HOLD
                            await self.update_agent(
                                "risk_auditor",
                                "IDLE",
                                "IDLE: No action proposed by consensus. Compliance monitors in standby.",
                                "ОЖИДАНИЕ: Действий по консенсусу не предложено. Мониторы комплаенса в режиме ожидания."
                            )
                            await self.update_agent(
                                "trader",
                                "IDLE",
                                "IDLE: No action proposed by consensus. Execution pipeline in standby.",
                                "ОЖИДАНИЕ: Действий по консенсусу не предложено. Конвейер выполнения в режиме ожидания."
                            )

                        # Pause briefly to let user visually appreciate the completed/vetoed state
                        await asyncio.sleep(1.5)

                        # Set all agent states back to IDLE at the end of the symbol evaluation
                        for role in self.agent_states:
                            await self.update_agent(
                                role,
                                "IDLE",
                                self.get_initial_message_en(role),
                                self.get_initial_message_ru(role)
                            )

                    except Exception as e:
                        logger.error(f"Error in consensus processing for {symbol}: {e}")
                        await sse_manager.broadcast("error", {"symbol": symbol, "message": str(e)})

                # Sleep until next evaluation interval
                await asyncio.sleep(config.trading_config.interval_seconds)

        except asyncio.CancelledError:
            logger.info("Consensus loop was cancelled")
        finally:
            config.trading_active = False
            logger.info(translate("trading_stopped"))
            await sse_manager.broadcast("status", {"active": False, "message": translate("trading_stopped")})

    async def get_current_prices(self) -> Dict[str, float]:
        """
        Retrieves current ticker prices for all active configured symbols.
        """
        prices = {}
        for symbol in config.trading_config.symbols:
            try:
                prices[symbol] = await market_proxy.get_ticker(symbol)
            except Exception:
                pass
        return prices

    async def update_active_symbols(self, symbols: List[str]):
        """
        Updates the watchlist of active symbols and broadcasts the new portfolio state.
        """
        cleaned_symbols = []
        for s in symbols:
            s_clean = s.strip().upper()
            if not s_clean.endswith("USDT"):
                s_clean = f"{s_clean}USDT"
            cleaned_symbols.append(s_clean)
        
        config.trading_config.symbols = cleaned_symbols
        logger.info(f"Symbols updated via chat command: {cleaned_symbols}")
        
        await self.get_current_portfolio_value()
        current_prices = await self.get_current_prices()
        await sse_manager.broadcast("portfolio", {
            "total_value": self.mock_portfolio_value,
            "drawdown": self.mock_drawdown,
            "holdings": self.holdings,
            "purchase_prices": self.purchase_prices,
            "usd_cash": self.usd_cash,
            "current_prices": current_prices,
            "symbols": cleaned_symbols
        })

    async def update_stop_loss(self, limit: float):
        """
        Updates the asset stop-loss limit percentage.
        """
        config.trading_config.stop_loss_limit = limit
        logger.info(f"Stop loss limit updated via chat command: {limit}")
        
        await self.get_current_portfolio_value()
        current_prices = await self.get_current_prices()
        await sse_manager.broadcast("portfolio", {
            "total_value": self.mock_portfolio_value,
            "drawdown": self.mock_drawdown,
            "holdings": self.holdings,
            "purchase_prices": self.purchase_prices,
            "usd_cash": self.usd_cash,
            "current_prices": current_prices,
            "stop_loss_limit": limit
        })

    async def reset_portfolio(self, balance: float):
        """
        Resets portfolio holdings, cash, drawdown, logs and agent states.
        """
        self.mock_portfolio_value = balance
        self.usd_cash = balance
        self.holdings = {}
        self.purchase_prices = {}
        self.mock_drawdown = 0.0
        
        from app.agents.trader import execution_logs
        execution_logs.clear()
        
        # Reset agent states to IDLE
        for role in self.agent_states:
            await self.update_agent(
                role,
                "IDLE",
                self.get_initial_message_en(role),
                self.get_initial_message_ru(role)
            )
            
        await sse_manager.broadcast("portfolio", {
            "total_value": self.mock_portfolio_value,
            "drawdown": self.mock_drawdown,
            "holdings": self.holdings,
            "purchase_prices": self.purchase_prices,
            "usd_cash": self.usd_cash
        })
        self.save_state()

    async def liquidate_asset(self, symbol: str) -> str:
        """
        Immediately liquidates all holdings for a given symbol.
        """
        symbol_clean = symbol.strip().upper()
        if not symbol_clean.endswith("USDT"):
            symbol_clean = f"{symbol_clean}USDT"
            
        qty = self.holdings.get(symbol_clean, 0.0)
        if qty <= 1e-6:
            logger.info(f"No holdings to liquidate for {symbol_clean}")
            return f"No holdings to liquidate for {symbol_clean}."
            
        try:
            current_price = await market_proxy.get_ticker(symbol_clean)
        except Exception as e:
            current_price = self.purchase_prices.get(symbol_clean, 100.0)
            if current_price <= 0.0:
                current_price = 100.0
                
        # Execute trade
        from app.agents.trader import trader
        exec_log = await trader.execute_trade(
            symbol=symbol_clean,
            action="SELL",
            current_price=current_price,
            confidence=1.0,
            inference_delay_ms=50,
            quantity=qty
        )
        
        exec_log["notes"] = f"Manual chat liquidation triggered."
        combined_reasoning = f"Manual liquidation of {symbol_clean} triggered by supervisor command."
        exec_log["reasoning"] = combined_reasoning
        
        # Record to trade journal
        try:
            from app.services.journal_manager import journal_manager
            journal_manager.record_journal_entry(
                symbol=symbol_clean,
                action="SELL",
                price=exec_log["vwap_fill_price"],
                quantity=exec_log["quantity"],
                status=exec_log["status"],
                fee_usd=exec_log["fee_usd"],
                slippage_pct=exec_log["slippage_pct"],
                reasoning=combined_reasoning,
                entry_id=exec_log["id"]
            )
        except Exception as e:
            logger.error(f"Failed to record journal entry: {e}")
            
        # Record trade to update holdings/cash
        self.record_trade(
            symbol=symbol_clean,
            action="SELL",
            quantity=exec_log["quantity"],
            vwap_fill_price=exec_log["vwap_fill_price"],
            fee_usd=exec_log["fee_usd"],
            status=exec_log["status"]
        )
        
        # Recalculate portfolio value
        await self.get_current_portfolio_value()
        
        # Broadcast updates
        await sse_manager.broadcast("execution", exec_log)
        current_prices = await self.get_current_prices()
        await sse_manager.broadcast("portfolio", {
            "total_value": self.mock_portfolio_value,
            "drawdown": self.mock_drawdown,
            "holdings": self.holdings,
            "purchase_prices": self.purchase_prices,
            "usd_cash": self.usd_cash,
            "current_prices": current_prices
        })
        
        return f"Successfully liquidated {qty:.6f} {symbol_clean} at {exec_log['vwap_fill_price']:.2f} USD."

    async def update_portfolio_cap(self, cap: float):
        """
        Updates the portfolio cap per trade.
        """
        config.trading_config.portfolio_cap = cap
        logger.info(f"Portfolio cap updated via chat command: {cap}")
        await self.get_current_portfolio_value()
        current_prices = await self.get_current_prices()
        await sse_manager.broadcast("portfolio", {
            "total_value": self.mock_portfolio_value,
            "drawdown": self.mock_drawdown,
            "holdings": self.holdings,
            "purchase_prices": self.purchase_prices,
            "usd_cash": self.usd_cash,
            "current_prices": current_prices,
            "portfolio_cap": cap
        })

    async def update_drawdown_limit(self, limit: float):
        """
        Updates the drawdown limit.
        """
        config.trading_config.drawdown_limit = limit
        logger.info(f"Drawdown limit updated via chat command: {limit}")
        await self.get_current_portfolio_value()
        current_prices = await self.get_current_prices()
        await sse_manager.broadcast("portfolio", {
            "total_value": self.mock_portfolio_value,
            "drawdown": self.mock_drawdown,
            "holdings": self.holdings,
            "purchase_prices": self.purchase_prices,
            "usd_cash": self.usd_cash,
            "current_prices": current_prices,
            "drawdown_limit": limit
        })

    async def update_slippage_limit(self, limit: float):
        """
        Updates the slippage limit.
        """
        config.trading_config.slippage_limit = limit
        logger.info(f"Slippage limit updated via chat command: {limit}")
        await self.get_current_portfolio_value()
        current_prices = await self.get_current_prices()
        await sse_manager.broadcast("portfolio", {
            "total_value": self.mock_portfolio_value,
            "drawdown": self.mock_drawdown,
            "holdings": self.holdings,
            "purchase_prices": self.purchase_prices,
            "usd_cash": self.usd_cash,
            "current_prices": current_prices,
            "slippage_limit": limit
        })

    async def update_interval(self, seconds: int):
        """
        Updates the consensus evaluation interval in seconds.
        """
        config.trading_config.interval_seconds = seconds
        logger.info(f"Consensus evaluation interval updated via chat command: {seconds}")
        await self.get_current_portfolio_value()
        current_prices = await self.get_current_prices()
        await sse_manager.broadcast("portfolio", {
            "total_value": self.mock_portfolio_value,
            "drawdown": self.mock_drawdown,
            "holdings": self.holdings,
            "purchase_prices": self.purchase_prices,
            "usd_cash": self.usd_cash,
            "current_prices": current_prices,
            "interval_seconds": seconds
        })

    async def set_trading_state(self, active: bool):
        """
        Starts or stops the trading consensus loop.
        """
        logger.info(f"Trading state toggled via chat command: {active}")
        if active and not config.trading_active:
            self.start()
        elif not active and config.trading_active:
            self.stop()

    def start(self):
        """
        Starts the background consensus loop.
        """
        global _consensus_task
        if config.trading_active:
            raise RuntimeError(translate("trading_already_running"))
        
        config.trading_active = True
        _consensus_task = asyncio.create_task(self.run_consensus_loop())

    def stop(self):
        """
        Stops the background consensus loop.
        """
        global _consensus_task
        if not config.trading_active:
            raise RuntimeError(translate("trading_not_running"))
            
        config.trading_active = False
        if _consensus_task:
            _consensus_task.cancel()
            _consensus_task = None

portfolio_manager = PortfolioManagerAgent()
