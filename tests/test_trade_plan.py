"""Tests for the immutable trade plan contract (src/apex/opportunity/trade_plan.py).

Covers: exact LONG/SHORT entry/invalidation/stop/target formulas for
SWEEP_RECLAIM and all three SUPPORT_RESISTANCE_* families, VOLATILITY_COMPRESSION
always UNAVAILABLE, malformed/missing/nonfinite/nonpositive/wrong-side inputs,
no score/sizing dependency, canonical deterministic provenance, and source
candle exclusion (evaluation_not_before_ms == source_candle_close_time).
"""
from __future__ import annotations

import dataclasses
import json
import math

import pytest

from apex.opportunity.contract import CONTRACT_VERSION, DetectorFinding, PRIMARY_TIMEFRAME_DURATION_MS
from apex.opportunity.trade_plan import (
    ENTRY_TYPE_RESTING_LIMIT_AT_SOURCE_CLOSE,
    OUTCOME_HORIZON_BARS,
    REASON_INVALID_DIRECTION,
    REASON_INVALID_STOP_GEOMETRY,
    REASON_INVALID_TIMEFRAME,
    REASON_MALFORMED_SOURCE_FIELD,
    REASON_MISSING_SOURCE_FIELD,
    REASON_NO_STRUCTURAL_INVALIDATION,
    REASON_NONFINITE_VALUE,
    REASON_NONPOSITIVE_VALUE,
    REASON_UNSUPPORTED_SETUP_FAMILY,
    TRADE_PLAN_CONTRACT_VERSION,
    TradePlan,
    build_trade_plan,
    trade_plan_from_row,
)

FORBIDDEN_FIELD_SUBSTRINGS = (
    "account", "risk_usd", "quantity", "notional", "margin", "leverage", "size",
)


def _finding(**overrides) -> DetectorFinding:
    base = dict(
        symbol="BTC",
        direction="LONG",
        setup_family="SWEEP_RECLAIM",
        detector_version="sweep_reclaim_v0_1",
        primary_timeframe="5m",
        source_candle_open_time=1_000_000,
        source_candle_close_time=1_000_300_000,
        fingerprint_key="anchor-1",
        anchor_price=100.0,
        anchor_open_time=900_000,
        evidence={},
        warnings=[],
        measurements={},
    )
    base.update(overrides)
    return DetectorFinding(**base)


def _build(finding: DetectorFinding, **kwargs) -> TradePlan:
    defaults = dict(
        plan_uid="plan-uid-1",
        opportunity_uid="opp-uid-1",
        fingerprint="fp-1",
        created_at="2026-09-15T00:00:00Z",
    )
    defaults.update(kwargs)
    return build_trade_plan(finding, **defaults)


# ------------------------------------------------------------------ formulas


def test_sweep_reclaim_long_formula():
    finding = _finding(
        direction="LONG",
        setup_family="SWEEP_RECLAIM",
        measurements={"reclaim_close": 110.0, "sweep_low": 100.0, "sweep_high": 999.0},
    )
    plan = _build(finding)
    assert plan.availability == "AVAILABLE"
    assert plan.entry_price == 110.0
    assert plan.stop_price == 100.0
    assert plan.invalidation_price == 100.0
    assert plan.risk_distance == pytest.approx(10.0)
    assert plan.target_1r_price == pytest.approx(120.0)
    assert plan.target_2r_price == pytest.approx(130.0)
    assert plan.entry_type == ENTRY_TYPE_RESTING_LIMIT_AT_SOURCE_CLOSE


def test_sweep_reclaim_short_formula():
    finding = _finding(
        direction="SHORT",
        setup_family="SWEEP_RECLAIM",
        measurements={"reclaim_close": 90.0, "sweep_low": 1.0, "sweep_high": 100.0},
    )
    plan = _build(finding)
    assert plan.availability == "AVAILABLE"
    assert plan.entry_price == 90.0
    assert plan.stop_price == 100.0
    assert plan.risk_distance == pytest.approx(10.0)
    assert plan.target_1r_price == pytest.approx(80.0)
    assert plan.target_2r_price == pytest.approx(70.0)


@pytest.mark.parametrize(
    "family",
    [
        "SUPPORT_RESISTANCE_REJECTION",
        "SUPPORT_RESISTANCE_BREAKOUT_RETEST",
        "SUPPORT_RESISTANCE_FAILED_BREAKOUT",
    ],
)
def test_sr_family_long_formula(family):
    finding = _finding(
        direction="LONG",
        setup_family=family,
        evidence={"source_close": 105.0, "zone_lower": 100.0, "zone_upper": 108.0},
    )
    plan = _build(finding)
    assert plan.availability == "AVAILABLE"
    assert plan.entry_price == 105.0
    assert plan.stop_price == 100.0
    assert plan.risk_distance == pytest.approx(5.0)
    assert plan.target_1r_price == pytest.approx(110.0)
    assert plan.target_2r_price == pytest.approx(115.0)


