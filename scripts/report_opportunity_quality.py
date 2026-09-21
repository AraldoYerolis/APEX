"""Opportunity Quality Report v0.1 — read-only cohort quality report over
opportunity_observations / opportunity_trade_plans /
opportunity_trade_plan_outcomes.

Usage (run from repo root):
    PYTHONPATH=src python scripts/report_opportunity_quality.py
    PYTHONPATH=src python scripts/report_opportunity_quality.py --cost-r 0.05
    PYTHONPATH=src python scripts/report_opportunity_quality.py --fee-r 0.02 --slippage-r 0.01
    PYTHONPATH=src python scripts/report_opportunity_quality.py --family SWEEP_RECLAIM --direction LONG

This script is READ-ONLY, always: it opens SQLite with `mode=ro` (fails
closed — raises rather than creating a missing database file) plus
`PRAGMA query_only=ON`, and it never calls init_db() and never creates or
migrates anything. It never sends a notification and never imports
`apex.notifications`.

Cost-adjusted (net-R) metrics require an explicit round-trip cost, supplied
either as `--cost-r TOTAL` or as `--fee-r FEE --slippage-r SLIPPAGE`
(summed). Without one, every net-* metric in the report is plainly labeled
UNAVAILABLE rather than defaulting to zero cost — this script never invents
a cost. Gross (pre-cost) metrics are always shown regardless.

Score bands shown in this report are fixed, versioned, descriptive labels
only (see opportunity/quality_cohorts.py) — never an alert/score threshold.
Only a clear resolved HIT_2R/STOPPED outcome ever contributes to gross/net
R; every other outcome state (including AMBIGUOUS, INSUFFICIENT_DATA, and
every still-open/expired state) is reported separately with its own
explicit denominator, never coerced into a win or a loss.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from pathlib import Path
from typing import Any

from apex.config import get_settings
from apex.opportunity.quality_cohorts import (
    SCORE_BAND_ORDER,
    CohortQualitySummary,
    QualityCohortReport,
    QualityRow,
    build_quality_cohort_report,
)

REQUIRED_TABLES = ("opportunity_observations", "opportunity_trade_plans", "opportunity_trade_plan_outcomes")

_SCORE_COLUMNS = frozenset({"total_score", "score_version"})

_TOP_CLUSTERS_SHOWN = 20


def _open_readonly_connection(db_path: str) -> sqlite3.Connection:
    """A genuine read-only SQLite connection: mode=ro fails closed (raises)
    instead of creating a missing database file, and PRAGMA query_only=ON
    additionally refuses any write this connection might otherwise attempt.
    Never applies schema.sql or any migration, unlike init_db().
    """
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _missing_tables(conn: sqlite3.Connection) -> list[str]:
    return [t for t in REQUIRED_TABLES if not _table_exists(conn, t)]


def _has_score_columns(conn: sqlite3.Connection) -> bool:
    """Safe read-only schema inspection (PRAGMA table_info) — this script
    never adds the Context and ranking v0.1 score columns itself; a
    database that predates them is simply reported as entirely unscored.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(opportunity_observations)").fetchall()}
    return _SCORE_COLUMNS.issubset(cols)


def _row_to_quality_row(row: Any, *, scored_schema: bool) -> QualityRow:
    keys = row.keys()
    total_score = row["total_score"] if scored_schema and "total_score" in keys else None
    score_version = row["score_version"] if scored_schema and "score_version" in keys else None
    return QualityRow(
        opportunity_uid=row["opportunity_uid"],
        symbol=row["symbol"],
        direction=row["direction"],
        setup_family=row["setup_family"],
        primary_timeframe=row["primary_timeframe"],
        total_score=total_score,
        score_version=score_version,
        plan_availability=row["plan_availability"],
        evaluation_not_before_ms=row["evaluation_not_before_ms"],
        evaluation_expiry_ms=row["evaluation_expiry_ms"],
        outcome_state=row["outcome_state"],
    )


