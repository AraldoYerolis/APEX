"""Read-only report of opportunity_observations — TA Opportunity Engine v0.1.

Usage (run from repo root):
    PYTHONPATH=src python scripts/report_opportunities.py
    PYTHONPATH=src python scripts/report_opportunities.py --since 2026-09-01T00:00:00Z
    PYTHONPATH=src python scripts/report_opportunities.py --family SWEEP_RECLAIM --status ACTIVE
    PYTHONPATH=src python scripts/report_opportunities.py --ranked
    PYTHONPATH=src python scripts/report_opportunities.py --ranked --status EXPIRED

Without --ranked, this does not start the bot and does not change config,
but it does call init_db(settings.apex_db_path), which executes schema.sql's
CREATE TABLE IF NOT EXISTS statements and the column-migration ALTERs
against whatever database that path points at — idempotent DDL, but DDL
execution nonetheless. Point APEX_DB_PATH at a disposable/local database, not
an existing user or production database, unless you have independently
confirmed running this schema DDL against it is acceptable.

--ranked uses a genuine READ-ONLY SQLite connection instead (mode=ro,
PRAGMA query_only=ON) and never calls init_db — it never creates a missing
database file and never applies any migration/backfill merely to display a
report. If the database or its opportunity_observations table does not
exist yet, or predates the Context and ranking v0.1 score columns, this is
detected via safe read-only schema inspection (PRAGMA table_info) and every
row is reported as legacy/unscored rather than raising or mutating
anything.

CONTEXT-ALIGNMENT SCORE DISCLAIMER (see opportunity/scoring.py,
opportunity/context.py): `total_score` is a transparent research
context-alignment score — NOT a win probability, NOT a live signal
confidence rating, and NOT a trade recommendation. No score or rank ever
removes a finding from this report.

Without --ranked, this report does NOT score or rank opportunities at all
— it only surfaces detection counts and raw detector measurements so the
underlying data can be inspected.

Snapshot/version semantics (read before interpreting VOLATILITY_COMPRESSION
rows)
---------------------------------------------------------------------------
- `direction` on a VOLATILITY_COMPRESSION row is the INITIAL descriptive
  direction observed at first detection — never a live re-assessment of the
  episode. A LONG finding and a later SHORT finding for the same underlying
  episode share one fingerprint/opportunity_uid; the stored `direction` is
  whichever one was present when the row was first inserted.
- Only rows with detector_version == COMPRESSION_IMMUTABLE_SNAPSHOT_VERSION
  (below) have this guarantee for measurements_json/evidence_json/
  warnings_json/anchor/source-candle fields — those are frozen at first
  detection and never overwritten by reconfirmation. Older compression rows
  (detector_version "vol_compression_v0_1") predate this convention: their
  measurements_json reflects whatever finding last touched the row, not
  necessarily the first one. Do not relabel older rows as having the new
  immutable-snapshot semantics.
- SWEEP_RECLAIM rows are unaffected by any of the above: direction is
  structural identity there (unchanged), and measurements_json is refreshed
  on every reconfirmation (unchanged).
- `occurrence_count` counts scan confirmations of the same fingerprint,
  including the first insert — it is not a count of unique candles
  inspected or of independent market events; a single long-lived episode
  re-confirmed on many scans accumulates a high count without that implying
  many distinct episodes.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from apex.config import get_settings
from apex.db import repository as repo
from apex.db.connection import close_db, init_db
from apex.opportunity.scoring import SCORE_VERSION

# Context and ranking v0.1's five score columns — used only to detect
# whether a database predates this milestone via safe read-only schema
# inspection (see _has_score_columns). Not a second source of truth for
# the column list itself; repository.py/schema.sql/connection.py own that.
_SCORE_COLUMNS = frozenset(
    {"context_json", "component_scores_json", "total_score", "score_version", "score_warnings_json"}
)

# Compression detector_version that introduced the immutable first-detection
# snapshot (direction-invariant fingerprint + frozen measurements_json/
# evidence_json/warnings_json/anchor/source-candle fields on reconfirmation).
# Keep this in sync with DETECTOR_VERSION in
# src/apex/opportunity/detectors/volatility_compression.py — it exists here
# only so this report can label rows correctly, not as a second source of
# truth for the version string itself.
COMPRESSION_IMMUTABLE_SNAPSHOT_VERSION = "vol_compression_v0_1_1"


def _counter_lines(counter: Counter, total: int) -> list[str]:
    lines = []
    for key, n in counter.most_common():
        pct = f"{n / total * 100:5.1f}%" if total else "  n/a"
        lines.append(f"    {str(key):<24} {n:5d}  {pct}")
    return lines


def _snapshot_label(row: Any) -> str:
    """Describe what a row's direction/measurements actually represent."""
    if row["setup_family"] != "VOLATILITY_COMPRESSION":
        return "structural direction; measurements refreshed each reconfirmation"
    if row["detector_version"] == COMPRESSION_IMMUTABLE_SNAPSHOT_VERSION:
        return "initial direction (frozen); measurements frozen at first detection"
    return "initial direction; measurements last-touched (pre-snapshot detector version)"


