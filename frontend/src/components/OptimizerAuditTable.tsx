import React, { useState } from 'react';
import { OptimizationLogEntry } from '../types';
import { runOptimizer } from '../services/apiClient';
import { Settings, Play, Sparkles, CheckCircle2, RotateCcw, AlertTriangle, HelpCircle } from 'lucide-react';

interface OptimizerAuditTableProps {
  logs: OptimizationLogEntry[];
  onRefresh: () => void;
  locale: 'en' | 'ru';
}

export const OptimizerAuditTable: React.FC<OptimizerAuditTableProps> = ({ logs, onRefresh, locale }) => {
  const [isTuning, setIsTuning] = useState(false);
  const [expandedReason, setExpandedReason] = useState<string | null>(null);

  const handleTriggerTuning = async () => {
    setIsTuning(true);
    try {
      await runOptimizer();
      onRefresh();
    } catch (err) {
      console.error('Failed to run optimizer:', err);
    } finally {
      setIsTuning(false);
    }
  };

  const getStatusBadge = (status: string) => {
    switch (status) {
      case 'ACTIVE':
        return (
          <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-amber-400/10 text-amber-400 border border-amber-400/20">
            <Sparkles className="w-3 h-3 mr-1 animate-pulse" />
            {locale === 'en' ? 'TESTING' : 'ТЕСТ'}
          </span>
        );
      case 'REVERTED':
        return (
          <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-rose-500/10 text-rose-400 border border-rose-500/20">
            <RotateCcw className="w-3 h-3 mr-1" />
            {locale === 'en' ? 'REVERTED' : 'ОТКАТ'}
          </span>
        );
      case 'COMPLETED':
      default:
        return (
          <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
            <CheckCircle2 className="w-3 h-3 mr-1" />
            {locale === 'en' ? 'VALIDATED' : 'ПРИНЯТО'}
          </span>
        );
    }
  };

  const formatParamName = (name: string) => {
    return name.replace(/_/g, ' ').toUpperCase();
  };

  return (
    <div className="bg-slate-900/60 backdrop-blur-md rounded-xl border border-slate-800/80 p-5 shadow-2xl flex flex-col h-full">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <div className="p-2 bg-indigo-500/10 rounded-lg text-indigo-400 border border-indigo-500/20">
            <Settings className="w-5 h-5" />
          </div>
          <div>
            <h3 className="text-base font-semibold text-slate-100">
              {locale === 'en' ? 'System Self-Tuning Audit' : 'Аудит Самооптимизации'}
            </h3>
            <p className="text-xs text-slate-400 mt-0.5">
              {locale === 'en' ? 'Attribution logs for parameter variations' : 'Логи аттрибуции для вариации параметров'}
            </p>
          </div>
        </div>
        
        <button
          onClick={handleTriggerTuning}
          disabled={isTuning}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-indigo-600 text-white hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed transition duration-200 shadow-md shadow-indigo-600/10"
        >
          <Play className={`w-3.5 h-3.5 ${isTuning ? 'animate-spin' : ''}`} />
          {isTuning 
            ? (locale === 'en' ? 'Tuning...' : 'Оптимизация...') 
            : (locale === 'en' ? 'Trigger Self-Tuning' : 'Запустить Настройку')}
        </button>
      </div>

      <div className="flex-1 overflow-y-auto max-h-[220px]">
        {logs.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-8 text-center">
            <HelpCircle className="w-8 h-8 text-slate-600 mb-2" />
            <p className="text-sm text-slate-400 font-medium">
              {locale === 'en' ? 'No optimization adjustments recorded yet' : 'Пока нет записей о настройке параметров'}
            </p>
            <p className="text-xs text-slate-500 max-w-xs mt-1">
              {locale === 'en' 
                ? 'Tuning triggers every 30 cycles or on Stop Loss liquidations.' 
                : 'Настройка запускается каждые 30 циклов или при ликвидации стоп-лосса.'}
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs border-collapse">
              <thead>
                <tr className="border-b border-slate-800 text-slate-400 font-medium pb-2">
                  <th className="py-2 pr-2">{locale === 'en' ? 'Time' : 'Время'}</th>
                  <th className="py-2 px-2">{locale === 'en' ? 'Parameter' : 'Параметр'}</th>
                  <th className="py-2 px-2">{locale === 'en' ? 'Tuning (Old → New)' : 'Изменение'}</th>
                  <th className="py-2 px-2">{locale === 'en' ? 'Attribution Score' : 'Оценка'}</th>
                  <th className="py-2 px-2">{locale === 'en' ? 'Status' : 'Статус'}</th>
                </tr>
              </thead>
              <tbody>
                {logs.slice().reverse().map((log) => (
                  <React.Fragment key={log.id}>
                    <tr 
                      className="border-b border-slate-800/40 hover:bg-slate-800/20 cursor-pointer transition duration-150"
                      onClick={() => setExpandedReason(expandedReason === log.id ? null : log.id)}
                    >
                      <td className="py-2.5 pr-2 font-mono text-[11px] text-slate-400">
                        {new Date(log.timestamp * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                      </td>
                      <td className="py-2.5 px-2 font-medium text-slate-200">
                        {formatParamName(log.param_name)}
                      </td>
                      <td className="py-2.5 px-2 font-mono text-[11px] text-slate-300">
                        <span className="text-slate-500">{log.old_value.toFixed(4)}</span>
                        <span className="mx-1.5 text-slate-600">→</span>
                        <span className="text-indigo-400 font-semibold">{log.new_value.toFixed(4)}</span>
                      </td>
                      <td className="py-2.5 px-2">
                        {log.attribution_checked ? (
                          <span className={`font-mono font-semibold ${
                            log.attribution_score > 0.02 
                              ? 'text-emerald-400' 
                              : log.attribution_score < -0.02 
                                ? 'text-rose-400' 
                                : 'text-slate-400'
                          }`}>
                            {log.attribution_score > 0 ? '+' : ''}{log.attribution_score.toFixed(4)}
                          </span>
                        ) : (
                          <span className="text-slate-500 italic text-[11px]">
                            {locale === 'en' ? 'Pending...' : 'Ожидание...'}
                          </span>
                        )}
                      </td>
                      <td className="py-2.5 px-2">
                        {getStatusBadge(log.status)}
                      </td>
                    </tr>
                    {expandedReason === log.id && (
                      <tr className="bg-slate-950/40">
                        <td colSpan={5} className="py-3 px-4 text-slate-300 border-b border-slate-800/80 leading-relaxed text-[11px]">
                          <div className="flex flex-col gap-1.5">
                            <div>
                              <strong className="text-indigo-400 font-semibold uppercase tracking-wider text-[10px] mr-1.5">
                                {locale === 'en' ? 'Hypothesis:' : 'Гипотеза:'}
                              </strong>
                              {log.reasoning}
                            </div>
                            <div className="flex items-center gap-4 text-slate-500 text-[10px] mt-0.5">
                              <span>
                                {locale === 'en' ? 'Baseline Win Rate:' : 'Базовая точность:'}{' '}
                                <strong className="text-slate-400">{(log.baseline_win_rate * 100).toFixed(0)}%</strong>
                              </span>
                              <span>
                                {locale === 'en' ? 'Baseline Port Value:' : 'Базовый портфель:'}{' '}
                                <strong className="text-slate-400">${log.baseline_portfolio_value.toLocaleString([], { maximumFractionDigits: 2 })}</strong>
                              </span>
                            </div>
                          </div>
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
};
