"""Tests for scripts/report_shadow_alerts.py — read-only decision loading,
pure summary building/rendering, and the CLI. Every test uses a disposable
tmp_path SQLite database seeded directly via the repository layer; never the
production database, network, or a real clock.
"""
from __future__ import annotations

import ast
import inspect
import json
import sqlite3
import sys

import pytest

from apex.db import repository as repo
from apex.db.connection import close_db, init_db

sys.path.insert(0, "scripts")
import report_shadow_alerts as script

PILOT_ID = "pilot-001"


def _env(monkeypatch, db_path: str, pilot_id: str = "") -> None:
    monkeypatch.setenv("APEX_DB_PATH", db_path)
    monkeypatch.setenv("ACTION_TOKEN_SECRET", "test-report-secret")
    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "fake")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "fake")
    monkeypatch.setenv("ALERTS_ENABLED", "false")
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    monkeypatch.setenv("OPPORTUNITY_ENGINE_ENABLED", "false")
    monkeypatch.setenv("SHADOW_ALERT_PILOT_ID", pilot_id)


def _decision(
    *, run_at, opportunity_uid, pilot_id=PILOT_ID, symbol="BTC", direction="LONG",
    setup_family="SWEEP_RECLAIM", timeframe="5m", would_emit=False,
    reason_codes=None, message=None,
) -> repo.ShadowDecisionRecord:
    reason_codes = reason_codes if reason_codes is not None else (["EMIT_ELIGIBLE"] if would_emit else ["STALE_OPPORTUNITY"])
    return repo.ShadowDecisionRecord(
        pilot_id=pilot_id,
        evaluator_version="shadow_alerts_v0_1",
        runtime_version="shadow_alert_runtime_v0_1",
        run_at=run_at,
        opportunity_uid=opportunity_uid,
        symbol=symbol,
        direction=direction,
        setup_family=setup_family,
        primary_timeframe=timeframe,
        evaluation_not_before_ms=1000,
        evaluation_expiry_ms=2000,
        entry_price=100.0,
        stop_price=90.0,
        target_1r_price=110.0,
        target_2r_price=120.0,
        market_snapshot_json=None,
        cost_fee_r=0.02,
        cost_slippage_r=0.01,
        cost_total_r=0.03,
        cohort_evidence_json=None,
        would_emit=would_emit,
        reason_codes_json=json.dumps(reason_codes),
        message=message,
        rule_evidence_json="{}",
    )


# ---------------------------------------------------------------------------
# load_decisions
# ---------------------------------------------------------------------------


def test_load_decisions_filters_by_pilot_id(tmp_path):
    db_path = str(tmp_path / "shadow.db")
    conn = init_db(db_path)
    try:
        repo.insert_shadow_decisions(
            conn,
            [
                _decision(run_at="2026-01-01T00:00:00.000Z", opportunity_uid="a", pilot_id="pilot-A"),
                _decision(run_at="2026-01-01T00:00:00.000Z", opportunity_uid="b", pilot_id="pilot-B"),
            ],
        )
        rows = script.load_decisions(conn, "pilot-A")
    finally:
        close_db()
    assert len(rows) == 1
    assert rows[0]["opportunity_uid"] == "a"


def test_load_decisions_never_loads_more_than_bound_plus_one(monkeypatch, tmp_path):
    """Bounded read: monkeypatches MAX_REPORT_DECISIONS to a small value
    (rather than seeding a real MAX_REPORT_DECISIONS-sized batch) to prove
    load_decisions fetches at most bound+1 rows — exactly one over the bound,
    never silently truncated to the bound itself or left unbounded.
    """
    db_path = str(tmp_path / "shadow.db")
    conn = init_db(db_path)
    try:
        monkeypatch.setattr(script, "MAX_REPORT_DECISIONS", 2)
        repo.insert_shadow_decisions(
            conn,
            [
                _decision(run_at=f"2026-01-01T00:0{i}:00.000Z", opportunity_uid=f"u{i}")
                for i in range(5)
            ],
        )
        rows = script.load_decisions(conn, PILOT_ID)
    finally:
        close_db()
    assert len(rows) == 3


