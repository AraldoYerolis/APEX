"""Database repository — all SQL operations live here."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from apex.db.models import Alert, DailyRisk, Market, PaperTrade, SignalFeature, SignalObservation, Snooze
from apex.opportunity.contract import Opportunity
from apex.opportunity.scoring import SCORE_VERSION
from apex.opportunity.trade_plan import TradePlan
from apex.opportunity.trade_plan_outcome import TERMINAL_OUTCOME_STATES, TradePlanOutcomeEvaluation


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


def record_tp1_milestone(
    conn: sqlite3.Connection,
    uid: str,
    *,
    hit_at: str,
    time_seconds: float,
    mfe: Optional[float] = None,
    mae: Optional[float] = None,
) -> None:
    """Record that TP1 was reached without closing the observation.

    The observation remains OBSERVED so it continues to be evaluated for
    TP2, stop, or expiry. Only called when hit_1r_at is currently NULL.
    MFE/MAE are updated alongside the milestone timestamp.
    """
    conn.execute(
        """
        UPDATE signal_observations
        SET hit_1r_at=?, time_to_1r_seconds=?,
            max_favorable_excursion=?, max_adverse_excursion=?,
            updated_at=?
        WHERE observation_uid=?
        """,
        (hit_at, time_seconds, mfe, mae, _now_utc(), uid),
    )
    conn.commit()


def close_signal_observation(
    conn: sqlite3.Connection,
    uid: str,
    *,
    status: str,
    outcome_r: Optional[float] = None,
    closed_at: Optional[str] = None,
    mfe: Optional[float] = None,
    mae: Optional[float] = None,
    # Milestone 10A additions — all optional for backward compatibility
    hit_1r_at: Optional[str] = None,
    hit_2r_at: Optional[str] = None,
    stopped_at: Optional[str] = None,
    expired_at: Optional[str] = None,
    first_terminal_status: Optional[str] = None,
    final_status: Optional[str] = None,
    time_to_1r_seconds: Optional[float] = None,
    time_to_2r_seconds: Optional[float] = None,
    time_to_stop_seconds: Optional[float] = None,
    time_to_expiry_seconds: Optional[float] = None,
    hit_1r_before_stop: Optional[int] = None,
    hit_1r_before_expiry: Optional[int] = None,
) -> None:
    """Mark an observation as closed with a final outcome.

    hit_1r_at and time_to_1r_seconds use COALESCE so that a value set
    earlier by record_tp1_milestone is preserved when TP2 or stop closes
    the observation later. All Milestone 10A params default to None for
    backward compatibility with call sites that don't supply them.
    """
    conn.execute(
        """
        UPDATE signal_observations
        SET status=?, outcome_r=?, closed_at=?,
            max_favorable_excursion=?, max_adverse_excursion=?,
            hit_1r_at=COALESCE(hit_1r_at, ?),
            hit_2r_at=?, stopped_at=?, expired_at=?,
            first_terminal_status=?, final_status=?,
            time_to_1r_seconds=COALESCE(time_to_1r_seconds, ?),
            time_to_2r_seconds=?, time_to_stop_seconds=?,
            time_to_expiry_seconds=?,
            hit_1r_before_stop=?, hit_1r_before_expiry=?,
            updated_at=?
        WHERE observation_uid=?
        """,
        (
            status,
            outcome_r,
            closed_at or _now_utc(),
            mfe,
            mae,
            hit_1r_at,          # COALESCE: preserve existing value from record_tp1_milestone
            hit_2r_at,
            stopped_at,
            expired_at,
            first_terminal_status,
            final_status,
            time_to_1r_seconds,  # COALESCE: preserve existing value
            time_to_2r_seconds,
            time_to_stop_seconds,
            time_to_expiry_seconds,
            hit_1r_before_stop,
            hit_1r_before_expiry,
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


# ------------------------------------------------------------------ signal_features

def insert_signal_feature(conn: sqlite3.Connection, feature: SignalFeature) -> None:
    """Insert a signal feature row. INSERT OR IGNORE — safe to call twice for same obs uid."""
    conn.execute(
        """
        INSERT OR IGNORE INTO signal_features (
            observation_uid, captured_at, symbol, direction, signal_type, observed_at,
            entry_price, stop_price, target_1r, target_2r,
            candle_open, candle_high, candle_low, candle_close, candle_volume, candle_open_time,
            rsi_val, atr_val, vwap_val, ema_fast, ema_slow,
            price_vs_vwap_pct, ema_spread_pct, atr_pct,
            trend_bias, trend_reason, pullback_state, pullback_reason,
            btc_trend_bias, eth_trend_bias, market_regime_label,
            relative_strength_rank, relative_strength_score,
            feature_version, metadata_json
        ) VALUES (
            ?,?,?,?,?,?,
            ?,?,?,?,
            ?,?,?,?,?,?,
            ?,?,?,?,?,
            ?,?,?,
            ?,?,?,?,
            ?,?,?,
            ?,?,
            ?,?
        )
        """,
        (
            feature.observation_uid, feature.captured_at, feature.symbol,
            feature.direction, feature.signal_type, feature.observed_at,
            feature.entry_price, feature.stop_price, feature.target_1r, feature.target_2r,
            feature.candle_open, feature.candle_high, feature.candle_low,
            feature.candle_close, feature.candle_volume, feature.candle_open_time,
            feature.rsi_val, feature.atr_val, feature.vwap_val,
            feature.ema_fast, feature.ema_slow,
            feature.price_vs_vwap_pct, feature.ema_spread_pct, feature.atr_pct,
            feature.trend_bias, feature.trend_reason,
            feature.pullback_state, feature.pullback_reason,
            feature.btc_trend_bias, feature.eth_trend_bias, feature.market_regime_label,
            feature.relative_strength_rank, feature.relative_strength_score,
            feature.feature_version, feature.metadata_json,
        ),
    )
    conn.commit()


def get_signal_features(
    conn: sqlite3.Connection,
    since: Optional[str] = None,
    signal_type: Optional[str] = None,
    limit: int = 200,
) -> list[sqlite3.Row]:
    """Return signal feature rows, newest first."""
    conditions = []
    params: list = []
    if since:
        conditions.append("captured_at >= ?")
        params.append(since)
    if signal_type:
        conditions.append("signal_type = ?")
        params.append(signal_type)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    return conn.execute(
        f"SELECT * FROM signal_features {where} ORDER BY captured_at DESC LIMIT ?",
        params,
    ).fetchall()


def update_signal_feature_outcome_from_observation(
    conn: sqlite3.Connection,
    uid: str,
) -> None:
    """Sync outcome fields from signal_observations into signal_features.

    Called after every evaluation pass that changes an observation's state.
    Safe to call when no matching signal_features row exists (no-op in that case).
    Never modifies the signal_observations row.
    """
    row = conn.execute(
        "SELECT * FROM signal_observations WHERE observation_uid=?", (uid,)
    ).fetchone()
    if row is None:
        return
    outcome_status = row["final_status"] or row["status"]
    conn.execute(
        """
        UPDATE signal_features
        SET outcome_status=?, outcome_r=?,
            hit_1r_at=?, hit_2r_at=?, stopped_at=?, expired_at=?,
            time_to_1r_seconds=?, time_to_2r_seconds=?,
            time_to_stop_seconds=?, time_to_expiry_seconds=?,
            hit_1r_before_stop=?, hit_1r_before_expiry=?,
            updated_at=?
        WHERE observation_uid=?
        """,
        (
            outcome_status, row["outcome_r"],
            row["hit_1r_at"], row["hit_2r_at"], row["stopped_at"], row["expired_at"],
            row["time_to_1r_seconds"], row["time_to_2r_seconds"],
            row["time_to_stop_seconds"], row["time_to_expiry_seconds"],
            row["hit_1r_before_stop"], row["hit_1r_before_expiry"],
            _now_utc(),
            uid,
        ),
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


# ------------------------------------------------------------------ opportunity_observations
# TA Opportunity Engine v0.1 — additive, research-only. See
# src/apex/opportunity/engine.py for the only caller of these functions.

def insert_opportunity(conn: sqlite3.Connection, opportunity: Opportunity) -> int:
    cur = conn.execute(
        """
        INSERT INTO opportunity_observations (
            opportunity_uid, fingerprint, symbol, direction, setup_family,
            detector_version, contract_version, primary_timeframe, status,
            research_only, first_detected_at, last_seen_at, occurrence_count,
            source_candle_open_time, source_candle_close_time,
            anchor_price, anchor_open_time,
            evidence_json, warnings_json, measurements_json,
            context_json, component_scores_json, total_score,
            score_version, score_warnings_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            opportunity.opportunity_uid,
            opportunity.fingerprint,
            opportunity.symbol,
            opportunity.direction,
            opportunity.setup_family,
            opportunity.detector_version,
            opportunity.contract_version,
            opportunity.primary_timeframe,
            opportunity.status,
            int(opportunity.research_only),
            opportunity.first_detected_at,
            opportunity.last_seen_at,
            opportunity.occurrence_count,
            opportunity.source_candle_open_time,
            opportunity.source_candle_close_time,
            opportunity.anchor_price,
            opportunity.anchor_open_time,
            opportunity.evidence_json,
            opportunity.warnings_json,
            opportunity.measurements_json,
            # Context and ranking v0.1 — immutable first-detection score/
            # context snapshot (see contract.py's Opportunity docstring and
            # engine.py's _record_finding). touch_opportunity() below never
            # writes these five columns, so this INSERT is their only
            # writer for a row's entire lifetime.
            opportunity.context_json,
            opportunity.component_scores_json,
            opportunity.total_score,
            opportunity.score_version,
            opportunity.score_warnings_json,
        ),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def get_active_opportunity_by_fingerprint(
    conn: sqlite3.Connection, fingerprint: str
) -> Optional[sqlite3.Row]:
    """Return the current ACTIVE row for this fingerprint, if any.

    Deliberately does not consider EXPIRED rows a match — see engine.py's
    fingerprint/dedupe semantics docstring for why fingerprint is not a
    DB-level unique constraint.
    """
    return conn.execute(
        """
        SELECT * FROM opportunity_observations
        WHERE fingerprint=? AND status='ACTIVE'
        ORDER BY last_seen_at DESC LIMIT 1
        """,
        (fingerprint,),
    ).fetchone()