# ------------------------------------------------------------------ --ranked (read-only)


def _open_readonly_connection(db_path: str) -> sqlite3.Connection:
    """A genuine read-only SQLite connection: mode=ro fails closed (raises)
    instead of creating a missing database file, and PRAGMA query_only=ON
    additionally refuses any write this connection might otherwise attempt
    — this never applies schema.sql or any migration, unlike init_db().
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


def _has_score_columns(conn: sqlite3.Connection) -> bool:
    """Safe read-only schema inspection (PRAGMA table_info) — the only way
    this script decides whether a database predates the Context and
    ranking v0.1 score columns; it never attempts to add them itself."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(opportunity_observations)").fetchall()}
    return _SCORE_COLUMNS.issubset(cols)


def _is_current_scored_row(row: Any, scored_schema: bool) -> bool:
    if not scored_schema:
        return False
    keys = row.keys()
    if "total_score" not in keys or "score_version" not in keys:
        return False
    total_score = row["total_score"]
    score_version = row["score_version"]
    if total_score is None or score_version != SCORE_VERSION:
        return False
    if not isinstance(total_score, (int, float)) or isinstance(total_score, bool):
        return False
    return 0 <= total_score <= 100


def _print_ranked_report(rows: list, *, status: Optional[str], scored_schema: bool) -> None:
    total = len(rows)
    print()
    print("=" * 88)
    print("  APEX TA Opportunity Engine — Ranked Report (read-only, --ranked)")
    print("=" * 88)
    print("  Score = research CONTEXT-ALIGNMENT only (see opportunity/scoring.py).")
    print("  NOT a win probability, NOT live signal confidence, NOT a trade recommendation.")
    print("  No score or rank ever removes a finding from this report.")
    print(f"  Status filter      : {status}")
    print(f"  Total rows         : {total}")
    if not scored_schema:
        print("  Schema predates Context and ranking v0.1 score columns — every row is UNSCORED/legacy.")
    print()

    if total == 0:
        print("  No opportunities match this filter.")
        print("=" * 88)
        return

    print(
        f"  {'Rank':>4} {'UID':>10} {'Score':>7} {'Version':<24} {'Symbol':<10} {'Family':<30} "
        f"{'Dir':<5} {'TF':<3} {'First detected (as-of)':<22} {'Status':<8}"
    )
    print("  " + "-" * 148)
    for i, row in enumerate(rows, start=1):
        keys = row.keys()
        is_current = _is_current_scored_row(row, scored_schema)
        total_score = row["total_score"] if scored_schema and "total_score" in keys else None
        score_version = row["score_version"] if scored_schema and "score_version" in keys else None
        score_label = f"{total_score:.1f}" if is_current else "UNSCORED"
        version_label = score_version if (scored_schema and score_version) else "legacy/none"
        uid_short = row["opportunity_uid"][:10]
        print(
            f"  {i:>4} {uid_short:>10} {score_label:>7} {version_label:<24} {row['symbol']:<10} "
            f"{row['setup_family']:<30} {row['direction']:<5} {row['primary_timeframe']:<3} "
            f"{row['first_detected_at']:<22} {row['status']:<8}"
        )

        if scored_schema and "score_warnings_json" in keys and row["score_warnings_json"]:
            try:
                warnings = json.loads(row["score_warnings_json"])
            except (TypeError, ValueError):
                warnings = row["score_warnings_json"]
            if warnings:
                print(f"        warnings   : {warnings}")

        if scored_schema and "component_scores_json" in keys and row["component_scores_json"]:
            try:
                components = json.loads(row["component_scores_json"])
            except (TypeError, ValueError):
                components = None
            if components:
                print(
                    "        coverage   : "
                    f"available={components.get('available_weight')} "
                    f"applicable={components.get('applicable_weight')}"
                )
                for c in components.get("components", []):
                    print(
                        f"          {c['name']:<18} status={c['status']:<14} "
                        f"direction={c['component_direction']} alignment={c['alignment']} "
                        f"contribution={c['contribution']}"
                    )

    print("=" * 88)