def test_load_decisions_joins_current_outcome_state(tmp_path):
    from apex.opportunity.contract import CONTRACT_VERSION, Opportunity
    from apex.opportunity.trade_plan import TRADE_PLAN_CONTRACT_VERSION, TradePlan
    from apex.opportunity.trade_plan_outcome import (
        TRADE_PLAN_OUTCOME_CONTRACT_VERSION,
        TradePlanOutcomeEvaluation,
    )

    db_path = str(tmp_path / "shadow.db")
    conn = init_db(db_path)
    try:
        repo.insert_opportunity(
            conn,
            Opportunity(
                opportunity_uid="uid-1", fingerprint="fp-1", symbol="BTC", direction="LONG",
                setup_family="SWEEP_RECLAIM", detector_version="v", primary_timeframe="5m",
                first_detected_at="2026-01-01T00:00:00Z", last_seen_at="2026-01-01T00:00:00Z",
                source_candle_open_time=1000, source_candle_close_time=1300,
                evidence_json="{}", warnings_json="[]", measurements_json="{}",
            ),
        )
        plan = TradePlan(
            plan_uid="plan-1", opportunity_uid="uid-1", symbol="BTC", direction="LONG",
            setup_family="SWEEP_RECLAIM", primary_timeframe="5m", detector_version="v",
            opportunity_contract_version=CONTRACT_VERSION, plan_contract_version=TRADE_PLAN_CONTRACT_VERSION,
            source_candle_open_time=1000, source_candle_close_time=1300, created_at="2026-01-01T00:00:00Z",
            availability="AVAILABLE", unavailable_reason=None, entry_type="RESTING_LIMIT_AT_SOURCE_CLOSE",
            entry_price=100.0, invalidation_price=90.0, stop_price=90.0, risk_distance=10.0,
            target_1r_price=110.0, target_2r_price=120.0, target_1r_multiple=1.0, target_2r_multiple=2.0,
            reward_risk_1r=1.0, reward_risk_2r=2.0, evaluation_not_before_ms=1000, evaluation_expiry_ms=2000,
            provenance_json="{}", warnings_json="[]",
        )
        outcome = TradePlanOutcomeEvaluation(
            plan_uid="plan-1", opportunity_uid="uid-1", contract_version=TRADE_PLAN_OUTCOME_CONTRACT_VERSION,
            state="HIT_2R", is_terminal=True, last_evaluated_ms=2000, last_evaluated_open_time=1900,
            entry_open_time=1000, hit_1r_open_time=None, terminal_open_time=1900, terminal_reason="HORIZON_REACHED",
            mfe_r=2.0, mae_r=0.0, data_quality="COMPLETE", first_missing_boundary_ms=None, is_ambiguous=False,
            decisive_ohlc=None, crossed_levels=(), evidence_json="{}",
        )
        repo.insert_trade_plan_with_outcome(conn, plan, outcome)
        repo.insert_shadow_decisions(
            conn, [_decision(run_at="2026-01-01T00:00:00.000Z", opportunity_uid="uid-1", would_emit=True)]
        )
        rows = script.load_decisions(conn, PILOT_ID)
    finally:
        close_db()
    assert len(rows) == 1
    assert rows[0]["current_outcome_state"] == "HIT_2R"


def test_load_decisions_no_outcome_row_is_none(tmp_path):
    db_path = str(tmp_path / "shadow.db")
    conn = init_db(db_path)
    try:
        repo.insert_shadow_decisions(
            conn, [_decision(run_at="2026-01-01T00:00:00.000Z", opportunity_uid="orphan")]
        )
        rows = script.load_decisions(conn, PILOT_ID)
    finally:
        close_db()
    assert rows[0]["current_outcome_state"] is None


# ---------------------------------------------------------------------------
# build_summary / render_report (pure)
# ---------------------------------------------------------------------------


