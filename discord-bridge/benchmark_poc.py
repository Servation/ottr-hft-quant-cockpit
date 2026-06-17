import json
import os
import sys
import time
import numpy as np
from openai import OpenAI

# Paths
MEETING_LOG = r"d:\crypto-trading-bot\discord-bridge\data\meeting_log.json"
TEST_VAULT = r"d:\crypto-trading-bot\discord-bridge\data\test_vesper_vault"
VESPER_PATH = r"d:\vesper-text"

def cosine_similarity(v1, v2):
    dot = np.dot(v1, v2)
    norm1 = np.linalg.norm(v1)
    norm2 = np.linalg.norm(v2)
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot / (norm1 * norm2)

def run_benchmark():
    query = "What did we say about SOL allocation and volatility?"
    print("="*60)
    print(f"BENCHMARK COMPARISON: Vesper (TF-IDF) vs LM Studio (Embeddings)")
    print(f"Query: '{query}'")
    print("="*60)
    
    # 1. Load data
    with open(MEETING_LOG, "r") as f:
        data = json.load(f)
    meetings = data.get("meetings", [])
    
    # Extract text blocks
    meeting_texts = []
    for m in meetings:
        meeting_id = m.get("id", "unknown")
        summary = m.get("summary", "")
        contribs = m.get("agent_contributions", {})
        
        content = f"# Meeting {meeting_id}\n\n## Summary\n{summary}\n\n"
        for agent, text in contribs.items():
            content += f"## {agent.capitalize()}\n{text}\n\n"
        meeting_texts.append({"id": meeting_id, "text": content})
        
    print(f"\n--- 1. LM STUDIO (nomic-embed-text) ---")
    start_time_lm = time.time()
    try:
        client = OpenAI(base_url="http://localhost:1234/v1", api_key="lm-studio")
        
        # Embed all meetings
        meeting_embeddings = []
        for mt in meeting_texts:
            resp = client.embeddings.create(input=mt["text"], model="nomic-embed-text")
            meeting_embeddings.append({
                "id": mt["id"],
                "text": mt["text"],
                "vector": resp.data[0].embedding
            })
            
        # Embed query
        q_resp = client.embeddings.create(input=query, model="nomic-embed-text")
        q_vector = q_resp.data[0].embedding
        
        # Compute similarities
        for me in meeting_embeddings:
            me["score"] = cosine_similarity(q_vector, me["vector"])
            
        # Sort by score
        meeting_embeddings.sort(key=lambda x: x["score"], reverse=True)
        top_lm = meeting_embeddings[:2]
        lm_time = time.time() - start_time_lm
        
        print(f"Time Taken: {lm_time:.4f} seconds")
        print("\nTop 2 LM Studio Results (Full meeting documents):")
        for res in top_lm:
            # truncate output for benchmark readability
            snippet = res["text"][:300].replace('\n', ' ') + "..."
            print(f"- [Score: {res['score']:.4f}] {res['id']}.md\n  Snippet: {snippet}")
            
    except Exception as e:
        print(f"LM Studio Failed: {e}")
        lm_time = time.time() - start_time_lm


    print(f"\n\n--- 2. VESPER TEXT (TF-IDF Intent Isolator) ---")
    start_time_vesper = time.time()
    
    sys.path.append(VESPER_PATH)
    import vesper_engine
    
    packet, metadata, err = vesper_engine.generate_context_packet(TEST_VAULT, query, top_k=2)
    vesper_time = time.time() - start_time_vesper
    
    if err:
        print(f"Vesper Error: {err}")
    else:
        print(f"Time Taken: {vesper_time:.4f} seconds")
        print("\nTop 2 Vesper Results (Isolated Paragraphs):")
        print(packet)
        print("Metadata Scores:")
        for m in metadata:
            print(f"- [Score: {m['score']:.4f}] {m['file']}")
            
    print("\n" + "="*60)
    print(f"SPEED COMPARISON:")
    print(f"LM Studio: {lm_time:.4f}s")
    print(f"Vesper:    {vesper_time:.4f}s")
    if vesper_time > 0 and lm_time > 0:
        print(f"Vesper is {lm_time/vesper_time:.1f}x faster.")
    print("="*60)

if __name__ == "__main__":
    run_benchmark()
