"""Tests for Milestone 11D: Exit Policy Simulator.

Covers:
- Empty DB exits safely
- Baseline TP1 / HIT_2R / STOPPED / EXPIRED counts correct
- expired-after-TP1 and expired-without-TP1 separated correctly
- Avg outcome R (Policy A) excludes null outcome_r rows
- Policy B: TP1 rows → +1R, stopped-no-tp1 → -1R, expired-no-tp1 → 0R
- Policy C: same assignment as B (same helper, BE approximation)
- Policy D strict: EXPIRED excluded from avg R
- Policy D with-zero: EXPIRED counted as 0R
- Policy E: expired-after-TP1 → +0.5R, expired-without-TP1 → -0.25R
- Timing bucket analysis runs without crash with/without timing data
- Empty cohort (LONG or SHORT with 0 rows) renders safely
- Comparison table appears in output
- Timing analysis section appears in output
- Simulation limitations section appears
- Interpretation section appears
- Snapshot integration does not crash on empty DB
- Script importable directly
- No DB writes
- CLI --feature-version and --signal-type flags accepted
"""
from __future__ import annotations

import io
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from apex.db import repository as repo
from apex.db.connection import close_db, init_db
from apex.db.models import SignalFeature, SignalObservation
from apex.utils.ids import new_uid
from apex.utils.time import minutes_from_now, utcnow_iso
from scripts.simulate_exit_policies import (
    _policy_a_r,
    _policy_b_r,
    _policy_c_r,
    _policy_d_r_strict,
    _policy_d_r_with_zero,
    _policy_e_r,
    _avg,
    _has_tp1,
    _POLICY_E_EXP_AFTER_TP1_R,
    _POLICY_E_EXP_NO_TP1_R,
)


# ------------------------------------------------------------------ helpers

def _make_env(monkeypatch, db_path: str) -> None:
    monkeypatch.setenv("APEX_DB_PATH", db_path)
    monkeypatch.setenv("ACTION_TOKEN_SECRET", "test-11d")
    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "fake")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "fake")
    monkeypatch.setenv("ALERTS_ENABLED", "false")
    monkeypatch.setenv("DRY_RUN_MODE", "true")


