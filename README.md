# APEX — Hyperliquid Perp Scalp Alert Bot

APEX is a Python alert bot that scans Hyperliquid perpetual futures markets for
trend-pullback setups and sends actionable trade plans to your phone via Pushover.

**This is an alert-only MVP. It does not place orders. All execution is manual.**

---

## What APEX Does

- Connects to Hyperliquid public market data (HTTP + WebSocket)
- Dynamically discovers all Hyperliquid perp markets, filters by volume/priority
- Scans for TREND_PULLBACK setups using EMA, VWAP, RSI, and ATR on 15m + 5m candles
- Sends two levels of alerts via Pushover:
  - **SETUP_FORMING** — early warning to get ready
  - **CONFIRMED_SETUP** — entry zone, stop, 1R/2R targets, position size
- Action links in alerts: Enter, Skip, Snooze 15m
- Tracks paper trades; sends follow-up outcome links: Win, Loss, Breakeven
- Stores everything in SQLite
- Enforces per-symbol alert cooldowns and daily planned-loss lockout
- Runs as a systemd service on a Hetzner VPS

---

## What MVP Does NOT Do

- No live order placement
- No wallet or private key handling
- No Solana/Birdeye scanning
- No dashboard or web UI
- No backtesting engine
- No multi-user support
- No reversal/counter-trend strategies

---

## Setup

### Requirements

- Python 3.11+
- A [Pushover](https://pushover.net) account (app token + user key)
- A Hetzner (or any Linux) VPS for production

### Local Development

```bash
# Clone
git clone https://github.com/AraldoYerolis/APEX.git
cd APEX

# Create virtualenv
python3.11 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
pip install -e .

# Copy and configure environment
cp .env.example .env
# Edit .env — fill in at minimum PUSHOVER_APP_TOKEN, PUSHOVER_USER_KEY, ACTION_TOKEN_SECRET
```

---

## .env Configuration

Key variables:

| Variable | Description |
|---|---|
| `PUSHOVER_APP_TOKEN` | Pushover app token (required for alerts) |
| `PUSHOVER_USER_KEY` | Pushover user key (required for alerts) |
| `APEX_PUBLIC_BASE_URL` | Your public domain (required for action links) |
| `ACTION_TOKEN_SECRET` | Random secret for HMAC tokens — **change this!** |
| `ACCOUNT_SIZE_USD` | Your account size for position sizing |
| `RISK_PER_TRADE_PCT` | Max risk per trade as % of account (default: 1) |
| `MAX_DAILY_PLANNED_LOSS_PCT` | Daily loss lockout threshold (default: 3%) |
| `SCAN_MODE` | `MAJOR_ONLY` (default) or `ALL_PERPS` |
| `PRIORITY_SYMBOLS` | Always-scan symbols: `BTC,ETH,SOL,HYPE` |

See `.env.example` for full reference.

---

## Running Locally

```bash
# Initialize database
PYTHONPATH=src python scripts/init_db.py

# Start the bot
PYTHONPATH=src python -m apex.main

# Or use the dev script
bash scripts/run_dev.sh
```

The FastAPI server will start on `http://127.0.0.1:8000`.

Health check: `curl http://127.0.0.1:8000/health`

Status: `curl http://127.0.0.1:8000/status`

---

## Running Smoke Tests

```bash
# Test Hyperliquid connectivity (no credentials needed)
PYTHONPATH=src python scripts/smoke_test_hyperliquid.py

# Test Pushover (requires PUSHOVER_APP_TOKEN and PUSHOVER_USER_KEY in .env)
PYTHONPATH=src python scripts/smoke_test_pushover.py
```

---

## Running Unit Tests

```bash
PYTHONPATH=src pytest tests/ -v
```

Tests cover: EMA, RSI, ATR, VWAP, trend bias, risk sizing, throttles, daily lockout,
action tokens.

---

## Deploying to Hetzner with systemd

See [deploy/VPS_SETUP.md](deploy/VPS_SETUP.md) for full step-by-step instructions.

Summary:
1. Install Python 3.11+
2. Create `apex` user, clone to `/opt/apex`
3. Create venv, install deps
4. Copy and edit `.env`
5. Run `scripts/init_db.py`
6. Copy `deploy/apex.service.example` → `/etc/systemd/system/apex.service`
7. `systemctl enable --now apex`

---

## Optional Caddy Reverse Proxy

Once you have a domain pointing to your VPS:

```
# /etc/caddy/Caddyfile
apex.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Set `APEX_PUBLIC_BASE_URL=https://apex.example.com` in `.env` and restart.

Caddy handles TLS automatically via Let's Encrypt.

---

## How Pushover Action Links Work

Each alert contains clickable links like:

```
Enter: https://apex.example.com/action/alerts/{uid}/enter?token=...
Skip:  https://apex.example.com/action/alerts/{uid}/skip?token=...
```

Tokens are HMAC-signed with your `ACTION_TOKEN_SECRET` and expire after
`ACTION_TOKEN_TTL_HOURS` (default: 48h). They are stateless — no DB storage required.

When you tap **Enter**:
- Alert is marked `ENTERED`
- A paper trade is created
- A follow-up is sent after `FOLLOWUP_AFTER_ENTER_MINUTES` (default: 30m) with Win/Loss/Breakeven links

When you tap **Loss**:
- Trade risk is added to your daily planned loss tracker
- If daily limit is reached, CONFIRMED alerts are suppressed for the day

**Action links require `APEX_PUBLIC_BASE_URL` to be set.** Without it, alerts still send but contain a placeholder message instead of links.

---

## Known Limitations

- **No live execution** — MVP is alert-only
- **Position sizing is informational** — $100 account default, adjust `ACCOUNT_SIZE_USD`
- **VWAP is rolling, not session-anchored** — configurable lookback (default: 96 candles)
- **Candle history depends on Hyperliquid API** — `candleSnapshot` payload format should be verified against current API docs
- **`metaAndAssetCtxs` availability** — may not be a public endpoint; bot falls back gracefully
- **WebSocket feed symbols** — capped at 30 symbols to avoid subscription overload
- **No emergency Pushover priority** — emergency requires receipt handling; not implemented
- **Paper trade entry price** — uses alert's entry_high as a proxy; actual fill price is unknown

---

## Future Roadmap

1. Birdeye Solana token scanning
2. Solana DEX liquidity filters
3. Hyperliquid testnet/paper execution
4. One-click approved order execution (live trading)
5. Full auto-trading mode
6. Web dashboard
7. Backtesting engine
8. Strategy performance analytics
9. Reversal bounce strategy
10. News/volatility event filters
11. Funding rate / open interest filters
12. Telegram and Discord notifications
13. Multi-user support with auth
14. PostgreSQL migration
