"""Exit / Timing Cohort Analysis — Milestone 11E.

Reads closed CONFIRMED_SETUP signal_features + signal_observations data and
answers: "Which cohorts produce fast TP1 and avoid expired-without-TP1?"

Eight cohort dimensions:
  A  Overall baseline
  B  Direction (LONG / SHORT)
  C  Symbol (top N by count, quality-ranked)
  D  UTC hour (grouped by observed_at hour)
  E  Session (ASIA 00-07 UTC, LONDON 08-12, US 13-20, LATE_US 21-23)
  F  Candidate gate (metadata_json from 11C)
  G  Indicator buckets (RSI, ATR%, EMA spread%, price vs VWAP%)
  H  Time-to-TP1 cohorts (<=5m, 5-10m, 10-15m, >15m, NO_TP1)

Each cohort shows: TP1%, HIT_2R%, STOPPED%, EXPIRED%, expired-after-TP1%,
expired-without-TP1%, Policy B AvgR, Policy E AvgR, Avg MFE, Avg MAE,
median time-to-TP1, LONG/SHORT counts, direction-concentration warnings,
sample-size warnings.

REPORT-ONLY.
Does not change scanning, alerts, gates, or trading.
Does not write to the database.
Uses historical dry-run observations only.

Usage (run from repo root):
    PYTHONPATH=src python scripts/analyze_exit_timing_cohorts.py
    PYTHONPATH=src python scripts/analyze_exit_timing_cohorts.py \\
        --since 2026-05-21T19:47:48Z --feature-version 11C_v1
    PYTHONPATH=src python scripts/analyze_exit_timing_cohorts.py --min-n 5
    PYTHONPATH=src python scripts/analyze_exit_timing_cohorts.py --top-symbols 10
"""
from __future__ import annotations

import argparse
import json
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
from scripts.simulate_exit_policies import (
    _avg,
    _expired_after_tp1,
    _expired_without_tp1,
    _has_tp1,
    _is_hit2r,
    _is_stopped,
    _median,
    _percentile,
    _policy_b_r,
    _policy_e_r,
    _stopped_without_tp1,
)

_DEFAULT_MIN_N = 10
_DEFAULT_SIGNAL_TYPE = "CONFIRMED_SETUP"
_DEFAULT_TOP_SYMBOLS = 8

# Direction-concentration threshold (%)
_DIR_CONCENTRATION_PCT = 90.0

# RSI / ATR% / EMA spread% / price-vs-VWAP% bucket boundaries
_RSI_BUCKETS = [
    (None, 40.0, "RSI < 40"),
    (40.0, 50.0, "RSI 40-50"),
    (50.0, 60.0, "RSI 50-60"),
    (60.0, 70.0, "RSI 60-70"),
    (70.0, None, "RSI > 70"),
]
_ATR_BUCKETS = [
    (None, 0.20, "ATR% < 0.20"),
    (0.20, 0.50, "ATR% 0.20-0.50"),
    (0.50, 1.00, "ATR% 0.50-1.00"),
    (1.00, None, "ATR% > 1.00"),
]
_EMA_SPREAD_BUCKETS = [
    (None, -0.50, "EMA% < -0.50"),
    (-0.50, -0.25, "EMA% -0.50 to -0.25"),
    (-0.25, 0.00, "EMA% -0.25 to 0"),
    (0.00, 0.25, "EMA% 0 to 0.25"),
    (0.25, None, "EMA% > 0.25"),
]
_PRICE_VS_VWAP_BUCKETS = [
    (None, -1.00, "PvV% < -1.00"),
    (-1.00, -0.25, "PvV% -1.00 to -0.25"),
    (-0.25, 0.25, "PvV% -0.25 to 0.25"),
    (0.25, 1.00, "PvV% 0.25 to 1.00"),
    (1.00, None, "PvV% > 1.00"),
]

# Session UTC hour boundaries (inclusive start, exclusive end)
_SESSIONS = [
    ("ASIA",     0,  8),
    ("LONDON",   8, 13),
    ("US",      13, 21),
    ("LATE_US", 21, 24),
]

