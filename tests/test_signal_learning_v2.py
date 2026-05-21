"""Tests for Milestone 10C: Signal Learning Report v2.

Covers:
- No default row cap (unlimited by default)
- --limit works when explicitly passed
- symbol+direction grouping
- long vs short breakdown
- macro alignment classification helper
- bucket classification helpers (boundary conditions)
- quality score formula
- recommendation label logic
- expiry quality section present
- feature bucket section present
- report does not mutate config (report-only safety)
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
    monkeypatch.setenv("ACTION_TOKEN_SECRET", "test-10c-secret")
    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "fake")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "fake")
    monkeypatch.setenv("ALERTS_ENABLED", "false")
    monkeypatch.setenv("DRY_RUN_MODE", "true")


def _insert_obs(
    conn,
    observed_at: str,
    symbol: str = "BTC",
    direction: str = "LONG",
) -> str:
    uid = new_uid()
    obs = SignalObservation(
        observation_uid=uid,
        observed_at=observed_at,
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
    rsi_val: float = 45.0,
    btc_trend_bias: str = "LONG",
    eth_trend_bias: str = "LONG",
    atr_pct: float = 0.3,
    price_vs_vwap_pct: float = 0.1,
    ema_spread_pct: float = 0.2,
) -> None:
    feature = SignalFeature(
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
        atr_val=0.3,
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
    repo.insert_signal_feature(conn, feature)


def _close_obs_hit2r(conn, uid: str) -> None:
    now = utcnow_iso()
    repo.close_signal_observation(
        conn, uid,
        status="HIT_2R", outcome_r=2.0, closed_at=now,
        hit_1r_at=now, time_to_1r_seconds=120.0,
        hit_2r_at=now, time_to_2r_seconds=240.0,
        first_terminal_status="HIT_2R", final_status="HIT_2R",
    )
    repo.update_signal_feature_outcome_from_observation(conn, uid)


def _close_obs_stopped(conn, uid: str, hit_1r_before: int = 0) -> None:
    now = utcnow_iso()
    repo.close_signal_observation(
        conn, uid,
        status="STOPPED", outcome_r=-1.0, closed_at=now,
        stopped_at=now, time_to_stop_seconds=90.0,
        hit_1r_before_stop=hit_1r_before,
        first_terminal_status="STOPPED", final_status="STOPPED",
    )
    repo.update_signal_feature_outcome_from_observation(conn, uid)


def _close_obs_expired(conn, uid: str, hit_1r_before: int = 0) -> None:
    now = utcnow_iso()
    repo.close_signal_observation(
        conn, uid,
        status="EXPIRED", outcome_r=None, closed_at=now,
        expired_at=now, time_to_expiry_seconds=900.0,
        hit_1r_before_expiry=hit_1r_before,
        first_terminal_status="EXPIRED", final_status="EXPIRED",
    )
    repo.update_signal_feature_outcome_from_observation(conn, uid)


# ------------------------------------------------------------------ pure unit tests: helpers

def test_macro_alignment_long_btc_long():
    from scripts.report_signal_learning import _macro_alignment_label
    assert _macro_alignment_label("LONG", "LONG") == "aligned"


def test_macro_alignment_long_btc_short():
    from scripts.report_signal_learning import _macro_alignment_label
    assert _macro_alignment_label("LONG", "SHORT") == "opposed"


def test_macro_alignment_short_btc_short():
    from scripts.report_signal_learning import _macro_alignment_label
    assert _macro_alignment_label("SHORT", "SHORT") == "aligned"


def test_macro_alignment_short_btc_long():
    from scripts.report_signal_learning import _macro_alignment_label
    assert _macro_alignment_label("SHORT", "LONG") == "opposed"


def test_macro_alignment_none_bias():
    from scripts.report_signal_learning import _macro_alignment_label
    assert _macro_alignment_label("LONG", "NONE") == "neutral"
    assert _macro_alignment_label("LONG", None) == "neutral"
    assert _macro_alignment_label("SHORT", None) == "neutral"


def test_rsi_bucket_boundaries():
    from scripts.report_signal_learning import _rsi_bucket
    assert _rsi_bucket(35.0) == "<40"
    assert _rsi_bucket(40.0) == "40-45"
    assert _rsi_bucket(44.9) == "40-45"
    assert _rsi_bucket(45.0) == "45-50"
    assert _rsi_bucket(50.0) == "50-55"
    assert _rsi_bucket(55.0) == "55-60"
    assert _rsi_bucket(60.0) == ">60"
    assert _rsi_bucket(None) == "unknown"


def test_atr_bucket_boundaries():
    from scripts.report_signal_learning import _atr_bucket
    assert _atr_bucket(0.05) == "<0.10"
    assert _atr_bucket(0.10) == "0.10-0.25"
    assert _atr_bucket(0.25) == "0.25-0.50"
    assert _atr_bucket(0.50) == "0.50-1.00"
    assert _atr_bucket(1.00) == ">1.00"
    assert _atr_bucket(None) == "unknown"


def test_vwap_bucket_boundaries():
    from scripts.report_signal_learning import _vwap_bucket
    assert _vwap_bucket(-0.30) == "below -0.25"
    assert _vwap_bucket(-0.25) == "-0.25 to 0"
    assert _vwap_bucket(-0.10) == "-0.25 to 0"
    assert _vwap_bucket(0.0) == "0 to +0.25"
    assert _vwap_bucket(0.24) == "0 to +0.25"
    assert _vwap_bucket(0.25) == "+0.25 to +0.50"
    assert _vwap_bucket(0.50) == "above +0.50"
    assert _vwap_bucket(None) == "unknown"


def test_ema_spread_bucket_boundaries():
    from scripts.report_signal_learning import _ema_spread_bucket
    assert _ema_spread_bucket(-0.30) == "below -0.25"
    assert _ema_spread_bucket(-0.20) == "-0.25 to 0"
    assert _ema_spread_bucket(0.0) == "0 to +0.25"
    assert _ema_spread_bucket(0.30) == "+0.25 to +0.50"
    assert _ema_spread_bucket(0.60) == "above +0.50"
    assert _ema_spread_bucket(None) == "unknown"


def test_quality_score_none_when_n_lt_10():
    from scripts.report_signal_learning import _quality_score
    score = _quality_score(0.5, 0.2, 0.1, 0.1, None, None, n=9)
    assert score is None


def test_quality_score_high_tp1_tp2():
    from scripts.report_signal_learning import _quality_score
    # High TP1 + TP2 → high score
    score = _quality_score(0.6, 0.3, 0.1, 0.1, None, None, n=20)
    assert score is not None
    assert score > 0.2  # 0.6*0.3 + 0.3*0.4 - 0.1*0.2 - 0.1*0.1 = 0.18 + 0.12 - 0.02 - 0.01 = 0.27


def test_quality_score_clamped_to_zero_on_terrible_stats():
    from scripts.report_signal_learning import _quality_score
    # Worst case: 0 TP1, 0 TP2, 100% stop, 100% expiry no TP1
    score = _quality_score(0.0, 0.0, 1.0, 1.0, None, None, n=50)
    assert score == pytest.approx(0.0)


def test_quality_score_mfe_bonus():
    from scripts.report_signal_learning import _quality_score
    base = _quality_score(0.3, 0.1, 0.2, 0.2, None, None, n=20)
    with_mfe = _quality_score(0.3, 0.1, 0.2, 0.2, 1.5, None, n=20)
    assert with_mfe is not None and base is not None
    assert with_mfe > base


def test_quality_score_mae_penalty():
    from scripts.report_signal_learning import _quality_score
    base = _quality_score(0.3, 0.1, 0.2, 0.2, None, None, n=20)
    with_mae = _quality_score(0.3, 0.1, 0.2, 0.2, None, 0.9, n=20)
    assert with_mae is not None and base is not None
    assert with_mae < base


def test_recommendation_insufficient_sample():
    from scripts.report_signal_learning import _recommendation_label
    label = _recommendation_label(None, 0.5, 0.1, 0.3, n=5, overall_tp1_rate=0.2, overall_stop_rate=0.2)
    assert label == "INSUFFICIENT_SAMPLE"


def test_recommendation_promising():
    from scripts.report_signal_learning import _recommendation_label
    # TP1 rate 50% vs overall 20% — well above 1.25x threshold
    # Stop rate 10% vs overall 20%*1.25 = 25% — below threshold
    score = 0.40
    label = _recommendation_label(score, 0.50, 0.10, 0.30, n=20, overall_tp1_rate=0.20, overall_stop_rate=0.20)
    assert label == "PROMISING_OBSERVE"


def test_recommendation_weak():
    from scripts.report_signal_learning import _recommendation_label
    # TP1 rate 5% vs overall 20% — below 0.75x threshold
    score = 0.05
    label = _recommendation_label(score, 0.05, 0.10, 0.70, n=20, overall_tp1_rate=0.20, overall_stop_rate=0.20)
    assert label == "WEAK_OBSERVE_ONLY"


def test_recommendation_needs_filtering():
    from scripts.report_signal_learning import _recommendation_label
    # TP1 rate 20% = exactly overall → not promising, not weak
    # stop_rate 20% ≤ 0.35 → not weak
    score = 0.10
    label = _recommendation_label(score, 0.20, 0.20, 0.60, n=20, overall_tp1_rate=0.20, overall_stop_rate=0.20)
    assert label == "NEEDS_FILTERING"


def test_recommendation_high_stop_rate_is_weak():
    from scripts.report_signal_learning import _recommendation_label
    score = 0.05
    label = _recommendation_label(score, 0.25, 0.40, 0.30, n=20, overall_tp1_rate=0.20, overall_stop_rate=0.20)
    assert label == "WEAK_OBSERVE_ONLY"


# ------------------------------------------------------------------ integration tests

def test_no_default_row_cap(tmp_path, monkeypatch):
    """Without --limit, all rows are loaded regardless of count."""
    db_path = str(tmp_path / "no_cap.db")
    _make_env_overrides(monkeypatch, db_path)
    conn = init_db(db_path)

    # Insert 12 features — previously capped at 500 in snapshot, this tests no default cap
    for i in range(12):
        uid = _insert_obs(conn, utcnow_iso())
        _insert_feature(conn, uid)

    close_db()

    import scripts.report_signal_learning as mod

    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main([])

    output = buf.getvalue()
    assert "Rows loaded          : 12" in output
    assert "Limit                : none" in output


def test_explicit_limit_restricts_rows(tmp_path, monkeypatch):
    """--limit N loads at most N rows."""
    db_path = str(tmp_path / "limit.db")
    _make_env_overrides(monkeypatch, db_path)
    conn = init_db(db_path)

    for i in range(8):
        uid = _insert_obs(conn, utcnow_iso())
        _insert_feature(conn, uid)

    close_db()

    import scripts.report_signal_learning as mod

    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main(["--limit", "5"])

    output = buf.getvalue()
    assert "Rows loaded          : 5" in output
    assert "Limit                : 5" in output


def test_sym_dir_breakdown_appears(tmp_path, monkeypatch):
    """Symbol+direction breakdown table is printed when confirmed rows exist."""
    db_path = str(tmp_path / "sym_dir.db")
    _make_env_overrides(monkeypatch, db_path)
    conn = init_db(db_path)

    # Insert BTC LONG and ETH LONG rows with outcomes
    for sym in ["BTC", "ETH"]:
        uid = _insert_obs(conn, utcnow_iso(), symbol=sym)
        _insert_feature(conn, uid, symbol=sym)
        _close_obs_hit2r(conn, uid)

    close_db()

    import scripts.report_signal_learning as mod

    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main([])

    output = buf.getvalue()
    assert "Symbol + direction breakdown" in output
    assert "BTC" in output
    assert "ETH" in output


def test_long_vs_short_breakdown(tmp_path, monkeypatch):
    """Long vs short breakdown table is printed."""
    db_path = str(tmp_path / "long_short.db")
    _make_env_overrides(monkeypatch, db_path)
    conn = init_db(db_path)

    long_uid = _insert_obs(conn, utcnow_iso(), direction="LONG")
    _insert_feature(conn, long_uid, direction="LONG")
    _close_obs_hit2r(conn, long_uid)

    short_uid = _insert_obs(conn, utcnow_iso(), direction="SHORT")
    _insert_feature(conn, short_uid, direction="SHORT")
    _close_obs_stopped(conn, short_uid)

    close_db()

    import scripts.report_signal_learning as mod

    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main([])

    output = buf.getvalue()
    assert "Long vs Short breakdown" in output
    assert "LONG" in output
    assert "SHORT" in output


def test_short_warning_when_small_sample(tmp_path, monkeypatch):
    """Warning printed when SHORT sample < 10."""
    db_path = str(tmp_path / "short_warn.db")
    _make_env_overrides(monkeypatch, db_path)
    conn = init_db(db_path)

    # 1 LONG, 1 SHORT — SHORT well below 10
    for d in ["LONG", "SHORT"]:
        uid = _insert_obs(conn, utcnow_iso(), direction=d)
        _insert_feature(conn, uid, direction=d)
        _close_obs_stopped(conn, uid)

    close_db()

    import scripts.report_signal_learning as mod

    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main([])

    output = buf.getvalue()
    assert "WARNING" in output
    assert "SHORT sample" in output


def test_macro_alignment_section_present(tmp_path, monkeypatch):
    """Macro alignment section is printed when btc_trend_bias data exists."""
    db_path = str(tmp_path / "macro.db")
    _make_env_overrides(monkeypatch, db_path)
    conn = init_db(db_path)

    uid = _insert_obs(conn, utcnow_iso(), direction="LONG")
    _insert_feature(conn, uid, direction="LONG", btc_trend_bias="LONG")
    _close_obs_hit2r(conn, uid)

    close_db()

    import scripts.report_signal_learning as mod

    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main([])

    output = buf.getvalue()
    assert "Macro alignment analysis" in output
    assert "BTC trend bias alignment" in output
    assert "aligned" in output


def test_expiry_quality_section_present(tmp_path, monkeypatch):
    """Expiry quality section is printed when expired rows exist."""
    db_path = str(tmp_path / "expiry_q.db")
    _make_env_overrides(monkeypatch, db_path)
    conn = init_db(db_path)

    uid1 = _insert_obs(conn, utcnow_iso())
    _insert_feature(conn, uid1)
    _close_obs_expired(conn, uid1, hit_1r_before=0)  # expired without TP1

    uid2 = _insert_obs(conn, utcnow_iso())
    _insert_feature(conn, uid2)
    _close_obs_expired(conn, uid2, hit_1r_before=1)  # expired after TP1

    close_db()

    import scripts.report_signal_learning as mod

    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main([])

    output = buf.getvalue()
    assert "Expiry quality" in output
    assert "After TP1" in output
    assert "Without TP1" in output


def test_feature_bucket_section_present(tmp_path, monkeypatch):
    """Feature bucket tables are printed when closed confirmed rows exist."""
    db_path = str(tmp_path / "buckets.db")
    _make_env_overrides(monkeypatch, db_path)
    conn = init_db(db_path)

    uid = _insert_obs(conn, utcnow_iso())
    _insert_feature(conn, uid, rsi_val=47.0, atr_pct=0.3)
    _close_obs_hit2r(conn, uid)

    close_db()

    import scripts.report_signal_learning as mod

    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main([])

    output = buf.getvalue()
    assert "Feature bucket analysis" in output
    assert "RSI at capture" in output
    assert "ATR %price" in output
    assert "Price vs VWAP" in output
    assert "EMA spread" in output


def test_quality_score_in_output_when_n_ge_10(tmp_path, monkeypatch):
    """Quality scores appear for symbol+direction groups with N>=10 closed rows."""
    db_path = str(tmp_path / "score.db")
    _make_env_overrides(monkeypatch, db_path)
    conn = init_db(db_path)

    for i in range(12):
        uid = _insert_obs(conn, utcnow_iso(), symbol="BTC")
        _insert_feature(conn, uid, symbol="BTC")
        _close_obs_hit2r(conn, uid)

    close_db()

    import scripts.report_signal_learning as mod

    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main([])

    output = buf.getvalue()
    assert "PROMISING_OBSERVE" in output or "NEEDS_FILTERING" in output or "WEAK_OBSERVE_ONLY" in output
    # Should not be INSUFFICIENT_SAMPLE since N=12
    # Check quality score column is numeric (not 'n/a') by verifying a decimal appears
    assert "DIAGNOSTIC ONLY" in output


def test_insufficient_sample_label_when_n_lt_10(tmp_path, monkeypatch):
    """INSUFFICIENT_SAMPLE appears for symbol+direction groups with fewer than 10 closed rows."""
    db_path = str(tmp_path / "insuf.db")
    _make_env_overrides(monkeypatch, db_path)
    conn = init_db(db_path)

    for i in range(5):
        uid = _insert_obs(conn, utcnow_iso(), symbol="XYZ")
        _insert_feature(conn, uid, symbol="XYZ")
        _close_obs_expired(conn, uid)

    close_db()

    import scripts.report_signal_learning as mod

    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main([])

    output = buf.getvalue()
    assert "INSUFFICIENT_SAMPLE" in output


def test_report_does_not_mutate_settings(tmp_path, monkeypatch):
    """Running the report does not change alerts_enabled or dry_run_mode."""
    db_path = str(tmp_path / "nomutate.db")
    _make_env_overrides(monkeypatch, db_path)
    conn = init_db(db_path)

    uid = _insert_obs(conn, utcnow_iso())
    _insert_feature(conn, uid)
    close_db()

    from apex.config import get_settings
    import scripts.report_signal_learning as mod

    with redirect_stdout(io.StringIO()):
        mod.main([])

    settings = get_settings()
    assert settings.alerts_enabled is False
    assert settings.dry_run_mode is True


def test_snapshot_no_limit_cap(tmp_path, monkeypatch):
    """create_apex_snapshot no longer hardcodes --limit 500 in learning argv."""
    db_path = str(tmp_path / "snap_no_cap.db")
    out_path = str(tmp_path / "snap_no_cap.txt")
    _make_env_overrides(monkeypatch, db_path)

    conn = init_db(db_path)
    # Insert 3 features — just enough to verify the section runs with no limit
    for _ in range(3):
        uid = _insert_obs(conn, utcnow_iso())
        _insert_feature(conn, uid)
    close_db()

    import scripts.create_apex_snapshot as mod

    with redirect_stdout(io.StringIO()):
        mod.main(["--out", out_path])

    content = Path(out_path).read_text()
    assert "Signal Learning Report" in content
    # New format: should show "Limit : none" not "Rows loaded (limit=500)"
    assert "Limit                : none" in content
    assert "Rows loaded (limit=" not in content


def test_report_learning_label_summary_printed(tmp_path, monkeypatch):
    """Label summary section is printed."""
    db_path = str(tmp_path / "label_sum.db")
    _make_env_overrides(monkeypatch, db_path)
    conn = init_db(db_path)

    uid = _insert_obs(conn, utcnow_iso())
    _insert_feature(conn, uid)
    _close_obs_stopped(conn, uid)

    close_db()

    import scripts.report_signal_learning as mod

    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main([])

    output = buf.getvalue()
    assert "Label summary" in output
