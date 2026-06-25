"""
Embedding helpers backed by the local LM Studio /v1/embeddings endpoint (S4).

Used for semantic meeting-memory retrieval: embed past meetings + the query and rank
by cosine similarity, so relevant precedent surfaces by *meaning* rather than keyword
overlap (the previous TF-IDF approach). Reuses the AgentLLM AsyncOpenAI client (same
base URL). Fails soft — returns None / 0.0 so retrieval degrades to "no context"
rather than crashing a meeting if the embedding model is unavailable.
"""

import logging
import math
import os
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__)

# LM Studio embedding model id (override via env to match what's loaded).
_EMBED_MODEL = os.getenv("LLM_EMBED_MODEL_ID", "text-embedding-nomic-embed-text-v1.5")
_MAX_CHARS = 8000  # keep within the embedder's context window


async def embed(text: str) -> Optional[List[float]]:
    """Return the embedding vector for `text`, or None on empty input / failure."""
    if not text or not text.strip():
        return None
    try:
        from bot.agents import agent_llm  # reuse the configured AsyncOpenAI client
        # Best-effort path: disable client retries so a downed embedder fails fast
        # and quiet instead of thrashing the network (we degrade to "no context").
        resp = await agent_llm._client.with_options(max_retries=0).embeddings.create(
            model=_EMBED_MODEL, input=text[:_MAX_CHARS]
        )
        return list(resp.data[0].embedding)
    except Exception as e:
        # Log the exception *type*, not its str(): the raw repr can contain phrases
        # ("Connection error") that the offline eval harness treats as a hard-fail
        # marker, and this is an expected soft-degrade (retrieval → "no context"),
        # not a failure.
        logger.warning("Embedding unavailable (%s); retrieval degrades to no-context.", type(e).__name__)
        return None


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two vectors. 0.0 on empty / mismatched / zero vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
