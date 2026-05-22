"""Exit Policy Simulator — Milestone 11D.

Reads closed CONFIRMED_SETUP signal_features + signal_observations data and
compares alternate exit / accounting policies without changing any runtime
behavior.

REPORT-ONLY SIMULATION.
Does not change scanning, alerts, gates, or trading.
Does not write to the database.
Uses historical dry-run observations only.

Purpose: answer "Given observed dry-run signal behavior, would different exit
accounting or timing rules have produced better research outcomes?"

Usage (run from repo root):
    PYTHONPATH=src python scripts/simulate_exit_policies.py
    PYTHONPATH=src python scripts/simulate_exit_policies.py --since 2026-05-21T19:47:48Z
    PYTHONPATH=src python scripts/simulate_exit_policies.py --since 2026-05-21T19:47:48Z --min-n 10
    PYTHONPATH=src python scripts/simulate_exit_policies.py --feature-version 11C_v1
    PYTHONPATH=src python scripts/simulate_exit_policies.py --signal-type CONFIRMED_SETUP
"""
from __future__ import annotations

import argparse
import statistics
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
from scripts.report_signal_learning import _outcome_stats, _pct

# -----------------------------------------------------------------------
# Policy E accounting constants — explicit, named, easy to audit.
# Changing these does not affect any runtime behavior.
# -----------------------------------------------------------------------
_POLICY_E_EXP_AFTER_TP1_R = 0.5    # approximate R for expired-after-TP1
_POLICY_E_EXP_NO_TP1_R = -0.25     # approximate R for expired-without-TP1

# Timing buckets for Policy F analysis (seconds)
_TIMING_BUCKETS_SECS = [
    (5 * 60,  "<=5m"),
    (10 * 60, "<=10m"),
    (15 * 60, "<=15m"),
    (30 * 60, "<=30m"),
]

_DEFAULT_MIN_N = 10
_DEFAULT_SIGNAL_TYPE = "CONFIRMED_SETUP"


# ======================================================================
# Pure helper functions
# ======================================================================

def _avg(vals: list[float]) -> Optional[float]:
    return sum(vals) / len(vals) if vals else None


def _median(vals: list[float]) -> Optional[float]:
    return statistics.median(vals) if vals else None


def _percentile(vals: list[float], p: int) -> Optional[float]:
    """Simple percentile using nearest-rank method."""
    if not vals:
        return None
    s = sorted(vals)
    idx = max(0, int(len(s) * p / 100) - 1)
    return s[idx]


def _avg_s(val: Optional[float], suffix: str = "R") -> str:
    return f"{val:+.3f} {suffix}" if val is not None else f"n/a {suffix}"


def _n_s(n: int, total: int) -> str:
    return f"{n} / {total}"


def _pct_s(n: int, total: int) -> str:
    if total == 0:
        return "  n/a"
    return f"{n / total * 100:5.1f}%"


def _has_tp1(row: Any) -> bool:
    """Row reached TP1 at some point (hit_1r_at is not null)."""
    return row["hit_1r_at"] is not None


def _is_hit2r(row: Any) -> bool:
    return row["outcome_status"] == "HIT_2R"


def _is_stopped(row: Any) -> bool:
    return row["outcome_status"] == "STOPPED"


def _is_expired(row: Any) -> bool:
    return row["outcome_status"] == "EXPIRED"


def _stopped_without_tp1(row: Any) -> bool:
    return _is_stopped(row) and not _has_tp1(row)


def _expired_after_tp1(row: Any) -> bool:
    return _is_expired(row) and row["hit_1r_before_expiry"] == 1


def _expired_without_tp1(row: Any) -> bool:
    return _is_expired(row) and row["hit_1r_before_expiry"] != 1


# ======================================================================
# Policy simulators — return list of simulated R values
# ======================================================================

