"""SUPPORT_RESISTANCE_BREAKOUT_RETEST detector — pure, price-action-only,
research-only.

Evaluates only the latest supplied closed candle (`source`) against confirmed
Support/Resistance zones (see
`support_resistance.find_support_resistance_zones`) built from every candle
in the bounded trailing window *except* the reserved event tail. A LONG
breakout retest is: price begins strictly below a zone, some later candle
closes strictly above it (the breakout), the breakout is not lost before the
source candle, the source candle touches or penetrates the broken zone
without trading through its far side, and the source candle's own close
holds strictly above the broken boundary. SHORT is the exact mirror. The
source candle is both the retest and hold confirmation — a separate later
confirmation candle is deliberately omitted from v0.1, since a fully closed
candle that touches the broken zone and closes back beyond its broken
boundary already proves the bounded price-action claim.

No-lookahead: the entire possible breakout/retest event tail
(`event_window_candles = max_retest_bars + 1` rows) is reserved *before*
`zone_df` is built, so no possible breakout or source candle can influence
pivot discovery, pivot-local tolerance, clustering, zone boundaries, touch
identity, or role. The row immediately before the earliest possible
breakout may remain in `zone_df` — it is context before the event, not part
of the event. This function assumes supplied rows are already fully closed,
as all existing pure detectors do; it does not read the clock.
"""
from __future__ import annotations

import math
from typing import NamedTuple, Optional

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

DETECTOR_VERSION = "support_resistance_breakout_retest_v0_1"

# An explicitly untuned, bounded intraday parameter. Five follows the
# existing five-bar sweep/reclaim event bound (see sweep_reclaim.py's
# DEFAULT_SWEEP_LOOKBACK) — no outcome data exists yet for this detector.
DEFAULT_MAX_RETEST_BARS = 5

# The reserved event tail: the breakout candidates plus the single source
# (retest/hold) candle.
EVENT_WINDOW_CANDLES = DEFAULT_MAX_RETEST_BARS + 1

# One S/R-primitive minimum window plus the reserved event tail — that tail
# is deliberately excluded from `zone_df` before zones are built (see module
# docstring).
MIN_CANDLES_REQUIRED = SUPPORT_RESISTANCE_MIN_ROWS_REQUIRED + EVENT_WINDOW_CANDLES

REQUIRED_COLUMNS = ("open", "high", "low", "close", "open_time")


def detect_support_resistance_breakout_retest(
    symbol: str,
    timeframe: PrimaryTimeframe,
    df: pd.DataFrame,
    *,
    max_retest_bars: int = DEFAULT_MAX_RETEST_BARS,
) -> list[DetectorFinding]:
    """Evaluate the latest closed bar of `df` for a Support/Resistance
    breakout retest.

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

    # bool is an int subclass; excluded explicitly. Non-int (e.g. a float,
    # even an integral-valued one) is rejected outright rather than coerced.
    if isinstance(max_retest_bars, bool) or not isinstance(max_retest_bars, int):
        return []
    if max_retest_bars < 1 or max_retest_bars > LOOKBACK_CANDLES:
        return []
    n = max_retest_bars
    event_window_candles = n + 1
    required_rows = SUPPORT_RESISTANCE_MIN_ROWS_REQUIRED + event_window_candles

    if len(df) < required_rows:
        return []
    for column in REQUIRED_COLUMNS:
        if column not in df.columns:
            return []
    # Duplicate required column labels would make e.g. df["close"] a
    # DataFrame instead of a Series, raising downstream in float(...);
    # reject before any scalar extraction is attempted.
    if df.columns[df.columns.isin(REQUIRED_COLUMNS)].duplicated().any():
        return []

    window = df.tail(LOOKBACK_CANDLES + event_window_candles).reset_index(drop=True)
    if len(window) < required_rows:
        return []
    if not _validate_window(window):
        return []

    zone_df = window.iloc[:-event_window_candles]
    event_df = window.iloc[-event_window_candles:]

    zones = find_support_resistance_zones(zone_df, timeframe)
    if not isinstance(zones, tuple):
        # Malformed/monkeypatched primitive output — fail closed rather than
        # allow a partial candidate set from an unexpected shape.
        return []
    if not zones:
        return []
    normalized_zones: list[_NormalizedZone] = []
    for zone in zones:
        normalized = _validate_zone(zone, timeframe)
        if normalized is None:
            # A single malformed returned zone withholds the whole scan
            # rather than allowing a partial candidate set (sealed design).
            return []
        normalized_zones.append(normalized)

    long_candidates, short_candidates = _collect_candidates(normalized_zones, zone_df, event_df, n)

    if long_candidates and short_candidates:
        # Contradictory dual-direction qualification — fail closed rather
        # than guess (sealed design).
        return []

    if long_candidates:
        chosen = min(long_candidates, key=lambda c: c[0])
        return [_build_finding("LONG", symbol, timeframe, event_df, chosen, n)]

    if short_candidates:
        chosen = min(short_candidates, key=lambda c: c[0])
        return [_build_finding("SHORT", symbol, timeframe, event_df, chosen, n)]

    return []


def _validate_window(window: pd.DataFrame) -> bool:
    """Bounded validation over the whole trailing `window` (every candle
    zones will be built from, plus the reserved event tail). Malformed rows
    outside this window are never inspected and therefore never matter —
    only `df.tail(LOOKBACK_CANDLES + event_window_candles)` is ever read.
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


