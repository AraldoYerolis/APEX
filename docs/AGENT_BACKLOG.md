# APEX Agent Backlog

Implementation roadmap for the APEX agent system.
Each milestone builds on the previous. No milestone skips the safety progression.

---

## Milestone 10B — Signal Feature Snapshot + Learning Memory

**Goal:** Capture a snapshot of indicator values (EMA, RSI, ATR, VWAP, trend bias, pullback state) at the exact moment each signal observation is recorded. This creates the foundational feature store for all downstream scoring agents.

**Implementation notes:**
- Add a `signal_features` DB table: `observation_uid` (FK), `ema_fast`, `ema_slow`, `rsi`, `atr`, `vwap`, `trend_bias`, `pullback_state`, `candle_high`, `candle_low`, `candle_close`, `captured_at`, `metadata_json`
- Capture features synchronously in `_record_observation()` immediately after inserting the observation row
- Write only when `dry_run_mode=True`
- Add a migration for the new table (same pattern as Milestone 10A — `CREATE TABLE IF NOT EXISTS`)
- Add a read-only `scripts/show_signal_features.py` dump script

**Safety constraints:**
- Must not change signal generation or alert behavior
- Must not write to `alerts`, `paper_trades`, `daily_risk`, `signal_observations`
- Feature capture failure must be swallowed (same pattern as observation insert failure)

**Success criteria:**
- Every new `signal_observations` row has a corresponding `signal_features` row
- Feature values match what was computed at observation time (not re-computed later)
- Existing observations without features remain valid (NULL features = pre-10B)

**Tests required:**
- Feature row is created when observation is inserted
- Feature row matches indicator values from the candle store at that moment
- Feature capture failure does not interrupt observation insert
- No feature rows for SETUP_FORMING (no risk_plan, skip feature capture)
- Feature table is not written in live mode (dry_run_mode=False)

**What not to do:**
- Do not backfill features for existing observations (candle state at that time is gone)
- Do not compute features asynchronously — capture inline while candles are fresh
- Do not add ML models or training loops yet

---

## Milestone 10C — Signal Quality Agent / Learning Report v2  ← COMPLETE

**Goal:** Deep read-only analysis of `signal_features` + `signal_observations` history.
Symbol/direction quality scores, macro alignment, feature buckets, expiry quality.

**What was built:**
- `scripts/report_signal_learning.py` completely rewritten as v2
- Fixed root bug: `create_apex_snapshot.py` was hardcoding `--limit 500`, causing the
  learning report to only see ~210 of 288 confirmed observations. Removed the cap.
- `--limit` now defaults to `None` (unlimited); the SQL `--since` filter is applied
  in the WHERE clause (not in Python after a capped load)
- New sections: long vs short breakdown, symbol+direction breakdown with quality
  scores and recommendation labels, BTC/ETH macro alignment tables, expiry quality
  (after-TP1 vs without-TP1), feature bucket analysis (RSI, ATR%, VWAP%, EMA spread%)
- MFE/MAE pulled via SQL LEFT JOIN with `signal_observations` (no schema change)
- Diagnostic quality score formula: tp1_rate×0.30 + tp2_rate×0.40 − stop_rate×0.20
  − expiry_no_tp1_rate×0.10 ± MFE/MAE adjustments. Clamped [0, 1].
- Recommendation labels: PROMISING_OBSERVE / NEEDS_FILTERING / WEAK_OBSERVE_ONLY /
  INSUFFICIENT_SAMPLE. Min N=10 required for a non-insufficient label.
- 32 new tests in `tests/test_signal_learning_v2.py`

**How to run:**
```bash
# Full dataset (unlimited)
PYTHONPATH=src python scripts/report_signal_learning.py

# Filtered since a timestamp
PYTHONPATH=src python scripts/report_signal_learning.py --since 2026-05-20T00:00:00Z

# Export raw features to CSV
PYTHONPATH=src python scripts/report_signal_learning.py --csv /tmp/features.csv

# Manual row cap for debugging
PYTHONPATH=src python scripts/report_signal_learning.py --limit 200
```