# Time-to-TP1 buckets (seconds)
_TP1_TIME_BUCKETS = [
    (None,      5 * 60, "<=5m"),
    (5 * 60,   10 * 60, "5-10m"),
    (10 * 60,  15 * 60, "10-15m"),
    (15 * 60,  None,    ">15m"),
]


# ======================================================================
# Pure cohort metrics
# ======================================================================

def _cohort_metrics(rows: list[Any]) -> dict:
    """Compute all key metrics for a cohort. Pure — no I/O, no DB.

    Rows must be closed signal_features rows joined with signal_observations
    (same JOIN as simulate_exit_policies.py: sf.* + so_mfe, so_mae, etc.).

    Returns a dict with:
      n, tp1_n, hit2r_n, stopped_n, expired_n,
      exp_after_tp1_n, exp_no_tp1_n, stopped_no_tp1_n,
      long_n, short_n,
      policy_b_avg_r, policy_e_avg_r,
      avg_mfe, avg_mae,
      median_tp1_secs,
      p25_tp1_secs, p75_tp1_secs,
      out_r_vals (list, for coverage checks)
    """
    st = _outcome_stats(rows)
    n = st["n"]
    long_n = sum(1 for r in rows if r["direction"] == "LONG")
    short_n = n - long_n

    # Policy B/E simulated R values
    b_vals = [_policy_b_r(r) for r in rows]
    e_vals = [_policy_e_r(r) for r in rows]

    # Time-to-TP1 (from sf.time_to_1r_seconds, synced from observations)
    tp1_times = [r["time_to_1r_seconds"] for r in rows
                 if r["time_to_1r_seconds"] is not None and _has_tp1(r)]

    return {
        "n": n,
        "tp1_n": st["tp1"],
        "hit2r_n": st["tp2"],
        "stopped_n": st["stopped"],
        "expired_n": st["expired"],
        "exp_after_tp1_n": st["exp_after_tp1"],
        "exp_no_tp1_n": st["exp_no_tp1"],
        "stopped_no_tp1_n": sum(1 for r in rows if _stopped_without_tp1(r)),
        "long_n": long_n,
        "short_n": short_n,
        "policy_b_avg_r": _avg(b_vals),
        "policy_e_avg_r": _avg(e_vals),
        "avg_mfe": _avg(st["mfe_vals"]),
        "avg_mae": _avg(st["mae_vals"]),
        "median_tp1_secs": _median(tp1_times),
        "p25_tp1_secs": _percentile(tp1_times, 25),
        "p75_tp1_secs": _percentile(tp1_times, 75),
        "tp1_times": tp1_times,
        "out_r_vals": st["out_r_vals"],
    }


# ======================================================================
# Print helpers
# ======================================================================

def _print_divider(char: str = "=", width: int = 68) -> None:
    print(char * width)


def _print_section(title: str) -> None:
    print()
    _print_divider()
    print(f"  {title}")
    _print_divider()


def _pct_s(n: int, total: int) -> str:
    if total == 0:
        return "  n/a"
    return f"{n / total * 100:5.1f}%"


def _r_s(val: Optional[float]) -> str:
    return f"{val:+.3f}R" if val is not None else "   n/a "


def _secs_to_min(val: Optional[float]) -> str:
    return f"{val / 60:.1f}m" if val is not None else "n/a"


def _is_dir_concentrated(long_n: int, short_n: int) -> bool:
    total = long_n + short_n
    if total == 0:
        return False
    return (long_n / total * 100 >= _DIR_CONCENTRATION_PCT
            or short_n / total * 100 >= _DIR_CONCENTRATION_PCT)


