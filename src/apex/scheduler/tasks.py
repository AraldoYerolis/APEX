"""Scheduled background tasks."""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime as _dt, timezone as _tz
from typing import Literal

from apex.actions.tokens import generate_token
from apex.config import Settings
from apex.data.candle_store import CandleStore
from apex.data.hyperliquid_client import HyperliquidClient
from apex.data.market_universe import refresh_universe
from apex.db import repository as repo
from apex.db.models import Alert, SignalObservation
from apex.notifications.pushover_client import PushoverClient
from apex.notifications.templates import (
    format_confirmed_alert,
    format_forming_alert,
    format_followup_message,
)
from apex.strategy.signal_engine import SignalCandidate, evaluate_symbol
from apex.utils.ids import new_uid
from apex.utils.time import minutes_from_now, utcnow_iso, minutes_ago_iso

logger = logging.getLogger(__name__)

# Outcome codes returned by _send_alert.
# Used by run_signal_scan to build an accurate per-scan breakdown.
AlertOutcome = Literal[
    "SENT",               # Pushover notification actually delivered
    "SEND_FAILED",        # alerts_enabled=True but Pushover call failed
    "SUPPRESSED_TYPE",    # alert_type not in ALERT_TYPES_ENABLED
    "SUPPRESSED_DISABLED",# alerts_enabled=False (dry-run would-be)
]


@dataclass
class ScanSummary:
    markets_scanned: int = 0
    no_data: int = 0
    stale: int = 0
    no_signal: int = 0
    engine_suppressed: int = 0   # throttled / snoozed / lockout (signal_engine)
    suppressed_type: int = 0     # ALERT_TYPES_ENABLED gate
    suppressed_disabled: int = 0 # ALERTS_ENABLED=false gate
    sent: int = 0
    send_failed: int = 0

    @property
    def candidates(self) -> int:
        """Signals that passed signal_engine but may have been gated later."""
        return self.suppressed_type + self.suppressed_disabled + self.sent + self.send_failed

    def log(self, logger: logging.Logger, dry_run: bool) -> None:
        prefix = "[DRY RUN] " if dry_run else ""
        parts = [
            f"{self.markets_scanned} markets scanned",
            f"{self.candidates} candidate signals",
            f"{self.sent} sent",
        ]
        if self.suppressed_disabled:
            parts.append(f"{self.suppressed_disabled} dry-run would-be")
        if self.suppressed_type:
            parts.append(f"{self.suppressed_type} suppressed by type")
        if self.send_failed:
            parts.append(f"{self.send_failed} send failed")
        if self.engine_suppressed:
            parts.append(f"{self.engine_suppressed} throttled/snoozed by engine")
        if self.no_data:
            parts.append(f"{self.no_data} no data")
        if self.stale:
            parts.append(f"{self.stale} stale")
        logger.info(f"{prefix}Scan complete: {', '.join(parts)}")


async def run_universe_refresh(
    conn: sqlite3.Connection,
    client: HyperliquidClient,
    settings: Settings,
) -> list[str]:
    try:
        return await refresh_universe(conn, client, settings)
    except Exception as e:
        logger.error(f"Universe refresh failed: {e}")
        return []


