"""Tests for src/apex/opportunity/shadow_alerts.py — the pure, fail-closed
shadow-alert batch evaluator. Every external fact (now, candidates, market
snapshots, costs, cohort evidence, prior state) is a synthetic fixture built
in this file; nothing here touches a real clock, database, network, or
notification transport.
"""
from __future__ import annotations

import ast
import inspect
import sys
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from apex.opportunity import shadow_alerts
from apex.opportunity.contract import CONTRACT_VERSION as OPPORTUNITY_CONTRACT_VERSION
from apex.opportunity.quality_cohorts import CohortKey
from apex.opportunity.scoring import SCORE_VERSION
from apex.opportunity.shadow_alerts import (
    MAX_MARKET_SNAPSHOT_AGE_SECONDS,
    REASON_CLUSTER_NON_REPRESENTATIVE,
    REASON_COHORT_EVIDENCE_MISSING,
    REASON_COOLDOWN_ACTIVE,
    REASON_COST_ESTIMATE_INVALID,
    REASON_COST_ESTIMATE_MISSING,
    REASON_COST_TOO_HIGH,
    REASON_DUPLICATE_OPPORTUNITY_UID_IN_BATCH,
    REASON_EMIT_ELIGIBLE,
    REASON_EVALUATION_NOT_STARTED,
    REASON_EVALUATION_WINDOW_EXPIRED,
    REASON_EVALUATION_WINDOW_UNAVAILABLE,
    REASON_INVALID_NOW_TIMESTAMP,
    REASON_INVALID_PLAN_GEOMETRY,
    REASON_LAST_SEEN_AT_INVALID,
    REASON_LAST_SEEN_IN_FUTURE,
    REASON_MARKET_BAR_TOUCHED_ENTRY,
    REASON_MARKET_PRICE_OUT_OF_RANGE,
    REASON_MARKET_SNAPSHOT_INVALID_PRICE,
    REASON_MARKET_SNAPSHOT_MISSING,
    REASON_MARKET_SNAPSHOT_NOT_CURRENT,
    REASON_MARKET_SNAPSHOT_SYMBOL_MISMATCH,
    REASON_OPPORTUNITY_ALREADY_EMITTED,
    REASON_OPPORTUNITY_NOT_ACTIVE,
    REASON_OPPORTUNITY_NOT_RESEARCH_ONLY,
    REASON_OPPOSITE_DIRECTION_CONFLICT,
    REASON_OUTCOME_AMBIGUOUS,
    REASON_OUTCOME_DATA_INCOMPLETE,
    REASON_OUTCOME_NOT_PENDING_ENTRY,
    REASON_OVERLAPS_OPEN_CLUSTER,
    REASON_PLAN_UNAVAILABLE,
    REASON_PRIOR_STATE_INVALID,
    REASON_QUIET_HOURS_ACTIVE,
    REASON_ROLLING_DAY_CAP_REACHED,
    REASON_ROLLING_HOUR_CAP_REACHED,
    REASON_STALE_OPPORTUNITY,
    REASON_TIMEZONE_CONVERSION_FAILED,
    REASON_UNSUPPORTED_OPPORTUNITY_CONTRACT_VERSION,
    REASON_UNSUPPORTED_OUTCOME_CONTRACT_VERSION,
    REASON_UNSUPPORTED_PLAN_CONTRACT_VERSION,
    REASON_UNSUPPORTED_SCORE_VERSION,
    CohortQualityEvidence,
    CostEstimate,
    MarketSnapshot,
    OpenClusterWindow,
    PriorEmission,
    PriorShadowState,
    ShadowCandidate,
    evaluate_shadow_candidates,
)
from apex.opportunity.trade_plan import TRADE_PLAN_CONTRACT_VERSION
from apex.opportunity.trade_plan_outcome import TRADE_PLAN_OUTCOME_CONTRACT_VERSION

DETROIT = ZoneInfo("America/Detroit")

