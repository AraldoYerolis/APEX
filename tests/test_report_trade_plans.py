"""Tests for scripts/report_trade_plans.py — read-only report over
opportunity_trade_plans / opportunity_trade_plan_outcomes.

Covers: deterministic filters (symbol/direction/family/timeframe/
availability/state), deterministic order, explicit exceptional states
(UNAVAILABLE/AMBIGUOUS/INSUFFICIENT_DATA/EXPIRED never hidden), a missing
database file never being created, a database predating this milestone
reported as legacy/no-data, and a genuinely read-only connection (mode=ro +
PRAGMA query_only) that cannot write.
"""
from __future__ import annotations

import sqlite3
import sys

import pytest

from apex.db import repository as repo
from apex.db.connection import close_db, init_db
from apex.opportunity.contract import Opportunity
from apex.opportunity.trade_plan import TRADE_PLAN_CONTRACT_VERSION, TradePlan
from apex.opportunity.trade_plan_outcome import TRADE_PLAN_OUTCOME_CONTRACT_VERSION, TradePlanOutcomeEvaluation

sys.path.insert(0, "scripts")
import report_trade_plans  # noqa: E402


def _env(monkeypatch, db_path: str) -> None:
    monkeypatch.setenv("APEX_DB_PATH", db_path)
    monkeypatch.setenv("ACTION_TOKEN_SECRET", "test-report-secret")
    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "fake")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "fake")
    monkeypatch.setenv("ALERTS_ENABLED", "false")
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    monkeypatch.setenv("OPPORTUNITY_ENGINE_ENABLED", "false")


def _seed(conn, *, opportunity_uid, plan_uid, symbol="BTC", direction="LONG",
          family="SWEEP_RECLAIM", timeframe="5m", availability="AVAILABLE",
          state="PENDING_ENTRY", created_at="2026-09-15T00:00:00Z") -> None:
    repo.insert_opportunity(
        conn,
        Opportunity(
            opportunity_uid=opportunity_uid,
            fingerprint=f"fp-{opportunity_uid}",
            symbol=symbol,
            direction=direction,
            setup_family=family,
            detector_version="sweep_reclaim_v0_1",
            primary_timeframe=timeframe,
            first_detected_at=created_at,
            last_seen_at=created_at,
            source_candle_open_time=1000,
            source_candle_close_time=1_300_000,
        ),
    )
    if availability == "AVAILABLE":
        plan = TradePlan(
            plan_uid=plan_uid, opportunity_uid=opportunity_uid, symbol=symbol, direction=direction,
            setup_family=family, primary_timeframe=timeframe, detector_version="sweep_reclaim_v0_1",
            opportunity_contract_version="opportunity_v0_1", plan_contract_version=TRADE_PLAN_CONTRACT_VERSION,
            source_candle_open_time=1000, source_candle_close_time=1_300_000, created_at=created_at,
            availability="AVAILABLE", unavailable_reason=None, entry_type="RESTING_LIMIT_AT_SOURCE_CLOSE",
            entry_price=110.0, invalidation_price=100.0, stop_price=100.0, risk_distance=10.0,
            target_1r_price=120.0, target_2r_price=130.0, target_1r_multiple=1.0, target_2r_multiple=2.0,
            reward_risk_1r=1.0, reward_risk_2r=2.0, evaluation_not_before_ms=1_300_000,
            evaluation_expiry_ms=1_300_000 + 24 * 300_000, provenance_json="{}", warnings_json="[]",
        )
    else:
        plan = TradePlan(
            plan_uid=plan_uid, opportunity_uid=opportunity_uid, symbol=symbol, direction=direction,
            setup_family=family, primary_timeframe=timeframe, detector_version="vol_compression_v0_1_1",
            opportunity_contract_version="opportunity_v0_1", plan_contract_version=TRADE_PLAN_CONTRACT_VERSION,
            source_candle_open_time=1000, source_candle_close_time=1_300_000, created_at=created_at,
            availability="UNAVAILABLE", unavailable_reason="NO_STRUCTURAL_INVALIDATION", entry_type=None,
            entry_price=None, invalidation_price=None, stop_price=None, risk_distance=None,
            target_1r_price=None, target_2r_price=None, target_1r_multiple=None, target_2r_multiple=None,
            reward_risk_1r=None, reward_risk_2r=None, evaluation_not_before_ms=1_300_000,
            evaluation_expiry_ms=1_300_000 + 24 * 300_000, provenance_json="{}", warnings_json="[]",
        )
    outcome = TradePlanOutcomeEvaluation(
        plan_uid=plan_uid, opportunity_uid=opportunity_uid, contract_version=TRADE_PLAN_OUTCOME_CONTRACT_VERSION,
        state=state, is_terminal=state in ("NOT_EVALUABLE", "HIT_2R", "STOPPED", "EXPIRED_UNENTERED",
                                            "EXPIRED_OPEN", "AMBIGUOUS"),
        last_evaluated_ms=None, last_evaluated_open_time=None, entry_open_time=None, hit_1r_open_time=None,
        terminal_open_time=None, terminal_reason=None, mfe_r=None, mae_r=None, data_quality="COMPLETE",
        first_missing_boundary_ms=None, is_ambiguous=(state == "AMBIGUOUS"), decisive_ohlc=None,
        crossed_levels=(), evidence_json="{}",
    )
    repo.insert_trade_plan_with_outcome(conn, plan, outcome)


