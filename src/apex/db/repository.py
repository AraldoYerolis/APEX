"""Database repository — all SQL operations live here."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from apex.db.models import Alert, DailyRisk, Market, PaperTrade, SignalObservation, Snooze


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ------------------------------------------------------------------ markets

def upsert_market(conn: sqlite3.Connection, market: Market) -> None:
    conn.execute(
        """
        INSERT INTO markets (symbol, is_active, is_priority, scan_enabled,
            last_24h_volume_usd, last_open_interest_usd, last_mid_price, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
            is_active=excluded.is_active,
            is_priority=excluded.is_priority,
            scan_enabled=excluded.scan_enabled,
            last_24h_volume_usd=excluded.last_24h_volume_usd,
            last_open_interest_usd=excluded.last_open_interest_usd,
            last_mid_price=excluded.last_mid_price,
            updated_at=excluded.updated_at
        """,
        (
            market.symbol,
            int(market.is_active),
            int(market.is_priority),
            int(market.scan_enabled),
            market.last_24h_volume_usd,
            market.last_open_interest_usd,
            market.last_mid_price,
            _now_utc(),
        ),
    )
    conn.commit()


def get_scan_enabled_markets(conn: sqlite3.Connection) -> list[Market]:
    rows = conn.execute(
        "SELECT * FROM markets WHERE scan_enabled=1 AND is_active=1 ORDER BY last_24h_volume_usd DESC NULLS LAST"
    ).fetchall()
    return [_row_to_market(r) for r in rows]


def _row_to_market(row: sqlite3.Row) -> Market:
    return Market(
        id=row["id"],
        symbol=row["symbol"],
        is_active=bool(row["is_active"]),
        is_priority=bool(row["is_priority"]),
        scan_enabled=bool(row["scan_enabled"]),
        last_24h_volume_usd=row["last_24h_volume_usd"],
        last_open_interest_usd=row["last_open_interest_usd"],
        last_mid_price=row["last_mid_price"],
    )


# ------------------------------------------------------------------ candles

def upsert_candle(conn: sqlite3.Connection, candle) -> None:
    conn.execute(
        """
        INSERT INTO candles (symbol, timeframe, open_time, close_time,
            open, high, low, close, volume, is_closed, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, timeframe, open_time) DO UPDATE SET
            close_time=excluded.close_time,
            open=excluded.open,
            high=excluded.high,
            low=excluded.low,
            close=excluded.close,
            volume=excluded.volume,
            is_closed=excluded.is_closed,
            updated_at=excluded.updated_at
        """,
        (
            candle.symbol,
            candle.timeframe,
            candle.open_time,
            candle.close_time,
            candle.open,
            candle.high,
            candle.low,
            candle.close,
            candle.volume,
            int(candle.is_closed),
            _now_utc(),
        ),
    )
    conn.commit()


def get_candles(
    conn: sqlite3.Connection,
    symbol: str,
    timeframe: str,
    limit: int = 200,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM candles
        WHERE symbol=? AND timeframe=? AND is_closed=1
        ORDER BY open_time DESC LIMIT ?
        """,
        (symbol, timeframe, limit),
    ).fetchall()


# ------------------------------------------------------------------ alerts

def insert_alert(conn: sqlite3.Connection, alert: Alert) -> int:
    cur = conn.execute(
        """
        INSERT INTO alerts (
            alert_uid, symbol, direction, alert_type, setup_type, status,
            entry_low, entry_high, reference_price, stop_price,
            target_1r, target_2r, invalidation_price,
            risk_usd, suggested_notional_usd, stop_distance_pct,
            confidence_score, message, expires_at, sent_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            alert.alert_uid,
            alert.symbol,
            alert.direction,
            alert.alert_type,
            alert.setup_type,
            alert.status,
            alert.entry_low,
            alert.entry_high,
            alert.reference_price,
            alert.stop_price,
            alert.target_1r,
            alert.target_2r,
            alert.invalidation_price,
            alert.risk_usd,
            alert.suggested_notional_usd,
            alert.stop_distance_pct,
            alert.confidence_score,
            alert.message,
            alert.expires_at,
            alert.sent_at,
        ),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def get_alert_by_uid(conn: sqlite3.Connection, uid: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM alerts WHERE alert_uid=?", (uid,)).fetchone()


def update_alert_status(conn: sqlite3.Connection, uid: str, status: str) -> None:
    conn.execute(
        "UPDATE alerts SET status=?, updated_at=? WHERE alert_uid=?",
        (status, _now_utc(), uid),
    )
    conn.commit()


def get_recent_alerts(
    conn: sqlite3.Connection,
    symbol: str,
    alert_type: str,
    since_iso: str,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM alerts
        WHERE symbol=? AND alert_type=? AND sent_at>=?
        ORDER BY sent_at DESC
        """,
        (symbol, alert_type, since_iso),
    ).fetchall()