def _row(**kwargs) -> sqlite3.Row:
    """Build a real sqlite3.Row (build_summary indexes by column name) via
    a throwaway in-memory connection with the exact shape build_summary and
    render_report need.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE t (run_at TEXT, opportunity_uid TEXT, would_emit INTEGER, "
        "reason_codes_json TEXT, symbol TEXT, direction TEXT, setup_family TEXT, "
        "primary_timeframe TEXT, current_outcome_state TEXT)"
    )
    base = dict(
        run_at="2026-01-01T00:00:00.000Z", opportunity_uid="uid", would_emit=0,
        reason_codes_json="[]", symbol="BTC", direction="LONG", setup_family="SWEEP_RECLAIM",
        primary_timeframe="5m", current_outcome_state=None,
    )
    base.update(kwargs)
    conn.execute(
        "INSERT INTO t VALUES (?,?,?,?,?,?,?,?,?)",
        (
            base["run_at"], base["opportunity_uid"], base["would_emit"], base["reason_codes_json"],
            base["symbol"], base["direction"], base["setup_family"], base["primary_timeframe"],
            base["current_outcome_state"],
        ),
    )
    conn.commit()
    return conn.execute("SELECT * FROM t").fetchone()


def test_build_summary_counts_runs_decisions_and_emit_suppressed():
    rows = [
        _row(run_at="r1", opportunity_uid="a", would_emit=1, reason_codes_json=json.dumps(["EMIT_ELIGIBLE"])),
        _row(run_at="r1", opportunity_uid="b", would_emit=0, reason_codes_json=json.dumps(["COOLDOWN_ACTIVE"])),
        _row(run_at="r2", opportunity_uid="a", would_emit=0, reason_codes_json=json.dumps(["OPPORTUNITY_ALREADY_EMITTED"])),
    ]
    summary = script.build_summary(rows)
    assert summary["total_runs"] == 2
    assert summary["total_decisions"] == 3
    assert summary["unique_candidates"] == 2
    assert summary["would_emit"] == 1
    assert summary["suppressed"] == 2
    assert summary["reason_counts"]["EMIT_ELIGIBLE"] == 1
    assert summary["reason_counts"]["COOLDOWN_ACTIVE"] == 1
    assert summary["cooldown_count"] == 1
    assert summary["duplicate_count"] == 1


def test_build_summary_groups_stale_conflict_cap_cost_quality_reasons():
    rows = [
        _row(opportunity_uid="a", reason_codes_json=json.dumps(["STALE_OPPORTUNITY"])),
        _row(opportunity_uid="b", reason_codes_json=json.dumps(["MARKET_SNAPSHOT_NOT_CURRENT"])),
        _row(opportunity_uid="c", reason_codes_json=json.dumps(["OPPOSITE_DIRECTION_CONFLICT"])),
        _row(opportunity_uid="d", reason_codes_json=json.dumps(["ROLLING_HOUR_CAP_REACHED"])),
        _row(opportunity_uid="e", reason_codes_json=json.dumps(["ROLLING_DAY_CAP_REACHED"])),
        _row(opportunity_uid="f", reason_codes_json=json.dumps(["MARKET_BAR_TOUCHED_ENTRY"])),
        _row(opportunity_uid="g", reason_codes_json=json.dumps(["COST_TOO_HIGH"])),
        _row(opportunity_uid="h", reason_codes_json=json.dumps(["COHORT_INSUFFICIENT_RESOLVED_CLUSTERS"])),
        _row(opportunity_uid="i", reason_codes_json=json.dumps(["COHORT_EVIDENCE_MISSING"])),
    ]
    summary = script.build_summary(rows)
    assert summary["stale_count"] == 2
    assert summary["conflict_count"] == 1
    assert summary["cap_count"] == 2
    assert summary["touched_or_out_of_range_count"] == 1
    assert summary["cost_failure_count"] == 1
    assert summary["quality_gate_failure_count"] == 2


def test_build_summary_concentration_breakdowns():
    rows = [
        _row(opportunity_uid="a", symbol="BTC", direction="LONG", setup_family="SWEEP_RECLAIM", primary_timeframe="5m"),
        _row(opportunity_uid="b", symbol="BTC", direction="SHORT", setup_family="SWEEP_RECLAIM", primary_timeframe="5m"),
        _row(opportunity_uid="c", symbol="ETH", direction="LONG", setup_family="VOLATILITY_COMPRESSION", primary_timeframe="3m"),
    ]
    summary = script.build_summary(rows)
    assert summary["by_symbol"]["BTC"] == 2
    assert summary["by_symbol"]["ETH"] == 1
    assert summary["by_direction"]["LONG"] == 2
    assert summary["by_direction"]["SHORT"] == 1
    assert summary["by_family"]["SWEEP_RECLAIM"] == 2
    assert summary["by_timeframe"]["5m"] == 2
    assert summary["by_cohort"]["SWEEP_RECLAIM:5m:LONG"] == 1


def test_build_summary_outcome_state_honestly_labels_missing_outcome():
    rows = [
        _row(opportunity_uid="a", current_outcome_state="HIT_2R"),
        _row(opportunity_uid="b", current_outcome_state=None),
    ]
    summary = script.build_summary(rows)
    assert summary["by_current_outcome_state"]["HIT_2R"] == 1
    assert summary["by_current_outcome_state"]["NO_OUTCOME_ROW"] == 1


def test_build_summary_malformed_reason_codes_json_is_counted_not_silently_empty():
    rows = [_row(opportunity_uid="a", reason_codes_json="not-json")]
    summary = script.build_summary(rows)
    assert summary["total_decisions"] == 1
    assert summary["reason_counts"] == {}
    assert summary["malformed_reason_evidence_count"] == 1


def test_build_summary_non_list_reason_codes_json_is_counted_malformed():
    rows = [_row(opportunity_uid="a", reason_codes_json=json.dumps({"not": "a list"}))]
    summary = script.build_summary(rows)
    assert summary["reason_counts"] == {}
    assert summary["malformed_reason_evidence_count"] == 1


def test_build_summary_non_string_reason_code_element_is_counted_malformed():
    rows = [_row(opportunity_uid="a", reason_codes_json=json.dumps(["EMIT_ELIGIBLE", 42]))]
    summary = script.build_summary(rows)
    assert summary["reason_counts"] == {}
    assert summary["malformed_reason_evidence_count"] == 1


def test_build_summary_well_formed_reason_codes_never_counted_malformed():
    rows = [_row(opportunity_uid="a", reason_codes_json=json.dumps(["EMIT_ELIGIBLE"]))]
    summary = script.build_summary(rows)
    assert summary["reason_counts"] == {"EMIT_ELIGIBLE": 1}
    assert summary["malformed_reason_evidence_count"] == 0


def test_render_report_is_deterministic():
    rows = [_row(opportunity_uid="a", would_emit=1, reason_codes_json=json.dumps(["EMIT_ELIGIBLE"]))]
    summary = script.build_summary(rows)
    text_a = script.render_report(PILOT_ID, summary)
    text_b = script.render_report(PILOT_ID, summary)
    assert text_a == text_b
    assert PILOT_ID in text_a
    assert "LIMITATIONS" in text_a
    assert "never shows a fill" in text_a
    assert "never issues a recommendation" in text_a


def test_render_report_shows_malformed_reason_evidence_count():
    rows = [
        _row(opportunity_uid="a", reason_codes_json="not-json"),
        _row(opportunity_uid="b", reason_codes_json=json.dumps(["EMIT_ELIGIBLE"]), would_emit=1),
    ]
    summary = script.build_summary(rows)
    text = script.render_report(PILOT_ID, summary)
    assert "malformed reason evidence   = 1" in text


def test_render_report_never_invents_a_fill_or_recommendation():
    rows = [_row(opportunity_uid="a", would_emit=1, reason_codes_json=json.dumps(["EMIT_ELIGIBLE"]))]
    summary = script.build_summary(rows)
    text = script.render_report(PILOT_ID, summary)
    for forbidden in ("fill_price", "pnl", "buy now", "sell now", "should enter"):
        assert forbidden not in text.lower()
    assert "recommendation is ever computed" in text


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_missing_db_file_exits_nonzero_and_never_creates_one(monkeypatch, tmp_path):
    db_path = str(tmp_path / "does_not_exist.db")
    _env(monkeypatch, db_path, pilot_id=PILOT_ID)
    with pytest.raises(SystemExit) as exc_info:
        script.main([])
    assert exc_info.value.code == 1
    assert not (tmp_path / "does_not_exist.db").exists()


def test_cli_no_pilot_id_exits_nonzero(monkeypatch, tmp_path, capsys):
    db_path = str(tmp_path / "shadow.db")
    init_db(db_path)
    close_db()
    _env(monkeypatch, db_path, pilot_id="")
    with pytest.raises(SystemExit) as exc_info:
        script.main([])
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "--pilot-id" in err


def test_cli_missing_tables_reports_gracefully(monkeypatch, tmp_path, capsys):
    db_path = str(tmp_path / "empty.db")
    _env(monkeypatch, db_path, pilot_id=PILOT_ID)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE some_other_table (id INTEGER)")
    conn.commit()
    conn.close()

    script.main([])
    out = capsys.readouterr().out
    assert "Missing table" in out


def test_cli_end_to_end_prints_summary(monkeypatch, tmp_path, capsys):
    db_path = str(tmp_path / "shadow.db")
    _env(monkeypatch, db_path, pilot_id=PILOT_ID)
    conn = init_db(db_path)
    try:
        repo.insert_shadow_decisions(
            conn,
            [
                _decision(run_at="2026-01-01T00:00:00.000Z", opportunity_uid="a", would_emit=True),
                _decision(run_at="2026-01-01T00:01:00.000Z", opportunity_uid="b", would_emit=False),
            ],
        )
    finally:
        close_db()

    script.main([])
    out = capsys.readouterr().out
    assert "would_emit=1" in out
    assert "suppressed=1" in out
    assert "SWEEP_RECLAIM" in out


def test_cli_explicit_pilot_id_overrides_settings(monkeypatch, tmp_path, capsys):
    db_path = str(tmp_path / "shadow.db")
    _env(monkeypatch, db_path, pilot_id="configured-pilot")
    conn = init_db(db_path)
    try:
        repo.insert_shadow_decisions(
            conn, [_decision(run_at="2026-01-01T00:00:00.000Z", opportunity_uid="a", pilot_id="explicit-pilot")]
        )
    finally:
        close_db()

    script.main(["--pilot-id", "explicit-pilot"])
    out = capsys.readouterr().out
    assert "explicit-pilot" in out
    assert "total_decisions=1" in out


def test_cli_exact_bound_decision_count_succeeds(monkeypatch, tmp_path, capsys):
    """Monkeypatches MAX_REPORT_DECISIONS to a small value so an exact-bound
    row count can be tested without seeding a real MAX_REPORT_DECISIONS-sized
    batch — a decision count exactly AT the bound must still render normally.
    """
    db_path = str(tmp_path / "shadow.db")
    _env(monkeypatch, db_path, pilot_id=PILOT_ID)
    monkeypatch.setattr(script, "MAX_REPORT_DECISIONS", 3)
    conn = init_db(db_path)
    try:
        repo.insert_shadow_decisions(
            conn,
            [
                _decision(run_at=f"2026-01-01T00:0{i}:00.000Z", opportunity_uid=f"u{i}")
                for i in range(3)
            ],
        )
    finally:
        close_db()

    script.main([])
    out = capsys.readouterr().out
    assert "total_decisions=3" in out


def test_cli_overflow_decision_count_fails_closed_nonzero_exit(monkeypatch, tmp_path, capsys):
    """One row over the (monkeypatched, small) bound must fail the whole
    report closed with a clear nonzero exit — never a silently truncated
    report."""
    db_path = str(tmp_path / "shadow.db")
    _env(monkeypatch, db_path, pilot_id=PILOT_ID)
    monkeypatch.setattr(script, "MAX_REPORT_DECISIONS", 3)
    conn = init_db(db_path)
    try:
        repo.insert_shadow_decisions(
            conn,
            [
                _decision(run_at=f"2026-01-01T00:0{i}:00.000Z", opportunity_uid=f"u{i}")
                for i in range(4)
            ],
        )
    finally:
        close_db()

    with pytest.raises(SystemExit) as exc_info:
        script.main([])
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "more than 3 decision rows" in err
    out = capsys.readouterr().out
    assert out == ""  # never a partial/truncated report on stdout


def test_cli_never_writes_to_database(monkeypatch, tmp_path):
    """Sanity check that the read-only connection genuinely refuses writes:
    PRAGMA query_only=ON must reject any accidental write this script's own
    read path might attempt."""
    db_path = str(tmp_path / "shadow.db")
    init_db(db_path)
    close_db()

    ro_conn = script._open_readonly_connection(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro_conn.execute(
                "INSERT INTO shadow_alert_decisions (pilot_id, evaluator_version, runtime_version, "
                "run_at, opportunity_uid, symbol, direction, setup_family, primary_timeframe, "
                "would_emit, reason_codes_json, rule_evidence_json) "
                "VALUES ('x','x','x','x','x','x','LONG','x','x',0,'[]','{}')"
            )
    finally:
        ro_conn.close()


# ---------------------------------------------------------------------------
# Transport/notification isolation
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
        assert module != "httpx" and not module.startswith("httpx.")
