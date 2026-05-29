/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import { ChartDataPoint, ExecutionLogEntry, MarketAsset, TradingAgentState } from '../types';

// Helper to format timestamps gracefully
export function getFormattedTime(date: Date = new Date()): string {
  return date.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    second: '2-digit',
    hour12: true
  });
}

// Generate starting array of 60 points of historical data
export function generateHistoricalPoints(startingBalance: number = 100000): ChartDataPoint[] {
  const points: ChartDataPoint[] = [];
  let currentEquity = startingBalance * 0.985;
  let currentCash = currentEquity * 0.9;
  
  const now = new Date();
  
  for (let i = 59; i >= 0; i--) {
    const ptDate = new Date(now.getTime() - i * 60000); // 1 minute intervals
    // Create random walk with general upward trend
    const drift = (startingBalance * 0.015) / 60;
    const noise = (Math.random() - 0.48) * (startingBalance * 0.005);
    currentEquity += drift + noise;
    
    // Cash balance lags or fluctuates with allocation adjustments
    const cashSkew = (Math.random() - 0.5) * (startingBalance * 0.002);
    // Maintain roughly 90% allocation
    currentCash = currentEquity * (0.89 + Math.random() * 0.012) + cashSkew;
    
    // Format timestamp as interactive label
    const timeLabel = ptDate.toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
      hour12: true
    });
    points.push({
      timeLabel,
      equity: Math.round(currentEquity),
      cash: Math.round(currentCash),
      timestamp: ptDate.getTime()
    });
  }
  return points;
}

function pad(num: number): string {
  return String(num).padStart(2, '0');
}

// Pre-packaged starting list of 10 execution logs
export function generateInitialLogs(): ExecutionLogEntry[] {
  const symbols = ['BTC', 'ETH', 'SOL', 'BNB'];
  const actions: ('BUY' | 'SELL' | 'HOLD')[] = ['BUY', 'SELL', 'BUY', 'SELL', 'BUY', 'BUY', 'SELL', 'BUY', 'SELL', 'BUY'];
  const basePrices: Record<string, number> = { BTC: 88200, ETH: 3450, SOL: 165, BNB: 580 };
  
  const list: ExecutionLogEntry[] = [];
  const now = new Date();
  
  for (let i = 0; i < 10; i++) {
    const timestamp = getFormattedTime(new Date(now.getTime() - (10 - i) * 15000));
    const symbol = symbols[i % symbols.length];
    const action = actions[i % actions.length];
    const latencyMs = Math.round(4.8 + Math.random() * 32.5 * (1 + (i % 3) * 0.2));
    
    const arrivalPrice = basePrices[symbol] * (1 + (Math.random() - 0.5) * 0.006);
    const slipFactor = (Math.random() * 0.0004) + 0.0001; 
    const isBuy = action === 'BUY';
    const vwapFillPrice = arrivalPrice * (1 + (isBuy ? slipFactor : -slipFactor));
    const slippagePct = slipFactor * 100;
    
    // calculate fee based on dynamic nominal size of ~ $20,000
    const size = Number((20000 / arrivalPrice).toFixed(4));
    const feeUsd = (arrivalPrice * size) * 0.0006; // 6 bps fee
    
    list.push({
      id: `FILL-${1000 + i}`,
      timestamp,
      symbol,
      action,
      quantity: size,
      latencyMs,
      arrivalPrice: Number(arrivalPrice.toFixed(symbol === 'BTC' || symbol === 'ETH' || symbol === 'BNB' || symbol === 'SOL' ? 2 : 4)),
      vwapFillPrice: Number(vwapFillPrice.toFixed(symbol === 'BTC' || symbol === 'ETH' || symbol === 'BNB' || symbol === 'SOL' ? 2 : 4)),
      slippagePct: Number(slippagePct.toFixed(4)),
      feeUsd: Number(feeUsd.toFixed(2))
    });
  }
  return list;
}

// Standard trading log generator
export function createNewLogEntry(
  symbol: string,
  action: 'BUY' | 'SELL' | 'HOLD',
  currentPrice: number
): ExecutionLogEntry {
  const timestamp = getFormattedTime(new Date());
  const latencyMs = Number((3.6 + Math.random() * 18.4).toFixed(2));
  
  const slipFactor = (Math.random() * 0.0002) + 0.0001;
  const isBuy = action === 'BUY';
  const vwapFillPrice = currentPrice * (1 + (isBuy ? slipFactor : -slipFactor));
  const slippagePct = slipFactor * 100;
  
  // calculate fee based on dynamic nominal size of ~ $25,000
  const size = Number((25000 / currentPrice).toFixed(4));
  const feeUsd = (currentPrice * size) * 0.0006;
  
  return {
    id: `FILL-${Math.round(1000 + Date.now() % 9000)}`,
    timestamp,
    symbol,
    action,
    quantity: size,
    latencyMs,
    arrivalPrice: Number(currentPrice.toFixed(symbol === 'BTC' || symbol === 'ETH' || symbol === 'BNB' || symbol === 'SOL' ? 2 : 4)),
    vwapFillPrice: Number(vwapFillPrice.toFixed(symbol === 'BTC' || symbol === 'ETH' || symbol === 'BNB' || symbol === 'SOL' ? 2 : 4)),
    slippagePct: Number(slippagePct.toFixed(4)),
    feeUsd: Number(feeUsd.toFixed(2))
  };
}
