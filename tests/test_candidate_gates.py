"""Tests for Milestone 11C: Candidate Gate Evaluation and Prospective Tracking.

Covers:
- Gate A (ATR): boundary values, missing feature
- Gate B (EMA spread): boundary values, missing feature
- Gate C (RSI): boundary values, missing feature
- Gate D (combined ATR + EMA): passes only when both A and B pass
- Missing/null features fail safely without crashing
- All gates missing → fail safely with reason strings
- Gate metadata is stored in signal_features.metadata_json
- feature_version is '11C_v1' for new observations
- Candidate gate result does not suppress observation creation
- Report script handles empty 11C data gracefully
- Snapshot includes Candidate Gate Report section
- Report script is importable via direct file path
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from apex.db import repository as repo
from apex.db.connection import close_db, init_db
from apex.db.models import SignalFeature, SignalObservation
from apex.strategy.candidate_gates import (
    GATE_VERSION,
    evaluate_candidate_gates,
)
from apex.utils.ids import new_uid
from apex.utils.time import minutes_from_now, utcnow_iso


# ------------------------------------------------------------------ helpers

def _make_env(monkeypatch, db_path: str) -> None:
    monkeypatch.setenv("APEX_DB_PATH", db_path)
    monkeypatch.setenv("ACTION_TOKEN_SECRET", "test-11c")
    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "fake")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "fake")
    monkeypatch.setenv("ALERTS_ENABLED", "false")
    monkeypatch.setenv("DRY_RUN_MODE", "true")


def _insert_obs(conn, direction: str = "LONG") -> str:
    uid = new_uid()
    obs = SignalObservation(
        observation_uid=uid,
        observed_at=utcnow_iso(),
        symbol="BTC",
        direction=direction,
        signal_type="CONFIRMED_SETUP",
        entry_price=100.0,
        stop_price=99.0,
        target_1r=101.0,
        target_2r=102.0,
        expires_at=minutes_from_now(15),
    )
    repo.insert_signal_observation(conn, obs)
    return uid


def _insert_feature_with_gates(
    conn,
    uid: str,
    atr_pct: float = 0.30,
    ema_spread_pct: float = -0.10,
    rsi_val: float = 55.0,
) -> None:
    gate_result = evaluate_candidate_gates(
        atr_pct=atr_pct,
        ema_spread_pct=ema_spread_pct,
        rsi_val=rsi_val,
    )
    feat = SignalFeature(
        observation_uid=uid,
        captured_at=utcnow_iso(),
        symbol="BTC",
        direction="LONG",
        signal_type="CONFIRMED_SETUP",
        observed_at=utcnow_iso(),
        entry_price=100.0,
        stop_price=99.0,
        target_1r=101.0,
        target_2r=102.0,
        rsi_val=rsi_val,
        atr_pct=atr_pct,
        ema_spread_pct=ema_spread_pct,
        metadata_json=json.dumps(gate_result),
    )
    repo.insert_signal_feature(conn, feat)


def _close_hit2r(conn, uid: str) -> None:
    now = utcnow_iso()
    repo.close_signal_observation(
        conn, uid,
        status="HIT_2R", outcome_r=2.0, closed_at=now,
        hit_1r_at=now, time_to_1r_seconds=120.0,
        hit_2r_at=now, time_to_2r_seconds=240.0,
        first_terminal_status="HIT_2R", final_status="HIT_2R",
    )
    repo.update_signal_feature_outcome_from_observation(conn, uid)


def _run_gate_report(argv=None):
    import scripts.report_candidate_gates as mod
    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main(argv or [])
    return buf.getvalue()


# ================================================================== unit tests: evaluate_candidate_gates

# ------------------------------------------------------------------ Gate A: ATR boundaries

def test_gate_a_passes_at_lower_boundary():
    result = evaluate_candidate_gates(atr_pct=0.10, ema_spread_pct=None, rsi_val=None)
    assert result["gates"]["GATE_A_ATR_0_10_TO_1_00"]["passed"] is True


def test_gate_a_passes_at_upper_boundary():
    result = evaluate_candidate_gates(atr_pct=1.00, ema_spread_pct=None, rsi_val=None)
    assert result["gates"]["GATE_A_ATR_0_10_TO_1_00"]["passed"] is True


def test_gate_a_passes_midrange():
    result = evaluate_candidate_gates(atr_pct=0.50, ema_spread_pct=None, rsi_val=None)
    assert result["gates"]["GATE_A_ATR_0_10_TO_1_00"]["passed"] is True


def test_gate_a_fails_below_lower_boundary():
    result = evaluate_candidate_gates(atr_pct=0.09, ema_spread_pct=None, rsi_val=None)
    assert result["gates"]["GATE_A_ATR_0_10_TO_1_00"]["passed"] is False


def test_gate_a_fails_above_upper_boundary():
    result = evaluate_candidate_gates(atr_pct=1.01, ema_spread_pct=None, rsi_val=None)
    assert result["gates"]["GATE_A_ATR_0_10_TO_1_00"]["passed"] is False


def test_gate_a_fails_on_missing_atr():
    result = evaluate_candidate_gates(atr_pct=None, ema_spread_pct=None, rsi_val=None)
    g = result["gates"]["GATE_A_ATR_0_10_TO_1_00"]
    assert g["passed"] is False
    assert "missing" in g["reason"]


# ------------------------------------------------------------------ Gate B: EMA spread boundaries

def test_gate_b_passes_at_lower_boundary():
    result = evaluate_candidate_gates(atr_pct=None, ema_spread_pct=-0.25, rsi_val=None)
    assert result["gates"]["GATE_B_EMA_SPREAD_NEG_0_25_TO_0"]["passed"] is True


def test_gate_b_passes_just_below_zero():
    result = evaluate_candidate_gates(atr_pct=None, ema_spread_pct=-0.01, rsi_val=None)
    assert result["gates"]["GATE_B_EMA_SPREAD_NEG_0_25_TO_0"]["passed"] is True


def test_gate_b_fails_at_zero():
    """Gate B is exclusive at 0 (-0.25 <= x < 0)."""
    result = evaluate_candidate_gates(atr_pct=None, ema_spread_pct=0.0, rsi_val=None)
    assert result["gates"]["GATE_B_EMA_SPREAD_NEG_0_25_TO_0"]["passed"] is False


def test_gate_b_fails_positive():
    result = evaluate_candidate_gates(atr_pct=None, ema_spread_pct=0.10, rsi_val=None)
    assert result["gates"]["GATE_B_EMA_SPREAD_NEG_0_25_TO_0"]["passed"] is False


def test_gate_b_fails_below_neg_0_25():
    result = evaluate_candidate_gates(atr_pct=None, ema_spread_pct=-0.30, rsi_val=None)
    assert result["gates"]["GATE_B_EMA_SPREAD_NEG_0_25_TO_0"]["passed"] is False


def test_gate_b_fails_on_missing_ema_spread():
    result = evaluate_candidate_gates(atr_pct=None, ema_spread_pct=None, rsi_val=None)
    g = result["gates"]["GATE_B_EMA_SPREAD_NEG_0_25_TO_0"]
    assert g["passed"] is False
    assert "missing" in g["reason"]


# ------------------------------------------------------------------ Gate C: RSI boundaries

def test_gate_c_passes_at_lower_boundary():
    result = evaluate_candidate_gates(atr_pct=None, ema_spread_pct=None, rsi_val=50.0)
    assert result["gates"]["GATE_C_RSI_50_TO_60"]["passed"] is True


def test_gate_c_passes_at_upper_boundary():
    result = evaluate_candidate_gates(atr_pct=None, ema_spread_pct=None, rsi_val=60.0)
    assert result["gates"]["GATE_C_RSI_50_TO_60"]["passed"] is True


def test_gate_c_passes_midrange():
    result = evaluate_candidate_gates(atr_pct=None, ema_spread_pct=None, rsi_val=55.0)
    assert result["gates"]["GATE_C_RSI_50_TO_60"]["passed"] is True


def test_gate_c_fails_below_50():
    result = evaluate_candidate_gates(atr_pct=None, ema_spread_pct=None, rsi_val=49.9)
    assert result["gates"]["GATE_C_RSI_50_TO_60"]["passed"] is False


def test_gate_c_fails_above_60():
    result = evaluate_candidate_gates(atr_pct=None, ema_spread_pct=None, rsi_val=60.1)
    assert result["gates"]["GATE_C_RSI_50_TO_60"]["passed"] is False


def test_gate_c_fails_on_missing_rsi():
    result = evaluate_candidate_gates(atr_pct=None, ema_spread_pct=None, rsi_val=None)
    g = result["gates"]["GATE_C_RSI_50_TO_60"]
    assert g["passed"] is False
    assert "missing" in g["reason"]


# ------------------------------------------------------------------ Gate D: combined

def test_gate_d_passes_when_both_a_and_b_pass():
    result = evaluate_candidate_gates(atr_pct=0.50, ema_spread_pct=-0.10, rsi_val=None)
    assert result["gates"]["GATE_D_ATR_AND_EMA_COMBINED"]["passed"] is True


def test_gate_d_fails_when_only_a_passes():
    result = evaluate_candidate_gates(atr_pct=0.50, ema_spread_pct=0.10, rsi_val=None)
    assert result["gates"]["GATE_D_ATR_AND_EMA_COMBINED"]["passed"] is False


def test_gate_d_fails_when_only_b_passes():
    result = evaluate_candidate_gates(atr_pct=0.05, ema_spread_pct=-0.10, rsi_val=None)
    assert result["gates"]["GATE_D_ATR_AND_EMA_COMBINED"]["passed"] is False


def test_gate_d_fails_when_neither_passes():
    result = evaluate_candidate_gates(atr_pct=0.05, ema_spread_pct=0.10, rsi_val=None)
    assert result["gates"]["GATE_D_ATR_AND_EMA_COMBINED"]["passed"] is False


def test_gate_d_fails_on_missing_atr():
    result = evaluate_candidate_gates(atr_pct=None, ema_spread_pct=-0.10, rsi_val=None)
    g = result["gates"]["GATE_D_ATR_AND_EMA_COMBINED"]
    assert g["passed"] is False
    assert "missing" in g["reason"]


def test_gate_d_fails_on_missing_ema_spread():
    result = evaluate_candidate_gates(atr_pct=0.50, ema_spread_pct=None, rsi_val=None)
    g = result["gates"]["GATE_D_ATR_AND_EMA_COMBINED"]
    assert g["passed"] is False
    assert "missing" in g["reason"]


def test_gate_d_fails_on_both_missing():
    result = evaluate_candidate_gates(atr_pct=None, ema_spread_pct=None, rsi_val=None)
    g = result["gates"]["GATE_D_ATR_AND_EMA_COMBINED"]
    assert g["passed"] is False
    assert "missing" in g["reason"]


# ------------------------------------------------------------------ Result structure

def test_result_has_gate_version():
    result = evaluate_candidate_gates(atr_pct=0.50, ema_spread_pct=-0.10, rsi_val=55.0)
    assert result["candidate_gate_version"] == GATE_VERSION


def test_result_candidate_gate_passed_true_when_any_passes():
    # All three pass
    result = evaluate_candidate_gates(atr_pct=0.50, ema_spread_pct=-0.10, rsi_val=55.0)
    assert result["candidate_gate_passed"] is True


def test_result_candidate_gate_passed_false_when_none_pass():
    result = evaluate_candidate_gates(atr_pct=None, ema_spread_pct=None, rsi_val=None)
    assert result["candidate_gate_passed"] is False


def test_result_has_all_four_gate_keys():
    result = evaluate_candidate_gates(atr_pct=0.50, ema_spread_pct=-0.10, rsi_val=55.0)
    expected = {
        "GATE_A_ATR_0_10_TO_1_00",
        "GATE_B_EMA_SPREAD_NEG_0_25_TO_0",
        "GATE_C_RSI_50_TO_60",
        "GATE_D_ATR_AND_EMA_COMBINED",
    }
    assert set(result["gates"].keys()) == expected


def test_result_is_json_serialisable():
    result = evaluate_candidate_gates(atr_pct=0.50, ema_spread_pct=-0.10, rsi_val=55.0)
    serialised = json.dumps(result)
    parsed = json.loads(serialised)
    assert parsed["candidate_gate_version"] == GATE_VERSION


def test_result_reason_strings_are_present():
    result = evaluate_candidate_gates(atr_pct=0.50, ema_spread_pct=-0.10, rsi_val=55.0)
    for gate_name, gate_data in result["gates"].items():
        assert "reason" in gate_data
        assert isinstance(gate_data["reason"], str)
        assert len(gate_data["reason"]) > 0


# ================================================================== DB integration: metadata_json stored

def test_feature_version_is_11c_v1(tmp_path, monkeypatch):
    """New SignalFeature rows get feature_version='11C_v1'."""
    db_path = str(tmp_path / "fv.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature_with_gates(conn, uid)
    rows = repo.get_signal_features(conn)
    assert rows[0]["feature_version"] == "11C_v1"
    close_db()


def test_metadata_json_stored_in_db(tmp_path, monkeypatch):
    """metadata_json is written and parseable from the DB row."""
    db_path = str(tmp_path / "meta.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature_with_gates(conn, uid, atr_pct=0.50, ema_spread_pct=-0.10, rsi_val=55.0)
    rows = repo.get_signal_features(conn)
    raw = rows[0]["metadata_json"]
    assert raw is not None
    parsed = json.loads(raw)
    assert parsed["candidate_gate_version"] == GATE_VERSION
    assert "gates" in parsed
    close_db()


def test_gate_a_pass_recorded_in_metadata(tmp_path, monkeypatch):
    """Gate A pass is correctly stored in DB metadata."""
    db_path = str(tmp_path / "ga.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature_with_gates(conn, uid, atr_pct=0.50)
    rows = repo.get_signal_features(conn)
    parsed = json.loads(rows[0]["metadata_json"])
    assert parsed["gates"]["GATE_A_ATR_0_10_TO_1_00"]["passed"] is True
    close_db()


def test_gate_a_fail_recorded_in_metadata(tmp_path, monkeypatch):
    """Gate A fail is correctly stored in DB metadata."""
    db_path = str(tmp_path / "gaf.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature_with_gates(conn, uid, atr_pct=0.05)
    rows = repo.get_signal_features(conn)
    parsed = json.loads(rows[0]["metadata_json"])
    assert parsed["gates"]["GATE_A_ATR_0_10_TO_1_00"]["passed"] is False
    close_db()


def test_observation_count_unchanged_by_gate_eval(tmp_path, monkeypatch):
    """Inserting features with gate metadata does not change observation count."""
    db_path = str(tmp_path / "count.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    for _ in range(5):
        uid = _insert_obs(conn)
        _insert_feature_with_gates(conn, uid)
    obs_count = conn.execute("SELECT COUNT(*) FROM signal_observations").fetchone()[0]
    feat_count = conn.execute("SELECT COUNT(*) FROM signal_features").fetchone()[0]
    assert obs_count == 5
    assert feat_count == 5
    close_db()


# ================================================================== report: empty / no-11C data

def test_report_runs_empty_db(tmp_path, monkeypatch):
    """Gate report exits cleanly on empty DB."""
    db_path = str(tmp_path / "empty.db")
    _make_env(monkeypatch, db_path)
    init_db(db_path)
    close_db()
    output = _run_gate_report()
    assert "Candidate Gate" in output
    assert "REPORT-ONLY" in output


def test_report_no_11c_data_message(tmp_path, monkeypatch):
    """Report prints 'No 11C candidate gate observations found yet' when no data."""
    db_path = str(tmp_path / "no11c.db")
    _make_env(monkeypatch, db_path)
    init_db(db_path)
    close_db()
    output = _run_gate_report()
    assert "No 11C candidate gate observations found yet" in output


def test_report_with_data_shows_cohorts(tmp_path, monkeypatch):
    """Report shows cohort stats when closed 11C rows exist."""
    db_path = str(tmp_path / "data.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    for _ in range(5):
        uid = _insert_obs(conn)
        _insert_feature_with_gates(conn, uid, atr_pct=0.50, ema_spread_pct=-0.10, rsi_val=55.0)
        _close_hit2r(conn, uid)
    close_db()
    output = _run_gate_report()
    assert "BASELINE_ALL_WITH_GATE_VERSION" in output
    assert "GATE_A_ATR_0_10_TO_1_00" in output
    assert "GATE_B_EMA_SPREAD_NEG_0_25_TO_0" in output
    assert "GATE_C_RSI_50_TO_60" in output
    assert "GATE_D_ATR_AND_EMA_COMBINED" in output


def test_report_shows_insufficient_sample_warning(tmp_path, monkeypatch):
    """Report warns about INSUFFICIENT SAMPLE when cohort n < min_n."""
    db_path = str(tmp_path / "small.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature_with_gates(conn, uid, atr_pct=0.50, ema_spread_pct=-0.10, rsi_val=55.0)
    _close_hit2r(conn, uid)
    close_db()
    output = _run_gate_report(["--min-n", "10"])
    assert "INSUFFICIENT" in output


def test_report_interpretation_section_appears(tmp_path, monkeypatch):
    """Interpretation section appears when closed rows exist."""
    db_path = str(tmp_path / "interp.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature_with_gates(conn, uid, atr_pct=0.50)
    _close_hit2r(conn, uid)
    close_db()
    output = _run_gate_report()
    assert "Interpretation" in output
    assert "IMPORTANT" in output


def test_report_gate_definitions_section_appears(tmp_path, monkeypatch):
    """Gate definitions section appears when data exists."""
    db_path = str(tmp_path / "defs.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature_with_gates(conn, uid)
    _close_hit2r(conn, uid)
    close_db()
    output = _run_gate_report()
    assert "Candidate gate definitions" in output


# ================================================================== snapshot integration

def test_snapshot_includes_candidate_gate_section(tmp_path, monkeypatch):
    """create_apex_snapshot includes Candidate Gate Report section."""
    db_path = str(tmp_path / "snap.db")
    out_path = str(tmp_path / "snap.txt")
    _make_env(monkeypatch, db_path)
    init_db(db_path)
    close_db()

    import scripts.create_apex_snapshot as snap
    buf = io.StringIO()
    with redirect_stdout(buf):
        snap.main(["--out", out_path])

    content = Path(out_path).read_text()
    assert "Candidate Gate Report (11C)" in content


def test_snapshot_gate_section_no_crash_empty_db(tmp_path, monkeypatch):
    """Snapshot gate section does not crash on empty DB."""
    db_path = str(tmp_path / "snap_empty.db")
    out_path = str(tmp_path / "snap_empty.txt")
    _make_env(monkeypatch, db_path)
    init_db(db_path)
    close_db()

    import scripts.create_apex_snapshot as snap
    snap.main(["--out", out_path])
    content = Path(out_path).read_text()
    assert "No 11C candidate gate observations found yet" in content


# ================================================================== direct import

def test_gate_module_importable_directly():
    """candidate_gates module can be imported via direct file path."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "candidate_gates",
        Path(__file__).resolve().parent.parent
        / "src" / "apex" / "strategy" / "candidate_gates.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "evaluate_candidate_gates")
    assert hasattr(mod, "GATE_VERSION")
    assert hasattr(mod, "GATE_DEFINITIONS")


