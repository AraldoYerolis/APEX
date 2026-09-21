"""Tests for src/apex/opportunity/quality_cohorts.py — pure joined-evidence
cohort analysis: score bands, R mapping, rates/denominators, deterministic
overlap clustering, and full cohort-report construction (including
cost-adjustment and the qualification floor). No database, network, or
clock anywhere in this file — every `QualityRow` is a synthetic fixture.
"""
from __future__ import annotations

import math

from apex.opportunity.quality_cohorts import (
    SCORE_BAND_ORDER,
    UNSCORED_BAND,
    CohortKey,
    OverlapInterval,
    QualityRow,
    build_overlap_clusters,
    build_quality_cohort_report,
    clusters_for_rows,
    compute_outcome_rates,
    gross_r_for_state,
    net_r_for_gross,
    score_band,
)
from apex.opportunity.quality_uncertainty import MIN_RESOLVED_CLUSTERS


def _row(
    uid: str,
    *,
    symbol: str = "BTC",
    direction: str = "LONG",
    setup_family: str = "SWEEP_RECLAIM",
    primary_timeframe: str = "5m",
    total_score=None,
    score_version=None,
    plan_availability="AVAILABLE",
    not_before_ms=0,
    expiry_ms=1000,
    outcome_state=None,
) -> QualityRow:
    return QualityRow(
        opportunity_uid=uid,
        symbol=symbol,
        direction=direction,
        setup_family=setup_family,
        primary_timeframe=primary_timeframe,
        total_score=total_score,
        score_version=score_version,
        plan_availability=plan_availability,
        evaluation_not_before_ms=not_before_ms,
        evaluation_expiry_ms=expiry_ms,
        outcome_state=outcome_state,
    )


# ---------------------------------------------------------------------------
# Score bands
# ---------------------------------------------------------------------------


class TestScoreBand:
    def test_none_score_is_unscored(self):
        assert score_band(None) == UNSCORED_BAND

    def test_non_numeric_score_is_unscored(self):
        assert score_band("not-a-number") == UNSCORED_BAND  # type: ignore[arg-type]
        assert score_band(True) == UNSCORED_BAND  # bool excluded

    def test_boundaries_are_half_open_except_the_top(self):
        assert score_band(0.0) == "VERY_LOW_0_20"
        assert score_band(19.999) == "VERY_LOW_0_20"
        assert score_band(20.0) == "LOW_20_40"
        assert score_band(39.999) == "LOW_20_40"
        assert score_band(40.0) == "NEUTRAL_40_60"
        assert score_band(59.999) == "NEUTRAL_40_60"
        assert score_band(60.0) == "HIGH_60_80"
        assert score_band(79.999) == "HIGH_60_80"
        assert score_band(80.0) == "VERY_HIGH_80_100"
        assert score_band(100.0) == "VERY_HIGH_80_100"

    def test_out_of_range_scores_are_clamped_not_rejected(self):
        assert score_band(-5.0) == "VERY_LOW_0_20"
        assert score_band(150.0) == "VERY_HIGH_80_100"

    def test_score_band_order_ends_with_unscored(self):
        assert SCORE_BAND_ORDER[-1] == UNSCORED_BAND
        assert len(SCORE_BAND_ORDER) == 6


# ---------------------------------------------------------------------------
# R mapping
# ---------------------------------------------------------------------------


class TestGrossRForState:
    def test_hit_2r_maps_to_plus_two(self):
        assert gross_r_for_state("HIT_2R") == 2.0

    def test_stopped_maps_to_minus_one(self):
        assert gross_r_for_state("STOPPED") == -1.0

    def test_every_other_state_and_none_map_to_none(self):
        for state in (
            "HIT_1R",
            "AMBIGUOUS",
            "INSUFFICIENT_DATA",
            "NOT_EVALUABLE",
            "PENDING_ENTRY",
            "ENTERED",
            "EXPIRED_UNENTERED",
            "EXPIRED_OPEN",
            None,
            "SOMETHING_UNKNOWN",
        ):
            assert gross_r_for_state(state) is None


