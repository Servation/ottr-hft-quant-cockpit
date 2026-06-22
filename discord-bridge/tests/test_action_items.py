"""
Tests for the three agent action items wired up in paper trading:
  - Mercury:       place_limit_order tool (Athena calls it; order persists & triggers)
  - Midas/Zephyr: SOL exposure cap blocks oversized BUY SOL in execute_trade
  - Atlas:         MACD signal-line crossover detection triggers emergency meeting
"""
import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch

from bot.tools import handle_tool_call
from bot.alerts import AlertMonitor

# ---------------------------------------------------------------------------
# Shared price stubs
# ---------------------------------------------------------------------------
PRICES_NO_SOL = {
    "BTC": {"price": 50_000.0, "change_24h": 1.0},
    "ETH": {"price": 3_000.0, "change_24h": -0.5},
}
PRICES_WITH_SOL = {
    **PRICES_NO_SOL,
    "SOL": {"price": 100.0, "change_24h": 2.0},
}


# ===========================================================================
# Mercury: place_limit_order tool
# ===========================================================================

class TestPlaceLimitOrder:
    """place_limit_order stores a pending order and returns a confirmation."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, mocker):
        monkeypatch.setenv("TRADING_DRY_RUN", "0")
        mocker.patch("bot.tools.price_feed.get_prices", new=AsyncMock(return_value=PRICES_WITH_SOL))

    @pytest.mark.asyncio
    async def test_buy_limit_order_stored(self, mocker):
        """BUY limit order is stored in the portfolio and confirmation returned."""
        place = mocker.patch(
            "bot.tools.portfolio.place_order",
            return_value="abc12345",
        )
        result = await handle_tool_call(
            "place_limit_order",
            {"action": "BUY", "asset": "BTC", "amount": 500, "target_price": 45_000},
        )
        place.assert_called_once_with("LIMIT", "BUY", "BTC", 500.0, 45_000.0)
        assert "abc12345" in result
        assert "BTC" in result
        assert "45,000" in result

    @pytest.mark.asyncio
    async def test_sell_limit_order_stored(self, mocker):
        """SELL limit order uses ≥ direction in confirmation message."""
        mocker.patch("bot.tools.portfolio.place_order", return_value="xyz99999")
        result = await handle_tool_call(
            "place_limit_order",
            {"action": "SELL", "asset": "ETH", "amount": 0.5, "target_price": 3_500},
        )
        assert "≥" in result
        assert "ETH" in result

    @pytest.mark.asyncio
    async def test_limit_order_blocked_by_dry_run(self, monkeypatch, mocker):
        """place_limit_order is blocked when TRADING_DRY_RUN=1."""
        monkeypatch.setenv("TRADING_DRY_RUN", "1")
        place = mocker.patch("bot.tools.portfolio.place_order")
        result = await handle_tool_call(
            "place_limit_order",
            {"action": "BUY", "asset": "BTC", "amount": 500, "target_price": 45_000},
        )
        assert "dry-run" in result.lower() or "blocked" in result.lower()
        place.assert_not_called()

    @pytest.mark.asyncio
    async def test_limit_order_triggers_at_target(self, mocker):
        """When price falls to the limit target, check_orders executes the trade."""
        from bot.portfolio import Portfolio

        p = Portfolio.__new__(Portfolio)
        p._state = {
            "cash": 10_000.0,
            "holdings": {"BTC": {"quantity": 0.0, "avg_cost": 0.0}},
            "total_pnl": 0.0,
            "trade_history": [],
            "orders": [
                {
                    "id": "order01",
                    "type": "LIMIT",
                    "action": "BUY",
                    "asset": "BTC",
                    "amount": 500.0,
                    "target_price": 45_000.0,
                    "timestamp": time.time(),
                }
            ],
            "min_trade_usd": 100.0,
        }

        buy_spy = mocker.patch.object(p, "buy", wraps=p.buy)
        mocker.patch.object(p, "save")

        # Price drops to exactly the limit price → should trigger
        prices_at_limit = {"BTC": {"price": 45_000.0}}
        executed = p.check_orders(prices_at_limit)

        assert len(executed) == 1
        assert executed[0]["asset"] == "BTC"
        buy_spy.assert_called_once()
        assert p._state["orders"] == []  # order consumed

    @pytest.mark.asyncio
    async def test_limit_order_does_not_trigger_above_target(self, mocker):
        """BUY limit order must NOT fire while price is above the target."""
        from bot.portfolio import Portfolio

        p = Portfolio.__new__(Portfolio)
        p._state = {
            "cash": 10_000.0,
            "holdings": {"BTC": {"quantity": 0.0, "avg_cost": 0.0}},
            "total_pnl": 0.0,
            "trade_history": [],
            "orders": [
                {
                    "id": "order02",
                    "type": "LIMIT",
                    "action": "BUY",
                    "asset": "BTC",
                    "amount": 500.0,
                    "target_price": 45_000.0,
                    "timestamp": time.time(),
                }
            ],
            "min_trade_usd": 100.0,
        }
        mocker.patch.object(p, "save")

        prices_above = {"BTC": {"price": 50_000.0}}
        executed = p.check_orders(prices_above)

        assert executed == []
        assert len(p._state["orders"]) == 1  # still pending


# ===========================================================================
# Midas/Zephyr: SOL exposure cap
# ===========================================================================

class TestSolExposureCap:
    """execute_trade blocks BUY SOL when projected portfolio weight exceeds the cap."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, mocker):
        monkeypatch.setenv("TRADING_DRY_RUN", "0")
        monkeypatch.delenv("MAX_TRADE_USD", raising=False)
        mocker.patch("bot.tools.price_feed.get_prices", new=AsyncMock(return_value=PRICES_WITH_SOL))

    def _mock_settings(self, mocker, max_sol_pct):
        """Replace bot.tools.settings with a MagicMock that returns the given cap."""
        mock_settings = MagicMock()
        mock_settings.get.side_effect = lambda key, default=None: (
            {"max_sol_exposure_pct": max_sol_pct} if key == "thresholds" else default
        )
        mocker.patch("bot.tools.settings", mock_settings)
        return mock_settings

    def _stub_portfolio(self, mocker, total_value, sol_quantity=0.0):
        """Stub portfolio total value and SOL holdings."""
        mocker.patch("bot.tools.portfolio.get_total_value", return_value=total_value)
        import bot.tools as tools_mod
        # Use patch.dict so mocker reverts the mutation after each test —
        # direct dict assignment is NOT auto-reverted and can corrupt the live file.
        mocker.patch.dict(
            tools_mod.portfolio._state["holdings"],
            {"SOL": {"quantity": sol_quantity, "avg_cost": 0.0}},
        )

    @pytest.mark.asyncio
    async def test_sol_buy_blocked_over_cap(self, mocker):
        """BUY SOL is blocked when it would push SOL above max_sol_exposure_pct."""
        # Portfolio: $10k total, $0 SOL.  Buying $3k SOL → ~23% > 20% cap.
        self._mock_settings(mocker, max_sol_pct=20.0)
        self._stub_portfolio(mocker, total_value=10_000.0, sol_quantity=0.0)
        buy = mocker.patch("bot.tools.portfolio.buy")

        result = await handle_tool_call(
            "execute_trade", {"action": "BUY", "asset": "SOL", "amount": 3_000}
        )

        assert "cap" in result.lower() or "exposure" in result.lower()
        buy.assert_not_called()

    @pytest.mark.asyncio
    async def test_sol_buy_allowed_under_cap(self, mocker):
        """BUY SOL proceeds when projected weight stays below the cap."""
        # Portfolio: $10k total, $0 SOL.  Buying $1k SOL → ~9% < 20% cap.
        self._mock_settings(mocker, max_sol_pct=20.0)
        self._stub_portfolio(mocker, total_value=10_000.0, sol_quantity=0.0)
        buy = mocker.patch(
            "bot.tools.portfolio.buy",
            return_value={"quantity": 10.0, "fill_price": 100.0},
        )

        result = await handle_tool_call(
            "execute_trade", {"action": "BUY", "asset": "SOL", "amount": 1_000}
        )

        buy.assert_called_once()
        assert "Trade Executed" in result

    @pytest.mark.asyncio
    async def test_sol_cap_not_applied_to_other_assets(self, mocker):
        """The SOL exposure cap must not affect BUY orders for BTC or ETH."""
        self._mock_settings(mocker, max_sol_pct=20.0)
        buy = mocker.patch(
            "bot.tools.portfolio.buy",
            return_value={"quantity": 0.02, "fill_price": 50_000.0},
        )

        result = await handle_tool_call(
            "execute_trade", {"action": "BUY", "asset": "BTC", "amount": 1_000}
        )

        buy.assert_called_once()
        assert "Trade Executed" in result

    @pytest.mark.asyncio
    async def test_sol_cap_disabled_when_zero(self, mocker):
        """A cap of 0 (unset) means no restriction on SOL buys."""
        self._mock_settings(mocker, max_sol_pct=0)
        self._stub_portfolio(mocker, total_value=10_000.0, sol_quantity=0.0)
        buy = mocker.patch(
            "bot.tools.portfolio.buy",
            return_value={"quantity": 100.0, "fill_price": 100.0},
        )

        await handle_tool_call(
            "execute_trade", {"action": "BUY", "asset": "SOL", "amount": 9_000}
        )
        buy.assert_called_once()


