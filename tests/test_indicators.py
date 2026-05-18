"""Tests for indicator calculations."""
import math

import pandas as pd
import pytest

from apex.indicators.ema import ema, ema_from_closes
from apex.indicators.rsi import rsi, rsi_from_closes
from apex.indicators.atr import atr, atr_from_ohlc
from apex.indicators.vwap import vwap, vwap_from_ohlcv


# ------------------------------------------------------------------ EMA

def test_ema_basic():
    closes = [10.0, 11.0, 12.0, 11.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
    result = ema_from_closes(closes, period=3)
    assert len(result) == len(closes)
    assert not math.isnan(result[-1])
    # EMA should be somewhere between min and max
    assert min(closes) <= result[-1] <= max(closes)


def test_ema_insufficient_data():
    closes = [10.0, 11.0]
    result = ema_from_closes(closes, period=5)
    # Should still return values (EWM works with partial data)
    assert len(result) == 2


def test_ema_fast_gt_slow_in_uptrend():
    """In a clear uptrend, EMA9 should be above EMA21."""
    closes = list(range(1, 50))  # [1, 2, ..., 49]
    fast = ema_from_closes(closes, period=9)
    slow = ema_from_closes(closes, period=21)
    assert fast[-1] > slow[-1]


def test_ema_fast_lt_slow_in_downtrend():
    """In a clear downtrend, EMA9 should be below EMA21."""
    closes = list(range(50, 1, -1))  # [50, 49, ..., 2]
    fast = ema_from_closes(closes, period=9)
    slow = ema_from_closes(closes, period=21)
    assert fast[-1] < slow[-1]


# ------------------------------------------------------------------ RSI

def test_rsi_range():
    """RSI values must be in [0, 100]."""
    closes = [100, 102, 101, 103, 105, 104, 106, 108, 107, 109, 110,
              108, 107, 106, 105, 104, 103]
    result = rsi_from_closes(closes, period=14)
    for v in result:
        if not math.isnan(v):
            assert 0 <= v <= 100


def test_rsi_overbought_in_uptrend():
    """Strong up-move with occasional small dips should produce RSI > 70."""
    # Mostly up, tiny dips to avoid division-by-zero in RS calculation
    closes = []
    price = 100.0
    for i in range(30):
        price += 2.0 if i % 5 != 4 else -0.1  # small dip every 5th candle
        closes.append(price)
    result = rsi_from_closes(closes, period=14)
    valid = [v for v in result if not math.isnan(v)]
    assert len(valid) > 0
    assert valid[-1] > 70


def test_rsi_oversold_in_downtrend():
    """Consistent strong down-moves should produce RSI < 30."""
    closes = [float(200 - i * 2) for i in range(30)]
    result = rsi_from_closes(closes, period=14)
    valid = [v for v in result if not math.isnan(v)]
    assert valid[-1] < 30


def test_rsi_insufficient_data():
    closes = [100.0, 101.0]
    result = rsi_from_closes(closes, period=14)
    assert all(math.isnan(v) for v in result)


# ------------------------------------------------------------------ ATR

def test_atr_positive():
    """ATR must be positive for volatile candles."""
    highs  = [105, 106, 104, 107, 108, 106, 109, 110, 108, 111, 112, 110, 113, 114, 112]
    lows   = [100, 101,  99, 102, 103, 101, 104, 105, 103, 106, 107, 105, 108, 109, 107]
    closes = [103, 104, 102, 105, 106, 104, 107, 108, 106, 109, 110, 108, 111, 112, 110]
    result = atr_from_ohlc(highs, lows, closes, period=14)
    valid = [v for v in result if not math.isnan(v)]
    assert all(v > 0 for v in valid)


def test_atr_flat_market():
    """ATR near zero for flat market."""
    highs  = [100.0] * 20
    lows   = [100.0] * 20
    closes = [100.0] * 20
    result = atr_from_ohlc(highs, lows, closes, period=14)
    valid = [v for v in result if not math.isnan(v)]
    assert all(v < 0.01 for v in valid)


# ------------------------------------------------------------------ VWAP

def test_vwap_basic():
    highs  = [105.0] * 20
    lows   = [95.0]  * 20
    closes = [100.0] * 20
    volumes = [1000.0] * 20
    result = vwap_from_ohlcv(highs, lows, closes, volumes, lookback=20)
    # typical price = (105 + 95 + 100) / 3 = 100
    assert all(abs(v - 100.0) < 0.01 for v in result if not math.isnan(v))


def test_vwap_zero_volume():
    """VWAP handles zero volume without crash."""
    highs   = [105.0] * 10
    lows    = [95.0]  * 10
    closes  = [100.0] * 10
    volumes = [0.0]   * 10
    result = vwap_from_ohlcv(highs, lows, closes, volumes, lookback=10)
    # All NaN is acceptable when volume is zero
    assert len(result) == 10