def _policy_a_r(row: Any) -> Optional[float]:
    """Policy A: current recorded outcome_r. Returns None if not available."""
    return row["outcome_r"]


def _policy_b_r(row: Any) -> float:
    """Policy B: TP1 scalp accounting.
    - Any row that reached TP1 → +1.0R
    - Stopped without TP1 → -1.0R
    - Expired without TP1 → 0.0R (neutral miss)
    """
    if _has_tp1(row):
        return 1.0
    if _is_stopped(row):
        return -1.0
    return 0.0  # expired without TP1


def _policy_c_r(row: Any) -> float:
    """Policy C: TP1-then-breakeven approximation.
    - Reached TP1 → +1.0R (approximate: assumes at minimum breakeven post-TP1)
    - Stopped before TP1 → -1.0R
    - Expired without TP1 → 0.0R
    NOTE: This is APPROXIMATE. Post-TP1 path is not reconstructed from data.
    The actual outcome after TP1 may differ; this simulates a 'move stop to BE' exit.
    """
    if _has_tp1(row):
        return 1.0
    if _is_stopped(row):
        return -1.0
    return 0.0


def _policy_d_r_strict(row: Any) -> Optional[float]:
    """Policy D: strict 2R only — treats EXPIRED as excluded (None)."""
    if _is_hit2r(row):
        return 2.0
    if _is_stopped(row):
        return -1.0
    return None  # EXPIRED excluded


def _policy_d_r_with_zero(row: Any) -> float:
    """Policy D variant: EXPIRED counted as 0.0R."""
    if _is_hit2r(row):
        return 2.0
    if _is_stopped(row):
        return -1.0
    return 0.0  # EXPIRED as neutral


def _policy_e_r(row: Any) -> float:
    """Policy E: expire-without-TP1 as small loss.
    - HIT_2R → +2.0R
    - STOPPED → -1.0R
    - Expired after TP1 → +0.5R (POLICY_E_EXP_AFTER_TP1_R)
    - Expired without TP1 → -0.25R (POLICY_E_EXP_NO_TP1_R)
    ACCOUNTING SIMULATION ONLY. Not actual trade execution.
    Constants defined at top of script.
    """
    if _is_hit2r(row):
        return 2.0
    if _is_stopped(row):
        return -1.0
    if _expired_after_tp1(row):
        return _POLICY_E_EXP_AFTER_TP1_R
    return _POLICY_E_EXP_NO_TP1_R


# ======================================================================
# Reporting helpers
# ======================================================================

def _print_divider(char: str = "=", width: int = 68) -> None:
    print(char * width)


def _print_section(title: str) -> None:
    print()
    _print_divider()
    print(f"  {title}")
    _print_divider()


def _print_baseline_block(rows: list[Any], label: str) -> None:
    """Print detailed outcome statistics for a row set."""
    st = _outcome_stats(rows)
    n = st["n"]
    long_n = sum(1 for r in rows if r["direction"] == "LONG")
    short_n = n - long_n
    exp_n = st["expired"]
    r_n = len(st["out_r_vals"])
    r_excl = n - r_n
    mfe_n = len(st["mfe_vals"])
    mae_n = len(st["mae_vals"])

    avg_mfe = _avg(st["mfe_vals"])
    avg_mae = _avg(st["mae_vals"])
    avg_r = _avg(st["out_r_vals"])

    print(f"  {label}")
    print(f"    Rows           : {n}")
    print(f"    TP1 milestone  : {st['tp1']:4d}  {_pct_s(st['tp1'], n)}")
    print(f"    HIT_2R         : {st['tp2']:4d}  {_pct_s(st['tp2'], n)}")
    print(f"    STOPPED        : {st['stopped']:4d}  {_pct_s(st['stopped'], n)}")
    print(f"    EXPIRED        : {st['expired']:4d}  {_pct_s(st['expired'], n)}")
    if exp_n > 0:
        print(f"      after TP1    : {st['exp_after_tp1']:4d}  "
              f"{_pct_s(st['exp_after_tp1'], exp_n)} of expired")
        print(f"      w/o  TP1     : {st['exp_no_tp1']:4d}  "
              f"{_pct_s(st['exp_no_tp1'], exp_n)} of expired")
    print(f"    Avg MFE        : {_avg_s(avg_mfe)}  (n={mfe_n})")
    print(f"    Avg MAE        : {_avg_s(avg_mae)}  (n={mae_n})")
    print(f"    Avg outcome R  : {_avg_s(avg_r)}  (n={r_n} / {n})")
    print(f"    Outcome R cov  : {r_n} / {n}; {r_excl} excluded (outcome_r null)")
    print(f"    LONG / SHORT   : {long_n} / {short_n}")
    print()


