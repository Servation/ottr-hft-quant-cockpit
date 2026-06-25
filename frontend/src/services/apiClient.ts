import { 
  MarketAsset, 
  ExecutionLogEntry, 
  TradingAgentState,
  BackendAgentState,
  PortfolioSnapshot,
  NewsItem,
  LLMConfig,
  LLMTestResult,
  TradingConfig,
  ChatMessage,
  OptimizationLogEntry,
  SystemHealth
} from '../types';

const API_BASE = '/api/v1';

// Shared-secret API key for state-changing requests. Reads a build-time Vite var
// (VITE_OTTR_API_KEY) with a runtime localStorage override for self-hosted setups.
// Read-only endpoints do not send it (the SSE stream can't carry headers).
function getApiKey(): string {
  let ls = '';
  try {
    ls = typeof localStorage !== 'undefined' ? (localStorage.getItem('ottr_api_key') || '') : '';
  } catch {
    ls = '';
  }
  return ((import.meta as any).env?.VITE_OTTR_API_KEY as string) || ls || '';
}

function writeHeaders(json: boolean = true): Record<string, string> {
  const h: Record<string, string> = json ? { 'Content-Type': 'application/json' } : {};
  const key = getApiKey();
  if (key) h['X-API-Key'] = key;
  return h;
}