def _print_cohort_block(label: str, rows: list[Any], min_n: int, *,
                        show_timing: bool = True) -> None:
    """Print full metrics block for a single cohort."""
    m = _cohort_metrics(rows)
    n = m["n"]

    warn = ""
    if n == 0:
        warn = "  [NO DATA]"
    elif n < min_n:
        warn = f"  [INSUFFICIENT n={n} < {min_n}]"

    print(f"\n  {label}:{warn}")
    if n == 0:
        print("    No closed rows.")
        return

    if _is_dir_concentrated(m["long_n"], m["short_n"]):
        dominant = "LONG" if m["long_n"] >= m["short_n"] else "SHORT"
        print(f"    [DIR-CONCENTRATED: {m['long_n']} LONG / {m['short_n']} SHORT"
              f" — reflects {dominant} regime]")

    exp_n = m["expired_n"]
    print(f"    Rows              : {n}")
    print(f"    TP1               : {m['tp1_n']:4d}  {_pct_s(m['tp1_n'], n)}")
    print(f"    HIT_2R            : {m['hit2r_n']:4d}  {_pct_s(m['hit2r_n'], n)}")
    print(f"    STOPPED           : {m['stopped_n']:4d}  {_pct_s(m['stopped_n'], n)}"
          f"  (w/o TP1: {m['stopped_no_tp1_n']})")
    print(f"    EXPIRED           : {m['expired_n']:4d}  {_pct_s(m['expired_n'], n)}")
    if exp_n > 0:
        print(f"      after TP1       : {m['exp_after_tp1_n']:4d}  "
              f"{_pct_s(m['exp_after_tp1_n'], exp_n)} of expired")
        print(f"      w/o TP1         : {m['exp_no_tp1_n']:4d}  "
              f"{_pct_s(m['exp_no_tp1_n'], exp_n)} of expired")
    print(f"    Avg MFE           : {m['avg_mfe']:+.3f}R" if m["avg_mfe"] is not None
          else "    Avg MFE           : n/a")
    print(f"    Avg MAE           : {m['avg_mae']:+.3f}R" if m["avg_mae"] is not None
          else "    Avg MAE           : n/a")
    print(f"    Policy B AvgR     : {_r_s(m['policy_b_avg_r'])}"
          f"  (TP1→+1R, stop-noTP1→-1R, exp-noTP1→0R)")
    print(f"    Policy E AvgR     : {_r_s(m['policy_e_avg_r'])}"
          f"  (2R→+2R, stop→-1R, expTP1→+0.5R, expNoTP1→-0.25R)")
    print(f"    LONG / SHORT      : {m['long_n']} / {m['short_n']}")
    if show_timing:
        if m["tp1_times"]:
            print(f"    Median TP1 time   : {_secs_to_min(m['median_tp1_secs'])}"
                  f"  (P25={_secs_to_min(m['p25_tp1_secs'])}"
                  f" P75={_secs_to_min(m['p75_tp1_secs'])})")
        else:
            print("    Median TP1 time   : n/a (no TP1 rows with timing data)")


# ======================================================================
# Ranking helpers
# ======================================================================

def _cohort_rank_score(m: dict, baseline_tp1_rate: float) -> Optional[float]:
    """Ranking score for a cohort relative to baseline.

    Formula:
      score = policy_b_avg_r * 2.0
            + (tp1_rate - baseline_tp1_rate) * 10.0
            - exp_no_tp1_rate * 5.0

    Higher is better. Returns None if n == 0 or policy_b_avg_r is None.
    Designed to reward high TP1 rate, penalize expired-no-TP1,
    and use policy B R as the primary signal.
    """
    n = m["n"]
    if n == 0 or m["policy_b_avg_r"] is None:
        return None
    tp1_rate = m["tp1_n"] / n
    exp_no_tp1_rate = m["exp_no_tp1_n"] / n
    return (m["policy_b_avg_r"] * 2.0
            + (tp1_rate - baseline_tp1_rate) * 10.0
            - exp_no_tp1_rate * 5.0)


