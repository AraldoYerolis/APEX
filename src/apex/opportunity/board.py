"""Live Opportunity Board v0.1 — local, development-only, read-only research
UI/API over existing ranked opportunity observations, immutable trade plans,
and prospective outcome evidence.

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
  <title>APEX — Live Opportunity Board (research only)</title>
  <style>
    body { font-family: -apple-system, sans-serif; background: #0f1220; color: #e8e8f0;
           margin: 0; padding: 0 0 2rem 0; }
    header { background: #1a1a2e; padding: 1rem; position: sticky; top: 0; }
    header h1 { margin: 0 0 0.35rem 0; font-size: 1.1rem; color: #e94560; }
    .notice { font-size: 0.78rem; line-height: 1.4; color: #f5c542; margin: 0; }
    .meta { font-size: 0.75rem; color: #999; padding: 0.5rem 1rem 0 1rem; }
    .cards { display: flex; flex-direction: column; gap: 0.75rem; padding: 1rem; }
    .card { background: #16213e; border-radius: 10px; padding: 0.9rem 1rem; }
    .card h2 { margin: 0 0 0.4rem 0; font-size: 1rem; color: #fff; }
    .row { display: flex; flex-wrap: wrap; gap: 0.4rem 1rem; font-size: 0.82rem; color: #cfcfe0; }
    .row .label { color: #8888a0; }
    .badge { display: inline-block; padding: 0.1rem 0.45rem; border-radius: 6px;
             font-size: 0.72rem; font-weight: 600; background: #333a5c; color: #cfd2ff; }
    .badge.eligible { background: #1f6f43; color: #d8ffe8; }
    .badge.stale, .badge.unknown, .badge.not-eligible { background: #6f2f2f; color: #ffd8d8; }
    .section { margin-top: 0.5rem; padding-top: 0.5rem; border-top: 1px solid #2a2f4d; }
    .empty { padding: 1rem; color: #999; }
  </style>
</head>
<body>
"""

_PAGE_FOOT = """</body>
</html>"""


def _badge_class(value: str) -> str:
    upper = (value or "").upper()
    if upper in ("FRESH", "REVIEW_ELIGIBLE", "AVAILABLE"):
        return "eligible"
    if upper in ("STALE", "UNKNOWN", "NOT_ELIGIBLE", "UNAVAILABLE"):
        return "not-eligible"
    return ""


