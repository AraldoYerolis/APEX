"""Cluster-aware uncertainty and qualification — Opportunity Quality Report
and Shadow Alerts v0.1.

Pure, stdlib-only statistics over already-clustered net-R outcomes. Never
reads a clock, config, database, filesystem, or network client, and never
imports anything outside the Python standard library — both
`opportunity/quality_cohorts.py` (the report's cohort builder) and
`opportunity/shadow_alerts.py` (the live shadow evaluator) depend on this
module so the same qualification thresholds are the single source of truth
in both places.

Cluster-aware mean/SE
----------------------
A naive per-row standard error overstates confidence when many rows in a
cohort come from the same overlapping-signal cluster (the same underlying
market event re-detected/re-confirmed, or several correlated setups on the
same symbol at the same time). To correct for this, every clear-resolved
outcome is first reduced to one equally weighted mean net-R value per
overlap cluster (see `quality_cohorts.build_overlap_clusters`), and the
mean/standard-error/lower-bound are computed across those cluster means,
never across the raw per-row outcomes directly.

Qualification
--------------
A cohort "qualifies" for net-quality purposes only when all four rules in
`qualify_cohort` pass. This is a fixed, non-negotiable floor: it is never
weakened by a caller and it never depends on any numeric score value or
score band (see `quality_cohorts.py`'s score-band docstring) — score bands
must never become a qualification or alert threshold.
"""
from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

UNCERTAINTY_VERSION = "quality_uncertainty_v0_1"

# Cohort qualification floor (see module docstring). Fixed constants, never
# parameterized by a caller.
MIN_RESOLVED_CLUSTERS = 100
MAX_AMBIGUITY_RATE = 0.10
Z_95 = 1.96

REASON_INSUFFICIENT_RESOLVED_CLUSTERS = "INSUFFICIENT_RESOLVED_CLUSTERS"
REASON_AMBIGUITY_RATE_UNAVAILABLE = "AMBIGUITY_RATE_UNAVAILABLE"
REASON_AMBIGUITY_RATE_TOO_HIGH = "AMBIGUITY_RATE_TOO_HIGH"
REASON_NET_EXPECTANCY_UNAVAILABLE = "NET_EXPECTANCY_UNAVAILABLE"
REASON_NET_EXPECTANCY_NOT_POSITIVE = "NET_EXPECTANCY_NOT_POSITIVE"
REASON_LOWER_BOUND_UNAVAILABLE = "LOWER_BOUND_UNAVAILABLE"
REASON_LOWER_BOUND_NOT_POSITIVE = "LOWER_BOUND_NOT_POSITIVE"


def _is_finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass(frozen=True)
class UncertaintyResult:
    """Cluster-aware uncertainty for one cohort's clear-resolved net-R
    outcomes. `cluster_means` is sorted by cluster_id for determinism.
    `lower_bound_95` is `mean_r - Z_95 * standard_error`, or None when fewer
    than two resolved clusters exist (an SE cannot be estimated from one
    point).
    """

    uncertainty_version: str
    cluster_count: int
    cluster_means: tuple[float, ...]
    mean_r: float | None
    standard_error: float | None
    lower_bound_95: float | None


def compute_cluster_uncertainty(
    cluster_net_r_values: Mapping[str, Sequence[float]],
) -> UncertaintyResult:
    """Pure. `cluster_net_r_values` maps cluster_id -> the clear-resolved
    net-R values of every row in that cluster (already cost-adjusted by the
    caller). Each cluster contributes exactly one equally weighted mean,
    regardless of how many rows it contains, so a single busy cluster never
    outweighs many independent ones. Non-finite/malformed per-row values are
    dropped defensively rather than raising; a cluster left with zero valid
    values contributes no mean at all.
    """
    means: list[float] = []
    for cluster_id in sorted(cluster_net_r_values):
        raw_values = cluster_net_r_values[cluster_id]
        valid_values = [float(v) for v in raw_values if _is_finite_number(v)]
        if not valid_values:
            continue
        means.append(statistics.mean(valid_values))

    n = len(means)
    if n == 0:
        return UncertaintyResult(
            uncertainty_version=UNCERTAINTY_VERSION,
            cluster_count=0,
            cluster_means=(),
            mean_r=None,
            standard_error=None,
            lower_bound_95=None,
        )

    mean_r = statistics.mean(means)
    if n < 2:
        return UncertaintyResult(
            uncertainty_version=UNCERTAINTY_VERSION,
            cluster_count=n,
            cluster_means=tuple(means),
            mean_r=mean_r,
            standard_error=None,
            lower_bound_95=None,
        )

    standard_deviation = statistics.stdev(means)
    standard_error = standard_deviation / math.sqrt(n)
    lower_bound_95 = mean_r - Z_95 * standard_error
    return UncertaintyResult(
        uncertainty_version=UNCERTAINTY_VERSION,
        cluster_count=n,
        cluster_means=tuple(means),
        mean_r=mean_r,
        standard_error=standard_error,
        lower_bound_95=lower_bound_95,
    )


@dataclass(frozen=True)
class QualificationResult:
    """Whether a cohort meets the net-quality qualification floor, and the
    deterministic, order-stable set of reasons it does not (empty when it
    qualifies).
    """

    qualifies: bool
    reasons: tuple[str, ...]


def qualify_cohort(
    *,
    distinct_resolved_clusters: int,
    ambiguous_rate: float | None,
    net_mean_r: float | None,
    lower_bound_95: float | None,
) -> QualificationResult:
    """Pure. Every input is evidence already computed by the caller (see
    `quality_cohorts.py`'s cohort summaries) — this function only applies
    the fixed floor from the module docstring. `net_mean_r` must already be
    cost-adjusted; a cohort with a missing/invalid cost never reaches here
    with a non-None `net_mean_r` (see `quality_cohorts.py`).
    """
    reasons: list[str] = []

    if distinct_resolved_clusters < MIN_RESOLVED_CLUSTERS:
        reasons.append(REASON_INSUFFICIENT_RESOLVED_CLUSTERS)

    if ambiguous_rate is None:
        reasons.append(REASON_AMBIGUITY_RATE_UNAVAILABLE)
    elif ambiguous_rate > MAX_AMBIGUITY_RATE:
        reasons.append(REASON_AMBIGUITY_RATE_TOO_HIGH)

    if net_mean_r is None:
        reasons.append(REASON_NET_EXPECTANCY_UNAVAILABLE)
    elif not (net_mean_r > 0):
        reasons.append(REASON_NET_EXPECTANCY_NOT_POSITIVE)

    if lower_bound_95 is None:
        reasons.append(REASON_LOWER_BOUND_UNAVAILABLE)
    elif not (lower_bound_95 > 0):
        reasons.append(REASON_LOWER_BOUND_NOT_POSITIVE)

    return QualificationResult(qualifies=(len(reasons) == 0), reasons=tuple(reasons))
