-- APEX SQLite schema

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ---------------------------------------------------------------
-- markets
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS markets (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol                  TEXT NOT NULL UNIQUE,
    is_active               INTEGER NOT NULL DEFAULT 1,
    is_priority             INTEGER NOT NULL DEFAULT 0,
    scan_enabled            INTEGER NOT NULL DEFAULT 1,
    last_24h_volume_usd     REAL,
    last_open_interest_usd  REAL,
    last_mid_price          REAL,
    created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_markets_symbol ON markets(symbol);

-- ---------------------------------------------------------------
-- candles
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS candles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    timeframe   TEXT NOT NULL,
    open_time   INTEGER NOT NULL,   -- Unix ms
    close_time  INTEGER NOT NULL,   -- Unix ms
    open        REAL NOT NULL,
    high        REAL NOT NULL,
    low         REAL NOT NULL,
    close       REAL NOT NULL,
    volume      REAL NOT NULL DEFAULT 0,
    is_closed   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE(symbol, timeframe, open_time)
);

CREATE INDEX IF NOT EXISTS idx_candles_lookup ON candles(symbol, timeframe, open_time);

-- ---------------------------------------------------------------
-- alerts
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alerts (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_uid               TEXT NOT NULL UNIQUE,
    symbol                  TEXT NOT NULL,
    direction               TEXT NOT NULL CHECK(direction IN ('LONG','SHORT')),
    alert_type              TEXT NOT NULL CHECK(alert_type IN ('SETUP_FORMING','CONFIRMED_SETUP')),
    setup_type              TEXT NOT NULL DEFAULT 'TREND_PULLBACK',
    status                  TEXT NOT NULL DEFAULT 'SENT'
                                CHECK(status IN ('SENT','ENTERED','SKIPPED','SNOOZED','EXPIRED')),
    entry_low               REAL,
    entry_high              REAL,
    reference_price         REAL,
    stop_price              REAL,
    target_1r               REAL,
    target_2r               REAL,
    invalidation_price      REAL,
    risk_usd                REAL,
    suggested_notional_usd  REAL,
    stop_distance_pct       REAL,
    confidence_score        REAL,
    message                 TEXT,
    expires_at              TEXT,
    sent_at                 TEXT,
    created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_alerts_symbol ON alerts(symbol, alert_type, status);
CREATE INDEX IF NOT EXISTS idx_alerts_uid ON alerts(alert_uid);

-- ---------------------------------------------------------------
-- paper_trades
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS paper_trades (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_uid               TEXT NOT NULL UNIQUE,
    alert_id                INTEGER NOT NULL REFERENCES alerts(id),
    symbol                  TEXT NOT NULL,
    direction               TEXT NOT NULL CHECK(direction IN ('LONG','SHORT')),
    setup_type              TEXT NOT NULL DEFAULT 'TREND_PULLBACK',
    entry_price             REAL NOT NULL,
    stop_price              REAL NOT NULL,
    target_1r               REAL NOT NULL,
    target_2r               REAL NOT NULL,
    risk_usd                REAL NOT NULL,
    suggested_notional_usd  REAL,
    status                  TEXT NOT NULL DEFAULT 'OPEN'
                                CHECK(status IN ('OPEN','WIN','LOSS','BREAKEVEN','CLOSED_MANUAL')),
    opened_at               TEXT NOT NULL,
    outcome_at              TEXT,
    followup_sent_at        TEXT,
    created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_trades_uid ON paper_trades(trade_uid);
CREATE INDEX IF NOT EXISTS idx_trades_status ON paper_trades(status);

-- ---------------------------------------------------------------
-- daily_risk
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS daily_risk (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date              TEXT NOT NULL UNIQUE,   -- YYYY-MM-DD UTC
    planned_loss_used_usd   REAL NOT NULL DEFAULT 0,
    max_planned_loss_usd    REAL NOT NULL,
    lockout_active          INTEGER NOT NULL DEFAULT 0,
    created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- ---------------------------------------------------------------
-- snoozes
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS snoozes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    snoozed_until   TEXT NOT NULL,
    reason          TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_snoozes_symbol ON snoozes(symbol, snoozed_until);

-- ---------------------------------------------------------------
-- app_events
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    level           TEXT NOT NULL DEFAULT 'INFO',
    event_type      TEXT NOT NULL,
    message         TEXT NOT NULL,
    metadata_json   TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_app_events_type ON app_events(event_type, created_at);

-- ---------------------------------------------------------------
-- signal_observations
-- Dry-run forward-test tracking. Completely separate from alerts,
-- paper_trades, and daily_risk. Never consumed by cooldown logic.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signal_observations (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_uid         TEXT UNIQUE NOT NULL,
    observed_at             TEXT NOT NULL,           -- ISO8601 UTC
    symbol                  TEXT NOT NULL,
    direction               TEXT NOT NULL CHECK(direction IN ('LONG','SHORT')),
    signal_type             TEXT NOT NULL CHECK(signal_type IN ('SETUP_FORMING','CONFIRMED_SETUP')),
    -- Price levels: NULL for SETUP_FORMING (no risk_plan at that stage)
    entry_price             REAL,
    stop_price              REAL,
    target_1r               REAL,
    target_2r               REAL,
    status                  TEXT NOT NULL DEFAULT 'OBSERVED'
                                CHECK(status IN ('OBSERVED','HIT_1R','HIT_2R','STOPPED','EXPIRED')),
    outcome_r               REAL,                    -- +1/+2/-1 on close; NULL while open
    max_favorable_excursion REAL,                    -- running max move toward target (in R)
    max_adverse_excursion   REAL,                    -- running max move against position (in R)
    expires_at              TEXT NOT NULL,
    closed_at               TEXT,
    metadata_json           TEXT,                    -- trend/pullback reasons at observation time
    -- Milestone 10A: TP1 is non-terminal; tracked as a milestone, not a close event.
    -- These columns are NULL on rows written before this milestone (backward compat).
    hit_1r_at               TEXT,                    -- ISO8601 UTC when TP1 first reached
    hit_2r_at               TEXT,                    -- ISO8601 UTC when TP2 reached
    stopped_at              TEXT,                    -- ISO8601 UTC when stopped
    expired_at              TEXT,                    -- ISO8601 UTC when expired
    first_terminal_status   TEXT,                    -- HIT_2R | STOPPED | EXPIRED
    final_status            TEXT,                    -- same as first_terminal_status for now
    time_to_1r_seconds      REAL,                    -- seconds from observed_at to hit_1r_at
    time_to_2r_seconds      REAL,                    -- seconds from observed_at to hit_2r_at
    time_to_stop_seconds    REAL,                    -- seconds from observed_at to stopped_at
    time_to_expiry_seconds  REAL,                    -- seconds from observed_at to expired_at
    hit_1r_before_stop      INTEGER,                 -- 1 if TP1 was hit before stop; 0 otherwise
    hit_1r_before_expiry    INTEGER,                 -- 1 if TP1 was hit before expiry; 0 otherwise
    created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_observations_symbol
    ON signal_observations(symbol, signal_type, status);
CREATE INDEX IF NOT EXISTS idx_observations_uid
    ON signal_observations(observation_uid);
CREATE INDEX IF NOT EXISTS idx_observations_open
    ON signal_observations(status, expires_at);

-- ---------------------------------------------------------------
-- signal_features
-- Captures indicator/market context at observation time.
-- Outcome fields are denormalized from signal_observations and
-- synced after each evaluation pass.
-- Dry-run only. Never modifies alerts, paper_trades, or live state.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signal_features (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_uid         TEXT NOT NULL UNIQUE REFERENCES signal_observations(observation_uid),
    captured_at             TEXT NOT NULL,
    symbol                  TEXT NOT NULL,
    direction               TEXT NOT NULL,
    signal_type             TEXT NOT NULL,
    observed_at             TEXT NOT NULL,
    -- Price levels
    entry_price             REAL,
    stop_price              REAL,
    target_1r               REAL,
    target_2r               REAL,
    -- Candle context (last closed candle at capture time, setup_timeframe)
    candle_open             REAL,
    candle_high             REAL,
    candle_low              REAL,
    candle_close            REAL,
    candle_volume           REAL,
    candle_open_time        INTEGER,
    -- Indicator snapshot
    rsi_val                 REAL,
    atr_val                 REAL,
    vwap_val                REAL,
    ema_fast                REAL,
    ema_slow                REAL,
    price_vs_vwap_pct       REAL,        -- (price - vwap) / vwap * 100
    ema_spread_pct          REAL,        -- (ema_fast - ema_slow) / ema_slow * 100
    atr_pct                 REAL,        -- atr / price * 100
    -- Trend/setup context
    trend_bias              TEXT,        -- LONG | SHORT | NONE
    trend_reason            TEXT,
    pullback_state          TEXT,
    pullback_reason         TEXT,
    -- Market context (populated where available)
    btc_trend_bias          TEXT,
    eth_trend_bias          TEXT,
    market_regime_label     TEXT,
    relative_strength_rank  INTEGER,
    relative_strength_score REAL,
    -- Outcome fields (synced from signal_observations after close/milestone)
    outcome_status          TEXT,        -- mirrors status / final_status
    outcome_r               REAL,
    hit_1r_at               TEXT,
    hit_2r_at               TEXT,
    stopped_at              TEXT,
    expired_at              TEXT,
    time_to_1r_seconds      REAL,
    time_to_2r_seconds      REAL,
    time_to_stop_seconds    REAL,
    time_to_expiry_seconds  REAL,
    hit_1r_before_stop      INTEGER,
    hit_1r_before_expiry    INTEGER,
    -- Metadata
    feature_version         TEXT NOT NULL DEFAULT '10B_v1',
    metadata_json           TEXT,
    created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_signal_features_uid
    ON signal_features(observation_uid);
CREATE INDEX IF NOT EXISTS idx_signal_features_symbol
    ON signal_features(symbol, signal_type, captured_at);

-- ---------------------------------------------------------------
-- opportunity_observations
-- TA Opportunity Engine v0.1 — additive, research-only.
-- Completely separate from alerts, paper_trades, daily_risk,
-- signal_observations, and signal_features. Never consumed by the
-- existing signal/alert path, cooldown logic, or Pushover.
-- contract_version documents the shape of evidence_json/warnings_json/
-- measurements_json for a given row (see src/apex/opportunity/contract.py).
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS opportunity_observations (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_uid          TEXT NOT NULL UNIQUE,
    fingerprint              TEXT NOT NULL,          -- dedupe identity; NOT unique (see engine.py)
    symbol                   TEXT NOT NULL,
    direction                TEXT NOT NULL CHECK(direction IN ('LONG','SHORT')),
    setup_family             TEXT NOT NULL CHECK(setup_family IN ('VOLATILITY_COMPRESSION','SWEEP_RECLAIM')),
    detector_version         TEXT NOT NULL,
    contract_version         TEXT NOT NULL,
    primary_timeframe        TEXT NOT NULL CHECK(primary_timeframe IN ('3m','5m')),
    status                   TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','EXPIRED')),
    research_only            INTEGER NOT NULL DEFAULT 1,
    first_detected_at        TEXT NOT NULL,           -- ISO8601 UTC
    last_seen_at             TEXT NOT NULL,           -- ISO8601 UTC
    occurrence_count         INTEGER NOT NULL DEFAULT 1,
    source_candle_open_time  INTEGER NOT NULL,        -- Unix ms
    source_candle_close_time INTEGER NOT NULL,        -- Unix ms
    anchor_price             REAL,
    anchor_open_time         INTEGER,                 -- Unix ms
    evidence_json            TEXT,                    -- supporting evidence
    warnings_json            TEXT,                    -- conflicting evidence / warnings
    measurements_json        TEXT,                    -- raw detector measurements
    closed_at                TEXT,                    -- ISO8601 UTC when EXPIRED
    created_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_opportunity_observations_fingerprint
    ON opportunity_observations(fingerprint, status);
CREATE INDEX IF NOT EXISTS idx_opportunity_observations_lookup
    ON opportunity_observations(symbol, setup_family, primary_timeframe, status);
CREATE INDEX IF NOT EXISTS idx_opportunity_observations_uid
    ON opportunity_observations(opportunity_uid);
