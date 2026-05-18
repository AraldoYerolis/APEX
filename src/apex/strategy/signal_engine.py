"""Signal engine: orchestrates trend filter + pullback strategy + all gate checks."""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from apex.config import Settings
from apex.db import repository as repo
from apex.strategy.pullback_strategy import PullbackResult, evaluate_pullback
from apex.strategy.throttles import can_send_alert, is_symbol_snoozed
from apex.strategy.trend_filter import TrendBias, compute_trend_bias

logger = logging.getLogger(__name__)


@dataclass
class SignalCandidate:
    symbol: str
    direction: str
    alert_type: str  # SETUP_FORMING | CONFIRMED_SETUP
    pullback: PullbackResult
    trend: TrendBias
    suppressed: bool = False
    suppression_reason: str = ""


def evaluate_symbol(
    symbol: str,
    df_15m: pd.DataFrame,
    df_5m: pd.DataFrame,
    conn: sqlite3.Connection,
    settings: Settings,
) -> Optional[SignalCandidate]:
    """Run full signal evaluation for one symbol. Returns candidate or None."""

    # 1. Compute 15m trend bias
    trend = compute_trend_bias(
        df_15m,
        ema_fast_period=settings.ema_fast,
        ema_slow_period=settings.ema_slow,
        vwap_lookback=settings.vwap_lookback_candles,
    )

    if trend.bias == "NONE":
        logger.debug(f"{symbol}: no trend bias — {trend.reason}")
        return None

    # 2. Evaluate 5m pullback
    result = evaluate_pullback(
        df_5m=df_5m,
        trend=trend,
        account_size_usd=settings.account_size_usd,
        risk_per_trade_pct=settings.risk_per_trade_pct,
        max_stop_distance_pct=settings.max_stop_distance_pct,
        stop_atr_multiplier=settings.stop_atr_multiplier,
        rsi_period=settings.rsi_period,
        atr_period=settings.atr_period,
        vwap_lookback=settings.vwap_lookback_candles,
    )

    if result.state == "NONE":
        logger.debug(f"{symbol}: no setup — {result.reason}")
        return None

    alert_type = "CONFIRMED_SETUP" if result.state == "CONFIRMED" else "SETUP_FORMING"

    # 3. Snooze check
    if is_symbol_snoozed(conn, symbol):
        logger.info(f"{symbol}: snoozed — skipping")
        return SignalCandidate(
            symbol=symbol,
            direction=trend.bias,
            alert_type=alert_type,
            pullback=result,
            trend=trend,
            suppressed=True,
            suppression_reason="symbol snoozed",
        )

    # 4. Daily lockout check (only for confirmed)
    if alert_type == "CONFIRMED_SETUP":
        if repo.is_daily_lockout_active(conn, settings.max_daily_loss_usd):
            logger.info(f"{symbol}: daily lockout active — suppressing CONFIRMED alert")
            return SignalCandidate(
                symbol=symbol,
                direction=trend.bias,
                alert_type=alert_type,
                pullback=result,
                trend=trend,
                suppressed=True,
                suppression_reason="daily loss lockout",
            )

    # 5. Throttle check
    allowed, throttle_reason = can_send_alert(
        conn=conn,
        symbol=symbol,
        alert_type=alert_type,
        confirmed_cooldown_minutes=settings.confirmed_alert_cooldown_minutes,
        forming_cooldown_minutes=settings.forming_alert_cooldown_minutes,
        global_confirmed_per_hour=settings.global_confirmed_alerts_per_hour,
    )
    if not allowed:
        logger.info(f"{symbol}: throttled — {throttle_reason}")
        return SignalCandidate(
            symbol=symbol,
            direction=trend.bias,
            alert_type=alert_type,
            pullback=result,
            trend=trend,
            suppressed=True,
            suppression_reason=throttle_reason,
        )

    return SignalCandidate(
        symbol=symbol,
        direction=trend.bias,
        alert_type=alert_type,
        pullback=result,
        trend=trend,
    )
