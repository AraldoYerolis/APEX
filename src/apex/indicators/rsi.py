"""Relative Strength Index."""
from __future__ import annotations

import pandas as pd


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI using EWM with alpha=1/period."""
    if len(series) < period + 1:
        return pd.Series([float("nan")] * len(series), index=series.index)

    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def rsi_from_closes(closes: list[float], period: int = 14) -> list[float]:
    s = pd.Series(closes, dtype=float)
    return rsi(s, period).tolist()
