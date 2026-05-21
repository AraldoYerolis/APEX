"""Tests for Milestone 10B: signal_features table and learning report.

Covers:
- insert_signal_feature inserts a row
- insert_signal_feature is idempotent (INSERT OR IGNORE) on duplicate obs uid
- get_signal_features returns rows, respects --since filter
- update_signal_feature_outcome_from_observation syncs correctly
- report_signal_learning runs without error (empty DB, rows with outcomes)
- snapshot includes Learning Report section
"""
from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from apex.db import repository as repo
from apex.db.connection import close_db, init_db
from apex.db.models import SignalFeature, SignalObservation
from apex.utils.ids import new_uid
from apex.utils.time import minutes_ago_iso, minutes_from_now, utcnow_iso


# ------------------------------------------------------------------ helpers

def _make_env_overrides(monkeypatch, db_path: str) -> None:
    monkeypatch.setenv("APEX_DB_PATH", db_path)
    monkeypatch.setenv("ACTION_TOKEN_SECRET", "test-10b-feat-secret")
    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "fake")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "fake")
    monkeypatch.setenv("ALERTS_ENABLED", "false")
    monkeypatch.setenv("DRY_RUN_MODE", "true")


def _insert_obs(conn, observed_at: str, symbol: str = "BTC") -> str:
    uid = new_uid()
    obs = SignalObservation(
        observation_uid=uid,
        observed_at=observed_at,
        symbol=symbol,
        direction="LONG",
        signal_type="CONFIRMED_SETUP",
        entry_price=100.0,
        stop_price=99.0,
        target_1r=101.0,
        target_2r=102.0,
        expires_at=minutes_from_now(15),
    )
    repo.insert_signal_observation(conn, obs)
    return uid


def _make_feature(obs_uid: str, captured_at: str, symbol: str = "BTC") -> SignalFeature:
    return SignalFeature(
        observation_uid=obs_uid,
        captured_at=captured_at,
        symbol=symbol,
        direction="LONG",
        signal_type="CONFIRMED_SETUP",
        observed_at=captured_at,
        entry_price=100.0,
        stop_price=99.0,
        target_1r=101.0,
        target_2r=102.0,
        candle_open=99.5,
        candle_high=101.0,
        candle_low=99.0,
        candle_close=100.5,
        candle_volume=1000.0,
        candle_open_time=1700000000000,
        rsi_val=45.0,
        atr_val=1.0,
        vwap_val=100.2,
        ema_fast=100.1,
        ema_slow=99.8,
        price_vs_vwap_pct=0.3,
        ema_spread_pct=0.3,
        atr_pct=1.0,
        trend_bias="LONG",
        trend_reason="EMA bullish",
        pullback_state="CONFIRMED_SETUP",
        pullback_reason="RSI ok",
        btc_trend_bias="LONG",
        eth_trend_bias="LONG",
    )


# ------------------------------------------------------------------ DB layer tests

def test_insert_signal_feature(tmp_path, monkeypatch):
    """insert_signal_feature inserts a row that can be retrieved."""
    db_path = str(tmp_path / "feat.db")
    _make_env_overrides(monkeypatch, db_path)
    conn = init_db(db_path)

    uid = _insert_obs(conn, utcnow_iso())
    feature = _make_feature(uid, utcnow_iso())
    repo.insert_signal_feature(conn, feature)

    rows = repo.get_signal_features(conn)
    assert len(rows) == 1
    assert rows[0]["observation_uid"] == uid
    assert rows[0]["symbol"] == "BTC"
    assert rows[0]["rsi_val"] == pytest.approx(45.0)
    assert rows[0]["feature_version"] == "11C_v1"
    close_db()


def test_insert_signal_feature_idempotent(tmp_path, monkeypatch):
    """INSERT OR IGNORE: inserting the same obs_uid twice does not raise."""
    db_path = str(tmp_path / "feat_idem.db")
    _make_env_overrides(monkeypatch, db_path)
    conn = init_db(db_path)

    uid = _insert_obs(conn, utcnow_iso())
    feature = _make_feature(uid, utcnow_iso())
    repo.insert_signal_feature(conn, feature)
    # Second insert: should not raise, row count stays at 1
    repo.insert_signal_feature(conn, feature)

    rows = repo.get_signal_features(conn)
    assert len(rows) == 1
    close_db()


def test_get_signal_features_since_filter(tmp_path, monkeypatch):
    """get_signal_features with since= filters old rows."""
    db_path = str(tmp_path / "feat_since.db")
    _make_env_overrides(monkeypatch, db_path)
    conn = init_db(db_path)

    old_uid = _insert_obs(conn, minutes_ago_iso(120), symbol="OLD")
    new_uid_ = _insert_obs(conn, utcnow_iso(), symbol="NEW")

    old_feat = _make_feature(old_uid, minutes_ago_iso(120), symbol="OLD")
    new_feat = _make_feature(new_uid_, utcnow_iso(), symbol="NEW")
    repo.insert_signal_feature(conn, old_feat)
    repo.insert_signal_feature(conn, new_feat)

    all_rows = repo.get_signal_features(conn)
    assert len(all_rows) == 2

    recent_rows = repo.get_signal_features(conn, since=minutes_ago_iso(60))
    assert len(recent_rows) == 1
    assert recent_rows[0]["symbol"] == "NEW"
    close_db()