def load_rows(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    symbol: str | None = None,
    family: str | None = None,
    timeframe: str | None = None,
    direction: str | None = None,
) -> list[QualityRow]:
    """Read-only join of opportunity_observations LEFT JOIN
    opportunity_trade_plans LEFT JOIN opportunity_trade_plan_outcomes,
    mapped into `QualityRow`. A LEFT JOIN (not INNER) so an opportunity
    that predates Trade plans and outcome evidence v0.1 (no plan/outcome
    row) is still represented, with `plan_availability`/`outcome_state`
    left None rather than being silently dropped.
    """
    scored_schema = _has_score_columns(conn)
    score_select = "o.total_score AS total_score, o.score_version AS score_version," if scored_schema else ""

    conditions: list[str] = []
    params: list[Any] = []
    if since:
        conditions.append("o.last_seen_at >= ?")
        params.append(since)
    if symbol:
        conditions.append("o.symbol = ?")
        params.append(symbol.upper())
    if family:
        conditions.append("o.setup_family = ?")
        params.append(family.upper())
    if timeframe:
        conditions.append("o.primary_timeframe = ?")
        params.append(timeframe)
    if direction:
        conditions.append("o.direction = ?")
        params.append(direction.upper())
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    query = f"""
        SELECT
            o.opportunity_uid AS opportunity_uid,
            o.symbol AS symbol,
            o.direction AS direction,
            o.setup_family AS setup_family,
            o.primary_timeframe AS primary_timeframe,
            {score_select}
            p.availability AS plan_availability,
            p.evaluation_not_before_ms AS evaluation_not_before_ms,
            p.evaluation_expiry_ms AS evaluation_expiry_ms,
            oc.state AS outcome_state
        FROM opportunity_observations o
        LEFT JOIN opportunity_trade_plans p ON p.opportunity_uid = o.opportunity_uid
        LEFT JOIN opportunity_trade_plan_outcomes oc ON oc.plan_uid = p.plan_uid
        {where}
        ORDER BY o.opportunity_uid ASC
    """
    rows = conn.execute(query, params).fetchall()
    return [_row_to_quality_row(r, scored_schema=scored_schema) for r in rows]


def build_report(rows: list[QualityRow], *, cost_r: float | None) -> QualityCohortReport:
    """Pure. Thin, testable wrapper around
    `quality_cohorts.build_quality_cohort_report`."""
    return build_quality_cohort_report(rows, cost_r=cost_r)


def _fmt_optional(value: float | None, fmt: str = "{:.3f}") -> str:
    return fmt.format(value) if value is not None else "n/a"


def _render_summary_block(summary: CohortQualitySummary) -> list[str]:
    lines: list[str] = []
    lines.append(f"  [{summary.dimension}] {summary.key}")
    r = summary.rates
    lines.append(
        f"    n_total={r.total_n}  clear_resolved={r.clear_resolved_n}  "
        f"distinct_resolved_clusters={summary.distinct_resolved_clusters}"
    )
    lines.append(
        f"    ambiguous={r.ambiguous_n} ({_fmt_optional(r.ambiguous_rate, '{:.1%}')})   "
        f"incomplete={r.incomplete_n} ({_fmt_optional(r.incomplete_rate, '{:.1%}')})   "
        f"expired={r.expired_n} ({_fmt_optional(r.expired_rate, '{:.1%}')})   other={r.other_n}"
    )
    lines.append(
        f"    gross_mean_r={_fmt_optional(summary.gross_mean_r, '{:+.3f}')}   "
        f"net_mean_r={_fmt_optional(summary.net_mean_r, '{:+.3f}')}   "
        f"cost_r={_fmt_optional(summary.cost_r, '{:+.4f}')}"
    )
    u = summary.uncertainty
    lines.append(
        f"    uncertainty[{u.uncertainty_version}]: clusters={u.cluster_count}   "
        f"mean_r={_fmt_optional(u.mean_r, '{:+.3f}')}   se={_fmt_optional(u.standard_error)}   "
        f"lower_bound_95={_fmt_optional(u.lower_bound_95, '{:+.3f}')}"
    )
    q = summary.qualification
    qual_label = "QUALIFIES" if q.qualifies else "DOES NOT QUALIFY"
    reasons_label = f"   reasons={list(q.reasons)}" if q.reasons else ""
    lines.append(f"    qualification: {qual_label}{reasons_label}")
    return lines


def render_report(report: QualityCohortReport) -> str:
    """Pure, deterministic text rendering. See module docstring."""
    lines: list[str] = []
    lines.append("=" * 100)
    lines.append("  APEX Opportunity Quality Report v0.1 (read-only)")
    lines.append("=" * 100)
    lines.append(
        "  Descriptive research report only. Score bands are fixed, versioned, descriptive"
    )
    lines.append(
        "  labels — never an alert/score threshold. Only clear resolved HIT_2R/STOPPED map to"
    )
    lines.append(
        "  +2R/-1R; every other outcome state is reported separately, never coerced into a win/loss."
    )
    if report.cost_r is None:
        lines.append("  NOTE: no explicit round-trip cost supplied — every net-* metric is UNAVAILABLE.")
    else:
        lines.append(f"  Round-trip cost assumption: {report.cost_r:+.4f}R (explicit, caller-supplied)")
    lines.append("")

    lines.append("OVERALL")
    lines.extend(_render_summary_block(report.overall))

    lines.append("")
    lines.append("BY SETUP FAMILY")
    for key in sorted(report.by_setup_family):
        lines.extend(_render_summary_block(report.by_setup_family[key]))

    lines.append("")
    lines.append("BY TIMEFRAME")
    for key in sorted(report.by_timeframe):
        lines.extend(_render_summary_block(report.by_timeframe[key]))

    lines.append("")
    lines.append("BY DIRECTION")
    for key in sorted(report.by_direction):
        lines.extend(_render_summary_block(report.by_direction[key]))

    lines.append("")
    lines.append("BY SCORE BAND (descriptive only — never an alert threshold)")
    for key in SCORE_BAND_ORDER:
        summary = report.by_score_band.get(key)
        if summary is not None:
            lines.extend(_render_summary_block(summary))

    lines.append("")
    lines.append("BY SYMBOL (concentration, descending sample size)")
    for key in sorted(report.by_symbol, key=lambda k: (-report.by_symbol[k].total_n, k)):
        lines.extend(_render_summary_block(report.by_symbol[key]))

    lines.append("")
    lines.append(f"BY OVERLAPPING CLUSTER (top {_TOP_CLUSTERS_SHOWN} by sample size)")
    cluster_keys = sorted(report.by_cluster, key=lambda k: (-report.by_cluster[k].total_n, k))
    for key in cluster_keys[:_TOP_CLUSTERS_SHOWN]:
        lines.extend(_render_summary_block(report.by_cluster[key]))
    if len(cluster_keys) > _TOP_CLUSTERS_SHOWN:
        lines.append(f"    ... {len(cluster_keys) - _TOP_CLUSTERS_SHOWN} more cluster(s) omitted ...")

    lines.append("")
    lines.append(
        "EXACT QUALITY COHORT (setup_family:timeframe:direction) "
        "— the cohort shadow_alerts.py gates on"
    )
    for cohort_key in sorted(
        report.by_exact_cohort, key=lambda k: (k.setup_family, k.primary_timeframe, k.direction)
    ):
        lines.extend(_render_summary_block(report.by_exact_cohort[cohort_key]))

    lines.append("=" * 100)
    return "\n".join(lines)


