"""Tests for the SUPPORT_RESISTANCE_REJECTION detector (TA Opportunity Engine
v0.1 research-only). Covers: exact LONG/SHORT positive cases and symmetry,
strict prior/final close boundaries, inclusive touch, ratio-threshold
boundary behavior, zero-range candles, non-positive zone-boundary fail-closed
division safety, source-candle exclusion from zone construction and
zone-input invariance across differing source candles, fingerprint identity
(repeated input, later rejection at the same zone), multi-zone nearest
selection and deterministic tie-breaking, contradictory dual-direction
fail-closed behavior, input validation (including duplicate required column
labels and unhashable timeframe values), bounded-window malformed-row
scoping, real Support/Resistance primitive integration plus front-window
eviction, and contract/JSON/no-wiring/performance checks.

`find_support_resistance_zones` (as bound inside the detector module) is
monkeypatched for tests that isolate detector geometry/selection logic from
zone construction; the real primitive is exercised directly for the
integration and eviction tests.
"""
from __future__ import annotations

import inspect
import json
import time
from typing import Optional, get_args

import pandas as pd
import pytest

import apex.opportunity.detectors.support_resistance_rejection as srr
from apex.opportunity.contract import (
    DIRECTION_INVARIANT_FAMILIES,
    PRIMARY_TIMEFRAME_DURATION_MS,
    SetupFamily,
)
from apex.opportunity.detectors.support_resistance_rejection import (
    DETECTOR_VERSION,
    MIN_CANDLES_REQUIRED,
    detect_support_resistance_rejection,
)
from apex.opportunity.support_resistance import (
    LOOKBACK_CANDLES,
    SupportResistanceZone,
    find_support_resistance_zones,
)


# ------------------------------------------------------------------ helpers

def _row(o: float, h: float, low: float, c: float) -> dict:
    return {"open": o, "high": h, "low": low, "close": c}


def _build_df(prev: dict, src: dict, filler_count: Optional[int] = None) -> pd.DataFrame:
    """`filler_count` valid flat baseline rows (far from any test's zone
    prices), followed by `prev` then `src` as the final two rows — the only
    two rows most tests actually vary.
    """
    if filler_count is None:
        filler_count = MIN_CANDLES_REQUIRED - 2
    rows = []
    for t in range(filler_count):
        rows.append({"open_time": t, "open": 50.0, "high": 50.5, "low": 49.5, "close": 50.0})
    rows.append({"open_time": filler_count, **prev})
    rows.append({"open_time": filler_count + 1, **src})
    return pd.DataFrame(rows)


def _valid_prev_src() -> tuple[dict, dict]:
    return _row(99.6, 99.7, 99.4, 99.6), _row(100.8, 101.0, 99.0, 99.5)


def _zone(
    lower: float,
    upper: float,
    center: Optional[float] = None,
    first_touch: int = 10,
    most_recent: int = 15,
    pivot_count: int = 2,
    pivot_times: Optional[tuple] = None,
) -> SupportResistanceZone:
    if center is None:
        center = (lower + upper) / 2
    if pivot_times is None:
        pivot_times = (first_touch, most_recent)
    return SupportResistanceZone(
        timeframe="5m",
        center=center,
        lower=lower,
        upper=upper,
        role=None,
        distance_pct=None,
        contributing_pivot_count=pivot_count,
        pivot_open_times=tuple(pivot_times),
        first_touch_open_time=first_touch,
        most_recent_touch_open_time=most_recent,
    )


ZONE_LOWER = 100.0
ZONE_UPPER = 100.3


def _short_zone() -> SupportResistanceZone:
    return _zone(lower=ZONE_LOWER, upper=ZONE_UPPER, first_touch=10, most_recent=15)