**Safety constraints (all maintained):**
- Read-only: never writes to `alerts`, `paper_trades`, `daily_risk`, `signal_observations`
- Quality scores and recommendation labels are DIAGNOSTIC ONLY
- Labels are not used for signal generation, filtering, or alert gating
- No config values are changed at runtime
- `ALERTS_ENABLED=false`, `DRY_RUN_MODE=true` are not touched

**What the major sections mean:**
- **Outcome summary**: overall TP1/TP2/stop/expiry rates across all closed confirmed features
- **Long vs short**: compare outcomes by direction; warns when SHORT N < 10
- **Symbol + direction breakdown**: per-group metrics, diagnostic score (0–1), recommendation label
- **Macro alignment**: how BTC/ETH trend bias at capture time correlates with outcomes
- **Expiry quality**: separates "expired after TP1" (partial win) from "expired without TP1" (full miss)
- **Feature buckets**: RSI/ATR/VWAP/EMA spread bucket analysis — which indicator conditions correlate with better or worse outcomes
- **Diagnostic quality scores**: REPORT-ONLY, not stored in DB, not used for trading

**What not to do:**
- Do not use quality scores or recommendation labels to filter signals or change config
- Do not store diagnostic scores in DB until a dedicated `signal_quality_scores` table
  is designed (planned for a future 10D sub-milestone)
- Do not re-enable `--limit` cap in snapshot scripts

---

## Milestone 10D — Setup Scoring Agent

**Goal:** Combine available agent outputs (quality scores, plus placeholders for regime/MTF/SR) into a single composite score per open observation.

**Implementation notes:**
- Add a `setup_scores` table (see AGENT_ARCHITECTURE.md for full schema)
- Implement `run_setup_scoring_agent()` — runs after observation evaluation pass
- Score all OBSERVED CONFIRMED observations
- Weights are constants in config; default weights from AGENT_ARCHITECTURE.md
- Missing inputs (e.g., regime not yet implemented) use a neutral adjustment of 0

**Safety constraints:**
- Scores are informational only — no alert gating yet
- Read-only with respect to all input tables

**Success criteria:**
- Every open CONFIRMED observation has a `setup_scores` row within 2 evaluation passes
- Score updates on every evaluation pass
- Missing upstream inputs produce neutral (0) adjustments, not errors

**Tests required:**
- Composite score computed correctly with all inputs present
- Composite score computed with only quality_scores available (others NULL → 0 adjustment)
- Score bounded [0, 100]

**What not to do:**
- Do not gate signals on score threshold yet
- Do not hardcode symbol-specific weights

---

## Milestone 10E — Multi-Timeframe Confirmation Agent

**Goal:** Check 15m and 1h trend alignment for each signal. Add MTF alignment as an input to the Setup Scoring Agent.

**Implementation notes:**
- Add a `mtf_confirmations` table
- Compute 1h trend bias (EMA fast/slow) at observation time
- Alignment score: both aligned = +1.0, mixed = 0.0, opposed = -1.0
- Backfill not required — new observations only

**Safety constraints:**
- Must not require new API calls — use candle data already in the candle store
- If 1h candles are not in the store, alignment_score = 0.0 (neutral), not an error

**Success criteria:**
- MTF alignment computed for all new CONFIRMED observations
- 1h candle data is already being fetched by the WebSocket subscriptions if 1h is added to the timeframes list (may require adding 1h to backfill list)

**Tests required:**
- Alignment score = +1.0 when 15m and 1h both agree
- Alignment score = 0.0 when 1h data is missing
- MTF table not written in live mode

**What not to do:**
- Do not add 4h timeframe yet — complexity/data cost is not yet justified
- Do not add MTF as a hard gate on signal generation

---

## Milestone 10F — Support/Resistance Agent

**Goal:** Identify recent pivot highs/lows and compute how far the entry price is from the nearest adverse S/R level. Add SR score as an input to Setup Scoring.

**Implementation notes:**
- Compute pivots from 200-candle lookback on setup_timeframe
- Adverse level for LONG = nearest resistance above entry; for SHORT = nearest support below entry
- Distance in R units = distance_to_level / stop_distance
- Score = min(1.0, distance_r / 2.0) — higher = more room before obstruction

**Safety constraints:**
- Must not modify stop prices or targets
- Must not use levels to block signal generation

