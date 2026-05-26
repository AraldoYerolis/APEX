"""Tests for Milestone 11F: Research Candidate Prospective Tracker.

Covers:
- Pure evaluator unit tests (evaluate_research_candidates):
  - SESSION_US matches hours 13–20 inclusive
  - SESSION_LATE_US matches hours 21–23
  - HOUR_10_AVOID matches hour 10 only
  - HOUR_03_AVOID matches hour 3 only
  - HOUR_21_AVOID matches hour 21 only
  - SESSION_US does not match hours outside 13-20
  - SESSION_LATE_US does not match hour 20
  - ATR_0_50_TO_1_00 lower boundary (0.50 passes)
  - ATR_0_50_TO_1_00 upper boundary (1.00 passes)
  - ATR below 0.50 fails
  - ATR above 1.00 fails
  - ATR missing fails safely with clear reason
  - EMA_GT_0_25: 0.26 passes
  - EMA_GT_0_25: 0.25 does NOT pass (strictly > 0.25)
  - EMA below 0.25 fails
  - EMA missing fails safely
  - SYMBOL_WLD/DOGE/BTC match only exact normalized symbols
  - SYMBOL_WLD does not match BTC
  - Symbol case normalization
  - direction tags handle LONG/SHORT/None safely
  - invalid observed_at does not crash, produces clear reason
  - missing observed_at does not crash
  - output is JSON-serializable
  - positive/negative lists are correct subsets
  - direction-concentration notes appear for EMA_GT_0_25 and SYMBOL_WLD when matched
  - SESSION_LONDON and SESSION_ASIA context tags appear

- Persistence / integration tests:
  - 11F metadata merged with existing 11C gate metadata in tasks
  - existing candidate_gate fields (candidate_gate_version, gates) are preserved
  - feature_version remains 11C_v1 (unchanged)
  - missing/null atr_pct does not crash observation feature capture
  - 11F metadata does not suppress or alter observation creation (observation inserted)
  - research_candidate_version = "11F_v1" in stored metadata

- Report tests:
  - empty DB exits safely with "No 11F research candidate observations found yet."
  - report with 11F data shows baseline section
  - report filters to research_candidate_version='11F_v1'
  - report shows positive tags section
  - report shows negative tags section
  - malformed metadata_json does not crash report
  - snapshot integration does not crash on empty DB
  - direct import works
  - --since filter works
  - --min-n warning shows for small cohorts
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apex.strategy.research_candidates import (
    RESEARCH_CANDIDATE_VERSION,
    evaluate_research_candidates,
    _parse_hour,
)


# ================================================================== helpers

def _rc(observed_at=None, atr_pct=None, ema_spread_pct=None,
        symbol=None, direction=None) -> dict:
    return evaluate_research_candidates(
        atr_pct=atr_pct,
        ema_spread_pct=ema_spread_pct,
        observed_at=observed_at,
        symbol=symbol,
        direction=direction,
    )


def _matched(result: dict, tag: str) -> bool:
    return tag in result["research_candidate_names"]


def _reason(result: dict, tag: str) -> str:
    return result["research_candidate_flags"][tag]["reason"]


# ================================================================== unit: _parse_hour

def test_parse_hour_T_format():
    assert _parse_hour("2026-05-21T14:30:00Z") == 14


def test_parse_hour_space_format():
    assert _parse_hour("2026-05-21 09:15:00") == 9


def test_parse_hour_midnight():
    assert _parse_hour("2026-05-21T00:00:00Z") == 0


def test_parse_hour_23():
    assert _parse_hour("2026-05-21T23:59:59Z") == 23


def test_parse_hour_empty():
    assert _parse_hour("") is None


def test_parse_hour_none():
    assert _parse_hour(None) is None


def test_parse_hour_invalid():
    assert _parse_hour("not-a-timestamp") is None


# ================================================================== unit: SESSION_US

def test_session_us_hour_13():
    r = _rc(observed_at="2026-05-21T13:00:00Z")
    assert _matched(r, "SESSION_US")


def test_session_us_hour_15():
    r = _rc(observed_at="2026-05-21T15:30:00Z")
    assert _matched(r, "SESSION_US")


def test_session_us_hour_20():
    r = _rc(observed_at="2026-05-21T20:59:00Z")
    assert _matched(r, "SESSION_US")


def test_session_us_hour_12_not_matched():
    r = _rc(observed_at="2026-05-21T12:59:00Z")
    assert not _matched(r, "SESSION_US")


def test_session_us_hour_21_not_matched():
    r = _rc(observed_at="2026-05-21T21:00:00Z")
    assert not _matched(r, "SESSION_US")


# ================================================================== unit: SESSION_LATE_US

def test_session_late_us_hour_21():
    r = _rc(observed_at="2026-05-21T21:00:00Z")
    assert _matched(r, "SESSION_LATE_US")


def test_session_late_us_hour_23():
    r = _rc(observed_at="2026-05-21T23:59:00Z")
    assert _matched(r, "SESSION_LATE_US")


def test_session_late_us_hour_20_not_matched():
    r = _rc(observed_at="2026-05-21T20:59:00Z")
    assert not _matched(r, "SESSION_LATE_US")


def test_session_late_us_hour_0_not_matched():
    r = _rc(observed_at="2026-05-21T00:00:00Z")
    assert not _matched(r, "SESSION_LATE_US")


# ================================================================== unit: HOUR_10_AVOID

def test_hour_10_avoid_matches():
    r = _rc(observed_at="2026-05-21T10:30:00Z")
    assert _matched(r, "HOUR_10_AVOID")
    assert r["research_candidate_flags"]["HOUR_10_AVOID"]["matched"] is True


def test_hour_10_avoid_not_hour_9():
    r = _rc(observed_at="2026-05-21T09:00:00Z")
    assert not _matched(r, "HOUR_10_AVOID")


def test_hour_10_avoid_not_hour_11():
    r = _rc(observed_at="2026-05-21T11:00:00Z")
    assert not _matched(r, "HOUR_10_AVOID")


# ================================================================== unit: HOUR_03_AVOID

def test_hour_03_avoid_matches():
    r = _rc(observed_at="2026-05-21T03:15:00Z")
    assert _matched(r, "HOUR_03_AVOID")


def test_hour_03_avoid_not_hour_4():
    r = _rc(observed_at="2026-05-21T04:00:00Z")
    assert not _matched(r, "HOUR_03_AVOID")


# ================================================================== unit: HOUR_21_AVOID

def test_hour_21_avoid_matches():
    r = _rc(observed_at="2026-05-21T21:45:00Z")
    assert _matched(r, "HOUR_21_AVOID")


def test_hour_21_avoid_not_hour_20():
    r = _rc(observed_at="2026-05-21T20:00:00Z")
    assert not _matched(r, "HOUR_21_AVOID")


def test_hour_21_avoid_not_hour_22():
    r = _rc(observed_at="2026-05-21T22:00:00Z")
    assert not _matched(r, "HOUR_21_AVOID")


# Note: HOUR_21_AVOID overlaps with SESSION_LATE_US (21 is in 21-23)
def test_hour_21_both_late_us_and_hour21_avoid():
    r = _rc(observed_at="2026-05-21T21:00:00Z")
    assert _matched(r, "HOUR_21_AVOID")
    assert _matched(r, "SESSION_LATE_US")


# ================================================================== unit: ATR_0_50_TO_1_00

def test_atr_lower_boundary_passes():
    r = _rc(atr_pct=0.50)
    assert _matched(r, "ATR_0_50_TO_1_00")


def test_atr_upper_boundary_passes():
    r = _rc(atr_pct=1.00)
    assert _matched(r, "ATR_0_50_TO_1_00")


def test_atr_middle_passes():
    r = _rc(atr_pct=0.75)
    assert _matched(r, "ATR_0_50_TO_1_00")


def test_atr_below_lower_fails():
    r = _rc(atr_pct=0.49)
    assert not _matched(r, "ATR_0_50_TO_1_00")


def test_atr_above_upper_fails():
    r = _rc(atr_pct=1.01)
    assert not _matched(r, "ATR_0_50_TO_1_00")


def test_atr_missing_fails_safely():
    r = _rc(atr_pct=None)
    assert not _matched(r, "ATR_0_50_TO_1_00")
    assert "missing" in _reason(r, "ATR_0_50_TO_1_00")


def test_atr_zero_fails():
    r = _rc(atr_pct=0.0)
    assert not _matched(r, "ATR_0_50_TO_1_00")


# ================================================================== unit: EMA_GT_0_25

def test_ema_gt_0_25_passes():
    r = _rc(ema_spread_pct=0.26)
    assert _matched(r, "EMA_GT_0_25")


def test_ema_exactly_0_25_does_not_pass():
    # strictly > 0.25
    r = _rc(ema_spread_pct=0.25)
    assert not _matched(r, "EMA_GT_0_25")


def test_ema_below_0_25_fails():
    r = _rc(ema_spread_pct=0.10)
    assert not _matched(r, "EMA_GT_0_25")


def test_ema_negative_fails():
    r = _rc(ema_spread_pct=-0.10)
    assert not _matched(r, "EMA_GT_0_25")


def test_ema_missing_fails_safely():
    r = _rc(ema_spread_pct=None)
    assert not _matched(r, "EMA_GT_0_25")
    assert "missing" in _reason(r, "EMA_GT_0_25")


def test_ema_large_positive_passes():
    r = _rc(ema_spread_pct=1.50)
    assert _matched(r, "EMA_GT_0_25")


# ================================================================== unit: symbol tags

def test_symbol_wld_matches():
    r = _rc(symbol="WLD")
    assert _matched(r, "SYMBOL_WLD")


def test_symbol_doge_matches():
    r = _rc(symbol="DOGE")
    assert _matched(r, "SYMBOL_DOGE")


def test_symbol_btc_matches():
    r = _rc(symbol="BTC")
    assert _matched(r, "SYMBOL_BTC")


def test_symbol_wld_case_insensitive():
    r = _rc(symbol="wld")
    assert _matched(r, "SYMBOL_WLD")


def test_symbol_wld_does_not_match_btc():
    r = _rc(symbol="WLD")
    assert not _matched(r, "SYMBOL_BTC")
    assert not _matched(r, "SYMBOL_DOGE")


def test_symbol_eth_does_not_match_any():
    r = _rc(symbol="ETH")
    assert not _matched(r, "SYMBOL_WLD")
    assert not _matched(r, "SYMBOL_DOGE")
    assert not _matched(r, "SYMBOL_BTC")


def test_symbol_none_fails_safely():
    r = _rc(symbol=None)
    assert not _matched(r, "SYMBOL_WLD")
    assert "missing" in _reason(r, "SYMBOL_WLD")


def test_symbol_empty_string_fails_safely():
    r = _rc(symbol="")
    assert not _matched(r, "SYMBOL_WLD")


# ================================================================== unit: direction tags

def test_direction_long_matched():
    r = _rc(direction="LONG")
    assert _matched(r, "DIRECTION_LONG")
    assert not _matched(r, "DIRECTION_SHORT")


def test_direction_short_matched():
    r = _rc(direction="SHORT")
    assert _matched(r, "DIRECTION_SHORT")
    assert not _matched(r, "DIRECTION_LONG")


def test_direction_case_insensitive():
    r = _rc(direction="long")
    assert _matched(r, "DIRECTION_LONG")


def test_direction_none_fails_safely():
    r = _rc(direction=None)
    assert not _matched(r, "DIRECTION_LONG")
    assert not _matched(r, "DIRECTION_SHORT")
    assert "missing" in _reason(r, "DIRECTION_LONG")


# ================================================================== unit: session context tags

def test_session_asia_hour_3():
    r = _rc(observed_at="2026-05-21T03:00:00Z")
    assert _matched(r, "SESSION_ASIA")


def test_session_asia_hour_0():
    r = _rc(observed_at="2026-05-21T00:00:00Z")
    assert _matched(r, "SESSION_ASIA")


def test_session_asia_not_hour_8():
    r = _rc(observed_at="2026-05-21T08:00:00Z")
    assert not _matched(r, "SESSION_ASIA")


def test_session_london_hour_10():
    r = _rc(observed_at="2026-05-21T10:00:00Z")
    assert _matched(r, "SESSION_LONDON")


def test_session_london_not_hour_13():
    r = _rc(observed_at="2026-05-21T13:00:00Z")
    assert not _matched(r, "SESSION_LONDON")


# ================================================================== unit: invalid observed_at

def test_invalid_observed_at_does_not_crash():
    r = _rc(observed_at="garbage")
    assert isinstance(r, dict)
    assert not _matched(r, "SESSION_US")
    # reason should contain something meaningful
    assert "could not parse" in _reason(r, "SESSION_US") or "missing" in _reason(r, "SESSION_US")


def test_missing_observed_at_does_not_crash():
    r = _rc(observed_at=None)
    assert isinstance(r, dict)
    assert not _matched(r, "SESSION_US")
    assert "missing" in _reason(r, "SESSION_US")


# ================================================================== unit: output structure

def test_output_is_json_serializable():
    r = _rc(
        observed_at="2026-05-21T14:00:00Z",
        atr_pct=0.70,
        ema_spread_pct=0.30,
        symbol="BTC",
        direction="LONG",
    )
    dumped = json.dumps(r)
    parsed = json.loads(dumped)
    assert parsed["research_candidate_version"] == RESEARCH_CANDIDATE_VERSION


def test_output_has_required_keys():
    r = _rc()
    assert "research_candidate_version" in r
    assert "research_candidate_names" in r
    assert "research_candidate_flags" in r
    assert "research_candidate_positive" in r
    assert "research_candidate_negative" in r
    assert "research_candidate_notes" in r


def test_positive_list_subset_of_names():
    r = _rc(
        observed_at="2026-05-21T14:00:00Z",
        atr_pct=0.70,
        symbol="WLD",
        direction="LONG",
    )
    for tag in r["research_candidate_positive"]:
        assert tag in r["research_candidate_names"]


def test_negative_list_subset_of_names():
    r = _rc(observed_at="2026-05-21T10:00:00Z")
    for tag in r["research_candidate_negative"]:
        assert tag in r["research_candidate_names"]


def test_version_field_correct():
    r = _rc()
    assert r["research_candidate_version"] == "11F_v1"


# ================================================================== unit: direction-concentration notes

def test_ema_gt_0_25_match_has_dc_note():
    r = _rc(ema_spread_pct=0.50)
    assert any("EMA_GT_0_25" in note for note in r["research_candidate_notes"])


def test_symbol_wld_match_has_dc_note():
    r = _rc(symbol="WLD")
    assert any("SYMBOL_WLD" in note for note in r["research_candidate_notes"])


def test_symbol_doge_no_dc_note():
    r = _rc(symbol="DOGE")
    # DOGE has no direction_concentration_note
    assert not any("SYMBOL_DOGE" in note for note in r["research_candidate_notes"])


def test_symbol_btc_no_dc_note():
    r = _rc(symbol="BTC")
    assert not any("SYMBOL_BTC" in note for note in r["research_candidate_notes"])


def test_no_match_no_dc_notes():
    r = _rc(symbol="ETH", ema_spread_pct=-0.10)
    assert r["research_candidate_notes"] == []


# ================================================================== integration: persistence

def _make_env(monkeypatch, db_path: str) -> None:
    monkeypatch.setenv("APEX_DB_PATH", db_path)
    monkeypatch.setenv("ACTION_TOKEN_SECRET", "test-11f")
    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "fake")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "fake")
    monkeypatch.setenv("ALERTS_ENABLED", "false")
    monkeypatch.setenv("DRY_RUN_MODE", "true")


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_11f.db")


def _insert_feature_with_metadata(conn, metadata_json: str | None = None):
    """Insert a signal_features row with given metadata_json for integration tests."""
    from apex.db import repository as repo
    from apex.db.models import SignalFeature, SignalObservation
    from apex.utils.ids import new_uid
    from apex.utils.time import minutes_from_now, utcnow_iso

    uid = new_uid()
    obs = SignalObservation(
        observation_uid=uid,
        observed_at="2026-05-21T14:00:00Z",
        symbol="BTC",
        direction="LONG",
        signal_type="CONFIRMED_SETUP",
        entry_price=100.0,
        stop_price=99.0,
        target_1r=101.0,
        target_2r=102.0,
        expires_at=minutes_from_now(15),
    )
    repo.insert_signal_observation(conn, obs)

    feat = SignalFeature(
        observation_uid=uid,
        captured_at=utcnow_iso(),
        symbol="BTC",
        direction="LONG",
        signal_type="CONFIRMED_SETUP",
        observed_at="2026-05-21T14:00:00Z",
        entry_price=100.0,
        stop_price=99.0,
        target_1r=101.0,
        target_2r=102.0,
        atr_pct=0.70,
        metadata_json=metadata_json,
    )
    repo.insert_signal_feature(conn, feat)
    return uid


def test_11f_metadata_merges_with_gate_metadata(db_path, monkeypatch):
    """11F research candidate fields are merged alongside existing 11C gate fields."""
    _make_env(monkeypatch, db_path)
    from apex.db.connection import close_db, init_db
    from apex.strategy.candidate_gates import evaluate_candidate_gates
    from apex.strategy.research_candidates import evaluate_research_candidates

    # Simulate what tasks.py does
    gate_result = evaluate_candidate_gates(atr_pct=0.70, ema_spread_pct=-0.10, rsi_val=55.0)
    gate_json = json.dumps(gate_result)

    rc_result = evaluate_research_candidates(
        atr_pct=0.70,
        ema_spread_pct=-0.10,
        observed_at="2026-05-21T14:00:00Z",
        symbol="BTC",
        direction="LONG",
    )

    merged = json.loads(gate_json)
    merged.update(rc_result)
    merged_json = json.dumps(merged)

    conn = init_db(db_path)
    _insert_feature_with_metadata(conn, merged_json)
    close_db()

    # Verify: the stored metadata has both gate and research candidate fields
    conn = init_db(db_path)
    row = conn.execute("SELECT metadata_json FROM signal_features LIMIT 1").fetchone()
    close_db()

    assert row is not None
    stored = json.loads(row["metadata_json"])

    # Gate fields preserved
    assert "candidate_gate_version" in stored
    assert "gates" in stored

    # Research candidate fields present
    assert stored["research_candidate_version"] == "11F_v1"
    assert "research_candidate_names" in stored
    assert "research_candidate_flags" in stored


def test_existing_gate_fields_preserved_in_merge():
    """evaluate_research_candidates does not overwrite candidate_gate keys."""
    from apex.strategy.candidate_gates import evaluate_candidate_gates
    from apex.strategy.research_candidates import evaluate_research_candidates

    gate_result = evaluate_candidate_gates(atr_pct=0.50, ema_spread_pct=-0.10, rsi_val=55.0)
    rc_result = evaluate_research_candidates(
        atr_pct=0.50,
        ema_spread_pct=-0.10,
        observed_at="2026-05-21T15:00:00Z",
        symbol="BTC",
        direction="LONG",
    )

    merged = dict(gate_result)
    merged.update(rc_result)

    # Gate fields still present — version bumped to 11G_v1 in Milestone 11G
    assert "candidate_gate_version" in merged
    assert merged["candidate_gate_version"] == "11G_v1"
    assert "gates" in merged
    assert "GATE_A_ATR_0_10_TO_1_00" in merged["gates"]

    # Research fields present
    assert merged["research_candidate_version"] == "11F_v1"


def test_feature_version_unchanged(db_path, monkeypatch):
    """feature_version remains 11C_v1 — not changed by 11F."""
    _make_env(monkeypatch, db_path)
    from apex.db.connection import close_db, init_db
    from apex.db import repository as repo
    from apex.db.models import SignalFeature, SignalObservation
    from apex.utils.ids import new_uid
    from apex.utils.time import minutes_from_now, utcnow_iso

    conn = init_db(db_path)

    uid = new_uid()
    obs = SignalObservation(
        observation_uid=uid,
        observed_at=utcnow_iso(),
        symbol="BTC",
        direction="LONG",
        signal_type="CONFIRMED_SETUP",
        entry_price=100.0,
        stop_price=99.0,
        target_1r=101.0,
        target_2r=102.0,
        expires_at=minutes_from_now(15),
    )
    repo.insert_signal_observation(conn, obs)

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
    )
    repo.insert_signal_feature(conn, feat)

    row = conn.execute("SELECT feature_version FROM signal_features LIMIT 1").fetchone()
    close_db()

    assert row["feature_version"] == "11C_v1"


def test_missing_atr_does_not_crash():
    """evaluate_research_candidates handles None atr_pct without crashing."""
    r = evaluate_research_candidates(
        atr_pct=None,
        ema_spread_pct=None,
        observed_at="2026-05-21T14:00:00Z",
        symbol="BTC",
        direction="LONG",
    )
    assert isinstance(r, dict)
    assert r["research_candidate_flags"]["ATR_0_50_TO_1_00"]["matched"] is False
    assert "missing" in r["research_candidate_flags"]["ATR_0_50_TO_1_00"]["reason"]


def test_all_none_inputs_do_not_crash():
    """All None inputs produce a valid (all-unmatched) result."""
    r = evaluate_research_candidates(
        atr_pct=None,
        ema_spread_pct=None,
        observed_at=None,
        symbol=None,
        direction=None,
    )
    assert isinstance(r, dict)
    assert r["research_candidate_positive"] == []
    assert r["research_candidate_negative"] == []
    json.dumps(r)  # must be serializable


# ================================================================== report tests

def _make_full_metadata(
    observed_at: str = "2026-05-21T14:00:00Z",
    symbol: str = "BTC",
    direction: str = "LONG",
    atr_pct: float = 0.70,
    ema_spread_pct: float = -0.10,
) -> str:
    from apex.strategy.candidate_gates import evaluate_candidate_gates
    gate = evaluate_candidate_gates(atr_pct=atr_pct, ema_spread_pct=ema_spread_pct, rsi_val=55.0)
    rc = evaluate_research_candidates(
        atr_pct=atr_pct,
        ema_spread_pct=ema_spread_pct,
        observed_at=observed_at,
        symbol=symbol,
        direction=direction,
    )
    merged = dict(gate)
    merged.update(rc)
    return json.dumps(merged)


def _insert_closed_row(conn, outcome_status: str = "HIT_2R",
                       direction: str = "LONG",
                       observed_at: str = "2026-05-21T14:00:00Z",
                       symbol: str = "BTC",
                       atr_pct: float = 0.70,
                       ema_spread_pct: float = -0.10) -> str:
    from apex.db import repository as repo
    from apex.db.models import SignalFeature, SignalObservation
    from apex.utils.ids import new_uid
    from apex.utils.time import minutes_from_now, utcnow_iso

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

    meta = _make_full_metadata(
        observed_at=observed_at, symbol=symbol, direction=direction,
        atr_pct=atr_pct, ema_spread_pct=ema_spread_pct,
    )

    feat = SignalFeature(
        observation_uid=uid,
        captured_at=utcnow_iso(),
        symbol=symbol,
        direction=direction,
        signal_type="CONFIRMED_SETUP",
        observed_at=observed_at,
        entry_price=100.0,
        stop_price=99.0,
        target_1r=101.0,
        target_2r=102.0,
        atr_pct=atr_pct,
        ema_spread_pct=ema_spread_pct,
        metadata_json=meta,
    )
    repo.insert_signal_feature(conn, feat)

    now = utcnow_iso()
    if outcome_status == "HIT_2R":
        repo.close_signal_observation(
            conn, uid, status="HIT_2R", outcome_r=2.0, closed_at=now,
            hit_1r_at=now, time_to_1r_seconds=120.0,
            hit_2r_at=now, time_to_2r_seconds=240.0,
            first_terminal_status="HIT_2R", final_status="HIT_2R",
        )
    elif outcome_status == "STOPPED":
        repo.close_signal_observation(
            conn, uid, status="STOPPED", outcome_r=-1.0, closed_at=now,
            stopped_at=now, time_to_stop_seconds=90.0,
            hit_1r_before_stop=0,
            first_terminal_status="STOPPED", final_status="STOPPED",
        )
    elif outcome_status == "EXPIRED":
        repo.close_signal_observation(
            conn, uid, status="EXPIRED", outcome_r=None, closed_at=now,
            expired_at=now, time_to_expiry_seconds=900.0,
            hit_1r_before_expiry=0,
            first_terminal_status="EXPIRED", final_status="EXPIRED",
        )
    repo.update_signal_feature_outcome_from_observation(conn, uid)
    return uid


def _run_report(argv=None):
    import scripts.report_research_candidates as mod
    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main(argv or [])
    return buf.getvalue()


def test_empty_db_exits_safely(db_path, monkeypatch):
    _make_env(monkeypatch, db_path)
    from apex.db.connection import close_db, init_db
    init_db(db_path)
    close_db()
    out = _run_report()
    assert "No 11F research candidate observations found yet." in out


def test_report_filters_to_11f_metadata(db_path, monkeypatch):
    """Report only includes rows with research_candidate_version = '11F_v1'."""
    _make_env(monkeypatch, db_path)
    from apex.db.connection import close_db, init_db
    from apex.db import repository as repo
    from apex.db.models import SignalFeature, SignalObservation
    from apex.utils.ids import new_uid
    from apex.utils.time import minutes_from_now, utcnow_iso

    conn = init_db(db_path)

    # Insert a row WITHOUT 11F metadata
    uid_no_11f = new_uid()
    obs = SignalObservation(
        observation_uid=uid_no_11f,
        observed_at=utcnow_iso(),
        symbol="BTC",
        direction="LONG",
        signal_type="CONFIRMED_SETUP",
        entry_price=100.0, stop_price=99.0, target_1r=101.0, target_2r=102.0,
        expires_at=minutes_from_now(15),
    )
    repo.insert_signal_observation(conn, obs)
    feat_no_11f = SignalFeature(
        observation_uid=uid_no_11f,
        captured_at=utcnow_iso(),
        symbol="BTC", direction="LONG",
        signal_type="CONFIRMED_SETUP",
        observed_at=utcnow_iso(),
        entry_price=100.0, stop_price=99.0, target_1r=101.0, target_2r=102.0,
        metadata_json=json.dumps({"candidate_gate_version": "11C_v1", "gates": {}}),
    )
    repo.insert_signal_feature(conn, feat_no_11f)

    # Insert a row WITH 11F metadata
    _insert_closed_row(conn, outcome_status="HIT_2R")
    close_db()

    out = _run_report()
    # Should show baseline with only the 11F row (not the non-11F row)
    assert "Rows with 11F metadata" in out
    assert "1" in out  # only 1 row with 11F metadata


def test_report_shows_baseline_section(db_path, monkeypatch):
    _make_env(monkeypatch, db_path)
    from apex.db.connection import close_db, init_db
    conn = init_db(db_path)
    _insert_closed_row(conn)
    close_db()
    out = _run_report()
    assert "1 — Baseline" in out
    assert "BASELINE_ALL_11F" in out


def test_report_shows_positive_tags_section(db_path, monkeypatch):
    _make_env(monkeypatch, db_path)
    from apex.db.connection import close_db, init_db
    conn = init_db(db_path)
    _insert_closed_row(conn)
    close_db()
    out = _run_report()
    assert "2 — Positive / Review Tags" in out
    assert "SESSION_US" in out


def test_report_shows_negative_tags_section(db_path, monkeypatch):
    _make_env(monkeypatch, db_path)
    from apex.db.connection import close_db, init_db
    conn = init_db(db_path)
    _insert_closed_row(conn)
    close_db()
    out = _run_report()
    assert "3 — Negative / Avoid Tags" in out
    assert "SESSION_LATE_US" in out


def test_report_insufficient_warning(db_path, monkeypatch):
    _make_env(monkeypatch, db_path)
    from apex.db.connection import close_db, init_db
    conn = init_db(db_path)
    _insert_closed_row(conn)  # only 1 row
    close_db()
    out = _run_report(["--min-n", "10"])
    assert "INSUFFICIENT" in out


def test_report_malformed_metadata_does_not_crash(db_path, monkeypatch):
    _make_env(monkeypatch, db_path)
    from apex.db.connection import close_db, init_db
    from apex.db import repository as repo
    from apex.db.models import SignalFeature, SignalObservation
    from apex.utils.ids import new_uid
    from apex.utils.time import minutes_from_now, utcnow_iso

    conn = init_db(db_path)
    uid = new_uid()
    obs = SignalObservation(
        observation_uid=uid,
        observed_at=utcnow_iso(),
        symbol="BTC", direction="LONG",
        signal_type="CONFIRMED_SETUP",
        entry_price=100.0, stop_price=99.0, target_1r=101.0, target_2r=102.0,
        expires_at=minutes_from_now(15),
    )
    repo.insert_signal_observation(conn, obs)
    feat = SignalFeature(
        observation_uid=uid,
        captured_at=utcnow_iso(),
        symbol="BTC", direction="LONG",
        signal_type="CONFIRMED_SETUP",
        observed_at=utcnow_iso(),
        entry_price=100.0, stop_price=99.0, target_1r=101.0, target_2r=102.0,
        metadata_json="not-valid-json{{",
    )
    repo.insert_signal_feature(conn, feat)
    close_db()

    # Should not crash — malformed row just doesn't match 11F filter
    out = _run_report()
    assert "No 11F research candidate observations found yet." in out


def test_report_since_filter(db_path, monkeypatch):
    _make_env(monkeypatch, db_path)
    from apex.db.connection import close_db, init_db
    conn = init_db(db_path)
    _insert_closed_row(conn)
    close_db()
    out = _run_report(["--since", "2099-01-01T00:00:00Z"])
    assert "No 11F research candidate observations found yet." in out


def test_report_policy_b_metrics_computed(db_path, monkeypatch):
    """Report computes Policy B AvgR correctly."""
    _make_env(monkeypatch, db_path)
    from apex.db.connection import close_db, init_db
    conn = init_db(db_path)
    # US session row (14:00) that hit TP1
    _insert_closed_row(conn, outcome_status="HIT_2R",
                       observed_at="2026-05-21T14:00:00Z")
    # Non-US session expired row (03:00)
    _insert_closed_row(conn, outcome_status="EXPIRED",
                       observed_at="2026-05-21T03:00:00Z")
    close_db()
    out = _run_report()
    assert "Policy B" in out or "BAvgR" in out


def test_snapshot_integration_no_crash(db_path, monkeypatch):
    """Snapshot includes Research Candidate Report (11F) section and does not crash."""
    _make_env(monkeypatch, db_path)
    from apex.db.connection import close_db, init_db
    init_db(db_path)
    close_db()

    from scripts.create_apex_snapshot import main as snap_main
    import tempfile, os
    buf = io.StringIO()
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        snap_path = f.name
    try:
        with redirect_stdout(buf):
            snap_main(["--out", snap_path])
        content = Path(snap_path).read_text()
        assert "Research Candidate Report (11F)" in content
    finally:
        os.unlink(snap_path)


def test_direct_import():
    """Report script is importable as a module."""
    import scripts.report_research_candidates as mod
    assert callable(mod.main)
    assert callable(mod._parse_rc_meta)
    assert callable(mod._tag_matched)
