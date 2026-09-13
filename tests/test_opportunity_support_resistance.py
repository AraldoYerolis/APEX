"""Tests for the Support / Resistance zone primitive (TA Opportunity Engine
v0.1 research-only). Covers: valid zone construction and immutability,
SUPPORT/RESISTANCE/AT_PRICE/no-reference classification, input validation
(missing columns, blank timeframe, invalid reference, too-short input,
non-finite/non-positive/inverted OHLC, non-integral/duplicate/unsorted
open_time), no-lookahead tail exclusion and evidence stability across
appends, bounded-window front eviction, tolerance-boundary clustering,
complete-link chain splitting, mixed HIGH/LOW zone membership, deterministic
ordering, flat-series behavior, duplicate-touch-time rejection, and a
bounded-performance exercise.

`find_pivots` (imported into this module) is monkeypatched only for the
precise clustering-boundary, mixed-kind, duplicate-touch, and
transitive-chain contract tests; every no-lookahead, rolling-window, and
validation test uses real synthetic closed candles run through the actual
pivot detector.
"""
from __future__ import annotations

import dataclasses
import time

import pandas as pd
import pytest

import apex.opportunity.support_resistance as sr
from apex.indicators.atr import true_range
from apex.opportunity.features import Pivot
from apex.opportunity.support_resistance import (
    SupportResistanceZone,
    find_support_resistance_zones,
)


# ------------------------------------------------------------------ helpers

def _base_candles(n: int, price: float = 100.0, spread: float = 0.5) -> pd.DataFrame:
    """Flat closed candles: constant true range (1.0), so every pivot built
    from these rows gets tolerance == max(0.5 * 1.0, 0.0025 * price) == 0.5
    for any price under 200 — a fixed, hand-checkable tolerance.
    """
    return pd.DataFrame({
        "open_time": list(range(n)),
        "open": [price] * n,
        "high": [price + spread] * n,
        "low": [price - spread] * n,
        "close": [price] * n,
        "volume": [1000.0] * n,
    })


def _spike_low(df: pd.DataFrame, index: int, low_price: float) -> None:
    df.loc[index, "low"] = low_price


def _low_spike_df(n: int, spike_open_times: list[int], spike_price: float = 90.0) -> pd.DataFrame:
    df = _base_candles(n)
    for t in spike_open_times:
        _spike_low(df, t, spike_price)
    return df


def _pivot(open_time: int, price: float, kind: str, index: int) -> Pivot:
    return Pivot(kind=kind, index=index, open_time=open_time, price=price)


def _valid_ohlc_df(n: int = 20) -> pd.DataFrame:
    closes = [100.0 + (i % 3) * 0.01 for i in range(n)]
    return pd.DataFrame({
        "open_time": list(range(n)),
        "open": closes,
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "volume": [1000.0] * n,
    })


# ------------------------------------------------------------------ zone construction / immutability

def test_zone_construction_and_immutable_output(monkeypatch):
    df = _base_candles(30)
    pivots = [
        _pivot(14, 100.0, "HIGH", 14),
        _pivot(20, 100.2, "LOW", 20),
    ]
    monkeypatch.setattr(sr, "find_pivots", lambda *a, **k: pivots)

    zones = find_support_resistance_zones(df, "5m")
    assert isinstance(zones, tuple)
    assert len(zones) == 1
    zone = zones[0]
    assert isinstance(zone, SupportResistanceZone)
    assert zone.timeframe == "5m"
    assert zone.lower == pytest.approx(99.7)
    assert zone.upper == pytest.approx(100.5)
    assert zone.center == pytest.approx(100.1)
    assert zone.contributing_pivot_count == 2
    assert zone.pivot_open_times == (14, 20)
    assert zone.first_touch_open_time == 14
    assert zone.most_recent_touch_open_time == 20
    assert isinstance(zone.pivot_open_times, tuple)

    with pytest.raises(dataclasses.FrozenInstanceError):
        zone.center = 999.0


# ------------------------------------------------------------------ classification / distance

def _classification_zone(monkeypatch, reference_price):
    df = _base_candles(30)
    pivots = [
        _pivot(14, 100.0, "HIGH", 14),
        _pivot(20, 100.2, "LOW", 20),
    ]
    monkeypatch.setattr(sr, "find_pivots", lambda *a, **k: pivots)
    zones = find_support_resistance_zones(df, "5m", reference_price=reference_price)
    assert len(zones) == 1
    return zones[0]


def test_classification_support_when_reference_above_zone(monkeypatch):
    zone = _classification_zone(monkeypatch, 200.0)
    assert zone.role == "SUPPORT"
    assert zone.distance_pct == pytest.approx((100.1 - 200.0) / 200.0 * 100)


