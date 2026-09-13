"""Support / Resistance zone primitive — pure, research-only.

Clusters confirmed structural pivots (see `features.find_pivots`) into
horizontal price zones. Nothing in this module is wired into the engine,
detectors, or any persistence/notification path in this milestone; it is a
standalone, importable primitive for later research.

Two repaint hazards drove the design choices below, both raised against an
earlier draft that used a single shared, "now"-anchored ATR:

  - A pivot's tolerance must never change once confirmed, or an
    already-reported zone's boundaries could silently shift as unrelated
    later candles arrive. This module therefore computes each pivot's
    tolerance from a trailing true-range window ending AT that pivot (never
    a shared "current" ATR), and reuses `indicators.atr.true_range` for the
    per-bar true range only — never the EWM-recursive `atr()`, whose
    recursion origin shifts whenever the buffer's front edge trims, which
    would perturb even an already-confirmed pivot's tolerance.
  - Greedy single-link clustering lets a long, gradually-drifting chain of
    "close enough to their neighbor" pivots merge into one unbounded zone.
    This module uses complete-link clustering instead (a candidate must be
    within tolerance of every existing member, not just the nearest one),
    which bounds a zone's total width by its tightest contributing pivot's
    tolerance.

`LOOKBACK_CANDLES`, `ATR_PERIOD`, `ATR_MULTIPLIER`, `MIN_TOLERANCE_PCT`, and
`MIN_PIVOT_TOUCHES` are conservative first-pass structural parameters. They
have not been tuned against outcome data — no outcome data exists yet for
this primitive.

No-lookahead / no-repaint: pivots come from `features.find_pivots`, so the
same closed-candle confirmation lag applies here. Appending new candles can
only extend or reorganize a zone with newly confirmed evidence; a still-
in-window confirmed pivot's price, open_time, and tolerance never change.
As the front of the bounded `LOOKBACK_CANDLES` window evicts old candles, a
zone may honestly shrink or disappear — no evicted evidence is remembered
or fabricated to keep a zone alive.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Literal, Optional

import numpy as np
import pandas as pd

from apex.indicators.atr import true_range
from apex.opportunity.features import DEFAULT_LEFT_BARS, DEFAULT_RIGHT_BARS, Pivot, find_pivots

Role = Literal["SUPPORT", "RESISTANCE", "AT_PRICE"]

LOOKBACK_CANDLES = 200
ATR_PERIOD = 14
ATR_MULTIPLIER = 0.5
MIN_TOLERANCE_PCT = 0.0025
MIN_PIVOT_TOUCHES = 2

REQUIRED_COLUMNS = ("open_time", "high", "low", "close")
MIN_ROWS_REQUIRED = ATR_PERIOD + DEFAULT_RIGHT_BARS + 1


@dataclass(frozen=True)
class SupportResistanceZone:
    timeframe: str
    center: float
    lower: float
    upper: float
    role: Optional[Role]
    distance_pct: Optional[float]
    contributing_pivot_count: int
    pivot_open_times: tuple[int, ...]
    first_touch_open_time: int
    most_recent_touch_open_time: int


@dataclass(frozen=True)
class _Candidate:
    open_time: int
    price: float
    tolerance: float
    kind: Literal["HIGH", "LOW"]


def find_support_resistance_zones(
    df: pd.DataFrame,
    timeframe: str,
    reference_price: Optional[float] = None,
) -> tuple[SupportResistanceZone, ...]:
    """Return deterministic S/R zones built from the trailing bounded window.

    `df` must have columns high/low/close/open_time, oldest first, closed
    candles only (the shape CandleStore.get_df returns). Only the most
    recent `LOOKBACK_CANDLES` rows are ever considered — history is never
    fetched or persisted by this function. Malformed input is rejected
    conservatively with an empty tuple rather than partially processed.
    """
    if not isinstance(timeframe, str) or not timeframe.strip():
        return ()

    if reference_price is not None:
        try:
            reference_price = float(reference_price)
        except (TypeError, ValueError):
            return ()
        if not np.isfinite(reference_price) or reference_price <= 0:
            return ()

    if df is None:
        return ()
    for column in REQUIRED_COLUMNS:
        if column not in df.columns:
            return ()

    windowed = df.tail(LOOKBACK_CANDLES).reset_index(drop=True)
    if len(windowed) < MIN_ROWS_REQUIRED:
        return ()

    values = _validate_and_extract(windowed)
    if values is None:
        return ()
    open_times, highs, lows, closes = values

    pivots = find_pivots(windowed, left_bars=DEFAULT_LEFT_BARS, right_bars=DEFAULT_RIGHT_BARS)
    if not pivots:
        return ()

    tr = true_range(pd.Series(highs), pd.Series(lows), pd.Series(closes))
    rolling_tr_mean = tr.rolling(ATR_PERIOD, min_periods=ATR_PERIOD).mean()

    candidates = _build_candidates(pivots, rolling_tr_mean)
    if not candidates:
        return ()

    zones = []
    for cluster in _cluster(candidates):
        zone = _build_zone(cluster, timeframe, reference_price)
        if zone is not None:
            zones.append(zone)

    zones.sort(key=lambda z: (z.center, z.first_touch_open_time, z.most_recent_touch_open_time))
    return tuple(zones)


def _validate_and_extract(
    df: pd.DataFrame,
) -> Optional[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]:
    """Validate required columns and return finite float arrays, or None.

    `open_time` is required to be finite but not positive (zero is a valid
    open time); `high`, `low`, and `close` retain the strict positivity
    requirement.
    """
    arrays = {}
    for column in REQUIRED_COLUMNS:
        convert = _to_finite_array if column == "open_time" else _to_finite_positive_array
        column_values = convert(df[column])
        if column_values is None:
            return None
        arrays[column] = column_values

    open_times, highs, lows, closes = (
        arrays["open_time"],
        arrays["high"],
        arrays["low"],
        arrays["close"],
    )

    if not np.all(highs >= lows):
        return None

    if not np.all(open_times == np.floor(open_times)):
        return None
    if len(open_times) > 1 and not np.all(np.diff(open_times) > 0):
        return None

    return open_times, highs, lows, closes


def _to_finite_array(series: pd.Series) -> Optional[np.ndarray]:
    try:
        values = series.to_numpy(dtype=np.float64)
    except (TypeError, ValueError):
        return None
    if not np.all(np.isfinite(values)):
        return None
    return values


def _to_finite_positive_array(series: pd.Series) -> Optional[np.ndarray]:
    values = _to_finite_array(series)
    if values is None:
        return None
    if not np.all(values > 0):
        return None
    return values


def _build_candidates(pivots: list[Pivot], rolling_tr_mean: pd.Series) -> list[_Candidate]:
    seen: set[tuple[int, float]] = set()
    candidates: list[_Candidate] = []
    for pivot in pivots:
        if pivot.index < ATR_PERIOD:
            # A pivot's 14-row true-range window is rows
            # [pivot.index - 13, pivot.index]; row 0 of any slice lacks an
            # in-slice previous close and falls back to a bare high-low
            # true range, so only a pivot at index >= ATR_PERIOD has a
            # complete pivot-local true-range history.
            continue
        window_mean = rolling_tr_mean.iloc[pivot.index]
        if pd.isna(window_mean):
            continue
        key = (pivot.open_time, pivot.price)
        if key in seen:
            continue
        seen.add(key)
        tolerance = max(ATR_MULTIPLIER * float(window_mean), MIN_TOLERANCE_PCT * pivot.price)
        candidates.append(
            _Candidate(
                open_time=pivot.open_time,
                price=pivot.price,
                tolerance=tolerance,
                kind=pivot.kind,
            )
        )

    candidates.sort(key=lambda c: (c.price, c.open_time, c.kind))
    return candidates


def _cluster(candidates: list[_Candidate]) -> list[list[_Candidate]]:
    """Complete-link clustering: a candidate joins only if mutually close to
    every current member, so a transitive chain of close neighbors cannot
    produce an unbounded zone.
    """
    clusters: list[list[_Candidate]] = []
    current: list[_Candidate] = []
    for candidate in candidates:
        if current and not all(
            abs(candidate.price - member.price) <= min(candidate.tolerance, member.tolerance)
            for member in current
        ):
            clusters.append(current)
            current = [candidate]
        else:
            current.append(candidate)
    if current:
        clusters.append(current)
    return clusters


def _build_zone(
    cluster: list[_Candidate],
    timeframe: str,
    reference_price: Optional[float],
) -> Optional[SupportResistanceZone]:
    touch_times = sorted({c.open_time for c in cluster})
    if len(touch_times) < MIN_PIVOT_TOUCHES:
        return None

    lower = max(c.price - c.tolerance for c in cluster)
    upper = min(c.price + c.tolerance for c in cluster)
    center = median(c.price for c in cluster)
    center = min(max(center, lower), upper)

    role: Optional[Role] = None
    distance_pct: Optional[float] = None
    if reference_price is not None:
        distance_pct = (center - reference_price) / reference_price * 100
        if reference_price < lower:
            role = "RESISTANCE"
        elif reference_price > upper:
            role = "SUPPORT"
        else:
            role = "AT_PRICE"

    pivot_open_times = tuple(touch_times)

    return SupportResistanceZone(
        timeframe=timeframe,
        center=center,
        lower=lower,
        upper=upper,
        role=role,
        distance_pct=distance_pct,
        contributing_pivot_count=len(cluster),
        pivot_open_times=pivot_open_times,
        first_touch_open_time=pivot_open_times[0],
        most_recent_touch_open_time=pivot_open_times[-1],
    )
