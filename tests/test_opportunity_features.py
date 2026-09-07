"""Tests for the pivot/swing structure primitive (TA Opportunity Engine v0.1).

Covers: pivot high/low detection, no-lookahead / no-repaint semantics, and
most_recent_pivot filtering.
"""
from __future__ import annotations

import pandas as pd

from apex.opportunity.features import find_pivots, most_recent_pivot


def _make_df(closes, highs=None, lows=None):
    n = len(closes)
    highs = highs or [c + 0.5 for c in closes]
    lows = lows or [c - 0.5 for c in closes]
    return pd.DataFrame({
        "open": closes,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [1000.0] * n,
        "open_time": list(range(n)),
    })


def _flat_with_spike(n: int, spike_index: int, spike_high: float, spike_low: float):
    closes = [100.0] * n
    highs = [100.5] * n
    lows = [99.5] * n
    highs[spike_index] = spike_high
    lows[spike_index] = spike_low
    return _make_df(closes, highs, lows)


# ------------------------------------------------------------------ pivot detection

def test_find_pivots_detects_high_and_low():
    n = 20
    df = _flat_with_spike(n, spike_index=10, spike_high=110.0, spike_low=99.5)
    pivots = find_pivots(df, left_bars=3, right_bars=3)
    highs = [p for p in pivots if p.kind == "HIGH"]
    assert any(p.index == 10 and p.price == 110.0 for p in highs)


def test_find_pivots_detects_low():
    n = 20
    df = _flat_with_spike(n, spike_index=10, spike_high=100.5, spike_low=90.0)
    pivots = find_pivots(df, left_bars=3, right_bars=3)
    lows = [p for p in pivots if p.kind == "LOW"]
    assert any(p.index == 10 and p.price == 90.0 for p in lows)


def test_find_pivots_too_short_returns_empty():
    df = _make_df([100.0] * 5)
    assert find_pivots(df, left_bars=3, right_bars=3) == []


def test_find_pivots_flat_series_no_spurious_pivots():
    df = _make_df([100.0] * 20)
    pivots = find_pivots(df, left_bars=3, right_bars=3)
    # Every bar ties for max/min in its window; tie-break-to-earliest means
    # only the leftmost eligible bar registers, not one pivot per position.
    assert len(pivots) <= 2


# ------------------------------------------------------------------ no-lookahead / no-repaint

def test_pivot_not_confirmed_until_right_bars_exist():
    """A spike within the last `right_bars` candles must not be reported yet."""
    n = 20
    spike_index = n - 2  # only 1 bar exists after it; right_bars=3 requires 3
    df = _flat_with_spike(n, spike_index=spike_index, spike_high=110.0, spike_low=99.5)
    pivots = find_pivots(df, left_bars=3, right_bars=3)
    assert not any(p.index == spike_index for p in pivots)


def test_confirmed_pivots_do_not_repaint_as_more_candles_arrive():
    """Once confirmed, a pivot's presence/price/index must never change."""
    n = 20
    df = _flat_with_spike(n, spike_index=10, spike_high=110.0, spike_low=99.5)
    pivots_before = find_pivots(df, left_bars=3, right_bars=3)
    before = {(p.kind, p.index, p.price) for p in pivots_before}
    assert ("HIGH", 10, 110.0) in before

    # Extend with 10 more ordinary candles — the earlier pivot must still be
    # reported identically, and running the same pure function again must
    # reproduce it rather than mutate/drop it.
    extra = _make_df([100.0] * 10)
    extra["open_time"] = range(n, n + 10)
    extended_df = pd.concat([df, extra], ignore_index=True)

    pivots_after = find_pivots(extended_df, left_bars=3, right_bars=3)
    after = {(p.kind, p.index, p.price) for p in pivots_after}
    assert ("HIGH", 10, 110.0) in after


# ------------------------------------------------------------------ most_recent_pivot

def test_most_recent_pivot_returns_highest_index():
    n = 30
    df = _make_df([100.0] * n)
    highs = df["high"].tolist()
    lows = df["low"].tolist()
    highs[10] = 110.0
    highs[20] = 120.0
    df = _make_df(df["close"].tolist(), highs, lows)

    pivots = find_pivots(df, left_bars=3, right_bars=3)
    latest = most_recent_pivot(pivots, "HIGH")
    assert latest is not None
    assert latest.index == 20


def test_most_recent_pivot_respects_before_index():
    n = 30
    df = _make_df([100.0] * n)
    highs = df["high"].tolist()
    lows = df["low"].tolist()
    highs[10] = 110.0
    highs[20] = 120.0
    df = _make_df(df["close"].tolist(), highs, lows)

    pivots = find_pivots(df, left_bars=3, right_bars=3)
    anchor = most_recent_pivot(pivots, "HIGH", before_index=15)
    assert anchor is not None
    assert anchor.index == 10


def test_most_recent_pivot_none_when_no_match():
    df = _make_df([100.0] * 20)
    pivots = find_pivots(df, left_bars=3, right_bars=3)
    assert most_recent_pivot(pivots, "HIGH", before_index=0) is None