def touch_opportunity(
    conn: sqlite3.Connection,
    opportunity_uid: str,
    *,
    last_seen_at: str,
    occurrence_count: int,
    measurements_json: Optional[str],
) -> None:
    """Refresh an existing ACTIVE opportunity on re-detection (dedupe hit)."""
    conn.execute(
        """
        UPDATE opportunity_observations
        SET last_seen_at=?, occurrence_count=?, measurements_json=?, updated_at=?
        WHERE opportunity_uid=?
        """,
        (last_seen_at, occurrence_count, measurements_json, _now_utc(), opportunity_uid),
    )
    conn.commit()


def expire_stale_opportunities(
    conn: sqlite3.Connection, primary_timeframe: str, cutoff_iso: str
) -> int:
    """Mark ACTIVE opportunities on this timeframe EXPIRED if not re-confirmed since cutoff_iso.

    Returns the number of rows expired.
    """
    now = _now_utc()
    cur = conn.execute(
        """
        UPDATE opportunity_observations
        SET status='EXPIRED', closed_at=?, updated_at=?
        WHERE status='ACTIVE' AND primary_timeframe=? AND last_seen_at<?
        """,
        (now, now, primary_timeframe, cutoff_iso),
    )
    conn.commit()
    return cur.rowcount


def get_opportunities(
    conn: sqlite3.Connection,
    *,
    since: Optional[str] = None,
    setup_family: Optional[str] = None,
    symbol: Optional[str] = None,
    direction: Optional[str] = None,
    primary_timeframe: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 500,
) -> list[sqlite3.Row]:
    """Read-only query for reporting. Never used by the engine itself."""
    conditions = []
    params: list = []
    if since:
        conditions.append("last_seen_at >= ?")
        params.append(since)
    if setup_family:
        conditions.append("setup_family = ?")
        params.append(setup_family)
    if symbol:
        conditions.append("symbol = ?")
        params.append(symbol.upper())
    if direction:
        conditions.append("direction = ?")
        params.append(direction)
    if primary_timeframe:
        conditions.append("primary_timeframe = ?")
        params.append(primary_timeframe)
    if status:
        conditions.append("status = ?")
        params.append(status)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    return conn.execute(
        f"SELECT * FROM opportunity_observations {where} ORDER BY last_seen_at DESC LIMIT ?",
        params,
    ).fetchall()


