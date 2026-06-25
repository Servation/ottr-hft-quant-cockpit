/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

export type AgentStatusType = 'IDLE' | 'EXECUTING' | 'COMPLETED' | 'VETOED' | 'THINKING' | 'SPEAKING';

export interface TradingAgentState {
  id: string;
  name: string;
  description?: string;
  status: AgentStatusType;
  message: { en: string; ru: string };
  lastUpdated: string;
  history?: { timestamp: string; message: { en: string; ru: string }; status: AgentStatusType }[];
}

export interface BackendAgentState {
  id: string;
  name: string;
  description?: string;
  status: AgentStatusType;
  current_task: string;
}

export interface OptimizationLogEntry {
  id: string;
  timestamp: number;
  param_name: string;
  old_value: number;
  new_value: number;
  reasoning: string;
  baseline_portfolio_value: number;
  baseline_win_rate: number;
  attribution_checked: boolean;
  attribution_score: number;
  status: 'ACTIVE' | 'REVERTED' | 'COMPLETED';
}

export interface ExecutionLogEntry {
  id: string;
  timestamp: string;
  symbol: string;
  action: 'BUY' | 'SELL' | 'HOLD';
  quantity: number;
  latencyMs: number;
  arrivalPrice: number;
  vwapFillPrice: number;
  slippagePct: number;
  feeUsd: number;
  reasoning?: string;
}

export interface MarketAsset {
  symbol: string;
  name: string;
  price: number;
  change24h: number;
  volume?: number;
  volumeQuote?: number;
}

export interface ChartDataPoint {
  timeLabel: string;
  equity: number;
  cash: number;
  timestamp?: number;
}

export interface LocalLlmConfig {
  hostUrl: string;
  apiKey: string;
  modelId: string;
  status: 'DISCONNECTED' | 'TESTING' | 'CONNECTED' | 'ERROR';
  pingMs?: number;
  fallbackHostUrl?: string;
  fallbackApiKey?: string;
  fallbackModelId?: string;
  fallbackActive?: boolean;
}

// Risk-adjusted performance metrics computed by the bridge over its equity
// curve. Fields are null until enough history accumulates (never a fake 0).
export interface PerformanceMetrics {
  total_return: number | null;
  cagr: number | null;
  sharpe: number | null;
  sortino: number | null;
  max_drawdown: number | null;   // positive fraction (0.25 = -25%)
  benchmark_return: number | null; // BTC buy-and-hold return over the window
  alpha: number | null;            // strategy return minus benchmark
  num_points: number;
}

// Tier 3 risk-enforcement state, surfaced read-only from the bridge (no secrets):
// whether enforcement is on, whether the drawdown breaker has halted new BUYs, and the
// current drawdown vs the configured limit.
export interface RiskState {
  enabled: boolean;
  halted: boolean;
  halted_since: number | null;
  current_drawdown: number | null;   // positive fraction off the peak (0.16 = -16%)
  stop_loss_pct: number | null;
  max_drawdown_halt_pct: number | null;
}

export interface PortfolioSnapshot {
  equity: number;
  cash: number;
  allocations: { symbol: string; percentage: number }[];
  tradingActive?: boolean;
  holdings?: Record<string, number>;
  purchasePrices?: Record<string, number>;
  currentPrices?: Record<string, number>;
  performance?: PerformanceMetrics | null;
  risk?: RiskState | null;
}

export interface NewsItem {
  title: string;
  link: string;
  pubDate: string;
  source: string;
}

export interface LLMConfig {
  base_url: string;
  api_key: string;
  model_id: string;
  fallback_base_url?: string;
  fallback_api_key?: string;
  fallback_model_id?: string;
  fallback_active?: boolean;
}

export interface LLMTestResult {
  status: 'CONNECTED' | 'ERROR';
  ping_ms?: number;
  error_message?: string;
}

export interface TradingConfig {
  strategy: 'DD90/10' | 'AdaptiveTrend';
  ticksPerMinute: number;
  activeCryptos: string[];
  stopLossLimit?: number;
}

export interface ChatMessage {
  sender: 'user' | 'agent';
  text: string;
  timestamp: string;
}

// Tier 4 (O2): aggregated component health from the gateway /health/detailed.
export interface ComponentHealth {
  status: 'OK' | 'DEGRADED' | 'DOWN' | 'STALE' | 'UNKNOWN';
  latency_ms?: number;
  last_quote_age_sec?: number | null;
  next_meeting?: string;
  next_type?: string;
  positions?: number;
}

export interface SystemHealth {
  status: 'OK' | 'DEGRADED' | 'DOWN';
  components: Record<string, ComponentHealth>;
}

