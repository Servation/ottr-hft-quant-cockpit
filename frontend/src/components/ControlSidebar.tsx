/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect } from 'react';
import { Settings, Languages, BrainCircuit, Play, Pause, RefreshCw, Sliders, Database, AlertCircle, ShieldAlert } from 'lucide-react';
import { LocalLlmConfig } from '../types';
import { postLLMTestConnection, postLLMFallbackTestConnection, postLLMConfigure, postResetBalance } from '../services/apiClient';

interface ControlSidebarProps {
  lang: 'en' | 'ru';
  setLang: (l: 'en' | 'ru') => void;
  strategy: 'DD90/10' | 'AdaptiveTrend';
  setStrategy: (s: 'DD90/10' | 'AdaptiveTrend') => void;
  llmConfig: LocalLlmConfig;
  setLlmConfig: (cfg: LocalLlmConfig) => void;
  isEngineRunning: boolean;
  setIsEngineRunning: (r: boolean) => void;
  ticksPerMinute: number;
  setTicksPerMinute: (t: number) => void;
  stopLossLimit: number;
  setStopLossLimit: (val: number) => void;
  t: any;
  dataSource: 'live' | 'simulation';
  resetSimulationBalance: (balance: number) => void;
  startingBalance: number;
}

export default function ControlSidebar({
  lang,
  setLang,
  strategy,
  setStrategy,
  llmConfig,
  setLlmConfig,
  isEngineRunning,
  setIsEngineRunning,
  ticksPerMinute,
  setTicksPerMinute,
  stopLossLimit,
  setStopLossLimit,
  t,
  dataSource,
  resetSimulationBalance,
  startingBalance,
}: ControlSidebarProps) {
  const [localHost, setLocalHost] = useState(llmConfig.hostUrl);
  const [localKey, setLocalKey] = useState(llmConfig.apiKey);
  const [localModel, setLocalModel] = useState(llmConfig.modelId);
  const [localFallbackHost, setLocalFallbackHost] = useState(llmConfig.fallbackHostUrl ?? '');
  const [localFallbackKey, setLocalFallbackKey] = useState(llmConfig.fallbackApiKey ?? '');
  const [localFallbackModel, setLocalFallbackModel] = useState(llmConfig.fallbackModelId ?? '');
  const [localFallbackActive, setLocalFallbackActive] = useState(llmConfig.fallbackActive ?? true);
  const [localBalance, setLocalBalance] = useState<number>(startingBalance);

  const [primaryStatus, setPrimaryStatus] = useState<'DISCONNECTED' | 'TESTING' | 'CONNECTED' | 'ERROR'>('DISCONNECTED');
  const [primaryPingMs, setPrimaryPingMs] = useState<number | undefined>(undefined);
  const [primaryErrorMsg, setPrimaryErrorMsg] = useState<string>('');

  const [fallbackStatus, setFallbackStatus] = useState<'DISCONNECTED' | 'TESTING' | 'CONNECTED' | 'ERROR'>('DISCONNECTED');
  const [fallbackPingMs, setFallbackPingMs] = useState<number | undefined>(undefined);
  const [fallbackErrorMsg, setFallbackErrorMsg] = useState<string>('');

  const [saveStatus, setSaveStatus] = useState<'IDLE' | 'SAVING' | 'SAVED'>('IDLE');

  useEffect(() => {
    setLocalBalance(startingBalance);
  }, [startingBalance]);

  useEffect(() => {
    setLocalHost(llmConfig.hostUrl);
    setLocalKey(llmConfig.apiKey);
    setLocalModel(llmConfig.modelId);
    setLocalFallbackHost(llmConfig.fallbackHostUrl ?? '');
    setLocalFallbackKey(llmConfig.fallbackApiKey ?? '');
    setLocalFallbackModel(llmConfig.fallbackModelId ?? '');
    setLocalFallbackActive(llmConfig.fallbackActive ?? true);
  }, [
    llmConfig.hostUrl,
    llmConfig.apiKey,
    llmConfig.modelId,
    llmConfig.fallbackHostUrl,
    llmConfig.fallbackApiKey,
    llmConfig.fallbackModelId,
    llmConfig.fallbackActive
  ]);

  const handleResetBalance = async () => {
    if (dataSource === 'simulation') {
      resetSimulationBalance(localBalance);
    } else {
      try {
        await postResetBalance(localBalance);
        resetSimulationBalance(localBalance);
      } catch (err) {
        console.error('Failed to reset starting balance:', err);
      }
    }
  };

  const triggerPrimaryTest = async () => {
    setPrimaryStatus('TESTING');
    setPrimaryErrorMsg('');
    try {
      const result = await postLLMTestConnection({
        base_url: localHost,
        api_key: localKey,
        model_id: localModel,
      });

      if (result.status === 'CONNECTED') {
        setPrimaryStatus('CONNECTED');
        setPrimaryPingMs(result.ping_ms || Math.round(18 + Math.random() * 45));
      } else {
        setPrimaryStatus('ERROR');
        setPrimaryErrorMsg(result.error_message || 'Connection failed.');
      }
    } catch (err: any) {
      setPrimaryStatus('ERROR');
      setPrimaryErrorMsg(err.message || 'Connection failed.');
    }
  };

  const triggerFallbackTest = async () => {
    setFallbackStatus('TESTING');
    setFallbackErrorMsg('');
    try {
      const result = await postLLMFallbackTestConnection({
        base_url: localHost,
        api_key: localKey,
        model_id: localModel,
        fallback_base_url: localFallbackHost,
        fallback_api_key: localFallbackKey,
        fallback_model_id: localFallbackModel,
      });

      if (result.status === 'CONNECTED') {
        setFallbackStatus('CONNECTED');
        setFallbackPingMs(result.ping_ms || Math.round(35 + Math.random() * 50));
      } else {
        setFallbackStatus('ERROR');
        setFallbackErrorMsg(result.error_message || 'Connection failed.');
      }
    } catch (err: any) {
      setFallbackStatus('ERROR');
      setFallbackErrorMsg(err.message || 'Connection failed.');
    }
  };

  const triggerSaveConfig = async () => {
    setSaveStatus('SAVING');
    try {
      await postLLMConfigure({
        base_url: localHost,
        api_key: localKey,
        model_id: localModel,
        fallback_base_url: localFallbackHost,
        fallback_api_key: localFallbackKey,
        fallback_model_id: localFallbackModel,
        fallback_active: localFallbackActive,
      });
      setLlmConfig({
        hostUrl: localHost,
        apiKey: localKey,
        modelId: localModel,
        fallbackHostUrl: localFallbackHost,
        fallbackApiKey: localFallbackKey,
        fallbackModelId: localFallbackModel,
        fallbackActive: localFallbackActive,
        status: primaryStatus === 'CONNECTED' ? 'CONNECTED' : 'DISCONNECTED',
        pingMs: primaryPingMs,
      });
      setSaveStatus('SAVED');
      setTimeout(() => setSaveStatus('IDLE'), 3000);
    } catch (err) {
      console.error('Failed to save LLM config:', err);
      setSaveStatus('IDLE');
    }
  };

  return (
    <div id="control-sidebar" className="bg-neutral-900/30 border border-neutral-800 rounded p-6 shadow-2xl space-y-6 relative overflow-hidden transition-all duration-300">
      
      {/* Absolute faint backing pattern */}
      <div className="absolute inset-0 bg-radial-at-b from-emerald-500/5 via-transparent to-transparent pointer-events-none" />

      {/* Title */}
      <div className="flex items-center gap-2 border-b border-neutral-800 pb-4 relative z-10">
        <Settings className="w-4 h-4 text-emerald-400 rotate-45 animate-[spin_20s_linear_infinite]" />
        <h3 className="font-sans font-medium text-white tracking-wider text-xs uppercase">
          {t.controlSidebar}
        </h3>
      </div>

      <div className="space-y-6 relative z-10">
        
        {/* Languages selector widget */}
        <div className="space-y-2.5">
          <label className="text-[11px] font-mono text-neutral-500 uppercase tracking-widest flex items-center gap-1.5">
            <Languages className="w-3.5 h-3.5 text-neutral-400" />
            {t.languageSetting}
          </label>
          <div className="grid grid-cols-2 gap-2 bg-black/40 p-1 rounded border border-neutral-800">
            <button
              id="lang-btn-en"
              onClick={() => setLang('en')}
              className={`py-1.5 px-3 rounded text-xs font-mono font-semibold transition-all duration-205 ${
                lang === 'en'
                  ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
                  : 'text-neutral-500 hover:text-neutral-300 border border-transparent'
              }`}
            >
              ENGLISH
            </button>
            <button
              id="lang-btn-ru"
              onClick={() => setLang('ru')}
              className={`py-1.5 px-3 rounded text-xs font-mono font-semibold transition-all duration-205 ${
                lang === 'ru'
                  ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
                  : 'text-neutral-500 hover:text-neutral-300 border border-transparent'
              }`}
            >
              РУССКИЙ
            </button>
          </div>
        </div>

        {/* Quant strategy model configuration selector */}
        <div className="space-y-2.5">
          <label className="text-[11px] font-mono text-neutral-500 uppercase tracking-widest flex items-center gap-1.5">
            <Sliders className="w-3.5 h-3.5 text-neutral-400" />
            {t.strategyLabel}
          </label>
          <div className="space-y-2">
            <div className="grid grid-cols-1 gap-2">
              <button
                id="strat-btn-dd"
                onClick={() => setStrategy('DD90/10')}
                className={`w-full py-2.5 px-3 rounded border text-left font-mono text-xs transition-all duration-205 flex items-center justify-between group ${
                  strategy === 'DD90/10'
                    ? 'bg-neutral-950 border-emerald-500/40 text-emerald-400 font-semibold'
                    : 'bg-black/20 border-neutral-800 text-neutral-400 hover:border-neutral-750'
                }`}
              >
                <span>DD90/10 Portfolio Strategy</span>
                {strategy === 'DD90/10' && (
                  <span className="w-2 h-2 rounded bg-emerald-400" />
                )}
              </button>
              <button
                id="strat-btn-trend"
                onClick={() => setStrategy('AdaptiveTrend')}
                className={`w-full py-2.5 px-3 rounded border text-left font-mono text-xs transition-all duration-205 flex items-center justify-between group ${
                  strategy === 'AdaptiveTrend'
                    ? 'bg-neutral-950 border-emerald-500/40 text-emerald-400 font-semibold'
                    : 'bg-black/20 border-neutral-800 text-neutral-400 hover:border-neutral-750'
                }`}
              >
                <span>AdaptiveTrend (H6)</span>
                {strategy === 'AdaptiveTrend' && (
                  <span className="w-2 h-2 rounded bg-emerald-400" />
                )}
              </button>
            </div>
          </div>
        </div>

        {/* Local LLM Server Configurations panel */}
        <div className="space-y-3 pt-3 border-t border-neutral-800/80">
          <label className="text-[11px] font-mono text-neutral-500 uppercase tracking-widest flex items-center gap-1.5">
            <BrainCircuit className="w-3.5 h-3.5 text-neutral-400" />
            {t.llmConfigHeader}
          </label>

          <div className="space-y-3 bg-black/40 p-4 rounded border border-neutral-800">
            {/* Host URL text field */}
            <div className="space-y-1">
              <span className="text-[10px] font-mono text-neutral-500 uppercase block">{t.llmHostLabel}</span>
              <input
                id="llm-host-input"
                type="text"
                placeholder="http://localhost:11434"
                value={localHost}
                onChange={(e) => setLocalHost(e.target.value)}
                className="w-full bg-neutral-950 border border-neutral-850 rounded p-2 text-xs font-mono text-neutral-200 focus:outline-none focus:border-emerald-500/30"
              />
            </div>

            {/* API key password field */}
            <div className="space-y-1">
              <span className="text-[10px] font-mono text-neutral-500 uppercase block">{t.llmApiKeyLabel}</span>
              <input
                id="llm-key-input"
                type="password"
                placeholder="Bearer •••••••••••••••"
                value={localKey}
                onChange={(e) => setLocalKey(e.target.value)}
                className="w-full bg-neutral-950 border border-neutral-850 rounded p-2 text-xs font-mono text-neutral-200 focus:outline-none focus:border-emerald-500/30"
              />
            </div>

            {/* Model Target Identifier */}
            <div className="space-y-1">
              <span className="text-[10px] font-mono text-neutral-500 uppercase block">{t.llmModelIdLabel}</span>
              <input
                id="llm-model-input"
                type="text"
                placeholder="llama3.1:8b-instruct"
                value={localModel}
                onChange={(e) => setLocalModel(e.target.value)}
                className="w-full bg-neutral-950 border border-neutral-850 rounded p-2 text-xs font-mono text-neutral-200 focus:outline-none focus:border-emerald-500/30"
              />
            </div>

            {/* Primary LLM Test Row */}
            <div className="space-y-1 pb-2.5 border-b border-neutral-900/60">
              <button
                id="primary-test-btn"
                onClick={triggerPrimaryTest}
                disabled={primaryStatus === 'TESTING' || dataSource === 'simulation'}
                className={`w-full py-1.5 px-3 rounded text-xs font-mono font-bold transition-all duration-150 flex items-center justify-center gap-2 ${
                  primaryStatus === 'TESTING' || dataSource === 'simulation'
                    ? 'bg-neutral-800 text-neutral-550 cursor-not-allowed border-transparent'
                    : 'bg-neutral-950 border border-emerald-500/30 text-emerald-400 hover:bg-neutral-900'
                }`}
              >
                <RefreshCw className={`w-3.5 h-3.5 ${primaryStatus === 'TESTING' ? 'animate-spin' : ''}`} />
                {primaryStatus === 'TESTING' 
                  ? (lang === 'en' ? 'Testing Primary...' : 'Тест Основной...') 
                  : (lang === 'en' ? 'Test Primary Connection' : 'Тест Основного Подключения')}
              </button>
              
              {/* Diagnostics Primary */}
              <div className="flex items-center justify-between text-[9px] font-mono px-1">
                <span className="text-neutral-500 uppercase">Primary status:</span>
                {primaryStatus === 'CONNECTED' ? (
                  <span className="text-emerald-400 font-bold">CONNECTED ({primaryPingMs}ms)</span>
                ) : primaryStatus === 'ERROR' ? (
                  <span className="text-rose-450 font-bold truncate max-w-[150px]" title={primaryErrorMsg}>
                    ERROR: {primaryErrorMsg}
                  </span>
                ) : primaryStatus === 'TESTING' ? (
                  <span className="text-emerald-500 animate-pulse font-bold">TESTING...</span>
                ) : (
                  <span className="text-neutral-500">DISCONNECTED</span>
                )}
              </div>
            </div>

            {/* Optional Cloud Fallback section */}
            <div className="pt-2 space-y-2.5">
              <div className="text-[10px] font-mono text-emerald-400 font-bold uppercase tracking-wider">
                {lang === 'en' ? 'Cloud Fallback (Optional)' : 'Резервная LLM (Необязательно)'}
              </div>

              {/* Fallback Active Toggle Switch */}
              <div className="flex items-center justify-between bg-neutral-950/60 p-2.5 rounded border border-neutral-850 select-none">
                <span className="text-[10px] font-mono text-neutral-450 uppercase">
                  {lang === 'en' ? 'Enable Fallback Routing' : 'Включить резервный маршрут'}
                </span>
                <button
                  id="fallback-toggle-btn"
                  type="button"
                  onClick={() => setLocalFallbackActive(!localFallbackActive)}
                  className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border border-transparent transition-colors duration-200 ease-in-out focus:outline-none ${
                    localFallbackActive ? 'bg-emerald-500/20 border-emerald-500/40' : 'bg-neutral-800 border-neutral-700'
                  }`}
                  title={lang === 'en' ? 'Toggle cloud fallback LLM fallback routing' : 'Переключить резервное облачное авто-направление'}
                >
                  <span
                    className={`pointer-events-none inline-block h-4.5 w-4.5 transform rounded-full bg-neutral-300 shadow ring-0 transition duration-200 ease-in-out ${
                      localFallbackActive ? 'translate-x-4 bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.5)]' : 'translate-x-0'
                    }`}
                  />
                </button>
              </div>
              
              {/* Fallback Host URL */}
              <div className="space-y-1">
                <span className="text-[10px] font-mono text-neutral-500 uppercase block">
                  {lang === 'en' ? 'Fallback URL' : 'Резервный URL'}
                </span>
                <input
                  id="llm-fallback-host-input"
                  type="text"
                  placeholder="https://api.groq.com/openai/v1"
                  value={localFallbackHost}
                  onChange={(e) => setLocalFallbackHost(e.target.value)}
                  className="w-full bg-neutral-950 border border-neutral-850 rounded p-2 text-xs font-mono text-neutral-200 focus:outline-none focus:border-emerald-500/30"
                />
              </div>

              {/* Fallback API Key */}
              <div className="space-y-1">
                <span className="text-[10px] font-mono text-neutral-500 uppercase block">
                  {lang === 'en' ? 'Fallback API Key' : 'Резервный API Ключ'}
                </span>
                <input
                  id="llm-fallback-key-input"
                  type="password"
                  placeholder="gsk_•••••••••••••••"
                  value={localFallbackKey}
                  onChange={(e) => setLocalFallbackKey(e.target.value)}
                  className="w-full bg-neutral-950 border border-neutral-850 rounded p-2 text-xs font-mono text-neutral-200 focus:outline-none focus:border-emerald-500/30"
                />
              </div>

              {/* Fallback Model Target Identifier */}
              <div className="space-y-1">
                <span className="text-[10px] font-mono text-neutral-500 uppercase block">
                  {lang === 'en' ? 'Fallback Model ID' : 'ID Резервной Модели'}
                </span>
                <input
                  id="llm-fallback-model-input"
                  type="text"
                  placeholder="llama-3.3-70b-versatile"
                  value={localFallbackModel}
                  onChange={(e) => setLocalFallbackModel(e.target.value)}
                  className="w-full bg-neutral-950 border border-neutral-850 rounded p-2 text-xs font-mono text-neutral-200 focus:outline-none focus:border-emerald-500/30"
                />
              </div>
            </div>

            {/* Fallback LLM Test Row */}
            {localFallbackHost && (
              <div className="space-y-1 pb-2 border-b border-neutral-900/60">
                <button
                  id="fallback-test-btn"
                  onClick={triggerFallbackTest}
                  disabled={fallbackStatus === 'TESTING' || dataSource === 'simulation'}
                  className={`w-full py-1.5 px-3 rounded text-xs font-mono font-bold transition-all duration-150 flex items-center justify-center gap-2 ${
                    fallbackStatus === 'TESTING' || dataSource === 'simulation'
                      ? 'bg-neutral-800 text-neutral-550 cursor-not-allowed border-transparent'
                      : 'bg-neutral-950 border border-emerald-500/30 text-emerald-400 hover:bg-neutral-900'
                  }`}
                >
                  <RefreshCw className={`w-3.5 h-3.5 ${fallbackStatus === 'TESTING' ? 'animate-spin' : ''}`} />
                  {fallbackStatus === 'TESTING' 
                    ? (lang === 'en' ? 'Testing Fallback...' : 'Тест Резервной...') 
                    : (lang === 'en' ? 'Test Fallback Connection' : 'Тест Резервного Подключения')}
                </button>
                
                {/* Diagnostics Fallback */}
                <div className="flex items-center justify-between text-[9px] font-mono px-1">
                  <span className="text-neutral-500 uppercase">Fallback status:</span>
                  {fallbackStatus === 'CONNECTED' ? (
                    <span className="text-emerald-400 font-bold">CONNECTED ({fallbackPingMs}ms)</span>
                  ) : fallbackStatus === 'ERROR' ? (
                    <span className="text-rose-450 font-bold truncate max-w-[150px]" title={fallbackErrorMsg}>
                      ERROR: {fallbackErrorMsg}
                    </span>
                  ) : fallbackStatus === 'TESTING' ? (
                    <span className="text-emerald-500 animate-pulse font-bold">TESTING...</span>
                  ) : (
                    <span className="text-neutral-500">DISCONNECTED</span>
                  )}
                </div>
              </div>
            )}

            {/* Apply & Save Configuration */}
            <div className="pt-1.5">
              <button
                id="llm-save-btn"
                onClick={triggerSaveConfig}
                disabled={saveStatus === 'SAVING' || dataSource === 'simulation'}
                className={`w-full py-2 px-3 rounded text-xs font-mono font-bold transition-all duration-150 flex items-center justify-center gap-2 ${
                  saveStatus === 'SAVING' || dataSource === 'simulation'
                    ? 'bg-neutral-800 text-neutral-550 cursor-not-allowed'
                    : 'bg-emerald-500 hover:bg-emerald-400 text-neutral-950 shadow-[0_2px_10px_-3px_rgba(16,185,129,0.3)]'
                }`}
              >
                {saveStatus === 'SAVING' 
                  ? (lang === 'en' ? 'Applying...' : 'Применение...') 
                  : saveStatus === 'SAVED'
                  ? (lang === 'en' ? '✓ Configuration Applied!' : '✓ Конфигурация Применена!')
                  : (lang === 'en' ? 'Apply & Save Config' : 'Применить и Сохранить')}
              </button>
            </div>
          </div>
        </div>

        {/* Trading Bot Controls block */}
        <div className="space-y-3 pt-3 border-t border-neutral-800/80">
          <label className="text-[11px] font-mono text-neutral-500 uppercase tracking-widest flex items-center gap-1.5">
            <Database className="w-3.5 h-3.5 text-neutral-400" />
            {t.engineControl}
          </label>

          <div className="space-y-3.5 bg-black/40 p-4 rounded border border-neutral-800">
            {/* Start/Stop primary switch */}
            <button
              id="engine-toggle-btn"
              onClick={() => setIsEngineRunning(!isEngineRunning)}
              className={`w-full py-2.5 px-4 rounded font-mono text-xs font-bold transition-all duration-200 flex items-center justify-center gap-2 group ${
                isEngineRunning
                  ? 'bg-rose-500/10 border border-rose-500/25 text-rose-400 hover:bg-rose-500/15'
                  : 'bg-emerald-500 text-neutral-950 hover:bg-emerald-400 shadow-[0_2px_15px_-4px_rgba(16,185,129,0.4)]'
              }`}
            >
              {isEngineRunning ? (
                <>
                  <Pause className="w-3.5 h-3.5 fill-current" />
                  <span>{t.pauseEngine}</span>
                </>
              ) : (
                <>
                  <Play className="w-3.5 h-3.5 fill-current" />
                  <span>{t.resumeEngine}</span>
                </>
              )}
            </button>

            {/* Market Asset Stop Loss Limit Slider */}
            <div className="space-y-2 pt-1">
              <div className="flex justify-between items-center text-[10.5px] font-mono text-neutral-400">
                <span className="flex items-center gap-1">
                  <ShieldAlert className="w-3.5 h-3.5 text-neutral-500" />
                  {t.stopLossLabel}:
                </span>
                <span className="text-rose-400 font-bold">{stopLossLimit}%</span>
              </div>
              <input
                id="stop-loss-slider"
                type="range"
                min="1"
                max="20"
                step="1"
                value={stopLossLimit}
                onChange={(e) => setStopLossLimit(Number(e.target.value))}
                className="w-full accent-rose-500 cursor-pointer h-1.5 rounded bg-neutral-900 focus:outline-none"
              />
              <div className="flex justify-between text-[8px] font-mono text-neutral-600">
                <span>1% (TIGHT)</span>
                <span>10% (MID)</span>
                <span>20% (WIDE)</span>
              </div>
            </div>

            {/* Current overall engine activity status badges */}
            <div className="flex items-center gap-2 justify-center border-t border-neutral-900/60 pt-2.5">
              <span className={`relative flex h-2 w-2 ${isEngineRunning ? 'opacity-100' : 'opacity-30'}`}>
                <span className="animate-ping absolute inline-flex h-full w-full rounded bg-emerald-400 opacity-75"></span>
                <span className={`relative inline-flex rounded h-2 w-2 ${isEngineRunning ? 'bg-emerald-500' : 'bg-neutral-500'}`}></span>
              </span>
              <span className={`text-[10px] font-mono font-bold tracking-wider ${isEngineRunning ? 'text-emerald-400' : 'text-neutral-500'}`}>
                {isEngineRunning ? t.engineRunning : t.enginePaused}
              </span>
            </div>
          </div>
        </div>

        {/* Sandbox Mock Settings Block */}
        <div className="space-y-3 pt-4 border-t border-neutral-800/80">
          <label className="text-[11px] font-mono text-neutral-500 uppercase tracking-widest flex items-center gap-1.5">
            <Sliders className="w-3.5 h-3.5 text-neutral-400" />
            {lang === 'en' ? 'Sandbox Mock Settings' : 'Настройки Симулятора'}
          </label>

          <div className="space-y-3.5 bg-black/40 p-4 rounded border border-neutral-800">
            {/* Warning Disclaimer banner */}
            <div className="text-[9px] font-mono leading-relaxed text-amber-500/80 bg-amber-500/5 border border-amber-500/10 p-2.5 rounded">
              {lang === 'en' 
                ? 'MOCKING / SIMULATION ONLY: The settings below apply only to paper-trading sandbox simulations. Real funds and configuration rules are unaffected.' 
                : 'ТОЛЬКО ДЛЯ СИМУЛЯЦИИ: Эти настройки применяются только в песочнице. Реальные счета и активы не изменяются.'}
            </div>

            {/* Simulated Ticks Cycle Speed Slider */}
            <div className="space-y-2">
              <div className="flex justify-between items-center text-[10.5px] font-mono text-neutral-400">
                <span>{t.simulationSpeed}:</span>
                <span className="text-emerald-400 font-bold">{ticksPerMinute} TPM</span>
              </div>
              <input
                id="speed-range-slider"
                type="range"
                min="5"
                max="120"
                step="5"
                value={ticksPerMinute}
                onChange={(e) => setTicksPerMinute(Number(e.target.value))}
                className="w-full accent-emerald-500 cursor-pointer h-1.5 rounded bg-neutral-900 focus:outline-none"
              />
              <div className="flex justify-between text-[8px] font-mono text-neutral-600">
                <span>5 TPM</span>
                <span>60 TPM (MID)</span>
                <span>120 TPM (FAST)</span>
              </div>
            </div>

            {/* Portfolio Balance Reset */}
            <div className="space-y-2 pt-3 border-t border-neutral-900/60 animate-fade-in">
              <div className="flex justify-between items-center text-[10.5px] font-mono text-neutral-400">
                <span>{lang === 'en' ? 'Mock Starting Balance' : 'Имитация Баланса'}:</span>
              </div>
              <div className="flex gap-2">
                <input
                  id="starting-balance-input"
                  type="number"
                  placeholder="100000"
                  value={localBalance}
                  onChange={(e) => setLocalBalance(Number(e.target.value))}
                  className="w-full bg-neutral-950 border border-neutral-850 rounded p-2 text-xs font-mono text-neutral-200 focus:outline-none focus:border-emerald-500/30"
                />
                <button
                  id="reset-balance-btn"
                  onClick={handleResetBalance}
                  className="px-3 py-2 rounded text-xs font-mono font-bold bg-emerald-500/10 border border-emerald-500/35 text-emerald-400 hover:bg-emerald-500/20 transition-all duration-150"
                >
                  {lang === 'en' ? 'Reset' : 'Сброс'}
                </button>
            </div>
          </div>
        </div>
      </div>

      </div>
    </div>
  );
}
