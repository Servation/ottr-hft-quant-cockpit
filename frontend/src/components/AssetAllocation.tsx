/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React from 'react';
import { MarketAsset, PortfolioSnapshot } from '../types';
import { ShieldCheck, ShieldAlert, ArrowRight, Wallet } from 'lucide-react';

interface AssetAllocationProps {
  strategy: 'DD90/10' | 'AdaptiveTrend';
  marketPrices: Record<string, MarketAsset>;
  paperEquity: number;
  portfolioSnapshot: PortfolioSnapshot | null;
  lang: 'en' | 'ru';
  t: any;
}

export default function AssetAllocation({ strategy, marketPrices, paperEquity, portfolioSnapshot, lang, t }: AssetAllocationProps) {
  // Determine percentages based on strategy
  const isDD = strategy === 'DD90/10';
  
  // Actual holdings from live gateway if available
  const hasLiveHoldings = !!(portfolioSnapshot && portfolioSnapshot.holdings);
  
  // Base quantities and values
  let btcQty = 0;
  let ethQty = 0;
  let altsVal = 0;
  let cashVal = portfolioSnapshot ? portfolioSnapshot.cash : (paperEquity * 0.10); // default 10% cash in sim
  
  if (hasLiveHoldings && portfolioSnapshot && portfolioSnapshot.holdings) {
    const getQty = (val: any) => (typeof val === 'object' && val !== null ? (val.quantity || 0) : Number(val || 0));

    btcQty = getQty(portfolioSnapshot.holdings["BTC"]) || getQty(portfolioSnapshot.holdings["BTCUSDT"]);
    ethQty = getQty(portfolioSnapshot.holdings["ETH"]) || getQty(portfolioSnapshot.holdings["ETHUSDT"]);
    
    // Sum up values of all other holdings as altcoins
    Object.keys(portfolioSnapshot.holdings).forEach((symbolKey) => {
      if (!symbolKey.startsWith("BTC") && !symbolKey.startsWith("ETH")) {
        const qty = getQty(portfolioSnapshot.holdings![symbolKey]);
        const cleanSymbol = symbolKey.replace("USDT", "");
        const price = portfolioSnapshot.currentPrices?.[symbolKey] || marketPrices[cleanSymbol]?.price || 0;
        altsVal += qty * price;
      }
    });
  }
  
  const btcPrice = portfolioSnapshot?.currentPrices?.["BTC"] || portfolioSnapshot?.currentPrices?.["BTCUSDT"] || marketPrices.BTC?.price || 88100;
  const ethPrice = portfolioSnapshot?.currentPrices?.["ETH"] || portfolioSnapshot?.currentPrices?.["ETHUSDT"] || marketPrices.ETH?.price || 3450;
  
  const btcAllocValue = hasLiveHoldings ? (btcQty * btcPrice) : ((paperEquity * (isDD ? 90.0 : 60.0)) / 100);
  const ethAllocValue = hasLiveHoldings ? (ethQty * ethPrice) : ((paperEquity * (isDD ? 7.0 : 25.0)) / 100);
  const altsAllocValue = hasLiveHoldings ? altsVal : ((paperEquity * (isDD ? 3.0 : 15.0)) / 100);
  
  // Total equity
  const totalVal = hasLiveHoldings && portfolioSnapshot ? portfolioSnapshot.equity : (btcAllocValue + ethAllocValue + altsAllocValue + cashVal);
  
  // Percent fractions
  const btcAllocPct = totalVal > 0 ? Number(((btcAllocValue / totalVal) * 100).toFixed(1)) : 0;
  const ethAllocPct = totalVal > 0 ? Number(((ethAllocValue / totalVal) * 100).toFixed(1)) : 0;
  const altsAllocPct = totalVal > 0 ? Number(((altsAllocValue / totalVal) * 100).toFixed(1)) : 0;
  const cashPct = totalVal > 0 ? Number(((cashVal / totalVal) * 100).toFixed(1)) : 0;



  // Let's format money nicely
  const formatCurrency = (val: number) => {
    return new Intl.NumberFormat(lang === 'en' ? 'en-US' : 'ru-RU', {
      style: 'currency',
      currency: 'USD',
      maximumFractionDigits: 0,
    }).format(val);
  };

  // Coordinates for the Donut Chart SVG
  // Radius r = 60. Circumference = 2 * pi * 60 = 376.99
  const r = 60;
  const circumference = 2 * Math.PI * r;

  // Segment strokeDasharray and strokeDashoffset computations
  const btcLength = (btcAllocPct / 100) * circumference;
  const ethLength = (ethAllocPct / 100) * circumference;
  const altsLength = (altsAllocPct / 100) * circumference;
  const cashLength = (cashPct / 100) * circumference;

  // Cumulative offset starting at -90deg rotation (12 o'clock)
  const btcOffset = 0;
  const ethOffset = btcLength;
  const altsOffset = btcLength + ethLength;
  const cashOffset = btcLength + ethLength + altsLength;

  return (
    <div id="allocation-widget" className="bg-neutral-900/30 border border-neutral-800 rounded p-6 shadow-2xl relative overflow-hidden transition-all duration-300 flex flex-col justify-between">
      
      {/* Visual Ambient Grid Backing */}
      <div className="absolute inset-x-0 bottom-0 h-48 bg-gradient-to-t from-violet-500/5 via-transparent to-transparent pointer-events-none" />

      {/* Primary Section title */}
      <div>
        <div className="flex items-center justify-between mb-4 relative z-10">
          <div className="flex items-center gap-2">
            <Wallet className="w-4 h-4 text-emerald-400" />
            <h3 className="font-sans font-medium text-white tracking-tight uppercase text-xs">
              {t.assetAllocation}
            </h3>
          </div>
          <span className="text-[10px] font-mono font-medium px-2 py-0.5 rounded bg-black/40 border border-neutral-800 text-neutral-450 uppercase tracking-widest flex items-center gap-1">
            <span className="w-1.5 h-1.5 bg-emerald-450 rounded-full animate-pulse" />
            {strategy === 'DD90/10' ? 'CORE PORTFOLIO' : strategy}
          </span>
        </div>

        {/* Short strategy brief */}
        {(isDD ? t.allocationStrategyDesc["DD90/10"] : t.allocationStrategyDesc["AdaptiveTrend"]) && (
          <p className="text-[11px] font-mono text-neutral-400 bg-black/40 border border-neutral-800 p-2.5 rounded mb-6 leading-relaxed">
            {isDD ? t.allocationStrategyDesc["DD90/10"] : t.allocationStrategyDesc["AdaptiveTrend"]}
          </p>
        )}

        {/* Visual Rings layout section */}
        <div className="grid grid-cols-1 sm:grid-cols-12 gap-6 items-center mb-6 relative z-10">
          
          {/* Circular donut mapping block */}
          <div className="sm:col-span-6 flex justify-center py-2 relative">
            <svg width="160" height="160" viewBox="0 0 160 160" className="-rotate-90">
              <defs>
                {/* Radial drop glow shadow filters */}
                <filter id="btc-glow" x="-10%" y="-10%" width="120%" height="120%">
                  <feDropShadow dx="0" dy="0" stdDeviation="2" floodColor="#10b981" floodOpacity="0.5" />
                </filter>
                <filter id="eth-glow" x="-10%" y="-10%" width="120%" height="120%">
                  <feDropShadow dx="0" dy="0" stdDeviation="2" floodColor="#f59e0b" floodOpacity="0.5" />
                </filter>
                <filter id="alts-glow" x="-10%" y="-10%" width="120%" height="120%">
                  <feDropShadow dx="0" dy="0" stdDeviation="2" floodColor="#8b5cf6" floodOpacity="0.7" />
                </filter>
                <filter id="cash-glow" x="-10%" y="-10%" width="120%" height="120%">
                  <feDropShadow dx="0" dy="0" stdDeviation="2" floodColor="#6b7280" floodOpacity="0.5" />
                </filter>
              </defs>

              {/* Base background circle */}
              <circle
                cx="80"
                cy="80"
                r={r}
                fill="none"
                stroke="#171717"
                strokeWidth="14"
              />

              {/* BTC Arc Segment */}
              <circle
                cx="80"
                cy="80"
                r={r}
                fill="none"
                stroke="#10b981"
                strokeWidth="14"
                strokeDasharray={`${btcLength} ${circumference}`}
                strokeDashoffset={-btcOffset}
                strokeLinecap="butt"
                filter="url(#btc-glow)"
                className="transition-all duration-1000 ease-in-out"
              />

              {/* ETH Arc Segment */}
              <circle
                cx="80"
                cy="80"
                r={r}
                fill="none"
                stroke="#f59e0b"
                strokeWidth="14"
                strokeDasharray={`${ethLength} ${circumference}`}
                strokeDashoffset={-ethOffset}
                strokeLinecap="butt"
                filter="url(#eth-glow)"
                className="transition-all duration-1000 ease-in-out"
              />

              {/* Alts Arc Segment */}
              <circle
                cx="80"
                cy="80"
                r={r}
                fill="none"
                stroke="#8b5cf6"
                strokeWidth="14"
                strokeDasharray={`${altsLength} ${circumference}`}
                strokeDashoffset={-altsOffset}
                strokeLinecap="butt"
                filter="url(#alts-glow)"
                className="transition-all duration-1000 ease-in-out"
              />

              {/* Cash Arc Segment */}
              <circle
                cx="80"
                cy="80"
                r={r}
                fill="none"
                stroke="#6b7280"
                strokeWidth="14"
                strokeDasharray={`${cashLength} ${circumference}`}
                strokeDashoffset={-cashOffset}
                strokeLinecap="butt"
                filter="url(#cash-glow)"
                className="transition-all duration-1000 ease-in-out"
              />
            </svg>

            {/* Absolute overlay values printed dead center */}
            <div className="absolute inset-0 flex flex-col justify-center items-center pointer-events-none select-none">
              <span className="text-[9px] font-mono tracking-widest text-neutral-400 uppercase">{lang === 'en' ? 'PORTFOLIO' : 'ПОРТФЕЛЬ'}</span>
              <span className="text-base font-semibold text-white tracking-tight mt-0.5">{formatCurrency(totalVal)}</span>
            </div>
          </div>

          {/* Allocation Legends and asset value breakdowns list */}
          <div className="sm:col-span-6 space-y-4">
            
            {/* BTC Core item */}
            <div className="group space-y-1">
              <div className="flex items-center justify-between text-xs font-mono">
                <span className="flex items-center gap-1.5 text-neutral-300">
                  <span className="w-2 hover:scale-125 h-2 bg-emerald-500 rounded" />
                  {lang === 'en' ? 'BTC Core' : 'Биткоин Core'}
                </span>
                <span className="text-white font-medium">{btcAllocPct}%</span>
              </div>
              <div className="w-full bg-neutral-950 h-1.5 rounded overflow-hidden border border-neutral-900">
                <div 
                  className="bg-emerald-500 h-full rounded transition-all duration-1000 ease-out" 
                  style={{ width: `${btcAllocPct}%` }}
                />
              </div>
              <div className="text-[10px] font-mono text-neutral-500 text-right">
                {formatCurrency(btcAllocValue)} <span className="text-neutral-600">({(btcAllocValue / (marketPrices.BTC?.price || 88100)).toFixed(3)} BTC)</span>
              </div>
            </div>

            {/* ETH Ledger item */}
            <div className="group space-y-1">
              <div className="flex items-center justify-between text-xs font-mono">
                <span className="flex items-center gap-1.5 text-neutral-300">
                  <span className="w-2 hover:scale-125 h-2 bg-amber-500 rounded" />
                  {lang === 'en' ? 'ETH Ledger' : 'Эфириум Ledger'}
                </span>
                <span className="text-white font-medium">{ethAllocPct}%</span>
              </div>
              <div className="w-full bg-neutral-950 h-1.5 rounded overflow-hidden border border-neutral-900">
                <div 
                  className="bg-amber-500 h-full rounded transition-all duration-1000 ease-out" 
                  style={{ width: `${ethAllocPct}%` }}
                />
              </div>
              <div className="text-[10px] font-mono text-neutral-500 text-right">
                {formatCurrency(ethAllocValue)} <span className="text-neutral-600">({(ethAllocValue / (marketPrices.ETH?.price || 3450)).toFixed(3)} ETH)</span>
              </div>
            </div>

            {/* Altcoin Reserves item */}
            <div className="group space-y-1">
              <div className="flex items-center justify-between text-xs font-mono">
                <span className="flex items-center gap-1.5 text-neutral-300">
                  <span className="w-2 hover:scale-125 h-2 bg-violet-500 rounded" />
                  {lang === 'en' ? 'Altcoin Reserve' : 'Резерв Альтов'}
                </span>
                <span className="text-white font-medium">{altsAllocPct}%</span>
              </div>
              <div className="w-full bg-neutral-950 h-1.5 rounded overflow-hidden border border-neutral-900">
                <div 
                  className="bg-violet-500 h-full rounded transition-all duration-1000 ease-out" 
                  style={{ width: `${altsAllocPct}%` }}
                />
              </div>
              <div className="text-[10px] font-mono text-neutral-500 text-right">
                {formatCurrency(altsAllocValue)}
              </div>
            </div>

            {/* Cash reserve item */}
            <div className="group space-y-1">
              <div className="flex items-center justify-between text-xs font-mono">
                <span className="flex items-center gap-1.5 text-neutral-300">
                  <span className="w-2 hover:scale-125 h-2 bg-neutral-500 rounded" />
                  {lang === 'en' ? 'Cash Balance' : 'Баланс Наличных'}
                </span>
                <span className="text-white font-medium">{cashPct}%</span>
              </div>
              <div className="w-full bg-neutral-950 h-1.5 rounded overflow-hidden border border-neutral-900">
                <div 
                  className="bg-neutral-500 h-full rounded transition-all duration-1000 ease-out" 
                  style={{ width: `${cashPct}%` }}
                />
              </div>
              <div className="text-[10px] font-mono text-neutral-500 text-right">
                {formatCurrency(cashVal)}
              </div>
            </div>

          </div>

        </div>
      </div>

      {/* Safety auditing status bar removed as requested */}

    </div>
  );
}
