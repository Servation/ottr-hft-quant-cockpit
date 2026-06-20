import logging
from typing import Dict, Any, Optional
from pydantic_settings import BaseSettings
from pydantic import BaseModel

# Load .env so local (non-Docker) runs pick up OTTR_API_KEY (and friends) into
# os.environ. In Docker, compose injects these directly. Guarded so a missing
# python-dotenv degrades gracefully to "env must be set externally".
try:
    from pathlib import Path as _Path
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_Path(__file__).resolve().parents[1] / ".env")   # agent-gateway/.env
    _load_dotenv(_Path(__file__).resolve().parents[2] / ".env")   # repo-root .env
except Exception:
    pass

class Settings(BaseSettings):
    # LLM configurations (must be empty by default)
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model_id: str = ""

    # Fallback LLM configurations
    llm_fallback_base_url: Optional[str] = None
    llm_fallback_api_key: Optional[str] = None
    llm_fallback_model_id: Optional[str] = None
    llm_fallback_active: bool = True

    # Service configurations
    java_engine_url: str = "http://localhost:8080"
    locale: str = "en"  # "en" or "ru"
    # Comma-separated allowed CORS origins (GATEWAY_ALLOWED_ORIGINS).
    allowed_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    
    # Mempool & Blockchain URLs
    mempool_api_url: str = "https://mempool.space/api"
    blockchain_api_url: str = "https://blockchain.info"

    # Default caching times in seconds
    ticker_cache_ttl: int = 8
    news_cache_ttl: int = 30

    class Config:
        env_prefix = "GATEWAY_"
        case_sensitive = False

# Global mutable settings instance
settings = Settings()

# Mutable trading configuration
class TradingConfig(BaseModel):
    portfolio_cap: float = 0.05       # 5% portfolio cap
    drawdown_limit: float = 0.045     # 4.5% drawdown limit
    slippage_limit: float = 0.0008    # 0.08% slippage limit
    symbols: list[str] = ["BTCUSDT", "ETHUSDT"]
    interval_seconds: int = 10
    stop_loss_limit: float = 0.07     # 7% stop loss limit
    strategy: str = "DD90/10"         # Active target allocation profile strategy

# Global active trading config and state
trading_config = TradingConfig()
trading_active = False

