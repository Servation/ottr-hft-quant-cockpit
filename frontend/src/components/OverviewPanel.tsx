/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useRef } from 'react';
import { ChartDataPoint, PerformanceMetrics } from '../types';
import { TrendingUp, Coins, DollarSign, Activity, Scale, TrendingDown } from 'lucide-react';

interface OverviewPanelProps {
  data: ChartDataPoint[];
  lang: 'en' | 'ru';
  t: any;
  performance?: PerformanceMetrics | null;
}

export default function OverviewPanel({ data, lang, t, performance }: OverviewPanelProps) {
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);
  const [timeframe, setTimeframe] = useState<'SEC' | 'MIN' | 'HOUR' | 'DAY' | 'WEEK'>('SEC');
  const containerRef = useRef<HTMLDivElement>(null);

  if (data.length === 0) return null;

  // Calculate current stats based on master data
  const currentPoint = data[data.length - 1];
  const initialPoint = data[0];
  const totalChange = currentPoint.equity - initialPoint.equity;
  const pctChange = (totalChange / initialPoint.equity) * 100;
  const isPositive = totalChange >= 0;

  // Dynamic grouping and downsampling based on selected timeframe
  const getAggregatedData = (): (ChartDataPoint & { displayTime: string; ts: number })[] => {
    // Spacing fallback if ticks don't have timestamp
    const ticksWithTs = data.map((pt, idx) => ({
      ...pt,
      ts: pt.timestamp ?? (Date.now() - (data.length - 1 - idx) * 4000)
    }));

    if (timeframe === 'SEC') {
      return ticksWithTs.slice(-60).map(pt => ({
        ...pt,
        displayTime: new Date(pt.ts).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
      }));
    }

    let intervalMs = 60000; // MIN
    let labelFormat: Intl.DateTimeFormatOptions = { hour: '2-digit', minute: '2-digit', hour12: false };
    let limit = 60;

    if (timeframe === 'MIN') {
      intervalMs = 60000;
      labelFormat = { hour: '2-digit', minute: '2-digit', hour12: false };
      limit = 60;
    } else if (timeframe === 'HOUR') {
      intervalMs = 3600000;
      labelFormat = { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false };
      limit = 24;
    } else if (timeframe === 'DAY') {
      intervalMs = 86400000;
      labelFormat = { month: '2-digit', day: '2-digit' };
      limit = 30;
    } else if (timeframe === 'WEEK') {
      intervalMs = 86400000 * 7;
      labelFormat = { month: '2-digit', day: '2-digit' };
      limit = 12;
    }

    const groups: Record<number, typeof ticksWithTs[0]> = {};
    ticksWithTs.forEach((pt) => {
      const key = Math.floor(pt.ts / intervalMs);
      groups[key] = pt; // use latest tick in group
    });

    const sortedKeys = Object.keys(groups).map(Number).sort((a, b) => a - b);
    const aggregated = sortedKeys.map((key) => {
      const pt = groups[key];
      return {
        ...pt,
        displayTime: new Date(pt.ts).toLocaleString(undefined, labelFormat)
      };
    });

    return aggregated.slice(-limit);
  };

  let displayData = getAggregatedData();
  // Safe fallback if only 1 data point is aggregated
  if (displayData.length === 1) {
    displayData = [
      { ...displayData[0], ts: displayData[0].ts - 1000, displayTime: '' },
      displayData[0]
    ];
  }

  // Min-Max values for SVG scaling based on aggregated display points
  const equityVals = displayData.map((d) => d.equity);
  const cashVals = displayData.map((d) => d.cash);
  const allVals = [...equityVals, ...cashVals];
  
  const rawMinY = Math.min(...allVals);
  const rawMaxY = Math.max(...allVals);
  const rangeY = rawMaxY - rawMinY || 10000;
  
  // Padding on top and bottom of chart grid space
  const minY = Math.max(0, rawMinY - rangeY * 0.08);
  const maxY = rawMaxY + rangeY * 0.08;
  const scaleY = maxY - minY || 10000;

  // Chart view dimensions in viewBox coordinate space
  const width = 800;
  const height = 350; // Increased height to 350px
  const paddingX = 55;
  const paddingY = 25; // 25px top and bottom padding
  const chartW = width - paddingX * 2;
  const chartH = height - paddingY * 2;

  // Convert coordinate logic
  const getX = (index: number) => {
    if (displayData.length <= 1) return paddingX;
    return paddingX + (index / (displayData.length - 1)) * chartW;
  };

  const getY = (val: number) => {
    return paddingY + chartH - ((val - minY) / scaleY) * chartH;
  };

  // Generate SVG path generators
  let equityPath = '';
  let cashPath = '';
  let equityAreaPath = '';
  let cashAreaPath = '';

  if (displayData.length > 0) {
    const eq0_X = getX(0);
    const eq0_Y = getY(displayData[0].equity);
    const cs0_Y = getY(displayData[0].cash);

    let eqPoints = `M ${eq0_X} ${eq0_Y}`;
    let csPoints = `M ${eq0_X} ${cs0_Y}`;

    for (let i = 1; i < displayData.length; i++) {
      eqPoints += ` L ${getX(i)} ${getY(displayData[i].equity)}`;
      csPoints += ` L ${getX(i)} ${getY(displayData[i].cash)}`;
    }

    equityPath = eqPoints;
    cashPath = csPoints;

    // Connect to baseline for area fills
    const baselineY = paddingY + chartH;
    equityAreaPath = `${eqPoints} L ${getX(displayData.length - 1)} ${baselineY} L ${paddingX} ${baselineY} Z`;
    cashAreaPath = `${csPoints} L ${getX(displayData.length - 1)} ${baselineY} L ${paddingX} ${baselineY} Z`;
  }

  // Handle Mouse Hover Interactions and Crosshair Calculation
  const handleMouseMove = (e: React.MouseEvent<SVGSVGElement, MouseEvent>) => {
    if (!containerRef.current) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const xViewBox = ((e.clientX - rect.left) / rect.width) * width;
    const xRatio = (xViewBox - paddingX) / chartW;
    const approxIndex = Math.round(xRatio * (displayData.length - 1));
    const finalIndex = Math.max(0, Math.min(displayData.length - 1, approxIndex));
    setHoverIndex(finalIndex);
  };

  const handleMouseLeave = () => {
    setHoverIndex(null);
  };

  // Grid configuration
  const gridCount = 5;

  // Selected or active index reference points (defaults to last tick on default state)
  const activeIndex = hoverIndex !== null ? hoverIndex : displayData.length - 1;
  const activePt = displayData[activeIndex];

  const formatCurrency = (val: number) => {
    return new Intl.NumberFormat(lang === 'en' ? 'en-US' : 'ru-RU', {
      style: 'currency',
      currency: 'USD',
      maximumFractionDigits: 0,
    }).format(val);
  };

  // Performance metric formatting. Values are fractions; null means not enough
  // history yet, rendered as an em dash rather than a misleading 0.
  const fmtPct = (v: number | null | undefined) =>
    v === null || v === undefined ? '—' : `${v >= 0 ? '+' : ''}${(v * 100).toFixed(2)}%`;
  const fmtRatio = (v: number | null | undefined) =>
    v === null || v === undefined ? '—' : v.toFixed(2);
  const hasPerf = !!(performance && performance.num_points >= 2);
  const alpha = performance?.alpha ?? null;
  const alphaPositive = (alpha ?? 0) >= 0;
  const maxDd = performance?.max_drawdown;

  return (
    <div id="overview-panel" className="bg-neutral-900/30 border border-neutral-800 rounded p-6 shadow-2xl space-y-6 relative overflow-hidden transition-all duration-300">
      
      {/* Absolute faint background glow grids */}
      <div className="absolute inset-0 bg-radial-at-t from-emerald-500/5 via-transparent to-transparent pointer-events-none" />

      {/* Grid Headers and Stats Blocks */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6 relative z-10">
        
        {/* Total Paper Equity Container */}
        <div id="metric-equity" className="bg-black/40 p-5 rounded border border-neutral-800 hover:border-emerald-500/40 transition-all duration-300 group">
          <div className="flex items-center justify-between mb-2">
            <span className="text-[10px] uppercase tracking-wider text-neutral-500 font-semibold flex items-center gap-1.5">
              <Activity className="w-3.5 h-3.5 text-emerald-500 animate-pulse" />
              {t.paperEquityLabel}
            </span>
            <span className={`text-[10px] font-mono font-medium px-2 py-0.5 rounded ${isPositive ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' : 'bg-rose-500/10 text-rose-400 border border-rose-500/20'}`}>
              {isPositive ? '+' : ''}{pctChange.toFixed(3)}%
            </span>
          </div>
          <div className="flex items-baseline gap-2">
            <span className="text-3xl font-light text-white tracking-tight group-hover:text-emerald-400 transition-colors duration-300">
              {formatCurrency(currentPoint.equity)}
            </span>
          </div>
          <div className="mt-2 text-[10px] font-mono text-neutral-500">
            {lang === 'en' ? 'Live delta: ' : 'Живое отклонение: '}
            <span className={isPositive ? 'text-emerald-400' : 'text-rose-400'}>
              {formatCurrency(totalChange)}
            </span>
          </div>
        </div>

        {/* Unleveraged Cash Reserve Container */}
        <div id="metric-cash" className="bg-black/40 p-5 rounded border border-neutral-800 hover:border-blue-500/40 transition-all duration-300 group">
          <div className="flex items-center justify-between mb-2">
            <span className="text-[10px] uppercase tracking-wider text-neutral-500 font-semibold flex items-center gap-1.5">
              <Coins className="w-3.5 h-3.5 text-blue-500" />
              {t.cashBalanceLabel}
            </span>
            <span className="text-[10px] font-mono text-blue-400 font-medium">
              {((currentPoint.cash / currentPoint.equity) * 100).toFixed(1)}% RATIO
            </span>
          </div>
          <div className="flex items-baseline gap-2">
            <span className="text-3xl font-light text-neutral-300 tracking-tight group-hover:text-blue-400 transition-colors duration-300">
              {formatCurrency(currentPoint.cash)}
            </span>
          </div>
          <div className="mt-2 text-[10px] font-mono text-neutral-500">
            {lang === 'en' ? 'Allocated capacity: ' : 'Задействованный капитал: '}
            <span className="text-neutral-400">{formatCurrency(currentPoint.equity - currentPoint.cash)}</span>
          </div>
        </div>

      </div>

      {/* Risk-adjusted performance vs buy-and-hold BTC (M1). Hidden until the
          bridge equity curve has at least two samples. */}
      {hasPerf && (
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 relative z-10">

          {/* Return vs BTC HODL (with alpha) */}
          <div className="bg-black/40 p-4 rounded border border-neutral-800 hover:border-emerald-500/40 transition-all duration-300">
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-[10px] uppercase tracking-wider text-neutral-500 font-semibold flex items-center gap-1.5">
                <TrendingUp className="w-3.5 h-3.5 text-emerald-500" />
                {lang === 'en' ? 'Return vs BTC HODL' : 'Доходность к BTC HODL'}
              </span>
              <span className={`text-[10px] font-mono font-medium px-2 py-0.5 rounded ${alphaPositive ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' : 'bg-rose-500/10 text-rose-400 border border-rose-500/20'}`}>
                {fmtPct(alpha)} α
              </span>
            </div>
            <span className="text-xl font-light text-white tracking-tight">{fmtPct(performance?.total_return)}</span>
            <div className="mt-1 text-[10px] font-mono text-neutral-500">
              {lang === 'en' ? 'BTC HODL: ' : 'BTC HODL: '}
              <span className="text-amber-400">{fmtPct(performance?.benchmark_return)}</span>
            </div>
          </div>

          {/* Sharpe (with Sortino subtext) */}
          <div className="bg-black/40 p-4 rounded border border-neutral-800 hover:border-blue-500/40 transition-all duration-300">
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-[10px] uppercase tracking-wider text-neutral-500 font-semibold flex items-center gap-1.5">
                <Scale className="w-3.5 h-3.5 text-blue-500" />
                {lang === 'en' ? 'Sharpe Ratio' : 'Коэф. Шарпа'}
              </span>
            </div>
            <span className="text-xl font-light text-white tracking-tight">{fmtRatio(performance?.sharpe)}</span>
            <div className="mt-1 text-[10px] font-mono text-neutral-500">
              {lang === 'en' ? 'Sortino: ' : 'Сортино: '}
              <span className="text-neutral-400">{fmtRatio(performance?.sortino)}</span>
            </div>
          </div>

          {/* Max Drawdown */}
          <div className="bg-black/40 p-4 rounded border border-neutral-800 hover:border-rose-500/40 transition-all duration-300">
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-[10px] uppercase tracking-wider text-neutral-500 font-semibold flex items-center gap-1.5">
                <TrendingDown className="w-3.5 h-3.5 text-rose-500" />
                {lang === 'en' ? 'Max Drawdown' : 'Макс. Просадка'}
              </span>
            </div>
            <span className="text-xl font-light text-white tracking-tight">
              {maxDd === null || maxDd === undefined ? '—' : `-${(maxDd * 100).toFixed(2)}%`}
            </span>
            <div className="mt-1 text-[10px] font-mono text-neutral-500">
              {(performance?.num_points ?? 0)} {lang === 'en' ? 'samples' : 'точек'}
            </div>
          </div>

        </div>
      )}

      {/* SVG Multi-Layer Chart Block */}
      <div className="relative mt-4 bg-black/40 p-4 rounded border border-neutral-800" ref={containerRef}>
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 mb-4">
          <div className="space-y-1">
            <span className="text-[10px] uppercase tracking-wider text-neutral-400 font-bold flex items-center gap-2">
              <span className="flex h-2 w-2 rounded-full bg-emerald-500 animate-ping" />
              {lang === 'en' ? 'ALGORITHMIC EQUITY PERFORMANCE GRAPH' : 'ГРАФИК ДОХОДНОСТИ КВАНТОВОГО КАПИТАЛА'}
            </span>
            <div className="flex items-center gap-4 text-[10px] font-mono">
              <div className="flex items-center gap-1.5">
                <span className="w-3 h-0.5 bg-emerald-500 inline-block" />
                <span className="text-neutral-400">{t.paperEquityLabel}</span>
              </div>
              <div className="flex items-center gap-1.5">
                <span className="w-3 h-0.5 bg-blue-500 inline-block text-dashed" />
                <span className="text-neutral-400">{t.cashBalanceLabel}</span>
              </div>
            </div>
          </div>
          
          {/* Timeframe pill selector group */}
          <div className="flex items-center gap-0.5 bg-black/50 p-0.5 rounded border border-neutral-800 self-start sm:self-center shadow-[0_0_15px_rgba(16,185,129,0.02)]">
            {(['SEC', 'MIN', 'HOUR', 'DAY', 'WEEK'] as const).map((tf) => (
              <button
                key={tf}
                onClick={() => setTimeframe(tf)}
                className={`px-2.5 py-1 text-[9px] font-mono font-bold tracking-wider rounded transition-all duration-200 ${
                  timeframe === tf
                    ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/35 shadow-[0_0_10px_rgba(16,185,129,0.15)]'
                    : 'text-neutral-500 hover:text-neutral-300 border border-transparent'
                }`}
              >
                {tf}
              </button>
            ))}
          </div>
        </div>

        {/* Responsive Vector Engine */}
        <div className="relative h-[350px] w-full">
          <svg
            className="w-full h-full cursor-crosshair overflow-visible"
            viewBox={`0 0 ${width} ${height}`}
            preserveAspectRatio="none"
            onMouseMove={handleMouseMove}
            onMouseLeave={handleMouseLeave}
          >
            {/* Defined Gradients and Filters for Aesthetic Glows */}
            <defs>
              <linearGradient id="eqGlow" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#10b981" stopOpacity="0.18" />
                <stop offset="100%" stopColor="#10b981" stopOpacity="0.00" />
              </linearGradient>
              <linearGradient id="csGlow" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#3b82f6" stopOpacity="0.12" />
                <stop offset="100%" stopColor="#3b82f6" stopOpacity="0.00" />
              </linearGradient>
              <filter id="drop-glow" x="-20%" y="-20%" width="140%" height="140%">
                <feDropShadow dx="0" dy="2" stdDeviation="4" floodColor="#10b981" floodOpacity="0.3" />
              </filter>
            </defs>

            {/* Render Grid Overlay */}
            <g className="text-neutral-800 opacity-60">
              {/* Y Grid lines */}
              {[minY, minY + scaleY * 0.25, minY + scaleY * 0.5, minY + scaleY * 0.75, maxY].map((val, i) => {
                const y = getY(val);
                return (
                  <g key={`grid-y-${i}`}>
                    <line
                      x1={paddingX}
                      y1={y}
                      x2={width - paddingX}
                      y2={y}
                      stroke="currentColor"
                      strokeWidth="1"
                      strokeDasharray="4 6"
                    />
                    <text
                      x={paddingX - 10}
                      y={y + 3}
                      textAnchor="end"
                      className="text-[9px] font-mono fill-neutral-500"
                    >
                      {formatCurrency(val)}
                    </text>
                  </g>
                );
              })}

              {/* X Grid lines and text labels */}
              {Array.from({ length: gridCount + 1 }).map((_, i) => {
                const idx = Math.round((i / gridCount) * (displayData.length - 1));
                if (idx < 0 || idx >= displayData.length) return null;
                const x = getX(idx);
                const pt = displayData[idx];
                return (
                  <g key={`grid-x-${i}`}>
                    <line
                      x1={x}
                      y1={paddingY}
                      x2={x}
                      y2={height - paddingY}
                      stroke="currentColor"
                      strokeWidth="1"
                      strokeDasharray="2 4"
                    />
                    <text
                      x={x}
                      y={height - 5}
                      textAnchor="middle"
                      className="text-[9px] font-mono fill-neutral-500"
                    >
                      {pt.displayTime}
                    </text>
                  </g>
                );
              })}
            </g>

            {/* Render Cash Path Layers (underneath Equity) */}
            {cashPath && (
              <>
                <path
                  d={cashAreaPath}
                  fill="url(#csGlow)"
                  className="transition-all duration-300"
                />
                <path
                  d={cashPath}
                  fill="none"
                  stroke="#3b82f6"
                  strokeWidth="1.5"
                  strokeDasharray="3 3 animate-dash"
                  className="transition-all duration-300"
                />
              </>
            )}

            {/* Render Equity Path Layers (main glowing asset line) */}
            {equityPath && (
              <>
                <path
                  d={equityAreaPath}
                  fill="url(#eqGlow)"
                  className="transition-all duration-300"
                />
                <path
                  d={equityPath}
                  fill="none"
                  stroke="#10b981"
                  strokeWidth="2.5"
                  filter="url(#drop-glow)"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  className="transition-all duration-300"
                />
              </>
            )}

            {/* Interactive Cursor overlay */}
            {hoverIndex !== null && hoverIndex < displayData.length && (
              <g>
                {/* Vertical slider */}
                <line
                  x1={getX(hoverIndex)}
                  y1={paddingY}
                  x2={getX(hoverIndex)}
                  y2={height - paddingY}
                  stroke="#525252"
                  strokeWidth="1.5"
                />

                {/* Tracking Dots */}
                <circle
                  cx={getX(hoverIndex)}
                  cy={getY(displayData[hoverIndex].equity)}
                  r="5"
                  fill="#10b981"
                  stroke="#0a0a0a"
                  strokeWidth="1.5"
                />
                <circle
                  cx={getX(hoverIndex)}
                  cy={getY(displayData[hoverIndex].cash)}
                  r="4"
                  fill="#3b82f6"
                  stroke="#0a0a0a"
                  strokeWidth="1.5"
                />
              </g>
            )}
          </svg>
        </div>

        {/* Tracking Legend Console readout (anchored or responding to hover index) */}
        <div className="flex flex-wrap items-center justify-between gap-2 mt-2 pt-2 border-t border-neutral-900 text-xs font-mono">
          <div className="flex gap-4">
            <div>
              <span className="text-neutral-500 uppercase">{lang === 'en' ? 'TIME INTERVAL' : 'ТАКТОВЫЙ ШАГ'}:</span>{' '}
              <span className="text-neutral-200">{activePt.displayTime || activePt.timeLabel}</span>
            </div>
            <div>
              <span className="text-neutral-500 uppercase">{t.paperEquityLabel}:</span>{' '}
              <span className="text-emerald-400 font-semibold">{formatCurrency(activePt.equity)}</span>
            </div>
            <div>
              <span className="text-neutral-500 uppercase">{t.cashBalanceLabel}:</span>{' '}
              <span className="text-blue-400 font-semibold">{formatCurrency(activePt.cash)}</span>
            </div>
          </div>
          <div className="text-[10px] text-neutral-500 text-right italic">
            {hoverIndex !== null 
              ? (lang === 'en' ? '📌 INSIGHT: Reading point index ' + hoverIndex : '📌 НАБЛЮДЕНИЕ: Извлечение индекса сектора ' + hoverIndex)
              : (lang === 'en' ? 'ℹ️ Drag cursor across line to query snapshot parameters' : 'ℹ️ Проведите курсор над графиком для запроса значений')}
          </div>
        </div>

      </div>
    </div>
  );
}
