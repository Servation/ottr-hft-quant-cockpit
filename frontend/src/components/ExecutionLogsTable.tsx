/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import { ExecutionLogEntry } from '../types';
import { ListCollapse, ChevronRight, ChevronDown, Cpu, HelpCircle, ArrowUpRight, ArrowDownRight, Zap } from 'lucide-react';

interface ExecutionLogsTableProps {
  logs: ExecutionLogEntry[];
  lang: 'en' | 'ru';
  t: any;
  onSelectTransaction?: (id: string) => void;
}

export default function ExecutionLogsTable({ logs, lang, t, onSelectTransaction }: ExecutionLogsTableProps) {
  const [expandedLogIds, setExpandedLogIds] = useState<Record<string, boolean>>({});

  const toggleExpand = (id: string) => {
    setExpandedLogIds(prev => ({ ...prev, [id]: !prev[id] }));
  };

  // Sort logs by timestamp descending so the newest entry is always on top
  const sortedLogs = [...logs].reverse();

  // Helper formatting currencies
  const formatValue = (num: number, symbol: string) => {
    const isSpy = symbol === 'SPY';
    const limitDigits = isSpy ? 2 : symbol === 'ETH' ? 2 : 1;
    return new Intl.NumberFormat(lang === 'en' ? 'en-US' : 'ru-RU', {
      style: 'currency',
      currency: 'USD',
      maximumFractionDigits: limitDigits,
    }).format(num);
  };
  
  const formatUsdSimple = (num: number) => {
    return new Intl.NumberFormat(lang === 'en' ? 'en-US' : 'ru-RU', {
      style: 'currency',
      currency: 'USD',
      maximumFractionDigits: 2,
    }).format(num);
  };

  return (
    <div id="execution-logs" className="bg-neutral-900/30 border border-neutral-800 rounded p-6 shadow-2xl space-y-4 overflow-hidden relative transition-all duration-300">
      
      {/* Decorative top corner accent line */}
      <div className="absolute top-0 right-0 w-32 h-[1px] bg-gradient-to-l from-emerald-500/40 to-transparent" />

      {/* Header telemetry area */}
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <ListCollapse className="w-4 h-4 text-emerald-400" />
          <h3 className="font-sans font-medium text-white tracking-tight uppercase text-xs">
            {t.executionFillsLog}
          </h3>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-mono text-neutral-500 uppercase">
            {lang === 'en' ? 'TRANSACTION BLOCK COUNT' : 'АКТИВНЫХ ОРДЕРОВ'}:
          </span>
          <span className="text-xs font-mono px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 font-bold">
            {logs.length}
          </span>
        </div>
      </div>

      {/* Table grid wrapper with vertical scroll constraints */}
      <div className="border border-neutral-800 rounded overflow-hidden bg-black/40 shadow-inner">
        <div className="overflow-x-auto overflow-y-auto max-h-[340px] min-w-full custom-scrollbar">
          <table className="min-w-full divide-y divide-neutral-900 text-xs font-mono text-neutral-300 relative">
            
            {/* Table heads */}
            <thead className="bg-[#121212] text-neutral-500 sticky top-0 z-10 uppercase tracking-wider text-[10px] border-b border-neutral-800">
              <tr>
                <th scope="col" className="px-4 py-3 text-left font-mono font-bold">{t.timestampCol}</th>
                <th scope="col" className="px-4 py-3 text-left font-mono font-bold">ID</th>
                <th scope="col" className="px-4 py-3 text-left font-mono font-bold">{t.symbolCol}</th>
                <th scope="col" className="px-4 py-3 text-center font-mono font-bold">{t.actionCol}</th>
                <th scope="col" className="px-4 py-3 text-right font-mono font-bold">{t.qtyCol}</th>
                <th scope="col" className="px-4 py-3 text-right font-mono font-bold">{t.latencyCol}</th>
                <th scope="col" className="px-4 py-3 text-right font-mono font-bold">{t.arrivalCol}</th>
                <th scope="col" className="px-4 py-3 text-right font-mono font-bold">{t.vwapCol}</th>
                <th scope="col" className="px-4 py-3 text-right font-mono font-bold">{t.slippageCol}</th>
                <th scope="col" className="px-4 py-3 text-right font-mono font-bold">{t.feeCol}</th>
              </tr>
            </thead>

            {/* List entries */}
            <tbody className="divide-y divide-neutral-900 bg-neutral-950/20">
              {sortedLogs.map((log, idx) => {
                const isBuy = log.action === 'BUY';
                const isHold = log.action === 'HOLD';
                const isFirstRow = idx === 0;
                const isExpanded = !!expandedLogIds[log.id];

                return (
                  <React.Fragment key={log.id}>
                    <tr 
                      className={`hover:bg-neutral-900/20 transition-colors ${
                        isFirstRow 
                          ? 'bg-emerald-500/5 animate-[pulse_2s_infinite]' 
                          : ''
                      }`}
                    >
                      
                      {/* Timestamp cell with status pulse and toggle button */}
                      <td className="px-4 py-3.5 whitespace-nowrap text-neutral-450">
                        <div className="flex items-center gap-1.5">
                          {log.reasoning && (
                            <button
                              onClick={() => toggleExpand(log.id)}
                              className="p-1 hover:bg-neutral-800 rounded transition-colors text-neutral-500 hover:text-neutral-300 focus:outline-none shrink-0"
                              title={lang === 'en' ? 'Toggle AI Reasoning' : 'Показать логику ИИ'}
                            >
                              {isExpanded ? (
                                <ChevronDown className="w-3.5 h-3.5 text-emerald-400" />
                              ) : (
                                <ChevronRight className="w-3.5 h-3.5" />
                              )}
                            </button>
                          )}
                          {isFirstRow ? (
                            <span className="relative flex h-1.5 w-1.5 shrink-0">
                              <span className="animate-ping absolute inline-flex h-full w-full rounded bg-emerald-400 opacity-75"></span>
                              <span className="relative inline-flex rounded h-1.5 w-1.5 bg-emerald-500"></span>
                            </span>
                          ) : (
                            !log.reasoning && <span className="w-1.5 h-1.5 rounded-full bg-neutral-800 shrink-0" />
                          )}
                          <span className={isFirstRow ? 'text-neutral-100 font-semibold' : ''}>
                            {log.timestamp}
                          </span>
                        </div>
                      </td>

                      {/* Transaction ID with click-to-query command trigger */}
                      <td className="px-4 py-3.5 whitespace-nowrap text-left font-mono">
                        {onSelectTransaction ? (
                          <button
                            onClick={() => onSelectTransaction(log.id)}
                            className="text-emerald-400 hover:text-emerald-300 font-bold focus:outline-none flex items-center gap-1 group text-left"
                            title={lang === 'en' ? 'Click to ask about this transaction' : 'Нажмите, чтобы спросить об этой сделке'}
                          >
                            <span className="border-b border-dashed border-emerald-500/40 group-hover:border-emerald-300 transition-colors">
                              {log.id}
                            </span>
                          </button>
                        ) : (
                          <span className="text-neutral-400">{log.id}</span>
                        )}
                      </td>

                      {/* Asset Symbol key */}
                      <td className="px-4 py-3.5 whitespace-nowrap font-bold text-neutral-200">
                        {log.symbol}
                      </td>

                      {/* Trade action action identifier split */}
                      <td className="px-4 py-3.5 whitespace-nowrap text-center">
                        {isBuy ? (
                          <span className="inline-flex items-center gap-0.5 px-2 py-0.5 rounded text-[9px] font-bold bg-emerald-500/10 text-emerald-400 border border-emerald-500/15">
                            <ArrowUpRight className="w-3 h-3" />
                            {lang === 'en' ? 'BUY' : 'ПОКУПКА'}
                          </span>
                        ) : isHold ? (
                          <span className="inline-flex items-center gap-0.5 px-2 py-0.5 rounded text-[9px] font-bold bg-neutral-800 text-neutral-400 border border-neutral-750">
                            {lang === 'en' ? 'HOLD' : 'УДЕРЖ.'}
                          </span>
                        ) : (
                          <span className="inline-flex items-center gap-0.5 px-2 py-0.5 rounded text-[9px] font-bold bg-rose-500/10 text-rose-400 border border-rose-500/15">
                            <ArrowDownRight className="w-3 h-3" />
                            {lang === 'en' ? 'SELL' : 'ПРОДАЖА'}
                          </span>
                        )}
                      </td>

                      {/* Quantity cell */}
                      <td className="px-4 py-3.5 whitespace-nowrap text-right text-neutral-350">
                        {log.quantity ? (
                          <div className="flex flex-col items-end">
                            <span className="font-semibold text-neutral-100">
                              {log.quantity.toFixed(6)} <span className="text-[10px] text-neutral-500 font-normal">{log.symbol.replace('USDT', '')}</span>
                            </span>
                            <span className="text-[10px] text-neutral-450 font-normal">
                              ~{formatUsdSimple(log.quantity * log.vwapFillPrice)}
                            </span>
                          </div>
                        ) : (
                          '0.000000'
                        )}
                      </td>

                      {/* Latency timing offset */}
                      <td className="px-4 py-3.5 whitespace-nowrap text-right">
                        <div className="flex items-center justify-end gap-1 font-semibold">
                          <Zap className={`w-3 h-3 ${log.latencyMs < 12 ? 'text-emerald-400' : 'text-teal-500'}`} />
                          <span className={log.latencyMs < 12 ? 'text-emerald-400' : 'text-neutral-300'}>
                            {log.latencyMs.toFixed(2)}
                          </span>
                          <span className="text-[10px] text-neutral-600 font-normal">ms</span>
                        </div>
                      </td>

                      {/* Arrival Benchmark quotation */}
                      <td className="px-4 py-3.5 whitespace-nowrap text-right text-neutral-350 font-medium">
                        {formatValue(log.arrivalPrice, log.symbol)}
                      </td>

                      {/* Executed VWAP pricing */}
                      <td className="px-4 py-3.5 whitespace-nowrap text-right font-medium text-neutral-250">
                        {formatValue(log.vwapFillPrice, log.symbol)}
                      </td>

                      {/* Execution tracking slippage deviation percent */}
                      <td className="px-4 py-3.5 whitespace-nowrap text-right">
                        <span className={`font-semibold ${log.slippagePct > 0.04 ? 'text-amber-400' : 'text-emerald-400'}`}>
                          {log.slippagePct.toFixed(4)}%
                        </span>
                      </td>

                      {/* Fee metric cell content */}
                      <td className="px-4 py-3.5 whitespace-nowrap text-right">
                        <div className="text-neutral-300">
                          ${log.feeUsd.toFixed(2)}
                        </div>
                      </td>

                    </tr>
                    {isExpanded && log.reasoning && (
                      <tr className="bg-neutral-950/40">
                        <td colSpan={10} className="px-6 py-4 text-xs text-neutral-450 border-t border-neutral-900">
                          <div className="bg-[#0b0b0b]/60 border border-neutral-900 rounded p-4 space-y-3 font-mono text-[10.5px] leading-relaxed text-neutral-350 shadow-inner whitespace-pre-wrap max-w-full overflow-x-auto">
                            <div className="flex items-center gap-1.5 text-emerald-400 font-bold uppercase text-[9.5px] tracking-wider mb-1 border-b border-neutral-900 pb-2">
                              <Cpu className="w-3.5 h-3.5" />
                              {lang === 'en' ? 'AI Decision Reasoning Chain (CoT)' : 'Цепочка рассуждений ИИ (CoT)'}
                            </div>
                            {log.reasoning}
                          </div>
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                );
              })}
            </tbody>

          </table>
        </div>

        {/* Empty placeholder fallback */}
        {logs.length === 0 && (
          <div className="text-center py-10 text-neutral-500 font-mono text-xs">
            {lang === 'en' ? 'No execution fills committed yet.' : 'Журнал исполнения ордеров пуст.'}
          </div>
        )}

      </div>

    </div>
  );
}
