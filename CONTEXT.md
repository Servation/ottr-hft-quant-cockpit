# OTTR Quant Dashboard: Domain Context & Terminology

This document defines the core domain terms and concepts of the OTTR algorithmic trading system. These definitions guide the architectural design, agent interactions, and user interface.

## Core Concepts

### Trading Consensus
The process by which multiple specialized AI agents share market evaluations, technical analysis, and sentiment signals to reach a joint trading decision (BUY, SELL, or HOLD) for a target asset.

### Target Allocation Profile
A predefined strategy configuration defining the desired weight distributions of assets in the portfolio (e.g., BTC, ETH, Altcoins). The system dynamically adjusts orders to converge toward these target allocations while respecting safety buffers.

### Execution Path
The routing mechanism selected for filling an order based on order size and confidence. Supported paths include:
- **Direct**: Immediate matching of small/high-confidence orders.
- **TWAP (Time-Weighted Average Price)**: Slicing orders over time to minimize market impact.
- **VWAP (Volume-Weighted Average Price)**: Slicing orders in proportion to historical volume distribution.

### Risk Compliance
The rules and boundaries checked by the system before any trade is executed. These include drawdown limits, portfolio value caps, slippage limits, and asset-specific target buffers.

---

## Agent Roles & Glossary

### Altcoin Screener Agent
A specialized agent that continuously scans the broader market to find high-momentum altcoins and update the active watchlist.

### Technical Analyst Agent
An agent that uses historical market prices and technical indicators (RSI, MACD, EMAs) to forecast short-term price movements and output signals.

### Sentiment Analyst Agent
An agent that gauges market psychology by analyzing on-chain indicators (like SOPR) and real-time news feeds.

### Trader Agent
The action-taking agent that translates consensus decisions into concrete orders, determines the order size, and selects the optimal Execution Path.

### Risk Auditor Agent
An agent that evaluates proposed trades against the Risk Compliance parameters to approve or veto the order.

### Performance Optimizer Agent (New)
A meta-learning agent that regularly reviews trade execution history and journal feedback to tune parameters (like stop-loss limits, portfolio caps, or execution path thresholds) to optimize overall system profitability.

---

## Agent Tools

Exposed actions that agents can run to obtain data or modify parameters:
- **Multi-Timeframe Candle Tool (`fetch_candles`)**: Allows agents to retrieve historical OHLCV data from Binance/Yahoo Finance across different intervals (e.g., 1m, 1h, 1d) for multi-scale technical indicators.
- **Order Book Imbalance Tool (`fetch_order_book_imbalance`)**: Queries the Discord Bridge's internal limit order book to retrieve bid-ask volume imbalance ratios near the mid price.
- **On-chain Indicators**: Querying network transaction profitability (e.g., SOPR).
- **Meme & Narrative Harvester Tool (`harvest_market_narratives`)**: A tool that fetches top aggregated market headlines, allowing the agent to dynamically extract current market narratives, catalysts, and FUD memes using semantic classification.
- **Fear & Greed Index Tool (`fetch_fear_and_greed_index`)**: Queries public sentiment APIs to retrieve the overall daily market sentiment index.
- **Historical Journal Reader**: Querying past trade results, slippage metrics, and performance analytics.
- **Parameter Control Tool**: Modifying active portfolio parameters (stop-losses, buffers) based on performance reviews.

---

## Self-Tuning & Attribution

### Optimizer Audit & Attribution Loop
The continuous process by which the Performance Optimizer Agent reviews the consequences of its parameter changes. It evaluates whether updates to risk metrics or execution paths resulted in improved portfolio win rates or reduced drawdown, reverting adjustments if performance deteriorates.

### Parameter Attribution Tracking
The ledger of all configuration changes made by the system, recording the previous value, the new value, the LLM reasoning/hypothesis for the change, a snapshot of portfolio health metrics at that time, and the subsequent attribution score.

