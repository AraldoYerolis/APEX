"""Versioned research-only Opportunity contract — TA Opportunity Engine v0.1.

An Opportunity is a prospective, additive research record only. It is not a
signal, alert, or trade instruction, and nothing here may influence
signal_observations, signal_features, alerts, ALERTS_ENABLED, Pushover, or
trade execution/risk sizing (see CLAUDE.md milestone spec).

Deliberately excluded from v0.1 (later milestones): confidence thresholds,
ACTIONABLE/HIGH_CONVICTION classification, alert eligibility, position size,
execution instructions. CONTRACT_VERSION exists so a future milestone can
version-gate how it interprets older rows, the same way
signal_features.feature_version already does for the existing signal path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

CONTRACT_VERSION = "opportunity_v0_1"

Direction = Literal["LONG", "SHORT"]
SetupFamily = Literal["VOLATILITY_COMPRESSION", "SWEEP_RECLAIM"]
OpportunityStatus = Literal["ACTIVE", "EXPIRED"]
PrimaryTimeframe = Literal["3m", "5m"]

# Families whose fingerprint identity must NOT include direction (see
# engine.compute_fingerprint). VOLATILITY_COMPRESSION direction is
# descriptive evidence about a developing expansion, not part of the
# structural compression episode's identity — see
# detectors/volatility_compression.py's module docstring. SWEEP_RECLAIM is
# deliberately excluded from this set: its direction (LONG off a pivot low
# vs SHORT off a pivot high) genuinely is part of the structural event's
# identity, and its fingerprint formula/reconfirmation behavior must stay
# exactly as before.
DIRECTION_INVARIANT_FAMILIES: frozenset[str] = frozenset({"VOLATILITY_COMPRESSION"})

# Fingerprint-construction placeholder standing in for a real LONG/SHORT
# direction when the finding's family is in DIRECTION_INVARIANT_FAMILIES.
# This never reaches the `direction` column itself (see Opportunity below
# and schema.sql's `direction TEXT NOT NULL CHECK(direction IN
# ('LONG','SHORT'))`) — it is purely an identity component inside
# engine.compute_fingerprint's hash input.
DIRECTION_INVARIANT_FINGERPRINT_TOKEN = "DIRECTION_INVARIANT"

# Candle duration in milliseconds for each PrimaryTimeframe, keyed to match
# CandleStore's epoch-ms `open_time`. Used only to derive a real close
# timestamp (open_time + duration) for DetectorFinding.source_candle_close_time
# — CandleStore.get_df() does not expose the candle's actual close_time to
# detectors (see candle_store.py), and open_time + duration is exact for
# fixed-length klines (no DST/leap-second irregularity at these durations).
PRIMARY_TIMEFRAME_DURATION_MS: dict[str, int] = {
    "3m": 3 * 60 * 1000,
    "5m": 5 * 60 * 1000,
}


@dataclass
class DetectorFinding:
    """Raw output of a single pure-detector evaluation.

    Detectors never see the DB and never assign opportunity_uid,
    fingerprint, first_detected_at, or last_seen_at — that identity and
    lifecycle assignment belongs to the engine (see engine.py), so detector
    logic stays trivially unit-testable against plain DataFrames.

    `fingerprint_key` is the detector-chosen, family-specific component that
    the engine combines with symbol/timeframe/family/version (and direction,
    except for DIRECTION_INVARIANT_FAMILIES) to build the final dedupe
    fingerprint (see engine.compute_fingerprint).
    """

    symbol: str
    direction: Direction
    setup_family: SetupFamily
    detector_version: str
    primary_timeframe: PrimaryTimeframe
    source_candle_open_time: int
    source_candle_close_time: int
    fingerprint_key: str
    anchor_price: Optional[float] = None
    anchor_open_time: Optional[int] = None
    evidence: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    measurements: dict = field(default_factory=dict)


@dataclass
class Opportunity:
    """Canonical, versioned research object persisted to
    opportunity_observations. Deliberately minimal for v0.1.
    """

    opportunity_uid: str
    fingerprint: str
    symbol: str
    direction: Direction
    setup_family: SetupFamily
    detector_version: str
    primary_timeframe: PrimaryTimeframe
    first_detected_at: str
    last_seen_at: str
    source_candle_open_time: int
    source_candle_close_time: int
    contract_version: str = CONTRACT_VERSION
    status: OpportunityStatus = "ACTIVE"
    research_only: bool = True
    occurrence_count: int = 1
    anchor_price: Optional[float] = None
    anchor_open_time: Optional[int] = None
    evidence_json: Optional[str] = None
    warnings_json: Optional[str] = None
    measurements_json: Optional[str] = None
    closed_at: Optional[str] = None
    id: Optional[int] = None
