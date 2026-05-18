"""Read-only report of signal_observations for dry-run quality analysis.

Usage (run from repo root):
    PYTHONPATH=src python scripts/report_signal_observations.py

Requires APEX_DB_PATH to be set in .env (or defaults to ./data/apex.db).
Does not start the bot, does not write any data.
"""
from __future__ import annotations

import sys
from collections import defaultdict

from apex.config import get_settings
from apex.db.connection import close_db, init_db


def _pct(n: int, total: int) -> str:
    if total == 0:
        return "  n/a"
    return f"{n / total * 100:5.1f}%"


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

    # Outcome counts are CONFIRMED_SETUP only — SETUP_FORMING rows do not have
    # R-outcome evaluation so must not pollute these stats.
    confirmed_open = [r for r in confirmed if r["status"] == "OBSERVED"]
    hit_1r = [r for r in confirmed if r["status"] == "HIT_1R"]
    hit_2r = [r for r in confirmed if r["status"] == "HIT_2R"]
    stopped = [r for r in confirmed if r["status"] == "STOPPED"]
    expired = [r for r in confirmed if r["status"] == "EXPIRED"]

    # Outcome_r average over closed CONFIRMED with a recorded outcome_r
    outcome_rs = [
        r["outcome_r"]
        for r in confirmed
        if r["outcome_r"] is not None
    ]
    avg_r = sum(outcome_rs) / len(outcome_rs) if outcome_rs else None

    # Per-symbol breakdown (CONFIRMED only)
    by_symbol: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "HIT_1R": 0, "HIT_2R": 0, "STOPPED": 0, "EXPIRED": 0, "OBSERVED": 0}
    )
    for r in confirmed:
        sym = r["symbol"]
        by_symbol[sym]["total"] += 1
        by_symbol[sym][r["status"]] += 1

    print()
    print("=" * 60)
    print("  APEX Signal Observations Report")
    print("=" * 60)
    print(f"  Total observations : {total}")
    print(f"  Open (OBSERVED)    : {len(open_obs)}")
    print(f"  Closed             : {len(closed_obs)}")
    print()
    print("  By signal type:")
    print(f"    CONFIRMED_SETUP  : {len(confirmed)}")
    print(f"    SETUP_FORMING    : {len(forming)}")
    print()
    print("  Outcomes (CONFIRMED_SETUP):")
    n_confirmed = len(confirmed)
    print(f"    HIT_1R   : {len(hit_1r):4d}  {_pct(len(hit_1r), n_confirmed)}")
    print(f"    HIT_2R   : {len(hit_2r):4d}  {_pct(len(hit_2r), n_confirmed)}")
    print(f"    STOPPED  : {len(stopped):4d}  {_pct(len(stopped), n_confirmed)}")
    print(f"    EXPIRED  : {len(expired):4d}  {_pct(len(expired), n_confirmed)}")
    print(f"    OBSERVED : {len(confirmed_open):4d}  {_pct(len(confirmed_open), n_confirmed)}")
    if avg_r is not None:
        print(f"\n  Avg outcome_r (closed CONFIRMED): {avg_r:+.2f} R  (n={len(outcome_rs)})")
    else:
        print("\n  Avg outcome_r: n/a (no closed CONFIRMED observations yet)")

    if by_symbol:
        print()
        print("  By symbol (CONFIRMED, top 15 by count):")
        sorted_syms = sorted(by_symbol.items(), key=lambda x: x[1]["total"], reverse=True)
        print(f"  {'Symbol':<12} {'Total':>5} {'1R':>5} {'2R':>5} {'STOP':>6} {'EXP':>5} {'OPEN':>5}")
        print("  " + "-" * 46)
        for sym, counts in sorted_syms[:15]:
            print(
                f"  {sym:<12} {counts['total']:>5} "
                f"{counts['HIT_1R']:>5} {counts['HIT_2R']:>5} "
                f"{counts['STOPPED']:>6} {counts['EXPIRED']:>5} {counts['OBSERVED']:>5}"
            )

    print()
    print("  Recent 10 observations:")
    print(f"  {'UID':>10}  {'Symbol':<10} {'Type':<18} {'Dir':<6} {'Status':<9} {'Observed at':<22} {'R':>5}")
    print("  " + "-" * 88)
    for r in rows[:10]:
        uid_short = r["observation_uid"][:8]
        outcome = f"{r['outcome_r']:+.1f}" if r["outcome_r"] is not None else "  -"
        print(
            f"  {uid_short:>10}  {r['symbol']:<10} {r['signal_type']:<18} "
            f"{r['direction']:<6} {r['status']:<9} {r['observed_at']:<22} {outcome:>5}"
        )

    print()
    print("=" * 60)
    close_db()


if __name__ == "__main__":
    main()
