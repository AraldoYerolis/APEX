"""Tests for the SUPPORT_RESISTANCE_BREAKOUT_RETEST detector (TA Opportunity
Engine v0.1 research-only). Covers: symmetric LONG/SHORT positive cases and
all finding fields, strict pre-breakout/breakout-close boundaries, inclusive
near/far source touch, far-side/held-boundary rejection, intermediate close
loss invalidating an event, adjacent/max-gap/one-bar-too-old breakout
windows, fingerprint identity (repeated input, later retest of the same
still-held breakout), most-recent-breakout selection over an earlier
invalidated attempt, deterministic multi-zone tie-breaking independent of
zone order, dual-direction fail-closed behavior, the full malformed-input
matrix (including duplicate required column labels and an unhashable
timeframe), malformed primitive output / invalid zone metadata fail-closed
behavior, bounded-window malformed-row scoping, real Support/Resistance
primitive integration plus front-window eviction, no-lookahead zone_df
invariance, and contract/JSON/no-wiring/performance checks.

`find_support_resistance_zones` (as bound inside the detector module) is
monkeypatched for tests that isolate detector event geometry/selection logic
from zone construction; the real primitive is exercised directly for the
integration and eviction tests.
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import time
from typing import Optional, get_args

import numpy as np
import pandas as pd
import pytest

import apex.opportunity.detectors.support_resistance_breakout_retest as sbr
from apex.opportunity.contract import (
    DIRECTION_INVARIANT_FAMILIES,
    PRIMARY_TIMEFRAME_DURATION_MS,
    SetupFamily,
)
from apex.opportunity.detectors.support_resistance_breakout_retest import (
    DEFAULT_MAX_RETEST_BARS,
    DETECTOR_VERSION,
    EVENT_WINDOW_CANDLES,
    MIN_CANDLES_REQUIRED,
    detect_support_resistance_breakout_retest,
)
from apex.opportunity.support_resistance import (
    LOOKBACK_CANDLES,
    MIN_ROWS_REQUIRED as SUPPORT_RESISTANCE_MIN_ROWS_REQUIRED,
    SupportResistanceZone,
    find_support_resistance_zones,
)


# ------------------------------------------------------------------ helpers

def _row(o: float, h: float, low: float, c: float) -> dict:
    return {"open": o, "high": h, "low": low, "close": c}


def _zone(
    lower: float,
    upper: float,
    center: Optional[float] = None,
    first_touch: int = 10,
    most_recent: int = 15,
    pivot_count: int = 2,
    pivot_times: Optional[tuple] = None,
    timeframe: str = "5m",
) -> SupportResistanceZone:
    if center is None:
        center = (lower + upper) / 2
    if pivot_times is None:
        pivot_times = (first_touch, most_recent)
    return SupportResistanceZone(
        timeframe=timeframe,
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


def _build_df(
    prior: dict,
    event_rows: list[dict],
    max_retest_bars: int = 1,
    filler_count: Optional[int] = None,
) -> pd.DataFrame:
    """`filler_count` valid flat baseline rows, then `prior` (the last
    zone_df row — the immediately-preceding candle for the earliest possible
    breakout), then exactly `max_retest_bars + 1` `event_rows` (the
    breakout-candidate rows followed by the source row).
    """
    event_window_candles = max_retest_bars + 1
    assert len(event_rows) == event_window_candles
    if filler_count is None:
        required_rows = SUPPORT_RESISTANCE_MIN_ROWS_REQUIRED + event_window_candles
        filler_count = required_rows - 1 - event_window_candles
    rows = []
    t = 0
    for _ in range(filler_count):
        rows.append({"open_time": t, "open": 50.0, "high": 50.5, "low": 49.5, "close": 50.0})
        t += 1
    rows.append({"open_time": t, **prior})
    t += 1
    for r in event_rows:
        rows.append({"open_time": t, **r})
        t += 1
    return pd.DataFrame(rows)


LONG_ZONE_LOWER = 100.0
LONG_ZONE_UPPER = 100.3
SHORT_ZONE_LOWER = 99.7
SHORT_ZONE_UPPER = 100.0


def _long_zone() -> SupportResistanceZone:
    return _zone(lower=LONG_ZONE_LOWER, upper=LONG_ZONE_UPPER, first_touch=10, most_recent=15)


def _short_zone() -> SupportResistanceZone:
    return _zone(lower=SHORT_ZONE_LOWER, upper=SHORT_ZONE_UPPER, first_touch=10, most_recent=15)


def _long_prior_breakout_source() -> tuple[dict, dict, dict]:
    prior = _row(99.6, 99.7, 99.4, 99.6)
    breakout = _row(100.4, 100.6, 100.3, 100.5)
    source = _row(100.5, 100.6, 100.1, 100.4)
    return prior, breakout, source


def _short_prior_breakout_source() -> tuple[dict, dict, dict]:
    prior = _row(100.4, 100.5, 100.3, 100.4)
    breakout = _row(99.9, 100.0, 99.5, 99.5)
    source = _row(99.6, 99.9, 99.4, 99.6)
    return prior, breakout, source


def _resistance_touch_df(pad_rows: int = 0, extra_rows: int = 0) -> pd.DataFrame:
    """`pad_rows` extra baseline rows, then 50 zone-construction rows with two
    confirmed resistance-pivot touches (price 105.0) at relative positions 20
    and 35, then `extra_rows` further baseline rows.
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
    for _ in range(extra_rows):
        rows.append({"open_time": t, "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0})
        t += 1
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ positive cases / symmetry / contract fields

def test_long_and_short_positive_cases_are_symmetric(monkeypatch):
    long_zone = _long_zone()
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (long_zone,))
    prior, breakout, source = _long_prior_breakout_source()
    df = _build_df(prior, [breakout, source], max_retest_bars=1)
    findings = detect_support_resistance_breakout_retest("BTC", "5m", df, max_retest_bars=1)
    assert len(findings) == 1
    f = findings[0]
    assert f.direction == "LONG"
    assert f.symbol == "BTC"
    assert f.setup_family == "SUPPORT_RESISTANCE_BREAKOUT_RETEST"
    assert f.detector_version == DETECTOR_VERSION
    assert f.primary_timeframe == "5m"
    assert f.anchor_price == long_zone.upper
    assert f.anchor_open_time == long_zone.first_touch_open_time
    assert f.source_candle_close_time == (
        f.source_candle_open_time + PRIMARY_TIMEFRAME_DURATION_MS["5m"]
    )
    breakout_open_time = f.evidence["breakout_open_time"]
    assert f.fingerprint_key == (
        f"{long_zone.first_touch_open_time}:{breakout_open_time}:{f.source_candle_open_time}"
    )
    assert f.evidence["zone_center"] == long_zone.center
    assert f.evidence["zone_lower"] == long_zone.lower
    assert f.evidence["zone_upper"] == long_zone.upper
    assert f.evidence["zone_first_touch_open_time"] == long_zone.first_touch_open_time
    assert f.evidence["zone_pivot_open_times"] == list(long_zone.pivot_open_times)
    assert f.evidence["prior_close_before_breakout"] == 99.6
    assert f.evidence["breakout_close"] == 100.5
    assert f.evidence["source_extreme"] == 100.1
    assert f.evidence["source_close"] == 100.4
    assert f.evidence["bars_since_breakout"] == 1
    assert f.evidence["max_retest_bars"] == 1
    assert f.warnings == []
    assert f.measurements["breakout_distance_pct"] == pytest.approx(
        (100.5 - long_zone.upper) / long_zone.upper * 100.0
    )
    assert f.measurements["retest_penetration_pct"] == pytest.approx(
        (long_zone.upper - 100.1) / long_zone.upper * 100.0
    )
    assert f.measurements["hold_distance_pct"] == pytest.approx(
        (100.4 - long_zone.upper) / long_zone.upper * 100.0
    )
    assert f.measurements["source_candle_range"] == pytest.approx(100.6 - 100.1)

    short_zone = _short_zone()
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (short_zone,))
    prior_s, breakout_s, source_s = _short_prior_breakout_source()
    df_s = _build_df(prior_s, [breakout_s, source_s], max_retest_bars=1)
    findings_s = detect_support_resistance_breakout_retest("BTC", "5m", df_s, max_retest_bars=1)
    assert len(findings_s) == 1
    fs = findings_s[0]
    assert fs.direction == "SHORT"
    assert fs.anchor_price == short_zone.lower
    assert fs.anchor_open_time == short_zone.first_touch_open_time
    assert fs.evidence["zone_center"] == short_zone.center
    assert fs.evidence["zone_first_touch_open_time"] == short_zone.first_touch_open_time
    assert fs.evidence["source_extreme"] == 99.9
    assert fs.evidence["source_close"] == 99.6
    assert fs.measurements["breakout_distance_pct"] == pytest.approx(
        (short_zone.lower - 99.5) / short_zone.lower * 100.0
    )
    assert fs.measurements["retest_penetration_pct"] == pytest.approx(
        (99.9 - short_zone.lower) / short_zone.lower * 100.0
    )
    assert fs.measurements["hold_distance_pct"] == pytest.approx(
        (short_zone.lower - 99.6) / short_zone.lower * 100.0
    )


