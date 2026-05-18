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

| Variable | Default | Description |
|---|---|---|
| `PUSHOVER_APP_TOKEN` | (empty) | Pushover app token — required to send alerts |
| `PUSHOVER_USER_KEY` | (empty) | Pushover user key — required to send alerts |
| `APEX_PUBLIC_BASE_URL` | (empty) | Your public domain — required for action links |
| `ACTION_TOKEN_SECRET` | (placeholder) | HMAC secret for action links — **change this!** |
| `ALERTS_ENABLED` | `false` | Master alert switch — set `true` to send live alerts |
| `DRY_RUN_MODE` | `true` | Adds `[DRY RUN]` prefix to suppressed-alert log lines |
| `ALERT_TYPES_ENABLED` | `CONFIRMED_SETUP` | Which alert types to send — `SETUP_FORMING`, `CONFIRMED_SETUP`, or both comma-separated |
| `ACCOUNT_SIZE_USD` | `100` | Your account size for position sizing |
| `RISK_PER_TRADE_PCT` | `1` | Max risk per trade as % of account |
| `MAX_DAILY_PLANNED_LOSS_PCT` | `3` | Daily loss lockout threshold |
| `SCAN_MODE` | `MAJOR_ONLY` | `MAJOR_ONLY` or `ALL_PERPS` |
| `PRIORITY_SYMBOLS` | `BTC,ETH,SOL,HYPE` | Always-scan symbols regardless of volume |

See `.env.example` for the full reference.

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

## Local Runtime Safety

APEX defaults to a safe, non-alerting state out of the box:

| Setting | Default | Meaning |
|---|---|---|
| `ALERTS_ENABLED` | `false` | Bot scans and logs, but **never sends Pushover notifications** |
| `DRY_RUN_MODE` | `true` | Log lines for suppressed alerts show `[DRY RUN]` prefix |
| `ALERT_TYPES_ENABLED` | `CONFIRMED_SETUP` | Only CONFIRMED setups will send (when enabled) |

**Enable alerts only when you are ready:**

```ini
# In .env
ALERTS_ENABLED=true
DRY_RUN_MODE=false
ALERT_TYPES_ENABLED=CONFIRMED_SETUP
```

Smoke test scripts (`smoke_test_pushover.py`, `send_simulated_alert.py`) bypass the
`ALERTS_ENABLED` flag intentionally — they send directly via Pushover to test credentials
and message formatting.

---

## Running Smoke Tests

```bash
# Test Hyperliquid connectivity (no credentials needed)
PYTHONPATH=src python scripts/smoke_test_hyperliquid.py

# Test Pushover credentials and delivery
PYTHONPATH=src python scripts/smoke_test_pushover.py

# Send a simulated strategy alert to test message formatting
PYTHONPATH=src python scripts/send_simulated_alert.py --type SETUP_FORMING
PYTHONPATH=src python scripts/send_simulated_alert.py --type CONFIRMED_SETUP
PYTHONPATH=src python scripts/send_simulated_alert.py --type CONFIRMED_SETUP --symbol ETH --direction SHORT
```

---

## Local HTTP Action Route Smoke Test

Validates every action route end-to-end through real HTTP against a locally
running APEX server — no Pushover, no live trades, no private keys.

**Terminal 1 — start APEX:**
```bash
PYTHONPATH=src .venv/bin/python -m apex.main
```

**Terminal 2 — run the smoke test:**
```bash
PYTHONPATH=src .venv/bin/python scripts/smoke_test_action_routes.py
```

What it checks:
- `/action/alerts/{uid}/enter` → alert status=ENTERED, paper_trade created
- `/action/alerts/{uid}/skip` → alert status=SKIPPED
- `/action/alerts/{uid}/snooze-15` → alert status=SNOOZED, snooze row in DB
- `/action/trades/{uid}/win` → trade status=WIN
- `/action/trades/{uid}/breakeven` → trade status=BREAKEVEN
- `/action/trades/{uid}/loss` → trade status=LOSS, daily_risk incremented
- Duplicate actions → idempotent "Already Entered / Already Closed" response
- Invalid token → "Invalid Token" response
- Missing alert/trade → "Not Found" response

All smoke rows use the symbol `SMOKE_BTC` and UIDs prefixed with `smoke_` so
they are easy to identify and clean up manually if needed.

---

## Running Unit Tests

```bash
PYTHONPATH=src pytest tests/ -v
```

Tests cover: EMA, RSI, ATR, VWAP, trend bias, risk sizing, throttles, daily lockout,
action tokens, action handler lifecycle, dry-run alert accounting, shutdown ordering.

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
