package com.crypto.marketengine.engine;

import com.crypto.marketengine.model.ExecutionResult;
import com.crypto.marketengine.model.OrderBookLevel;
import lombok.Getter;

import java.util.*;
import java.util.concurrent.ConcurrentSkipListMap;
import java.util.stream.Collectors;

public class LimitOrderBook {

    @Getter
    private final String symbol;

    // Bids sorted in descending order (highest price first)
    private final ConcurrentSkipListMap<Double, Double> bids = new ConcurrentSkipListMap<>(Comparator.reverseOrder());
    
    // Asks sorted in ascending order (lowest price first)
    private final ConcurrentSkipListMap<Double, Double> asks = new ConcurrentSkipListMap<>();

    public LimitOrderBook(String symbol) {
        this.symbol = symbol;
    }

    /**
     * Seeds synthetic levels from ticker mid-price using a power-law distribution.
     * Spread is defined as ATR * 0.001.
     */
    public synchronized void seedFromTicker(double midPrice, double atr) {
        bids.clear();
        asks.clear();

        double spread = Math.max(atr * 0.001, 0.01);
        double step = Math.max(atr * 0.002, 0.01);

        double baseQty = 50.0;
        int numLevels = 50;

        for (int i = 0; i < numLevels; i++) {
            // Bid side
            double bidPrice = roundPrice(midPrice - (spread / 2.0) - i * step);
            double bidQty = roundQty(baseQty / Math.pow(i + 1, 0.7));
            if (bidPrice > 0) {
                bids.put(bidPrice, bidQty);
            }

            // Ask side
            double askPrice = roundPrice(midPrice + (spread / 2.0) + i * step);
            double askQty = roundQty(baseQty / Math.pow(i + 1, 0.7));
            asks.put(askPrice, askQty);
        }
    }

    /**
     * Overlays real Binance L2 book depth snapshot when keys are present.
     */
    public synchronized void overlayRealDepth(List<OrderBookLevel> realBids, List<OrderBookLevel> realAsks) {
        if (realBids != null && !realBids.isEmpty()) {
            bids.clear();
            for (OrderBookLevel level : realBids) {
                bids.put(roundPrice(level.getPrice()), roundQty(level.getQuantity()));
            }
        }
        if (realAsks != null && !realAsks.isEmpty()) {
            asks.clear();
            for (OrderBookLevel level : realAsks) {
                asks.put(roundPrice(level.getPrice()), roundQty(level.getQuantity()));
            }
        }
    }

    /**
     * Matches a market order against the order book, updates depth, and returns execution result.
     */
    public synchronized ExecutionResult matchMarketOrder(String side, double quantity, double baseSlippage) {
        long startTime = System.currentTimeMillis();
        String orderId = UUID.randomUUID().toString();

        double remaining = quantity;
        double filled = 0.0;
        double accumulatedCost = 0.0;

        if ("BUY".equalsIgnoreCase(side)) {
            double avgDepth = asks.values().stream().mapToDouble(Double::doubleValue).average().orElse(1.0);
            while (remaining > 0 && !asks.isEmpty()) {
                Map.Entry<Double, Double> entry = asks.firstEntry();
                if (entry == null) break;

                double price = entry.getKey();
                double available = entry.getValue();

                if (available <= remaining) {
                    filled += available;
                    accumulatedCost += available * price;
                    remaining -= available;
                    asks.remove(price);
                } else {
                    filled += remaining;
                    accumulatedCost += remaining * price;
                    asks.put(price, roundQty(available - remaining));
                    remaining = 0;
                }
            }

            double vwapPrice = filled > 0 ? (accumulatedCost / filled) : 0.0;
            double slippagePercent = ExecutionMath.calculateDynamicSlippage(baseSlippage, quantity, avgDepth);
            double finalPrice = roundPrice(vwapPrice * (1.0 + slippagePercent));
            double feeDeducted = roundQty(finalPrice * filled * 0.001); // 0.1% fee

            long executionTimeMs = System.currentTimeMillis() - startTime;
            String status = (remaining == 0) ? "FILLED" : (filled > 0 ? "PARTIALLY_FILLED" : "REJECTED");

            return ExecutionResult.builder()
                    .orderId(orderId)
                    .symbol(symbol)
                    .side("BUY")
                    .requestedQuantity(quantity)
                    .filledQuantity(roundQty(filled))
                    .vwapPrice(finalPrice)
                    .slippage(slippagePercent)
                    .feeDeducted(feeDeducted)
                    .status(status)
                    .executionTimeMs(executionTimeMs)
                    .build();

        } else if ("SELL".equalsIgnoreCase(side)) {
            double avgDepth = bids.values().stream().mapToDouble(Double::doubleValue).average().orElse(1.0);
            while (remaining > 0 && !bids.isEmpty()) {
                Map.Entry<Double, Double> entry = bids.firstEntry();
                if (entry == null) break;

                double price = entry.getKey();
                double available = entry.getValue();

                if (available <= remaining) {
                    filled += available;
                    accumulatedCost += available * price;
                    remaining -= available;
                    bids.remove(price);
                } else {
                    filled += remaining;
                    accumulatedCost += remaining * price;
                    bids.put(price, roundQty(available - remaining));
                    remaining = 0;
                }
            }

            double vwapPrice = filled > 0 ? (accumulatedCost / filled) : 0.0;
            double slippagePercent = ExecutionMath.calculateDynamicSlippage(baseSlippage, quantity, avgDepth);
            double finalPrice = roundPrice(vwapPrice * (1.0 - slippagePercent));
            double feeDeducted = roundQty(finalPrice * filled * 0.001); // 0.1% fee

            long executionTimeMs = System.currentTimeMillis() - startTime;
            String status = (remaining == 0) ? "FILLED" : (filled > 0 ? "PARTIALLY_FILLED" : "REJECTED");

            return ExecutionResult.builder()
                    .orderId(orderId)
                    .symbol(symbol)
                    .side("SELL")
                    .requestedQuantity(quantity)
                    .filledQuantity(roundQty(filled))
                    .vwapPrice(finalPrice)
                    .slippage(slippagePercent)
                    .feeDeducted(feeDeducted)
                    .status(status)
                    .executionTimeMs(executionTimeMs)
                    .build();
        }

        return ExecutionResult.builder()
                .orderId(orderId)
                .symbol(symbol)
                .side(side)
                .requestedQuantity(quantity)
                .filledQuantity(0)
                .vwapPrice(0)
                .slippage(0)
                .feeDeducted(0)
                .status("REJECTED")
                .executionTimeMs(System.currentTimeMillis() - startTime)
                .build();
    }

    public synchronized List<OrderBookLevel> getBidsList() {
        return bids.entrySet().stream()
                .map(e -> new OrderBookLevel(e.getKey(), e.getValue()))
                .collect(Collectors.toList());
    }

    public synchronized List<OrderBookLevel> getAsksList() {
        return asks.entrySet().stream()
                .map(e -> new OrderBookLevel(e.getKey(), e.getValue()))
                .collect(Collectors.toList());
    }

    private double roundPrice(double price) {
        return Math.round(price * 1e8) / 1e8;
    }

    private double roundQty(double qty) {
        return Math.round(qty * 1e8) / 1e8;
    }
}