# ------------------------------------------------------------------ boundary conditions

def test_prior_close_not_strictly_below_zone_fails(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_long_zone(),))
    prior = _row(100.0, 100.1, 99.9, 100.0)  # prior.close == zone.lower, not strictly below
    _, breakout, source = _long_prior_breakout_source()
    df = _build_df(prior, [breakout, source], max_retest_bars=1)
    assert detect_support_resistance_breakout_retest("BTC", "5m", df, max_retest_bars=1) == []


def test_breakout_close_not_strictly_above_zone_fails(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_long_zone(),))
    prior, _, source = _long_prior_breakout_source()
    breakout = _row(100.2, 100.35, 100.1, 100.3)  # breakout.close == zone.upper exactly
    df = _build_df(prior, [breakout, source], max_retest_bars=1)
    assert detect_support_resistance_breakout_retest("BTC", "5m", df, max_retest_bars=1) == []


def test_short_prior_close_not_strictly_above_zone_fails(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_short_zone(),))
    prior = _row(100.0, 100.1, 99.9, 100.0)  # prior.close == zone.upper, not strictly above
    _, breakout, source = _short_prior_breakout_source()
    df = _build_df(prior, [breakout, source], max_retest_bars=1)
    assert detect_support_resistance_breakout_retest("BTC", "5m", df, max_retest_bars=1) == []


def test_short_breakout_close_not_strictly_below_zone_fails(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_short_zone(),))
    prior, _, source = _short_prior_breakout_source()
    breakout = _row(99.8, 99.9, 99.65, 99.7)  # breakout.close == zone.lower exactly
    df = _build_df(prior, [breakout, source], max_retest_bars=1)
    assert detect_support_resistance_breakout_retest("BTC", "5m", df, max_retest_bars=1) == []


def test_source_touch_inclusive_at_near_and_far_boundary(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_long_zone(),))
    prior, breakout, _ = _long_prior_breakout_source()

    source_near = _row(100.1, 100.5, 100.0, 100.4)  # low == zone.lower exactly
    df_near = _build_df(prior, [breakout, source_near], max_retest_bars=1)
    findings_near = detect_support_resistance_breakout_retest("BTC", "5m", df_near, max_retest_bars=1)
    assert len(findings_near) == 1

    source_far = _row(100.3, 100.5, 100.3, 100.4)  # low == zone.upper exactly
    df_far = _build_df(prior, [breakout, source_far], max_retest_bars=1)
    findings_far = detect_support_resistance_breakout_retest("BTC", "5m", df_far, max_retest_bars=1)
    assert len(findings_far) == 1


