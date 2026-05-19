"""Read-only debug report for dry-run signal quality analysis.

Prints a conversion funnel, per-symbol quality table, and plain-English
interpretation. Never modifies any data or starts any services.

Usage (run from repo root):
    PYTHONPATH=src python scripts/debug_signal_conditions.py

Requires APEX_DB_PATH to be set in .env (or defaults to ./data/apex.db).
"""
from __future__ import annotations

import sys
from collections import defaultdict
from typing import Any

from apex.config import get_settings
from apex.db.connection import close_db, init_db


def _pct(n: int, total: int, width: int = 6) -> str:
    if total == 0:
        return " " * (width - 3) + "n/a"
    return f"{n / total * 100:{width - 1}.1f}%"


def _effective_final_status(row: Any) -> str:
    return row["final_status"] or row["status"]


def main() -> None:
    settings = get_settings()
    conn = init_db(settings.apex_db_path)

    # --- Safety summary ---
    print()
    print("=" * 68)
    print("  APEX Debug: Signal Conditions")
    print("=" * 68)
    print()
    print("  Safety:")
    print(f"    alerts_enabled   : {settings.alerts_enabled}")
    print(f"    dry_run_mode     : {settings.dry_run_mode}")
    print(f"    scan_mode        : {settings.scan_mode}")

    try:
        markets = conn.execute(
            "SELECT COUNT(*) as cnt FROM markets WHERE scan_enabled=1 AND is_active=1"
        ).fetchone()
        scan_count = markets["cnt"] if markets else 0
        print(f"    scan symbols     : {scan_count}")
    except Exception as e:
        print(f"    scan symbols     : ERROR ({e})")
        scan_count = 0

    # --- Recent app_events (scan activity) ---
    try:
        recent_events = conn.execute(
            """
            SELECT event_type, message, created_at FROM app_events
            WHERE event_type IN ('STARTUP','SHUTDOWN','ALERT_SUPPRESSED','ALERT_GENERATED')
            ORDER BY created_at DESC LIMIT 5
            """
        ).fetchall()
        if recent_events:
            print()
            print("  Recent app events (last 5):")
            for ev in recent_events:
                ts = ev["created_at"][:19]
                print(f"    {ts}  {ev['event_type']:<25}  {ev['message'][:50]}")
    except Exception:
        pass  # app_events table may not exist in test environments

    # --- Fetch all observations ---
    try:
        rows = conn.execute(
            "SELECT * FROM signal_observations ORDER BY observed_at DESC"
        ).fetchall()
    except Exception as e:
        print(f"\n  ERROR: could not query signal_observations: {e}", file=sys.stderr)
        close_db()
        sys.exit(1)

    if not rows:
        print("\n  No signal observations recorded yet.")
        close_db()
        return

    confirmed = [r for r in rows if r["signal_type"] == "CONFIRMED_SETUP"]
    forming = [r for r in rows if r["signal_type"] == "SETUP_FORMING"]

    # Terminal confirmed counts
    tp1_hit = [r for r in confirmed if r["hit_1r_at"] is not None]
    tp2_hit = [r for r in confirmed if _effective_final_status(r) == "HIT_2R"]
    stopped_all = [r for r in confirmed if _effective_final_status(r) == "STOPPED"]
    expired_all = [r for r in confirmed if _effective_final_status(r) == "EXPIRED"]
    legacy_t1 = [r for r in confirmed if r["status"] == "HIT_1R" and r["final_status"] is None]
    open_confirmed = [r for r in confirmed if r["status"] == "OBSERVED"]

    n_forming = len(forming)
    n_confirmed = len(confirmed)

    # --- Conversion funnel ---
    print()
    print("  Observation conversion funnel:")
    print(f"    SETUP_FORMING recorded      : {n_forming:>5}")
    print(f"    CONFIRMED_SETUP recorded    : {n_confirmed:>5}  {_pct(n_confirmed, n_forming + n_confirmed)} of all")
    print(f"    TP1 milestone reached       : {len(tp1_hit):>5}  {_pct(len(tp1_hit), n_confirmed)} of confirmed")
    print(f"    TP2 reached (HIT_2R)        : {len(tp2_hit):>5}  {_pct(len(tp2_hit), n_confirmed)} of confirmed")
    print(f"    STOPPED                     : {len(stopped_all):>5}  {_pct(len(stopped_all), n_confirmed)} of confirmed")
    print(f"    EXPIRED                     : {len(expired_all):>5}  {_pct(len(expired_all), n_confirmed)} of confirmed")
    if legacy_t1:
        print(f"    HIT_1R (legacy terminal)    : {len(legacy_t1):>5}  {_pct(len(legacy_t1), n_confirmed)} of confirmed")
    print(f"    Still open (OBSERVED)       : {len(open_confirmed):>5}  {_pct(len(open_confirmed), n_confirmed)} of confirmed")

    # Stopped/expired with vs without TP1
    stopped_post_tp1 = [r for r in stopped_all if r["hit_1r_before_stop"] == 1]
    expired_post_tp1 = [r for r in expired_all if r["hit_1r_before_expiry"] == 1]

    if stopped_all or expired_all:
        print()
        print("  Stop/expiry detail (where flag available):")
        if stopped_all:
            print(f"    Stopped after TP1  : {len(stopped_post_tp1):>4}  {_pct(len(stopped_post_tp1), len(stopped_all))} of stopped")
        if expired_all:
            print(f"    Expired after TP1  : {len(expired_post_tp1):>4}  {_pct(len(expired_post_tp1), len(expired_all))} of expired")

    # --- Per-symbol quality table ---
    by_sym: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "total": 0, "tp1": 0, "tp2": 0, "stopped": 0, "expired": 0,
        "mfe": [], "mae": [],
    })

    for r in confirmed:
        s = r["symbol"]
        by_sym[s]["total"] += 1
        fs = _effective_final_status(r)
        if r["hit_1r_at"] is not None:
            by_sym[s]["tp1"] += 1
        if fs == "HIT_2R":
            by_sym[s]["tp2"] += 1
        elif fs == "STOPPED":
            by_sym[s]["stopped"] += 1
        elif fs == "EXPIRED":
            by_sym[s]["expired"] += 1
        if r["max_favorable_excursion"] is not None:
            by_sym[s]["mfe"].append(r["max_favorable_excursion"])
        if r["max_adverse_excursion"] is not None:
            by_sym[s]["mae"].append(r["max_adverse_excursion"])

    if by_sym:
        print()
        print("  Per-symbol quality (CONFIRMED, sorted by TP1 rate):")
        sorted_syms = sorted(
            by_sym.items(),
            key=lambda x: x[1]["tp1"] / x[1]["total"] if x[1]["total"] > 0 else 0,
            reverse=True,
        )
        hdr = f"  {'Symbol':<12} {'N':>4} {'TP1%':>6} {'TP2%':>6} {'STOP%':>7} {'EXP%':>6} {'avgMFE':>7} {'avgMAE':>7}"
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for sym, c in sorted_syms:
            tot = c["total"]
            tp1_pct = _pct(c["tp1"], tot)
            tp2_pct = _pct(c["tp2"], tot)
            stp_pct = _pct(c["stopped"], tot)
            exp_pct = _pct(c["expired"], tot)
            mfe_s = f"{sum(c['mfe'])/len(c['mfe']):+.2f}" if c["mfe"] else "  n/a"
            mae_s = f"{sum(c['mae'])/len(c['mae']):+.2f}" if c["mae"] else "  n/a"
            print(
                f"  {sym:<12} {tot:>4} {tp1_pct:>6} {tp2_pct:>6} "
                f"{stp_pct:>7} {exp_pct:>6} {mfe_s:>7} {mae_s:>7}"
            )

    # --- Plain-English interpretation ---
    print()
    print("  Interpretation:")

    if n_confirmed == 0:
        print("    No confirmed observations yet — not enough data to interpret.")
        close_db()
        return

    overall_tp1_rate = len(tp1_hit) / n_confirmed if n_confirmed else 0
    overall_tp2_rate = len(tp2_hit) / n_confirmed if n_confirmed else 0
    overall_stop_rate = len(stopped_all) / n_confirmed if n_confirmed else 0
    overall_expiry_rate = len(expired_all) / n_confirmed if n_confirmed else 0

    # Classify signal quality at an overall level
    if overall_tp1_rate >= 0.35:
        tp1_verdict = f"TP1 milestone rate is solid ({overall_tp1_rate:.0%})."
    elif overall_tp1_rate >= 0.20:
        tp1_verdict = f"TP1 milestone rate is moderate ({overall_tp1_rate:.0%})."
    else:
        tp1_verdict = (
            f"TP1 milestone rate is low ({overall_tp1_rate:.0%}). "
            "Signals may be entering too late or the setup timeframe is too noisy."
        )

    if overall_expiry_rate >= 0.60:
        expiry_verdict = (
            f"Expiry rate is high ({overall_expiry_rate:.0%}). "
            "Setups are not triggering within the expiration window. "
            "Consider reviewing expiration time or entry timing."
        )
    elif overall_expiry_rate >= 0.40:
        expiry_verdict = (
            f"Expiry rate is elevated ({overall_expiry_rate:.0%}). "
            "Worth monitoring — many setups resolve without hitting any level."
        )
    else:
        expiry_verdict = f"Expiry rate is acceptable ({overall_expiry_rate:.0%})."

    if overall_stop_rate <= 0.15:
        stop_verdict = f"Stop rate is low ({overall_stop_rate:.0%}) — favorable."
    elif overall_stop_rate <= 0.30:
        stop_verdict = f"Stop rate is moderate ({overall_stop_rate:.0%})."
    else:
        stop_verdict = (
            f"Stop rate is elevated ({overall_stop_rate:.0%}). "
            "Setups are being stopped out frequently. "
            "Review stop placement or trend filter strength."
        )

    print(f"    {tp1_verdict}")
    print(f"    {expiry_verdict}")
    print(f"    {stop_verdict}")

    # Identify weak symbols
    weak = [
        (sym, c) for sym, c in by_sym.items()
        if c["total"] >= 5
        and (c["expired"] / c["total"]) >= 0.65
        and (c["tp1"] / c["total"]) < 0.20
    ]
    strong = [
        (sym, c) for sym, c in by_sym.items()
        if c["total"] >= 5
        and (c["tp1"] / c["total"]) >= 0.30
        and (c["stopped"] / c["total"]) <= 0.25
    ]

    if weak:
        sym_list = ", ".join(s for s, _ in sorted(weak, key=lambda x: x[1]["expired"], reverse=True))
        print(
            f"    Weak symbols (high expiry, low TP1): {sym_list}. "
            "Consider reducing weight or reviewing trend filter for these."
        )
    if strong:
        sym_list = ", ".join(s for s, _ in sorted(strong, key=lambda x: x[1]["tp1"], reverse=True))
        print(
            f"    Stronger symbols (higher TP1, acceptable stop rate): {sym_list}."
        )
    if not weak and not strong:
        print("    No symbols have enough confirmed observations (≥5) for pattern detection yet.")

    print()
    print("  Note: This is a read-only diagnostic report. No live alerts or")
    print("  trading recommendations are made. All data is from dry-run mode only.")
    print()
    print("=" * 68)
    close_db()


if __name__ == "__main__":
    main()
