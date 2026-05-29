package com.crypto.marketengine.engine;

import org.springframework.stereotype.Component;
import java.util.concurrent.ConcurrentHashMap;

@Component
public class LimitOrderBookManager {

    private final ConcurrentHashMap<String, LimitOrderBook> books = new ConcurrentHashMap<>();

    public LimitOrderBook getBook(String symbol) {
        String upperSymbol = symbol.toUpperCase();
        return books.computeIfAbsent(upperSymbol, s -> {
            LimitOrderBook book = new LimitOrderBook(s);
            // Pre-seed with default values based on common trading pairs
            if ("BTCUSDT".equals(s)) {
                book.seedFromTicker(90000.0, 1500.0);
            } else if ("ETHUSDT".equals(s)) {
                book.seedFromTicker(3500.0, 100.0);
            } else if ("SOLUSDT".equals(s)) {
                book.seedFromTicker(150.0, 5.0);
            } else {
                // Generic fallback pre-seeding
                book.seedFromTicker(100.0, 2.0);
            }
            return book;
        });
    }

    public boolean hasBook(String symbol) {
        return books.containsKey(symbol.toUpperCase());
    }
}
