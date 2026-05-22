"""Tests for Milestone 11E: Exit / Timing Cohort Analysis.

Covers:
- Pure unit tests: _cohort_metrics, _hour_from_observed_at, _session_for_hour,
  _in_bucket, _parse_gate_meta, _gate_passed, _cohort_rank_score,
  _is_dir_concentrated
- Empty DB exits safely without error
- Baseline section shows correct counts
- Section A renders with row count
- Section B (direction) splits LONG / SHORT correctly
- Section C (symbol) groups symbols correctly
- Section D (UTC hour) extracts hours from observed_at
- Section E (session) assigns ASIA / LONDON / US / LATE_US
- Section F (candidate gate) reads metadata_json
- Section G (indicator buckets) filters by RSI, ATR%, EMA spread%, PvV%
- Section H (TP1 timing buckets) groups by time_to_1r_seconds
- Section I (ranking) appears with score column
- Direction-concentrated cohort emits [DIR-CONCENTRATED] warning
- Insufficient-N cohort emits INSUFFICIENT warning
- No DB writes occur
- CLI --feature-version filter works
- CLI --since filter works
- Snapshot integration does not crash on empty DB
- Script importable directly
"""
from __future__ import annotations

import io
import json
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from apex.db import repository as repo
from apex.db.connection import close_db, init_db
from apex.db.models import SignalFeature, SignalObservation
from apex.utils.ids import new_uid
from apex.utils.time import minutes_from_now, utcnow_iso
from scripts.analyze_exit_timing_cohorts import (
    _cohort_metrics,
    _cohort_rank_score,
    _gate_passed,
    _hour_from_observed_at,
    _in_bucket,
    _is_dir_concentrated,
    _parse_gate_meta,
    _session_for_hour,
)


# ------------------------------------------------------------------ helpers

def _make_env(monkeypatch, db_path: str) -> None:
    monkeypatch.setenv("APEX_DB_PATH", db_path)
    monkeypatch.setenv("ACTION_TOKEN_SECRET", "test-11e")
    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "fake")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "fake")
    monkeypatch.setenv("ALERTS_ENABLED", "false")
    monkeypatch.setenv("DRY_RUN_MODE", "true")


def _make_row(
    direction="LONG",
    symbol="BTC",
    observed_at="2026-05-21T14:30:00Z",
    outcome_status="HIT_2R",
    outcome_r=2.0,
    hit_1r_at="2026-01-01T00:00:00Z",
    hit_1r_before_expiry=None,
    hit_1r_before_stop=None,
    time_to_1r_seconds=120.0,
    time_to_stop_seconds=None,
    time_to_expiry_seconds=None,
    so_mfe=None,
    so_mae=None,
    rsi_val=None,
    atr_pct=None,
    ema_spread_pct=None,
    price_vs_vwap_pct=None,
    metadata_json=None,
) -> dict:
    """Build a minimal dict simulating a signal_features + JOIN row for unit tests."""
    return {
        "direction": direction,
        "symbol": symbol,
        "observed_at": observed_at,
        "outcome_status": outcome_status,
        "outcome_r": outcome_r,
        "hit_1r_at": hit_1r_at,
        "hit_1r_before_expiry": hit_1r_before_expiry,
        "hit_1r_before_stop": hit_1r_before_stop,
        "time_to_1r_seconds": time_to_1r_seconds,
        "time_to_stop_seconds": time_to_stop_seconds,
        "time_to_expiry_seconds": time_to_expiry_seconds,
        "so_mfe": so_mfe,
        "so_mae": so_mae,
        "rsi_val": rsi_val,
        "atr_pct": atr_pct,
        "ema_spread_pct": ema_spread_pct,
        "price_vs_vwap_pct": price_vs_vwap_pct,
        "metadata_json": metadata_json,
    }