@pytest.mark.parametrize(
    "family",
    [
        "SUPPORT_RESISTANCE_REJECTION",
        "SUPPORT_RESISTANCE_BREAKOUT_RETEST",
        "SUPPORT_RESISTANCE_FAILED_BREAKOUT",
    ],
)
def test_sr_family_short_formula(family):
    finding = _finding(
        direction="SHORT",
        setup_family=family,
        evidence={"source_close": 103.0, "zone_lower": 95.0, "zone_upper": 108.0},
    )
    plan = _build(finding)
    assert plan.availability == "AVAILABLE"
    assert plan.entry_price == 103.0
    assert plan.stop_price == 108.0
    assert plan.risk_distance == pytest.approx(5.0)
    assert plan.target_1r_price == pytest.approx(98.0)
    assert plan.target_2r_price == pytest.approx(93.0)


def test_compression_always_unavailable():
    finding = _finding(
        direction="LONG",
        setup_family="VOLATILITY_COMPRESSION",
        measurements={"reclaim_close": 1.0},  # irrelevant, must still be UNAVAILABLE
    )
    plan = _build(finding)
    assert plan.availability == "UNAVAILABLE"
    assert plan.unavailable_reason == REASON_NO_STRUCTURAL_INVALIDATION
    for field_name in (
        "entry_price", "invalidation_price", "stop_price", "risk_distance",
        "target_1r_price", "target_2r_price", "target_1r_multiple", "target_2r_multiple",
        "reward_risk_1r", "reward_risk_2r", "entry_type",
    ):
        assert getattr(plan, field_name) is None


# ------------------------------------------------------------------ malformed inputs


def test_missing_source_field_is_unavailable():
    finding = _finding(setup_family="SWEEP_RECLAIM", measurements={"sweep_low": 1.0})
    plan = _build(finding)
    assert plan.availability == "UNAVAILABLE"
    assert plan.unavailable_reason == REASON_MISSING_SOURCE_FIELD
    assert plan.entry_price is None


def test_malformed_source_field_is_unavailable():
    finding = _finding(
        setup_family="SWEEP_RECLAIM",
        measurements={"reclaim_close": "not-a-number", "sweep_low": 1.0},
    )
    plan = _build(finding)
    assert plan.availability == "UNAVAILABLE"
    assert plan.unavailable_reason == REASON_MALFORMED_SOURCE_FIELD


def test_bool_source_field_is_malformed():
    finding = _finding(
        setup_family="SWEEP_RECLAIM",
        measurements={"reclaim_close": True, "sweep_low": 1.0},
    )
    plan = _build(finding)
    assert plan.unavailable_reason == REASON_MALFORMED_SOURCE_FIELD


def test_nonfinite_value_is_unavailable():
    finding = _finding(
        setup_family="SWEEP_RECLAIM",
        measurements={"reclaim_close": math.inf, "sweep_low": 1.0},
    )
    plan = _build(finding)
    assert plan.availability == "UNAVAILABLE"
    assert plan.unavailable_reason == REASON_NONFINITE_VALUE


def test_nonpositive_value_is_unavailable():
    finding = _finding(
        setup_family="SWEEP_RECLAIM",
        measurements={"reclaim_close": 0.0, "sweep_low": 1.0},
    )
    plan = _build(finding)
    assert plan.availability == "UNAVAILABLE"
    assert plan.unavailable_reason == REASON_NONPOSITIVE_VALUE


def test_wrong_side_geometry_long_is_unavailable():
    # LONG requires stop < entry; here stop is above entry.
    finding = _finding(
        direction="LONG",
        setup_family="SWEEP_RECLAIM",
        measurements={"reclaim_close": 100.0, "sweep_low": 110.0},
    )
    plan = _build(finding)
    assert plan.availability == "UNAVAILABLE"
    assert plan.unavailable_reason == REASON_INVALID_STOP_GEOMETRY


def test_wrong_side_geometry_short_is_unavailable():
    finding = _finding(
        direction="SHORT",
        setup_family="SWEEP_RECLAIM",
        measurements={"reclaim_close": 100.0, "sweep_high": 90.0},
    )
    plan = _build(finding)
    assert plan.availability == "UNAVAILABLE"
    assert plan.unavailable_reason == REASON_INVALID_STOP_GEOMETRY


def test_equal_entry_and_stop_is_invalid_geometry():
    finding = _finding(
        direction="LONG",
        setup_family="SWEEP_RECLAIM",
        measurements={"reclaim_close": 100.0, "sweep_low": 100.0},
    )
    plan = _build(finding)
    assert plan.unavailable_reason == REASON_INVALID_STOP_GEOMETRY


def test_invalid_direction_is_unavailable():
    finding = _finding(setup_family="SWEEP_RECLAIM", measurements={"reclaim_close": 1.0, "sweep_low": 0.5})
    finding.direction = "SIDEWAYS"  # DetectorFinding is not frozen; defensive-only path
    plan = _build(finding)
    assert plan.availability == "UNAVAILABLE"
    assert plan.unavailable_reason == REASON_INVALID_DIRECTION


