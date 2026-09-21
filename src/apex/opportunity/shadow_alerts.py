"""Opportunity Quality Report and Shadow Alerts v0.1 — pure shadow-alert
evaluator.

`evaluate_shadow_candidates` is a pure, fail-closed batch function. Every
external fact it needs is injected by the caller as a plain argument: the
current aware UTC time, candidate rows, market snapshots, explicit fee/
slippage cost estimates, qualified cohort-quality evidence, and prior local
shadow-decision history. This module never reads a wall clock, an
environment variable, a config object, a database, the filesystem, a
network client, a notification template, or a notification transport — see
`tests/test_shadow_alerts.py`'s transport-isolation test, which proves this
by making any import of `apex.notifications` fail loudly.

This module never sends anything: it only ever returns a `ShadowDecision`
per input candidate, each with a `would_emit` flag, a deterministic set of
reason codes, rule evidence, and — only when every gate passes — a rendered
would-be phone message. Actual delivery (Pushover or otherwise) is
out of scope for v0.1 and deliberately does not exist here.

Rule pipeline
--------------
Every candidate independently passes through a fixed set of *local* gates
(contract validity, freshness, evaluation-window validity (not-yet-started
or expired), market-snapshot sanity, explicit cost, and cohort
qualification) that accumulate every
applicable failure reason code — a candidate is never short-circuited after
its first failing local gate. Only candidates with zero local-gate failures
proceed to the *batch-wide* stages, processed in input order: duplicate-uid
detection, previously-emitted-uid suppression, opposite-direction same-
symbol conflict suppression, same-symbol/same-direction overlapping-window
cluster deduplication (one deterministic representative per cluster — see
`quality_cohorts.build_overlap_clusters`, shared so both modules cluster
identically), then history-based suppression (already-open overlapping
cluster, 30-minute same-symbol/direction cooldown, and rolling 1-hour/
Detroit-calendar-day emission caps). Quiet hours (05:00-22:00
America/Detroit) are evaluated once against `now` and applied to every
candidate as a local gate.

Before any of the above runs, every injected `PriorShadowState` item
(emitted uids, `PriorEmission`s, `OpenClusterWindow`s) is validated for
well-formedness. If any item is malformed, the whole batch fails closed:
every candidate is suppressed with `REASON_PRIOR_STATE_INVALID` and no
other local or batch-wide gate ever runs — a malformed prior fact must
never be silently dropped or repaired and used anyway.

The "exact quality cohort" a candidate is gated on is
`(setup_family, primary_timeframe, direction)` — see
`quality_cohorts.CohortKey`. No numeric score or score band is ever used as
a qualification or alert threshold anywhere in this module.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apex.opportunity.contract import CONTRACT_VERSION as OPPORTUNITY_CONTRACT_VERSION
from apex.opportunity.quality_cohorts import (
    CohortKey,
    OverlapInterval,
    build_overlap_clusters,
    cluster_id_by_uid,
)
from apex.opportunity.quality_uncertainty import qualify_cohort
from apex.opportunity.scoring import SCORE_VERSION
from apex.opportunity.trade_plan import TRADE_PLAN_CONTRACT_VERSION
from apex.opportunity.trade_plan_outcome import TRADE_PLAN_OUTCOME_CONTRACT_VERSION

SHADOW_ALERTS_VERSION = "shadow_alerts_v0_1"

MAX_FRESHNESS_SECONDS = 120.0
MAX_MARKET_SNAPSHOT_AGE_SECONDS = 15.0
MAX_ROUND_TRIP_COST_R = 0.10
COOLDOWN = timedelta(minutes=30)
MAX_EMISSIONS_PER_HOUR = 3
MAX_EMISSIONS_PER_DAY = 10
DETROIT_TZ_NAME = "America/Detroit"
QUIET_HOURS_START_HOUR = 5   # inclusive
QUIET_HOURS_END_HOUR = 22    # exclusive

REASON_INVALID_NOW_TIMESTAMP = "INVALID_NOW_TIMESTAMP"
REASON_PRIOR_STATE_INVALID = "PRIOR_STATE_INVALID"

REASON_OPPORTUNITY_NOT_ACTIVE = "OPPORTUNITY_NOT_ACTIVE"
REASON_OPPORTUNITY_NOT_RESEARCH_ONLY = "OPPORTUNITY_NOT_RESEARCH_ONLY"
REASON_UNSUPPORTED_OPPORTUNITY_CONTRACT_VERSION = "UNSUPPORTED_OPPORTUNITY_CONTRACT_VERSION"
REASON_UNSUPPORTED_PLAN_CONTRACT_VERSION = "UNSUPPORTED_PLAN_CONTRACT_VERSION"
REASON_UNSUPPORTED_OUTCOME_CONTRACT_VERSION = "UNSUPPORTED_OUTCOME_CONTRACT_VERSION"
REASON_UNSUPPORTED_SCORE_VERSION = "UNSUPPORTED_SCORE_VERSION"
REASON_PLAN_UNAVAILABLE = "PLAN_UNAVAILABLE"
REASON_OUTCOME_NOT_PENDING_ENTRY = "OUTCOME_NOT_PENDING_ENTRY"
REASON_OUTCOME_DATA_INCOMPLETE = "OUTCOME_DATA_INCOMPLETE"
REASON_OUTCOME_AMBIGUOUS = "OUTCOME_AMBIGUOUS"
REASON_INVALID_PLAN_GEOMETRY = "INVALID_PLAN_GEOMETRY"

REASON_LAST_SEEN_AT_INVALID = "LAST_SEEN_AT_INVALID"
REASON_LAST_SEEN_IN_FUTURE = "LAST_SEEN_IN_FUTURE"
REASON_STALE_OPPORTUNITY = "STALE_OPPORTUNITY"
REASON_EVALUATION_WINDOW_UNAVAILABLE = "EVALUATION_WINDOW_UNAVAILABLE"
REASON_EVALUATION_NOT_STARTED = "EVALUATION_NOT_STARTED"
REASON_EVALUATION_WINDOW_EXPIRED = "EVALUATION_WINDOW_EXPIRED"

REASON_MARKET_SNAPSHOT_MISSING = "MARKET_SNAPSHOT_MISSING"
REASON_MARKET_SNAPSHOT_SYMBOL_MISMATCH = "MARKET_SNAPSHOT_SYMBOL_MISMATCH"
REASON_MARKET_SNAPSHOT_INVALID_PRICE = "MARKET_SNAPSHOT_INVALID_PRICE"
REASON_MARKET_SNAPSHOT_NOT_CURRENT = "MARKET_SNAPSHOT_NOT_CURRENT"
REASON_MARKET_BAR_TOUCHED_ENTRY = "MARKET_BAR_TOUCHED_ENTRY"
REASON_MARKET_PRICE_OUT_OF_RANGE = "MARKET_PRICE_OUT_OF_RANGE"

REASON_COST_ESTIMATE_MISSING = "COST_ESTIMATE_MISSING"
REASON_COST_ESTIMATE_INVALID = "COST_ESTIMATE_INVALID"
REASON_COST_TOO_HIGH = "COST_TOO_HIGH"

REASON_COHORT_EVIDENCE_MISSING = "COHORT_EVIDENCE_MISSING"
_COHORT_REASON_PREFIX = "COHORT_"

REASON_QUIET_HOURS_ACTIVE = "QUIET_HOURS_ACTIVE"
REASON_TIMEZONE_CONVERSION_FAILED = "TIMEZONE_CONVERSION_FAILED"

REASON_DUPLICATE_OPPORTUNITY_UID_IN_BATCH = "DUPLICATE_OPPORTUNITY_UID_IN_BATCH"
REASON_OPPORTUNITY_ALREADY_EMITTED = "OPPORTUNITY_ALREADY_EMITTED"
REASON_OPPOSITE_DIRECTION_CONFLICT = "OPPOSITE_DIRECTION_CONFLICT"
REASON_CLUSTER_NON_REPRESENTATIVE = "CLUSTER_NON_REPRESENTATIVE"
REASON_OVERLAPS_OPEN_CLUSTER = "OVERLAPS_OPEN_CLUSTER"
REASON_COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"
REASON_ROLLING_HOUR_CAP_REACHED = "ROLLING_HOUR_CAP_REACHED"
REASON_ROLLING_DAY_CAP_REACHED = "ROLLING_DAY_CAP_REACHED"

REASON_EMIT_ELIGIBLE = "EMIT_ELIGIBLE"

MESSAGE_DISCLAIMER = (
    "RESEARCH/SHADOW-ONLY — NOT AN EXECUTION INSTRUCTION. "
    "No position sizing or order-placement instruction included."
)


def _is_finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_finite_positive(value: object) -> bool:
    return _is_finite_number(value) and value > 0  # type: ignore[operator]


# ---------------------------------------------------------------------------
# Injected-fact dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShadowCandidate:
    """One candidate opportunity + plan + outcome evidence snapshot,
    exactly as read (and pre-parsed) by the caller. Every field is a plain
    already-known fact — this module never derives one from a database row
    or a live API response itself.
    """

    opportunity_uid: str
    symbol: str
    direction: str
    setup_family: str
    primary_timeframe: str
    opportunity_status: str
    research_only: bool
    opportunity_contract_version: str
    last_seen_at: str
    score_version: str | None
    plan_contract_version: str | None
    plan_availability: str | None
    entry_price: float | None
    stop_price: float | None
    target_1r_price: float | None
    target_2r_price: float | None
    evaluation_not_before_ms: int | None
    evaluation_expiry_ms: int | None
    outcome_contract_version: str | None
    outcome_state: str | None
    outcome_data_quality: str | None
    outcome_is_ambiguous: bool

    @property
    def cohort_key(self) -> CohortKey:
        return CohortKey(self.setup_family, self.primary_timeframe, self.direction)


@dataclass(frozen=True)
class MarketSnapshot:
    """The current/forming-bar market evidence for one symbol."""

    symbol: str
    as_of: datetime | None
    current_price: float
    bar_open: float
    bar_high: float
    bar_low: float


@dataclass(frozen=True)
class CostEstimate:
    """Explicit fee + slippage estimate in R for one symbol."""

    fee_r: float
    slippage_r: float


@dataclass(frozen=True)
class CohortQualityEvidence:
    """The numeric evidence for one `CohortKey`, already computed offline
    by `quality_cohorts.build_quality_cohort_report` (its
    `by_exact_cohort` mapping). This evaluator re-derives qualification
    itself via `quality_uncertainty.qualify_cohort` rather than trusting a
    pre-computed boolean, so it stays self-auditing against the one shared
    threshold definition.
    """

    distinct_resolved_clusters: int
    ambiguous_rate: float | None
    net_expectancy_r: float | None
    lower_bound_95: float | None


@dataclass(frozen=True)
class PriorEmission:
    symbol: str
    direction: str
    emitted_at: datetime


@dataclass(frozen=True)
class OpenClusterWindow:
    """An already-open (unresolved) same-symbol/direction evaluation
    window from a prior run, injected so this batch can suppress a new
    candidate that overlaps it.
    """

    symbol: str
    direction: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class PriorShadowState:
    emitted_opportunity_uids: frozenset[str] = field(default_factory=frozenset)
    open_cluster_windows: tuple[OpenClusterWindow, ...] = ()
    emissions: tuple[PriorEmission, ...] = ()


@dataclass(frozen=True)
class ShadowDecision:
    opportunity_uid: str
    symbol: str
    direction: str
    would_emit: bool
    reason_codes: tuple[str, ...]
    evidence: dict[str, object]
    message: str | None


# ---------------------------------------------------------------------------
# now / quiet hours
# ---------------------------------------------------------------------------


def _is_aware_utc(now: datetime) -> bool:
    return now.tzinfo is not None and now.utcoffset() == timedelta(0)


def _detroit_local(now: datetime) -> datetime | None:
    try:
        tz = ZoneInfo(DETROIT_TZ_NAME)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return None
    try:
        return now.astimezone(tz)
    except (OverflowError, OSError, ValueError):
        return None


def _quiet_hours_code(detroit_now: datetime | None) -> str | None:
    if detroit_now is None:
        return REASON_TIMEZONE_CONVERSION_FAILED
    hour = detroit_now.hour
    if QUIET_HOURS_START_HOUR <= hour < QUIET_HOURS_END_HOUR:
        return None
    return REASON_QUIET_HOURS_ACTIVE


# ---------------------------------------------------------------------------
# Local (per-candidate) gates
# ---------------------------------------------------------------------------


def _parse_iso8601_utc(value: str | None) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def _valid_geometry(candidate: ShadowCandidate) -> bool:
    values = (
        candidate.entry_price,
        candidate.stop_price,
        candidate.target_1r_price,
        candidate.target_2r_price,
    )
    if not all(_is_finite_positive(v) for v in values):
        return False
    entry, stop, t1, t2 = values  # type: ignore[misc]
    if candidate.direction == "LONG":
        return stop < entry < t1 < t2
    if candidate.direction == "SHORT":
        return stop > entry > t1 > t2
    return False


def _contract_checks(candidate: ShadowCandidate) -> list[str]:
    codes: list[str] = []
    if candidate.opportunity_status != "ACTIVE":
        codes.append(REASON_OPPORTUNITY_NOT_ACTIVE)
    if candidate.research_only is not True:
        codes.append(REASON_OPPORTUNITY_NOT_RESEARCH_ONLY)
    if candidate.opportunity_contract_version != OPPORTUNITY_CONTRACT_VERSION:
        codes.append(REASON_UNSUPPORTED_OPPORTUNITY_CONTRACT_VERSION)
    if candidate.plan_contract_version != TRADE_PLAN_CONTRACT_VERSION:
        codes.append(REASON_UNSUPPORTED_PLAN_CONTRACT_VERSION)
    if candidate.outcome_contract_version != TRADE_PLAN_OUTCOME_CONTRACT_VERSION:
        codes.append(REASON_UNSUPPORTED_OUTCOME_CONTRACT_VERSION)
    if candidate.score_version != SCORE_VERSION:
        codes.append(REASON_UNSUPPORTED_SCORE_VERSION)
    if candidate.plan_availability != "AVAILABLE":
        codes.append(REASON_PLAN_UNAVAILABLE)
    if candidate.outcome_state != "PENDING_ENTRY":
        codes.append(REASON_OUTCOME_NOT_PENDING_ENTRY)
    if candidate.outcome_data_quality != "COMPLETE":
        codes.append(REASON_OUTCOME_DATA_INCOMPLETE)
    if candidate.outcome_is_ambiguous:
        codes.append(REASON_OUTCOME_AMBIGUOUS)
    if not _valid_geometry(candidate):
        codes.append(REASON_INVALID_PLAN_GEOMETRY)
    return codes


def _freshness_and_window_checks(
    candidate: ShadowCandidate, now: datetime
) -> tuple[list[str], float | None]:
    codes: list[str] = []
    last_seen = _parse_iso8601_utc(candidate.last_seen_at)
    age_seconds: float | None = None
    if last_seen is None:
        codes.append(REASON_LAST_SEEN_AT_INVALID)
    else:
        age_seconds = (now - last_seen).total_seconds()
        if age_seconds < 0:
            codes.append(REASON_LAST_SEEN_IN_FUTURE)
        elif age_seconds > MAX_FRESHNESS_SECONDS:
            codes.append(REASON_STALE_OPPORTUNITY)

    if candidate.evaluation_not_before_ms is None or candidate.evaluation_expiry_ms is None:
        codes.append(REASON_EVALUATION_WINDOW_UNAVAILABLE)
    else:
        now_ms = int(now.timestamp() * 1000)
        if now_ms < candidate.evaluation_not_before_ms:
            codes.append(REASON_EVALUATION_NOT_STARTED)
        if now_ms > candidate.evaluation_expiry_ms:
            codes.append(REASON_EVALUATION_WINDOW_EXPIRED)

    return codes, age_seconds


def _snapshot_is_current(as_of: datetime | None, now: datetime) -> bool:
    """A snapshot is current only when `as_of` is a real tz-aware UTC
    `datetime` and its age (relative to the injected `now`, never a real
    clock) is within `[0, MAX_MARKET_SNAPSHOT_AGE_SECONDS]` — missing,
    naive, wrong-offset, future, or stale snapshots are all not current.
    """
    if not isinstance(as_of, datetime) or not _is_aware_utc(as_of):
        return False
    age_seconds = (now - as_of).total_seconds()
    return 0.0 <= age_seconds <= MAX_MARKET_SNAPSHOT_AGE_SECONDS


def _market_checks(
    candidate: ShadowCandidate, snapshot: MarketSnapshot | None, now: datetime
) -> list[str]:
    codes: list[str] = []
    if snapshot is None:
        return [REASON_MARKET_SNAPSHOT_MISSING]
    if snapshot.symbol != candidate.symbol:
        codes.append(REASON_MARKET_SNAPSHOT_SYMBOL_MISMATCH)

    prices = (snapshot.current_price, snapshot.bar_open, snapshot.bar_high, snapshot.bar_low)
    if not all(_is_finite_positive(p) for p in prices):
        codes.append(REASON_MARKET_SNAPSHOT_INVALID_PRICE)
        return codes

    if not _snapshot_is_current(snapshot.as_of, now):
        codes.append(REASON_MARKET_SNAPSHOT_NOT_CURRENT)

    if not _valid_geometry(candidate):
        return codes

    entry = candidate.entry_price
    t1 = candidate.target_1r_price
    assert entry is not None and t1 is not None
    if candidate.direction == "LONG":
        if snapshot.bar_low <= entry:
            codes.append(REASON_MARKET_BAR_TOUCHED_ENTRY)
        if not (entry <= snapshot.current_price <= t1):
            codes.append(REASON_MARKET_PRICE_OUT_OF_RANGE)
    else:
        if snapshot.bar_high >= entry:
            codes.append(REASON_MARKET_BAR_TOUCHED_ENTRY)
        if not (t1 <= snapshot.current_price <= entry):
            codes.append(REASON_MARKET_PRICE_OUT_OF_RANGE)
    return codes


def _cost_checks(
    symbol: str, cost_estimates: Mapping[str, CostEstimate]
) -> tuple[list[str], float | None]:
    estimate = cost_estimates.get(symbol)
    if estimate is None:
        return [REASON_COST_ESTIMATE_MISSING], None
    if not (
        _is_finite_number(estimate.fee_r)
        and estimate.fee_r >= 0
        and _is_finite_number(estimate.slippage_r)
        and estimate.slippage_r >= 0
    ):
        return [REASON_COST_ESTIMATE_INVALID], None
    total = estimate.fee_r + estimate.slippage_r
    codes: list[str] = []
    if total > MAX_ROUND_TRIP_COST_R:
        codes.append(REASON_COST_TOO_HIGH)
    return codes, total


def _cohort_checks(
    cohort_key: CohortKey, cohort_quality: Mapping[CohortKey, CohortQualityEvidence]
) -> tuple[list[str], CohortQualityEvidence | None]:
    evidence = cohort_quality.get(cohort_key)
    if evidence is None:
        return [REASON_COHORT_EVIDENCE_MISSING], None
    result = qualify_cohort(
        distinct_resolved_clusters=evidence.distinct_resolved_clusters,
        ambiguous_rate=evidence.ambiguous_rate,
        net_mean_r=evidence.net_expectancy_r,
        lower_bound_95=evidence.lower_bound_95,
    )
    codes = [f"{_COHORT_REASON_PREFIX}{reason}" for reason in result.reasons]
    return codes, evidence


# ---------------------------------------------------------------------------
# Message rendering
# ---------------------------------------------------------------------------


def _render_message(
    candidate: ShadowCandidate, *, age_seconds: float | None, cohort_evidence: CohortQualityEvidence
) -> str:
    age_label = f"{age_seconds:.0f}s" if age_seconds is not None else "n/a"
    ambiguity_label = (
        f"{cohort_evidence.ambiguous_rate:.3f}" if cohort_evidence.ambiguous_rate is not None else "n/a"
    )
    net_label = (
        f"{cohort_evidence.net_expectancy_r:+.3f}R" if cohort_evidence.net_expectancy_r is not None else "n/a"
    )
    lower_label = (
        f"{cohort_evidence.lower_bound_95:+.3f}R" if cohort_evidence.lower_bound_95 is not None else "n/a"
    )
    lines = [
        MESSAGE_DISCLAIMER,
        (
            f"{candidate.symbol} {candidate.direction} — {candidate.setup_family} "
            f"({candidate.primary_timeframe})"
        ),
        (
            f"Entry {candidate.entry_price:.6g} | Stop {candidate.stop_price:.6g} | "
            f"T1 {candidate.target_1r_price:.6g} | T2 {candidate.target_2r_price:.6g}"
        ),
        f"Freshness: last_seen {age_label} ago",
        (
            "Cohort evidence: "
            f"resolved_clusters={cohort_evidence.distinct_resolved_clusters} "
            f"ambiguity_rate={ambiguity_label} "
            f"net_expectancy={net_label} "
            f"lower_bound_95={lower_label}"
        ),
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Batch evaluation
# ---------------------------------------------------------------------------


def _valid_prior_direction(direction: object) -> bool:
    return direction in ("LONG", "SHORT")


def _valid_prior_int_bound(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_prior_state(prior_state: PriorShadowState, *, now: datetime) -> tuple[str, ...]:
    """Pure, never raises. Validates every injected prior-history item
    before it is trusted anywhere downstream — see module docstring's
    fail-closed prior-history handling. Returns a tuple of human-readable
    diagnostic strings (empty when every item is well-formed), used only as
    auditable evidence on a suppressed `ShadowDecision`, never as a
    qualification/alert input itself.
    """
    errors: list[str] = []

    for uid in prior_state.emitted_opportunity_uids:
        if not isinstance(uid, str) or not uid:
            errors.append("emitted_opportunity_uids contains a non-string or empty value")
            break

    for emission in prior_state.emissions:
        if not isinstance(emission.symbol, str) or not emission.symbol:
            errors.append("a PriorEmission has an empty/non-string symbol")
        elif not _valid_prior_direction(emission.direction):
            errors.append("a PriorEmission has an invalid direction")
        elif not isinstance(emission.emitted_at, datetime) or not _is_aware_utc(
            emission.emitted_at
        ):
            errors.append("a PriorEmission.emitted_at is not a tz-aware UTC datetime")
        elif emission.emitted_at > now:
            errors.append("a PriorEmission.emitted_at is in the future")

    for window in prior_state.open_cluster_windows:
        if not isinstance(window.symbol, str) or not window.symbol:
            errors.append("an OpenClusterWindow has an empty/non-string symbol")
        elif not _valid_prior_direction(window.direction):
            errors.append("an OpenClusterWindow has an invalid direction")
        elif not (
            _valid_prior_int_bound(window.start_ms) and _valid_prior_int_bound(window.end_ms)
        ):
            errors.append("an OpenClusterWindow has non-integer bounds")
        elif window.start_ms > window.end_ms:
            errors.append("an OpenClusterWindow has start_ms > end_ms")

    return tuple(errors)


def _detroit_day_emission_count(
    emissions: Sequence[PriorEmission], *, tz: ZoneInfo, target_date
) -> int:
    count = 0
    for emission in emissions:
        try:
            local_dt = emission.emitted_at.astimezone(tz)
        except (OverflowError, OSError, ValueError):
            continue
        if local_dt.date() == target_date:
            count += 1
    return count


def evaluate_shadow_candidates(
    *,
    now: datetime,
    candidates: Sequence[ShadowCandidate],
    market_snapshots: Mapping[str, MarketSnapshot],
    cost_estimates: Mapping[str, CostEstimate],
    cohort_quality: Mapping[CohortKey, CohortQualityEvidence],
    prior_state: PriorShadowState,
) -> tuple[ShadowDecision, ...]:
    """Pure. See module docstring for the full rule pipeline. Returns one
    `ShadowDecision` per input candidate, in input order.
    """
    if not _is_aware_utc(now):
        return tuple(
            ShadowDecision(
                opportunity_uid=c.opportunity_uid,
                symbol=c.symbol,
                direction=c.direction,
                would_emit=False,
                reason_codes=(REASON_INVALID_NOW_TIMESTAMP,),
                evidence={},
                message=None,
            )
            for c in candidates
        )

    prior_state_errors = _validate_prior_state(prior_state, now=now)
    if prior_state_errors:
        return tuple(
            ShadowDecision(
                opportunity_uid=c.opportunity_uid,
                symbol=c.symbol,
                direction=c.direction,
                would_emit=False,
                reason_codes=(REASON_PRIOR_STATE_INVALID,),
                evidence={"prior_state_validation_errors": prior_state_errors},
                message=None,
            )
            for c in candidates
        )

    detroit_now = _detroit_local(now)
    quiet_code = _quiet_hours_code(detroit_now)

    local_codes: dict[int, list[str]] = {}
    local_evidence: dict[int, dict[str, object]] = {}

    for i, c in enumerate(candidates):
        codes: list[str] = []
        codes.extend(_contract_checks(c))

        window_codes, age_seconds = _freshness_and_window_checks(c, now)
        codes.extend(window_codes)

        codes.extend(_market_checks(c, market_snapshots.get(c.symbol), now))

        cost_codes, round_trip_cost_r = _cost_checks(c.symbol, cost_estimates)
        codes.extend(cost_codes)

        cohort_codes, cohort_evidence = _cohort_checks(c.cohort_key, cohort_quality)
        codes.extend(cohort_codes)

        if quiet_code is not None:
            codes.append(quiet_code)

        local_codes[i] = codes
        local_evidence[i] = {
            "age_seconds": age_seconds,
            "round_trip_cost_r": round_trip_cost_r,
            "cohort_evidence": cohort_evidence,
            "cohort_key": c.cohort_key,
        }

    # Duplicate opportunity_uid within this batch (order-sensitive).
    seen_uids: set[str] = set()
    for i, c in enumerate(candidates):
        if c.opportunity_uid in seen_uids:
            local_codes[i].append(REASON_DUPLICATE_OPPORTUNITY_UID_IN_BATCH)
        else:
            seen_uids.add(c.opportunity_uid)

    # Previously emitted opportunity_uid.
    for i, c in enumerate(candidates):
        if c.opportunity_uid in prior_state.emitted_opportunity_uids:
            local_codes[i].append(REASON_OPPORTUNITY_ALREADY_EMITTED)

    conflict_stage_indices = [i for i in range(len(candidates)) if not local_codes[i]]

    # Opposite-direction same-symbol conflict.
    directions_by_symbol: dict[str, set[str]] = {}
    for i in conflict_stage_indices:
        c = candidates[i]
        directions_by_symbol.setdefault(c.symbol, set()).add(c.direction)
    conflicted_symbols = {s for s, dirs in directions_by_symbol.items() if len(dirs) > 1}
    for i in conflict_stage_indices:
        if candidates[i].symbol in conflicted_symbols:
            local_codes[i].append(REASON_OPPOSITE_DIRECTION_CONFLICT)

    cluster_stage_indices = [i for i in conflict_stage_indices if not local_codes[i]]

    # Same-symbol/direction overlapping-window clustering across
    # families/timeframes; one deterministic representative per cluster.
    intervals = [
        OverlapInterval(
            uid=candidates[i].opportunity_uid,
            symbol=candidates[i].symbol,
            direction=candidates[i].direction,
            start_ms=candidates[i].evaluation_not_before_ms,  # type: ignore[arg-type]
            end_ms=candidates[i].evaluation_expiry_ms,  # type: ignore[arg-type]
        )
        for i in cluster_stage_indices
    ]
    clusters = build_overlap_clusters(intervals)
    uid_to_cluster = cluster_id_by_uid(clusters)
    interval_by_uid = {iv.uid: iv for iv in intervals}
    representative_uid_by_cluster: dict[str, str] = {}
    for cluster in clusters:
        members = [interval_by_uid[uid] for uid in cluster.member_uids]
        representative = min(members, key=lambda iv: (iv.start_ms, iv.uid))
        representative_uid_by_cluster[cluster.cluster_id] = representative.uid

    for i in cluster_stage_indices:
        c = candidates[i]
        cluster_id = uid_to_cluster.get(c.opportunity_uid)
        if cluster_id is not None and representative_uid_by_cluster.get(cluster_id) != c.opportunity_uid:
            local_codes[i].append(REASON_CLUSTER_NON_REPRESENTATIVE)

    history_stage_indices = [i for i in cluster_stage_indices if not local_codes[i]]

    tz = None if detroit_now is None else ZoneInfo(DETROIT_TZ_NAME)
    accumulated_emissions: list[PriorEmission] = list(prior_state.emissions)

    for i in history_stage_indices:
        c = candidates[i]
        start_ms = c.evaluation_not_before_ms
        end_ms = c.evaluation_expiry_ms

        overlaps_open = any(
            w.symbol == c.symbol
            and w.direction == c.direction
            and start_ms <= w.end_ms
            and w.start_ms <= end_ms
            for w in prior_state.open_cluster_windows
        )
        if overlaps_open:
            local_codes[i].append(REASON_OVERLAPS_OPEN_CLUSTER)

        same_key_emissions = [
            e for e in accumulated_emissions if e.symbol == c.symbol and e.direction == c.direction
        ]
        if same_key_emissions:
            last_emission = max(e.emitted_at for e in same_key_emissions)
            if now - last_emission < COOLDOWN:
                local_codes[i].append(REASON_COOLDOWN_ACTIVE)

        hour_ago = now - timedelta(hours=1)
        hour_count = sum(1 for e in accumulated_emissions if hour_ago < e.emitted_at <= now)
        if hour_count >= MAX_EMISSIONS_PER_HOUR:
            local_codes[i].append(REASON_ROLLING_HOUR_CAP_REACHED)

        day_count = _detroit_day_emission_count(
            accumulated_emissions, tz=tz, target_date=detroit_now.date()
        )
        if day_count >= MAX_EMISSIONS_PER_DAY:
            local_codes[i].append(REASON_ROLLING_DAY_CAP_REACHED)

        if not local_codes[i]:
            accumulated_emissions.append(
                PriorEmission(symbol=c.symbol, direction=c.direction, emitted_at=now)
            )

    decisions: list[ShadowDecision] = []
    for i, c in enumerate(candidates):
        codes = tuple(local_codes[i])
        would_emit = len(codes) == 0
        evidence = dict(local_evidence[i])
        message = None
        if would_emit:
            message = _render_message(
                c, age_seconds=evidence["age_seconds"], cohort_evidence=evidence["cohort_evidence"]
            )
            codes = (REASON_EMIT_ELIGIBLE,)
        decisions.append(
            ShadowDecision(
                opportunity_uid=c.opportunity_uid,
                symbol=c.symbol,
                direction=c.direction,
                would_emit=would_emit,
                reason_codes=codes,
                evidence=evidence,
                message=message,
            )
        )
    return tuple(decisions)