def _insert_obs(conn, direction="LONG", symbol="BTC",
                observed_at: str | None = None) -> str:
    uid = new_uid()
    obs = SignalObservation(
        observation_uid=uid,
        observed_at=observed_at or utcnow_iso(),
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


def _insert_feature(conn, uid: str, direction: str = "LONG",
                    symbol: str = "BTC",
                    observed_at: str | None = None,
                    rsi_val: float | None = None,
                    atr_pct: float | None = None,
                    ema_spread_pct: float | None = None,
                    metadata_json: str | None = None) -> None:
    feat = SignalFeature(
        observation_uid=uid,
        captured_at=utcnow_iso(),
        symbol=symbol,
        direction=direction,
        signal_type="CONFIRMED_SETUP",
        observed_at=observed_at or utcnow_iso(),
        entry_price=100.0,
        stop_price=99.0,
        target_1r=101.0,
        target_2r=102.0,
        atr_pct=atr_pct or 0.30,
        rsi_val=rsi_val,
        ema_spread_pct=ema_spread_pct,
        metadata_json=metadata_json,
    )
    repo.insert_signal_feature(conn, feat)


def _close_hit2r(conn, uid: str, t1: float = 120.0) -> None:
    now = utcnow_iso()
    repo.close_signal_observation(
        conn, uid,
        status="HIT_2R", outcome_r=2.0, closed_at=now,
        hit_1r_at=now, time_to_1r_seconds=t1,
        hit_2r_at=now, time_to_2r_seconds=t1 * 2,
        first_terminal_status="HIT_2R", final_status="HIT_2R",
    )
    repo.update_signal_feature_outcome_from_observation(conn, uid)


def _close_stopped(conn, uid: str, hit_1r: int = 0,
                   t_stop: float = 90.0) -> None:
    now = utcnow_iso()
    repo.close_signal_observation(
        conn, uid,
        status="STOPPED", outcome_r=-1.0, closed_at=now,
        stopped_at=now, time_to_stop_seconds=t_stop,
        hit_1r_before_stop=hit_1r,
        first_terminal_status="STOPPED", final_status="STOPPED",
    )
    repo.update_signal_feature_outcome_from_observation(conn, uid)


def _close_expired(conn, uid: str, hit_1r: int = 0,
                   t_exp: float = 900.0) -> None:
    now = utcnow_iso()
    repo.close_signal_observation(
        conn, uid,
        status="EXPIRED", outcome_r=None, closed_at=now,
        expired_at=now, time_to_expiry_seconds=t_exp,
        hit_1r_before_expiry=hit_1r,
        first_terminal_status="EXPIRED", final_status="EXPIRED",
    )
    repo.update_signal_feature_outcome_from_observation(conn, uid)


def _run_cohorts(argv=None):
    import scripts.analyze_exit_timing_cohorts as mod
    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main(argv or [])
    return buf.getvalue()


# ================================================================== unit: _hour_from_observed_at

def test_hour_from_T_format():
    assert _hour_from_observed_at("2026-05-21T14:30:00Z") == 14


def test_hour_from_space_format():
    assert _hour_from_observed_at("2026-05-21 09:15:00") == 9


def test_hour_from_midnight():
    assert _hour_from_observed_at("2026-05-21T00:00:00Z") == 0


def test_hour_from_23():
    assert _hour_from_observed_at("2026-05-21T23:59:59Z") == 23


def test_hour_from_empty():
    assert _hour_from_observed_at("") is None


def test_hour_from_none_value():
    assert _hour_from_observed_at(None) is None


# ================================================================== unit: _session_for_hour

def test_session_asia_hour_3():
    assert _session_for_hour(3) == "ASIA"


def test_session_asia_hour_0():
    assert _session_for_hour(0) == "ASIA"


def test_session_london_hour_8():
    assert _session_for_hour(8) == "LONDON"


def test_session_london_hour_12():
    assert _session_for_hour(12) == "LONDON"


def test_session_us_hour_13():
    assert _session_for_hour(13) == "US"


def test_session_us_hour_20():
    assert _session_for_hour(20) == "US"


def test_session_late_us_hour_21():
    assert _session_for_hour(21) == "LATE_US"


def test_session_late_us_hour_23():
    assert _session_for_hour(23) == "LATE_US"


def test_session_none_for_none_hour():
    assert _session_for_hour(None) is None


# ================================================================== unit: _in_bucket

def test_in_bucket_both_bounds():
    assert _in_bucket(0.5, 0.0, 1.0) is True


def test_in_bucket_at_lower_bound():
    assert _in_bucket(0.0, 0.0, 1.0) is True


def test_in_bucket_at_upper_bound_excluded():
    assert _in_bucket(1.0, 0.0, 1.0) is False  # upper bound is exclusive


def test_in_bucket_no_lower():
    assert _in_bucket(-5.0, None, 0.0) is True


def test_in_bucket_no_upper():
    assert _in_bucket(100.0, 50.0, None) is True


def test_in_bucket_none_value():
    assert _in_bucket(None, 0.0, 1.0) is False


def test_in_bucket_below_lower():
    assert _in_bucket(-1.0, 0.0, 1.0) is False


# ================================================================== unit: _parse_gate_meta / _gate_passed

def _make_gate_json(gate_key: str, passed: bool) -> str:
    return json.dumps({
        "candidate_gate_version": "11C_v1",
        "candidate_gate_passed": passed,
        "gates": {
            gate_key: {"passed": passed, "reason": "test"},
        },
    })


def test_parse_gate_meta_valid():
    row = _make_row(metadata_json=_make_gate_json("GATE_A_ATR_0_10_TO_1_00", True))
    meta = _parse_gate_meta(row)
    assert meta is not None
    assert "gates" in meta


def test_parse_gate_meta_none_raw():
    row = _make_row(metadata_json=None)
    assert _parse_gate_meta(row) is None


def test_parse_gate_meta_invalid_json():
    row = _make_row(metadata_json="not-json")
    assert _parse_gate_meta(row) is None


def test_gate_passed_true():
    row = _make_row(metadata_json=_make_gate_json("GATE_A_ATR_0_10_TO_1_00", True))
    meta = _parse_gate_meta(row)
    assert _gate_passed(meta, "GATE_A_ATR_0_10_TO_1_00") is True


def test_gate_passed_false():
    row = _make_row(metadata_json=_make_gate_json("GATE_A_ATR_0_10_TO_1_00", False))
    meta = _parse_gate_meta(row)
    assert _gate_passed(meta, "GATE_A_ATR_0_10_TO_1_00") is False


def test_gate_passed_missing_key():
    row = _make_row(metadata_json=_make_gate_json("GATE_A_ATR_0_10_TO_1_00", True))
    meta = _parse_gate_meta(row)
    assert _gate_passed(meta, "GATE_B_EMA_SPREAD_NEG_0_25_TO_0") is False


def test_gate_passed_none_meta():
    assert _gate_passed(None, "GATE_A_ATR_0_10_TO_1_00") is False


# ================================================================== unit: _is_dir_concentrated

def test_dir_concentrated_all_long():
    assert _is_dir_concentrated(100, 0) is True


def test_dir_concentrated_all_short():
    assert _is_dir_concentrated(0, 50) is True


def test_dir_concentrated_equal():
    assert _is_dir_concentrated(50, 50) is False


def test_dir_concentrated_just_below_threshold():
    # 89 / 100 = 89% < 90%
    assert _is_dir_concentrated(89, 11) is False


def test_dir_concentrated_at_threshold():
    # 90 / 100 = 90% >= 90%
    assert _is_dir_concentrated(90, 10) is True


def test_dir_concentrated_empty():
    assert _is_dir_concentrated(0, 0) is False


# ================================================================== unit: _cohort_metrics

def test_cohort_metrics_empty():
    m = _cohort_metrics([])
    assert m["n"] == 0
    assert m["tp1_n"] == 0
    assert m["policy_b_avg_r"] is None
    assert m["policy_e_avg_r"] is None
    assert m["avg_mfe"] is None


def test_cohort_metrics_hit2r_counts():
    rows = [_make_row(outcome_status="HIT_2R", outcome_r=2.0,
                      hit_1r_at="now", hit_1r_before_expiry=None)]
    m = _cohort_metrics(rows)
    assert m["n"] == 1
    assert m["hit2r_n"] == 1
    assert m["tp1_n"] == 1
    assert m["stopped_n"] == 0
    assert m["expired_n"] == 0


def test_cohort_metrics_policy_b_avg_r():
    # TP1 → +1R, stop-no-tp1 → -1R, exp-no-tp1 → 0R
    rows = [
        _make_row(outcome_status="HIT_2R", hit_1r_at="now"),           # +1R
        _make_row(outcome_status="STOPPED", outcome_r=-1.0,
                  hit_1r_at=None, hit_1r_before_stop=0),                # -1R
        _make_row(outcome_status="EXPIRED", outcome_r=None,
                  hit_1r_at=None, hit_1r_before_expiry=0),              # 0R
    ]
    m = _cohort_metrics(rows)
    # avg = (1 + -1 + 0) / 3 = 0.0
    assert m["policy_b_avg_r"] == pytest.approx(0.0)


def test_cohort_metrics_policy_e_expired_after_tp1():
    # expired-after-TP1 → +0.5R
    rows = [
        _make_row(outcome_status="EXPIRED", outcome_r=None,
                  hit_1r_at="now", hit_1r_before_expiry=1),
    ]
    m = _cohort_metrics(rows)
    assert m["policy_e_avg_r"] == pytest.approx(0.5)


def test_cohort_metrics_direction_counts():
    rows = [
        _make_row(direction="LONG"),
        _make_row(direction="LONG"),
        _make_row(direction="SHORT"),
    ]
    m = _cohort_metrics(rows)
    assert m["long_n"] == 2
    assert m["short_n"] == 1


def test_cohort_metrics_median_tp1_time():
    rows = [
        _make_row(hit_1r_at="now", time_to_1r_seconds=60.0,
                  outcome_status="HIT_2R"),
        _make_row(hit_1r_at="now", time_to_1r_seconds=180.0,
                  outcome_status="HIT_2R"),
    ]
    m = _cohort_metrics(rows)
    # median of [60, 180] = 120
    assert m["median_tp1_secs"] == pytest.approx(120.0)


def test_cohort_metrics_stopped_no_tp1():
    rows = [
        _make_row(outcome_status="STOPPED", hit_1r_at=None, hit_1r_before_stop=0),
        _make_row(outcome_status="STOPPED", hit_1r_at="now", hit_1r_before_stop=1),
    ]
    m = _cohort_metrics(rows)
    assert m["stopped_no_tp1_n"] == 1


# ================================================================== unit: _cohort_rank_score

def test_cohort_rank_score_none_on_empty():
    m = _cohort_metrics([])
    assert _cohort_rank_score(m, 0.25) is None


def test_cohort_rank_score_positive_high_tp1():
    # High TP1 rate vs baseline → positive score
    rows = [_make_row(outcome_status="HIT_2R", hit_1r_at="now") for _ in range(10)]
    m = _cohort_metrics(rows)
    score = _cohort_rank_score(m, 0.20)  # baseline 20%, cohort 100%
    assert score is not None
    assert score > 0


def test_cohort_rank_score_negative_high_exp_no_tp1():
    rows = [
        _make_row(outcome_status="EXPIRED", hit_1r_at=None,
                  hit_1r_before_expiry=0, outcome_r=None)
        for _ in range(10)
    ]
    m = _cohort_metrics(rows)
    score = _cohort_rank_score(m, 0.20)
    assert score is not None
    assert score < 0


# ================================================================== integration tests

@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_cohorts.db")


def test_empty_db_exits_safely(db_path, monkeypatch):
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    close_db()
    out = _run_cohorts()
    assert "No closed rows available" in out


def test_baseline_row_count_in_output(db_path, monkeypatch):
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid1 = _insert_obs(conn, direction="LONG")
    _insert_feature(conn, uid1, direction="LONG")
    _close_hit2r(conn, uid1)
    uid2 = _insert_obs(conn, direction="SHORT")
    _insert_feature(conn, uid2, direction="SHORT")
    _close_stopped(conn, uid2)
    close_db()
    out = _run_cohorts()
    assert "Closed rows" in out
    assert "2" in out


def test_section_a_renders(db_path, monkeypatch):
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature(conn, uid)
    _close_hit2r(conn, uid)
    close_db()
    out = _run_cohorts()
    assert "A — Overall Baseline" in out
    assert "BASELINE_ALL" in out


def test_section_b_direction_split(db_path, monkeypatch):
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    for _ in range(3):
        uid = _insert_obs(conn, direction="LONG")
        _insert_feature(conn, uid, direction="LONG")
        _close_hit2r(conn, uid)
    uid = _insert_obs(conn, direction="SHORT")
    _insert_feature(conn, uid, direction="SHORT")
    _close_stopped(conn, uid)
    close_db()
    out = _run_cohorts()
    assert "B — Direction Cohorts" in out
    assert "LONG" in out
    assert "SHORT" in out


def test_section_c_symbol_groups(db_path, monkeypatch):
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    for sym in ["BTC", "ETH", "SOL"]:
        uid = _insert_obs(conn, symbol=sym)
        _insert_feature(conn, uid, symbol=sym)
        _close_hit2r(conn, uid)
    close_db()
    out = _run_cohorts()
    assert "C — Symbol Cohorts" in out
    # At least one symbol should appear
    assert any(sym in out for sym in ("BTC", "ETH", "SOL"))


def test_section_d_utc_hour(db_path, monkeypatch):
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn, observed_at="2026-05-21T14:30:00Z")
    _insert_feature(conn, uid, observed_at="2026-05-21T14:30:00Z")
    _close_hit2r(conn, uid)
    close_db()
    out = _run_cohorts()
    assert "D — UTC Hour Cohorts" in out
    assert "14:00 UTC" in out


