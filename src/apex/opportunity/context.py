"""Pure multi-timeframe technical context — Context and ranking v0.1.

Builds the deterministic, serializable context snapshot that
`opportunity/scoring.py` scores and `opportunity/engine.py` freezes at a
finding's FIRST DETECTION only (see engine.py's immutable first-detection
snapshot policy). Nothing in this module reads the clock, touches the DB,
performs any fetch, or mutates its inputs — every function here is a pure
transform of already-resident candle DataFrames plus the caller's own
single `now_ms`.

v0.1 limitation (also surfaced in `context_to_dict`'s output and the
ranked report): this is a transparent CONTEXT-ALIGNMENT snapshot only. It
is not a probability, confidence rating, profitability estimate,
actionable threshold, or a normalized comparison of detector pattern
strength across setup families. Component directions come directly from
two existing pure primitives — `apex.strategy.trend_filter.compute_trend_bias`
and `apex.opportunity.features.find_pivots` — unmodified and at their
existing default parameters; nothing here reinterprets or re-tunes them.

Three scored components (see `apex.opportunity.scoring`):
  1. symbol_htf_trend  — the finding's own symbol, 15m EMA9/EMA21/VWAP96
                          trend bias, bounded to the last 96 eligible rows.
  2. primary_structure — the finding's own primary timeframe (3m/5m),
                          confirmed-pivot higher-highs/higher-lows (or
                          lower/lower) structure, bounded to the last 200
                          eligible rows.
  3. btc_htf_trend      — same rule as (1) but for the BTC reference
                          symbol's 15m candles; NOT_APPLICABLE for BTC
                          findings themselves (never double-counts BTC's
                          own trend against itself).

One descriptive-only component (never scored — see scoring.py):
  volatility — ATR_PERCENT = 100 * mean(last 14 true ranges) / latest_close.

Eligibility / no-lookahead: a candle is only eligible for any calculation
here if its own close boundary (open_time + timeframe duration) is at or
before the caller's `now_ms` — this is re-checked inside this module, not
merely assumed from whatever CandleStore already filtered upstream, so
these functions behave identically under direct unit test with synthetic
future-dated rows. A retained window that is gapped, duplicated,
unordered, malformed, or whose latest eligible close boundary is more than
two full frame durations behind `now_ms` (STALE) is rejected outright for
that component — never silently sorted, deduplicated, or repaired.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

import numpy as np
import pandas as pd

from apex.indicators.atr import true_range
from apex.opportunity.contract import PRIMARY_TIMEFRAME_DURATION_MS, Direction
from apex.opportunity.features import DEFAULT_LEFT_BARS, DEFAULT_RIGHT_BARS, Pivot, find_pivots
from apex.strategy.trend_filter import compute_trend_bias

CONTEXT_VERSION = "opportunity_context_v0_1"

# Component 1/3 (symbol/BTC 15m trend) additionally require 15m candles,
# which PRIMARY_TIMEFRAME_DURATION_MS (contract.py) deliberately does not
# carry (it is typed to the two primary timeframes only). This module owns
# the one extra duration it needs instead of widening that shared constant.
TIMEFRAME_DURATION_MS: dict[str, int] = {
    **PRIMARY_TIMEFRAME_DURATION_MS,
    "15m": 15 * 60 * 1000,
}

# "Bound each calculation to at most the last 200 eligible candles; trend
# calculations use at most the last 96" (sealed design).
MAX_TREND_ROWS = 96
MAX_STRUCTURE_ROWS = 200
MAX_VOLATILITY_ROWS = 200

ATR_PERIOD = 14
# Sealed design requires >=15 eligible primary rows for descriptive ATR%
# (one more than the 14-bar true-range window itself), independent of
# indicators.atr.true_range's own row-0 handling (it does not propagate
# NaN there — pandas' default skipna DataFrame.max(axis=1) falls back to
# the single finite high-low term when the two prev_close-based terms are
# NaN, so row 0 is a valid, if less informative, true-range value there).
MIN_ATR_ROWS = ATR_PERIOD + 1

REQUIRED_CANDLE_COLUMNS = ("open_time", "open", "high", "low", "close", "volume")

ComponentStatus = Literal["AVAILABLE", "UNAVAILABLE", "NOT_APPLICABLE"]
ComponentDirection = Literal["LONG", "SHORT", "NEUTRAL"]


@dataclass(frozen=True)
class PivotRef:
    kind: Literal["HIGH", "LOW"]
    open_time: int
    price: float
    confirmed_close_boundary_ms: int


@dataclass(frozen=True)
class TrendComponent:
    status: ComponentStatus
    direction: Optional[ComponentDirection]
    reason: str
    sample_count: int
    latest_close_boundary_ms: Optional[int]


@dataclass(frozen=True)
class StructureComponent:
    status: ComponentStatus
    direction: Optional[ComponentDirection]
    reason: str
    sample_count: int
    latest_close_boundary_ms: Optional[int]
    recent_high: Optional[PivotRef] = None
    prior_high: Optional[PivotRef] = None
    recent_low: Optional[PivotRef] = None
    prior_low: Optional[PivotRef] = None


@dataclass(frozen=True)
class VolatilityComponent:
    """Descriptive only. Never scored (see scoring.py module docstring) —
    compression is not universally favorable across unrelated setup
    families, so it carries no alignment weight.
    """

    status: ComponentStatus
    atr_percent: Optional[float]
    reason: str
    sample_count: int
    latest_close_boundary_ms: Optional[int]


@dataclass(frozen=True)
class OpportunityContext:
    """Immutable, first-detection context snapshot. `scored_direction` and
    `as_of_ms` are recorded on the snapshot itself so a persisted row can
    never be mistaken for a live re-assessment (see engine.py) — this
    mirrors the same convention VOLATILITY_COMPRESSION's own
    first-detection snapshot already established for its measurements.
    """

    version: str
    as_of_ms: int
    primary_timeframe: str
    scored_direction: Direction
    symbol_htf_trend: TrendComponent
    primary_structure: StructureComponent
    btc_htf_trend: TrendComponent
    volatility: VolatilityComponent


# ------------------------------------------------------------------ eligibility / validation


def _validate_window_shape(window: pd.DataFrame, duration: int) -> bool:
    """One bounded pass over `window` only — rows outside it are never
    inspected and therefore never matter (mirrors support_resistance.py /
    the S/R detectors' existing `_validate_window` convention).
    """
    try:
        open_time = window["open_time"].to_numpy(dtype=np.float64)
        o = window["open"].to_numpy(dtype=np.float64)
        h = window["high"].to_numpy(dtype=np.float64)
        low = window["low"].to_numpy(dtype=np.float64)
        c = window["close"].to_numpy(dtype=np.float64)
        v = window["volume"].to_numpy(dtype=np.float64)
    except (TypeError, ValueError):
        return False

    for arr in (open_time, o, h, low, c, v):
        if not np.all(np.isfinite(arr)):
            return False

    if not np.all(open_time == np.floor(open_time)) or not np.all(open_time >= 0):
        return False
    if not np.all(np.mod(open_time, duration) == 0):
        return False

    for arr in (o, h, low, c):
        if not np.all(arr > 0):
            return False
    if not np.all(low <= o) or not np.all(o <= h):
        return False
    if not np.all(low <= c) or not np.all(c <= h):
        return False
    if not np.all(v >= 0):
        return False

    if len(open_time) > 1 and not np.all(np.diff(open_time) == duration):
        # Covers gaps, duplicates, and out-of-order rows in one check: the
        # only way consecutive open_times differ by exactly `duration` is a
        # strictly increasing, contiguous, non-duplicated grid.
        return False

    return True


def _bounded_eligible_window(
    df: Optional[pd.DataFrame],
    *,
    timeframe: str,
    now_ms: int,
    max_rows: int,
) -> tuple[Optional[pd.DataFrame], str]:
    """Pure: never reads the clock, never mutates `df`.

    Returns `(window, "OK")` — a bounded, validated, oldest-first,
    eligible-only window (a NEW DataFrame; `df` itself is never mutated) —
    or `(None, reason)` where `reason` is one of "INVALID_TIMEFRAME",
    "INVALID_CUTOFF", "MISSING", "MALFORMED", "EMPTY_AFTER_CUTOFF", or
    "STALE".
    """
    duration = TIMEFRAME_DURATION_MS.get(timeframe)
    if duration is None:
        return None, "INVALID_TIMEFRAME"
    if not isinstance(now_ms, int) or isinstance(now_ms, bool) or now_ms < 0:
        return None, "INVALID_CUTOFF"
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return None, "MISSING"
    for col in REQUIRED_CANDLE_COLUMNS:
        if col not in df.columns:
            return None, "MALFORMED"
    if df.columns[df.columns.isin(REQUIRED_CANDLE_COLUMNS)].duplicated().any():
        return None, "MALFORMED"

    try:
        open_time_full = df["open_time"].to_numpy(dtype=np.float64)
    except (TypeError, ValueError):
        return None, "MALFORMED"
    if not np.all(np.isfinite(open_time_full)):
        return None, "MALFORMED"

    # Exclude future/current bars: a candle is only eligible once its own
    # close boundary is at or before now_ms. Future-only appended rows
    # (beyond this cutoff) never affect the retained window below.
    eligible_mask = (open_time_full + duration) <= now_ms
    eligible = df.loc[eligible_mask].reset_index(drop=True)
    if eligible.empty:
        return None, "EMPTY_AFTER_CUTOFF"

    window = eligible.tail(max_rows).reset_index(drop=True)
    if not _validate_window_shape(window, duration):
        return None, "MALFORMED"

    latest_open_time = int(window["open_time"].iloc[-1])
    latest_close_boundary_ms = latest_open_time + duration
    if now_ms - latest_close_boundary_ms > 2 * duration:
        return None, "STALE"

    return window, "OK"


# ------------------------------------------------------------------ component 1 & 3: 15m trend


def _trend_component_from_bias(window: pd.DataFrame, duration: int) -> TrendComponent:
    """compute_trend_bias is the existing, unmodified pure primitive
    (trend_filter.py); this only classifies its output into
    AVAILABLE/UNAVAILABLE without changing its EMA9/EMA21/VWAP96 logic.
    bias LONG/SHORT are always AVAILABLE; bias NONE is AVAILABLE NEUTRAL
    only when the reason is the genuine "conditions do not agree" case —
    "insufficient data" and "NaN in indicators" (nonfinite indicators,
    including no usable volume) are UNAVAILABLE instead.

    Sealed finite-indicator contract: compute_trend_bias's own "NaN in
    indicators" branch only checks `math.isnan`, so a genuine (non-NaN)
    infinity in ema_fast/ema_slow/vwap_val/last_close would otherwise still
    reach a returned LONG/SHORT/"conditions do not agree" bias unfiltered —
    an infinite EMA or VWAP value can make an `>`/`<` comparison resolve to
    a directional or neutral-looking result that carries no real meaning.
    Before trusting a LONG/SHORT/NEUTRAL bias, this additionally verifies
    all four returned indicator values are finite (`np.isfinite`, which
    rejects both NaN and +-inf); "insufficient data" and "NaN in
    indicators" remain untouched and distinguishable by this same check,
    since compute_trend_bias already sets those four fields to NaN for both
    of those cases, and NaN never satisfies `bias in ("LONG", "SHORT")` or
    reason == "EMA/VWAP conditions do not agree" in the first place — this
    check only ever intercepts the LONG/SHORT/"conditions do not agree"
    branches, so it can never relabel "insufficient data"/"NaN in
    indicators" into a different reason.
    """
    trend = compute_trend_bias(window)
    sample_count = len(window)
    latest_close_boundary_ms = int(window["open_time"].iloc[-1]) + duration

    indicators_finite = all(
        np.isfinite(v) for v in (trend.ema_fast, trend.ema_slow, trend.vwap_val, trend.last_close)
    )

    if trend.bias in ("LONG", "SHORT"):
        if not indicators_finite:
            return TrendComponent(
                status="UNAVAILABLE",
                direction=None,
                reason="NONFINITE_INDICATORS",
                sample_count=sample_count,
                latest_close_boundary_ms=latest_close_boundary_ms,
            )
        return TrendComponent(
            status="AVAILABLE",
            direction=trend.bias,
            reason=trend.reason,
            sample_count=sample_count,
            latest_close_boundary_ms=latest_close_boundary_ms,
        )
    if trend.reason == "EMA/VWAP conditions do not agree":
        if not indicators_finite:
            return TrendComponent(
                status="UNAVAILABLE",
                direction=None,
                reason="NONFINITE_INDICATORS",
                sample_count=sample_count,
                latest_close_boundary_ms=latest_close_boundary_ms,
            )
        return TrendComponent(
            status="AVAILABLE",
            direction="NEUTRAL",
            reason=trend.reason,
            sample_count=sample_count,
            latest_close_boundary_ms=latest_close_boundary_ms,
        )
    return TrendComponent(
        status="UNAVAILABLE",
        direction=None,
        reason=trend.reason,
        sample_count=sample_count,
        latest_close_boundary_ms=latest_close_boundary_ms,
    )


def compute_symbol_trend_component(
    df_15m: Optional[pd.DataFrame],
    now_ms: int,
) -> TrendComponent:
    """Component 1 (symbol's own 15m trend) and component 3 (BTC's 15m
    trend, for every symbol other than BTC itself) share this exact rule
    set — see module docstring. Bounded to the last 96 eligible closed 15m
    candles; compute_trend_bias's own >=22-row gate (EMA21 + 1) applies
    unchanged on top of that bound.
    """
    window, reason = _bounded_eligible_window(
        df_15m, timeframe="15m", now_ms=now_ms, max_rows=MAX_TREND_ROWS
    )
    if window is None:
        return TrendComponent(
            status="UNAVAILABLE",
            direction=None,
            reason=reason,
            sample_count=0,
            latest_close_boundary_ms=None,
        )
    return _trend_component_from_bias(window, TIMEFRAME_DURATION_MS["15m"])


def not_applicable_trend_component() -> TrendComponent:
    """Fixed constant for component 3 when the finding's own symbol IS the
    BTC reference symbol itself — never double-counts BTC's own 15m trend
    against itself (sealed design).
    """
    return TrendComponent(
        status="NOT_APPLICABLE",
        direction=None,
        reason="symbol is the BTC reference symbol",
        sample_count=0,
        latest_close_boundary_ms=None,
    )


# ------------------------------------------------------------------ component 2: primary structure


def _pivot_ref(pivot: Pivot, window: pd.DataFrame, duration: int) -> PivotRef:
    """A pivot at `pivot.index` is confirmed once `DEFAULT_RIGHT_BARS`
    additional closed candles exist after it (features.py's no-lookahead
    guarantee) — its confirmation close boundary is that confirming bar's
    own close boundary, not the pivot bar's own.
    """
    confirm_idx = pivot.index + DEFAULT_RIGHT_BARS
    confirm_open_time = int(window["open_time"].iloc[confirm_idx])
    return PivotRef(
        kind=pivot.kind,
        open_time=pivot.open_time,
        price=pivot.price,
        confirmed_close_boundary_ms=confirm_open_time + duration,
    )


def compute_primary_structure_component(
    primary_df: Optional[pd.DataFrame],
    timeframe: str,
    now_ms: int,
) -> StructureComponent:
    """find_pivots is the existing, unmodified pure primitive
    (features.py) at its own default 3-left/3-right parameters — no extra
    zone construction or detector refactor. Compares the last two
    CONFIRMED HIGH pivots and the last two CONFIRMED LOW pivots: both
    higher (higher highs AND higher lows) => LONG; both lower => SHORT;
    mixed or equal => NEUTRAL; fewer than two of either kind =>
    UNAVAILABLE.
    """
    window, reason = _bounded_eligible_window(
        primary_df, timeframe=timeframe, now_ms=now_ms, max_rows=MAX_STRUCTURE_ROWS
    )
    if window is None:
        return StructureComponent(
            status="UNAVAILABLE",
            direction=None,
            reason=reason,
            sample_count=0,
            latest_close_boundary_ms=None,
        )

    duration = TIMEFRAME_DURATION_MS[timeframe]
    sample_count = len(window)
    latest_close_boundary_ms = int(window["open_time"].iloc[-1]) + duration

    pivots = find_pivots(window, left_bars=DEFAULT_LEFT_BARS, right_bars=DEFAULT_RIGHT_BARS)
    highs = [p for p in pivots if p.kind == "HIGH"]
    lows = [p for p in pivots if p.kind == "LOW"]

    if len(highs) < 2 or len(lows) < 2:
        return StructureComponent(
            status="UNAVAILABLE",
            direction=None,
            reason="FEWER_THAN_TWO_CONFIRMED_PIVOTS",
            sample_count=sample_count,
            latest_close_boundary_ms=latest_close_boundary_ms,
        )

    # find_pivots returns pivots in ascending positional-index order, so the
    # last two entries of each kind are the two most recent.
    h_prior, h_recent = highs[-2], highs[-1]
    l_prior, l_recent = lows[-2], lows[-1]

    higher_highs = h_recent.price > h_prior.price
    lower_highs = h_recent.price < h_prior.price
    higher_lows = l_recent.price > l_prior.price
    lower_lows = l_recent.price < l_prior.price

    direction: ComponentDirection
    if higher_highs and higher_lows:
        direction = "LONG"
    elif lower_highs and lower_lows:
        direction = "SHORT"
    else:
        direction = "NEUTRAL"

    return StructureComponent(
        status="AVAILABLE",
        direction=direction,
        reason="last two confirmed HIGH and LOW pivots compared",
        sample_count=sample_count,
        latest_close_boundary_ms=latest_close_boundary_ms,
        recent_high=_pivot_ref(h_recent, window, duration),
        prior_high=_pivot_ref(h_prior, window, duration),
        recent_low=_pivot_ref(l_recent, window, duration),
        prior_low=_pivot_ref(l_prior, window, duration),
    )


# ------------------------------------------------------------------ descriptive volatility (unscored)


def compute_volatility_component(
    primary_df: Optional[pd.DataFrame],
    timeframe: str,
    now_ms: int,
) -> VolatilityComponent:
    """ATR_PERCENT = 100 * mean(last 14 true ranges) / latest_close, using
    the existing `indicators.atr.true_range` primitive unmodified. Requires
    >=15 eligible primary rows (sealed design) — one more row than the
    14-bar true-range window itself.
    """
    window, reason = _bounded_eligible_window(
        primary_df, timeframe=timeframe, now_ms=now_ms, max_rows=MAX_VOLATILITY_ROWS
    )
    if window is None:
        return VolatilityComponent(
            status="UNAVAILABLE",
            atr_percent=None,
            reason=reason,
            sample_count=0,
            latest_close_boundary_ms=None,
        )

    duration = TIMEFRAME_DURATION_MS[timeframe]
    sample_count = len(window)
    latest_close_boundary_ms = int(window["open_time"].iloc[-1]) + duration

    if sample_count < MIN_ATR_ROWS:
        return VolatilityComponent(
            status="UNAVAILABLE",
            atr_percent=None,
            reason="INSUFFICIENT_ROWS_FOR_ATR",
            sample_count=sample_count,
            latest_close_boundary_ms=latest_close_boundary_ms,
        )

    tr = true_range(window["high"], window["low"], window["close"])
    last_14_tr = tr.tail(ATR_PERIOD)
    if last_14_tr.isna().any() or not np.isfinite(last_14_tr.to_numpy(dtype=np.float64)).all():
        return VolatilityComponent(
            status="UNAVAILABLE",
            atr_percent=None,
            reason="NONFINITE_TRUE_RANGE",
            sample_count=sample_count,
            latest_close_boundary_ms=latest_close_boundary_ms,
        )

    latest_close = float(window["close"].iloc[-1])
    atr_percent = 100.0 * float(last_14_tr.mean()) / latest_close

    return VolatilityComponent(
        status="AVAILABLE",
        atr_percent=atr_percent,
        reason="mean(last 14 true ranges) / latest_close * 100",
        sample_count=sample_count,
        latest_close_boundary_ms=latest_close_boundary_ms,
    )


# ------------------------------------------------------------------ assembly / serialization


def assemble_context(
    *,
    as_of_ms: int,
    primary_timeframe: str,
    scored_direction: Direction,
    symbol_htf_trend: TrendComponent,
    primary_structure: StructureComponent,
    btc_htf_trend: TrendComponent,
    volatility: VolatilityComponent,
) -> OpportunityContext:
    """Cheap pure combiner — performs no computation, reads no clock, does
    no I/O. Callers (engine.py) build the shared per-symbol / per-scan /
    per-(symbol,timeframe) components once and pass them in here per
    finding, substituting only `scored_direction` — see engine.py's
    "Compute 15m trend once per symbol, BTC trend once per scan, primary
    structure/volatility once per eligible (symbol,timeframe)" contract.
    """
    return OpportunityContext(
        version=CONTEXT_VERSION,
        as_of_ms=as_of_ms,
        primary_timeframe=primary_timeframe,
        scored_direction=scored_direction,
        symbol_htf_trend=symbol_htf_trend,
        primary_structure=primary_structure,
        btc_htf_trend=btc_htf_trend,
        volatility=volatility,
    )


def _pivot_ref_to_dict(ref: Optional[PivotRef]) -> Optional[dict]:
    if ref is None:
        return None
    return {
        "kind": ref.kind,
        "open_time": ref.open_time,
        "price": ref.price,
        "confirmed_close_boundary_ms": ref.confirmed_close_boundary_ms,
    }


def _trend_component_to_dict(component: TrendComponent) -> dict:
    return {
        "status": component.status,
        "direction": component.direction,
        "reason": component.reason,
        "sample_count": component.sample_count,
        "latest_close_boundary_ms": component.latest_close_boundary_ms,
    }


def _structure_component_to_dict(component: StructureComponent) -> dict:
    return {
        "status": component.status,
        "direction": component.direction,
        "reason": component.reason,
        "sample_count": component.sample_count,
        "latest_close_boundary_ms": component.latest_close_boundary_ms,
        "recent_high": _pivot_ref_to_dict(component.recent_high),
        "prior_high": _pivot_ref_to_dict(component.prior_high),
        "recent_low": _pivot_ref_to_dict(component.recent_low),
        "prior_low": _pivot_ref_to_dict(component.prior_low),
    }


def _volatility_component_to_dict(component: VolatilityComponent) -> dict:
    return {
        "status": component.status,
        "atr_percent": component.atr_percent,
        "reason": component.reason,
        "sample_count": component.sample_count,
        "latest_close_boundary_ms": component.latest_close_boundary_ms,
    }


CONTEXT_LIMITATIONS_NOTE = (
    "v0.1: transparent context-alignment snapshot only — not a probability, "
    "confidence rating, profitability estimate, actionable threshold, or a "
    "normalized comparison of detector pattern strength across setup families."
)


def context_to_dict(context: OpportunityContext) -> dict:
    """Deterministic, JSON-serializable (safe under `json.dumps(...,
    allow_nan=False)`) representation — every leaf value is a plain
    str/int/float/bool/None, never a numpy scalar.
    """
    return {
        "version": context.version,
        "as_of_ms": context.as_of_ms,
        "primary_timeframe": context.primary_timeframe,
        "scored_direction": context.scored_direction,
        "symbol_htf_trend": _trend_component_to_dict(context.symbol_htf_trend),
        "primary_structure": _structure_component_to_dict(context.primary_structure),
        "btc_htf_trend": _trend_component_to_dict(context.btc_htf_trend),
        "volatility": _volatility_component_to_dict(context.volatility),
        "limitations": CONTEXT_LIMITATIONS_NOTE,
    }
