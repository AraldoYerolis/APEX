"""Tests for the pure multi-timeframe context primitives in
src/apex/opportunity/context.py (Context and ranking v0.1).

Covers: symbol/BTC 15m trend classification (AVAILABLE LONG/SHORT/NEUTRAL
vs UNAVAILABLE, including nonfinite-indicator and no-usable-volume cases),
the 96-row trend bound, primary-structure confirmed-pivot direction
(mirror LONG/SHORT cases, NEUTRAL/mixed, confirmation lag, fewer-than-two
UNAVAILABLE), descriptive ATR% correctness and its own row-count gate,
future/current-candle exclusion at a fixed now_ms, gapped/duplicated/
unordered/malformed/stale/zero-volume input rejection, BTC-self
NOT_APPLICABLE handling, immutability of inputs, and deterministic
JSON-safe serialization.
"""
from __future__ import annotations

import json
import math

import pandas as pd
import pytest

import apex.opportunity.context as opportunity_context
from apex.opportunity.context import (
    MAX_TREND_ROWS,
    TIMEFRAME_DURATION_MS,
    assemble_context,
    compute_primary_structure_component,
    compute_symbol_trend_component,
    compute_volatility_component,
    context_to_dict,
    not_applicable_trend_component,
)
from apex.strategy.trend_filter import TrendBias


# ------------------------------------------------------------------ helpers


