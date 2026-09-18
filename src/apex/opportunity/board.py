"""Live Opportunity Board v0.2 — local, development-only, read-only research
UI/API over existing ranked opportunity observations, immutable trade plans,
and prospective outcome evidence. v0.2 is a phone-first, server-rendered
HTML redesign only: the joined query, `_project_row` projection, JSON
response schema/values, eligibility/freshness semantics, and route/filter
behavior are unchanged from v0.1 (see each function's own docstring).

Safety boundary
----------------
This module never writes to any table, never sizes a position, never calls
alerting/notification/trading/execution code, and never opens or initializes
a database connection itself — it only reads the already-initialized global
connection (via `apex.db.connection.get_connection`) at request time, or uses
whatever connection was injected into `create_board_router`. Mounting is
entirely controlled by `apex.app.create_app` (see its
`live_opportunity_board_enabled` + `apex_env == "development"` gate); this
module has no mount logic of its own and no awareness of production.

Only two routes exist, both GET: `/opportunities` (HTML) and
`/opportunities/api` (JSON). Every response is a deliberately bounded,
safely projected view (see `_project_row`) — raw evidence/measurement/
context/component-score/provenance/decisive-OHLC/crossed-level JSON,
credentials, environment values, private paths, SQL, and stack traces are
never included. The context/research score is explicitly labeled as NOT a
probability, and `REVIEW_ELIGIBLE`/`NOT_ELIGIBLE` future-alert-review
eligibility (see `_compute_eligibility`) is a pure, deterministic read-time
classification — it never calls alerts, Pushover, email, trading, execution,
risk sizing, or any exchange client, and it never hides a row.
"""
from __future__ import annotations

import html
import json
import logging
import math
import sqlite3
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, Response

from apex.db import repository as repo
from apex.opportunity.trade_plan_outcome import TERMINAL_OUTCOME_STATES

logger = logging.getLogger(__name__)

# Bounded, deliberately duplicated (not imported) from
# apex.opportunity.engine.OPPORTUNITY_EXPIRY_MINUTES — the same existing
# per-timeframe reconfirmation lifecycle window, kept here purely as a local
# constant so this read-only board module never has to import the engine
# (and its detector/candle-store dependency chain) just to read two numbers.
# Keep in sync with engine.py's OPPORTUNITY_EXPIRY_MINUTES if that changes.
FRESHNESS_WINDOW_MINUTES: dict[str, int] = {
    "3m": 15,
    "5m": 25,
}

MAX_LIMIT = 200
DEFAULT_LIMIT = 100

_MAX_WARNING_ITEMS = 20
_MAX_WARNING_ITEM_LEN = 200

StatusParam = Literal["ACTIVE", "EXPIRED", "ALL"]
DirectionParam = Literal["LONG", "SHORT"]
TimeframeParam = Literal["3m", "5m"]
SetupFamilyParam = Literal[
    "VOLATILITY_COMPRESSION",
    "SWEEP_RECLAIM",
    "SUPPORT_RESISTANCE_REJECTION",
    "SUPPORT_RESISTANCE_BREAKOUT_RETEST",
    "SUPPORT_RESISTANCE_FAILED_BREAKOUT",
]

# Deterministic REVIEW_ELIGIBLE / NOT_ELIGIBLE reason codes (see
# _compute_eligibility). Every failed predicate is reported — never a
# suppressed/short-circuited subset — and no numeric score threshold is ever
# part of this evaluation.
ELIGIBILITY_REVIEW_ELIGIBLE = "REVIEW_ELIGIBLE"
ELIGIBILITY_NOT_ELIGIBLE = "NOT_ELIGIBLE"

REASON_STATUS_NOT_ACTIVE = "STATUS_NOT_ACTIVE"
REASON_NOT_RESEARCH_ONLY = "NOT_RESEARCH_ONLY"
REASON_NOT_FRESH = "NOT_FRESH"
REASON_SCORE_NOT_CURRENT_COMPATIBLE = "SCORE_NOT_CURRENT_COMPATIBLE"
REASON_PLAN_NOT_AVAILABLE = "PLAN_NOT_AVAILABLE"
REASON_OUTCOME_NOT_PRESENT = "OUTCOME_NOT_PRESENT"
REASON_OUTCOME_TERMINAL = "OUTCOME_TERMINAL"
REASON_OUTCOME_STATE_NOT_PENDING_ENTRY = "OUTCOME_STATE_NOT_PENDING_ENTRY"

_RESEARCH_ONLY_NOTICE = (
    "RESEARCH ONLY — NOT A RECOMMENDATION. This board is a read-only research "
    "view of existing detector/scoring/trade-plan/outcome records. The "
    "context-alignment score is NOT a probability, confidence rating, or "
    "trade signal. Nothing here places, sizes, or recommends a trade."
)

_GENERIC_ERROR_MESSAGE = "Live Opportunity Board data is temporarily unavailable."


# ------------------------------------------------------------------ pure helpers