def test_report_script_importable_directly():
    """report_candidate_gates.py can be imported via direct file path."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "report_candidate_gates",
        Path(__file__).resolve().parent.parent / "scripts" / "report_candidate_gates.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "main")
    assert hasattr(mod, "evaluate_candidate_gates") or hasattr(mod, "_parse_gate_meta")


# ================================================================== Milestone 11C.1: clarity fixes

# ------------------------------------------------------------------ additional helpers

def _close_stopped(conn, uid: str) -> None:
    now = utcnow_iso()
    repo.close_signal_observation(
        conn, uid,
        status="STOPPED", outcome_r=-1.0, closed_at=now,
        stopped_at=now, time_to_stop_seconds=90.0,
        hit_1r_before_stop=0,
        first_terminal_status="STOPPED", final_status="STOPPED",
    )
    repo.update_signal_feature_outcome_from_observation(conn, uid)


def _close_expired(conn, uid: str, hit_1r: int = 0) -> None:
    now = utcnow_iso()
    repo.close_signal_observation(
        conn, uid,
        status="EXPIRED", outcome_r=None, closed_at=now,
        expired_at=now, time_to_expiry_seconds=900.0,
        hit_1r_before_expiry=hit_1r,
        first_terminal_status="EXPIRED", final_status="EXPIRED",
    )
    repo.update_signal_feature_outcome_from_observation(conn, uid)


def _insert_feature_short(conn, uid: str, atr_pct=0.30, ema_spread_pct=-0.10, rsi_val=55.0):
    """Insert a SHORT feature with gate metadata."""
    gate_result = evaluate_candidate_gates(
        atr_pct=atr_pct,
        ema_spread_pct=ema_spread_pct,
        rsi_val=rsi_val,
    )
    feat = SignalFeature(
        observation_uid=uid,
        captured_at=utcnow_iso(),
        symbol="BTC",
        direction="SHORT",
        signal_type="CONFIRMED_SETUP",
        observed_at=utcnow_iso(),
        entry_price=100.0,
        stop_price=101.0,
        target_1r=99.0,
        target_2r=98.0,
        rsi_val=rsi_val,
        atr_pct=atr_pct,
        ema_spread_pct=ema_spread_pct,
        metadata_json=json.dumps(gate_result),
    )
    repo.insert_signal_feature(conn, feat)


def _insert_obs_short(conn) -> str:
    uid = new_uid()
    obs = SignalObservation(
        observation_uid=uid,
        observed_at=utcnow_iso(),
        symbol="BTC",
        direction="SHORT",
        signal_type="CONFIRMED_SETUP",
        entry_price=100.0,
        stop_price=101.0,
        target_1r=99.0,
        target_2r=98.0,
        expires_at=minutes_from_now(15),
    )
    repo.insert_signal_observation(conn, obs)
    return uid


# ------------------------------------------------------------------ outcome_r coverage tests

def test_avg_outcome_r_excludes_null_outcome_r(tmp_path, monkeypatch):
    """Avg outcome R only uses non-null outcome_r (expired rows excluded)."""
    db_path = str(tmp_path / "cov.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    # 3 expired (outcome_r=None) + 1 stopped (outcome_r=-1.0) + 1 hit2r (outcome_r=2.0)
    for _ in range(3):
        uid = _insert_obs(conn)
        _insert_feature_with_gates(conn, uid, atr_pct=0.50)
        _close_expired(conn, uid)

    uid_s = _insert_obs(conn)
    _insert_feature_with_gates(conn, uid_s, atr_pct=0.50)
    _close_stopped(conn, uid_s)

    uid_h = _insert_obs(conn)
    _insert_feature_with_gates(conn, uid_h, atr_pct=0.50)
    _close_hit2r(conn, uid_h)

    close_db()

    output = _run_gate_report()
    # Only 2 of 5 rows have non-null outcome_r
    assert "n=2 / 5" in output or "2 / 5" in output


def test_report_shows_outcome_r_coverage_label(tmp_path, monkeypatch):
    """Report shows 'Outcome R cov' label in cohort section."""
    db_path = str(tmp_path / "rcov.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature_with_gates(conn, uid, atr_pct=0.50)
    _close_hit2r(conn, uid)
    close_db()

    output = _run_gate_report()
    assert "Outcome R cov" in output


def test_report_shows_avg_outcome_r_label(tmp_path, monkeypatch):
    """Report uses 'Avg outcome R' label instead of 'Avg R'."""
    db_path = str(tmp_path / "rlabel.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature_with_gates(conn, uid, atr_pct=0.50)
    _close_hit2r(conn, uid)
    close_db()

    output = _run_gate_report()
    assert "Avg outcome R" in output
    # Old label "Avg R" should not appear as a standalone label
    assert "    Avg R " not in output


def test_null_outcome_r_not_silently_counted(tmp_path, monkeypatch):
    """An expired row with outcome_r=None must not contribute to Avg outcome R."""
    db_path = str(tmp_path / "nullr.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    # One expired row (outcome_r=None) and one HIT_2R row (outcome_r=2.0)
    uid_e = _insert_obs(conn)
    _insert_feature_with_gates(conn, uid_e, atr_pct=0.50)
    _close_expired(conn, uid_e)

    uid_h = _insert_obs(conn)
    _insert_feature_with_gates(conn, uid_h, atr_pct=0.50)
    _close_hit2r(conn, uid_h)

    close_db()

    output = _run_gate_report()
    # Coverage should show 1 contributing row out of 2, not 2 out of 2
    assert "n=1 / 2" in output or "1 / 2" in output


def test_partial_coverage_warning_appears(tmp_path, monkeypatch):
    """PARTIAL COVERAGE warning appears when outcome_r coverage < 80%."""
    db_path = str(tmp_path / "partial.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    # 9 expired (no outcome_r) + 1 stopped (has outcome_r): 10% coverage
    for _ in range(9):
        uid = _insert_obs(conn)
        _insert_feature_with_gates(conn, uid, atr_pct=0.50)
        _close_expired(conn, uid)

    uid_s = _insert_obs(conn)
    _insert_feature_with_gates(conn, uid_s, atr_pct=0.50)
    _close_stopped(conn, uid_s)

    close_db()

    output = _run_gate_report()
    assert "PARTIAL COVERAGE" in output


def test_full_coverage_no_partial_warning(tmp_path, monkeypatch):
    """No PARTIAL COVERAGE warning when all rows have non-null outcome_r."""
    db_path = str(tmp_path / "full.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    # All rows have non-null outcome_r (stopped or hit2r)
    for _ in range(5):
        uid = _insert_obs(conn)
        _insert_feature_with_gates(conn, uid, atr_pct=0.50)
        _close_hit2r(conn, uid)

    close_db()

    output = _run_gate_report()
    assert "PARTIAL COVERAGE" not in output


def test_outcome_r_coverage_note_in_interpretation(tmp_path, monkeypatch):
    """Interpretation section shows coverage note for expired rows."""
    db_path = str(tmp_path / "covnote.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    uid_e = _insert_obs(conn)
    _insert_feature_with_gates(conn, uid_e, atr_pct=0.50)
    _close_expired(conn, uid_e)

    uid_h = _insert_obs(conn)
    _insert_feature_with_gates(conn, uid_h, atr_pct=0.50)
    _close_hit2r(conn, uid_h)

    close_db()

    output = _run_gate_report()
    assert "outcome_r is null" in output.lower() or "outcome_r" in output


# ------------------------------------------------------------------ direction concentration tests

def test_direction_concentration_warning_100pct_short(tmp_path, monkeypatch):
    """Warning appears when a gate cohort is 100% SHORT."""
    db_path = str(tmp_path / "short.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    # Gate B passes for ema_spread_pct=-0.10; all are SHORT
    for _ in range(5):
        uid = _insert_obs_short(conn)
        _insert_feature_short(conn, uid, atr_pct=0.50, ema_spread_pct=-0.10, rsi_val=55.0)
        _close_hit2r(conn, uid)

    # Add a LONG row that does NOT pass Gate B (ema_spread_pct=0.10)
    uid_l = _insert_obs(conn)
    _insert_feature_with_gates(conn, uid_l, atr_pct=0.50, ema_spread_pct=0.10, rsi_val=55.0)
    _close_hit2r(conn, uid_l)

    close_db()

    output = _run_gate_report()
    # Gate B cohort is 5 SHORT, 0 LONG → direction concentrated
    assert "DIR-CONCENTRATED" in output or "direction-concentrated" in output.lower()


def test_direction_concentration_warning_in_interpretation(tmp_path, monkeypatch):
    """Interpretation section includes direction-concentration warning text."""
    db_path = str(tmp_path / "dircwarn.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    # All SHORT rows passing Gate B
    for _ in range(5):
        uid = _insert_obs_short(conn)
        _insert_feature_short(conn, uid, atr_pct=0.50, ema_spread_pct=-0.10, rsi_val=55.0)
        _close_hit2r(conn, uid)

    # 1 LONG not passing Gate B (so Gate B cohort stays all-SHORT)
    uid_l = _insert_obs(conn)
    _insert_feature_with_gates(conn, uid_l, atr_pct=0.50, ema_spread_pct=0.10, rsi_val=55.0)
    _close_hit2r(conn, uid_l)

    close_db()

    output = _run_gate_report()
    assert "Direction-concentration warnings" in output
    assert "SHORT" in output


def test_direction_concentration_100pct_long(tmp_path, monkeypatch):
    """Warning appears when gate cohort is 100% LONG."""
    db_path = str(tmp_path / "long100.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    # Gate C passes rsi_val=55.0, all LONG
    for _ in range(5):
        uid = _insert_obs(conn)
        _insert_feature_with_gates(conn, uid, atr_pct=0.30, ema_spread_pct=0.10, rsi_val=55.0)
        _close_hit2r(conn, uid)

    # 1 SHORT not passing Gate C (rsi=70)
    uid_s = _insert_obs_short(conn)
    _insert_feature_short(conn, uid_s, atr_pct=0.30, ema_spread_pct=0.10, rsi_val=70.0)
    _close_hit2r(conn, uid_s)

    close_db()

    output = _run_gate_report()
    assert "DIR-CONCENTRATED" in output or "direction-concentrated" in output.lower()


def test_no_direction_concentration_warning_mixed(tmp_path, monkeypatch):
    """No direction warning when cohort is mixed LONG/SHORT."""
    db_path = str(tmp_path / "mixed.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    # Mix of LONG and SHORT both passing Gate A (atr_pct=0.50)
    for _ in range(5):
        uid = _insert_obs(conn)
        _insert_feature_with_gates(conn, uid, atr_pct=0.50, ema_spread_pct=0.10, rsi_val=70.0)
        _close_hit2r(conn, uid)
    for _ in range(5):
        uid = _insert_obs_short(conn)
        _insert_feature_short(conn, uid, atr_pct=0.50, ema_spread_pct=0.10, rsi_val=70.0)
        _close_hit2r(conn, uid)

    close_db()

    output = _run_gate_report()
    assert "DIR-CONCENTRATED" not in output
    assert "Direction-concentration warnings" not in output


# ------------------------------------------------------------------ expired-after-TP1 explanation

def test_expired_after_tp1_still_displayed(tmp_path, monkeypatch):
    """expired-after-TP1 count still appears in the report output."""
    db_path = str(tmp_path / "extp1.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    # 2 expired-after-TP1, 1 expired-without-TP1
    for hit in (1, 1, 0):
        uid = _insert_obs(conn)
        _insert_feature_with_gates(conn, uid, atr_pct=0.50)
        _close_expired(conn, uid, hit_1r=hit)

    close_db()

    output = _run_gate_report()
    assert "after TP1" in output
    assert "w/o TP1" in output


def test_partial_success_explanation_in_report(tmp_path, monkeypatch):
    """Report includes partial-success interpretation for expired rows."""
    db_path = str(tmp_path / "partsucc.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature_with_gates(conn, uid, atr_pct=0.50)
    _close_expired(conn, uid, hit_1r=1)
    close_db()

    output = _run_gate_report()
    assert "Expired-after-TP1" in output or "expired-after-TP1" in output.lower()
    assert "partial success" in output.lower() or "Partial success" in output


# ------------------------------------------------------------------ summary table

def test_summary_table_shows_avgouter_label(tmp_path, monkeypatch):
    """Summary table uses AvgOutR or AvgOutcomeR column label."""
    db_path = str(tmp_path / "sumtbl.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature_with_gates(conn, uid, atr_pct=0.50)
    _close_hit2r(conn, uid)
    close_db()

    output = _run_gate_report()
    assert "AvgOutR" in output or "AvgOutcomeR" in output or "OutR" in output


def test_summary_table_shows_r_n_column(tmp_path, monkeypatch):
    """Summary table includes coverage column (R_n or similar)."""
    db_path = str(tmp_path / "sumrn.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature_with_gates(conn, uid, atr_pct=0.50)
    _close_hit2r(conn, uid)
    close_db()

    output = _run_gate_report()
    assert "R_n" in output or "Rcov" in output or "OutR_n" in output


def test_summary_table_shows_ls_column(tmp_path, monkeypatch):
    """Summary table includes L/S direction column."""
    db_path = str(tmp_path / "sumls.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature_with_gates(conn, uid, atr_pct=0.50)
    _close_hit2r(conn, uid)
    close_db()

    output = _run_gate_report()
    assert "L/S" in output


# ------------------------------------------------------------------ empty cohort safety

def test_empty_cohort_renders_without_crash(tmp_path, monkeypatch):
    """A cohort with zero rows renders cleanly."""
    db_path = str(tmp_path / "empty_cohort.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    # Only Gate A passes (atr_pct=0.50); Gate B fails (ema_spread_pct positive)
    # Gate C fails (rsi=70); Gate D fails
    uid = _insert_obs(conn)
    _insert_feature_with_gates(conn, uid, atr_pct=0.50, ema_spread_pct=0.10, rsi_val=70.0)
    _close_hit2r(conn, uid)
    close_db()

    # Should not crash even though Gate B/C/D cohorts have zero rows
    output = _run_gate_report()
    assert "No closed rows in this cohort" in output