def _simulate_policy(rows: list[Any], r_fn, exclude_none: bool = False) -> tuple[list[float], int]:
    """Apply r_fn to each row; return (r_vals, excluded_count).

    If exclude_none=True, rows returning None are excluded.
    Otherwise None is treated as 0.0 (caller decides).
    """
    r_vals = []
    excluded = 0
    for row in rows:
        v = r_fn(row)
        if v is None:
            excluded += 1
        else:
            r_vals.append(v)
    return r_vals, excluded


def _wins_losses(r_vals: list[float]) -> tuple[int, int, int]:
    """Return (wins, losses, neutral) where win > 0, loss < 0, neutral == 0."""
    wins = sum(1 for v in r_vals if v > 0)
    losses = sum(1 for v in r_vals if v < 0)
    neutral = sum(1 for v in r_vals if v == 0)
    return wins, losses, neutral


# ======================================================================
# Timing analysis
# ======================================================================

def _timing_bucket_analysis(rows: list[Any]) -> None:
    """Policy F + G: timing analysis of TP1 reach time and early-failure."""
    tp1_rows = [r for r in rows if _has_tp1(r)]
    tp1_times = [r["time_to_1r_seconds"] for r in tp1_rows if r["time_to_1r_seconds"] is not None]

    print("  Policy G — TP1 Time-to-Hit Analysis")
    print(f"    Rows that reached TP1 : {len(tp1_rows)}")
    if not tp1_times:
        print("    No time_to_1r_seconds data available.")
        print()
    else:
        avg_t = _avg(tp1_times)
        med_t = _median(tp1_times)
        p25 = _percentile(tp1_times, 25)
        p75 = _percentile(tp1_times, 75)
        print(f"    n with timing data    : {len(tp1_times)}")
        print(f"    Avg time to TP1       : {avg_t / 60:.1f} min" if avg_t else "    Avg time to TP1       : n/a")
        print(f"    Median time to TP1    : {med_t / 60:.1f} min" if med_t else "    Median time to TP1    : n/a")
        print(f"    P25 / P75             : "
              f"{p25 / 60:.1f} min / {p75 / 60:.1f} min" if (p25 and p75) else "    P25 / P75             : n/a")
        print()
        print("    TP1 within time bucket:")
        prev_label = "start"
        prev_n = 0
        for secs, label in _TIMING_BUCKETS_SECS:
            bucket_n = sum(1 for t in tp1_times if t <= secs)
            new_this_bucket = bucket_n - prev_n
            print(f"      {label:<8}: {bucket_n:4d} cumulative  ({new_this_bucket:+d} vs {prev_label})")
            prev_label = label
            prev_n = bucket_n
        after_30 = sum(1 for t in tp1_times if t > 30 * 60)
        print(f"      >30m    : {after_30:4d} cumulative")
    print()

    print("  Policy F — Early-Failure Timeout Analysis")
    non_tp1_rows = [r for r in rows if not _has_tp1(r)]
    stop_times = [r["time_to_stop_seconds"] for r in non_tp1_rows if r["time_to_stop_seconds"] is not None]
    exp_times = [r["time_to_expiry_seconds"] for r in non_tp1_rows if r["time_to_expiry_seconds"] is not None]

    if not stop_times and not exp_times:
        print("    Insufficient timing data for non-TP1 rows.")
        print()
        return

    print(f"    Non-TP1 rows          : {len(non_tp1_rows)}")
    print()
    print("    How quickly did non-TP1 signals fail?")
    print("    (For stopped rows: time_to_stop_seconds; for expired: time_to_expiry_seconds)")
    print()

    failure_times = stop_times + exp_times
    if failure_times:
        avg_f = _avg(failure_times)
        med_f = _median(failure_times)
        print(f"    n with timing data    : {len(failure_times)}")
        print(f"    Avg failure time      : {avg_f / 60:.1f} min" if avg_f else "    Avg failure time      : n/a")
        print(f"    Median failure time   : {med_f / 60:.1f} min" if med_f else "    Median failure time   : n/a")
        print()
        print("    Failures by bucket (cumulative):")
        prev_n = 0
        for secs, label in _TIMING_BUCKETS_SECS:
            bucket_n = sum(1 for t in failure_times if t <= secs)
            new_n = bucket_n - prev_n
            pct = bucket_n / len(failure_times) * 100
            print(f"      {label:<8}: {bucket_n:4d} ({pct:4.1f}% of failures)  ({new_n:+d})")
            prev_n = bucket_n
        after_30 = sum(1 for t in failure_times if t > 30 * 60)
        print(f"      >30m    : {after_30:4d} ({after_30 / len(failure_times) * 100:.1f}%)")
    print()