def _finite_or_none(value: Any) -> Optional[float]:
    """Normalize a stored numeric value for standards-safe JSON: None for
    anything that is not a genuine finite real number (None, bool, non-
    numeric, NaN, or +/-inf) — never a raw non-finite value.
    """
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    fv = float(value)
    return fv if math.isfinite(fv) else None


def _parse_iso_utc(value: Any) -> Optional[datetime]:
    """Parse an exact `%Y-%m-%dT%H:%M:%SZ` UTC timestamp, or None for
    anything else (missing, wrong type, or malformed) — never raises.
    """
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc)


def _compute_freshness(primary_timeframe: Any, last_seen_at: Any, as_of: datetime) -> dict:
    """FRESH/STALE/UNKNOWN as of `as_of` (request UTC time), using the fixed
    per-timeframe lifecycle windows in FRESHNESS_WINDOW_MINUTES. An unknown
    timeframe, an unparseable last_seen_at, or a last_seen_at that is not
    actually in the past relative to as_of is always UNKNOWN — never FRESH.
    """
    as_of_iso = as_of.strftime("%Y-%m-%dT%H:%M:%SZ")
    threshold_minutes = FRESHNESS_WINDOW_MINUTES.get(primary_timeframe)
    if threshold_minutes is None:
        return {
            "state": "UNKNOWN",
            "age_seconds": None,
            "stale_threshold_minutes": None,
            "as_of": as_of_iso,
        }

    last_seen_dt = _parse_iso_utc(last_seen_at)
    if last_seen_dt is None:
        return {
            "state": "UNKNOWN",
            "age_seconds": None,
            "stale_threshold_minutes": threshold_minutes,
            "as_of": as_of_iso,
        }

    age_seconds = (as_of - last_seen_dt).total_seconds()
    if not math.isfinite(age_seconds) or age_seconds < 0:
        return {
            "state": "UNKNOWN",
            "age_seconds": None,
            "stale_threshold_minutes": threshold_minutes,
            "as_of": as_of_iso,
        }

    state = "FRESH" if age_seconds <= threshold_minutes * 60 else "STALE"
    return {
        "state": state,
        "age_seconds": round(age_seconds, 1),
        "stale_threshold_minutes": threshold_minutes,
        "as_of": as_of_iso,
    }


def _parse_bounded_warnings(raw_json: Optional[str]) -> list[str]:
    """Fail-closed parse of a warnings JSON column: only a JSON list of
    bounded, non-nested scalar strings is ever accepted. Any malformed JSON,
    non-list top level, oversized list, non-string/boolean/nested item, or
    over-length string yields an empty list — nothing raw is ever returned
    on malformed input.
    """
    if not raw_json:
        return []
    try:
        parsed = json.loads(raw_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list) or len(parsed) > _MAX_WARNING_ITEMS:
        return []
    result: list[str] = []
    for item in parsed:
        if not isinstance(item, str) or isinstance(item, bool) or len(item) > _MAX_WARNING_ITEM_LEN:
            return []
        result.append(item)
    return result


def _compute_eligibility(
    *,
    status: Any,
    research_only: bool,
    freshness_state: str,
    score_current_compatible: bool,
    plan_availability: Optional[str],
    outcome_present: bool,
    outcome_state: Optional[str],
) -> tuple[str, list[str]]:
    """Pure, deterministic future-alert review eligibility. REVIEW_ELIGIBLE
    requires every condition below to hold simultaneously; otherwise
    NOT_ELIGIBLE is returned together with every failed predicate's reason
    code (never a short-circuited subset, never a numeric score threshold).
    Never calls alerts, Pushover, email, trading, execution, risk sizing, or
    any exchange client — purely arithmetic/string comparisons over already-
    read row values.
    """
    reasons: list[str] = []
    if status != "ACTIVE":
        reasons.append(REASON_STATUS_NOT_ACTIVE)
    if not research_only:
        reasons.append(REASON_NOT_RESEARCH_ONLY)
    if freshness_state != "FRESH":
        reasons.append(REASON_NOT_FRESH)
    if not score_current_compatible:
        reasons.append(REASON_SCORE_NOT_CURRENT_COMPATIBLE)
    if plan_availability != "AVAILABLE":
        reasons.append(REASON_PLAN_NOT_AVAILABLE)
    if not outcome_present:
        reasons.append(REASON_OUTCOME_NOT_PRESENT)
    else:
        if outcome_state in TERMINAL_OUTCOME_STATES:
            reasons.append(REASON_OUTCOME_TERMINAL)
        if outcome_state != "PENDING_ENTRY":
            reasons.append(REASON_OUTCOME_STATE_NOT_PENDING_ENTRY)

    state = ELIGIBILITY_REVIEW_ELIGIBLE if not reasons else ELIGIBILITY_NOT_ELIGIBLE
    return state, reasons


