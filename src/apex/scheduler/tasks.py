"""Scheduled background tasks."""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Literal

from apex.actions.tokens import generate_token
from apex.config import Settings
from apex.data.candle_store import CandleStore
from apex.data.hyperliquid_client import HyperliquidClient
from apex.data.market_universe import refresh_universe
from apex.db import repository as repo
from apex.db.models import Alert
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
