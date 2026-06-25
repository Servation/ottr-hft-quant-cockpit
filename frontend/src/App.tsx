/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useRef } from 'react';
import { translations } from './translations';
import { ChartDataPoint, ExecutionLogEntry, MarketAsset, TradingAgentState, LocalLlmConfig, BackendAgentState, PortfolioSnapshot, OptimizationLogEntry } from './types';
import {
  createNewLogEntry,
  getFormattedTime
} from './utils/simulator';
import {
  checkGatewayHealth,
  fetchMarketData,
  fetchPortfolioSnapshot,
  fetchExecutionLogs,
  postTradingConfig,
  postTradingStart,
  postTradingStop,
  subscribeToAgentEvents,
  postLLMConfigure,
  getOptimizerHistory
} from './services/apiClient';

// Import subcomponents
import OverviewPanel from './components/OverviewPanel';
import SystemHealthPanel from './components/SystemHealthPanel';
import AssetAllocation from './components/AssetAllocation';
import AgentRoomConsole from './components/AgentRoomConsole';
import ExecutionLogsTable from './components/ExecutionLogsTable';

import {
  TrendingUp, 
  ShieldAlert, 
  Activity, 
  Cpu, 
  Database, 
  Network, 
  Terminal, 
  Clock, 
  Play, 
  Compass, 
  HelpCircle,
  TrendingDown
} from 'lucide-react';

// Static master registry of available cryptocurrencies (strictly crypto)
const AVAILABLE_CRYPTOS = [
  { symbol: 'SOL', name: 'Solana', colorClass: 'text-purple-400' },
  { symbol: 'BNB', name: 'Binance Coin', colorClass: 'text-amber-400' },
  { symbol: 'XRP', name: 'Ripple', colorClass: 'text-blue-300' },
  { symbol: 'ADA', name: 'Cardano', colorClass: 'text-blue-400' },
  { symbol: 'DOGE', name: 'Dogecoin', colorClass: 'text-yellow-500' },
  { symbol: 'AVAX', name: 'Avalanche', colorClass: 'text-rose-500' },
  { symbol: 'LINK', name: 'Chainlink', colorClass: 'text-indigo-400' },
  { symbol: 'DOT', name: 'Polkadot', colorClass: 'text-pink-400' },
  { symbol: 'UNI', name: 'Uniswap', colorClass: 'text-pink-500' },
  { symbol: 'LTC', name: 'Litecoin', colorClass: 'text-neutral-400' },
];

