import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { postTradingStart, postAgentChat, fetchPortfolioSnapshot, fetchSystemHealth } from './apiClient';

// Smoke test: state-changing calls must attach the configured X-API-Key.
// Vite statically inlines VITE_-prefixed env vars at transform time, so we
// compare against the same baked value the client reads (set in frontend/.env).
// Run with: npm test
const EXPECTED_KEY = (import.meta as any).env?.VITE_OTTR_API_KEY as string | undefined;

describe('apiClient auth headers', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) })));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('postTradingStart sends the configured X-API-Key', async () => {
    expect(EXPECTED_KEY, 'set VITE_OTTR_API_KEY in frontend/.env').toBeTruthy();
    await postTradingStart();
    const [, init] = (fetch as any).mock.calls[0];
    expect(init.headers['X-API-Key']).toBe(EXPECTED_KEY);
  });

  it('postAgentChat sends X-API-Key and JSON content-type', async () => {
    expect(EXPECTED_KEY, 'set VITE_OTTR_API_KEY in frontend/.env').toBeTruthy();
    await postAgentChat('hello', []);
    const [, init] = (fetch as any).mock.calls[0];
    expect(init.headers['X-API-Key']).toBe(EXPECTED_KEY);
    expect(init.headers['Content-Type']).toBe('application/json');
  });
});

describe('fetchPortfolioSnapshot mapping', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('derives real allocations and passes through performance metrics', async () => {
    const snapshot = {
      total_value: 13000,
      usd_cash: 2000,
      holdings: {
        BTC: { quantity: 0.1, avg_cost: 50000 }, // 0.1 * 60000 = 6000
        ETH: { quantity: 2, avg_cost: 2000 },     // 2 * 2500   = 5000
      },
      current_prices: { BTC: 60000, ETH: 2500 },
      drawdown: 0.1,
      performance: {
        total_return: 0.1, cagr: null, sharpe: 1.2, sortino: 0.9,
        max_drawdown: 0.1, benchmark_return: 0.2, alpha: -0.1, num_points: 10,
      },
      risk: { enabled: true, halted: false, halted_since: null,
              current_drawdown: 0.05, stop_loss_pct: 10, max_drawdown_halt_pct: 15 },
      trading_active: true,
    };
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => snapshot })));

    const result = await fetchPortfolioSnapshot();

    // total = 6000 + 5000 + 2000 = 13000 -> BTC 46.2%, ETH 38.5%, Cash 15.4%.
    expect(result.allocations.find(a => a.symbol === 'BTC')?.percentage).toBeCloseTo(46.2, 1);
    expect(result.allocations.find(a => a.symbol === 'ETH')?.percentage).toBeCloseTo(38.5, 1);
    expect(result.allocations.find(a => a.symbol === 'Cash')?.percentage).toBeCloseTo(15.4, 1);
    // No more hardcoded 60/30/10.
    expect(result.allocations.find(a => a.symbol === 'BTC')?.percentage).not.toBe(60);

    // Performance metrics flow through untouched.
    expect(result.performance?.sharpe).toBe(1.2);
    expect(result.performance?.alpha).toBe(-0.1);
    expect(result.performance?.num_points).toBe(10);

    // Tier 3 risk-enforcement state passes through read-only.
    expect(result.risk?.enabled).toBe(true);
    expect(result.risk?.current_drawdown).toBe(0.05);
  });
});

describe('fetchSystemHealth', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('returns the aggregated component health', async () => {
    const health = { status: 'DEGRADED', components: { bridge: { status: 'OK' }, llm: { status: 'DOWN' } } };
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => health })));
    const result = await fetchSystemHealth();
    expect(result.status).toBe('DEGRADED');
    expect(result.components.llm.status).toBe('DOWN');
    expect(result.components.bridge.status).toBe('OK');
  });
});