async def run_signal_scan(
    conn: sqlite3.Connection,
    candle_store: CandleStore,
    settings: Settings,
    pushover: PushoverClient,
) -> ScanSummary:
    """Scan all enabled markets for setups. Returns a ScanSummary."""
    markets = repo.get_scan_enabled_markets(conn)
    if not markets:
        logger.warning("Signal scan: no scan-enabled markets")
        return ScanSummary()

    summary = ScanSummary(markets_scanned=len(markets))
    logger.info(f"Scanning {len(markets)} markets...")

    for market in markets:
        symbol = market.symbol
        try:
            df_15m = candle_store.get_df(symbol, settings.trend_timeframe)
            df_5m = candle_store.get_df(symbol, settings.setup_timeframe)

            if df_15m is None or df_5m is None:
                logger.debug(f"{symbol}: no candle data yet")
                summary.no_data += 1
                continue

            # Stale data check
            if candle_store.is_stale(symbol, settings.trend_timeframe):
                logger.debug(f"{symbol}: 15m data stale — skipping")
                summary.stale += 1
                continue
            if candle_store.is_stale(symbol, settings.setup_timeframe):
                logger.debug(f"{symbol}: 5m data stale — skipping")
                summary.stale += 1
                continue

            candidate = evaluate_symbol(symbol, df_15m, df_5m, conn, settings)
            if candidate is None:
                summary.no_signal += 1
                continue
            if candidate.suppressed:
                summary.engine_suppressed += 1
                continue

            # Record observation for dry-run signal quality tracking.
            # Only active in dry-run mode — this feature is intentionally scoped
            # to observation-only operation. Live-alert mode is handled separately
            # if and when that is explicitly designed.
            if settings.dry_run_mode:
                _record_observation(candidate, conn, settings)

            outcome = await _send_alert(candidate, conn, settings, pushover)
            if outcome == "SENT":
                summary.sent += 1
            elif outcome == "SEND_FAILED":
                summary.send_failed += 1
            elif outcome == "SUPPRESSED_TYPE":
                summary.suppressed_type += 1
            elif outcome == "SUPPRESSED_DISABLED":
                summary.suppressed_disabled += 1

        except Exception as e:
            logger.error(f"Error scanning {symbol}: {e}", exc_info=True)

    summary.log(logger, dry_run=settings.dry_run_mode)
    return summary


def _dry_run_prefix(settings: Settings) -> str:
    return "[DRY RUN] " if settings.dry_run_mode else ""