export default function App() {
  const [lang, setLang] = useState<'en' | 'ru'>(() => {
    const saved = localStorage.getItem('ottr_lang');
    return (saved === 'en' || saved === 'ru') ? saved : 'en';
  });
  const [strategy, setStrategy] = useState<'DD90/10' | 'AdaptiveTrend'>(() => {
    const saved = localStorage.getItem('ottr_strategy');
    return (saved === 'DD90/10' || saved === 'AdaptiveTrend') ? saved : 'DD90/10';
  });
  const [dataSource, setDataSource] = useState<'live' | 'simulation'>('live');
  
  // Track dynamically customized active tracked cryptocurrencies (defaulting to BTC and ETH core)
  const [activeCryptos, setActiveCryptos] = useState<string[]>(() => {
    const saved = localStorage.getItem('ottr_active_cryptos');
    if (saved) {
      try {
        const parsed = JSON.parse(saved);
        if (Array.isArray(parsed) && parsed.length > 0) {
          return parsed;
        }
      } catch (e) {}
    }
    return ['BTC', 'ETH'];
  });

  useEffect(() => {
    localStorage.setItem('ottr_lang', lang);
  }, [lang]);

  useEffect(() => {
    localStorage.setItem('ottr_strategy', strategy);
  }, [strategy]);

  useEffect(() => {
    localStorage.setItem('ottr_active_cryptos', JSON.stringify(activeCryptos));
  }, [activeCryptos]);

  const addCrypto = (symbol: string) => {
    if (!activeCryptos.includes(symbol)) {
      setActiveCryptos((prev) => [...prev, symbol]);
      
      // Bootstrap initial asset state in marketPrices so it loads immediately before the first polling cycle returns
      setMarketPrices((prev) => {
        if (prev[symbol]) return prev;
        const defaultDef: Record<string, { price: number; name: string }> = {
          BNB: { price: 580, name: 'Binance Coin' },
          SOL: { price: 165, name: 'Solana' },
          XRP: { price: 0.58, name: 'Ripple' },
          ADA: { price: 0.45, name: 'Cardano' },
          DOGE: { price: 0.14, name: 'Dogecoin' },
          AVAX: { price: 14.50, name: 'Avalanche' },
          LINK: { price: 15.20, name: 'Chainlink' },
          DOT: { price: 6.20, name: 'Polkadot' },
          UNI: { price: 7.50, name: 'Uniswap' },
          LTC: { price: 82.50, name: 'Litecoin' }
        };
        const def = defaultDef[symbol] || { price: 100, name: symbol };
        return {
          ...prev,
          [symbol]: {
            symbol,
            name: def.name,
            price: def.price,
            change24h: 0,
            volume: symbol === 'SOL' ? 1200000 : symbol === 'XRP' ? 35000000 : 500000
          }
        };
      });
    }
  };

  const removeCrypto = (symbol: string) => {
    if (symbol !== 'BTC' && symbol !== 'ETH') {
      setActiveCryptos((prev) => prev.filter((s) => s !== symbol));
    }
  };
  
  // Local LLM properties
  const [llmConfig, setLlmConfig] = useState<LocalLlmConfig>(() => {
    const saved = localStorage.getItem('ottr_llm_config');
    if (saved) {
      try {
        const parsed = JSON.parse(saved);
        return {
          hostUrl: parsed.hostUrl ?? 'http://localhost:11434',
          apiKey: parsed.apiKey ?? '',
          modelId: parsed.modelId ?? 'llama3.1-quant',
          fallbackHostUrl: parsed.fallbackHostUrl ?? '',
          fallbackApiKey: parsed.fallbackApiKey ?? '',
          fallbackModelId: parsed.fallbackModelId ?? '',
          fallbackActive: parsed.fallbackActive ?? true,
          status: 'DISCONNECTED',
        };
      } catch (e) {}
    }
    return {
      hostUrl: 'http://localhost:11434',
      apiKey: '',
      modelId: 'llama3.1-quant',
      fallbackHostUrl: '',
      fallbackApiKey: '',
      fallbackModelId: '',
      fallbackActive: true,
      status: 'DISCONNECTED',
    };
  });

  useEffect(() => {
    localStorage.setItem(
      'ottr_llm_config',
      JSON.stringify({
        hostUrl: llmConfig.hostUrl,
        apiKey: llmConfig.apiKey,
        modelId: llmConfig.modelId,
        fallbackHostUrl: llmConfig.fallbackHostUrl,
        fallbackApiKey: llmConfig.fallbackApiKey,
        fallbackModelId: llmConfig.fallbackModelId,
        fallbackActive: llmConfig.fallbackActive ?? true,
      })
    );
  }, [
    llmConfig.hostUrl,
    llmConfig.apiKey,
    llmConfig.modelId,
    llmConfig.fallbackHostUrl,
    llmConfig.fallbackApiKey,
    llmConfig.fallbackModelId,
    llmConfig.fallbackActive
  ]);

  const [isEngineRunning, setIsEngineRunning] = useState<boolean>(true);
  const isInitialMount = useRef(true);
  const [ticksPerMinute, setTicksPerMinute] = useState<number>(() => {
    const saved = localStorage.getItem('ottr_ticks_per_minute');
    return saved ? Number(saved) : 15;
  });

  useEffect(() => {
    localStorage.setItem('ottr_ticks_per_minute', ticksPerMinute.toString());
  }, [ticksPerMinute]);

  const [stopLossLimit, setStopLossLimit] = useState<number>(() => {
    const saved = localStorage.getItem('ottr_stop_loss_limit');
    return saved ? Number(saved) : 7;
  });

  useEffect(() => {
    localStorage.setItem('ottr_stop_loss_limit', stopLossLimit.toString());
  }, [stopLossLimit]);

  const [consoleTab, setConsoleTab] = useState<'telemetry' | 'terminal'>('telemetry');
  const [chatInput, setChatInput] = useState<string>('');

  const handleSelectTransaction = (id: string) => {
    setChatInput(lang === 'en' ? `Explain transaction ${id}` : `Объясни сделку ${id}`);
    setConsoleTab('terminal');
    const element = document.getElementById('agents-console');
    if (element) {
      element.scrollIntoView({ behavior: 'smooth' });
    }
  };

  const [startingBalance, setStartingBalance] = useState<number>(() => {
    const saved = localStorage.getItem('ottr_starting_balance');
    return saved ? Number(saved) : 10000;
  });

  const [portfolioSnapshot, setPortfolioSnapshot] = useState<PortfolioSnapshot | null>(null);

  // Live-only state: market prices, the equity chart, and execution logs are
  // populated entirely from the backend feeds — no fabricated demo seed (which
  // previously flashed a fake ~$100k history before real data loaded).
  const [marketPrices, setMarketPrices] = useState<Record<string, MarketAsset>>({});

  const [chartData, setChartData] = useState<ChartDataPoint[]>([]);

  const [logs, setLogs] = useState<ExecutionLogEntry[]>([]);

  // Keep track of parameter tuning history logs from the Optimizer
  const [optimizationHistory, setOptimizationHistory] = useState<OptimizationLogEntry[]>([]);

  // Track the multi-agent execution status phase (0 to 4)
  const [consensusPhase, setConsensusPhase] = useState<number>(0);

  // Pre-seed current agents state list
  const [agents, setAgents] = useState<TradingAgentState[]>(() => {
    return [
      { id: 'technical_analyst', name: 'Atlas (Technical Analyst)', description: 'Evaluates technical indicators & chart trends', status: 'IDLE', message: { en: 'IDLE: Awaiting next price frame computation...', ru: 'ОЖИДАНИЕ: Ожидание вычисления следующего тика цен...' }, lastUpdated: getFormattedTime(), history: [] },
      { id: 'sentiment_analyst', name: 'Luna (Sentiment Analyst)', description: 'Analyzes social feeds & on-chain SOPR', status: 'IDLE', message: { en: 'IDLE: Polling social indexes and order books...', ru: 'ОЖИДАНИЕ: Опрос социальных индексов и стаканов заявок...' }, lastUpdated: getFormattedTime(), history: [] },
      { id: 'trader', name: 'Mercury (Trader)', description: 'Sizes positions & executes final orders', status: 'IDLE', message: { en: 'IDLE: Position sizing pipeline in standby...', ru: 'ОЖИДАНИЕ: Конвейер сайзинга позиций находится в режиме ожидания...' }, lastUpdated: getFormattedTime(), history: [] },
      { id: 'risk_auditor', name: 'Rogue (Risk Auditor)', description: 'Enforces risk limits & vetoes bad trades', status: 'IDLE', message: { en: 'IDLE: Compliance monitors at zero utilization...', ru: 'ОЖИДАНИЕ: Мониторы комплаенса не загружены...' }, lastUpdated: getFormattedTime(), history: [] },
      { id: 'performance_optimizer', name: 'Zephyr (Performance Optimizer)', description: 'Retrospectively analyzes trades to tune risk limits', status: 'IDLE', message: { en: 'IDLE: Optimizing hyper-parameters...', ru: 'ОЖИДАНИЕ: Оптимизация параметров...' }, lastUpdated: getFormattedTime(), history: [] },
      { id: 'portfolio_manager', name: 'Midas (Portfolio Manager)', description: 'Rebalances allocations and tracks paper equity', status: 'IDLE', message: { en: 'IDLE: Rebalancing constraints in standby...', ru: 'ОЖИДАНИЕ: Ребалансировка...' }, lastUpdated: getFormattedTime(), history: [] },
      { id: 'meeting_chair', name: 'Athena (Meeting Chair)', description: 'Coordinates inter-agent Discord consensus meetings', status: 'IDLE', message: { en: 'IDLE: No consensus meetings scheduled...', ru: 'ОЖИДАНИЕ: Собрания не запланированы...' }, lastUpdated: getFormattedTime(), history: [] },
    ];
  });

  // Remove old stale cache (incl. the retired mock chart/log seeds so a
  // returning user doesn't reload fabricated history from localStorage).
  useEffect(() => {
    localStorage.removeItem('ottr_agents');
    localStorage.removeItem('ottr_chart_data');
    localStorage.removeItem('ottr_execution_logs');
  }, []);

  const activeTranslations = translations[lang];

  // Health check on mount to auto-detect microservices
  useEffect(() => {
    async function initConnection() {
      const isHealthy = await checkGatewayHealth();
      if (isHealthy) {
        setDataSource('live');
        try {
          const initialLogs = await fetchExecutionLogs();
          if (initialLogs && initialLogs.length > 0) {
            setLogs(initialLogs.slice(-50));
          }
          const portfolio = await fetchPortfolioSnapshot();
          if (portfolio.tradingActive !== undefined) {
            setIsEngineRunning(portfolio.tradingActive);
          }
          setPortfolioSnapshot(portfolio);
          try {
            const optHistory = await getOptimizerHistory();
            setOptimizationHistory(optHistory);
          } catch (e) {
            console.error('Failed to fetch initial optimizer history:', e);
          }
          let targetEquity = portfolio.equity;
          let targetCash = portfolio.cash;

          const savedStartingBalance = localStorage.getItem('ottr_starting_balance');
          if (savedStartingBalance) {
            const parsedSaved = Number(savedStartingBalance);
            // If backend is at default $100,000 and logs are empty, auto-restore the user's starting balance
            if (portfolio.equity === 100000 && (!initialLogs || initialLogs.length === 0)) {
              try {
                const { postResetBalance } = await import('./services/apiClient');
                await postResetBalance(parsedSaved);
                targetEquity = parsedSaved;
                targetCash = parsedSaved;
              } catch (e) {
                console.error("Auto-restoring starting balance failed:", e);
              }
            }
          }

          setChartData((prev) => {
            const now = new Date();
            const timeLabel = now.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', hour12: true });
            const updated = [...prev];
            if (updated.length > 0) {
              updated[updated.length - 1] = { timeLabel, equity: targetEquity, cash: targetCash, timestamp: Date.now() };
            }
            return updated;
          });

          // Auto-configure backend LLM if saved config exists in localStorage
          const saved = localStorage.getItem('ottr_llm_config');
          if (saved) {
            try {
              const parsed = JSON.parse(saved);
              if (parsed.hostUrl && parsed.modelId) {
                await postLLMConfigure({
                  base_url: parsed.hostUrl,
                  api_key: parsed.apiKey || '',
                  model_id: parsed.modelId,
                  fallback_base_url: parsed.fallbackHostUrl || '',
                  fallback_api_key: parsed.fallbackApiKey || '',
                  fallback_model_id: parsed.fallbackModelId || '',
                  fallback_active: parsed.fallbackActive ?? true
                });
                setLlmConfig(prev => ({
                  ...prev,
                  fallbackHostUrl: parsed.fallbackHostUrl ?? '',
                  fallbackApiKey: parsed.fallbackApiKey ?? '',
                  fallbackModelId: parsed.fallbackModelId ?? '',
                  fallbackActive: parsed.fallbackActive ?? true,
                  status: 'CONNECTED'
                }));
              }
            } catch (e) {
              console.error("Auto LLM configuration failed:", e);
            }
          }
        } catch (e) {
          console.error("Error loading initial live data:", e);
        }
      } else {
        setDataSource('simulation');
      }
    }
    initConnection();
  }, []);

  // Sync config options to gateway when in live mode
  useEffect(() => {
    if (dataSource !== 'live') return;
    const syncConfig = async () => {
      try {
        await postTradingConfig({
          strategy,
          ticksPerMinute,
          activeCryptos,
          stopLossLimit
        });
      } catch (err) {
        console.error('Failed to sync config with gateway:', err);
      }
    };
    syncConfig();
  }, [strategy, ticksPerMinute, activeCryptos, stopLossLimit, dataSource]);

  // Sync engine running state to gateway when in live mode
  useEffect(() => {
    if (dataSource !== 'live') return;
    if (isInitialMount.current) {
      isInitialMount.current = false;
      return;
    }
    const syncState = async () => {
      try {
        if (isEngineRunning) {
          await postTradingStart();
        } else {
          await postTradingStop();
        }
      } catch (err) {
        console.error('Failed to sync engine running state:', err);
      }
    };
    syncState();
  }, [isEngineRunning, dataSource]);

  // SSE event stream subscriber for agent events when live
  useEffect(() => {
    if (dataSource !== 'live') return;

    let eventSource: EventSource | null = null;
    function connectSSE() {
      eventSource = subscribeToAgentEvents(
        (backendStates: BackendAgentState[]) => {
          setAgents((prevAgents) => {
            return prevAgents.map((agent) => {
              const bState = backendStates.find((b) => b.id === agent.id);
              if (bState) {
                const timeStr = new Date().toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true });
                const newMsg = { en: bState.current_task || bState.status, ru: bState.current_task || bState.status };
                
                const getDesc = (name: string) => {
                  if (name.includes("Data") || name.includes("Research")) return "Evaluates technical indicators & chart trends";
                  if (name.includes("Sentiment")) return "Analyzes social feeds & on-chain SOPR";
                  if (name.includes("Trad") || name.includes("Strategist")) return "Sizes positions & executes final orders";
                  if (name.includes("Risk") || name.includes("Manager")) return "Enforces risk limits & vetoes bad trades";
                  return agent.description || "Autocoded Discord Agent";
                };

                const history = agent.history || [];
                const lastEntry = history[history.length - 1];
                const updatedHistory = (lastEntry && lastEntry.message.en === newMsg.en && lastEntry.status === bState.status)
                  ? history
                  : [...history, { timestamp: timeStr, message: newMsg, status: bState.status }].slice(-50);

                return {
                  ...agent,
                  name: bState.name || agent.name,
                  description: getDesc(bState.name || agent.name),
                  status: bState.status,
                  message: newMsg,
                  lastUpdated: timeStr,
                  history: updatedHistory
                };
              }
              return agent;
            });
          });
        },
        (newLog) => {
          setLogs((prev) => {
            if (prev.some((l) => l.id === newLog.id)) {
              return prev;
            }
            const next = [...prev, newLog];
            if (next.length > 50) next.shift();
            return next;
          });
        },
        (portfolio) => {
          setPortfolioSnapshot(portfolio);
          setChartData((prev) => {
            const now = new Date();
            const timeLabel = now.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', hour12: true });
            const pt = { timeLabel, equity: portfolio.equity, cash: portfolio.cash, timestamp: Date.now() };
            const updated = [...prev, pt];
            if (updated.length > 2000) updated.shift();
            return updated;
          });
        },
        () => {
          console.warn('SSE connection disconnected. Reconnecting in 5s...');
          if (eventSource) {
            eventSource.close();
          }
          setTimeout(connectSSE, 5000);
        },
        (history) => {
          setOptimizationHistory(history);
        }
      );
    }

    connectSSE();

    return () => {
      if (eventSource) {
        eventSource.close();
      }
    };
  }, [dataSource]);

  // Real-time API feeds loop (Binance + Yahoo proxy via Node Express)
  useEffect(() => {
    if (dataSource !== 'live') return;

    async function fetchMarketFeeds() {
      try {
        const feeds = await fetchMarketData(activeCryptos);
        setMarketPrices((prev) => {
          const nextPrices = { ...prev };
          Object.keys(feeds).forEach((key) => {
            nextPrices[key] = { ...prev[key], ...feeds[key] };
          });
          return nextPrices;
        });
      } catch (err) {
        console.error('Error fetching real-time market data feeds:', err);
      }
    }

    fetchMarketFeeds();
    const feedsInterval = setInterval(fetchMarketFeeds, 4000); // 4 seconds interval feeds
    return () => clearInterval(feedsInterval);
  }, [activeCryptos, dataSource]);

  // Poll portfolio snapshot in live mode to keep the chart updated
  useEffect(() => {
    if (dataSource !== 'live') return;

    async function updatePortfolioChart() {
      try {
        const portfolio = await fetchPortfolioSnapshot();
        setPortfolioSnapshot(portfolio);
        setChartData((prev) => {
          const now = new Date();
          const timeLabel = now.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', hour12: true });
          const updated = [...prev, { timeLabel, equity: portfolio.equity, cash: portfolio.cash, timestamp: Date.now() }];
          if (updated.length > 2000) updated.shift();
          return updated;
        });
      } catch (err) {
        console.error('Error polling portfolio snapshot:', err);
      }
    }

    updatePortfolioChart();
    const interval = setInterval(updatePortfolioChart, 5000); // Poll every 5 seconds
    return () => clearInterval(interval);
  }, [dataSource]);

  // Quantitative dynamic engine simulated intervals
  useEffect(() => {
    if (dataSource !== 'simulation') return;
    if (!isEngineRunning) return;

    // Convert TPM to Millisecond delay
    const tickerDelayMs = (60 / ticksPerMinute) * 1000;

    const tickerId = setInterval(() => {
      // Step 1: Fluctuate market variables gently
      setMarketPrices((prev) => {
        const nextPrices = { ...prev };
        Object.keys(nextPrices).forEach((symbol) => {
          const oldAsset = nextPrices[symbol];
          if (!oldAsset) return;
          const oldPrice = oldAsset.price;
          
          let drift = (Math.random() - 0.485) * 0.003; // Cryptocurrency standard brownian motion drift

          const newPrice = Math.max(0.001, oldPrice * (1 + drift));
          nextPrices[symbol] = {
            ...oldAsset,
            price: Number(newPrice.toFixed(symbol === 'ETH' || symbol === 'BTC' || symbol === 'BNB' || symbol === 'LTC' || symbol === 'SOL' ? 2 : 4)),
            change24h: oldAsset.change24h + drift * 100
          };
        });
        return nextPrices;
      });

      // Step 2: Recalculate portfolio paper variables & append to chart history array
      setChartData((prevHistory) => {
        const latestPt = prevHistory[prevHistory.length - 1];
        
        // Compute return using strategy allocation coefficients (purely crypto-driven)
        const btcSplit = strategy === 'DD90/10' ? 0.90 : 0.60;
        const ethSplit = strategy === 'DD90/10' ? 0.07 : 0.25;
        const altsSplit = strategy === 'DD90/10' ? 0.03 : 0.15;

        // Apply a little random drift based on weights
        const randomReturnFactor = (Math.random() - 0.48) * 0.0008;
        const nextEquityMultiplier = 1 + randomReturnFactor;
        const nextEquity = Math.round(latestPt.equity * nextEquityMultiplier);

        // Cash component fluctuation
        const cashRatio = strategy === 'DD90/10' ? 0.89 + Math.random() * 0.015 : 0.64 + Math.random() * 0.025;
        const nextCash = Math.round(nextEquity * cashRatio);

        const now = new Date();
        const nextTimeLabel = now.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', hour12: true });

        // Maintain a sliding buffer of 2000 points
        const updated = [...prevHistory, { timeLabel: nextTimeLabel, equity: nextEquity, cash: nextCash, timestamp: Date.now() }];
        if (updated.length > 2000) {
          updated.shift();
        }
        return updated;
      });

      // Step 3: Run the Multi-Agent consensus state machine tick
      setConsensusPhase((prevPhase) => {
        const nextPhase = (prevPhase + 1) % 5;
        const timeStr = getFormattedTime();

        setAgents((prevAgents) => {
          return prevAgents.map((agent) => {
            let nextStatus = agent.status;
            let nextMessage = agent.message;

            if (nextPhase === 1) {
              if (agent.id === 'agentScreener') {
                nextStatus = 'EXECUTING';
                nextMessage = {
                  en: 'EXECUTING: Screening altcoin trends and momentum...',
                  ru: 'ВЫПОЛНЕНИЕ: Анализ трендов и импульса альткоинов...'
                };
              } else if (agent.id === 'agentTechnical') {
                nextStatus = 'EXECUTING';
                nextMessage = {
                  en: 'EXECUTING: Mapping moving averages, RSI divergence loops, and liquidity traps...',
                  ru: 'ВЫПОЛНЕНИЕ: Карта скользящих средних, дивергенция RSI и ловушки ликвидности...'
                };
              } else {
                nextStatus = 'IDLE';
                nextMessage = agent.id === 'agentSentiment' 
                  ? { en: 'IDLE: Polling social indexes and order books...', ru: 'ОЖИДАНИЕ: Опрос социальных индексов и стаканов заявок...' }
                  : agent.id === 'agentTrader' 
                  ? { en: 'IDLE: Position sizing pipeline in standby...', ru: 'ОЖИРАНИЕ: Конвейер сайзинга позиций в режиме ожидания...' }
                  : { en: 'IDLE: Compliance monitors at zero utilization...', ru: 'ОЖИДАНИЕ: Мониторы комплаенса не загружены...' };
              }
            } 
            else if (nextPhase === 2) {
              if (agent.id === 'agentScreener') {
                nextStatus = 'COMPLETED';
                nextMessage = {
                  en: 'COMPLETED: Screener updated active symbols.',
                  ru: 'УСПЕШНО: Обновлены активные символы.'
                };
              }
              if (agent.id === 'agentTechnical') {
                nextStatus = 'COMPLETED';
                nextMessage = {
                  en: 'COMPLETED: High-alpha buy signal calculated. Signal indicators look positive.',
                  ru: 'УСПЕШНО: Сигнал на покупку сформирован. Индикаторы в положительной зоне.'
                };
              }
              if (agent.id === 'agentSentiment') {
                nextStatus = 'EXECUTING';
                nextMessage = {
                  en: 'EXECUTING: Scraping social accounts and parsing book order imbalance ratios...',
                  ru: 'ВЫПОЛНЕНИЕ: Парсинг социальных сетей и анализ дисбаланса книги лимитных заявок...'
                };
              }
            } 
            else if (nextPhase === 3) {
              if (agent.id === 'agentScreener') {
                nextStatus = 'IDLE';
                nextMessage = { en: 'IDLE: Waiting to screen next cycle...', ru: 'ОЖИДАНИЕ: Ожидание анализа следующего цикла...' };
              }
              if (agent.id === 'agentSentiment') {
                nextStatus = 'COMPLETED';
                nextMessage = {
                  en: 'COMPLETED: Positive index found (+0.74). Volume buy walls confirm momentum.',
                  ru: 'УСПЕШНО: Индекс доверия (+0.74). Стены покупок объемами подтверждают импульс.'
                };
              }
              if (agent.id === 'agentTrader') {
                nextStatus = 'EXECUTING';
                nextMessage = {
                  en: 'EXECUTING: Formatting immediate block quotes. Selecting optimal liquidity path...',
                  ru: 'ВЫПОЛНЕНИЕ: Спецификация блоков ордеров. Выбор оптимального пула ликвидности...'
                };
              }
            } 
            else if (nextPhase === 4) {
              if (agent.id === 'agentScreener') {
                nextStatus = 'IDLE';
                nextMessage = { en: 'IDLE: Waiting to screen next cycle...', ru: 'ОЖИДАНИЕ: Ожидание анализа следующего цикла...' };
              }
              if (agent.id === 'agentTrader') {
                nextStatus = 'EXECUTING';
                nextMessage = {
                  en: 'EXECUTING: Communicating order blocks to dark pool channels...',
                  ru: 'ВЫПОЛНЕНИЕ: Передача блока лимитных заявок в заблокированные пулы...'
                };
              }
              if (agent.id === 'agentRisk') {
                nextStatus = 'EXECUTING';
                nextMessage = {
                  en: 'EXECUTING: Confirming slippage margins, size allocation caps, and drawdown barriers...',
                  ru: 'ВЫПОЛНЕНИЕ: Экспертиза лимитов проскальзывания и волатильности портфеля...'
                };
              }
            } 
            else {
              const isAccepted = Math.random() > 0.15;
              const activeAssetSymbols = activeCryptos;
              const chosenSymbol = activeAssetSymbols[Math.floor(Math.random() * activeAssetSymbols.length)];
              const targetLatestPrice = marketPrices[chosenSymbol]?.price || (chosenSymbol === 'ETH' ? 3450 : chosenSymbol === 'SOL' ? 165 : chosenSymbol === 'BNB' ? 580 : 88200);

              if (agent.id === 'agentScreener') {
                nextStatus = 'IDLE';
                nextMessage = { en: 'IDLE: Waiting to screen next cycle...', ru: 'ОЖИДАНИЕ: Ожидание анализа следующего цикла...' };
              }
              if (isAccepted) {
                if (agent.id === 'agentRisk') {
                  nextStatus = 'COMPLETED';
                  nextMessage = {
                    en: 'COMPLETED: Order approved. Zero margin breaching thresholds detected.',
                    ru: 'УСПЕШНО: Блок одобрен. Пределы рыночных рисков не нарушены.'
                  };
                }
                if (agent.id === 'agentTrader') {
                  const actionType: 'BUY' | 'SELL' = Math.random() > 0.4 ? 'BUY' : 'SELL';
                  const freshLog = createNewLogEntry(chosenSymbol, actionType, targetLatestPrice);
                  setLogs((prevLogs) => {
                    const nextLogs = [...prevLogs, freshLog];
                    if (nextLogs.length > 50) nextLogs.shift();
                    return nextLogs;
                  });

                  nextStatus = 'COMPLETED';
                  nextMessage = {
                    en: `COMPLETED: Fully filled index block for ${chosenSymbol} at ${targetLatestPrice} USD.`,
                    ru: `УСПЕШНО: Завершен закуп блока ${chosenSymbol} по цене ${targetLatestPrice} USD.`
                  };
                }
                if (agent.id === 'agentTechnical' || agent.id === 'agentSentiment') {
                  nextStatus = 'IDLE';
                  nextMessage = agent.id === 'agentTechnical' 
                    ? { en: 'IDLE: Awaiting next price frame computation...', ru: 'ОЖИДАНИЕ: Ожидание вычисления следующего тика цен...' }
                    : { en: 'IDLE: Polling social indexes and order books...', ru: 'ОЖИДАНИЕ: Опрос социальных индексов и стаканов заявок...' };
                }
              } else {
                if (agent.id === 'agentRisk') {
                  nextStatus = 'VETOED';
                  nextMessage = {
                    en: 'VETOED: Volatility limit breached. Disabling execution block.',
                    ru: 'ВЕТО: Превышен лимит волатильности. Выполнение заблокировано.'
                  };
                }
                if (agent.id === 'agentTrader') {
                  const freshLog = createNewLogEntry(chosenSymbol, 'HOLD', targetLatestPrice);
                  setLogs((prevLogs) => {
                    const nextLogs = [...prevLogs, freshLog];
                    if (nextLogs.length > 50) nextLogs.shift();
                    return nextLogs;
                  });

                  nextStatus = 'VETOED';
                  nextMessage = {
                    en: 'VETOED: Order dropped due to auditing protection guidelines.',
                    ru: 'ОТМЕНЕНО: Заявка отклонена по регламенту защиты от проскальзывания.'
                  };
                }
                if (agent.id === 'agentTechnical' || agent.id === 'agentSentiment') {
                  nextStatus = 'IDLE';
                  nextMessage = agent.id === 'agentTechnical' 
                    ? { en: 'IDLE: Awaiting next price frame computation...', ru: 'ОЖИДАНИЕ: Ожидание вычисления следующего тика цен...' }
                    : { en: 'IDLE: Polling social indexes and order books...', ru: 'ОЖИДАНИЕ: Опрос социальных индексов и стаканов заявок...' };
                }
              }
            }

            const history = agent.history || [];
            const lastEntry = history[history.length - 1];
            const updatedHistory = (lastEntry && lastEntry.message.en === nextMessage.en && lastEntry.status === nextStatus)
              ? history
              : [...history, { timestamp: timeStr, message: nextMessage, status: nextStatus }].slice(-50);

            return {
              ...agent,
              status: nextStatus,
              message: nextMessage,
              lastUpdated: timeStr,
              history: updatedHistory
            };
          });
        });

        return nextPhase;
      });

    }, tickerDelayMs);

    return () => clearInterval(tickerId);
  }, [isEngineRunning, ticksPerMinute, strategy, marketPrices, activeCryptos]);

  const resetSimulationBalance = (balance: number) => {
    setStartingBalance(balance);
    localStorage.setItem('ottr_starting_balance', balance.toString());
    setChartData((prev) => {
      const now = new Date();
      const timeLabel = now.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', hour12: true });
      return [{ timeLabel, equity: balance, cash: balance, timestamp: Date.now() }];
    });
    
    // Clear fills execution logs
    setLogs([]);
    
    // Reset agents and their histories to default IDLE
    setAgents([
      { id: 'technical_analyst', name: 'Atlas (Technical Analyst)', status: 'IDLE', message: { en: 'IDLE: Awaiting next price frame computation...', ru: 'ОЖИДАНИЕ: Ожидание вычисления следующего тика цен...' }, lastUpdated: getFormattedTime(), history: [] },
      { id: 'sentiment_analyst', name: 'Luna (Sentiment Analyst)', status: 'IDLE', message: { en: 'IDLE: Polling social indexes and order books...', ru: 'ОЖИДАНИЕ: Опрос социальных индексов и стаканов заявок...' }, lastUpdated: getFormattedTime(), history: [] },
      { id: 'trader', name: 'Mercury (Trader)', status: 'IDLE', message: { en: 'IDLE: Position sizing pipeline in standby...', ru: 'ОЖИДАНИЕ: Конвейер сайзинга позиций находится в режиме ожидания...' }, lastUpdated: getFormattedTime(), history: [] },
      { id: 'risk_auditor', name: 'Rogue (Risk Auditor)', status: 'IDLE', message: { en: 'IDLE: Compliance monitors at zero utilization...', ru: 'ОЖИДАНИЕ: Мониторы комплаенса не загружены...' }, lastUpdated: getFormattedTime(), history: [] },
      { id: 'performance_optimizer', name: 'Zephyr (Performance Optimizer)', status: 'IDLE', message: { en: 'IDLE: Optimizing hyper-parameters...', ru: 'ОЖИДАНИЕ: Оптимизация параметров...' }, lastUpdated: getFormattedTime(), history: [] },
      { id: 'portfolio_manager', name: 'Midas (Portfolio Manager)', status: 'IDLE', message: { en: 'IDLE: Rebalancing constraints in standby...', ru: 'ОЖИДАНИЕ: Ребалансировка...' }, lastUpdated: getFormattedTime(), history: [] },
      { id: 'meeting_chair', name: 'Athena (Meeting Chair)', status: 'IDLE', message: { en: 'IDLE: No consensus meetings scheduled...', ru: 'ОЖИДАНИЕ: Собрания не запланированы...' }, lastUpdated: getFormattedTime(), history: [] },
    ]);
  };

  return (
    <div className="min-h-screen bg-[#070707] text-neutral-200 flex flex-col selection:bg-emerald-500/30 selection:text-emerald-200">
      
      {/* Top Professional Navigation Bar */}
      <header className="border-b border-neutral-900 bg-neutral-950/80 backdrop-blur-md px-6 py-4 sticky top-0 z-50">
        <div className="max-w-7xl mx-auto flex flex-col sm:flex-row items-center justify-between gap-4">
          
          <div className="flex items-center gap-3">
            <div className="p-2 bg-emerald-500/10 border border-emerald-500/20 rounded">
              <Network className="w-5 h-5 text-emerald-400" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-xs font-sans font-semibold tracking-widest text-[#ececec] uppercase">
                  {activeTranslations.terminalTitle}
                </h1>
                <span className={`text-[10px] px-2 py-0.5 rounded border font-semibold font-mono flex items-center gap-1 ${
                  dataSource === 'live'
                    ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/15'
                    : 'bg-amber-500/10 text-amber-400 border-amber-500/15'
                }`}>
                  <span className={`w-1.5 h-1.5 rounded-full ${dataSource === 'live' ? 'bg-emerald-400 animate-pulse' : 'bg-amber-400'}`} />
                  {dataSource === 'live' ? 'LIVE' : 'SIMULATION'}
                </span>
              </div>
            </div>
          </div>

          {/* Core high-frequency prices strip tracker ticker */}
          <div className="flex flex-wrap items-center gap-4 sm:gap-6">
            
            {/* Dynamic Active Crypto Tickers */}
            {activeCryptos.map((symbol) => {
              const info = marketPrices[symbol];
              if (!info) return null;

              const isDefaultCrypto = symbol === 'BTC' || symbol === 'ETH';
              const colorClass = symbol === 'BTC' 
                ? 'text-amber-500' 
                : symbol === 'ETH' 
                ? 'text-violet-400' 
                : (AVAILABLE_CRYPTOS.find((c) => c.symbol === symbol)?.colorClass || 'text-emerald-400');

              return (
                <div key={symbol} className="bg-black/40 px-3 py-1 bg-gradient-to-br from-neutral-900/50 to-neutral-950/20 rounded border border-neutral-800/80 flex flex-col gap-0.5 font-mono min-w-[110px] relative group pr-7 transition-all hover:border-neutral-700">
                  {!isDefaultCrypto && (
                    <button
                      onClick={() => removeCrypto(symbol)}
                      className="absolute top-0.5 right-1 text-neutral-500 hover:text-rose-450 font-sans text-xs transition-colors p-0.5 leading-none"
                      title="Remove Ticker"
                    >
                      ×
                    </button>
                  )}
                  <div className="flex items-center justify-between gap-2">
                    <span className={`text-[9.5px] ${colorClass} font-bold`}>{symbol}</span>
                    <span className={`text-[8.5px] ${info.change24h >= 0 ? 'text-emerald-400' : 'text-rose-450'} font-semibold`}>
                      {info.change24h >= 0 ? '▲' : '▼'} {Math.abs(info.change24h).toFixed(2)}%
                    </span>
                  </div>
                  <div className="flex items-baseline justify-between gap-3">
                    <span className="text-[11.5px] font-semibold text-neutral-100">
                      ${info.price.toLocaleString(undefined, { minimumFractionDigits: info.price < 2 ? 4 : 2, maximumFractionDigits: info.price < 2 ? 4 : 2 })}
                    </span>
                    <span className="text-[8px] text-neutral-550">
                      V: {(info.volume ?? 0) >= 1e6 ? ((info.volume ?? 0) / 1e6).toFixed(1) + 'M' : Math.round(info.volume ?? 0).toLocaleString()}
                    </span>
                  </div>
                </div>
              );
            })}

            {/* Purely Cryptocurrency dropdown picker */}
            {activeCryptos.length < AVAILABLE_CRYPTOS.length + 2 && (
              <div className="relative">
                <select
                  onChange={(e) => {
                    const val = e.target.value;
                    if (val) {
                      addCrypto(val);
                      e.target.value = ''; // Reset select
                    }
                  }}
                  className="bg-black/60 border border-neutral-800 text-[9px] font-mono text-neutral-400 hover:text-white hover:border-neutral-600 rounded px-2.5 py-1.5 focus:border-emerald-500/50 outline-none cursor-pointer h-full transition-all"
                  defaultValue=""
                >
                  <option value="" disabled>+ CRYPTO</option>
                  {AVAILABLE_CRYPTOS
                    .filter((c) => !activeCryptos.includes(c.symbol))
                    .map((c) => (
                      <option key={c.symbol} value={c.symbol}>
                        {c.symbol} - {c.name}
                      </option>
                    ))
                  }
                </select>
              </div>
            )}

          </div>

        </div>
      </header>

      {/* Main Dashboard Panel layout container */}
      <main className="flex-1 max-w-7xl w-full mx-auto p-4 sm:p-6 lg:p-8 space-y-6">
        
        {/* Overview equity curve and key metrics cards */}
        <SystemHealthPanel lang={lang} />

        <OverviewPanel
          data={chartData}
          lang={lang}
          t={activeTranslations}
          performance={portfolioSnapshot?.performance}
          risk={portfolioSnapshot?.risk}
        />

        {/* Allocation & Discord Agents Board */}
        <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
          <div className="lg:col-span-4">
            <AssetAllocation 
              strategy={strategy} 
              marketPrices={marketPrices} 
              paperEquity={chartData[chartData.length - 1]?.equity || 0}
              portfolioSnapshot={portfolioSnapshot}
              lang={lang} 
              t={activeTranslations} 
            />
          </div>
          
          <div className="lg:col-span-8">
            <AgentRoomConsole 
              agents={agents} 
              lang={lang} 
              t={activeTranslations} 
              activeTab={consoleTab}
              setActiveTab={setConsoleTab}
              inputValue={chatInput}
              setInputValue={setChatInput}
            />
          </div>
        </div>

        {/* Transaction logs table */}
        <ExecutionLogsTable 
          logs={logs} 
          lang={lang} 
          t={activeTranslations} 
          onSelectTransaction={handleSelectTransaction}
        />

      </main>

      {/* Futuristic status bar footer */}
      <footer className="mt-auto border-t border-neutral-900 bg-[#0e0e0e] py-3.5 px-6 text-[10.5px] font-mono text-neutral-500">
        <div className="max-w-7xl mx-auto flex flex-col sm:flex-row justify-between items-center gap-2">
          <div className="flex items-center gap-2">
            <span className="flex h-1.5 w-1.5 rounded-full bg-emerald-500 animate-pulse" />
            <span>SYSTEM UTC: {new Date().toISOString().replace('T', ' ').substring(0, 19)}</span>
          </div>
        </div>
      </footer>

    </div>
  );
}
