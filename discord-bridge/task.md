# Discord Bridge — Implementation Task Tracker

## Status: 🚧 In Progress

### Phase 1: Foundation
- [ ] `.env.example`
- [ ] `requirements.txt`
- [ ] `config/settings.yaml`
- [ ] `bot/__init__.py`

### Phase 2: Core Infrastructure
- [ ] `bot/price_feed.py` — CoinGecko + CoinCap with 60s caching
- [ ] `bot/portfolio.py` — Paper portfolio with atomic JSON persistence

### Phase 3: Agent System
- [ ] `config/personas/` — 7 agent system prompt files
- [ ] `bot/agents.py` — Agent definitions + LLM calling via OpenAI SDK

### Phase 4: Memory & Meetings
- [ ] `bot/memory.py` — Meeting log, rolling summaries, decision log
- [ ] `bot/meetings.py` — Meeting types, turn sequencing, facilitator logic

### Phase 5: Discord Integration
- [ ] `bot/main.py` — Bot lifecycle, webhook management, message posting
- [ ] `bot/ceo_handler.py` — Directive detection and queuing
- [ ] `bot/alerts.py` — Price threshold monitoring, emergency triggers
- [ ] `bot/scheduler.py` — APScheduler integration, meeting rotation

### Phase 6: Documentation
- [ ] `README.md` — Setup and usage instructions
- [ ] `data/.gitkeep` — Runtime state directory
