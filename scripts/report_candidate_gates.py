"""Candidate Gate Prospective Tracking Report — Milestone 11C / 11C.1 / 11G.

Reads closed CONFIRMED_SETUP signal_features rows that carry 11C/11G candidate
gate metadata and compares outcome statistics across gate cohorts.

Semantics note (Milestone 11G):
  - Rows tagged `candidate_gate_version=11C_v1` used ANY semantics:
    `candidate_gate_passed=True` when at least one gate passed.
  - Rows tagged `candidate_gate_version=11G_v1` use ALL semantics:
    `candidate_gate_passed=True` only when every gate passed. A
    `candidate_gate_any_passed` field preserves the legacy ANY value.
  - The interpretation section below reports both ALL and ANY counts so
    11C and 11G cohorts can be compared on equivalent terms.

REPORT-ONLY. Does not write to the database. Does not change signal
generation, alerts, config, or runtime behavior.

This report only becomes meaningful after enough new observations have
accumulated with feature_version = '11C_v1'. Until then, cohort sample
sizes will be small and results should be treated as INSUFFICIENT.

Avg outcome R note (11C.1):
  Expired observations typically have outcome_r = NULL and are therefore
  excluded from Avg outcome R. The report shows coverage as "n=X / N rows"
  so you can see how many rows contributed. Do not treat Avg outcome R as a
  full-population average unless coverage is near 100%.

Direction-concentration note (11C.1):
  If a gate cohort selects predominantly one direction (>=90% LONG or SHORT),
  the report emits a warning. This is important when comparing gate cohorts:
  direction-concentrated cohorts reflect a direction/regime effect, not a
  generally valid gate result.

Usage (run from repo root):
    PYTHONPATH=src python scripts/report_candidate_gates.py
    PYTHONPATH=src python scripts/report_candidate_gates.py --since 2026-06-01T00:00:00Z
    PYTHONPATH=src python scripts/report_candidate_gates.py --min-n 10
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

# -----------------------------------------------------------------------
# sys.path bootstrap — same pattern as other APEX report scripts.
# -----------------------------------------------------------------------
_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent
_SRC_DIR = _REPO_ROOT / "src"
for _p in (str(_REPO_ROOT), str(_SRC_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from apex.config import get_settings
from apex.db.connection import close_db, init_db
from apex.strategy.candidate_gates import (
    GATE_DEFINITIONS,
    GATE_VERSION,
    LEGACY_GATE_VERSION_ANY,
)
from scripts.report_signal_learning import _outcome_stats, _pct

# Gate versions the report knows how to interpret.
# 11C_v1 used `any` semantics for candidate_gate_passed.
# 11G_v1 uses strict `all` semantics and adds candidate_gate_any_passed,
# research_captured, alert_eligible.
_SUPPORTED_GATE_VERSIONS = (LEGACY_GATE_VERSION_ANY, GATE_VERSION)

_DEFAULT_MIN_N = 10

# Threshold above which we flag a cohort as direction-concentrated.
_DIR_CONCENTRATION_PCT = 90.0

# Threshold below which we flag outcome_r coverage as materially partial.
_COVERAGE_WARN_PCT = 80.0

# Cohort definitions: (display_name, gate_key_or_None)
# gate_key_or_None: if None → include all rows with gate version (baseline)
#                   if str  → include rows where that gate passed
_COHORTS = [
    ("BASELINE_ALL_WITH_GATE_VERSION",   None),
    ("GATE_A_ATR_0_10_TO_1_00",          "GATE_A_ATR_0_10_TO_1_00"),
    ("GATE_B_EMA_SPREAD_NEG_0_25_TO_0",  "GATE_B_EMA_SPREAD_NEG_0_25_TO_0"),
    ("GATE_C_RSI_50_TO_60",              "GATE_C_RSI_50_TO_60"),
    ("GATE_D_ATR_AND_EMA_COMBINED",      "GATE_D_ATR_AND_EMA_COMBINED"),
]


# ======================================================================
# Helpers
# ======================================================================

def _parse_gate_meta(row: Any) -> Optional[dict]:
    """Parse metadata_json from a signal_features row. Returns None on error
    or when the row does not carry a supported candidate_gate_version."""
    raw = row["metadata_json"]
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or "gates" not in parsed:
            return None
        if parsed.get("candidate_gate_version") not in _SUPPORTED_GATE_VERSIONS:
            return None
        return parsed
    except (json.JSONDecodeError, TypeError):
        return None


def _all_gates_passed(meta: Optional[dict]) -> bool:
    """11G ALL semantics — every gate in the metadata passed.

    Works for both 11C_v1 and 11G_v1 rows: for 11G_v1 it just reads
    `candidate_gate_passed`; for 11C_v1 it derives it from the gate map
    so the two cohorts are directly comparable.
    """
    if meta is None:
        return False
    version = meta.get("candidate_gate_version")
    if version == GATE_VERSION:
        return bool(meta.get("candidate_gate_passed", False))
    # Legacy 11C_v1 row — derive ALL by walking the gate map.
    gates = meta.get("gates", {})
    if not gates:
        return False
    return all(bool(g.get("passed", False)) for g in gates.values())


def _any_gate_passed(meta: Optional[dict]) -> bool:
    """Legacy 11C ANY semantics — at least one gate passed.

    For 11G_v1 rows reads `candidate_gate_any_passed`; for 11C_v1 rows
    reads the (then equivalent) `candidate_gate_passed`.
    """
    if meta is None:
        return False
    version = meta.get("candidate_gate_version")
    if version == GATE_VERSION:
        return bool(meta.get("candidate_gate_any_passed", False))
    return bool(meta.get("candidate_gate_passed", False))


def _alert_eligible(meta: Optional[dict]) -> Optional[bool]:
    """Return the 11G `alert_eligible` flag if present, else None.

    None means the row predates 11G (no alert_eligible was recorded).
    """
    if meta is None:
        return None
    if "alert_eligible" not in meta:
        return None
    return bool(meta["alert_eligible"])


def _gate_passed(meta: Optional[dict], gate_key: str) -> bool:
    """Return True if the given gate passed in this row's metadata."""
    if meta is None:
        return False
    gates = meta.get("gates", {})
    gate_data = gates.get(gate_key)
    if gate_data is None:
        return False
    return bool(gate_data.get("passed", False))


