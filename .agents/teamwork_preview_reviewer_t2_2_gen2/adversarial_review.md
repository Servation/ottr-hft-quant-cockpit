# Adversarial Review — 2026-06-15T16:21:00-07:00

## Challenge Summary

**Overall risk assessment**: LOW

The SQLite-backed vector storage and semantic querying implementations are robust. Basic edge cases (division by zero, dimension mismatch, concurrent writes, lock handling) are explicitly addressed. The only residual risks relate to asynchronous event loop blocking from synchronous SQLite calls, which are typical for simple embedded database systems and acceptable for the current architecture.

## Challenges

### [Medium] Challenge 1: Async Event Loop Blocking
- **Assumption challenged**: Executing SQLite queries synchronously inside an async framework (`bot/memory.py`) is safe.
- **Attack scenario**: High write/read loads under concurrent client connections block the asyncio event loop, causing latency spikes in unrelated tasks (e.g. price feeds, scheduling).
- **Blast radius**: Increased latency across all async operations in the discord bridge.
- **Mitigation**: Offload synchronous SQLite operations (`conn.execute`, `conn.commit`, `conn.close`) to a thread pool executor using `asyncio.to_thread` or `loop.run_in_executor`.

### [Low] Challenge 2: Approximate Token Budget Estimation
- **Assumption challenged**: Approximate token counts via word count (`words = formatted.split()`) is accurate enough to stay within LLM token budgets.
- **Attack scenario**: If summaries contain many complex/multibyte unicode characters or technical terms that result in multiple BPE tokens per word, the actual token usage may significantly exceed the word count.
- **Blast radius**: LLM API returns context-window limit errors or truncates context unexpectedly.
- **Mitigation**: Integrate a proper tiktoken/LLM tokenizer client if precise budgeting is needed, or reduce the budget threshold slightly to leave a safety margin.

### [Low] Challenge 3: Division-by-Zero and All-Zero Vector Behavior
- **Assumption challenged**: Cosine similarity handles all-zero vectors properly.
- **Attack scenario**: An empty or corrupted embedding vector (all zeros) stored in the database might trigger division-by-zero or return NaN.
- **Blast radius**: Corrupt similarity scores or runtime exceptions.
- **Mitigation**: Code checks `v_norm == 0` and sets `similarity = 0.0`, and checks `q_norm == 0` and returns `[]`. This is already successfully mitigated in the code.

## Stress Test Results

- **Dimension mismatch**: A query vector of different dimension is sent to the search method -> Expected behavior: raise `ValueError` -> Actual behavior: raised `ValueError: Dimension mismatch...` -> PASS
- **Concurrent saves**: 10 concurrent writes executed simultaneously -> Expected behavior: SQLite database handles transactions and lock guarantees -> Actual behavior: 10 records written cleanly and database is intact -> PASS
- **Empty / Null values**: Save record with empty string fields -> Expected behavior: DB successfully inserts and retrieves NULL/empty metadata -> Actual behavior: successfully saved and retrieved -> PASS

## Unchallenged Areas

- **File System Permissions**: We didn't perform physical host-level read-only testing, but we did simulate permission errors via patching `os.replace` which correctly failed, matching expectations.
