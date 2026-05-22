"""Candidate Gate Prospective Tracking Report — Milestone 11C / 11C.1.

Reads closed CONFIRMED_SETUP signal_features rows that carry 11C candidate
gate metadata (feature_version = '11C_v1') and compares outcome statistics
across gate cohorts.

REPORT-ONLY. Does not write to the database. Does not change signal
generation, alerts, config, or runtime behavior.

This report only becomes meaningful after enough new observations have
accumulated with feature_version = '11C_v1'. Until then, cohort sample
sizes will be small and results should be treated as INSUFFICIENT.

Avg outcome R note (11C.1):
  Expired observations typically have outcome_r = NULL and are therefore
  excluded from Avg outcome R. The report shows coverage as "n=X / N rows"
  so you can see how many rows contributed. Do not treat Avg outcome R as a
  full-population average unless coverage is near 100%.

Direction-concentration note (11C.1):
  If a gate cohort selects predominantly one direction (>=90% LONG or SHORT),
  the report emits a warning. This is important when comparing gate cohorts:
  direction-concentrated cohorts reflect a direction/regime effect, not a
  generally valid gate result.

Usage (run from repo root):
    PYTHONPATH=src python scripts/report_candidate_gates.py
    PYTHONPATH=src python scripts/report_candidate_gates.py --since 2026-06-01T00:00:00Z
    PYTHONPATH=src python scripts/report_candidate_gates.py --min-n 10
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

# -----------------------------------------------------------------------
# sys.path bootstrap — same pattern as other APEX report scripts.
# -----------------------------------------------------------------------
_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent
_SRC_DIR = _REPO_ROOT / "src"
for _p in (str(_REPO_ROOT), str(_SRC_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from apex.config import get_settings
from apex.db.connection import close_db, init_db
from apex.strategy.candidate_gates import GATE_DEFINITIONS, GATE_VERSION
from scripts.report_signal_learning import _outcome_stats, _pct

_DEFAULT_MIN_N = 10

# Threshold above which we flag a cohort as direction-concentrated.
_DIR_CONCENTRATION_PCT = 90.0

# Threshold below which we flag outcome_r coverage as materially partial.
_COVERAGE_WARN_PCT = 80.0

# Cohort definitions: (display_name, gate_key_or_None)
# gate_key_or_None: if None → include all rows with gate version (baseline)
#                   if str  → include rows where that gate passed
_COHORTS = [
    ("BASELINE_ALL_WITH_GATE_VERSION",   None),
    ("GATE_A_ATR_0_10_TO_1_00",          "GATE_A_ATR_0_10_TO_1_00"),
    ("GATE_B_EMA_SPREAD_NEG_0_25_TO_0",  "GATE_B_EMA_SPREAD_NEG_0_25_TO_0"),
    ("GATE_C_RSI_50_TO_60",              "GATE_C_RSI_50_TO_60"),
    ("GATE_D_ATR_AND_EMA_COMBINED",      "GATE_D_ATR_AND_EMA_COMBINED"),
]


# ======================================================================
# Helpers
# ======================================================================

def _parse_gate_meta(row: Any) -> Optional[dict]:
    """Parse metadata_json from a signal_features row. Returns None on error."""
    raw = row["metadata_json"]
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or "gates" not in parsed:
            return None
        return parsed
    except (json.JSONDecodeError, TypeError):
        return None


def _gate_passed(meta: Optional[dict], gate_key: str) -> bool:
    """Return True if the given gate passed in this row's metadata."""
    if meta is None:
        return False
    gates = meta.get("gates", {})
    gate_data = gates.get(gate_key)
    if gate_data is None:
        return False
    return bool(gate_data.get("passed", False))


def _avg_r_num(vals: list[float]) -> Optional[float]:
    return sum(vals) / len(vals) if vals else None