def _avg_r_num(vals: list[float]) -> Optional[float]:
    return sum(vals) / len(vals) if vals else None


def _direction_pcts(rows: list[Any]) -> tuple[int, int, float, float]:
    """Return (long_n, short_n, long_pct, short_pct)."""
    n = len(rows)
    long_n = sum(1 for r in rows if r["direction"] == "LONG")
    short_n = n - long_n
    long_pct = long_n / n * 100 if n else 0.0
    short_pct = short_n / n * 100 if n else 0.0
    return long_n, short_n, long_pct, short_pct


def _is_direction_concentrated(long_pct: float, short_pct: float) -> bool:
    return long_pct >= _DIR_CONCENTRATION_PCT or short_pct >= _DIR_CONCENTRATION_PCT


def _outcome_r_coverage(st: dict, n: int) -> tuple[int, int]:
    """Return (r_n, r_excluded) — rows with non-null outcome_r, rows excluded."""
    r_n = len(st["out_r_vals"])
    r_excluded = n - r_n
    return r_n, r_excluded


def _print_cohort(label: str, rows: list[Any], min_n: int) -> None:
    """Print outcome statistics for a single cohort — Milestone 11C.1 format."""
    st = _outcome_stats(rows)
    n = st["n"]
    long_n, short_n, long_pct, short_pct = _direction_pcts(rows)
    exp_n = st["expired"]
    r_n, r_excluded = _outcome_r_coverage(st, n)

    warn = ""
    if n == 0:
        warn = "  [NO DATA]"
    elif n < min_n:
        warn = f"  [INSUFFICIENT SAMPLE — n={n} < min_n={min_n}]"

    print(f"  {label}:{warn}")
    if n == 0:
        print(f"    No closed rows in this cohort.")
        print()
        return

    # Direction concentration inline warning
    if _is_direction_concentrated(long_pct, short_pct):
        dominant = "LONG" if long_pct >= _DIR_CONCENTRATION_PCT else "SHORT"
        print(f"    [DIR-CONCENTRATED: {long_n} LONG / {short_n} SHORT — "
              f"results reflect {dominant} regime, not a general gate]")

    print(f"    Rows           : {n}")
    print(f"    TP1            : {st['tp1']:4d}  {_pct(st['tp1'], n)}")
    print(f"    HIT_2R         : {st['tp2']:4d}  {_pct(st['tp2'], n)}")
    print(f"    STOPPED        : {st['stopped']:4d}  {_pct(st['stopped'], n)}")
    print(f"    EXPIRED        : {st['expired']:4d}  {_pct(st['expired'], n)}")
    if exp_n > 0:
        print(f"      after TP1    : {st['exp_after_tp1']:4d}  "
              f"{_pct(st['exp_after_tp1'], exp_n)} of expired")
        print(f"      w/o TP1      : {st['exp_no_tp1']:4d}  "
              f"{_pct(st['exp_no_tp1'], exp_n)} of expired")

    mfe_n = len(st["mfe_vals"])
    mae_n = len(st["mae_vals"])
    avg_mfe_s = f"{_avg_r_num(st['mfe_vals']):+.2f}" if st["mfe_vals"] else "n/a"
    avg_mae_s = f"{_avg_r_num(st['mae_vals']):+.2f}" if st["mae_vals"] else "n/a"
    avg_r_s = f"{_avg_r_num(st['out_r_vals']):+.3f}" if st["out_r_vals"] else "n/a"

    print(f"    Avg MFE        : {avg_mfe_s} R  (n={mfe_n})")
    print(f"    Avg MAE        : {avg_mae_s} R  (n={mae_n})")
    print(f"    Avg outcome R  : {avg_r_s} R  (n={r_n} / {n})")
    print(f"    Outcome R cov  : {r_n} / {n} rows; "
          f"{r_excluded} excluded (outcome_r is null)")
    if r_excluded > 0 and n > 0:
        coverage_pct = r_n / n * 100
        if coverage_pct < _COVERAGE_WARN_PCT:
            print(f"    [PARTIAL COVERAGE: {coverage_pct:.0f}% — "
                  f"compare TP1/STOP/EXPIRED and MFE/MAE alongside Avg outcome R]")
    print(f"    LONG/SHORT     : {long_n} / {short_n}")
    print()