def test_section_e_session_us(db_path, monkeypatch):
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn, observed_at="2026-05-21T15:00:00Z")
    _insert_feature(conn, uid, observed_at="2026-05-21T15:00:00Z")
    _close_hit2r(conn, uid)
    close_db()
    out = _run_cohorts()
    assert "E — Session Cohorts" in out
    assert "US" in out


def test_section_e_session_asia(db_path, monkeypatch):
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn, observed_at="2026-05-21T03:00:00Z")
    _insert_feature(conn, uid, observed_at="2026-05-21T03:00:00Z")
    _close_hit2r(conn, uid)
    close_db()
    out = _run_cohorts()
    assert "ASIA" in out


def test_section_f_gate_cohorts(db_path, monkeypatch):
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    gate_json = json.dumps({
        "candidate_gate_version": "11C_v1",
        "candidate_gate_passed": True,
        "gates": {
            "GATE_A_ATR_0_10_TO_1_00": {"passed": True, "reason": "ok"},
            "GATE_B_EMA_SPREAD_NEG_0_25_TO_0": {"passed": False, "reason": "miss"},
            "GATE_C_RSI_50_TO_60": {"passed": False, "reason": "miss"},
            "GATE_D_ATR_AND_EMA_COMBINED": {"passed": False, "reason": "miss"},
        },
    })
    uid = _insert_obs(conn)
    _insert_feature(conn, uid, metadata_json=gate_json)
    _close_hit2r(conn, uid)
    close_db()
    out = _run_cohorts()
    assert "F — Candidate Gate Cohorts" in out
    assert "GATE_PASSED_ANY" in out


