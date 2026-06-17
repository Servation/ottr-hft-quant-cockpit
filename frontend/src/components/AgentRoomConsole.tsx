/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useRef, useEffect } from 'react';
import { TradingAgentState, AgentStatusType, ChatMessage } from '../types';
import { postAgentChat } from '../services/apiClient';
import { Terminal, Cpu, Clock, ChevronRight } from 'lucide-react';

interface AgentRoomConsoleProps {
  agents: TradingAgentState[];
  lang: 'en' | 'ru';
  t: any;
  activeTab: 'telemetry' | 'terminal';
  setActiveTab: (tab: 'telemetry' | 'terminal') => void;
  inputValue: string;
  setInputValue: (val: string) => void;
}

export default function AgentRoomConsole({ 
  agents, 
  lang, 
  t,
  inputValue,
  setInputValue
}: AgentRoomConsoleProps) {
  const [chatHistory, setChatHistory] = useState<ChatMessage[]>(() => [
    {
      sender: 'agent',
      text: "Welcome to the CEO Terminal. Issue your directives here to coordinate the 8-agent Raft consensus system.",
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
        text: "Failed to communicate with the Discord bridge proxy.",
        timestamp: new Date().toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
      };
      setChatHistory((prev) => [...prev, errorMessage]);
    } finally {
      setIsSending(false);
    }
  };

  const getStatusColor = (status: AgentStatusType) => {
    switch (status) {
      case 'IDLE': return 'text-neutral-500 bg-neutral-800';
      case 'EXECUTING': return 'text-black bg-[#00ff9d]';
      case 'COMPLETED': return 'text-black bg-[#00d0ff]';
      case 'VETOED': return 'text-white bg-[#ff3366]';
      default: return 'text-neutral-500 bg-neutral-800';
    }
  };

  return (
    <div className="bg-[#0a0a0a] border-2 border-[#1a1a1a] p-6 space-y-6 flex flex-col">
      
      {/* Title block */}
      <div className="flex items-center justify-between border-b-2 border-[#1a1a1a] pb-4">
        <div className="flex items-center gap-2">
          <Terminal className="w-5 h-5 text-[#00ff9d]" />
          <h3 className="font-mono font-bold text-white uppercase text-sm tracking-widest">
            {t.agentRoomConsole || "DISCORD AGENT CONSENSUS GRID"}
          </h3>
        </div>
        <div className="flex items-center gap-2">
          <span className="flex h-2 w-2 rounded-full bg-[#00ff9d] animate-pulse" />
          <span className="text-[10px] font-mono font-bold text-[#00ff9d] tracking-widest">SYSTEM ONLINE</span>
        </div>
      </div>

      {/* 8-Agent Grid - Flat Design */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {agents.map((agent) => {
          return (
            <div
              key={agent.id}
              className="bg-[#111111] border-2 border-[#222222] p-4 flex flex-col gap-3 transition-colors hover:border-[#333333]"
            >
              <div className="flex items-start justify-between">
                <div className="flex items-center gap-2.5">
                  <div className="bg-[#1a1a1a] p-1.5 border border-[#333]">
                    <Cpu className={`w-4 h-4 ${agent.status !== 'IDLE' ? 'text-[#00ff9d]' : 'text-neutral-500'}`} />
                  </div>
                  <div>
                    <div className="text-xs font-mono font-bold text-neutral-100 uppercase tracking-wide">
                      {agent.name}
                    </div>
                    {agent.description && (
                      <div className="text-[9px] font-mono text-neutral-400 tracking-wide mt-0.5 leading-tight">
                        {agent.description}
                      </div>
                    )}
                  </div>
                </div>
              </div>

              {/* Status Badge */}
              <div className="flex items-center">
                <span className={`text-[9px] font-mono font-bold px-2 py-0.5 tracking-wider uppercase ${getStatusColor(agent.status)}`}>
                  {agent.status}
                </span>
              </div>

              {/* Log Stream */}
              <div className="text-[10px] font-mono leading-relaxed bg-[#000000] p-2 border border-[#1a1a1a] text-neutral-400 h-14 overflow-y-auto custom-scrollbar">
                <span className="text-[#00ff9d] mr-1">&gt;</span> 
                {agent.message[lang]}
              </div>

              <div className="flex justify-end text-[9px] font-mono text-neutral-600 mt-auto">
                <span className="flex items-center gap-1">
                  <Clock className="w-2.5 h-2.5" />
                  {agent.lastUpdated}
                </span>
              </div>
            </div>
          );
        })}
      </div>

      {/* CEO Directive Terminal Chat */}
      <div className="mt-4 flex flex-col h-64 bg-[#111] border-2 border-[#222]">
        <div className="bg-[#1a1a1a] border-b-2 border-[#222] p-2 flex items-center gap-2">
          <Terminal className="w-3.5 h-3.5 text-neutral-400" />
          <span className="text-[10px] font-mono font-bold text-neutral-400 uppercase tracking-widest">CEO DIRECTIVE LOG</span>
        </div>
        
        <div ref={scrollContainerRef} className="flex-1 min-h-0 overflow-y-auto p-4 space-y-3 font-mono text-[11px] custom-scrollbar">
          {chatHistory.map((msg, i) => (
            <div key={i} className={`flex flex-col ${msg.sender === 'user' ? 'items-end' : 'items-start'}`}>
              <div className={`max-w-[80%] p-2.5 border-2 ${
                msg.sender === 'user'
                  ? 'bg-[#00ff9d]/10 border-[#00ff9d] text-[#00ff9d]'
                  : 'bg-[#1a1a1a] border-[#333] text-neutral-300'
              }`}>
                {msg.text}
              </div>
              <span className="text-[9px] text-neutral-600 mt-1 uppercase font-bold tracking-wider">
                {msg.sender === 'user' ? 'YOU' : 'SYSTEM'} • {msg.timestamp}
              </span>
            </div>
          ))}
          {isSending && (
            <div className="flex items-center gap-2 text-[#00ff9d] text-[10px] uppercase tracking-widest font-bold">
              <span className="animate-pulse">PROCESSING DIRECTIVE...</span>
            </div>
          )}
        </div>

        <form onSubmit={handleSendMessage} className="flex border-t-2 border-[#222] bg-[#0a0a0a]">
          <div className="flex items-center px-3 text-[#00ff9d]">
            <ChevronRight className="w-4 h-4" />
          </div>
          <input
            type="text"
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            disabled={isSending}
            placeholder="ENTER CEO DIRECTIVE FOR DISCORD BOT..."
            className="flex-1 bg-transparent py-3 text-[11px] font-mono font-bold text-white placeholder-neutral-700 focus:outline-none uppercase tracking-wide"
          />
          <button
            type="submit"
            disabled={isSending || !inputValue.trim()}
            className="bg-[#00ff9d] text-black px-6 font-mono font-bold text-[11px] uppercase tracking-widest hover:bg-[#00cc7a] disabled:opacity-50 transition-colors"
          >
            EXECUTE
          </button>
        </form>
      </div>

    </div>
  );
}
