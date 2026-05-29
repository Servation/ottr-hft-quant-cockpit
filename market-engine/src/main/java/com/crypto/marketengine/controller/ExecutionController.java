package com.crypto.marketengine.controller;

import com.crypto.marketengine.engine.LimitOrderBook;
import com.crypto.marketengine.engine.LimitOrderBookManager;
import com.crypto.marketengine.model.ExecutionResult;
import com.crypto.marketengine.model.OrderBookLevel;
import com.crypto.marketengine.model.OrderRequest;
import com.crypto.marketengine.ratelimit.BinanceRateLimiter;
import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.HashMap;
import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api/v1")
public class ExecutionController {

    private final LimitOrderBookManager bookManager;
    private final BinanceRateLimiter rateLimiter;
    
    // Custom Prometheus Metrics
    private final Counter orderCounter;
    private final Counter executionVolumeCounter;
    private final Counter rateLimitBackoffs;

    public ExecutionController(LimitOrderBookManager bookManager, BinanceRateLimiter rateLimiter, MeterRegistry meterRegistry) {
        this.bookManager = bookManager;
        this.rateLimiter = rateLimiter;
        
        this.orderCounter = meterRegistry.counter("market_engine_orders_matched_total");
        this.executionVolumeCounter = meterRegistry.counter("market_engine_volume_usd_total");
        this.rateLimitBackoffs = meterRegistry.counter("market_engine_ratelimit_backoffs_total");
    }

    @PostMapping("/execute")
    public ResponseEntity<ExecutionResult> executeOrder(
            @RequestBody OrderRequest orderRequest,
            @RequestHeader(value = "X-Inference-Delay-Ms", required = false, defaultValue = "0") long inferenceDelayMs,
            @RequestHeader(value = "X-API-KEY", required = false) String apiKey,
            @RequestHeader(value = "Authorization", required = false) String authHeader,
            HttpServletResponse response) {

        long startTime = System.currentTimeMillis();
        long targetExecutionTimeMs = startTime + inferenceDelayMs;
        boolean isAuthenticated = (apiKey != null && !apiKey.isEmpty()) || (authHeader != null && !authHeader.isEmpty());

        // Dynamic weight limit checking (execute order is weight 5)
        int initialLimiterWeight = rateLimiter.getWeight(isAuthenticated);
        int currentWeight = rateLimiter.acquire(5, isAuthenticated);
        
        // Track backoffs
        if (rateLimiter.getWeight(isAuthenticated) - initialLimiterWeight > 5) {
            // Suggesting backoff sleep was triggered
            rateLimitBackoffs.increment();
        }

        // Simulate model inference delay if specified
        if (inferenceDelayMs > 0) {
            try {
                Thread.sleep(inferenceDelayMs);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }

        LimitOrderBook book = bookManager.getBook(orderRequest.getSymbol());
        
        // Match order (default base slippage is 0.1% or 0.001)
        ExecutionResult result = book.matchMarketOrder(orderRequest.getSide(), orderRequest.getQuantity(), 0.001);
        
        // Populate additional execution latency details
        long actualEndTime = System.currentTimeMillis();
        result.setTargetExecutionTimeMs(targetExecutionTimeMs);
        result.setActualLatencyMs(actualEndTime - startTime);

        // Update Prometheus Metrics
        orderCounter.increment();
        if ("FILLED".equals(result.getStatus()) || "PARTIALLY_FILLED".equals(result.getStatus())) {
            executionVolumeCounter.increment(result.getFilledQuantity() * result.getVwapPrice());
        }

        // Add the Binance rate-limiting weight header to the response
        response.setHeader("X-MBX-USED-WEIGHT-1M", String.valueOf(currentWeight));

        return ResponseEntity.ok(result);
    }

    @GetMapping("/orderbook/{symbol}")
    public ResponseEntity<Map<String, Object>> getOrderBook(
            @PathVariable String symbol,
            @RequestHeader(value = "X-API-KEY", required = false) String apiKey,
            @RequestHeader(value = "Authorization", required = false) String authHeader,
            HttpServletResponse response) {

        boolean isAuthenticated = (apiKey != null && !apiKey.isEmpty()) || (authHeader != null && !authHeader.isEmpty());
        
        // Public/Auth check weight 1 for GET depth
        int currentWeight = rateLimiter.acquire(1, isAuthenticated);
        response.setHeader("X-MBX-USED-WEIGHT-1M", String.valueOf(currentWeight));

        LimitOrderBook book = bookManager.getBook(symbol);

        Map<String, Object> body = new HashMap<>();
        body.put("symbol", book.getSymbol());
        body.put("bids", book.getBidsList());
        body.put("asks", book.getAsksList());

        return ResponseEntity.ok(body);
    }

    @PostMapping("/orderbook/{symbol}/seed")
    public ResponseEntity<Map<String, String>> seedOrderBook(
            @PathVariable String symbol,
            @RequestParam double midPrice,
            @RequestParam double atr,
            HttpServletResponse response) {

        LimitOrderBook book = bookManager.getBook(symbol);
        book.seedFromTicker(midPrice, atr);

        Map<String, String> body = new HashMap<>();
        body.put("status", "SUCCESS");
        body.put("message", "Seeded order book for " + symbol.toUpperCase() + " with mid price " + midPrice + " and ATR " + atr);

        return ResponseEntity.ok(body);
    }

    @PostMapping("/orderbook/{symbol}/overlay")
    public ResponseEntity<Map<String, String>> overlayOrderBook(
            @PathVariable String symbol,
            @RequestBody OverlayRequest overlayRequest,
            HttpServletResponse response) {

        LimitOrderBook book = bookManager.getBook(symbol);
        book.overlayRealDepth(overlayRequest.getBids(), overlayRequest.getAsks());

        Map<String, String> body = new HashMap<>();
        body.put("status", "SUCCESS");
        body.put("message", "Overlaid Binance depth for " + symbol.toUpperCase());

        return ResponseEntity.ok(body);
    }

    @GetMapping("/health")
    public ResponseEntity<Map<String, String>> getHealth() {
        Map<String, String> body = new HashMap<>();
        body.put("status", "UP");
        return ResponseEntity.ok(body);
    }

    // Helper request class for overlaying
    public static class OverlayRequest {
        private List<OrderBookLevel> bids;
        private List<OrderBookLevel> asks;

        public List<OrderBookLevel> getBids() {
            return bids;
        }

        public void setBids(List<OrderBookLevel> bids) {
            this.bids = bids;
        }

        public List<OrderBookLevel> getAsks() {
            return asks;
        }

        public void setAsks(List<OrderBookLevel> asks) {
            this.asks = asks;
        }
    }
}