def _grid_df(
    n: int,
    *,
    timeframe: str,
    start_open_time: int = 0,
    overrides: dict | None = None,
) -> pd.DataFrame:
    """A minimal valid, grid-aligned, contiguous closed-candle DataFrame:
    flat baseline OHLCV unless overridden per-row via `overrides` (a dict
    of {row_index: {"open":..., "high":..., "low":..., "close":...}}).
    """
    duration = TIMEFRAME_DURATION_MS[timeframe]
    open_times = [start_open_time + i * duration for i in range(n)]
    opens = [100.0] * n
    highs = [100.5] * n
    lows = [99.5] * n
    closes = [100.0] * n
    volumes = [1000.0] * n

    for idx, fields in (overrides or {}).items():
        if "open" in fields:
            opens[idx] = fields["open"]
        if "high" in fields:
            highs[idx] = fields["high"]
        if "low" in fields:
            lows[idx] = fields["low"]
        if "close" in fields:
            closes[idx] = fields["close"]
        if "volume" in fields:
            volumes[idx] = fields["volume"]

    return pd.DataFrame({
        "open_time": open_times,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


def _now_ms_all_closed(df: pd.DataFrame, timeframe: str) -> int:
    duration = TIMEFRAME_DURATION_MS[timeframe]
    return int(df["open_time"].iloc[-1]) + duration


def _uptrend_15m_df(n: int = 40, start_open_time: int = 0) -> pd.DataFrame:
    closes = [100.0 + i for i in range(n)]
    return pd.DataFrame({
        "open_time": [start_open_time + i * TIMEFRAME_DURATION_MS["15m"] for i in range(n)],
        "open": closes,
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "volume": [1000.0] * n,
    })


def _downtrend_15m_df(n: int = 40, start_open_time: int = 0) -> pd.DataFrame:
    closes = [200.0 - i for i in range(n)]
    return pd.DataFrame({
        "open_time": [start_open_time + i * TIMEFRAME_DURATION_MS["15m"] for i in range(n)],
        "open": closes,
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "volume": [1000.0] * n,
    })


def _flat_15m_df(n: int = 40, start_open_time: int = 0) -> pd.DataFrame:
    return pd.DataFrame({
        "open_time": [start_open_time + i * TIMEFRAME_DURATION_MS["15m"] for i in range(n)],
        "open": [100.0] * n,
        "high": [100.5] * n,
        "low": [99.5] * n,
        "close": [100.0] * n,
        "volume": [1000.0] * n,
    })


# ------------------------------------------------------------------ component 1/3: 15m trend


def test_symbol_trend_long_available():
    df = _uptrend_15m_df()
    component = compute_symbol_trend_component(df, _now_ms_all_closed(df, "15m"))
    assert component.status == "AVAILABLE"
    assert component.direction == "LONG"
    assert component.sample_count == len(df)


def test_symbol_trend_short_available():
    df = _downtrend_15m_df()
    component = compute_symbol_trend_component(df, _now_ms_all_closed(df, "15m"))
    assert component.status == "AVAILABLE"
    assert component.direction == "SHORT"


def test_symbol_trend_flat_is_available_neutral_not_unavailable():
    df = _flat_15m_df()
    component = compute_symbol_trend_component(df, _now_ms_all_closed(df, "15m"))
    assert component.status == "AVAILABLE"
    assert component.direction == "NEUTRAL"


def test_symbol_trend_missing_df_is_unavailable():
    component = compute_symbol_trend_component(None, 1_000_000)
    assert component.status == "UNAVAILABLE"
    assert component.direction is None
    assert component.reason == "MISSING"


def test_symbol_trend_too_few_rows_is_unavailable():
    df = _uptrend_15m_df(n=10)
    component = compute_symbol_trend_component(df, _now_ms_all_closed(df, "15m"))
    assert component.status == "UNAVAILABLE"
    assert component.reason == "insufficient data"


def test_symbol_trend_zero_volume_vwap_nan_is_unavailable():
    """No usable volume -> VWAP is NaN -> compute_trend_bias's own NaN
    branch -> UNAVAILABLE, distinguished from a genuine NEUTRAL read."""
    df = _uptrend_15m_df()
    df["volume"] = 0.0
    component = compute_symbol_trend_component(df, _now_ms_all_closed(df, "15m"))
    assert component.status == "UNAVAILABLE"
    assert component.reason == "NaN in indicators"


def test_symbol_trend_bounded_to_last_96_rows():
    df = _uptrend_15m_df(n=300)
    component = compute_symbol_trend_component(df, _now_ms_all_closed(df, "15m"))
    assert component.status == "AVAILABLE"
    assert component.sample_count == MAX_TREND_ROWS


def test_symbol_trend_does_not_mutate_input():
    df = _uptrend_15m_df()
    before = df.copy(deep=True)
    compute_symbol_trend_component(df, _now_ms_all_closed(df, "15m"))
    pd.testing.assert_frame_equal(df, before)


# ------------------------------------------------------------------ sealed finite-indicator contract


def _mock_bias(monkeypatch, bias, reason, **field_overrides):
    """Replace compute_trend_bias with a deterministic fake returning a
    fixed TrendBias — used to reach the specific nonfinite-indicator paths
    that real EMA/VWAP arithmetic would rarely produce (e.g. a genuine +inf
    that is not NaN, which compute_trend_bias's own math.isnan check does
    not catch).
    """
    fields = dict(ema_fast=1.0, ema_slow=1.0, vwap_val=1.0, last_close=1.0)
    fields.update(field_overrides)
    fixed = TrendBias(bias=bias, reason=reason, **fields)
    monkeypatch.setattr(opportunity_context, "compute_trend_bias", lambda window: fixed)


@pytest.mark.parametrize("field", ["ema_fast", "ema_slow", "vwap_val", "last_close"])
@pytest.mark.parametrize("bad_value", [float("inf"), float("-inf")])
def test_long_bias_with_infinite_indicator_is_unavailable_not_trusted(monkeypatch, field, bad_value):
    """compute_trend_bias's own NaN check does not catch +-inf — a genuine
    infinity in any of the four indicator fields must still be rejected
    here even though bias/reason otherwise look like a valid LONG read.
    """
    _mock_bias(monkeypatch, bias="LONG", reason="EMA9>EMA21 and close above VWAP", **{field: bad_value})
    df = _uptrend_15m_df()
    component = compute_symbol_trend_component(df, _now_ms_all_closed(df, "15m"))
    assert component.status == "UNAVAILABLE"
    assert component.direction is None
    assert component.reason == "NONFINITE_INDICATORS"


@pytest.mark.parametrize("field", ["ema_fast", "ema_slow", "vwap_val", "last_close"])
def test_neutral_bias_with_infinite_indicator_is_unavailable_not_trusted(monkeypatch, field):
    _mock_bias(
        monkeypatch, bias="NONE", reason="EMA/VWAP conditions do not agree",
        **{field: float("inf")},
    )
    df = _uptrend_15m_df()
    component = compute_symbol_trend_component(df, _now_ms_all_closed(df, "15m"))
    assert component.status == "UNAVAILABLE"
    assert component.direction is None
    assert component.reason == "NONFINITE_INDICATORS"


def test_short_bias_with_infinite_indicator_is_unavailable_not_trusted(monkeypatch):
    _mock_bias(
        monkeypatch, bias="SHORT", reason="EMA9<EMA21 and close below VWAP",
        vwap_val=float("-inf"),
    )
    df = _downtrend_15m_df()
    component = compute_symbol_trend_component(df, _now_ms_all_closed(df, "15m"))
    assert component.status == "UNAVAILABLE"
    assert component.reason == "NONFINITE_INDICATORS"


def test_valid_finite_long_bias_still_available_unaffected_by_the_guard(monkeypatch):
    """Contrast case: an entirely finite, genuinely valid LONG read must
    still pass through as AVAILABLE — the guard only rejects nonfinite
    values, never a real result.
    """
    _mock_bias(
        monkeypatch, bias="LONG", reason="EMA9>EMA21 and close above VWAP",
        ema_fast=105.0, ema_slow=100.0, vwap_val=101.0, last_close=106.0,
    )
    df = _uptrend_15m_df()
    component = compute_symbol_trend_component(df, _now_ms_all_closed(df, "15m"))
    assert component.status == "AVAILABLE"
    assert component.direction == "LONG"


def test_insufficient_data_reason_unaffected_by_the_finite_indicator_guard(monkeypatch):
    """compute_trend_bias's real "insufficient data" case already returns
    all-NaN indicator fields; the new finite-indicator guard must not
    relabel this into NONFINITE_INDICATORS — it stays distinguishable as
    "insufficient data" exactly as before.
    """
    _mock_bias(
        monkeypatch, bias="NONE", reason="insufficient data",
        ema_fast=float("nan"), ema_slow=float("nan"),
        vwap_val=float("nan"), last_close=float("nan"),
    )
    df = _uptrend_15m_df()
    component = compute_symbol_trend_component(df, _now_ms_all_closed(df, "15m"))
    assert component.status == "UNAVAILABLE"
    assert component.reason == "insufficient data"


def test_nan_in_indicators_reason_unaffected_by_the_finite_indicator_guard(monkeypatch):
    _mock_bias(
        monkeypatch, bias="NONE", reason="NaN in indicators",
        vwap_val=float("nan"),
    )
    df = _uptrend_15m_df()
    component = compute_symbol_trend_component(df, _now_ms_all_closed(df, "15m"))
    assert component.status == "UNAVAILABLE"
    assert component.reason == "NaN in indicators"


def test_btc_self_is_not_applicable_constant():
    component = not_applicable_trend_component()
    assert component.status == "NOT_APPLICABLE"
    assert component.direction is None
    assert component.sample_count == 0


# ------------------------------------------------------------------ future/current exclusion, gaps, staleness


def test_future_current_candle_excluded_at_fixed_now():
    df = _uptrend_15m_df(n=30)
    duration = TIMEFRAME_DURATION_MS["15m"]
    # now_ms cuts off exactly at the second-to-last candle's own close
    # boundary — the very last candle has not closed yet as of this now.
    now_ms = int(df["open_time"].iloc[-2]) + duration
    component = compute_symbol_trend_component(df, now_ms)
    assert component.status == "AVAILABLE"
    assert component.latest_close_boundary_ms == now_ms
    assert component.sample_count == len(df) - 1


def test_future_only_appended_rows_do_not_change_result():
    df = _uptrend_15m_df(n=30)
    duration = TIMEFRAME_DURATION_MS["15m"]
    now_ms = _now_ms_all_closed(df, "15m")
    baseline = compute_symbol_trend_component(df, now_ms)

    extended = pd.concat(
        [df, _grid_df(5, timeframe="15m", start_open_time=int(df["open_time"].iloc[-1]) + duration)],
        ignore_index=True,
    )
    extended_result = compute_symbol_trend_component(extended, now_ms)
    assert extended_result.direction == baseline.direction
    assert extended_result.sample_count == baseline.sample_count
    assert extended_result.latest_close_boundary_ms == baseline.latest_close_boundary_ms


def test_gap_in_retained_window_is_rejected_not_repaired():
    n = 30
    duration = TIMEFRAME_DURATION_MS["15m"]
    open_times = [i * duration for i in range(n)]
    del open_times[15]  # remove one step -> a gap in the middle of the window
    open_times.append(n * duration)  # keep row count the same
    df = pd.DataFrame({
        "open_time": open_times,
        "open": [100.0] * n,
        "high": [100.5] * n,
        "low": [99.5] * n,
        "close": [100.0] * n,
        "volume": [1000.0] * n,
    })
    now_ms = int(max(open_times)) + duration
    component = compute_symbol_trend_component(df, now_ms)
    assert component.status == "UNAVAILABLE"
    assert component.reason == "MALFORMED"


def test_duplicate_open_time_in_retained_window_is_rejected():
    df = _uptrend_15m_df(n=30)
    df.loc[10, "open_time"] = df.loc[9, "open_time"]  # duplicate, not strictly increasing
    now_ms = _now_ms_all_closed(df, "15m")
    component = compute_symbol_trend_component(df, now_ms)
    assert component.status == "UNAVAILABLE"
    assert component.reason == "MALFORMED"


def test_unordered_open_time_in_retained_window_is_rejected():
    df = _uptrend_15m_df(n=10)
    df.loc[5, "open_time"] = 2 * TIMEFRAME_DURATION_MS["15m"]  # out of order (duplicates row 2's slot too)
    now_ms = _now_ms_all_closed(df, "15m")
    component = compute_symbol_trend_component(df, now_ms)
    assert component.status == "UNAVAILABLE"
    assert component.reason == "MALFORMED"


def test_malformed_ohlc_is_rejected():
    df = _uptrend_15m_df(n=30)
    df.loc[10, "low"] = df.loc[10, "high"] + 1.0  # low > high
    now_ms = _now_ms_all_closed(df, "15m")
    component = compute_symbol_trend_component(df, now_ms)
    assert component.status == "UNAVAILABLE"
    assert component.reason == "MALFORMED"


def test_stale_latest_close_boundary_is_rejected():
    df = _uptrend_15m_df(n=30)
    duration = TIMEFRAME_DURATION_MS["15m"]
    now_ms = _now_ms_all_closed(df, "15m") + 3 * duration  # more than 2 full durations stale
    component = compute_symbol_trend_component(df, now_ms)
    assert component.status == "UNAVAILABLE"
    assert component.reason == "STALE"


def test_invalid_timeframe_rejected():
    df = _uptrend_15m_df(n=30)
    component = compute_primary_structure_component(df, "1h", _now_ms_all_closed(df, "15m"))
    assert component.status == "UNAVAILABLE"
    assert component.reason == "INVALID_TIMEFRAME"


def test_invalid_cutoff_rejected():
    df = _uptrend_15m_df(n=30)
    component = compute_symbol_trend_component(df, now_ms=-5)
    assert component.status == "UNAVAILABLE"
    assert component.reason == "INVALID_CUTOFF"

    component_bool = compute_symbol_trend_component(df, now_ms=True)  # bool is not a real now_ms
    assert component_bool.status == "UNAVAILABLE"
    assert component_bool.reason == "INVALID_CUTOFF"


# ------------------------------------------------------------------ component 2: primary structure


def _mirror_structure_df(*, ascending: bool, extra_tail_bars: int = 5) -> pd.DataFrame:
    """Two confirmed LOW pivots and two confirmed HIGH pivots, well
    separated and each isolated by flat baseline bars on both sides.
    `ascending=True` -> higher highs AND higher lows (LONG structure);
    `ascending=False` -> the exact mirror (lower highs AND lower lows,
    SHORT structure). `extra_tail_bars` controls how many bars exist after
    the final (HIGH2) pivot bar — must be >= 3 for it to be CONFIRMED.
    """
    low1, low2 = (97.0, 98.0) if ascending else (98.0, 97.0)
    high1, high2 = (103.0, 105.0) if ascending else (105.0, 103.0)
    close2 = 104.0 if ascending else 102.5  # must stay <= high2 (103 in the descending mirror)
    overrides = {
        5: {"low": low1, "open": 99.0, "close": 98.0, "high": 99.5},
        10: {"high": high1, "low": 100.5, "open": 101.0, "close": 102.0},
        15: {"low": low2, "open": 99.5, "close": 98.5, "high": 100.0},
        20: {"high": high2, "low": 102.0, "open": 103.0, "close": close2},
    }
    n = 21 + extra_tail_bars
    return _grid_df(n, timeframe="5m", overrides=overrides)


def test_primary_structure_ascending_pivots_yield_long():
    df = _mirror_structure_df(ascending=True)
    now_ms = _now_ms_all_closed(df, "5m")
    component = compute_primary_structure_component(df, "5m", now_ms)
    assert component.status == "AVAILABLE"
    assert component.direction == "LONG"
    assert component.recent_high.price == 105.0
    assert component.prior_high.price == 103.0
    assert component.recent_low.price == 98.0
    assert component.prior_low.price == 97.0


def test_primary_structure_descending_pivots_yield_short_mirror():
    df = _mirror_structure_df(ascending=False)
    now_ms = _now_ms_all_closed(df, "5m")
    component = compute_primary_structure_component(df, "5m", now_ms)
    assert component.status == "AVAILABLE"
    assert component.direction == "SHORT"
    assert component.recent_high.price == 103.0
    assert component.prior_high.price == 105.0
    assert component.recent_low.price == 97.0
    assert component.prior_low.price == 98.0


def test_primary_structure_mixed_is_neutral():
    # Higher highs but lower lows (broadening structure) -> NEUTRAL.
    overrides = {
        5: {"low": 98.0, "open": 99.0, "close": 98.5, "high": 99.5},
        10: {"high": 103.0, "low": 100.5, "open": 101.0, "close": 102.0},
        15: {"low": 97.0, "open": 99.5, "close": 98.0, "high": 100.0},  # lower than idx5's low
        20: {"high": 105.0, "low": 102.0, "open": 103.0, "close": 104.0},  # higher than idx10's high
    }
    df = _grid_df(26, timeframe="5m", overrides=overrides)
    now_ms = _now_ms_all_closed(df, "5m")
    component = compute_primary_structure_component(df, "5m", now_ms)
    assert component.status == "AVAILABLE"
    assert component.direction == "NEUTRAL"


def test_primary_structure_fewer_than_two_pivots_is_unavailable():
    df = _grid_df(30, timeframe="5m")  # perfectly flat -> zero confirmed pivots of either kind
    now_ms = _now_ms_all_closed(df, "5m")
    component = compute_primary_structure_component(df, "5m", now_ms)
    assert component.status == "UNAVAILABLE"
    assert component.reason == "FEWER_THAN_TWO_CONFIRMED_PIVOTS"


def test_primary_structure_confirmation_lag_unavailable_then_available():
    """The final HIGH pivot at idx20 is only CONFIRMED once 3 bars exist
    after it — with exactly 2 tail bars it is not yet confirmed (only 1
    HIGH pivot total is confirmed, so structure is UNAVAILABLE); with 3
    tail bars it becomes the second confirmed HIGH and structure resolves.
    """
    not_yet = _mirror_structure_df(ascending=True, extra_tail_bars=2)
    now_ms_not_yet = _now_ms_all_closed(not_yet, "5m")
    component_not_yet = compute_primary_structure_component(not_yet, "5m", now_ms_not_yet)
    assert component_not_yet.status == "UNAVAILABLE"

    now = _mirror_structure_df(ascending=True, extra_tail_bars=3)
    now_ms_now = _now_ms_all_closed(now, "5m")
    component_now = compute_primary_structure_component(now, "5m", now_ms_now)
    assert component_now.status == "AVAILABLE"
    assert component_now.direction == "LONG"


def test_primary_structure_pivot_refs_record_confirmation_boundary():
    df = _mirror_structure_df(ascending=True)
    now_ms = _now_ms_all_closed(df, "5m")
    component = compute_primary_structure_component(df, "5m", now_ms)
    duration = TIMEFRAME_DURATION_MS["5m"]
    # HIGH2 pivot is at positional index 20; confirmed once 3 bars after it
    # exist, i.e. at index 23's own close boundary.
    expected_boundary = int(df["open_time"].iloc[23]) + duration
    assert component.recent_high.confirmed_close_boundary_ms == expected_boundary


def test_primary_structure_does_not_mutate_input():
    df = _mirror_structure_df(ascending=True)
    before = df.copy(deep=True)
    compute_primary_structure_component(df, "5m", _now_ms_all_closed(df, "5m"))
    pd.testing.assert_frame_equal(df, before)


# ------------------------------------------------------------------ descriptive volatility (ATR%)


def test_volatility_atr_percent_hand_computed():
    n = 20
    duration = TIMEFRAME_DURATION_MS["5m"]
    df = pd.DataFrame({
        "open_time": [i * duration for i in range(n)],
        "open": [100.0] * n,
        "high": [105.0] * n,
        "low": [95.0] * n,
        "close": [100.0] * n,
        "volume": [1000.0] * n,
    })
    now_ms = int(df["open_time"].iloc[-1]) + duration
    component = compute_volatility_component(df, "5m", now_ms)
    assert component.status == "AVAILABLE"
    # TR = max(high-low, |high-prev_close|, |low-prev_close|)
    #    = max(10, |105-100|, |95-100|) = max(10, 5, 5) = 10 for every bar
    # after the first (which has no prev_close and is excluded from the
    # bounded last-14 window here since sample_count > 15).
    # atr_percent = 100 * mean(last 14 TRs) / latest_close = 100*10/100 = 10.0
    assert component.atr_percent == pytest.approx(10.0)


def test_volatility_insufficient_rows_is_unavailable():
    n = 14  # one short of MIN_ATR_ROWS (15)
    duration = TIMEFRAME_DURATION_MS["5m"]
    df = pd.DataFrame({
        "open_time": [i * duration for i in range(n)],
        "open": [100.0] * n, "high": [105.0] * n, "low": [95.0] * n,
        "close": [100.0] * n, "volume": [1000.0] * n,
    })
    now_ms = int(df["open_time"].iloc[-1]) + duration
    component = compute_volatility_component(df, "5m", now_ms)
    assert component.status == "UNAVAILABLE"
    assert component.reason == "INSUFFICIENT_ROWS_FOR_ATR"


def test_volatility_missing_df_is_unavailable():
    component = compute_volatility_component(None, "5m", 1_000_000)
    assert component.status == "UNAVAILABLE"
    assert component.atr_percent is None


# ------------------------------------------------------------------ assembly / serialization


def test_assemble_context_and_serialize_is_deterministic_and_json_safe():
    df15 = _uptrend_15m_df()
    df_primary = _mirror_structure_df(ascending=True)
    now_ms = _now_ms_all_closed(df_primary, "5m")

    symbol_htf_trend = compute_symbol_trend_component(df15, now_ms)
    primary_structure = compute_primary_structure_component(df_primary, "5m", now_ms)
    btc_htf_trend = not_applicable_trend_component()
    volatility = compute_volatility_component(df_primary, "5m", now_ms)

    context = assemble_context(
        as_of_ms=now_ms,
        primary_timeframe="5m",
        scored_direction="LONG",
        symbol_htf_trend=symbol_htf_trend,
        primary_structure=primary_structure,
        btc_htf_trend=btc_htf_trend,
        volatility=volatility,
    )

    d1 = context_to_dict(context)
    d2 = context_to_dict(context)
    assert d1 == d2

    serialized = json.dumps(d1, allow_nan=False)
    round_tripped = json.loads(serialized)
    assert round_tripped["scored_direction"] == "LONG"
    assert round_tripped["primary_timeframe"] == "5m"
    assert round_tripped["btc_htf_trend"]["status"] == "NOT_APPLICABLE"
    assert math.isfinite(round_tripped["as_of_ms"])
