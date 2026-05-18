"""Rolling VWAP over a configurable candle lookback window."""
from __future__ import annotations

import pandas as pd


def vwap(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    lookback: int = 96,
) -> pd.Series:
    """Rolling VWAP = sum(typical_price * volume) / sum(volume) over lookback candles.

    Returns NaN where volume sum is zero or lookback cannot be satisfied.
    """
    typical = (high + low + close) / 3
    pv = typical * volume

    pv_sum = pv.rolling(window=lookback, min_periods=1).sum()
    vol_sum = volume.rolling(window=lookback, min_periods=1).sum()

    result = pv_sum / vol_sum.replace(0, float("nan"))
    return result


def vwap_from_ohlcv(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[float],
    lookback: int = 96,
) -> list[float]:
    h = pd.Series(highs, dtype=float)
    l = pd.Series(lows, dtype=float)
    c = pd.Series(closes, dtype=float)
    v = pd.Series(volumes, dtype=float)
    return vwap(h, l, c, v, lookback).tolist()