def test_invalid_timeframe_is_unavailable():
    finding = _finding(setup_family="SWEEP_RECLAIM", measurements={"reclaim_close": 1.0, "sweep_low": 0.5})
    finding.primary_timeframe = "1h"
    plan = _build(finding)
    assert plan.availability == "UNAVAILABLE"
    assert plan.unavailable_reason == REASON_INVALID_TIMEFRAME
    assert plan.evaluation_expiry_ms is None


def test_unsupported_setup_family_is_unavailable():
    finding = _finding(setup_family="SWEEP_RECLAIM")
    finding.setup_family = "NOT_A_REAL_FAMILY"
    plan = _build(finding)
    assert plan.availability == "UNAVAILABLE"
    assert plan.unavailable_reason == REASON_UNSUPPORTED_SETUP_FAMILY


# ------------------------------------------------------------------ safety boundary


def test_no_score_or_sizing_fields_on_trade_plan():
    field_names = {f.name for f in dataclasses.fields(TradePlan)}
    for name in field_names:
        lowered = name.lower()
        assert "score" not in lowered
        for forbidden in FORBIDDEN_FIELD_SUBSTRINGS:
            assert forbidden not in lowered, f"forbidden sizing field: {name}"


def test_provenance_never_contains_score_terms():
    finding = _finding(
        setup_family="SWEEP_RECLAIM",
        measurements={"reclaim_close": 110.0, "sweep_low": 100.0},
    )
    plan = _build(finding)
    assert "score" not in plan.provenance_json.lower()


# ------------------------------------------------------------------ provenance / determinism


def test_canonical_provenance_is_sorted_and_deterministic():
    finding = _finding(
        setup_family="SWEEP_RECLAIM",
        measurements={"reclaim_close": 110.0, "sweep_low": 100.0},
    )
    plan_a = _build(finding)
    plan_b = _build(finding)
    assert plan_a.provenance_json == plan_b.provenance_json

    parsed = json.loads(plan_a.provenance_json)
    assert parsed["opportunity_uid"] == "opp-uid-1"
    assert parsed["fingerprint"] == "fp-1"
    assert parsed["opportunity_contract_version"] == CONTRACT_VERSION
    assert parsed["plan_contract_version"] == TRADE_PLAN_CONTRACT_VERSION
    assert parsed["entry_source_field"] == "measurements.reclaim_close"
    assert parsed["stop_source_field"] == "measurements.sweep_low"
    assert parsed["no_look_ahead_boundary_ms"] == finding.source_candle_close_time

    # Re-serialize with sort_keys to confirm the stored string is already canonical.
    assert plan_a.provenance_json == json.dumps(parsed, sort_keys=True, allow_nan=False)


def test_sr_provenance_entry_field_labels_evidence():
    finding = _finding(
        setup_family="SUPPORT_RESISTANCE_REJECTION",
        evidence={"source_close": 105.0, "zone_lower": 100.0, "zone_upper": 108.0},
    )
    plan = _build(finding)
    parsed = json.loads(plan.provenance_json)
    assert parsed["entry_source_field"] == "evidence.source_close"
    assert parsed["stop_source_field"] == "evidence.zone_lower"


# ------------------------------------------------------------------ no-look-ahead


def test_evaluation_not_before_equals_source_candle_close():
    finding = _finding(
        setup_family="SWEEP_RECLAIM",
        measurements={"reclaim_close": 110.0, "sweep_low": 100.0},
        source_candle_close_time=42_000_300_000,
    )
    plan = _build(finding)
    assert plan.evaluation_not_before_ms == 42_000_300_000


def test_evaluation_expiry_is_fixed_horizon():
    finding = _finding(
        setup_family="SWEEP_RECLAIM",
        primary_timeframe="5m",
        measurements={"reclaim_close": 110.0, "sweep_low": 100.0},
        source_candle_close_time=1_000_000_000,
    )
    plan = _build(finding)
    duration = PRIMARY_TIMEFRAME_DURATION_MS["5m"]
    assert plan.evaluation_expiry_ms == 1_000_000_000 + OUTCOME_HORIZON_BARS * duration


def test_research_only_marker_always_true():
    finding = _finding(setup_family="VOLATILITY_COMPRESSION")
    plan = _build(finding)
    assert plan.research_only is True


# ------------------------------------------------------------------ row round-trip


def test_trade_plan_from_row_round_trips(tmp_path):
    import sqlite3

    finding = _finding(
        setup_family="SWEEP_RECLAIM",
        measurements={"reclaim_close": 110.0, "sweep_low": 100.0},
    )
    plan = _build(finding)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    columns = [f.name for f in dataclasses.fields(TradePlan)]
    conn.execute(f"CREATE TABLE t ({', '.join(columns)})")
    values = [getattr(plan, c) for c in columns]
    placeholders = ",".join("?" for _ in columns)
    conn.execute(f"INSERT INTO t VALUES ({placeholders})", values)
    row = conn.execute("SELECT * FROM t").fetchone()

    rebuilt = trade_plan_from_row(row)
    assert rebuilt == plan