def _resolve_cost_r(args: argparse.Namespace) -> tuple[float | None, str | None]:
    """Pure. Returns `(cost_r, error_message)` — `error_message` is set
    only for a malformed *explicit* cost input; a wholly absent cost input
    is not an error (returns `(None, None)`), see module docstring.
    """
    if args.cost_r is not None:
        if not (math.isfinite(args.cost_r) and args.cost_r >= 0):
            return None, "--cost-r must be a finite, nonnegative number"
        return args.cost_r, None
    if args.fee_r is not None or args.slippage_r is not None:
        if args.fee_r is None or args.slippage_r is None:
            return None, "--fee-r and --slippage-r must both be supplied together (or use --cost-r)"
        if not (math.isfinite(args.fee_r) and args.fee_r >= 0):
            return None, "--fee-r must be a finite, nonnegative number"
        if not (math.isfinite(args.slippage_r) and args.slippage_r >= 0):
            return None, "--slippage-r must be a finite, nonnegative number"
        return args.fee_r + args.slippage_r, None
    return None, None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "APEX Opportunity Quality Report v0.1 — read-only, never creates or migrates "
            "the database, never sends a notification"
        )
    )
    parser.add_argument("--symbol", metavar="SYMBOL", default=None)
    parser.add_argument("--family", metavar="SETUP_FAMILY", default=None)
    parser.add_argument("--timeframe", metavar="3m|5m", default=None)
    parser.add_argument("--direction", metavar="LONG|SHORT", default=None)
    parser.add_argument(
        "--since", metavar="ISO_TIMESTAMP", default=None,
        help="Filter to opportunities with last_seen_at >= TIMESTAMP",
    )
    parser.add_argument(
        "--cost-r", metavar="R", type=float, default=None,
        help="Explicit total round-trip cost in R (mutually usable instead of --fee-r/--slippage-r)",
    )
    parser.add_argument("--fee-r", metavar="R", type=float, default=None, help="Explicit fee cost in R")
    parser.add_argument(
        "--slippage-r", metavar="R", type=float, default=None, help="Explicit slippage cost in R"
    )
    args = parser.parse_args(argv)

    cost_r, cost_error = _resolve_cost_r(args)
    if cost_error is not None:
        print(f"ERROR: {cost_error}", file=sys.stderr)
        sys.exit(1)

    settings = get_settings()
    db_path = settings.apex_db_path

    if not Path(db_path).exists():
        print(
            f"No database file at {db_path!r}; this report never creates one "
            "(run the bot first to initialize it).",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        conn = _open_readonly_connection(db_path)
    except sqlite3.Error as e:
        print(f"ERROR: could not open {db_path!r} read-only: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        missing = _missing_tables(conn)
        if missing:
            print()
            print("=" * 100)
            print("  APEX Opportunity Quality Report v0.1 (read-only)")
            print("=" * 100)
            print(f"  Missing table(s): {', '.join(missing)}. Nothing to report.")
            print("=" * 100)
            return

        try:
            rows = load_rows(
                conn,
                since=args.since,
                symbol=args.symbol,
                family=args.family,
                timeframe=args.timeframe,
                direction=args.direction,
            )
        except sqlite3.Error as e:
            print(f"ERROR: could not query quality evidence: {e}", file=sys.stderr)
            sys.exit(1)

        report = build_report(rows, cost_r=cost_r)
        print(render_report(report))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