def test_update_signal_feature_outcome_syncs_correctly(tmp_path, monkeypatch):
    """After closing an observation, outcome sync correctly updates signal_features."""
    db_path = str(tmp_path / "feat_outcome.db")
    _make_env_overrides(monkeypatch, db_path)
    conn = init_db(db_path)

    uid = _insert_obs(conn, utcnow_iso())
    feature = _make_feature(uid, utcnow_iso())
    repo.insert_signal_feature(conn, feature)

    # Verify no outcome yet
    rows = repo.get_signal_features(conn)
    assert rows[0]["outcome_status"] is None

    # Close the observation as HIT_2R
    now = utcnow_iso()
    repo.close_signal_observation(
        conn, uid,
        status="HIT_2R", outcome_r=2.0, closed_at=now,
        hit_2r_at=now, time_to_2r_seconds=300.0,
        first_terminal_status="HIT_2R", final_status="HIT_2R",
    )

    # Sync outcomes
    repo.update_signal_feature_outcome_from_observation(conn, uid)

    rows = repo.get_signal_features(conn)
    assert rows[0]["outcome_status"] == "HIT_2R"
    assert rows[0]["outcome_r"] == pytest.approx(2.0)
    assert rows[0]["time_to_2r_seconds"] == pytest.approx(300.0)
    close_db()


def test_update_outcome_noop_when_no_feature_row(tmp_path, monkeypatch):
    """update_signal_feature_outcome_from_observation is a no-op when no feature row exists."""
    db_path = str(tmp_path / "feat_noop.db")
    _make_env_overrides(monkeypatch, db_path)
    conn = init_db(db_path)

    uid = _insert_obs(conn, utcnow_iso())
    # No feature row inserted
    # Should not raise
    repo.update_signal_feature_outcome_from_observation(conn, uid)
    close_db()


def test_update_outcome_noop_when_obs_not_found(tmp_path, monkeypatch):
    """update_signal_feature_outcome_from_observation is a no-op for unknown uid."""
    db_path = str(tmp_path / "feat_unknown.db")
    _make_env_overrides(monkeypatch, db_path)
    conn = init_db(db_path)

    repo.update_signal_feature_outcome_from_observation(conn, "nonexistent-uid")
    close_db()


def test_tp1_milestone_syncs_to_features(tmp_path, monkeypatch):
    """After recording TP1 milestone, outcome_status in features is updated to OBSERVED with hit_1r_at set."""
    db_path = str(tmp_path / "feat_tp1.db")
    _make_env_overrides(monkeypatch, db_path)
    conn = init_db(db_path)

    uid = _insert_obs(conn, utcnow_iso())
    feature = _make_feature(uid, utcnow_iso())
    repo.insert_signal_feature(conn, feature)

    now = utcnow_iso()
    repo.record_tp1_milestone(conn, uid, hit_at=now, time_seconds=120.0)
    repo.update_signal_feature_outcome_from_observation(conn, uid)

    rows = repo.get_signal_features(conn)
    assert rows[0]["hit_1r_at"] == now
    # status is still OBSERVED after TP1 milestone
    assert rows[0]["outcome_status"] == "OBSERVED"
    close_db()


def test_stopped_outcome_syncs_hit_1r_before_stop(tmp_path, monkeypatch):
    """hit_1r_before_stop is synced correctly."""
    db_path = str(tmp_path / "feat_stop.db")
    _make_env_overrides(monkeypatch, db_path)
    conn = init_db(db_path)

    uid = _insert_obs(conn, utcnow_iso())
    feature = _make_feature(uid, utcnow_iso())
    repo.insert_signal_feature(conn, feature)

    now = utcnow_iso()
    repo.close_signal_observation(
        conn, uid,
        status="STOPPED", outcome_r=-1.0, closed_at=now,
        stopped_at=now, time_to_stop_seconds=180.0,
        hit_1r_before_stop=0,
        first_terminal_status="STOPPED", final_status="STOPPED",
    )
    repo.update_signal_feature_outcome_from_observation(conn, uid)

    rows = repo.get_signal_features(conn)
    assert rows[0]["outcome_status"] == "STOPPED"
    assert rows[0]["outcome_r"] == pytest.approx(-1.0)
    assert rows[0]["hit_1r_before_stop"] == 0
    close_db()