def _run_ranked_report(args: argparse.Namespace) -> None:
    settings = get_settings()
    db_path = settings.apex_db_path

    if not Path(db_path).exists():
        print(
            f"No database file at {db_path!r}; --ranked never creates one "
            "(use the non-ranked report, or run the bot, to initialize it first).",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        conn = _open_readonly_connection(db_path)
    except sqlite3.Error as e:
        print(f"ERROR: could not open {db_path!r} read-only: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        if not _table_exists(conn, "opportunity_observations"):
            print()
            print("=" * 88)
            print("  APEX TA Opportunity Engine — Ranked Report (read-only, --ranked)")
            print("=" * 88)
            print("  No opportunity_observations table found. Nothing to report.")
            print("=" * 88)
            return

        scored_schema = _has_score_columns(conn)
        # --ranked defaults to ACTIVE unless the caller explicitly filtered
        # by --status; the optional EXPIRED view stays an explicit,
        # separate, historical call, never mixed with ACTIVE by default.
        status = args.status if args.status is not None else "ACTIVE"

        try:
            if scored_schema:
                rows = repo.get_ranked_opportunities(
                    conn,
                    since=args.since,
                    setup_family=args.family,
                    symbol=args.symbol,
                    primary_timeframe=args.timeframe,
                    status=status,
                    limit=args.limit,
                )
            else:
                # Legacy schema predating Context and ranking v0.1 has no
                # score columns at all, so every row is unscored by
                # definition — fall back to the plain chronological query
                # rather than issuing SQL that references missing columns.
                rows = repo.get_opportunities(
                    conn,
                    since=args.since,
                    setup_family=args.family,
                    symbol=args.symbol,
                    primary_timeframe=args.timeframe,
                    status=status,
                    limit=args.limit,
                )
        except sqlite3.Error as e:
            print(f"ERROR: could not query opportunity_observations: {e}", file=sys.stderr)
            sys.exit(1)

        _print_ranked_report(list(rows), status=status, scored_schema=scored_schema)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="APEX TA Opportunity Engine v0.1 report — read-only, no scoring"
    )
    parser.add_argument("--since", metavar="ISO_TIMESTAMP", default=None,
                         help="Filter to opportunities with last_seen_at >= TIMESTAMP")
    parser.add_argument("--family", metavar="SETUP_FAMILY", default=None,
                         help="VOLATILITY_COMPRESSION or SWEEP_RECLAIM")
    parser.add_argument("--symbol", metavar="SYMBOL", default=None)
    parser.add_argument("--timeframe", metavar="3m|5m", default=None)
    parser.add_argument("--status", metavar="ACTIVE|EXPIRED", default=None)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument(
        "--ranked", action="store_true",
        help=(
            "Show the read-only, research CONTEXT-ALIGNMENT ranked view instead of the "
            "default chronological report (defaults --status to ACTIVE unless set explicitly). "
            "Uses a genuine read-only connection; never creates or migrates the database."
        ),
    )
    args = parser.parse_args(argv)

    if args.ranked:
        _run_ranked_report(args)
        return

    settings = get_settings()
    conn = init_db(settings.apex_db_path)

    try:
        rows = repo.get_opportunities(
            conn,
            since=args.since,
            setup_family=args.family,
            symbol=args.symbol,
            primary_timeframe=args.timeframe,
            status=args.status,
            limit=args.limit,
        )
    except Exception as e:
        print(f"ERROR: could not query opportunity_observations: {e}", file=sys.stderr)
        print("Has the DB been initialized with the latest schema?", file=sys.stderr)
        close_db()
        sys.exit(1)

    total = len(rows)
    print()
    print("=" * 68)
    print("  APEX TA Opportunity Engine v0.1 — Research Report")
    print("=" * 68)
    if args.since:
        print(f"  Filtered since   : {args.since}")
    print(f"  Total rows       : {total}")
    print(f"  opportunity_engine_enabled = {settings.opportunity_engine_enabled}")
    print(f"  dry_run_mode               = {settings.dry_run_mode}")

    if total == 0:
        print()
        print("  No opportunities recorded yet.")
        print("=" * 68)
        close_db()
        return

    by_family = Counter(r["setup_family"] for r in rows)
    by_symbol = Counter(r["symbol"] for r in rows)
    by_direction = Counter(r["direction"] for r in rows)
    by_timeframe = Counter(r["primary_timeframe"] for r in rows)
    by_status = Counter(r["status"] for r in rows)
    by_detector_version = Counter(r["detector_version"] for r in rows)

    print()
    print("  By setup_family:")
    print(*_counter_lines(by_family, total), sep="\n")

    print()
    print("  By primary_timeframe:")
    print(*_counter_lines(by_timeframe, total), sep="\n")

    print()
    print("  By detector_version:")
    print(*_counter_lines(by_detector_version, total), sep="\n")
    print(
        f"    (compression rows are only guaranteed an immutable first-detection"
        f" snapshot under detector_version={COMPRESSION_IMMUTABLE_SNAPSHOT_VERSION!r})"
    )

    print()
    print("  By direction (see 'Snapshot/version semantics' in this script's docstring —")
    print("  for VOLATILITY_COMPRESSION this is the INITIAL direction, not a live re-assessment):")
    print(*_counter_lines(by_direction, total), sep="\n")

    print()
    print("  By status (ACTIVE vs EXPIRED):")
    print(*_counter_lines(by_status, total), sep="\n")

    print()
    print("  By symbol (top 15):")
    print(*_counter_lines(Counter(dict(by_symbol.most_common(15))), total), sep="\n")

    occurrence_counts = [r["occurrence_count"] for r in rows]
    deduped = sum(1 for c in occurrence_counts if c > 1)
    print()
    print("  Deduplication (occurrence_count = scan confirmations incl. the first,")
    print("  not unique candles or independent market events):")
    print(f"    Rows re-confirmed >1x : {deduped:5d}  {deduped / total * 100:5.1f}%")
    print(f"    Max occurrence_count  : {max(occurrence_counts):5d}")
    print(f"    Avg occurrence_count  : {sum(occurrence_counts) / total:5.2f}")

    print()
    print(f"  Most recent {min(10, total)} opportunities:")
    print(
        f"  {'UID':>10}  {'Symbol':<10} {'Family':<22} {'Dir':<5} "
        f"{'TF':<3} {'Status':<8} {'Occ':>4} {'DetectorVersion':<22} {'Last seen':<20}"
    )
    print("  " + "-" * 116)
    for r in rows[:10]:
        uid_short = r["opportunity_uid"][:8]
        print(
            f"  {uid_short:>10}  {r['symbol']:<10} {r['setup_family']:<22} "
            f"{r['direction']:<5} {r['primary_timeframe']:<3} {r['status']:<8} "
            f"{r['occurrence_count']:>4} {r['detector_version']:<22} {r['last_seen_at']:<20}"
        )

    print()
    print("  Raw measurements for the most recent opportunity:")
    latest: dict[str, Any] = rows[0]
    print(f"    snapshot semantics: {_snapshot_label(latest)}")
    for field_name in ("evidence_json", "warnings_json", "measurements_json"):
        try:
            parsed = json.loads(latest[field_name]) if latest[field_name] else None
        except (TypeError, ValueError):
            parsed = latest[field_name]
        print(f"    {field_name}: {parsed}")

    print()
    print("=" * 68)
    close_db()


if __name__ == "__main__":
    main()