**Success criteria:**
- SR score computed for all new CONFIRMED observations with sufficient candle history
- Score of 0 (worst) when entry is directly at a resistance level
- Score of 1 (best) when nearest adverse level is ≥ 2R away

**Tests required:**
- Score = 0 when entry touches resistance
- Score = 1 when nearest adverse level is ≥ 2R away
- Graceful handling of insufficient candle history

**What not to do:**
- Do not implement volume-at-price or market profile yet
- Do not persist levels to DB — compute fresh each run

---

## Milestone 10G — Market Regime Agent

**Goal:** Classify the current market regime using BTC as proxy. Add regime adjustment to Setup Scoring.

**Implementation notes:**
- Regimes: TRENDING_UP, TRENDING_DOWN, RANGING, HIGH_VOL
- Classification based on BTC 15m EMA alignment + ATR percentile
- Regime score: TRENDING_UP LONG = +1.0, TRENDING_DOWN SHORT = +1.0, opposed = -0.5, RANGING = 0.0, HIGH_VOL = -0.3
- Add `regime_snapshots` table

**Safety constraints:**
- Must not suppress signals in live mode
- BTC is a scan-enabled symbol already; use existing candle data

**Success criteria:**
- Regime classified every 15 minutes
- Regime score is applied as an adjustment to composite score in 10D

**Tests required:**
- TRENDING_UP regime when BTC EMA fast > slow and ATR is not elevated
- HIGH_VOL regime when ATR percentile > 80th

**What not to do:**
- Do not use on-chain data or funding rates yet
- Do not use regime to hard-filter signals

---

## Milestone 10H — Relative Strength Agent

**Goal:** Score each symbol's recent price performance relative to BTC. Symbols outperforming BTC in the signal direction score higher.

**Implementation notes:**
- Use 24h simple return: (close_now - close_24h_ago) / close_24h_ago
- RS score = symbol_return - btc_return, clamped to [-1, +1]
- Positive RS score for LONG = favorable; negative RS for SHORT = favorable
- Add `rel_strength_snapshots` table; update every 15 minutes

**Safety constraints:**
- BTC candles must already be in the candle store (guaranteed if BTC is priority symbol)

**Success criteria:**
- RS score computed for all scan-enabled symbols every 15 minutes

**Tests required:**
- RS score = +1 when symbol outperforms BTC by > 1 std dev
- RS score = 0 when symbol performance matches BTC

**What not to do:**
- Do not compute intra-candle RS; use close-to-close
- Do not add sector/basket comparisons yet

---

## Milestone 11A — Paper Execution Agent

**Goal:** Track virtual positions for open observations — entry, stop management, and exit — using candle data. Produces realistic simulated P&L in R units.

**Implementation notes:**
- Add `paper_positions` table
- For each open CONFIRMED observation, open a virtual position at entry_price
- Track: break-even stop move (after TP1), trailing stop (optional, configurable)
- Close virtual position when observation closes
- P&L tracked in R units

**Safety constraints:**
- Must not connect to exchange
- Must not read wallet/account state
- Runs in dry_run_mode only

**Success criteria:**
- Virtual P&L matches expected R outcome for known test cases
- Break-even stop logic correctly applied after TP1 milestone

**Tests required:**
- Virtual position opens when observation is OBSERVED
- Break-even stop applied after TP1
- Position closes with correct P&L when observation closes

**What not to do:**
- Do not simulate slippage or funding costs yet — simple R-unit tracking only
- Do not write real order records

---

## Milestone 11B — Trade Review Agent

**Goal:** Auto-generate a structured post-trade review for every closed CONFIRMED observation. Compare predicted score vs actual outcome.

**Implementation notes:**
- Add `trade_reviews` table
- Review triggered by observation close (EXPIRED, STOPPED, HIT_2R)
- Compare `setup_scores.composite_score` (if available) to actual `outcome_r`
- Generate structured notes JSON: entry quality, exit quality, what the setup scored vs what happened

**Safety constraints:**
- Read-only with respect to all tables except `trade_reviews`
- Must not re-open closed observations

**Success criteria:**
- Every newly closed CONFIRMED observation gets a review within one evaluation pass
- Reviews include setup score comparison when 10D data is available

