"""Opportunity Quality Report and Shadow Alerts v0.1 — shared, pure cohort
analysis.

Joins the read-only evidence already produced by the existing opportunity
pipeline (`opportunity/contract.py`, `opportunity/trade_plan.py`,
`opportunity/trade_plan_outcome.py`, `opportunity/scoring.py`) into one
typed `QualityRow` per opportunity, then builds descriptive cohort summaries
from it. This module never opens a database connection, reads a clock, or
imports anything from `apex.notifications` — row loading from SQLite is the
report script's job (`scripts/report_opportunity_quality.py`); this module
only ever receives already-loaded `QualityRow` objects.

Score bands are descriptive only
----------------------------------
`score_band()` buckets a row's already-computed `total_score` into a fixed,
versioned, human-readable label for reporting only. Score bands are never
used anywhere in this module (or in `quality_uncertainty.py` or
`shadow_alerts.py`) as a qualification or alert threshold — a cohort's
resolved-cluster count, ambiguity rate, net expectancy, and lower bound are
the only qualification inputs (see `quality_uncertainty.qualify_cohort`).

R mapping is deliberately narrow
-----------------------------------
Only a clear resolved `HIT_2R` outcome maps to `+2R` and a clear resolved
`STOPPED` outcome maps to `-1R` (see `gross_r_for_state`). `HIT_1R`,
`AMBIGUOUS`, `INSUFFICIENT_DATA`, `NOT_EVALUABLE`, `PENDING_ENTRY`,
`ENTERED`, `EXPIRED_UNENTERED`, and `EXPIRED_OPEN` are never coerced into a
win or a loss — they fall out of every R-based aggregate and are instead
surfaced through `OutcomeRates`' separate ambiguous/incomplete/expired
counts, each with its own explicit denominator (`total_n`).

Overlapping-signal clustering
--------------------------------
`build_overlap_clusters` builds deterministic transitive-overlap clusters
of same-symbol/same-direction evaluation windows. It is shared, unmodified,
by `shadow_alerts.py`'s own candidate-deduplication clustering so both
modules assign the same cluster identity/order to the same underlying
interval data (see that module's docstring).

The "exact quality cohort" used for shadow-alert qualification
------------------------------------------------------------------
`CohortKey` (setup_family, primary_timeframe, direction) is the one cohort
dimension `shadow_alerts.py` gates on — see its own module docstring. Score
band, symbol, and overlap cluster are additional report-only breakdowns
(`build_quality_cohort_report`'s other dimensions); they are never used to
gate a shadow alert.
"""
from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import NamedTuple

from apex.opportunity import quality_uncertainty as uncertainty

SCORE_BAND_VERSION = "quality_score_band_v0_1"

UNSCORED_BAND = "UNSCORED"

# Fixed, versioned, descriptive score bands. Order matters: the first band
# whose [lo, hi) contains the score wins, except the final band, which is
# closed on both ends so a score of exactly 100.0 is still classified.
_SCORE_BANDS: tuple[tuple[float, float, str], ...] = (
    (0.0, 20.0, "VERY_LOW_0_20"),
    (20.0, 40.0, "LOW_20_40"),
    (40.0, 60.0, "NEUTRAL_40_60"),
    (60.0, 80.0, "HIGH_60_80"),
    (80.0, 100.0, "VERY_HIGH_80_100"),
)

# Canonical display order for score-band cohorts (report rendering only) —
# lowest to highest, with UNSCORED last regardless of value.
SCORE_BAND_ORDER: tuple[str, ...] = tuple(label for _, _, label in _SCORE_BANDS) + (UNSCORED_BAND,)

# Only these two outcome states are ever mapped to a gross R value (see
# module docstring). Every other state, including a missing outcome
# altogether, maps to None.
_CLEAR_RESOLVED_GROSS_R: dict[str, float] = {"HIT_2R": 2.0, "STOPPED": -1.0}

_AMBIGUOUS_STATES = frozenset({"AMBIGUOUS"})
_INCOMPLETE_STATES = frozenset({"INSUFFICIENT_DATA"})
_EXPIRED_STATES = frozenset({"EXPIRED_UNENTERED", "EXPIRED_OPEN"})