# ===========================================================================
# Atlas: MACD flip detection
# ===========================================================================

class TestMacdFlip:
    """AlertMonitor.check_macd_flip() detects crossovers and schedules emergency meetings."""

    BULLISH_INDICATORS = {
        "BTC": {"EMA_20": 64_000.0, "EMA_50": 63_000.0, "RSI_14": 55.0,
                "MACD": 150.0, "MACD_signal": 100.0},   # histogram +50 (positive)
        "ETH": {"EMA_20": 3_400.0, "EMA_50": 3_300.0, "RSI_14": 52.0,
                "MACD": 20.0, "MACD_signal": 25.0},     # histogram -5 (negative)
    }
    BEARISH_INDICATORS = {
        "BTC": {"EMA_20": 64_000.0, "EMA_50": 63_000.0, "RSI_14": 48.0,
                "MACD": 80.0, "MACD_signal": 100.0},    # histogram -20 (negative)
    }

    @pytest.fixture
    def monitor(self):
        return AlertMonitor()

    @pytest.mark.asyncio
    async def test_no_flip_on_first_reading(self, monitor, mocker):
        """First tick seeds the histogram; no flip can be detected yet."""
        mocker.patch(
            "bot.alerts.price_feed.get_technical_indicators",
            new=AsyncMock(return_value=self.BULLISH_INDICATORS),
        )
        flips = await monitor.check_macd_flip()
        assert flips == []
        assert "BTC" in monitor._prev_macd_histogram

    @pytest.mark.asyncio
    async def test_bullish_crossover_detected(self, monitor, mocker):
        """Negative → positive histogram triggers a BULLISH flip for BTC."""
        # Seed with negative histogram
        monitor._prev_macd_histogram["BTC"] = -30.0

        mocker.patch(
            "bot.alerts.price_feed.get_technical_indicators",
            new=AsyncMock(return_value={
                "BTC": {"MACD": 150.0, "MACD_signal": 100.0,
                        "EMA_20": 64_000.0, "EMA_50": 63_000.0, "RSI_14": 55.0},
            }),
        )
        flips = await monitor.check_macd_flip()

        assert len(flips) == 1
        assert flips[0]["asset"] == "BTC"
        assert flips[0]["direction"] == "BULLISH"

    @pytest.mark.asyncio
    async def test_bearish_crossover_detected(self, monitor, mocker):
        """Positive → negative histogram triggers a BEARISH flip."""
        monitor._prev_macd_histogram["ETH"] = 40.0

        mocker.patch(
            "bot.alerts.price_feed.get_technical_indicators",
            new=AsyncMock(return_value={
                "ETH": {"MACD": 10.0, "MACD_signal": 25.0,
                        "EMA_20": 3_400.0, "EMA_50": 3_300.0, "RSI_14": 45.0},
            }),
        )
        flips = await monitor.check_macd_flip()

        assert len(flips) == 1
        assert flips[0]["direction"] == "BEARISH"

    @pytest.mark.asyncio
    async def test_no_flip_when_sign_unchanged(self, monitor, mocker):
        """Same-sign histogram change must not generate a flip."""
        monitor._prev_macd_histogram["BTC"] = 10.0  # already positive

        mocker.patch(
            "bot.alerts.price_feed.get_technical_indicators",
            new=AsyncMock(return_value={
                "BTC": {"MACD": 150.0, "MACD_signal": 100.0,  # histogram still +50
                        "EMA_20": 64_000.0, "EMA_50": 63_000.0, "RSI_14": 55.0},
            }),
        )
        flips = await monitor.check_macd_flip()
        assert flips == []

    @pytest.mark.asyncio
    async def test_macd_flip_triggers_emergency_meeting(self, mocker):
        """_trigger_macd_alert posts to the channel and schedules an emergency meeting."""
        monitor = AlertMonitor()
        mock_bot = MagicMock()
        mock_bot._trading_floor_channel = MagicMock()
        mock_bot._trading_floor_channel.send = AsyncMock()

        mock_scheduler = mocker.patch(
            "bot.scheduler.meeting_scheduler.schedule_emergency",
            new=AsyncMock(),
        )

        flips = [{"asset": "BTC", "direction": "BULLISH", "macd": 150.0, "signal": 100.0}]
        await monitor._trigger_macd_alert(flips, mock_bot)

        mock_bot._trading_floor_channel.send.assert_awaited_once()
        msg = mock_bot._trading_floor_channel.send.call_args[0][0]
        assert "MACD" in msg
        assert "BTC" in msg
        mock_scheduler.assert_awaited_once()
        # Alert data passed to emergency meeting should reference the MACD direction
        alert_data = mock_scheduler.call_args[0][0]
        assert alert_data[0]["direction"] == "MACD_BULLISH"

    @pytest.mark.asyncio
    async def test_macd_flip_ignored_during_cooldown(self, mocker):
        """A second MACD flip within 4 hours must not fire another alert."""
        monitor = AlertMonitor()
        mock_bot = MagicMock()
        mock_bot._trading_floor_channel = MagicMock()
        mock_bot._trading_floor_channel.send = AsyncMock()

        mocker.patch(
            "bot.alerts.price_feed.get_technical_indicators",
            new=AsyncMock(return_value={
                "BTC": {"MACD": 150.0, "MACD_signal": 100.0,
                        "EMA_20": 0.0, "EMA_50": 0.0, "RSI_14": 50.0},
            }),
        )
        trigger = mocker.patch.object(monitor, "_trigger_macd_alert", new=AsyncMock())

        # Set the last alert to "just now" so cooldown hasn't elapsed
        monitor._last_macd_alert_time = time.monotonic()
        monitor._prev_macd_histogram["BTC"] = -5.0  # negative → will flip to positive

        # Simulate one monitor loop iteration (manually driving the cooldown check)
        flips = await monitor.check_macd_flip()
        if flips and monitor._macd_cooldown_elapsed():
            await monitor._trigger_macd_alert(flips, mock_bot)
            monitor._last_macd_alert_time = time.monotonic()

        trigger.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_macd_flip_resilient_to_api_error(self, mocker, monitor):
        """API failure returns empty list without raising."""
        mocker.patch(
            "bot.alerts.price_feed.get_technical_indicators",
            new=AsyncMock(side_effect=Exception("Kraken timeout")),
        )
        flips = await monitor.check_macd_flip()
        assert flips == []