class _NormalizedZone(NamedTuple):
    """A returned `SupportResistanceZone`'s fields, revalidated and
    normalized to plain Python `int`/`float` values (see sealed design's
    normalization requirement). Everything downstream — candidate keys,
    evidence, measurements, anchor, and fingerprint — is built from this
    normalized shape rather than the raw returned zone, so a monkeypatched
    or exotic-numeric-typed primitive can never leak a NumPy scalar, bool,
    or other non-plain value into a `DetectorFinding`.
    """

    lower: float
    upper: float
    center: float
    contributing_pivot_count: int
    pivot_open_times: tuple[int, ...]
    first_touch_open_time: int
    most_recent_touch_open_time: int


def _as_plain_int(value: object) -> Optional[int]:
    """Return `value` as a plain Python `int` only if it is a genuine
    integer scalar (Python `int` or NumPy integer). `bool` is an `int`
    subclass and is explicitly excluded; fractional, string, and other
    types are rejected rather than coerced.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, np.integer)):
        return int(value)
    return None


def _validate_zone(zone: SupportResistanceZone, timeframe: str) -> Optional[_NormalizedZone]:
    """Validate a single returned zone's shape/geometry before any
    comparison or division uses it, and return it normalized to plain
    Python values (or `None` if invalid). See module docstring / sealed
    design.
    """
    try:
        lower = float(zone.lower)
        upper = float(zone.upper)
        center = float(zone.center)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lower) and math.isfinite(upper) and math.isfinite(center)):
        return None
    if lower <= 0 or upper <= 0 or center <= 0:
        return None
    if lower > center or center > upper:
        return None
    if zone.timeframe != timeframe:
        return None

    contributing_pivot_count = _as_plain_int(zone.contributing_pivot_count)
    if contributing_pivot_count is None or contributing_pivot_count < 2:
        return None

    pivot_open_times_raw = zone.pivot_open_times
    if not isinstance(pivot_open_times_raw, tuple) or len(pivot_open_times_raw) < 2:
        return None
    pivot_times: list[int] = []
    for raw_time in pivot_open_times_raw:
        pivot_time = _as_plain_int(raw_time)
        if pivot_time is None:
            return None
        pivot_times.append(pivot_time)
    if contributing_pivot_count != len(pivot_times):
        return None
    for earlier, later in zip(pivot_times, pivot_times[1:]):
        if not later > earlier:
            return None

    first_touch_open_time = _as_plain_int(zone.first_touch_open_time)
    most_recent_touch_open_time = _as_plain_int(zone.most_recent_touch_open_time)
    if first_touch_open_time is None or most_recent_touch_open_time is None:
        return None
    if first_touch_open_time != pivot_times[0]:
        return None
    if most_recent_touch_open_time != pivot_times[-1]:
        return None

    return _NormalizedZone(
        lower=lower,
        upper=upper,
        center=center,
        contributing_pivot_count=contributing_pivot_count,
        pivot_open_times=tuple(pivot_times),
        first_touch_open_time=first_touch_open_time,
        most_recent_touch_open_time=most_recent_touch_open_time,
    )


def _collect_candidates(
    zones: list[_NormalizedZone],
    zone_df: pd.DataFrame,
    event_df: pd.DataFrame,
    n: int,
) -> tuple[list[tuple], list[tuple]]:
    """Collect every valid LONG/SHORT breakout-retest candidate across every
    zone and every possible breakout position within the reserved event
    tail. Each candidate carries its deterministic selection key as element
    0, per the sealed design's minimum-key selection rule. Breakout
    positions are walked newest-to-oldest per the sealed design; the
    fully-ordered selection key (not iteration order) determines which
    candidate is ultimately chosen, so this ordering does not change output.
    """
    source = event_df.iloc[-1]
    source_low = float(source["low"])
    source_high = float(source["high"])
    source_close = float(source["close"])

    long_candidates: list[tuple] = []
    short_candidates: list[tuple] = []

    for zone in zones:
        zone_lower = zone.lower
        zone_upper = zone.upper

        for i in range(n - 1, -1, -1):
            b = event_df.iloc[i]
            p = zone_df.iloc[-1] if i == 0 else event_df.iloc[i - 1]

            b_close = float(b["close"])
            p_close = float(p["close"])
            breakout_open_time = int(b["open_time"])
            bars_since_breakout = n - i

            intermediates = event_df.iloc[i + 1 : n]
            intermediate_closes = intermediates["close"].to_numpy(dtype=np.float64)

            if (
                p_close < zone_lower
                and b_close > zone_upper
                and (intermediate_closes.size == 0 or np.all(intermediate_closes > zone_upper))
                and zone_lower <= source_low <= zone_upper
                and source_close > zone_upper
            ):
                approach_distance = zone_lower - p_close
                key = (
                    bars_since_breakout,
                    approach_distance,
                    zone.center,
                    zone.first_touch_open_time,
                    zone.most_recent_touch_open_time,
                    breakout_open_time,
                )
                long_candidates.append(
                    (key, zone, breakout_open_time, b_close, p_close, bars_since_breakout)
                )

            if (
                p_close > zone_upper
                and b_close < zone_lower
                and (intermediate_closes.size == 0 or np.all(intermediate_closes < zone_lower))
                and zone_lower <= source_high <= zone_upper
                and source_close < zone_lower
            ):
                approach_distance = p_close - zone_upper
                key = (
                    bars_since_breakout,
                    approach_distance,
                    zone.center,
                    zone.first_touch_open_time,
                    zone.most_recent_touch_open_time,
                    breakout_open_time,
                )
                short_candidates.append(
                    (key, zone, breakout_open_time, b_close, p_close, bars_since_breakout)
                )

    return long_candidates, short_candidates


def _build_finding(
    direction: Direction,
    symbol: str,
    timeframe: PrimaryTimeframe,
    event_df: pd.DataFrame,
    candidate: tuple,
    n: int,
) -> DetectorFinding:
    _, zone, breakout_open_time, breakout_close, prior_close, bars_since_breakout = candidate

    source = event_df.iloc[-1]
    source_open_time = int(source["open_time"])
    source_low = float(source["low"])
    source_high = float(source["high"])
    source_close = float(source["close"])
    source_range = source_high - source_low

    if direction == "LONG":
        anchor_price = zone.upper
        source_extreme = source_low
        breakout_distance_pct = (breakout_close - zone.upper) / zone.upper * 100.0
        retest_penetration_pct = (zone.upper - source_low) / zone.upper * 100.0
        hold_distance_pct = (source_close - zone.upper) / zone.upper * 100.0
    else:
        anchor_price = zone.lower
        source_extreme = source_high
        breakout_distance_pct = (zone.lower - breakout_close) / zone.lower * 100.0
        retest_penetration_pct = (source_high - zone.lower) / zone.lower * 100.0
        hold_distance_pct = (zone.lower - source_close) / zone.lower * 100.0

    pivot_open_times = list(zone.pivot_open_times)

    evidence = {
        "max_retest_bars": n,
        "zone_center": zone.center,
        "zone_lower": zone.lower,
        "zone_upper": zone.upper,
        "zone_contributing_pivot_count": zone.contributing_pivot_count,
        "zone_pivot_open_times": pivot_open_times,
        "zone_first_touch_open_time": zone.first_touch_open_time,
        "zone_most_recent_touch_open_time": zone.most_recent_touch_open_time,
        "prior_close_before_breakout": prior_close,
        "breakout_open_time": breakout_open_time,
        "breakout_close": breakout_close,
        "source_open_time": source_open_time,
        "source_extreme": source_extreme,
        "source_close": source_close,
        "bars_since_breakout": bars_since_breakout,
    }

    measurements = {
        "anchor_price": anchor_price,
        "anchor_open_time": zone.first_touch_open_time,
        "breakout_distance_pct": breakout_distance_pct,
        "retest_penetration_pct": retest_penetration_pct,
        "hold_distance_pct": hold_distance_pct,
        "bars_since_breakout": bars_since_breakout,
        "source_candle_range": source_range,
    }

    return DetectorFinding(
        symbol=symbol,
        direction=direction,
        setup_family="SUPPORT_RESISTANCE_BREAKOUT_RETEST",
        detector_version=DETECTOR_VERSION,
        primary_timeframe=timeframe,
        source_candle_open_time=source_open_time,
        # CandleStore.get_df() does not expose the candle's real close_time
        # to detectors (see candle_store.py), so this is derived rather than
        # duplicating open_time under a misleading name (same pattern as
        # sweep_reclaim.py / support_resistance_rejection.py).
        source_candle_close_time=source_open_time + PRIMARY_TIMEFRAME_DURATION_MS[timeframe],
        # Event-based key: stable on repeated evaluation of identical input;
        # a later retest at the same zone/breakout (different
        # source_open_time) gets a new identity instead of colliding with
        # this one.
        fingerprint_key=f"{zone.first_touch_open_time}:{breakout_open_time}:{source_open_time}",
        anchor_price=anchor_price,
        anchor_open_time=zone.first_touch_open_time,
        evidence=evidence,
        warnings=[],
        measurements=measurements,
    )
