"""Tests for scripts/report_opportunity_quality.py — read-only row loading,
pure report building/rendering, and the CLI. Every test uses a disposable
tmp_path SQLite database seeded directly via the repository layer; never the
production database, network, or a real clock.
"""
from __future__ import annotations

import ast
import inspect
import sqlite3
import sys

import pytest

from apex.db import repository as repo
from apex.db.connection import close_db, init_db
from apex.opportunity.contract import CONTRACT_VERSION, Opportunity
from apex.opportunity.quality_cohorts import QualityRow, build_quality_cohort_report
from apex.opportunity.trade_plan import TRADE_PLAN_CONTRACT_VERSION, TradePlan
from apex.opportunity.trade_plan_outcome import (
    TRADE_PLAN_OUTCOME_CONTRACT_VERSION,
    TradePlanOutcomeEvaluation,
)

sys.path.insert(0, "scripts")
import report_opportunity_quality as script


def _env(monkeypatch, db_path: str) -> None:
    monkeypatch.setenv("APEX_DB_PATH", db_path)
    monkeypatch.setenv("ACTION_TOKEN_SECRET", "test-report-secret")
    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "fake")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "fake")
    monkeypatch.setenv("ALERTS_ENABLED", "false")
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    monkeypatch.setenv("OPPORTUNITY_ENGINE_ENABLED", "false")


def _seed_opportunity(conn, *, uid: str, **overrides) -> None:
    base = {
        "opportunity_uid": uid,
        "fingerprint": f"fp-{uid}",
        "symbol": "BTC",
        "direction": "LONG",
        "setup_family": "SWEEP_RECLAIM",
        "detector_version": "sweep_reclaim_v0_1",
        "primary_timeframe": "5m",
        "first_detected_at": "2026-01-01T00:00:00Z",
        "last_seen_at": "2026-01-01T00:05:00Z",
        "source_candle_open_time": 1000,
        "source_candle_close_time": 1300,
        "anchor_price": 100.0,
        "anchor_open_time": 1000,
        "evidence_json": "{}",
        "warnings_json": "[]",
        "measurements_json": "{}",
    }
    base.update(overrides)
    repo.insert_opportunity(conn, Opportunity(**base))


def _seed_plan_and_outcome(
    conn,
    *,
    uid: str,
    symbol: str = "BTC",
    direction: str = "LONG",
    setup_family: str = "SWEEP_RECLAIM",
    primary_timeframe: str = "5m",
    availability: str = "AVAILABLE",
    entry: float = 100.0,
    stop: float = 90.0,
    t1: float = 110.0,
    t2: float = 120.0,
    not_before_ms: int = 1000,
    expiry_ms: int = 2000,
    outcome_state: str = "HIT_2R",
) -> None:
    plan = TradePlan(
        plan_uid=f"plan-{uid}",
        opportunity_uid=uid,
        symbol=symbol,
        direction=direction,
        setup_family=setup_family,
        primary_timeframe=primary_timeframe,
        detector_version="sweep_reclaim_v0_1",
        opportunity_contract_version=CONTRACT_VERSION,
        plan_contract_version=TRADE_PLAN_CONTRACT_VERSION,
        source_candle_open_time=1000,
        source_candle_close_time=1300,
        created_at="2026-01-01T00:05:00Z",
        availability=availability,
        unavailable_reason=None if availability == "AVAILABLE" else "NO_STRUCTURAL_INVALIDATION",
        entry_type="RESTING_LIMIT_AT_SOURCE_CLOSE" if availability == "AVAILABLE" else None,
        entry_price=entry if availability == "AVAILABLE" else None,
        invalidation_price=stop if availability == "AVAILABLE" else None,
        stop_price=stop if availability == "AVAILABLE" else None,
        risk_distance=abs(entry - stop) if availability == "AVAILABLE" else None,
        target_1r_price=t1 if availability == "AVAILABLE" else None,
        target_2r_price=t2 if availability == "AVAILABLE" else None,
        target_1r_multiple=1.0 if availability == "AVAILABLE" else None,
        target_2r_multiple=2.0 if availability == "AVAILABLE" else None,
        reward_risk_1r=1.0 if availability == "AVAILABLE" else None,
        reward_risk_2r=2.0 if availability == "AVAILABLE" else None,
        evaluation_not_before_ms=not_before_ms,
        evaluation_expiry_ms=expiry_ms,
        provenance_json="{}",
        warnings_json="[]",
    )
    outcome = TradePlanOutcomeEvaluation(
        plan_uid=plan.plan_uid,
        opportunity_uid=uid,
        contract_version=TRADE_PLAN_OUTCOME_CONTRACT_VERSION,
        state=outcome_state,
        is_terminal=True,
        last_evaluated_ms=5000,
        last_evaluated_open_time=1900,
        entry_open_time=1000,
        hit_1r_open_time=None,
        terminal_open_time=1900,
        terminal_reason="HORIZON_REACHED",
        mfe_r=1.0,
        mae_r=0.5,
        data_quality="COMPLETE",
        first_missing_boundary_ms=None,
        is_ambiguous=False,
        decisive_ohlc=None,
        crossed_levels=(),
        evidence_json="{}",
    )
    repo.insert_trade_plan_with_outcome(conn, plan, outcome)