def count_confirmed_alerts_since(conn: sqlite3.Connection, since_iso: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM alerts WHERE alert_type='CONFIRMED_SETUP' AND sent_at>=?",
        (since_iso,),
    ).fetchone()
    return int(row["cnt"]) if row else 0


# ------------------------------------------------------------------ paper trades

def insert_paper_trade(conn: sqlite3.Connection, trade: PaperTrade) -> int:
    cur = conn.execute(
        """
        INSERT INTO paper_trades (
            trade_uid, alert_id, symbol, direction, setup_type,
            entry_price, stop_price, target_1r, target_2r,
            risk_usd, suggested_notional_usd, status, opened_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            trade.trade_uid,
            trade.alert_id,
            trade.symbol,
            trade.direction,
            trade.setup_type,
            trade.entry_price,
            trade.stop_price,
            trade.target_1r,
            trade.target_2r,
            trade.risk_usd,
            trade.suggested_notional_usd,
            trade.status,
            trade.opened_at,
        ),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def get_trade_by_uid(conn: sqlite3.Connection, uid: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM paper_trades WHERE trade_uid=?", (uid,)).fetchone()


def update_trade_status(
    conn: sqlite3.Connection,
    uid: str,
    status: str,
    outcome_at: Optional[str] = None,
) -> None:
    conn.execute(
        "UPDATE paper_trades SET status=?, outcome_at=?, updated_at=? WHERE trade_uid=?",
        (status, outcome_at or _now_utc(), _now_utc(), uid),
    )
    conn.commit()


def get_open_trades_needing_followup(
    conn: sqlite3.Connection,
    before_iso: str,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM paper_trades
        WHERE status='OPEN' AND followup_sent_at IS NULL AND opened_at<=?
        """,
        (before_iso,),
    ).fetchall()


def mark_followup_sent(conn: sqlite3.Connection, uid: str) -> None:
    conn.execute(
        "UPDATE paper_trades SET followup_sent_at=?, updated_at=? WHERE trade_uid=?",
        (_now_utc(), _now_utc(), uid),
    )
    conn.commit()


# ------------------------------------------------------------------ daily_risk

