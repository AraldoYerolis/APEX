"""Read-only report of signal_observations for dry-run quality analysis.

Usage (run from repo root):
    PYTHONPATH=src python scripts/report_signal_observations.py

Requires APEX_DB_PATH to be set in .env (or defaults to ./data/apex.db).
Does not start the bot, does not write any data.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from typing import Any

from apex.config import get_settings
from apex.db.connection import close_db, init_db


def _pct(n: int, total: int) -> str:
    if total == 0:
        return "  n/a"
    return f"{n / total * 100:5.1f}%"


def _avg(values: list[float]) -> str:
    if not values:
        return "n/a"
    return f"{sum(values) / len(values):.1f}"


def _effective_final_status(row: Any) -> str:
    """Return the best available terminal status for a closed row.

    Rows written before Milestone 10A have final_status=NULL; fall back to
    status in that case so legacy HIT_1R rows are still counted correctly.
    """
    return row["final_status"] or row["status"]


def main() -> None:
    settings = get_settings()
    conn = init_db(settings.apex_db_path)

    try:
        rows = conn.execute(
            "SELECT * FROM signal_observations ORDER BY observed_at DESC"
        ).fetchall()
    except Exception as e:
        print(f"ERROR: could not query signal_observations: {e}", file=sys.stderr)
        print("Has the DB been initialized with the latest schema?", file=sys.stderr)
        close_db()
        sys.exit(1)

    total = len(rows)
    if total == 0:
        print("No signal observations recorded yet.")
        close_db()
        return

    open_obs = [r for r in rows if r["status"] == "OBSERVED"]
    closed_obs = [r for r in rows if r["status"] != "OBSERVED"]
    confirmed = [r for r in rows if r["signal_type"] == "CONFIRMED_SETUP"]
    forming = [r for r in rows if r["signal_type"] == "SETUP_FORMING"]

    # --- Outcome counts (CONFIRMED_SETUP only) ---
    # Use _effective_final_status so pre-10A HIT_1R rows are counted under their
    # legacy status, and new rows are counted under their final_status.
    confirmed_open = [r for r in confirmed if r["status"] == "OBSERVED"]

    # TP1 milestone: any confirmed row that has hit_1r_at set (regardless of final outcome)
    tp1_milestone = [r for r in confirmed if r["hit_1r_at"] is not None]

    # Terminal outcomes — new rows use final_status; legacy rows use status
    hit_2r = [r for r in confirmed if _effective_final_status(r) == "HIT_2R"]
    stopped = [r for r in confirmed if _effective_final_status(r) == "STOPPED"]
    expired = [r for r in confirmed if _effective_final_status(r) == "EXPIRED"]
    # Legacy HIT_1R rows (pre-Milestone 10A, terminal): still counted as terminal wins
    legacy_hit_1r = [r for r in confirmed if r["status"] == "HIT_1R" and r["final_status"] is None]

    # Stopped with/without prior TP1
    stopped_after_tp1 = [r for r in stopped if r["hit_1r_before_stop"] == 1]
    stopped_without_tp1 = [r for r in stopped if r["hit_1r_before_stop"] == 0]
    # hit_1r_before_stop is NULL on pre-10A rows; don't count those here
    stopped_legacy = [r for r in stopped if r["hit_1r_before_stop"] is None]

    # Expired with/without prior TP1
    expired_after_tp1 = [r for r in expired if r["hit_1r_before_expiry"] == 1]
    expired_without_tp1 = [r for r in expired if r["hit_1r_before_expiry"] == 0]
    expired_legacy = [r for r in expired if r["hit_1r_before_expiry"] is None]

    # Avg outcome_r over all closed confirmed rows that have one
    outcome_rs = [r["outcome_r"] for r in confirmed if r["outcome_r"] is not None]
    avg_r = sum(outcome_rs) / len(outcome_rs) if outcome_rs else None

    # Timing averages
    t1_times = [r["time_to_1r_seconds"] for r in confirmed if r["time_to_1r_seconds"] is not None]
    t2_times = [r["time_to_2r_seconds"] for r in confirmed if r["time_to_2r_seconds"] is not None]
    stop_times = [r["time_to_stop_seconds"] for r in confirmed if r["time_to_stop_seconds"] is not None]

    def _fmt_mins(secs_list: list[float]) -> str:
        if not secs_list:
            return "n/a"
        avg_secs = sum(secs_list) / len(secs_list)
        return f"{avg_secs / 60:.1f} min  (n={len(secs_list)})"

    # MFE/MAE averages
    mfe_vals = [r["max_favorable_excursion"] for r in confirmed if r["max_favorable_excursion"] is not None]
    mae_vals = [r["max_adverse_excursion"] for r in confirmed if r["max_adverse_excursion"] is not None]

    # Long vs Short breakdown (CONFIRMED only)
    long_confirmed = [r for r in confirmed if r["direction"] == "LONG"]
    short_confirmed = [r for r in confirmed if r["direction"] == "SHORT"]

    # Per-symbol breakdown (CONFIRMED only)
    by_symbol: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "total": 0, "tp1": 0, "HIT_2R": 0, "STOPPED": 0, "EXPIRED": 0,
        "legacy_HIT_1R": 0, "OBSERVED": 0,
        "mfe": [], "mae": [],
    })
    for r in confirmed:
        sym = r["symbol"]
        by_symbol[sym]["total"] += 1
        fs = _effective_final_status(r)
        if r["hit_1r_at"] is not None:
            by_symbol[sym]["tp1"] += 1
        if fs in ("HIT_2R", "STOPPED", "EXPIRED"):
            by_symbol[sym][fs] += 1
        elif r["status"] == "HIT_1R" and r["final_status"] is None:
            by_symbol[sym]["legacy_HIT_1R"] += 1
        elif r["status"] == "OBSERVED":
            by_symbol[sym]["OBSERVED"] += 1
        if r["max_favorable_excursion"] is not None:
            by_symbol[sym]["mfe"].append(r["max_favorable_excursion"])
        if r["max_adverse_excursion"] is not None:
            by_symbol[sym]["mae"].append(r["max_adverse_excursion"])

    n_confirmed = len(confirmed)
    n_closed_confirmed = n_confirmed - len(confirmed_open)

    print()
    print("=" * 68)
    print("  APEX Signal Observations Report")
    print("=" * 68)
    print(f"  Total observations  : {total}")
    print(f"  Open (OBSERVED)     : {len(open_obs)}")
    print(f"  Closed              : {len(closed_obs)}")
    print()
    print("  By signal type:")
    print(f"    CONFIRMED_SETUP   : {n_confirmed}")
    print(f"    SETUP_FORMING     : {len(forming)}")
    print()

    print("  CONFIRMED outcomes (all closed confirmed):")
    print(f"    TP1 milestone hit : {len(tp1_milestone):4d}  {_pct(len(tp1_milestone), n_confirmed)}"
          "  (reached TP1, regardless of final outcome)")
    print(f"    HIT_2R            : {len(hit_2r):4d}  {_pct(len(hit_2r), n_confirmed)}")
    print(f"    STOPPED (total)   : {len(stopped):4d}  {_pct(len(stopped), n_confirmed)}")
    if stopped_after_tp1 or stopped_without_tp1:
        print(f"      ↳ after TP1     : {len(stopped_after_tp1):4d}  {_pct(len(stopped_after_tp1), len(stopped))}"
              "  (of stopped)")
        print(f"      ↳ without TP1   : {len(stopped_without_tp1):4d}  {_pct(len(stopped_without_tp1), len(stopped))}"
              "  (of stopped)")
        if stopped_legacy:
            print(f"      ↳ legacy (pre-10A): {len(stopped_legacy):3d}")
    print(f"    EXPIRED (total)   : {len(expired):4d}  {_pct(len(expired), n_confirmed)}")
    if expired_after_tp1 or expired_without_tp1:
        print(f"      ↳ after TP1     : {len(expired_after_tp1):4d}  {_pct(len(expired_after_tp1), len(expired))}"
              "  (of expired)")
        print(f"      ↳ without TP1   : {len(expired_without_tp1):4d}  {_pct(len(expired_without_tp1), len(expired))}"
              "  (of expired)")
        if expired_legacy:
            print(f"      ↳ legacy (pre-10A): {len(expired_legacy):3d}")
    if legacy_hit_1r:
        print(f"    HIT_1R (legacy)   : {len(legacy_hit_1r):4d}  {_pct(len(legacy_hit_1r), n_confirmed)}"
              "  (terminal TP1, pre-Milestone-10A)")
    print(f"    OBSERVED (open)   : {len(confirmed_open):4d}  {_pct(len(confirmed_open), n_confirmed)}")

    print()
    print("  Timing (CONFIRMED, where available):")
    print(f"    Avg time to TP1   : {_fmt_mins(t1_times)}")
    print(f"    Avg time to TP2   : {_fmt_mins(t2_times)}")
    print(f"    Avg time to stop  : {_fmt_mins(stop_times)}")

    print()
    print("  Excursion (CONFIRMED, where available):")
    if mfe_vals:
        print(f"    Avg MFE           : {sum(mfe_vals)/len(mfe_vals):+.2f} R  (n={len(mfe_vals)})")
    else:
        print("    Avg MFE           : n/a")
    if mae_vals:
        print(f"    Avg MAE           : {sum(mae_vals)/len(mae_vals):+.2f} R  (n={len(mae_vals)})")
    else:
        print("    Avg MAE           : n/a")
    if avg_r is not None:
        print(f"    Avg outcome_r     : {avg_r:+.2f} R  (n={len(outcome_rs)}, closed confirmed)")
    else:
        print("    Avg outcome_r     : n/a")

    print()
    print("  Direction breakdown (CONFIRMED):")
    print(f"    LONG              : {len(long_confirmed):4d}  {_pct(len(long_confirmed), n_confirmed)}")
    print(f"    SHORT             : {len(short_confirmed):4d}  {_pct(len(short_confirmed), n_confirmed)}")

    if by_symbol:
        print()
        print("  By symbol (CONFIRMED, top 15 by count):")
        sorted_syms = sorted(by_symbol.items(), key=lambda x: x[1]["total"], reverse=True)
        hdr = f"  {'Symbol':<12} {'Tot':>4} {'TP1%':>6} {'2R%':>6} {'STOP%':>7} {'EXP%':>6} {'avgMFE':>7} {'avgMAE':>7}"
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for sym, c in sorted_syms[:15]:
            tot = c["total"]
            tp1_pct = f"{c['tp1']/tot*100:5.1f}%" if tot else "  n/a"
            t2_pct = f"{c['HIT_2R']/tot*100:5.1f}%" if tot else "  n/a"
            st_pct = f"{c['STOPPED']/tot*100:5.1f}%" if tot else "  n/a"
            ex_pct = f"{c['EXPIRED']/tot*100:5.1f}%" if tot else "  n/a"
            mfe_s = f"{sum(c['mfe'])/len(c['mfe']):+.2f}" if c["mfe"] else "  n/a"
            mae_s = f"{sum(c['mae'])/len(c['mae']):+.2f}" if c["mae"] else "  n/a"
            print(f"  {sym:<12} {tot:>4} {tp1_pct:>6} {t2_pct:>6} {st_pct:>7} {ex_pct:>6} {mfe_s:>7} {mae_s:>7}")

    print()
    print("  Recent 10 observations:")
    print(
        f"  {'UID':>10}  {'Symbol':<10} {'Type':<18} {'Dir':<5} "
        f"{'Status':<9} {'TP1':>4} {'Observed at':<20} {'R':>5}"
    )
    print("  " + "-" * 90)
    for r in rows[:10]:
        uid_short = r["observation_uid"][:8]
        outcome = f"{r['outcome_r']:+.1f}" if r["outcome_r"] is not None else "  -"
        tp1_mark = "Y" if r["hit_1r_at"] else "-"
        print(
            f"  {uid_short:>10}  {r['symbol']:<10} {r['signal_type']:<18} "
            f"{r['direction']:<5} {r['status']:<9} {tp1_mark:>4} "
            f"{r['observed_at']:<20} {outcome:>5}"
        )

    print()
    print("=" * 68)
    close_db()


if __name__ == "__main__":
    main()
