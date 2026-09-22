"""Shadow Alert Runtime Wiring v0.1 — read-only shadow-alert decision report.

Usage (run from repo root):
    PYTHONPATH=src python scripts/report_shadow_alerts.py
    PYTHONPATH=src python scripts/report_shadow_alerts.py --pilot-id my-pilot-01

This script is READ-ONLY, always: it opens SQLite with `mode=ro` (fails
closed — raises rather than creating a missing database file) plus
`PRAGMA query_only=ON`, and it never calls init_db() and never creates or
migrates anything. It never sends a notification and never imports
`apex.notifications`. It never makes a network call.

It reports only what `apex.opportunity.shadow_runtime` actually recorded in
`shadow_alert_decisions` for one pilot_id: run/decision counts, the
would-emit count, suppression counts broken out by exact reason code (plus
a few readability groupings — duplicate/conflict/cooldown/cap/stale/
touched-or-out-of-range/cost/quality-gate), unique-candidate and symbol/
direction/family/timeframe/cohort concentration, and — where available — the
CURRENT `opportunity_trade_plan_outcomes.state` for each decision's
opportunity_uid (explicitly labeled "at report time": a decision row is
immutable evidence of what was true when it was recorded, while the joined
outcome state can keep changing afterward). This script never invents a
fill, a P&L figure, or a recommendation — only counts of what the runtime
actually persisted.
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

REQUIRED_TABLES = ("shadow_alert_decisions", "opportunity_trade_plan_outcomes")

# Reason-code groupings for a few operationally interesting headline counts
# (see apex.opportunity.shadow_alerts for the full reason-code set) — every
# reason is still counted individually via the "by exact reason" breakdown
# regardless of whether it also falls into one of these groups.
_DUPLICATE_REASONS = ("DUPLICATE_OPPORTUNITY_UID_IN_BATCH", "OPPORTUNITY_ALREADY_EMITTED")
_CONFLICT_REASONS = ("OPPOSITE_DIRECTION_CONFLICT", "CLUSTER_NON_REPRESENTATIVE", "OVERLAPS_OPEN_CLUSTER")
_COOLDOWN_REASONS = ("COOLDOWN_ACTIVE",)
_CAP_REASONS = ("ROLLING_HOUR_CAP_REACHED", "ROLLING_DAY_CAP_REACHED")
_STALE_REASONS = ("STALE_OPPORTUNITY", "MARKET_SNAPSHOT_NOT_CURRENT", "MARKET_SNAPSHOT_MISSING")
_TOUCHED_OR_OUT_OF_RANGE_REASONS = ("MARKET_BAR_TOUCHED_ENTRY", "MARKET_PRICE_OUT_OF_RANGE")
_COST_REASONS = ("COST_ESTIMATE_MISSING", "COST_ESTIMATE_INVALID", "COST_TOO_HIGH")
_QUALITY_GATE_PREFIX = "COHORT_"
_QUALITY_GATE_EXTRA_REASONS = ("COHORT_EVIDENCE_MISSING",)

_TOP_ROWS_SHOWN = 20

# Bounded read of shadow_alert_decisions for one pilot_id. Exceeding this
# bound fails the whole report closed (see load_decisions/main below) rather
# than silently rendering a truncated report.
MAX_REPORT_DECISIONS = 200_000


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


def load_decisions(
    conn: sqlite3.Connection, pilot_id: str, *, limit: Optional[int] = None
) -> list[sqlite3.Row]:
    """Read-only load of every shadow_alert_decisions row for `pilot_id`,
    LEFT JOINed to opportunity_trade_plan_outcomes for the CURRENT outcome
    state (see module docstring's "at report time" note) — a decision row
    itself is immutable, this join is not. Ordered for determinism.

    Bounded to at most `MAX_REPORT_DECISIONS + 1` rows by default (`limit`
    is read from the module constant at call time, not baked in as a
    default-argument value, so tests can monkeypatch the bound without
    seeding `MAX_REPORT_DECISIONS` real rows) — one row over the bound is
    exactly what `main()` needs to detect and fail closed on overflow
    without ever silently truncating (see main() below).
    """
    effective_limit = limit if limit is not None else MAX_REPORT_DECISIONS + 1
    return conn.execute(
        """
        SELECT d.*, po.state AS current_outcome_state
        FROM shadow_alert_decisions d
        LEFT JOIN opportunity_trade_plan_outcomes po ON po.opportunity_uid = d.opportunity_uid
        WHERE d.pilot_id = ?
        ORDER BY d.run_at ASC, d.opportunity_uid ASC
        LIMIT ?
        """,
        (pilot_id, effective_limit),
    ).fetchall()


def _reason_codes(row: sqlite3.Row) -> tuple[list[str], bool]:
    """Returns `(codes, malformed)`. `codes` is the list of valid string
    reason codes usable for the reason-code breakdown; `malformed` is True
    whenever the stored reason_codes_json fails to parse as JSON, is not a
    list, or contains any non-string element — such a row is never silently
    treated as a valid empty reason list (see build_summary's
    malformed_reason_evidence_count).
    """
    try:
        parsed = json.loads(row["reason_codes_json"])
    except (TypeError, ValueError):
        return [], True
    if not isinstance(parsed, list) or not all(isinstance(c, str) for c in parsed):
        return [], True
    return parsed, False


def build_summary(rows: list[sqlite3.Row]) -> dict[str, Any]:
    """Pure. Aggregates already-loaded decision rows into report counts.
    Never invents a fill, P&L, or recommendation — only counts of what the
    runtime actually recorded (see module docstring).
    """
    total_runs = len({row["run_at"] for row in rows})
    total_decisions = len(rows)
    unique_candidates = len({row["opportunity_uid"] for row in rows})
    would_emit = sum(1 for row in rows if row["would_emit"])
    suppressed = total_decisions - would_emit

    reason_counts: Counter[str] = Counter()
    malformed_reason_evidence_count = 0
    for row in rows:
        codes, malformed = _reason_codes(row)
        if malformed:
            malformed_reason_evidence_count += 1
        reason_counts.update(codes)

    def _grouped(reasons: tuple[str, ...]) -> int:
        return sum(reason_counts.get(r, 0) for r in reasons)

    quality_gate_failure_count = sum(
        count
        for reason, count in reason_counts.items()
        if reason.startswith(_QUALITY_GATE_PREFIX) or reason in _QUALITY_GATE_EXTRA_REASONS
    )

    by_symbol: Counter[str] = Counter(row["symbol"] for row in rows)
    by_direction: Counter[str] = Counter(row["direction"] for row in rows)
    by_family: Counter[str] = Counter(row["setup_family"] for row in rows)
    by_timeframe: Counter[str] = Counter(row["primary_timeframe"] for row in rows)
    by_cohort: Counter[str] = Counter(
        f"{row['setup_family']}:{row['primary_timeframe']}:{row['direction']}" for row in rows
    )
    by_current_outcome_state: Counter[str] = Counter(
        row["current_outcome_state"] if row["current_outcome_state"] is not None else "NO_OUTCOME_ROW"
        for row in rows
    )

    return {
        "total_runs": total_runs,
        "total_decisions": total_decisions,
        "unique_candidates": unique_candidates,
        "would_emit": would_emit,
        "suppressed": suppressed,
        "reason_counts": dict(sorted(reason_counts.items())),
        "malformed_reason_evidence_count": malformed_reason_evidence_count,
        "duplicate_count": _grouped(_DUPLICATE_REASONS),
        "conflict_count": _grouped(_CONFLICT_REASONS),
        "cooldown_count": _grouped(_COOLDOWN_REASONS),
        "cap_count": _grouped(_CAP_REASONS),
        "stale_count": _grouped(_STALE_REASONS),
        "touched_or_out_of_range_count": _grouped(_TOUCHED_OR_OUT_OF_RANGE_REASONS),
        "cost_failure_count": _grouped(_COST_REASONS),
        "quality_gate_failure_count": quality_gate_failure_count,
        "by_symbol": dict(sorted(by_symbol.items(), key=lambda kv: (-kv[1], kv[0]))),
        "by_direction": dict(sorted(by_direction.items())),
        "by_family": dict(sorted(by_family.items())),
        "by_timeframe": dict(sorted(by_timeframe.items())),
        "by_cohort": dict(sorted(by_cohort.items(), key=lambda kv: (-kv[1], kv[0]))),
        "by_current_outcome_state": dict(sorted(by_current_outcome_state.items())),
    }


def render_report(pilot_id: str, summary: dict[str, Any]) -> str:
    """Pure, deterministic text rendering."""
    lines: list[str] = []
    lines.append("=" * 100)
    lines.append("  APEX Shadow Alert Runtime Wiring v0.1 — Decision Report (read-only)")
    lines.append(f"  pilot_id = {pilot_id!r}")
    lines.append("=" * 100)
    lines.append(
        "  Descriptive evidence report only. No fill, P&L, or recommendation is ever computed"
    )
    lines.append(
        "  or shown here — only counts of what apex.opportunity.shadow_runtime actually recorded."
    )
    lines.append("")

    lines.append(f"total_runs={summary['total_runs']}  total_decisions={summary['total_decisions']}  "
                 f"unique_candidates={summary['unique_candidates']}")
    lines.append(f"would_emit={summary['would_emit']}  suppressed={summary['suppressed']}")
    lines.append("")

    lines.append("SUPPRESSION GROUPINGS (a decision may count in more than one group)")
    lines.append(f"  duplicate/already-emitted   = {summary['duplicate_count']}")
    lines.append(f"  conflict/cluster            = {summary['conflict_count']}")
    lines.append(f"  cooldown                    = {summary['cooldown_count']}")
    lines.append(f"  hour/day cap                = {summary['cap_count']}")
    lines.append(f"  stale/missing snapshot      = {summary['stale_count']}")
    lines.append(f"  touched entry/out of range  = {summary['touched_or_out_of_range_count']}")
    lines.append(f"  cost failure                = {summary['cost_failure_count']}")
    lines.append(f"  quality-gate failure        = {summary['quality_gate_failure_count']}")
    lines.append(f"  malformed reason evidence   = {summary['malformed_reason_evidence_count']}")
    lines.append("")

    lines.append("BY EXACT REASON CODE")
    for reason, count in summary["reason_counts"].items():
        lines.append(f"  {reason}: {count}")
    lines.append("")

    lines.append("BY SYMBOL (concentration, descending)")
    for key, count in summary["by_symbol"].items():
        lines.append(f"  {key}: {count}")
    lines.append("")

    lines.append("BY DIRECTION")
    for key, count in summary["by_direction"].items():
        lines.append(f"  {key}: {count}")
    lines.append("")

    lines.append("BY SETUP FAMILY")
    for key, count in summary["by_family"].items():
        lines.append(f"  {key}: {count}")
    lines.append("")

    lines.append("BY TIMEFRAME")
    for key, count in summary["by_timeframe"].items():
        lines.append(f"  {key}: {count}")
    lines.append("")

    lines.append(f"BY EXACT COHORT (setup_family:timeframe:direction, top {_TOP_ROWS_SHOWN})")
    for key, count in list(summary["by_cohort"].items())[:_TOP_ROWS_SHOWN]:
        lines.append(f"  {key}: {count}")
    lines.append("")

    lines.append(
        "BY CURRENT OUTCOME STATE (opportunity_trade_plan_outcomes.state AT REPORT TIME — "
        "may differ from the state when each decision was recorded)"
    )
    for key, count in summary["by_current_outcome_state"].items():
        lines.append(f"  {key}: {count}")
    lines.append("")

    lines.append("LIMITATIONS")
    lines.append("  - This report never shows a fill, an entry/exit price outcome, or a P&L figure.")
    lines.append("  - It never issues a recommendation to trade, size, or act on any decision.")
    lines.append(
        "  - 'BY CURRENT OUTCOME STATE' reflects the LATEST evaluated outcome as of report time, "
        "not the state at decision time."
    )
    lines.append(
        "  - Cost/cohort evidence shown elsewhere in shadow_alert_decisions rows is frozen at "
        "decision time and is not re-derived here."
    )
    lines.append("=" * 100)
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "APEX Shadow Alert Runtime Wiring v0.1 — read-only decision report, never creates or "
            "migrates the database, never sends a notification, never makes a network call"
        )
    )
    parser.add_argument(
        "--pilot-id", metavar="PILOT_ID", default=None,
        help="Pilot id to report on (defaults to the configured SHADOW_ALERT_PILOT_ID)",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    db_path = settings.apex_db_path
    pilot_id = args.pilot_id if args.pilot_id is not None else settings.shadow_alert_pilot_id

    if not pilot_id:
        print(
            "ERROR: no --pilot-id given and SHADOW_ALERT_PILOT_ID is not configured.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not Path(db_path).exists():
        print(
            f"No database file at {db_path!r}; this report never creates one "
            "(run the bot first to initialize it).",
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
            print("  APEX Shadow Alert Runtime Wiring v0.1 — Decision Report (read-only)")
            print("=" * 100)
            print(f"  Missing table(s): {', '.join(missing)}. Nothing to report.")
            print("=" * 100)
            return

        try:
            rows = load_decisions(conn, pilot_id)
        except sqlite3.Error as e:
            print(f"ERROR: could not query shadow alert decision evidence: {e}", file=sys.stderr)
            sys.exit(1)

        if len(rows) > MAX_REPORT_DECISIONS:
            print(
                f"ERROR: pilot_id {pilot_id!r} has more than {MAX_REPORT_DECISIONS} decision rows; "
                "refusing to render a truncated report. Narrow the query or raise the bound instead.",
                file=sys.stderr,
            )
            sys.exit(1)

        summary = build_summary(rows)
        print(render_report(pilot_id, summary))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
