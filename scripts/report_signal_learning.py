"""Read-only learning report for signal_features data.

Summarises captured indicator/context snapshots and their outcomes.
Useful for identifying which indicator combinations correlate with better outcomes.

Usage (run from repo root):
    PYTHONPATH=src python scripts/report_signal_learning.py
    PYTHONPATH=src python scripts/report_signal_learning.py --since 2026-05-01T00:00:00Z
    PYTHONPATH=src python scripts/report_signal_learning.py --limit 500 --csv /tmp/features.csv

Does not start the bot, does not write any data.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from apex.config import get_settings
from apex.db.connection import close_db, init_db


def _pct(n: int, total: int, width: int = 6) -> str:
    if total == 0:
        return " " * (width - 3) + "n/a"
    return f"{n / total * 100:{width - 1}.1f}%"


def _avg(values: list[float]) -> str:
    if not values:
        return "  n/a"
    return f"{sum(values) / len(values):+.2f}"


def _stats(values: list[float]) -> str:
    if not values:
        return "  n/a"
    mean = sum(values) / len(values)
    lo = min(values)
    hi = max(values)
    return f"mean={mean:+.2f}  min={lo:+.2f}  max={hi:+.2f}  n={len(values)}"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="APEX signal learning report")
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
        default=1000,
        help="Max rows to load (default: 1000)",
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

    try:
        rows = conn.execute(
            "SELECT * FROM signal_features ORDER BY captured_at DESC LIMIT ?",
            (args.limit,),
        ).fetchall()
    except Exception as e:
        print(f"ERROR: could not query signal_features: {e}", file=sys.stderr)
        print("Has the DB been initialized with the latest schema?", file=sys.stderr)
        close_db()
        sys.exit(1)

    if args.since:
        rows = [r for r in rows if r["captured_at"] >= args.since]

    print()
    print("=" * 68)
    print("  APEX Signal Learning Report")
    print("=" * 68)
    if args.since:
        print(f"  Filtered since      : {args.since}")
    print(f"  Rows loaded (limit={args.limit})")

    total = len(rows)
    if total == 0:
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

    print(f"  Total features      : {total}")
    print(f"    CONFIRMED_SETUP   : {len(confirmed)}")
    print(f"    SETUP_FORMING     : {len(forming)}")
    if confirmed:
        print(f"    With outcomes     : {len(with_outcome)}  {_pct(len(with_outcome), len(confirmed))} of confirmed")
        print(f"    Without outcomes  : {len(without_outcome)}  (open/pending)")

    # --- Outcome summary (CONFIRMED closed rows only) ---
    if with_outcome:
        tp1 = [r for r in with_outcome if r["hit_1r_at"] is not None]
        tp2 = [r for r in with_outcome if r["outcome_status"] == "HIT_2R"]
        stopped = [r for r in with_outcome if r["outcome_status"] == "STOPPED"]
        expired = [r for r in with_outcome if r["outcome_status"] == "EXPIRED"]
        n = len(with_outcome)

        print()
        print("  Outcome summary (CONFIRMED, closed):")
        print(f"    TP1 milestone hit : {len(tp1):4d}  {_pct(len(tp1), n)}")
        print(f"    HIT_2R            : {len(tp2):4d}  {_pct(len(tp2), n)}")
        print(f"    STOPPED           : {len(stopped):4d}  {_pct(len(stopped), n)}")
        print(f"    EXPIRED           : {len(expired):4d}  {_pct(len(expired), n)}")

    # --- Indicator distribution stats (CONFIRMED) ---
    _INDICATOR_FIELDS = [
        ("rsi_val",           "RSI"),
        ("atr_pct",           "ATR %price"),
        ("price_vs_vwap_pct", "Price vs VWAP %"),
        ("ema_spread_pct",    "EMA spread %"),
    ]

    if confirmed:
        print()
        print("  Indicator snapshot distribution (CONFIRMED, all):")
        hdr = f"  {'Field':<22} {'N':>4}   Stats"
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for field_name, label in _INDICATOR_FIELDS:
            vals = [r[field_name] for r in confirmed if r[field_name] is not None]
            print(f"  {label:<22} {len(vals):>4}   {_stats(vals)}")

    # --- TP1-hit vs TP1-miss indicator comparison ---
    if with_outcome:
        tp1_hit_rows = [r for r in with_outcome if r["hit_1r_at"] is not None]
        tp1_miss_rows = [r for r in with_outcome if r["hit_1r_at"] is None]
        if tp1_hit_rows and tp1_miss_rows:
            print()
            print("  Indicator split: TP1-hit vs TP1-miss (CONFIRMED, closed):")
            hdr2 = f"  {'Field':<22} {'TP1-hit mean':>14} {'TP1-miss mean':>14}"
            print(hdr2)
            print("  " + "-" * (len(hdr2) - 2))
            for field_name, label in _INDICATOR_FIELDS:
                hit_vals = [r[field_name] for r in tp1_hit_rows if r[field_name] is not None]
                miss_vals = [r[field_name] for r in tp1_miss_rows if r[field_name] is not None]
                hit_mean = f"{sum(hit_vals)/len(hit_vals):+.2f}" if hit_vals else "   n/a"
                miss_mean = f"{sum(miss_vals)/len(miss_vals):+.2f}" if miss_vals else "   n/a"
                print(f"  {label:<22} {hit_mean:>14} {miss_mean:>14}")

    # --- BTC/ETH macro context breakdown ---
    btc_counts: dict[str, int] = defaultdict(int)
    eth_counts: dict[str, int] = defaultdict(int)
    for r in confirmed:
        if r["btc_trend_bias"]:
            btc_counts[r["btc_trend_bias"]] += 1
        if r["eth_trend_bias"]:
            eth_counts[r["eth_trend_bias"]] += 1

    if btc_counts or eth_counts:
        print()
        print("  Macro context at capture time (CONFIRMED):")
        if btc_counts:
            btc_parts = ", ".join(f"{k}={v}" for k, v in sorted(btc_counts.items()))
            print(f"    BTC trend bias    : {btc_parts}")
        if eth_counts:
            eth_parts = ", ".join(f"{k}={v}" for k, v in sorted(eth_counts.items()))
            print(f"    ETH trend bias    : {eth_parts}")

    # --- Feature version breakdown ---
    ver_counts: dict[str, int] = defaultdict(int)
    for r in rows:
        ver_counts[r["feature_version"] or "unknown"] += 1
    if ver_counts:
        print()
        print("  Feature versions captured:")
        for ver, cnt in sorted(ver_counts.items()):
            print(f"    {ver:<20} : {cnt}")

    # --- Recent 10 features ---
    print()
    print(f"  Recent {min(10, total)} features:")
    print(
        f"  {'UID':>10}  {'Symbol':<10} {'Type':<18} {'Dir':<5} "
        f"{'RSI':>5} {'ATR%':>5} {'VWAP%':>6} {'BTC':>5}  {'Outcome':<9} {'R':>5}"
    )
    print("  " + "-" * 90)
    for r in rows[:10]:
        uid_short = r["observation_uid"][:8]
        rsi_s = f"{r['rsi_val']:.1f}" if r["rsi_val"] is not None else "  -"
        atr_s = f"{r['atr_pct']:.2f}" if r["atr_pct"] is not None else "  -"
        vwap_s = f"{r['price_vs_vwap_pct']:+.1f}" if r["price_vs_vwap_pct"] is not None else "  -"
        btc_s = (r["btc_trend_bias"] or "-")[:5]
        outcome_s = r["outcome_status"] or "OPEN"
        r_s = f"{r['outcome_r']:+.1f}" if r["outcome_r"] is not None else "  -"
        print(
            f"  {uid_short:>10}  {r['symbol']:<10} {r['signal_type']:<18} "
            f"{r['direction']:<5} {rsi_s:>5} {atr_s:>5} {vwap_s:>6} {btc_s:>5}  "
            f"{outcome_s:<9} {r_s:>5}"
        )

    print()
    print("=" * 68)

    # --- Optional CSV export ---
    if args.csv:
        try:
            csv_path = Path(args.csv)
            if rows:
                fieldnames = list(rows[0].keys())
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
