import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Dict, Any, List, Optional

from bot import settings, PROJECT_ROOT

logger = logging.getLogger(__name__)

# Persistence path
_DATA_DIR = PROJECT_ROOT / "data"
_PORTFOLIO_FILE = _DATA_DIR / "portfolio_state.json"

# Defaults from settings
_portfolio_cfg = settings.get("portfolio", {})
_STARTING_BALANCE = float(_portfolio_cfg.get("starting_balance", 10000))
_SLIPPAGE_PCT = float(_portfolio_cfg.get("slippage_pct", 0.1))
_DEFAULT_MIN_TRADE_USD = float(_portfolio_cfg.get("min_trade_usd", 100.0))
_ASSETS: List[str] = _portfolio_cfg.get("assets", ["BTC", "ETH"])
_MAX_TRADE_HISTORY = 50


def _default_state() -> Dict[str, Any]:
    """Generates the initial portfolio state."""
    holdings: Dict[str, Dict[str, float]] = {}
    for asset in _ASSETS:
        holdings[asset] = {"quantity": 0.0, "avg_cost": 0.0}
    return {
        "cash": _STARTING_BALANCE,
        "holdings": holdings,
        "total_pnl": 0.0,
        "trade_history": [],
        "orders": [],
        "min_trade_usd": _DEFAULT_MIN_TRADE_USD,
    }