# ======================================================================
# Main
# ======================================================================

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="APEX Candidate Gate Prospective Tracking Report [Milestone 11C] — report-only"
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
        help=f"Minimum closed rows for a non-INSUFFICIENT cohort (default: {_DEFAULT_MIN_N})",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    conn = init_db(settings.apex_db_path)

    # The 11G semantics version lives inside metadata_json
    # (candidate_gate_version), not in the SQL feature_version column. Filter
    # by metadata_json presence and rely on _parse_gate_meta below to drop rows
    # that don't carry a recognised candidate_gate_version.
    try:
        conditions = ["sf.metadata_json IS NOT NULL"]
        params: list[Any] = []
        if args.since:
            conditions.append("sf.captured_at >= ?")
            params.append(args.since)
        where = "WHERE " + " AND ".join(conditions)
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

    # Restrict to rows that carry recognised candidate gate metadata.
    rows_with_gate_meta = [r for r in rows if _parse_gate_meta(r) is not None]
    confirmed = [
        r for r in rows_with_gate_meta if r["signal_type"] == "CONFIRMED_SETUP"
    ]
    closed = [r for r in confirmed if r["outcome_status"] not in (None, "OBSERVED")]

    # Split by gate version so we can report 11C and 11G cohorts side by side.
    n_11c = sum(
        1 for r in closed
        if (m := _parse_gate_meta(r)) is not None
        and m.get("candidate_gate_version") == LEGACY_GATE_VERSION_ANY
    )
    n_11g = sum(
        1 for r in closed
        if (m := _parse_gate_meta(r)) is not None
        and m.get("candidate_gate_version") == GATE_VERSION
    )

    # ------------------------------------------------------------------
    # Section 1: Header and disclaimer
    # ------------------------------------------------------------------
    print()
    print("=" * 68)
    print("  APEX Candidate Gate Prospective Tracking Report  [Milestone 11C/11G]")
    print("=" * 68)
    print("  REPORT-ONLY. Does not change scanning, alerts, or config.")
    print("  Candidate gates are DRY-RUN RESEARCH ONLY.")
    print("  Do not use these results to enable any runtime filter.")
    print("  A dedicated approval milestone is required before any gate is")
    print("  applied to live or dry-run signal scanning.")
    print()
    print(f"  Active gate version    : {GATE_VERSION} (strict ALL semantics)")
    print(f"  Legacy gate version    : {LEGACY_GATE_VERSION_ANY} (ANY semantics)")
    if args.since:
        print(f"  Filtered since         : {args.since}")
    print(f"  Rows w/ gate metadata  : {len(rows_with_gate_meta)}")
    print(f"  Closed confirmed       : {len(closed)}")
    print(f"    of which 11C_v1      : {n_11c}")
    print(f"    of which 11G_v1      : {n_11g}")
    print(f"  Min-N threshold        : {args.min_n}")

    if len(rows_with_gate_meta) == 0:
        print()
        print("  No candidate gate observations found yet.")
        print("  Results will appear once new observations are captured with")
        print(f"  candidate_gate_version in {list(_SUPPORTED_GATE_VERSIONS)}.")
        print()
        print("=" * 68)
        return

    if len(closed) == 0:
        print()
        print("  No closed CONFIRMED_SETUP rows with gate version yet.")
        print("  Results will appear once observations close.")
        print()
        print("=" * 68)
        return

    # ------------------------------------------------------------------
    # Section 2: Gate definitions
    # ------------------------------------------------------------------
    print()
    print("=" * 68)
    print("  Candidate gate definitions")
    print("=" * 68)
    print()
    for gate_key, rule in GATE_DEFINITIONS.items():
        print(f"  {gate_key:<44}  {rule}")

    # ------------------------------------------------------------------
    # Section 3: Per-cohort outcome statistics
    # ------------------------------------------------------------------
    print()
    print("=" * 68)
    print("  Cohort outcome statistics")
    print("=" * 68)
    print("  Only closed CONFIRMED_SETUP rows with gate version are included.")
    print("  Open / OBSERVED rows are excluded (outcomes not yet known).")
    print()
    print("  NOTE on Avg outcome R:")
    print("  Expired observations typically have outcome_r = NULL and are")
    print("  excluded from Avg outcome R. Coverage shows how many rows")
    print("  contributed. Do not read Avg outcome R as a full-population")
    print("  average when coverage is low.")
    print()
    print("  NOTE on expired-after-TP1 vs expired-without-TP1:")
    print("  'after TP1'  = reached 1R milestone but did not hit 2R before expiry.")
    print("                 Partial success — entry was correct but exit timing failed.")
    print("  'w/o TP1'    = did not reach 1R before expiry.")
    print("                 Full miss for the observation window.")
    print()

    # Pre-parse all metadata to avoid repeated JSON parsing
    row_metas = [_parse_gate_meta(r) for r in closed]

    cohort_rows: dict[str, list] = {}
    for cohort_name, gate_key in _COHORTS:
        if gate_key is None:
            cohort_rows[cohort_name] = list(closed)
        else:
            cohort_rows[cohort_name] = [
                r for r, meta in zip(closed, row_metas)
                if _gate_passed(meta, gate_key)
            ]

    for cohort_name, gate_key in _COHORTS:
        _print_cohort(cohort_name, cohort_rows[cohort_name], args.min_n)

    # ------------------------------------------------------------------
    # Section 4: Summary comparison table
    # ------------------------------------------------------------------
    print("=" * 68)
    print("  Summary comparison (vs baseline)")
    print("=" * 68)
    print()
    print("  Columns: N=cohort rows, TP1%/Δ, STP%/Δ, AvgOutR=Avg outcome R,")
    print("  R_n=rows contributing to AvgOutR, L/S=LONG count / SHORT count.")
    print("  [DC] = direction-concentrated (>=90% one direction).")
    print()

    baseline_rows = cohort_rows["BASELINE_ALL_WITH_GATE_VERSION"]
    baseline_st = _outcome_stats(baseline_rows)
    b_n = baseline_st["n"]
    b_tp1_r = baseline_st["tp1"] / b_n if b_n else 0.0
    b_stop_r = baseline_st["stopped"] / b_n if b_n else 0.0

    def _delta_pp(kept_count: int, kept_n: int, base_rate: float) -> str:
        if kept_n == 0:
            return "  n/a"
        return f"{(kept_count / kept_n - base_rate) * 100:+.1f}pp"

    hdr = (
        f"  {'Cohort':<43} {'N':>4} "
        f"{'TP1%':>5} {'TP1Δ':>7} "
        f"{'STP%':>5} {'STPΔ':>7} "
        f"{'AvgOutR':>8} {'R_n':>8}  {'L/S'}"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for cohort_name, gate_key in _COHORTS:
        cr = cohort_rows[cohort_name]
        st = _outcome_stats(cr)
        n = st["n"]
        tp1_pct = st["tp1"] / n * 100 if n else 0.0
        stop_pct = st["stopped"] / n * 100 if n else 0.0
        avg_r = _avg_r_num(st["out_r_vals"])
        r_n = len(st["out_r_vals"])
        d_tp1 = _delta_pp(st["tp1"], n, b_tp1_r)
        d_stp = _delta_pp(st["stopped"], n, b_stop_r)
        avg_r_s = f"{avg_r:+.3f}" if avg_r is not None else "    n/a"
        long_n, short_n, long_pct, short_pct = _direction_pcts(cr)
        dc_flag = " [DC]" if _is_direction_concentrated(long_pct, short_pct) else ""
        is_baseline = gate_key is None
        suffix = " ← baseline" if is_baseline else ""
        r_n_s = f"{r_n}/{n}"
        ls_s = f"{long_n}/{short_n}"
        print(
            f"  {(cohort_name[:42] + dc_flag)[:43]:<43} {n:>4} "
            f"{tp1_pct:>4.1f}% {d_tp1:>7} "
            f"{stop_pct:>4.1f}% {d_stp:>7} "
            f"{avg_r_s:>8} {r_n_s:>8}  {ls_s}{suffix}"
        )

    # ------------------------------------------------------------------
    # Section 5: Interpretation
    # ------------------------------------------------------------------
    print()
    print("=" * 68)
    print("  Interpretation")
    print("=" * 68)
    print()

    total_with_meta = sum(1 for m in row_metas if m is not None)
    total_without_meta = len(closed) - total_with_meta

    print(
        f"  {len(closed)} closed rows with candidate gate metadata "
        f"({n_11c} on 11C_v1 + {n_11g} on 11G_v1)."
    )
    if total_without_meta > 0:
        print(f"  {total_without_meta} row(s) missing gate metadata (None metadata_json).")

    # 11G ALL semantics (strict): every gate passes.
    all_pass_n = sum(1 for m in row_metas if _all_gates_passed(m))
    # 11C-equivalent ANY semantics: at least one gate passes.
    any_pass_n = sum(1 for m in row_metas if _any_gate_passed(m))
    print(
        f"  {all_pass_n} row(s) pass ALL gates strictly "
        f"({_pct(all_pass_n, len(closed))}) — 11G `candidate_gate_passed`."
    )
    print(
        f"  {any_pass_n} row(s) pass at least ONE gate "
        f"({_pct(any_pass_n, len(closed))}) — legacy ANY semantics."
    )

    # 11G alert-eligibility cohort: rows that would have produced a Pushover
    # send if ALERTS_ENABLED were true (engine-clean + alert_type enabled).
    # Only meaningful for 11G_v1 rows that recorded this field.
    elig_present = [m for m in row_metas if _alert_eligible(m) is not None]
    if elig_present:
        elig_true = sum(1 for m in elig_present if _alert_eligible(m))
        print(
            f"  {elig_true} row(s) alert_eligible=true of {len(elig_present)} "
            f"with the flag ({_pct(elig_true, len(elig_present))}) — "
            f"would-be Pushover sends if ALERTS_ENABLED=true."
        )
    print()

    # Outcome R coverage note
    baseline_st2 = _outcome_stats(baseline_rows)
    b_r_n, b_r_excl = _outcome_r_coverage(baseline_st2, len(baseline_rows))
    print("  Avg outcome R — coverage note:")
    print(f"  Baseline: {b_r_n} / {len(baseline_rows)} rows have non-null outcome_r.")
    if b_r_excl > 0:
        print(f"  {b_r_excl} baseline rows are excluded from Avg outcome R because")
        print("  outcome_r is null (typically EXPIRED rows).")
        b_cov_pct = b_r_n / len(baseline_rows) * 100 if baseline_rows else 0.0
        if b_cov_pct < _COVERAGE_WARN_PCT:
            print(f"  Coverage is {b_cov_pct:.0f}% — Avg outcome R reflects only STOPPED and")
            print("  HIT_2R rows. Compare TP1/STOP/EXPIRED rates and MFE/MAE alongside it.")
    print()

    # Direction-concentration warnings
    dir_warnings = []
    for cohort_name, gate_key in _COHORTS[1:]:  # skip baseline
        cr = cohort_rows[cohort_name]
        if not cr:
            continue
        long_n, short_n, long_pct, short_pct = _direction_pcts(cr)
        if _is_direction_concentrated(long_pct, short_pct):
            dominant = "LONG" if long_pct >= _DIR_CONCENTRATION_PCT else "SHORT"
            dir_warnings.append((cohort_name, long_n, short_n, dominant))

    if dir_warnings:
        print("  Direction-concentration warnings:")
        for cohort_name, long_n, short_n, dominant in dir_warnings:
            print(f"  {cohort_name}:")
            print(f"    {long_n} LONG / {short_n} SHORT — {dominant} only in this window.")
            print(f"    Treat results as {dominant}-regime evidence, not a general gate result.")
            print(f"    The gate's apparent performance may reflect direction/regime bias,")
            print(f"    not a broadly applicable signal improvement.")
        print()

    # Partial-success explanation
    print("  Expired-after-TP1 vs expired-without-TP1:")
    print("  'Expired after TP1' means the signal reached the 1R milestone but")
    print("  the trade did not hit 2R before the observation window expired.")
    print("  This is a partial success: entry direction was correct but exit")
    print("  timing or target spacing may need adjustment.")
    print("  'Expired without TP1' means the signal never reached 1R before")
    print("  expiry — a full miss within the observation window.")
    print("  These are qualitatively different outcomes and should not be")
    print("  collapsed into a single 'expired is bad' reading.")
    print()

    # Sample size summary
    for cohort_name, gate_key in _COHORTS[1:]:
        cr = cohort_rows[cohort_name]
        n = len(cr)
        if n < args.min_n:
            print(f"  {cohort_name}: n={n} — INSUFFICIENT SAMPLE (need >= {args.min_n}).")
        else:
            print(f"  {cohort_name}: n={n} — sufficient for initial comparison.")
    print()

    print("  IMPORTANT: These results are prospective dry-run tracking only.")
    print("  Do not draw conclusions from < 20 rows per cohort.")
    print("  A consistent improvement over 50+ rows is required before any")
    print("  gate is promoted to a runtime filter (requires explicit milestone).")
    print()
    print("=" * 68)


if __name__ == "__main__":
    main()
