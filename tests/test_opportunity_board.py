"""Tests for Live Opportunity Board v0.1 (src/apex/opportunity/board.py) —
a local, development-only, read-only HTML/JSON view joining ranked
opportunity_observations to immutable trade plans and prospective outcome
evidence.

Covers: mount policy (default-off; development + explicit flag only; never
mounted in production even with the flag true); GET-only routes with
/health preserved; the joined repository query's row shapes (complete row,
missing/unavailable plan, missing/terminal/ambiguous outcome); score
current/legacy/malformed/out-of-range compatibility; deterministic ordering
and filters; freshness (FRESH/STALE/UNKNOWN) and complete, never-suppressed
future-alert review eligibility reasons; HTML escaping and bounded-warning
parsing with no raw JSON/secret leakage; non-finite-number/malformed-JSON
serialization safety; and that repository/route reads never mutate the
connection.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from apex.app import create_app
from apex.config import Settings
from apex.db import repository as repo
from apex.db.connection import close_db, init_db
from apex.opportunity import board
from apex.opportunity.contract import Opportunity
from apex.opportunity.scoring import SCORE_VERSION
from apex.opportunity.trade_plan import TRADE_PLAN_CONTRACT_VERSION, TradePlan
from apex.opportunity.trade_plan_outcome import (
    TERMINAL_OUTCOME_STATES,
    TRADE_PLAN_OUTCOME_CONTRACT_VERSION,
    TradePlanOutcomeEvaluation,
)


# ------------------------------------------------------------------ seeding helpers

def _seed_opportunity(conn, **overrides) -> str:
    base = dict(
        opportunity_uid=f"uid-{overrides.get('fingerprint', 'x')}",
        fingerprint="fp-x",
        symbol="BTC",
        direction="LONG",
        setup_family="SWEEP_RECLAIM",
        detector_version="sweep_reclaim_v0_1",
        primary_timeframe="5m",
        status="ACTIVE",
        research_only=True,
        first_detected_at="2026-09-17T00:00:00Z",
        last_seen_at="2026-09-17T00:00:00Z",
        occurrence_count=1,
        source_candle_open_time=1000,
        source_candle_close_time=1300,
        total_score=70.0,
        score_version=SCORE_VERSION,
        score_warnings_json="[]",
        context_json="{}",
        component_scores_json="{}",
        evidence_json="{}",
        warnings_json="[]",
        measurements_json="{}",
    )
    base.update(overrides)
    repo.insert_opportunity(conn, Opportunity(**base))
    return base["opportunity_uid"]


def _make_plan(
    *,
    plan_uid: str,
    opportunity_uid: str,
    symbol: str = "BTC",
    direction: str = "LONG",
    family: str = "SWEEP_RECLAIM",
    timeframe: str = "5m",
    created_at: str = "2026-09-17T00:00:00Z",
    availability: str = "AVAILABLE",
    unavailable_reason: str | None = None,
    detector_version: str = "sweep_reclaim_v0_1",
) -> TradePlan:
    provenance = json.dumps({"note": "PROVENANCE_SENTINEL"})
    if availability == "AVAILABLE":
        return TradePlan(
            plan_uid=plan_uid, opportunity_uid=opportunity_uid, symbol=symbol, direction=direction,
            setup_family=family, primary_timeframe=timeframe, detector_version=detector_version,
            opportunity_contract_version="opportunity_v0_1", plan_contract_version=TRADE_PLAN_CONTRACT_VERSION,
            source_candle_open_time=1000, source_candle_close_time=1300, created_at=created_at,
            availability="AVAILABLE", unavailable_reason=None, entry_type="RESTING_LIMIT_AT_SOURCE_CLOSE",
            entry_price=110.0, invalidation_price=100.0, stop_price=100.0, risk_distance=10.0,
            target_1r_price=120.0, target_2r_price=130.0, target_1r_multiple=1.0, target_2r_multiple=2.0,
            reward_risk_1r=1.0, reward_risk_2r=2.0, evaluation_not_before_ms=1300,
            evaluation_expiry_ms=1300 + 24 * 300_000, provenance_json=provenance, warnings_json="[]",
        )
    return TradePlan(
        plan_uid=plan_uid, opportunity_uid=opportunity_uid, symbol=symbol, direction=direction,
        setup_family=family, primary_timeframe=timeframe, detector_version=detector_version,
        opportunity_contract_version="opportunity_v0_1", plan_contract_version=TRADE_PLAN_CONTRACT_VERSION,
        source_candle_open_time=1000, source_candle_close_time=1300, created_at=created_at,
        availability="UNAVAILABLE", unavailable_reason=unavailable_reason or "NO_STRUCTURAL_INVALIDATION",
        entry_type=None, entry_price=None, invalidation_price=None, stop_price=None, risk_distance=None,
        target_1r_price=None, target_2r_price=None, target_1r_multiple=None, target_2r_multiple=None,
        reward_risk_1r=None, reward_risk_2r=None, evaluation_not_before_ms=1300,
        evaluation_expiry_ms=1300 + 24 * 300_000, provenance_json=provenance, warnings_json="[]",
    )


def _make_outcome(
    *,
    plan_uid: str,
    opportunity_uid: str,
    state: str = "PENDING_ENTRY",
    is_ambiguous: bool = False,
    mfe_r: float | None = None,
    mae_r: float | None = None,
    data_quality: str = "COMPLETE",
) -> TradePlanOutcomeEvaluation:
    return TradePlanOutcomeEvaluation(
        plan_uid=plan_uid, opportunity_uid=opportunity_uid,
        contract_version=TRADE_PLAN_OUTCOME_CONTRACT_VERSION,
        state=state, is_terminal=state in TERMINAL_OUTCOME_STATES,
        last_evaluated_ms=1_600_000, last_evaluated_open_time=1300, entry_open_time=None,
        hit_1r_open_time=None, terminal_open_time=None, terminal_reason=None,
        mfe_r=mfe_r, mae_r=mae_r, data_quality=data_quality,
        first_missing_boundary_ms=None, is_ambiguous=is_ambiguous, decisive_ohlc=None,
        crossed_levels=(), evidence_json=json.dumps({"note": "EVIDENCE_SENTINEL"}),
    )


def _client(conn, *, enabled: bool = True, env: str = "development") -> TestClient:
    settings = Settings(live_opportunity_board_enabled=enabled, apex_env=env)
    app = create_app(settings=settings, conn=conn)
    return TestClient(app)


# ------------------------------------------------------------------ mount policy

def test_routes_absent_by_default(tmp_path):
    conn = init_db(str(tmp_path / "default_off.db"))
    try:
        client = _client(conn, enabled=False, env="development")
        assert client.get("/opportunities").status_code == 404
        assert client.get("/opportunities/api").status_code == 404
        assert client.get("/health").status_code == 200
    finally:
        close_db()


def test_routes_mounted_only_in_development_with_explicit_flag(tmp_path):
    conn = init_db(str(tmp_path / "dev_on.db"))
    try:
        dev_client = _client(conn, enabled=True, env="development")
        assert dev_client.get("/opportunities").status_code == 200
        assert dev_client.get("/opportunities/api").status_code == 200

        prod_client = _client(conn, enabled=True, env="production")
        assert prod_client.get("/opportunities").status_code == 404
        assert prod_client.get("/opportunities/api").status_code == 404
        assert prod_client.get("/health").status_code == 200
    finally:
        close_db()


def test_get_only_and_health_preserved(tmp_path):
    conn = init_db(str(tmp_path / "get_only.db"))
    try:
        client = _client(conn, enabled=True, env="development")
        assert client.post("/opportunities").status_code == 405
        assert client.put("/opportunities").status_code == 405
        assert client.delete("/opportunities").status_code == 405
        assert client.post("/opportunities/api").status_code == 405
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok", "service": "apex"}
    finally:
        close_db()


# ------------------------------------------------------------------ joined row shapes

def test_complete_joined_row_available_plan_pending_outcome(tmp_path):
    conn = init_db(str(tmp_path / "joined.db"))
    try:
        uid = _seed_opportunity(conn, opportunity_uid="uid-a", fingerprint="fp-a")
        plan = _make_plan(plan_uid="plan-a", opportunity_uid=uid, availability="AVAILABLE")
        outcome = _make_outcome(plan_uid="plan-a", opportunity_uid=uid, state="PENDING_ENTRY")
        repo.insert_trade_plan_with_outcome(conn, plan, outcome)

        rows = repo.get_board_opportunities(conn)
        assert len(rows) == 1
        row = rows[0]
        assert row["plan_plan_uid"] == "plan-a"
        assert row["plan_availability"] == "AVAILABLE"
        assert row["outcome_state"] == "PENDING_ENTRY"

        projected = board._project_row(row, as_of=datetime(2026, 9, 17, 0, 5, 0, tzinfo=timezone.utc))
        assert projected["plan_present"] is True
        assert projected["outcome_present"] is True
        assert projected["plan_entry_price"] == 110.0
        assert projected["plan_target_2r_price"] == 130.0
        assert projected["eligibility_state"] == board.ELIGIBILITY_REVIEW_ELIGIBLE
        assert projected["eligibility_reasons"] == []
    finally:
        close_db()


def test_missing_plan_row(tmp_path):
    conn = init_db(str(tmp_path / "no_plan.db"))
    try:
        uid = _seed_opportunity(conn, opportunity_uid="uid-b", fingerprint="fp-b")
        row = repo.get_board_opportunities(conn)[0]
        assert row["plan_plan_uid"] is None
        assert row["outcome_state"] is None

        projected = board._project_row(row, as_of=datetime.now(timezone.utc))
        assert projected["plan_present"] is False
        assert projected["plan_availability"] is None
        assert projected["plan_entry_price"] is None
        assert projected["outcome_present"] is False
        assert projected["outcome_state"] is None
        assert board.REASON_PLAN_NOT_AVAILABLE in projected["eligibility_reasons"]
        assert board.REASON_OUTCOME_NOT_PRESENT in projected["eligibility_reasons"]
        assert uid == "uid-b"
    finally:
        close_db()


def test_unavailable_plan_with_not_evaluable_outcome(tmp_path):
    conn = init_db(str(tmp_path / "unavailable.db"))
    try:
        uid = _seed_opportunity(conn, opportunity_uid="uid-c", fingerprint="fp-c")
        plan = _make_plan(
            plan_uid="plan-c", opportunity_uid=uid, availability="UNAVAILABLE",
            unavailable_reason="NO_STRUCTURAL_INVALIDATION",
        )
        outcome = _make_outcome(plan_uid="plan-c", opportunity_uid=uid, state="NOT_EVALUABLE")
        repo.insert_trade_plan_with_outcome(conn, plan, outcome)

        row = repo.get_board_opportunities(conn)[0]
        projected = board._project_row(row, as_of=datetime.now(timezone.utc))
        assert projected["plan_present"] is True
        assert projected["plan_availability"] == "UNAVAILABLE"
        assert projected["plan_unavailable_reason"] == "NO_STRUCTURAL_INVALIDATION"
        assert projected["outcome_present"] is True
        assert projected["outcome_state"] == "NOT_EVALUABLE"
        assert projected["outcome_is_terminal"] is True
        assert board.REASON_PLAN_NOT_AVAILABLE in projected["eligibility_reasons"]
        assert board.REASON_OUTCOME_TERMINAL in projected["eligibility_reasons"]
    finally:
        close_db()


def test_terminal_and_ambiguous_outcome_never_hidden(tmp_path):
    conn = init_db(str(tmp_path / "ambiguous.db"))
    try:
        uid = _seed_opportunity(conn, opportunity_uid="uid-d", fingerprint="fp-d")
        plan = _make_plan(plan_uid="plan-d", opportunity_uid=uid, availability="AVAILABLE")
        outcome = _make_outcome(
            plan_uid="plan-d", opportunity_uid=uid, state="AMBIGUOUS", is_ambiguous=True,
            mfe_r=1.5, mae_r=0.5,
        )
        repo.insert_trade_plan_with_outcome(conn, plan, outcome)

        row = repo.get_board_opportunities(conn)[0]
        projected = board._project_row(row, as_of=datetime.now(timezone.utc))
        assert projected["outcome_state"] == "AMBIGUOUS"
        assert projected["outcome_is_terminal"] is True
        assert projected["outcome_is_ambiguous"] is True
        assert projected["outcome_mfe_r"] == 1.5
        assert projected["outcome_mae_r"] == 0.5
        assert board.REASON_OUTCOME_TERMINAL in projected["eligibility_reasons"]
    finally:
        close_db()


# ------------------------------------------------------------------ score compatibility

def test_current_score_is_compatible_and_shown(tmp_path):
    conn = init_db(str(tmp_path / "score_current.db"))
    try:
        _seed_opportunity(
            conn, opportunity_uid="uid-cur", fingerprint="fp-cur",
            total_score=82.5, score_version=SCORE_VERSION,
        )
        row = repo.get_board_opportunities(conn)[0]
        projected = board._project_row(row, as_of=datetime.now(timezone.utc))
        assert projected["score_value"] == 82.5
        assert projected["score_current_compatible"] is True
    finally:
        close_db()


def test_legacy_score_version_is_incompatible_but_still_shown(tmp_path):
    conn = init_db(str(tmp_path / "score_legacy.db"))
    try:
        _seed_opportunity(
            conn, opportunity_uid="uid-legacy", fingerprint="fp-legacy",
            total_score=90.0, score_version="some_older_version",
        )
        row = repo.get_board_opportunities(conn)[0]
        projected = board._project_row(row, as_of=datetime.now(timezone.utc))
        assert projected["score_value"] == 90.0
        assert projected["score_current_compatible"] is False
    finally:
        close_db()


def test_out_of_range_score_is_incompatible(tmp_path):
    conn = init_db(str(tmp_path / "score_oob.db"))
    try:
        _seed_opportunity(
            conn, opportunity_uid="uid-oob", fingerprint="fp-oob",
            total_score=150.0, score_version=SCORE_VERSION,
        )
        row = repo.get_board_opportunities(conn)[0]
        projected = board._project_row(row, as_of=datetime.now(timezone.utc))
        assert projected["score_current_compatible"] is False
    finally:
        close_db()


def test_malformed_nonfinite_score_normalizes_to_none(tmp_path):
    conn = init_db(str(tmp_path / "score_nan.db"))
    try:
        _seed_opportunity(
            conn, opportunity_uid="uid-nan", fingerprint="fp-nan",
            total_score=float("nan"), score_version=SCORE_VERSION,
        )
        row = repo.get_board_opportunities(conn)[0]
        projected = board._project_row(row, as_of=datetime.now(timezone.utc))
        assert projected["score_value"] is None
        assert projected["score_current_compatible"] is False
        json.dumps(projected, allow_nan=False)  # must not raise
    finally:
        close_db()


# ------------------------------------------------------------------ ordering and filters

def test_deterministic_ordering_and_symbol_filter(tmp_path):
    conn = init_db(str(tmp_path / "ordering.db"))
    try:
        _seed_opportunity(
            conn, opportunity_uid="uid-hi", fingerprint="fp-hi", symbol="BTC",
            total_score=90.0, last_seen_at="2026-09-17T00:10:00Z",
        )
        _seed_opportunity(
            conn, opportunity_uid="uid-lo", fingerprint="fp-lo", symbol="BTC",
            total_score=40.0, last_seen_at="2026-09-17T00:05:00Z",
        )
        _seed_opportunity(
            conn, opportunity_uid="uid-eth", fingerprint="fp-eth", symbol="ETH",
            total_score=95.0, last_seen_at="2026-09-17T00:15:00Z",
        )

        btc_rows = repo.get_board_opportunities(conn, symbol="BTC")
        assert [r["opportunity_uid"] for r in btc_rows] == ["uid-hi", "uid-lo"]

        all_rows = repo.get_board_opportunities(conn)
        assert [r["opportunity_uid"] for r in all_rows] == ["uid-eth", "uid-hi", "uid-lo"]
    finally:
        close_db()


def test_status_filter_defaults_to_active(tmp_path):
    conn = init_db(str(tmp_path / "status_filter.db"))
    try:
        _seed_opportunity(conn, opportunity_uid="uid-active", fingerprint="fp-active", status="ACTIVE")
        _seed_opportunity(conn, opportunity_uid="uid-expired", fingerprint="fp-expired", status="EXPIRED")

        active_only = repo.get_board_opportunities(conn)
        assert [r["opportunity_uid"] for r in active_only] == ["uid-active"]

        all_rows = repo.get_board_opportunities(conn, status=None)
        assert {r["opportunity_uid"] for r in all_rows} == {"uid-active", "uid-expired"}
    finally:
        close_db()


# ------------------------------------------------------------------ freshness

def test_freshness_fresh_stale_unknown():
    as_of = datetime(2026, 9, 17, 0, 20, 0, tzinfo=timezone.utc)

    fresh = board._compute_freshness("5m", "2026-09-17T00:00:00Z", as_of)  # 20min old, threshold 25
    assert fresh["state"] == "FRESH"
    assert fresh["stale_threshold_minutes"] == 25

    stale = board._compute_freshness("5m", "2026-09-16T23:00:00Z", as_of)  # 80min old
    assert stale["state"] == "STALE"

    unknown_timeframe = board._compute_freshness("1h", "2026-09-17T00:00:00Z", as_of)
    assert unknown_timeframe["state"] == "UNKNOWN"

    unknown_timestamp = board._compute_freshness("5m", "not-a-timestamp", as_of)
    assert unknown_timestamp["state"] == "UNKNOWN"

    unknown_future = board._compute_freshness("5m", "2026-09-17T01:00:00Z", as_of)  # in the future
    assert unknown_future["state"] == "UNKNOWN"

    fresh_3m = board._compute_freshness("3m", "2026-09-17T00:10:00Z", as_of)  # 10min old, threshold 15
    assert fresh_3m["state"] == "FRESH"
    stale_3m = board._compute_freshness("3m", "2026-09-17T00:00:00Z", as_of)  # 20min old, threshold 15
    assert stale_3m["state"] == "STALE"


def test_stale_rows_are_never_hidden_from_results(tmp_path):
    conn = init_db(str(tmp_path / "stale_visible.db"))
    try:
        _seed_opportunity(
            conn, opportunity_uid="uid-stale", fingerprint="fp-stale",
            primary_timeframe="5m", last_seen_at="2020-01-01T00:00:00Z",
        )
        client = _client(conn, enabled=True, env="development")
        resp = client.get("/opportunities/api")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["count"] == 1
        row = payload["rows"][0]
        assert row["freshness_state"] == "STALE"
        assert board.REASON_NOT_FRESH in row["eligibility_reasons"]
        assert row["eligibility_state"] == board.ELIGIBILITY_NOT_ELIGIBLE
    finally:
        close_db()


# ------------------------------------------------------------------ eligibility

def test_eligibility_reports_every_failed_predicate_deterministically():
    state, reasons = board._compute_eligibility(
        status="EXPIRED",
        research_only=False,
        freshness_state="STALE",
        score_current_compatible=False,
        plan_availability="UNAVAILABLE",
        outcome_present=False,
        outcome_state=None,
    )
    assert state == board.ELIGIBILITY_NOT_ELIGIBLE
    assert reasons == [
        board.REASON_STATUS_NOT_ACTIVE,
        board.REASON_NOT_RESEARCH_ONLY,
        board.REASON_NOT_FRESH,
        board.REASON_SCORE_NOT_CURRENT_COMPATIBLE,
        board.REASON_PLAN_NOT_AVAILABLE,
        board.REASON_OUTCOME_NOT_PRESENT,
    ]


def test_eligibility_review_eligible_when_every_condition_met():
    state, reasons = board._compute_eligibility(
        status="ACTIVE", research_only=True, freshness_state="FRESH",
        score_current_compatible=True, plan_availability="AVAILABLE",
        outcome_present=True, outcome_state="PENDING_ENTRY",
    )
    assert state == board.ELIGIBILITY_REVIEW_ELIGIBLE
    assert reasons == []


def test_eligibility_outcome_terminal_and_wrong_state_reasons():
    state, reasons = board._compute_eligibility(
        status="ACTIVE", research_only=True, freshness_state="FRESH",
        score_current_compatible=True, plan_availability="AVAILABLE",
        outcome_present=True, outcome_state="STOPPED",
    )
    assert state == board.ELIGIBILITY_NOT_ELIGIBLE
    assert board.REASON_OUTCOME_TERMINAL in reasons
    assert board.REASON_OUTCOME_STATE_NOT_PENDING_ENTRY in reasons


def test_eligibility_signature_has_no_numeric_score_threshold():
    # No score value is ever accepted by this function — only the boolean
    # current/compatible verdict — confirming eligibility never gates on a
    # numeric score threshold.
    import inspect

    params = inspect.signature(board._compute_eligibility).parameters
    assert "score_value" not in params
    assert "score" not in params
    assert "score_current_compatible" in params


# ------------------------------------------------------------------ bounded warning parsing

def test_parse_bounded_warnings_fail_closed():
    assert board._parse_bounded_warnings(None) == []
    assert board._parse_bounded_warnings("") == []
    assert board._parse_bounded_warnings("not json") == []
    assert board._parse_bounded_warnings(json.dumps({"a": 1})) == []  # not a list
    assert board._parse_bounded_warnings(json.dumps(["ok", "warnings"])) == ["ok", "warnings"]
    assert board._parse_bounded_warnings(json.dumps(["ok", ["nested"]])) == []
    assert board._parse_bounded_warnings(json.dumps(["ok", 123])) == []
    assert board._parse_bounded_warnings(json.dumps(["ok", True])) == []
    assert board._parse_bounded_warnings(json.dumps(["x" * 500])) == []
    assert board._parse_bounded_warnings(json.dumps([f"w{i}" for i in range(50)])) == []


# ------------------------------------------------------------------ HTML escaping / no raw leakage

def test_html_escapes_dynamic_values_and_never_leaks_raw_json(tmp_path):
    conn = init_db(str(tmp_path / "escape.db"))
    try:
        uid = _seed_opportunity(
            conn, opportunity_uid="uid-xss", fingerprint="fp-xss",
            symbol="<script>alert(1)</script>",
            evidence_json=json.dumps({"secret": "RAW_EVIDENCE_SENTINEL"}),
            measurements_json=json.dumps({"secret": "RAW_MEASUREMENTS_SENTINEL"}),
            context_json=json.dumps({"secret": "RAW_CONTEXT_SENTINEL"}),
            component_scores_json=json.dumps({"secret": "RAW_COMPONENT_SENTINEL"}),
        )
        plan = _make_plan(plan_uid="plan-xss", opportunity_uid=uid, availability="AVAILABLE")
        outcome = _make_outcome(plan_uid="plan-xss", opportunity_uid=uid, state="PENDING_ENTRY")
        repo.insert_trade_plan_with_outcome(conn, plan, outcome)

        client = _client(conn, enabled=True, env="development")

        html_body = client.get("/opportunities").text
        assert "<script>alert(1)</script>" not in html_body
        assert "&lt;script&gt;" in html_body

        api_body = client.get("/opportunities/api").text

        for sentinel in (
            "RAW_EVIDENCE_SENTINEL", "RAW_MEASUREMENTS_SENTINEL",
            "RAW_CONTEXT_SENTINEL", "RAW_COMPONENT_SENTINEL",
            "PROVENANCE_SENTINEL", "EVIDENCE_SENTINEL",
        ):
            assert sentinel not in html_body
            assert sentinel not in api_body

        for field_name in (
            "evidence_json", "measurements_json", "context_json",
            "component_scores_json", "provenance_json", "decisive_ohlc",
            "crossed_levels", "fingerprint",
        ):
            assert field_name not in api_body
    finally:
        close_db()


def test_research_only_notice_present_in_both_views(tmp_path):
    conn = init_db(str(tmp_path / "notice.db"))
    try:
        client = _client(conn, enabled=True, env="development")
        html_body = client.get("/opportunities").text
        assert "RESEARCH ONLY" in html_body
        assert "NOT a probability" in html_body

        api_payload = client.get("/opportunities/api").json()
        assert "RESEARCH ONLY" in api_payload["research_only_notice"]
    finally:
        close_db()


# ------------------------------------------------------------------ malformed JSON / non-finite numbers

def test_nonfinite_score_and_malformed_warnings_stay_serializable_via_api(tmp_path):
    conn = init_db(str(tmp_path / "nonfinite_api.db"))
    try:
        _seed_opportunity(
            conn, opportunity_uid="uid-nan", fingerprint="fp-nan",
            total_score=float("nan"), score_version=SCORE_VERSION,
            score_warnings_json="{not valid json",
        )
        client = _client(conn, enabled=True, env="development")
        resp = client.get("/opportunities/api")
        assert resp.status_code == 200
        payload = resp.json()  # raises if the body is not valid JSON
        row = payload["rows"][0]
        assert row["score_value"] is None
        assert row["score_warnings"] == []
    finally:
        close_db()


# ------------------------------------------------------------------ filter validation

def test_invalid_filter_params_rejected_with_422(tmp_path):
    conn = init_db(str(tmp_path / "invalid_filters.db"))
    try:
        client = _client(conn, enabled=True, env="development")
        assert client.get("/opportunities/api", params={"direction": "SIDEWAYS"}).status_code == 422
        assert client.get("/opportunities/api", params={"primary_timeframe": "1h"}).status_code == 422
        assert client.get("/opportunities/api", params={"limit": 500}).status_code == 422
        assert client.get("/opportunities/api", params={"symbol": "bad symbol!"}).status_code == 422
        assert client.get("/opportunities/api", params={"limit": board.MAX_LIMIT}).status_code == 200
    finally:
        close_db()


# ------------------------------------------------------------------ generic error paths

def test_no_connection_returns_generic_503_html_and_json(monkeypatch):
    monkeypatch.setattr("apex.db.connection.is_open", lambda: False)
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(board.create_board_router(None))
    client = TestClient(app)

    html_resp = client.get("/opportunities")
    assert html_resp.status_code == 503
    assert board._GENERIC_ERROR_MESSAGE in html_resp.text
    for leak in ("Traceback", "sqlite3", "SELECT ", "Database not initialized"):
        assert leak not in html_resp.text

    api_resp = client.get("/opportunities/api")
    assert api_resp.status_code == 503
    assert api_resp.json() == {"error": board._GENERIC_ERROR_MESSAGE}
    for leak in ("Traceback", "sqlite3", "SELECT ", "Database not initialized"):
        assert leak not in api_resp.text


def test_injected_repository_exception_returns_generic_500_html_and_json(tmp_path, monkeypatch):
    conn = init_db(str(tmp_path / "read_exception.db"))
    try:
        secret_detail = (
            "SELECT * FROM secret_table WHERE token='SENTINEL_CREDENTIAL_XYZ' "
            "-- /etc/apex/.env.production"
        )

        def _raise(*args, **kwargs):
            raise RuntimeError(secret_detail)

        monkeypatch.setattr(repo, "get_board_opportunities", _raise)
        client = _client(conn, enabled=True, env="development")

        html_resp = client.get("/opportunities")
        assert html_resp.status_code == 500
        assert board._GENERIC_ERROR_MESSAGE in html_resp.text
        for leak in (
            secret_detail, "SENTINEL_CREDENTIAL_XYZ", "SELECT ", "/etc/apex",
            "Traceback", "RuntimeError",
        ):
            assert leak not in html_resp.text

        api_resp = client.get("/opportunities/api")
        assert api_resp.status_code == 500
        assert api_resp.json() == {"error": board._GENERIC_ERROR_MESSAGE}
        for leak in (
            secret_detail, "SENTINEL_CREDENTIAL_XYZ", "SELECT ", "/etc/apex",
            "Traceback", "RuntimeError",
        ):
            assert leak not in api_resp.text
    finally:
        close_db()


def test_injected_html_renderer_exception_returns_generic_500_and_does_not_leak(tmp_path, monkeypatch):
    conn = init_db(str(tmp_path / "render_exception.db"))
    try:
        _seed_opportunity(conn, opportunity_uid="uid-render-exc", fingerprint="fp-render-exc")
        secret_detail = "RENDER_INTERNAL_SECRET_SENTINEL_abc123"

        def _raise(*args, **kwargs):
            raise RuntimeError(secret_detail)

        # Only the HTML renderer is broken; the read/assembly path (and the
        # separate JSON route, which never calls _render_html) stay intact.
        monkeypatch.setattr(board, "_render_html", _raise)
        client = _client(conn, enabled=True, env="development")

        html_resp = client.get("/opportunities")
        assert html_resp.status_code == 500
        assert board._GENERIC_ERROR_MESSAGE in html_resp.text
        for leak in (secret_detail, "RENDER_INTERNAL_SECRET_SENTINEL", "Traceback", "RuntimeError"):
            assert leak not in html_resp.text

        # The 503 connection-unavailable and JSON-route behavior are
        # unaffected by a rendering failure.
        api_resp = client.get("/opportunities/api")
        assert api_resp.status_code == 200
    finally:
        close_db()


# ------------------------------------------------------------------ valid filters via HTTP

def test_direction_filter_restricts_rows_via_api(tmp_path):
    conn = init_db(str(tmp_path / "direction_filter.db"))
    try:
        _seed_opportunity(
            conn, opportunity_uid="uid-long", fingerprint="fp-long", direction="LONG",
            total_score=90.0, last_seen_at="2026-09-17T00:10:00Z",
        )
        _seed_opportunity(
            conn, opportunity_uid="uid-short", fingerprint="fp-short", direction="SHORT",
            total_score=95.0, last_seen_at="2026-09-17T00:15:00Z",
        )
        client = _client(conn, enabled=True, env="development")

        resp = client.get("/opportunities/api", params={"direction": "LONG"})
        assert resp.status_code == 200
        rows = resp.json()["rows"]
        assert [r["opportunity_uid"] for r in rows] == ["uid-long"]
        assert all(r["direction"] == "LONG" for r in rows)
    finally:
        close_db()


def test_primary_timeframe_filter_restricts_rows_via_api(tmp_path):
    conn = init_db(str(tmp_path / "timeframe_filter.db"))
    try:
        _seed_opportunity(
            conn, opportunity_uid="uid-3m", fingerprint="fp-3m", primary_timeframe="3m",
            total_score=90.0, last_seen_at="2026-09-17T00:10:00Z",
        )
        _seed_opportunity(
            conn, opportunity_uid="uid-5m", fingerprint="fp-5m", primary_timeframe="5m",
            total_score=95.0, last_seen_at="2026-09-17T00:15:00Z",
        )
        client = _client(conn, enabled=True, env="development")

        resp = client.get("/opportunities/api", params={"primary_timeframe": "3m"})
        assert resp.status_code == 200
        rows = resp.json()["rows"]
        assert [r["opportunity_uid"] for r in rows] == ["uid-3m"]
        assert all(r["primary_timeframe"] == "3m" for r in rows)
    finally:
        close_db()


def test_setup_family_filter_restricts_rows_via_api(tmp_path):
    conn = init_db(str(tmp_path / "setup_family_filter.db"))
    try:
        _seed_opportunity(
            conn, opportunity_uid="uid-sweep", fingerprint="fp-sweep",
            setup_family="SWEEP_RECLAIM", detector_version="sweep_reclaim_v0_1",
            total_score=90.0, last_seen_at="2026-09-17T00:10:00Z",
        )
        _seed_opportunity(
            conn, opportunity_uid="uid-vol", fingerprint="fp-vol",
            setup_family="VOLATILITY_COMPRESSION", detector_version="volatility_compression_v0_1",
            total_score=95.0, last_seen_at="2026-09-17T00:15:00Z",
        )
        client = _client(conn, enabled=True, env="development")

        resp = client.get(
            "/opportunities/api", params={"setup_family": "VOLATILITY_COMPRESSION"}
        )
        assert resp.status_code == 200
        rows = resp.json()["rows"]
        assert [r["opportunity_uid"] for r in rows] == ["uid-vol"]
        assert all(r["setup_family"] == "VOLATILITY_COMPRESSION" for r in rows)
    finally:
        close_db()


# ------------------------------------------------------------------ status=ALL / symbol via HTTP API

def test_status_all_returns_active_and_expired_via_api(tmp_path):
    conn = init_db(str(tmp_path / "status_all_api.db"))
    try:
        _seed_opportunity(conn, opportunity_uid="uid-active-api", fingerprint="fp-active-api", status="ACTIVE")
        _seed_opportunity(conn, opportunity_uid="uid-expired-api", fingerprint="fp-expired-api", status="EXPIRED")
        client = _client(conn, enabled=True, env="development")

        default_resp = client.get("/opportunities/api")
        assert default_resp.status_code == 200
        default_rows = default_resp.json()["rows"]
        assert [r["opportunity_uid"] for r in default_rows] == ["uid-active-api"]

        all_resp = client.get("/opportunities/api", params={"status": "ALL"})
        assert all_resp.status_code == 200
        all_payload = all_resp.json()
        assert all_payload["status_filter"] == "ALL"
        assert {r["opportunity_uid"] for r in all_payload["rows"]} == {
            "uid-active-api", "uid-expired-api",
        }
    finally:
        close_db()


def test_symbol_filter_restricts_rows_via_api(tmp_path):
    conn = init_db(str(tmp_path / "symbol_filter_api.db"))
    try:
        _seed_opportunity(
            conn, opportunity_uid="uid-btc-api", fingerprint="fp-btc-api", symbol="BTC",
            total_score=90.0, last_seen_at="2026-09-17T00:10:00Z",
        )
        _seed_opportunity(
            conn, opportunity_uid="uid-eth-api", fingerprint="fp-eth-api", symbol="ETH",
            total_score=95.0, last_seen_at="2026-09-17T00:15:00Z",
        )
        client = _client(conn, enabled=True, env="development")

        resp = client.get("/opportunities/api", params={"symbol": "BTC"})
        assert resp.status_code == 200
        rows = resp.json()["rows"]
        assert [r["opportunity_uid"] for r in rows] == ["uid-btc-api"]
        assert all(r["symbol"] == "BTC" for r in rows)
    finally:
        close_db()


# ------------------------------------------------------------------ freshness exact boundary

def test_freshness_boundary_exact_inclusive():
    as_of_5m = datetime(2026, 9, 17, 0, 25, 0, tzinfo=timezone.utc)
    exact_5m = board._compute_freshness("5m", "2026-09-17T00:00:00Z", as_of_5m)  # exactly 25min
    assert exact_5m["state"] == "FRESH"
    assert exact_5m["age_seconds"] == 1500.0
    assert exact_5m["stale_threshold_minutes"] == 25

    as_of_3m = datetime(2026, 9, 17, 0, 15, 0, tzinfo=timezone.utc)
    exact_3m = board._compute_freshness("3m", "2026-09-17T00:00:00Z", as_of_3m)  # exactly 15min
    assert exact_3m["state"] == "FRESH"
    assert exact_3m["age_seconds"] == 900.0
    assert exact_3m["stale_threshold_minutes"] == 15


# ------------------------------------------------------------------ read-only guarantee

def test_repository_and_route_reads_never_mutate_connection(tmp_path):
    conn = init_db(str(tmp_path / "no_mutation.db"))
    try:
        _seed_opportunity(conn, opportunity_uid="uid-ro", fingerprint="fp-ro")
        before = conn.total_changes

        repo.get_board_opportunities(conn)
        assert conn.total_changes == before

        client = _client(conn, enabled=True, env="development")
        client.get("/opportunities")
        client.get("/opportunities/api")
        assert conn.total_changes == before
    finally:
        close_db()


# ------------------------------------------------------------------ v0.2 phone readability
#
# The tests below cover the v0.2 HTML redesign: presentation grouping,
# display-only helpers (price/age/score formatting, plain-English mappings),
# and the technical-disclosure confinement of raw/machine values. They build
# synthetic already-projected row dicts (the exact shape `_project_row`
# returns) so they can exercise rendering/grouping edge cases — including
# deliberately invalid/unknown values — without needing a `setup_family`,
# `direction`, etc. that would violate the real database's CHECK
# constraints. Every field `_project_row` produces is present, so these
# stay a faithful stand-in for a real projected row.

def _projected_row(**overrides) -> dict:
    base = dict(
        opportunity_uid="uid-synthetic",
        symbol="BTC",
        direction="LONG",
        setup_family="SWEEP_RECLAIM",
        primary_timeframe="5m",
        status="ACTIVE",
        research_only=True,
        first_detected_at="2026-09-17T00:00:00Z",
        last_seen_at="2026-09-17T00:00:00Z",
        occurrence_count=1,
        source_candle_open_time=1000,
        source_candle_close_time=1300,
        score_value=70.0,
        score_version=SCORE_VERSION,
        score_current_compatible=True,
        score_warnings=[],
        freshness_state="FRESH",
        freshness_age_seconds=300.0,
        freshness_stale_threshold_minutes=25,
        freshness_as_of="2026-09-17T00:05:00Z",
        plan_present=True,
        plan_availability="AVAILABLE",
        plan_unavailable_reason=None,
        plan_entry_type="RESTING_LIMIT_AT_SOURCE_CLOSE",
        plan_entry_price=110.0,
        plan_invalidation_price=100.0,
        plan_stop_price=100.0,
        plan_target_1r_price=120.0,
        plan_target_2r_price=130.0,
        plan_reward_risk_1r=1.0,
        plan_reward_risk_2r=2.0,
        plan_evaluation_not_before_ms=1300,
        plan_evaluation_expiry_ms=1300 + 24 * 300_000,
        plan_contract_version=TRADE_PLAN_CONTRACT_VERSION,
        outcome_present=True,
        outcome_state="PENDING_ENTRY",
        outcome_is_terminal=False,
        outcome_last_evaluated_ms=1_600_000,
        outcome_data_quality="COMPLETE",
        outcome_is_ambiguous=False,
        outcome_mfe_r=None,
        outcome_mae_r=None,
        opportunity_contract_version="opportunity_v0_1",
        detector_version="sweep_reclaim_v0_1",
        outcome_contract_version=TRADE_PLAN_OUTCOME_CONTRACT_VERSION,
        eligibility_state=board.ELIGIBILITY_REVIEW_ELIGIBLE,
        eligibility_reasons=[],
    )
    base.update(overrides)
    return base


def _not_eligible_row(**overrides) -> dict:
    overrides.setdefault("eligibility_state", board.ELIGIBILITY_NOT_ELIGIBLE)
    return _projected_row(**overrides)


# ------------------------------------------------------------------ display helpers: price

def test_format_price_high_ordinary_subdollar_verysmall_none_nonfinite():
    assert board._format_price(68123.4) == "68123.40"           # high-priced
    assert board._format_price(110.0) == "110.00"                # ordinary
    assert board._format_price(0.85) == "0.8500"                 # sub-dollar
    assert board._format_price(0.0000001234) == "0.0000001234"   # very small positive
    assert board._format_price(0) == "0.00"
    assert board._format_price(None) == "—"
    assert board._format_price(True) == "—"  # bool is never treated as numeric
    assert board._format_price(float("nan")) == "—"
    assert board._format_price(float("inf")) == "—"
    assert board._format_price(float("-inf")) == "—"


def test_format_price_extreme_finite_values_never_raise():
    # A pathologically large but finite value overflows the default Decimal
    # context's precision inside quantize() (2 decimal places fixed for
    # values >= 1) and must degrade to the display dash, not raise
    # decimal.InvalidOperation and crash the request.
    assert board._format_price(1e120) == "—"
    assert board._format_price(-1e120) == "—"
    assert board._format_price(1e300) == "—"
    # The sub-1 branch scales its quantization exponent to the value's own
    # magnitude, so extreme small-but-finite values stay well within
    # precision and must keep formatting normally (never crash, never
    # degrade to the dash).
    tiny = board._format_price(5e-300)
    assert tiny != "—"
    assert tiny.startswith("0.")
    smallest_subnormal = board._format_price(5e-324)
    assert smallest_subnormal != "—"
    assert smallest_subnormal.startswith("0.")


def test_format_score_rounds_for_display_only():
    assert board._format_score(82.53) == "82.5"
    assert board._format_score(None) == "—"
    assert board._format_score(float("nan")) == "—"


def test_format_age_buckets_and_unknown_fallback():
    assert board._format_age(None) == "unknown age"
    assert board._format_age(float("nan")) == "unknown age"
    assert board._format_age(-5) == "unknown age"
    assert board._format_age(True) == "unknown age"
    assert board._format_age(10) == "just now"
    assert board._format_age(125) == "2m ago"
    assert board._format_age(3700) == "1h ago"
    assert board._format_age(90000) == "1d ago"


def test_price_display_formatting_never_changes_raw_api_json_values(tmp_path):
    conn = init_db(str(tmp_path / "price_fmt.db"))
    try:
        uid = _seed_opportunity(conn, opportunity_uid="uid-price", fingerprint="fp-price")
        provenance = json.dumps({"note": "PROVENANCE_SENTINEL"})
        plan = TradePlan(
            plan_uid="plan-price", opportunity_uid=uid, symbol="BTC", direction="LONG",
            setup_family="SWEEP_RECLAIM", primary_timeframe="5m", detector_version="sweep_reclaim_v0_1",
            opportunity_contract_version="opportunity_v0_1", plan_contract_version=TRADE_PLAN_CONTRACT_VERSION,
            source_candle_open_time=1000, source_candle_close_time=1300, created_at="2026-09-17T00:00:00Z",
            availability="AVAILABLE", unavailable_reason=None, entry_type="RESTING_LIMIT_AT_SOURCE_CLOSE",
            entry_price=0.0000001234, invalidation_price=0.5, stop_price=0.85, risk_distance=0.0001,
            target_1r_price=68123.4, target_2r_price=110.0, target_1r_multiple=1.0, target_2r_multiple=2.0,
            reward_risk_1r=1.0, reward_risk_2r=2.0, evaluation_not_before_ms=1300,
            evaluation_expiry_ms=1300 + 24 * 300_000, provenance_json=provenance, warnings_json="[]",
        )
        outcome = _make_outcome(plan_uid="plan-price", opportunity_uid=uid, state="PENDING_ENTRY")
        repo.insert_trade_plan_with_outcome(conn, plan, outcome)

        client = _client(conn, enabled=True, env="development")
        api_payload = client.get("/opportunities/api").json()
        row = api_payload["rows"][0]
        assert row["plan_entry_price"] == 0.0000001234
        assert row["plan_stop_price"] == 0.85
        assert row["plan_target_1r_price"] == 68123.4
        assert row["plan_target_2r_price"] == 110.0

        html_body = client.get("/opportunities").text
        assert "0.0000001234" in html_body
        assert "0.8500" in html_body
        assert "68123.40" in html_body
        assert "110.00" in html_body
    finally:
        close_db()


def test_extreme_price_display_dash_never_affects_raw_api_json_value(tmp_path):
    # An extreme finite price that _format_price cannot render (see
    # test_format_price_extreme_finite_values_never_raise) must still pass
    # through the JSON API untouched — display-only formatting failure must
    # never mutate, drop, or null out the raw API numeric value.
    conn = init_db(str(tmp_path / "price_extreme_fmt.db"))
    try:
        uid = _seed_opportunity(conn, opportunity_uid="uid-price-extreme", fingerprint="fp-price-extreme")
        provenance = json.dumps({"note": "PROVENANCE_SENTINEL"})
        plan = TradePlan(
            plan_uid="plan-price-extreme", opportunity_uid=uid, symbol="BTC", direction="LONG",
            setup_family="SWEEP_RECLAIM", primary_timeframe="5m", detector_version="sweep_reclaim_v0_1",
            opportunity_contract_version="opportunity_v0_1", plan_contract_version=TRADE_PLAN_CONTRACT_VERSION,
            source_candle_open_time=1000, source_candle_close_time=1300, created_at="2026-09-17T00:00:00Z",
            availability="AVAILABLE", unavailable_reason=None, entry_type="RESTING_LIMIT_AT_SOURCE_CLOSE",
            entry_price=1e120, invalidation_price=100.0, stop_price=100.0, risk_distance=10.0,
            target_1r_price=120.0, target_2r_price=130.0, target_1r_multiple=1.0, target_2r_multiple=2.0,
            reward_risk_1r=1.0, reward_risk_2r=2.0, evaluation_not_before_ms=1300,
            evaluation_expiry_ms=1300 + 24 * 300_000, provenance_json=provenance, warnings_json="[]",
        )
        outcome = _make_outcome(plan_uid="plan-price-extreme", opportunity_uid=uid, state="PENDING_ENTRY")
        repo.insert_trade_plan_with_outcome(conn, plan, outcome)

        client = _client(conn, enabled=True, env="development")
        api_payload = client.get("/opportunities/api").json()
        row = api_payload["rows"][0]
        assert row["plan_entry_price"] == 1e120  # untouched raw value, not a display dash

        html_resp = client.get("/opportunities")
        assert html_resp.status_code == 200  # the extreme value degrades gracefully, no crash
        assert "—" in html_resp.text  # the unrenderable price shows the display dash
    finally:
        close_db()


# ------------------------------------------------------------------ grouping, order, summary

def test_summary_counts_group_order_and_full_row_preservation():
    as_of = datetime(2026, 9, 17, 0, 5, 0, tzinfo=timezone.utc)
    ready = _projected_row(opportunity_uid="uid-ready")
    watching = _not_eligible_row(
        opportunity_uid="uid-watch",
        eligibility_reasons=[board.REASON_PLAN_NOT_AVAILABLE, board.REASON_OUTCOME_NOT_PRESENT],
        plan_present=False, plan_availability=None, plan_unavailable_reason=None,
        plan_entry_type=None, plan_entry_price=None, plan_invalidation_price=None,
        plan_stop_price=None, plan_target_1r_price=None, plan_target_2r_price=None,
        plan_reward_risk_1r=None, plan_reward_risk_2r=None, plan_evaluation_not_before_ms=None,
        plan_evaluation_expiry_ms=None, plan_contract_version=None,
        outcome_present=False, outcome_state=None, outcome_is_terminal=None,
        outcome_last_evaluated_ms=None, outcome_data_quality=None, outcome_is_ambiguous=None,
        outcome_contract_version=None,
    )
    old = _not_eligible_row(
        opportunity_uid="uid-old",
        eligibility_reasons=[board.REASON_NOT_FRESH],
        freshness_state="STALE",
    )
    rows = [ready, watching, old]

    grouped = board._group_rows(rows)
    assert [r["opportunity_uid"] for r in grouped[board._GROUP_READY]] == ["uid-ready"]
    assert [r["opportunity_uid"] for r in grouped[board._GROUP_WATCHING]] == ["uid-watch"]
    assert [r["opportunity_uid"] for r in grouped[board._GROUP_OLD]] == ["uid-old"]

    html_body = board._render_html(rows, as_of=as_of, status="ACTIVE", limit=board.DEFAULT_LIMIT)

    # Every row is preserved in the HTML, including the collapsed old/stale
    # group (a native <details> without `open` still contains its content).
    for uid in ("uid-ready", "uid-watch", "uid-old"):
        assert uid in html_body
    assert 'class="summary-item ready"><span class="count">1</span>' in html_body
    assert 'class="summary-item watching"><span class="count">1</span>' in html_body
    assert 'class="summary-item old"><span class="count">1</span>' in html_body

    # Use the unique section/card container markers, not the group-label
    # text — that same text also appears earlier in the fixed-order summary
    # bar (_render_summary), so asserting on label text alone would pass
    # even if the actual section order below the summary were wrong.
    ready_pos = html_body.index('class="group group-ready"')
    watching_pos = html_body.index('class="group group-watching"')
    old_pos = html_body.index('class="group old-group"')
    assert ready_pos < watching_pos < old_pos


def test_group_rows_preserves_input_order_within_each_group():
    w1 = _not_eligible_row(
        opportunity_uid="w1", eligibility_reasons=[board.REASON_PLAN_NOT_AVAILABLE],
        outcome_present=False, outcome_state=None, outcome_is_terminal=None,
    )
    o1 = _not_eligible_row(
        opportunity_uid="o1", eligibility_reasons=[board.REASON_NOT_FRESH], freshness_state="STALE",
    )
    w2 = _not_eligible_row(
        opportunity_uid="w2", eligibility_reasons=[board.REASON_PLAN_NOT_AVAILABLE],
        outcome_present=False, outcome_state=None, outcome_is_terminal=None,
    )
    o2 = _not_eligible_row(
        opportunity_uid="o2", eligibility_reasons=[board.REASON_STATUS_NOT_ACTIVE], status="EXPIRED",
    )

    grouped = board._group_rows([w1, o1, w2, o2])
    assert [r["opportunity_uid"] for r in grouped[board._GROUP_WATCHING]] == ["w1", "w2"]
    assert [r["opportunity_uid"] for r in grouped[board._GROUP_OLD]] == ["o1", "o2"]


def test_current_actionable_content_precedes_stale_regardless_of_input_order():
    as_of = datetime(2026, 9, 17, 0, 5, 0, tzinfo=timezone.utc)
    watching = _not_eligible_row(
        opportunity_uid="uid-watch-order", eligibility_reasons=[board.REASON_PLAN_NOT_AVAILABLE],
        outcome_present=False, outcome_state=None, outcome_is_terminal=None,
    )
    old = _not_eligible_row(
        opportunity_uid="uid-old-order", eligibility_reasons=[board.REASON_NOT_FRESH],
        freshness_state="STALE",
    )
    # Input order is [old, watching] — the renderer must still place the
    # current/actionable group before the stale one.
    html_body = board._render_html([old, watching], as_of=as_of, status="ACTIVE", limit=board.DEFAULT_LIMIT)
    assert html_body.index("uid-watch-order") < html_body.index("uid-old-order")


# ------------------------------------------------------------------ default card content

def test_default_card_shows_plain_english_age_and_bounded_price():
    as_of = datetime(2026, 9, 17, 0, 5, 0, tzinfo=timezone.utc)
    row = _projected_row(setup_family="VOLATILITY_COMPRESSION")
    html_body = board._render_html([row], as_of=as_of, status="ACTIVE", limit=board.DEFAULT_LIMIT)

    assert "Volatility squeeze" in html_body  # plain-English setup label, not the raw code
    assert "5m ago" in html_body              # human age from freshness_age_seconds=300.0
    assert "110.00" in html_body              # plan_entry_price bounded to 2 significant decimals
    assert "Meets every research-review condition right now" in html_body  # plain-English reason


def test_raw_ids_epochs_contract_versions_and_reason_codes_confined_to_technical():
    as_of = datetime(2026, 9, 17, 0, 5, 0, tzinfo=timezone.utc)
    row = _not_eligible_row(
        opportunity_uid="uid-confine-technical",
        eligibility_reasons=[board.REASON_NOT_FRESH],
        freshness_state="STALE",
    )
    html_body = board._render_html([row], as_of=as_of, status="ACTIVE", limit=board.DEFAULT_LIMIT)
    prefix, marker, suffix = html_body.partition('<details class="technical">')
    assert marker  # the technical disclosure exists

    raw_values = (
        "uid-confine-technical",
        board.REASON_NOT_FRESH,
        row["opportunity_contract_version"],
        row["detector_version"],
        str(row["plan_evaluation_not_before_ms"]),
        str(row["outcome_last_evaluated_ms"]),
    )
    for raw in raw_values:
        assert raw not in prefix, f"{raw!r} leaked into primary card content"
        assert raw in suffix, f"{raw!r} missing from technical disclosure"


def test_unknown_setup_family_direction_and_reason_code_remain_escaped_and_visible():
    as_of = datetime(2026, 9, 17, 0, 5, 0, tzinfo=timezone.utc)
    row = _not_eligible_row(
        setup_family="<b>UNKNOWN_FAMILY</b>",
        direction="SIDEWAYS",
        eligibility_reasons=["SOME_FUTURE_REASON_CODE"],
        freshness_state="STALE",
    )
    html_body = board._render_html([row], as_of=as_of, status="ACTIVE", limit=board.DEFAULT_LIMIT)

    # An unrecognized setup family/direction is never dropped or treated as
    # safe — it is escaped and still visible.
    assert "<b>UNKNOWN_FAMILY</b>" not in html_body
    assert "&lt;b&gt;UNKNOWN_FAMILY&lt;/b&gt;" in html_body
    assert "SIDEWAYS" in html_body

    # An unrecognized eligibility reason code never leaks into primary card
    # content (it gets a generic plain-English fallback there)...
    primary_content, _, technical_content = html_body.partition('<details class="technical">')
    assert "SOME_FUTURE_REASON_CODE" not in primary_content
    # ...but it must still be visible, escaped, in the technical disclosure.
    assert "SOME_FUTURE_REASON_CODE" in technical_content


# ------------------------------------------------------------------ required warnings / safety scan

def test_required_research_warnings_visible_in_html_and_error_page():
    as_of = datetime(2026, 9, 17, 0, 5, 0, tzinfo=timezone.utc)
    html_body = board._render_html([], as_of=as_of, status="ACTIVE", limit=board.DEFAULT_LIMIT)
    error_body = board._error_html()
    for body in (html_body, error_body):
        assert "RESEARCH ONLY" in body
        assert "NOT A RECOMMENDATION" in body
        assert "NOT a probability" in body
        assert "places, sizes, or recommends a trade" in body


def test_rendered_html_has_no_scripts_forms_handlers_or_action_controls():
    as_of = datetime(2026, 9, 17, 0, 5, 0, tzinfo=timezone.utc)
    row = _projected_row()
    stale_row = _not_eligible_row(
        opportunity_uid="uid-stale-scan",
        eligibility_reasons=[board.REASON_NOT_FRESH],
        freshness_state="STALE",
    )
    html_body = board._render_html(
        [row, stale_row], as_of=as_of, status="ACTIVE", limit=board.DEFAULT_LIMIT
    )
    error_body = board._error_html()

    for body in (html_body, error_body):
        lowered = body.lower()
        for forbidden in ("<script", "<form", " onclick=", " onload=", " onerror=", "javascript:"):
            assert forbidden not in lowered
        assert "src=" not in lowered
        assert "http://" not in lowered
        assert "https://" not in lowered
        for action_word in ("approve", "execute", "place order", "buy now", "sell now"):
            assert action_word not in lowered