// Health check to determine gateway availability
export async function checkGatewayHealth(): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE}/health`, {
      method: 'GET',
      headers: { 'Accept': 'application/json' },
    });
    if (!response.ok) return false;
    const data = await response.json();
    return data.status === 'ok' || data.status === 'healthy' || response.status === 200;
  } catch (err) {
    return false;
  }
}

// Fetch market prices for active cryptocurrencies
export async function fetchMarketData(symbols: string[]): Promise<Record<string, MarketAsset>> {
  const query = symbols.join(',');
  const response = await fetch(`${API_BASE}/market-data?symbols=${query}`);
  if (!response.ok) {
    throw new Error(`Failed to fetch market data: ${response.statusText}`);
  }
  return response.json();
}



// Helper to map a backend log item to the frontend's ExecutionLogEntry interface
function mapLogEntry(item: any, idx: number): ExecutionLogEntry {
  const timestampVal = item.timestamp ?? (Date.now() / 1000);
  let timestampStr = '';
  try {
    const dateObj = new Date(timestampVal * 1000);
    timestampStr = dateObj.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit', hour12: true });
  } catch (e) {
    timestampStr = new Date().toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit', hour12: true });
  }

  return {
    id: item.id ?? `log-${idx}-${timestampVal}`,
    timestamp: timestampStr,
    symbol: item.symbol ?? 'BTC',
    action: item.action ?? 'HOLD',
    quantity: item.quantity ?? 0,
    latencyMs: item.execution_latency_ms ?? item.latencyMs ?? 0,
    arrivalPrice: item.price ?? item.arrivalPrice ?? 0,
    vwapFillPrice: item.vwap_fill_price ?? item.price ?? item.vwapFillPrice ?? 0,
    slippagePct: item.slippage_pct ?? item.slippagePct ?? 0.0001,
    feeUsd: item.fee_usd ?? item.feeUsd ?? 0,
    reasoning: item.reasoning ?? '',
  };
}

// Fetch portfolio snapshot (equity, cash, asset allocations)
export async function fetchPortfolioSnapshot(): Promise<PortfolioSnapshot> {
  const response = await fetch(`${API_BASE}/portfolio/snapshot`);
  if (!response.ok) {
    throw new Error(`Failed to fetch portfolio snapshot: ${response.statusText}`);
  }
  const data = await response.json();
  const equity = data.total_value ?? 250000;
  const drawdown = data.drawdown ?? 0.012;
  const cash = data.usd_cash !== undefined ? Math.round(data.usd_cash) : Math.round(equity * (1.0 - drawdown));

  // Real allocation weights from live holdings valued at current prices (plus
  // cash), replacing the old hardcoded 60/30/10 placeholder.
  const holdings = (data.holdings || {}) as Record<string, any>;
  const currentPrices = (data.current_prices || {}) as Record<string, number>;
  const cashValue = data.usd_cash ?? 0;
  const holdingEntries = Object.entries(holdings).map(([sym, h]) => {
    const qty = typeof h === 'object' && h !== null ? (h.quantity ?? 0) : Number(h || 0);
    const price = currentPrices[sym] ?? (typeof h === 'object' && h !== null ? (h.avg_cost ?? 0) : 0);
    return { symbol: sym.replace('USDT', ''), value: qty * price };
  });
  const allocTotal = cashValue + holdingEntries.reduce((s, e) => s + e.value, 0);
  const pct = (v: number) => Math.round((v / allocTotal) * 1000) / 10;
  const allocations = allocTotal > 0
    ? [...holdingEntries.map(e => ({ symbol: e.symbol, percentage: pct(e.value) })),
       { symbol: 'Cash', percentage: pct(cashValue) }]
    : [];

  return {
    equity,
    cash,
    allocations,
    tradingActive: data.trading_active,
    holdings: data.holdings,
    purchasePrices: data.purchase_prices,
    currentPrices: data.current_prices,
    performance: data.performance ?? null,
    risk: data.risk ?? null,
  };
}

// Fetch aggregated component health (gateway /health/detailed). Read-only.
export async function fetchSystemHealth(): Promise<SystemHealth> {
  const response = await fetch(`${API_BASE}/health/detailed`);
  if (!response.ok) {
    throw new Error(`Failed to fetch system health: ${response.statusText}`);
  }
  return response.json();
}

// Fetch execution logs
export async function fetchExecutionLogs(limit: number = 50): Promise<ExecutionLogEntry[]> {
  const response = await fetch(`${API_BASE}/execution-logs?limit=${limit}`);
  if (!response.ok) {
    throw new Error(`Failed to fetch execution logs: ${response.statusText}`);
  }
  const data = await response.json();
  if (Array.isArray(data)) {
    return data.map((item, idx) => mapLogEntry(item, idx));
  }
  return [];
}



// Start trading loops on gateway
export async function postTradingStart(): Promise<void> {
  const response = await fetch(`${API_BASE}/trading/start`, { method: 'POST', headers: writeHeaders(false) });
  if (!response.ok) {
    throw new Error(`Failed to start trading: ${response.statusText}`);
  }
}

// Stop trading loops on gateway
export async function postTradingStop(): Promise<void> {
  const response = await fetch(`${API_BASE}/trading/stop`, { method: 'POST', headers: writeHeaders(false) });
  if (!response.ok) {
    throw new Error(`Failed to stop trading: ${response.statusText}`);
  }
}

// Reset sandbox portfolio starting balance
export async function postResetBalance(balance: number): Promise<void> {
  const response = await fetch(`${API_BASE}/portfolio/reset-balance`, {
    method: 'POST',
    headers: writeHeaders(),
    body: JSON.stringify({ balance }),
  });
  if (!response.ok) {
    throw new Error(`Failed to reset starting balance: ${response.statusText}`);
  }
}

// SSE subscription for real-time agent/execution states
export function subscribeToAgentEvents(
  onAgentState: (state: BackendAgentState[]) => void,
  onExecution: (log: ExecutionLogEntry) => void,
  onPortfolio: (portfolio: PortfolioSnapshot) => void,
  onError: () => void,
  onOptimizationHistory?: (history: OptimizationLogEntry[]) => void
): EventSource {
  const eventSource = new EventSource(`${API_BASE}/events/stream`);

  eventSource.addEventListener('agent_state', (event: any) => {
    try {
      const data = JSON.parse(event.data);
      if (Array.isArray(data)) {
        onAgentState(data);
      }
    } catch (err) {
      console.error('Error parsing agent_state SSE event:', err);
    }
  });

  eventSource.addEventListener('execution', (event: any) => {
    try {
      const data = JSON.parse(event.data);
      onExecution(mapLogEntry(data, 0));
    } catch (err) {
      console.error('Error parsing execution SSE event:', err);
    }
  });

  eventSource.addEventListener('portfolio', (event: any) => {
    try {
      const data = JSON.parse(event.data);
      const equity = data.total_value ?? 250000;
      const drawdown = data.drawdown ?? 0.012;
      const cash = data.usd_cash !== undefined ? Math.round(data.usd_cash) : Math.round(equity * (1.0 - drawdown));
      onPortfolio({
        equity,
        cash,
        allocations: [],
        holdings: data.holdings,
        purchasePrices: data.purchase_prices,
        currentPrices: data.current_prices
      });
    } catch (err) {
      console.error('Error parsing portfolio SSE event:', err);
    }
  });

  eventSource.addEventListener('optimization_history', (event: any) => {
    try {
      const data = JSON.parse(event.data);
      if (Array.isArray(data) && onOptimizationHistory) {
        onOptimizationHistory(data);
      }
    } catch (err) {
      console.error('Error parsing optimization_history SSE event:', err);
    }
  });

  eventSource.onerror = () => {
    onError();
  };

  return eventSource;
}

export async function postAgentChat(message: string, history: ChatMessage[]): Promise<{ text: string }> {
  const response = await fetch(`${API_BASE}/agent/chat`, {
    method: 'POST',
    headers: writeHeaders(),
    body: JSON.stringify({
      message,
      history: history.map(h => ({ sender: h.sender, text: h.text }))
    }),
  });
  if (!response.ok) {
    throw new Error(`Failed to send chat message: ${response.statusText}`);
  }
  return response.json();
}

// Fetch optimization history
export async function getOptimizerHistory(): Promise<OptimizationLogEntry[]> {
  const response = await fetch(`${API_BASE}/optimizer/history`);
  if (!response.ok) {
    throw new Error(`Failed to fetch optimizer history: ${response.statusText}`);
  }
  return response.json();
}

// Configure LLM parameters
export async function postLLMConfigure(config: LLMConfig): Promise<void> {
  const response = await fetch(`${API_BASE}/llm/configure`, {
    method: 'POST',
    headers: writeHeaders(),
    body: JSON.stringify(config),
  });
  if (!response.ok) {
    throw new Error(`Failed to configure LLM: ${response.statusText}`);
  }
}

// Update trading config
export async function postTradingConfig(config: TradingConfig): Promise<void> {
  const response = await fetch(`${API_BASE}/trading/config`, {
    method: 'POST',
    headers: writeHeaders(),
    body: JSON.stringify(config),
  });
  if (!response.ok) {
    throw new Error(`Failed to update trading config: ${response.statusText}`);
  }
}
