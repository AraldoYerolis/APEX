"""Signal Quality Agent — Learning Report v2 (Milestone 10C).

Read-only analysis of signal_features + signal_observations data.
Covers: symbol/direction performance, long vs short, macro alignment,
feature buckets, expiry quality, diagnostic quality scores.

Usage (run from repo root):
    PYTHONPATH=src python scripts/report_signal_learning.py
    PYTHONPATH=src python scripts/report_signal_learning.py --since 2026-05-01T00:00:00Z
    PYTHONPATH=src python scripts/report_signal_learning.py --limit 200 --csv /tmp/features.csv

Does not start the bot, does not write any data, does not change config.

DIAGNOSTIC QUALITY SCORES AND RECOMMENDATION LABELS ARE REPORT-ONLY.
They are not used for trading, alerts, or strategy filtering.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from apex.config import get_settings
from apex.db.connection import close_db, init_db


# ------------------------------------------------------------------ formatting

def _pct(n: int, total: int, width: int = 6) -> str:
    if total == 0:
        return " " * (width - 3) + "n/a"
    return f"{n / total * 100:{width - 1}.1f}%"


def _avg_r(values: list[float]) -> str:
    if not values:
        return "   n/a"
    return f"{sum(values) / len(values):+.2f}"


def _fmt_mins(secs_list: list[float]) -> str:
    if not secs_list:
        return "n/a"
    return f"{sum(secs_list) / len(secs_list) / 60:.1f}m"


def _stats_line(values: list[float]) -> str:
    if not values:
        return "  n/a"
    mean = sum(values) / len(values)
    lo = min(values)
    hi = max(values)
    return f"mean={mean:+.2f}  min={lo:+.2f}  max={hi:+.2f}  n={len(values)}"


# ------------------------------------------------------------------ bucketing

def _rsi_bucket(v: Optional[float]) -> str:
    if v is None:
        return "unknown"
    if v < 40:
        return "<40"
    if v < 45:
        return "40-45"
    if v < 50:
        return "45-50"
    if v < 55:
        return "50-55"
    if v < 60:
        return "55-60"
    return ">60"


def _atr_bucket(v: Optional[float]) -> str:
    if v is None:
        return "unknown"
    if v < 0.10:
        return "<0.10"
    if v < 0.25:
        return "0.10-0.25"
    if v < 0.50:
        return "0.25-0.50"
    if v < 1.00:
        return "0.50-1.00"
    return ">1.00"


def _vwap_bucket(v: Optional[float]) -> str:
    if v is None:
        return "unknown"
    if v < -0.25:
        return "below -0.25"
    if v < 0:
        return "-0.25 to 0"
    if v < 0.25:
        return "0 to +0.25"
    if v < 0.50:
        return "+0.25 to +0.50"
    return "above +0.50"


def _ema_spread_bucket(v: Optional[float]) -> str:
    if v is None:
        return "unknown"
    if v < -0.25:
        return "below -0.25"
    if v < 0:
        return "-0.25 to 0"
    if v < 0.25:
        return "0 to +0.25"
    if v < 0.50:
        return "+0.25 to +0.50"
    return "above +0.50"


# ------------------------------------------------------------------ macro alignment

def _macro_alignment_label(direction: str, bias: Optional[str]) -> str:
    """Return aligned/opposed/neutral for a signal direction vs a macro bias."""
    if not bias or bias == "NONE":
        return "neutral"
    if direction == "LONG":
        if bias == "LONG":
            return "aligned"
        if bias == "SHORT":
            return "opposed"
    elif direction == "SHORT":
        if bias == "SHORT":
            return "aligned"
        if bias == "LONG":
            return "opposed"
    return "neutral"


# ------------------------------------------------------------------ outcome stats helper

def _outcome_stats(rows: list[Any]) -> dict:
    """Compute outcome statistics for a list of signal_features rows (closed confirmed).

    Expects rows from the JOIN query that includes max_favorable_excursion,
    max_adverse_excursion from signal_observations.
    """
    n = len(rows)
    tp1 = [r for r in rows if r["hit_1r_at"] is not None]
    tp2 = [r for r in rows if r["outcome_status"] == "HIT_2R"]
    stopped = [r for r in rows if r["outcome_status"] == "STOPPED"]
    expired = [r for r in rows if r["outcome_status"] == "EXPIRED"]
    exp_after_tp1 = [r for r in expired if r["hit_1r_before_expiry"] == 1]
    exp_no_tp1 = [r for r in expired if r["hit_1r_before_expiry"] == 0]

    mfe_vals = [r["so_mfe"] for r in rows if r["so_mfe"] is not None]
    mae_vals = [r["so_mae"] for r in rows if r["so_mae"] is not None]
    out_r_vals = [r["outcome_r"] for r in rows if r["outcome_r"] is not None]
    t1_secs = [r["time_to_1r_seconds"] for r in rows if r["time_to_1r_seconds"] is not None]
    t_stop_secs = [r["time_to_stop_seconds"] for r in rows if r["time_to_stop_seconds"] is not None]

    return {
        "n": n,
        "tp1": len(tp1),
        "tp2": len(tp2),
        "stopped": len(stopped),
        "expired": len(expired),
        "exp_after_tp1": len(exp_after_tp1),
        "exp_no_tp1": len(exp_no_tp1),
        "mfe_vals": mfe_vals,
        "mae_vals": mae_vals,
        "out_r_vals": out_r_vals,
        "t1_secs": t1_secs,
        "t_stop_secs": t_stop_secs,
    }


# ------------------------------------------------------------------ scoring / labels

def _quality_score(
    tp1_rate: float,
    tp2_rate: float,
    stop_rate: float,
    expiry_no_tp1_rate: float,
    avg_mfe: Optional[float],
    avg_mae: Optional[float],
    avg_outcome_r: Optional[float],
    n: int,
    overall_tp1_rate: float = 0.20,
    overall_stop_rate: float = 0.20,
) -> Optional[float]:
    """Compute a diagnostic quality score in [0, 1].

    REPORT-ONLY. Not used for trading, alerts, or config changes.

    Starts from a 0.50 baseline and adjusts relative to overall rates.
    This produces a useful spread rather than clamping most groups to 0.00.

    Formula:
      score = 0.50
            + (tp1_rate  - overall_tp1_rate)  * 1.0   # relative TP1 reward
            + tp2_rate * 0.80                          # absolute TP2 reward
            - (stop_rate - overall_stop_rate) * 0.70   # relative stop penalty
            - expiry_no_tp1_rate * 0.40                # expiry-without-TP1 penalty
            + MFE bonus if avg_mfe > 1.0R
            - MAE penalty if avg_mae > 0.8R
            - outcome_r penalty if avg_outcome_r < -0.5
      Clamped to [0, 1].
    """
    if n < 10:
        return None
    score = 0.50
    score += (tp1_rate - overall_tp1_rate) * 1.0
    score += tp2_rate * 0.80
    score -= (stop_rate - overall_stop_rate) * 0.70
    score -= expiry_no_tp1_rate * 0.40
    if avg_mfe is not None and avg_mfe > 1.0:
        score += 0.04
    if avg_mae is not None and avg_mae > 0.8:
        score -= 0.04
    if avg_outcome_r is not None and avg_outcome_r < -0.5:
        score -= 0.05
    return max(0.0, min(1.0, score))


def _recommendation_label(
    score: Optional[float],
    tp1_rate: float,
    stop_rate: float,
    expiry_rate: float,
    n: int,
    overall_tp1_rate: float,
    overall_stop_rate: float,
) -> str:
    """Assign a recommendation label for a symbol+direction group.

    REPORT-ONLY. Labels (checked in order):
      INSUFFICIENT_SAMPLE       — N < 10 or score is None
      WATCHLIST_NEEDS_EXPIRY_FIX — expiry >= 80% but TP1 or stop suggests some
                                   signal quality worth watching
      WEAK_OBSERVE_ONLY         — low TP1, high stop, terrible score, or
                                   extreme expiry without redeeming TP1
      PROMISING_OBSERVE         — above-average TP1, acceptable stop, expiry < 80%,
                                   score clearly above minimum threshold
      NEEDS_FILTERING           — moderate performance, not promising, not terrible

    Hard constraints:
      - Score <= 0.05 → never PROMISING_OBSERVE
      - Expiry >= 0.80 → never PROMISING_OBSERVE
    """
    if n < 10 or score is None:
        return "INSUFFICIENT_SAMPLE"

    promising_tp1_threshold = overall_tp1_rate * 1.25
    weak_tp1_threshold = overall_tp1_rate * 0.75
    high_stop_threshold = max(overall_stop_rate * 1.25, 0.30)

    # Extreme expiry: separate into watchlist vs weak based on TP1/stop quality
    if expiry_rate >= 0.80:
        has_above_avg_tp1 = tp1_rate >= overall_tp1_rate * 1.10
        has_good_stop = stop_rate <= overall_stop_rate * 0.90
        if has_above_avg_tp1 or has_good_stop:
            return "WATCHLIST_NEEDS_EXPIRY_FIX"
        return "WEAK_OBSERVE_ONLY"

    # Terrible score gate
    if score <= 0.05:
        return "WEAK_OBSERVE_ONLY"

    # Weak on TP1 or high stop
    if tp1_rate < weak_tp1_threshold or stop_rate > 0.35:
        return "WEAK_OBSERVE_ONLY"

    # Promising: above-average TP1, acceptable stop, expiry already < 80%
    if tp1_rate >= promising_tp1_threshold and stop_rate <= high_stop_threshold:
        return "PROMISING_OBSERVE"

    return "NEEDS_FILTERING"


# ------------------------------------------------------------------ bucket table printer

_BUCKET_ORDERS = {
    "rsi": ["<40", "40-45", "45-50", "50-55", "55-60", ">60", "unknown"],
    "atr": ["<0.10", "0.10-0.25", "0.25-0.50", "0.50-1.00", ">1.00", "unknown"],
    "vwap": ["below -0.25", "-0.25 to 0", "0 to +0.25", "+0.25 to +0.50", "above +0.50", "unknown"],
    "ema": ["below -0.25", "-0.25 to 0", "0 to +0.25", "+0.25 to +0.50", "above +0.50", "unknown"],
}


def _print_bucket_table(
    rows: list[Any],
    label: str,
    bucket_fn,
    bucket_key: str,
    overall_tp1_rate: float,
    overall_stop_rate: float,
    overall_expiry_rate: float,
) -> None:
    """Print a bucket analysis table and interpretation notes."""
    buckets: dict[str, list] = defaultdict(list)
    for r in rows:
        key = bucket_fn(r[label] if label in dict(r).keys() else None)
        buckets[key].append(r)

    ordering = _BUCKET_ORDERS.get(bucket_key, sorted(buckets.keys()))
    active = [b for b in ordering if b in buckets]
    if not active:
        return

    hdr = (
        f"  {'Bucket':<18} {'N':>4} {'TP1%':>6} {'TP2%':>6} "
        f"{'STOP%':>6} {'EXP%':>6} {'avgMFE':>7} {'avgMAE':>7} {'avgR':>7}"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    interpretations = []
    for bucket in active:
        b_rows = buckets[bucket]
        st = _outcome_stats(b_rows)
        n = st["n"]
        tp1_r = st["tp1"] / n if n else 0
        tp2_r = st["tp2"] / n if n else 0
        stop_r = st["stopped"] / n if n else 0
        exp_r = st["expired"] / n if n else 0
        mfe_s = _avg_r(st["mfe_vals"])
        mae_s = _avg_r(st["mae_vals"])
        out_s = _avg_r(st["out_r_vals"])
        print(
            f"  {bucket:<18} {n:>4} {_pct(st['tp1'], n):>6} {_pct(st['tp2'], n):>6} "
            f"{_pct(st['stopped'], n):>6} {_pct(st['expired'], n):>6} "
            f"{mfe_s:>7} {mae_s:>7} {out_s:>7}"
        )
        # Interpretation flags
        if n >= 10:
            notes = []
            if tp1_r > overall_tp1_rate * 1.3:
                notes.append(f"TP1 above average ({tp1_r:.0%} vs {overall_tp1_rate:.0%} avg)")
            if exp_r > overall_expiry_rate * 1.2 and exp_r > 0.70:
                notes.append(f"high expiry ({exp_r:.0%})")
            if stop_r > overall_stop_rate * 1.3 and stop_r > 0.25:
                notes.append(f"elevated stop rate ({stop_r:.0%})")
            if notes:
                interpretations.append(f"    {bucket}: {'; '.join(notes)}")

    if interpretations:
        print()
        print("  Interpretation:")
        for note in interpretations:
            print(note)


# ------------------------------------------------------------------ main

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="APEX signal learning report v2 (10C)")
    parser.add_argument(
        "--since",
        metavar="ISO_TIMESTAMP",
        default=None,
        help="Filter features captured at >= TIMESTAMP",
    )
    parser.add_argument(
        "--limit",
        metavar="N",
        type=int,
        default=None,
        help="Limit rows loaded (default: unlimited). For debugging only.",
    )
    parser.add_argument(
        "--csv",
        metavar="PATH",
        default=None,
        help="Also write features to a CSV file at PATH",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    conn = init_db(settings.apex_db_path)

    # Join signal_features with signal_observations to get MFE/MAE
    # All reads — never modifies any table.
    try:
        conditions = []
        params: list[Any] = []
        if args.since:
            conditions.append("sf.captured_at >= ?")
            params.append(args.since)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        order = "ORDER BY sf.captured_at DESC"
        limit_clause = ""
        if args.limit is not None:
            limit_clause = "LIMIT ?"
            params.append(args.limit)
        query = f"""
            SELECT sf.*,
                   so.max_favorable_excursion AS so_mfe,
                   so.max_adverse_excursion   AS so_mae
            FROM signal_features sf
            LEFT JOIN signal_observations so
                   ON sf.observation_uid = so.observation_uid
            {where} {order} {limit_clause}
        """
        rows = conn.execute(query, params).fetchall()
    except Exception as e:
        print(f"ERROR: could not query signal_features: {e}", file=sys.stderr)
        print("Has the DB been initialized with the latest schema?", file=sys.stderr)
        close_db()
        sys.exit(1)

    total = len(rows)

    print()
    print("=" * 68)
    print("  APEX Signal Learning Report  [v2 / Milestone 10C]")
    print("=" * 68)
    print("  NOTE: Quality scores and recommendation labels are DIAGNOSTIC")
    print("  ONLY. They are not used for trading, alerts, or config.")
    print()
    if args.since:
        print(f"  Filtered since      : {args.since}")
    print(f"  Rows loaded          : {total}")
    print(f"  Limit                : {args.limit if args.limit is not None else 'none'}")

    if total == 0:
        print()
        print("  No signal features captured yet.")
        print()
        print("  Tip: feature capture starts automatically once an observation is")
        print("  recorded. Run the scanner in dry-run mode to populate this table.")
        print()
        print("=" * 68)
        close_db()
        return

    confirmed = [r for r in rows if r["signal_type"] == "CONFIRMED_SETUP"]
    forming = [r for r in rows if r["signal_type"] == "SETUP_FORMING"]

    with_outcome = [r for r in confirmed if r["outcome_status"] not in (None, "OBSERVED")]
    without_outcome = [r for r in confirmed if r["outcome_status"] in (None, "OBSERVED")]

    print()
    print(f"  Total features      : {total}")
    print(f"    CONFIRMED_SETUP   : {len(confirmed)}")
    print(f"    SETUP_FORMING     : {len(forming)}")
    if confirmed:
        print(f"    With outcomes     : {len(with_outcome)}  {_pct(len(with_outcome), len(confirmed))} of confirmed")
        print(f"    Without outcomes  : {len(without_outcome)}  (open/pending)")

    if not confirmed:
        print()
        print("  No CONFIRMED_SETUP features yet.")
        print()
        print("=" * 68)
        close_db()
        return

    # Pre-compute overall rates from all closed confirmed for later comparisons
    overall_st = _outcome_stats(with_outcome) if with_outcome else _outcome_stats([])
    n_closed = overall_st["n"]
    overall_tp1_rate = overall_st["tp1"] / n_closed if n_closed else 0.0
    overall_tp2_rate = overall_st["tp2"] / n_closed if n_closed else 0.0
    overall_stop_rate = overall_st["stopped"] / n_closed if n_closed else 0.0
    overall_expiry_rate = overall_st["expired"] / n_closed if n_closed else 0.0

    # ------------------------------------------------------------------ outcome summary
    if with_outcome:
        st = overall_st
        expired_all = [r for r in with_outcome if r["outcome_status"] == "EXPIRED"]
        exp_after_tp1 = [r for r in expired_all if r["hit_1r_before_expiry"] == 1]
        exp_no_tp1 = [r for r in expired_all if r["hit_1r_before_expiry"] == 0]

        print()
        print("  Outcome summary (CONFIRMED, closed):")
        print(f"    TP1 milestone hit : {st['tp1']:4d}  {_pct(st['tp1'], n_closed)}")
        print(f"    HIT_2R            : {st['tp2']:4d}  {_pct(st['tp2'], n_closed)}")
        print(f"    STOPPED           : {st['stopped']:4d}  {_pct(st['stopped'], n_closed)}")
        print(f"    EXPIRED (total)   : {st['expired']:4d}  {_pct(st['expired'], n_closed)}")
        if expired_all:
            print(f"      ↳ after TP1     : {len(exp_after_tp1):4d}  {_pct(len(exp_after_tp1), len(expired_all))} of expired")
            print(f"      ↳ without TP1   : {len(exp_no_tp1):4d}  {_pct(len(exp_no_tp1), len(expired_all))} of expired")
        print(f"    Avg MFE           : {_avg_r(st['mfe_vals'])} R  (n={len(st['mfe_vals'])})")
        print(f"    Avg MAE           : {_avg_r(st['mae_vals'])} R  (n={len(st['mae_vals'])})")
        print(f"    Avg outcome R     : {_avg_r(st['out_r_vals'])} R  (n={len(st['out_r_vals'])})")

    # ------------------------------------------------------------------ long vs short
    print()
    print("  Long vs Short breakdown (CONFIRMED, closed):")
    dir_groups = {"LONG": [], "SHORT": []}
    for r in with_outcome:
        d = r["direction"]
        if d in dir_groups:
            dir_groups[d].append(r)

    n_long = len(dir_groups["LONG"])
    n_short = len(dir_groups["SHORT"])
    n_dir_total = n_long + n_short
    if n_dir_total > 0:
        long_pct = n_long / n_dir_total * 100
        short_pct = n_short / n_dir_total * 100
        print(f"    LONG: {n_long} ({long_pct:.0f}%)   SHORT: {n_short} ({short_pct:.0f}%)")
        if n_short < 10:
            print(f"    WARNING: SHORT sample is very small (N={n_short}). Do not draw conclusions from SHORT stats.")
        hdr = (
            f"  {'Dir':<6} {'N':>4} {'TP1%':>6} {'TP2%':>6} "
            f"{'STOP%':>6} {'EXP%':>6} {'avgMFE':>7} {'avgMAE':>7} {'avgR':>7}"
        )
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for direction in ["LONG", "SHORT"]:
            g = dir_groups[direction]
            n = len(g)
            if n == 0:
                print(f"  {direction:<6} {n:>4}   (no data)")
                continue
            st = _outcome_stats(g)
            print(
                f"  {direction:<6} {n:>4} {_pct(st['tp1'], n):>6} {_pct(st['tp2'], n):>6} "
                f"{_pct(st['stopped'], n):>6} {_pct(st['expired'], n):>6} "
                f"{_avg_r(st['mfe_vals']):>7} {_avg_r(st['mae_vals']):>7} {_avg_r(st['out_r_vals']):>7}"
            )

    # ------------------------------------------------------------------ symbol + direction breakdown
    print()
    print("  Symbol + direction breakdown (CONFIRMED):")
    print("  Min N=10 required for quality score and recommendation label.")
    print("  DIAGNOSTIC ONLY — not used for live decisions.")
    print()

    # Build groups: all confirmed (open + closed) for totals; closed for metrics
    sd_all: dict[tuple[str, str], list] = defaultdict(list)
    sd_closed: dict[tuple[str, str], list] = defaultdict(list)
    for r in confirmed:
        key = (r["symbol"], r["direction"])
        sd_all[key].append(r)
        if r["outcome_status"] not in (None, "OBSERVED"):
            sd_closed[key].append(r)

    # Sort by total confirmed descending, then symbol ascending
    sorted_keys = sorted(sd_all.keys(), key=lambda k: (-len(sd_all[k]), k[0]))

    # Print header
    hdr3 = (
        f"  {'Symbol':<12} {'Dir':<6} {'Tot':>4} {'Cls':>4} {'Opn':>4} "
        f"{'TP1%':>6} {'TP2%':>6} {'STOP%':>6} {'EXP%':>6} "
        f"{'avgMFE':>7} {'avgMAE':>7} {'avgR':>6} "
        f"{'Score':>6}  Label"
    )
    print(hdr3)
    print("  " + "-" * (len(hdr3) - 2))

    scores_and_labels: dict[tuple[str, str], tuple[Optional[float], str]] = {}

    for key in sorted_keys:
        sym, direction = key
        all_rows = sd_all[key]
        closed_rows = sd_closed.get(key, [])
        n_total = len(all_rows)
        n_closed_g = len(closed_rows)
        n_open_g = n_total - n_closed_g

        if closed_rows:
            st = _outcome_stats(closed_rows)
            tp1_r = st["tp1"] / n_closed_g
            tp2_r = st["tp2"] / n_closed_g
            stop_r = st["stopped"] / n_closed_g
            exp_r = st["expired"] / n_closed_g
            exp_no_tp1_r = st["exp_no_tp1"] / n_closed_g if n_closed_g else 0
            avg_mfe = sum(st["mfe_vals"]) / len(st["mfe_vals"]) if st["mfe_vals"] else None
            avg_mae = sum(st["mae_vals"]) / len(st["mae_vals"]) if st["mae_vals"] else None

            avg_out_r = sum(st["out_r_vals"]) / len(st["out_r_vals"]) if st["out_r_vals"] else None
            score = _quality_score(
                tp1_r, tp2_r, stop_r, exp_no_tp1_r, avg_mfe, avg_mae, avg_out_r,
                n_closed_g, overall_tp1_rate, overall_stop_rate,
            )
            label = _recommendation_label(
                score, tp1_r, stop_r, exp_r, n_closed_g, overall_tp1_rate, overall_stop_rate
            )
            scores_and_labels[key] = (score, label)

            score_s = f"{score:.2f}" if score is not None else "  n/a"
            print(
                f"  {sym:<12} {direction:<6} {n_total:>4} {n_closed_g:>4} {n_open_g:>4} "
                f"{_pct(st['tp1'], n_closed_g):>6} {_pct(st['tp2'], n_closed_g):>6} "
                f"{_pct(st['stopped'], n_closed_g):>6} {_pct(st['expired'], n_closed_g):>6} "
                f"{_avg_r(st['mfe_vals']):>7} {_avg_r(st['mae_vals']):>7} "
                f"{_avg_r(st['out_r_vals']):>6} "
                f"{score_s:>6}  {label}"
            )
        else:
            scores_and_labels[key] = (None, "INSUFFICIENT_SAMPLE")
            print(
                f"  {sym:<12} {direction:<6} {n_total:>4} {'0':>4} {n_open_g:>4} "
                f"{'n/a':>6} {'n/a':>6} {'n/a':>6} {'n/a':>6} "
                f"{'n/a':>7} {'n/a':>7} {'n/a':>6} "
                f"{'n/a':>6}  INSUFFICIENT_SAMPLE"
            )

    # Summary of labels
    label_counts: dict[str, int] = defaultdict(int)
    for (s, label) in scores_and_labels.values():
        label_counts[label] += 1
    print()
    print("  Label summary (symbol+direction groups):")
    for lbl in ["PROMISING_OBSERVE", "WATCHLIST_NEEDS_EXPIRY_FIX", "NEEDS_FILTERING", "WEAK_OBSERVE_ONLY", "INSUFFICIENT_SAMPLE"]:
        cnt = label_counts.get(lbl, 0)
        if cnt:
            print(f"    {lbl:<25} : {cnt}")

    # ------------------------------------------------------------------ macro alignment
    print()
    print("  Macro alignment analysis (CONFIRMED, closed)")
    print("  Aligned = setup direction matches macro bias. Report-only.")
    print()

    for macro_field, macro_label in [("btc_trend_bias", "BTC"), ("eth_trend_bias", "ETH")]:
        align_groups: dict[str, list] = {"aligned": [], "opposed": [], "neutral": []}
        for r in with_outcome:
            bias = r[macro_field]
            lbl = _macro_alignment_label(r["direction"], bias)
            align_groups[lbl].append(r)

        counts = {k: len(v) for k, v in align_groups.items()}
        if sum(counts.values()) == 0:
            print(f"  {macro_label}: no data")
            continue

        print(f"  {macro_label} trend bias alignment:")
        hdr4 = (
            f"  {'Group':<10} {'N':>4} {'TP1%':>6} {'TP2%':>6} "
            f"{'STOP%':>6} {'EXP%':>6} {'avgMFE':>7} {'avgMAE':>7} {'avgR':>7}"
        )
        print(hdr4)
        print("  " + "-" * (len(hdr4) - 2))
        for grp in ["aligned", "opposed", "neutral"]:
            g = align_groups[grp]
            n = len(g)
            if n == 0:
                print(f"  {grp:<10} {n:>4}   (no data)")
                continue
            st = _outcome_stats(g)
            print(
                f"  {grp:<10} {n:>4} {_pct(st['tp1'], n):>6} {_pct(st['tp2'], n):>6} "
                f"{_pct(st['stopped'], n):>6} {_pct(st['expired'], n):>6} "
                f"{_avg_r(st['mfe_vals']):>7} {_avg_r(st['mae_vals']):>7} {_avg_r(st['out_r_vals']):>7}"
            )
        print()

    # ------------------------------------------------------------------ expiry quality
    print()
    print("  Expiry quality (CONFIRMED, closed)")
    print("  Expired-after-TP1 is less damaging than expired-without-TP1.")
    print()
    all_expired = [r for r in with_outcome if r["outcome_status"] == "EXPIRED"]
    exp_aft = [r for r in all_expired if r["hit_1r_before_expiry"] == 1]
    exp_wo = [r for r in all_expired if r["hit_1r_before_expiry"] == 0]
    exp_unk = [r for r in all_expired if r["hit_1r_before_expiry"] is None]
    n_exp = len(all_expired)

    print(f"  Total expired         : {n_exp}")
    print(f"    After TP1           : {len(exp_aft):4d}  {_pct(len(exp_aft), n_exp)}")
    print(f"    Without TP1         : {len(exp_wo):4d}  {_pct(len(exp_wo), n_exp)}")
    if exp_unk:
        print(f"    Flag unknown/NULL   : {len(exp_unk):4d}")

    # Per-symbol expiry breakdown (symbols with >= 5 expired)
    exp_by_sym: dict[str, dict] = defaultdict(lambda: {"total": 0, "after_tp1": 0, "no_tp1": 0})
    for r in all_expired:
        s = r["symbol"]
        exp_by_sym[s]["total"] += 1
        if r["hit_1r_before_expiry"] == 1:
            exp_by_sym[s]["after_tp1"] += 1
        elif r["hit_1r_before_expiry"] == 0:
            exp_by_sym[s]["no_tp1"] += 1

    sym_with_enough = [(s, d) for s, d in exp_by_sym.items() if d["total"] >= 5]
    if sym_with_enough:
        print()
        print("  Expiry breakdown by symbol (N>=5 expired):")
        hdr5 = f"  {'Symbol':<12} {'ExpTot':>7} {'AfterTP1':>9} {'NoTP1':>7} {'NoTP1%':>8}"
        print(hdr5)
        print("  " + "-" * (len(hdr5) - 2))
        for sym, d in sorted(sym_with_enough, key=lambda x: -x[1]["total"]):
            print(
                f"  {sym:<12} {d['total']:>7} {d['after_tp1']:>9} {d['no_tp1']:>7} "
                f"{_pct(d['no_tp1'], d['total']):>8}"
            )

    # ------------------------------------------------------------------ feature buckets
    print()
    print("  Feature bucket analysis (CONFIRMED, closed)")
    print("  Buckets with N<10 are shown but not interpreted.")
    print()

    bucket_configs = [
        ("RSI at capture", "rsi_val", _rsi_bucket, "rsi"),
        ("ATR %price", "atr_pct", _atr_bucket, "atr"),
        ("Price vs VWAP %", "price_vs_vwap_pct", _vwap_bucket, "vwap"),
        ("EMA spread %", "ema_spread_pct", _ema_spread_bucket, "ema"),
    ]

    for section_label, field_name, bucket_fn, bucket_key in bucket_configs:
        # Need a wrapper so _print_bucket_table can look up the field
        # Patch: pass field_name as the label key
        print(f"  -- {section_label} --")
        # Build rows dict with consistent field access
        bucket_rows = [r for r in with_outcome if True]  # all closed confirmed
        _print_bucket_table(
            bucket_rows,
            field_name,
            bucket_fn,
            bucket_key,
            overall_tp1_rate,
            overall_stop_rate,
            overall_expiry_rate,
        )
        print()

    # ------------------------------------------------------------------ TP1-hit vs TP1-miss
    _INDICATOR_FIELDS = [
        ("rsi_val",           "RSI"),
        ("atr_pct",           "ATR %price"),
        ("price_vs_vwap_pct", "Price vs VWAP %"),
        ("ema_spread_pct",    "EMA spread %"),
    ]

    tp1_hit_rows = [r for r in with_outcome if r["hit_1r_at"] is not None]
    tp1_miss_rows = [r for r in with_outcome if r["hit_1r_at"] is None]
    if tp1_hit_rows and tp1_miss_rows:
        print()
        print("  Indicator split: TP1-hit vs TP1-miss (CONFIRMED, closed):")
        hdr6 = f"  {'Field':<22} {'TP1-hit mean':>14} {'TP1-miss mean':>14} {'Diff':>8}"
        print(hdr6)
        print("  " + "-" * (len(hdr6) - 2))
        for field_name, lbl in _INDICATOR_FIELDS:
            hit_vals = [r[field_name] for r in tp1_hit_rows if r[field_name] is not None]
            miss_vals = [r[field_name] for r in tp1_miss_rows if r[field_name] is not None]
            hit_mean = sum(hit_vals) / len(hit_vals) if hit_vals else None
            miss_mean = sum(miss_vals) / len(miss_vals) if miss_vals else None
            hit_s = f"{hit_mean:+.2f}" if hit_mean is not None else "   n/a"
            miss_s = f"{miss_mean:+.2f}" if miss_mean is not None else "   n/a"
            diff_s = f"{hit_mean - miss_mean:+.2f}" if (hit_mean is not None and miss_mean is not None) else "   n/a"
            print(f"  {lbl:<22} {hit_s:>14} {miss_s:>14} {diff_s:>8}")

    if confirmed:
        print()
        print("  Indicator snapshot distribution (CONFIRMED, all):")
        hdr7 = f"  {'Field':<22} {'N':>4}   Stats"
        print(hdr7)
        print("  " + "-" * (len(hdr7) - 2))
        for field_name, lbl in _INDICATOR_FIELDS:
            vals = [r[field_name] for r in confirmed if r[field_name] is not None]
            print(f"  {lbl:<22} {len(vals):>4}   {_stats_line(vals)}")

    # ------------------------------------------------------------------ top actionable findings
    # Report-only. Does not recommend enabling alerts or changing runtime filters.
    if with_outcome and n_closed >= 10:
        findings: list[str] = []

        # --- Symbol/direction findings ---
        promising = [(k, sl) for k, sl in scores_and_labels.items() if sl[1] == "PROMISING_OBSERVE"]
        watchlist = [(k, sl) for k, sl in scores_and_labels.items() if sl[1] == "WATCHLIST_NEEDS_EXPIRY_FIX"]
        weak = [(k, sl) for k, sl in scores_and_labels.items() if sl[1] == "WEAK_OBSERVE_ONLY"]

        if promising:
            sym_strs = [f"{s}/{d}" for (s, d), _ in sorted(promising)]
            findings.append(f"Candidate future review: {', '.join(sym_strs)} labeled PROMISING_OBSERVE — worth monitoring when N grows.")
        if watchlist:
            sym_strs = [f"{s}/{d}" for (s, d), _ in sorted(watchlist)]
            findings.append(f"Candidate future filter: {', '.join(sym_strs)} labeled WATCHLIST_NEEDS_EXPIRY_FIX — setup quality present but expiry too high.")
        if weak:
            sym_strs = [f"{s}/{d}" for (s, d), _ in sorted(weak)]
            findings.append(f"Candidate future filter: {', '.join(sym_strs)} labeled WEAK_OBSERVE_ONLY — low signal quality, no action recommended.")

        # --- Feature bucket findings (RSI and ATR, most actionable) ---
        for section_label_fb, field_name_fb, bucket_fn_fb, bucket_key_fb in [
            ("RSI", "rsi_val", _rsi_bucket, "rsi"),
            ("ATR %price", "atr_pct", _atr_bucket, "atr"),
        ]:
            fb: dict[str, list] = defaultdict(list)
            for r in with_outcome:
                fb[bucket_fn_fb(r[field_name_fb])].append(r)

            best_tp1_bucket = None
            best_tp1_rate_fb = -1.0
            worst_stop_bucket = None
            worst_stop_rate_fb = -1.0

            for bkt, bkt_rows in fb.items():
                if len(bkt_rows) < 10 or bkt == "unknown":
                    continue
                bst = _outcome_stats(bkt_rows)
                n_b = bst["n"]
                bkt_tp1 = bst["tp1"] / n_b
                bkt_stop = bst["stopped"] / n_b
                if bkt_tp1 > best_tp1_rate_fb and bkt_tp1 > overall_tp1_rate * 1.2:
                    best_tp1_rate_fb = bkt_tp1
                    best_tp1_bucket = (bkt, bkt_tp1, bkt_stop, n_b)
                if bkt_stop > worst_stop_rate_fb and bkt_stop > overall_stop_rate * 1.3:
                    worst_stop_rate_fb = bkt_stop
                    worst_stop_bucket = (bkt, bkt_stop, n_b)

            if best_tp1_bucket:
                bkt, btp1, bstop, bn = best_tp1_bucket
                findings.append(
                    f"Candidate future review: {section_label_fb} bucket '{bkt}' has above-average TP1 "
                    f"({btp1:.0%} vs {overall_tp1_rate:.0%} avg), N={bn}."
                )
            if worst_stop_bucket:
                bkt, bstop, bn = worst_stop_bucket
                findings.append(
                    f"Candidate future filter: avoid {section_label_fb} '{bkt}' — stop rate elevated "
                    f"({bstop:.0%} vs {overall_stop_rate:.0%} avg), N={bn}."
                )

        # --- Macro alignment finding ---
        for macro_field_fa, macro_label_fa in [("btc_trend_bias", "BTC"), ("eth_trend_bias", "ETH")]:
            ag: dict[str, list] = {"aligned": [], "opposed": [], "neutral": []}
            for r in with_outcome:
                ag[_macro_alignment_label(r["direction"], r[macro_field_fa])].append(r)
            n_al = len(ag["aligned"])
            n_op = len(ag["opposed"])
            if n_al >= 10 and n_op >= 10:
                al_st = _outcome_stats(ag["aligned"])
                op_st = _outcome_stats(ag["opposed"])
                al_tp1 = al_st["tp1"] / n_al
                op_tp1 = op_st["tp1"] / n_op
                if op_tp1 > al_tp1 * 1.20:
                    findings.append(
                        f"Note: {macro_label_fa}-opposed setups have higher TP1 than aligned "
                        f"({op_tp1:.0%} vs {al_tp1:.0%}). Sample may be unrepresentative — monitor."
                    )

        if findings:
            print()
            print("  Top actionable diagnostic findings")
            print("  Report-only: do not change runtime filters without a separate milestone.")
            print()
            for i, finding in enumerate(findings, 1):
                # Wrap at ~80 chars
                prefix = f"    {i}. "
                words = finding.split()
                line = prefix
                for word in words:
                    if len(line) + len(word) + 1 > 80:
                        print(line)
                        line = "       " + word
                    else:
                        line += ("" if line == prefix else " ") + word
                if line.strip():
                    print(line)

    # ------------------------------------------------------------------ feature versions
    ver_counts: dict[str, int] = defaultdict(int)
    for r in rows:
        ver_counts[r["feature_version"] or "unknown"] += 1
    if ver_counts:
        print()
        print("  Feature versions captured:")
        for ver, cnt in sorted(ver_counts.items()):
            print(f"    {ver:<20} : {cnt}")

    # ------------------------------------------------------------------ recent 10
    print()
    print(f"  Recent {min(10, total)} features:")
    print(
        f"  {'UID':>10}  {'Symbol':<10} {'Dir':<5} "
        f"{'RSI':>5} {'ATR%':>5} {'VWAP%':>6} {'BTC':>5}  {'Outcome':<9} {'R':>5}"
    )
    print("  " + "-" * 80)
    for r in rows[:10]:
        uid_short = r["observation_uid"][:8]
        rsi_s = f"{r['rsi_val']:.1f}" if r["rsi_val"] is not None else "  -"
        atr_s = f"{r['atr_pct']:.2f}" if r["atr_pct"] is not None else "  -"
        vwap_s = f"{r['price_vs_vwap_pct']:+.1f}" if r["price_vs_vwap_pct"] is not None else "  -"
        btc_s = (r["btc_trend_bias"] or "-")[:5]
        outcome_s = r["outcome_status"] or "OPEN"
        r_s = f"{r['outcome_r']:+.1f}" if r["outcome_r"] is not None else "  -"
        print(
            f"  {uid_short:>10}  {r['symbol']:<10} {r['direction']:<5} "
            f"{rsi_s:>5} {atr_s:>5} {vwap_s:>6} {btc_s:>5}  "
            f"{outcome_s:<9} {r_s:>5}"
        )

    print()
    print("=" * 68)

    # ------------------------------------------------------------------ CSV export
    if args.csv:
        try:
            csv_path = Path(args.csv)
            if rows:
                fieldnames = [k for k in rows[0].keys()]
                with open(csv_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    for r in rows:
                        writer.writerow(dict(r))
            print(f"  CSV written to: {csv_path}  ({total} rows)")
        except Exception as e:
            print(f"  WARNING: CSV export failed: {e}", file=sys.stderr)

    close_db()


if __name__ == "__main__":
    main()
