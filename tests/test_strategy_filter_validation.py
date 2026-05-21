"""Tests for Milestone 11B: Out-of-Sample Filter Validation.

Covers:
- script runs on empty DB (no crash)
- baseline sections appear in output
- train/validation split is chronological and sized correctly
- VALIDATED_CANDIDATE label when both sets improve
- TRAIN_ONLY_OVERFIT label when only train improves
- INSUFFICIENT_VALIDATION_SAMPLE label when val_n < min_n
- HIGH_EXPIRY_RISK label when val expiry >= 80%
- FAILS_VALIDATION label when val clearly worsens
- VALIDATION_ONLY_REGIME_SHIFT label when only val improves
- PROMISING_NEEDS_MORE_DATA label for weak/mixed signals
- stability ranking section appears
- overfit warning section appears
- interpretation section appears
- CSV export writes rows with expected columns
- snapshot includes Out-of-Sample Filter Validation Report section
- script does not write to DB (read-only)
- script can be imported directly
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


# ------------------------------------------------------------------ helpers

def _make_env(monkeypatch, db_path: str) -> None:
    monkeypatch.setenv("APEX_DB_PATH", db_path)
    monkeypatch.setenv("ACTION_TOKEN_SECRET", "test-11b")
    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "fake")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "fake")
    monkeypatch.setenv("ALERTS_ENABLED", "false")
    monkeypatch.setenv("DRY_RUN_MODE", "true")


def _insert_obs(conn, symbol: str = "BTC", direction: str = "LONG") -> str:
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


def _insert_feature(
    conn,
    uid: str,
    symbol: str = "BTC",
    direction: str = "LONG",
    rsi_val: float = 50.0,
    atr_pct: float = 0.30,
    price_vs_vwap_pct: float = 0.10,
    ema_spread_pct: float = 0.20,
    btc_trend_bias: str = "LONG",
    eth_trend_bias: str = "LONG",
) -> None:
    feat = SignalFeature(
        observation_uid=uid,
        captured_at=utcnow_iso(),
        symbol=symbol,
        direction=direction,
        signal_type="CONFIRMED_SETUP",
        observed_at=utcnow_iso(),
        entry_price=100.0,
        stop_price=99.0,
        target_1r=101.0,
        target_2r=102.0,
        rsi_val=rsi_val,
        atr_val=atr_pct,
        vwap_val=100.0,
        ema_fast=100.1,
        ema_slow=99.9,
        price_vs_vwap_pct=price_vs_vwap_pct,
        ema_spread_pct=ema_spread_pct,
        atr_pct=atr_pct,
        trend_bias=direction,
        trend_reason="test",
        pullback_state="CONFIRMED_SETUP",
        pullback_reason="test",
        btc_trend_bias=btc_trend_bias,
        eth_trend_bias=eth_trend_bias,
    )
    repo.insert_signal_feature(conn, feat)


def _close_hit1r(conn, uid: str) -> None:
    now = utcnow_iso()
    repo.close_signal_observation(
        conn, uid,
        status="HIT_1R", outcome_r=1.0, closed_at=now,
        hit_1r_at=now, time_to_1r_seconds=120.0,
        first_terminal_status="HIT_1R", final_status="HIT_1R",
    )
    repo.update_signal_feature_outcome_from_observation(conn, uid)


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


def _make_closed_row(conn, direction="LONG", atr_pct=0.30, close_fn=None):
    """Insert observation + feature, close it, return uid."""
    uid = _insert_obs(conn, direction=direction)
    _insert_feature(conn, uid, direction=direction, atr_pct=atr_pct)
    if close_fn is None:
        close_fn = _close_hit2r
    close_fn(conn, uid)
    return uid


def _run_validator(argv=None):
    import scripts.validate_strategy_filters as mod
    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main(argv or [])
    return buf.getvalue()


# ------------------------------------------------------------------ unit tests: _stability_label

from scripts.validate_strategy_filters import _stability_label, _stability_score


def test_stability_label_insufficient_validation_sample():
    """val_n < min_n → INSUFFICIENT_VALIDATION_SAMPLE regardless of other params."""
    label = _stability_label(
        train_n=20, val_n=5, min_n=10,
        train_d_tp1=0.10, train_d_r=0.10,
        val_d_tp1=0.10, val_d_r=0.10,
        val_d_stop=-0.05, val_exp_rate=0.30,
    )
    assert label == "INSUFFICIENT_VALIDATION_SAMPLE"


def test_stability_label_high_expiry_risk():
    """val expiry >= 80% → HIGH_EXPIRY_RISK even when metrics improve."""
    label = _stability_label(
        train_n=30, val_n=15, min_n=10,
        train_d_tp1=0.10, train_d_r=0.10,
        val_d_tp1=0.05, val_d_r=0.05,
        val_d_stop=0.00, val_exp_rate=0.85,
    )
    assert label == "HIGH_EXPIRY_RISK"


def test_stability_label_fails_validation():
    """val clearly worsens on TP1 and avgR → FAILS_VALIDATION."""
    label = _stability_label(
        train_n=30, val_n=15, min_n=10,
        train_d_tp1=0.05, train_d_r=0.03,
        val_d_tp1=-0.05, val_d_r=-0.08,
        val_d_stop=0.02, val_exp_rate=0.40,
    )
    assert label == "FAILS_VALIDATION"


def test_stability_label_train_only_overfit():
    """Train improves but val does not → TRAIN_ONLY_OVERFIT."""
    label = _stability_label(
        train_n=30, val_n=15, min_n=10,
        train_d_tp1=0.08, train_d_r=0.06,
        val_d_tp1=0.00, val_d_r=0.00,
        val_d_stop=0.01, val_exp_rate=0.40,
    )
    assert label == "TRAIN_ONLY_OVERFIT"


def test_stability_label_validation_only_regime_shift():
    """Val improves but train was weak → VALIDATION_ONLY_REGIME_SHIFT."""
    label = _stability_label(
        train_n=30, val_n=15, min_n=10,
        train_d_tp1=0.00, train_d_r=0.00,
        val_d_tp1=0.05, val_d_r=0.06,
        val_d_stop=0.00, val_exp_rate=0.30,
    )
    assert label == "VALIDATION_ONLY_REGIME_SHIFT"


def test_stability_label_validated_candidate():
    """Both train and val improve, low expiry, stop not worse → VALIDATED_CANDIDATE."""
    label = _stability_label(
        train_n=30, val_n=15, min_n=10,
        train_d_tp1=0.06, train_d_r=0.05,
        val_d_tp1=0.04, val_d_r=0.04,
        val_d_stop=0.01, val_exp_rate=0.35,
    )
    assert label == "VALIDATED_CANDIDATE"


def test_stability_label_promising_needs_more_data():
    """Both train and val show no improvement above threshold → PROMISING_NEEDS_MORE_DATA."""
    # train_d_tp1=0.005 <= 0.01 and train_d_r=0.010 <= 0.02 → train_improves=False
    # val_d_tp1=0.005 <= 0.01 and val_d_r=0.010 <= 0.02 → val_improves=False
    # val_d_tp1 > -0.02 → val_worsens=False
    # Falls through all branches → PROMISING_NEEDS_MORE_DATA
    label = _stability_label(
        train_n=30, val_n=15, min_n=10,
        train_d_tp1=0.005, train_d_r=0.010,
        val_d_tp1=0.005, val_d_r=0.010,
        val_d_stop=0.01, val_exp_rate=0.35,
    )
    assert label == "PROMISING_NEEDS_MORE_DATA"


def test_stability_score_penalizes_high_expiry():
    """Score is lower when val expiry >= 80%."""
    score_low = _stability_score(
        val_d_tp1=0.05, val_d_r=0.05, val_d_stop=-0.02,
        val_d_exp_no_tp1=0.0, val_exp_rate=0.30,
        val_n=20, min_n=10, train_improves=True,
    )
    score_high = _stability_score(
        val_d_tp1=0.05, val_d_r=0.05, val_d_stop=-0.02,
        val_d_exp_no_tp1=0.0, val_exp_rate=0.85,
        val_n=20, min_n=10, train_improves=True,
    )
    assert score_low > score_high


def test_stability_score_penalizes_train_not_improving():
    """Score is lower when train does not improve (regime shift discount)."""
    score_train_ok = _stability_score(
        val_d_tp1=0.05, val_d_r=0.05, val_d_stop=-0.02,
        val_d_exp_no_tp1=0.0, val_exp_rate=0.30,
        val_n=20, min_n=10, train_improves=True,
    )
    score_train_no = _stability_score(
        val_d_tp1=0.05, val_d_r=0.05, val_d_stop=-0.02,
        val_d_exp_no_tp1=0.0, val_exp_rate=0.30,
        val_n=20, min_n=10, train_improves=False,
    )
    assert score_train_ok > score_train_no


# ------------------------------------------------------------------ integration: empty DB

def test_validator_runs_empty_db(tmp_path, monkeypatch):
    """Script exits cleanly with no crash when DB is empty."""
    db_path = str(tmp_path / "empty.db")
    _make_env(monkeypatch, db_path)
    init_db(db_path)
    close_db()

    output = _run_validator()
    assert "Out-of-Sample Filter Validation" in output
    assert "REPORT-ONLY" in output


def test_validator_empty_db_exits_gracefully(tmp_path, monkeypatch):
    """Empty DB prints 'No closed CONFIRMED_SETUP rows' message."""
    db_path = str(tmp_path / "empty.db")
    _make_env(monkeypatch, db_path)
    init_db(db_path)
    close_db()

    output = _run_validator()
    assert "No closed CONFIRMED_SETUP rows" in output


# ------------------------------------------------------------------ integration: baseline

def test_validator_baseline_sections_appear(tmp_path, monkeypatch):
    """Baseline performance section appears when there is data."""
    db_path = str(tmp_path / "baseline.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    for _ in range(20):
        _make_closed_row(conn)
    close_db()

    output = _run_validator()
    assert "Baseline performance" in output
    assert "Full dataset" in output
    assert "Train" in output
    assert "Validation" in output


# ------------------------------------------------------------------ integration: split logic

def test_validator_split_counts(tmp_path, monkeypatch):
    """70/30 split prints correct train and validation row counts."""
    db_path = str(tmp_path / "split.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    for _ in range(20):
        _make_closed_row(conn)
    close_db()

    output = _run_validator(["--split", "0.70"])
    # 20 rows × 0.70 = 14 train, 6 validation
    assert "Train rows        : 14" in output
    assert "Validation rows   : 6" in output


def test_validator_split_80_20(tmp_path, monkeypatch):
    """80/20 split sizes are correct."""
    db_path = str(tmp_path / "split80.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    for _ in range(10):
        _make_closed_row(conn)
    close_db()

    output = _run_validator(["--split", "0.80"])
    assert "Train rows        : 8" in output
    assert "Validation rows   : 2" in output


# ------------------------------------------------------------------ integration: sections

def test_validator_per_filter_table_section_appears(tmp_path, monkeypatch):
    """Per-filter train vs validation section appears when data is sufficient."""
    db_path = str(tmp_path / "table.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    for _ in range(30):
        _make_closed_row(conn)
    close_db()

    output = _run_validator()
    assert "Per-filter train vs validation results" in output


def test_validator_stability_ranking_section_appears(tmp_path, monkeypatch):
    """Stability ranking section appears when there is data."""
    db_path = str(tmp_path / "rank.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    for _ in range(30):
        _make_closed_row(conn)
    close_db()

    output = _run_validator()
    assert "Out-of-sample stability ranking" in output


def test_validator_overfit_warning_section_appears(tmp_path, monkeypatch):
    """Overfit/unstable warning section appears when there is data."""
    db_path = str(tmp_path / "overfit.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    for _ in range(30):
        _make_closed_row(conn)
    close_db()

    output = _run_validator()
    assert "Likely overfit / unstable filters" in output


def test_validator_interpretation_section_appears(tmp_path, monkeypatch):
    """Interpretation section appears when there is data."""
    db_path = str(tmp_path / "interp.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    for _ in range(30):
        _make_closed_row(conn)
    close_db()

    output = _run_validator()
    assert "Interpretation" in output
    assert "REPORT-ONLY VALIDATION" in output


# ------------------------------------------------------------------ integration: CSV export

def test_validator_csv_export(tmp_path, monkeypatch):
    """--csv flag writes a CSV with expected header columns."""
    db_path = str(tmp_path / "csv.db")
    csv_path = str(tmp_path / "out.csv")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    for _ in range(20):
        _make_closed_row(conn)
    close_db()

    _run_validator(["--csv", csv_path])
    assert Path(csv_path).exists()
    content = Path(csv_path).read_text()
    assert "name" in content
    assert "stability" in content
    assert "train_d_tp1" in content
    assert "val_d_tp1" in content


def test_validator_csv_has_expected_row_count(tmp_path, monkeypatch):
    """CSV has one row per candidate filter."""
    db_path = str(tmp_path / "csv2.db")
    csv_path = str(tmp_path / "out2.csv")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    for _ in range(20):
        _make_closed_row(conn)
    close_db()

    import scripts.validate_strategy_filters as mod
    _run_validator(["--csv", csv_path])
    lines = Path(csv_path).read_text().strip().splitlines()
    # header + one row per candidate filter
    assert len(lines) == len(mod._CANDIDATE_FILTERS) + 1


# ------------------------------------------------------------------ integration: read-only

def test_validator_does_not_write_to_db(tmp_path, monkeypatch):
    """Running the validator does not change the DB row count."""
    db_path = str(tmp_path / "readonly.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    for _ in range(20):
        _make_closed_row(conn)
    close_db()

    conn2 = sqlite3.connect(db_path)
    before = conn2.execute("SELECT COUNT(*) FROM signal_features").fetchone()[0]
    conn2.close()

    _run_validator()

    conn3 = sqlite3.connect(db_path)
    after = conn3.execute("SELECT COUNT(*) FROM signal_features").fetchone()[0]
    conn3.close()

    assert before == after


# ------------------------------------------------------------------ integration: snapshot

def test_validator_snapshot_section_appears(tmp_path, monkeypatch):
    """create_apex_snapshot includes Out-of-Sample Filter Validation Report section."""
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
    assert "Out-of-Sample Filter Validation Report" in content


# ------------------------------------------------------------------ direct import

def test_validator_direct_script_import_works():
    """Script can be imported directly without PYTHONPATH=.:src hack."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "validate_strategy_filters",
        Path(__file__).resolve().parent.parent / "scripts" / "validate_strategy_filters.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "main")
    assert hasattr(mod, "_stability_label")
    assert hasattr(mod, "_CANDIDATE_FILTERS")
