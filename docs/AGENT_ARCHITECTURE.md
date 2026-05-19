# APEX Agent Architecture

## Core Design Principle

Agents in APEX start as **read-only analysts, not autonomous traders**.

Every agent is introduced in a mode where it:
- reads data from the DB or candle store
- writes structured JSON records to a dedicated DB table or stdout
- never sends alerts, never places trades, never changes config
- is explicitly promoted to an active role only after its read-only output has been reviewed and approved

This is a deliberate progression: observe → score → recommend → (human approves) → act.
No agent skips steps.

---

## Communication Model

Agents do not debate in freeform text. They communicate through:

1. **Structured DB records** — each agent writes to its own table (e.g., `signal_features`, `setup_scores`, `regime_snapshots`). Other agents read those records by query.
2. **JSON blobs** in `metadata_json` columns — used for supplementary detail that does not need its own column.
3. **Coordinator queries** — the Coordinator Agent assembles a composite view by joining multiple agent output tables. It does not ask agents to re-run; it reads their latest records.

No agent writes to another agent's output table. No agent reads raw env secrets or private keys.

---

## Architecture: Coordinator + Specialists

```
┌─────────────────────────────────────────────────────────┐
│                   Coordinator Agent                      │
│  Assembles: signal_features + scores + regime +          │
│  sr_levels + rel_strength + liquidity → composite view   │
│  Writes: composite_scores table (read-only output)       │
└──────────────────────────┬──────────────────────────────┘
                           │ reads
          ┌────────────────┼────────────────────┐
          ▼                ▼                    ▼
   Signal Quality    Setup Scoring       Market Regime
      Agent             Agent               Agent
          ▼                ▼                    ▼
   Multi-TF Conf    Support/Resistance    Relative Strength
      Agent             Agent                Agent
          ▼
   Liquidity/OB    News/Event Risk
      Agent            Agent
```

Each specialist writes to its own table. The Coordinator reads all tables and produces a ranked composite score per observation.

---

## Proposed Agents

### Signal Debug Agent

**Purpose:** Snapshot the raw signal conditions at the moment a signal fires — trend state, pullback state, RSI, ATR, EMAs, VWAP. Used to understand *why* a signal was generated.

**Inputs:**
- `signal_observations` row (symbol, direction, observed_at)
- Candle data at time of observation (from `candle_store` or `candles` table)
- Settings (timeframes, indicator params)

**Outputs:**
- `signal_features` table row with JSON blob of indicator values at observation time
- Fields: `observation_uid`, `ema_fast`, `ema_slow`, `rsi`, `atr`, `vwap`, `trend_bias`, `pullback_state`, `candle_high`, `candle_low`, `candle_close`, `captured_at`, `metadata_json`

**Forbidden actions:**
- Must not write to `alerts`, `paper_trades`, `daily_risk`, or `signal_observations`
- Must not send notifications
- Must not call external APIs

**First safe implementation:** Capture features synchronously in `_record_observation()` (dry-run only) and write to `signal_features`. Run as part of the existing 60-second scan.

---

### Signal Quality Agent

**Purpose:** Analyze `signal_observations` history to compute per-symbol and per-direction quality metrics. Feeds the scoring pipeline.

**Inputs:**
- `signal_observations` table (all closed CONFIRMED rows)
- Configurable lookback window (e.g., last 90 days or last N observations)

**Outputs:**
- `signal_quality_scores` table: `symbol`, `direction`, `n_observations`, `tp1_rate`, `tp2_rate`, `stop_rate`, `expiry_rate`, `avg_mfe`, `avg_mae`, `avg_time_to_1r_minutes`, `score` (0–1 normalized), `computed_at`

**Forbidden actions:**
- Read-only with respect to `signal_observations`
- Must not change alert behavior or signal generation

**First safe implementation:** Run as a scheduled job every 15 minutes in dry-run mode. Output is purely informational until the Scoring Agent is wired in.

---

### Setup Scoring Agent

