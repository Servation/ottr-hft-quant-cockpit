/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useRef, useEffect } from 'react';
import { TradingAgentState, AgentStatusType, ChatMessage } from '../types';
import { postAgentChat } from '../services/apiClient';
import { Network, Terminal, Cpu, Clock, CheckCircle2, AlertTriangle, PlayCircle, Eye } from 'lucide-react';

interface AgentRoomConsoleProps {
  agents: TradingAgentState[];
  lang: 'en' | 'ru';
  t: any;
  activeTab: 'telemetry' | 'terminal';
  setActiveTab: (tab: 'telemetry' | 'terminal') => void;
  inputValue: string;
  setInputValue: (val: string) => void;
}

// Technical parameters to expose interactively for added depth
const agentParams: Record<string, Record<string, any>> = {
  agentTechnical: {
    en: { "Indicators": "EMA-20, EMA-50, RSI(14)", "Overbought Offset": "RSI > 72", "Oversold Offset": "RSI < 28", "Execution Loop": "H6 Momentum Walk" },
    ru: { "Индикаторы": "EMA-20, EMA-50, RSI(14)", "Уровень Перекупленности": "RSI > 72", "Уровень Перепроданности": "RSI < 28", "Цикл Исполнения": "Торговый Моментум H6" }
  },
  agentSentiment: {
    en: { "Scrapers": "X feeds, CryptoPanic, Reddit Books", "Sentiment Weight": "0.45 Alpha", "Current Score": "+0.68 Dynamic", "Imbalance Factor": "1.28 Delta" },
    ru: { "Парсинг-Каналы": "X ленты, CryptoPanic, Reddit Books", "Вес Сентимента": "0.45 Alpha", "Текущий Индекс": "+0.68 Динам.", "Дисбаланс Стакана": "1.28 Delta" }
  },
  agentTrader: {
    en: { "Order Block Size": "0.25 BTC / 4 ETH", "Dark Pool Route": "Direct HFT Inbound", "Time-In-Force": "IOC (Immediate-Or-Cancel)", "VWAP Offset Limit": "3 bps max" },
    ru: { "Размер Блока Заявок": "0.25 BTC / 4 ETH", "Маршрутизатор": "Теневой пул HFT", "Тип Исполнения": "IOC (Немедленно/Отмена)", "Предел VWAP": "макс. 3 б.п." }
  },
  agentRisk: {
    en: { "Max Leverage": "1.0x Paper unleveraged", "Drawdown Guardrail": "4.5% Cap", "Max Slippage Margin": "0.08% Block limit", "Kill Switch Trigger": "+10% Max slip" },
    ru: { "Кредитное Плечо": "1.0x (Без плеча)", "Защита от Просадки": "Лимит 4.5%", "Лимит Слиппиджа": "макс. 0.08%", "Аварийный Триггер": "+10% слиппиджа" }
  },
  agentScreener: {
    en: { "Candidates": "10 Major Altcoins", "Selection Weight": "24h change % momentum", "Active Assets": "2 dynamic consensus slots", "Cycle Frequency": "Every 10 rounds" },
    ru: { "Кандидаты": "10 ведущих альткоинов", "Критерий выбора": "Импульс изменения за 24ч", "Активные активы": "2 динамических слота", "Период обновления": "Каждые 10 раундов" }
  }
};

