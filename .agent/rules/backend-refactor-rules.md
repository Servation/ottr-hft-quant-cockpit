# Quantitative Backend Engineering Rules

---

## 1. Local LLM Connector
* Use Python's asynchronous `openai.AsyncOpenAI` client.
* Measure local model generation overhead using `time.perf_counter()` to parse execution latency.

---

## 2. Mathematical Models & Strategies

### AdaptiveTrend Momentum Score
$$MOM_t^{(i)} = \frac{P_t^{(i)} - P_{t-L}^{(i)}}{P_{t-L}^{(i)}}$$

### AdaptiveTrend Trailing Stop
$$S_t^{(i)} = \max\left(S_{t-1}^{(i)}, P_t^{(i)} - \alpha \cdot ATR_t^{(i)}\right)$$

### DD90/10 Portfolio Cash Dilution
$$\mathbf{w}_{\text{active}} = (1 - \phi_t) \cdot \mathbf{w}_{\text{target}} + \phi_t \cdot \mathbf{w}_{\text{cash}}$$

---

## 3. High-Performance Java Market Engine
* Reconstruct Level 2 Limit Order Books (LOB) using concurrent, thread-safe structures in Spring Boot 3.3+.
* Emulate Binance-style 1-minute request weight limits capped at 6000 weights, and append the `X-MBX-USED-WEIGHT-1M` header to all responses.