# ---------------------------------------------------------------------------
# load_rows
# ---------------------------------------------------------------------------


def test_load_rows_joins_plan_and_outcome(tmp_path):
    db_path = str(tmp_path / "quality.db")
    conn = init_db(db_path)
    try:
        _seed_opportunity(conn, uid="uid-1")
        _seed_plan_and_outcome(conn, uid="uid-1", outcome_state="HIT_2R")
        rows = script.load_rows(conn)
    finally:
        close_db()

    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, QualityRow)
    assert row.opportunity_uid == "uid-1"
    assert row.plan_availability == "AVAILABLE"
    assert row.outcome_state == "HIT_2R"
    assert row.evaluation_not_before_ms == 1000
    assert row.evaluation_expiry_ms == 2000


def test_load_rows_represents_missing_plan_as_none_not_dropped(tmp_path):
    db_path = str(tmp_path / "quality.db")
    conn = init_db(db_path)
    try:
        _seed_opportunity(conn, uid="uid-legacy")
        rows = script.load_rows(conn)
    finally:
        close_db()

    assert len(rows) == 1
    row = rows[0]
    assert row.plan_availability is None
    assert row.outcome_state is None
    assert row.evaluation_not_before_ms is None


def test_load_rows_filters_by_symbol_family_timeframe_direction(tmp_path):
    db_path = str(tmp_path / "quality.db")
    conn = init_db(db_path)
    try:
        _seed_opportunity(conn, uid="uid-btc", symbol="BTC", setup_family="SWEEP_RECLAIM")
        _seed_plan_and_outcome(conn, uid="uid-btc", symbol="BTC")
        _seed_opportunity(conn, uid="uid-eth", symbol="ETH", setup_family="VOLATILITY_COMPRESSION")
        rows = script.load_rows(conn, symbol="BTC")
    finally:
        close_db()

    assert len(rows) == 1
    assert rows[0].opportunity_uid == "uid-btc"


# ---------------------------------------------------------------------------
# build_report / render_report (pure)
# ---------------------------------------------------------------------------


def _synthetic_row(uid: str, *, outcome_state: str, not_before_ms: int, expiry_ms: int) -> QualityRow:
    return QualityRow(
        opportunity_uid=uid,
        symbol="BTC",
        direction="LONG",
        setup_family="SWEEP_RECLAIM",
        primary_timeframe="5m",
        total_score=55.0,
        score_version="context_alignment_v0_1",
        plan_availability="AVAILABLE",
        evaluation_not_before_ms=not_before_ms,
        evaluation_expiry_ms=expiry_ms,
        outcome_state=outcome_state,
    )


def test_build_report_matches_quality_cohorts_builder():
    rows = [_synthetic_row("u1", outcome_state="HIT_2R", not_before_ms=0, expiry_ms=100)]
    report = script.build_report(rows, cost_r=0.05)
    expected = build_quality_cohort_report(rows, cost_r=0.05)
    assert report.overall.gross_mean_r == expected.overall.gross_mean_r
    assert report.overall.net_mean_r == expected.overall.net_mean_r


def test_render_report_is_deterministic_and_labels_missing_cost():
    rows = [_synthetic_row("u1", outcome_state="HIT_2R", not_before_ms=0, expiry_ms=100)]
    report = script.build_report(rows, cost_r=None)
    text_a = script.render_report(report)
    text_b = script.render_report(report)
    assert text_a == text_b
    assert "UNAVAILABLE" in text_a
    assert "EXACT QUALITY COHORT" in text_a
    assert "never an alert threshold" in text_a


