"""Trade plans and outcome evidence v0.1 — immutable, research-only trade
plan contract for a NEW opportunity.

A TradePlan is a deterministic, pure derivation from one already-immutable
`DetectorFinding` (see opportunity/contract.py) plus the already-assigned
`opportunity_uid`/`fingerprint` identity. It is built and persisted at most
once, in the successful NEW-opportunity branch of engine._record_finding,
and never touched again — a reconfirmation of the same opportunity never
calls this module again, and there is no backfill path for historical
opportunities that predate this milestone (see engine.py).

Safety boundary
----------------
This module never creates an alert/order, never sizes a position, never
recommends leverage, and never reads or influences detector eligibility,
ranking, or scoring (`opportunity/context.py` / `opportunity/scoring.py` are
never imported here, and `strategy/risk.py` — which computes LIVE position
sizing — is never imported here either). A TradePlan carries price levels
and R-multiples only: no account size, dollar risk, quantity, notional,
margin, leverage, or execution instruction is ever computed or stored.

Deterministic entry/invalidation/stop/target formulas
-------------------------------------------------------
Exact formulas, keyed off the immutable first-detection finding fields
already recorded by each detector (see opportunity/detectors/*.py):

  VOLATILITY_COMPRESSION            — always UNAVAILABLE
                                       (REASON_NO_STRUCTURAL_INVALIDATION):
                                       compression describes range/ATR
                                       contraction only, with no structural
                                       level a stop could reference.
  SWEEP_RECLAIM                     — entry = measurements.reclaim_close;
                                       LONG invalidation/stop =
                                       measurements.sweep_low; SHORT
                                       invalidation/stop =
                                       measurements.sweep_high.
  SUPPORT_RESISTANCE_REJECTION,
  SUPPORT_RESISTANCE_BREAKOUT_RETEST,
  SUPPORT_RESISTANCE_FAILED_BREAKOUT — entry = evidence.source_close; LONG
                                       invalidation/stop =
                                       evidence.zone_lower; SHORT
                                       invalidation/stop = evidence.zone_upper.

`invalidation_price` and `stop_price` are kept as two distinct persisted
columns even though their value is identical in v0.1 — they are different
concepts (structural invalidation vs. the risk-management stop) that may
diverge in a later milestone.

Missing/malformed/nonfinite/nonpositive/wrong-side source data (or an
invalid direction/timeframe/setup_family on the finding itself — defensive
only, since detectors only ever emit sealed-contract values) never raises:
it produces an UNAVAILABLE plan with a deterministic `unavailable_reason`
and every derived price/R field left NULL. Nothing here ever infers a level
from data later than the source candle.

No-look-ahead
--------------
`evaluation_not_before_ms` is always exactly `source_candle_close_time` —
the source candle itself can never fill the plan; only a strictly later
closed candle (see trade_plan_outcome.py) can. `evaluation_expiry_ms` is a
fixed `evaluation_not_before_ms + OUTCOME_HORIZON_BARS * timeframe
duration` — a bounded research window, not a holding-period
recommendation.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Literal, Optional

from apex.opportunity.contract import (
    CONTRACT_VERSION,
    PRIMARY_TIMEFRAME_DURATION_MS,
    Direction,
    DetectorFinding,
    PrimaryTimeframe,
    SetupFamily,
)

TRADE_PLAN_CONTRACT_VERSION = "trade_plan_v0_1"

# Bounded prospective research window: 24 bars of the plan's own
# primary_timeframe past evaluation_not_before_ms. Not a holding-period
# recommendation — see trade_plan_outcome.py.
OUTCOME_HORIZON_BARS = 24

ENTRY_TYPE_RESTING_LIMIT_AT_SOURCE_CLOSE = "RESTING_LIMIT_AT_SOURCE_CLOSE"

Availability = Literal["AVAILABLE", "UNAVAILABLE"]

# Deterministic unavailable_reason codes.
REASON_NO_STRUCTURAL_INVALIDATION = "NO_STRUCTURAL_INVALIDATION"
REASON_MISSING_SOURCE_FIELD = "MISSING_SOURCE_FIELD"
REASON_MALFORMED_SOURCE_FIELD = "MALFORMED_SOURCE_FIELD"
REASON_NONFINITE_VALUE = "NONFINITE_VALUE"
REASON_NONPOSITIVE_VALUE = "NONPOSITIVE_VALUE"
REASON_INVALID_STOP_GEOMETRY = "INVALID_STOP_GEOMETRY"
REASON_INVALID_DIRECTION = "INVALID_DIRECTION"
REASON_INVALID_TIMEFRAME = "INVALID_TIMEFRAME"
REASON_UNSUPPORTED_SETUP_FAMILY = "UNSUPPORTED_SETUP_FAMILY"

# Families whose entry/stop levels come from `measurements` vs `evidence`,
# and the exact field-name mapping — see module docstring for the formulas.
_SR_FAMILIES: frozenset[str] = frozenset(
    {
        "SUPPORT_RESISTANCE_REJECTION",
        "SUPPORT_RESISTANCE_BREAKOUT_RETEST",
        "SUPPORT_RESISTANCE_FAILED_BREAKOUT",
    }
)


@dataclass(frozen=True)
class TradePlan:
    """Canonical, versioned, immutable research trade plan for one
    opportunity_uid. See module docstring for the safety boundary and
    exact formulas. Deliberately excludes any account/sizing/execution
    field.
    """

    plan_uid: str
    opportunity_uid: str
    symbol: str
    direction: Direction
    setup_family: SetupFamily
    primary_timeframe: PrimaryTimeframe
    detector_version: str
    opportunity_contract_version: str
    plan_contract_version: str
    source_candle_open_time: int
    source_candle_close_time: int
    created_at: str
    availability: Availability
    unavailable_reason: Optional[str]
    entry_type: Optional[str]
    entry_price: Optional[float]
    invalidation_price: Optional[float]
    stop_price: Optional[float]
    risk_distance: Optional[float]
    target_1r_price: Optional[float]
    target_2r_price: Optional[float]
    target_1r_multiple: Optional[float]
    target_2r_multiple: Optional[float]
    reward_risk_1r: Optional[float]
    reward_risk_2r: Optional[float]
    evaluation_not_before_ms: Optional[int]
    evaluation_expiry_ms: Optional[int]
    provenance_json: str
    warnings_json: str
    research_only: bool = True


def _extract_positive_finite(source: Any, key: str) -> tuple[Optional[float], Optional[str]]:
    """Extract and validate one numeric level from a detector's raw
    `measurements`/`evidence` dict. Returns `(value, None)` on success or
    `(None, reason)` on the first failing check — presence, then real
    numeric type (bool excluded), then finiteness, then strict positivity.
    """
    if not isinstance(source, dict) or key not in source:
        return None, REASON_MISSING_SOURCE_FIELD
    raw = source[key]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None, REASON_MALFORMED_SOURCE_FIELD
    value = float(raw)
    if not math.isfinite(value):
        return None, REASON_NONFINITE_VALUE
    if value <= 0:
        return None, REASON_NONPOSITIVE_VALUE
    return value, None


def _build_provenance(
    finding: DetectorFinding,
    *,
    opportunity_uid: str,
    fingerprint: Optional[str],
    not_before_ms: Optional[int],
    entry_field: Optional[str],
    stop_field: Optional[str],
) -> str:
    """Canonical (sort_keys, allow_nan=False) provenance JSON. Identifies
    the opportunity/fingerprint, every relevant contract/detector version,
    the source candle boundary, the exact source field each level was read
    from, the formula/version applied, and the no-look-ahead boundary.
    Never reads any score/context field (opportunity/context.py and
    opportunity/scoring.py are never imported by this module at all).
    """
    payload = {
        "opportunity_uid": opportunity_uid,
        "fingerprint": fingerprint,
        "opportunity_contract_version": CONTRACT_VERSION,
        "plan_contract_version": TRADE_PLAN_CONTRACT_VERSION,
        "detector_version": finding.detector_version,
        "setup_family": finding.setup_family,
        "symbol": finding.symbol,
        "direction": finding.direction,
        "primary_timeframe": finding.primary_timeframe,
        "source_candle_open_time": finding.source_candle_open_time,
        "source_candle_close_time": finding.source_candle_close_time,
        "entry_source_field": entry_field,
        "stop_source_field": stop_field,
        "formula_version": TRADE_PLAN_CONTRACT_VERSION,
        "no_look_ahead_boundary_ms": not_before_ms,
    }
    return json.dumps(payload, sort_keys=True, allow_nan=False)


def build_trade_plan(
    finding: DetectorFinding,
    *,
    plan_uid: str,
    opportunity_uid: str,
    fingerprint: Optional[str] = None,
    created_at: str,
) -> TradePlan:
    """Pure, deterministic derivation of one immutable TradePlan from one
    DetectorFinding. Never raises — every failure mode (missing setup_family
    support, invalid direction/timeframe, missing/malformed/nonfinite/
    nonpositive/wrong-side source field) yields an UNAVAILABLE plan with a
    deterministic `unavailable_reason` instead.

    `plan_uid`/`opportunity_uid`/`fingerprint`/`created_at` are supplied by
    the caller (engine.py) rather than generated here, matching the existing
    architecture where identity/lifecycle assignment belongs to the engine,
    not the pure detector/plan-building layer (see contract.py's
    DetectorFinding docstring).
    """
    direction_valid = finding.direction in ("LONG", "SHORT")
    timeframe_valid = finding.primary_timeframe in PRIMARY_TIMEFRAME_DURATION_MS
    duration = PRIMARY_TIMEFRAME_DURATION_MS.get(finding.primary_timeframe)
    not_before_ms = finding.source_candle_close_time
    expiry_ms = (
        not_before_ms + OUTCOME_HORIZON_BARS * duration if timeframe_valid and duration else None
    )
    warnings_json = json.dumps(list(finding.warnings), sort_keys=True, allow_nan=False)

    def _unavailable(
        reason: str,
        *,
        entry_field: Optional[str] = None,
        stop_field: Optional[str] = None,
    ) -> TradePlan:
        provenance = _build_provenance(
            finding,
            opportunity_uid=opportunity_uid,
            fingerprint=fingerprint,
            not_before_ms=not_before_ms,
            entry_field=entry_field,
            stop_field=stop_field,
        )
        return TradePlan(
            plan_uid=plan_uid,
            opportunity_uid=opportunity_uid,
            symbol=finding.symbol,
            direction=finding.direction,
            setup_family=finding.setup_family,
            primary_timeframe=finding.primary_timeframe,
            detector_version=finding.detector_version,
            opportunity_contract_version=CONTRACT_VERSION,
            plan_contract_version=TRADE_PLAN_CONTRACT_VERSION,
            source_candle_open_time=finding.source_candle_open_time,
            source_candle_close_time=finding.source_candle_close_time,
            created_at=created_at,
            availability="UNAVAILABLE",
            unavailable_reason=reason,
            entry_type=None,
            entry_price=None,
            invalidation_price=None,
            stop_price=None,
            risk_distance=None,
            target_1r_price=None,
            target_2r_price=None,
            target_1r_multiple=None,
            target_2r_multiple=None,
            reward_risk_1r=None,
            reward_risk_2r=None,
            evaluation_not_before_ms=not_before_ms,
            evaluation_expiry_ms=expiry_ms,
            provenance_json=provenance,
            warnings_json=warnings_json,
        )

    if not direction_valid:
        return _unavailable(REASON_INVALID_DIRECTION)
    if not timeframe_valid:
        return _unavailable(REASON_INVALID_TIMEFRAME)

    if finding.setup_family == "VOLATILITY_COMPRESSION":
        return _unavailable(REASON_NO_STRUCTURAL_INVALIDATION)

    if finding.setup_family == "SWEEP_RECLAIM":
        source = finding.measurements
        source_label = "measurements"
        entry_key = "reclaim_close"
        stop_key = "sweep_low" if finding.direction == "LONG" else "sweep_high"
    elif finding.setup_family in _SR_FAMILIES:
        source = finding.evidence
        source_label = "evidence"
        entry_key = "source_close"
        stop_key = "zone_lower" if finding.direction == "LONG" else "zone_upper"
    else:
        return _unavailable(REASON_UNSUPPORTED_SETUP_FAMILY)

    entry_field = f"{source_label}.{entry_key}"
    stop_field = f"{source_label}.{stop_key}"

    entry_price, entry_err = _extract_positive_finite(source, entry_key)
    if entry_err is not None:
        return _unavailable(entry_err, entry_field=entry_field)
    stop_price, stop_err = _extract_positive_finite(source, stop_key)
    if stop_err is not None:
        return _unavailable(stop_err, entry_field=entry_field, stop_field=stop_field)

    assert entry_price is not None and stop_price is not None  # for type-checkers
    geometry_ok = stop_price < entry_price if finding.direction == "LONG" else stop_price > entry_price
    if not geometry_ok:
        return _unavailable(
            REASON_INVALID_STOP_GEOMETRY, entry_field=entry_field, stop_field=stop_field
        )

    risk_distance = abs(entry_price - stop_price)
    sign = 1.0 if finding.direction == "LONG" else -1.0
    target_1r_price = entry_price + sign * risk_distance
    target_2r_price = entry_price + sign * 2.0 * risk_distance

    provenance = _build_provenance(
        finding,
        opportunity_uid=opportunity_uid,
        fingerprint=fingerprint,
        not_before_ms=not_before_ms,
        entry_field=entry_field,
        stop_field=stop_field,
    )

    return TradePlan(
        plan_uid=plan_uid,
        opportunity_uid=opportunity_uid,
        symbol=finding.symbol,
        direction=finding.direction,
        setup_family=finding.setup_family,
        primary_timeframe=finding.primary_timeframe,
        detector_version=finding.detector_version,
        opportunity_contract_version=CONTRACT_VERSION,
        plan_contract_version=TRADE_PLAN_CONTRACT_VERSION,
        source_candle_open_time=finding.source_candle_open_time,
        source_candle_close_time=finding.source_candle_close_time,
        created_at=created_at,
        availability="AVAILABLE",
        unavailable_reason=None,
        entry_type=ENTRY_TYPE_RESTING_LIMIT_AT_SOURCE_CLOSE,
        entry_price=entry_price,
        invalidation_price=stop_price,
        stop_price=stop_price,
        risk_distance=risk_distance,
        target_1r_price=target_1r_price,
        target_2r_price=target_2r_price,
        target_1r_multiple=1.0,
        target_2r_multiple=2.0,
        reward_risk_1r=1.0,
        reward_risk_2r=2.0,
        evaluation_not_before_ms=not_before_ms,
        evaluation_expiry_ms=expiry_ms,
        provenance_json=provenance,
        warnings_json=warnings_json,
    )


def trade_plan_from_row(row: Any) -> TradePlan:
    """Reconstruct a TradePlan from an `opportunity_trade_plans` row (or any
    mapping exposing the same column names, e.g. a joined query — see
    db/repository.py's get_nonterminal_trade_plan_outcomes). Read-only
    reconstruction; never mutates or re-derives anything.
    """
    return TradePlan(
        plan_uid=row["plan_uid"],
        opportunity_uid=row["opportunity_uid"],
        symbol=row["symbol"],
        direction=row["direction"],
        setup_family=row["setup_family"],
        primary_timeframe=row["primary_timeframe"],
        detector_version=row["detector_version"],
        opportunity_contract_version=row["opportunity_contract_version"],
        plan_contract_version=row["plan_contract_version"],
        source_candle_open_time=row["source_candle_open_time"],
        source_candle_close_time=row["source_candle_close_time"],
        created_at=row["created_at"],
        availability=row["availability"],
        unavailable_reason=row["unavailable_reason"],
        entry_type=row["entry_type"],
        entry_price=row["entry_price"],
        invalidation_price=row["invalidation_price"],
        stop_price=row["stop_price"],
        risk_distance=row["risk_distance"],
        target_1r_price=row["target_1r_price"],
        target_2r_price=row["target_2r_price"],
        target_1r_multiple=row["target_1r_multiple"],
        target_2r_multiple=row["target_2r_multiple"],
        reward_risk_1r=row["reward_risk_1r"],
        reward_risk_2r=row["reward_risk_2r"],
        evaluation_not_before_ms=row["evaluation_not_before_ms"],
        evaluation_expiry_ms=row["evaluation_expiry_ms"],
        provenance_json=row["provenance_json"],
        warnings_json=row["warnings_json"],
        research_only=bool(row["research_only"]),
    )
