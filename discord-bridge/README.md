# 🦦 OTTR — Discord Trading Floor

> **O**perations & **T**rading **T**eam **R**unner — An autonomous AI trading team that debates, decides, and executes crypto strategies live on Discord.

OTTR runs a cast of AI agent personas (powered by a local LLM) on a Discord server. The agents hold scheduled meetings, respond to CEO (human) directives, and trigger emergency sessions when markets move violently.

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| **Python** | 3.10+ | 3.11 or 3.12 recommended |
| **LM Studio** | Latest | Running with **Gemma 4 12B** (or any OpenAI-compatible model) |
| **Discord Bot** | — | A bot application with the necessary intents enabled |
| **pip** | Latest | Or use `uv` for faster installs |

---

## Setup

### 1. Create a Discord Bot Application

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications).
2. Click **New Application** → give it a name (e.g., "OTTR Trading Floor").
3. Navigate to **Bot** → click **Reset Token** → copy the token.
4. Under **Privileged Gateway Intents**, enable:
   - ✅ **Message Content Intent**
   - ✅ **Server Members Intent** *(optional but recommended)*
5. Navigate to **OAuth2** → **URL Generator**:
   - Scopes: `bot`
   - Bot Permissions: `Send Messages`, `Manage Webhooks`, `Read Message History`, `Embed Links`
6. Copy the generated URL and invite the bot to your server.

### 2. Set Up Discord Channels

Create two text channels on your server:

| Channel | Purpose |
|---|---|
| `#trading-floor` | Main arena where agents post and humans issue directives |
| `#system-status` | Operational logs, errors, meeting start/end notices |

Copy each channel's ID (right-click → *Copy Channel ID*; enable Developer Mode in Discord settings first).

### 3. Configure Environment

```bash
cd discord-bridge
cp .env.example .env
```

Edit `.env`:

```env
DISCORD_BOT_TOKEN=your-bot-token-here
DISCORD_TRADING_FLOOR_CHANNEL_ID=123456789012345678
DISCORD_SYSTEM_STATUS_CHANNEL_ID=123456789012345679
LLM_BASE_URL=http://localhost:1234/v1
LLM_MODEL_ID=gemma-4-12b
```

### 4. Install Dependencies

```bash
pip install -r requirements.txt
```

---

## Running

```bash
cd discord-bridge
python -m bot.main
```

On successful startup you'll see:

```
OTTR Trading Floor — ONLINE
Agents syncing… meetings scheduled.
```

---

## Configuration

All runtime configuration lives in `config/settings.yaml`. Key sections:

| Key | Default | Description |
|---|---|---|
| `meeting_hours` | `[0,4,8,12,16,20]` | Cron hours (US/Pacific) for scheduled meetings |
| `emergency_price_drop_pct` | `5.0` | % drop in 60 min to trigger emergency |
| `emergency_price_spike_pct` | `8.0` | % spike in 60 min to trigger emergency |
| `check_interval_seconds` | `60` | Price-check polling interval |
| `alert_cooldown_seconds` | `1800` | Minimum gap between emergency alerts |
| `price_history_window_minutes` | `60` | Look-back window for threshold checks |

---

## Agent Personas

| Agent ID | Name | Role | Specialty |
|---|---|---|---|
| `technical_analyst` | Alex Chen | Technical Analyst | Chart patterns, RSI, MACD, Bollinger bands |
| `fundamentals` | Sarah Mitchell | Fundamentals Analyst | On-chain metrics, tokenomics, macro trends |
| `risk_manager` | David Park | Risk Manager | Position sizing, drawdown limits, VaR |
| `quant` | Dr. Maya Rodriguez | Quant Strategist | Statistical arb, mean reversion, correlation |
| `sentiment` | Jordan Lee | Sentiment Analyst | Social signals, news flow, fear & greed |
| `portfolio_mgr` | Rachel Kim | Portfolio Manager | Allocation, rebalancing, performance tracking |
| `devil_advocate` | Marcus Thompson | Devil's Advocate | Contrarian challenges, stress-testing theses |

Each agent has a full system prompt in `config/personas/<agent_id>.txt`.

---

## Architecture

```
discord-bridge/
├── bot/
│   ├── __init__.py          # Settings loader
│   ├── main.py              # Bot lifecycle & webhook management
│   ├── agents.py            # Agent definitions & LLM caller
│   ├── alerts.py            # Emergency price-threshold monitor
│   ├── ceo_handler.py       # CEO directive queue & live LLM message router
│   ├── meetings.py          # Meeting types & turn sequencing
│   ├── memory.py            # Vesper TF-IDF Semantic Memory & recent context
│   ├── portfolio.py         # Paper portfolio with JSON persistence
│   ├── price_feed.py        # CoinGecko + CoinCap price fetcher (BTC, ETH, SOL, BNB, XRP, ADA, DOGE, LINK, AVAX)
│   └── scheduler.py         # APScheduler cron integration
├── config/
│   ├── settings.yaml        # Runtime configuration
│   └── personas/            # Agent system prompts
├── data/                    # Runtime state (portfolio)
│   └── vesper_vault/        # Markdown-based semantic memory vault
├── .env                     # Secrets (not committed)
├── .env.example             # Template
├── requirements.txt         # Python dependencies
└── README.md                # This file
```