**Purpose:** Produce a composite score (0–100) for each open observation by combining inputs from specialist agents. The score is a weighted sum; weights are configurable but defaults are hardcoded.

**Inputs:**
- `signal_quality_scores` (historical quality per symbol/direction)
- `regime_snapshots` (current market regime)
- `mtf_confirmations` (multi-timeframe alignment)
- `sr_levels` (distance to nearest support/resistance)
- `rel_strength_snapshots` (relative strength vs BTC)
- `liquidity_snapshots` (spread and order book depth)

**Outputs:**
- `setup_scores` table: `observation_uid`, `base_score`, `regime_adjustment`, `mtf_adjustment`, `sr_adjustment`, `rel_strength_adjustment`, `liquidity_adjustment`, `quality_adjustment`, `composite_score`, `scored_at`

**Score weighting (defaults, all configurable):**

| Component                | Default weight |
|--------------------------|---------------|
| Base setup score         | 30%           |
| Historical signal quality| 20%           |
| Market regime            | 15%           |
| Multi-TF alignment       | 15%           |
| Support/resistance       | 10%           |
| Relative strength        | 5%            |
| Liquidity/order book     | 5%            |

**Forbidden actions:**
- Must not trigger alerts — scores are informational only until 13A
- Must not modify `signal_observations`

**First safe implementation:** Run after observation evaluation pass; score all open observations. Log scores to `setup_scores`. No downstream action.

---

### Market Regime Agent

**Purpose:** Classify the current macro market state (trending up, trending down, ranging, high volatility) using BTC as the reference market. Used as a multiplier on setup scores.

**Inputs:**
- BTC candle data (15m and 1h timeframes) from candle store
- Configurable lookback (e.g., 96 candles = 24h on 15m)

**Outputs:**
- `regime_snapshots` table: `regime` (TRENDING_UP, TRENDING_DOWN, RANGING, HIGH_VOL), `btc_ema_fast`, `btc_ema_slow`, `btc_atr_pct`, `regime_score` (-1.0 to +1.0), `captured_at`

**Forbidden actions:**
- Must not use regime to autonomously suppress or amplify alerts
- Must not write outside `regime_snapshots`

**First safe implementation:** Run every 15 minutes on the BTC candle data that is already in the candle store. No BTC-specific API calls needed.

---

### Multi-Timeframe Confirmation Agent

**Purpose:** Check whether the signal direction is confirmed on both the trend timeframe (15m) and a higher timeframe (1h or 4h). Alignment score is used as a scoring adjustment.

**Inputs:**
- Candle data for the symbol across trend_timeframe, setup_timeframe, and 1h
- EMA/trend state computed per timeframe

**Outputs:**
- `mtf_confirmations` table: `symbol`, `direction`, `trend_15m`, `trend_1h`, `alignment_score` (-1.0 to +1.0), `captured_at`

**Forbidden actions:**
- Must not gate signals directly — only contributes to scoring

**First safe implementation:** Compute alignment at observation record time and write to `mtf_confirmations`.

---

### Support/Resistance Agent

**Purpose:** Identify key nearby support and resistance levels (pivot highs/lows, round numbers, prior session high/low). Score is based on how far the entry price is from the nearest adverse level.

**Inputs:**
- Candle data (setup_timeframe and trend_timeframe, lookback 200 candles)
- Entry price from `signal_observations`

**Outputs:**
- `sr_levels` table: `symbol`, `level_type` (SUPPORT, RESISTANCE, ROUND_NUMBER), `price`, `strength` (0–1), `timeframe`, `captured_at`
- `sr_scores` table: `observation_uid`, `nearest_adverse_level`, `distance_r`, `score`, `computed_at`

**Forbidden actions:**
- Must not use levels to modify stop prices in `signal_observations`
- Must not write to alerts or paper_trades

**First safe implementation:** Compute on demand when scoring an observation. Levels are computed fresh each run; no persistent caching initially.

