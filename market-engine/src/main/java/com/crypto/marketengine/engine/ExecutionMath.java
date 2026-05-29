package com.crypto.marketengine.engine;

import com.crypto.marketengine.model.OHLC;
import java.util.List;

public class ExecutionMath {

    public static double calculateDynamicSlippage(double base, double orderSize, double avgDepth) {
        if (avgDepth <= 0) return base;
        return base * Math.pow(orderSize / avgDepth, 0.6);
    }

    public static double calculateATR(List<OHLC> candles, int period) {
        if (candles == null || candles.size() < 2) return 0.0;
        int n = candles.size();
        double[] tr = new double[n];
        tr[0] = candles.get(0).getHigh() - candles.get(0).getLow();
        for (int i = 1; i < n; i++) {
            double h = candles.get(i).getHigh();
            double l = candles.get(i).getLow();
            double pc = candles.get(i - 1).getClose();
            tr[i] = Math.max(h - l, Math.max(Math.abs(h - pc), Math.abs(l - pc)));
        }
        if (n <= period) {
            double sum = 0;
            for (int i = 0; i < n; i++) sum += tr[i];
            return sum / n;
        }
        double atr = 0;
        for (int i = 0; i < period; i++) {
            atr += tr[i];
        }
        atr /= period;
        for (int i = period; i < n; i++) {
            atr = (atr * (period - 1) + tr[i]) / period;
        }
        return atr;
    }

    public static double calculateH6Momentum(List<OHLC> candles) {
        if (candles == null || candles.size() < 14) {
            return 0.0;
        }
        int n = candles.size();

        // 1. Weighted 6h Rate-of-Change (ROC)
        double roc6 = 0.0;
        if (n >= 7) {
            double currentClose = candles.get(n - 1).getClose();
            double sumWeights = 0.0;
            double sumWeightedRoc = 0.0;
            for (int i = 1; i <= 6; i++) {
                double prevClose = candles.get(n - 1 - i).getClose();
                if (prevClose > 0) {
                    double r = (currentClose - prevClose) / prevClose;
                    double weight = 7.0 - i;
                    sumWeightedRoc += r * weight;
                    sumWeights += weight;
                }
            }
            if (sumWeights > 0) {
                roc6 = sumWeightedRoc / sumWeights;
            }
        }

        // 2. Volume Momentum
        double volMom = 0.0;
        if (n >= 6) {
            double recentVolSum = 0;
            for (int i = n - 6; i < n; i++) {
                recentVolSum += candles.get(i).getVolume();
            }
            double recentVolAvg = recentVolSum / 6.0;

            double histVolSum = 0;
            int histCount = Math.min(24, n);
            for (int i = n - histCount; i < n; i++) {
                histVolSum += candles.get(i).getVolume();
            }
            double histVolAvg = histVolSum / histCount;

            if (histVolAvg > 0) {
                volMom = (recentVolAvg - histVolAvg) / histVolAvg;
            }
        }

        // 3. RSI Divergence
        double[] rsi = calculateRSI(candles, 14);
        double rsiDiv = 0.0;
        if (rsi.length >= 6) {
            double pCurr = candles.get(n - 1).getClose();
            double pPrev = candles.get(n - 6).getClose();
            double rsiCurr = rsi[n - 1];
            double rsiPrev = rsi[n - 6];

            if (pCurr < pPrev && rsiCurr > rsiPrev) {
                rsiDiv = 1.0; // Bullish divergence
            } else if (pCurr > pPrev && rsiCurr < rsiPrev) {
                rsiDiv = -1.0; // Bearish divergence
            }
        }

        return (roc6 * 0.5) + (volMom * 0.3) + (rsiDiv * 0.2);
    }

    public static double[] calculateRSI(List<OHLC> candles, int period) {
        if (candles == null || candles.size() <= period) {
            return new double[candles == null ? 0 : candles.size()];
        }
        int n = candles.size();
        double[] rsi = new double[n];
        double[] gains = new double[n];
        double[] losses = new double[n];
        for (int i = 1; i < n; i++) {
            double diff = candles.get(i).getClose() - candles.get(i - 1).getClose();
            if (diff > 0) {
                gains[i] = diff;
                losses[i] = 0;
            } else {
                gains[i] = 0;
                losses[i] = -diff;
            }
        }
        double avgGain = 0;
        double avgLoss = 0;
        for (int i = 1; i <= period; i++) {
            avgGain += gains[i];
            avgLoss += losses[i];
        }
        avgGain /= period;
        avgLoss /= period;
        if (avgLoss == 0) rsi[period] = 100;
        else rsi[period] = 100 - (100 / (1 + avgGain / avgLoss));

        for (int i = period + 1; i < n; i++) {
            avgGain = (avgGain * (period - 1) + gains[i]) / period;
            avgLoss = (avgLoss * (period - 1) + losses[i]) / period;
            if (avgLoss == 0) rsi[i] = 100;
            else rsi[i] = 100 - (100 / (1 + avgGain / avgLoss));
        }
        return rsi;
    }
}