def get_ranked_opportunities(
    conn: sqlite3.Connection,
    *,
    since: Optional[str] = None,
    setup_family: Optional[str] = None,
    symbol: Optional[str] = None,
    direction: Optional[str] = None,
    primary_timeframe: Optional[str] = None,
    status: Optional[str] = "ACTIVE",
    limit: int = 500,
) -> list[sqlite3.Row]:
    """Read-only ranked query for reporting (Context and ranking v0.1) —
    never used by the engine itself, and never writes anything.

    Ranking is applied over the FULL filtered result set BEFORE `LIMIT` is
    applied (a single ORDER BY ... LIMIT statement, not a Python sort of an
    already-limited batch), within whatever `since`/`setup_family`/
    `symbol`/`direction`/`primary_timeframe`/`status` filters are supplied —
    it never reaches across those filters.

    A row ranks in the "scored" group only if its `score_version` matches
    the CURRENT `apex.opportunity.scoring.SCORE_VERSION` AND its
    `total_score` is a finite, in-range [0, 100] number; every other row
    (NULL score, an older/unknown score_version, or a malformed/
    out-of-range stored value) is explicitly unscored/incompatible and
    always sorts after every scored row, regardless of its numeric value.
    Deterministic tie-break within each group: total_score DESC,
    last_seen_at DESC, symbol ASC, primary_timeframe ASC, setup_family ASC,
    direction ASC, opportunity_uid ASC — never incidental DB row order.

    `status` defaults to 'ACTIVE' (an explicit, historical EXPIRED view is
    a separate, deliberate call with `status="EXPIRED"`; the two are never
    mixed by default). Pass `status=None` to explicitly disable the status
    filter and rank across every status.
    """
    conditions = []
    params: list = []
    if since:
        conditions.append("last_seen_at >= ?")
        params.append(since)
    if setup_family:
        conditions.append("setup_family = ?")
        params.append(setup_family)
    if symbol:
        conditions.append("symbol = ?")
        params.append(symbol.upper())
    if direction:
        conditions.append("direction = ?")
        params.append(direction)
    if primary_timeframe:
        conditions.append("primary_timeframe = ?")
        params.append(primary_timeframe)
    if status:
        conditions.append("status = ?")
        params.append(status)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    query_params = [SCORE_VERSION, *params, limit]
    return conn.execute(
        f"""
        SELECT *,
            CASE
                WHEN score_version = ?
                     AND total_score IS NOT NULL
                     AND typeof(total_score) IN ('integer', 'real')
                     AND total_score >= 0 AND total_score <= 100
                THEN 0 ELSE 1
            END AS rank_group
        FROM opportunity_observations
        {where}
        ORDER BY
            rank_group ASC,
            total_score DESC,
            last_seen_at DESC,
            symbol ASC,
            primary_timeframe ASC,
            setup_family ASC,
            direction ASC,
            opportunity_uid ASC
        LIMIT ?
        """,
        query_params,
    ).fetchall()


