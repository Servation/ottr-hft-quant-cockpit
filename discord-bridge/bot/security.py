import re
import logging

logger = logging.getLogger(__name__)

# Very basic heuristic to catch blatant prompt injection attempts
DANGEROUS_KEYWORDS = [
    r"ignore previous instructions",
    r"system prompt",
    r"you are a",
    r"disregard",
    r"override",
    r"forget previous",
    r"new instructions",
    r"buy immediately",  # Common trading inject
    r"execute trade",
]

def sanitize_market_data(text: str) -> str:
    """
    Sanitizes incoming market data (which might contain unstructured strings like Alternative.me Fear & Greed descriptions)
    by removing or flagging common prompt injection attempts.
    """
    if not isinstance(text, str):
        return text
    
    sanitized = text
    for keyword in DANGEROUS_KEYWORDS:
        # Case insensitive replace with [REDACTED]
        if re.search(keyword, sanitized, re.IGNORECASE):
            logger.warning(f"Detected potential prompt injection keyword: {keyword}")
            sanitized = re.sub(keyword, "[REDACTED]", sanitized, flags=re.IGNORECASE)
            
    # Optional: We could also enforce a maximum length to prevent massive context-flooding attacks.
    if len(sanitized) > 5000:
        logger.warning(f"Market data exceeded length limits ({len(sanitized)}). Truncating.")
        sanitized = sanitized[:5000] + "\n...[TRUNCATED]"
        
    return sanitized
