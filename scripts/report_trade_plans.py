"""Read-only report of opportunity_trade_plans / opportunity_trade_plan_outcomes
— Trade plans and outcome evidence v0.1.

Usage (run from repo root):
    PYTHONPATH=src python scripts/report_trade_plans.py
    PYTHONPATH=src python scripts/report_trade_plans.py --symbol BTC --direction LONG
    PYTHONPATH=src python scripts/report_trade_plans.py --family SWEEP_RECLAIM --state HIT_2R
    PYTHONPATH=src python scripts/report_trade_plans.py --availability UNAVAILABLE

This script is READ-ONLY, always: it opens SQLite with `mode=ro` (fails
closed — raises rather than creating a missing database file) plus
`PRAGMA query_only=ON` (refuses any write this connection might otherwise
attempt), and it never calls init_db() and never creates or migrates
anything. If the database file, the opportunity_trade_plans table, or the
opportunity_trade_plan_outcomes table does not exist yet, this is detected
via safe read-only schema inspection (PRAGMA table_info /
sqlite_master) and reported as a clear legacy/no-data result rather than
raising or mutating anything.

RESEARCH-ONLY DISCLAIMER (see opportunity/trade_plan.py,
opportunity/trade_plan_outcome.py): every plan and outcome row shown here is
a hypothetical, research-only record — a resting-limit-at-source-close fill
against *actual* later closed-candle data, never a claim of real execution,
fill proof, win probability, position sizing, or a trade recommendation.
No row is ever hidden because it is UNAVAILABLE, AMBIGUOUS, INSUFFICIENT_DATA,
or EXPIRED — every state is shown exactly as recorded.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

from apex.config import get_settings

REQUIRED_TABLES = ("opportunity_trade_plans", "opportunity_trade_plan_outcomes")


def _open_readonly_connection(db_path: str) -> sqlite3.Connection:
    """A genuine read-only SQLite connection: mode=ro fails closed (raises)
    instead of creating a missing database file, and PRAGMA query_only=ON
    additionally refuses any write this connection might otherwise attempt.
    Never applies schema.sql or any migration, unlike init_db().
    """
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _missing_tables(conn: sqlite3.Connection) -> list[str]:
    return [t for t in REQUIRED_TABLES if not _table_exists(conn, t)]


def _query_rows(conn: sqlite3.Connection, args: argparse.Namespace) -> list[sqlite3.Row]:
    conditions = []
    params: list[Any] = []
    if args.symbol:
        conditions.append("p.symbol = ?")
        params.append(args.symbol.upper())
    if args.direction:
        conditions.append("p.direction = ?")
        params.append(args.direction.upper())
    if args.family:
        conditions.append("p.setup_family = ?")
        params.append(args.family.upper())
    if args.timeframe:
        conditions.append("p.primary_timeframe = ?")
        params.append(args.timeframe)
    if args.availability:
        conditions.append("p.availability = ?")
        params.append(args.availability.upper())
    if args.state:
        conditions.append("o.state = ?")
        params.append(args.state.upper())
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(args.limit)
    return conn.execute(
        f"""
        SELECT p.*, o.state AS outcome_state, o.last_evaluated_ms, o.last_evaluated_open_time,
               o.entry_open_time, o.hit_1r_open_time, o.terminal_open_time, o.terminal_reason,
               o.mfe_r, o.mae_r, o.data_quality, o.first_missing_boundary_ms,
               o.is_ambiguous, o.decisive_ohlc_json, o.crossed_levels_json, o.evidence_json,
               o.contract_version AS outcome_contract_version
        FROM opportunity_trade_plans p
        JOIN opportunity_trade_plan_outcomes o ON o.plan_uid = p.plan_uid
        {where}
        ORDER BY p.created_at DESC, p.plan_uid ASC
        LIMIT ?
        """,
        params,
    ).fetchall()


def _print_report(rows: list[sqlite3.Row], args: argparse.Namespace) -> None:
    total = len(rows)
    print()
    print("=" * 100)
    print("  APEX Trade Plans and Outcome Evidence v0.1 — Report (read-only)")
    print("=" * 100)
    print("  Every row is a HYPOTHETICAL research record: a resting-limit-at-source-close fill")
    print("  against later actual closed-candle data — NOT execution, fill proof, win probability,")
    print("  position sizing, or a trade recommendation. No state is ever hidden.")
    print(f"  Filters: symbol={args.symbol} direction={args.direction} family={args.family} "
          f"timeframe={args.timeframe} availability={args.availability} state={args.state}")
    print(f"  Total rows: {total}")
    print()

    if total == 0:
        print("  No trade plans match this filter.")
        print("=" * 100)
        return

    print(
        f"  {'Symbol':<10} {'Dir':<5} {'TF':<3} {'Family':<30} "
        f"{'Avail':<11} {'State':<18} {'Entry':>10} {'Stop':>10} {'T1':>10} {'T2':>10}"
    )
    print("  " + "-" * 150)
    for row in rows:
        entry = f"{row['entry_price']:.4f}" if row["entry_price"] is not None else "n/a"
        stop = f"{row['stop_price']:.4f}" if row["stop_price"] is not None else "n/a"
        t1 = f"{row['target_1r_price']:.4f}" if row["target_1r_price"] is not None else "n/a"
        t2 = f"{row['target_2r_price']:.4f}" if row["target_2r_price"] is not None else "n/a"
        print(
            f"  {row['symbol']:<10} {row['direction']:<5} "
            f"{row['primary_timeframe']:<3} {row['setup_family']:<30} {row['availability']:<11} "
            f"{row['outcome_state']:<18} {entry:>10} {stop:>10} {t1:>10} {t2:>10}"
        )
        print(f"        plan_uid           : {row['plan_uid']}")
        if row["availability"] == "UNAVAILABLE":
            print(f"        unavailable_reason : {row['unavailable_reason']}")
        print(
            f"        created_at={row['created_at']} evaluation_not_before_ms={row['evaluation_not_before_ms']} "
            f"evaluation_expiry_ms={row['evaluation_expiry_ms']}"
        )
        print(
            f"        mfe_r={row['mfe_r']} mae_r={row['mae_r']} data_quality={row['data_quality']} "
            f"is_ambiguous={bool(row['is_ambiguous'])} terminal_reason={row['terminal_reason']}"
        )
        if row["decisive_ohlc_json"]:
            try:
                decisive = json.loads(row["decisive_ohlc_json"])
            except (TypeError, ValueError):
                decisive = row["decisive_ohlc_json"]
            print(f"        decisive_ohlc      : {decisive}")
        if row["crossed_levels_json"]:
            try:
                crossed = json.loads(row["crossed_levels_json"])
            except (TypeError, ValueError):
                crossed = row["crossed_levels_json"]
            if crossed:
                print(f"        crossed_levels     : {crossed}")

    print("=" * 100)


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "APEX Trade Plans and Outcome Evidence v0.1 report — read-only, "
            "never creates or migrates the database"
        )
    )
    parser.add_argument("--symbol", metavar="SYMBOL", default=None)
    parser.add_argument("--direction", metavar="LONG|SHORT", default=None)
    parser.add_argument("--family", metavar="SETUP_FAMILY", default=None)
    parser.add_argument("--timeframe", metavar="3m|5m", default=None)
    parser.add_argument("--availability", metavar="AVAILABLE|UNAVAILABLE", default=None)
    parser.add_argument(
        "--state",
        metavar="OUTCOME_STATE",
        default=None,
        help=(
            "NOT_EVALUABLE|PENDING_ENTRY|ENTERED|HIT_1R|HIT_2R|STOPPED|"
            "EXPIRED_UNENTERED|EXPIRED_OPEN|AMBIGUOUS|INSUFFICIENT_DATA"
        ),
    )
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args(argv)

    settings = get_settings()
    db_path = settings.apex_db_path

    if not Path(db_path).exists():
        print(
            f"No database file at {db_path!r}; this report never creates one "
            "(run the bot, or scripts/report_opportunities.py without --ranked, to initialize it first).",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        conn = _open_readonly_connection(db_path)
    except sqlite3.Error as e:
        print(f"ERROR: could not open {db_path!r} read-only: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        missing = _missing_tables(conn)
        if missing:
            print()
            print("=" * 100)
            print("  APEX Trade Plans and Outcome Evidence v0.1 — Report (read-only)")
            print("=" * 100)
            print(
                "  This database predates Trade plans and outcome evidence v0.1 "
                f"(missing table(s): {', '.join(missing)}). No plan/outcome data to report."
            )
            print("=" * 100)
            return

        try:
            rows = _query_rows(conn, args)
        except sqlite3.Error as e:
            print(f"ERROR: could not query trade plan tables: {e}", file=sys.stderr)
            sys.exit(1)

        _print_report(list(rows), args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
