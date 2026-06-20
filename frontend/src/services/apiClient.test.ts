import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { postTradingStart, postAgentChat } from './apiClient';

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