def test_classification_resistance_when_reference_below_zone(monkeypatch):
    zone = _classification_zone(monkeypatch, 50.0)
    assert zone.role == "RESISTANCE"
    assert zone.distance_pct == pytest.approx((100.1 - 50.0) / 50.0 * 100)


def test_classification_at_price_when_reference_inside_zone(monkeypatch):
    zone = _classification_zone(monkeypatch, 100.1)
    assert zone.role == "AT_PRICE"
    assert zone.distance_pct == pytest.approx(0.0, abs=1e-9)


def test_classification_none_when_no_reference_supplied(monkeypatch):
    zone = _classification_zone(monkeypatch, None)
    assert zone.role is None
    assert zone.distance_pct is None


# ------------------------------------------------------------------ input validation

def test_missing_required_column_returns_empty():
    df = _valid_ohlc_df().drop(columns=["high"])
    assert find_support_resistance_zones(df, "5m") == ()


def test_blank_timeframe_returns_empty():
    df = _valid_ohlc_df()
    assert find_support_resistance_zones(df, "") == ()
    assert find_support_resistance_zones(df, "   ") == ()


def test_invalid_reference_price_returns_empty():
    df = _valid_ohlc_df()
    for bad_reference in (0.0, -5.0, float("nan"), float("inf"), "not-a-number"):
        assert find_support_resistance_zones(df, "5m", reference_price=bad_reference) == ()


def test_too_short_input_returns_empty():
    df = _valid_ohlc_df(n=10)
    assert find_support_resistance_zones(df, "5m") == ()


def test_nan_ohlc_value_returns_empty():
    df = _valid_ohlc_df()
    df.loc[5, "high"] = float("nan")
    assert find_support_resistance_zones(df, "5m") == ()


def test_inf_ohlc_value_returns_empty():
    df = _valid_ohlc_df()
    df.loc[5, "low"] = float("inf")
    assert find_support_resistance_zones(df, "5m") == ()


def test_non_positive_ohlc_value_returns_empty():
    df = _valid_ohlc_df()
    df.loc[5, "close"] = 0.0
    assert find_support_resistance_zones(df, "5m") == ()


def test_inverted_high_low_returns_empty():
    df = _valid_ohlc_df()
    df.loc[5, "high"] = 90.0
    df.loc[5, "low"] = 95.0
    assert find_support_resistance_zones(df, "5m") == ()


def test_non_integral_open_time_returns_empty():
    df = _valid_ohlc_df()
    df.loc[5, "open_time"] = 5.5
    assert find_support_resistance_zones(df, "5m") == ()


def test_duplicate_open_time_returns_empty():
    df = _valid_ohlc_df()
    df.loc[5, "open_time"] = df.loc[4, "open_time"]
    assert find_support_resistance_zones(df, "5m") == ()


def test_unsorted_open_time_returns_empty():
    df = _valid_ohlc_df()
    times = df["open_time"].tolist()
    times[5], times[6] = times[6], times[5]
    df["open_time"] = times
    assert find_support_resistance_zones(df, "5m") == ()


# ------------------------------------------------------------------ no-lookahead tail exclusion / evidence stability

def test_unconfirmed_tail_pivot_excluded():
    n = 40
    df = _low_spike_df(n, [15, 25])
    # right_bars=3 needs 3 confirmed candles after a pivot; a spike 2 bars
    # from the end has none, so it must never contribute to any zone yet.
    _spike_low(df, n - 2, 90.0)

    zones = find_support_resistance_zones(df, "5m")
    assert len(zones) == 1
    assert zones[0].pivot_open_times == (15, 25)
    assert (n - 2) not in zones[0].pivot_open_times


def test_retained_evidence_stability_after_ordinary_appends():
    n = 40
    df = _low_spike_df(n, [15, 25])

    zones_before = find_support_resistance_zones(df, "5m")
    matches_before = [z for z in zones_before if z.pivot_open_times == (15, 25)]
    assert len(matches_before) == 1
    zone_before = matches_before[0]

    extra = _base_candles(10)
    extra["open_time"] = range(n, n + 10)
    df_after = pd.concat([df, extra], ignore_index=True)

    zones_after = find_support_resistance_zones(df_after, "5m")
    matches_after = [z for z in zones_after if z.pivot_open_times == (15, 25)]
    assert len(matches_after) == 1
    assert matches_after[0] == zone_before


# ------------------------------------------------------------------ bounded-window front eviction

