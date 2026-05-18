"""15m trend bias filter."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

import pandas as pd

from apex.indicators.ema import ema
from apex.indicators.vwap import vwap


Bias = Literal["LONG", "SHORT", "NONE"]


@dataclass
class TrendBias:
    bias: Bias
    ema_fast: float
    ema_slow: float
    vwap_val: float
    last_close: float
    reason: str


def compute_trend_bias(
    df: pd.DataFrame,
    ema_fast_period: int = 9,
    ema_slow_period: int = 21,
    vwap_lookback: int = 96,
) -> TrendBias:
    """Compute 15m trend bias.

    df must have columns: open, high, low, close, volume (oldest first).
    """
    if len(df) < max(ema_fast_period, ema_slow_period) + 1:
        return TrendBias(
            bias="NONE",
            ema_fast=float("nan"),
            ema_slow=float("nan"),
            vwap_val=float("nan"),
            last_close=float("nan"),
            reason="insufficient data",
        )

    ema_f = ema(df["close"], ema_fast_period)
    ema_s = ema(df["close"], ema_slow_period)
    vwap_s = vwap(df["high"], df["low"], df["close"], df["volume"], vwap_lookback)

    ema_fast_val = float(ema_f.iloc[-1])
    ema_slow_val = float(ema_s.iloc[-1])
    vwap_val = float(vwap_s.iloc[-1])
    last_close = float(df["close"].iloc[-1])

    import math
    if any(math.isnan(v) for v in [ema_fast_val, ema_slow_val, vwap_val, last_close]):
        return TrendBias(
            bias="NONE",
            ema_fast=ema_fast_val,
            ema_slow=ema_slow_val,
            vwap_val=vwap_val,
            last_close=last_close,
            reason="NaN in indicators",
        )

    ema_long = ema_fast_val > ema_slow_val
    price_above_vwap = last_close > vwap_val

    ema_short = ema_fast_val < ema_slow_val
    price_below_vwap = last_close < vwap_val

    if ema_long and price_above_vwap:
        return TrendBias(
            bias="LONG",
            ema_fast=ema_fast_val,
            ema_slow=ema_slow_val,
            vwap_val=vwap_val,
            last_close=last_close,
            reason="EMA9>EMA21 and close above VWAP",
        )
    elif ema_short and price_below_vwap:
        return TrendBias(
            bias="SHORT",
            ema_fast=ema_fast_val,
            ema_slow=ema_slow_val,
            vwap_val=vwap_val,
            last_close=last_close,
            reason="EMA9<EMA21 and close below VWAP",
        )
    else:
        return TrendBias(
            bias="NONE",
            ema_fast=ema_fast_val,
            ema_slow=ema_slow_val,
            vwap_val=vwap_val,
            last_close=last_close,
            reason="EMA/VWAP conditions do not agree",
        )
