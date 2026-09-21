"""Tests for src/apex/opportunity/quality_uncertainty.py — cluster-aware
uncertainty and cohort qualification. Pure stdlib-only computation; no
database, network, or clock involved anywhere in this file.
"""
from __future__ import annotations

import math

from apex.opportunity.quality_uncertainty import (
    MAX_AMBIGUITY_RATE,
    MIN_RESOLVED_CLUSTERS,
    REASON_AMBIGUITY_RATE_TOO_HIGH,
    REASON_AMBIGUITY_RATE_UNAVAILABLE,
    REASON_INSUFFICIENT_RESOLVED_CLUSTERS,
    REASON_LOWER_BOUND_NOT_POSITIVE,
    REASON_LOWER_BOUND_UNAVAILABLE,
    REASON_NET_EXPECTANCY_NOT_POSITIVE,
    REASON_NET_EXPECTANCY_UNAVAILABLE,
    Z_95,
    compute_cluster_uncertainty,
    qualify_cohort,
)


class TestComputeClusterUncertainty:
    def test_no_clusters_yields_all_none(self):
        result = compute_cluster_uncertainty({})
        assert result.cluster_count == 0
        assert result.cluster_means == ()
        assert result.mean_r is None
        assert result.standard_error is None
        assert result.lower_bound_95 is None

    def test_single_cluster_has_mean_but_no_se_or_lower_bound(self):
        result = compute_cluster_uncertainty({"c1": [2.0, -1.0, 2.0]})
        assert result.cluster_count == 1
        assert result.mean_r == 1.0
        assert result.standard_error is None
        assert result.lower_bound_95 is None

    def test_two_clusters_computes_mean_se_and_lower_bound(self):
        # Cluster means: 2.0 and -1.0 -> mean 0.5, sample stdev 1.5*sqrt(2)... compute directly.
        result = compute_cluster_uncertainty({"a": [2.0], "b": [-1.0]})
        assert result.cluster_count == 2
        assert result.cluster_means == (2.0, -1.0)
        assert math.isclose(result.mean_r, 0.5)
        # sample stdev of [2.0, -1.0] with ddof=1
        expected_stdev = math.sqrt(((2.0 - 0.5) ** 2 + (-1.0 - 0.5) ** 2) / 1)
        expected_se = expected_stdev / math.sqrt(2)
        assert math.isclose(result.standard_error, expected_se)
        expected_lower = 0.5 - Z_95 * expected_se
        assert math.isclose(result.lower_bound_95, expected_lower)

    def test_cluster_means_sorted_by_cluster_id_deterministically(self):
        result = compute_cluster_uncertainty({"zzz": [1.0], "aaa": [3.0], "mmm": [2.0]})
        assert result.cluster_means == (3.0, 2.0, 1.0)

    def test_multiple_rows_in_one_cluster_average_to_one_equally_weighted_mean(self):
        # A busy cluster of 10 rows must not outweigh a cluster of 1 row.
        busy = [2.0] * 10
        quiet = [-1.0]
        result = compute_cluster_uncertainty({"busy": busy, "quiet": quiet})
        assert result.cluster_count == 2
        assert set(result.cluster_means) == {2.0, -1.0}
        assert math.isclose(result.mean_r, 0.5)

    def test_non_finite_and_malformed_values_are_dropped(self):
        result = compute_cluster_uncertainty(
            {"a": [2.0, float("nan"), float("inf")], "b": [-1.0, True]}  # bool excluded
        )
        assert result.cluster_count == 2
        assert result.cluster_means == (2.0, -1.0)

    def test_cluster_with_only_invalid_values_contributes_nothing(self):
        result = compute_cluster_uncertainty({"a": [2.0], "b": [float("nan")]})
        assert result.cluster_count == 1
        assert result.cluster_means == (2.0,)


class TestQualifyCohort:
    def _passing_kwargs(self) -> dict:
        return {
            "distinct_resolved_clusters": MIN_RESOLVED_CLUSTERS,
            "ambiguous_rate": MAX_AMBIGUITY_RATE,
            "net_mean_r": 0.01,
            "lower_bound_95": 0.01,
        }

    def test_all_rules_pass_at_exact_boundaries(self):
        result = qualify_cohort(**self._passing_kwargs())
        assert result.qualifies is True
        assert result.reasons == ()

    def test_insufficient_clusters_fails(self):
        kwargs = self._passing_kwargs()
        kwargs["distinct_resolved_clusters"] = MIN_RESOLVED_CLUSTERS - 1
        result = qualify_cohort(**kwargs)
        assert result.qualifies is False
        assert REASON_INSUFFICIENT_RESOLVED_CLUSTERS in result.reasons

    def test_ambiguity_rate_unavailable_fails(self):
        kwargs = self._passing_kwargs()
        kwargs["ambiguous_rate"] = None
        result = qualify_cohort(**kwargs)
        assert REASON_AMBIGUITY_RATE_UNAVAILABLE in result.reasons

    def test_ambiguity_rate_just_over_threshold_fails(self):
        kwargs = self._passing_kwargs()
        kwargs["ambiguous_rate"] = MAX_AMBIGUITY_RATE + 0.0001
        result = qualify_cohort(**kwargs)
        assert REASON_AMBIGUITY_RATE_TOO_HIGH in result.reasons

    def test_net_expectancy_unavailable_fails(self):
        kwargs = self._passing_kwargs()
        kwargs["net_mean_r"] = None
        result = qualify_cohort(**kwargs)
        assert REASON_NET_EXPECTANCY_UNAVAILABLE in result.reasons

    def test_net_expectancy_exactly_zero_fails(self):
        kwargs = self._passing_kwargs()
        kwargs["net_mean_r"] = 0.0
        result = qualify_cohort(**kwargs)
        assert REASON_NET_EXPECTANCY_NOT_POSITIVE in result.reasons

    def test_net_expectancy_negative_fails(self):
        kwargs = self._passing_kwargs()
        kwargs["net_mean_r"] = -0.01
        result = qualify_cohort(**kwargs)
        assert REASON_NET_EXPECTANCY_NOT_POSITIVE in result.reasons

    def test_lower_bound_unavailable_fails(self):
        kwargs = self._passing_kwargs()
        kwargs["lower_bound_95"] = None
        result = qualify_cohort(**kwargs)
        assert REASON_LOWER_BOUND_UNAVAILABLE in result.reasons

    def test_lower_bound_exactly_zero_fails(self):
        kwargs = self._passing_kwargs()
        kwargs["lower_bound_95"] = 0.0
        result = qualify_cohort(**kwargs)
        assert REASON_LOWER_BOUND_NOT_POSITIVE in result.reasons

    def test_all_reasons_accumulate_simultaneously(self):
        result = qualify_cohort(
            distinct_resolved_clusters=0,
            ambiguous_rate=None,
            net_mean_r=None,
            lower_bound_95=None,
        )
        assert result.qualifies is False
        assert result.reasons == (
            REASON_INSUFFICIENT_RESOLVED_CLUSTERS,
            REASON_AMBIGUITY_RATE_UNAVAILABLE,
            REASON_NET_EXPECTANCY_UNAVAILABLE,
            REASON_LOWER_BOUND_UNAVAILABLE,
        )