def _is_finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def score_band(total_score: float | None) -> str:
    """Pure. Descriptive-only classification of an already-computed
    `total_score` (see `opportunity/scoring.py`). Never gates anything —
    see module docstring.
    """
    if not _is_finite_number(total_score):
        return UNSCORED_BAND
    value = float(total_score)  # type: ignore[arg-type]
    clamped = min(100.0, max(0.0, value))
    for lo, hi, label in _SCORE_BANDS:
        if lo <= clamped < hi:
            return label
    return _SCORE_BANDS[-1][2]  # clamped == 100.0


def gross_r_for_state(outcome_state: str | None) -> float | None:
    """Pure. See module docstring's "R mapping is deliberately narrow"."""
    if outcome_state is None:
        return None
    return _CLEAR_RESOLVED_GROSS_R.get(outcome_state)


def net_r_for_gross(gross_r: float | None, cost_r: float | None) -> float | None:
    """Pure. `cost_r` is an explicit, caller-supplied round-trip cost in R,
    subtracted from `gross_r` regardless of win/loss. Returns None (never
    invents a cost) when `gross_r` is unresolved or `cost_r` is missing,
    non-finite, or negative — this is what makes a missing/invalid cost
    input "remain visible" downstream (see module docstring and
    `quality_uncertainty.qualify_cohort`).
    """
    if gross_r is None:
        return None
    if not _is_finite_number(cost_r) or cost_r < 0:  # type: ignore[operator]
        return None
    return gross_r - cost_r  # type: ignore[operator]


class CohortKey(NamedTuple):
    """The "exact quality cohort" — see module docstring."""

    setup_family: str
    primary_timeframe: str
    direction: str


@dataclass(frozen=True)
class QualityRow:
    """One joined opportunity + trade-plan + outcome evidence record. Built
    by the report script's row loader from `opportunity_observations` LEFT
    JOIN `opportunity_trade_plans` LEFT JOIN
    `opportunity_trade_plan_outcomes` — a plan/outcome may be absent for an
    opportunity that predates Trade plans and outcome evidence v0.1 (see
    `opportunity/trade_plan.py`'s module docstring), which this dataclass
    represents as `None` rather than inventing a state.
    """

    opportunity_uid: str
    symbol: str
    direction: str
    setup_family: str
    primary_timeframe: str
    total_score: float | None
    score_version: str | None
    plan_availability: str | None
    evaluation_not_before_ms: int | None
    evaluation_expiry_ms: int | None
    outcome_state: str | None

    @property
    def cohort_key(self) -> CohortKey:
        return CohortKey(self.setup_family, self.primary_timeframe, self.direction)

    @property
    def score_band(self) -> str:
        return score_band(self.total_score)

    @property
    def has_overlap_window(self) -> bool:
        return (
            self.plan_availability == "AVAILABLE"
            and self.evaluation_not_before_ms is not None
            and self.evaluation_expiry_ms is not None
            and self.evaluation_expiry_ms >= self.evaluation_not_before_ms
        )


# ---------------------------------------------------------------------------
# Deterministic overlapping-signal clustering (shared with shadow_alerts.py)
# ---------------------------------------------------------------------------


class OverlapInterval(NamedTuple):
    """One symbol/direction-scoped evaluation window to cluster. `uid` is
    the caller's own unique identity for the interval (an opportunity_uid
    in both current callers).
    """

    uid: str
    symbol: str
    direction: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class OverlapCluster:
    """One deterministic connected component of transitively overlapping
    same-symbol/same-direction intervals. `member_uids` is sorted for
    determinism. `cluster_id` is derived from the earliest-starting member
    (ties broken by uid), so it is stable across repeated calls on the same
    input regardless of input ordering.
    """

    cluster_id: str
    symbol: str
    direction: str
    member_uids: tuple[str, ...]