def _direction_pcts(rows: list[Any]) -> tuple[int, int, float, float]:
    """Return (long_n, short_n, long_pct, short_pct)."""
    n = len(rows)
    long_n = sum(1 for r in rows if r["direction"] == "LONG")
    short_n = n - long_n
    long_pct = long_n / n * 100 if n else 0.0
    short_pct = short_n / n * 100 if n else 0.0
    return long_n, short_n, long_pct, short_pct


def _is_direction_concentrated(long_pct: float, short_pct: float) -> bool:
    return long_pct >= _DIR_CONCENTRATION_PCT or short_pct >= _DIR_CONCENTRATION_PCT


def _outcome_r_coverage(st: dict, n: int) -> tuple[int, int]:
    """Return (r_n, r_excluded) — rows with non-null outcome_r, rows excluded."""
    r_n = len(st["out_r_vals"])
    r_excluded = n - r_n
    return r_n, r_excluded


def _print_cohort(label: str, rows: list[Any], min_n: int) -> None:
    """Print outcome statistics for a single cohort — Milestone 11C.1 format."""
    st = _outcome_stats(rows)
    n = st["n"]
    long_n, short_n, long_pct, short_pct = _direction_pcts(rows)
    exp_n = st["expired"]
    r_n, r_excluded = _outcome_r_coverage(st, n)

    warn = ""
    if n == 0:
        warn = "  [NO DATA]"
    elif n < min_n:
        warn = f"  [INSUFFICIENT SAMPLE — n={n} < min_n={min_n}]"

    print(f"  {label}:{warn}")
    if n == 0:
        print(f"    No closed rows in this cohort.")
        print()
        return

    # Direction concentration inline warning
    if _is_direction_concentrated(long_pct, short_pct):
        dominant = "LONG" if long_pct >= _DIR_CONCENTRATION_PCT else "SHORT"
        print(f"    [DIR-CONCENTRATED: {long_n} LONG / {short_n} SHORT — "
              f"results reflect {dominant} regime, not a general gate]")

    print(f"    Rows           : {n}")
    print(f"    TP1            : {st['tp1']:4d}  {_pct(st['tp1'], n)}")
    print(f"    HIT_2R         : {st['tp2']:4d}  {_pct(st['tp2'], n)}")
    print(f"    STOPPED        : {st['stopped']:4d}  {_pct(st['stopped'], n)}")
    print(f"    EXPIRED        : {st['expired']:4d}  {_pct(st['expired'], n)}")
    if exp_n > 0:
        print(f"      after TP1    : {st['exp_after_tp1']:4d}  "
              f"{_pct(st['exp_after_tp1'], exp_n)} of expired")
        print(f"      w/o TP1      : {st['exp_no_tp1']:4d}  "
              f"{_pct(st['exp_no_tp1'], exp_n)} of expired")

    mfe_n = len(st["mfe_vals"])
    mae_n = len(st["mae_vals"])
    avg_mfe_s = f"{_avg_r_num(st['mfe_vals']):+.2f}" if st["mfe_vals"] else "n/a"
    avg_mae_s = f"{_avg_r_num(st['mae_vals']):+.2f}" if st["mae_vals"] else "n/a"
    avg_r_s = f"{_avg_r_num(st['out_r_vals']):+.3f}" if st["out_r_vals"] else "n/a"

    print(f"    Avg MFE        : {avg_mfe_s} R  (n={mfe_n})")
    print(f"    Avg MAE        : {avg_mae_s} R  (n={mae_n})")
    print(f"    Avg outcome R  : {avg_r_s} R  (n={r_n} / {n})")
    print(f"    Outcome R cov  : {r_n} / {n} rows; "
          f"{r_excluded} excluded (outcome_r is null)")
    if r_excluded > 0 and n > 0:
        coverage_pct = r_n / n * 100
        if coverage_pct < _COVERAGE_WARN_PCT:
            print(f"    [PARTIAL COVERAGE: {coverage_pct:.0f}% — "
                  f"compare TP1/STOP/EXPIRED and MFE/MAE alongside Avg outcome R]")
    print(f"    LONG/SHORT     : {long_n} / {short_n}")
    print()