async def _send_alert(
    candidate: SignalCandidate,
    conn: sqlite3.Connection,
    settings: Settings,
    pushover: PushoverClient,
) -> AlertOutcome:
    """Evaluate gates, persist alert (only when sending), and deliver via Pushover.

    Gate order:
      1. ALERT_TYPES_ENABLED  — checked before any DB write; no cooldown consumed.
      2. ALERTS_ENABLED       — checked before any DB write; no cooldown consumed.
         Dry-run would-be alerts intentionally do NOT update the throttle/cooldown
         state so that switching ALERTS_ENABLED=true later does not find all symbols
         already on cooldown from ghost observations.
      3. Pushover send        — DB row written first so the alert_uid is valid for
         action links even if the Pushover call itself fails transiently.

    Returns one of the AlertOutcome literal values for the caller to count.
    """
    result = candidate.pullback
    trend = candidate.trend
    alert_type = candidate.alert_type
    prefix = _dry_run_prefix(settings)

    # --- Gate 1: alert type enabled? ---
    # No DB write here — type-filtered signals don't consume cooldown slots.
    if alert_type not in settings.alert_types_enabled_list:
        logger.info(
            f"{prefix}SUPPRESSED ({alert_type} not in ALERT_TYPES_ENABLED) | "
            f"{candidate.symbol} {candidate.direction}"
        )
        return "SUPPRESSED_TYPE"

    # Build alert object (not yet persisted)
    alert_uid = new_uid()
    now = utcnow_iso()
    expires = minutes_from_now(settings.setup_expiration_minutes)
    price = result.current_price

    alert = Alert(
        alert_uid=alert_uid,
        symbol=candidate.symbol,
        direction=candidate.direction,
        alert_type=alert_type,
        reference_price=price,
        entry_low=price * 0.9995,
        entry_high=price * 1.0005,
        stop_price=result.risk_plan.stop_price if result.risk_plan else None,
        target_1r=result.risk_plan.target_1r if result.risk_plan else None,
        target_2r=result.risk_plan.target_2r if result.risk_plan else None,
        invalidation_price=result.risk_plan.stop_price if result.risk_plan else None,
        risk_usd=result.risk_plan.risk_usd if result.risk_plan else None,
        suggested_notional_usd=(
            result.risk_plan.suggested_notional_usd if result.risk_plan else None
        ),
        stop_distance_pct=result.risk_plan.stop_distance_pct if result.risk_plan else None,
        expires_at=expires,
        sent_at=now,
        status="SENT",
    )

    # Format message (needed for logging in both gate-2 and send paths)
    token = generate_token(alert_uid, settings.action_token_secret, settings.action_token_ttl_hours)
    if alert_type == "CONFIRMED_SETUP":
        title, body = format_confirmed_alert(
            alert=alert,
            rsi_val=result.rsi_val,
            atr_val=result.atr_val,
            trend_reason=trend.reason,
            token=token,
            base_url=settings.apex_public_base_url,
        )
        priority = settings.pushover_confirmed_priority
    else:
        title, body = format_forming_alert(
            alert=alert,
            rsi_val=result.rsi_val,
            atr_val=result.atr_val,
            trend_reason=trend.reason,
            token=token,
            base_url=settings.apex_public_base_url,
        )
        priority = settings.pushover_default_priority

    # --- Gate 2: alerts globally enabled? ---
    # No DB write here — suppressed observations must not consume cooldown slots.
    # See docstring for rationale.
    if not settings.alerts_enabled:
        logger.info(
            f"{prefix}WOULD-BE ALERT | {alert_type} | "
            f"{candidate.symbol} {candidate.direction} | "
            f"title={title!r} | reason={result.reason} | "
            f"(suppressed: ALERTS_ENABLED=false)"
        )
        repo.log_event(
            conn,
            "ALERT_SUPPRESSED",
            f"{prefix}{alert_type}: {candidate.symbol} {candidate.direction} — ALERTS_ENABLED=false",
            metadata={"alert_uid": alert_uid, "reason": result.reason},
        )
        return "SUPPRESSED_DISABLED"

    # --- All gates passed: persist and send ---
    alert_id = repo.insert_alert(conn, alert)
    alert.id = alert_id

    repo.log_event(
        conn,
        "ALERT_GENERATED",
        f"{alert_type}: {candidate.symbol} {candidate.direction}",
        metadata={"alert_uid": alert_uid, "reason": result.reason},
    )
    logger.info(
        f"Alert: {alert_type} | {candidate.symbol} {candidate.direction} | {result.reason}"
    )

    ok = await pushover.send(title=title, message=body, priority=priority)
    if not ok:
        repo.log_event(
            conn,
            "ALERT_NOTIFICATION_FAILED",
            f"Pushover failed for alert {alert_uid}",
            level="ERROR",
        )
        return "SEND_FAILED"

    return "SENT"


async def run_followup_check(
    conn: sqlite3.Connection,
    settings: Settings,
    pushover: PushoverClient,
) -> None:
    """Send follow-up outcome requests for open trades past the follow-up window."""
    cutoff = minutes_ago_iso(settings.followup_after_enter_minutes)
    trades = repo.get_open_trades_needing_followup(conn, cutoff)

    for trade_row in trades:
        try:
            from apex.db.models import PaperTrade

            trade = PaperTrade(
                trade_uid=trade_row["trade_uid"],
                alert_id=trade_row["alert_id"],
                symbol=trade_row["symbol"],
                direction=trade_row["direction"],
                setup_type=trade_row["setup_type"],
                entry_price=trade_row["entry_price"],
                stop_price=trade_row["stop_price"],
                target_1r=trade_row["target_1r"],
                target_2r=trade_row["target_2r"],
                risk_usd=trade_row["risk_usd"],
                suggested_notional_usd=trade_row["suggested_notional_usd"],
                opened_at=trade_row["opened_at"],
            )

            token = generate_token(
                trade.trade_uid,
                settings.action_token_secret,
                settings.action_token_ttl_hours,
            )
            title, body = format_followup_message(trade, token, settings.apex_public_base_url)

            ok = await pushover.send(title=title, message=body, priority=settings.pushover_default_priority)
            if ok:
                repo.mark_followup_sent(conn, trade.trade_uid)
                logger.info(f"Follow-up sent for trade {trade.trade_uid}")
        except Exception as e:
            logger.error(f"Follow-up error for trade {trade_row['trade_uid']}: {e}")


