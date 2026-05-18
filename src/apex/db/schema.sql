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
