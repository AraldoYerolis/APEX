"""SWEEP_RECLAIM detector — pure, price-action-only, research-only.

This is a PRICE-ACTION sweep/reclaim around a prior confirmed structural
pivot (see features.py). APEX has no order-book/L2 depth data, so this is
explicitly not liquidity-sweep detection in the order-book sense — it only
ever looks at OHLC candle geometry.

LONG definition:
  1. A confirmed pivot LOW exists strictly before the trailing
     `sweep_lookback`-bar window (the "anchor" — the prior meaningful low).
  2. Within that trailing window, some bar's low trades through the anchor
     by at least `min_sweep_depth_pct` (the "sweep").
  3. At or after the sweep bar (still within the window), some bar's close
     reclaims back above the anchor price (the "reclaim").

SHORT is symmetric using a confirmed pivot HIGH and the reverse inequalities.

If a sweep occurs with no subsequent reclaim in the window ("false sweep"),
or no qualifying prior pivot exists, no finding is returned — this detector
never reports a partial/ambiguous state in v0.1.

No-lookahead: pivots come from features.find_pivots, which only confirms a
pivot once `pivot_right_bars` future bars exist after it, and the anchor is
additionally required to predate the sweep window entirely — so the anchor
can never be part of the sweep/reclaim event it anchors.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from apex.opportunity.contract import PRIMARY_TIMEFRAME_DURATION_MS, DetectorFinding, PrimaryTimeframe
from apex.opportunity.features import (
    DEFAULT_LEFT_BARS,
    DEFAULT_RIGHT_BARS,
    find_pivots,
    most_recent_pivot,
)
from apex.strategy.trend_filter import compute_trend_bias

DETECTOR_VERSION = "sweep_reclaim_v0_1"

# Bars checked for a sweep-then-reclaim sequence. Kept short (intraday) so a
# "prior structure" reading stays meaningfully separate from the event that
# tests it.
DEFAULT_SWEEP_LOOKBACK = 5
# The sweep must clear the anchor by at least this percentage to filter out
# float noise / exact-touch ties rather than a genuine break of structure.
DEFAULT_MIN_SWEEP_DEPTH_PCT = 0.02
MIN_CANDLES_REQUIRED = DEFAULT_LEFT_BARS + DEFAULT_RIGHT_BARS + DEFAULT_SWEEP_LOOKBACK + 1


def detect_sweep_reclaim(
    symbol: str,
    timeframe: PrimaryTimeframe,
    df: pd.DataFrame,
    *,
    pivot_left_bars: int = DEFAULT_LEFT_BARS,
    pivot_right_bars: int = DEFAULT_RIGHT_BARS,
    sweep_lookback: int = DEFAULT_SWEEP_LOOKBACK,
    min_sweep_depth_pct: float = DEFAULT_MIN_SWEEP_DEPTH_PCT,
    context_df_15m: Optional[pd.DataFrame] = None,
) -> list[DetectorFinding]:
    """Evaluate the trailing `sweep_lookback` bars of `df` for sweep+reclaim.

    `df` must have columns open/high/low/close/volume/open_time, oldest
    first, closed candles only. Returns 0, 1, or 2 findings (LONG and SHORT
    are checked independently).
    """
    n = len(df)
    if n < MIN_CANDLES_REQUIRED:
        return []

    pivots = find_pivots(df, left_bars=pivot_left_bars, right_bars=pivot_right_bars)
    sweep_window_start = n - sweep_lookback
    window = df.iloc[sweep_window_start:]

    context_evidence: dict = {}
    context_bias: Optional[str] = None
    if context_df_15m is not None and not context_df_15m.empty:
        context_bias = compute_trend_bias(context_df_15m).bias
        context_evidence["context_15m_trend_bias"] = context_bias

    findings: list[DetectorFinding] = []

    long_finding = _check_long(
        symbol, timeframe, df, window, pivots, sweep_window_start,
        min_sweep_depth_pct, pivot_left_bars, pivot_right_bars, sweep_lookback,
        context_bias, context_evidence,
    )
    if long_finding is not None:
        findings.append(long_finding)

    short_finding = _check_short(
        symbol, timeframe, df, window, pivots, sweep_window_start,
        min_sweep_depth_pct, pivot_left_bars, pivot_right_bars, sweep_lookback,
        context_bias, context_evidence,
    )
    if short_finding is not None:
        findings.append(short_finding)

    return findings


def _check_long(
    symbol, timeframe, df, window, pivots, sweep_window_start,
    min_sweep_depth_pct, pivot_left_bars, pivot_right_bars, sweep_lookback,
    context_bias, context_evidence,
) -> Optional[DetectorFinding]:
    anchor = most_recent_pivot(pivots, "LOW", before_index=sweep_window_start)
    if anchor is None:
        return None

    sweep_threshold = anchor.price * (1 - min_sweep_depth_pct / 100)
    lows = window["low"].to_numpy()
    closes = window["close"].to_numpy()

    sweep_pos = None
    for j in range(len(window)):
        if lows[j] < sweep_threshold:
            sweep_pos = j
            break
    if sweep_pos is None:
        return None

    reclaim_pos = None
    for k in range(sweep_pos, len(window)):
        if closes[k] > anchor.price:
            reclaim_pos = k
            break
    if reclaim_pos is None:
        return None  # false sweep — swept but never reclaimed

    sweep_row = window.iloc[sweep_pos]
    reclaim_row = window.iloc[reclaim_pos]
    sweep_depth_pct = (anchor.price - float(sweep_row["low"])) / anchor.price * 100
    reclaim_strength_pct = (float(reclaim_row["close"]) - anchor.price) / anchor.price * 100

    warnings: list[str] = []
    if context_bias not in (None, "NONE", "LONG"):
        warnings.append(f"15m trend bias ({context_bias}) conflicts with LONG sweep-reclaim")

    return DetectorFinding(
        symbol=symbol,
        direction="LONG",
        setup_family="SWEEP_RECLAIM",
        detector_version=DETECTOR_VERSION,
        primary_timeframe=timeframe,
        source_candle_open_time=int(reclaim_row["open_time"]),
        # CandleStore.get_df() does not expose the candle's real close_time
        # to detectors (see candle_store.py), so this is derived rather than
        # duplicating open_time under a misleading name (Correction 5).
        source_candle_close_time=int(reclaim_row["open_time"])
        + PRIMARY_TIMEFRAME_DURATION_MS[timeframe],
        # Anchored to the structural pivot's own open_time: as long as the
        # same prior pivot keeps anchoring re-detections within the trailing
        # window, this stays stable; a genuinely different (newer) pivot
        # produces a different fingerprint, i.e. a separate opportunity.
        fingerprint_key=str(anchor.open_time),
        anchor_price=anchor.price,
        anchor_open_time=anchor.open_time,
        evidence={
            "pivot_left_bars": pivot_left_bars,
            "pivot_right_bars": pivot_right_bars,
            "sweep_lookback": sweep_lookback,
            "min_sweep_depth_pct": min_sweep_depth_pct,
            **context_evidence,
        },
        warnings=warnings,
        measurements={
            "anchor_price": anchor.price,
            "anchor_open_time": anchor.open_time,
            "sweep_open_time": int(sweep_row["open_time"]),
            "sweep_low": float(sweep_row["low"]),
            "sweep_depth_pct": sweep_depth_pct,
            "reclaim_open_time": int(reclaim_row["open_time"]),
            "reclaim_close": float(reclaim_row["close"]),
            "reclaim_strength_pct": reclaim_strength_pct,
            "bars_between_sweep_and_reclaim": reclaim_pos - sweep_pos,
        },
    )


def _check_short(
    symbol, timeframe, df, window, pivots, sweep_window_start,
    min_sweep_depth_pct, pivot_left_bars, pivot_right_bars, sweep_lookback,
    context_bias, context_evidence,
) -> Optional[DetectorFinding]:
    anchor = most_recent_pivot(pivots, "HIGH", before_index=sweep_window_start)
    if anchor is None:
        return None

    sweep_threshold = anchor.price * (1 + min_sweep_depth_pct / 100)
    highs = window["high"].to_numpy()
    closes = window["close"].to_numpy()

    sweep_pos = None
    for j in range(len(window)):
        if highs[j] > sweep_threshold:
            sweep_pos = j
            break
    if sweep_pos is None:
        return None

    reclaim_pos = None
    for k in range(sweep_pos, len(window)):
        if closes[k] < anchor.price:
            reclaim_pos = k
            break
    if reclaim_pos is None:
        return None  # false sweep — swept but never reclaimed

    sweep_row = window.iloc[sweep_pos]
    reclaim_row = window.iloc[reclaim_pos]
    sweep_depth_pct = (float(sweep_row["high"]) - anchor.price) / anchor.price * 100
    reclaim_strength_pct = (anchor.price - float(reclaim_row["close"])) / anchor.price * 100

    warnings: list[str] = []
    if context_bias not in (None, "NONE", "SHORT"):
        warnings.append(f"15m trend bias ({context_bias}) conflicts with SHORT sweep-reclaim")

    return DetectorFinding(
        symbol=symbol,
        direction="SHORT",
        setup_family="SWEEP_RECLAIM",
        detector_version=DETECTOR_VERSION,
        primary_timeframe=timeframe,
        source_candle_open_time=int(reclaim_row["open_time"]),
        # CandleStore.get_df() does not expose the candle's real close_time
        # to detectors (see candle_store.py), so this is derived rather than
        # duplicating open_time under a misleading name (Correction 5).
        source_candle_close_time=int(reclaim_row["open_time"])
        + PRIMARY_TIMEFRAME_DURATION_MS[timeframe],
        fingerprint_key=str(anchor.open_time),
        anchor_price=anchor.price,
        anchor_open_time=anchor.open_time,
        evidence={
            "pivot_left_bars": pivot_left_bars,
            "pivot_right_bars": pivot_right_bars,
            "sweep_lookback": sweep_lookback,
            "min_sweep_depth_pct": min_sweep_depth_pct,
            **context_evidence,
        },
        warnings=warnings,
        measurements={
            "anchor_price": anchor.price,
            "anchor_open_time": anchor.open_time,
            "sweep_open_time": int(sweep_row["open_time"]),
            "sweep_high": float(sweep_row["high"]),
            "sweep_depth_pct": sweep_depth_pct,
            "reclaim_open_time": int(reclaim_row["open_time"]),
            "reclaim_close": float(reclaim_row["close"]),
            "reclaim_strength_pct": reclaim_strength_pct,
            "bars_between_sweep_and_reclaim": reclaim_pos - sweep_pos,
        },
    )