def test_missing_database_file_never_created(monkeypatch, tmp_path, capsys):
    db_path = str(tmp_path / "does_not_exist.db")
    _env(monkeypatch, db_path)
    with pytest.raises(SystemExit):
        report_trade_plans.main([])
    assert not (tmp_path / "does_not_exist.db").exists()


def test_legacy_database_missing_trade_plan_tables_reports_no_data(monkeypatch, tmp_path, capsys):
    db_path = str(tmp_path / "legacy.db")
    _env(monkeypatch, db_path)
    init_db(db_path)
    close_db()
    raw = sqlite3.connect(db_path)
    try:
        raw.execute("DROP TABLE IF EXISTS opportunity_trade_plans")
        raw.execute("DROP TABLE IF EXISTS opportunity_trade_plan_outcomes")
        raw.commit()
    finally:
        raw.close()

    report_trade_plans.main([])
    out = capsys.readouterr().out
    assert "predates" in out.lower()
    assert "opportunity_trade_plans" in out


def test_filters_and_deterministic_order(monkeypatch, tmp_path, capsys):
    db_path = str(tmp_path / "filters.db")
    _env(monkeypatch, db_path)
    conn = init_db(db_path)
    try:
        _seed(conn, opportunity_uid="uid-1", plan_uid="plan-1", symbol="BTC", direction="LONG",
              family="SWEEP_RECLAIM", timeframe="5m", state="PENDING_ENTRY",
              created_at="2026-09-15T00:00:00Z")
        _seed(conn, opportunity_uid="uid-2", plan_uid="plan-2", symbol="ETH", direction="SHORT",
              family="SWEEP_RECLAIM", timeframe="3m", state="STOPPED",
              created_at="2026-09-15T01:00:00Z")
        _seed(conn, opportunity_uid="uid-3", plan_uid="plan-3", symbol="BTC", direction="LONG",
              family="SUPPORT_RESISTANCE_REJECTION", timeframe="5m", state="HIT_2R",
              created_at="2026-09-15T02:00:00Z")
    finally:
        close_db()

    report_trade_plans.main(["--symbol", "BTC"])
    out = capsys.readouterr().out
    assert "plan-1" in out
    assert "plan-2" not in out  # ETH row filtered out
    # Deterministic order: created_at DESC — uid-3 (latest) before uid-1.
    pos_3 = out.find("plan-3")
    pos_1 = out.find("plan-1")
    assert pos_3 != -1 and pos_1 != -1
    assert pos_3 < pos_1


def test_state_and_availability_filters(monkeypatch, tmp_path, capsys):
    db_path = str(tmp_path / "state_filter.db")
    _env(monkeypatch, db_path)
    conn = init_db(db_path)
    try:
        _seed(conn, opportunity_uid="uid-a", plan_uid="plan-a", availability="AVAILABLE",
              state="ENTERED")
        _seed(conn, opportunity_uid="uid-b", plan_uid="plan-b", availability="UNAVAILABLE",
              state="NOT_EVALUABLE")
    finally:
        close_db()

    report_trade_plans.main(["--availability", "UNAVAILABLE"])
    out = capsys.readouterr().out
    assert "plan-b" in out
    assert "plan-a" not in out

    report_trade_plans.main(["--state", "ENTERED"])
    out2 = capsys.readouterr().out
    assert "plan-a" in out2
    assert "plan-b" not in out2


def test_exceptional_states_are_never_hidden(monkeypatch, tmp_path, capsys):
    db_path = str(tmp_path / "exceptional.db")
    _env(monkeypatch, db_path)
    conn = init_db(db_path)
    try:
        _seed(conn, opportunity_uid="uid-unavail", plan_uid="plan-unavail",
              availability="UNAVAILABLE", state="NOT_EVALUABLE")
        _seed(conn, opportunity_uid="uid-ambig", plan_uid="plan-ambig", state="AMBIGUOUS")
        _seed(conn, opportunity_uid="uid-insuff", plan_uid="plan-insuff", state="INSUFFICIENT_DATA")
        _seed(conn, opportunity_uid="uid-expired", plan_uid="plan-expired", state="EXPIRED_UNENTERED")
    finally:
        close_db()

    report_trade_plans.main([])
    out = capsys.readouterr().out
    for plan_uid in ("plan-unavail", "plan-ambig", "plan-insuff", "plan-expired"):
        assert plan_uid in out
    assert "NOT_EVALUABLE" in out
    assert "AMBIGUOUS" in out
    assert "INSUFFICIENT_DATA" in out
    assert "EXPIRED_UNENTERED" in out


def test_readonly_connection_rejects_writes(monkeypatch, tmp_path):
    db_path = str(tmp_path / "readonly.db")
    _env(monkeypatch, db_path)
    init_db(db_path)
    close_db()

    ro_conn = report_trade_plans._open_readonly_connection(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro_conn.execute("INSERT INTO markets (symbol) VALUES ('XRP')")
    finally:
        ro_conn.close()


def test_never_calls_init_db(monkeypatch, tmp_path):
    """A database file that does not exist stays missing after running the
    report — confirms this script never calls init_db()/creates a DB.
    """
    db_path = str(tmp_path / "never_created.db")
    _env(monkeypatch, db_path)
    with pytest.raises(SystemExit):
        report_trade_plans.main([])
    assert not (tmp_path / "never_created.db").exists()