def test_front_eviction_shrinks_zone_when_oldest_touch_evicted():
    df1 = _low_spike_df(200, [50, 100, 150])
    zones1 = find_support_resistance_zones(df1, "5m")
    match1 = [z for z in zones1 if z.pivot_open_times == (50, 100, 150)]
    assert len(match1) == 1
    assert match1[0].contributing_pivot_count == 3

    extra = _base_candles(60)
    extra["open_time"] = range(200, 260)
    df2 = pd.concat([df1, extra], ignore_index=True)

    zones2 = find_support_resistance_zones(df2, "5m")
    match2 = [z for z in zones2 if 100 in z.pivot_open_times and 150 in z.pivot_open_times]
    assert len(match2) == 1
    zone2 = match2[0]
    assert zone2.pivot_open_times == (100, 150)
    assert zone2.contributing_pivot_count == 2
    assert zone2.first_touch_open_time == 100
    assert 50 not in zone2.pivot_open_times


def test_front_eviction_disappears_zone_below_min_touches():
    df1 = _low_spike_df(200, [50, 100, 150])
    extra = _base_candles(110)
    extra["open_time"] = range(200, 310)
    df3 = pd.concat([df1, extra], ignore_index=True)

    zones3 = find_support_resistance_zones(df3, "5m")
    assert not any(150 in z.pivot_open_times for z in zones3)


# ------------------------------------------------------------------ tolerance boundary / complete-link clustering

def test_exactly_at_tolerance_touches_join_same_zone(monkeypatch):
    df = _base_candles(30)
    pivots = [
        _pivot(14, 100.0, "HIGH", 14),
        _pivot(20, 100.5, "LOW", 20),  # diff == tolerance (0.5) exactly
    ]
    monkeypatch.setattr(sr, "find_pivots", lambda *a, **k: pivots)

    zones = find_support_resistance_zones(df, "5m")
    assert len(zones) == 1
    assert zones[0].pivot_open_times == (14, 20)
    assert zones[0].contributing_pivot_count == 2


def test_just_outside_tolerance_splits_into_separate_zones(monkeypatch):
    df = _base_candles(30)
    pivots = [
        _pivot(15, 100.0, "HIGH", 15),
        _pivot(16, 100.0, "LOW", 16),
        _pivot(20, 100.51, "HIGH", 20),  # diff from 100.0 == 0.51 > tolerance
        _pivot(21, 100.51, "LOW", 21),
    ]
    monkeypatch.setattr(sr, "find_pivots", lambda *a, **k: pivots)

    zones = find_support_resistance_zones(df, "5m")
    assert len(zones) == 2
    pivot_time_sets = {z.pivot_open_times for z in zones}
    assert pivot_time_sets == {(15, 16), (20, 21)}


def test_complete_link_splits_transitive_chain(monkeypatch):
    df = _base_candles(30)
    # A-B gap 0.3 and B-C gap 0.4 are each within the 0.5 tolerance, but the
    # A-C gap is 0.7 — single-link chaining would merge all four; complete
    # link must split them into two separate zones.
    pivots = [
        _pivot(15, 100.0, "HIGH", 15),   # A
        _pivot(16, 100.3, "LOW", 16),    # B
        _pivot(20, 100.7, "HIGH", 20),   # C
        _pivot(21, 100.7, "LOW", 21),    # D (duplicate touch of C's price)
    ]
    monkeypatch.setattr(sr, "find_pivots", lambda *a, **k: pivots)

    zones = find_support_resistance_zones(df, "5m")
    assert len(zones) == 2
    pivot_time_sets = {z.pivot_open_times for z in zones}
    assert pivot_time_sets == {(15, 16), (20, 21)}
    assert all(z.contributing_pivot_count == 2 for z in zones)


def test_high_and_low_pivots_share_one_structural_zone(monkeypatch):
    df = _base_candles(30)
    pivots = [
        _pivot(14, 100.0, "HIGH", 14),
        _pivot(20, 100.1, "LOW", 20),
    ]
    monkeypatch.setattr(sr, "find_pivots", lambda *a, **k: pivots)

    zones = find_support_resistance_zones(df, "5m")
    assert len(zones) == 1
    zone = zones[0]
    assert zone.contributing_pivot_count == 2
    assert zone.pivot_open_times == (14, 20)


# ------------------------------------------------------------------ true-range boundary repair regression

