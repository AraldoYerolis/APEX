"""Out-of-Sample Filter Validation — Milestone 11B.

Splits closed CONFIRMED_SETUP rows chronologically into a train set and a
validation set, then measures whether candidate filters that improved metrics
on the train set continue to improve metrics on the held-out validation set.

REPORT-ONLY. Does not change runtime scanning, alerts, or trading.
Does not write to the database. Does not enable live mode.
Validation results are not written into runtime config.

Usage (run from repo root):
    PYTHONPATH=src python scripts/validate_strategy_filters.py
    PYTHONPATH=src python scripts/validate_strategy_filters.py --since 2026-05-20T10:28:00Z
    PYTHONPATH=src python scripts/validate_strategy_filters.py --since 2026-05-20T10:28:00Z --split 0.70 --min-n 10
    PYTHONPATH=src python scripts/validate_strategy_filters.py --since 2026-05-20T10:28:00Z --csv /tmp/filter_validation.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any, Optional

# -----------------------------------------------------------------------
# sys.path bootstrap — mirrors simulate_strategy_filters.py and
# create_apex_snapshot.py so PYTHONPATH=src is sufficient.
# -----------------------------------------------------------------------
_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent
_SRC_DIR = _REPO_ROOT / "src"
for _p in (str(_REPO_ROOT), str(_SRC_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from apex.config import get_settings
from apex.db.connection import close_db, init_db

# Reuse pure helpers from the learning report — no side effects, no main().
from scripts.report_signal_learning import (
    _avg_r,
    _macro_alignment_label,
    _outcome_stats,
    _pct,
)

# Import predicate functions from the simulator — no duplication needed.
from scripts.simulate_strategy_filters import (
    _filter_atr_0_10_to_1_00,
    _filter_atr_0_25_to_0_50,
    _filter_atr_gte_0_10,
    _filter_btc_aligned,
    _filter_btc_opposed,
    _filter_eth_aligned,
    _filter_eth_opposed,
    _filter_ema_spread_0_25_to_0_50,
    _filter_ema_spread_neg_0_25_to_0,
    _filter_long_only,
    _filter_rsi_50_to_60,
    _filter_short_only,
    _filter_vwap_above_0_to_0_25,
)

_DEFAULT_SPLIT = 0.70
_DEFAULT_MIN_N = 10

# Combined predicate (no group labels — not retrospective)
def _filter_btc_opposed_and_atr_gte_0_10(r: Any) -> bool:
    return _filter_btc_opposed(r) and _filter_atr_gte_0_10(r)


# Ordered list of (name, plain-English rule, predicate) for validation.
# Only non-retrospective filters are included — label-based filters would
# leak train-set label assignments into validation, making them meaningless.
_CANDIDATE_FILTERS = [
    ("KEEP_ATR_GTE_0_10",
     "atr_pct >= 0.10",
     _filter_atr_gte_0_10),
    ("KEEP_ATR_0_10_TO_1_00",
     "0.10 <= atr_pct <= 1.00",
     _filter_atr_0_10_to_1_00),
    ("KEEP_ATR_0_25_TO_0_50",
     "0.25 <= atr_pct <= 0.50",
     _filter_atr_0_25_to_0_50),
    ("KEEP_EMA_SPREAD_NEG_0_25_TO_0",
     "-0.25 <= ema_spread_pct < 0",
     _filter_ema_spread_neg_0_25_to_0),
    ("KEEP_EMA_SPREAD_0_25_TO_0_50",
     "0.25 <= ema_spread_pct <= 0.50",
     _filter_ema_spread_0_25_to_0_50),
    ("KEEP_RSI_50_TO_60",
     "50 <= rsi_val <= 60",
     _filter_rsi_50_to_60),
    ("KEEP_PRICE_ABOVE_VWAP_0_TO_0_25",
     "0 <= price_vs_vwap_pct <= 0.25",
     _filter_vwap_above_0_to_0_25),
    ("KEEP_LONG_ONLY",
     "direction == LONG",
     _filter_long_only),
    ("KEEP_SHORT_ONLY",
     "direction == SHORT",
     _filter_short_only),
    ("KEEP_BTC_ALIGNED",
     "btc_trend_bias aligned with signal direction",
     _filter_btc_aligned),
    ("KEEP_BTC_OPPOSED",
     "btc_trend_bias opposed to signal direction",
     _filter_btc_opposed),
    ("KEEP_ETH_ALIGNED",
     "eth_trend_bias aligned with signal direction",
     _filter_eth_aligned),
    ("KEEP_ETH_OPPOSED",
     "eth_trend_bias opposed to signal direction",
     _filter_eth_opposed),
    ("KEEP_BTC_OPPOSED_AND_ATR_GTE_0_10",
     "btc_trend_bias opposed AND atr_pct >= 0.10",
     _filter_btc_opposed_and_atr_gte_0_10),
]


# ======================================================================
# Core helpers
# ======================================================================

def _avg_r_num(vals: list[float]) -> Optional[float]:
    """Return numeric avg R or None."""
    return sum(vals) / len(vals) if vals else None


def _filter_stats(rows: list[Any], pred) -> tuple[list[Any], dict]:
    """Apply predicate, return (kept_rows, outcome_stats)."""
    kept = [r for r in rows if pred(r)]
    return kept, _outcome_stats(kept)


def _rate(count: int, n: int) -> float:
    return count / n if n else 0.0


def _delta(kept_rate: float, base_rate: float) -> float:
    return kept_rate - base_rate


def _stability_label(
    train_n: int,
    val_n: int,
    min_n: int,
    train_d_tp1: float,
    train_d_r: Optional[float],
    val_d_tp1: float,
    val_d_r: Optional[float],
    val_d_stop: float,
    val_exp_rate: float,
) -> str:
    """Assign a stability label for a validated filter.

    Checked in order:
      INSUFFICIENT_VALIDATION_SAMPLE — val_n < min_n
      HIGH_EXPIRY_RISK               — val expiry >= 80% even if metrics improve
      FAILS_VALIDATION               — val clearly worsens on TP1 and avgR
      TRAIN_ONLY_OVERFIT             — train improves, validation worsens
      VALIDATION_ONLY_REGIME_SHIFT   — val improves, train was weak/neutral
      VALIDATED_CANDIDATE            — both train and val directionally positive,
                                       val expiry < 80%, val stop not materially worse
      PROMISING_NEEDS_MORE_DATA      — both directionally positive but weak/mixed
    """
    if val_n < min_n:
        return "INSUFFICIENT_VALIDATION_SAMPLE"

    if val_exp_rate >= 0.80:
        return "HIGH_EXPIRY_RISK"

    train_improves = (train_d_tp1 > 0.01) or (train_d_r is not None and train_d_r > 0.02)
    val_improves = (val_d_tp1 > 0.01) or (val_d_r is not None and val_d_r > 0.02)
    val_worsens = (val_d_tp1 < -0.02 and (val_d_r is None or val_d_r < -0.03))
    val_stop_bad = val_d_stop > 0.03

    if val_worsens:
        return "FAILS_VALIDATION"

    if train_improves and not val_improves:
        return "TRAIN_ONLY_OVERFIT"

    if val_improves and not train_improves:
        return "VALIDATION_ONLY_REGIME_SHIFT"

    if train_improves and val_improves and not val_stop_bad:
        return "VALIDATED_CANDIDATE"

    return "PROMISING_NEEDS_MORE_DATA"


def _stability_score(
    val_d_tp1: float,
    val_d_r: Optional[float],
    val_d_stop: float,
    val_d_exp_no_tp1: float,
    val_exp_rate: float,
    val_n: int,
    min_n: int,
    train_improves: bool,
) -> float:
    """Numeric score for stability ranking. Higher is better."""
    d_r = val_d_r or 0.0
    score = val_d_tp1 * 0.40 + (-val_d_stop) * 0.30 + d_r * 0.20 + (-val_d_exp_no_tp1) * 0.10
    if val_exp_rate >= 0.80:
        score -= 0.25
    if val_n < min_n * 2:
        score -= 0.05
    if not train_improves:
        score -= 0.10
    return score


def _print_baseline_block(label: str, rows: list[Any]) -> None:
    st = _outcome_stats(rows)
    n = st["n"]
    long_n = sum(1 for r in rows if r["direction"] == "LONG")
    short_n = n - long_n
    exp_n = st["expired"]
    print(f"  {label}:")
    print(f"    Rows        : {n}")
    print(f"    TP1         : {st['tp1']:4d}  {_pct(st['tp1'], n)}")
    print(f"    HIT_2R      : {st['tp2']:4d}  {_pct(st['tp2'], n)}")
    print(f"    STOPPED     : {st['stopped']:4d}  {_pct(st['stopped'], n)}")
    print(f"    EXPIRED     : {st['expired']:4d}  {_pct(st['expired'], n)}")
    print(f"      after TP1 : {st['exp_after_tp1']:4d}  {_pct(st['exp_after_tp1'], exp_n or 1)} of expired")
    print(f"      w/o  TP1  : {st['exp_no_tp1']:4d}  {_pct(st['exp_no_tp1'], exp_n or 1)} of expired")
    avg_mfe_s = f"{_avg_r_num(st['mfe_vals']):+.2f}" if st["mfe_vals"] else "n/a"
    avg_mae_s = f"{_avg_r_num(st['mae_vals']):+.2f}" if st["mae_vals"] else "n/a"
    avg_r_s = f"{_avg_r_num(st['out_r_vals']):+.3f}" if st["out_r_vals"] else "n/a"
    print(f"    Avg MFE     : {avg_mfe_s} R")
    print(f"    Avg MAE     : {avg_mae_s} R")
    print(f"    Avg R       : {avg_r_s} R")
    print(f"    LONG/SHORT  : {long_n} / {short_n}")


# ======================================================================
# Main
# ======================================================================

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="APEX Out-of-Sample Filter Validation [Milestone 11B] — report-only"
    )
    parser.add_argument(
        "--since",
        metavar="ISO_TIMESTAMP",
        default=None,
        help="Filter features captured at >= TIMESTAMP",
    )
    parser.add_argument(
        "--split",
        metavar="FLOAT",
        type=float,
        default=_DEFAULT_SPLIT,
        help=f"Fraction of rows to use as train set (default: {_DEFAULT_SPLIT}; remainder = validation)",
    )
    parser.add_argument(
        "--min-n",
        metavar="N",
        type=int,
        default=_DEFAULT_MIN_N,
        help=f"Minimum validation rows required for non-INSUFFICIENT label (default: {_DEFAULT_MIN_N})",
    )
    parser.add_argument(
        "--csv",
        metavar="PATH",
        default=None,
        help="Write validation results to a CSV file at PATH",
    )
    args = parser.parse_args(argv)

    if not (0.0 < args.split < 1.0):
        print("ERROR: --split must be between 0 and 1 (exclusive).", file=sys.stderr)
        sys.exit(1)

    settings = get_settings()
    conn = init_db(settings.apex_db_path)

    # ------------------------------------------------------------------
    # Load data — chronological order (ASC) for correct train/val split
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
    # Section 1: Header + disclaimer
    # ------------------------------------------------------------------
    print()
    print("=" * 68)
    print("  APEX Out-of-Sample Filter Validation  [Milestone 11B]")
    print("=" * 68)
    print("  REPORT-ONLY VALIDATION.")
    print("  Does not change runtime scanning, alerts, or trading.")
    print("  Does not write to the database.")
    print("  Candidate filters remain dry-run research only.")
    print("  Validation reduces but does not eliminate overfitting risk.")
    print()
    if args.since:
        print(f"  Filtered since    : {args.since}")

    n_closed = len(closed)
    split_idx = int(n_closed * args.split)
    train_rows = closed[:split_idx]
    val_rows = closed[split_idx:]

    print(f"  Rows loaded       : {len(rows)}")
    print(f"  Closed confirmed  : {n_closed}  (open/pending excluded)")
    print(f"  Split ratio       : {args.split:.0%} train / {1 - args.split:.0%} validation")
    print(f"  Train rows        : {len(train_rows)}")
    print(f"  Validation rows   : {len(val_rows)}")
    print(f"  Min-N threshold   : {args.min_n}")

    if n_closed == 0:
        print()
        print("  No closed CONFIRMED_SETUP rows. Exiting.")
        print()
        print("=" * 68)
        return

    if len(val_rows) == 0:
        print()
        print("  Validation set is empty (too few rows for this split ratio).")
        print("  Try --split 0.60 or collect more data.")
        print()
        print("=" * 68)
        return

    # ------------------------------------------------------------------
    # Section 2: Baseline comparison
    # ------------------------------------------------------------------
    print()
    print("=" * 68)
    print("  Baseline performance")
    print("=" * 68)
    print()
    _print_baseline_block("Full dataset", closed)
    print()
    _print_baseline_block(f"Train  (first {args.split:.0%})", train_rows)
    print()
    _print_baseline_block(f"Validation  (last {1 - args.split:.0%})", val_rows)

    # Pre-compute baseline rates for both splits
    train_st = _outcome_stats(train_rows)
    val_st = _outcome_stats(val_rows)

    t_n = train_st["n"]
    v_n = val_st["n"]

    t_tp1_r = _rate(train_st["tp1"], t_n)
    t_stop_r = _rate(train_st["stopped"], t_n)
    t_exp_r = _rate(train_st["expired"], t_n)
    t_exp_no_tp1_r = _rate(train_st["exp_no_tp1"], t_n)
    t_avg_r = _avg_r_num(train_st["out_r_vals"])

    v_tp1_r = _rate(val_st["tp1"], v_n)
    v_stop_r = _rate(val_st["stopped"], v_n)
    v_exp_r = _rate(val_st["expired"], v_n)
    v_exp_no_tp1_r = _rate(val_st["exp_no_tp1"], v_n)
    v_avg_r = _avg_r_num(val_st["out_r_vals"])

    # ------------------------------------------------------------------
    # Section 3 + 4: Run filters and build per-filter table
    # ------------------------------------------------------------------
    print()
    print("=" * 68)
    print("  Per-filter train vs validation results")
    print("=" * 68)
    print("  Train = first rows chronologically; Validation = later held-out rows.")
    print("  Δ values are relative to the corresponding split baseline.")
    print()

    def _delta_s(v: Optional[float]) -> str:
        if v is None:
            return "  n/a"
        return f"{v * 100:+.1f}pp"

    def _delta_r_s(v: Optional[float]) -> str:
        if v is None:
            return "   n/a"
        return f"{v:+.3f}R"

    filter_results: list[dict] = []

    hdr = (
        f"  {'Filter':<44} "
        f"{'TrN':>4} {'VaN':>4} "
        f"{'TrTP1Δ':>7} {'VaTP1Δ':>7} "
        f"{'TrStpΔ':>7} {'VaStpΔ':>7} "
        f"{'TrExpΔ':>7} {'VaExpΔ':>7} "
        f"{'TrRΔ':>7} {'VaRΔ':>7}  "
        f"Stability"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    for name, rule_desc, pred in _CANDIDATE_FILTERS:
        # Train stats
        train_kept, tr_st = _filter_stats(train_rows, pred)
        tr_n = tr_st["n"]
        tr_tp1_r = _rate(tr_st["tp1"], tr_n)
        tr_stop_r = _rate(tr_st["stopped"], tr_n)
        tr_exp_r = _rate(tr_st["expired"], tr_n)
        tr_exp_no_tp1_r = _rate(tr_st["exp_no_tp1"], tr_n)
        tr_avg_r = _avg_r_num(tr_st["out_r_vals"])
        tr_d_tp1 = _delta(tr_tp1_r, t_tp1_r)
        tr_d_stop = _delta(tr_stop_r, t_stop_r)
        tr_d_exp = _delta(tr_exp_r, t_exp_r)
        tr_d_exp_no_tp1 = _delta(tr_exp_no_tp1_r, t_exp_no_tp1_r)
        tr_d_r = (tr_avg_r - t_avg_r) if (tr_avg_r is not None and t_avg_r is not None) else None
        tr_kept_pct = tr_n / t_n * 100 if t_n else 0.0

        # Validation stats
        val_kept, va_st = _filter_stats(val_rows, pred)
        va_n = va_st["n"]
        va_tp1_r = _rate(va_st["tp1"], va_n)
        va_stop_r = _rate(va_st["stopped"], va_n)
        va_exp_r = _rate(va_st["expired"], va_n)
        va_exp_no_tp1_r = _rate(va_st["exp_no_tp1"], va_n)
        va_avg_r = _avg_r_num(va_st["out_r_vals"])
        va_d_tp1 = _delta(va_tp1_r, v_tp1_r)
        va_d_stop = _delta(va_stop_r, v_stop_r)
        va_d_exp = _delta(va_exp_r, v_exp_r)
        va_d_exp_no_tp1 = _delta(va_exp_no_tp1_r, v_exp_no_tp1_r)
        va_d_r = (va_avg_r - v_avg_r) if (va_avg_r is not None and v_avg_r is not None) else None
        va_kept_pct = va_n / v_n * 100 if v_n else 0.0

        train_improves = (tr_d_tp1 > 0.01) or (tr_d_r is not None and tr_d_r > 0.02)

        stability = _stability_label(
            tr_n, va_n, args.min_n,
            tr_d_tp1, tr_d_r,
            va_d_tp1, va_d_r,
            va_d_stop, va_exp_r,
        )

        stab_score = _stability_score(
            va_d_tp1, va_d_r, va_d_stop, va_d_exp_no_tp1,
            va_exp_r, va_n, args.min_n, train_improves,
        )

        print(
            f"  {name[:43]:<44} "
            f"{tr_n:>4} {va_n:>4} "
            f"{_delta_s(tr_d_tp1):>7} {_delta_s(va_d_tp1):>7} "
            f"{_delta_s(tr_d_stop):>7} {_delta_s(va_d_stop):>7} "
            f"{_delta_s(tr_d_exp):>7} {_delta_s(va_d_exp):>7} "
            f"{_delta_r_s(tr_d_r):>7} {_delta_r_s(va_d_r):>7}  "
            f"{stability}"
        )

        filter_results.append({
            "name": name,
            "rule": rule_desc,
            "train_kept": tr_n,
            "val_kept": va_n,
            "train_kept_pct": tr_kept_pct,
            "val_kept_pct": va_kept_pct,
            "train_tp1_pct": tr_tp1_r * 100,
            "val_tp1_pct": va_tp1_r * 100,
            "train_stop_pct": tr_stop_r * 100,
            "val_stop_pct": va_stop_r * 100,
            "train_exp_pct": tr_exp_r * 100,
            "val_exp_pct": va_exp_r * 100,
            "train_avg_r": tr_avg_r,
            "val_avg_r": va_avg_r,
            "train_d_tp1": tr_d_tp1,
            "val_d_tp1": va_d_tp1,
            "train_d_stop": tr_d_stop,
            "val_d_stop": va_d_stop,
            "train_d_exp": tr_d_exp,
            "val_d_exp": va_d_exp,
            "train_d_r": tr_d_r,
            "val_d_r": va_d_r,
            "train_improves": train_improves,
            "stability": stability,
            "stability_score": stab_score,
        })

    # ------------------------------------------------------------------
    # Section 5: Stability ranking
    # ------------------------------------------------------------------
    print()
    print("=" * 68)
    print("  Out-of-sample stability ranking")
    print("=" * 68)
    print("  Ranked by validation-set improvement (TP1Δ 40%, stopΔ 30%, avgRΔ 20%,")
    print("  expNoTP1Δ 10%). Penalties for high validation expiry, thin sample,")
    print("  and train-only improvement.")
    print("  INSUFFICIENT_VALIDATION_SAMPLE filters excluded.")

    rankable = [r for r in filter_results if r["stability"] != "INSUFFICIENT_VALIDATION_SAMPLE"]
    ranked = sorted(rankable, key=lambda r: r["stability_score"], reverse=True)[:10]

    if ranked:
        print()
        hdr2 = (
            f"  {'Filter':<44} {'VaN':>4} {'VaTP1Δ':>7} {'VaStpΔ':>7} "
            f"{'VaExpΔ':>7} {'VaRΔ':>7} {'Score':>7}  Stability"
        )
        print(hdr2)
        print("  " + "-" * (len(hdr2) - 2))
        for r in ranked:
            print(
                f"  {r['name'][:43]:<44} {r['val_kept']:>4} "
                f"{_delta_s(r['val_d_tp1']):>7} {_delta_s(r['val_d_stop']):>7} "
                f"{_delta_s(r['val_d_exp']):>7} {_delta_r_s(r['val_d_r']):>7} "
                f"{r['stability_score']:>+7.3f}  {r['stability']}"
            )
    else:
        print()
        print("  No filters with sufficient validation sample to rank.")

    # ------------------------------------------------------------------
    # Section 6: Overfit / unstable warning
    # ------------------------------------------------------------------
    print()
    print("=" * 68)
    print("  Likely overfit / unstable filters")
    print("=" * 68)

    overfit = [
        r for r in filter_results
        if r["stability"] in (
            "TRAIN_ONLY_OVERFIT",
            "FAILS_VALIDATION",
            "HIGH_EXPIRY_RISK",
            "INSUFFICIENT_VALIDATION_SAMPLE",
        )
    ]

    if overfit:
        print()
        for r in overfit:
            val_d_r_s = _delta_r_s(r["val_d_r"])
            print(
                f"  {r['name']:<44}  "
                f"TrTP1Δ={_delta_s(r['train_d_tp1'])}  "
                f"VaTP1Δ={_delta_s(r['val_d_tp1'])}  "
                f"VaRΔ={val_d_r_s}  "
                f"({r['stability']})"
            )
    else:
        print()
        print("  None flagged.")

    # ------------------------------------------------------------------
    # Section 7: Interpretation
    # ------------------------------------------------------------------
    print()
    print("=" * 68)
    print("  Interpretation")
    print("=" * 68)
    print()

    validated = [r for r in filter_results if r["stability"] == "VALIDATED_CANDIDATE"]
    promising = [r for r in filter_results if r["stability"] == "PROMISING_NEEDS_MORE_DATA"]
    regime_shift = [r for r in filter_results if r["stability"] == "VALIDATION_ONLY_REGIME_SHIFT"]
    train_overfit = [r for r in filter_results if r["stability"] == "TRAIN_ONLY_OVERFIT"]
    high_exp = [r for r in filter_results if r["stability"] == "HIGH_EXPIRY_RISK"]
    fails = [r for r in filter_results if r["stability"] == "FAILS_VALIDATION"]
    insuf = [r for r in filter_results if r["stability"] == "INSUFFICIENT_VALIDATION_SAMPLE"]

    print(
        f"  {len(validated)} VALIDATED_CANDIDATE, "
        f"{len(promising)} PROMISING_NEEDS_MORE_DATA, "
        f"{len(regime_shift)} VALIDATION_ONLY_REGIME_SHIFT,\n"
        f"  {len(train_overfit)} TRAIN_ONLY_OVERFIT, "
        f"{len(high_exp)} HIGH_EXPIRY_RISK, "
        f"{len(fails)} FAILS_VALIDATION, "
        f"{len(insuf)} INSUFFICIENT_VALIDATION_SAMPLE."
    )
    print()
    print("  No runtime filters are recommended automatically.")
    print("  Validation is still based on dry-run observations only.")
    print("  A filter needs stable validation over time before becoming a")
    print("  runtime gate. Current validation set covers only the most recent")
    f"  {1 - args.split:.0%} of captured observations.\n"
    print(f"  ({1 - args.split:.0%} of total closed confirmed rows.)")
    print()

    if high_exp:
        names = ", ".join(r["name"] for r in high_exp[:3])
        extra = f" (+{len(high_exp) - 3} more)" if len(high_exp) > 3 else ""
        print(f"  HIGH_EXPIRY_RISK filters: {names}{extra}.")
        print("  High expiry in validation means the issue may be exit timing or")
        print("  expiration window logic, not only entry filtering.")
        print()

    if train_overfit:
        print(f"  {len(train_overfit)} filter(s) labeled TRAIN_ONLY_OVERFIT.")
        print("  These improved the train set but failed on held-out validation data.")
        print("  Treat them as likely coincidences in the train period, not real edges.")
        print()

    if validated:
        print(f"  {len(validated)} filter(s) labeled VALIDATED_CANDIDATE:")
        for r in validated:
            va_r_s = _delta_r_s(r["val_d_r"])
            print(
                f"    {r['name']}: VaN={r['val_kept']} "
                f"VaTP1Δ={_delta_s(r['val_d_tp1'])} "
                f"VaRΔ={va_r_s}"
            )
        print()
        print("  VALIDATED_CANDIDATE means directional improvement on both train")
        print("  and validation, with expiry < 80% and stop not materially worse.")
        print("  This is encouraging but not sufficient for a runtime gate.")
        print("  A dedicated 11C milestone with explicit approval is required.")
        print()

    if len(validated) == 0 and len(promising) == 0:
        print("  No filters validated cleanly. Continue collecting observations or")
        print("  investigate exit/expiry logic before proposing runtime gates.")
        print()

    print("  RETROSPECTIVE NOTE: All observations are from dry-run mode.")
    print("  Validation splits are temporal but the overall sample may reflect")
    print("  a single market regime. Monitor for regime changes over time.")

    # ------------------------------------------------------------------
    # Optional CSV export
    # ------------------------------------------------------------------
    if args.csv:
        csv_path = Path(args.csv)
        fieldnames = [
            "name", "rule",
            "train_kept", "val_kept", "train_kept_pct", "val_kept_pct",
            "train_tp1_pct", "val_tp1_pct",
            "train_stop_pct", "val_stop_pct",
            "train_exp_pct", "val_exp_pct",
            "train_avg_r", "val_avg_r",
            "train_d_tp1", "val_d_tp1",
            "train_d_stop", "val_d_stop",
            "train_d_exp", "val_d_exp",
            "train_d_r", "val_d_r",
            "stability", "stability_score",
        ]
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for res in filter_results:
                writer.writerow({k: res.get(k) for k in fieldnames})
        print()
        print(f"  CSV written to: {csv_path}")

    print()
    print("=" * 68)


if __name__ == "__main__":
    main()