def test_source_trading_through_far_side_fails(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_long_zone(),))
    prior, breakout, _ = _long_prior_breakout_source()
    source = _row(100.3, 100.5, 99.9, 100.4)  # low < zone.lower: trades through the far side
    df = _build_df(prior, [breakout, source], max_retest_bars=1)
    assert detect_support_resistance_breakout_retest("BTC", "5m", df, max_retest_bars=1) == []


def test_source_close_exactly_at_held_boundary_fails(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_long_zone(),))
    prior, breakout, _ = _long_prior_breakout_source()
    source = _row(100.1, 100.4, 100.05, 100.3)  # close == zone.upper, not strictly above
    df = _build_df(prior, [breakout, source], max_retest_bars=1)
    assert detect_support_resistance_breakout_retest("BTC", "5m", df, max_retest_bars=1) == []


def test_short_source_touch_inclusive_at_near_and_far_boundary(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_short_zone(),))
    prior, breakout, _ = _short_prior_breakout_source()

    source_near = _row(99.6, SHORT_ZONE_LOWER, 99.5, 99.6)  # high == zone.lower exactly
    df_near = _build_df(prior, [breakout, source_near], max_retest_bars=1)
    findings_near = detect_support_resistance_breakout_retest("BTC", "5m", df_near, max_retest_bars=1)
    assert len(findings_near) == 1

    source_far = _row(99.6, SHORT_ZONE_UPPER, 99.5, 99.6)  # high == zone.upper exactly
    df_far = _build_df(prior, [breakout, source_far], max_retest_bars=1)
    findings_far = detect_support_resistance_breakout_retest("BTC", "5m", df_far, max_retest_bars=1)
    assert len(findings_far) == 1


def test_short_source_trading_through_far_side_fails(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_short_zone(),))
    prior, breakout, _ = _short_prior_breakout_source()
    source = _row(99.6, 100.1, 99.5, 99.6)  # high > zone.upper: trades through the far side
    df = _build_df(prior, [breakout, source], max_retest_bars=1)
    assert detect_support_resistance_breakout_retest("BTC", "5m", df, max_retest_bars=1) == []


def test_short_source_close_exactly_at_held_boundary_fails(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_short_zone(),))
    prior, breakout, _ = _short_prior_breakout_source()
    source = _row(99.7, 99.9, 99.6, SHORT_ZONE_LOWER)  # close == zone.lower, not strictly below
    df = _build_df(prior, [breakout, source], max_retest_bars=1)
    assert detect_support_resistance_breakout_retest("BTC", "5m", df, max_retest_bars=1) == []


def test_intermediate_close_loss_invalidates_event(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_long_zone(),))
    prior = _row(99.6, 99.7, 99.4, 99.6)
    breakout = _row(100.4, 100.6, 100.3, 100.5)
    bad_intermediate = _row(100.2, 100.3, 100.1, 100.2)  # closes back inside the zone
    source = _row(100.5, 100.6, 100.1, 100.4)
    df = _build_df(prior, [breakout, bad_intermediate, source], max_retest_bars=2)
    assert detect_support_resistance_breakout_retest("BTC", "5m", df, max_retest_bars=2) == []


# ------------------------------------------------------------------ event-window bounds

def test_adjacent_breakout_retest_qualifies(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_long_zone(),))
    baseline = _row(99.6, 99.7, 99.4, 99.6)
    breakout = _row(100.4, 100.6, 100.3, 100.5)
    source = _row(100.5, 100.6, 100.1, 100.4)
    event_rows = [baseline, baseline, baseline, baseline, breakout, source]
    df = _build_df(baseline, event_rows, max_retest_bars=5)
    findings = detect_support_resistance_breakout_retest("BTC", "5m", df, max_retest_bars=5)
    assert len(findings) == 1
    assert findings[0].evidence["bars_since_breakout"] == 1


def test_exact_maximum_gap_breakout_retest_qualifies(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_long_zone(),))
    prior = _row(99.6, 99.7, 99.4, 99.6)
    breakout = _row(100.4, 100.6, 100.3, 100.5)
    held = _row(100.5, 100.6, 100.4, 100.5)
    source = _row(100.5, 100.6, 100.1, 100.4)
    event_rows = [breakout, held, held, held, source]
    df = _build_df(prior, event_rows, max_retest_bars=4)
    findings = detect_support_resistance_breakout_retest("BTC", "5m", df, max_retest_bars=4)
    assert len(findings) == 1
    assert findings[0].evidence["bars_since_breakout"] == 4


def test_breakout_one_bar_too_old_is_never_inspected(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_long_zone(),))
    # The true breakout already happened one row before the reserved event
    # window (it becomes the last row of zone_df, i.e. `prior`), so every
    # possible in-window breakout candidate's own prior-close is already
    # above the zone — none can satisfy "prior_close < zone.lower".
    prior = _row(100.5, 100.6, 100.4, 100.5)  # already broken out
    held = _row(100.5, 100.6, 100.4, 100.5)
    source = _row(100.5, 100.6, 100.1, 100.4)
    event_rows = [held, held, held, held, source]
    df = _build_df(prior, event_rows, max_retest_bars=4)
    assert detect_support_resistance_breakout_retest("BTC", "5m", df, max_retest_bars=4) == []


# ------------------------------------------------------------------ fingerprint identity