# ======================================================================
# Main
# ======================================================================

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="APEX Exit Policy Simulator [Milestone 11D] — report-only"
    )
    parser.add_argument(
        "--since",
        metavar="ISO_TIMESTAMP",
        default=None,
        help="Filter features captured_at >= TIMESTAMP",
    )
    parser.add_argument(
        "--min-n",
        metavar="N",
        type=int,
        default=_DEFAULT_MIN_N,
        help=f"Minimum rows for a meaningful result (default: {_DEFAULT_MIN_N})",
    )
    parser.add_argument(
        "--feature-version",
        metavar="VERSION",
        default=None,
        help="Filter by feature_version (e.g. 11C_v1). Default: all versions.",
    )
    parser.add_argument(
        "--signal-type",
        metavar="TYPE",
        default=_DEFAULT_SIGNAL_TYPE,
        help=f"Signal type to analyse (default: {_DEFAULT_SIGNAL_TYPE})",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    conn = init_db(settings.apex_db_path)

    try:
        conditions: list[str] = ["sf.signal_type = ?"]
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

    closed = [r for r in rows if r["outcome_status"] not in (None, "OBSERVED")]
    long_rows = [r for r in closed if r["direction"] == "LONG"]
    short_rows = [r for r in closed if r["direction"] == "SHORT"]

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------
    print()
    _print_divider()
    print("  APEX Exit Policy Simulator  [Milestone 11D]")
    _print_divider()
    print("  REPORT-ONLY SIMULATION.")
    print("  Does not change scanning, alerts, gates, or trading.")
    print("  Does not write to the database.")
    print("  Uses historical dry-run observations only.")
    print()
    print(f"  Signal type            : {args.signal_type}")
    if args.since:
        print(f"  Filtered since         : {args.since}")
    if args.feature_version:
        print(f"  Feature version filter : {args.feature_version}")
    print(f"  Total loaded rows      : {len(rows)}")
    print(f"  Closed rows            : {len(closed)}")
    print(f"  Min-N threshold        : {args.min_n}")

    if len(closed) == 0:
        print()
        print("  No closed rows available. Exiting.")
        print()
        _print_divider()
        return

    # ------------------------------------------------------------------
    # Section 2: Baseline recorded outcomes
    # ------------------------------------------------------------------
    _print_section("Baseline Recorded Outcomes")
    print("  This section uses the recorded outcome fields exactly as stored.")
    print("  outcome_r is populated only for STOPPED and HIT_2R rows; EXPIRED")
    print("  rows typically have outcome_r = NULL.")
    print()
    _print_baseline_block(closed, "All closed confirmed rows")

    if long_rows or short_rows:
        _print_baseline_block(long_rows, "LONG only")
        _print_baseline_block(short_rows, "SHORT only")

    # ------------------------------------------------------------------
    # Section 3: Policy assumptions
    # ------------------------------------------------------------------
    _print_section("Policy Assumptions")
    print("  Each policy reinterprets recorded outcome fields under a different")
    print("  accounting model. No new data is inferred or fabricated.")
    print()
    print(f"  Policy A  CURRENT_RECORDED_OUTCOME")
    print(f"    Uses recorded outcome_r. NULL where not populated (EXPIRED).")
    print()
    print(f"  Policy B  TP1_SCALP_ACCOUNTING")
    print(f"    TP1 reached → +1.0R  (any row with hit_1r_at not null)")
    print(f"    Stopped w/o TP1 → -1.0R")
    print(f"    Expired w/o TP1 → 0.0R (neutral miss, not a loss)")
    print(f"    Tests: is APEX better as a 1R scalp than a 2R target system?")
    print()
    print(f"  Policy C  TP1_THEN_BREAKEVEN_APPROX")
    print(f"    TP1 reached → +1.0R  (approximate 'move stop to BE' exit)")
    print(f"    Stopped before TP1 → -1.0R")
    print(f"    Expired without TP1 → 0.0R")
    print(f"    APPROXIMATION: post-TP1 path is not reconstructed from data.")
    print()
    print(f"  Policy D  STRICT_2R_ONLY")
    print(f"    HIT_2R → +2.0R")
    print(f"    STOPPED → -1.0R")
    print(f"    EXPIRED → excluded (D-strict) OR 0.0R (D-with-zero)")
    print()
    print(f"  Policy E  EXPIRE_PARTIAL_CREDIT")
    print(f"    HIT_2R → +2.0R")
    print(f"    STOPPED → -1.0R")
    print(f"    Expired after TP1 → +{_POLICY_E_EXP_AFTER_TP1_R:.2f}R  (ACCOUNTING CONSTANT)")
    print(f"    Expired w/o TP1   → {_POLICY_E_EXP_NO_TP1_R:+.2f}R  (ACCOUNTING CONSTANT)")
    print(f"    ACCOUNTING SIMULATION ONLY — not actual trade execution.")

    # ------------------------------------------------------------------
    # Section 4: Simulated policy comparison table
    # ------------------------------------------------------------------
    _print_section("Simulated Policy Comparison")
    print("  Simulated avg R for each accounting policy.")
    print("  Coverage = rows contributing to avg R / total rows.")
    print()

    def _policy_row(label: str, rows_in: list[Any], r_fn, notes: str = "",
                    exclude_none: bool = False) -> None:
        r_vals, excl = _simulate_policy(rows_in, r_fn, exclude_none=exclude_none)
        n = len(rows_in)
        total_counted = len(r_vals)
        avg = _avg(r_vals)
        wins, losses, neutral = _wins_losses(r_vals)
        avg_s = f"{avg:+.3f}" if avg is not None else "  n/a"
        cov = f"{total_counted}/{n}"
        print(f"  {label:<34} N={n:>4}  AvgR={avg_s}  "
              f"W={wins} L={losses} ~={neutral}  cov={cov}  {notes}")

    # Policy A: recorded outcome_r (non-null only)
    _policy_row("A  CURRENT_RECORDED",      closed, _policy_a_r, "recorded outcome_r only", exclude_none=True)
    _policy_row("B  TP1_SCALP",             closed, _policy_b_r, "1R scalp")
    _policy_row("C  TP1_BREAKEVEN_APPROX",  closed, _policy_c_r, "approx BE post-TP1")
    _policy_row("D  STRICT_2R_EXCL_EXP",    closed, _policy_d_r_strict, "expired excluded", exclude_none=True)
    _policy_row("D2 STRICT_2R_EXP_AS_0",    closed, _policy_d_r_with_zero, "expired=0")
    _policy_row("E  EXPIRE_PARTIAL_CREDIT",  closed, _policy_e_r, f"exp_after_tp1={_POLICY_E_EXP_AFTER_TP1_R:+.2f} exp_no_tp1={_POLICY_E_EXP_NO_TP1_R:+.2f}")

    if long_rows:
        print()
        print("  --- LONG only ---")
        _policy_row("A  CURRENT_RECORDED",      long_rows, _policy_a_r, "recorded", exclude_none=True)
        _policy_row("B  TP1_SCALP",             long_rows, _policy_b_r, "1R scalp")
        _policy_row("E  EXPIRE_PARTIAL_CREDIT",  long_rows, _policy_e_r, "partial credit")

    if short_rows:
        print()
        print("  --- SHORT only ---")
        _policy_row("A  CURRENT_RECORDED",      short_rows, _policy_a_r, "recorded", exclude_none=True)
        _policy_row("B  TP1_SCALP",             short_rows, _policy_b_r, "1R scalp")
        _policy_row("E  EXPIRE_PARTIAL_CREDIT",  short_rows, _policy_e_r, "partial credit")

    # ------------------------------------------------------------------
    # Section 5: Timing analysis
    # ------------------------------------------------------------------
    _print_section("Timing Analysis (Policies F and G)")
    print("  Uses time_to_1r_seconds, time_to_stop_seconds,")
    print("  time_to_expiry_seconds from signal_observations.")
    print("  Rows missing timing fields are excluded from time averages.")
    print()
    _timing_bucket_analysis(closed)

    # ------------------------------------------------------------------
    # Section 6: Direction breakdown
    # ------------------------------------------------------------------
    _print_section("Direction Breakdown")
    print("  LONG vs SHORT comparison under Policy B (TP1 scalp accounting)")
    print("  to make direction effects visible alongside exit policy effects.")
    print()

    for dir_label, dir_rows in [("LONG", long_rows), ("SHORT", short_rows)]:
        n = len(dir_rows)
        if n == 0:
            print(f"  {dir_label}: no closed rows.")
            print()
            continue
        tp1_n = sum(1 for r in dir_rows if _has_tp1(r))
        stop_no_tp1 = sum(1 for r in dir_rows if _stopped_without_tp1(r))
        exp_no_tp1 = sum(1 for r in dir_rows if _expired_without_tp1(r))
        exp_after_tp1 = sum(1 for r in dir_rows if _expired_after_tp1(r))
        r_b, _ = _simulate_policy(dir_rows, _policy_b_r)
        r_e, _ = _simulate_policy(dir_rows, _policy_e_r)
        print(f"  {dir_label}  (n={n})")
        print(f"    TP1 reached      : {tp1_n:4d}  {_pct_s(tp1_n, n)}")
        print(f"    Stopped w/o TP1  : {stop_no_tp1:4d}  {_pct_s(stop_no_tp1, n)}")
        print(f"    Expired after TP1: {exp_after_tp1:4d}  {_pct_s(exp_after_tp1, n)}")
        print(f"    Expired w/o TP1  : {exp_no_tp1:4d}  {_pct_s(exp_no_tp1, n)}")
        print(f"    Avg R (Policy B) : {_avg_s(_avg(r_b))}")
        print(f"    Avg R (Policy E) : {_avg_s(_avg(r_e))}")
        if n < args.min_n:
            print(f"    [INSUFFICIENT SAMPLE: n={n} < min_n={args.min_n}]")
        print()

    # ------------------------------------------------------------------
    # Section 7: Simulation limitations
    # ------------------------------------------------------------------
    _print_section("Simulation Limitations")
    print()
    print("  1. This simulator uses recorded dry-run observations only.")
    print("     It does not reconstruct candle-by-candle or tick-by-tick price paths.")
    print()
    print("  2. Policies B and C assign +1.0R to all TP1-reaching rows regardless")
    print("     of what happened after TP1. The actual post-TP1 outcome (TP2 hit,")
    print("     stopped, or expired) is known from the data but these policies")
    print("     assume a 1R take-profit would have been executed at TP1.")
    print()
    print("  3. Policy E accounting constants (+{:.2f}R / {:+.2f}R) are arbitrary".format(
        _POLICY_E_EXP_AFTER_TP1_R, _POLICY_E_EXP_NO_TP1_R))
    print("     assumptions. They are not derived from candle data.")
    print()
    print("  4. All policies assume all signals were taken with equal size.")
    print("     In practice slippage, position sizing, and entry execution would differ.")
    print()
    print("  5. Timing analysis uses recorded timestamps (hit_1r_at, stopped_at,")
    print("     expired_at) from signal_observations. Rows without timestamps")
    print("     are excluded from time averages. These timestamps reflect evaluation")
    print("     pass frequency, not exact tick-level events.")
    print()
    print("  6. Results are research-only. They cannot be used to enable trading,")
    print("     alerts, or any runtime behavior change.")

    # ------------------------------------------------------------------
    # Section 8: Interpretation
    # ------------------------------------------------------------------
    _print_section("Interpretation")
    print()
    n = len(closed)
    st = _outcome_stats(closed)
    tp1_pct = st["tp1"] / n * 100 if n else 0.0
    exp_pct = st["expired"] / n * 100 if n else 0.0

    b_vals, _ = _simulate_policy(closed, _policy_b_r)
    avg_b = _avg(b_vals)
    e_vals, _ = _simulate_policy(closed, _policy_e_r)
    avg_e = _avg(e_vals)
    a_vals, _ = _simulate_policy(closed, _policy_a_r, exclude_none=True)
    avg_a = _avg(a_vals)

    print(f"  Baseline: {n} closed rows. TP1 rate: {tp1_pct:.1f}%. Expiry rate: {exp_pct:.1f}%.")
    print()
    if avg_a is not None:
        print(f"  Policy A (recorded outcome_r): {avg_a:+.3f}R avg "
              f"(n={len(a_vals)} / {n} rows have non-null outcome_r)")
        print("  EXPIRED rows are excluded — this understates the true per-observation R.")
    print()
    if avg_b is not None:
        print(f"  Policy B (TP1 scalp): {avg_b:+.3f}R avg across all {n} rows.")
        if avg_b > 0:
            print("  Positive under B: treating TP1 as full exit produces positive expectancy.")
        elif avg_b < 0:
            print("  Negative under B: even TP1-only accounting does not produce positive expectancy.")
        else:
            print("  Zero under B: TP1 rate exactly offsets stops + neutral-misses.")
    print()
    if avg_e is not None:
        print(f"  Policy E (partial credit): {avg_e:+.3f}R avg.")
        print(f"  This assigns partial credit (+{_POLICY_E_EXP_AFTER_TP1_R:.2f}R) for expired-after-TP1")
        print(f"  and a small loss ({_POLICY_E_EXP_NO_TP1_R:+.2f}R) for expired-without-TP1.")
    print()
    print("  The high expiry rate is the dominant research finding. Before considering")
    print("  entry filters or target changes, investigate whether the expiration window")
    print("  is too short for the observed setup resolution times.")
    print()
    print("  All findings are dry-run research only.")
    print("  No runtime behavior, alerts, or trading are affected by this report.")
    print()
    _print_divider()


if __name__ == "__main__":
    main()