def build_overlap_clusters(intervals: Sequence[OverlapInterval]) -> tuple[OverlapCluster, ...]:
    """Pure, deterministic. Builds transitive-overlap connected components
    per (symbol, direction) group. Two intervals in the same group overlap
    when their closed ranges intersect (`start_a <= end_b and start_b <=
    end_a`); overlap is transitive (A-B and B-C overlapping puts A, B, and C
    in one cluster even if A and C do not directly overlap).

    Implementation note: sorting each (symbol, direction) group by
    `(start_ms, uid)` and sweeping while tracking the running maximum
    `end_ms` is equivalent to full pairwise-transitive-closure clustering
    for 1-D intervals, and is what this function does (no O(n^2) pairwise
    comparison needed). A malformed interval with `end_ms < start_ms` is
    defensively treated as its own single-member cluster rather than
    raising or silently repairing it: malformed intervals are swept out of
    the group entirely before well-formed clustering runs, so a malformed
    interval never merges with — or interrupts the transitive merge of —
    any well-formed interval, regardless of shared start times or sort tie
    ordering.
    """
    groups: dict[tuple[str, str], list[OverlapInterval]] = {}
    for interval in intervals:
        key = (interval.symbol, interval.direction)
        groups.setdefault(key, []).append(interval)

    clusters: list[OverlapCluster] = []
    for symbol, direction in sorted(groups):
        group = groups[(symbol, direction)]
        ordered = sorted(group, key=lambda iv: (iv.start_ms, iv.uid))

        malformed = [iv for iv in ordered if iv.end_ms < iv.start_ms]
        well_formed = [iv for iv in ordered if iv.end_ms >= iv.start_ms]

        for iv in malformed:
            clusters.append(_finalize_cluster(symbol, direction, [iv]))

        current: list[OverlapInterval] = []
        current_max_end: int | None = None
        for iv in well_formed:
            if current and current_max_end is not None and iv.start_ms <= current_max_end:
                current.append(iv)
                current_max_end = max(current_max_end, iv.end_ms)
                continue
            if current:
                clusters.append(_finalize_cluster(symbol, direction, current))
            current = [iv]
            current_max_end = iv.end_ms
        if current:
            clusters.append(_finalize_cluster(symbol, direction, current))

    return tuple(clusters)


def _finalize_cluster(symbol: str, direction: str, members: list[OverlapInterval]) -> OverlapCluster:
    ordered = sorted(members, key=lambda iv: (iv.start_ms, iv.uid))
    anchor_uid = ordered[0].uid
    member_uids = tuple(sorted(iv.uid for iv in members))
    cluster_id = f"{symbol}:{direction}:{anchor_uid}"
    return OverlapCluster(
        cluster_id=cluster_id, symbol=symbol, direction=direction, member_uids=member_uids
    )


def cluster_id_by_uid(clusters: Sequence[OverlapCluster]) -> dict[str, str]:
    """Pure. Flattens `build_overlap_clusters`' output into a `uid ->
    cluster_id` lookup, for tagging individual rows/candidates.
    """
    mapping: dict[str, str] = {}
    for cluster in clusters:
        for uid in cluster.member_uids:
            mapping[uid] = cluster.cluster_id
    return mapping


def clusters_for_rows(rows: Sequence[QualityRow]) -> dict[str, str]:
    """Pure. Builds the global overlap-cluster assignment for every row
    that has a defined evaluation window (`has_overlap_window`), and
    returns the `opportunity_uid -> cluster_id` mapping. Rows without a
    window (no plan, or an UNAVAILABLE plan) are simply absent from the
    mapping.
    """
    intervals = [
        OverlapInterval(
            uid=row.opportunity_uid,
            symbol=row.symbol,
            direction=row.direction,
            start_ms=row.evaluation_not_before_ms,  # type: ignore[arg-type]
            end_ms=row.evaluation_expiry_ms,  # type: ignore[arg-type]
        )
        for row in rows
        if row.has_overlap_window
    ]
    return cluster_id_by_uid(build_overlap_clusters(intervals))


# ---------------------------------------------------------------------------
# Rates with explicit denominators
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutcomeRates:
    """Every rate's denominator is `total_n` (the cohort's whole sample),
    never the clear-resolved subset — so ambiguity/incomplete/expiry rates
    stay comparable across cohorts with different resolution rates. `other_n`
    covers every remaining state (HIT_1R, PENDING_ENTRY, ENTERED,
    NOT_EVALUABLE, or no outcome/plan row at all).
    """

    total_n: int
    clear_resolved_n: int
    ambiguous_n: int
    ambiguous_rate: float | None
    incomplete_n: int
    incomplete_rate: float | None
    expired_n: int
    expired_rate: float | None
    other_n: int


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def compute_outcome_rates(rows: Sequence[QualityRow]) -> OutcomeRates:
    """Pure. See `OutcomeRates` docstring for denominator semantics."""
    total_n = len(rows)
    clear_resolved_n = sum(1 for r in rows if gross_r_for_state(r.outcome_state) is not None)
    ambiguous_n = sum(1 for r in rows if r.outcome_state in _AMBIGUOUS_STATES)
    incomplete_n = sum(1 for r in rows if r.outcome_state in _INCOMPLETE_STATES)
    expired_n = sum(1 for r in rows if r.outcome_state in _EXPIRED_STATES)
    other_n = total_n - clear_resolved_n - ambiguous_n - incomplete_n - expired_n
    return OutcomeRates(
        total_n=total_n,
        clear_resolved_n=clear_resolved_n,
        ambiguous_n=ambiguous_n,
        ambiguous_rate=_rate(ambiguous_n, total_n),
        incomplete_n=incomplete_n,
        incomplete_rate=_rate(incomplete_n, total_n),
        expired_n=expired_n,
        expired_rate=_rate(expired_n, total_n),
        other_n=other_n,
    )