# ------------------------------------------------------------------ opportunity_trade_plans /
# opportunity_trade_plan_outcomes
# Trade plans and outcome evidence v0.1 — additive, research-only. See
# src/apex/opportunity/trade_plan.py / trade_plan_outcome.py for the pure
# contracts these functions persist. Only opportunity/engine.py (plan
# creation) and scheduler/tasks.py (outcome evaluation) call these; neither
# ever writes to opportunity_observations itself.

def insert_trade_plan_with_outcome(
    conn: sqlite3.Connection,
    plan: TradePlan,
    outcome: TradePlanOutcomeEvaluation,
) -> None:
    """Atomically insert the one immutable trade plan row and its initial
    one-to-one outcome row for a brand-new opportunity. Never called for a
    reconfirmation, and opportunity_trade_plans is never UPDATEd by any
    function in this module — this INSERT is its only writer.

    If either INSERT or the final commit fails, the transaction is
    explicitly rolled back before the exception is re-raised, so a failed
    pair never leaves a half-written row pending in the connection's
    transaction state for an unrelated later commit() to accidentally
    persist.
    """
    try:
        _insert_trade_plan_with_outcome_unguarded(conn, plan, outcome)
    except Exception:
        conn.rollback()
        raise


def _insert_trade_plan_with_outcome_unguarded(
    conn: sqlite3.Connection,
    plan: TradePlan,
    outcome: TradePlanOutcomeEvaluation,
) -> None:
    conn.execute(
        """
        INSERT INTO opportunity_trade_plans (
            plan_uid, opportunity_uid, symbol, direction, setup_family,
            primary_timeframe, detector_version, opportunity_contract_version,
            plan_contract_version, source_candle_open_time, source_candle_close_time,
            created_at, availability, unavailable_reason, entry_type, entry_price,
            invalidation_price, stop_price, risk_distance,
            target_1r_price, target_2r_price, target_1r_multiple, target_2r_multiple,
            reward_risk_1r, reward_risk_2r,
            evaluation_not_before_ms, evaluation_expiry_ms,
            provenance_json, warnings_json, research_only
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            plan.plan_uid,
            plan.opportunity_uid,
            plan.symbol,
            plan.direction,
            plan.setup_family,
            plan.primary_timeframe,
            plan.detector_version,
            plan.opportunity_contract_version,
            plan.plan_contract_version,
            plan.source_candle_open_time,
            plan.source_candle_close_time,
            plan.created_at,
            plan.availability,
            plan.unavailable_reason,
            plan.entry_type,
            plan.entry_price,
            plan.invalidation_price,
            plan.stop_price,
            plan.risk_distance,
            plan.target_1r_price,
            plan.target_2r_price,
            plan.target_1r_multiple,
            plan.target_2r_multiple,
            plan.reward_risk_1r,
            plan.reward_risk_2r,
            plan.evaluation_not_before_ms,
            plan.evaluation_expiry_ms,
            plan.provenance_json,
            plan.warnings_json,
            int(plan.research_only),
        ),
    )
    conn.execute(
        """
        INSERT INTO opportunity_trade_plan_outcomes (
            plan_uid, opportunity_uid, contract_version, state,
            last_evaluated_ms, last_evaluated_open_time,
            entry_open_time, hit_1r_open_time, terminal_open_time, terminal_reason,
            mfe_r, mae_r, data_quality, first_missing_boundary_ms,
            is_ambiguous, decisive_ohlc_json, crossed_levels_json, evidence_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            outcome.plan_uid,
            outcome.opportunity_uid,
            outcome.contract_version,
            outcome.state,
            outcome.last_evaluated_ms,
            outcome.last_evaluated_open_time,
            outcome.entry_open_time,
            outcome.hit_1r_open_time,
            outcome.terminal_open_time,
            outcome.terminal_reason,
            outcome.mfe_r,
            outcome.mae_r,
            outcome.data_quality,
            outcome.first_missing_boundary_ms,
            int(outcome.is_ambiguous),
            json.dumps(outcome.decisive_ohlc, sort_keys=True, allow_nan=False)
            if outcome.decisive_ohlc is not None
            else None,
            json.dumps(list(outcome.crossed_levels), sort_keys=True, allow_nan=False),
            outcome.evidence_json,
        ),
    )
    conn.commit()


