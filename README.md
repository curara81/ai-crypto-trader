# AI Crypto Trading Bot

Gemini 2.5 Flash AI가 직접 매수/매도를 판단하는 암호화폐 자동매매 시스템.  
Freqtrade + Upbit (KRW 마켓) 기반, macOS LaunchAgent로 24/7 운영.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Gemini 2.5 Flash AI                  │
│  (Market Data + Order Book + News + Past Decisions)     │
│         → buy/sell/hold + confidence + sizing           │
└──────────────┬──────────────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────────────┐
│              GeminiDecisionStrategy (L4+)                │
│  - 5min candle analysis                                 │
│  - Multi-timeframe (5m/15m/1h/4h)                       │
│  - Order book bid/ask pressure                          │
│  - Real-time ticker data                                │
│  - Tavily news sentiment                                │
│  - Self-learning feedback loop (JSONL)                  │
│  - AI-driven position sizing (0.5x ~ 2.0x)             │
└──────────────┬──────────────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────────────┐
│              Freqtrade Engine                            │
│  - Upbit exchange (KRW market)                          │
│  - 8 coin pairs (BTC, ETH, XRP, SOL, ADA, DOGE, etc.)  │
│  - Dry-run / Live trading                               │
└──────────────┬──────────────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────────────┐
│              Supporting Services                         │
│  - Telegram AI Bot (Korean NLP commands)                │
│  - Korean Trade Notifier                                │
│  - Performance Analyzer (30min cycle, 6h reports)       │
│  - Weekly Auto-Reoptimizer                              │
└─────────────────────────────────────────────────────────┘
```

## Strategy Evolution

| Level | Strategy | Decision Engine |
|-------|----------|-----------------|
| L1 | SimpleStrategy | EMA crossover only |
| L2 | AdvancedStrategy | Multi-indicator rules |
| L3 | LLMSentimentStrategy | Rules + Claude Haiku news sentiment |
| **L4+** | **GeminiDecisionStrategy** | **Gemini AI decides everything** |

## Key Features

### GeminiDecisionStrategy (L4+)
- **AI-First**: Gemini 2.5 Flash directly decides buy/sell/hold with confidence scoring
- **Order Book Analysis**: Real-time bid/ask spread, buy/sell pressure from Upbit API
- **Multi-Timeframe**: 5min, 15min, 1h, 4h trend alignment
- **News Sentiment**: Tavily API for real-time crypto news headlines
- **Self-Learning**: Past decisions + actual trade outcomes fed back to AI
- **Dynamic Position Sizing**: AI suggests 0.5x-2.0x stake based on conviction
- **ATR-based Stoploss**: Confidence-adjusted dynamic stop-loss

### Telegram AI Bot
- Korean natural language commands ("수익률 보여줘", "잔고 얼마야?")
- Slash command support (/profit, /status, /balance, /ai, /orderbook)
- Order book summary across top 5 coins
- Live AI decision viewer

### Performance Analyzer
- Correlates AI decisions with actual trade outcomes
- Win/loss pattern analysis by confidence level, pair, RSI zone
- 6-hour Telegram performance reports
- JSONL decision logging for continuous learning

## Setup

### Prerequisites
- Python 3.12+
- Freqtrade 2024.x+
- macOS (for LaunchAgent daemons)

### API Keys Required
```bash
export GEMINI_API_KEY="your-gemini-api-key"        # Google AI Studio
export TAVILY_API_KEY="your-tavily-api-key"        # News search (optional)
export ANTHROPIC_API_KEY="your-anthropic-api-key"  # Telegram bot NLP
```

### Installation
```bash
# 1. Clone
git clone https://github.com/curara81/ai-crypto-trader.git
cd ai-crypto-trader

# 2. Create venv & install Freqtrade
python3 -m venv ft_env
source ft_env/bin/activate
pip install freqtrade

# 3. Copy config and replace placeholders
cp freqtrade_userdata/config_example.json freqtrade_userdata/config_upbit_dryrun.json

# Generate strong JWT secret and API password
echo "JWT secret: $(openssl rand -hex 32)"
echo "API password: $(openssl rand -base64 24)"
# Then edit config_upbit_dryrun.json — replace all REPLACE_WITH_* fields with real values

# 4. Set working directory (default: ~/trading)
export TRADING_ROOT="$HOME/trading"   # override if installed elsewhere

# 5. Run (dry-run)
freqtrade trade --dry-run \
  --strategy GeminiDecisionStrategy \
  --config freqtrade_userdata/config_upbit_dryrun.json \
  --userdir freqtrade_userdata
```

### Environment Variables
| Var | Purpose | Default |
|-----|---------|---------|
| `TRADING_ROOT` | Project root used for logs, models, configs | `~/trading` |
| `FREQTRADE_CONFIG` | Path to live config (override) | `$TRADING_ROOT/freqtrade_userdata/config_upbit_dryrun.json` |
| `GEMINI_API_KEY` | Google AI Studio API key | required for L4+ |
| `TAVILY_API_KEY` | News search | optional |
| `ANTHROPIC_API_KEY` | Claude fallback + Telegram bot NLP | optional |
| `DAILY_MAX_LOSS_PCT` | Daily cumulative loss limit (guardrail) | `2.0` |
| `MAX_TOTAL_EXPOSURE_KRW` | Sum-of-stakes hard cap (guardrail) | `300000` |
| `MAX_PER_PAIR_KRW` | Per-pair stake cap (guardrail) | `100000` |
| `GEMINI_MOCK_FROM_LOG` | Replay JSONL decisions for backtest | unset |
| `SKIP_DQN` | Skip DQN retraining (faster) | unset |

### Safety Guardrails (v3.1+)
Three safety rails protect against runaway losses when transitioning to live trading:
- **KillSwitch**: `touch ~/trading/KILLSWITCH` instantly blocks all new entries
- **DailyLossGuard**: Tracks daily cumulative P&L in `state/daily_loss.json`, blocks new entries when threshold crossed
- **PositionCap**: Enforces total exposure and per-pair limits via Freqtrade API

Secrets are read from macOS Keychain first (via `scripts/secrets_helper.py`),
then fall back to environment variables.

### macOS LaunchAgent Setup
See `launchd/` directory for example plist files.

## Project Structure
```
trading/
├── freqtrade_userdata/
│   ├── strategies/
│   │   ├── GeminiDecisionStrategy.py   # L4+ AI strategy (main)
│   │   ├── LLMSentimentStrategy.py     # L3 sentiment strategy
│   │   ├── AdvancedStrategyV2.py       # L2 multi-indicator
│   │   └── SimpleStrategy.py          # L1 basic EMA
│   ├── config_example.json            # Config template
│   └── logs/                          # Decision logs (gitignored)
├── scripts/
│   ├── telegram_ai_bot.py             # Korean NLP Telegram bot
│   ├── korean_notifier.py             # Trade notification daemon
│   ├── performance_analyzer.py        # Win/loss pattern analyzer
│   └── auto_reoptimize.sh            # Weekly hyperopt runner
└── launchd/                           # Example LaunchAgent plists
```

## Performance

Early results (paper trading, ~40 trades):
- High confidence (0.7+): **75% win rate, +0.55% avg**
- Medium confidence (0.5-0.7): 33% win rate
- Best pairs: ETH/KRW, XRP/KRW

## Cost

Gemini 2.5 Flash with thinking enabled:
- ~2,300 API calls/day (8 pairs × 5min intervals)
- ~$16-22/day (thinking tokens dominate cost)
- GCP free credits recommended for paper trading phase

## License

MIT
