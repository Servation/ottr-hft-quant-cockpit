import asyncio
import sys
import os
from openai import AsyncOpenAI

async def test_embeddings():
    # Retrieve configuration similar to bot/agents.py
    # Load dotenv from repo root if present
    try:
        from dotenv import load_dotenv
        from pathlib import Path
        PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
        load_dotenv(PROJECT_ROOT / ".env")
        print("Loaded environment from:", PROJECT_ROOT / ".env")
    except ImportError:
        print("dotenv not installed, using OS env directly")

    base_url = os.getenv("LLM_BASE_URL", "http://localhost:1234/v1")
    model_id = os.getenv("LLM_MODEL_ID", "gemma-4-12b-it")

    print(f"Configured Base URL: {base_url}")
    print(f"Configured Model ID: {model_id}")

    client = AsyncOpenAI(
        base_url=base_url,
        api_key="lm-studio"
    )

    # 1. Query loaded models
    print("\n--- Querying Loaded Models in LM-Studio ---")
    try:
        models_response = await client.models.list()
        loaded_models = [m.id for m in models_response.data]
        print(f"Loaded models in LM-Studio: {loaded_models}")
    except Exception as e:
        print(f"Error querying loaded models: {e}")
        loaded_models = []

    # 2. Test embedding generation
    print("\n--- Testing Embedding Generation ---")
    test_models = []
    if loaded_models:
        test_models.extend(loaded_models)
    
    # Also test model_id and default/ada models
    for m in [model_id, "text-embedding-ada-002", "nomic-ai/nomic-embed-text-v1.5-GGUF"]:
        if m not in test_models:
            test_models.append(m)

    for model in test_models:
        print(f"\nAttempting embedding generation with model: '{model}'")
        try:
            response = await client.embeddings.create(
                input="Test sentence for generating vector embeddings.",
                model=model
            )
            embedding = response.data[0].embedding
            print(f"SUCCESS: Generated embedding using model '{model}'!")
            print(f"Embedding length: {len(embedding)}")
            print(f"First 5 dimensions: {embedding[:5]}")
        except Exception as e:
            print(f"FAILED for model '{model}': {type(e).__name__} - {e}")

if __name__ == "__main__":
    asyncio.run(test_embeddings())