def test_section_g_rsi_bucket(db_path, monkeypatch):
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature(conn, uid, rsi_val=55.0)
    _close_hit2r(conn, uid)
    close_db()
    out = _run_cohorts()
    assert "G — Indicator Bucket Cohorts" in out
    assert "RSI 50-60" in out


def test_section_g_atr_bucket(db_path, monkeypatch):
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature(conn, uid, atr_pct=0.35)
    _close_hit2r(conn, uid)
    close_db()
    out = _run_cohorts()
    assert "ATR% 0.20-0.50" in out


def test_section_h_tp1_timing_bucket(db_path, monkeypatch):
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature(conn, uid)
    _close_hit2r(conn, uid, t1=180.0)  # 3 minutes → <=5m bucket
    close_db()
    out = _run_cohorts()
    assert "H — Time-to-TP1 Cohorts" in out
    assert "<=5m" in out


def test_section_h_no_tp1_bucket(db_path, monkeypatch):
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature(conn, uid)
    _close_expired(conn, uid, hit_1r=0)
    close_db()
    out = _run_cohorts()
    assert "NO_TP1" in out


def test_section_i_ranking_appears(db_path, monkeypatch):
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    for _ in range(3):
        uid = _insert_obs(conn, direction="LONG")
        _insert_feature(conn, uid, direction="LONG")
        _close_hit2r(conn, uid)
    for _ in range(3):
        uid = _insert_obs(conn, direction="SHORT")
        _insert_feature(conn, uid, direction="SHORT")
        _close_expired(conn, uid)
    close_db()
    out = _run_cohorts()
    assert "I — Best / Worst Cohort Ranking" in out
    assert "Score" in out


