import os
import json
import logging
import time
import re
from typing import Dict, Any, List, Tuple, Optional
from app.config import settings, trading_config, clamp_config_parameters, translate
from app.llm_connector import generate_chat_completion, LLMConfigurationError

logger = logging.getLogger(__name__)

class PerformanceOptimizerAgent:
    def __init__(self):
        self.history_file = os.path.join(os.getcwd(), "optimization_history.json")
        self.journal_file = os.path.join(os.getcwd(), "trade_journal.json")
        self.optimization_history: List[Dict[str, Any]] = []
        self.load_optimization_history()

    def load_optimization_history(self):
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, "r", encoding="utf-8") as f:
                    self.optimization_history = json.load(f)
                logger.info(f"Loaded {len(self.optimization_history)} optimization log entries.")
            except Exception as e:
                logger.error(f"Failed to load optimization history: {e}")
        else:
            self.optimization_history = []

    def save_optimization_history(self):
        try:
            with open(self.history_file, "w", encoding="utf-8") as f:
                json.dump(self.optimization_history, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save optimization history: {e}")

    def get_recent_journal_entries(self, limit: int = 50) -> List[Dict[str, Any]]:
        if os.path.exists(self.journal_file):
            try:
                with open(self.journal_file, "r", encoding="utf-8") as f:
                    entries = json.load(f)
                    return entries[-limit:]
            except Exception as e:
                logger.error(f"Failed to read trade journal: {e}")
        return []

    def calculate_win_rate(self, entries: List[Dict[str, Any]]) -> float:
        if not entries:
            return 0.0
        # Win is defined as: for SELL orders, price > average purchase cost
        # Since the journal records action, price, and reasoning, we can approximate
        # by checking if a SELL order resulted in a profit compared to local portfolio manager records,
        # or simplify: count successful fills vs failures, or calculate profit/loss.
        # Let's check for entries where action is SELL and price is higher than average cost if we can,
        # or simply count successful orders.
        # Alternatively, a win is defined as any sell that did not trigger a stop-loss or was a profit.
        # Let's calculate win rate based on: count of 'status' == 'SUCCESS' or 'FILLED' divided by total attempts.
        # Or better: count the entries that are SELLs. If the return of the trade is positive.
        # Let's implement a robust metric: profit trades / total closed trades.
        # Since we might not have purchase cost easily mapped in journal entries, let's look at the reasoning
        # or price differences, or simple win/loss.
        # Let's count SELL entries where the notes/reasoning does not indicate "Stop Loss triggered" as wins,
        # or check if the sale price is greater than purchase price.
        # For simplicity in this mock/simulated sandbox, let's calculate:
        # wins = count of SELL trades where status is SUCCESS/FILLED/SIMULATED and is not a stop loss.
        sells = [e for e in entries if e.get("action") == "SELL"]
        if not sells:
            return 0.5 # Baseline neutral
        wins = sum(1 for e in sells if "Stop Loss" not in e.get("reasoning", "") and e.get("status") in ("SUCCESS", "FILLED", "SIMULATED"))
        return wins / len(sells)

    async def run_attribution_check(self, current_portfolio_value: float) -> List[str]:
        """
        Scans for active parameters under test, checks if enough trades have elapsed,
        and attributes performance to score/rollback changes.
        Returns list of actions taken (e.g. rollbacks).
        """
        actions_taken = []
        active_entries = [e for e in self.optimization_history if e.get("status") == "ACTIVE"]
        if not active_entries:
            return actions_taken

        journal_entries = self.get_recent_journal_entries(50)
        
        for entry in active_entries:
            change_time = entry.get("timestamp", 0)
            # Find journal entries that occurred AFTER this change
            post_change_trades = [j for j in journal_entries if j.get("timestamp", 0) > change_time]
            
            # We require at least 5 post-change trades to evaluate (or 3 for faster loop in simulation)
            if len(post_change_trades) >= 3:
                # Calculate post-change win rate
                post_win_rate = self.calculate_win_rate(post_change_trades)
                baseline_win_rate = entry.get("baseline_win_rate", 0.5)
                
                # Calculate portfolio value change
                baseline_val = entry.get("baseline_portfolio_value", 100000.0)
                val_change_pct = (current_portfolio_value - baseline_val) / baseline_val if baseline_val > 0 else 0.0
                
                # Attribution scoring logic:
                # Positive if win rate improved or portfolio value went up.
                # Negative if win rate dropped or portfolio value went down.
                score = (post_win_rate - baseline_win_rate) * 2.0 + val_change_pct * 10.0
                
                entry["attribution_checked"] = True
                entry["attribution_score"] = float(f"{score:.4f}")
                
                if score < -0.05:  # Significant drop in performance -> ROLLBACK
                    entry["status"] = "REVERTED"
                    param = entry.get("param_name")
                    old_val = entry.get("old_value")
                    
                    # Apply rollback to trading_config
                    if hasattr(trading_config, param):
                        setattr(trading_config, param, old_val)
                        clamp_config_parameters()
                        msg = f"ROLLBACK: Reverted '{param}' to {old_val} due to negative attribution score ({score:.4f})."
                        logger.warning(msg)
                        actions_taken.append(msg)
                else:
                    entry["status"] = "COMPLETED"
                    msg = f"SUCCESS: Parameter '{entry.get('param_name')}' tuning validated with score {score:.4f}."
                    logger.info(msg)
                    actions_taken.append(msg)
                    
        self.save_optimization_history()
        return actions_taken

    async def optimize(self, current_portfolio_value: float, current_drawdown: float) -> Dict[str, Any]:
        """
        Runs the full optimization pass:
        1. Checks attribution of previous changes.
        2. Evaluates recent journal trade history.
        3. Invokes LLM to analyze and suggest parameter updates.
        4. Clamps and applies changes.
        """
        # Run attribution step first
        attribution_msgs = await self.run_attribution_check(current_portfolio_value)

        # Retrieve recent logs
        recent_trades = self.get_recent_journal_entries(30)
        win_rate = self.calculate_win_rate(recent_trades)
        
        # Compile trade summaries for prompt
        trade_summaries = []
        total_slippage = 0.0
        for t in recent_trades:
            total_slippage += t.get("slippage_pct", 0.0)
            trade_summaries.append(
                f"- Trade {t.get('id', 'N/A')}: {t.get('action')} {t.get('quantity')} {t.get('symbol')} "
                f"price={t.get('price')} status={t.get('status')} slippage={t.get('slippage_pct'):.4f}%"
            )
        avg_slippage = total_slippage / len(recent_trades) if recent_trades else 0.05
        trade_logs_str = "\n".join(trade_summaries) if trade_summaries else "No trades recorded in current session."

        prompt = f"""
You are the Performance Optimizer Agent in a multi-agent crypto trading system.
Your job is to analyze trade execution statistics and adjust system risk/routing parameters to maximize profits and minimize drawdown.

Current Parameters:
- Portfolio Cap Per Trade: {trading_config.portfolio_cap:.4f} (Max fraction of total portfolio value allowed per trade)
- Drawdown Limit: {trading_config.drawdown_limit:.4f} (Max drawdown before halting trading)
- Slippage Limit: {trading_config.slippage_limit:.4f} (Max slippage before vetoing orders)
- Stop Loss Limit: {trading_config.stop_loss_limit:.4f} (Loss percentage threshold before forced liquidation)

Current Performance Metrics:
- Portfolio Total Value: {current_portfolio_value:.2f} USD
- Current Drawdown: {current_drawdown * 100:.2f}%
- Win Rate (non-stop-loss sells / total sells): {win_rate * 100:.1f}%
- Average Slippage: {avg_slippage:.4f}%

Recent Trade Execution Logs:
{trade_logs_str}

Evaluate if the system parameters need adjustments:
- If we are hitting stop losses frequently on minor noise, maybe `stop_loss_limit` is too tight and should be slightly increased.
- If average slippage is close to or exceeding the `slippage_limit`, maybe the `slippage_limit` needs to be increased or path routing needs to adapt.
- If win rate is low or drawdown is high, maybe the `portfolio_cap` (order sizing) should be decreased to manage risk.

Think step-by-step about this performance data. Formulate a hypothesis and explanation for your recommended changes.
You can propose a change to exactly ONE parameter at a time to isolate attribution.
Your response MUST conclude with exactly this format:
PROPOSED_PARAM: [portfolio_cap/drawdown_limit/slippage_limit/stop_loss_limit/None]
PROPOSED_VALUE: [new numerical value, or Current Value if None]
REASONING: [1-sentence explanation of your hypothesis]
"""

        messages = [
            {"role": "system", "content": "You are a quantitative systems optimizer. Follow formatting rules precisely and think step-by-step."},
            {"role": "user", "content": prompt}
        ]

        analysis_text = ""
        param_name = "None"
        new_value = 0.0
        reasoning = "No optimization needed."
        inference_time = 0.0

        # Check if there is already an active parameter under test to prevent stacking changes
        active_entries = [e for e in self.optimization_history if e.get("status") == "ACTIVE"]
        
        if active_entries:
            analysis_text = f"Optimization pass skipped: Currently testing parameter '{active_entries[0].get('param_name')}'."
            logger.info(analysis_text)
        else:
            try:
                analysis_text, inference_time = await generate_chat_completion(messages, timeout=6.0)
                
                # Parse proposal
                param_match = re.search(r"PROPOSED_PARAM:\s*([a-zA-Z_]+)", analysis_text)
                val_match = re.search(r"PROPOSED_VALUE:\s*([0-9.]+)", analysis_text)
                reason_match = re.search(r"REASONING:\s*(.*)", analysis_text)
                
                param_name = param_match.group(1).strip() if param_match else "None"
                new_value = float(val_match.group(1).strip()) if val_match else 0.0
                reasoning = reason_match.group(1).strip() if reason_match else "No change proposed."

                # Verify parameter exists in config and has changed
                if param_name != "None" and hasattr(trading_config, param_name):
                    old_value = getattr(trading_config, param_name)
                    if abs(new_value - old_value) > 1e-6:
                        # Log and apply parameter change
                        logger.info(f"OPTIMIZATION PROPOSED: Update '{param_name}' from {old_value} to {new_value}.")
                        
                        # Apply change
                        setattr(trading_config, param_name, new_value)
                        # Clamp parameter within strict safety bounds
                        clamp_config_parameters()
                        clamped_value = getattr(trading_config, param_name)
                        
                        # Record change in history
                        entry_id = f"OPT-{int(time.time())}"
                        entry = {
                            "id": entry_id,
                            "timestamp": time.time(),
                            "param_name": param_name,
                            "old_value": old_value,
                            "new_value": clamped_value,
                            "reasoning": reasoning,
                            "baseline_portfolio_value": current_portfolio_value,
                            "baseline_win_rate": win_rate,
                            "attribution_checked": False,
                            "attribution_score": 0.0,
                            "status": "ACTIVE"
                        }
                        self.optimization_history.append(entry)
                        self.save_optimization_history()
                        
                        msg = f"Applied optimization: Updated '{param_name}' to {clamped_value} (safety clamped). Reasoning: {reasoning}"
                        logger.info(msg)
                        attribution_msgs.append(msg)
                    else:
                        param_name = "None"
                        reasoning = "Proposed value matches current value."
                else:
                    param_name = "None"
            except (LLMConfigurationError, Exception) as e:
                logger.warning(f"LLM optimization run failed or was skipped: {e}")
                analysis_text = f"Optimization run failed: {e}"

        result = {
            "agent": "Performance Optimizer",
            "analysis": analysis_text,
            "proposed_param": param_name,
            "proposed_value": new_value,
            "reasoning": reasoning,
            "attribution_actions": attribution_msgs,
            "inference_time_ms": int(inference_time * 1000)
        }
        
        return result

performance_optimizer = PerformanceOptimizerAgent()