# June 15 2026, 15:00 UTC -> 11:00 EDT Detroit (non-quiet, unambiguous DST).
FIXED_NOW = datetime(2026, 6, 15, 15, 0, 0, tzinfo=UTC)
FIXED_NOW_MS = int(FIXED_NOW.timestamp() * 1000)
LAST_SEEN_30S_AGO = (FIXED_NOW - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _candidate(**overrides) -> ShadowCandidate:
    base = {
        "opportunity_uid": "uid-1",
        "symbol": "BTC",
        "direction": "LONG",
        "setup_family": "SWEEP_RECLAIM",
        "primary_timeframe": "5m",
        "opportunity_status": "ACTIVE",
        "research_only": True,
        "opportunity_contract_version": OPPORTUNITY_CONTRACT_VERSION,
        "last_seen_at": LAST_SEEN_30S_AGO,
        "score_version": SCORE_VERSION,
        "plan_contract_version": TRADE_PLAN_CONTRACT_VERSION,
        "plan_availability": "AVAILABLE",
        "entry_price": 100.0,
        "stop_price": 90.0,
        "target_1r_price": 110.0,
        "target_2r_price": 120.0,
        "evaluation_not_before_ms": FIXED_NOW_MS - 2_000_000,
        "evaluation_expiry_ms": FIXED_NOW_MS + 1_000_000,
        "outcome_contract_version": TRADE_PLAN_OUTCOME_CONTRACT_VERSION,
        "outcome_state": "PENDING_ENTRY",
        "outcome_data_quality": "COMPLETE",
        "outcome_is_ambiguous": False,
    }
    base.update(overrides)
    return ShadowCandidate(**base)


def _snapshot(**overrides) -> MarketSnapshot:
    base = {
        "symbol": "BTC",
        "as_of": FIXED_NOW - timedelta(seconds=5),
        "current_price": 105.0,
        "bar_open": 104.0,
        "bar_high": 106.0,
        "bar_low": 101.0,
    }
    base.update(overrides)
    return MarketSnapshot(**base)


def _cost(**overrides) -> CostEstimate:
    base = {"fee_r": 0.02, "slippage_r": 0.02}
    base.update(overrides)
    return CostEstimate(**base)


def _cohort_evidence(**overrides) -> CohortQualityEvidence:
    base = {
        "distinct_resolved_clusters": 150,
        "ambiguous_rate": 0.02,
        "net_expectancy_r": 0.3,
        "lower_bound_95": 0.1,
    }
    base.update(overrides)
    return CohortQualityEvidence(**base)


def _run(candidates, *, now=FIXED_NOW, market_snapshots=None, cost_estimates=None,
          cohort_quality=None, prior_state=None):
    if market_snapshots is None:
        market_snapshots = {"BTC": _snapshot()}
    if cost_estimates is None:
        cost_estimates = {"BTC": _cost()}
    if cohort_quality is None:
        cohort_quality = {CohortKey("SWEEP_RECLAIM", "5m", "LONG"): _cohort_evidence()}
    if prior_state is None:
        prior_state = PriorShadowState()
    return evaluate_shadow_candidates(
        now=now,
        candidates=candidates,
        market_snapshots=market_snapshots,
        cost_estimates=cost_estimates,
        cohort_quality=cohort_quality,
        prior_state=prior_state,
    )


class TestBaselineEmits:
    def test_valid_candidate_emits_with_message(self):
        (decision,) = _run([_candidate()])
        assert decision.would_emit is True
        assert decision.reason_codes == (REASON_EMIT_ELIGIBLE,)
        assert decision.message is not None

    def test_message_contains_required_fields_and_no_sizing_language(self):
        (decision,) = _run([_candidate()])
        msg = decision.message
        assert "RESEARCH/SHADOW-ONLY" in msg
        assert "BTC" in msg and "LONG" in msg
        assert "SWEEP_RECLAIM" in msg and "5m" in msg
        assert "100" in msg and "90" in msg and "110" in msg and "120" in msg
        assert "Freshness" in msg
        for forbidden in ("position size", "leverage", "notional", "margin", "quantity", "units of"):
            assert forbidden not in msg.lower()

    def test_one_decision_per_input_candidate_in_order(self):
        candidates = [
            _candidate(opportunity_uid="uid-a", symbol="AAA"),
            _candidate(opportunity_uid="uid-b", symbol="BBB"),
        ]
        decisions = _run(
            candidates,
            market_snapshots={"AAA": _snapshot(symbol="AAA"), "BBB": _snapshot(symbol="BBB")},
            cost_estimates={"AAA": _cost(), "BBB": _cost()},
            cohort_quality={
                CohortKey("SWEEP_RECLAIM", "5m", "LONG"): _cohort_evidence(),
            },
        )
        assert [d.opportunity_uid for d in decisions] == ["uid-a", "uid-b"]


class TestContractGates:
    @pytest.mark.parametrize(
        "field,value,expected_reason",
        [
            ("opportunity_status", "EXPIRED", REASON_OPPORTUNITY_NOT_ACTIVE),
            ("research_only", False, REASON_OPPORTUNITY_NOT_RESEARCH_ONLY),
            ("opportunity_contract_version", "bogus_v0", REASON_UNSUPPORTED_OPPORTUNITY_CONTRACT_VERSION),
            ("plan_contract_version", "bogus_v0", REASON_UNSUPPORTED_PLAN_CONTRACT_VERSION),
            ("outcome_contract_version", "bogus_v0", REASON_UNSUPPORTED_OUTCOME_CONTRACT_VERSION),
            ("score_version", "bogus_v0", REASON_UNSUPPORTED_SCORE_VERSION),
            ("outcome_state", "ENTERED", REASON_OUTCOME_NOT_PENDING_ENTRY),
            ("outcome_data_quality", "INSUFFICIENT_DATA", REASON_OUTCOME_DATA_INCOMPLETE),
            ("outcome_is_ambiguous", True, REASON_OUTCOME_AMBIGUOUS),
        ],
    )
    def test_single_contract_field_failure_suppresses(self, field, value, expected_reason):
        (decision,) = _run([_candidate(**{field: value})])
        assert decision.would_emit is False
        assert expected_reason in decision.reason_codes

    def test_unavailable_plan_suppresses_with_plan_and_geometry_reasons(self):
        (decision,) = _run(
            [
                _candidate(
                    plan_availability="UNAVAILABLE",
                    entry_price=None,
                    stop_price=None,
                    target_1r_price=None,
                    target_2r_price=None,
                )
            ]
        )
        assert REASON_PLAN_UNAVAILABLE in decision.reason_codes
        assert REASON_INVALID_PLAN_GEOMETRY in decision.reason_codes

    def test_invalid_long_geometry_suppresses(self):
        (decision,) = _run([_candidate(stop_price=105.0)])  # stop above entry for LONG
        assert REASON_INVALID_PLAN_GEOMETRY in decision.reason_codes

    def test_invalid_short_geometry_suppresses(self):
        short_candidate = _candidate(
            direction="SHORT", entry_price=100.0, stop_price=90.0, target_1r_price=90.0,
            target_2r_price=80.0,
        )
        (decision,) = _run(
            [short_candidate],
            market_snapshots={"BTC": _snapshot(current_price=95.0, bar_high=96.0, bar_low=94.0)},
            cohort_quality={CohortKey("SWEEP_RECLAIM", "5m", "SHORT"): _cohort_evidence()},
        )
        assert REASON_INVALID_PLAN_GEOMETRY in decision.reason_codes

    def test_valid_short_candidate_emits(self):
        short_candidate = _candidate(
            direction="SHORT", entry_price=100.0, stop_price=110.0, target_1r_price=90.0,
            target_2r_price=80.0,
        )
        (decision,) = _run(
            [short_candidate],
            market_snapshots={"BTC": _snapshot(current_price=95.0, bar_high=99.0, bar_low=94.0)},
            cohort_quality={CohortKey("SWEEP_RECLAIM", "5m", "SHORT"): _cohort_evidence()},
        )
        assert decision.would_emit is True


class TestFreshnessAndWindow:
    def test_last_seen_at_malformed_suppresses(self):
        (decision,) = _run([_candidate(last_seen_at="not-a-timestamp")])
        assert REASON_LAST_SEEN_AT_INVALID in decision.reason_codes

    def test_last_seen_in_future_suppresses(self):
        future = (FIXED_NOW + timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        (decision,) = _run([_candidate(last_seen_at=future)])
        assert REASON_LAST_SEEN_IN_FUTURE in decision.reason_codes

    def test_age_over_120_seconds_suppresses(self):
        stale = (FIXED_NOW - timedelta(seconds=121)).strftime("%Y-%m-%dT%H:%M:%SZ")
        (decision,) = _run([_candidate(last_seen_at=stale)])
        assert REASON_STALE_OPPORTUNITY in decision.reason_codes

    def test_age_exactly_zero_and_120_are_within_bounds(self):
        exactly_now = FIXED_NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
        exactly_120 = (FIXED_NOW - timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for ts in (exactly_now, exactly_120):
            (decision,) = _run([_candidate(last_seen_at=ts)])
            assert decision.would_emit is True, ts

    def test_missing_evaluation_window_suppresses(self):
        (decision,) = _run(
            [_candidate(evaluation_not_before_ms=None, evaluation_expiry_ms=None)]
        )
        assert REASON_EVALUATION_WINDOW_UNAVAILABLE in decision.reason_codes

    def test_expired_evaluation_window_suppresses(self):
        (decision,) = _run(
            [_candidate(evaluation_expiry_ms=FIXED_NOW_MS - 1000)]
        )
        assert REASON_EVALUATION_WINDOW_EXPIRED in decision.reason_codes


class TestMarketGates:
    def test_missing_snapshot_suppresses(self):
        (decision,) = _run([_candidate()], market_snapshots={})
        assert REASON_MARKET_SNAPSHOT_MISSING in decision.reason_codes

    def test_symbol_mismatch_suppresses(self):
        (decision,) = _run([_candidate()], market_snapshots={"BTC": _snapshot(symbol="ETH")})
        assert REASON_MARKET_SNAPSHOT_SYMBOL_MISMATCH in decision.reason_codes

    def test_non_finite_price_suppresses(self):
        (decision,) = _run(
            [_candidate()], market_snapshots={"BTC": _snapshot(current_price=float("nan"))}
        )
        assert REASON_MARKET_SNAPSHOT_INVALID_PRICE in decision.reason_codes

    def test_snapshot_from_the_future_suppresses(self):
        (decision,) = _run(
            [_candidate()],
            market_snapshots={"BTC": _snapshot(as_of=FIXED_NOW + timedelta(seconds=5))},
        )
        assert REASON_MARKET_SNAPSHOT_NOT_CURRENT in decision.reason_codes

    def test_snapshot_age_exactly_at_max_passes(self):
        (decision,) = _run(
            [_candidate()],
            market_snapshots={
                "BTC": _snapshot(
                    as_of=FIXED_NOW - timedelta(seconds=MAX_MARKET_SNAPSHOT_AGE_SECONDS)
                )
            },
        )
        assert REASON_MARKET_SNAPSHOT_NOT_CURRENT not in decision.reason_codes

    def test_snapshot_older_than_max_suppresses(self):
        (decision,) = _run(
            [_candidate()],
            market_snapshots={
                "BTC": _snapshot(
                    as_of=FIXED_NOW - timedelta(seconds=MAX_MARKET_SNAPSHOT_AGE_SECONDS + 0.001)
                )
            },
        )
        assert REASON_MARKET_SNAPSHOT_NOT_CURRENT in decision.reason_codes

    def test_naive_snapshot_timestamp_suppresses(self):
        naive_as_of = datetime(2026, 6, 15, 14, 59, 55)  # no tzinfo  # noqa: DTZ001
        (decision,) = _run(
            [_candidate()], market_snapshots={"BTC": _snapshot(as_of=naive_as_of)}
        )
        assert REASON_MARKET_SNAPSHOT_NOT_CURRENT in decision.reason_codes

    def test_non_utc_aware_snapshot_timestamp_suppresses(self):
        offset_as_of = (FIXED_NOW - timedelta(seconds=5)).astimezone(DETROIT)
        (decision,) = _run(
            [_candidate()], market_snapshots={"BTC": _snapshot(as_of=offset_as_of)}
        )
        assert REASON_MARKET_SNAPSHOT_NOT_CURRENT in decision.reason_codes

    def test_long_bar_low_touching_entry_suppresses(self):
        (decision,) = _run(
            [_candidate()], market_snapshots={"BTC": _snapshot(bar_low=100.0)}
        )
        assert REASON_MARKET_BAR_TOUCHED_ENTRY in decision.reason_codes

    def test_long_price_above_target_1_suppresses(self):
        (decision,) = _run(
            [_candidate()], market_snapshots={"BTC": _snapshot(current_price=115.0)}
        )
        assert REASON_MARKET_PRICE_OUT_OF_RANGE in decision.reason_codes

    def test_long_price_below_entry_suppresses(self):
        (decision,) = _run(
            [_candidate()], market_snapshots={"BTC": _snapshot(current_price=95.0)}
        )
        assert REASON_MARKET_PRICE_OUT_OF_RANGE in decision.reason_codes

    def test_short_bar_high_touching_entry_suppresses(self):
        short_candidate = _candidate(
            direction="SHORT", entry_price=100.0, stop_price=110.0, target_1r_price=90.0,
            target_2r_price=80.0,
        )
        (decision,) = _run(
            [short_candidate],
            market_snapshots={"BTC": _snapshot(current_price=95.0, bar_high=100.0, bar_low=94.0)},
            cohort_quality={CohortKey("SWEEP_RECLAIM", "5m", "SHORT"): _cohort_evidence()},
        )
        assert REASON_MARKET_BAR_TOUCHED_ENTRY in decision.reason_codes


class TestCostGates:
    def test_missing_cost_estimate_suppresses(self):
        (decision,) = _run([_candidate()], cost_estimates={})
        assert REASON_COST_ESTIMATE_MISSING in decision.reason_codes

    def test_negative_cost_component_suppresses(self):
        (decision,) = _run([_candidate()], cost_estimates={"BTC": _cost(fee_r=-0.01)})
        assert REASON_COST_ESTIMATE_INVALID in decision.reason_codes

    def test_non_finite_cost_component_suppresses(self):
        (decision,) = _run([_candidate()], cost_estimates={"BTC": _cost(slippage_r=float("inf"))})
        assert REASON_COST_ESTIMATE_INVALID in decision.reason_codes

    def test_round_trip_cost_over_threshold_suppresses(self):
        (decision,) = _run([_candidate()], cost_estimates={"BTC": _cost(fee_r=0.06, slippage_r=0.06)})
        assert REASON_COST_TOO_HIGH in decision.reason_codes

    def test_round_trip_cost_exactly_at_threshold_passes(self):
        (decision,) = _run([_candidate()], cost_estimates={"BTC": _cost(fee_r=0.05, slippage_r=0.05)})
        assert decision.would_emit is True


class TestCohortGates:
    def test_missing_cohort_evidence_suppresses(self):
        (decision,) = _run([_candidate()], cohort_quality={})
        assert REASON_COHORT_EVIDENCE_MISSING in decision.reason_codes

    def test_cohort_below_cluster_floor_suppresses_with_cohort_prefixed_reason(self):
        (decision,) = _run(
            [_candidate()],
            cohort_quality={
                CohortKey("SWEEP_RECLAIM", "5m", "LONG"): _cohort_evidence(distinct_resolved_clusters=5)
            },
        )
        assert any(code.startswith("COHORT_") for code in decision.reason_codes)
        assert decision.would_emit is False

    def test_score_band_is_never_part_of_the_gating_key(self):
        # CohortQualityEvidence carries no score/score-band field at all —
        # this test documents that by construction (see dataclass fields).
        evidence = _cohort_evidence()
        assert not hasattr(evidence, "score_band")
        assert not hasattr(evidence, "total_score")


class TestQuietHours:
    def _now_at_detroit_local(self, dt_local: datetime) -> datetime:
        return dt_local.astimezone(UTC)

    def test_quiet_hours_suppress_all_candidates(self):
        now = self._now_at_detroit_local(datetime(2026, 6, 15, 2, 0, 0, tzinfo=DETROIT))  # 2 AM local
        (decision,) = _run([_candidate(last_seen_at=(now - timedelta(seconds=10)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"))], now=now)
        assert REASON_QUIET_HOURS_ACTIVE in decision.reason_codes

    def test_boundary_exactly_5am_passes(self):
        now = self._now_at_detroit_local(datetime(2026, 6, 15, 5, 0, 0, tzinfo=DETROIT))
        candidate = _candidate(last_seen_at=(now - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        (decision,) = _run([candidate], now=now)
        assert REASON_QUIET_HOURS_ACTIVE not in decision.reason_codes

    def test_boundary_just_before_5am_fails(self):
        now = self._now_at_detroit_local(datetime(2026, 6, 15, 4, 59, 59, tzinfo=DETROIT))
        candidate = _candidate(last_seen_at=(now - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        (decision,) = _run([candidate], now=now)
        assert REASON_QUIET_HOURS_ACTIVE in decision.reason_codes

    def test_boundary_exactly_10pm_fails(self):
        now = self._now_at_detroit_local(datetime(2026, 6, 15, 22, 0, 0, tzinfo=DETROIT))
        candidate = _candidate(last_seen_at=(now - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        (decision,) = _run([candidate], now=now)
        assert REASON_QUIET_HOURS_ACTIVE in decision.reason_codes

    def test_boundary_just_before_10pm_passes(self):
        now = self._now_at_detroit_local(datetime(2026, 6, 15, 21, 59, 59, tzinfo=DETROIT))
        candidate = _candidate(last_seen_at=(now - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        (decision,) = _run([candidate], now=now)
        assert REASON_QUIET_HOURS_ACTIVE not in decision.reason_codes

    def test_dst_offset_changes_which_utc_instants_are_quiet(self):
        # The identical UTC wall-clock hour (09:30 UTC) is quiet in EST
        # (winter, UTC-5 -> 04:30 local) but not quiet in EDT (summer,
        # UTC-4 -> 05:30 local) -- proves real zoneinfo DST conversion is
        # used, not a fixed offset.
        winter_now = datetime(2026, 1, 15, 9, 30, 0, tzinfo=UTC)
        summer_now = datetime(2026, 7, 15, 9, 30, 0, tzinfo=UTC)

        winter_candidate = _candidate(
            last_seen_at=(winter_now - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        summer_candidate = _candidate(
            last_seen_at=(summer_now - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        )

        (winter_decision,) = _run([winter_candidate], now=winter_now)
        (summer_decision,) = _run([summer_candidate], now=summer_now)

        assert REASON_QUIET_HOURS_ACTIVE in winter_decision.reason_codes
        assert REASON_QUIET_HOURS_ACTIVE not in summer_decision.reason_codes

    def test_naive_now_is_rejected_for_every_candidate(self):
        naive_now = datetime(2026, 6, 15, 15, 0, 0)  # no tzinfo  # noqa: DTZ001
        (decision,) = _run([_candidate()], now=naive_now)
        assert decision.reason_codes == (REASON_INVALID_NOW_TIMESTAMP,)

    def test_non_utc_aware_now_is_rejected(self):
        offset_now = FIXED_NOW.astimezone(DETROIT)  # aware, but not UTC offset
        (decision,) = _run([_candidate()], now=offset_now)
        assert decision.reason_codes == (REASON_INVALID_NOW_TIMESTAMP,)

    def test_zoneinfo_failure_fails_closed_never_falls_back_to_utc(self, monkeypatch):
        def _boom(_name):
            raise ZoneInfoNotFoundError("simulated missing tzdata")

        monkeypatch.setattr(shadow_alerts, "ZoneInfo", _boom)
        (decision,) = _run([_candidate()])
        assert REASON_TIMEZONE_CONVERSION_FAILED in decision.reason_codes


class TestDuplicateAndConflict:
    def test_duplicate_opportunity_uid_in_batch_suppresses_the_second_occurrence(self):
        candidates = [_candidate(opportunity_uid="uid-dup"), _candidate(opportunity_uid="uid-dup")]
        first, second = _run(candidates)
        assert first.would_emit is True
        assert second.would_emit is False
        assert REASON_DUPLICATE_OPPORTUNITY_UID_IN_BATCH in second.reason_codes

    def test_previously_emitted_uid_suppresses(self):
        prior_state = PriorShadowState(emitted_opportunity_uids=frozenset({"uid-1"}))
        (decision,) = _run([_candidate()], prior_state=prior_state)
        assert REASON_OPPORTUNITY_ALREADY_EMITTED in decision.reason_codes

    def test_opposite_direction_same_symbol_suppresses_both(self):
        # A single market snapshot that legitimately satisfies both
        # directions' geometry/price-range gates, isolating the assertion
        # to the conflict rule itself rather than an incidental market
        # failure.
        long_candidate = _candidate(
            opportunity_uid="uid-long", direction="LONG",
            entry_price=90.0, stop_price=80.0, target_1r_price=100.0, target_2r_price=110.0,
        )
        short_candidate = _candidate(
            opportunity_uid="uid-short", direction="SHORT",
            entry_price=110.0, stop_price=120.0, target_1r_price=100.0, target_2r_price=90.0,
        )
        decisions = _run(
            [long_candidate, short_candidate],
            market_snapshots={"BTC": _snapshot(current_price=100.0, bar_low=95.0, bar_high=105.0)},
            cohort_quality={
                CohortKey("SWEEP_RECLAIM", "5m", "LONG"): _cohort_evidence(),
                CohortKey("SWEEP_RECLAIM", "5m", "SHORT"): _cohort_evidence(),
            },
        )
        long_decision, short_decision = decisions
        assert long_decision.would_emit is False
        assert short_decision.would_emit is False
        assert REASON_OPPOSITE_DIRECTION_CONFLICT in long_decision.reason_codes
        assert REASON_OPPOSITE_DIRECTION_CONFLICT in short_decision.reason_codes


class TestClusterDeduplication:
    def test_overlapping_same_symbol_direction_selects_earliest_start_as_representative(self):
        early = _candidate(
            opportunity_uid="uid-early", setup_family="SWEEP_RECLAIM",
            evaluation_not_before_ms=FIXED_NOW_MS - 2_000_000,
            evaluation_expiry_ms=FIXED_NOW_MS + 1_000_000,
        )
        late = _candidate(
            opportunity_uid="uid-late", setup_family="SUPPORT_RESISTANCE_REJECTION",
            evaluation_not_before_ms=FIXED_NOW_MS - 1_000_000,
            evaluation_expiry_ms=FIXED_NOW_MS + 2_000_000,
        )
        decisions = _run(
            [late, early],  # input order deliberately reversed vs. start_ms order
            cohort_quality={
                CohortKey("SWEEP_RECLAIM", "5m", "LONG"): _cohort_evidence(),
                CohortKey("SUPPORT_RESISTANCE_REJECTION", "5m", "LONG"): _cohort_evidence(),
            },
        )
        late_decision, early_decision = decisions
        assert early_decision.would_emit is True
        assert late_decision.would_emit is False
        assert REASON_CLUSTER_NON_REPRESENTATIVE in late_decision.reason_codes

    def test_non_overlapping_future_candidate_is_suppressed_as_not_started(self):
        first = _candidate(
            opportunity_uid="uid-1", evaluation_not_before_ms=FIXED_NOW_MS - 2_000_000,
            evaluation_expiry_ms=FIXED_NOW_MS + 100_000,
        )
        second = _candidate(
            opportunity_uid="uid-2", evaluation_not_before_ms=FIXED_NOW_MS + 500_000,
            evaluation_expiry_ms=FIXED_NOW_MS + 1_500_000,
        )
        first_decision, second_decision = _run([first, second])
        assert first_decision.would_emit is True
        assert second_decision.would_emit is False
        assert second_decision.reason_codes == (REASON_EVALUATION_NOT_STARTED,)
        assert REASON_CLUSTER_NON_REPRESENTATIVE not in second_decision.reason_codes


class TestHistorySuppression:
    def test_overlapping_open_cluster_suppresses(self):
        prior_state = PriorShadowState(
            open_cluster_windows=(
                OpenClusterWindow(
                    symbol="BTC", direction="LONG",
                    start_ms=FIXED_NOW_MS - 500_000, end_ms=FIXED_NOW_MS + 500_000,
                ),
            )
        )
        (decision,) = _run([_candidate()], prior_state=prior_state)
        assert REASON_OVERLAPS_OPEN_CLUSTER in decision.reason_codes

    def test_cooldown_blocks_within_30_minutes(self):
        prior_state = PriorShadowState(
            emissions=(PriorEmission(symbol="BTC", direction="LONG", emitted_at=FIXED_NOW - timedelta(minutes=10)),)
        )
        (decision,) = _run([_candidate()], prior_state=prior_state)
        assert REASON_COOLDOWN_ACTIVE in decision.reason_codes

    def test_cooldown_expires_at_exactly_30_minutes(self):
        prior_state = PriorShadowState(
            emissions=(PriorEmission(symbol="BTC", direction="LONG", emitted_at=FIXED_NOW - timedelta(minutes=30)),)
        )
        (decision,) = _run([_candidate()], prior_state=prior_state)
        assert REASON_COOLDOWN_ACTIVE not in decision.reason_codes

    def test_rolling_hour_cap_blocks_the_fourth_emission(self):
        prior_emissions = tuple(
            PriorEmission(symbol=f"SYM{i}", direction="LONG", emitted_at=FIXED_NOW - timedelta(minutes=10))
            for i in range(3)
        )
        prior_state = PriorShadowState(emissions=prior_emissions)
        (decision,) = _run([_candidate()], prior_state=prior_state)
        assert REASON_ROLLING_HOUR_CAP_REACHED in decision.reason_codes

    def test_rolling_hour_cap_accumulates_within_the_batch(self):
        candidates = [
            _candidate(opportunity_uid=f"uid-{i}", symbol=f"SYM{i}")
            for i in range(4)
        ]
        market_snapshots = {f"SYM{i}": _snapshot(symbol=f"SYM{i}") for i in range(4)}
        cost_estimates = {f"SYM{i}": _cost() for i in range(4)}
        decisions = _run(
            candidates,
            market_snapshots=market_snapshots,
            cost_estimates=cost_estimates,
            cohort_quality={CohortKey("SWEEP_RECLAIM", "5m", "LONG"): _cohort_evidence()},
        )
        assert sum(1 for d in decisions if d.would_emit) == 3
        assert REASON_ROLLING_HOUR_CAP_REACHED in decisions[3].reason_codes

    def test_rolling_day_cap_blocks_the_eleventh_emission(self):
        prior_emissions = tuple(
            PriorEmission(symbol=f"SYM{i}", direction="LONG", emitted_at=FIXED_NOW - timedelta(hours=2))
            for i in range(10)
        )
        prior_state = PriorShadowState(emissions=prior_emissions)
        (decision,) = _run([_candidate()], prior_state=prior_state)
        assert REASON_ROLLING_DAY_CAP_REACHED in decision.reason_codes


class TestDetroitDayRollover:
    def _detroit_local(self, dt_local: datetime) -> datetime:
        return dt_local.astimezone(UTC)

    def _candidate_at(self, now: datetime) -> ShadowCandidate:
        # Build a candidate whose freshness/evaluation-window local gates
        # are satisfied relative to this test's own `now`, not the module
        # `FIXED_NOW` fixture, so a candidate can actually reach the
        # history-based day-cap stage.
        now_ms = int(now.timestamp() * 1000)
        return _candidate(
            last_seen_at=(now - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            evaluation_not_before_ms=now_ms - 2_000_000,
            evaluation_expiry_ms=now_ms + 1_000_000,
        )

    def _snapshot_at(self, now: datetime) -> MarketSnapshot:
        return _snapshot(as_of=now - timedelta(seconds=5))

    def test_emissions_just_after_local_midnight_count_toward_current_day_cap(self):
        now = self._detroit_local(datetime(2026, 6, 15, 12, 0, 0, tzinfo=DETROIT))
        just_after_midnight = self._detroit_local(datetime(2026, 6, 15, 0, 0, 1, tzinfo=DETROIT))
        prior_emissions = tuple(
            PriorEmission(symbol=f"SYM{i}", direction="LONG", emitted_at=just_after_midnight)
            for i in range(10)
        )
        prior_state = PriorShadowState(emissions=prior_emissions)
        (decision,) = _run(
            [self._candidate_at(now)],
            now=now,
            market_snapshots={"BTC": self._snapshot_at(now)},
            prior_state=prior_state,
        )
        assert REASON_ROLLING_DAY_CAP_REACHED in decision.reason_codes

    def test_emissions_just_before_local_midnight_do_not_count_toward_current_day_cap(self):
        now = self._detroit_local(datetime(2026, 6, 15, 12, 0, 0, tzinfo=DETROIT))
        just_before_midnight = self._detroit_local(datetime(2026, 6, 14, 23, 59, 59, tzinfo=DETROIT))
        prior_emissions = tuple(
            PriorEmission(symbol=f"SYM{i}", direction="LONG", emitted_at=just_before_midnight)
            for i in range(10)
        )
        prior_state = PriorShadowState(emissions=prior_emissions)
        (decision,) = _run(
            [self._candidate_at(now)],
            now=now,
            market_snapshots={"BTC": self._snapshot_at(now)},
            prior_state=prior_state,
        )
        assert decision.would_emit is True
        assert REASON_ROLLING_DAY_CAP_REACHED not in decision.reason_codes

    def test_day_cap_rollover_is_correct_across_a_dst_spring_forward_date(self):
        # 2026-03-08 is the US spring-forward date (2 AM EST -> 3 AM EDT);
        # local midnight itself is unambiguous, before the gap.
        now = self._detroit_local(datetime(2026, 3, 8, 12, 0, 0, tzinfo=DETROIT))
        just_after_midnight = self._detroit_local(datetime(2026, 3, 8, 0, 30, 0, tzinfo=DETROIT))
        just_before_midnight = self._detroit_local(datetime(2026, 3, 7, 23, 30, 0, tzinfo=DETROIT))

        same_day_emissions = tuple(
            PriorEmission(symbol=f"SYM{i}", direction="LONG", emitted_at=just_after_midnight)
            for i in range(10)
        )
        prior_day_emissions = tuple(
            PriorEmission(symbol=f"OLD{i}", direction="LONG", emitted_at=just_before_midnight)
            for i in range(10)
        )

        (same_day_decision,) = _run(
            [self._candidate_at(now)],
            now=now,
            market_snapshots={"BTC": self._snapshot_at(now)},
            prior_state=PriorShadowState(emissions=same_day_emissions),
        )
        assert REASON_ROLLING_DAY_CAP_REACHED in same_day_decision.reason_codes

        (prior_day_decision,) = _run(
            [self._candidate_at(now)],
            now=now,
            market_snapshots={"BTC": self._snapshot_at(now)},
            prior_state=PriorShadowState(emissions=prior_day_emissions),
        )
        assert prior_day_decision.would_emit is True
        assert REASON_ROLLING_DAY_CAP_REACHED not in prior_day_decision.reason_codes


class TestPriorStateValidation:
    def test_malformed_emission_symbol_fails_closed(self):
        prior_state = PriorShadowState(
            emissions=(PriorEmission(symbol="", direction="LONG", emitted_at=FIXED_NOW),)
        )
        (decision,) = _run([_candidate()], prior_state=prior_state)
        assert decision.reason_codes == (REASON_PRIOR_STATE_INVALID,)
        assert decision.would_emit is False
        assert decision.message is None
        assert decision.evidence

    def test_naive_emission_timestamp_fails_closed(self):
        naive_emitted_at = datetime(2026, 6, 15, 14, 0, 0)  # no tzinfo  # noqa: DTZ001
        prior_state = PriorShadowState(
            emissions=(PriorEmission(symbol="BTC", direction="LONG", emitted_at=naive_emitted_at),)
        )
        (decision,) = _run([_candidate()], prior_state=prior_state)
        assert decision.reason_codes == (REASON_PRIOR_STATE_INVALID,)

    def test_future_emission_timestamp_fails_closed(self):
        prior_state = PriorShadowState(
            emissions=(
                PriorEmission(
                    symbol="BTC", direction="LONG", emitted_at=FIXED_NOW + timedelta(seconds=1)
                ),
            )
        )
        (decision,) = _run([_candidate()], prior_state=prior_state)
        assert decision.reason_codes == (REASON_PRIOR_STATE_INVALID,)

    def test_invalid_emission_direction_fails_closed(self):
        prior_state = PriorShadowState(
            emissions=(PriorEmission(symbol="BTC", direction="SIDEWAYS", emitted_at=FIXED_NOW),)
        )
        (decision,) = _run([_candidate()], prior_state=prior_state)
        assert decision.reason_codes == (REASON_PRIOR_STATE_INVALID,)

    def test_malformed_open_cluster_window_bounds_fail_closed(self):
        prior_state = PriorShadowState(
            open_cluster_windows=(
                OpenClusterWindow(symbol="BTC", direction="LONG", start_ms=100.0, end_ms=200),  # type: ignore[arg-type]
            )
        )
        (decision,) = _run([_candidate()], prior_state=prior_state)
        assert decision.reason_codes == (REASON_PRIOR_STATE_INVALID,)

    def test_open_cluster_window_start_after_end_fails_closed(self):
        prior_state = PriorShadowState(
            open_cluster_windows=(
                OpenClusterWindow(symbol="BTC", direction="LONG", start_ms=200, end_ms=100),
            )
        )
        (decision,) = _run([_candidate()], prior_state=prior_state)
        assert decision.reason_codes == (REASON_PRIOR_STATE_INVALID,)

    def test_empty_emitted_uid_fails_closed(self):
        prior_state = PriorShadowState(emitted_opportunity_uids=frozenset({""}))
        (decision,) = _run([_candidate()], prior_state=prior_state)
        assert decision.reason_codes == (REASON_PRIOR_STATE_INVALID,)

    def test_invalid_prior_state_suppresses_every_candidate_in_the_batch(self):
        prior_state = PriorShadowState(
            emissions=(PriorEmission(symbol="", direction="LONG", emitted_at=FIXED_NOW),)
        )
        candidates = [
            _candidate(opportunity_uid="uid-a", symbol="AAA"),
            _candidate(opportunity_uid="uid-b", symbol="BBB"),
        ]
        decisions = _run(
            candidates,
            market_snapshots={"AAA": _snapshot(symbol="AAA"), "BBB": _snapshot(symbol="BBB")},
            cost_estimates={"AAA": _cost(), "BBB": _cost()},
            prior_state=prior_state,
        )
        assert all(d.reason_codes == (REASON_PRIOR_STATE_INVALID,) for d in decisions)

    def test_well_formed_prior_state_is_unaffected(self):
        prior_state = PriorShadowState(
            emitted_opportunity_uids=frozenset({"uid-old"}),
            open_cluster_windows=(OpenClusterWindow(symbol="ETH", direction="LONG", start_ms=0, end_ms=100),),
            emissions=(PriorEmission(symbol="ETH", direction="LONG", emitted_at=FIXED_NOW - timedelta(hours=2)),),
        )
        (decision,) = _run([_candidate()], prior_state=prior_state)
        assert REASON_PRIOR_STATE_INVALID not in decision.reason_codes
        assert decision.would_emit is True


class TestSuppressionEvidence:
    def test_evidence_dict_present_even_when_suppressed(self):
        (decision,) = _run([_candidate(opportunity_status="EXPIRED")])
        assert decision.would_emit is False
        assert "age_seconds" in decision.evidence
        assert "cohort_key" in decision.evidence
        assert decision.evidence["cohort_key"] == CohortKey("SWEEP_RECLAIM", "5m", "LONG")


class TestTransportIsolation:
    def test_module_has_no_forbidden_static_imports(self):
        source = inspect.getsource(shadow_alerts)
        tree = ast.parse(source)
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)

        forbidden_prefixes = (
            "httpx", "os", "socket", "sqlite3",
            "apex.notifications", "apex.strategy", "apex.actions", "apex.db", "apex.config",
        )
        for module in imported_modules:
            for forbidden in forbidden_prefixes:
                assert module != forbidden and not module.startswith(forbidden + "."), (
                    f"shadow_alerts.py must never import {module!r}"
                )

        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "environ"
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
            ):
                raise AssertionError("shadow_alerts.py must never reference os.environ")

    def test_evaluator_still_works_when_notifications_import_is_poisoned(self):
        class _BoomFinder:
            def find_module(self, fullname, path=None):
                if fullname == "apex.notifications" or fullname.startswith("apex.notifications."):
                    raise AssertionError(f"must never import {fullname!r}")

        finder = _BoomFinder()
        sys.meta_path.insert(0, finder)
        try:
            for name in list(sys.modules):
                if name == "apex.notifications" or name.startswith("apex.notifications."):
                    del sys.modules[name]
            (decision,) = _run([_candidate()])
            assert decision.would_emit is True
        finally:
            sys.meta_path.remove(finder)
