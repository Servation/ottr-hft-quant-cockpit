import os

ROOT_DIR = r"d:\crypto-trading-bot\agent-gateway"

for root, _, files in os.walk(ROOT_DIR):
    for file in files:
        if file.endswith(".py"):
            path = os.path.join(root, file)
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            for idx, line in enumerate(lines):
                if "broadcast" in line:
                    print(f"{file}:{idx+1} -> {line.strip()}")