def _project_row(row: sqlite3.Row, *, as_of: datetime) -> dict:
    """The single safe, bounded row projection shared by the HTML and JSON
    views (see module docstring) — deliberately excludes every raw evidence/
    measurement/context/component-score/provenance/decisive-OHLC/crossed-
    level JSON field, credential, environment value, private path, SQL
    fragment, stack trace, and any account/sizing/quantity/leverage/margin/
    order/notification field.
    """
    keys = row.keys()
    plan_present = "plan_plan_uid" in keys and row["plan_plan_uid"] is not None
    outcome_present = "outcome_state" in keys and row["outcome_state"] is not None

    freshness = _compute_freshness(row["primary_timeframe"], row["last_seen_at"], as_of)
    score_current_compatible = bool(row["rank_group"] == 0)
    outcome_state = row["outcome_state"] if outcome_present else None

    eligibility_state, eligibility_reasons = _compute_eligibility(
        status=row["status"],
        research_only=bool(row["research_only"]),
        freshness_state=freshness["state"],
        score_current_compatible=score_current_compatible,
        plan_availability=row["plan_availability"] if plan_present else None,
        outcome_present=outcome_present,
        outcome_state=outcome_state,
    )

    return {
        "opportunity_uid": row["opportunity_uid"],
        "symbol": row["symbol"],
        "direction": row["direction"],
        "setup_family": row["setup_family"],
        "primary_timeframe": row["primary_timeframe"],
        "status": row["status"],
        "research_only": bool(row["research_only"]),
        "first_detected_at": row["first_detected_at"],
        "last_seen_at": row["last_seen_at"],
        "occurrence_count": row["occurrence_count"],
        "source_candle_open_time": row["source_candle_open_time"],
        "source_candle_close_time": row["source_candle_close_time"],
        "score_value": _finite_or_none(row["total_score"]),
        "score_version": row["score_version"],
        "score_current_compatible": score_current_compatible,
        "score_warnings": _parse_bounded_warnings(row["score_warnings_json"]),
        "freshness_state": freshness["state"],
        "freshness_age_seconds": freshness["age_seconds"],
        "freshness_stale_threshold_minutes": freshness["stale_threshold_minutes"],
        "freshness_as_of": freshness["as_of"],
        "plan_present": plan_present,
        "plan_availability": row["plan_availability"] if plan_present else None,
        "plan_unavailable_reason": row["plan_unavailable_reason"] if plan_present else None,
        "plan_entry_type": row["plan_entry_type"] if plan_present else None,
        "plan_entry_price": _finite_or_none(row["plan_entry_price"]) if plan_present else None,
        "plan_invalidation_price": (
            _finite_or_none(row["plan_invalidation_price"]) if plan_present else None
        ),
        "plan_stop_price": _finite_or_none(row["plan_stop_price"]) if plan_present else None,
        "plan_target_1r_price": (
            _finite_or_none(row["plan_target_1r_price"]) if plan_present else None
        ),
        "plan_target_2r_price": (
            _finite_or_none(row["plan_target_2r_price"]) if plan_present else None
        ),
        "plan_reward_risk_1r": (
            _finite_or_none(row["plan_reward_risk_1r"]) if plan_present else None
        ),
        "plan_reward_risk_2r": (
            _finite_or_none(row["plan_reward_risk_2r"]) if plan_present else None
        ),
        "plan_evaluation_not_before_ms": (
            row["plan_evaluation_not_before_ms"] if plan_present else None
        ),
        "plan_evaluation_expiry_ms": row["plan_evaluation_expiry_ms"] if plan_present else None,
        "plan_contract_version": row["plan_contract_version"] if plan_present else None,
        "outcome_present": outcome_present,
        "outcome_state": outcome_state,
        "outcome_is_terminal": (
            (outcome_state in TERMINAL_OUTCOME_STATES) if outcome_present else None
        ),
        "outcome_last_evaluated_ms": (
            row["outcome_last_evaluated_ms"] if outcome_present else None
        ),
        "outcome_data_quality": row["outcome_data_quality"] if outcome_present else None,
        "outcome_is_ambiguous": (
            bool(row["outcome_is_ambiguous"]) if outcome_present else None
        ),
        "outcome_mfe_r": _finite_or_none(row["outcome_mfe_r"]) if outcome_present else None,
        "outcome_mae_r": _finite_or_none(row["outcome_mae_r"]) if outcome_present else None,
        "opportunity_contract_version": row["contract_version"],
        "detector_version": row["detector_version"],
        "outcome_contract_version": row["outcome_contract_version"] if outcome_present else None,
        "eligibility_state": eligibility_state,
        "eligibility_reasons": eligibility_reasons,
    }


# ------------------------------------------------------------------ HTML rendering