def test_signal_features_table_exists_after_init(tmp_path, monkeypatch):
    """signal_features table is created by init_db on fresh installs."""
    db_path = str(tmp_path / "fresh.db")
    _make_env_overrides(monkeypatch, db_path)
    conn = init_db(db_path)

    result = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='signal_features'"
    ).fetchone()
    assert result is not None, "signal_features table was not created"
    close_db()


# ------------------------------------------------------------------ report_signal_learning tests

def test_report_learning_empty_db(tmp_path, monkeypatch):
    """report_signal_learning runs cleanly when no features are captured yet."""
    db_path = str(tmp_path / "learn_empty.db")
    _make_env_overrides(monkeypatch, db_path)
    conn = init_db(db_path)
    close_db()

    import scripts.report_signal_learning as mod

    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main([])

    output = buf.getvalue()
    assert "APEX Signal Learning Report" in output
    assert "No signal features captured yet" in output


def test_report_learning_with_rows(tmp_path, monkeypatch):
    """report_signal_learning shows totals when features are present."""
    db_path = str(tmp_path / "learn_rows.db")
    _make_env_overrides(monkeypatch, db_path)
    conn = init_db(db_path)

    uid1 = _insert_obs(conn, utcnow_iso(), symbol="BTC")
    uid2 = _insert_obs(conn, utcnow_iso(), symbol="ETH")
    repo.insert_signal_feature(conn, _make_feature(uid1, utcnow_iso(), symbol="BTC"))
    repo.insert_signal_feature(conn, _make_feature(uid2, utcnow_iso(), symbol="ETH"))
    close_db()

    import scripts.report_signal_learning as mod

    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main([])

    output = buf.getvalue()
    assert "APEX Signal Learning Report" in output
    assert "Total features      : 2" in output


def test_report_learning_since_filter(tmp_path, monkeypatch):
    """--since filters features older than the cutoff."""
    db_path = str(tmp_path / "learn_since.db")
    _make_env_overrides(monkeypatch, db_path)
    conn = init_db(db_path)

    old_uid = _insert_obs(conn, minutes_ago_iso(120), symbol="OLD")
    new_uid_ = _insert_obs(conn, utcnow_iso(), symbol="NEW")
    repo.insert_signal_feature(conn, _make_feature(old_uid, minutes_ago_iso(120), symbol="OLD"))
    repo.insert_signal_feature(conn, _make_feature(new_uid_, utcnow_iso(), symbol="NEW"))
    close_db()

    import scripts.report_signal_learning as mod

    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main(["--since", minutes_ago_iso(60)])

    output = buf.getvalue()
    assert "Filtered since" in output
    assert "Total features      : 1" in output


def test_report_learning_with_outcomes(tmp_path, monkeypatch):
    """Report shows outcome summary when features have synced outcomes."""
    db_path = str(tmp_path / "learn_outcomes.db")
    _make_env_overrides(monkeypatch, db_path)
    conn = init_db(db_path)

    uid = _insert_obs(conn, utcnow_iso())
    repo.insert_signal_feature(conn, _make_feature(uid, utcnow_iso()))
    # Close observation and sync
    now = utcnow_iso()
    repo.close_signal_observation(
        conn, uid,
        status="HIT_2R", outcome_r=2.0, closed_at=now,
        hit_2r_at=now, time_to_2r_seconds=200.0,
        first_terminal_status="HIT_2R", final_status="HIT_2R",
    )
    repo.update_signal_feature_outcome_from_observation(conn, uid)
    close_db()

    import scripts.report_signal_learning as mod

    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main([])

    output = buf.getvalue()
    assert "Outcome summary" in output
    assert "HIT_2R" in output


def test_report_learning_csv_export(tmp_path, monkeypatch):
    """--csv flag writes a CSV file."""
    db_path = str(tmp_path / "learn_csv.db")
    csv_path = str(tmp_path / "features.csv")
    _make_env_overrides(monkeypatch, db_path)
    conn = init_db(db_path)

    uid = _insert_obs(conn, utcnow_iso())
    repo.insert_signal_feature(conn, _make_feature(uid, utcnow_iso()))
    close_db()

    import scripts.report_signal_learning as mod

    with redirect_stdout(io.StringIO()):
        mod.main(["--csv", csv_path])

    p = Path(csv_path)
    assert p.exists()
    content = p.read_text()
    assert "observation_uid" in content  # CSV header
    assert "BTC" in content


# ------------------------------------------------------------------ snapshot integration

def test_snapshot_includes_learning_section(tmp_path, monkeypatch):
    """create_apex_snapshot includes a Signal Learning Report section."""
    db_path = str(tmp_path / "snap_learn.db")
    out_path = str(tmp_path / "snap_learn.txt")
    _make_env_overrides(monkeypatch, db_path)

    conn = init_db(db_path)
    close_db()

    import scripts.create_apex_snapshot as mod

    with redirect_stdout(io.StringIO()):
        mod.main(["--out", out_path])

    content = Path(out_path).read_text()
    assert "Signal Learning Report" in content