# ======================================================================
# Main
# ======================================================================

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="APEX Candidate Gate Prospective Tracking Report [Milestone 11C] — report-only"
    )
    parser.add_argument(
        "--since",
        metavar="ISO_TIMESTAMP",
        default=None,
        help="Filter features captured at >= TIMESTAMP",
    )
    parser.add_argument(
        "--min-n",
        metavar="N",
        type=int,
        default=_DEFAULT_MIN_N,
        help=f"Minimum closed rows for a non-INSUFFICIENT cohort (default: {_DEFAULT_MIN_N})",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    conn = init_db(settings.apex_db_path)

    try:
        conditions = ["sf.feature_version = ?"]
        params: list[Any] = [GATE_VERSION]
        if args.since:
            conditions.append("sf.captured_at >= ?")
            params.append(args.since)
        where = "WHERE " + " AND ".join(conditions)
        query = f"""
            SELECT sf.*,
                   so.max_favorable_excursion AS so_mfe,
                   so.max_adverse_excursion   AS so_mae
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

    confirmed = [r for r in rows if r["signal_type"] == "CONFIRMED_SETUP"]
    closed = [r for r in confirmed if r["outcome_status"] not in (None, "OBSERVED")]

    # ------------------------------------------------------------------
    # Section 1: Header and disclaimer
    # ------------------------------------------------------------------
    print()
    print("=" * 68)
    print("  APEX Candidate Gate Prospective Tracking Report  [Milestone 11C]")
    print("=" * 68)
    print("  REPORT-ONLY. Does not change scanning, alerts, or config.")
    print("  Candidate gates are DRY-RUN RESEARCH ONLY.")
    print("  Do not use these results to enable any runtime filter.")
    print("  A dedicated approval milestone is required before any gate is")
    print("  applied to live or dry-run signal scanning.")
    print()
    print(f"  Gate version tracked   : {GATE_VERSION}")
    if args.since:
        print(f"  Filtered since         : {args.since}")
    print(f"  11C rows loaded        : {len(rows)}")
    print(f"  Closed confirmed       : {len(closed)}")
    print(f"  Min-N threshold        : {args.min_n}")

    if len(rows) == 0:
        print()
        print("  No 11C candidate gate observations found yet.")
        print("  Results will appear once new observations are captured after")
        print(f"  deploying Milestone 11C (feature_version='{GATE_VERSION}').")
        print()
        print("=" * 68)
        return

    if len(closed) == 0:
        print()
        print("  No closed CONFIRMED_SETUP rows with gate version yet.")
        print("  Results will appear once new 11C observations close.")
        print()
        print("=" * 68)
        return

    # ------------------------------------------------------------------
    # Section 2: Gate definitions
    # ------------------------------------------------------------------
    print()
    print("=" * 68)
    print("  Candidate gate definitions")
    print("=" * 68)
    print()
    for gate_key, rule in GATE_DEFINITIONS.items():
        print(f"  {gate_key:<44}  {rule}")

    # ------------------------------------------------------------------
    # Section 3: Per-cohort outcome statistics
    # ------------------------------------------------------------------
    print()
    print("=" * 68)
    print("  Cohort outcome statistics")
    print("=" * 68)
    print("  Only closed CONFIRMED_SETUP rows with gate version are included.")
    print("  Open / OBSERVED rows are excluded (outcomes not yet known).")
    print()
    print("  NOTE on Avg outcome R:")
    print("  Expired observations typically have outcome_r = NULL and are")
    print("  excluded from Avg outcome R. Coverage shows how many rows")
    print("  contributed. Do not read Avg outcome R as a full-population")
    print("  average when coverage is low.")
    print()
    print("  NOTE on expired-after-TP1 vs expired-without-TP1:")
    print("  'after TP1'  = reached 1R milestone but did not hit 2R before expiry.")
    print("                 Partial success — entry was correct but exit timing failed.")
    print("  'w/o TP1'    = did not reach 1R before expiry.")
    print("                 Full miss for the observation window.")
    print()

    # Pre-parse all metadata to avoid repeated JSON parsing
    row_metas = [_parse_gate_meta(r) for r in closed]

    cohort_rows: dict[str, list] = {}
    for cohort_name, gate_key in _COHORTS:
        if gate_key is None:
            cohort_rows[cohort_name] = list(closed)
        else:
            cohort_rows[cohort_name] = [
                r for r, meta in zip(closed, row_metas)
                if _gate_passed(meta, gate_key)
            ]

    for cohort_name, gate_key in _COHORTS:
        _print_cohort(cohort_name, cohort_rows[cohort_name], args.min_n)

    # ------------------------------------------------------------------
    # Section 4: Summary comparison table
    # ------------------------------------------------------------------
    print("=" * 68)
    print("  Summary comparison (vs baseline)")
    print("=" * 68)
    print()
    print("  Columns: N=cohort rows, TP1%/Δ, STP%/Δ, AvgOutR=Avg outcome R,")
    print("  R_n=rows contributing to AvgOutR, L/S=LONG count / SHORT count.")
    print("  [DC] = direction-concentrated (>=90% one direction).")
    print()

    baseline_rows = cohort_rows["BASELINE_ALL_WITH_GATE_VERSION"]
    baseline_st = _outcome_stats(baseline_rows)
    b_n = baseline_st["n"]
    b_tp1_r = baseline_st["tp1"] / b_n if b_n else 0.0
    b_stop_r = baseline_st["stopped"] / b_n if b_n else 0.0

    def _delta_pp(kept_count: int, kept_n: int, base_rate: float) -> str:
        if kept_n == 0:
            return "  n/a"
        return f"{(kept_count / kept_n - base_rate) * 100:+.1f}pp"

    hdr = (
        f"  {'Cohort':<43} {'N':>4} "
        f"{'TP1%':>5} {'TP1Δ':>7} "
        f"{'STP%':>5} {'STPΔ':>7} "
        f"{'AvgOutR':>8} {'R_n':>8}  {'L/S'}"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for cohort_name, gate_key in _COHORTS:
        cr = cohort_rows[cohort_name]
        st = _outcome_stats(cr)
        n = st["n"]
        tp1_pct = st["tp1"] / n * 100 if n else 0.0
        stop_pct = st["stopped"] / n * 100 if n else 0.0
        avg_r = _avg_r_num(st["out_r_vals"])
        r_n = len(st["out_r_vals"])
        d_tp1 = _delta_pp(st["tp1"], n, b_tp1_r)
        d_stp = _delta_pp(st["stopped"], n, b_stop_r)
        avg_r_s = f"{avg_r:+.3f}" if avg_r is not None else "    n/a"
        long_n, short_n, long_pct, short_pct = _direction_pcts(cr)
        dc_flag = " [DC]" if _is_direction_concentrated(long_pct, short_pct) else ""
        is_baseline = gate_key is None
        suffix = " ← baseline" if is_baseline else ""
        r_n_s = f"{r_n}/{n}"
        ls_s = f"{long_n}/{short_n}"
        print(
            f"  {(cohort_name[:42] + dc_flag)[:43]:<43} {n:>4} "
            f"{tp1_pct:>4.1f}% {d_tp1:>7} "
            f"{stop_pct:>4.1f}% {d_stp:>7} "
            f"{avg_r_s:>8} {r_n_s:>8}  {ls_s}{suffix}"
        )

    # ------------------------------------------------------------------
    # Section 5: Interpretation
    # ------------------------------------------------------------------
    print()
    print("=" * 68)
    print("  Interpretation")
    print("=" * 68)
    print()

    total_with_meta = sum(1 for m in row_metas if m is not None)
    total_without_meta = len(closed) - total_with_meta

    print(f"  {len(closed)} closed rows with gate version '{GATE_VERSION}'.")
    if total_without_meta > 0:
        print(f"  {total_without_meta} row(s) missing gate metadata (None metadata_json).")

    any_pass_n = sum(1 for m in row_metas if m is not None and m.get("candidate_gate_passed"))
    print(f"  {any_pass_n} row(s) pass at least one gate ({_pct(any_pass_n, len(closed))}).")
    print()

    # Outcome R coverage note
    baseline_st2 = _outcome_stats(baseline_rows)
    b_r_n, b_r_excl = _outcome_r_coverage(baseline_st2, len(baseline_rows))
    print("  Avg outcome R — coverage note:")
    print(f"  Baseline: {b_r_n} / {len(baseline_rows)} rows have non-null outcome_r.")
    if b_r_excl > 0:
        print(f"  {b_r_excl} baseline rows are excluded from Avg outcome R because")
        print("  outcome_r is null (typically EXPIRED rows).")
        b_cov_pct = b_r_n / len(baseline_rows) * 100 if baseline_rows else 0.0
        if b_cov_pct < _COVERAGE_WARN_PCT:
            print(f"  Coverage is {b_cov_pct:.0f}% — Avg outcome R reflects only STOPPED and")
            print("  HIT_2R rows. Compare TP1/STOP/EXPIRED rates and MFE/MAE alongside it.")
    print()

    # Direction-concentration warnings
    dir_warnings = []
    for cohort_name, gate_key in _COHORTS[1:]:  # skip baseline
        cr = cohort_rows[cohort_name]
        if not cr:
            continue
        long_n, short_n, long_pct, short_pct = _direction_pcts(cr)
        if _is_direction_concentrated(long_pct, short_pct):
            dominant = "LONG" if long_pct >= _DIR_CONCENTRATION_PCT else "SHORT"
            dir_warnings.append((cohort_name, long_n, short_n, dominant))

    if dir_warnings:
        print("  Direction-concentration warnings:")
        for cohort_name, long_n, short_n, dominant in dir_warnings:
            print(f"  {cohort_name}:")
            print(f"    {long_n} LONG / {short_n} SHORT — {dominant} only in this window.")
            print(f"    Treat results as {dominant}-regime evidence, not a general gate result.")
            print(f"    The gate's apparent performance may reflect direction/regime bias,")
            print(f"    not a broadly applicable signal improvement.")
        print()

    # Partial-success explanation
    print("  Expired-after-TP1 vs expired-without-TP1:")
    print("  'Expired after TP1' means the signal reached the 1R milestone but")
    print("  the trade did not hit 2R before the observation window expired.")
    print("  This is a partial success: entry direction was correct but exit")
    print("  timing or target spacing may need adjustment.")
    print("  'Expired without TP1' means the signal never reached 1R before")
    print("  expiry — a full miss within the observation window.")
    print("  These are qualitatively different outcomes and should not be")
    print("  collapsed into a single 'expired is bad' reading.")
    print()

    # Sample size summary
    for cohort_name, gate_key in _COHORTS[1:]:
        cr = cohort_rows[cohort_name]
        n = len(cr)
        if n < args.min_n:
            print(f"  {cohort_name}: n={n} — INSUFFICIENT SAMPLE (need >= {args.min_n}).")
        else:
            print(f"  {cohort_name}: n={n} — sufficient for initial comparison.")
    print()

    print("  IMPORTANT: These results are prospective dry-run tracking only.")
    print("  Do not draw conclusions from < 20 rows per cohort.")
    print("  A consistent improvement over 50+ rows is required before any")
    print("  gate is promoted to a runtime filter (requires explicit milestone).")
    print()
    print("=" * 68)


if __name__ == "__main__":
    main()