def _esc(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "true" if value else "false"
    return html.escape(str(value), quote=True)


def _esc_list(values: list[str]) -> str:
    if not values:
        return "—"
    return ", ".join(html.escape(str(v), quote=True) for v in values)


_PAGE_HEAD = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>APEX — Mobile Opportunity Board v0.2 (research only)</title>
  <style>
    * { box-sizing: border-box; }
    html, body { overflow-x: hidden; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           background: #0f1220; color: #f0f0f6; margin: 0; padding: 0 0 2.5rem 0;
           font-size: 16px; line-height: 1.5; }
    header { background: #1a1a2e; padding: 0.7rem 0.9rem; }
    header .title { margin: 0 0 0.35rem 0; font-size: 0.95rem; font-weight: 700; color: #ffffff; }
    header .notice { margin: 0; font-size: 0.82rem; line-height: 1.5; color: #ffd15c; }
    .meta { font-size: 0.78rem; color: #a9a9c0; padding: 0.6rem 0.9rem 0 0.9rem; word-break: break-word; }
    .summary { display: flex; flex-wrap: wrap; gap: 0.6rem; padding: 0.8rem 0.9rem 0 0.9rem; }
    .summary-item { flex: 1 1 8.5rem; border-radius: 12px; padding: 0.7rem 0.8rem;
                    font-size: 0.92rem; font-weight: 600; background: #1c2340; color: #e8e8f4; }
    .summary-item .count { display: block; font-size: 1.5rem; font-weight: 700; line-height: 1.2; }
    .summary-item.ready { background: #163a28; color: #d8ffe8; }
    .summary-item.watching { background: #3a3418; color: #fff3cf; }
    .summary-item.old { background: #3a1f1f; color: #ffdada; }
    .summary-caption { padding: 0.5rem 0.9rem 0 0.9rem; font-size: 0.76rem; color: #a9a9c0; }
    .group { padding: 0.9rem 0.9rem 0.2rem 0.9rem; }
    .group > h2 { font-size: 1.02rem; margin: 0 0 0.6rem 0; color: #ffffff; }
    details.old-group { margin: 0.9rem 0.9rem 0.2rem 0.9rem; }
    details.old-group > summary { font-size: 1rem; font-weight: 700; padding: 0.85rem 0.9rem;
                                   min-height: 44px; display: flex; align-items: center;
                                   background: #1c2340; border-radius: 12px; color: #ffffff;
                                   cursor: pointer; }
    details.old-group[open] > summary { border-radius: 12px 12px 0 0; }
    .cards { display: flex; flex-direction: column; gap: 0.85rem; margin-top: 0.7rem; }
    .card { background: #16213e; border-radius: 14px; padding: 1rem; overflow-wrap: anywhere; }
    .card-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.45rem;
                 font-size: 1.08rem; font-weight: 700; color: #ffffff; }
    .direction.long { color: #7cffb2; }
    .direction.short { color: #ff9b9b; }
    .setup { margin-top: 0.2rem; font-size: 0.9rem; color: #c7c9e8; }
    .status-row { display: flex; align-items: center; flex-wrap: wrap; gap: 0.6rem; margin-top: 0.55rem; }
    .badge { display: inline-block; padding: 0.28rem 0.65rem; border-radius: 999px;
             font-size: 0.8rem; font-weight: 700; }
    .badge.ready { background: #1f6f43; color: #eaffef; }
    .badge.watching { background: #7a5e12; color: #fff3d6; }
    .badge.old { background: #6f2f2f; color: #ffe1e1; }
    .age { font-size: 0.85rem; color: #b7b9d6; }
    .plan-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0.5rem; margin-top: 0.75rem; }
    .plan-cell { background: #0f1530; border-radius: 10px; padding: 0.55rem 0.65rem; }
    .plan-label { display: block; font-size: 0.72rem; text-transform: uppercase;
                  letter-spacing: 0.03em; color: #9294bd; }
    .plan-value { display: block; font-size: 1.08rem; font-weight: 700; color: #ffffff; margin-top: 0.1rem; }
    .context-score { margin-top: 0.7rem; font-size: 0.9rem; color: #d6d7ef; }
    .context-score .context-note { font-size: 0.78rem; color: #9294bd; }
    .reason { margin-top: 0.45rem; font-size: 0.88rem; color: #c7c9e8; line-height: 1.5; }
    details.technical { margin-top: 0.75rem; }
    details.technical > summary { padding: 0.6rem 0.15rem; min-height: 44px; display: flex;
                                   align-items: center; font-size: 0.85rem; color: #9294bd;
                                   cursor: pointer; }
    .tech-grid { font-size: 0.78rem; color: #b7b9d6; display: flex; flex-direction: column;
                 gap: 0.32rem; padding: 0.3rem 0.15rem 0 0.15rem; overflow-wrap: anywhere;
                 word-break: break-word; }
    .tech-row { display: flex; flex-wrap: wrap; gap: 0.3rem; }
    .tech-row .tech-label { color: #7d7fa3; }
    .empty { padding: 1rem 0.9rem; color: #a9a9c0; }
  </style>
</head>
<body>
"""

_PAGE_FOOT = """</body>
</html>"""

_PRICE_DASH = "—"

_GROUP_READY = "ready"
_GROUP_WATCHING = "watching"
_GROUP_OLD = "old"

_GROUP_LABELS = {
    _GROUP_READY: "Ready to review",
    _GROUP_WATCHING: "Watching",
    _GROUP_OLD: "Old / no longer actionable",
}

# Plain-English display mappings for phone scanning. An unrecognized value
# is returned unchanged by `_plain_label` (never dropped, never silently
# treated as safe/eligible) so `_esc`/`html.escape` at the call site still
# shows and escapes it.
_SETUP_FAMILY_LABELS = {
    "VOLATILITY_COMPRESSION": "Volatility squeeze",
    "SWEEP_RECLAIM": "Sweep & reclaim",
    "SUPPORT_RESISTANCE_REJECTION": "Level rejection",
    "SUPPORT_RESISTANCE_BREAKOUT_RETEST": "Breakout retest",
    "SUPPORT_RESISTANCE_FAILED_BREAKOUT": "Failed breakout",
}

_DIRECTION_LABELS = {"LONG": "Long", "SHORT": "Short"}
_DIRECTION_CLASSES = {"LONG": "long", "SHORT": "short"}

_ENTRY_TYPE_LABELS = {
    "RESTING_LIMIT_AT_SOURCE_CLOSE": "Limit order at setup close",
}

_AVAILABILITY_LABELS = {
    "AVAILABLE": "Plan available",
    "UNAVAILABLE": "No plan available",
}

_OUTCOME_STATE_LABELS = {
    "NOT_EVALUABLE": "Not evaluable",
    "PENDING_ENTRY": "Waiting for entry",
    "ENTERED": "Entered, in progress",
    "HIT_1R": "Hit target 1",
    "HIT_2R": "Hit target 2",
    "STOPPED": "Stopped out",
    "EXPIRED_UNENTERED": "Expired, never entered",
    "EXPIRED_OPEN": "Expired while open",
    "AMBIGUOUS": "Ambiguous outcome",
    "INSUFFICIENT_DATA": "Insufficient data",
}

_ELIGIBILITY_REASON_LABELS = {
    REASON_STATUS_NOT_ACTIVE: "no longer active",
    REASON_NOT_RESEARCH_ONLY: "not flagged research-only",
    REASON_NOT_FRESH: "no longer fresh",
    REASON_SCORE_NOT_CURRENT_COMPATIBLE: "scored with an older method",
    REASON_PLAN_NOT_AVAILABLE: "no trade plan available",
    REASON_OUTCOME_NOT_PRESENT: "outcome not yet tracked",
    REASON_OUTCOME_TERMINAL: "outcome already resolved",
    REASON_OUTCOME_STATE_NOT_PENDING_ENTRY: "past the entry stage",
}


def _plain_label(mapping: dict[str, str], value: Optional[str]) -> Optional[str]:
    """Look up a known plain-English label for a raw code; an unrecognized
    value is returned unchanged, never dropped, so it still surfaces
    (escaped, at the call site) instead of disappearing or reading as safe.
    """
    if value is None:
        return None
    return mapping.get(value, value)


def _labeled_esc(mapping: dict[str, str], value: Optional[str]) -> str:
    """Escaped 'Plain label (RAW_CODE)' for a technical-detail field with a
    known plain-English mapping, or just the escaped raw value if unmapped
    or missing — the raw code is always still visible, escaped, never
    dropped.
    """
    if value is None:
        return "—"
    label = mapping.get(value)
    if label is None:
        return _esc(value)
    return f"{_esc(label)} ({_esc(value)})"


def _format_price(value: Any) -> str:
    """Deterministic, display-only price formatting. `Decimal(str(value))`
    round-trips a float's own shortest decimal representation, so this never
    reintroduces binary floating-point noise (e.g. 0.1 + 0.2). Uses 2
    decimal places at or above 1, and 4 significant figures below 1 so a
    sub-dollar or very small positive price keeps meaningful precision
    instead of rounding away to 0.00. Purely a rendering helper — it never
    touches the underlying API numeric value. A pathologically large or
    small (but finite) value can make the fixed-precision `quantize()` call
    exceed the default Decimal context's precision (e.g. 1e120), which
    raises `decimal.InvalidOperation` rather than producing a value; that
    and any other Decimal edge case is treated as unrenderable and falls
    back to the display dash, same as None/non-finite.
    """
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return _PRICE_DASH
    fv = float(value)
    if not math.isfinite(fv):
        return _PRICE_DASH
    try:
        dec = Decimal(str(fv))
        if dec == 0:
            return "0.00"
        sign = "-" if dec < 0 else ""
        abs_dec = abs(dec)
        if abs_dec >= 1:
            quant = Decimal("0.01")
        else:
            quant = Decimal(1).scaleb(abs_dec.adjusted() - 3)
        quantized = abs_dec.quantize(quant, rounding=ROUND_HALF_UP)
    except DecimalException:
        return _PRICE_DASH
    return f"{sign}{format(quantized, 'f')}"


def _format_score(value: Any) -> str:
    """Display-only rounding of the already-projected score to one decimal
    place — never touches the raw API `score_value`.
    """
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return _PRICE_DASH
    if not math.isfinite(value):
        return _PRICE_DASH
    return f"{value:.1f}"


def _format_age(age_seconds: Any) -> str:
    """Honest, display-only human age derived from the already-computed
    freshness age. Missing or invalid input always stays 'unknown age'
    rather than inventing a value.
    """
    if (
        age_seconds is None
        or isinstance(age_seconds, bool)
        or not isinstance(age_seconds, (int, float))
    ):
        return "unknown age"
    if not math.isfinite(age_seconds) or age_seconds < 0:
        return "unknown age"
    if age_seconds < 60:
        return "just now"
    minutes = int(age_seconds // 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _presentation_group(row: dict) -> str:
    """Pure, presentation-only phone-scanning bucket for one already-
    projected row. Never hides a row (every group is always rendered in
    full, see `_render_html`) and is never itself a trade recommendation or
    execution signal — purely a restatement of facts `_project_row` already
    computed (eligibility, freshness, status, outcome terminality).

    - `ready`: eligibility_state is REVIEW_ELIGIBLE.
    - `watching`: not ready, but fresh, active, and not terminal.
    - `old`: everything else (stale/unknown freshness, non-active status,
      or a terminal outcome).
    """
    if row["eligibility_state"] == ELIGIBILITY_REVIEW_ELIGIBLE:
        return _GROUP_READY
    if (
        row["freshness_state"] == "FRESH"
        and row["status"] == "ACTIVE"
        and not row["outcome_is_terminal"]
    ):
        return _GROUP_WATCHING
    return _GROUP_OLD


def _group_rows(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {_GROUP_READY: [], _GROUP_WATCHING: [], _GROUP_OLD: []}
    for row in rows:
        grouped[_presentation_group(row)].append(row)
    return grouped


def _plain_reason_text(reasons: list[str]) -> str:
    """Plain-English join of eligibility reasons for primary card content.
    A raw reason code is never surfaced here, even for one this module
    doesn't yet have a label for — the full raw list always remains
    available in the technical disclosure (see `_render_technical`).
    """
    if not reasons:
        return "an unresolved review condition"
    return ", ".join(
        _ELIGIBILITY_REASON_LABELS.get(code, "an additional review condition")
        for code in reasons
    )


def _reason_line(row: dict, group: str) -> str:
    """One plain-English sentence for why a card sits in its group — purely
    descriptive of facts already computed elsewhere in this module, never
    itself a trade recommendation or execution signal.
    """
    if group == _GROUP_READY:
        return "Meets every research-review condition right now (not a trade recommendation)."
    detail = _plain_reason_text(row["eligibility_reasons"])
    if group == _GROUP_WATCHING:
        return f"Fresh and active, but not yet ready to review: {detail}."
    return f"No longer actionable for review: {detail}."


def _render_header() -> str:
    return (
        "<header>"
        '<p class="title">APEX — Mobile Opportunity Board v0.2 (research only)</p>'
        f'<p class="notice">{html.escape(_RESEARCH_ONLY_NOTICE, quote=True)}</p>'
        "</header>"
    )


def _render_summary(grouped: dict[str, list[dict]]) -> str:
    items = "".join(
        f'<div class="summary-item {key}"><span class="count">{len(grouped[key])}</span>'
        f"{html.escape(_GROUP_LABELS[key], quote=True)}</div>"
        for key in (_GROUP_READY, _GROUP_WATCHING, _GROUP_OLD)
    )
    return (
        f'<div class="summary">{items}</div>'
        '<p class="summary-caption">Grouping is presentation-only, based on the existing '
        "status/freshness/plan/outcome facts below — it is never a trade recommendation or "
        "execution signal.</p>"
    )


def _render_plan_grid(row: dict) -> str:
    cells = (
        ("Entry", row["plan_entry_price"]),
        ("Stop", row["plan_stop_price"]),
        ("Target 1", row["plan_target_1r_price"]),
        ("Target 2", row["plan_target_2r_price"]),
    )
    parts = "".join(
        f'<div class="plan-cell"><span class="plan-label">{label}</span>'
        f'<span class="plan-value">{_format_price(value)}</span></div>'
        for label, value in cells
    )
    return f'<div class="plan-grid">{parts}</div>'


def _render_technical(row: dict) -> str:
    fields = (
        ("Opportunity ID", _esc(row["opportunity_uid"])),
        ("First detected", _esc(row["first_detected_at"])),
        ("Last seen", _esc(row["last_seen_at"])),
        ("Occurrences", _esc(row["occurrence_count"])),
        ("Freshness state", _esc(row["freshness_state"])),
        ("Freshness age (s)", _esc(row["freshness_age_seconds"])),
        ("Stale after (min)", _esc(row["freshness_stale_threshold_minutes"])),
        ("Freshness as of", _esc(row["freshness_as_of"])),
        ("Score version", _esc(row["score_version"])),
        ("Score current-compatible", _esc(row["score_current_compatible"])),
        ("Score warnings", _esc_list(row["score_warnings"])),
        ("Plan availability", _labeled_esc(_AVAILABILITY_LABELS, row["plan_availability"])),
        ("Plan unavailable reason", _esc(row["plan_unavailable_reason"])),
        ("Entry type", _labeled_esc(_ENTRY_TYPE_LABELS, row["plan_entry_type"])),
        ("Invalidation price", _esc(row["plan_invalidation_price"])),
        ("Reward:risk 1R", _esc(row["plan_reward_risk_1r"])),
        ("Reward:risk 2R", _esc(row["plan_reward_risk_2r"])),
        ("Evaluation not-before (ms)", _esc(row["plan_evaluation_not_before_ms"])),
        ("Evaluation expiry (ms)", _esc(row["plan_evaluation_expiry_ms"])),
        ("Plan contract version", _esc(row["plan_contract_version"])),
        ("Outcome present", _esc(row["outcome_present"])),
        ("Outcome state", _labeled_esc(_OUTCOME_STATE_LABELS, row["outcome_state"])),
        ("Outcome terminal", _esc(row["outcome_is_terminal"])),
        ("Outcome last evaluated (ms)", _esc(row["outcome_last_evaluated_ms"])),
        ("Outcome data quality", _esc(row["outcome_data_quality"])),
        ("Outcome ambiguous", _esc(row["outcome_is_ambiguous"])),
        ("MFE (R)", _esc(row["outcome_mfe_r"])),
        ("MAE (R)", _esc(row["outcome_mae_r"])),
        ("Opportunity contract version", _esc(row["opportunity_contract_version"])),
        ("Detector version", _esc(row["detector_version"])),
        ("Outcome contract version", _esc(row["outcome_contract_version"])),
        ("Raw eligibility state", _esc(row["eligibility_state"])),
        ("Raw eligibility reasons", _esc_list(row["eligibility_reasons"])),
    )
    rows_html = "".join(
        f'<div class="tech-row"><span class="tech-label">{label}</span><span>{value}</span></div>'
        for label, value in fields
    )
    return (
        '<details class="technical"><summary>Technical details</summary>'
        f'<div class="tech-grid">{rows_html}</div></details>'
    )


def _render_card(row: dict, *, group: str) -> str:
    direction_class = _DIRECTION_CLASSES.get(row["direction"], "")
    direction_label = _esc(_plain_label(_DIRECTION_LABELS, row["direction"]))
    setup_label = _esc(_plain_label(_SETUP_FAMILY_LABELS, row["setup_family"]))
    badge_label = html.escape(_GROUP_LABELS[group], quote=True)
    age_label = html.escape(_format_age(row["freshness_age_seconds"]), quote=True)
    score_label = html.escape(_format_score(row["score_value"]), quote=True)
    reason_label = html.escape(_reason_line(row, group), quote=True)
    return f"""
    <article class="card">
      <div class="card-head">
        <span class="symbol">{_esc(row['symbol'])}</span>
        <span class="direction {direction_class}">{direction_label}</span>
        <span class="timeframe">{_esc(row['primary_timeframe'])}</span>
      </div>
      <div class="setup">{setup_label}</div>
      <div class="status-row">
        <span class="badge {group}">{badge_label}</span>
        <span class="age">{age_label}</span>
      </div>
      {_render_plan_grid(row)}
      <div class="context-score">Research context score: {score_label}
        <span class="context-note">(not a probability or confidence rating)</span></div>
      <div class="reason">{reason_label}</div>
      {_render_technical(row)}
    </article>"""


def _render_group_section(group_key: str, rows: list[dict]) -> str:
    label = html.escape(_GROUP_LABELS[group_key], quote=True)
    if not rows:
        body = '<div class="empty">None right now.</div>'
    else:
        body = '<div class="cards">' + "".join(
            _render_card(r, group=group_key) for r in rows
        ) + "</div>"
    return f'<section class="group group-{group_key}"><h2>{label} ({len(rows)})</h2>{body}</section>'


def _render_old_group_section(rows: list[dict]) -> str:
    label = html.escape(_GROUP_LABELS[_GROUP_OLD], quote=True)
    if not rows:
        body = '<div class="empty">None right now.</div>'
    else:
        body = '<div class="cards">' + "".join(
            _render_card(r, group=_GROUP_OLD) for r in rows
        ) + "</div>"
    return f'<details class="group old-group"><summary>{label} ({len(rows)})</summary>{body}</details>'


def _render_html(rows: list[dict], *, as_of: datetime, status: str, limit: int) -> str:
    as_of_iso = as_of.strftime("%Y-%m-%dT%H:%M:%SZ")
    body = [
        _PAGE_HEAD,
        _render_header(),
        f'<div class="meta">as_of={html.escape(as_of_iso, quote=True)} '
        f'status_filter={html.escape(str(status), quote=True)} '
        f'limit={html.escape(str(limit), quote=True)} '
        f'count={html.escape(str(len(rows)), quote=True)}</div>',
    ]
    if not rows:
        body.append('<div class="empty">No opportunities match this filter.</div>')
    else:
        grouped = _group_rows(rows)
        body.append(_render_summary(grouped))
        body.append(_render_group_section(_GROUP_READY, grouped[_GROUP_READY]))
        body.append(_render_group_section(_GROUP_WATCHING, grouped[_GROUP_WATCHING]))
        body.append(_render_old_group_section(grouped[_GROUP_OLD]))
    body.append(_PAGE_FOOT)
    return "".join(body)


def _error_html() -> str:
    return (
        _PAGE_HEAD
        + _render_header()
        + f'<div class="empty">{html.escape(_GENERIC_ERROR_MESSAGE, quote=True)}</div>'
        + _PAGE_FOOT
    )


def _error_json_response(status_code: int) -> Response:
    body = json.dumps({"error": _GENERIC_ERROR_MESSAGE}, allow_nan=False)
    return Response(content=body, media_type="application/json", status_code=status_code)


# ------------------------------------------------------------------ router factory

def create_board_router(conn: Optional[sqlite3.Connection] = None) -> APIRouter:
    """Build the Live Opportunity Board router. `conn`, if supplied, is used
    for every request instead of resolving the global connection — intended
    for tests. Only GET routes are ever registered.
    """
    router = APIRouter(tags=["opportunity-board"])

    def _resolve_connection() -> Optional[sqlite3.Connection]:
        if conn is not None:
            return conn
        from apex.db.connection import get_connection, is_open

        if not is_open():
            return None
        return get_connection()

    def _load_rows(
        db_conn: sqlite3.Connection,
        *,
        status: str,
        symbol: Optional[str],
        direction: Optional[str],
        primary_timeframe: Optional[str],
        setup_family: Optional[str],
        limit: int,
    ) -> tuple[list[dict], datetime]:
        as_of = datetime.now(timezone.utc)
        rows = repo.get_board_opportunities(
            db_conn,
            setup_family=setup_family,
            symbol=symbol,
            direction=direction,
            primary_timeframe=primary_timeframe,
            status=None if status == "ALL" else status,
            limit=limit,
        )
        projected = [_project_row(row, as_of=as_of) for row in rows]
        return projected, as_of

    @router.get("/opportunities", response_class=HTMLResponse)
    async def opportunities_html(
        status: StatusParam = "ACTIVE",
        symbol: Optional[str] = Query(
            None, min_length=1, max_length=20, pattern=r"^[A-Za-z0-9]+$"
        ),
        direction: Optional[DirectionParam] = None,
        primary_timeframe: Optional[TimeframeParam] = None,
        setup_family: Optional[SetupFamilyParam] = None,
        limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    ) -> HTMLResponse:
        db_conn = _resolve_connection()
        if db_conn is None:
            return HTMLResponse(content=_error_html(), status_code=503)
        try:
            rows, as_of = _load_rows(
                db_conn,
                status=status,
                symbol=symbol,
                direction=direction,
                primary_timeframe=primary_timeframe,
                setup_family=setup_family,
                limit=limit,
            )
            html_content = _render_html(rows, as_of=as_of, status=status, limit=limit)
        except Exception:
            logger.exception("Live Opportunity Board: HTML read/assembly failed")
            return HTMLResponse(content=_error_html(), status_code=500)
        return HTMLResponse(content=html_content)

    @router.get("/opportunities/api")
    async def opportunities_api(
        status: StatusParam = "ACTIVE",
        symbol: Optional[str] = Query(
            None, min_length=1, max_length=20, pattern=r"^[A-Za-z0-9]+$"
        ),
        direction: Optional[DirectionParam] = None,
        primary_timeframe: Optional[TimeframeParam] = None,
        setup_family: Optional[SetupFamilyParam] = None,
        limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    ) -> Response:
        db_conn = _resolve_connection()
        if db_conn is None:
            return _error_json_response(503)
        try:
            rows, as_of = _load_rows(
                db_conn,
                status=status,
                symbol=symbol,
                direction=direction,
                primary_timeframe=primary_timeframe,
                setup_family=setup_family,
                limit=limit,
            )
        except Exception:
            logger.exception("Live Opportunity Board: API read/assembly failed")
            return _error_json_response(500)

        payload = {
            "as_of": as_of.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status_filter": status,
            "limit": limit,
            "count": len(rows),
            "research_only_notice": _RESEARCH_ONLY_NOTICE,
            "rows": rows,
        }
        try:
            body = json.dumps(payload, allow_nan=False)
        except (TypeError, ValueError):
            logger.exception("Live Opportunity Board: JSON serialization failed")
            return _error_json_response(500)
        return Response(content=body, media_type="application/json")

    return router
