package com.crypto.marketengine.ratelimit;

import lombok.Getter;
import org.springframework.stereotype.Component;
import java.util.concurrent.ConcurrentLinkedQueue;

@Component
public class BinanceRateLimiter {
    
    private static class WeightRecord {
        private final long timestamp;
        private final int weight;

        public WeightRecord(long timestamp, int weight) {
            this.timestamp = timestamp;
            this.weight = weight;
        }

        public long getTimestamp() {
            return timestamp;
        }

        public int getWeight() {
            return weight;
        }
    }

    private final ConcurrentLinkedQueue<WeightRecord> publicWindow = new ConcurrentLinkedQueue<>();
    private final ConcurrentLinkedQueue<WeightRecord> authWindow = new ConcurrentLinkedQueue<>();

    private static final int AUTH_LIMIT = 6000;
    private static final int AUTH_BACKOFF_THRESHOLD = 5400;
    private static final int PUBLIC_LIMIT = 1200;
    private static final int PUBLIC_BACKOFF_THRESHOLD = 1000;

    /**
     * Acquires weight from the sliding window limiter.
     * Injects a 5-second sleep/backoff if the backoff threshold is exceeded.
     * Returns the updated window weight.
     */
    public synchronized int acquire(int weight, boolean isAuthenticated) {
        long now = System.currentTimeMillis();
        ConcurrentLinkedQueue<WeightRecord> window = isAuthenticated ? authWindow : publicWindow;
        int threshold = isAuthenticated ? AUTH_BACKOFF_THRESHOLD : PUBLIC_BACKOFF_THRESHOLD;

        // Clean up old records (older than 60s)
        cleanWindow(window, now);

        int currentWeight = getWindowWeight(window);

        // Check if we need to back off
        if (currentWeight + weight > threshold) {
            try {
                Thread.sleep(5000);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            // Recalculate time and window after backoff sleep
            now = System.currentTimeMillis();
            cleanWindow(window, now);
            currentWeight = getWindowWeight(window);
        }

        window.add(new WeightRecord(now, weight));
        return currentWeight + weight;
    }

    private void cleanWindow(ConcurrentLinkedQueue<WeightRecord> window, long now) {
        while (!window.isEmpty()) {
            WeightRecord record = window.peek();
            if (record != null && (now - record.getTimestamp() > 60000)) {
                window.poll();
            } else {
                break;
            }
        }
    }

    public synchronized int getWeight(boolean isAuthenticated) {
        long now = System.currentTimeMillis();
        ConcurrentLinkedQueue<WeightRecord> window = isAuthenticated ? authWindow : publicWindow;
        cleanWindow(window, now);
        return getWindowWeight(window);
    }

    private int getWindowWeight(ConcurrentLinkedQueue<WeightRecord> window) {
        return window.stream().mapToInt(WeightRecord::getWeight).sum();
    }
}