# ---------------------------------------------------------------------------
# Cohort summaries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CohortQualitySummary:
    """One dimension/key's full quality picture: sample counts, rates,
    gross/net R, cluster-aware uncertainty, and qualification. `cost_r` is
    echoed back so a report can show exactly what cost assumption produced
    `net_mean_r` (or why it is unavailable).
    """

    dimension: str
    key: str
    total_n: int
    rates: OutcomeRates
    distinct_resolved_clusters: int
    gross_mean_r: float | None
    net_mean_r: float | None
    cost_r: float | None
    uncertainty: uncertainty.UncertaintyResult
    qualification: uncertainty.QualificationResult


def summarize_cohort(
    rows: Sequence[QualityRow],
    *,
    dimension: str,
    key: str,
    cost_r: float | None,
    cluster_ids: Mapping[str, str],
) -> CohortQualitySummary:
    """Pure. Builds one `CohortQualitySummary` for an arbitrary subset of
    rows (a "cohort"). `cluster_ids` must be the *global* `opportunity_uid
    -> cluster_id` mapping from `clusters_for_rows` on the full dataset,
    not recomputed on this subset — overlap identity is a property of the
    underlying market event, not of which cohort happens to be displayed
    (see module docstring).
    """
    rates = compute_outcome_rates(rows)
    cost_valid = _is_finite_number(cost_r) and cost_r >= 0  # type: ignore[operator]

    gross_values: list[float] = []
    net_values: list[float] = []
    cluster_net_r: dict[str, list[float]] = {}
    missing_cluster_mapping = False
    for row in rows:
        gross_r = gross_r_for_state(row.outcome_state)
        if gross_r is None:
            continue
        gross_values.append(gross_r)
        net_r = net_r_for_gross(gross_r, cost_r)
        if net_r is None:
            continue
        cluster_id = cluster_ids.get(row.opportunity_uid)
        if cluster_id is None:
            # A clear-resolved, cost-valid row with no cluster mapping means
            # the net/cluster row sets would no longer match the gross row
            # set — fail net qualification closed instead of computing a
            # net mean/uncertainty over a mismatched subset (see module
            # docstring).
            missing_cluster_mapping = True
            continue
        net_values.append(net_r)
        cluster_net_r.setdefault(cluster_id, []).append(net_r)

    gross_mean_r = statistics.mean(gross_values) if gross_values else None
    # Net mean/qualification must fail closed on a missing/invalid cost or a
    # missing cluster mapping: if any clear-resolved row lost its net value
    # to a bad cost, or lost its cluster identity, net_mean_r for the whole
    # cohort is None (never a partial mean over fewer rows than
    # gross_mean_r covered) — see module docstring.
    net_mean_r = (
        statistics.mean(net_values)
        if (cost_valid and net_values and not missing_cluster_mapping)
        else None
    )

    uncertainty_result = (
        uncertainty.compute_cluster_uncertainty(cluster_net_r)
        if (cost_valid and not missing_cluster_mapping)
        else uncertainty.compute_cluster_uncertainty({})
    )
    distinct_resolved_clusters = uncertainty_result.cluster_count

    qualification = uncertainty.qualify_cohort(
        distinct_resolved_clusters=distinct_resolved_clusters,
        ambiguous_rate=rates.ambiguous_rate,
        net_mean_r=net_mean_r,
        lower_bound_95=uncertainty_result.lower_bound_95,
    )

    return CohortQualitySummary(
        dimension=dimension,
        key=key,
        total_n=rates.total_n,
        rates=rates,
        distinct_resolved_clusters=distinct_resolved_clusters,
        gross_mean_r=gross_mean_r,
        net_mean_r=net_mean_r,
        cost_r=cost_r if cost_valid else None,
        uncertainty=uncertainty_result,
        qualification=qualification,
    )


