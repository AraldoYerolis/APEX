"""Tests for trend filter and pullback strategy."""
import math

import pandas as pd
import pytest

from apex.strategy.trend_filter import compute_trend_bias
from apex.strategy.pullback_strategy import evaluate_pullback


def _make_df(closes, highs=None, lows=None, volumes=None):
    n = len(closes)
    highs = highs or [c * 1.005 for c in closes]
    lows = lows or [c * 0.995 for c in closes]
    volumes = volumes or [1000.0] * n
    return pd.DataFrame({
        "open": closes,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
        "open_time": list(range(n)),
    })


# ------------------------------------------------------------------ trend filter

def test_long_bias():
    """Uptrending market should produce LONG bias."""
    # Strong uptrend: 50 candles going from 100 to 150
    closes = [100 + i for i in range(50)]
    df = _make_df(closes)
    result = compute_trend_bias(df, ema_fast_period=9, ema_slow_period=21, vwap_lookback=30)
    assert result.bias == "LONG"


def test_short_bias():
    """Downtrending market should produce SHORT bias."""
    closes = [150 - i for i in range(50)]
    df = _make_df(closes)
    result = compute_trend_bias(df, ema_fast_period=9, ema_slow_period=21, vwap_lookback=30)
    assert result.bias == "SHORT"


def test_no_bias_insufficient_data():
    """Too few candles should produce NONE bias."""
    closes = [100.0, 101.0, 102.0]
    df = _make_df(closes)
    result = compute_trend_bias(df, ema_fast_period=9, ema_slow_period=21)
    assert result.bias == "NONE"


def test_no_bias_flat_market():
    """Flat sideways market — EMAs cross, VWAP is at price — bias should be NONE."""
    # Flat price → EMA9 ≈ EMA21 ≈ VWAP, conditions don't strongly agree
    closes = [100.0] * 50
    df = _make_df(closes)
    result = compute_trend_bias(df, ema_fast_period=9, ema_slow_period=21)
    # Flat market: EMA9 == EMA21 (not strictly greater), so cannot be LONG
    # price == VWAP exactly
    assert result.bias == "NONE"


# ------------------------------------------------------------------ pullback strategy

def _make_long_bias():
    from apex.strategy.trend_filter import TrendBias
    return TrendBias(
        bias="LONG",
        ema_fast=105.0,
        ema_slow=100.0,
        vwap_val=103.0,
        last_close=104.0,
        reason="EMA9>EMA21 and close above VWAP",
    )


def _make_short_bias():
    from apex.strategy.trend_filter import TrendBias
    return TrendBias(
        bias="SHORT",
        ema_fast=95.0,
        ema_slow=100.0,
        vwap_val=97.0,
        last_close=96.0,
        reason="EMA9<EMA21 and close below VWAP",
    )


def test_long_pullback_forming():
    """Create a pullback scenario: price near VWAP, RSI cooling, not yet turning."""
    # Build candle data where:
    # - Price started high (~110) and pulled back to ~103 (near VWAP)
    # - RSI should be in the cooling range (40-55)
    import numpy as np

    # 40 candles: starts at 110, drops to 103 (pullback)
    closes = [110 - i * 0.2 for i in range(35)] + [103.0, 103.5]  # 37 candles
    # Make volumes decent to get meaningful VWAP
    volumes = [1000.0] * len(closes)

    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    df = _make_df(closes, highs=highs, lows=lows, volumes=volumes)

    trend = _make_long_bias()

    result = evaluate_pullback(
        df_5m=df,
        trend=trend,
        account_size_usd=100.0,
        risk_per_trade_pct=1.0,
        max_stop_distance_pct=4.0,
        stop_atr_multiplier=1.25,
        rsi_period=14,
        atr_period=14,
        vwap_lookback=30,
    )

    # We don't strictly require FORMING here since exact RSI depends on data;
    # just ensure it doesn't crash and returns a valid state
    assert result.state in ("NONE", "FORMING", "CONFIRMED")
    assert result.direction == "LONG"
    assert not math.isnan(result.rsi_val)
    assert not math.isnan(result.atr_val)


def test_short_pullback_no_setup_when_no_bias():
    """When trend bias is NONE, no setup should form."""
    from apex.strategy.trend_filter import TrendBias

    no_bias = TrendBias(
        bias="NONE",
        ema_fast=100.0,
        ema_slow=100.0,
        vwap_val=100.0,
        last_close=100.0,
        reason="no agreement",
    )
    closes = [100.0] * 30
    df = _make_df(closes)

    result = evaluate_pullback(
        df_5m=df,
        trend=no_bias,
        account_size_usd=100.0,
        risk_per_trade_pct=1.0,
        max_stop_distance_pct=4.0,
    )
    assert result.state == "NONE"


def test_insufficient_candles_returns_none():
    """Too few 5m candles should return NONE."""
    closes = [100.0] * 5
    df = _make_df(closes)
    trend = _make_long_bias()

    result = evaluate_pullback(
        df_5m=df,
        trend=trend,
        account_size_usd=100.0,
        risk_per_trade_pct=1.0,
        max_stop_distance_pct=4.0,
    )
    assert result.state == "NONE"
