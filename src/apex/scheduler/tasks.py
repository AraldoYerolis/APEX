"""Scheduled background tasks."""
from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING

import pandas as pd

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
) -> None:
    """Scan all enabled markets for setups."""
    markets = repo.get_scan_enabled_markets(conn)
    if not markets:
        logger.warning("Signal scan: no scan-enabled markets")
        return

    logger.info(f"Scanning {len(markets)} markets...")
    alerts_sent = 0

    for market in markets:
        symbol = market.symbol
        try:
            df_15m = candle_store.get_df(symbol, settings.trend_timeframe)
            df_5m = candle_store.get_df(symbol, settings.setup_timeframe)

            if df_15m is None or df_5m is None:
                logger.debug(f"{symbol}: no candle data yet")
                continue

            # Stale data check
            if candle_store.is_stale(symbol, settings.trend_timeframe):
                logger.debug(f"{symbol}: 15m data stale — skipping")
                continue
            if candle_store.is_stale(symbol, settings.setup_timeframe):
                logger.debug(f"{symbol}: 5m data stale — skipping")
                continue

            candidate = evaluate_symbol(symbol, df_15m, df_5m, conn, settings)
            if candidate is None or candidate.suppressed:
                continue

            await _send_alert(candidate, conn, settings, pushover)
            alerts_sent += 1

        except Exception as e:
            logger.error(f"Error scanning {symbol}: {e}", exc_info=True)

    logger.info(f"Scan complete: {alerts_sent} alerts sent from {len(markets)} markets")


async def _send_alert(
    candidate: SignalCandidate,
    conn: sqlite3.Connection,
    settings: Settings,
    pushover: PushoverClient,
) -> None:
    """Persist alert and send Pushover notification."""
    result = candidate.pullback
    trend = candidate.trend
    alert_type = candidate.alert_type

    alert_uid = new_uid()
    now = utcnow_iso()
    expires = minutes_from_now(settings.setup_expiration_minutes)

    price = result.current_price
    entry_low = price * 0.9995
    entry_high = price * 1.0005

    alert = Alert(
        alert_uid=alert_uid,
        symbol=candidate.symbol,
        direction=candidate.direction,
        alert_type=alert_type,
        reference_price=price,
        entry_low=entry_low,
        entry_high=entry_high,
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

    alert_id = repo.insert_alert(conn, alert)
    alert.id = alert_id

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

    # Persist message body
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