@dataclass(frozen=True)
class QualityCohortReport:
    """The full set of dimension breakdowns a report renders. Every mapping
    is `key -> CohortQualitySummary` with deterministic (sorted) key
    ordering enforced at render time, never at storage time, so this
    dataclass itself carries no ordering assumption.
    """

    overall: CohortQualitySummary
    by_setup_family: dict[str, CohortQualitySummary]
    by_timeframe: dict[str, CohortQualitySummary]
    by_direction: dict[str, CohortQualitySummary]
    by_score_band: dict[str, CohortQualitySummary]
    by_symbol: dict[str, CohortQualitySummary]
    by_cluster: dict[str, CohortQualitySummary]
    by_exact_cohort: dict[CohortKey, CohortQualitySummary]
    cost_r: float | None


def _group_by(rows: Sequence[QualityRow], key_fn) -> dict[str, list[QualityRow]]:
    groups: dict[str, list[QualityRow]] = {}
    for row in rows:
        groups.setdefault(key_fn(row), []).append(row)
    return groups


def build_quality_cohort_report(
    rows: Sequence[QualityRow], *, cost_r: float | None
) -> QualityCohortReport:
    """Pure. Builds every cohort dimension required by the report:
    setup family, timeframe, direction, descriptive score band, symbol
    concentration, and overlapping cluster — plus the combined "exact
    quality cohort" (setup_family, primary_timeframe, direction) that
    `shadow_alerts.py` gates on. `cost_r` is the caller's single explicit
    round-trip cost assumption in R, applied uniformly (see
    `net_r_for_gross`); passing None leaves every net metric visibly
    unavailable rather than inventing one.
    """
    cluster_ids = clusters_for_rows(rows)

    def _summary(dimension: str, key: str, subset: Sequence[QualityRow]) -> CohortQualitySummary:
        return summarize_cohort(
            subset, dimension=dimension, key=key, cost_r=cost_r, cluster_ids=cluster_ids
        )

    overall = _summary("overall", "ALL", rows)

    by_setup_family = {
        key: _summary("setup_family", key, subset)
        for key, subset in _group_by(rows, lambda r: r.setup_family).items()
    }
    by_timeframe = {
        key: _summary("primary_timeframe", key, subset)
        for key, subset in _group_by(rows, lambda r: r.primary_timeframe).items()
    }
    by_direction = {
        key: _summary("direction", key, subset)
        for key, subset in _group_by(rows, lambda r: r.direction).items()
    }
    by_score_band = {
        key: _summary("score_band", key, subset)
        for key, subset in _group_by(rows, lambda r: r.score_band).items()
    }
    by_symbol = {
        key: _summary("symbol", key, subset)
        for key, subset in _group_by(rows, lambda r: r.symbol).items()
    }
    by_cluster_uid: dict[str, list[QualityRow]] = {}
    for row in rows:
        cluster_id = cluster_ids.get(row.opportunity_uid)
        if cluster_id is None:
            continue
        by_cluster_uid.setdefault(cluster_id, []).append(row)
    by_cluster = {
        key: _summary("overlap_cluster", key, subset) for key, subset in by_cluster_uid.items()
    }

    by_exact_cohort_rows: dict[CohortKey, list[QualityRow]] = {}
    for row in rows:
        by_exact_cohort_rows.setdefault(row.cohort_key, []).append(row)
    by_exact_cohort = {
        cohort_key: _summary(
            "exact_cohort",
            f"{cohort_key.setup_family}:{cohort_key.primary_timeframe}:{cohort_key.direction}",
            subset,
        )
        for cohort_key, subset in by_exact_cohort_rows.items()
    }

    return QualityCohortReport(
        overall=overall,
        by_setup_family=by_setup_family,
        by_timeframe=by_timeframe,
        by_direction=by_direction,
        by_score_band=by_score_band,
        by_symbol=by_symbol,
        by_cluster=by_cluster,
        by_exact_cohort=by_exact_cohort,
        cost_r=cost_r,
    )