class Portfolio:
    """
    Paper-trading portfolio tracker with persistence.
    Supports buy/sell with configurable slippage, tracks cost basis,
    P&L, and persists state to disk via atomic writes.
    """

    def __init__(self) -> None:
        self._state: Dict[str, Any] = _default_state()
        self.load()

    @property
    def min_trade_usd(self) -> float:
        return float(self._state.get("min_trade_usd", _DEFAULT_MIN_TRADE_USD))

    @min_trade_usd.setter
    def min_trade_usd(self, value: float) -> None:
        self._state["min_trade_usd"] = float(value)
        self.save()

    # ── Persistence ──────────────────────────────────────────────

    def load(self) -> None:
        """Load portfolio state from disk, creating default if missing."""
        if _PORTFOLIO_FILE.exists():
            try:
                with open(_PORTFOLIO_FILE, "r", encoding="utf-8") as f:
                    self._state = json.load(f)
                # Healing/upgrade check
                if "min_trade_usd" not in self._state:
                    self._state["min_trade_usd"] = _DEFAULT_MIN_TRADE_USD
                if "orders" not in self._state:
                    self._state["orders"] = []
                    self.save()
                logger.info("Loaded portfolio from %s", _PORTFOLIO_FILE)
            except (json.JSONDecodeError, OSError) as e:
                logger.error("Failed to load portfolio (%s), resetting to default", e)
                self._state = _default_state()
                self.save()
        else:
            logger.info("No existing portfolio found, initialising with $%.2f", _STARTING_BALANCE)
            self._state = _default_state()
            self.save()

    def save(self) -> None:
        """Atomically persist state: write temp file then os.replace()."""
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(_DATA_DIR), suffix=".tmp", prefix="portfolio_"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2)
            os.replace(tmp_path, str(_PORTFOLIO_FILE))
            logger.debug("Portfolio saved to %s", _PORTFOLIO_FILE)
        except OSError as e:
            logger.error("Failed to save portfolio: %s", e)

    # ── Trading ──────────────────────────────────────────────────

    def buy(self, asset: str, usd_amount: float, price: float) -> Dict[str, Any]:
        """
        Buy an asset with a USD amount at the given market price.
        Slippage is applied upward (worse fill for buyer).
        Returns a trade record dict.
        """
        if usd_amount <= 0 or price <= 0:
            raise ValueError("usd_amount and price must be positive")

        if usd_amount < self.min_trade_usd:
            raise ValueError(
                f"Trade size (${usd_amount:.2f}) is below the minimum trade limit (${self.min_trade_usd:.2f})"
            )

        if usd_amount > self._state["cash"]:
            raise ValueError(
                f"Insufficient cash: have ${self._state['cash']:.2f}, need ${usd_amount:.2f}"
            )

        fill_price = price * (1.0 + _SLIPPAGE_PCT / 100.0)
        quantity = usd_amount / fill_price
        cost = quantity * fill_price

        # Update holdings
        holding = self._state["holdings"].setdefault(
            asset, {"quantity": 0.0, "avg_cost": 0.0}
        )
        prev_qty = holding["quantity"]
        prev_cost = holding["avg_cost"]

        # Weighted average cost basis
        total_qty = prev_qty + quantity
        if total_qty > 0:
            holding["avg_cost"] = (
                (prev_cost * prev_qty) + (fill_price * quantity)
            ) / total_qty
        holding["quantity"] = total_qty

        self._state["cash"] -= cost

        trade = {
            "timestamp": time.time(),
            "action": "BUY",
            "asset": asset,
            "quantity": quantity,
            "fill_price": fill_price,
            "usd_amount": cost,
            "slippage_pct": _SLIPPAGE_PCT,
        }
        self._append_trade(trade)
        self.save()
        logger.info(
            "BUY %.8f %s @ $%.2f (fill $%.2f, cost $%.2f)",
            quantity, asset, price, fill_price, cost,
        )
        return trade

    def sell(self, asset: str, quantity: float, price: float) -> Dict[str, Any]:
        """
        Sell a quantity of an asset at the given market price.
        Slippage is applied downward (worse fill for seller).
        Returns a trade record dict.
        """
        if quantity <= 0 or price <= 0:
            raise ValueError("quantity and price must be positive")

        holding = self._state["holdings"].get(asset)
        if holding is None or holding["quantity"] < quantity:
            available = holding["quantity"] if holding else 0.0
            raise ValueError(
                f"Insufficient {asset}: have {available:.8f}, want {quantity:.8f}"
            )

        fill_price = price * (1.0 - _SLIPPAGE_PCT / 100.0)
        proceeds = quantity * fill_price

        # Check minimum trade threshold unless we are liquidating the full position
        is_full_liquidation = abs(quantity - holding["quantity"]) < 1e-9
        if not is_full_liquidation and proceeds < self.min_trade_usd:
            raise ValueError(
                f"Sell trade value (${proceeds:.2f}) is below the minimum trade limit (${self.min_trade_usd:.2f})"
            )

        # Realised P&L for this trade
        avg_cost = holding["avg_cost"]
        realised_pnl = (fill_price - avg_cost) * quantity

        holding["quantity"] -= quantity
        if holding["quantity"] < 1e-12:
            holding["quantity"] = 0.0
            holding["avg_cost"] = 0.0

        self._state["cash"] += proceeds
        self._state["total_pnl"] += realised_pnl

        trade = {
            "timestamp": time.time(),
            "action": "SELL",
            "asset": asset,
            "quantity": quantity,
            "fill_price": fill_price,
            "usd_amount": proceeds,
            "realised_pnl": realised_pnl,
            "slippage_pct": _SLIPPAGE_PCT,
        }
        self._append_trade(trade)
        self.save()
        logger.info(
            "SELL %.8f %s @ $%.2f (fill $%.2f, proceeds $%.2f, P&L $%.2f)",
            quantity, asset, price, fill_price, proceeds, realised_pnl,
        )
        return trade

    # ── Orders ───────────────────────────────────────────────────

    def place_order(self, order_type: str, action: str, asset: str, amount: float, target_price: float) -> str:
        """Place a pending order."""
        import uuid
        order_id = str(uuid.uuid4())[:8]
        order = {
            "id": order_id,
            "type": order_type.upper(),
            "action": action.upper(),
            "asset": asset.upper(),
            "amount": float(amount),
            "target_price": float(target_price),
            "timestamp": time.time(),
        }
        self._state.setdefault("orders", []).append(order)
        self.save()
        logger.info("Placed %s %s order %s for %s %s @ $%.2f", order_type, action, order_id, amount, asset, target_price)
        return order_id

    def cancel_all_orders(self, asset: str) -> int:
        """Cancel all pending orders for an asset."""
        orders = self._state.setdefault("orders", [])
        initial_count = len(orders)
        self._state["orders"] = [o for o in orders if o["asset"] != asset.upper()]
        canceled_count = initial_count - len(self._state["orders"])
        if canceled_count > 0:
            self.save()
            logger.info("Canceled %d orders for %s", canceled_count, asset)
        return canceled_count

    def check_orders(self, current_prices: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Check if any pending orders should be triggered based on current prices."""
        orders = self._state.setdefault("orders", [])
        executed = []
        remaining_orders = []

        for order in orders:
            asset = order["asset"]
            current_price = current_prices.get(asset, {}).get("price", 0.0)
            if current_price <= 0:
                remaining_orders.append(order)
                continue

            triggered = False
            target_price = order["target_price"]

            if order["type"] == "LIMIT" and order["action"] == "BUY":
                if current_price <= target_price:
                    triggered = True
            elif order["type"] == "LIMIT" and order["action"] == "SELL":
                if current_price >= target_price:
                    triggered = True
            elif order["type"] == "STOP" and order["action"] == "SELL":
                if current_price <= target_price:
                    triggered = True
            elif order["type"] == "TAKE_PROFIT" and order["action"] == "SELL":
                if current_price >= target_price:
                    triggered = True

            if triggered:
                try:
                    if order["action"] == "BUY":
                        trade = self.buy(asset, order["amount"], current_price)
                    else:
                        trade = self.sell(asset, order["amount"], current_price)
                    trade["triggered_order_type"] = order["type"]
                    executed.append(trade)
                    logger.info("Order %s triggered and executed.", order["id"])
                except Exception as e:
                    logger.error("Failed to execute triggered order %s: %s", order["id"], e)
            else:
                remaining_orders.append(order)

        if len(remaining_orders) != len(orders):
            self._state["orders"] = remaining_orders
            self.save()

        return executed

    # ── Queries ───────────────────────────────────────────────────

    def get_total_value(self, prices_dict: Dict[str, Dict[str, Any]]) -> float:
        """
        Computes total portfolio value (cash + holdings at current prices).
        prices_dict format: {'BTC': {'price': 65000.0, ...}, ...}
        """
        total = self._state["cash"]
        for asset, holding in self._state["holdings"].items():
            asset_price = prices_dict.get(asset, {}).get("price", 0.0)
            total += holding["quantity"] * asset_price
        return total

    def get_summary(self) -> Dict[str, Any]:
        """Returns a snapshot of the portfolio for display or LLM context."""
        return {
            "cash": self._state["cash"],
            "holdings": {
                asset: {
                    "quantity": h["quantity"],
                    "avg_cost": h["avg_cost"],
                }
                for asset, h in self._state["holdings"].items()
            },
            "pending_orders": self._state.setdefault("orders", []),
            "total_pnl": self._state["total_pnl"],
            "min_trade_usd": self.min_trade_usd,
            "recent_trades": self._state["trade_history"][-5:],
        }

    # ── Internals ─────────────────────────────────────────────────

    def _append_trade(self, trade: Dict[str, Any]) -> None:
        """Appends a trade record, capping history at _MAX_TRADE_HISTORY."""
        history = self._state["trade_history"]
        history.append(trade)
        if len(history) > _MAX_TRADE_HISTORY:
            self._state["trade_history"] = history[-_MAX_TRADE_HISTORY:]


portfolio = Portfolio()
