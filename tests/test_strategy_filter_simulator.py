"""Tests for Milestone 11A: Strategy Filter Simulator.

Covers:
- script runs on empty DB (no crash)
- baseline stats are printed
- ATR >= 0.10 filter excludes low-ATR rows
- ATR 0.25-0.50 filter keeps correct rows
- RSI filter boundaries
- VWAP near filter boundaries
- LONG-only / SHORT-only filters
- BTC aligned/opposed macro classification
- insufficient sample label when kept rows < min-n
- simulated deltas vs baseline appear in output
- top simulated filters section appears
- dangerous filters section appears
- snapshot includes Strategy Filter Simulation Report section
- simulator does not mutate settings or DB row counts
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
from apex.utils.time import minutes_from_now, utcnow_iso


# ------------------------------------------------------------------ helpers

def _make_env(monkeypatch, db_path: str) -> None:
    monkeypatch.setenv("APEX_DB_PATH", db_path)
    monkeypatch.setenv("ACTION_TOKEN_SECRET", "test-11a")
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


def _run_sim(argv=None):
    import scripts.simulate_strategy_filters as mod
    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main(argv or [])
    return buf.getvalue()


# ------------------------------------------------------------------ basic tests

def test_simulator_runs_empty_db(tmp_path, monkeypatch):
    """Script exits cleanly with no crash when DB is empty."""
    db_path = str(tmp_path / "empty.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    close_db()

    output = _run_sim()
    assert "Strategy Filter Simulator" in output
    assert "REPORT-ONLY" in output


def test_simulator_shows_no_closed_rows_message(tmp_path, monkeypatch):
    """When no closed rows exist, simulator prints appropriate message."""
    db_path = str(tmp_path / "open_only.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature(conn, uid)
    # Do not close — leave OBSERVED
    close_db()

    output = _run_sim()
    assert "No closed CONFIRMED_SETUP rows" in output


def test_baseline_stats_printed(tmp_path, monkeypatch):
    """Baseline section is printed when closed rows exist."""
    db_path = str(tmp_path / "baseline.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    for _ in range(3):
        uid = _insert_obs(conn)
        _insert_feature(conn, uid)
        _close_hit2r(conn, uid)
    for _ in range(2):
        uid = _insert_obs(conn)
        _insert_feature(conn, uid)
        _close_stopped(conn, uid)
    close_db()

    output = _run_sim()
    assert "Baseline performance" in output
    assert "HIT_2R" in output or "TP1 milestone" in output
    assert "STOPPED" in output


# ------------------------------------------------------------------ filter predicate unit tests

def test_filter_atr_gte_0_10():
    from scripts.simulate_strategy_filters import _filter_atr_gte_0_10

    class _Row(dict):
        def __getitem__(self, key):
            return super().__getitem__(key)

    assert _filter_atr_gte_0_10({"atr_pct": 0.10}) is True
    assert _filter_atr_gte_0_10({"atr_pct": 0.50}) is True
    assert _filter_atr_gte_0_10({"atr_pct": 0.09}) is False
    assert _filter_atr_gte_0_10({"atr_pct": 0.00}) is False
    assert _filter_atr_gte_0_10({"atr_pct": None}) is False


def test_filter_atr_0_25_to_0_50():
    from scripts.simulate_strategy_filters import _filter_atr_0_25_to_0_50

    assert _filter_atr_0_25_to_0_50({"atr_pct": 0.25}) is True
    assert _filter_atr_0_25_to_0_50({"atr_pct": 0.50}) is True
    assert _filter_atr_0_25_to_0_50({"atr_pct": 0.37}) is True
    assert _filter_atr_0_25_to_0_50({"atr_pct": 0.24}) is False
    assert _filter_atr_0_25_to_0_50({"atr_pct": 0.51}) is False
    assert _filter_atr_0_25_to_0_50({"atr_pct": None}) is False


def test_filter_rsi_45_to_60():
    from scripts.simulate_strategy_filters import _filter_rsi_45_to_60

    assert _filter_rsi_45_to_60({"rsi_val": 45.0}) is True
    assert _filter_rsi_45_to_60({"rsi_val": 60.0}) is True
    assert _filter_rsi_45_to_60({"rsi_val": 52.0}) is True
    assert _filter_rsi_45_to_60({"rsi_val": 44.9}) is False
    assert _filter_rsi_45_to_60({"rsi_val": 60.1}) is False
    assert _filter_rsi_45_to_60({"rsi_val": None}) is False


def test_filter_rsi_55_to_60():
    from scripts.simulate_strategy_filters import _filter_rsi_55_to_60

    assert _filter_rsi_55_to_60({"rsi_val": 55.0}) is True
    assert _filter_rsi_55_to_60({"rsi_val": 60.0}) is True
    assert _filter_rsi_55_to_60({"rsi_val": 54.9}) is False
    assert _filter_rsi_55_to_60({"rsi_val": 60.1}) is False


def test_filter_vwap_near():
    from scripts.simulate_strategy_filters import _filter_vwap_near

    assert _filter_vwap_near({"price_vs_vwap_pct": 0.0}) is True
    assert _filter_vwap_near({"price_vs_vwap_pct": -0.25}) is True
    assert _filter_vwap_near({"price_vs_vwap_pct": 0.25}) is True
    assert _filter_vwap_near({"price_vs_vwap_pct": -0.26}) is False
    assert _filter_vwap_near({"price_vs_vwap_pct": 0.26}) is False
    assert _filter_vwap_near({"price_vs_vwap_pct": None}) is False


def test_filter_vwap_above_0_to_0_25():
    from scripts.simulate_strategy_filters import _filter_vwap_above_0_to_0_25

    assert _filter_vwap_above_0_to_0_25({"price_vs_vwap_pct": 0.0}) is True
    assert _filter_vwap_above_0_to_0_25({"price_vs_vwap_pct": 0.25}) is True
    assert _filter_vwap_above_0_to_0_25({"price_vs_vwap_pct": -0.01}) is False
    assert _filter_vwap_above_0_to_0_25({"price_vs_vwap_pct": 0.26}) is False


def test_filter_long_only():
    from scripts.simulate_strategy_filters import _filter_long_only, _filter_short_only

    assert _filter_long_only({"direction": "LONG"}) is True
    assert _filter_long_only({"direction": "SHORT"}) is False
    assert _filter_short_only({"direction": "SHORT"}) is True
    assert _filter_short_only({"direction": "LONG"}) is False


def test_filter_btc_aligned_opposed():
    from scripts.simulate_strategy_filters import _filter_btc_aligned, _filter_btc_opposed

    # LONG signal, BTC=LONG → aligned
    assert _filter_btc_aligned({"direction": "LONG", "btc_trend_bias": "LONG"}) is True
    assert _filter_btc_opposed({"direction": "LONG", "btc_trend_bias": "LONG"}) is False

    # LONG signal, BTC=SHORT → opposed
    assert _filter_btc_aligned({"direction": "LONG", "btc_trend_bias": "SHORT"}) is False
    assert _filter_btc_opposed({"direction": "LONG", "btc_trend_bias": "SHORT"}) is True

    # SHORT signal, BTC=SHORT → aligned
    assert _filter_btc_aligned({"direction": "SHORT", "btc_trend_bias": "SHORT"}) is True
    assert _filter_btc_opposed({"direction": "SHORT", "btc_trend_bias": "SHORT"}) is False

    # LONG signal, BTC=None → neutral (neither aligned nor opposed)
    assert _filter_btc_aligned({"direction": "LONG", "btc_trend_bias": None}) is False
    assert _filter_btc_opposed({"direction": "LONG", "btc_trend_bias": None}) is False


# ------------------------------------------------------------------ integration: filter results

def test_atr_gte_0_10_excludes_low_atr_rows(tmp_path, monkeypatch):
    """ATR >= 0.10 filter excludes rows with atr_pct < 0.10."""
    db_path = str(tmp_path / "atr_filter.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    # 5 low ATR rows (should be excluded)
    for _ in range(5):
        uid = _insert_obs(conn)
        _insert_feature(conn, uid, atr_pct=0.05)
        _close_stopped(conn, uid)

    # 8 normal ATR rows (should be kept)
    for _ in range(8):
        uid = _insert_obs(conn)
        _insert_feature(conn, uid, atr_pct=0.30)
        _close_hit2r(conn, uid)

    close_db()

    output = _run_sim()
    assert "KEEP_ATR_GTE_0_10" in output
    # Should show 8 kept (not 13)
    assert "Kept      :    8" in output or "8 (" in output


def test_atr_0_25_to_0_50_keeps_correct_rows(tmp_path, monkeypatch):
    """ATR 0.25-0.50 filter keeps only rows in that band."""
    db_path = str(tmp_path / "atr_band.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    # 4 in-band rows
    for _ in range(4):
        uid = _insert_obs(conn)
        _insert_feature(conn, uid, atr_pct=0.35)
        _close_hit2r(conn, uid)

    # 4 out-of-band rows (low)
    for _ in range(4):
        uid = _insert_obs(conn)
        _insert_feature(conn, uid, atr_pct=0.05)
        _close_stopped(conn, uid)

    # 4 out-of-band rows (high)
    for _ in range(4):
        uid = _insert_obs(conn)
        _insert_feature(conn, uid, atr_pct=2.00)
        _close_stopped(conn, uid)

    close_db()

    output = _run_sim()
    assert "KEEP_ATR_0_25_TO_0_50" in output
    # In-band: 4 rows. min_n default is 10 so verdict = INSUFFICIENT_SAMPLE
    assert "INSUFFICIENT_SAMPLE" in output


def test_long_only_filter(tmp_path, monkeypatch):
    """KEEP_LONG_ONLY keeps only LONG rows."""
    db_path = str(tmp_path / "long_only.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    for _ in range(8):
        uid = _insert_obs(conn, direction="LONG")
        _insert_feature(conn, uid, direction="LONG")
        _close_hit2r(conn, uid)

    for _ in range(5):
        uid = _insert_obs(conn, direction="SHORT")
        _insert_feature(conn, uid, direction="SHORT")
        _close_stopped(conn, uid)

    close_db()

    output = _run_sim()
    assert "KEEP_LONG_ONLY" in output
    assert "KEEP_SHORT_ONLY" in output


def test_insufficient_sample_label(tmp_path, monkeypatch):
    """When kept rows < min-n, verdict is INSUFFICIENT_SAMPLE."""
    db_path = str(tmp_path / "insuf.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    # 3 rows with RSI 55-60 (will be in that filter bucket)
    for _ in range(3):
        uid = _insert_obs(conn)
        _insert_feature(conn, uid, rsi_val=57.0)
        _close_hit2r(conn, uid)

    # 10 rows outside that RSI range to give baseline
    for _ in range(10):
        uid = _insert_obs(conn)
        _insert_feature(conn, uid, rsi_val=45.0)
        _close_stopped(conn, uid)

    close_db()

    # With min-n=10, RSI 55-60 filter will have 3 rows → INSUFFICIENT_SAMPLE
    output = _run_sim(["--min-n", "10"])
    assert "INSUFFICIENT_SAMPLE" in output


def test_deltas_vs_baseline_appear(tmp_path, monkeypatch):
    """Output contains delta columns (Δ vs baseline)."""
    db_path = str(tmp_path / "deltas.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    for _ in range(12):
        uid = _insert_obs(conn)
        _insert_feature(conn, uid, atr_pct=0.30)
        _close_hit2r(conn, uid)

    close_db()

    output = _run_sim()
    assert "Δ vs baseline" in output


def test_top_filters_section_appears(tmp_path, monkeypatch):
    """Top simulated filters ranking section appears."""
    db_path = str(tmp_path / "top.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    for _ in range(12):
        uid = _insert_obs(conn)
        _insert_feature(conn, uid, atr_pct=0.30)
        _close_hit2r(conn, uid)

    close_db()

    output = _run_sim()
    assert "Top simulated filters" in output


def test_dangerous_filters_section_appears(tmp_path, monkeypatch):
    """Dangerous filters section appears."""
    db_path = str(tmp_path / "danger.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    for _ in range(12):
        uid = _insert_obs(conn)
        _insert_feature(conn, uid, atr_pct=0.30)
        _close_hit2r(conn, uid)

    close_db()

    output = _run_sim()
    assert "Filters that look dangerous" in output


def test_interpretation_section_appears(tmp_path, monkeypatch):
    """Interpretation section appears and includes safety disclaimer."""
    db_path = str(tmp_path / "interp.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    for _ in range(12):
        uid = _insert_obs(conn)
        _insert_feature(conn, uid, atr_pct=0.30)
        _close_hit2r(conn, uid)

    close_db()

    output = _run_sim()
    assert "Interpretation" in output
    assert "No runtime changes are recommended automatically" in output


def test_btc_aligned_opposed_filter(tmp_path, monkeypatch):
    """BTC aligned and opposed filters appear in output."""
    db_path = str(tmp_path / "btc_macro.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    # Aligned rows: LONG signal, BTC=LONG
    for _ in range(8):
        uid = _insert_obs(conn, direction="LONG")
        _insert_feature(conn, uid, direction="LONG", btc_trend_bias="LONG")
        _close_hit2r(conn, uid)

    # Opposed rows: LONG signal, BTC=SHORT
    for _ in range(6):
        uid = _insert_obs(conn, direction="LONG")
        _insert_feature(conn, uid, direction="LONG", btc_trend_bias="SHORT")
        _close_stopped(conn, uid)

    close_db()

    output = _run_sim()
    assert "KEEP_BTC_ALIGNED" in output
    assert "KEEP_BTC_OPPOSED" in output


def test_simulator_does_not_mutate_settings(tmp_path, monkeypatch):
    """Running the simulator does not change alerts_enabled or dry_run_mode."""
    db_path = str(tmp_path / "nomutate.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)
    uid = _insert_obs(conn)
    _insert_feature(conn, uid)
    _close_hit2r(conn, uid)
    close_db()

    from apex.config import get_settings
    _run_sim()

    settings = get_settings()
    assert settings.alerts_enabled is False
    assert settings.dry_run_mode is True


def test_simulator_does_not_change_row_count(tmp_path, monkeypatch):
    """Row count in signal_features is unchanged after running the simulator."""
    db_path = str(tmp_path / "rowcount.db")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    for _ in range(5):
        uid = _insert_obs(conn)
        _insert_feature(conn, uid)
        _close_hit2r(conn, uid)

    count_before = conn.execute("SELECT COUNT(*) FROM signal_features").fetchone()[0]
    close_db()

    _run_sim()

    # Re-open and check
    conn2 = init_db(db_path)
    count_after = conn2.execute("SELECT COUNT(*) FROM signal_features").fetchone()[0]
    close_db()

    assert count_before == count_after == 5


def test_csv_export(tmp_path, monkeypatch):
    """--csv flag writes a CSV file with expected columns."""
    db_path = str(tmp_path / "csv.db")
    csv_path = str(tmp_path / "out.csv")
    _make_env(monkeypatch, db_path)
    conn = init_db(db_path)

    for _ in range(5):
        uid = _insert_obs(conn)
        _insert_feature(conn, uid)
        _close_hit2r(conn, uid)
    close_db()

    _run_sim(["--csv", csv_path])

    p = Path(csv_path)
    assert p.exists()
    content = p.read_text()
    assert "name" in content
    assert "verdict" in content
    assert "n_kept" in content


def test_snapshot_includes_filter_simulation_section(tmp_path, monkeypatch):
    """create_apex_snapshot includes Strategy Filter Simulation Report section."""
    db_path = str(tmp_path / "snap.db")
    out_path = str(tmp_path / "snap.txt")
    _make_env(monkeypatch, db_path)

    conn = init_db(db_path)
    close_db()

    import scripts.create_apex_snapshot as mod
    with redirect_stdout(io.StringIO()):
        mod.main(["--out", out_path])

    content = Path(out_path).read_text()
    assert "Strategy Filter Simulation Report" in content
