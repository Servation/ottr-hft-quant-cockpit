/**
 * System health panel (Tier 4 / O2). Polls the gateway's aggregated /health/detailed
 * (~30s) and renders a compact component status grid (LLM, price feed, scheduler,
 * portfolio, bridge) with a top-line OK / DEGRADED / DOWN verdict. Read-only.
 */
import React, { useEffect, useState } from 'react';
import { fetchSystemHealth } from '../services/apiClient';
import { SystemHealth } from '../types';
import { Activity, CheckCircle2, AlertTriangle, XCircle } from 'lucide-react';

const STATUS_COLOR: Record<string, string> = {
  OK: 'text-emerald-400',
  DEGRADED: 'text-amber-400',
  STALE: 'text-amber-400',
  DOWN: 'text-rose-400',
  UNKNOWN: 'text-neutral-500',
};

const LABEL: Record<string, string> = {
  llm: 'LLM',
  price_feed: 'Price Feed',
  scheduler: 'Scheduler',
  portfolio: 'Portfolio',
  bridge: 'Bridge',
};

export default function SystemHealthPanel({ lang }: { lang: 'en' | 'ru' }) {
  const [health, setHealth] = useState<SystemHealth | null>(null);

  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        const h = await fetchSystemHealth();
        if (active) setHealth(h);
      } catch {
        if (active) setHealth(null);
      }
    };
    load();
    const id = setInterval(load, 30000);
    return () => {
      active = false;
      clearInterval(id);
    };
  }, []);

  if (!health) return null;

  const overall = health.status;
  const OverallIcon = overall === 'OK' ? CheckCircle2 : overall === 'DOWN' ? XCircle : AlertTriangle;

  return (
    <div className="bg-black/40 p-3 rounded border border-neutral-800 text-[11px] font-mono">
      <div className="flex items-center justify-between mb-2">
        <span className="uppercase tracking-wider text-neutral-500 font-semibold flex items-center gap-1.5">
          <Activity className="w-3.5 h-3.5 text-emerald-500" />
          {lang === 'en' ? 'System Health' : 'Состояние системы'}
        </span>
        <span className={`flex items-center gap-1 ${STATUS_COLOR[overall] || 'text-neutral-500'}`}>
          <OverallIcon className="w-3.5 h-3.5" /> {overall}
        </span>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
        {Object.entries(health.components).map(([key, c]) => (
          <div key={key} className="flex items-center gap-1.5" title={c.status}>
            <span className={`inline-block w-1.5 h-1.5 rounded-full ${(STATUS_COLOR[c.status] || 'text-neutral-500').replace('text-', 'bg-')}`} />
            <span className="text-neutral-400">{LABEL[key] || key}</span>
            <span className={STATUS_COLOR[c.status] || 'text-neutral-500'}>{c.status}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