def _seconds_between(earlier: str, later: str) -> float:
    """Elapsed seconds between two ISO8601 UTC timestamps (``%Y-%m-%dT%H:%M:%SZ``)."""
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    t0 = _dt.strptime(earlier, fmt).replace(tzinfo=_tz.utc)
    t1 = _dt.strptime(later, fmt).replace(tzinfo=_tz.utc)
    return (t1 - t0).total_seconds()


def _record_observation(
    candidate: SignalCandidate,
    conn: sqlite3.Connection,
    settings: Settings,
) -> None:
    """Record or refresh a signal observation for dry-run quality tracking.

    Deduplication: if an open (OBSERVED, not yet expired) row already exists
    for this symbol/direction/signal_type, touch its updated_at and return.
    Only inserts a new row when no open observation exists (first time, or
    after the previous one closed/expired).

    Safety guarantees — this function NEVER:
      - writes to the alerts table
      - reads or writes alert cooldown state
      - writes to daily_risk
      - sends Pushover notifications
      - requires exchange credentials
    """
    try:
        rp = candidate.pullback.risk_plan  # None for SETUP_FORMING

        existing = repo.get_open_signal_observation(
            conn, candidate.symbol, candidate.direction, candidate.alert_type
        )
        if existing is not None:
            repo.touch_signal_observation(conn, existing["observation_uid"])
            return

        metadata = {
            "trend_reason": candidate.trend.reason,
            "pullback_reason": candidate.pullback.reason,
        }
        obs = SignalObservation(
            observation_uid=new_uid(),
            observed_at=utcnow_iso(),
            symbol=candidate.symbol,
            direction=candidate.direction,
            signal_type=candidate.alert_type,
            entry_price=rp.entry_price if rp else None,
            stop_price=rp.stop_price if rp else None,
            target_1r=rp.target_1r if rp else None,
            target_2r=rp.target_2r if rp else None,
            expires_at=minutes_from_now(settings.setup_expiration_minutes),
            metadata_json=json.dumps(metadata),
        )
        repo.insert_signal_observation(conn, obs)
        logger.debug(
            f"Observation recorded: {candidate.alert_type} | "
            f"{candidate.symbol} {candidate.direction}"
        )
    except Exception as e:
        # Observation failure must never interrupt the scan or alert flow.
        logger.warning(f"Failed to record observation for {candidate.symbol}: {e}")