def test_dir_concentrated_warning_emitted(db_path, monkeypatch):
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    # 10 SHORT, 0 LONG → 100% SHORT → [DIR-CONCENTRATED]
    for _ in range(10):
        uid = _insert_obs(conn, direction="SHORT")
        _insert_feature(conn, uid, direction="SHORT")
        _close_expired(conn, uid)
    close_db()
    out = _run_cohorts()
    assert "DIR-CONCENTRATED" in out


def test_insufficient_n_warning(db_path, monkeypatch):
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn, direction="LONG", symbol="RARE")
    _insert_feature(conn, uid, direction="LONG", symbol="RARE")
    _close_hit2r(conn, uid)
    close_db()
    # min-n=10 means 1 row triggers INSUFFICIENT
    out = _run_cohorts(["--min-n", "10"])
    assert "INSUFFICIENT" in out


def test_feature_version_filter(db_path, monkeypatch):
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature(conn, uid)
    _close_hit2r(conn, uid)
    close_db()
    # Filter for a version that doesn't exist → 0 closed rows
    out = _run_cohorts(["--feature-version", "nonexistent_v99"])
    assert "No closed rows available" in out


def test_since_filter(db_path, monkeypatch):
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature(conn, uid)
    _close_hit2r(conn, uid)
    close_db()
    # Future timestamp → no rows
    out = _run_cohorts(["--since", "2099-01-01T00:00:00Z"])
    assert "No closed rows available" in out