def _print_ranking(
    labeled_metrics: list[tuple[str, dict]],
    baseline_tp1_rate: float,
    min_n: int,
    title: str,
) -> None:
    """Print best/worst ranking table for a set of cohorts."""
    scored = []
    for label, m in labeled_metrics:
        if m["n"] < min_n:
            continue
        score = _cohort_rank_score(m, baseline_tp1_rate)
        if score is None:
            continue
        scored.append((score, label, m))

    if not scored:
        print("  [No cohorts met min-n threshold for ranking]")
        return

    scored.sort(key=lambda x: x[0], reverse=True)

    print(f"  {'Rank':<5} {'Cohort':<35} {'Score':>7}"
          f"  {'n':>5}  {'TP1%':>6}  {'BAvgR':>7}  {'EAvgR':>7}")
    print("  " + "-" * 78)
    for rank, (score, label, m) in enumerate(scored, 1):
        n = m["n"]
        tp1_pct = m["tp1_n"] / n * 100 if n else 0.0
        b_r = _r_s(m["policy_b_avg_r"])
        e_r = _r_s(m["policy_e_avg_r"])
        dc = " [DC]" if _is_dir_concentrated(m["long_n"], m["short_n"]) else ""
        print(f"  {rank:<5} {label[:35]:<35} {score:>+7.3f}"
              f"  {n:>5}  {tp1_pct:>5.1f}%  {b_r:>7}  {e_r:>7}{dc}")


# ======================================================================
# Cohort filters
# ======================================================================

def _hour_from_observed_at(observed_at: str) -> Optional[int]:
    """Extract UTC hour (0-23) from ISO8601 observed_at string."""
    if not observed_at:
        return None
    try:
        # Format: "2026-05-21T19:47:48Z" or "2026-05-21 19:47:48"
        t = observed_at.strip()
        # Find the time part
        if "T" in t:
            time_part = t.split("T")[1].rstrip("Z")
        elif " " in t:
            time_part = t.split(" ")[1]
        else:
            return None
        hour_str = time_part.split(":")[0]
        return int(hour_str)
    except (IndexError, ValueError):
        return None


def _session_for_hour(hour: Optional[int]) -> Optional[str]:
    """Map UTC hour to session name."""
    if hour is None:
        return None
    for name, start, end in _SESSIONS:
        if start <= hour < end:
            return name
    return None


def _in_bucket(val: Optional[float], lo: Optional[float], hi: Optional[float]) -> bool:
    """True if val is in [lo, hi) (None = open boundary)."""
    if val is None:
        return False
    if lo is not None and val < lo:
        return False
    if hi is not None and val >= hi:
        return False
    return True


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
    if meta is None:
        return False
    gates = meta.get("gates", {})
    gate_data = gates.get(gate_key)
    if gate_data is None:
        return False
    return bool(gate_data.get("passed", False))


# ======================================================================
# Main
# ======================================================================

