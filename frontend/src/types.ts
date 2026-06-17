/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

export type AgentStatusType = 'IDLE' | 'EXECUTING' | 'COMPLETED' | 'VETOED' | 'THINKING' | 'SPEAKING';

export interface TradingAgentState {
  id: string;
  name: string;
  status: AgentStatusType;
  message: { en: string; ru: string };
  lastUpdated: string;
  history?: { timestamp: string; message: { en: string; ru: string }; status: AgentStatusType }[];
}

export interface BackendAgentState {
  id: string;
  name: string;
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

export interface PortfolioSnapshot {
  equity: number;
  cash: number;
  allocations: { symbol: string; percentage: number }[];
  tradingActive?: boolean;
  holdings?: Record<string, number>;
  purchasePrices?: Record<string, number>;
  currentPrices?: Record<string, number>;
  llm_fallback_base_url?: string;
  llm_fallback_api_key?: string;
  llm_fallback_model_id?: string;
  llm_fallback_active?: boolean;
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

