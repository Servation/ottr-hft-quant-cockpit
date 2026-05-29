package com.crypto.marketengine;

import com.crypto.marketengine.engine.ExecutionMath;
import com.crypto.marketengine.engine.LimitOrderBook;
import com.crypto.marketengine.model.ExecutionResult;
import com.crypto.marketengine.model.OHLC;
import com.crypto.marketengine.model.OrderBookLevel;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

import static org.junit.jupiter.api.Assertions.*;

public class LimitOrderBookTest {

    @Test
    public void testSeedingAndOverlay() {
        LimitOrderBook book = new LimitOrderBook("BTCUSDT");
        book.seedFromTicker(90000.0, 1500.0);

        List<OrderBookLevel> bids = book.getBidsList();
        List<OrderBookLevel> asks = book.getAsksList();

        assertFalse(bids.isEmpty(), "Bids should not be empty");
        assertFalse(asks.isEmpty(), "Asks should not be empty");
        assertTrue(bids.get(0).getPrice() < 90000.0, "Top bid should be below mid-price");
        assertTrue(asks.get(0).getPrice() > 90000.0, "Top ask should be above mid-price");

        // Test overlay
        List<OrderBookLevel> realBids = Arrays.asList(new OrderBookLevel(89500.0, 2.5));
        List<OrderBookLevel> realAsks = Arrays.asList(new OrderBookLevel(90500.0, 3.5));
        book.overlayRealDepth(realBids, realAsks);

        assertEquals(1, book.getBidsList().size(), "Bids size should be 1 after overlay");
        assertEquals(89500.0, book.getBidsList().get(0).getPrice());
        assertEquals(2.5, book.getBidsList().get(0).getQuantity());

        assertEquals(1, book.getAsksList().size(), "Asks size should be 1 after overlay");
        assertEquals(90500.0, book.getAsksList().get(0).getPrice());
        assertEquals(3.5, book.getAsksList().get(0).getQuantity());
    }

    @Test
    public void testMarketOrderMatching() {
        LimitOrderBook book = new LimitOrderBook("BTCUSDT");
        
        // Setup simple book
        List<OrderBookLevel> realBids = Arrays.asList(
                new OrderBookLevel(89000.0, 1.0),
                new OrderBookLevel(88000.0, 2.0)
        );
        List<OrderBookLevel> realAsks = Arrays.asList(
                new OrderBookLevel(91000.0, 1.5),
                new OrderBookLevel(92000.0, 2.5)
        );
        book.overlayRealDepth(realBids, realAsks);

        // Execute BUY order
        // Average depth is (1.5 + 2.5)/2 = 2.0
        // Base slippage is 0.001
        // Size is 2.0.
        // Asks will match: 1.5 @ 91000 and 0.5 @ 92000
        // VWAP = (1.5 * 91000 + 0.5 * 92000) / 2.0 = 182500 / 2.0 = 91250.0
        // Slippage = 0.001 * (2.0 / 2.0)^0.6 = 0.001
        // Final Price = 91250 * 1.001 = 91341.25
        ExecutionResult buyResult = book.matchMarketOrder("BUY", 2.0, 0.001);
        assertEquals("FILLED", buyResult.getStatus());
        assertEquals(2.0, buyResult.getFilledQuantity());
        assertEquals(91341.25, buyResult.getVwapPrice(), 0.01);
        assertEquals(0.001, buyResult.getSlippage(), 0.0001);
        assertEquals(91341.25 * 2.0 * 0.001, buyResult.getFeeDeducted(), 0.01);
    }

    @Test
    public void testExecutionMath() {
        double slippage = ExecutionMath.calculateDynamicSlippage(0.002, 10.0, 5.0);
        // 0.002 * (10.0 / 5.0)^0.6 = 0.002 * 2^0.6 = 0.002 * 1.5157 = 0.00303
        assertEquals(0.00303, slippage, 0.0001);

        // Test ATR
        List<OHLC> candles = new ArrayList<>();
        candles.add(new OHLC(100, 110, 95, 105, 1000));
        candles.add(new OHLC(105, 115, 100, 110, 1200));
        candles.add(new OHLC(110, 120, 105, 115, 1500));
        
        double atr = ExecutionMath.calculateATR(candles, 2);
        assertTrue(atr > 0, "ATR should be greater than 0");
    }
}