export default function AgentRoomConsole({ 
  agents, 
  lang, 
  t,
  activeTab,
  setActiveTab,
  inputValue,
  setInputValue
}: AgentRoomConsoleProps) {
  const [selectedAgentId, setSelectedAgentId] = useState<string>('agentTechnical');
  const [chatHistory, setChatHistory] = useState<ChatMessage[]>(() => [
    {
      sender: 'agent',
      text: lang === 'en' 
        ? "Welcome to the Command Terminal. Ask me questions about recent transactions or write commands like 'Set stop loss to 5%' or 'Pause trading'." 
        : "Добро пожаловать в командный терминал. Спросите меня о сделках или напишите команду, например: 'Установить стоп-лосс на 5%' или 'Приостановить торговлю'.",
      timestamp: new Date().toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
    }
  ]);
  const [isSending, setIsSending] = useState<boolean>(false);
  const scrollContainerRef = useRef<HTMLDivElement>(null);

  // Scroll to bottom on updates
  useEffect(() => {
    if (scrollContainerRef.current) {
      scrollContainerRef.current.scrollTop = scrollContainerRef.current.scrollHeight;
    }
  }, [chatHistory, isSending]);

  const handleSendMessage = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!inputValue.trim() || isSending) return;

    const userText = inputValue;
    setInputValue('');
    setIsSending(true);

    const userMessage: ChatMessage = {
      sender: 'user',
      text: userText,
      timestamp: new Date().toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
    };

    setChatHistory((prev) => [...prev, userMessage]);

    try {
      const response = await postAgentChat(userText, chatHistory);
      const agentMessage: ChatMessage = {
        sender: 'agent',
        text: response.text,
        timestamp: new Date().toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
      };
      setChatHistory((prev) => [...prev, agentMessage]);
    } catch (err) {
      console.error('Chat error:', err);
      const errorMessage: ChatMessage = {
        sender: 'agent',
        text: lang === 'en' 
          ? "Failed to communicate with the model. Please check gateway connection." 
          : "Ошибка связи с моделью. Проверьте подключение к шлюзу.",
        timestamp: new Date().toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
      };
      setChatHistory((prev) => [...prev, errorMessage]);
    } finally {
      setIsSending(false);
    }
  };

  const getStatusBadge = (status: AgentStatusType) => {
    switch (status) {
      case 'IDLE':
        return (
          <span className="flex items-center gap-1 text-[10px] font-mono font-medium px-2 py-0.5 rounded border bg-neutral-950 text-neutral-400 border-neutral-800">
            <span className="w-1.5 h-1.5 bg-neutral-500 rounded-full" />
            {t.statusIdle}
          </span>
        );
      case 'EXECUTING':
        return (
          <span className="flex items-center gap-1 text-[10px] font-mono font-medium px-2 py-0.5 rounded border bg-emerald-500/10 text-emerald-400 border-emerald-500/20">
            <span className="w-1.5 h-1.5 bg-emerald-400 rounded-full animate-ping" />
            {t.statusExecuting}
          </span>
        );
      case 'COMPLETED':
        return (
          <span className="flex items-center gap-1 text-[10px] font-mono font-medium px-2 py-0.5 rounded border bg-teal-500/10 text-teal-400 border-teal-500/20">
            <span className="w-1.5 h-1.5 bg-teal-400 rounded-full" />
            {t.statusCompleted}
          </span>
        );
      case 'VETOED':
        return (
          <span className="flex items-center gap-1 text-[10px] font-mono font-medium px-2 py-0.5 rounded border bg-rose-500/10 text-rose-400 border-rose-500/20">
            <span className="w-1.5 h-1.5 bg-rose-400 rounded-full" />
            {t.statusVetoed}
          </span>
        );
      default:
        return null;
    }
  };

  const getAgentName = (key: string) => {
    return t[key] || key;
  };

  const selectedAgent = agents.find((a) => a.id === selectedAgentId) || agents[0];

  return (
    <div id="agents-console" className="bg-neutral-900/30 border border-neutral-800 rounded p-6 shadow-2xl space-y-6 relative overflow-hidden transition-all duration-300">
      
      {/* Visual top microgrid decorative line */}
      <div className="absolute inset-x-0 top-0 h-[1px] bg-gradient-to-r from-transparent via-neutral-800 to-transparent" />

      {/* Title block */}
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <Network className="w-4 h-4 text-emerald-400" />
          <h3 className="font-sans font-medium text-white tracking-tight uppercase text-xs">
            {t.agentRoomConsole}
          </h3>
        </div>
        <div className="flex items-center gap-1 bg-black/40 border border-neutral-800 px-2.5 py-1 rounded text-[10px] font-mono text-neutral-400">
          <Terminal className="w-3.5 h-3.5 text-neutral-500" />
          <span>COOPERATIVE RAFT SYSTEM</span>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
        
        {/* Left Side: Dynamic Agent State Monitor Grid */}
        <div className="lg:col-span-7 space-y-3.5">
          {agents.map((agent) => {
            const isSelected = agent.id === selectedAgentId;
            return (
              <div
                key={agent.id}
                onClick={() => setSelectedAgentId(agent.id)}
                className={`p-4 rounded border transition-all duration-200 cursor-pointer flex flex-col justify-between gap-2 group relative overflow-hidden ${
                  isSelected 
                    ? 'bg-neutral-900/50 border-neutral-700 shadow-md' 
                    : 'bg-black/20 border-neutral-800/80 hover:bg-neutral-900/20 hover:border-neutral-800'
                }`}
              >
                {/* Background active node accent */}
                {isSelected && (
                  <div className="absolute top-0 right-0 w-24 h-24 bg-emerald-500/5 rounded-full blur-xl pointer-events-none" />
                )}

                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2.5">
                    <div className={`p-1.5 rounded border transition-all duration-200 ${
                      agent.status !== 'IDLE'
                        ? 'bg-emerald-950/20 border-emerald-500/30 text-emerald-400 shadow-[0_0_10px_rgba(16,185,129,0.1)]'
                        : isSelected
                        ? 'bg-neutral-900 text-neutral-400 border-neutral-700'
                        : 'bg-neutral-950 border-neutral-800 text-neutral-500 group-hover:text-neutral-300'
                    }`}>
                      <Cpu className={`w-4 h-4 ${agent.status !== 'IDLE' ? 'animate-pulse' : ''}`} />
                    </div>
                    <div>
                      <div className="text-xs font-sans font-medium text-neutral-200 flex items-center gap-1.5">
                        {getAgentName(agent.nameKey)}
                      </div>
                      <div className="text-[10px] font-sans text-neutral-500 leading-snug mt-0.5">
                        {t[agent.id + 'Desc'] || agent.id}
                      </div>
                    </div>
                  </div>
                </div>

                <div className="mt-1 text-[11px] font-mono leading-relaxed bg-black/40 p-2 rounded border border-neutral-900 text-neutral-300 h-12 overflow-y-auto custom-scrollbar">
                  <span className="text-neutral-500 tracking-wider">LOG_STREAM:</span> {agent.message[lang]}
                </div>

                <div className="flex justify-end text-[9px] font-mono text-neutral-500 mt-1">
                  <span className="flex items-center gap-1">
                    <Clock className="w-2.5 h-2.5" />
                    {agent.lastUpdated}
                  </span>
                </div>
              </div>
            );
          })}
        </div>

        {/* Right Side: Microservice Static Telemetry Core Terminal */}
        <div className="lg:col-span-5 flex flex-col">
          <div className="bg-black/40 border border-neutral-800 rounded p-5 flex flex-col justify-between relative overflow-hidden h-[450px] lg:h-[685px]">
            
            {/* Background glowing telemetry grid */}
            <div className="absolute inset-0 bg-[linear-gradient(rgba(10,10,10,0)_90%,rgba(16,185,129,0.02)_100%)] pointer-events-none" />
            
            <div className="flex flex-col flex-1 min-h-0 justify-between">
              <div className="flex flex-col flex-1 min-h-0">
                {/* Tabs Selector Header */}
                <div className="flex border-b border-neutral-900 mb-4 shrink-0 font-mono text-[10.5px]">
                  <button
                    onClick={() => setActiveTab('telemetry')}
                    className={`px-4 py-2 border-b-2 font-bold transition-colors ${
                      activeTab === 'telemetry' 
                        ? 'border-emerald-500 text-emerald-400 bg-neutral-950/20' 
                        : 'border-transparent text-neutral-500 hover:text-neutral-300'
                    }`}
                  >
                    {t.telemetryLogsTab}
                  </button>
                  <button
                    onClick={() => setActiveTab('terminal')}
                    className={`px-4 py-2 border-b-2 font-bold transition-colors ${
                      activeTab === 'terminal' 
                        ? 'border-emerald-500 text-emerald-400 bg-neutral-950/20' 
                        : 'border-transparent text-neutral-500 hover:text-neutral-300'
                    }`}
                  >
                    {t.commandTerminalTab}
                  </button>
                </div>

                {activeTab === 'telemetry' ? (
                  /* Telemetry Logs Panel */
                  <div className="flex flex-col flex-1 min-h-0 space-y-2 text-xs font-mono">
                    <div className="text-neutral-500 block uppercase text-[10px] tracking-wider shrink-0 mb-1">
                      {lang === 'en' ? 'NODE LOG STREAM: ' : 'ЛОГ НОДЫ: '}{getAgentName(selectedAgent.nameKey)}
                    </div>
                    
                    <div className="bg-neutral-950/70 p-3.5 rounded border border-neutral-850/80 text-neutral-350 leading-relaxed text-[10.5px] overflow-y-auto flex-1 min-h-0 space-y-2.5 custom-scrollbar">
                      {selectedAgent.history && selectedAgent.history.length > 0 ? (
                        [...selectedAgent.history].reverse().map((log, idx) => (
                          <div key={idx} className="border-b border-neutral-900 pb-2 last:border-0 last:pb-0">
                            <div className="flex justify-between text-[9px] text-neutral-500 mb-0.5 font-mono">
                              <span className={`font-semibold ${
                                log.status === 'EXECUTING' ? 'text-emerald-400' :
                                log.status === 'COMPLETED' ? 'text-teal-400' :
                                log.status === 'VETOED' ? 'text-rose-450' : 'text-neutral-500'
                              }`}>{log.status}</span>
                              <span>{log.timestamp}</span>
                            </div>
                            <div className="text-neutral-200">{log.message[lang]}</div>
                          </div>
                        ))
                      ) : (
                        <div className="text-neutral-500 italic text-center py-10 font-mono">
                          {lang === 'en' ? 'No historical logs recorded.' : 'История отсутствуют.'}
                        </div>
                      )}
                    </div>
                  </div>
                ) : (
                  /* Command Terminal Chat Panel */
                  <div className="flex-1 flex flex-col min-h-0">
                    <div ref={scrollContainerRef} className="flex-1 min-h-0 overflow-y-auto space-y-3.5 pr-1 py-1 font-mono text-[11px] custom-scrollbar">
                      {chatHistory.map((msg, i) => (
                        <div key={i} className={`flex flex-col ${msg.sender === 'user' ? 'items-end' : 'items-start'}`}>
                          <div className={`max-w-[90%] rounded p-3 leading-relaxed border ${
                            msg.sender === 'user'
                              ? 'bg-emerald-500/5 border-emerald-500/15 text-emerald-350'
                              : 'bg-neutral-950/70 border-neutral-850 text-neutral-300'
                          }`}>
                            {msg.text}
                          </div>
                          <span className="text-[8px] text-neutral-600 mt-1 px-1">
                            {msg.timestamp}
                          </span>
                        </div>
                      ))}
                      {isSending && (
                        <div className="flex items-center gap-1.5 text-neutral-550 italic text-[10.5px] px-1 animate-pulse font-mono">
                          <span className="relative flex h-1.5 w-1.5">
                            <span className="animate-ping absolute inline-flex h-full w-full rounded bg-emerald-400 opacity-75"></span>
                            <span className="relative inline-flex rounded h-1.5 w-1.5 bg-emerald-500"></span>
                          </span>
                          {lang === 'en' ? 'Model thinking...' : 'Модель думает...'}
                        </div>
                      )}
                    </div>

                    <form onSubmit={handleSendMessage} className="flex gap-2 border-t border-neutral-900 pt-3.5 mt-3 shrink-0">
                      <input
                        type="text"
                        value={inputValue}
                        onChange={(e) => setInputValue(e.target.value)}
                        disabled={isSending}
                        placeholder={t.chatInputPlaceholder}
                        className="flex-1 bg-black/60 border border-neutral-850 hover:border-neutral-750 focus:border-emerald-500/60 rounded px-3 py-2 text-xs font-mono text-neutral-200 placeholder-neutral-600 focus:outline-none transition-all disabled:opacity-50"
                      />
                      <button
                        type="submit"
                        disabled={isSending || !inputValue.trim()}
                        className="bg-emerald-500/10 hover:bg-emerald-500/20 text-emerald-400 border border-emerald-500/20 hover:border-emerald-500/35 px-4 py-2 rounded text-xs font-mono font-bold transition-all disabled:opacity-30 flex items-center gap-1"
                      >
                        {t.chatSend}
                      </button>
                    </form>
                  </div>
                )}
              </div>

              <div className="mt-4 pt-3 border-t border-neutral-900/60 flex items-center text-[10px] font-mono text-neutral-500 shrink-0">
                <span className="flex items-center gap-1 text-neutral-400">
                  <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
                  CONVERGENT consensus
                </span>
              </div>
            </div>

          </div>
        </div>

      </div>

      {/* Heartbeat sync ticker bar */}
      <div className="text-[10.5px] font-mono text-neutral-500 flex items-center bg-black/40 p-3 rounded border border-neutral-800/80">
        <span className="flex items-center gap-1.5">
          <span className="w-2.5 h-2.5 rounded-full bg-emerald-550 relative flex">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
            <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-emerald-500"></span>
          </span>
          {t.lastAgentPulse} <span className="text-neutral-350">{agents[0]?.lastUpdated || 'SYNCED'}</span>
        </span>
      </div>

    </div>
  );
}