def test_repeated_identical_input_yields_identical_finding(monkeypatch):
    zone = _long_zone()
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (zone,))
    prior, breakout, source = _long_prior_breakout_source()
    df = _build_df(prior, [breakout, source], max_retest_bars=1)

    f1 = detect_support_resistance_breakout_retest("BTC", "5m", df, max_retest_bars=1)[0]
    f2 = detect_support_resistance_breakout_retest("BTC", "5m", df, max_retest_bars=1)[0]
    assert f1 == f2


def test_later_retest_of_same_still_held_breakout_gets_distinct_fingerprint(monkeypatch):
    zone = _long_zone()
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (zone,))

    prior, breakout, source1 = _long_prior_breakout_source()
    df1 = _build_df(prior, [breakout, source1], max_retest_bars=1)
    finding1 = detect_support_resistance_breakout_retest("BTC", "5m", df1, max_retest_bars=1)[0]

    source2 = _row(100.5, 100.6, 100.2, 100.45)
    extra_open_time = int(df1["open_time"].iloc[-1]) + 1
    extra_row = pd.DataFrame([{**source2, "open_time": extra_open_time}])
    df2 = pd.concat([df1, extra_row], ignore_index=True)
    finding2 = detect_support_resistance_breakout_retest("BTC", "5m", df2, max_retest_bars=2)[0]

    assert finding1.fingerprint_key != finding2.fingerprint_key
    breakout_open_time = finding1.evidence["breakout_open_time"]
    assert finding2.evidence["breakout_open_time"] == breakout_open_time
    assert finding1.fingerprint_key.startswith(f"{zone.first_touch_open_time}:{breakout_open_time}:")
    assert finding2.fingerprint_key.startswith(f"{zone.first_touch_open_time}:{breakout_open_time}:")
    assert finding1.source_candle_open_time != finding2.source_candle_open_time


# ------------------------------------------------------------------ most-recent-breakout selection

def test_most_recent_valid_breakout_selected_over_earlier_invalidated_attempt(monkeypatch):
    """positions: [fake breakout, dip (invalidates fake + enables real), real
    breakout, held, held, source]. The fake attempt at position 0 must not be
    reported even though it satisfies the isolated prior/breakout pair — its
    own intermediate range includes the dip, which fails condition 3.
    """
    zone = _long_zone()
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (zone,))
    prior = _row(99.6, 99.7, 99.4, 99.6)
    fake_breakout = _row(100.4, 100.6, 100.3, 100.5)
    dip = _row(99.5, 99.6, 99.4, 99.5)
    real_breakout = _row(100.5, 100.7, 100.4, 100.6)
    held = _row(100.5, 100.6, 100.4, 100.5)
    source = _row(100.5, 100.6, 100.1, 100.4)
    event_rows = [fake_breakout, dip, real_breakout, held, held, source]
    df = _build_df(prior, event_rows, max_retest_bars=5)

    findings = detect_support_resistance_breakout_retest("BTC", "5m", df, max_retest_bars=5)
    assert len(findings) == 1
    f = findings[0]
    assert f.evidence["bars_since_breakout"] == 3
    assert f.evidence["breakout_close"] == 100.6
    assert f.evidence["breakout_open_time"] == int(df["open_time"].iloc[-4])


# ------------------------------------------------------------------ multi-zone tie-breaking

def test_tie_break_by_center_when_bars_since_and_distance_equal(monkeypatch):
    zone_a = _zone(lower=100.0, upper=100.3, center=100.15, first_touch=10, most_recent=15)
    zone_b = _zone(lower=100.0, upper=100.3, center=100.10, first_touch=30, most_recent=35)
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (zone_a, zone_b))
    prior, breakout, source = _long_prior_breakout_source()
    df = _build_df(prior, [breakout, source], max_retest_bars=1)

    findings = detect_support_resistance_breakout_retest("BTC", "5m", df, max_retest_bars=1)
    assert len(findings) == 1
    assert findings[0].anchor_open_time == zone_b.first_touch_open_time

    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (zone_b, zone_a))
    findings_reversed = detect_support_resistance_breakout_retest(
        "BTC", "5m", df, max_retest_bars=1
    )
    assert len(findings_reversed) == 1
    assert findings_reversed[0].anchor_open_time == zone_b.first_touch_open_time


def test_tie_break_by_first_touch_when_center_also_equal(monkeypatch):
    zone_a = _zone(lower=100.0, upper=100.3, center=100.15, first_touch=20, most_recent=25)
    zone_b = _zone(lower=100.0, upper=100.3, center=100.15, first_touch=10, most_recent=30)
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (zone_a, zone_b))
    prior, breakout, source = _long_prior_breakout_source()
    df = _build_df(prior, [breakout, source], max_retest_bars=1)

    findings = detect_support_resistance_breakout_retest("BTC", "5m", df, max_retest_bars=1)
    assert findings[0].anchor_open_time == zone_b.first_touch_open_time

    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (zone_b, zone_a))
    findings_reversed = detect_support_resistance_breakout_retest(
        "BTC", "5m", df, max_retest_bars=1
    )
    assert findings_reversed[0].anchor_open_time == zone_b.first_touch_open_time


def test_tie_break_by_most_recent_touch_when_all_else_equal(monkeypatch):
    zone_a = _zone(lower=100.0, upper=100.3, center=100.15, first_touch=10, most_recent=25)
    zone_b = _zone(lower=100.0, upper=100.3, center=100.15, first_touch=10, most_recent=20)
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (zone_a, zone_b))
    prior, breakout, source = _long_prior_breakout_source()
    df = _build_df(prior, [breakout, source], max_retest_bars=1)

    findings = detect_support_resistance_breakout_retest("BTC", "5m", df, max_retest_bars=1)
    assert (
        findings[0].evidence["zone_most_recent_touch_open_time"]
        == zone_b.most_recent_touch_open_time
    )

    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (zone_b, zone_a))
    findings_reversed = detect_support_resistance_breakout_retest(
        "BTC", "5m", df, max_retest_bars=1
    )
    assert (
        findings_reversed[0].evidence["zone_most_recent_touch_open_time"]
        == zone_b.most_recent_touch_open_time
    )