**Tests required:**
- Review created for newly closed observation
- Review gracefully handles missing setup_scores (pre-10D observations)

**What not to do:**
- Do not use LLM for review generation yet
- Do not email/push reviews

---

## Milestone 12A — Risk / Position Sizing Agent

**Goal:** Given a high-scoring observation and current drawdown state, suggest an optimal position size. Output is a recommendation only — no execution.

**Implementation notes:**
- Add `sizing_recommendations` table
- Base risk = `settings.risk_per_trade_pct`
- Drawdown adjustment: reduce size by 25% per 1% drawdown beyond 1%
- Score adjustment: scale size by composite_score / 100 (e.g., score 80 → 80% of base risk)
- Optional Kelly fraction using historical tp1_rate and win_size/loss_size from quality scores

**Safety constraints:**
- Must not modify `daily_risk`
- Must not place orders
- Sizing is a suggestion only

**Success criteria:**
- Sizing recommendation produced for all open CONFIRMED observations with composite scores
- Size is reduced correctly at elevated drawdown levels

**Tests required:**
- Size reduced at 2% drawdown
- Size increased (up to 1.5x max) at high composite score
- Size never exceeds 2x base risk regardless of score

**What not to do:**
- Do not implement Kelly sizing by default — opt-in only
- Do not expose sizing via public API endpoint

---

## Milestone 13A — Approval-Required Alert Flow

**Goal:** When a human explicitly enables alerts, high-scoring observations are promoted to real alerts with human approval in the loop.

**Implementation notes:**
- Requires ALERTS_ENABLED=true set explicitly by user
- Minimum composite score threshold (configurable, default 75)
- Alert is staged for approval — written to `staged_alerts` table first
- Human reviews via a new API endpoint or CLI command
- Only after explicit approval does the alert go to Pushover

**Safety constraints:**
- Must not activate while DRY_RUN_MODE=true
- Must not auto-approve — human confirmation always required
- ALERTS_ENABLED gate must not be bypassed

**Success criteria:**
- No alerts fire without human approval
- Staged alerts expire if not approved within a configurable window
- Approval action is logged to `app_events`

**Tests required:**
- Alert staged but not sent without approval
- Alert sent after approval
- Alert expired when approval window lapses

**What not to do:**
- Do not auto-approve based on score alone
- Do not add webhook-based auto-approval

---

## Milestone 14+ — Live Execution

**Goal:** Enable real order placement on Hyperliquid after all prior milestones have been reviewed, validated, and explicitly approved.

**Prerequisites (all must be met before implementation):**
- Milestones 10A–13A complete and reviewed
- Signal quality scores show positive expected value (tp2_rate > stop_rate over N>=100 observations)
- Paper execution agent P&L is positive over N>=50 paper positions
- User has explicitly approved moving to live mode in writing
- LIVE_TRADING_ENABLED=true set manually in production .env by user

**Implementation notes:**
- Adds a `HyperliquidExecutor` class — isolated in its own module
- Uses Hyperliquid API private key (never stored in DB, read from env only)
- Order sizes from `sizing_recommendations`
- Full audit trail: every order attempt logged to `order_log` table

**Safety constraints:**
- Private key must never be logged, written to DB, or transmitted outside the exchange API
- Position size hard cap: 2x `max_risk_usd` regardless of signals
- Automatic kill switch: if daily loss > `max_daily_loss_usd`, halt all trading immediately
- No leverage changes without explicit config update

**Success criteria:**
- Live orders placed only after human approval chain from 13A
- Every order has a corresponding `order_log` entry
- Kill switch tested in staging before production use

**What not to do:**
- Do not implement until all prior milestones are reviewed
- Do not add market orders without slippage consideration
- Do not auto-leverage

---

## Milestone ordering summary

```
10A  TP1 non-terminal milestone tracking       ← COMPLETE
10B  Signal feature snapshot                   ← COMPLETE
10C  Signal quality agent / learning report v2 ← COMPLETE
10D  Setup scoring agent
10E  Multi-TF confirmation
10F  Support/resistance agent
10G  Market regime agent
10H  Relative strength agent
11A  Paper execution agent
11B  Trade review agent
12A  Risk/position sizing agent
13A  Approval-required alert flow
14+  Live execution (explicit approval only)
```
