import json
import os
import sys
import shutil

# Paths
MEETING_LOG = r"d:\crypto-trading-bot\discord-bridge\data\meeting_log.json"
TEST_VAULT = r"d:\crypto-trading-bot\discord-bridge\data\test_vesper_vault"
VESPER_PATH = r"d:\vesper-text"

def run_test():
    # 1. Clean/Create vault dir
    if os.path.exists(TEST_VAULT):
        shutil.rmtree(TEST_VAULT)
    os.makedirs(TEST_VAULT)

    # 2. Read meeting log
    if not os.path.exists(MEETING_LOG):
        print("No meeting_log.json found!")
        return
        
    with open(MEETING_LOG, "r") as f:
        data = json.load(f)
        
    meetings = data.get("meetings", [])
    print(f"Loaded {len(meetings)} meetings from JSON.")
    
    # 3. Create Markdown files for Vesper
    for m in meetings:
        meeting_id = m.get("id", "unknown")
        summary = m.get("summary", "")
        contribs = m.get("agent_contributions", {})
        
        content = f"# Meeting {meeting_id}\n\n## Summary\n{summary}\n\n"
        for agent, text in contribs.items():
            content += f"## {agent.capitalize()}\n{text}\n\n"
            
        file_path = os.path.join(TEST_VAULT, f"{meeting_id}.md")
        with open(file_path, "w", encoding="utf-8") as out:
            out.write(content)
            
    print(f"Generated {len(meetings)} markdown files in {TEST_VAULT}.")

    # 4. Import and run Vesper
    sys.path.append(VESPER_PATH)
    import vesper_engine
    
    query = "What did we say about SOL allocation and volatility?"
    print(f"\n--- Running Vesper TF-IDF Engine ---")
    print(f"Query: '{query}'\n")
    
    packet, metadata, err = vesper_engine.generate_context_packet(TEST_VAULT, query, top_k=2)
    
    if err:
        print(f"Vesper Error: {err}")
    else:
        print("Vesper Packet Output:")
        print(packet)
        print("\nMetadata:")
        for m in metadata:
            print(f" - {m['file']} (Score: {m['score']:.4f})")

if __name__ == "__main__":
    run_test()