# ------------------------------------------------------------------ contradictory dual-direction

def test_dual_direction_qualification_returns_empty(monkeypatch):
    zone_long = _zone(lower=10.0, upper=20.0, first_touch=10, most_recent=15)
    zone_short = _zone(lower=80.0, upper=90.0, first_touch=20, most_recent=25)
    monkeypatch.setattr(
        sbr, "find_support_resistance_zones", lambda *a, **k: (zone_long, zone_short)
    )
    prior = _row(5.0, 5.5, 4.5, 5.0)
    breakout = _row(95.0, 95.5, 94.5, 95.0)  # >zone_long.upper, and >zone_short.upper
    intermediate = _row(50.0, 50.5, 49.5, 50.0)  # >zone_long.upper, and <zone_short.lower
    source = _row(50.0, 85.0, 15.0, 50.0)  # low in zone_long, high in zone_short, close between
    df = _build_df(prior, [breakout, intermediate, source], max_retest_bars=2)
    assert detect_support_resistance_breakout_retest("BTC", "5m", df, max_retest_bars=2) == []


# ------------------------------------------------------------------ input validation

def _minimal_valid_df() -> pd.DataFrame:
    """Sized for DEFAULT_MAX_RETEST_BARS so bare `detect(...)` calls (which
    default to it) see a well-formed, minimally-sized LONG breakout retest
    rather than short-circuiting on the row-count check first.
    """
    prior, breakout, source = _long_prior_breakout_source()
    baseline = prior  # prior already sits below the zone, same as a filler row
    event_rows = [baseline, baseline, baseline, baseline, breakout, source]
    return _build_df(prior, event_rows, max_retest_bars=DEFAULT_MAX_RETEST_BARS)


def test_none_input_returns_empty():
    assert detect_support_resistance_breakout_retest("BTC", "5m", None) == []


def test_non_dataframe_input_returns_empty():
    assert detect_support_resistance_breakout_retest("BTC", "5m", "not-a-df") == []
    assert detect_support_resistance_breakout_retest("BTC", "5m", [1, 2, 3]) == []


def test_blank_symbol_returns_empty(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_long_zone(),))
    df = _minimal_valid_df()
    assert detect_support_resistance_breakout_retest("", "5m", df) == []
    assert detect_support_resistance_breakout_retest("   ", "5m", df) == []


def test_non_string_symbol_returns_empty(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_long_zone(),))
    df = _minimal_valid_df()
    assert detect_support_resistance_breakout_retest(None, "5m", df) == []
    assert detect_support_resistance_breakout_retest(123, "5m", df) == []


def test_unsupported_timeframe_returns_empty(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_long_zone(),))
    df = _minimal_valid_df()
    assert detect_support_resistance_breakout_retest("BTC", "1h", df) == []
    assert detect_support_resistance_breakout_retest("BTC", "", df) == []


def test_unhashable_timeframe_returns_empty_without_raising(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_long_zone(),))
    df = _minimal_valid_df()
    assert detect_support_resistance_breakout_retest("BTC", ["5m"], df) == []
    assert detect_support_resistance_breakout_retest("BTC", {"5m": True}, df) == []


def test_invalid_max_retest_bars_returns_empty(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_long_zone(),))
    df = _minimal_valid_df()
    for bad_value in (True, False, 0, -1, 1.0, 1.5, "1", None, float("nan"), LOOKBACK_CANDLES + 1):
        assert (
            detect_support_resistance_breakout_retest("BTC", "5m", df, max_retest_bars=bad_value)
            == []
        )


def test_default_constants_are_consistent():
    assert EVENT_WINDOW_CANDLES == DEFAULT_MAX_RETEST_BARS + 1
    assert MIN_CANDLES_REQUIRED == SUPPORT_RESISTANCE_MIN_ROWS_REQUIRED + EVENT_WINDOW_CANDLES


def test_insufficient_rows_returns_empty(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_long_zone(),))
    prior, breakout, source = _long_prior_breakout_source()
    event_rows = [breakout] * DEFAULT_MAX_RETEST_BARS + [source]
    required_rows = SUPPORT_RESISTANCE_MIN_ROWS_REQUIRED + EVENT_WINDOW_CANDLES
    filler_count = required_rows - 1 - EVENT_WINDOW_CANDLES - 1  # one row short overall
    df = _build_df(prior, event_rows, max_retest_bars=DEFAULT_MAX_RETEST_BARS, filler_count=filler_count)
    assert len(df) == required_rows - 1
    assert detect_support_resistance_breakout_retest("BTC", "5m", df) == []


def test_missing_column_returns_empty(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_long_zone(),))
    df = _minimal_valid_df().drop(columns=["open"])
    assert detect_support_resistance_breakout_retest("BTC", "5m", df) == []


def test_duplicate_required_column_label_returns_empty_without_raising(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_long_zone(),))
    df = _minimal_valid_df()
    df = pd.concat([df, df[["open"]]], axis=1)  # "open" now appears twice
    assert list(df.columns).count("open") == 2
    assert detect_support_resistance_breakout_retest("BTC", "5m", df) == []


def test_non_finite_value_in_window_returns_empty(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_long_zone(),))
    df = _minimal_valid_df()
    df.loc[df.index[-1], "high"] = float("nan")
    assert detect_support_resistance_breakout_retest("BTC", "5m", df) == []

    df2 = _minimal_valid_df()
    df2.loc[df2.index[-2], "low"] = float("inf")
    assert detect_support_resistance_breakout_retest("BTC", "5m", df2) == []


