"""Tests for scripts/report_opportunities.py's snapshot/version labeling.

Read-only, disposable test databases only. Does not exercise detectors or
the scheduler — seeds opportunity_observations rows directly via the
repository layer to control detector_version/setup_family precisely, then
asserts the report's printed output describes each row's snapshot
semantics accurately (see the script's "Snapshot/version semantics"
docstring section).
"""
from __future__ import annotations

import sys

from apex.db import repository as repo
from apex.db.connection import close_db, init_db
from apex.opportunity.contract import Opportunity

sys.path.insert(0, "scripts")
import report_opportunities  # noqa: E402


def _env(monkeypatch, db_path: str) -> None:
    monkeypatch.setenv("APEX_DB_PATH", db_path)
    monkeypatch.setenv("ACTION_TOKEN_SECRET", "test-report-secret")
    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "fake")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "fake")
    monkeypatch.setenv("ALERTS_ENABLED", "false")
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    monkeypatch.setenv("OPPORTUNITY_ENGINE_ENABLED", "false")


def _seed_opportunity(conn, **overrides) -> None:
    base = dict(
        opportunity_uid=f"uid-{overrides.get('fingerprint', 'x')}",
        fingerprint="fp-x",
        symbol="BTC",
        direction="LONG",
        setup_family="VOLATILITY_COMPRESSION",
        detector_version="vol_compression_v0_1_1",
        primary_timeframe="5m",
        first_detected_at="2026-01-01T00:00:00Z",
        last_seen_at="2026-01-01T00:00:00Z",
        source_candle_open_time=1000,
        source_candle_close_time=1300,
        anchor_price=100.0,
        anchor_open_time=1000,
        evidence_json="{}",
        warnings_json="[]",
        measurements_json="{}",
    )
    base.update(overrides)
    repo.insert_opportunity(conn, Opportunity(**base))


def test_report_labels_new_compression_snapshot_as_frozen(monkeypatch, tmp_path, capsys):
    db_path = str(tmp_path / "report_new_compression.db")
    _env(monkeypatch, db_path)
    conn = init_db(db_path)
    try:
        _seed_opportunity(
            conn,
            opportunity_uid="uid-new",
            fingerprint="fp-new",
            detector_version="vol_compression_v0_1_1",
        )
    finally:
        close_db()

    report_opportunities.main([])
    out = capsys.readouterr().out
    assert "vol_compression_v0_1_1" in out
    assert "frozen" in out.lower()


def test_report_labels_legacy_compression_version_as_not_frozen(monkeypatch, tmp_path, capsys):
    db_path = str(tmp_path / "report_legacy_compression.db")
    _env(monkeypatch, db_path)
    conn = init_db(db_path)
    try:
        _seed_opportunity(
            conn,
            opportunity_uid="uid-legacy",
            fingerprint="fp-legacy",
            detector_version="vol_compression_v0_1",
        )
    finally:
        close_db()

    report_opportunities.main([])
    out = capsys.readouterr().out
    label = report_opportunities._snapshot_label(
        {"setup_family": "VOLATILITY_COMPRESSION", "detector_version": "vol_compression_v0_1"}
    )
    assert "pre-snapshot" in label
    assert label in out


def test_report_labels_sweep_reclaim_as_structural_and_refreshed(monkeypatch, tmp_path, capsys):
    db_path = str(tmp_path / "report_sweep.db")
    _env(monkeypatch, db_path)
    conn = init_db(db_path)
    try:
        _seed_opportunity(
            conn,
            opportunity_uid="uid-sweep",
            fingerprint="fp-sweep",
            setup_family="SWEEP_RECLAIM",
            detector_version="sweep_reclaim_v0_1",
        )
    finally:
        close_db()

    report_opportunities.main([])
    out = capsys.readouterr().out
    assert "structural direction" in out
    assert "refreshed each reconfirmation" in out


def test_report_detector_version_breakdown_distinguishes_versions(monkeypatch, tmp_path, capsys):
    db_path = str(tmp_path / "report_versions.db")
    _env(monkeypatch, db_path)
    conn = init_db(db_path)
    try:
        _seed_opportunity(
            conn, opportunity_uid="uid-a", fingerprint="fp-a",
            detector_version="vol_compression_v0_1_1",
        )
        _seed_opportunity(
            conn, opportunity_uid="uid-b", fingerprint="fp-b",
            detector_version="vol_compression_v0_1",
        )
    finally:
        close_db()

    report_opportunities.main([])
    out = capsys.readouterr().out
    assert "By detector_version:" in out
    assert "vol_compression_v0_1_1" in out
    assert "vol_compression_v0_1 " in out or out.count("vol_compression_v0_1") >= 2