def test_gapped_first_slice_boundary_withholds_index_13_admits_index_14(monkeypatch):
    """A deliberately large jump between the candle evicted by df.tail(200)
    and the new windowed index-0 row proves row 0's true range is genuinely
    incomplete, not just theoretically incomplete: a pivot at windowed index
    13 (whose nominal 14-row true-range window is rows 0..13, including that
    incomplete row 0) must never reach a public zone. An otherwise identical
    pivot pair at windowed index 14 (whose window is rows 1..14, each with
    an in-slice previous close) must.
    """
    n_total = 220
    df = _base_candles(n_total)
    # Row 19 is the last row evicted by tail(200); row 20 becomes windowed
    # index 0. The gap between them (100.0 -> 300.0) would dominate windowed
    # index 0's true range if the evicted close were visible to it.
    df.loc[19, ["open", "high", "low", "close"]] = [100.0, 100.5, 99.5, 100.0]
    df.loc[20, ["open", "high", "low", "close"]] = [300.0, 300.5, 299.5, 300.0]

    windowed_row_19_close = df.loc[19, "close"]
    windowed_row_20_high = df.loc[20, "high"]
    windowed_row_20_low = df.loc[20, "low"]
    incomplete_tr_0 = true_range(
        pd.Series([windowed_row_20_high]),
        pd.Series([windowed_row_20_low]),
        pd.Series([float("nan")]),
    ).iloc[0]
    would_be_tr_0_with_full_history = max(
        windowed_row_20_high - windowed_row_20_low,
        abs(windowed_row_20_high - windowed_row_19_close),
        abs(windowed_row_20_low - windowed_row_19_close),
    )
    assert incomplete_tr_0 < would_be_tr_0_with_full_history

    withheld_price = 500.0
    admitted_price = 510.0
    pivots = [
        _pivot(open_time=33, price=withheld_price, kind="HIGH", index=13),
        _pivot(open_time=38, price=withheld_price + 0.1, kind="LOW", index=18),
        _pivot(open_time=34, price=admitted_price, kind="HIGH", index=14),
        _pivot(open_time=39, price=admitted_price + 0.1, kind="LOW", index=19),
    ]
    monkeypatch.setattr(sr, "find_pivots", lambda *a, **k: pivots)

    zones = find_support_resistance_zones(df, "5m")

    assert not any(withheld_price - 1 <= z.center <= withheld_price + 1 for z in zones)
    admitted = [z for z in zones if admitted_price - 1 <= z.center <= admitted_price + 1]
    assert len(admitted) == 1
    assert admitted[0].pivot_open_times == (34, 39)
    assert admitted[0].contributing_pivot_count == 2


# ------------------------------------------------------------------ ordering / flat series / duplicate touch time

def test_zones_returned_in_deterministic_ascending_center_order(monkeypatch):
    df = _base_candles(30)
    pivots = [
        _pivot(20, 105.0, "HIGH", 20),
        _pivot(21, 105.0, "LOW", 21),
        _pivot(15, 95.0, "HIGH", 15),
        _pivot(16, 95.0, "LOW", 16),
    ]
    monkeypatch.setattr(sr, "find_pivots", lambda *a, **k: pivots)

    zones = find_support_resistance_zones(df, "5m")
    assert len(zones) == 2
    centers = [z.center for z in zones]
    assert centers == sorted(centers)
    assert zones[0].center < zones[1].center


def test_flat_series_produces_no_zones():
    df = _base_candles(200)
    assert find_support_resistance_zones(df, "5m") == ()


def test_duplicate_touch_open_time_does_not_satisfy_two_touch_minimum(monkeypatch):
    df = _base_candles(30)
    pivots = [
        _pivot(14, 100.0, "HIGH", 14),
        _pivot(14, 100.1, "LOW", 14),  # same open_time as the pivot above
    ]
    monkeypatch.setattr(sr, "find_pivots", lambda *a, **k: pivots)

    zones = find_support_resistance_zones(df, "5m")
    assert zones == ()


# ------------------------------------------------------------------ bounded performance

N_SYMBOLS = 30
TIMEFRAMES = ("3m", "5m")
MAX_SECONDS = 10.0  # generous bound; actual runtime is expected to be well under 1s


def test_bounded_performance_over_30_symbols_2_timeframes():
    df = _low_spike_df(200, [50, 100])

    start = time.perf_counter()
    total_calls = 0
    for _symbol in range(N_SYMBOLS):
        for timeframe in TIMEFRAMES:
            zones = find_support_resistance_zones(df, timeframe, reference_price=100.0)
            assert isinstance(zones, tuple)
            total_calls += 1
    elapsed = time.perf_counter() - start

    assert total_calls == N_SYMBOLS * len(TIMEFRAMES)
    assert elapsed < MAX_SECONDS, (
        f"{N_SYMBOLS} symbols x {len(TIMEFRAMES)} timeframes took "
        f"{elapsed:.3f}s, expected well under {MAX_SECONDS}s"
    )