def test_non_positive_value_in_window_returns_empty(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_long_zone(),))
    df = _minimal_valid_df()
    df.loc[df.index[-1], "close"] = 0.0
    assert detect_support_resistance_breakout_retest("BTC", "5m", df) == []

    df2 = _minimal_valid_df()
    df2.loc[df2.index[0], "open"] = -1.0
    assert detect_support_resistance_breakout_retest("BTC", "5m", df2) == []


def test_geometry_violation_in_window_returns_empty(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_long_zone(),))
    df = _minimal_valid_df()
    df.loc[df.index[-1], "open"] = df.loc[df.index[-1], "high"] + 1.0  # open > high
    assert detect_support_resistance_breakout_retest("BTC", "5m", df) == []

    df2 = _minimal_valid_df()
    df2.loc[df2.index[-1], "close"] = df2.loc[df2.index[-1], "low"] - 1.0  # close < low
    assert detect_support_resistance_breakout_retest("BTC", "5m", df2) == []


def test_fractional_open_time_in_window_returns_empty(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_long_zone(),))
    df = _minimal_valid_df()
    df.loc[df.index[-1], "open_time"] = float(df["open_time"].iloc[-1]) + 0.5
    assert detect_support_resistance_breakout_retest("BTC", "5m", df) == []


def test_duplicate_open_time_in_window_returns_empty(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_long_zone(),))
    df = _minimal_valid_df()
    df.loc[df.index[-1], "open_time"] = df["open_time"].iloc[-2]
    assert detect_support_resistance_breakout_retest("BTC", "5m", df) == []


def test_non_monotonic_open_time_in_window_returns_empty(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_long_zone(),))
    df = _minimal_valid_df()
    times = df["open_time"].tolist()
    times[-1], times[-2] = times[-2], times[-1]
    df["open_time"] = times
    assert detect_support_resistance_breakout_retest("BTC", "5m", df) == []


# ------------------------------------------------------------------ malformed primitive output / zone metadata

def test_zone_output_not_a_tuple_returns_empty(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: [_long_zone()])
    df = _minimal_valid_df()
    assert detect_support_resistance_breakout_retest("BTC", "5m", df) == []


def test_empty_zone_output_returns_empty_normally(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: ())
    df = _minimal_valid_df()
    assert detect_support_resistance_breakout_retest("BTC", "5m", df) == []


@pytest.mark.parametrize(
    "corruption",
    [
        {"lower": float("nan")},
        {"upper": float("inf")},
        {"center": 0.0},
        {"lower": 100.2, "center": 100.15},  # lower > center
        {"upper": 100.1, "center": 100.15},  # center > upper
        {"timeframe": "3m"},  # mismatched timeframe
        {"contributing_pivot_count": 1},
        {"contributing_pivot_count": 2.5},  # fractional
        {"contributing_pivot_count": "2"},  # string
        {"contributing_pivot_count": True},  # bool, not a genuine int
        {"contributing_pivot_count": 3},  # count/tuple-length mismatch (tuple has 2 elements)
        {"pivot_open_times": [10, 15]},  # not a tuple
        {"pivot_open_times": (15,)},  # fewer than two
        {"pivot_open_times": (15, 10)},  # not strictly increasing
        {"pivot_open_times": (10.5, 15)},  # fractional pivot time
        {"pivot_open_times": ("10", 15)},  # string pivot time
        {"first_touch_open_time": 999},  # inconsistent with tuple
        {"first_touch_open_time": 10.5},  # fractional
        {"first_touch_open_time": "10"},  # string
        {"most_recent_touch_open_time": 999},  # inconsistent with tuple
        {"most_recent_touch_open_time": 15.5},  # fractional
        {"most_recent_touch_open_time": "15"},  # string
    ],
)
def test_malformed_zone_metadata_fails_closed(monkeypatch, corruption):
    zone = dataclasses.replace(_long_zone(), **corruption)
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (zone,))
    df = _minimal_valid_df()
    assert detect_support_resistance_breakout_retest("BTC", "5m", df) == []


def test_bool_pivot_time_rejected_despite_numerically_matching_value(monkeypatch):
    """`True == 1` in Python, so a naive equality-only consistency check
    would silently accept a `bool` standing in for a matching pivot time.
    `_as_plain_int` must reject it outright regardless of magnitude.
    """
    zone = _zone(lower=100.0, upper=100.3, first_touch=1, most_recent=15, pivot_times=(1, 15))
    bad_zone = dataclasses.replace(zone, first_touch_open_time=True)
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (bad_zone,))
    df = _minimal_valid_df()
    assert detect_support_resistance_breakout_retest("BTC", "5m", df) == []


def test_numpy_scalar_zone_metadata_round_trips_as_plain_python_json(monkeypatch):
    zone = _long_zone()
    numpy_zone = dataclasses.replace(
        zone,
        center=np.float64(zone.center),
        lower=np.float64(zone.lower),
        upper=np.float64(zone.upper),
        contributing_pivot_count=np.int64(zone.contributing_pivot_count),
        pivot_open_times=tuple(np.int64(t) for t in zone.pivot_open_times),
        first_touch_open_time=np.int64(zone.first_touch_open_time),
        most_recent_touch_open_time=np.int64(zone.most_recent_touch_open_time),
    )
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (numpy_zone,))
    prior, breakout, source = _long_prior_breakout_source()
    df = _build_df(prior, [breakout, source], max_retest_bars=1)
    finding = detect_support_resistance_breakout_retest("BTC", "5m", df, max_retest_bars=1)[0]

    # json.dumps raises TypeError on NumPy scalars; a clean round-trip here
    # proves every accepted zone-derived value was normalized to a plain
    # Python int/float before being placed in evidence/measurements.
    assert json.loads(json.dumps(finding.evidence)) == finding.evidence
    assert json.loads(json.dumps(finding.measurements)) == finding.measurements

    assert type(finding.evidence["zone_center"]) is float
    assert type(finding.evidence["zone_lower"]) is float
    assert type(finding.evidence["zone_upper"]) is float
    assert type(finding.evidence["zone_contributing_pivot_count"]) is int
    assert all(type(t) is int for t in finding.evidence["zone_pivot_open_times"])
    assert type(finding.evidence["zone_first_touch_open_time"]) is int
    assert type(finding.evidence["zone_most_recent_touch_open_time"]) is int
    assert type(finding.anchor_price) is float
    assert type(finding.anchor_open_time) is int


