import React, { useState } from 'react';
import { OptimizationLogEntry } from '../types';
import { runOptimizer } from '../services/apiClient';
import { Settings, Play, Sparkles, CheckCircle2, RotateCcw, HelpCircle, ChevronDown, ChevronUp } from 'lucide-react';

interface OptimizerAuditTableProps {
  logs: OptimizationLogEntry[];
  onRefresh: () => void;
  locale: 'en' | 'ru';
}

export const OptimizerAuditTable: React.FC<OptimizerAuditTableProps> = ({ logs, onRefresh, locale }) => {
  const [isTuning, setIsTuning] = useState(false);
  const [isOpen, setIsOpen] = useState(false);
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
          <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium bg-amber-400/10 text-amber-400 border border-amber-400/20">
            <Sparkles className="w-2.5 h-2.5 mr-0.5 animate-pulse" />
            {locale === 'en' ? 'TESTING' : 'ТЕСТ'}
          </span>
        );
      case 'REVERTED':
        return (
          <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium bg-rose-500/10 text-rose-400 border border-rose-500/20">
            <RotateCcw className="w-2.5 h-2.5 mr-0.5" />
            {locale === 'en' ? 'REVERTED' : 'ОТКАТ'}
          </span>
        );
      case 'COMPLETED':
      default:
        return (
          <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
            <CheckCircle2 className="w-2.5 h-2.5 mr-0.5" />
            {locale === 'en' ? 'VALIDATED' : 'ПРИНЯТО'}
          </span>
        );
    }
  };

  const formatParamName = (name: string) => {
    return name.replace(/_/g, ' ').toUpperCase();
  };

  return (
    <div className="bg-slate-900/60 backdrop-blur-md rounded-xl border border-slate-800/80 p-4 shadow-2xl transition-all duration-300">
      <div 
        className="flex items-center justify-between cursor-pointer select-none"
        onClick={() => setIsOpen(!isOpen)}
      >
        <div className="flex items-center gap-2">
          <div className="p-1.5 bg-indigo-500/10 rounded-lg text-indigo-400 border border-indigo-500/20">
            <Settings className="w-4 h-4" />
          </div>
          <div>
            <h3 className="text-sm font-semibold text-slate-100 flex items-center gap-1.5">
              {locale === 'en' ? 'System Self-Tuning Audit' : 'Аудит Самооптимизации'}
              <span className="text-[10px] font-mono font-normal text-slate-500">
                ({logs.length})
              </span>
            </h3>
            <p className="text-[11px] text-slate-400">
              {locale === 'en' ? 'Click to view parameters tuning history' : 'Нажмите, чтобы просмотреть лог настроек'}
            </p>
          </div>
        </div>
        
        <div className="flex items-center gap-3" onClick={(e) => e.stopPropagation()}>
          <button
            onClick={handleTriggerTuning}
            disabled={isTuning}
            className="flex items-center gap-1 px-2.5 py-1 rounded-md text-[11px] font-medium bg-indigo-600 text-white hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed transition duration-200 shadow-md shadow-indigo-600/10"
          >
            <Play className={`w-3 h-3 ${isTuning ? 'animate-spin' : ''}`} />
            {isTuning 
              ? (locale === 'en' ? 'Tuning...' : 'Оптимизация...') 
              : (locale === 'en' ? 'Trigger Self-Tuning' : 'Запустить Настройку')}
          </button>
          
          <button
            onClick={() => setIsOpen(!isOpen)}
            className="p-1 text-slate-400 hover:text-slate-200 transition-colors"
          >
            {isOpen ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
          </button>
        </div>
      </div>

      {isOpen && (
        <div className="mt-4 pt-3 border-t border-slate-800/60 overflow-y-auto max-h-[160px]">
          {logs.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-6 text-center">
              <HelpCircle className="w-6 h-6 text-slate-600 mb-1.5" />
              <p className="text-xs text-slate-400 font-medium">
                {locale === 'en' ? 'No optimization adjustments recorded yet' : 'Пока нет записей о настройке параметров'}
              </p>
              <p className="text-[10px] text-slate-500 max-w-xs mt-0.5">
                {locale === 'en' 
                  ? 'Tuning triggers every 30 cycles or on Stop Loss liquidations.' 
                  : 'Настройка запускается каждые 30 циклов или при ликвидации стоп-лосса.'}
              </p>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-[11px] border-collapse">
                <thead>
                  <tr className="border-b border-slate-800/80 text-slate-400 font-medium pb-1.5">
                    <th className="py-1 pr-2">{locale === 'en' ? 'Time' : 'Время'}</th>
                    <th className="py-1 px-2">{locale === 'en' ? 'Parameter' : 'Параметр'}</th>
                    <th className="py-1 px-2">{locale === 'en' ? 'Tuning (Old → New)' : 'Изменение'}</th>
                    <th className="py-1 px-2">{locale === 'en' ? 'Attribution Score' : 'Оценка'}</th>
                    <th className="py-1 px-2">{locale === 'en' ? 'Status' : 'Статус'}</th>
                  </tr>
                </thead>
                <tbody>
                  {logs.slice().reverse().map((log) => (
                    <React.Fragment key={log.id}>
                      <tr 
                        className="border-b border-slate-800/30 hover:bg-slate-800/20 cursor-pointer transition duration-150"
                        onClick={() => setExpandedReason(expandedReason === log.id ? null : log.id)}
                      >
                        <td className="py-1.5 pr-2 font-mono text-[10px] text-slate-400">
                          {new Date(log.timestamp * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                        </td>
                        <td className="py-1.5 px-2 font-medium text-slate-200">
                          {formatParamName(log.param_name)}
                        </td>
                        <td className="py-1.5 px-2 font-mono text-[10px] text-slate-300">
                          <span className="text-slate-500">{log.old_value.toFixed(4)}</span>
                          <span className="mx-1 text-slate-600">→</span>
                          <span className="text-indigo-400 font-semibold">{log.new_value.toFixed(4)}</span>
                        </td>
                        <td className="py-1.5 px-2">
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
                            <span className="text-slate-500 italic text-[10px]">
                              {locale === 'en' ? 'Pending...' : 'Ожидание...'}
                            </span>
                          )}
                        </td>
                        <td className="py-1.5 px-2">
                          {getStatusBadge(log.status)}
                        </td>
                      </tr>
                      {expandedReason === log.id && (
                        <tr className="bg-slate-950/40">
                          <td colSpan={5} className="py-2.5 px-3.5 text-slate-300 border-b border-slate-800/60 leading-relaxed text-[10px]">
                            <div className="flex flex-col gap-1">
                              <div>
                                <strong className="text-indigo-400 font-semibold uppercase tracking-wider text-[9px] mr-1">
                                  {locale === 'en' ? 'Hypothesis:' : 'Гипотеза:'}
                                </strong>
                                {log.reasoning}
                              </div>
                              <div className="flex items-center gap-4 text-slate-500 text-[9px] mt-0.5">
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
      )}
    </div>
  );
};
