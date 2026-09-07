"""Minimal reusable pivot/swing structure primitive.

The runtime TREND_PULLBACK strategy's `_swing_low`/`_swing_high`
(pullback_strategy.py) is a plain rolling 5-candle tail min/max — enough for
a stop-loss distance, but not a generalized market-structure model: it has
no notion of a confirmed local extreme, so it cannot anchor SWEEP_RECLAIM or
future S/R detectors. This module implements a small, deterministic
fractal-style pivot detector instead, and nothing more — a full clustering/
S/R framework is out of scope for v0.1 (see CLAUDE.md milestone spec C).

No-lookahead guarantee: a bar at index i can only be confirmed as a pivot
once `right_bars` additional closed candles exist after it (i.e. i must
satisfy i <= len(df) - right_bars - 1). Because this function is pure and
recomputes pivots fresh from whatever candles are passed in, the last
`right_bars` candles of any df can never yield a pivot yet, and re-running
it later on an extended df reproduces the exact same already-confirmed
pivots — nothing already reported ever changes retroactively (no repaint).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

import numpy as np
import pandas as pd

PivotKind = Literal["HIGH", "LOW"]

# 3 bars each side is the smallest window that filters single-candle noise
# while keeping the confirmation lag usable on intraday timeframes: a
# 3-bar-right lag is 9-15 minutes on 3m candles, 15-25 minutes on 5m.
DEFAULT_LEFT_BARS = 3
DEFAULT_RIGHT_BARS = 3


@dataclass(frozen=True)
class Pivot:
    kind: PivotKind
    index: int  # positional index into the source DataFrame
    open_time: int
    price: float


def find_pivots(
    df: pd.DataFrame,
    left_bars: int = DEFAULT_LEFT_BARS,
    right_bars: int = DEFAULT_RIGHT_BARS,
) -> list[Pivot]:
    """Return confirmed swing pivots in `df` (oldest-first, closed candles).

    A bar at index i is a pivot HIGH when df.high[i] is the maximum over the
    window [i-left_bars, i+right_bars], tie-broken to the earliest bar in
    the window (so a flat top registers exactly one pivot, not one per tied
    bar). Pivot LOW is symmetric over df.low using the minimum.
    """
    n = len(df)
    pivots: list[Pivot] = []
    window_size = left_bars + right_bars + 1
    if n < window_size:
        return pivots

    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    open_times = df["open_time"].to_numpy()

    for i in range(left_bars, n - right_bars):
        window_hi = highs[i - left_bars : i + right_bars + 1]
        if int(np.argmax(window_hi)) == left_bars:
            pivots.append(
                Pivot(kind="HIGH", index=i, open_time=int(open_times[i]), price=float(highs[i]))
            )

        window_lo = lows[i - left_bars : i + right_bars + 1]
        if int(np.argmin(window_lo)) == left_bars:
            pivots.append(
                Pivot(kind="LOW", index=i, open_time=int(open_times[i]), price=float(lows[i]))
            )

    return pivots


def most_recent_pivot(
    pivots: list[Pivot],
    kind: PivotKind,
    *,
    before_index: Optional[int] = None,
) -> Optional[Pivot]:
    """Return the highest-index pivot of `kind`, optionally strictly before `before_index`."""
    candidates = [
        p for p in pivots if p.kind == kind and (before_index is None or p.index < before_index)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.index)