def test_one_malformed_zone_withholds_entire_scan(monkeypatch):
    good_zone = _long_zone()
    bad_zone = dataclasses.replace(good_zone, lower=float("nan"))
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (good_zone, bad_zone))
    df = _minimal_valid_df()
    assert detect_support_resistance_breakout_retest("BTC", "5m", df) == []


# ------------------------------------------------------------------ bounded-window malformed-row scoping

def test_malformed_row_outside_window_is_ignored(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_long_zone(),))
    prior, breakout, source = _long_prior_breakout_source()
    total_n = LOOKBACK_CANDLES + 2 + 15  # max_retest_bars=1 below -> event_window_candles=2
    filler_count = total_n - 1 - 2
    df = _build_df(prior, [breakout, source], max_retest_bars=1, filler_count=filler_count)
    assert len(df) == total_n
    df.loc[0, "close"] = float("nan")  # falls outside df.tail(LOOKBACK_CANDLES + 2)

    findings = detect_support_resistance_breakout_retest("BTC", "5m", df, max_retest_bars=1)
    assert len(findings) == 1


def test_malformed_row_inside_window_blocks_detection(monkeypatch):
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (_long_zone(),))
    prior, breakout, source = _long_prior_breakout_source()
    total_n = LOOKBACK_CANDLES + 2 + 15  # max_retest_bars=1 below -> event_window_candles=2
    filler_count = total_n - 1 - 2
    df = _build_df(prior, [breakout, source], max_retest_bars=1, filler_count=filler_count)
    window_start_pos = total_n - (LOOKBACK_CANDLES + 2)
    df.loc[window_start_pos, "close"] = float("nan")  # first row of the bounded window itself

    assert detect_support_resistance_breakout_retest("BTC", "5m", df, max_retest_bars=1) == []


# ------------------------------------------------------------------ no-lookahead zone_df invariance

def test_zone_df_excludes_reserved_event_tail(monkeypatch):
    captured: dict = {}

    def fake_find_zones(zone_df, timeframe):
        captured["zone_df"] = zone_df.copy()
        captured["timeframe"] = timeframe
        return ()

    monkeypatch.setattr(sbr, "find_support_resistance_zones", fake_find_zones)

    prior, breakout, source = _long_prior_breakout_source()
    df = _build_df(prior, [breakout, source], max_retest_bars=1)
    detect_support_resistance_breakout_retest("BTC", "5m", df, max_retest_bars=1)

    zone_df = captured["zone_df"]
    assert len(zone_df) == len(df) - 2
    assert int(zone_df["open_time"].iloc[-1]) == int(df["open_time"].iloc[-3])
    assert int(df["open_time"].iloc[-1]) not in zone_df["open_time"].to_numpy()
    assert int(df["open_time"].iloc[-2]) not in zone_df["open_time"].to_numpy()
    assert captured["timeframe"] == "5m"


def test_zone_df_invariant_to_breakout_and_source_tail_changes(monkeypatch):
    captured: list[pd.DataFrame] = []

    def fake_find_zones(zone_df, timeframe):
        captured.append(zone_df.copy())
        return ()

    monkeypatch.setattr(sbr, "find_support_resistance_zones", fake_find_zones)

    prior, breakout_a, source_a = _long_prior_breakout_source()
    df_a = _build_df(prior, [breakout_a, source_a], max_retest_bars=1)
    detect_support_resistance_breakout_retest("BTC", "5m", df_a, max_retest_bars=1)

    breakout_b = _row(200.0, 200.5, 199.5, 200.2)
    source_b = _row(50.0, 50.5, 49.5, 50.0)
    df_b = _build_df(prior, [breakout_b, source_b], max_retest_bars=1)
    detect_support_resistance_breakout_retest("BTC", "5m", df_b, max_retest_bars=1)

    assert len(captured) == 2
    pd.testing.assert_frame_equal(captured[0], captured[1])
    tail_a_times = {int(df_a["open_time"].iloc[-1]), int(df_a["open_time"].iloc[-2])}
    tail_b_times = {int(df_b["open_time"].iloc[-1]), int(df_b["open_time"].iloc[-2])}
    assert not tail_a_times & set(captured[0]["open_time"].to_numpy())
    assert not tail_b_times & set(captured[1]["open_time"].to_numpy())


# ------------------------------------------------------------------ real Support/Resistance primitive integration + eviction

def test_real_primitive_integration_long_breakout_retest():
    zone_df = _resistance_touch_df(pad_rows=0, extra_rows=1)
    zones = find_support_resistance_zones(zone_df, "5m")
    matches = [z for z in zones if z.pivot_open_times == (20, 35)]
    assert len(matches) == 1
    zone = matches[0]

    breakout = _row(zone.upper + 0.5, zone.upper + 1.0, zone.upper + 0.2, zone.upper + 0.7)
    source_low = (zone.lower + zone.upper) / 2
    source = _row(zone.upper + 0.5, zone.upper + 0.8, source_low, zone.upper + 0.4)

    breakout_open_time = int(zone_df["open_time"].iloc[-1]) + 1
    source_open_time = breakout_open_time + 1
    extra = pd.DataFrame(
        [
            {**breakout, "open_time": breakout_open_time},
            {**source, "open_time": source_open_time},
        ]
    )
    df = pd.concat([zone_df, extra], ignore_index=True)

    findings = detect_support_resistance_breakout_retest("BTC", "5m", df, max_retest_bars=1)
    assert len(findings) == 1
    f = findings[0]
    assert f.direction == "LONG"
    assert f.anchor_price == zone.upper
    assert f.anchor_open_time == zone.first_touch_open_time
    assert f.fingerprint_key == (
        f"{zone.first_touch_open_time}:{breakout_open_time}:{source_open_time}"
    )
    assert f.evidence["zone_contributing_pivot_count"] == 2