---

### Relative Strength Agent

**Purpose:** Measure how a symbol is performing relative to BTC. Symbols with positive relative strength in the signal direction score higher.

**Inputs:**
- Symbol candle data (15m, lookback 96 candles)
- BTC candle data (same timeframe, same lookback)

**Outputs:**
- `rel_strength_snapshots` table: `symbol`, `rs_score` (-1.0 to +1.0), `symbol_return_24h`, `btc_return_24h`, `captured_at`

**Forbidden actions:**
- Read-only; no writes outside `rel_strength_snapshots`

**First safe implementation:** Run every 15 minutes for all scan-enabled symbols.

---

### Liquidity / Order Book Agent

**Purpose:** Assess current market liquidity to avoid trading in thin markets. Uses bid/ask spread and order book depth from the Hyperliquid info API.

**Inputs:**
- Hyperliquid `l2Book` snapshot (via existing `HyperliquidClient`)
- 24h volume from `markets` table

**Outputs:**
- `liquidity_snapshots` table: `symbol`, `bid_ask_spread_pct`, `book_depth_usd` (combined top 5 levels), `volume_24h_usd`, `liquidity_score` (0–1), `captured_at`

**Forbidden actions:**
- Must not use liquidity data to place or modify orders
- Must not call order placement endpoints

**First safe implementation:** Run every 5 minutes for scan-enabled symbols. Gated behind dry_run_mode.

---

### News / Event Risk Agent

**Purpose:** Flag time windows with elevated macro event risk (Fed announcements, major crypto events). Suppresses or reduces scores for signals within a configurable window around known events.

**Inputs:**
- User-maintained event calendar (flat JSON file or DB table)
- Current UTC time

**Outputs:**
- `event_risk_snapshots` table: `event_name`, `event_at`, `risk_window_start`, `risk_window_end`, `risk_level` (LOW, MEDIUM, HIGH), `active` (bool), `captured_at`

**Forbidden actions:**
- Must not autonomously suppress alerts — only contributes a score adjustment
- Must not fetch from external news APIs without explicit approval

**First safe implementation:** Read from a static `data/events.json` file that the user maintains manually.

---

### Trade Review Agent

**Purpose:** After a paper trade closes (or an observation resolves), generate a structured post-trade review: entry quality, exit quality, comparison to the pre-trade score, and lessons.

**Inputs:**
- Closed `signal_observations` row
- Corresponding `setup_scores` row (if available)
- `signal_features` captured at observation time

**Outputs:**
- `trade_reviews` table: `observation_uid`, `entry_quality_score`, `exit_quality_score`, `predicted_score`, `actual_outcome_r`, `review_notes_json`, `reviewed_at`

**Forbidden actions:**
- Read-only with respect to all other tables
- Must not trigger follow-up alerts

**First safe implementation:** Run as a batch job after each evaluation pass, reviewing any newly closed CONFIRMED observations.

---

### Paper Execution Agent

**Purpose:** Simulate order execution on confirmed signals — track virtual entry, stop management (break-even, trailing), and exit. Uses candle data; no real orders ever placed.

**Inputs:**
- Open `signal_observations` with status OBSERVED
- Candle data for the symbol

**Outputs:**
- `paper_positions` table: `observation_uid`, `symbol`, `direction`, `virtual_entry`, `current_stop`, `current_target`, `status`, `pnl_r`, `opened_at`, `closed_at`

**Forbidden actions:**
- Must not connect to exchange APIs
- Must not place or simulate orders using real account state
- Must not read private keys or wallet addresses

**First safe implementation:** Mirror the existing observation evaluation logic with a virtual position tracker. Runs in dry_run_mode only.

---

### Risk / Position Sizing Agent

**Purpose:** Given a proposed entry, compute optimal position size based on account equity, current drawdown, recent win/loss streak, and composite setup score. Produces a suggested size only — no execution.