def test_render_report_shows_qualification_status():
    rows = [_synthetic_row("u1", outcome_state="HIT_2R", not_before_ms=0, expiry_ms=100)]
    report = script.build_report(rows, cost_r=0.0)
    text = script.render_report(report)
    assert "DOES NOT QUALIFY" in text  # only 1 resolved cluster, far below the floor


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_missing_db_file_exits_nonzero_and_never_creates_one(monkeypatch, tmp_path, capsys):
    db_path = str(tmp_path / "does_not_exist.db")
    _env(monkeypatch, db_path)
    with pytest.raises(SystemExit) as exc_info:
        script.main([])
    assert exc_info.value.code == 1
    assert not (tmp_path / "does_not_exist.db").exists()


def test_cli_missing_tables_reports_gracefully(monkeypatch, tmp_path, capsys):
    db_path = str(tmp_path / "empty.db")
    _env(monkeypatch, db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE some_other_table (id INTEGER)")
    conn.commit()
    conn.close()

    script.main([])
    out = capsys.readouterr().out
    assert "Missing table" in out


def test_cli_invalid_cost_r_exits_nonzero(monkeypatch, tmp_path, capsys):
    db_path = str(tmp_path / "quality.db")
    _env(monkeypatch, db_path)
    init_db(db_path)
    close_db()

    with pytest.raises(SystemExit) as exc_info:
        script.main(["--cost-r", "-1.0"])
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "--cost-r" in err


def test_cli_fee_without_slippage_exits_nonzero(monkeypatch, tmp_path):
    db_path = str(tmp_path / "quality.db")
    _env(monkeypatch, db_path)
    init_db(db_path)
    close_db()

    with pytest.raises(SystemExit):
        script.main(["--fee-r", "0.01"])


def test_cli_end_to_end_prints_cost_adjusted_report(monkeypatch, tmp_path, capsys):
    db_path = str(tmp_path / "quality.db")
    _env(monkeypatch, db_path)
    conn = init_db(db_path)
    try:
        _seed_opportunity(conn, uid="uid-1")
        _seed_plan_and_outcome(conn, uid="uid-1", outcome_state="HIT_2R")
    finally:
        close_db()

    script.main(["--fee-r", "0.02", "--slippage-r", "0.01"])
    out = capsys.readouterr().out
    assert "0.0300" in out  # combined cost echoed
    assert "SWEEP_RECLAIM" in out


def test_cli_never_writes_to_database(monkeypatch, tmp_path):
    """Sanity check that the read-only connection genuinely refuses writes:
    PRAGMA query_only=ON must reject any accidental write this script's own
    read path might attempt."""
    db_path = str(tmp_path / "quality.db")
    _env(monkeypatch, db_path)
    init_db(db_path)
    close_db()

    ro_conn = script._open_readonly_connection(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro_conn.execute("INSERT INTO opportunity_observations (opportunity_uid) VALUES ('x')")
    finally:
        ro_conn.close()


# ---------------------------------------------------------------------------
# Transport isolation
# ---------------------------------------------------------------------------


def test_script_has_no_static_notifications_import():
    source = inspect.getsource(script)
    tree = ast.parse(source)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    for module in imported_modules:
        assert module != "apex.notifications" and not module.startswith("apex.notifications.")


def test_report_still_works_when_notifications_import_is_poisoned(monkeypatch, tmp_path, capsys):
    import importlib

    class _BoomFinder:
        def find_module(self, fullname, path=None):
            if fullname == "apex.notifications" or fullname.startswith("apex.notifications."):
                raise AssertionError(f"must never import {fullname!r}")

    finder = _BoomFinder()
    sys.meta_path.insert(0, finder)
    try:
        for name in list(sys.modules):
            if name == "apex.notifications" or name.startswith("apex.notifications."):
                del sys.modules[name]

        db_path = str(tmp_path / "quality.db")
        _env(monkeypatch, db_path)
        conn = init_db(db_path)
        try:
            _seed_opportunity(conn, uid="uid-1")
            _seed_plan_and_outcome(conn, uid="uid-1", outcome_state="HIT_2R")
        finally:
            close_db()

        importlib.reload(script)
        script.main([])
        out = capsys.readouterr().out
        assert "SWEEP_RECLAIM" in out
    finally:
        sys.meta_path.remove(finder)
        importlib.reload(script)