### Data Flow

```
          Human CEO
              │
              ▼
     ┌────────────────┐
     │  ceo_handler   │──── queues directives
     └────────────────┘
              │
              ▼
     ┌────────────────┐     ┌──────────────┐
     │   scheduler    │◄────│  alert_monitor│ (emergency trigger)
     └────────────────┘     └──────────────┘
              │                     ▲
              ▼                     │
     ┌────────────────┐     ┌──────────────┐
     │ meeting_engine │     │  price_feed   │
     └────────────────┘     └──────────────┘
              │
      ┌───────┼───────┐
      ▼       ▼       ▼
   Agent₁  Agent₂  Agent₃ ...
      │       │       │
      ▼       ▼       ▼
   ┌────────────────────┐
   │   LLM (LM Studio)  │
   └────────────────────┘
              │
              ▼
     ┌────────────────┐
     │  Discord        │ ← webhooks post as each agent persona
     │  #trading-floor │
     └────────────────┘
```

---

## Core Features & Consensus Mechanics

OTTR operates as an autonomous, self-executing trading simulation floor with the following key mechanics:

### 1. Automated Trade & Parameter Execution
At the end of every meeting, the bot parses the facilitator's closing summary for structured tags:
- `[TRADE: BUY <ASSET> <USD_AMOUNT>]` (e.g. `[TRADE: BUY BTC 500]`) — Executes a simulated buy order on the paper portfolio.
- `[TRADE: SELL <ASSET> <QUANTITY>]` (e.g. `[TRADE: SELL BTC 0.15]`) — Executes a simulated sell order.
- `[PARAM: min_trade_usd=<VALUE>]` (e.g. `[PARAM: min_trade_usd=150]`) — Dynamically updates the minimum trade limit parameter.

All successfully executed trades and parameter updates are saved dynamically to `data/portfolio_state.json`.

### 2. End-of-Meeting Running Totals
Immediately after trade/parameter directives are processed, the Portfolio Manager posts a real-time portfolio status update to the trading floor showing:
- Cash balance
- Individual asset quantities and current USD market values
- Total portfolio net asset value (including unrealized P&L)
- Current minimum trade limits

### 3. Expanded Debate Round
The debate round dynamically selects **all** non-facilitator participants to speak (instead of capping at 3), giving the entire team a chance to voice disagreements and challenge colleagues.

### 4. Semantic Memory & Retrieval (Vesper)
The bot logs all past meetings, live discussions, and direct messages into a Markdown-based semantic vault (`data/vesper_vault`). When generating the context for an upcoming meeting, it performs a local TF-IDF semantic search to pull in the most historically relevant context, along with the most recent short-term memory.

### 5. Live CEO Message Routing
Human interactions in the `#trading-floor` channel are processed live by an LLM router, which categorizes intent into one of five actions:
- `[IGNORE]`: Drops casual chatter or rhetorical statements.
- `[QUEUE]`: Saves a directive to the agenda for the next scheduled meeting (capped at 3 items).
- `[EMERGENCY]`: Immediately triggers a full team meeting.
- `[DIRECT]`: Pings the single most relevant agent to respond instantly.
- `[DISCUSSION]`: Starts an ad-hoc live debate in the channel between two relevant agents.

### 6. Majority Rules & 100% Sizing
- **Majority Rules:** Decisions are governed by majority consensus. Unilateral agent vetoes (like from the Risk Auditor) are treated as dissenting votes and can be overridden by a majority vote.
- **Dynamic Allocation:** The Trader can propose, and the Portfolio Manager can execute, capital allocation sizing up to **100%** of available cash.
- **Minimum Trade & Dust Bypass:** A minimum trade limit (defaults to **$100**) is strictly enforced for buys and partial sells. Full asset liquidations (sells of the total quantity held) automatically bypass the minimum check to prevent leaving behind fractional "dust" holdings.

---

## Troubleshooting

### LM Studio Not Running / Connection Refused

```
Error: Connection refused at http://localhost:1234/v1
```

**Fix:** Start LM Studio, load the model, and ensure the local server is running on port 1234.

### Webhook Limit Reached

Discord allows a maximum of **15 webhooks per channel**. OTTR uses 7 (one per agent).

**Fix:** Delete unused webhooks via *Channel Settings → Integrations → Webhooks*.

### Rate Limits (429 Too Many Requests)

OTTR enforces a 2-second delay between webhook posts. If you still hit limits:

- Increase `WEBHOOK_POST_DELAY` in `bot/main.py`
- Reduce the number of agents speaking per meeting round

### Bot Doesn't Respond to Messages

1. Confirm **Message Content Intent** is enabled in the Developer Portal.
2. Confirm the bot has **Read Messages** and **Send Messages** permissions in the channel.
3. Confirm `DISCORD_TRADING_FLOOR_CHANNEL_ID` matches the correct channel.

### Meetings Not Firing

- Check that the scheduler started: look for `Scheduler started` in logs.
- Verify your system clock / timezone. Meetings fire on US/Pacific time.
- APScheduler requires `pytz` or `tzdata` — ensure one is installed.

---

## License

Internal use only — part of the OTTR HFT Quant Cockpit project.