# Localization dictionary
TRANSLATIONS: Dict[str, Dict[str, str]] = {
    "en": {
        "health_ok": "Gateway is healthy",
        "llm_configured": "LLM configured successfully",
        "llm_test_success": "LLM connection test succeeded in {latency:.2f}s: {response}",
        "llm_test_failed": "LLM connection test failed: {error}",
        "trading_started": "Trading loop started",
        "trading_stopped": "Trading loop stopped",
        "trading_already_running": "Trading loop is already running",
        "trading_not_running": "Trading loop is not running",
        "config_updated": "Trading configuration updated",
        "ticker_fetch_success": "Successfully fetched tickers",
        "ticker_fetch_failed": "Failed to fetch tickers: {error}",
        "news_fetch_success": "Successfully fetched news",
        "news_fetch_failed": "Failed to fetch news: {error}",
        "sopr_fetch_success": "Successfully computed SOPR: {sopr:.4f}",
        "sopr_fetch_failed": "Failed to compute SOPR: {error}",
        "signal_generated": "Agent {agent} generated signal: {signal}",
        "trade_approved": "Trade APPROVED by Risk Auditor: {details}",
        "trade_vetoed": "Trade VETOED by Risk Auditor: {reason}",
        "trade_executed": "Trade executed via path {path} in {latency:.2f}ms. Order size: {size}",
        "trade_execution_failed": "Trade execution failed: {error}",
        "consensus_no_action": "Consensus reached: NO ACTION",
        "consensus_action": "Consensus reached: {action} on {symbol}",
        "risk_limit_exceeded": "Risk limit exceeded: {limit}",
        "sse_client_connected": "SSE client connected: {client_id}",
        "sse_client_disconnected": "SSE client disconnected: {client_id}",
        "invalid_locale": "Invalid locale requested, defaulting to English",
        "error_parsing_tickers": "Error parsing tickers: {error}",
        "error_parsing_news": "Error parsing news: {error}",
        "stop_loss_triggered": "Stop Loss triggered for {symbol}: current price {price:.2f} drops below average cost {cost:.2f} (Limit: {limit_pct:.1f}%)",
        "stop_loss_liquidation": "Stop Loss liquidation approved: {details}"
    },
    "ru": {
        "health_ok": "Шлюз работает нормально",
        "llm_configured": "LLM успешно настроен",
        "llm_test_success": "Тест подключения LLM прошел успешно за {latency:.2f} сек: {response}",
        "llm_test_failed": "Ошибка теста подключения LLM: {error}",
        "trading_started": "Торговый цикл запущен",
        "trading_stopped": "Торговый цикл остановлен",
        "trading_already_running": "Торговый цикл уже запущен",
        "trading_not_running": "Торговый цикл не запущен",
        "config_updated": "Конфигурация торговли обновлена",
        "ticker_fetch_success": "Данные тикеров успешно получены",
        "ticker_fetch_failed": "Ошибка получения тикеров: {error}",
        "news_fetch_success": "Новости успешно получены",
        "news_fetch_failed": "Ошибка получения новостей: {error}",
        "sopr_fetch_success": "SOPR успешно рассчитан: {sopr:.4f}",
        "sopr_fetch_failed": "Ошибка расчета SOPR: {error}",
        "signal_generated": "Агент {agent} сформировал сигнал: {signal}",
        "trade_approved": "Сделка ОДОБРЕНА Риск-аудитором: {details}",
        "trade_vetoed": "Сделка ОТКЛОНЕНА Риск-аудитором: {reason}",
        "trade_executed": "Сделка выполнена по пути {path} за {latency:.2f} мс. Размер ордера: {size}",
        "trade_execution_failed": "Ошибка выполнения сделки: {error}",
        "consensus_no_action": "Консенсус достигнут: ДЕЙСТВИЙ НЕТ",
        "consensus_action": "Консенсус достигнут: {action} для {symbol}",
        "risk_limit_exceeded": "Превышен лимит риска: {limit}",
        "sse_client_connected": "Клиент SSE подключен: {client_id}",
        "sse_client_disconnected": "Клиент SSE отключен: {client_id}",
        "invalid_locale": "Запрошена неверная локаль, по умолчанию используется английский",
        "error_parsing_tickers": "Ошибка парсинга тикеров: {error}",
        "error_parsing_news": "Ошибка парсинга новостей: {error}",
        "stop_loss_triggered": "Стоп-лосс сработал для {symbol}: текущая цена {price:.2f} упала ниже средней цены покупки {cost:.2f} (Лимит: {limit_pct:.1f}%)",
        "stop_loss_liquidation": "Ликвидация по стоп-лоссу одобрена: {details}"
    }
}

def translate(key: str, **kwargs: Any) -> str:
    lang = settings.locale if settings.locale in TRANSLATIONS else "en"
    fmt = TRANSLATIONS[lang].get(key, TRANSLATIONS["en"].get(key, key))
    try:
        return fmt.format(**kwargs)
    except Exception:
        return fmt

def clamp_config_parameters():
    """
    Enforces hard safety boundaries on dynamic configuration settings
    to prevent AI agents from applying unsafe trading limits.
    """
    trading_config.portfolio_cap = max(0.01, min(0.15, trading_config.portfolio_cap))
    trading_config.stop_loss_limit = max(0.02, min(0.20, trading_config.stop_loss_limit))
    trading_config.slippage_limit = max(0.0002, min(0.005, trading_config.slippage_limit))
    trading_config.drawdown_limit = max(0.01, min(0.10, trading_config.drawdown_limit))