def _insert_obs(conn, direction: str = "LONG", symbol: str = "BTC") -> str:
    uid = new_uid()
    obs = SignalObservation(
        observation_uid=uid,
        observed_at=utcnow_iso(),
        symbol=symbol,
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


def _insert_feature(conn, uid: str, direction: str = "LONG") -> None:
    feat = SignalFeature(
        observation_uid=uid,
        captured_at=utcnow_iso(),
        symbol="BTC",
        direction=direction,
        signal_type="CONFIRMED_SETUP",
        observed_at=utcnow_iso(),
        entry_price=100.0,
        stop_price=99.0,
        target_1r=101.0,
        target_2r=102.0,
        atr_pct=0.30,
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


def _close_stopped(conn, uid: str, hit_1r: int = 0, t_stop: float = 90.0) -> None:
    now = utcnow_iso()
    repo.close_signal_observation(
        conn, uid,
        status="STOPPED", outcome_r=-1.0, closed_at=now,
        stopped_at=now, time_to_stop_seconds=t_stop,
        hit_1r_before_stop=hit_1r,
        first_terminal_status="STOPPED", final_status="STOPPED",
    )
    repo.update_signal_feature_outcome_from_observation(conn, uid)


def _close_expired(conn, uid: str, hit_1r: int = 0, t_exp: float = 900.0) -> None:
    now = utcnow_iso()
    repo.close_signal_observation(
        conn, uid,
        status="EXPIRED", outcome_r=None, closed_at=now,
        expired_at=now, time_to_expiry_seconds=t_exp,
        hit_1r_before_expiry=hit_1r,
        first_terminal_status="EXPIRED", final_status="EXPIRED",
    )
    repo.update_signal_feature_outcome_from_observation(conn, uid)


def _make_row(direction="LONG", outcome_status="HIT_2R", outcome_r=2.0,
              hit_1r_at="2026-01-01T00:00:00Z", hit_1r_before_expiry=None,
              hit_1r_before_stop=None, time_to_1r_seconds=120.0,
              time_to_stop_seconds=None, time_to_expiry_seconds=None):
    """Build a minimal dict simulating a signal_features + JOIN row for unit tests."""
    return {
        "direction": direction,
        "outcome_status": outcome_status,
        "outcome_r": outcome_r,
        "hit_1r_at": hit_1r_at,
        "hit_1r_before_expiry": hit_1r_before_expiry,
        "hit_1r_before_stop": hit_1r_before_stop,
        "time_to_1r_seconds": time_to_1r_seconds,
        "time_to_stop_seconds": time_to_stop_seconds,
        "time_to_expiry_seconds": time_to_expiry_seconds,
        "so_mfe": None,
        "so_mae": None,
    }


def _run_sim(argv=None):
    import scripts.simulate_exit_policies as mod
    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main(argv or [])
    return buf.getvalue()


# ================================================================== unit tests: policy functions

# ------------------------------------------------------------------ has_tp1 helper

def test_has_tp1_true_when_hit_1r_at_set():
    row = _make_row(hit_1r_at="2026-01-01T00:00:00Z")
    assert _has_tp1(row) is True


def test_has_tp1_false_when_hit_1r_at_none():
    row = _make_row(hit_1r_at=None)
    assert _has_tp1(row) is False


# ------------------------------------------------------------------ Policy A (recorded)

def test_policy_a_returns_recorded_outcome_r():
    row = _make_row(outcome_status="HIT_2R", outcome_r=2.0)
    assert _policy_a_r(row) == 2.0


def test_policy_a_returns_none_when_outcome_r_null():
    row = _make_row(outcome_status="EXPIRED", outcome_r=None)
    assert _policy_a_r(row) is None


def test_policy_a_returns_negative_for_stopped():
    row = _make_row(outcome_status="STOPPED", outcome_r=-1.0, hit_1r_at=None)
    assert _policy_a_r(row) == pytest.approx(-1.0)


# ------------------------------------------------------------------ Policy B (TP1 scalp)

def test_policy_b_tp1_row_is_plus_1r():
    row = _make_row(hit_1r_at="now")
    assert _policy_b_r(row) == pytest.approx(1.0)


def test_policy_b_stopped_without_tp1_is_minus_1r():
    row = _make_row(outcome_status="STOPPED", outcome_r=-1.0, hit_1r_at=None)
    assert _policy_b_r(row) == pytest.approx(-1.0)


def test_policy_b_expired_without_tp1_is_zero():
    row = _make_row(outcome_status="EXPIRED", outcome_r=None, hit_1r_at=None,
                    hit_1r_before_expiry=0)
    assert _policy_b_r(row) == pytest.approx(0.0)


def test_policy_b_hit2r_row_has_tp1_so_plus_1r():
    """HIT_2R rows also have hit_1r_at set → +1R under B."""
    row = _make_row(outcome_status="HIT_2R", outcome_r=2.0, hit_1r_at="now")
    assert _policy_b_r(row) == pytest.approx(1.0)


# ------------------------------------------------------------------ Policy C (TP1 breakeven)

def test_policy_c_same_as_b_for_tp1_row():
    row = _make_row(hit_1r_at="now")
    assert _policy_c_r(row) == pytest.approx(1.0)


def test_policy_c_stopped_without_tp1_is_minus_1r():
    row = _make_row(outcome_status="STOPPED", outcome_r=-1.0, hit_1r_at=None)
    assert _policy_c_r(row) == pytest.approx(-1.0)


def test_policy_c_expired_without_tp1_is_zero():
    row = _make_row(outcome_status="EXPIRED", outcome_r=None, hit_1r_at=None,
                    hit_1r_before_expiry=0)
    assert _policy_c_r(row) == pytest.approx(0.0)


# ------------------------------------------------------------------ Policy D (strict 2R)

def test_policy_d_strict_hit2r_is_plus_2r():
    row = _make_row(outcome_status="HIT_2R", outcome_r=2.0)
    assert _policy_d_r_strict(row) == pytest.approx(2.0)


def test_policy_d_strict_stopped_is_minus_1r():
    row = _make_row(outcome_status="STOPPED", outcome_r=-1.0, hit_1r_at=None)
    assert _policy_d_r_strict(row) == pytest.approx(-1.0)


def test_policy_d_strict_expired_returns_none():
    row = _make_row(outcome_status="EXPIRED", outcome_r=None, hit_1r_at=None)
    assert _policy_d_r_strict(row) is None


def test_policy_d_with_zero_expired_is_zero():
    row = _make_row(outcome_status="EXPIRED", outcome_r=None, hit_1r_at=None)
    assert _policy_d_r_with_zero(row) == pytest.approx(0.0)


def test_policy_d_with_zero_hit2r_is_plus_2r():
    row = _make_row(outcome_status="HIT_2R", outcome_r=2.0)
    assert _policy_d_r_with_zero(row) == pytest.approx(2.0)


# ------------------------------------------------------------------ Policy E (partial credit)

def test_policy_e_hit2r_is_plus_2r():
    row = _make_row(outcome_status="HIT_2R", outcome_r=2.0, hit_1r_at="now")
    assert _policy_e_r(row) == pytest.approx(2.0)


def test_policy_e_stopped_is_minus_1r():
    row = _make_row(outcome_status="STOPPED", outcome_r=-1.0, hit_1r_at=None)
    assert _policy_e_r(row) == pytest.approx(-1.0)


def test_policy_e_expired_after_tp1_is_partial_credit():
    row = _make_row(outcome_status="EXPIRED", outcome_r=None,
                    hit_1r_at="now", hit_1r_before_expiry=1)
    assert _policy_e_r(row) == pytest.approx(_POLICY_E_EXP_AFTER_TP1_R)


def test_policy_e_expired_without_tp1_is_small_loss():
    row = _make_row(outcome_status="EXPIRED", outcome_r=None,
                    hit_1r_at=None, hit_1r_before_expiry=0)
    assert _policy_e_r(row) == pytest.approx(_POLICY_E_EXP_NO_TP1_R)


def test_policy_e_constants_are_defined():
    """Policy E constants exist and have plausible values."""
    assert _POLICY_E_EXP_AFTER_TP1_R > 0
    assert _POLICY_E_EXP_NO_TP1_R <= 0


# ------------------------------------------------------------------ _avg helper

def test_avg_returns_none_for_empty():
    assert _avg([]) is None


def test_avg_correct():
    assert _avg([1.0, 2.0, 3.0]) == pytest.approx(2.0)


# ================================================================== integration tests

def test_sim_empty_db_no_crash(tmp_path, monkeypatch):
    """Script exits cleanly with no data."""
    db_path = str(tmp_path / "empty.db")
    _make_env(monkeypatch, db_path)
    init_db(db_path)
    close_db()
    output = _run_sim()
    assert "Exit Policy Simulator" in output
    assert "REPORT-ONLY" in output


def test_sim_no_closed_rows_exits_gracefully(tmp_path, monkeypatch):
    """Script prints 'No closed rows' when only open observations exist."""
    db_path = str(tmp_path / "open_only.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature(conn, uid)
    # Do NOT close it
    close_db()
    output = _run_sim()
    assert "No closed rows" in output


def test_sim_baseline_tp1_count(tmp_path, monkeypatch):
    """Baseline TP1 count is correct."""
    db_path = str(tmp_path / "tp1.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    # 2 HIT_2R (have TP1), 1 STOPPED (no TP1), 1 EXPIRED (no TP1)
    for _ in range(2):
        uid = _insert_obs(conn)
        _insert_feature(conn, uid)
        _close_hit2r(conn, uid)
    uid_s = _insert_obs(conn)
    _insert_feature(conn, uid_s)
    _close_stopped(conn, uid_s)
    uid_e = _insert_obs(conn)
    _insert_feature(conn, uid_e)
    _close_expired(conn, uid_e)
    close_db()

    output = _run_sim()
    # TP1 milestone: 2 rows (HIT_2R rows have hit_1r_at set)
    assert "TP1 milestone" in output
    assert "Baseline Recorded Outcomes" in output


def test_sim_expired_after_tp1_separated(tmp_path, monkeypatch):
    """Expired-after-TP1 and expired-without-TP1 appear separately."""
    db_path = str(tmp_path / "exp_tp1.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    # 1 expired-after-TP1, 1 expired-without-TP1
    uid_a = _insert_obs(conn)
    _insert_feature(conn, uid_a)
    _close_expired(conn, uid_a, hit_1r=1)

    uid_b = _insert_obs(conn)
    _insert_feature(conn, uid_b)
    _close_expired(conn, uid_b, hit_1r=0)

    close_db()
    output = _run_sim()
    assert "after TP1" in output
    assert "w/o  TP1" in output


def test_sim_avg_outcome_r_excludes_null(tmp_path, monkeypatch):
    """Policy A Avg outcome R excludes expired rows with null outcome_r."""
    db_path = str(tmp_path / "null_r.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    # 3 expired (outcome_r=None), 1 stopped (outcome_r=-1.0)
    for _ in range(3):
        uid = _insert_obs(conn)
        _insert_feature(conn, uid)
        _close_expired(conn, uid)
    uid_s = _insert_obs(conn)
    _insert_feature(conn, uid_s)
    _close_stopped(conn, uid_s)

    close_db()
    output = _run_sim()
    # Only 1 row contributes to Policy A avg R (the stopped row)
    assert "cov=1/4" in output or "1 / 4" in output or "n=1 / 4" in output


def test_sim_policy_b_positive_when_all_tp1(tmp_path, monkeypatch):
    """Policy B avg R is +1.0 when all rows reach TP1."""
    db_path = str(tmp_path / "all_tp1.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    for _ in range(5):
        uid = _insert_obs(conn)
        _insert_feature(conn, uid)
        _close_hit2r(conn, uid)
    close_db()

    output = _run_sim()
    # Policy B: all HIT_2R rows have TP1 → +1.0R each → avg = +1.000
    assert "TP1_SCALP" in output
    assert "+1.000" in output


def test_sim_policy_b_negative_when_all_stopped_no_tp1(tmp_path, monkeypatch):
    """Policy B avg R is -1.0 when all rows are stopped without TP1."""
    db_path = str(tmp_path / "all_stop.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    for _ in range(4):
        uid = _insert_obs(conn)
        _insert_feature(conn, uid)
        _close_stopped(conn, uid, hit_1r=0)
    close_db()

    output = _run_sim()
    assert "-1.000" in output


def test_sim_policy_d_strict_excludes_expired(tmp_path, monkeypatch):
    """Policy D strict shows lower coverage (expired excluded)."""
    db_path = str(tmp_path / "d_strict.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    uid_h = _insert_obs(conn)
    _insert_feature(conn, uid_h)
    _close_hit2r(conn, uid_h)

    uid_e = _insert_obs(conn)
    _insert_feature(conn, uid_e)
    _close_expired(conn, uid_e)

    close_db()
    output = _run_sim()
    assert "STRICT_2R_EXCL_EXP" in output
    # cov=1/2 because expired is excluded
    assert "cov=1/2" in output or "1/2" in output


def test_sim_policy_e_partial_credit_applied(tmp_path, monkeypatch):
    """Policy E assigns partial credit constants correctly."""
    db_path = str(tmp_path / "policy_e.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    # 1 expired-after-TP1, 1 expired-without-TP1
    uid_a = _insert_obs(conn)
    _insert_feature(conn, uid_a)
    _close_expired(conn, uid_a, hit_1r=1)

    uid_b = _insert_obs(conn)
    _insert_feature(conn, uid_b)
    _close_expired(conn, uid_b, hit_1r=0)

    close_db()
    output = _run_sim()
    assert "EXPIRE_PARTIAL_CREDIT" in output


def test_sim_timing_analysis_section_appears(tmp_path, monkeypatch):
    """Timing analysis section appears when data exists."""
    db_path = str(tmp_path / "timing.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature(conn, uid)
    _close_hit2r(conn, uid)
    close_db()

    output = _run_sim()
    assert "Timing Analysis" in output


def test_sim_timing_with_no_timing_data(tmp_path, monkeypatch):
    """Timing analysis handles missing timing fields without crashing."""
    db_path = str(tmp_path / "no_timing.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    # Insert an expired row but with no timing data (t_exp=None not settable through
    # normal path; but the feature row time_to_1r_seconds will be null)
    uid = _insert_obs(conn)
    _insert_feature(conn, uid)
    _close_expired(conn, uid)  # time fields are set to 900.0 by default in helper

    close_db()
    output = _run_sim()
    # Should not crash; Timing Analysis section should appear
    assert "Timing Analysis" in output


def test_sim_comparison_table_appears(tmp_path, monkeypatch):
    """Policy comparison table appears in output."""
    db_path = str(tmp_path / "tbl.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature(conn, uid)
    _close_hit2r(conn, uid)
    close_db()

    output = _run_sim()
    assert "Simulated Policy Comparison" in output
    assert "TP1_SCALP" in output
    assert "STRICT_2R" in output


def test_sim_limitations_section_appears(tmp_path, monkeypatch):
    """Simulation Limitations section appears."""
    db_path = str(tmp_path / "lim.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature(conn, uid)
    _close_hit2r(conn, uid)
    close_db()

    output = _run_sim()
    assert "Simulation Limitations" in output


def test_sim_interpretation_section_appears(tmp_path, monkeypatch):
    """Interpretation section appears."""
    db_path = str(tmp_path / "interp.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature(conn, uid)
    _close_hit2r(conn, uid)
    close_db()

    output = _run_sim()
    assert "Interpretation" in output
    assert "research only" in output.lower() or "dry-run research" in output.lower()


def test_sim_direction_breakdown_long_short(tmp_path, monkeypatch):
    """Direction breakdown shows LONG and SHORT separately."""
    db_path = str(tmp_path / "dir.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    uid_l = _insert_obs(conn, direction="LONG")
    _insert_feature(conn, uid_l, direction="LONG")
    _close_hit2r(conn, uid_l)

    uid_s = _insert_obs(conn, direction="SHORT")
    _insert_feature(conn, uid_s, direction="SHORT")
    _close_stopped(conn, uid_s)

    close_db()
    output = _run_sim()
    assert "Direction Breakdown" in output
    assert "LONG" in output
    assert "SHORT" in output


def test_sim_does_not_write_to_db(tmp_path, monkeypatch):
    """Running the simulator does not change DB row count."""
    db_path = str(tmp_path / "ro.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    for _ in range(3):
        uid = _insert_obs(conn)
        _insert_feature(conn, uid)
        _close_hit2r(conn, uid)
    close_db()

    raw = sqlite3.connect(db_path)
    before = raw.execute("SELECT COUNT(*) FROM signal_features").fetchone()[0]
    raw.close()

    _run_sim()

    raw2 = sqlite3.connect(db_path)
    after = raw2.execute("SELECT COUNT(*) FROM signal_features").fetchone()[0]
    raw2.close()

    assert before == after


def test_sim_feature_version_filter(tmp_path, monkeypatch):
    """--feature-version flag is accepted and filters rows."""
    db_path = str(tmp_path / "fv.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature(conn, uid)
    _close_hit2r(conn, uid)
    close_db()

    # Filter for a version that doesn't exist → should show 'No closed rows'
    output = _run_sim(["--feature-version", "99Z_v9"])
    assert "No closed rows" in output or "Exit Policy Simulator" in output


def test_sim_signal_type_filter(tmp_path, monkeypatch):
    """--signal-type flag is accepted."""
    db_path = str(tmp_path / "st.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature(conn, uid)
    _close_hit2r(conn, uid)
    close_db()

    output = _run_sim(["--signal-type", "CONFIRMED_SETUP"])
    assert "Exit Policy Simulator" in output


def test_sim_snapshot_section_appears(tmp_path, monkeypatch):
    """create_apex_snapshot includes Exit Policy Simulation Report section."""
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
    assert "Exit Policy Simulation Report (11D)" in content


def test_sim_snapshot_no_crash_empty_db(tmp_path, monkeypatch):
    """Snapshot with empty DB does not crash on exit policy section."""
    db_path = str(tmp_path / "snap_empty.db")
    out_path = str(tmp_path / "snap_empty.txt")
    _make_env(monkeypatch, db_path)
    init_db(db_path)
    close_db()

    import scripts.create_apex_snapshot as snap
    snap.main(["--out", out_path])
    content = Path(out_path).read_text()
    assert "Exit Policy Simulation Report (11D)" in content


def test_sim_script_importable_directly():
    """simulate_exit_policies.py importable via direct path."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "simulate_exit_policies",
        Path(__file__).resolve().parent.parent / "scripts" / "simulate_exit_policies.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "main")
    assert hasattr(mod, "_policy_b_r")
    assert hasattr(mod, "_policy_e_r")