def main(argv: list[str] | None = None) -> None:  # noqa: C901
    parser = argparse.ArgumentParser(
        description="APEX Exit / Timing Cohort Analysis [Milestone 11E] — report-only"
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
        help=f"Minimum rows to show non-INSUFFICIENT cohort (default: {_DEFAULT_MIN_N})",
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
    parser.add_argument(
        "--top-symbols",
        metavar="N",
        type=int,
        default=_DEFAULT_TOP_SYMBOLS,
        help=f"Number of top symbols to show in Section C (default: {_DEFAULT_TOP_SYMBOLS})",
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

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------
    print()
    _print_divider()
    print("  APEX Exit / Timing Cohort Analysis  [Milestone 11E]")
    _print_divider()
    print("  REPORT-ONLY. Does not change scanning, alerts, gates, or trading.")
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

    # Compute baseline TP1 rate for ranking
    baseline_m = _cohort_metrics(closed)
    baseline_tp1_rate = baseline_m["tp1_n"] / baseline_m["n"] if baseline_m["n"] else 0.0

    # ------------------------------------------------------------------
    # Section A: Overall baseline
    # ------------------------------------------------------------------
    _print_section("A — Overall Baseline")
    print("  All closed confirmed rows regardless of direction, symbol, or session.")
    _print_cohort_block("BASELINE_ALL", closed, args.min_n)

    # ------------------------------------------------------------------
    # Section B: Direction cohorts
    # ------------------------------------------------------------------
    _print_section("B — Direction Cohorts")
    long_rows = [r for r in closed if r["direction"] == "LONG"]
    short_rows = [r for r in closed if r["direction"] == "SHORT"]

    _print_cohort_block("LONG", long_rows, args.min_n)
    _print_cohort_block("SHORT", short_rows, args.min_n)

    b_cohorts = [
        ("LONG",  _cohort_metrics(long_rows)),
        ("SHORT", _cohort_metrics(short_rows)),
    ]
    print()
    print("  Ranking (B):")
    _print_ranking(b_cohorts, baseline_tp1_rate, args.min_n, "Direction")

    # ------------------------------------------------------------------
    # Section C: Symbol cohorts
    # ------------------------------------------------------------------
    _print_section("C — Symbol Cohorts")
    print(f"  Top {args.top_symbols} symbols by count (descending).")

    symbol_groups: dict[str, list[Any]] = {}
    for r in closed:
        sym = r["symbol"]
        symbol_groups.setdefault(sym, []).append(r)

    # Sort by count descending, then symbol ascending
    sorted_symbols = sorted(symbol_groups.keys(),
                            key=lambda s: (-len(symbol_groups[s]), s))
    top_symbols = sorted_symbols[:args.top_symbols]

    sym_cohorts = []
    for sym in top_symbols:
        sym_rows = symbol_groups[sym]
        _print_cohort_block(sym, sym_rows, args.min_n)
        sym_cohorts.append((sym, _cohort_metrics(sym_rows)))

    print()
    print("  Ranking (C) — quality score (higher = better TP1 rate, lower exp-no-TP1):")
    _print_ranking(sym_cohorts, baseline_tp1_rate, args.min_n, "Symbol")

    # ------------------------------------------------------------------
    # Section D: UTC hour cohorts
    # ------------------------------------------------------------------
    _print_section("D — UTC Hour Cohorts")
    print("  Rows grouped by the UTC hour extracted from observed_at.")

    hour_groups: dict[int, list[Any]] = {}
    for r in closed:
        h = _hour_from_observed_at(r["observed_at"])
        if h is not None:
            hour_groups.setdefault(h, []).append(r)

    hour_cohorts = []
    for h in sorted(hour_groups.keys()):
        label = f"Hour {h:02d}:00 UTC"
        h_rows = hour_groups[h]
        _print_cohort_block(label, h_rows, args.min_n)
        hour_cohorts.append((label, _cohort_metrics(h_rows)))

    if not hour_groups:
        print("\n  No observed_at data available for hour grouping.")

    # ------------------------------------------------------------------
    # Section E: Session cohorts
    # ------------------------------------------------------------------
    _print_section("E — Session Cohorts")
    print("  Sessions: ASIA=00-07 UTC, LONDON=08-12, US=13-20, LATE_US=21-23.")

    sess_groups: dict[str, list[Any]] = {s[0]: [] for s in _SESSIONS}
    sess_groups["UNKNOWN"] = []
    for r in closed:
        h = _hour_from_observed_at(r["observed_at"])
        sess = _session_for_hour(h)
        if sess is None:
            sess = "UNKNOWN"
        sess_groups[sess].append(r)

    sess_cohorts = []
    for sess_name, _, _ in _SESSIONS:
        s_rows = sess_groups[sess_name]
        _print_cohort_block(sess_name, s_rows, args.min_n)
        sess_cohorts.append((sess_name, _cohort_metrics(s_rows)))

    if sess_groups["UNKNOWN"]:
        _print_cohort_block("UNKNOWN", sess_groups["UNKNOWN"], args.min_n)

    print()
    print("  Ranking (E):")
    _print_ranking(sess_cohorts, baseline_tp1_rate, args.min_n, "Session")

    # ------------------------------------------------------------------
    # Section F: Candidate gate cohorts
    # ------------------------------------------------------------------
    _print_section("F — Candidate Gate Cohorts (11C metadata)")
    print("  Only rows with metadata_json from feature_version 11C_v1 are included.")
    print("  RESEARCH-ONLY. Gate results do not suppress or filter any signals.")

    gate_cohort_defs = [
        ("GATE_PASSED_ANY",              None),   # has gate metadata
        ("GATE_A_ATR_0_10_TO_1_00",     "GATE_A_ATR_0_10_TO_1_00"),
        ("GATE_B_EMA_SPREAD_NEG",        "GATE_B_EMA_SPREAD_NEG_0_25_TO_0"),
        ("GATE_C_RSI_50_TO_60",          "GATE_C_RSI_50_TO_60"),
        ("GATE_D_ATR_AND_EMA_COMBINED",  "GATE_D_ATR_AND_EMA_COMBINED"),
    ]

    rows_with_meta = [r for r in closed if _parse_gate_meta(r) is not None]
    gate_cohorts = []

    for label, gate_key in gate_cohort_defs:
        if gate_key is None:
            g_rows = rows_with_meta
        else:
            g_rows = [r for r in rows_with_meta
                      if _gate_passed(_parse_gate_meta(r), gate_key)]
        _print_cohort_block(label, g_rows, args.min_n)
        gate_cohorts.append((label, _cohort_metrics(g_rows)))

    print()
    print("  Ranking (F):")
    _print_ranking(gate_cohorts, baseline_tp1_rate, args.min_n, "Gate")

    # ------------------------------------------------------------------
    # Section G: Indicator bucket cohorts
    # ------------------------------------------------------------------
    _print_section("G — Indicator Bucket Cohorts")
    print("  Rows bucketed by RSI, ATR%, EMA spread%, and price vs VWAP%.")
    print("  Null values are excluded from each sub-section.")

    def _print_indicator_buckets(
        field: str,
        buckets: list[tuple],
        section_label: str,
    ) -> list[tuple[str, dict]]:
        print(f"\n  [{section_label}]")
        cohorts: list[tuple[str, dict]] = []
        for lo, hi, label in buckets:
            b_rows = [r for r in closed if _in_bucket(r[field], lo, hi)]
            _print_cohort_block(label, b_rows, args.min_n)
            cohorts.append((label, _cohort_metrics(b_rows)))
        return cohorts

    rsi_cohorts   = _print_indicator_buckets("rsi_val",         _RSI_BUCKETS,         "RSI")
    atr_cohorts   = _print_indicator_buckets("atr_pct",         _ATR_BUCKETS,         "ATR%")
    ema_cohorts   = _print_indicator_buckets("ema_spread_pct",  _EMA_SPREAD_BUCKETS,  "EMA Spread%")
    pvv_cohorts   = _print_indicator_buckets("price_vs_vwap_pct", _PRICE_VS_VWAP_BUCKETS, "Price vs VWAP%")

    all_g_cohorts = rsi_cohorts + atr_cohorts + ema_cohorts + pvv_cohorts
    print()
    print("  Ranking (G) — across all indicator buckets:")
    _print_ranking(all_g_cohorts, baseline_tp1_rate, args.min_n, "Indicator")

    # ------------------------------------------------------------------
    # Section H: Time-to-TP1 cohorts
    # ------------------------------------------------------------------
    _print_section("H — Time-to-TP1 Cohorts")
    print("  Rows bucketed by how quickly they reached TP1.")
    print("  NO_TP1 = rows that never reached TP1 (STOPPED or EXPIRED without TP1).")

    tp1_cohorts: list[tuple[str, dict]] = []

    for lo_s, hi_s, label in _TP1_TIME_BUCKETS:
        b_rows = [
            r for r in closed
            if _has_tp1(r)
            and _in_bucket(r["time_to_1r_seconds"], lo_s, hi_s)
        ]
        _print_cohort_block(label, b_rows, args.min_n, show_timing=False)
        tp1_cohorts.append((label, _cohort_metrics(b_rows)))

    # NO_TP1 cohort
    no_tp1_rows = [r for r in closed if not _has_tp1(r)]
    _print_cohort_block("NO_TP1", no_tp1_rows, args.min_n, show_timing=False)
    tp1_cohorts.append(("NO_TP1", _cohort_metrics(no_tp1_rows)))

    # ------------------------------------------------------------------
    # Section I: Best / Worst ranking across all dimensions
    # ------------------------------------------------------------------
    _print_section("I — Best / Worst Cohort Ranking (all dimensions)")
    print("  Score = policy_b_avg_r * 2.0")
    print("        + (tp1_rate - baseline_tp1_rate) * 10.0")
    print("        - exp_no_tp1_rate * 5.0")
    print(f"  Baseline TP1 rate = {baseline_tp1_rate:.1%}")
    print(f"  Minimum n = {args.min_n}")
    print()

    all_cohorts: list[tuple[str, dict]] = (
        [("ALL", baseline_m)]
        + [("B_" + l, m) for l, m in b_cohorts]
        + [("C_" + l, m) for l, m in sym_cohorts]
        + [("D_" + l, m) for l, m in hour_cohorts]
        + [("E_" + l, m) for l, m in sess_cohorts]
        + [("F_" + l, m) for l, m in gate_cohorts]
        + [("G_" + l, m) for l, m in all_g_cohorts]
        + [("H_" + l, m) for l, m in tp1_cohorts]
    )

    scored_all = []
    for label, m in all_cohorts:
        if m["n"] < args.min_n:
            continue
        score = _cohort_rank_score(m, baseline_tp1_rate)
        if score is None:
            continue
        dc = " [DC]" if _is_dir_concentrated(m["long_n"], m["short_n"]) else ""
        scored_all.append((score, label, m, dc))

    scored_all.sort(key=lambda x: x[0], reverse=True)

    top_k = 10
    worst_k = 5

    print(f"  TOP {top_k} COHORTS:")
    print(f"  {'Rank':<5} {'Cohort':<38} {'Score':>7}"
          f"  {'n':>5}  {'TP1%':>6}  {'BAvgR':>7}  {'EAvgR':>7}")
    print("  " + "-" * 80)
    for rank, (score, label, m, dc) in enumerate(scored_all[:top_k], 1):
        n = m["n"]
        tp1_pct = m["tp1_n"] / n * 100 if n else 0.0
        b_r = _r_s(m["policy_b_avg_r"])
        e_r = _r_s(m["policy_e_avg_r"])
        print(f"  {rank:<5} {label[:38]:<38} {score:>+7.3f}"
              f"  {n:>5}  {tp1_pct:>5.1f}%  {b_r:>7}  {e_r:>7}{dc}")

    if len(scored_all) > top_k:
        print()
        print(f"  WORST {worst_k} COHORTS:")
        print(f"  {'Rank':<5} {'Cohort':<38} {'Score':>7}"
              f"  {'n':>5}  {'TP1%':>6}  {'BAvgR':>7}  {'EAvgR':>7}")
        print("  " + "-" * 80)
        for rank, (score, label, m, dc) in enumerate(
            reversed(scored_all[-worst_k:]), 1
        ):
            n = m["n"]
            tp1_pct = m["tp1_n"] / n * 100 if n else 0.0
            b_r = _r_s(m["policy_b_avg_r"])
            e_r = _r_s(m["policy_e_avg_r"])
            print(f"  {rank:<5} {label[:38]:<38} {score:>+7.3f}"
                  f"  {n:>5}  {tp1_pct:>5.1f}%  {b_r:>7}  {e_r:>7}{dc}")

    # ------------------------------------------------------------------
    # Footer
    # ------------------------------------------------------------------
    _print_section("Notes and Limitations")
    print("  1. All results are from dry-run data only (DRY_RUN_MODE=true).")
    print("     No live trades, exchange orders, or real capital at risk.")
    print()
    print("  2. Policy B and E AvgR are accounting simulations, not live P&L.")
    print("     They reinterpret recorded outcome fields under alternative models.")
    print()
    print("  3. Median TP1 time requires time_to_1r_seconds to be populated.")
    print("     Rows without this field are excluded from timing stats only.")
    print()
    print("  4. Direction-concentrated cohorts [DC] reflect a regime/direction")
    print("     effect. Do not treat them as generally valid cohorts.")
    print()
    print("  5. Section F gate cohorts are PROSPECTIVE RESEARCH only. Gates do")
    print("     not suppress or filter signals in any runtime path.")
    print()
    print("  6. Cohort ranking score formula is not a trading signal.")
    print("     It is a diagnostic aid for identifying promising subpopulations.")
    print()
    _print_divider()


if __name__ == "__main__":
    main()
