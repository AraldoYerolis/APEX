"""Strategy Filter Simulator — Milestone 11A / 11A.1.

Read-only retrospective simulation of candidate filter rules against
historical dry-run signal_features + signal_observations data.

REPORT-ONLY. Does not change runtime scanning, alerts, or trading.
Does not write to the database. Does not enable live mode.

Usage (run from repo root):
    PYTHONPATH=src python scripts/simulate_strategy_filters.py
    PYTHONPATH=src python scripts/simulate_strategy_filters.py --since 2026-05-20T10:28:00Z
    PYTHONPATH=src python scripts/simulate_strategy_filters.py --since 2026-05-20T10:28:00Z --min-n 10
    PYTHONPATH=src python scripts/simulate_strategy_filters.py --since 2026-05-20T10:28:00Z --csv /tmp/filter_simulation.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

# -----------------------------------------------------------------------
# sys.path bootstrap — ensures this script works when invoked as:
#   PYTHONPATH=src python scripts/simulate_strategy_filters.py
# or via create_apex_snapshot.py (which adds repo root + src/ itself).
# Mirrors the same pattern used in create_apex_snapshot.py.
# -----------------------------------------------------------------------
_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent
_SRC_DIR = _REPO_ROOT / "src"
for _p in (str(_REPO_ROOT), str(_SRC_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from apex.config import get_settings
from apex.db.connection import close_db, init_db

# Import pure helpers from the learning report — no side effects, no main().
from scripts.report_signal_learning import (
    _avg_r,
    _macro_alignment_label,
    _outcome_stats,
    _pct,
    _quality_score,
    _recommendation_label,
)

_DEFAULT_MIN_N = 10

# Names of filter groups that use retrospective group labels
_RETROSPECTIVE_PREFIXES = (
    "KEEP_PROMISING", "KEEP_NOT_WEAK", "EXCLUDE_WEAK", "EXCLUDE_INSUFFICIENT",
    "KEEP_ATR_0_25_TO_0_50_AND_NOT_WEAK", "KEEP_ATR_GTE_0_10_AND_EXCLUDE_WEAK",
    "KEEP_NOT_WEAK_AND_NOT_INSUFFICIENT", "KEEP_PROMISING_OR_WATCHLIST_AND_ATR_GTE_0_10",
)


# ======================================================================
# Filter predicate functions
# Each accepts a single sqlite3.Row (dict-accessible) and returns bool.
# True = keep this row.
# ======================================================================

def _filter_atr_gte_0_10(r: Any) -> bool:
    v = r["atr_pct"]
    return v is not None and v >= 0.10


def _filter_atr_0_10_to_1_00(r: Any) -> bool:
    v = r["atr_pct"]
    return v is not None and 0.10 <= v <= 1.00


def _filter_atr_0_25_to_0_50(r: Any) -> bool:
    v = r["atr_pct"]
    return v is not None and 0.25 <= v <= 0.50


def _filter_ema_spread_neg_0_25_to_0(r: Any) -> bool:
    v = r["ema_spread_pct"]
    return v is not None and -0.25 <= v < 0.0


def _filter_ema_spread_0_25_to_0_50(r: Any) -> bool:
    v = r["ema_spread_pct"]
    return v is not None and 0.25 <= v <= 0.50


def _filter_ema_spread_above_0(r: Any) -> bool:
    v = r["ema_spread_pct"]
    return v is not None and v > 0.0


def _filter_rsi_45_to_60(r: Any) -> bool:
    v = r["rsi_val"]
    return v is not None and 45.0 <= v <= 60.0


def _filter_rsi_50_to_60(r: Any) -> bool:
    v = r["rsi_val"]
    return v is not None and 50.0 <= v <= 60.0


def _filter_rsi_55_to_60(r: Any) -> bool:
    v = r["rsi_val"]
    return v is not None and 55.0 <= v <= 60.0


def _filter_vwap_near(r: Any) -> bool:
    v = r["price_vs_vwap_pct"]
    return v is not None and -0.25 <= v <= 0.25


def _filter_vwap_above_0_to_0_25(r: Any) -> bool:
    v = r["price_vs_vwap_pct"]
    return v is not None and 0.0 <= v <= 0.25


def _filter_vwap_gte_neg_0_25(r: Any) -> bool:
    v = r["price_vs_vwap_pct"]
    return v is not None and v >= -0.25


def _filter_long_only(r: Any) -> bool:
    return r["direction"] == "LONG"


def _filter_short_only(r: Any) -> bool:
    return r["direction"] == "SHORT"


def _filter_btc_aligned(r: Any) -> bool:
    return _macro_alignment_label(r["direction"], r["btc_trend_bias"]) == "aligned"


def _filter_btc_opposed(r: Any) -> bool:
    return _macro_alignment_label(r["direction"], r["btc_trend_bias"]) == "opposed"


def _filter_btc_neutral(r: Any) -> bool:
    return _macro_alignment_label(r["direction"], r["btc_trend_bias"]) == "neutral"


def _filter_eth_aligned(r: Any) -> bool:
    return _macro_alignment_label(r["direction"], r["eth_trend_bias"]) == "aligned"


def _filter_eth_opposed(r: Any) -> bool:
    return _macro_alignment_label(r["direction"], r["eth_trend_bias"]) == "opposed"


def _filter_eth_neutral(r: Any) -> bool:
    return _macro_alignment_label(r["direction"], r["eth_trend_bias"]) == "neutral"


# ======================================================================
# Label-based filter factories
# These compute group labels from the dataset itself — RETROSPECTIVE.
# ======================================================================

def _build_group_labels(
    closed_rows: list[Any],
) -> dict[tuple[str, str], str]:
    """Compute recommendation labels per (symbol, direction) from the closed rows.

    Uses the same logic as report_signal_learning.py.
    RETROSPECTIVE: labels are derived from the same dataset being filtered,
    which means these filters may overfit the current sample.
    """
    sd_closed: dict[tuple[str, str], list] = defaultdict(list)
    for r in closed_rows:
        sd_closed[(r["symbol"], r["direction"])].append(r)

    if not closed_rows:
        return {}

    overall_st = _outcome_stats(closed_rows)
    n_closed = overall_st["n"]
    overall_tp1_rate = overall_st["tp1"] / n_closed if n_closed else 0.0
    overall_stop_rate = overall_st["stopped"] / n_closed if n_closed else 0.0

    labels: dict[tuple[str, str], str] = {}
    for key, rows in sd_closed.items():
        n_g = len(rows)
        if n_g == 0:
            labels[key] = "INSUFFICIENT_SAMPLE"
            continue
        st = _outcome_stats(rows)
        tp1_r = st["tp1"] / n_g
        tp2_r = st["tp2"] / n_g
        stop_r = st["stopped"] / n_g
        exp_r = st["expired"] / n_g
        exp_no_tp1_r = st["exp_no_tp1"] / n_g
        avg_mfe = sum(st["mfe_vals"]) / len(st["mfe_vals"]) if st["mfe_vals"] else None
        avg_mae = sum(st["mae_vals"]) / len(st["mae_vals"]) if st["mae_vals"] else None
        avg_out_r = sum(st["out_r_vals"]) / len(st["out_r_vals"]) if st["out_r_vals"] else None
        score = _quality_score(
            tp1_r, tp2_r, stop_r, exp_no_tp1_r, avg_mfe, avg_mae, avg_out_r,
            n_g, overall_tp1_rate, overall_stop_rate,
        )
        label = _recommendation_label(
            score, tp1_r, stop_r, exp_r, n_g, overall_tp1_rate, overall_stop_rate,
        )
        labels[key] = label
    return labels


def _make_label_filter(allowed_labels: set[str], group_labels: dict[tuple[str, str], str]):
    """Return a predicate that keeps rows whose group label is in allowed_labels."""
    def _pred(r: Any) -> bool:
        key = (r["symbol"], r["direction"])
        return group_labels.get(key, "INSUFFICIENT_SAMPLE") in allowed_labels
    return _pred


# ======================================================================
# Warning flags
# ======================================================================

def _compute_flags(
    res: dict,
    n_baseline: int,
    baseline_tp2_rate: float,
    baseline_avg_r: Optional[float],
    is_retrospective: bool,
) -> list[str]:
    """Return a list of warning flag strings for a filter result."""
    flags: list[str] = []
    if res["exp_rate"] >= 0.80:
        flags.append("HIGH_EXPIRY")
    if n_baseline > 0 and res["n_kept"] / n_baseline < 0.10:
        flags.append("THIN_SAMPLE")
    if is_retrospective:
        flags.append("RETROSPECTIVE_LABEL")
    # Stop materially worse: > 3pp above baseline
    if res["d_stop"] is not None and res["d_stop"] > 0.03:
        flags.append("STOP_WORSE")
    # TP2 materially worse: > 2pp below baseline
    if res["d_tp2"] is not None and res["d_tp2"] < -0.02:
        flags.append("TP2_WORSE")
    # avg R worse
    if res["d_r"] is not None and res["d_r"] < -0.05:
        flags.append("AVG_R_WORSE")
    return flags


def _is_retrospective(name: str) -> bool:
    return any(name.startswith(p) for p in _RETROSPECTIVE_PREFIXES)


# ======================================================================
# Simulation core
# ======================================================================

def _verdict(
    n_kept: int,
    min_n: int,
    n_baseline: int,
    baseline_tp1_rate: float,
    baseline_stop_rate: float,
    baseline_avg_r: Optional[float],
    kept_tp1_rate: float,
    kept_stop_rate: float,
    kept_avg_r: Optional[float],
    kept_exp_rate: float,
) -> str:
    """Assign a verdict label for a simulated filter.

    Verdict labels (checked in order):
      INSUFFICIENT_SAMPLE       — kept < min_n
      REDUCES_SAMPLE_TOO_MUCH   — kept < 10% of baseline
      WORSE_THAN_BASELINE       — TP1 and avgR both worse, or stop materially worse
      IMPROVES_BUT_EXPIRY_HIGH  — improves TP1/avgR but expiry >= 80%
      IMPROVES_SIGNAL_QUALITY   — improves TP1/avgR, stop not materially worse, expiry < 80%
      MIXED_NEEDS_REVIEW        — otherwise
    """
    if n_kept < min_n:
        return "INSUFFICIENT_SAMPLE"
    if n_baseline > 0 and n_kept / n_baseline < 0.10:
        return "REDUCES_SAMPLE_TOO_MUCH"

    d_tp1 = kept_tp1_rate - baseline_tp1_rate
    d_stop = kept_stop_rate - baseline_stop_rate
    d_r = (kept_avg_r or 0.0) - (baseline_avg_r or 0.0)

    # Worse on all meaningful axes
    if d_r <= -0.05 and d_tp1 <= 0:
        return "WORSE_THAN_BASELINE"
    if d_tp1 <= -0.03 and d_stop >= 0.03:
        return "WORSE_THAN_BASELINE"

    # Positive improvement candidate — check expiry before awarding IMPROVES
    is_improving = (d_r >= 0.05 and d_tp1 >= 0) or (d_tp1 >= 0.02 and d_stop <= 0)
    if is_improving:
        if kept_exp_rate >= 0.80:
            return "IMPROVES_BUT_EXPIRY_HIGH"
        return "IMPROVES_SIGNAL_QUALITY"

    return "MIXED_NEEDS_REVIEW"


def _balanced_score(
    res: dict,
    b_tp1_r: float,
    b_avg_r: Optional[float],
    is_retro: bool,
) -> float:
    """Compute a balanced score for ranking.

    Higher is better. Penalizes high expiry, thin sample, retrospective labels.
    """
    d_tp1 = res["d_tp1"] or 0.0
    d_stop = -(res["d_stop"] or 0.0)
    d_r = res["d_r"] or 0.0
    d_exp_no_tp1 = -(res["exp_no_tp1_rate"] - (res.get("b_exp_no_tp1_rate") or res["exp_no_tp1_rate"]))

    score = d_tp1 * 0.40 + d_stop * 0.30 + d_r * 0.20 + d_exp_no_tp1 * 0.10

    # Penalties
    if res["exp_rate"] >= 0.80:
        score -= 0.20
    if res["verdict"] == "REDUCES_SAMPLE_TOO_MUCH":
        score -= 0.10
    if is_retro:
        score -= 0.05

    return score


def _run_filter(
    name: str,
    rule_desc: str,
    pred,
    closed_rows: list[Any],
    baseline_st: dict,
    min_n: int,
    b_exp_no_tp1_rate: float,
) -> dict:
    """Run a single filter simulation and return a result dict."""
    kept = [r for r in closed_rows if pred(r)]
    removed = len(closed_rows) - len(kept)
    n_kept = len(kept)
    n_base = len(closed_rows)

    if n_kept == 0:
        return {
            "name": name,
            "rule": rule_desc,
            "n_kept": 0,
            "n_removed": removed,
            "kept_pct": 0.0,
            "tp1_rate": 0.0,
            "tp2_rate": 0.0,
            "stop_rate": 0.0,
            "exp_rate": 0.0,
            "exp_no_tp1_rate": 0.0,
            "avg_mfe": None,
            "avg_mae": None,
            "avg_r": None,
            "d_tp1": 0.0,
            "d_tp2": 0.0,
            "d_stop": 0.0,
            "d_exp": 0.0,
            "d_r": None,
            "b_exp_no_tp1_rate": b_exp_no_tp1_rate,
            "verdict": "INSUFFICIENT_SAMPLE",
            "flags": [],
        }

    st = _outcome_stats(kept)
    n = st["n"]
    tp1_r = st["tp1"] / n
    tp2_r = st["tp2"] / n
    stop_r = st["stopped"] / n
    exp_r = st["expired"] / n
    exp_no_tp1_r = st["exp_no_tp1"] / n
    avg_mfe = sum(st["mfe_vals"]) / len(st["mfe_vals"]) if st["mfe_vals"] else None
    avg_mae = sum(st["mae_vals"]) / len(st["mae_vals"]) if st["mae_vals"] else None
    avg_r = sum(st["out_r_vals"]) / len(st["out_r_vals"]) if st["out_r_vals"] else None

    b_n = baseline_st["n"]
    b_tp1_r = baseline_st["tp1"] / b_n if b_n else 0.0
    b_tp2_r = baseline_st["tp2"] / b_n if b_n else 0.0
    b_stop_r = baseline_st["stopped"] / b_n if b_n else 0.0
    b_exp_r = baseline_st["expired"] / b_n if b_n else 0.0
    b_avg_r = sum(baseline_st["out_r_vals"]) / len(baseline_st["out_r_vals"]) if baseline_st["out_r_vals"] else None

    d_r = (avg_r - b_avg_r) if (avg_r is not None and b_avg_r is not None) else None

    verdict = _verdict(
        n_kept, min_n, n_base,
        b_tp1_r, b_stop_r, b_avg_r,
        tp1_r, stop_r, avg_r,
        exp_r,
    )

    return {
        "name": name,
        "rule": rule_desc,
        "n_kept": n_kept,
        "n_removed": removed,
        "kept_pct": n_kept / n_base * 100 if n_base else 0.0,
        "tp1_rate": tp1_r,
        "tp2_rate": tp2_r,
        "stop_rate": stop_r,
        "exp_rate": exp_r,
        "exp_no_tp1_rate": exp_no_tp1_r,
        "avg_mfe": avg_mfe,
        "avg_mae": avg_mae,
        "avg_r": avg_r,
        "d_tp1": tp1_r - b_tp1_r,
        "d_tp2": tp2_r - b_tp2_r,
        "d_stop": stop_r - b_stop_r,
        "d_exp": exp_r - b_exp_r,
        "d_r": d_r,
        "b_exp_no_tp1_rate": b_exp_no_tp1_rate,
        "verdict": verdict,
        "flags": [],  # populated separately
    }


def _print_result(res: dict) -> None:
    n = res["n_kept"]
    n_removed = res["n_removed"]
    kept_pct = res["kept_pct"]
    verdict = res["verdict"]

    def _delta(v: Optional[float]) -> str:
        if v is None:
            return "   n/a"
        sign = "+" if v >= 0 else ""
        return f"{sign}{v * 100:.1f}pp"

    def _delta_r(v: Optional[float]) -> str:
        if v is None:
            return "   n/a"
        return f"{v:+.3f}R"

    print(f"  Rule      : {res['rule']}")
    print(f"  Kept      : {n:4d} ({kept_pct:.1f}%)   Removed: {n_removed}")
    if n > 0:
        print(
            f"  TP1%={_pct(int(res['tp1_rate']*n), n).strip():>6}  "
            f"TP2%={_pct(int(res['tp2_rate']*n), n).strip():>6}  "
            f"STOP%={_pct(int(res['stop_rate']*n), n).strip():>6}  "
            f"EXP%={_pct(int(res['exp_rate']*n), n).strip():>6}  "
            f"EXPnoTP1%={_pct(int(res['exp_no_tp1_rate']*n), n).strip():>6}"
        )
        avg_mfe_s = f"{res['avg_mfe']:+.2f}" if res["avg_mfe"] is not None else "n/a"
        avg_mae_s = f"{res['avg_mae']:+.2f}" if res["avg_mae"] is not None else "n/a"
        avg_r_s = f"{res['avg_r']:+.3f}R" if res["avg_r"] is not None else "n/a"
        print(f"  avgMFE={avg_mfe_s}  avgMAE={avg_mae_s}  avgR={avg_r_s}")
        print(
            f"  Δ vs baseline: TP1={_delta(res['d_tp1'])}  "
            f"TP2={_delta(res['d_tp2'])}  "
            f"STOP={_delta(res['d_stop'])}  "
            f"EXP={_delta(res['d_exp'])}  "
            f"avgR={_delta_r(res['d_r'])}"
        )
    flags = res.get("flags", [])
    if flags:
        print(f"  Flags     : {', '.join(flags)}")
    print(f"  Verdict   : {verdict}")


# ======================================================================
# Main
# ======================================================================

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="APEX Strategy Filter Simulator [Milestone 11A] — report-only"
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
        help=f"Minimum kept rows required for non-INSUFFICIENT_SAMPLE verdict (default: {_DEFAULT_MIN_N})",
    )
    parser.add_argument(
        "--csv",
        metavar="PATH",
        default=None,
        help="Write simulation results to a CSV file at PATH",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    conn = init_db(settings.apex_db_path)

    # ------------------------------------------------------------------
    # Load data — read-only JOIN, same pattern as report_signal_learning
    # ------------------------------------------------------------------
    try:
        conditions: list[str] = []
        params: list[Any] = []
        if args.since:
            conditions.append("sf.captured_at >= ?")
            params.append(args.since)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        query = f"""
            SELECT sf.*,
                   so.max_favorable_excursion AS so_mfe,
                   so.max_adverse_excursion   AS so_mae
            FROM signal_features sf
            LEFT JOIN signal_observations so
                   ON sf.observation_uid = so.observation_uid
            {where}
            ORDER BY sf.captured_at DESC
        """
        rows = conn.execute(query, params).fetchall()
    except Exception as e:
        print(f"ERROR: could not query signal_features: {e}", file=sys.stderr)
        close_db()
        sys.exit(1)

    close_db()

    # ------------------------------------------------------------------
    # Section 1: Header + safety disclaimer
    # ------------------------------------------------------------------
    print()
    print("=" * 68)
    print("  APEX Strategy Filter Simulator  [Milestone 11A]")
    print("=" * 68)
    print("  REPORT-ONLY SIMULATION.")
    print("  Does not change runtime scanning, alerts, or trading.")
    print("  Does not write to the database.")
    print("  Candidate filters are RETROSPECTIVE — results may overfit the")
    print("  current sample. Do not promote to runtime gates without further")
    print("  validation over a larger, time-separated dataset.")
    print()
    if args.since:
        print(f"  Filtered since  : {args.since}")
    print(f"  Rows loaded     : {len(rows)}")

    confirmed = [r for r in rows if r["signal_type"] == "CONFIRMED_SETUP"]
    closed_confirmed = [
        r for r in confirmed
        if r["outcome_status"] not in (None, "OBSERVED")
    ]
    print(f"  Confirmed rows  : {len(confirmed)}")
    print(f"  Closed confirmed: {len(closed_confirmed)}  (open/pending excluded from simulation)")
    print(f"  Min-N threshold : {args.min_n}")

    if not closed_confirmed:
        print()
        print("  No closed CONFIRMED_SETUP rows to simulate. Exiting.")
        print()
        print("=" * 68)
        return

    # ------------------------------------------------------------------
    # Section 2: Baseline performance
    # ------------------------------------------------------------------
    baseline_st = _outcome_stats(closed_confirmed)
    b_n = baseline_st["n"]
    b_tp1_r = baseline_st["tp1"] / b_n if b_n else 0.0
    b_tp2_r = baseline_st["tp2"] / b_n if b_n else 0.0
    b_stop_r = baseline_st["stopped"] / b_n if b_n else 0.0
    b_exp_r = baseline_st["expired"] / b_n if b_n else 0.0
    b_exp_no_tp1_r = baseline_st["exp_no_tp1"] / b_n if b_n else 0.0
    b_avg_r = sum(baseline_st["out_r_vals"]) / len(baseline_st["out_r_vals"]) if baseline_st["out_r_vals"] else None
    b_avg_mfe = sum(baseline_st["mfe_vals"]) / len(baseline_st["mfe_vals"]) if baseline_st["mfe_vals"] else None
    b_avg_mae = sum(baseline_st["mae_vals"]) / len(baseline_st["mae_vals"]) if baseline_st["mae_vals"] else None

    long_rows = [r for r in closed_confirmed if r["direction"] == "LONG"]
    short_rows = [r for r in closed_confirmed if r["direction"] == "SHORT"]

    print()
    print("=" * 68)
    print("  Baseline performance (all closed CONFIRMED_SETUP rows)")
    print("=" * 68)
    print(f"  Total confirmed : {len(confirmed)}")
    print(f"  Closed confirmed: {b_n}")
    print(f"  TP1 milestone   : {baseline_st['tp1']:4d}  {_pct(baseline_st['tp1'], b_n)}")
    print(f"  HIT_2R          : {baseline_st['tp2']:4d}  {_pct(baseline_st['tp2'], b_n)}")
    print(f"  STOPPED         : {baseline_st['stopped']:4d}  {_pct(baseline_st['stopped'], b_n)}")
    print(f"  EXPIRED (total) : {baseline_st['expired']:4d}  {_pct(baseline_st['expired'], b_n)}")
    print(f"    after TP1     : {baseline_st['exp_after_tp1']:4d}  {_pct(baseline_st['exp_after_tp1'], baseline_st['expired'] or 1)} of expired")
    print(f"    without TP1   : {baseline_st['exp_no_tp1']:4d}  {_pct(baseline_st['exp_no_tp1'], baseline_st['expired'] or 1)} of expired")
    mfe_s = f"{b_avg_mfe:+.2f}" if b_avg_mfe is not None else "n/a"
    mae_s = f"{b_avg_mae:+.2f}" if b_avg_mae is not None else "n/a"
    r_s = f"{b_avg_r:+.3f}" if b_avg_r is not None else "n/a"
    print(f"  Avg MFE         : {mfe_s} R")
    print(f"  Avg MAE         : {mae_s} R")
    print(f"  Avg outcome R   : {r_s} R")
    print(f"  LONG            : {len(long_rows):4d}  {_pct(len(long_rows), b_n)}")
    print(f"  SHORT           : {len(short_rows):4d}  {_pct(len(short_rows), b_n)}")

    # ------------------------------------------------------------------
    # Build group labels (RETROSPECTIVE — for label-based filters)
    # ------------------------------------------------------------------
    group_labels = _build_group_labels(closed_confirmed)

    # ------------------------------------------------------------------
    # Section 3: Define filter candidates
    # ------------------------------------------------------------------
    PROMISING = {"PROMISING_OBSERVE"}
    PROMISING_OR_WATCHLIST = {"PROMISING_OBSERVE", "WATCHLIST_NEEDS_EXPIRY_FIX"}
    NOT_WEAK = {"PROMISING_OBSERVE", "WATCHLIST_NEEDS_EXPIRY_FIX", "NEEDS_FILTERING"}

    filter_defs = [
        # ---- A: ATR filters
        ("KEEP_ATR_GTE_0_10",
         "atr_pct >= 0.10",
         _filter_atr_gte_0_10),
        ("KEEP_ATR_0_10_TO_1_00",
         "0.10 <= atr_pct <= 1.00",
         _filter_atr_0_10_to_1_00),
        ("KEEP_ATR_0_25_TO_0_50",
         "0.25 <= atr_pct <= 0.50",
         _filter_atr_0_25_to_0_50),
        ("EXCLUDE_ATR_LT_0_10",
         "atr_pct >= 0.10  [exclusion candidate for low-vol setups]",
         _filter_atr_gte_0_10),

        # ---- B: EMA spread filters
        ("KEEP_EMA_SPREAD_NEG_0_25_TO_0",
         "-0.25 <= ema_spread_pct < 0",
         _filter_ema_spread_neg_0_25_to_0),
        ("KEEP_EMA_SPREAD_0_25_TO_0_50",
         "0.25 <= ema_spread_pct <= 0.50",
         _filter_ema_spread_0_25_to_0_50),
        ("KEEP_EMA_SPREAD_ABOVE_0",
         "ema_spread_pct > 0",
         _filter_ema_spread_above_0),

        # ---- C: RSI filters
        ("KEEP_RSI_45_TO_60",
         "45 <= rsi_val <= 60",
         _filter_rsi_45_to_60),
        ("KEEP_RSI_50_TO_60",
         "50 <= rsi_val <= 60",
         _filter_rsi_50_to_60),
        ("KEEP_RSI_55_TO_60",
         "55 <= rsi_val <= 60",
         _filter_rsi_55_to_60),

        # ---- D: VWAP filters
        ("KEEP_PRICE_NEAR_VWAP",
         "-0.25 <= price_vs_vwap_pct <= 0.25",
         _filter_vwap_near),
        ("KEEP_PRICE_ABOVE_VWAP_0_TO_0_25",
         "0 <= price_vs_vwap_pct <= 0.25",
         _filter_vwap_above_0_to_0_25),
        ("EXCLUDE_EXTREME_BELOW_VWAP",
         "price_vs_vwap_pct >= -0.25  [exclusion of far-below-VWAP setups]",
         _filter_vwap_gte_neg_0_25),

        # ---- E: Direction filters
        ("KEEP_LONG_ONLY",
         "direction == LONG",
         _filter_long_only),
        ("KEEP_SHORT_ONLY",
         "direction == SHORT",
         _filter_short_only),

        # ---- F: Macro alignment filters
        ("KEEP_BTC_ALIGNED",
         "btc_trend_bias aligned with signal direction",
         _filter_btc_aligned),
        ("KEEP_BTC_OPPOSED",
         "btc_trend_bias opposed to signal direction",
         _filter_btc_opposed),
        ("KEEP_BTC_NEUTRAL_OR_UNKNOWN",
         "btc_trend_bias neutral or missing",
         _filter_btc_neutral),
        ("KEEP_ETH_ALIGNED",
         "eth_trend_bias aligned with signal direction",
         _filter_eth_aligned),
        ("KEEP_ETH_OPPOSED",
         "eth_trend_bias opposed to signal direction",
         _filter_eth_opposed),
        ("KEEP_ETH_NEUTRAL_OR_UNKNOWN",
         "eth_trend_bias neutral or missing",
         _filter_eth_neutral),

        # ---- G: Label-based filters (RETROSPECTIVE)
        ("KEEP_PROMISING_OBSERVE",
         "RETROSPECTIVE: group label == PROMISING_OBSERVE",
         _make_label_filter(PROMISING, group_labels)),
        ("KEEP_PROMISING_OR_WATCHLIST",
         "RETROSPECTIVE: group label in {PROMISING_OBSERVE, WATCHLIST_NEEDS_EXPIRY_FIX}",
         _make_label_filter(PROMISING_OR_WATCHLIST, group_labels)),
        ("EXCLUDE_WEAK_OBSERVE_ONLY",
         "RETROSPECTIVE: group label != WEAK_OBSERVE_ONLY",
         _make_label_filter(NOT_WEAK, group_labels)),
        ("EXCLUDE_INSUFFICIENT_SAMPLE_GROUPS",
         "RETROSPECTIVE: group label != INSUFFICIENT_SAMPLE",
         _make_label_filter(NOT_WEAK | {"INSUFFICIENT_SAMPLE"} - {"INSUFFICIENT_SAMPLE"},
                            group_labels)),

        # ---- H: Combined filters
        ("KEEP_ATR_0_25_TO_0_50_AND_NOT_WEAK",
         "0.25 <= atr_pct <= 0.50 AND group label != WEAK_OBSERVE_ONLY [RETROSPECTIVE]",
         lambda r: _filter_atr_0_25_to_0_50(r) and _make_label_filter(NOT_WEAK, group_labels)(r)),
        ("KEEP_ATR_GTE_0_10_AND_EXCLUDE_WEAK",
         "atr_pct >= 0.10 AND group label != WEAK_OBSERVE_ONLY [RETROSPECTIVE]",
         lambda r: _filter_atr_gte_0_10(r) and _make_label_filter(NOT_WEAK, group_labels)(r)),
        ("KEEP_NOT_WEAK_AND_NOT_INSUFFICIENT",
         "RETROSPECTIVE: group label not in {WEAK_OBSERVE_ONLY, INSUFFICIENT_SAMPLE}",
         _make_label_filter(NOT_WEAK, group_labels)),
        ("KEEP_PROMISING_OR_WATCHLIST_AND_ATR_GTE_0_10",
         "RETROSPECTIVE: label in {PROMISING, WATCHLIST} AND atr_pct >= 0.10",
         lambda r: _filter_atr_gte_0_10(r) and _make_label_filter(PROMISING_OR_WATCHLIST, group_labels)(r)),
        ("KEEP_BTC_OPPOSED_AND_ATR_GTE_0_10",
         "btc_trend_bias opposed AND atr_pct >= 0.10",
         lambda r: _filter_btc_opposed(r) and _filter_atr_gte_0_10(r)),
    ]

    # ------------------------------------------------------------------
    # Section 3: Run all filter simulations
    # ------------------------------------------------------------------
    print()
    print("=" * 68)
    print("  Candidate filter simulations")
    print("=" * 68)
    print("  Comparing each filter against baseline closed CONFIRMED rows.")
    print("  RETROSPECTIVE label filters may overfit — treat with caution.")

    results: list[dict] = []
    sections = [
        ("A — ATR filters", 4),
        ("B — EMA spread filters", 3),
        ("C — RSI filters", 3),
        ("D — VWAP filters", 3),
        ("E — Direction filters", 2),
        ("F — Macro alignment filters (BTC)", 3),
        ("F — Macro alignment filters (ETH)", 3),
        ("G — Label-based filters (RETROSPECTIVE)", 4),
        ("H — Combined filters", 5),
    ]

    idx = 0
    for section_name, count in sections:
        print()
        print(f"  -- {section_name} --")
        for _ in range(count):
            if idx >= len(filter_defs):
                break
            name, rule, pred = filter_defs[idx]
            idx += 1
            res = _run_filter(name, rule, pred, closed_confirmed, baseline_st, args.min_n, b_exp_no_tp1_r)
            # Compute and attach flags
            is_retro = _is_retrospective(name)
            res["flags"] = _compute_flags(res, b_n, b_tp2_r, b_avg_r, is_retro)
            results.append(res)
            print()
            print(f"  [{name}]")
            _print_result(res)

    # ------------------------------------------------------------------
    # Section 4: Rankings
    # ------------------------------------------------------------------
    print()
    print("=" * 68)
    print("  Top simulated filters (by improvement)")
    print("=" * 68)

    sufficient = [r for r in results if r["verdict"] not in ("INSUFFICIENT_SAMPLE", "REDUCES_SAMPLE_TOO_MUCH")]

    def _sort_key(r: dict):
        avg_r = r["avg_r"] if r["avg_r"] is not None else -99.0
        base_avg_r = b_avg_r if b_avg_r is not None else 0.0
        d_r = avg_r - base_avg_r
        d_tp1 = r["d_tp1"]
        d_stop = -r["d_stop"]
        d_exp_no_tp1 = -r["exp_no_tp1_rate"]
        return (d_r, d_tp1, d_stop, d_exp_no_tp1)

    ranked = sorted(sufficient, key=_sort_key, reverse=True)[:10]

    if ranked:
        print()
        hdr = (
            f"  {'Filter':<48} {'Kept':>5} {'TP1Δ':>7} {'STPΔ':>7} {'avgRΔ':>8}  Verdict"
        )
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for r in ranked:
            d_tp1_s = f"{r['d_tp1']*100:+.1f}pp" if r["d_tp1"] is not None else "  n/a"
            d_stop_s = f"{r['d_stop']*100:+.1f}pp" if r["d_stop"] is not None else "  n/a"
            d_r_s = f"{r['d_r']:+.3f}R" if r["d_r"] is not None else "   n/a"
            name_trunc = r["name"][:47]
            print(f"  {name_trunc:<48} {r['n_kept']:>5} {d_tp1_s:>7} {d_stop_s:>7} {d_r_s:>8}  {r['verdict']}")
    else:
        print()
        print("  No filters with sufficient sample to rank.")

    # Dangerous filters
    print()
    print("  Filters that look dangerous (worse outcomes):")
    dangerous = [
        r for r in results
        if r["verdict"] in ("WORSE_THAN_BASELINE",)
        or (r["d_tp1"] is not None and r["d_tp1"] <= -0.03 and r["n_kept"] >= args.min_n)
    ]
    if dangerous:
        for r in dangerous:
            d_tp1_s = f"{r['d_tp1']*100:+.1f}pp"
            d_r_s = f"{r['d_r']:+.3f}R" if r["d_r"] is not None else "n/a"
            print(f"    {r['name']:<48}  TP1Δ={d_tp1_s}  avgRΔ={d_r_s}  ({r['verdict']})")
    else:
        print("    None flagged.")

    # ------------------------------------------------------------------
    # Section 4b: Balanced candidates for future review
    # ------------------------------------------------------------------
    print()
    print("=" * 68)
    print("  Balanced candidates for future review")
    print("=" * 68)
    print("  Ranked by balanced score: TP1 improvement (40%), stop reduction (30%),")
    print("  avgR improvement (20%), expiry-without-TP1 reduction (10%).")
    print("  Penalties: high expiry (-0.20), thin sample (-0.10), retrospective label (-0.05).")
    print("  Insufficient-sample filters excluded.")

    eligible = [
        r for r in results
        if r["verdict"] != "INSUFFICIENT_SAMPLE" and r["n_kept"] >= args.min_n
    ]

    for r in eligible:
        is_retro = _is_retrospective(r["name"])
        r["balanced_score"] = _balanced_score(r, b_tp1_r, b_avg_r, is_retro)

    balanced_ranked = sorted(eligible, key=lambda r: r["balanced_score"], reverse=True)[:10]

    if balanced_ranked:
        print()
        hdr2 = (
            f"  {'Filter':<48} {'Kept':>5} {'Kpt%':>5} "
            f"{'TP1Δ':>7} {'STPΔ':>7} {'EXP%':>6} {'EXPnoTP1Δ':>10} {'avgRΔ':>8} "
            f"{'BScore':>7}  Verdict"
        )
        print(hdr2)
        print("  " + "-" * (len(hdr2) - 2))
        for r in balanced_ranked:
            d_tp1_s = f"{r['d_tp1']*100:+.1f}pp"
            d_stop_s = f"{r['d_stop']*100:+.1f}pp"
            d_r_s = f"{r['d_r']:+.3f}R" if r["d_r"] is not None else "   n/a"
            exp_pct = f"{r['exp_rate']*100:.1f}%"
            d_exp_no_tp1 = r["exp_no_tp1_rate"] - r["b_exp_no_tp1_rate"]
            d_exp_no_tp1_s = f"{d_exp_no_tp1*100:+.1f}pp"
            bscore_s = f"{r['balanced_score']:+.3f}"
            name_trunc = r["name"][:47]
            print(
                f"  {name_trunc:<48} {r['n_kept']:>5} {r['kept_pct']:>4.0f}% "
                f"{d_tp1_s:>7} {d_stop_s:>7} {exp_pct:>6} {d_exp_no_tp1_s:>10} {d_r_s:>8} "
                f"{bscore_s:>7}  {r['verdict']}"
            )
    else:
        print()
        print("  No eligible filters to rank.")

    # ------------------------------------------------------------------
    # Section 5: Interpretation
    # ------------------------------------------------------------------
    print()
    print("=" * 68)
    print("  Interpretation")
    print("=" * 68)
    print()

    improving = [r for r in results if r["verdict"] == "IMPROVES_SIGNAL_QUALITY"]
    improves_expiry = [r for r in results if r["verdict"] == "IMPROVES_BUT_EXPIRY_HIGH"]
    mixed = [r for r in results if r["verdict"] == "MIXED_NEEDS_REVIEW"]
    worse = [r for r in results if r["verdict"] == "WORSE_THAN_BASELINE"]
    insuf = [r for r in results if r["verdict"] in ("INSUFFICIENT_SAMPLE", "REDUCES_SAMPLE_TOO_MUCH")]

    high_expiry_remaining = [
        r for r in results
        if r["n_kept"] >= args.min_n and r["exp_rate"] >= 0.80
    ]

    print(
        f"  {len(improving)} IMPROVES_SIGNAL_QUALITY, "
        f"{len(improves_expiry)} IMPROVES_BUT_EXPIRY_HIGH, "
        f"{len(mixed)} MIXED, "
        f"{len(worse)} WORSE_THAN_BASELINE, "
        f"{len(insuf)} insufficient/thin."
    )
    print()
    print("  No runtime changes are recommended automatically.")
    print("  Candidate filters must be validated over more data before")
    print("  becoming real strategy gates. A future milestone (11B) is")
    print("  required before any filter is activated in production.")
    print()

    if improves_expiry:
        print(f"  {len(improves_expiry)} filter(s) labeled IMPROVES_BUT_EXPIRY_HIGH.")
        print("  These filters improve TP1 and/or avgR but expiry remains >= 80%.")
        print("  IMPROVES_BUT_EXPIRY_HIGH is NOT strategy-ready. High expiry at this")
        print("  level indicates the problem may be timing, expiration window, or")
        print("  exit logic rather than entry selection. Applying these filters would")
        print("  concentrate signals in a subset that still expires most of the time.")
        print()

    if high_expiry_remaining:
        names = ", ".join(r["name"] for r in high_expiry_remaining[:3])
        extra = f" (+{len(high_expiry_remaining) - 3} more)" if len(high_expiry_remaining) > 3 else ""
        print(f"  Expiry >= 80% even after applying: {names}{extra}.")
        print("  High expiry is the primary failure mode across most filter subsets.")
        print()

    if improving:
        best = max(improving, key=lambda r: (r["d_r"] or -99))
        print(f"  Best clean-improving filter by avgR: {best['name']}")
        d_r_s = f"{best['d_r']:+.3f}R" if best["d_r"] is not None else "n/a"
        print(f"    Kept: {best['n_kept']} rows ({best['kept_pct']:.1f}% of baseline)  ΔavgR={d_r_s}")
        print()

    print("  RETROSPECTIVE WARNING: Label-based filters (G/H sections) use")
    print("  group quality labels derived from the same dataset being filtered.")
    print("  These results cannot be used as evidence that the filter generalises")
    print("  to future data. They indicate which historical groups had better")
    print("  outcomes, not which future signals will.")
    print()
    print("  No runtime filters should be enabled based on this report alone.")

    # ------------------------------------------------------------------
    # Optional CSV export
    # ------------------------------------------------------------------
    if args.csv:
        csv_path = Path(args.csv)
        fieldnames = [
            "name", "rule", "n_kept", "n_removed", "kept_pct",
            "tp1_rate", "tp2_rate", "stop_rate", "exp_rate", "exp_no_tp1_rate",
            "avg_mfe", "avg_mae", "avg_r",
            "d_tp1", "d_tp2", "d_stop", "d_exp", "d_r",
            "balanced_score", "flags", "verdict",
        ]
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for res in results:
                row = {k: res.get(k) for k in fieldnames}
                row["flags"] = "|".join(res.get("flags", []))
                writer.writerow(row)
        print()
        print(f"  CSV written to: {csv_path}")

    print()
    print("=" * 68)


if __name__ == "__main__":
    main()