async def run_observation_evaluation(
    conn: sqlite3.Connection,
    candle_store: CandleStore,
    settings: Settings,
) -> None:
    """Evaluate open signal observations against current candle data.

    For each open CONFIRMED_SETUP observation, uses the most recent closed
    candle's high/low range to check for outcome. SETUP_FORMING observations
    have no price levels and are only expired.

    TP1 is a milestone, not a terminal event (Milestone 10A).
    When TP1 is hit the observation stays OBSERVED so that TP2, stop, or
    expiry can still be recorded. hit_1r_at is written via record_tp1_milestone.

    Outcome priority:
      Pre-TP1 (hit_1r_at is NULL):
        1. Expired             → EXPIRED  (hit_1r_before_expiry=0)
        2. Same-candle ambiguity (stop + any target): conservative STOPPED (hit_1r_before_stop=0)
        3. TP2 directly hit    → HIT_2R   (hit_1r_at also set; TP1 logically passed)
        4. TP1 hit             → milestone recorded, observation stays OBSERVED
        5. Stop hit            → STOPPED  (hit_1r_before_stop=0)
        6. Nothing             → update MFE/MAE, stay OBSERVED

      Post-TP1 (hit_1r_at is not NULL):
        1. Expired             → EXPIRED  (hit_1r_before_expiry=1)
        2. Same-candle ambiguity (stop + TP2): conservative STOPPED (hit_1r_before_stop=1)
        3. TP2 hit             → HIT_2R   (hit_1r_before_stop not applicable)
        4. Stop hit            → STOPPED  (hit_1r_before_stop=1)
        5. Nothing             → update MFE/MAE, stay OBSERVED

    MFE/MAE are updated on every pass in R units relative to entry_price.

    Safety: this function NEVER writes to alerts, paper_trades, daily_risk,
    or snoozes. It is read-only with respect to all live-trading state.
    """
    # Defensive guard: observation tracking is a dry-run-only feature.
    if not settings.dry_run_mode:
        return

    try:
        open_obs = repo.get_open_signal_observations(conn)
        if not open_obs:
            return

        now = utcnow_iso()

        for row in open_obs:
            uid = row["observation_uid"]
            symbol = row["symbol"]
            direction = row["direction"]
            signal_type = row["signal_type"]

            # 1. Expiry check (applies to all signal types)
            if now >= row["expires_at"]:
                repo.close_signal_observation(
                    conn, uid,
                    status="EXPIRED",
                    closed_at=now,
                    mfe=row["max_favorable_excursion"],
                    mae=row["max_adverse_excursion"],
                    expired_at=now,
                    time_to_expiry_seconds=_seconds_between(row["observed_at"], now),
                    hit_1r_before_expiry=1 if row["hit_1r_at"] else 0,
                    first_terminal_status="EXPIRED",
                    final_status="EXPIRED",
                )
                logger.debug(f"Observation {uid[:8]} expired: {symbol} {direction}")
                continue

            # 2. SETUP_FORMING: no price levels — expiry only
            if signal_type == "SETUP_FORMING":
                continue

            entry_price = row["entry_price"]
            stop_price = row["stop_price"]
            target_1r = row["target_1r"]
            target_2r = row["target_2r"]

            if None in (entry_price, stop_price, target_1r, target_2r):
                continue

            # 3. Get most recent closed candle high/low
            df = candle_store.get_df(symbol, settings.setup_timeframe)
            if df is None or df.empty:
                continue

            last = df.iloc[-1]
            high = float(last["high"])
            low = float(last["low"])

            # 4. Update running MFE/MAE (in R units from entry)
            r_size = abs(entry_price - stop_price)
            if r_size <= 0:
                continue

            if direction == "LONG":
                cur_favorable = (high - entry_price) / r_size
                cur_adverse = (entry_price - low) / r_size
            else:
                cur_favorable = (entry_price - low) / r_size
                cur_adverse = (high - entry_price) / r_size

            new_mfe = max(row["max_favorable_excursion"] or 0.0, cur_favorable)
            new_mae = max(row["max_adverse_excursion"] or 0.0, cur_adverse)

            # 5. Detect which levels were crossed this candle
            if direction == "LONG":
                stop_hit = low <= stop_price
                t1_hit = high >= target_1r
                t2_hit = high >= target_2r
            else:
                stop_hit = high >= stop_price
                t1_hit = low <= target_1r
                t2_hit = low <= target_2r

            already_hit_1r = row["hit_1r_at"] is not None

            # 6. Apply outcome logic
            if not already_hit_1r:
                # Pre-TP1 branch
                if stop_hit and (t1_hit or t2_hit):
                    # Same-candle ambiguity: conservative stop-first
                    repo.close_signal_observation(
                        conn, uid,
                        status="STOPPED", outcome_r=-1.0, closed_at=now,
                        mfe=new_mfe, mae=new_mae,
                        stopped_at=now,
                        time_to_stop_seconds=_seconds_between(row["observed_at"], now),
                        hit_1r_before_stop=0,
                        first_terminal_status="STOPPED", final_status="STOPPED",
                    )
                    logger.debug(
                        f"Obs {uid[:8]} STOPPED (conservative, pre-TP1): {symbol} {direction}"
                    )
                elif t2_hit:
                    # Direct TP2 hit — TP1 was logically passed on the way
                    t_secs = _seconds_between(row["observed_at"], now)
                    repo.close_signal_observation(
                        conn, uid,
                        status="HIT_2R", outcome_r=2.0, closed_at=now,
                        mfe=new_mfe, mae=new_mae,
                        hit_1r_at=now, time_to_1r_seconds=t_secs,
                        hit_2r_at=now, time_to_2r_seconds=t_secs,
                        first_terminal_status="HIT_2R", final_status="HIT_2R",
                    )
                    logger.debug(
                        f"Obs {uid[:8]} HIT_2R (direct, TP1 implicit): {symbol} {direction}"
                    )
                elif t1_hit:
                    # TP1 milestone — stay open for TP2 / stop / expiry
                    repo.record_tp1_milestone(
                        conn, uid,
                        hit_at=now,
                        time_seconds=_seconds_between(row["observed_at"], now),
                        mfe=new_mfe, mae=new_mae,
                    )
                    logger.debug(
                        f"Obs {uid[:8]} TP1 milestone (continuing): {symbol} {direction}"
                    )
                elif stop_hit:
                    repo.close_signal_observation(
                        conn, uid,
                        status="STOPPED", outcome_r=-1.0, closed_at=now,
                        mfe=new_mfe, mae=new_mae,
                        stopped_at=now,
                        time_to_stop_seconds=_seconds_between(row["observed_at"], now),
                        hit_1r_before_stop=0,
                        first_terminal_status="STOPPED", final_status="STOPPED",
                    )
                    logger.debug(
                        f"Obs {uid[:8]} STOPPED: {symbol} {direction}"
                    )
                else:
                    repo.update_observation_excursions(conn, uid, mfe=new_mfe, mae=new_mae)

            else:
                # Post-TP1 branch: track TP2 or stop only
                if stop_hit and t2_hit:
                    # Same-candle ambiguity after TP1: conservative stop-first
                    repo.close_signal_observation(
                        conn, uid,
                        status="STOPPED", outcome_r=-1.0, closed_at=now,
                        mfe=new_mfe, mae=new_mae,
                        stopped_at=now,
                        time_to_stop_seconds=_seconds_between(row["observed_at"], now),
                        hit_1r_before_stop=1,
                        first_terminal_status="STOPPED", final_status="STOPPED",
                    )
                    logger.debug(
                        f"Obs {uid[:8]} STOPPED (conservative, post-TP1): {symbol} {direction}"
                    )
                elif t2_hit:
                    repo.close_signal_observation(
                        conn, uid,
                        status="HIT_2R", outcome_r=2.0, closed_at=now,
                        mfe=new_mfe, mae=new_mae,
                        hit_2r_at=now,
                        time_to_2r_seconds=_seconds_between(row["observed_at"], now),
                        first_terminal_status="HIT_2R", final_status="HIT_2R",
                    )
                    logger.debug(
                        f"Obs {uid[:8]} HIT_2R (after TP1): {symbol} {direction}"
                    )
                elif stop_hit:
                    repo.close_signal_observation(
                        conn, uid,
                        status="STOPPED", outcome_r=-1.0, closed_at=now,
                        mfe=new_mfe, mae=new_mae,
                        stopped_at=now,
                        time_to_stop_seconds=_seconds_between(row["observed_at"], now),
                        hit_1r_before_stop=1,
                        first_terminal_status="STOPPED", final_status="STOPPED",
                    )
                    logger.debug(
                        f"Obs {uid[:8]} STOPPED (post-TP1): {symbol} {direction}"
                    )
                else:
                    repo.update_observation_excursions(conn, uid, mfe=new_mfe, mae=new_mae)

    except Exception as e:
        logger.error(f"Observation evaluation error: {e}", exc_info=True)