def _resistance_rejection_df(pad_rows: int = 0) -> pd.DataFrame:
    """`pad_rows` extra baseline rows, then 50 zone-construction rows with
    two confirmed resistance-pivot touches (price 105.0) at relative
    positions 20 and 35, then one crafted SHORT-rejection source row.
    """
    rows = []
    t = 0
    for _ in range(pad_rows):
        rows.append({"open_time": t, "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0})
        t += 1
    for i in range(50):
        high = 105.0 if i in (20, 35) else 100.5
        rows.append({"open_time": t, "open": 100.0, "high": high, "low": 99.5, "close": 100.0})
        t += 1
    rows.append({"open_time": t, "open": 104.0, "high": 105.0, "low": 99.0, "close": 100.5})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ positive cases / symmetry / contract fields

def test_short_and_long_rejection_positive_cases_are_symmetric(monkeypatch):
    short_zone = _zone(lower=100.0, upper=100.3, first_touch=10, most_recent=15)
    monkeypatch.setattr(srr, "find_support_resistance_zones", lambda *a, **k: (short_zone,))
    prev_short = _row(99.6, 99.7, 99.4, 99.6)
    src_short = _row(100.8, 101.0, 99.0, 99.5)
    df_short = _build_df(prev_short, src_short)
    short_findings = detect_support_resistance_rejection("BTC", "5m", df_short)
    assert len(short_findings) == 1
    short_finding = short_findings[0]
    assert short_finding.direction == "SHORT"
    assert short_finding.symbol == "BTC"
    assert short_finding.setup_family == "SUPPORT_RESISTANCE_REJECTION"
    assert short_finding.detector_version == DETECTOR_VERSION
    assert short_finding.primary_timeframe == "5m"
    assert short_finding.anchor_price == short_zone.lower
    assert short_finding.anchor_open_time == short_zone.first_touch_open_time
    assert short_finding.source_candle_close_time == (
        short_finding.source_candle_open_time + PRIMARY_TIMEFRAME_DURATION_MS["5m"]
    )
    assert short_finding.fingerprint_key == (
        f"{short_zone.first_touch_open_time}:{short_finding.source_candle_open_time}"
    )
    assert short_finding.evidence["zone_lower"] == short_zone.lower
    assert short_finding.evidence["zone_upper"] == short_zone.upper
    assert short_finding.evidence["zone_pivot_open_times"] == list(short_zone.pivot_open_times)
    assert short_finding.evidence["prior_close"] == 99.6
    assert short_finding.evidence["source_extreme"] == 101.0
    assert short_finding.evidence["source_close"] == 99.5
    assert short_finding.warnings == []

    long_zone = _zone(lower=99.7, upper=100.0, first_touch=10, most_recent=15)
    monkeypatch.setattr(srr, "find_support_resistance_zones", lambda *a, **k: (long_zone,))
    prev_long = _row(100.4, 100.5, 100.3, 100.4)
    src_long = _row(99.2, 101.0, 99.0, 100.5)
    df_long = _build_df(prev_long, src_long)
    long_findings = detect_support_resistance_rejection("BTC", "5m", df_long)
    assert len(long_findings) == 1
    long_finding = long_findings[0]
    assert long_finding.direction == "LONG"
    assert long_finding.anchor_price == long_zone.upper
    assert long_finding.anchor_open_time == long_zone.first_touch_open_time

    assert short_finding.measurements["rejection_range_ratio"] == pytest.approx(0.75)
    assert long_finding.measurements["rejection_range_ratio"] == pytest.approx(0.75)
    assert short_finding.measurements["rejection_range_ratio"] == pytest.approx(
        long_finding.measurements["rejection_range_ratio"]
    )


# ------------------------------------------------------------------ boundary conditions

def test_prior_close_not_strictly_below_zone_fails(monkeypatch):
    monkeypatch.setattr(srr, "find_support_resistance_zones", lambda *a, **k: (_short_zone(),))
    prev = _row(100.0, 100.1, 99.9, 100.0)  # prev.close == zone.lower, not strictly below
    src = _row(100.8, 101.0, 99.0, 99.5)
    df = _build_df(prev, src)
    assert detect_support_resistance_rejection("BTC", "5m", df) == []


def test_final_close_not_strictly_below_zone_fails(monkeypatch):
    monkeypatch.setattr(srr, "find_support_resistance_zones", lambda *a, **k: (_short_zone(),))
    prev = _row(99.6, 99.7, 99.4, 99.6)
    src = _row(100.8, 101.0, 99.0, 100.0)  # src.close == zone.lower, not strictly below
    df = _build_df(prev, src)
    assert detect_support_resistance_rejection("BTC", "5m", df) == []


def test_touch_exactly_at_lower_boundary_is_inclusive(monkeypatch):
    monkeypatch.setattr(srr, "find_support_resistance_zones", lambda *a, **k: (_short_zone(),))
    prev = _row(99.6, 99.7, 99.4, 99.6)
    src = _row(99.8, 100.0, 99.0, 99.5)  # src.high == zone.lower exactly
    df = _build_df(prev, src)
    findings = detect_support_resistance_rejection("BTC", "5m", df)
    assert len(findings) == 1
    assert findings[0].direction == "SHORT"


def test_rejection_ratio_exactly_at_threshold_qualifies(monkeypatch):
    monkeypatch.setattr(srr, "find_support_resistance_zones", lambda *a, **k: (_short_zone(),))
    prev = _row(99.0, 99.1, 98.9, 99.0)
    src = _row(99.5, 100.0, 98.0, 99.0)  # ratio = (100-99)/(100-98) = 0.5 exactly
    df = _build_df(prev, src)
    findings = detect_support_resistance_rejection("BTC", "5m", df, min_rejection_range_ratio=0.5)
    assert len(findings) == 1
    assert findings[0].measurements["rejection_range_ratio"] == pytest.approx(0.5)


def test_rejection_ratio_just_below_threshold_fails(monkeypatch):
    monkeypatch.setattr(srr, "find_support_resistance_zones", lambda *a, **k: (_short_zone(),))
    prev = _row(99.0, 99.1, 98.9, 99.0)
    src = _row(99.5, 100.0, 98.0, 99.01)  # ratio = (100-99.01)/2 = 0.495 < 0.5
    df = _build_df(prev, src)
    findings = detect_support_resistance_rejection("BTC", "5m", df, min_rejection_range_ratio=0.5)
    assert findings == []


def test_zero_range_source_candle_no_finding(monkeypatch):
    monkeypatch.setattr(srr, "find_support_resistance_zones", lambda *a, **k: (_short_zone(),))
    prev = _row(80.0, 80.5, 79.5, 80.0)
    src = _row(100.0, 100.0, 100.0, 100.0)  # sits at the boundary but has zero range
    df = _build_df(prev, src)
    assert detect_support_resistance_rejection("BTC", "5m", df) == []


def test_short_trigger_with_non_positive_zone_upper_fails_closed(monkeypatch):
    # zone.lower (100.0) would otherwise qualify a SHORT rejection; the
    # corrupted zone.upper must still block it before any division happens.
    corrupted_zone = _zone(lower=100.0, upper=-5.0, first_touch=10, most_recent=15)
    monkeypatch.setattr(srr, "find_support_resistance_zones", lambda *a, **k: (corrupted_zone,))
    prev, src = _valid_prev_src()
    df = _build_df(prev, src)
    assert detect_support_resistance_rejection("BTC", "5m", df) == []


def test_long_trigger_with_non_positive_zone_lower_fails_closed(monkeypatch):
    # zone.upper (100.0) would otherwise qualify a LONG rejection; the
    # corrupted zone.lower must still block it before any division happens.
    corrupted_zone = _zone(lower=-1.0, upper=100.0, first_touch=10, most_recent=15)
    monkeypatch.setattr(srr, "find_support_resistance_zones", lambda *a, **k: (corrupted_zone,))
    prev = _row(100.4, 100.5, 100.3, 100.4)
    src = _row(99.2, 101.0, 99.0, 100.5)
    df = _build_df(prev, src)
    assert detect_support_resistance_rejection("BTC", "5m", df) == []


# ------------------------------------------------------------------ no-lookahead: source exclusion / no future dependence

def test_source_candle_excluded_from_zone_construction(monkeypatch):
    captured: dict = {}

    def fake_find_zones(zone_df, timeframe):
        captured["zone_df"] = zone_df.copy()
        captured["timeframe"] = timeframe
        return ()

    monkeypatch.setattr(srr, "find_support_resistance_zones", fake_find_zones)

    prev, src = _valid_prev_src()
    df = _build_df(prev, src)
    detect_support_resistance_rejection("BTC", "5m", df)

    zone_df = captured["zone_df"]
    assert len(zone_df) == len(df) - 1
    assert int(zone_df["open_time"].iloc[-1]) == int(df["open_time"].iloc[-2])
    assert int(df["open_time"].iloc[-1]) not in zone_df["open_time"].to_numpy()
    assert captured["timeframe"] == "5m"


def test_zone_construction_input_unaffected_by_source_candle_values(monkeypatch):
    """The candidate source candle must never leak into the DataFrame used to
    build zones. Proven directly: capture the exact `zone_df` passed to
    `find_support_resistance_zones` for two runs that share identical
    preceding history but differ only in the final (source) row, and assert
    the captured input is identical and never contains the source row.
    """
    captured: list[pd.DataFrame] = []

    def fake_find_zones(zone_df, timeframe):
        captured.append(zone_df.copy())
        return ()

    monkeypatch.setattr(srr, "find_support_resistance_zones", fake_find_zones)

    prev, src_a = _valid_prev_src()
    src_b = _row(50.0, 50.5, 49.5, 50.0)

    df_a = _build_df(prev, src_a)
    detect_support_resistance_rejection("BTC", "5m", df_a)

    df_b = _build_df(prev, src_b)
    detect_support_resistance_rejection("BTC", "5m", df_b)

    assert len(captured) == 2
    pd.testing.assert_frame_equal(captured[0], captured[1])
    assert int(df_a["open_time"].iloc[-1]) not in captured[0]["open_time"].to_numpy()
    assert int(df_b["open_time"].iloc[-1]) not in captured[1]["open_time"].to_numpy()


# ------------------------------------------------------------------ fingerprint identity

def test_repeated_identical_input_yields_identical_finding(monkeypatch):
    zone = _short_zone()
    monkeypatch.setattr(srr, "find_support_resistance_zones", lambda *a, **k: (zone,))
    prev, src = _valid_prev_src()
    df = _build_df(prev, src)

    f1 = detect_support_resistance_rejection("BTC", "5m", df)[0]
    f2 = detect_support_resistance_rejection("BTC", "5m", df)[0]
    assert f1 == f2


def test_later_rejection_same_zone_gets_distinct_fingerprint(monkeypatch):
    zone = _short_zone()
    monkeypatch.setattr(srr, "find_support_resistance_zones", lambda *a, **k: (zone,))

    prev1, src1 = _valid_prev_src()
    df1 = _build_df(prev1, src1)
    finding1 = detect_support_resistance_rejection("BTC", "5m", df1)[0]

    prev2, src2 = _valid_prev_src()
    df2 = _build_df(prev2, src2, filler_count=MIN_CANDLES_REQUIRED + 5)
    finding2 = detect_support_resistance_rejection("BTC", "5m", df2)[0]

    assert finding1.fingerprint_key != finding2.fingerprint_key
    assert finding1.fingerprint_key.startswith(f"{zone.first_touch_open_time}:")
    assert finding2.fingerprint_key.startswith(f"{zone.first_touch_open_time}:")


# ------------------------------------------------------------------ multi-zone selection / tie-breaking

def test_multiple_zone_nearest_short_selected(monkeypatch):
    near_zone = _zone(lower=100.0, upper=100.3, center=100.15, first_touch=10, most_recent=15)
    far_zone = _zone(lower=105.0, upper=105.3, center=105.15, first_touch=20, most_recent=25)
    monkeypatch.setattr(
        srr, "find_support_resistance_zones", lambda *a, **k: (far_zone, near_zone)
    )

    prev = _row(99.6, 99.7, 99.4, 99.6)
    src = _row(100.8, 106.0, 99.0, 99.5)
    df = _build_df(prev, src)
    findings = detect_support_resistance_rejection("BTC", "5m", df)
    assert len(findings) == 1
    assert findings[0].anchor_price == near_zone.lower


def test_tie_break_by_center_when_distance_equal(monkeypatch):
    zone_a = _zone(lower=100.0, upper=100.3, center=100.15, first_touch=10, most_recent=15)
    zone_b = _zone(lower=100.0, upper=100.3, center=100.10, first_touch=30, most_recent=35)
    monkeypatch.setattr(srr, "find_support_resistance_zones", lambda *a, **k: (zone_a, zone_b))

    prev, src = _valid_prev_src()
    df = _build_df(prev, src)
    findings = detect_support_resistance_rejection("BTC", "5m", df)
    assert len(findings) == 1
    assert findings[0].anchor_open_time == zone_b.first_touch_open_time


def test_tie_break_by_first_touch_when_distance_and_center_equal(monkeypatch):
    zone_a = _zone(lower=100.0, upper=100.3, center=100.15, first_touch=20, most_recent=25)
    zone_b = _zone(lower=100.0, upper=100.3, center=100.15, first_touch=10, most_recent=30)
    monkeypatch.setattr(srr, "find_support_resistance_zones", lambda *a, **k: (zone_a, zone_b))

    prev, src = _valid_prev_src()
    df = _build_df(prev, src)
    findings = detect_support_resistance_rejection("BTC", "5m", df)
    assert findings[0].anchor_open_time == zone_b.first_touch_open_time


def test_tie_break_by_most_recent_touch_when_all_else_equal(monkeypatch):
    zone_a = _zone(lower=100.0, upper=100.3, center=100.15, first_touch=10, most_recent=25)
    zone_b = _zone(lower=100.0, upper=100.3, center=100.15, first_touch=10, most_recent=20)
    monkeypatch.setattr(srr, "find_support_resistance_zones", lambda *a, **k: (zone_a, zone_b))

    prev, src = _valid_prev_src()
    df = _build_df(prev, src)
    findings = detect_support_resistance_rejection("BTC", "5m", df)
    assert (
        findings[0].evidence["zone_most_recent_touch_open_time"]
        == zone_b.most_recent_touch_open_time
    )


# ------------------------------------------------------------------ contradictory dual-direction

def test_dual_direction_qualification_returns_empty(monkeypatch):
    short_zone = _zone(lower=100.5, upper=100.8, first_touch=5, most_recent=6)
    long_zone = _zone(lower=99.0, upper=99.5, first_touch=7, most_recent=8)
    monkeypatch.setattr(
        srr, "find_support_resistance_zones", lambda *a, **k: (short_zone, long_zone)
    )
    prev = _row(100.0, 100.1, 99.9, 100.0)
    src = _row(100.0, 101.0, 99.0, 100.0)
    df = _build_df(prev, src)
    assert detect_support_resistance_rejection("BTC", "5m", df) == []


# ------------------------------------------------------------------ input validation

def test_none_input_returns_empty():
    assert detect_support_resistance_rejection("BTC", "5m", None) == []


def test_non_dataframe_input_returns_empty():
    assert detect_support_resistance_rejection("BTC", "5m", "not-a-df") == []
    assert detect_support_resistance_rejection("BTC", "5m", [1, 2, 3]) == []


def test_blank_symbol_returns_empty():
    prev, src = _valid_prev_src()
    df = _build_df(prev, src)
    assert detect_support_resistance_rejection("", "5m", df) == []
    assert detect_support_resistance_rejection("   ", "5m", df) == []


def test_unsupported_timeframe_returns_empty():
    prev, src = _valid_prev_src()
    df = _build_df(prev, src)
    assert detect_support_resistance_rejection("BTC", "1h", df) == []
    assert detect_support_resistance_rejection("BTC", "", df) == []


def test_unhashable_timeframe_returns_empty_without_raising():
    prev, src = _valid_prev_src()
    df = _build_df(prev, src)
    assert detect_support_resistance_rejection("BTC", ["5m"], df) == []
    assert detect_support_resistance_rejection("BTC", {"5m": True}, df) == []


def test_invalid_ratio_returns_empty():
    prev, src = _valid_prev_src()
    df = _build_df(prev, src)
    for bad_ratio in (-0.01, 1.01, float("nan"), float("inf"), float("-inf"), "not-a-number", None):
        assert (
            detect_support_resistance_rejection("BTC", "5m", df, min_rejection_range_ratio=bad_ratio)
            == []
        )


def test_insufficient_rows_returns_empty():
    prev, src = _valid_prev_src()
    df = _build_df(prev, src, filler_count=MIN_CANDLES_REQUIRED - 3)
    assert len(df) == MIN_CANDLES_REQUIRED - 1
    assert detect_support_resistance_rejection("BTC", "5m", df) == []


def test_missing_column_returns_empty():
    prev, src = _valid_prev_src()
    df = _build_df(prev, src).drop(columns=["open"])
    assert detect_support_resistance_rejection("BTC", "5m", df) == []


def test_duplicate_required_column_label_returns_empty_without_raising():
    prev, src = _valid_prev_src()
    df = _build_df(prev, src)
    df = pd.concat([df, df[["open"]]], axis=1)  # "open" now appears twice
    assert list(df.columns).count("open") == 2
    assert detect_support_resistance_rejection("BTC", "5m", df) == []


def test_non_finite_relevant_value_returns_empty():
    prev, src = _valid_prev_src()
    src_bad = dict(src)
    src_bad["high"] = float("nan")
    assert detect_support_resistance_rejection("BTC", "5m", _build_df(prev, src_bad)) == []

    src_bad2 = dict(src)
    src_bad2["low"] = float("inf")
    assert detect_support_resistance_rejection("BTC", "5m", _build_df(prev, src_bad2)) == []


def test_non_positive_relevant_value_returns_empty():
    prev, src = _valid_prev_src()
    src_bad = dict(src)
    src_bad["close"] = 0.0
    assert detect_support_resistance_rejection("BTC", "5m", _build_df(prev, src_bad)) == []

    src_bad2 = dict(src)
    src_bad2["open"] = -1.0
    assert detect_support_resistance_rejection("BTC", "5m", _build_df(prev, src_bad2)) == []


def test_row_open_close_outside_high_low_returns_empty():
    prev, src = _valid_prev_src()
    src_bad = dict(src)
    src_bad["open"] = src_bad["high"] + 1.0  # open > high
    assert detect_support_resistance_rejection("BTC", "5m", _build_df(prev, src_bad)) == []

    src_bad2 = dict(src)
    src_bad2["close"] = src_bad2["low"] - 1.0  # close < low
    assert detect_support_resistance_rejection("BTC", "5m", _build_df(prev, src_bad2)) == []


def test_fractional_open_time_returns_empty():
    prev, src = _valid_prev_src()
    df = _build_df(prev, src)
    df.loc[df.index[-1], "open_time"] = float(df["open_time"].iloc[-1]) + 0.5
    assert detect_support_resistance_rejection("BTC", "5m", df) == []


def test_duplicate_open_time_returns_empty():
    prev, src = _valid_prev_src()
    df = _build_df(prev, src)
    df.loc[df.index[-1], "open_time"] = df["open_time"].iloc[-2]
    assert detect_support_resistance_rejection("BTC", "5m", df) == []


def test_non_monotonic_open_time_returns_empty():
    prev, src = _valid_prev_src()
    df = _build_df(prev, src)
    times = df["open_time"].tolist()
    times[-1], times[-2] = times[-2], times[-1]
    df["open_time"] = times
    assert detect_support_resistance_rejection("BTC", "5m", df) == []


# ------------------------------------------------------------------ bounded-window malformed-row scoping

def test_malformed_row_outside_window_is_ignored(monkeypatch):
    zone = _short_zone()
    monkeypatch.setattr(srr, "find_support_resistance_zones", lambda *a, **k: (zone,))

    prev, src = _valid_prev_src()
    total_n = LOOKBACK_CANDLES + 1 + 15  # 15 rows beyond the bounded window
    df = _build_df(prev, src, filler_count=total_n - 2)
    df.loc[0, "close"] = float("nan")  # falls outside df.tail(LOOKBACK_CANDLES + 1)

    findings = detect_support_resistance_rejection("BTC", "5m", df)
    assert len(findings) == 1


def test_malformed_row_inside_window_blocks_detection(monkeypatch):
    zone = _short_zone()
    monkeypatch.setattr(srr, "find_support_resistance_zones", lambda *a, **k: (zone,))

    prev, src = _valid_prev_src()
    total_n = LOOKBACK_CANDLES + 1 + 15
    df = _build_df(prev, src, filler_count=total_n - 2)
    window_start_pos = total_n - (LOOKBACK_CANDLES + 1)
    df.loc[window_start_pos, "close"] = float("nan")  # first row of the bounded window itself

    assert detect_support_resistance_rejection("BTC", "5m", df) == []


# ------------------------------------------------------------------ real Support/Resistance primitive integration + eviction

def test_real_primitive_integration_short_rejection():
    df = _resistance_rejection_df(pad_rows=0)

    zone_df = df.iloc[:-1]
    expected_zones = find_support_resistance_zones(zone_df, "5m")
    matches = [z for z in expected_zones if z.pivot_open_times == (20, 35)]
    assert len(matches) == 1
    expected_zone = matches[0]

    findings = detect_support_resistance_rejection("BTC", "5m", df)
    assert len(findings) == 1
    f = findings[0]
    assert f.direction == "SHORT"
    assert f.anchor_price == expected_zone.lower
    assert f.anchor_open_time == expected_zone.first_touch_open_time
    assert f.fingerprint_key == f"{expected_zone.first_touch_open_time}:{f.source_candle_open_time}"
    assert f.evidence["zone_contributing_pivot_count"] == 2


def test_front_eviction_removes_zone_and_finding():
    """Same structural touches as the integration test above, but with 190
    ordinary bars inserted between the touches and the source candle so the
    detector's own `df.tail(LOOKBACK_CANDLES + 1)` window no longer contains
    the older touch (open_time 20) — leaving only one touch, below
    `MIN_PIVOT_TOUCHES`, so the zone (and thus the finding) disappears even
    though the source candle's own geometry is unchanged.
    """
    gap = 190
    rows = []
    t = 0
    for i in range(36):
        high = 105.0 if i in (20, 35) else 100.5
        rows.append({"open_time": t, "open": 100.0, "high": high, "low": 99.5, "close": 100.0})
        t += 1
    for _ in range(gap):
        rows.append({"open_time": t, "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0})
        t += 1
    rows.append({"open_time": t, "open": 104.0, "high": 105.0, "low": 99.0, "close": 100.5})
    df = pd.DataFrame(rows)

    zone_df = df.iloc[:-1]
    real_zones = find_support_resistance_zones(zone_df, "5m")
    assert not any(z.pivot_open_times == (20, 35) for z in real_zones)

    assert detect_support_resistance_rejection("BTC", "5m", df) == []


# ------------------------------------------------------------------ contract / JSON / no-wiring / performance

def test_setup_family_and_direction_invariance_contract():
    assert "SUPPORT_RESISTANCE_REJECTION" in get_args(SetupFamily)
    assert "SUPPORT_RESISTANCE_REJECTION" not in DIRECTION_INVARIANT_FAMILIES


def test_evidence_and_measurements_are_json_serializable(monkeypatch):
    zone = _short_zone()
    monkeypatch.setattr(srr, "find_support_resistance_zones", lambda *a, **k: (zone,))
    prev, src = _valid_prev_src()
    df = _build_df(prev, src)
    finding = detect_support_resistance_rejection("BTC", "5m", df)[0]

    evidence_json = json.dumps(finding.evidence)
    measurements_json = json.dumps(finding.measurements)
    assert json.loads(evidence_json) == finding.evidence
    assert json.loads(measurements_json) == finding.measurements


def test_detector_module_has_no_runtime_wiring_imports():
    source = inspect.getsource(srr)
    forbidden_substrings = [
        "apex.opportunity.engine",
        "apex.scheduler",
        "candle_store",
        "notifications",
        "pushover",
        "repository",
        "trading",
    ]
    for token in forbidden_substrings:
        assert token not in source


MAX_SECONDS = 10.0  # generous bound; actual runtime is expected to be well under 1s


def test_bounded_performance_large_input():
    df = _resistance_rejection_df(pad_rows=4949)  # 5000 rows total
    assert len(df) == 5000

    start = time.perf_counter()
    total_calls = 0
    for _ in range(20):
        findings = detect_support_resistance_rejection("BTC", "5m", df)
        assert len(findings) == 1
        total_calls += 1
    elapsed = time.perf_counter() - start

    assert total_calls == 20
    assert elapsed < MAX_SECONDS, f"20 calls over 5000 rows took {elapsed:.3f}s, expected well under {MAX_SECONDS}s"
