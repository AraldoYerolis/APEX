"""TREND_PULLBACK setup detection on 5m candles."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Optional

import pandas as pd

from apex.indicators.atr import atr
from apex.indicators.rsi import rsi
from apex.indicators.vwap import vwap
from apex.strategy.trend_filter import TrendBias
from apex.strategy.risk import RiskPlan, calculate_risk_plan


SetupState = Literal["NONE", "FORMING", "CONFIRMED"]


@dataclass
class PullbackResult:
    state: SetupState
    direction: str  # LONG | SHORT
    reason: str
    rsi_val: float = float("nan")
    atr_val: float = float("nan")
    vwap_val: float = float("nan")
    current_price: float = float("nan")
    risk_plan: Optional[RiskPlan] = None


def _swing_low(df: pd.DataFrame, lookback: int = 5) -> float:
    return float(df["low"].tail(lookback).min())


def _swing_high(df: pd.DataFrame, lookback: int = 5) -> float:
    return float(df["high"].tail(lookback).max())


def evaluate_pullback(
    df_5m: pd.DataFrame,
    trend: TrendBias,
    account_size_usd: float,
    risk_per_trade_pct: float,
    max_stop_distance_pct: float,
    stop_atr_multiplier: float = 1.25,
    rsi_period: int = 14,
    atr_period: int = 14,
    vwap_lookback: int = 96,
) -> PullbackResult:
    """Evaluate 5m candles for a TREND_PULLBACK setup.

    Returns a PullbackResult indicating NONE / FORMING / CONFIRMED.
    """
    if trend.bias == "NONE":
        return PullbackResult(state="NONE", direction="NONE", reason="no trend bias")

    if len(df_5m) < max(rsi_period + 2, atr_period + 2, 10):
        return PullbackResult(
            state="NONE", direction=trend.bias, reason="insufficient 5m candles"
        )

    rsi_s = rsi(df_5m["close"], rsi_period)
    atr_s = atr(df_5m["high"], df_5m["low"], df_5m["close"], atr_period)
    vwap_s = vwap(df_5m["high"], df_5m["low"], df_5m["close"], df_5m["volume"], vwap_lookback)

    rsi_now = float(rsi_s.iloc[-1])
    rsi_prev = float(rsi_s.iloc[-2])
    atr_val = float(atr_s.iloc[-1])
    vwap_val = float(vwap_s.iloc[-1])
    price = float(df_5m["close"].iloc[-1])

    # Validate indicators
    for v in [rsi_now, rsi_prev, atr_val, vwap_val, price]:
        if math.isnan(v):
            return PullbackResult(
                state="NONE",
                direction=trend.bias,
                reason="NaN in 5m indicators",
                rsi_val=rsi_now,
                atr_val=atr_val,
                vwap_val=vwap_val,
                current_price=price,
            )

    direction = trend.bias

    if direction == "LONG":
        # --- FORMING: price pulling back toward VWAP, RSI cooling into 40-55 ---
        rsi_in_pullback_zone = 40 <= rsi_now <= 55
        near_vwap = price <= vwap_val * 1.005  # within 0.5% above VWAP or below
        not_overextended = price <= vwap_val * 1.02

        if rsi_in_pullback_zone and near_vwap and not_overextended:
            state = "FORMING"
            reason = f"LONG forming: RSI={rsi_now:.1f} in pullback zone, price near VWAP"
        else:
            return PullbackResult(
                state="NONE",
                direction="LONG",
                reason=f"LONG conditions not met: RSI={rsi_now:.1f} near_vwap={near_vwap}",
                rsi_val=rsi_now,
                atr_val=atr_val,
                vwap_val=vwap_val,
                current_price=price,
            )

        # --- CONFIRMED: RSI turning up from pullback zone, price reclaiming VWAP ---
        rsi_turning_up = rsi_now > rsi_prev
        price_reclaimed_vwap = price >= vwap_val * 0.998

        if rsi_turning_up and price_reclaimed_vwap:
            swing_lo = _swing_low(df_5m, lookback=5)
            atr_stop = price - stop_atr_multiplier * atr_val
            stop = min(swing_lo, atr_stop)  # farther (lower) of the two

            risk_plan = calculate_risk_plan(
                direction="LONG",
                entry_price=price,
                stop_price=stop,
                account_size_usd=account_size_usd,
                risk_per_trade_pct=risk_per_trade_pct,
                max_stop_distance_pct=max_stop_distance_pct,
            )
            if risk_plan is None:
                return PullbackResult(
                    state="FORMING",
                    direction="LONG",
                    reason=f"LONG forming but stop too wide or invalid: swing_lo={swing_lo:.4f}",
                    rsi_val=rsi_now,
                    atr_val=atr_val,
                    vwap_val=vwap_val,
                    current_price=price,
                )

            return PullbackResult(
                state="CONFIRMED",
                direction="LONG",
                reason=f"LONG confirmed: RSI turning up, price reclaimed VWAP",
                rsi_val=rsi_now,
                atr_val=atr_val,
                vwap_val=vwap_val,
                current_price=price,
                risk_plan=risk_plan,
            )

        return PullbackResult(
            state="FORMING",
            direction="LONG",
            reason=reason,
            rsi_val=rsi_now,
            atr_val=atr_val,
            vwap_val=vwap_val,
            current_price=price,
        )

    else:  # SHORT
        # --- FORMING: price bouncing up toward VWAP, RSI rising into 45-60 ---
        rsi_in_bounce_zone = 45 <= rsi_now <= 60
        near_vwap = price >= vwap_val * 0.995
        not_overextended = price >= vwap_val * 0.98

        if rsi_in_bounce_zone and near_vwap and not_overextended:
            state = "FORMING"
            reason = f"SHORT forming: RSI={rsi_now:.1f} in bounce zone, price near VWAP"
        else:
            return PullbackResult(
                state="NONE",
                direction="SHORT",
                reason=f"SHORT conditions not met: RSI={rsi_now:.1f} near_vwap={near_vwap}",
                rsi_val=rsi_now,
                atr_val=atr_val,
                vwap_val=vwap_val,
                current_price=price,
            )

        # --- CONFIRMED: RSI turning down, price rejecting back below VWAP ---
        rsi_turning_down = rsi_now < rsi_prev
        price_rejected_vwap = price <= vwap_val * 1.002

        if rsi_turning_down and price_rejected_vwap:
            swing_hi = _swing_high(df_5m, lookback=5)
            atr_stop = price + stop_atr_multiplier * atr_val
            stop = max(swing_hi, atr_stop)  # farther (higher) of the two

            risk_plan = calculate_risk_plan(
                direction="SHORT",
                entry_price=price,
                stop_price=stop,
                account_size_usd=account_size_usd,
                risk_per_trade_pct=risk_per_trade_pct,
                max_stop_distance_pct=max_stop_distance_pct,
            )
            if risk_plan is None:
                return PullbackResult(
                    state="FORMING",
                    direction="SHORT",
                    reason=f"SHORT forming but stop too wide or invalid: swing_hi={swing_hi:.4f}",
                    rsi_val=rsi_now,
                    atr_val=atr_val,
                    vwap_val=vwap_val,
                    current_price=price,
                )

            return PullbackResult(
                state="CONFIRMED",
                direction="SHORT",
                reason=f"SHORT confirmed: RSI turning down, price rejected VWAP",
                rsi_val=rsi_now,
                atr_val=atr_val,
                vwap_val=vwap_val,
                current_price=price,
                risk_plan=risk_plan,
            )

        return PullbackResult(
            state="FORMING",
            direction="SHORT",
            reason=reason,
            rsi_val=rsi_now,
            atr_val=atr_val,
            vwap_val=vwap_val,
            current_price=price,
        )
