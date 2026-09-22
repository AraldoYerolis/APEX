"""Shadow Alert Runtime Wiring v0.1 — default-off, fail-closed, read-only
runtime adapter wiring the pure `apex.opportunity.shadow_alerts` evaluator
(and `apex.opportunity.quality_cohorts`) to live APEX state, for a bounded
research pilot. Prepares evidence for a future bounded 24-hour research
pilot; this milestone itself never activates anything.

`run_shadow_alert_runtime` is the only entry point. It re-checks every
guard — `settings.shadow_alert_pilot_enabled`,
`settings.opportunity_engine_enabled`, `settings.trade_plan_evidence_enabled`,
`settings.dry_run_mode`, `not settings.alerts_enabled` — at entry regardless
of how/whether the caller already gated it (defense in depth, same pattern
as `apex.research.gmgn.runtime`/`apex.opportunity.engine`), before any
candidate/cohort/history/database read. Pilot identity/start/deadline/cost
settings are additionally validated (`resolve_pilot_state`): missing,
malformed, non-UTC, nonfinite, negative, reversed, expired, or
more-than-24-hours-apart settings fail the pilot closed. The runtime does
nothing before its configured start and permanently no-ops at/after its
deadline; it never mutates `Settings` to stop itself.

This module never imports `apex.notifications`, `apex.actions`,
`apex.strategy`, a broker/exchange client, or a sizing/order/trade module —
see `tests/test_shadow_runtime.py`'s AST-based isolation test. It never
sends anything: `evaluate_shadow_candidates` (the pure evaluator) is the
only place a `would_emit`/message decision is made, and this module only
ever persists what that function already decided, into the additive,
append-only `shadow_alert_decisions` table (see `db/schema.sql`) — never
`alerts`, `paper_trades`, `daily_risk`, any signal table, or any
opportunity/plan/outcome table.

Live market snapshot
----------------------
Market evidence comes only from `CandleStore.get_latest_live_snapshot`
(the latest ACCEPTED live WS observation) — never preload, backfill,
reconciliation, a closed-candle timestamp, `time.time()`, or a fabricated
current timestamp. A missing/stale live WS observation is passed straight
through as a missing `MarketSnapshot`, which `shadow_alerts` itself
suppresses on (`REASON_MARKET_SNAPSHOT_MISSING`).

`shadow_alerts.evaluate_shadow_candidates`'s `market_snapshots` mapping is
keyed only by symbol, while `CandleStore` snapshots are keyed by
`(symbol, timeframe)`. When one run's active candidate batch contains more
than one distinct `primary_timeframe` for the same symbol, no single
symbol-keyed snapshot could ever be correct for every candidate on that
symbol — so this adapter never places a snapshot for that symbol at all
(even if a valid WS snapshot exists for one or more of its timeframes),
and records explicit `MULTI_TIMEFRAME_CONFLICT_EVIDENCE_KEY` rule evidence
(the sorted conflicting timeframe list) on every affected decision. Every
candidate on that symbol is then suppressed by the pure evaluator itself
via `REASON_MARKET_SNAPSHOT_MISSING` — the evaluator's own contract,
ordering, and `would_emit` logic are never touched.

Cost consistency
------------------
`settings.shadow_alert_pilot_fee_r + settings.shadow_alert_pilot_slippage_r`
is the one total round-trip cost assumption used both to build the exact
cohort quality report (`quality_cohorts.build_quality_cohort_report`) and
as every candidate's `CostEstimate` — the same number feeds both.

Bounded reads, fail-closed on overflow/malformed rows
---------------------------------------------------------
Candidate, quality-report, and prior-decision reads are each bounded by a
fixed constant. Exceeding a bound, or a row that fails to map onto its
target dataclass (schema mismatch), aborts the whole run without
evaluating anything — never a silent truncate-and-continue.

Prior-state reconstruction, not repair
------------------------------------------
Every prior `would_emit=1` decision for this pilot is read back from
`shadow_alert_decisions` on every run (so a process restart never resets
suppression state) and mapped onto `shadow_alerts.PriorShadowState` as
literally as possible — a malformed stored value is never repaired or
dropped here; it is passed through so `evaluate_shadow_candidates`' own
prior-state validation can (and will) suppress the whole batch with
`REASON_PRIOR_STATE_INVALID`.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Optional

from apex.config import Settings
from apex.data.candle_store import CandleStore
from apex.db import repository as repo
from apex.opportunity.quality_cohorts import (
    CohortKey,
    QualityRow,
    build_quality_cohort_report,
)
from apex.opportunity.shadow_alerts import (
    SHADOW_ALERTS_VERSION,
    CohortQualityEvidence,
    CostEstimate,
    MarketSnapshot,
    OpenClusterWindow,
    PriorEmission,
    PriorShadowState,
    ShadowCandidate,
    ShadowDecision,
    evaluate_shadow_candidates,
)

logger = logging.getLogger("apex.opportunity.shadow_runtime")

RUNTIME_VERSION = "shadow_alert_runtime_v0_1"

# Bounded pilot-settings validation (see resolve_pilot_state).
MAX_PILOT_DURATION_HOURS = 24

# Bounded, explicit read limits per run (see run_shadow_alert_runtime). A
# read returning more than the bound aborts the whole run rather than
# silently truncating.
MAX_CANDIDATES_PER_RUN = 200
MAX_QUALITY_REPORT_ROWS_PER_RUN = 20_000
MAX_PRIOR_DECISION_ROWS_PER_RUN = 5_000

# Pilot gate / run-outcome reason codes — never a candidate-level reason
# code (those come from apex.opportunity.shadow_alerts unchanged).
PILOT_STATE_DISABLED = "PILOT_DISABLED"
PILOT_STATE_GUARD_NOT_MET = "SCHEDULER_GUARD_NOT_MET"
PILOT_STATE_INVALID_NOW = "INVALID_NOW"
PILOT_STATE_INVALID_SETTINGS = "INVALID_PILOT_SETTINGS"
PILOT_STATE_NOT_STARTED = "PILOT_NOT_STARTED"
PILOT_STATE_EXPIRED = "PILOT_EXPIRED"
PILOT_STATE_ACTIVE = "PILOT_ACTIVE"

RUN_REASON_OK = "OK"
RUN_REASON_CANDIDATE_QUERY_FAILED = "CANDIDATE_QUERY_FAILED"
RUN_REASON_CANDIDATE_LIMIT_OVERFLOW = "CANDIDATE_LIMIT_OVERFLOW"
RUN_REASON_MALFORMED_CANDIDATE_ROW = "MALFORMED_CANDIDATE_ROW"
RUN_REASON_QUALITY_QUERY_FAILED = "QUALITY_QUERY_FAILED"
RUN_REASON_QUALITY_LIMIT_OVERFLOW = "QUALITY_LIMIT_OVERFLOW"
RUN_REASON_MALFORMED_QUALITY_ROW = "MALFORMED_QUALITY_ROW"
RUN_REASON_QUALITY_REPORT_BUILD_FAILED = "QUALITY_REPORT_BUILD_FAILED"
RUN_REASON_PRIOR_QUERY_FAILED = "PRIOR_QUERY_FAILED"
RUN_REASON_PRIOR_LIMIT_OVERFLOW = "PRIOR_LIMIT_OVERFLOW"
RUN_REASON_WRITE_FAILED = "WRITE_FAILED"

# Runtime-only rule-evidence key (never one of shadow_alerts' own evidence
# keys) recording that this adapter withheld a symbol's market snapshot
# entirely because its active candidate batch spanned more than one
# primary_timeframe for that symbol (see run_shadow_alert_runtime's market
# snapshot loop) — never a candidate-level reason code, and never something
# the pure evaluator itself computes.
MULTI_TIMEFRAME_CONFLICT_EVIDENCE_KEY = "market_snapshot_withheld_multi_timeframe_conflict"


def _is_finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _parse_iso8601_utc(value: object) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def _parse_iso8601_ms_utc(value: object) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def _is_aware_utc(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() == timedelta(0)


def _run_at_for(now: datetime) -> str:
    """Deterministic, millisecond-precision run identity/timestamp for one
    whole `run_shadow_alert_runtime` call — used both as the human-readable
    run timestamp and, combined with pilot_id + opportunity_uid, as the
    database-level idempotency key (see schema.sql's UNIQUE constraint).
    """
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


@dataclass(frozen=True)
class PilotConfig:
    """Fully validated pilot identity/window/cost settings (see
    resolve_pilot_state). Never constructed for an invalid settings
    combination.
    """

    pilot_id: str
    start_at: datetime
    deadline_at: datetime
    fee_r: float
    slippage_r: float

    @property
    def total_cost_r(self) -> float:
        return self.fee_r + self.slippage_r


def resolve_pilot_state(settings: Settings, *, now: datetime) -> tuple[str, Optional[PilotConfig]]:
    """Pure. Resolves every gate this milestone requires, in order, and
    returns `(state, config)` — `config` is populated only for
    `PILOT_STATE_NOT_STARTED`/`PILOT_STATE_EXPIRED`/`PILOT_STATE_ACTIVE`
    (a fully well-formed pilot), and is None for every state that reflects
    a disabled/unmet-guard/invalid-input condition. Only
    `PILOT_STATE_ACTIVE` means the caller should actually evaluate
    candidates — every other state is a no-op, never a partial run.
    """
    if not settings.shadow_alert_pilot_enabled:
        return PILOT_STATE_DISABLED, None

    if not (
        settings.opportunity_engine_enabled
        and settings.trade_plan_evidence_enabled
        and settings.dry_run_mode
        and not settings.alerts_enabled
    ):
        return PILOT_STATE_GUARD_NOT_MET, None

    if not _is_aware_utc(now):
        return PILOT_STATE_INVALID_NOW, None

    pilot_id = settings.shadow_alert_pilot_id
    if not isinstance(pilot_id, str) or not pilot_id:
        return PILOT_STATE_INVALID_SETTINGS, None

    start = _parse_iso8601_utc(settings.shadow_alert_pilot_start_at)
    deadline = _parse_iso8601_utc(settings.shadow_alert_pilot_deadline_at)
    if start is None or deadline is None:
        return PILOT_STATE_INVALID_SETTINGS, None
    if deadline <= start:
        return PILOT_STATE_INVALID_SETTINGS, None
    if (deadline - start) > timedelta(hours=MAX_PILOT_DURATION_HOURS):
        return PILOT_STATE_INVALID_SETTINGS, None

    fee_r = settings.shadow_alert_pilot_fee_r
    slippage_r = settings.shadow_alert_pilot_slippage_r
    if not (_is_finite_number(fee_r) and fee_r >= 0):
        return PILOT_STATE_INVALID_SETTINGS, None
    if not (_is_finite_number(slippage_r) and slippage_r >= 0):
        return PILOT_STATE_INVALID_SETTINGS, None

    config = PilotConfig(
        pilot_id=pilot_id,
        start_at=start,
        deadline_at=deadline,
        fee_r=float(fee_r),
        slippage_r=float(slippage_r),
    )

    if now < start:
        return PILOT_STATE_NOT_STARTED, config
    if now >= deadline:
        return PILOT_STATE_EXPIRED, config
    return PILOT_STATE_ACTIVE, config


@dataclass(frozen=True)
class ShadowRuntimeResult:
    """Summary of one `run_shadow_alert_runtime` call. `ran=False` means no
    candidate was ever evaluated (see `reason`, one of the `PILOT_STATE_*`
    or `RUN_REASON_*` constants above); every count is 0 in that case.
    """

    ran: bool
    reason: str
    pilot_id: Optional[str] = None
    run_at: Optional[str] = None
    candidate_count: int = 0
    would_emit_count: int = 0
    suppressed_count: int = 0
    inserted_count: int = 0


def _candidate_from_row(row: sqlite3.Row) -> ShadowCandidate:
    return ShadowCandidate(
        opportunity_uid=row["opportunity_uid"],
        symbol=row["symbol"],
        direction=row["direction"],
        setup_family=row["setup_family"],
        primary_timeframe=row["primary_timeframe"],
        opportunity_status=row["opportunity_status"],
        research_only=bool(row["research_only"]),
        opportunity_contract_version=row["opportunity_contract_version"],
        last_seen_at=row["last_seen_at"],
        score_version=row["score_version"],
        plan_contract_version=row["plan_contract_version"],
        plan_availability=row["plan_availability"],
        entry_price=row["entry_price"],
        stop_price=row["stop_price"],
        target_1r_price=row["target_1r_price"],
        target_2r_price=row["target_2r_price"],
        evaluation_not_before_ms=row["evaluation_not_before_ms"],
        evaluation_expiry_ms=row["evaluation_expiry_ms"],
        outcome_contract_version=row["outcome_contract_version"],
        outcome_state=row["outcome_state"],
        outcome_data_quality=row["outcome_data_quality"],
        outcome_is_ambiguous=bool(row["outcome_is_ambiguous"]),
    )


def _quality_row_from_row(row: sqlite3.Row) -> QualityRow:
    return QualityRow(
        opportunity_uid=row["opportunity_uid"],
        symbol=row["symbol"],
        direction=row["direction"],
        setup_family=row["setup_family"],
        primary_timeframe=row["primary_timeframe"],
        total_score=row["total_score"],
        score_version=row["score_version"],
        plan_availability=row["plan_availability"],
        evaluation_not_before_ms=row["evaluation_not_before_ms"],
        evaluation_expiry_ms=row["evaluation_expiry_ms"],
        outcome_state=row["outcome_state"],
    )


def _reconstruct_prior_state(rows: Sequence[sqlite3.Row], *, now: datetime) -> PriorShadowState:
    """Deterministic, non-repairing reconstruction of `PriorShadowState`
    from every persisted would_emit=1 decision for this pilot (see
    `repo.get_shadow_emitted_decisions`). Every field is copied through as
    literally as possible — never coerced, defaulted, or dropped on a
    malformed value — so a corrupted row reaches
    `shadow_alerts.evaluate_shadow_candidates`'s own prior-state validation
    as genuinely invalid (`REASON_PRIOR_STATE_INVALID`, whole-batch
    suppression) rather than being silently repaired or excluded here.
    """
    now_ms = int(now.timestamp() * 1000)
    emitted_uids: set[str] = set()
    emissions: list[PriorEmission] = []
    open_windows: list[OpenClusterWindow] = []

    for row in rows:
        uid = row["opportunity_uid"]
        emitted_uids.add(uid)

        parsed_at = _parse_iso8601_ms_utc(row["run_at"])
        emissions.append(
            PriorEmission(
                symbol=row["symbol"],
                direction=row["direction"],
                # A row whose stored run_at fails to parse is never
                # dropped: the raw (non-datetime) value is passed straight
                # through so shadow_alerts._validate_prior_state's own
                # isinstance(..., datetime) check catches it.
                emitted_at=parsed_at if parsed_at is not None else row["run_at"],
            )
        )

        start_ms = row["evaluation_not_before_ms"]
        end_ms = row["evaluation_expiry_ms"]
        end_is_valid_int = isinstance(end_ms, int) and not isinstance(end_ms, bool)
        # A window whose bounds are missing/malformed is never treated as
        # "resolved" just to avoid reporting it — it is always still
        # included, letting shadow_alerts' own bound validation catch the
        # malformation. Only a genuinely well-formed, already-past window
        # is excluded as legitimately no longer open.
        still_open = not end_is_valid_int or end_ms > now_ms
        if still_open:
            open_windows.append(
                OpenClusterWindow(
                    symbol=row["symbol"],
                    direction=row["direction"],
                    start_ms=start_ms,
                    end_ms=end_ms,
                )
            )

    return PriorShadowState(
        emitted_opportunity_uids=frozenset(emitted_uids),
        open_cluster_windows=tuple(open_windows),
        emissions=tuple(emissions),
    )


def _json_safe_evidence(evidence: Mapping[str, object]) -> dict[str, object]:
    """JSON-serializable copy of one `ShadowDecision.evidence` dict — the
    only two non-JSON-native value types shadow_alerts ever puts there
    (`CohortQualityEvidence`, `CohortKey`) are expanded into plain dicts;
    every other value (float, None, tuple-of-str) already round-trips
    through `json.dumps` unchanged.
    """
    safe: dict[str, object] = {}
    for key, value in evidence.items():
        if isinstance(value, CohortQualityEvidence):
            safe[key] = {
                "distinct_resolved_clusters": value.distinct_resolved_clusters,
                "ambiguous_rate": value.ambiguous_rate,
                "net_expectancy_r": value.net_expectancy_r,
                "lower_bound_95": value.lower_bound_95,
            }
        elif isinstance(value, CohortKey):
            safe[key] = {
                "setup_family": value.setup_family,
                "primary_timeframe": value.primary_timeframe,
                "direction": value.direction,
            }
        else:
            safe[key] = value
    return safe


def _cohort_evidence_json(evidence: Mapping[str, object]) -> Optional[str]:
    cohort_evidence = evidence.get("cohort_evidence")
    if not isinstance(cohort_evidence, CohortQualityEvidence):
        return None
    return json.dumps(
        {
            "distinct_resolved_clusters": cohort_evidence.distinct_resolved_clusters,
            "ambiguous_rate": cohort_evidence.ambiguous_rate,
            "net_expectancy_r": cohort_evidence.net_expectancy_r,
            "lower_bound_95": cohort_evidence.lower_bound_95,
        },
        sort_keys=True,
        allow_nan=False,
    )


def _market_snapshot_json(snapshot: Optional[MarketSnapshot]) -> Optional[str]:
    if snapshot is None:
        return None
    as_of = snapshot.as_of.strftime("%Y-%m-%dT%H:%M:%S.%fZ") if isinstance(snapshot.as_of, datetime) else None
    return json.dumps(
        {
            "symbol": snapshot.symbol,
            "as_of": as_of,
            "current_price": snapshot.current_price,
            "bar_open": snapshot.bar_open,
            "bar_high": snapshot.bar_high,
            "bar_low": snapshot.bar_low,
        },
        sort_keys=True,
        allow_nan=False,
    )


def _build_decision_record(
    *,
    pilot: PilotConfig,
    run_at: str,
    candidate: ShadowCandidate,
    decision: ShadowDecision,
    market_snapshots: Mapping[str, MarketSnapshot],
    multi_timeframe_conflicts: Mapping[str, tuple[str, ...]],
) -> repo.ShadowDecisionRecord:
    evidence = decision.evidence
    safe_evidence = _json_safe_evidence(evidence)
    conflicting_timeframes = multi_timeframe_conflicts.get(candidate.symbol)
    if conflicting_timeframes is not None:
        # Runtime-only evidence: the pure evaluator's own evidence dict is
        # never mutated here — see module docstring's "never let one
        # timeframe's OHLC snapshot be silently reused for another
        # timeframe". This key documents WHY market_snapshot_json is None
        # for this decision even though a WS snapshot may exist for one or
        # more of this symbol's other active timeframes.
        safe_evidence[MULTI_TIMEFRAME_CONFLICT_EVIDENCE_KEY] = list(conflicting_timeframes)
    return repo.ShadowDecisionRecord(
        pilot_id=pilot.pilot_id,
        evaluator_version=SHADOW_ALERTS_VERSION,
        runtime_version=RUNTIME_VERSION,
        run_at=run_at,
        opportunity_uid=candidate.opportunity_uid,
        symbol=candidate.symbol,
        direction=candidate.direction,
        setup_family=candidate.setup_family,
        primary_timeframe=candidate.primary_timeframe,
        evaluation_not_before_ms=candidate.evaluation_not_before_ms,
        evaluation_expiry_ms=candidate.evaluation_expiry_ms,
        entry_price=candidate.entry_price,
        stop_price=candidate.stop_price,
        target_1r_price=candidate.target_1r_price,
        target_2r_price=candidate.target_2r_price,
        market_snapshot_json=_market_snapshot_json(market_snapshots.get(candidate.symbol)),
        cost_fee_r=pilot.fee_r,
        cost_slippage_r=pilot.slippage_r,
        cost_total_r=pilot.total_cost_r,
        cohort_evidence_json=_cohort_evidence_json(evidence),
        would_emit=decision.would_emit,
        reason_codes_json=json.dumps(list(decision.reason_codes), allow_nan=False),
        message=decision.message,
        rule_evidence_json=json.dumps(safe_evidence, sort_keys=True, allow_nan=False, default=str),
    )


async def run_shadow_alert_runtime(
    conn: sqlite3.Connection,
    candle_store: CandleStore,
    settings: Settings,
    *,
    now: Optional[datetime] = None,
) -> ShadowRuntimeResult:
    """Default-off, fail-closed Shadow Alert Runtime Wiring v0.1 entry
    point. Re-checks every guard (see module docstring) regardless of how
    it was invoked — `apex.main`'s scheduler registration is the primary
    gate, this is defense in depth. One aware UTC `now` is captured for the
    whole run (test-injectable; real wall clock by default) and used
    everywhere — never a second, independent clock read.
    """
    moment = now if now is not None else datetime.now(UTC)
    state, pilot = resolve_pilot_state(settings, now=moment)
    if state != PILOT_STATE_ACTIVE or pilot is None:
        return ShadowRuntimeResult(ran=False, reason=state)

    try:
        candidate_rows = repo.get_active_shadow_candidate_rows(conn, limit=MAX_CANDIDATES_PER_RUN + 1)
    except sqlite3.Error:
        logger.error("Shadow alert runtime candidate query failed", exc_info=True)
        return ShadowRuntimeResult(ran=False, reason=RUN_REASON_CANDIDATE_QUERY_FAILED, pilot_id=pilot.pilot_id)
    if len(candidate_rows) > MAX_CANDIDATES_PER_RUN:
        return ShadowRuntimeResult(ran=False, reason=RUN_REASON_CANDIDATE_LIMIT_OVERFLOW, pilot_id=pilot.pilot_id)

    try:
        candidates = [_candidate_from_row(row) for row in candidate_rows]
    except (IndexError, KeyError, TypeError, ValueError):
        logger.error("Shadow alert runtime candidate row mapping failed", exc_info=True)
        return ShadowRuntimeResult(ran=False, reason=RUN_REASON_MALFORMED_CANDIDATE_ROW, pilot_id=pilot.pilot_id)

    try:
        quality_rows_raw = repo.get_shadow_quality_rows(conn, limit=MAX_QUALITY_REPORT_ROWS_PER_RUN + 1)
    except sqlite3.Error:
        logger.error("Shadow alert runtime quality-row query failed", exc_info=True)
        return ShadowRuntimeResult(ran=False, reason=RUN_REASON_QUALITY_QUERY_FAILED, pilot_id=pilot.pilot_id)
    if len(quality_rows_raw) > MAX_QUALITY_REPORT_ROWS_PER_RUN:
        return ShadowRuntimeResult(ran=False, reason=RUN_REASON_QUALITY_LIMIT_OVERFLOW, pilot_id=pilot.pilot_id)

    try:
        quality_rows = [_quality_row_from_row(row) for row in quality_rows_raw]
    except (IndexError, KeyError, TypeError, ValueError):
        logger.error("Shadow alert runtime quality row mapping failed", exc_info=True)
        return ShadowRuntimeResult(ran=False, reason=RUN_REASON_MALFORMED_QUALITY_ROW, pilot_id=pilot.pilot_id)

    try:
        cohort_report = build_quality_cohort_report(quality_rows, cost_r=pilot.total_cost_r)
    except (TypeError, ValueError):
        # A quality row's evaluation_not_before_ms/evaluation_expiry_ms can
        # hold a type-mismatched malformed value (SQLite has no column-level
        # type enforcement) that mapped cleanly onto QualityRow (see
        # _quality_row_from_row) but only surfaces once
        # build_quality_cohort_report actually compares/sorts it (e.g.
        # QualityRow.has_overlap_window, build_overlap_clusters) — caught
        # here, precisely, rather than left to crash the whole run.
        logger.error("Shadow alert runtime quality report build failed", exc_info=True)
        return ShadowRuntimeResult(
            ran=False, reason=RUN_REASON_QUALITY_REPORT_BUILD_FAILED, pilot_id=pilot.pilot_id
        )
    cohort_quality = {
        key: CohortQualityEvidence(
            distinct_resolved_clusters=summary.distinct_resolved_clusters,
            ambiguous_rate=summary.rates.ambiguous_rate,
            net_expectancy_r=summary.net_mean_r,
            lower_bound_95=summary.uncertainty.lower_bound_95,
        )
        for key, summary in cohort_report.by_exact_cohort.items()
    }

    # The pure evaluator's market_snapshots mapping is keyed only by symbol,
    # but CandleStore snapshots are keyed by (symbol, timeframe). When one
    # active batch contains more than one distinct primary_timeframe for the
    # same symbol, no single symbol-keyed snapshot could ever be correct for
    # every candidate on that symbol — so no snapshot is ever placed for
    # that symbol at all, even if a valid WS snapshot exists for one or more
    # of its timeframes. Every candidate on that symbol is then suppressed
    # by evaluate_shadow_candidates itself via REASON_MARKET_SNAPSHOT_MISSING
    # (unchanged evaluator contract) — see module docstring.
    timeframes_by_symbol: dict[str, set[str]] = {}
    for candidate in candidates:
        timeframes_by_symbol.setdefault(candidate.symbol, set()).add(candidate.primary_timeframe)
    multi_timeframe_conflicts: dict[str, tuple[str, ...]] = {
        symbol: tuple(sorted(timeframes))
        for symbol, timeframes in timeframes_by_symbol.items()
        if len(timeframes) > 1
    }

    market_snapshots: dict[str, MarketSnapshot] = {}
    for candidate in candidates:
        if candidate.symbol in multi_timeframe_conflicts:
            continue
        if candidate.symbol in market_snapshots:
            continue
        snapshot = candle_store.get_latest_live_snapshot(candidate.symbol, candidate.primary_timeframe)
        if snapshot is None:
            continue
        market_snapshots[candidate.symbol] = MarketSnapshot(
            symbol=snapshot.symbol,
            as_of=datetime.fromtimestamp(snapshot.received_at_ms / 1000, tz=UTC),
            current_price=snapshot.close,
            bar_open=snapshot.open,
            bar_high=snapshot.high,
            bar_low=snapshot.low,
        )

    cost_estimates = {
        candidate.symbol: CostEstimate(fee_r=pilot.fee_r, slippage_r=pilot.slippage_r)
        for candidate in candidates
    }

    try:
        prior_rows = repo.get_shadow_emitted_decisions(
            conn, pilot.pilot_id, limit=MAX_PRIOR_DECISION_ROWS_PER_RUN + 1
        )
    except sqlite3.Error:
        logger.error("Shadow alert runtime prior-decision query failed", exc_info=True)
        return ShadowRuntimeResult(ran=False, reason=RUN_REASON_PRIOR_QUERY_FAILED, pilot_id=pilot.pilot_id)
    if len(prior_rows) > MAX_PRIOR_DECISION_ROWS_PER_RUN:
        return ShadowRuntimeResult(ran=False, reason=RUN_REASON_PRIOR_LIMIT_OVERFLOW, pilot_id=pilot.pilot_id)

    prior_state = _reconstruct_prior_state(prior_rows, now=moment)

    decisions = evaluate_shadow_candidates(
        now=moment,
        candidates=tuple(candidates),
        market_snapshots=market_snapshots,
        cost_estimates=cost_estimates,
        cohort_quality=cohort_quality,
        prior_state=prior_state,
    )

    run_at = _run_at_for(moment)
    records = [
        _build_decision_record(
            pilot=pilot, run_at=run_at, candidate=candidate, decision=decision,
            market_snapshots=market_snapshots, multi_timeframe_conflicts=multi_timeframe_conflicts,
        )
        for candidate, decision in zip(candidates, decisions)
    ]

    try:
        inserted = repo.insert_shadow_decisions(conn, records)
    except sqlite3.Error:
        logger.error("Shadow alert runtime decision write failed", exc_info=True)
        return ShadowRuntimeResult(
            ran=False, reason=RUN_REASON_WRITE_FAILED, pilot_id=pilot.pilot_id, run_at=run_at
        )

    would_emit_count = sum(1 for d in decisions if d.would_emit)
    return ShadowRuntimeResult(
        ran=True,
        reason=RUN_REASON_OK,
        pilot_id=pilot.pilot_id,
        run_at=run_at,
        candidate_count=len(candidates),
        would_emit_count=would_emit_count,
        suppressed_count=len(candidates) - would_emit_count,
        inserted_count=inserted,
    )
