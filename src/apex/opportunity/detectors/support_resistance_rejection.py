"""SUPPORT_RESISTANCE_REJECTION detector — pure, price-action-only,
research-only.

Evaluates only the latest supplied closed candle (`src`) against confirmed
Support/Resistance zones (see `support_resistance.find_support_resistance_zones`)
built from every candle in the bounded trailing window *except* `src` itself.
A rejection is a SHORT when price approaches a zone from below, pokes at or
into it, and closes back below it on the same candle; LONG is the symmetric
case approaching a zone from above. The rejection-range ratio measures how
much of the candle's total high-low range separates the extreme (the poke)
from the close (the rejection) — this intentionally includes body movement,
not just a wick, so it is not called a "wick ratio".

No-lookahead: `zone_df` (used to build the zones) excludes `src` entirely, so
the candidate candle can never influence the pivot confirmation, tolerance,
clustering, or zone selection that it is then evaluated against. This
function assumes supplied rows are already fully closed, as all existing
pure detectors do; it does not read the clock.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from apex.opportunity.contract import (
    PRIMARY_TIMEFRAME_DURATION_MS,
    Direction,
    DetectorFinding,
    PrimaryTimeframe,
)
from apex.opportunity.support_resistance import (
    LOOKBACK_CANDLES,
    MIN_ROWS_REQUIRED as SUPPORT_RESISTANCE_MIN_ROWS_REQUIRED,
    SupportResistanceZone,
    find_support_resistance_zones,
)

DETECTOR_VERSION = "support_resistance_rejection_v0_1"

# The rejection-range ratio's minimum fraction of the candle's high-low range
# that must separate the poked extreme from the close, i.e. how decisively
# the candle reversed away from the zone rather than merely grazing it. A
# conservative first-pass threshold, not tuned against outcome data (no
# outcome data exists yet for this detector).
DEFAULT_MIN_REJECTION_RANGE_RATIO = 0.33

# One extra row beyond the S/R primitive's own minimum: that extra row is
# `src`, the candidate rejection candle, which is deliberately excluded from
# `zone_df` before zones are built (see module docstring).
MIN_CANDLES_REQUIRED = SUPPORT_RESISTANCE_MIN_ROWS_REQUIRED + 1

REQUIRED_COLUMNS = ("open", "high", "low", "close", "open_time")


def detect_support_resistance_rejection(
    symbol: str,
    timeframe: PrimaryTimeframe,
    df: pd.DataFrame,
    *,
    min_rejection_range_ratio: float = DEFAULT_MIN_REJECTION_RANGE_RATIO,
) -> list[DetectorFinding]:
    """Evaluate the latest closed bar of `df` for a Support/Resistance
    rejection.

    `df` must have columns open/high/low/close/open_time, oldest first,
    closed candles only (the shape CandleStore.get_df returns). Returns 0 or
    1 findings; a candle that qualifies for both LONG and SHORT is
    directionally ambiguous and withheld (see module docstring / sealed
    design).
    """
    if df is None or not isinstance(df, pd.DataFrame):
        return []
    if not isinstance(symbol, str) or not symbol.strip():
        return []
    if not isinstance(timeframe, str) or timeframe not in PRIMARY_TIMEFRAME_DURATION_MS:
        return []

    try:
        ratio_threshold = float(min_rejection_range_ratio)
    except (TypeError, ValueError):
        return []
    if not math.isfinite(ratio_threshold) or ratio_threshold < 0.0 or ratio_threshold > 1.0:
        return []

    if len(df) < MIN_CANDLES_REQUIRED:
        return []
    for column in REQUIRED_COLUMNS:
        if column not in df.columns:
            return []
    # Duplicate required column labels would make e.g. src["close"] a Series
    # instead of a scalar, raising downstream in float(...); reject before
    # any scalar extraction is attempted.
    if df.columns[df.columns.isin(REQUIRED_COLUMNS)].duplicated().any():
        return []

    window = df.tail(LOOKBACK_CANDLES + 1).reset_index(drop=True)
    if len(window) < MIN_CANDLES_REQUIRED:
        return []
    if not _validate_window(window):
        return []

    src = window.iloc[-1]
    prev = window.iloc[-2]
    zone_df = window.iloc[:-1]

    zones = find_support_resistance_zones(zone_df, timeframe)
    if not zones:
        return []

    src_high = float(src["high"])
    src_low = float(src["low"])
    src_close = float(src["close"])
    prev_close = float(prev["close"])
    src_open_time = int(src["open_time"])

    if not src_high > src_low:
        return []
    src_range = src_high - src_low

    short_candidates: list[tuple[SupportResistanceZone, float]] = []
    long_candidates: list[tuple[SupportResistanceZone, float]] = []

    for zone in zones:
        if zone.lower <= 0 or zone.upper <= 0:
            # _build_finding divides by the selected boundary (zone.lower for
            # SHORT, zone.upper for LONG). Real market zones can't be
            # non-positive, but a malformed/monkeypatched zone must fail
            # closed here rather than raise ZeroDivisionError there.
            continue

        if prev_close < zone.lower and src_high >= zone.lower and src_close < zone.lower:
            ratio_val = (src_high - src_close) / src_range
            if ratio_val >= ratio_threshold:
                short_candidates.append((zone, ratio_val))

        if prev_close > zone.upper and src_low <= zone.upper and src_close > zone.upper:
            ratio_val = (src_close - src_low) / src_range
            if ratio_val >= ratio_threshold:
                long_candidates.append((zone, ratio_val))

    if short_candidates and long_candidates:
        # Contradictory dual-direction qualification on the same source
        # candle — fail closed rather than guess (sealed design).
        return []

    if short_candidates:
        zone, ratio_val = min(
            short_candidates,
            key=lambda pair: (
                pair[0].lower - prev_close,
                pair[0].center,
                pair[0].first_touch_open_time,
                pair[0].most_recent_touch_open_time,
            ),
        )
        return [
            _build_finding(
                symbol=symbol,
                timeframe=timeframe,
                direction="SHORT",
                zone=zone,
                ratio_threshold=ratio_threshold,
                ratio_val=ratio_val,
                src_open_time=src_open_time,
                src_high=src_high,
                src_low=src_low,
                src_close=src_close,
                src_range=src_range,
                prev_close=prev_close,
            )
        ]

    if long_candidates:
        zone, ratio_val = min(
            long_candidates,
            key=lambda pair: (
                prev_close - pair[0].upper,
                pair[0].center,
                pair[0].first_touch_open_time,
                pair[0].most_recent_touch_open_time,
            ),
        )
        return [
            _build_finding(
                symbol=symbol,
                timeframe=timeframe,
                direction="LONG",
                zone=zone,
                ratio_threshold=ratio_threshold,
                ratio_val=ratio_val,
                src_open_time=src_open_time,
                src_high=src_high,
                src_low=src_low,
                src_close=src_close,
                src_range=src_range,
                prev_close=prev_close,
            )
        ]

    return []


def _validate_window(window: pd.DataFrame) -> bool:
    """Bounded validation over the whole trailing `window` (the candidate
    candle plus every candle zones will be built from). Malformed rows
    outside this window are never inspected and therefore never matter —
    only `df.tail(LOOKBACK_CANDLES + 1)` is ever read.
    """
    try:
        opens = window["open"].to_numpy(dtype=np.float64)
        highs = window["high"].to_numpy(dtype=np.float64)
        lows = window["low"].to_numpy(dtype=np.float64)
        closes = window["close"].to_numpy(dtype=np.float64)
        open_times = window["open_time"].to_numpy(dtype=np.float64)
    except (TypeError, ValueError):
        return False

    for arr in (opens, highs, lows, closes, open_times):
        if not np.all(np.isfinite(arr)):
            return False

    for arr in (opens, highs, lows, closes):
        if not np.all(arr > 0):
            return False

    if not np.all(open_times == np.floor(open_times)):
        return False
    if len(open_times) > 1 and not np.all(np.diff(open_times) > 0):
        return False

    if not np.all(lows <= opens) or not np.all(opens <= highs):
        return False
    if not np.all(lows <= closes) or not np.all(closes <= highs):
        return False

    return True


def _build_finding(
    *,
    symbol: str,
    timeframe: PrimaryTimeframe,
    direction: Direction,
    zone: SupportResistanceZone,
    ratio_threshold: float,
    ratio_val: float,
    src_open_time: int,
    src_high: float,
    src_low: float,
    src_close: float,
    src_range: float,
    prev_close: float,
) -> DetectorFinding:
    if direction == "SHORT":
        anchor_price = zone.lower
        source_extreme = src_high
        penetration_depth_pct = (src_high - zone.lower) / zone.lower * 100.0
        rejection_distance_pct = (zone.lower - src_close) / zone.lower * 100.0
    else:
        anchor_price = zone.upper
        source_extreme = src_low
        penetration_depth_pct = (zone.upper - src_low) / zone.upper * 100.0
        rejection_distance_pct = (src_close - zone.upper) / zone.upper * 100.0

    pivot_open_times = [int(t) for t in zone.pivot_open_times]

    evidence = {
        "min_rejection_range_ratio": ratio_threshold,
        "zone_center": zone.center,
        "zone_lower": zone.lower,
        "zone_upper": zone.upper,
        "zone_contributing_pivot_count": zone.contributing_pivot_count,
        "zone_pivot_open_times": pivot_open_times,
        "zone_first_touch_open_time": zone.first_touch_open_time,
        "zone_most_recent_touch_open_time": zone.most_recent_touch_open_time,
        "prior_close": prev_close,
        "source_extreme": source_extreme,
        "source_close": src_close,
        "rejection_range_ratio": ratio_val,
    }

    measurements = {
        "anchor_price": anchor_price,
        "anchor_open_time": zone.first_touch_open_time,
        "penetration_depth_pct": penetration_depth_pct,
        "rejection_distance_pct": rejection_distance_pct,
        "rejection_range_ratio": ratio_val,
        "source_candle_range": src_range,
    }

    return DetectorFinding(
        symbol=symbol,
        direction=direction,
        setup_family="SUPPORT_RESISTANCE_REJECTION",
        detector_version=DETECTOR_VERSION,
        primary_timeframe=timeframe,
        source_candle_open_time=src_open_time,
        # CandleStore.get_df() does not expose the candle's real close_time
        # to detectors, so this is derived rather than duplicating
        # open_time under a misleading name (see sweep_reclaim.py /
        # volatility_compression.py for the same pattern).
        source_candle_close_time=src_open_time + PRIMARY_TIMEFRAME_DURATION_MS[timeframe],
        # Event-based key: stable on repeated evaluation of identical input,
        # and a later rejection candle at the same zone (different
        # source_open_time) gets a new identity instead of colliding with
        # this one.
        fingerprint_key=f"{zone.first_touch_open_time}:{src_open_time}",
        anchor_price=anchor_price,
        anchor_open_time=zone.first_touch_open_time,
        evidence=evidence,
        warnings=[],
        measurements=measurements,
    )