**Inputs:**
- `daily_risk` table (current drawdown state)
- `setup_scores` (composite score for the observation)
- Settings (account_size_usd, risk_per_trade_pct, max_stop_distance_pct)
- Historical `signal_quality_scores` for Kelly-fraction guidance

**Outputs:**
- `sizing_recommendations` table: `observation_uid`, `suggested_risk_pct`, `suggested_notional_usd`, `kelly_fraction`, `drawdown_adjustment`, `score_adjustment`, `final_risk_usd`, `computed_at`

**Forbidden actions:**
- Must not place orders
- Must not modify `daily_risk` table
- Must not read exchange credentials

**First safe implementation:** Run as a read-only sizing pass after the Setup Scoring Agent.

---

### Approval Flow / Execution Agent (Future — Milestone 13A+)

**Purpose:** When human approval is granted, convert a high-scoring observation into a live alert and eventually a real trade. This agent only activates after explicit written confirmation from the user and with ALERTS_ENABLED=true set in the environment.

**Inputs:**
- `composite_scores` row with score above configured threshold
- Human approval signal (API endpoint or CLI command)
- Exchange API credentials (only present when explicitly configured)

**Outputs:**
- `alerts` row (existing table)
- Pushover notification
- (Future) Real order to exchange

**Forbidden actions:**
- Must not activate while DRY_RUN_MODE=true
- Must not activate while ALERTS_ENABLED=false
- Must not read exchange credentials unless LIVE_TRADING_ENABLED=true is explicitly set
- Must never auto-approve — human confirmation is always required

**First safe implementation:** Not implemented until Milestone 13A. All prior milestones must be reviewed and approved first.

---

## Agent Communication Examples

### Signal feature capture (Signal Debug Agent output)

```json
{
  "observation_uid": "abc12345-...",
  "ema_fast": 43210.5,
  "ema_slow": 42980.2,
  "rsi": 48.3,
  "atr": 312.4,
  "vwap": 43050.0,
  "trend_bias": "LONG",
  "pullback_state": "CONFIRMED",
  "candle_high": 43280.0,
  "candle_low": 43100.0,
  "candle_close": 43230.0,
  "captured_at": "2026-05-19T14:22:00Z"
}
```

### Composite score (Coordinator output)

```json
{
  "observation_uid": "abc12345-...",
  "symbol": "BTC",
  "direction": "LONG",
  "base_score": 72,
  "regime_adjustment": +5,
  "mtf_adjustment": +8,
  "sr_adjustment": -3,
  "rel_strength_adjustment": +4,
  "liquidity_adjustment": +2,
  "quality_adjustment": +6,
  "composite_score": 94,
  "scored_at": "2026-05-19T14:22:05Z"
}
```

### Market regime snapshot

```json
{
  "regime": "TRENDING_UP",
  "btc_ema_fast": 43210.5,
  "btc_ema_slow": 42980.2,
  "btc_atr_pct": 0.72,
  "regime_score": 0.65,
  "captured_at": "2026-05-19T14:15:00Z"
}
```

---

## Safety Rules (Non-Negotiable)

These rules apply to every agent at every milestone:

1. **No live alerts without explicit approval.** ALERTS_ENABLED must be set to true in the environment by the user. No agent sets this automatically.
2. **No trading without explicit approval.** LIVE_TRADING_ENABLED does not exist yet. It will only be added at Milestone 14+.
3. **No exchange keys.** No agent reads, writes, or transmits private keys or wallet mnemonics.
4. **No public port 8000.** The FastAPI server binds to 127.0.0.1 only.
5. **No automatic config changes.** Agents never write to `.env`, `pyproject.toml`, or systemd unit files.
6. **No automatic leverage or risk changes.** Sizing recommendations are outputs only; they never write to `daily_risk` or settings.
7. **Dry-run gate everywhere.** Every new agent feature is gated behind `dry_run_mode=True` until explicitly promoted.
8. **No freeform LLM decisions in the execution path.** Agents produce structured scores; a human approves before any action.
