"""Exponential Moving Average."""
from __future__ import annotations

import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Standard EMA with adjust=False to match typical charting tools."""
    if len(series) < period:
        return pd.Series([float("nan")] * len(series), index=series.index)
    return series.ewm(span=period, adjust=False).mean()


def ema_from_closes(closes: list[float], period: int) -> list[float]:
    """Convenience wrapper; returns list of floats (NaN where insufficient data)."""
    s = pd.Series(closes, dtype=float)
    return ema(s, period).tolist()