def _render_row_html(row: dict) -> str:
    return f"""
    <div class="card">
      <h2>{_esc(row['symbol'])} {_esc(row['direction'])} · {_esc(row['setup_family'])} · {_esc(row['primary_timeframe'])}</h2>
      <div class="row">
        <span class="label">status</span> <span>{_esc(row['status'])}</span>
        <span class="label">research_only</span> <span>{_esc(row['research_only'])}</span>
        <span class="label">opportunity_uid</span> <span>{_esc(row['opportunity_uid'])}</span>
      </div>
      <div class="row">
        <span class="label">first_detected_at</span> <span>{_esc(row['first_detected_at'])}</span>
        <span class="label">last_seen_at</span> <span>{_esc(row['last_seen_at'])}</span>
        <span class="label">occurrence_count</span> <span>{_esc(row['occurrence_count'])}</span>
      </div>
      <div class="section">
        <div class="row">
          <span class="label">freshness</span>
          <span class="badge {_badge_class(row['freshness_state'])}">{_esc(row['freshness_state'])}</span>
          <span class="label">age_s</span> <span>{_esc(row['freshness_age_seconds'])}</span>
          <span class="label">stale_after_min</span> <span>{_esc(row['freshness_stale_threshold_minutes'])}</span>
          <span class="label">as_of</span> <span>{_esc(row['freshness_as_of'])}</span>
        </div>
        <div class="row">
          <span class="label">score</span> <span>{_esc(row['score_value'])}</span>
          <span class="label">score_version</span> <span>{_esc(row['score_version'])}</span>
          <span class="label">current_compatible</span> <span>{_esc(row['score_current_compatible'])}</span>
          <span class="label">score_warnings</span> <span>{_esc_list(row['score_warnings'])}</span>
        </div>
      </div>
      <div class="section">
        <div class="row">
          <span class="label">plan_present</span> <span>{_esc(row['plan_present'])}</span>
          <span class="label">plan_availability</span>
          <span class="badge {_badge_class(row['plan_availability'] or '')}">{_esc(row['plan_availability'])}</span>
          <span class="label">unavailable_reason</span> <span>{_esc(row['plan_unavailable_reason'])}</span>
          <span class="label">entry_type</span> <span>{_esc(row['plan_entry_type'])}</span>
        </div>
        <div class="row">
          <span class="label">entry</span> <span>{_esc(row['plan_entry_price'])}</span>
          <span class="label">invalidation</span> <span>{_esc(row['plan_invalidation_price'])}</span>
          <span class="label">stop</span> <span>{_esc(row['plan_stop_price'])}</span>
          <span class="label">1R</span> <span>{_esc(row['plan_target_1r_price'])}</span>
          <span class="label">2R</span> <span>{_esc(row['plan_target_2r_price'])}</span>
          <span class="label">RR1</span> <span>{_esc(row['plan_reward_risk_1r'])}</span>
          <span class="label">RR2</span> <span>{_esc(row['plan_reward_risk_2r'])}</span>
        </div>
        <div class="row">
          <span class="label">eval_not_before_ms</span> <span>{_esc(row['plan_evaluation_not_before_ms'])}</span>
          <span class="label">eval_expiry_ms</span> <span>{_esc(row['plan_evaluation_expiry_ms'])}</span>
        </div>
      </div>
      <div class="section">
        <div class="row">
          <span class="label">outcome_present</span> <span>{_esc(row['outcome_present'])}</span>
          <span class="label">outcome_state</span> <span>{_esc(row['outcome_state'])}</span>
          <span class="label">terminal</span> <span>{_esc(row['outcome_is_terminal'])}</span>
          <span class="label">last_evaluated_ms</span> <span>{_esc(row['outcome_last_evaluated_ms'])}</span>
          <span class="label">data_quality</span> <span>{_esc(row['outcome_data_quality'])}</span>
          <span class="label">ambiguous</span> <span>{_esc(row['outcome_is_ambiguous'])}</span>
          <span class="label">MFE_R</span> <span>{_esc(row['outcome_mfe_r'])}</span>
          <span class="label">MAE_R</span> <span>{_esc(row['outcome_mae_r'])}</span>
        </div>
      </div>
      <div class="section">
        <div class="row">
          <span class="label">opportunity_contract</span> <span>{_esc(row['opportunity_contract_version'])}</span>
          <span class="label">detector</span> <span>{_esc(row['detector_version'])}</span>
          <span class="label">plan_contract</span> <span>{_esc(row['plan_contract_version'])}</span>
          <span class="label">outcome_contract</span> <span>{_esc(row['outcome_contract_version'])}</span>
        </div>
        <div class="row">
          <span class="label">review_eligibility</span>
          <span class="badge {_badge_class(row['eligibility_state'])}">{_esc(row['eligibility_state'])}</span>
          <span class="label">reasons</span> <span>{_esc_list(row['eligibility_reasons'])}</span>
        </div>
      </div>
    </div>"""


def _render_html(rows: list[dict], *, as_of: datetime, status: str, limit: int) -> str:
    as_of_iso = as_of.strftime("%Y-%m-%dT%H:%M:%SZ")
    body = [
        _PAGE_HEAD,
        "<header>",
        "<h1>APEX — Live Opportunity Board v0.1 (research only)</h1>",
        f'<p class="notice">{html.escape(_RESEARCH_ONLY_NOTICE, quote=True)}</p>',
        "</header>",
        f'<div class="meta">as_of={html.escape(as_of_iso, quote=True)} '
        f'status_filter={html.escape(str(status), quote=True)} '
        f'limit={html.escape(str(limit), quote=True)} '
        f'count={html.escape(str(len(rows)), quote=True)}</div>',
    ]
    if not rows:
        body.append('<div class="empty">No opportunities match this filter.</div>')
    else:
        body.append('<div class="cards">')
        body.extend(_render_row_html(r) for r in rows)
        body.append("</div>")
    body.append(_PAGE_FOOT)
    return "".join(body)


def _error_html() -> str:
    return (
        _PAGE_HEAD
        + "<header><h1>APEX — Live Opportunity Board v0.1 (research only)</h1></header>"
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
        except Exception:
            logger.exception("Live Opportunity Board: HTML read/assembly failed")
            return HTMLResponse(content=_error_html(), status_code=500)
        return HTMLResponse(content=_render_html(rows, as_of=as_of, status=status, limit=limit))

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
