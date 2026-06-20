import os
import json
import logging
import time
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

# Default file location in workspace
JOURNAL_FILE = os.getenv("JOURNAL_FILE", os.path.join(os.getcwd(), "trade_journal.json"))

class JournalManager:
    def __init__(self, filepath: str = JOURNAL_FILE):
        self.filepath = filepath

    def _load_journal(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.filepath):
            return []
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception as e:
            logger.error(f"Failed to load trade journal from {self.filepath}: {e}")
        return []

    def _save_journal(self, data: List[Dict[str, Any]]):
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save trade journal to {self.filepath}: {e}")

    def get_recent_performance(self, symbol: str, limit: int = 3) -> str:
        """
        Loads the journal and formats the last `limit` trades for the given symbol.
        Returns a formatted string to be injected into the LLM prompt.
        """
        journal = self._load_journal()
        # Filter for the symbol
        symbol_trades = [t for t in journal if t.get("symbol") == symbol]
        
        # Sort by timestamp (should be sorted already, but let's be sure)
        symbol_trades.sort(key=lambda x: x.get("timestamp", 0))
        
        # Take the most recent ones
        recent_trades = symbol_trades[-limit:]
        
        if not recent_trades:
            return f"No recent trade history available for {symbol}."

        lines = [f"Recent trade history and outcomes for {symbol}:"]
        for idx, trade in enumerate(recent_trades, 1):
            ts = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(trade.get("timestamp", time.time())))
            action = trade.get("action")
            price = trade.get("price")
            qty = trade.get("quantity", 0.0)
            status = trade.get("status")
            fee = trade.get("fee_usd", 0.0)
            slippage = trade.get("slippage_pct", 0.0)
            reasoning = trade.get("reasoning", "No details").replace('\n', ' ')
            
            lines.append(
                f"- [{ts}] {action} {qty:.6f} @ {price:.2f} USD. Outcome: {status} (Fee: ${fee:.2f}, Slippage: {slippage:.4f}%). "
                f"Previous AI decision context: {reasoning}"
            )
        return "\n".join(lines)

    def record_journal_entry(
        self,
        symbol: str,
        action: str,
        price: float,
        quantity: float,
        status: str,
        fee_usd: float,
        slippage_pct: float,
        reasoning: str,
        entry_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Saves a new trade decision and reasoning text to the trade journal log file.
        """
        final_id = entry_id if entry_id else f"JOURNAL-{int(time.time() * 1000) % 9000 + 1000}"
        entry = {
            "id": final_id,
            "timestamp": time.time(),
            "symbol": symbol,
            "action": action,
            "price": price,
            "quantity": quantity,
            "status": status,
            "fee_usd": fee_usd,
            "slippage_pct": slippage_pct,
            "reasoning": reasoning
        }
        
        journal = self._load_journal()
        journal.append(entry)
        
        # Keep journal list bounded to prevent infinite file size (e.g. max 500 entries)
        if len(journal) > 500:
            journal.pop(0)
            
        self._save_journal(journal)
        logger.info(f"Recorded trade journal entry for {symbol} ({action})")
        return entry

# Global singleton
journal_manager = JournalManager()
