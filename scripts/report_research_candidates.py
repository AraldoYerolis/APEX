"""Research Candidate Prospective Tracking Report — Milestone 11F.

Reads post-11F signal_features rows (metadata_json contains
research_candidate_version = '11F_v1') and compares outcome statistics
across research candidate tags.

Purpose: answer "Do the 11E promising/weak cohorts continue to validate
prospectively after 11F was deployed?"

REPORT-ONLY. Does not write to the database. Does not change signal
generation, alerts, config, or runtime behavior. No runtime filters are
applied. Candidate tags are metadata only.

This report is only meaningful after enough post-11F observations have
closed. Until then, cohort sample sizes will be small and results should
be treated as INSUFFICIENT.

Positive tags (identified as promising in 11E):
  SESSION_US, ATR_0_50_TO_1_00, EMA_GT_0_25, SYMBOL_WLD, SYMBOL_DOGE, SYMBOL_BTC

Negative/avoid tags (identified as weak in 11E):
  SESSION_LATE_US, HOUR_10_AVOID, HOUR_03_AVOID, HOUR_21_AVOID

Usage (run from repo root):
    PYTHONPATH=src python scripts/report_research_candidates.py
    PYTHONPATH=src python scripts/report_research_candidates.py \\
        --since 2026-05-22T00:00:00Z
    PYTHONPATH=src python scripts/report_research_candidates.py \\
        --research-version 11F_v1 --min-n 10
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

# -----------------------------------------------------------------------
# sys.path bootstrap — mirrors other APEX report scripts.
# -----------------------------------------------------------------------
_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent
_SRC_DIR = _REPO_ROOT / "src"
for _p in (str(_REPO_ROOT), str(_SRC_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from apex.config import get_settings
from apex.db.connection import close_db, init_db
from apex.strategy.research_candidates import (
    CANDIDATE_DEFINITIONS,
    RESEARCH_CANDIDATE_VERSION,
    _NEGATIVE_TAGS,
    _POSITIVE_TAGS,
)
from scripts.analyze_exit_timing_cohorts import (
    _cohort_metrics,
    _is_dir_concentrated,
    _print_cohort_block,
    _print_divider,
    _print_section,
    _r_s,
    _secs_to_min,
)
from scripts.report_signal_learning import _pct

_DEFAULT_MIN_N = 10
_DEFAULT_SIGNAL_TYPE = "CONFIRMED_SETUP"


# ======================================================================
# Metadata helpers
# ======================================================================

def _parse_rc_meta(row: Any) -> Optional[dict]:
    """Parse metadata_json and return the dict if it contains 11F research candidate data."""
    raw = row["metadata_json"]
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return None
        if "research_candidate_version" not in parsed:
            return None
        return parsed
    except (json.JSONDecodeError, TypeError):
        return None


def _tag_matched(meta: Optional[dict], tag: str) -> bool:
    """Return True if this tag is in the matched list for this row."""
    if meta is None:
        return False
    names = meta.get("research_candidate_names", [])
    return tag in names


# ======================================================================
# Print helpers (supplement _print_cohort_block from analyze_exit_timing)
# ======================================================================

def _print_divider_local(char: str = "=", width: int = 68) -> None:
    print(char * width)


def _print_tag_cohort(
    label: str,
    tag: str,
    rows: list[Any],
    all_rc_rows: list[Any],
    min_n: int,
) -> None:
    """Print a single tag cohort: matched rows vs. non-matched for context."""
    matched = [r for r in rows if _tag_matched(_parse_rc_meta(r), tag)]
    not_matched = [r for r in rows if not _tag_matched(_parse_rc_meta(r), tag)]

    tag_def = CANDIDATE_DEFINITIONS.get(tag, {})
    note = tag_def.get("direction_concentration_note", "")

    print(f"\n  {label}")
    if note:
        print(f"  [NOTE: {note}]")
    print(f"  Basis: {tag_def.get('basis', 'n/a')}")

    _print_cohort_block(f"  MATCHED ({tag})", matched, min_n)
    _print_cohort_block(f"  NOT_MATCHED ({tag})", not_matched, min_n)


# ======================================================================
# Main
# ======================================================================

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="APEX Research Candidate Prospective Tracking Report [Milestone 11F] — report-only"
    )
    parser.add_argument(
        "--since",
        metavar="ISO_TIMESTAMP",
        default=None,
        help="Filter features captured_at >= TIMESTAMP (recommended: 11F deploy timestamp)",
    )
    parser.add_argument(
        "--min-n",
        metavar="N",
        type=int,
        default=_DEFAULT_MIN_N,
        help=f"Minimum closed rows to show non-INSUFFICIENT cohort (default: {_DEFAULT_MIN_N})",
    )
    parser.add_argument(
        "--signal-type",
        metavar="TYPE",
        default=_DEFAULT_SIGNAL_TYPE,
        help=f"Signal type to analyse (default: {_DEFAULT_SIGNAL_TYPE})",
    )
    parser.add_argument(
        "--research-version",
        metavar="VERSION",
        default=RESEARCH_CANDIDATE_VERSION,
        help=f"Research candidate version tag in metadata (default: {RESEARCH_CANDIDATE_VERSION})",
    )
    parser.add_argument(
        "--feature-version",
        metavar="VERSION",
        default=None,
        help="Optional feature_version filter (e.g. 11C_v1). Default: all versions.",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    conn = init_db(settings.apex_db_path)

    try:
        conditions: list[str] = [
            "sf.signal_type = ?",
            "sf.metadata_json IS NOT NULL",
        ]
        params: list[Any] = [args.signal_type]
        if args.since:
            conditions.append("sf.captured_at >= ?")
            params.append(args.since)
        if args.feature_version:
            conditions.append("sf.feature_version = ?")
            params.append(args.feature_version)
        where = "WHERE " + " AND ".join(conditions)
        query = f"""
            SELECT sf.*,
                   so.max_favorable_excursion AS so_mfe,
                   so.max_adverse_excursion   AS so_mae,
                   so.hit_1r_at              AS so_hit_1r_at,
                   so.hit_2r_at              AS so_hit_2r_at,
                   so.stopped_at             AS so_stopped_at,
                   so.expired_at             AS so_expired_at,
                   so.time_to_1r_seconds     AS so_time_to_1r,
                   so.time_to_2r_seconds     AS so_time_to_2r,
                   so.time_to_stop_seconds   AS so_time_to_stop,
                   so.time_to_expiry_seconds AS so_time_to_expiry,
                   so.hit_1r_before_stop     AS so_hit_1r_before_stop,
                   so.hit_1r_before_expiry   AS so_hit_1r_before_expiry
            FROM signal_features sf
            LEFT JOIN signal_observations so
                   ON sf.observation_uid = so.observation_uid
            {where}
            ORDER BY sf.captured_at ASC
        """
        rows = conn.execute(query, params).fetchall()
    except Exception as e:
        print(f"ERROR: could not query signal_features: {e}", file=sys.stderr)
        close_db()
        sys.exit(1)

    close_db()

    # Filter to rows with 11F research candidate metadata
    rc_rows = [r for r in rows if _parse_rc_meta(r) is not None
               and _parse_rc_meta(r).get("research_candidate_version") == args.research_version]

    # Of those, restrict to closed rows for outcome analysis
    closed_rc = [r for r in rc_rows if r["outcome_status"] not in (None, "OBSERVED")]

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------
    print()
    _print_divider()
    print("  APEX Research Candidate Prospective Tracking Report  [Milestone 11F]")
    _print_divider()
    print("  REPORT-ONLY. Does not change scanning, alerts, gates, or trading.")
    print("  Does not write to the database.")
    print("  Research candidate tags are metadata only — no signals are filtered.")
    print()
    print(f"  Signal type            : {args.signal_type}")
    print(f"  Research version       : {args.research_version}")
    if args.since:
        print(f"  Filtered since         : {args.since}")
    if args.feature_version:
        print(f"  Feature version filter : {args.feature_version}")
    print(f"  Total rows loaded      : {len(rows)}")
    print(f"  Rows with 11F metadata : {len(rc_rows)}")
    print(f"  Closed rows (outcomes) : {len(closed_rc)}")
    print(f"  Min-N threshold        : {args.min_n}")

    if len(rc_rows) == 0:
        print()
        print("  No 11F research candidate observations found yet.")
        print("  Deploy 11F and wait for observations to accumulate, then run")
        print("  this report with --since <11F_DEPLOY_TIMESTAMP>.")
        print()
        _print_divider()
        return

    if len(closed_rc) == 0:
        print()
        print("  11F observations exist but none have closed yet.")
        print("  Wait for observations to close before interpreting results.")
        print()
        _print_divider()
        return

    # ------------------------------------------------------------------
    # Section 1: Baseline — all post-11F closed rows
    # ------------------------------------------------------------------
    _print_section("1 — Baseline: All Post-11F Closed Rows")
    print("  All closed rows that carry 11F research candidate metadata.")
    print("  Use this as the reference for tag comparison.")
    _print_cohort_block("BASELINE_ALL_11F", closed_rc, args.min_n)

    baseline_m = _cohort_metrics(closed_rc)
    baseline_tp1_rate = (baseline_m["tp1_n"] / baseline_m["n"]
                         if baseline_m["n"] else 0.0)

    # ------------------------------------------------------------------
    # Section 2: Positive / review tags
    # ------------------------------------------------------------------
    _print_section("2 — Positive / Review Tags")
    print("  Tags identified as promising in 11E.")
    print("  PROSPECTIVE VALIDATION ONLY — these were not runtime filters.")
    print("  Compare matched rows against baseline to see if the edge holds.")
    print()
    print("  Warning: small sample sizes are expected early after 11F deployment.")
    print("  Do not draw conclusions from n < min_n cohorts.")

    positive_tags_ordered = [
        "SESSION_US",
        "ATR_0_50_TO_1_00",
        "EMA_GT_0_25",
        "SYMBOL_WLD",
        "SYMBOL_DOGE",
        "SYMBOL_BTC",
    ]

    pos_summary: list[tuple[str, dict, dict]] = []  # (tag, matched_m, not_matched_m)

    for tag in positive_tags_ordered:
        matched_rows = [r for r in closed_rc if _tag_matched(_parse_rc_meta(r), tag)]
        not_matched_rows = [r for r in closed_rc if not _tag_matched(_parse_rc_meta(r), tag)]
        tag_def = CANDIDATE_DEFINITIONS.get(tag, {})
        note = tag_def.get("direction_concentration_note", "")

        print(f"\n  [{tag}]")
        print(f"  Basis: {tag_def.get('basis', 'n/a')}")
        if note:
            print(f"  [NOTE: {note}]")

        _print_cohort_block(f"  MATCHED", matched_rows, args.min_n)
        _print_cohort_block(f"  NOT_MATCHED", not_matched_rows, args.min_n)

        pos_summary.append((tag, _cohort_metrics(matched_rows), _cohort_metrics(not_matched_rows)))

    # Summary table for positive tags
    print()
    print("  Positive Tags Summary:")
    print(f"  {'Tag':<22} {'n_matched':>9}  {'TP1%':>6}  {'BAvgR':>7}  "
          f"{'n_not':>6}  {'TP1%_not':>8}  {'DC':>4}")
    print("  " + "-" * 68)
    for tag, mm, nm in pos_summary:
        n_m = mm["n"]
        n_nm = nm["n"]
        tp1_m = mm["tp1_n"] / n_m * 100 if n_m else 0.0
        tp1_nm = nm["tp1_n"] / n_nm * 100 if n_nm else 0.0
        b_r = _r_s(mm["policy_b_avg_r"])
        dc = "[DC]" if _is_dir_concentrated(mm["long_n"], mm["short_n"]) else ""
        insuf_m = "*" if n_m < args.min_n else ""
        insuf_nm = "*" if n_nm < args.min_n else ""
        print(f"  {tag:<22} {n_m:>8}{insuf_m}  {tp1_m:>5.1f}%  {b_r:>7}  "
              f"{n_nm:>5}{insuf_nm}  {tp1_nm:>7.1f}%  {dc:>4}")
    print("  (* = below min_n threshold)")

    # ------------------------------------------------------------------
    # Section 3: Negative / avoid tags
    # ------------------------------------------------------------------
    _print_section("3 — Negative / Avoid Tags")
    print("  Tags identified as weak in 11E.")
    print("  PROSPECTIVE VALIDATION ONLY — these were not runtime filters.")
    print("  Compare matched rows against baseline to see if the weakness holds.")

    negative_tags_ordered = [
        "SESSION_LATE_US",
        "HOUR_10_AVOID",
        "HOUR_03_AVOID",
        "HOUR_21_AVOID",
    ]

    neg_summary: list[tuple[str, dict, dict]] = []

    for tag in negative_tags_ordered:
        matched_rows = [r for r in closed_rc if _tag_matched(_parse_rc_meta(r), tag)]
        not_matched_rows = [r for r in closed_rc if not _tag_matched(_parse_rc_meta(r), tag)]
        tag_def = CANDIDATE_DEFINITIONS.get(tag, {})

        print(f"\n  [{tag}]")
        print(f"  Basis: {tag_def.get('basis', 'n/a')}")

        _print_cohort_block(f"  MATCHED", matched_rows, args.min_n)
        _print_cohort_block(f"  NOT_MATCHED", not_matched_rows, args.min_n)

        neg_summary.append((tag, _cohort_metrics(matched_rows), _cohort_metrics(not_matched_rows)))

    # Summary table for negative tags
    print()
    print("  Negative Tags Summary:")
    print(f"  {'Tag':<22} {'n_matched':>9}  {'TP1%':>6}  {'BAvgR':>7}  "
          f"{'n_not':>6}  {'TP1%_not':>8}")
    print("  " + "-" * 62)
    for tag, mm, nm in neg_summary:
        n_m = mm["n"]
        n_nm = nm["n"]
        tp1_m = mm["tp1_n"] / n_m * 100 if n_m else 0.0
        tp1_nm = nm["tp1_n"] / n_nm * 100 if n_nm else 0.0
        b_r = _r_s(mm["policy_b_avg_r"])
        insuf_m = "*" if n_m < args.min_n else ""
        insuf_nm = "*" if n_nm < args.min_n else ""
        print(f"  {tag:<22} {n_m:>8}{insuf_m}  {tp1_m:>5.1f}%  {b_r:>7}  "
              f"{n_nm:>5}{insuf_nm}  {tp1_nm:>7.1f}%")
    print("  (* = below min_n threshold)")

    # ------------------------------------------------------------------
    # Section 4: Notes and direction concentration
    # ------------------------------------------------------------------
    _print_section("4 — Notes and Prospective Research Limitations")
    print("  1. All results are from dry-run data only (DRY_RUN_MODE=true).")
    print("     No live trades, exchange orders, or real capital at risk.")
    print()
    print("  2. Policy B and E AvgR are accounting simulations, not live P&L.")
    print()
    print("  3. Tags are prospective metadata only. Matching a tag does NOT")
    print("     suppress, filter, or alter any signal in the runtime path.")
    print()
    print("  4. Direction-concentrated [DC] cohorts reflect a regime/direction")
    print("     effect. EMA_GT_0_25 and SYMBOL_WLD were concentrated LONG in 11E.")
    print("     If direction composition changes post-11F, results may differ.")
    print()
    print("  5. Do not use these results to enable any runtime filter without")
    print("     an explicit approval milestone with dedicated safety review.")
    print()
    print("  6. Early results (n < min_n) should not be interpreted.")
    print("     Wait for sufficient sample size before drawing conclusions.")
    print()
    _print_divider()


if __name__ == "__main__":
    main()
