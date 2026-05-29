import time
import logging
import asyncio
from typing import Tuple, List, Dict, Any, Optional
from openai import AsyncOpenAI
from app.config import settings, translate

logger = logging.getLogger(__name__)

# Global lock to serialize local LLM requests and avoid context/VRAM congestion
inference_lock = asyncio.Lock()

class LLMConfigurationError(Exception):
    pass

def clean_base_url(url: str) -> str:
    if not url:
        return url
    url = url.strip()
    
    # Normalize OpenRouter URLs to prevent common path misconfigurations
    if "openrouter.ai" in url:
        url_normalized = url.rstrip("/")
        if not url_normalized.endswith("/api/v1"):
            if url_normalized.endswith("/v1"):
                url_normalized = url_normalized[:-3] + "/api/v1"
            elif url_normalized.endswith("/api"):
                url_normalized = url_normalized + "/v1"
            else:
                url_normalized = url_normalized + "/api/v1"
            return url_normalized

    # Normalize Groq URLs to prevent common path misconfigurations
    if "api.groq.com" in url:
        url_normalized = url.rstrip("/")
        if not url_normalized.endswith("/openai/v1"):
            if url_normalized.endswith("/v1"):
                url_normalized = url_normalized[:-3] + "/openai/v1"
            elif url_normalized.endswith("/openai"):
                url_normalized = url_normalized + "/v1"
            else:
                url_normalized = url_normalized + "/openai/v1"
            return url_normalized

    if not url.rstrip("/").endswith("/v1"):
        return url.rstrip("/") + "/v1"
    return url

async def get_llm_client() -> AsyncOpenAI:
    if not settings.llm_base_url or not settings.llm_api_key or not settings.llm_model_id:
        raise LLMConfigurationError("LLM keys are not configured. Please use POST /api/v1/llm/configure")
    return AsyncOpenAI(
        base_url=clean_base_url(settings.llm_base_url),
        api_key=settings.llm_api_key,
        default_headers={
            "HTTP-Referer": "https://github.com/JeffreySaelee/crypto-trading-bot",
            "X-Title": "Crypto Trading Bot"
        }
    )

def get_llm_fallback_client() -> Optional[AsyncOpenAI]:
    if not settings.llm_fallback_base_url or not settings.llm_fallback_api_key or not settings.llm_fallback_model_id:
        return None
    return AsyncOpenAI(
        base_url=clean_base_url(settings.llm_fallback_base_url),
        api_key=settings.llm_fallback_api_key,
        default_headers={
            "HTTP-Referer": "https://github.com/JeffreySaelee/crypto-trading-bot",
            "X-Title": "Crypto Trading Bot"
        }
    )