def get_trade_plan_by_opportunity_uid(
    conn: sqlite3.Connection, opportunity_uid: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM opportunity_trade_plans WHERE opportunity_uid=?",
        (opportunity_uid,),
    ).fetchone()


def get_nonterminal_trade_plan_outcomes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every plan+outcome pair still in a nonterminal state, joined for the
    scheduler's evaluation pass (see scheduler/tasks.py's
    run_trade_plan_outcome_evaluation). Read-only; never writes anything.
    """
    placeholders = ",".join("?" for _ in TERMINAL_OUTCOME_STATES)
    return conn.execute(
        f"""
        SELECT p.*, o.state AS outcome_state
        FROM opportunity_trade_plan_outcomes o
        JOIN opportunity_trade_plans p ON p.plan_uid = o.plan_uid
        WHERE o.state NOT IN ({placeholders})
        ORDER BY p.created_at ASC
        """,
        tuple(TERMINAL_OUTCOME_STATES),
    ).fetchall()


def update_trade_plan_outcome(
    conn: sqlite3.Connection, evaluation: TradePlanOutcomeEvaluation
) -> bool:
    """Apply one evaluation pass's result to the matching outcome row.

    Guarded so only a row currently in a NONTERMINAL state is ever touched
    (`WHERE state NOT IN (...)`) — a row that has already become terminal
    since it was read is left untouched rather than resurrected. Milestone
    timestamps (`entry_open_time`, `hit_1r_open_time`) use COALESCE so a
    value recorded by an earlier pass can never be cleared/overwritten by a
    later one. `mfe_r`/`mae_r` are monotonic non-decreasing: the already-
    recorded value is kept whenever this pass reports a lower or NULL value
    (e.g. a later replay against a shorter/gapped frame), while the first
    non-NULL value is still recorded and a genuinely higher value still
    updates. Returns True if a row was actually updated.
    """
    placeholders = ",".join("?" for _ in TERMINAL_OUTCOME_STATES)
    decisive_json = (
        json.dumps(evaluation.decisive_ohlc, sort_keys=True, allow_nan=False)
        if evaluation.decisive_ohlc is not None
        else None
    )
    crossed_json = json.dumps(list(evaluation.crossed_levels), sort_keys=True, allow_nan=False)
    cur = conn.execute(
        f"""
        UPDATE opportunity_trade_plan_outcomes
        SET state=?, last_evaluated_ms=?, last_evaluated_open_time=?,
            entry_open_time=COALESCE(entry_open_time, ?),
            hit_1r_open_time=COALESCE(hit_1r_open_time, ?),
            terminal_open_time=?, terminal_reason=?,
            mfe_r=CASE
                WHEN mfe_r IS NULL THEN ?
                WHEN ? IS NULL THEN mfe_r
                ELSE MAX(mfe_r, ?)
            END,
            mae_r=CASE
                WHEN mae_r IS NULL THEN ?
                WHEN ? IS NULL THEN mae_r
                ELSE MAX(mae_r, ?)
            END,
            data_quality=?, first_missing_boundary_ms=?,
            is_ambiguous=?, decisive_ohlc_json=?, crossed_levels_json=?,
            evidence_json=?, updated_at=?
        WHERE plan_uid=? AND state NOT IN ({placeholders})
        """,
        (
            evaluation.state,
            evaluation.last_evaluated_ms,
            evaluation.last_evaluated_open_time,
            evaluation.entry_open_time,
            evaluation.hit_1r_open_time,
            evaluation.terminal_open_time,
            evaluation.terminal_reason,
            evaluation.mfe_r,
            evaluation.mfe_r,
            evaluation.mfe_r,
            evaluation.mae_r,
            evaluation.mae_r,
            evaluation.mae_r,
            evaluation.data_quality,
            evaluation.first_missing_boundary_ms,
            int(evaluation.is_ambiguous),
            decisive_json,
            crossed_json,
            evaluation.evidence_json,
            _now_utc(),
            evaluation.plan_uid,
            *TERMINAL_OUTCOME_STATES,
        ),
    )
    conn.commit()
    return cur.rowcount > 0