class TestNetRForGross:
    def test_unresolved_gross_stays_none(self):
        assert net_r_for_gross(None, 0.05) is None

    def test_missing_cost_yields_none(self):
        assert net_r_for_gross(2.0, None) is None

    def test_negative_cost_yields_none(self):
        assert net_r_for_gross(2.0, -0.01) is None

    def test_non_finite_cost_yields_none(self):
        assert net_r_for_gross(2.0, float("nan")) is None
        assert net_r_for_gross(2.0, float("inf")) is None

    def test_valid_cost_is_subtracted_regardless_of_win_or_loss(self):
        assert net_r_for_gross(2.0, 0.05) == 1.95
        assert net_r_for_gross(-1.0, 0.05) == -1.05

    def test_zero_cost_is_valid(self):
        assert net_r_for_gross(2.0, 0.0) == 2.0


# ---------------------------------------------------------------------------
# Rates and explicit denominators
# ---------------------------------------------------------------------------


class TestComputeOutcomeRates:
    def test_denominators_are_always_the_full_sample(self):
        rows = [
            _row("u1", outcome_state="HIT_2R"),
            _row("u2", outcome_state="STOPPED"),
            _row("u3", outcome_state="AMBIGUOUS"),
            _row("u4", outcome_state="INSUFFICIENT_DATA"),
            _row("u5", outcome_state="EXPIRED_UNENTERED"),
            _row("u6", outcome_state="EXPIRED_OPEN"),
            _row("u7", outcome_state="HIT_1R"),
            _row("u8", outcome_state="PENDING_ENTRY"),
            _row("u9", outcome_state=None, plan_availability=None),
        ]
        rates = compute_outcome_rates(rows)
        assert rates.total_n == 9
        assert rates.clear_resolved_n == 2
        assert rates.ambiguous_n == 1
        assert rates.ambiguous_rate == 1 / 9
        assert rates.incomplete_n == 1
        assert rates.incomplete_rate == 1 / 9
        assert rates.expired_n == 2
        assert rates.expired_rate == 2 / 9
        assert rates.other_n == 3  # HIT_1R, PENDING_ENTRY, and the missing-outcome row

    def test_empty_sample_has_none_rates_not_zero_division(self):
        rates = compute_outcome_rates([])
        assert rates.total_n == 0
        assert rates.ambiguous_rate is None
        assert rates.incomplete_rate is None
        assert rates.expired_rate is None


# ---------------------------------------------------------------------------
# Deterministic overlap clustering
# ---------------------------------------------------------------------------