async def generate_chat_completion(
    messages: List[Dict[str, str]],
    temperature: float = 0.7,
    max_tokens: int = 500,
    timeout: Optional[float] = None
) -> Tuple[str, float]:
    """
    Generates chat completion using the dynamic LLM client.
    Serializes requests using a global lock to prevent VRAM congestion.
    Measures inference latency.
    """
    client = await get_llm_client()
    fallback_client = get_llm_fallback_client()
    
    # Adapt messages based on locale
    adapted_messages = []
    for msg in messages:
        adapted_messages.append({"role": msg["role"], "content": msg["content"]})
        
    if settings.locale == "ru":
        # Add language enforcement to the system instructions
        ru_instruction = "ВАЖНО: Все аналитические выводы, сигналы и текстовые ответы должны быть на русском языке."
        system_msg_exists = False
        for msg in adapted_messages:
            if msg["role"] == "system":
                msg["content"] = f"{msg['content']}\n\n{ru_instruction}"
                system_msg_exists = True
                break
        if not system_msg_exists:
            adapted_messages.insert(0, {"role": "system", "content": ru_instruction})

    async def _run_inference():
        async with inference_lock:
            start_time = time.perf_counter()
            response = await client.chat.completions.create(
                model=settings.llm_model_id,
                messages=adapted_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout
            )
            latency = time.perf_counter() - start_time
            message_obj = response.choices[0].message
            content = getattr(message_obj, "content", None) or ""
            if not content:
                for attr in ["reasoning", "reasoning_content"]:
                    if hasattr(message_obj, attr) and getattr(message_obj, attr):
                        content = getattr(message_obj, attr)
                        break
                if not content and hasattr(message_obj, "model_extra") and message_obj.model_extra:
                    for key in ["reasoning", "reasoning_content"]:
                        if key in message_obj.model_extra and message_obj.model_extra[key]:
                            content = message_obj.model_extra[key]
                            break
            logger.info(f"LLM Inference succeeded in {latency:.4f}s")
            return content, latency

    async def _run_fallback_inference():
        if not settings.llm_fallback_model_id or not fallback_client:
            raise LLMConfigurationError("Fallback LLM is not configured properly.")
        logger.warning(f"Primary LLM query failed. Routing fallback query to {settings.llm_fallback_model_id}...")
        start_time = time.perf_counter()
        response = await fallback_client.chat.completions.create(
            model=settings.llm_fallback_model_id,
            messages=adapted_messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout
        )
        latency = time.perf_counter() - start_time
        message_obj = response.choices[0].message
        content = getattr(message_obj, "content", None) or ""
        if not content:
            for attr in ["reasoning", "reasoning_content"]:
                if hasattr(message_obj, attr) and getattr(message_obj, attr):
                    content = getattr(message_obj, attr)
                    break
            if not content and hasattr(message_obj, "model_extra") and message_obj.model_extra:
                for key in ["reasoning", "reasoning_content"]:
                    if key in message_obj.model_extra and message_obj.model_extra[key]:
                        content = message_obj.model_extra[key]
                        break
        logger.info(f"Fallback LLM Inference succeeded in {latency:.4f}s")
        return content, latency

    try:
        if timeout is not None:
            try:
                return await asyncio.wait_for(_run_inference(), timeout=timeout)
            except asyncio.TimeoutError:
                raise TimeoutError(f"LLM request timed out after {timeout} seconds")
        else:
            return await _run_inference()
    except Exception as primary_error:
        if fallback_client and settings.llm_fallback_active:
            logger.warning(f"Primary LLM query failed: {primary_error}. Triggering cloud fallback routing...")
            try:
                if timeout is not None:
                    try:
                        return await asyncio.wait_for(_run_fallback_inference(), timeout=timeout)
                    except asyncio.TimeoutError:
                        raise TimeoutError(f"Fallback LLM request timed out after {timeout} seconds")
                else:
                    return await _run_fallback_inference()
            except Exception as fallback_error:
                logger.error(f"Fallback LLM query failed: {fallback_error}")
                raise fallback_error
        else:
            logger.warning("Primary LLM query failed and no fallback is configured.")
            raise primary_error

async def test_llm_connection(
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    model_id: Optional[str] = None
) -> Tuple[bool, str, float]:
    """
    Tests connectivity to the configured LLM API endpoint.
    Returns (success, response_or_error_msg, latency)
    """
    from typing import Optional
    try:
        b_url = base_url if base_url else settings.llm_base_url
        a_key = api_key if api_key else settings.llm_api_key
        m_id = model_id if model_id else settings.llm_model_id

        if not b_url or not a_key or not m_id:
            raise LLMConfigurationError("LLM keys are not configured.")

        client = AsyncOpenAI(
            base_url=clean_base_url(b_url),
            api_key=a_key,
            default_headers={
                "HTTP-Referer": "https://github.com/JeffreySaelee/crypto-trading-bot",
                "X-Title": "Crypto Trading Bot"
            }
        )
        messages = [{"role": "user", "content": "Ping"}]
        
        start_time = time.perf_counter()
        response = await client.chat.completions.create(
            model=m_id,
            messages=messages,
            temperature=0.7,
            max_tokens=10
        )
        latency = time.perf_counter() - start_time
        content = response.choices[0].message.content or ""
        return True, content.strip(), latency
    except Exception as e:
        return False, str(e), 0.0
