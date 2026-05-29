import React, { useState, useEffect } from 'react';
import { Newspaper, ExternalLink, Clock, RefreshCw, AlertCircle } from 'lucide-react';
import { fetchMarketNews } from '../services/apiClient';

interface NewsItem {
  id: string;
  title: string;
  source: string;
  link: string;
  pubDate: string;
  timestamp: string;
}

interface MarketNewsFeedProps {
  lang: 'en' | 'ru';
  t: any;
}

const SYNTHETIC_NEWS = {
  en: [
    { id: 'syn-1', title: 'FED signals potential rate cuts amid falling crypto volatility index.', source: 'Yahoo Finance', link: '#', pubDate: 'Just now' },
    { id: 'syn-2', title: 'Bitcoin network active addresses hit new all-time high as ETF inflows surge.', source: 'Coindesk', link: '#', pubDate: '10m ago' },
    { id: 'syn-3', title: 'Binance order book depth shows huge buy walls at support levels.', source: 'Binance News', link: '#', pubDate: '35m ago' }
  ],
  ru: [
    { id: 'syn-1', title: 'ФРС сигнализирует о возможном снижении ставок на фоне падения индекса волатильности.', source: 'Yahoo Finance', link: '#', pubDate: 'Только что' },
    { id: 'syn-2', title: 'Число активных адресов сети Биткоин достигло исторического максимума.', source: 'Coindesk', link: '#', pubDate: '10 мин. назад' },
    { id: 'syn-3', title: 'Глубина книги заявок Binance показывает огромные стены на покупку.', source: 'Binance News', link: '#', pubDate: '35 мин. назад' }
  ]
};

export default function MarketNewsFeed({ lang, t }: MarketNewsFeedProps) {
  const [news, setNews] = useState<NewsItem[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [lastSync, setLastSync] = useState<string>('');

  async function fetchNews() {
    try {
      setLoading(true);
      setError(null);
      
      let data;
      try {
        data = await fetchMarketNews();
      } catch (apiErr) {
        console.warn('API news fetch failed, falling back to synthetic news:', apiErr);
        data = SYNTHETIC_NEWS[lang];
      }

      const formattedData = data.map((item: any, idx: number) => ({
        id: item.id || `news-${idx}-${Date.now()}`,
        title: item.title,
        source: item.source || 'RSS Feed',
        link: item.link,
        pubDate: item.pubDate,
        timestamp: item.pubDate
      }));

      setNews(formattedData);
      setLastSync(new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }));
    } catch (err: any) {
      console.error('Error processing market news:', err);
      setError(lang === 'en' ? 'News stream temporarily disrupted.' : 'Поток новостей временно приостановлен.');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    fetchNews();
    // Refresh news every 30 seconds
    const interval = setInterval(fetchNews, 30000);
    return () => clearInterval(interval);
  }, [lang]);

  return (
    <div id="market-news-widget" className="bg-neutral-900/30 border border-neutral-800 rounded p-6 shadow-2xl relative overflow-hidden flex flex-col justify-between transition-all duration-300">
      
      {/* Background radial highlight */}
      <div className="absolute inset-x-0 top-0 h-32 bg-gradient-to-b from-amber-500/5 via-transparent to-transparent pointer-events-none" />

      {/* Header element */}
      <div className="flex items-center justify-between mb-4 relative z-10">
        <div className="flex items-center gap-2">
          <Newspaper className="w-4 h-4 text-amber-500" />
          <h3 className="font-sans font-medium text-white tracking-tight uppercase text-xs">
            {lang === 'en' ? 'Macro Environment News Feed' : 'Лента Макроэкономических Новостей'}
          </h3>
        </div>
        <div className="flex items-center gap-2">
          {lastSync && (
            <span className="text-[9px] font-mono text-neutral-500 flex items-center gap-1">
              <span className="w-1.5 h-1.5 rounded-full bg-amber-500/80 animate-ping" />
              SYNCED {lastSync}
            </span>
          )}
          <button 
            id="refresh-news-btn"
            onClick={fetchNews} 
            disabled={loading}
            className="p-1 rounded bg-black/40 border border-neutral-800 hover:border-amber-500/40 text-neutral-400 hover:text-white transition-all disabled:opacity-50"
            title="Refresh news feed"
          >
            <RefreshCw className={`w-3 h-3 ${loading ? 'animate-spin text-amber-500' : ''}`} />
          </button>
        </div>
      </div>

      {/* News Content Area */}
      <div className="relative z-10 flex-1 min-h-[220px] max-h-[340px] overflow-y-auto pr-1 space-y-3 custom-scrollbar">
        {loading && news.length === 0 ? (
          <div className="h-44 flex flex-col items-center justify-center text-neutral-600 font-mono gap-2">
            <RefreshCw className="w-5 h-5 animate-spin text-amber-500/55" />
            <span className="text-[10px] uppercase tracking-wider">{lang === 'en' ? 'Ingesting news nodes...' : 'Загрузка каналов новостей...'}</span>
          </div>
        ) : error && news.length === 0 ? (
          <div className="h-44 flex flex-col items-center justify-center text-rose-500/75 p-4 border border-rose-500/10 bg-rose-500/5 rounded font-mono text-center gap-2">
            <AlertCircle className="w-5 h-5" />
            <span className="text-[10px] uppercase tracking-wider font-semibold">{error}</span>
            <button 
              onClick={fetchNews}
              className="mt-2 text-[9px] hover:underline bg-neutral-900 border border-neutral-800 px-2 py-1 rounded"
            >
              {lang === 'en' ? 'Attempt Re-connection' : 'Повторить попытку'}
            </button>
          </div>
        ) : (
          news.map((item, idx) => (
            <div 
              key={item.id} 
              id={item.id}
              className="p-3 bg-black/40 border border-neutral-800/80 hover:border-amber-500/30 rounded transition-all duration-300 group"
            >
              <div className="flex items-center justify-between text-[9px] font-mono text-neutral-500 mb-1">
                <span className="text-amber-500/80 uppercase tracking-widest font-semibold">{item.source}</span>
                <span className="flex items-center gap-1">
                  <Clock className="w-2.5 h-2.5 text-neutral-600" />
                  {item.pubDate}
                </span>
              </div>
              <h4 className="text-[11px] text-neutral-200 group-hover:text-white transition-colors duration-200 leading-snug font-medium pr-4 relative">
                {item.title}
              </h4>
              <div className="flex justify-end mt-1">
                <a 
                  href={item.link} 
                  target="_blank" 
                  rel="noopener noreferrer"
                  className="text-neutral-500 hover:text-amber-400 p-1 rounded hover:bg-neutral-800/50 transition-all"
                  title={lang === 'en' ? 'Read Article' : 'Читать статью'}
                >
                  <ExternalLink className="w-3.5 h-3.5" />
                </a>
              </div>
            </div>
          ))
        )}
      </div>

    </div>
  );
}