def test_no_db_writes(db_path, monkeypatch):
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature(conn, uid)
    _close_hit2r(conn, uid)
    close_db()

    # Capture mtime before
    import os
    mtime_before = os.path.getmtime(db_path)

    _run_cohorts()

    mtime_after = os.path.getmtime(db_path)
    assert mtime_after == pytest.approx(mtime_before, abs=0.1)


def test_snapshot_integration_no_crash(db_path, monkeypatch):
    """Snapshot script can import and call analyze_exit_timing_cohorts without crash."""
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    close_db()
    from scripts.create_apex_snapshot import main as snap_main
    buf = io.StringIO()
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        snap_path = f.name
    try:
        with redirect_stdout(buf):
            snap_main(["--out", snap_path])
        content = Path(snap_path).read_text()
        assert "Exit / Timing Cohort Analysis Report (11E)" in content
    finally:
        os.unlink(snap_path)


def test_direct_import():
    """Script is importable as a module."""
    import scripts.analyze_exit_timing_cohorts as mod
    assert callable(mod.main)
    assert callable(mod._cohort_metrics)
    assert callable(mod._hour_from_observed_at)


def test_top_symbols_flag(db_path, monkeypatch):
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    for sym in ["AAA", "BBB", "CCC"]:
        uid = _insert_obs(conn, symbol=sym)
        _insert_feature(conn, uid, symbol=sym)
        _close_hit2r(conn, uid)
    close_db()
    # Limit to top 2 symbols
    out = _run_cohorts(["--top-symbols", "2"])
    assert "C — Symbol Cohorts" in out