class TestBuildOverlapClusters:
    def test_disjoint_intervals_are_separate_clusters(self):
        intervals = [
            OverlapInterval("a", "BTC", "LONG", 0, 100),
            OverlapInterval("b", "BTC", "LONG", 200, 300),
        ]
        clusters = build_overlap_clusters(intervals)
        assert len(clusters) == 2
        assert {c.member_uids for c in clusters} == {("a",), ("b",)}

    def test_directly_overlapping_intervals_merge(self):
        intervals = [
            OverlapInterval("a", "BTC", "LONG", 0, 100),
            OverlapInterval("b", "BTC", "LONG", 50, 150),
        ]
        clusters = build_overlap_clusters(intervals)
        assert len(clusters) == 1
        assert clusters[0].member_uids == ("a", "b")

    def test_transitive_overlap_via_bridging_interval(self):
        # a=[0,50], b=[40,60] (overlaps a), c=[55,100] (overlaps b, NOT a directly)
        intervals = [
            OverlapInterval("a", "BTC", "LONG", 0, 50),
            OverlapInterval("b", "BTC", "LONG", 40, 60),
            OverlapInterval("c", "BTC", "LONG", 55, 100),
        ]
        clusters = build_overlap_clusters(intervals)
        assert len(clusters) == 1
        assert clusters[0].member_uids == ("a", "b", "c")

    def test_touching_boundary_counts_as_overlap(self):
        intervals = [
            OverlapInterval("a", "BTC", "LONG", 0, 50),
            OverlapInterval("b", "BTC", "LONG", 50, 100),
        ]
        clusters = build_overlap_clusters(intervals)
        assert len(clusters) == 1

    def test_different_symbol_or_direction_never_clusters(self):
        intervals = [
            OverlapInterval("a", "BTC", "LONG", 0, 100),
            OverlapInterval("b", "ETH", "LONG", 0, 100),
            OverlapInterval("c", "BTC", "SHORT", 0, 100),
        ]
        clusters = build_overlap_clusters(intervals)
        assert len(clusters) == 3

    def test_cluster_identity_is_stable_regardless_of_input_order(self):
        intervals_a = [
            OverlapInterval("a", "BTC", "LONG", 0, 50),
            OverlapInterval("b", "BTC", "LONG", 40, 60),
        ]
        intervals_b = list(reversed(intervals_a))
        clusters_a = build_overlap_clusters(intervals_a)
        clusters_b = build_overlap_clusters(intervals_b)
        assert clusters_a == clusters_b

    def test_malformed_interval_becomes_its_own_singleton_cluster(self):
        intervals = [OverlapInterval("a", "BTC", "LONG", 100, 50)]  # end < start
        clusters = build_overlap_clusters(intervals)
        assert len(clusters) == 1
        assert clusters[0].member_uids == ("a",)

    def test_malformed_interval_never_merges_with_same_start_well_formed_interval(self):
        intervals = [
            OverlapInterval("a", "BTC", "LONG", 100, 50),  # malformed: end < start
            OverlapInterval("b", "BTC", "LONG", 100, 200),  # well-formed, same start_ms
        ]
        clusters = build_overlap_clusters(intervals)
        assert len(clusters) == 2
        assert {c.member_uids for c in clusters} == {("a",), ("b",)}

    def test_clusters_for_rows_excludes_rows_without_a_window(self):
        rows = [
            _row("u1", plan_availability="AVAILABLE", not_before_ms=0, expiry_ms=100),
            _row("u2", plan_availability="UNAVAILABLE", not_before_ms=None, expiry_ms=None),
            _row("u3", plan_availability=None, not_before_ms=None, expiry_ms=None),
        ]
        mapping = clusters_for_rows(rows)
        assert "u1" in mapping
        assert "u2" not in mapping
        assert "u3" not in mapping


# ---------------------------------------------------------------------------
# Full cohort-report construction
# ---------------------------------------------------------------------------