def test_front_eviction_removes_zone_and_finding():
    """Same structural touches as the integration test above, but with 190
    ordinary bars inserted between the touches and the reserved event tail
    so the detector's own bounded window no longer contains the older touch
    (open_time 20) — leaving only one touch, below MIN_PIVOT_TOUCHES, so the
    zone (and thus the finding) disappears even though a well-formed
    breakout/retest tail is appended.
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
    zone_df = pd.DataFrame(rows)

    real_zones = find_support_resistance_zones(zone_df, "5m")
    assert not any(z.pivot_open_times == (20, 35) for z in real_zones)

    breakout = _row(105.5, 105.8, 105.2, 105.6)
    source = _row(105.5, 105.7, 104.8, 105.4)
    extra = pd.DataFrame(
        [
            {**breakout, "open_time": t},
            {**source, "open_time": t + 1},
        ]
    )
    df = pd.concat([zone_df, extra], ignore_index=True)

    assert detect_support_resistance_breakout_retest("BTC", "5m", df, max_retest_bars=1) == []


# ------------------------------------------------------------------ contract / JSON / no-wiring / performance

def test_setup_family_and_direction_invariance_contract():
    assert "SUPPORT_RESISTANCE_BREAKOUT_RETEST" in get_args(SetupFamily)
    assert "SUPPORT_RESISTANCE_BREAKOUT_RETEST" not in DIRECTION_INVARIANT_FAMILIES


def test_evidence_and_measurements_are_json_serializable(monkeypatch):
    zone = _long_zone()
    monkeypatch.setattr(sbr, "find_support_resistance_zones", lambda *a, **k: (zone,))
    prior, breakout, source = _long_prior_breakout_source()
    df = _build_df(prior, [breakout, source], max_retest_bars=1)
    finding = detect_support_resistance_breakout_retest("BTC", "5m", df, max_retest_bars=1)[0]

    evidence_json = json.dumps(finding.evidence)
    measurements_json = json.dumps(finding.measurements)
    assert json.loads(evidence_json) == finding.evidence
    assert json.loads(measurements_json) == finding.measurements


# Runtime/wiring namespace roots the detector must never import from (engine,
# scheduler, live candle data access, notifications/alerts, DB persistence,
# configuration, application wiring, and the live strategy/trading path).
# `apex.opportunity.support_resistance` and `apex.opportunity.contract` are
# deliberately absent — the detector's own permitted, pure dependencies.
FORBIDDEN_IMPORT_ROOTS = (
    "apex.opportunity.engine",
    "apex.scheduler",
    "apex.data.candle_store",
    "apex.notifications",
    "apex.db",
    "apex.config",
    "apex.app",
    "apex.strategy",
)


def _is_forbidden_import(module_name: str) -> bool:
    return any(
        module_name == root or module_name.startswith(root + ".")
        for root in FORBIDDEN_IMPORT_ROOTS
    )


def test_detector_module_has_no_runtime_wiring_imports():
    """Parse the detector's own source with `ast` and inspect every
    Import/ImportFrom node directly, rather than substring-matching the raw
    source text (which false-positives on words like `candle_store` or
    `trading` appearing legitimately in comments/docstrings).
    """
    source = inspect.getsource(sbr)
    tree = ast.parse(source)

    imported_modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.append(node.module)

    # Non-vacuous: the detector does import real modules (pandas, numpy, the
    # contract, the S/R primitive) — prove the AST walk actually found them
    # before asserting none are forbidden.
    assert imported_modules

    forbidden_hits = [m for m in imported_modules if _is_forbidden_import(m)]
    assert forbidden_hits == []


MAX_SECONDS = 10.0  # generous bound; actual runtime is expected to be well under 1s


def test_bounded_performance_large_input():
    zone_df = _resistance_touch_df(pad_rows=4949, extra_rows=1)
    assert len(zone_df) == 5000
    zones = find_support_resistance_zones(zone_df, "5m")
    assert len(zones) == 1
    zone = zones[0]

    breakout = _row(zone.upper + 0.5, zone.upper + 1.0, zone.upper + 0.2, zone.upper + 0.7)
    source_low = (zone.lower + zone.upper) / 2
    source = _row(zone.upper + 0.5, zone.upper + 0.8, source_low, zone.upper + 0.4)
    breakout_open_time = int(zone_df["open_time"].iloc[-1]) + 1
    source_open_time = breakout_open_time + 1
    extra = pd.DataFrame(
        [
            {**breakout, "open_time": breakout_open_time},
            {**source, "open_time": source_open_time},
        ]
    )
    df = pd.concat([zone_df, extra], ignore_index=True)

    start = time.perf_counter()
    total_calls = 0
    for _ in range(20):
        findings = detect_support_resistance_breakout_retest("BTC", "5m", df, max_retest_bars=1)
        assert len(findings) == 1
        total_calls += 1
    elapsed = time.perf_counter() - start

    assert total_calls == 20
    assert elapsed < MAX_SECONDS, f"20 calls over 5000 rows took {elapsed:.3f}s, expected well under {MAX_SECONDS}s"
