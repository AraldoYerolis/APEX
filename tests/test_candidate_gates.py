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