class TestBuildQualityCohortReport:
    def test_dimensions_partition_rows_correctly(self):
        rows = [
            _row("u1", setup_family="SWEEP_RECLAIM", primary_timeframe="5m", direction="LONG",
                 symbol="BTC", outcome_state="HIT_2R", not_before_ms=0, expiry_ms=100),
            _row("u2", setup_family="VOLATILITY_COMPRESSION", primary_timeframe="3m", direction="SHORT",
                 symbol="ETH", outcome_state="STOPPED", not_before_ms=200, expiry_ms=300),
        ]
        report = build_quality_cohort_report(rows, cost_r=0.0)
        assert report.overall.total_n == 2
        assert set(report.by_setup_family) == {"SWEEP_RECLAIM", "VOLATILITY_COMPRESSION"}
        assert set(report.by_timeframe) == {"5m", "3m"}
        assert set(report.by_direction) == {"LONG", "SHORT"}
        assert set(report.by_symbol) == {"BTC", "ETH"}
        assert report.by_setup_family["SWEEP_RECLAIM"].total_n == 1
        exact_key = CohortKey("SWEEP_RECLAIM", "5m", "LONG")
        assert exact_key in report.by_exact_cohort
        assert report.by_exact_cohort[exact_key].total_n == 1

    def test_missing_cost_leaves_net_metrics_unavailable_but_gross_visible(self):
        rows = [_row("u1", outcome_state="HIT_2R", not_before_ms=0, expiry_ms=100)]
        report = build_quality_cohort_report(rows, cost_r=None)
        assert report.overall.gross_mean_r == 2.0
        assert report.overall.net_mean_r is None
        assert report.overall.cost_r is None

    def test_invalid_cost_also_leaves_net_metrics_unavailable(self):
        rows = [_row("u1", outcome_state="HIT_2R", not_before_ms=0, expiry_ms=100)]
        report = build_quality_cohort_report(rows, cost_r=-0.5)
        assert report.overall.net_mean_r is None
        assert report.overall.cost_r is None

    def test_cost_adjusted_net_mean_r_subtracts_cost_from_every_resolved_row(self):
        rows = [
            _row("u1", outcome_state="HIT_2R", not_before_ms=0, expiry_ms=100),
            _row("u2", outcome_state="STOPPED", not_before_ms=200, expiry_ms=300),
        ]
        report = build_quality_cohort_report(rows, cost_r=0.1)
        assert report.overall.gross_mean_r == 0.5  # (2.0 + -1.0) / 2
        assert math.isclose(report.overall.net_mean_r, 0.4)  # (1.9 + -1.1) / 2

    def test_cohort_below_cluster_floor_never_qualifies(self):
        rows = [
            _row(f"u{i}", outcome_state="HIT_2R", not_before_ms=i * 1000, expiry_ms=i * 1000 + 100)
            for i in range(5)
        ]
        report = build_quality_cohort_report(rows, cost_r=0.0)
        assert report.overall.qualification.qualifies is False

    def test_cohort_meeting_every_threshold_qualifies(self):
        # 100 distinct non-overlapping clusters, all HIT_2R, zero ambiguity,
        # zero cost -> positive net expectancy, positive lower bound.
        rows = [
            _row(f"u{i}", outcome_state="HIT_2R", not_before_ms=i * 1000, expiry_ms=i * 1000 + 100)
            for i in range(MIN_RESOLVED_CLUSTERS)
        ]
        report = build_quality_cohort_report(rows, cost_r=0.0)
        assert report.overall.distinct_resolved_clusters == MIN_RESOLVED_CLUSTERS
        assert report.overall.qualification.qualifies is True
        assert report.overall.qualification.reasons == ()

    def test_overlapping_rows_count_as_one_resolved_cluster_for_qualification(self):
        # Two rows sharing one overlapping window must not double-count as
        # two distinct resolved clusters.
        rows = [
            _row("u1", outcome_state="HIT_2R", not_before_ms=0, expiry_ms=100),
            _row("u2", outcome_state="HIT_2R", not_before_ms=50, expiry_ms=150),
        ]
        report = build_quality_cohort_report(rows, cost_r=0.0)
        assert report.overall.distinct_resolved_clusters == 1

    def test_high_ambiguity_rate_blocks_qualification_even_with_enough_clusters(self):
        resolved = [
            _row(f"u{i}", outcome_state="HIT_2R", not_before_ms=i * 1000, expiry_ms=i * 1000 + 100)
            for i in range(MIN_RESOLVED_CLUSTERS)
        ]
        ambiguous = [
            _row(f"amb{i}", outcome_state="AMBIGUOUS", not_before_ms=i * 1000, expiry_ms=i * 1000 + 100)
            for i in range(20)  # 20 / 120 > 10%
        ]
        report = build_quality_cohort_report(resolved + ambiguous, cost_r=0.0)
        assert report.overall.qualification.qualifies is False
        from apex.opportunity.quality_uncertainty import REASON_AMBIGUITY_RATE_TOO_HIGH

        assert REASON_AMBIGUITY_RATE_TOO_HIGH in report.overall.qualification.reasons

    def test_resolved_row_without_cluster_mapping_fails_net_closed(self):
        # A clear-resolved row with no evaluation window (so no overlap
        # cluster) must never sneak into net_mean_r or cluster uncertainty
        # via a mismatched row set — gross R stays descriptive/visible.
        rows = [
            _row(
                "u1",
                outcome_state="HIT_2R",
                plan_availability="UNAVAILABLE",
                not_before_ms=None,
                expiry_ms=None,
            ),
        ]
        report = build_quality_cohort_report(rows, cost_r=0.0)
        assert report.overall.gross_mean_r == 2.0
        assert report.overall.net_mean_r is None
        assert report.overall.distinct_resolved_clusters == 0
        assert report.overall.uncertainty.cluster_means == ()
        assert report.overall.qualification.qualifies is False

    def test_cluster_dimension_groups_rows_sharing_a_cluster_id(self):
        rows = [
            _row("u1", outcome_state="HIT_2R", not_before_ms=0, expiry_ms=100),
            _row("u2", outcome_state="STOPPED", not_before_ms=50, expiry_ms=150),
        ]
        report = build_quality_cohort_report(rows, cost_r=0.0)
        assert len(report.by_cluster) == 1
        (summary,) = report.by_cluster.values()
        assert summary.total_n == 2