def get_or_create_daily_risk(
    conn: sqlite3.Connection,
    max_loss_usd: float,
) -> sqlite3.Row:
    today = _today_utc()
    row = conn.execute(
        "SELECT * FROM daily_risk WHERE trade_date=?", (today,)
    ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO daily_risk (trade_date, planned_loss_used_usd, max_planned_loss_usd)
            VALUES (?,?,?)
            """,
            (today, 0.0, max_loss_usd),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM daily_risk WHERE trade_date=?", (today,)
        ).fetchone()
    return row  # type: ignore[return-value]


def add_daily_loss(
    conn: sqlite3.Connection,
    amount_usd: float,
    max_loss_usd: float,
) -> sqlite3.Row:
    row = get_or_create_daily_risk(conn, max_loss_usd)
    new_total = row["planned_loss_used_usd"] + amount_usd
    lockout = int(new_total >= max_loss_usd)
    conn.execute(
        """
        UPDATE daily_risk
        SET planned_loss_used_usd=?, lockout_active=?, updated_at=?
        WHERE trade_date=?
        """,
        (new_total, lockout, _now_utc(), row["trade_date"]),
    )
    conn.commit()
    return conn.execute(
        "SELECT * FROM daily_risk WHERE trade_date=?", (row["trade_date"],)
    ).fetchone()


def is_daily_lockout_active(conn: sqlite3.Connection, max_loss_usd: float) -> bool:
    row = get_or_create_daily_risk(conn, max_loss_usd)
    return bool(row["lockout_active"])


# ------------------------------------------------------------------ snoozes

def add_snooze(conn: sqlite3.Connection, symbol: str, until_iso: str, reason: str = "") -> None:
    conn.execute(
        "INSERT INTO snoozes (symbol, snoozed_until, reason) VALUES (?,?,?)",
        (symbol, until_iso, reason),
    )
    conn.commit()


def is_snoozed(conn: sqlite3.Connection, symbol: str) -> bool:
    now = _now_utc()
    row = conn.execute(
        "SELECT id FROM snoozes WHERE symbol=? AND snoozed_until>? LIMIT 1",
        (symbol, now),
    ).fetchone()
    return row is not None


# ------------------------------------------------------------------ signal_observations

def insert_signal_observation(conn: sqlite3.Connection, obs: SignalObservation) -> int:
    cur = conn.execute(
        """
        INSERT INTO signal_observations (
            observation_uid, observed_at, symbol, direction, signal_type,
            entry_price, stop_price, target_1r, target_2r,
            expires_at, metadata_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            obs.observation_uid,
            obs.observed_at,
            obs.symbol,
            obs.direction,
            obs.signal_type,
            obs.entry_price,
            obs.stop_price,
            obs.target_1r,
            obs.target_2r,
            obs.expires_at,
            obs.metadata_json,
        ),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def get_open_signal_observation(
    conn: sqlite3.Connection,
    symbol: str,
    direction: str,
    signal_type: str,
) -> Optional[sqlite3.Row]:
    """Return the open (OBSERVED, not yet expired) row for this symbol/direction/type.

    Used for deduplication: if a row exists, the caller should touch it rather
    than inserting a duplicate. An observation is considered still-open when
    status='OBSERVED' AND expires_at > now — even if we haven't formally run
    the evaluation job yet.
    """
    now = _now_utc()
    return conn.execute(
        """
        SELECT * FROM signal_observations
        WHERE symbol=? AND direction=? AND signal_type=?
          AND status='OBSERVED' AND expires_at>?
        ORDER BY observed_at DESC LIMIT 1
        """,
        (symbol, direction, signal_type, now),
    ).fetchone()


def touch_signal_observation(conn: sqlite3.Connection, uid: str) -> None:
    """Refresh updated_at on an existing open observation (dedupe hit)."""
    conn.execute(
        "UPDATE signal_observations SET updated_at=? WHERE observation_uid=?",
        (_now_utc(), uid),
    )
    conn.commit()


def get_open_signal_observations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all OBSERVED rows for evaluation."""
    return conn.execute(
        "SELECT * FROM signal_observations WHERE status='OBSERVED' ORDER BY observed_at ASC"
    ).fetchall()


def close_signal_observation(
    conn: sqlite3.Connection,
    uid: str,
    *,
    status: str,
    outcome_r: Optional[float] = None,
    closed_at: Optional[str] = None,
    mfe: Optional[float] = None,
    mae: Optional[float] = None,
) -> None:
    """Mark an observation as closed with a final outcome."""
    conn.execute(
        """
        UPDATE signal_observations
        SET status=?, outcome_r=?, closed_at=?,
            max_favorable_excursion=?, max_adverse_excursion=?,
            updated_at=?
        WHERE observation_uid=?
        """,
        (
            status,
            outcome_r,
            closed_at or _now_utc(),
            mfe,
            mae,
            _now_utc(),
            uid,
        ),
    )
    conn.commit()


def update_observation_excursions(
    conn: sqlite3.Connection,
    uid: str,
    *,
    mfe: float,
    mae: float,
) -> None:
    """Update running MFE/MAE on an observation that remains open."""
    conn.execute(
        """
        UPDATE signal_observations
        SET max_favorable_excursion=?, max_adverse_excursion=?, updated_at=?
        WHERE observation_uid=?
        """,
        (mfe, mae, _now_utc(), uid),
    )
    conn.commit()


# ------------------------------------------------------------------ app_events

def log_event(
    conn: sqlite3.Connection,
    event_type: str,
    message: str,
    level: str = "INFO",
    metadata: Optional[dict] = None,
) -> None:
    conn.execute(
        "INSERT INTO app_events (level, event_type, message, metadata_json) VALUES (?,?,?,?)",
        (level, event_type, message, json.dumps(metadata) if metadata else None),
    )
    conn.commit()
