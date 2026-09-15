"""Tests for scripts/report_opportunities.py's snapshot/version labeling and
its --ranked (Context and ranking v0.1) read-only ranked view.

Read-only, disposable test databases only. Does not exercise detectors or
the scheduler — seeds opportunity_observations rows directly via the
repository layer to control detector_version/setup_family precisely, then
asserts the report's printed output describes each row's snapshot
semantics accurately (see the script's "Snapshot/version semantics"
docstring section).
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys

import pytest

from apex.db import repository as repo
from apex.db.connection import close_db, init_db
from apex.opportunity.contract import Opportunity
from apex.opportunity.scoring import SCORE_VERSION

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


# ------------------------------------------------------------------ --ranked (read-only)


def _seed_scored_opportunity(conn, **overrides) -> None:
    base = dict(
        opportunity_uid=f"uid-{overrides.get('fingerprint', 'x')}",
        fingerprint="fp-x",
        symbol="BTC",
        direction="LONG",
        setup_family="SWEEP_RECLAIM",
        detector_version="sweep_reclaim_v0_1",
        primary_timeframe="5m",
        first_detected_at="2026-01-01T00:00:00Z",
        last_seen_at="2026-01-01T00:00:00Z",
        source_candle_open_time=1000,
        source_candle_close_time=1300,
        status="ACTIVE",
        total_score=75.0,
        score_version=SCORE_VERSION,
        component_scores_json='{"components": [], "available_weight": 25.0, "applicable_weight": 50.0}',
        score_warnings_json="[]",
        context_json="{}",
    )
    base.update(overrides)
    repo.insert_opportunity(conn, Opportunity(**base))


# The current five-family, pre-score-columns shape — i.e. a database that
# predates Context and ranking v0.1 entirely. Duplicated locally (not
# imported from a test-only fixture in another test file) so this test
# independently pins the "legacy" shape it claims to simulate, matching
# this test suite's existing per-file fixture convention.
_LEGACY_NO_SCORE_COLUMNS_SQL = """
CREATE TABLE opportunity_observations (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_uid          TEXT NOT NULL UNIQUE,
    fingerprint              TEXT NOT NULL,
    symbol                   TEXT NOT NULL,
    direction                TEXT NOT NULL CHECK(direction IN ('LONG','SHORT')),
    setup_family             TEXT NOT NULL CHECK(setup_family IN (
                                 'VOLATILITY_COMPRESSION','SWEEP_RECLAIM',
                                 'SUPPORT_RESISTANCE_REJECTION','SUPPORT_RESISTANCE_BREAKOUT_RETEST',
                                 'SUPPORT_RESISTANCE_FAILED_BREAKOUT'
                             )),
    detector_version         TEXT NOT NULL,
    contract_version         TEXT NOT NULL,
    primary_timeframe        TEXT NOT NULL CHECK(primary_timeframe IN ('3m','5m')),
    status                   TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','EXPIRED')),
    research_only            INTEGER NOT NULL DEFAULT 1,
    first_detected_at        TEXT NOT NULL,
    last_seen_at             TEXT NOT NULL,
    occurrence_count         INTEGER NOT NULL DEFAULT 1,
    source_candle_open_time  INTEGER NOT NULL,
    source_candle_close_time INTEGER NOT NULL,
    anchor_price             REAL,
    anchor_open_time         INTEGER,
    evidence_json            TEXT,
    warnings_json            TEXT,
    measurements_json        TEXT,
    closed_at                TEXT,
    created_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
"""


def _build_legacy_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_LEGACY_NO_SCORE_COLUMNS_SQL)
        conn.execute(
            """
            INSERT INTO opportunity_observations (
                opportunity_uid, fingerprint, symbol, direction, setup_family,
                detector_version, contract_version, primary_timeframe,
                first_detected_at, last_seen_at,
                source_candle_open_time, source_candle_close_time
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "uid-legacy", "fp-legacy", "BTC", "LONG", "SWEEP_RECLAIM",
                "sweep_reclaim_v0_1", "opportunity_v0_1", "5m",
                "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
                1000, 1300,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_ranked_report_prints_disclaimer_rank_and_score(monkeypatch, tmp_path, capsys):
    db_path = str(tmp_path / "ranked_basic.db")
    _env(monkeypatch, db_path)
    conn = init_db(db_path)
    try:
        _seed_scored_opportunity(conn, fingerprint="fp-a", opportunity_uid="uid-a", total_score=75.0)
    finally:
        close_db()

    report_opportunities.main(["--ranked"])
    out = capsys.readouterr().out
    assert "CONTEXT-ALIGNMENT" in out
    assert "NOT a win probability" in out
    assert "75.0" in out
    assert SCORE_VERSION in out
    assert "Status filter" in out
    assert "ACTIVE" in out  # default status


def test_ranked_report_never_creates_missing_database_file(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "does_not_exist.db"
    _env(monkeypatch, str(db_path))

    with pytest.raises(SystemExit):
        report_opportunities.main(["--ranked"])

    assert not db_path.exists()


def test_ranked_report_missing_opportunity_observations_table_is_graceful(monkeypatch, tmp_path):
    db_path = tmp_path / "empty_no_table.db"
    sqlite3.connect(str(db_path)).close()  # a real, valid, but table-less SQLite file
    _env(monkeypatch, str(db_path))

    report_opportunities.main(["--ranked"])  # must not raise or create/alter anything


def test_ranked_report_legacy_schema_reports_as_unscored_without_migrating(monkeypatch, tmp_path):
    db_path = tmp_path / "legacy_no_score_columns.db"
    _build_legacy_db(str(db_path))
    _env(monkeypatch, str(db_path))

    before_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()

    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        report_opportunities.main(["--ranked"])
    out = buf.getvalue()

    assert "UNSCORED" in out
    assert "predates Context and ranking v0.1" in out

    # A genuinely read-only connection: the file's bytes must be byte-
    # identical before and after --ranked ran (no migration, no write).
    after_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
    assert after_hash == before_hash

    # Schema itself was never touched: still no score columns.
    raw = sqlite3.connect(str(db_path))
    try:
        columns = {row[1] for row in raw.execute("PRAGMA table_info(opportunity_observations)").fetchall()}
        assert "total_score" not in columns
    finally:
        raw.close()


def test_ranked_report_read_only_connection_never_mutates_a_current_schema_database(
    monkeypatch, tmp_path
):
    """Data-equality check rather than a raw file-byte hash: init_db()
    enables WAL mode, whose own connection-close checkpoint behavior can
    innocuously touch the main file's bytes independent of anything
    --ranked itself does — a provably unchanged row set is the meaningful,
    non-flaky guarantee that no SQL write happened.
    """
    db_path = tmp_path / "ranked_no_mutation.db"
    _env(monkeypatch, str(db_path))
    conn = init_db(str(db_path))
    try:
        _seed_scored_opportunity(conn, fingerprint="fp-a", opportunity_uid="uid-a")
    finally:
        close_db()

    def _snapshot() -> list[dict]:
        raw = sqlite3.connect(str(db_path))
        raw.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in raw.execute("SELECT * FROM opportunity_observations").fetchall()]
        finally:
            raw.close()

    before = _snapshot()
    report_opportunities.main(["--ranked"])
    after = _snapshot()
    assert after == before


def test_ranked_report_default_status_active_excludes_expired_unless_overridden(
    monkeypatch, tmp_path, capsys
):
    db_path = str(tmp_path / "ranked_status.db")
    _env(monkeypatch, db_path)
    conn = init_db(db_path)
    try:
        _seed_scored_opportunity(
            conn, fingerprint="fp-active", opportunity_uid="uid-active",
            status="ACTIVE", total_score=40.0,
        )
        _seed_scored_opportunity(
            conn, fingerprint="fp-expired", opportunity_uid="uid-expir",
            status="EXPIRED", total_score=95.0,
        )
    finally:
        close_db()

    report_opportunities.main(["--ranked"])
    out = capsys.readouterr().out
    assert "uid-active" in out
    assert "uid-expir" not in out

    report_opportunities.main(["--ranked", "--status", "EXPIRED"])
    out2 = capsys.readouterr().out
    assert "uid-expir" in out2
    assert "uid-active" not in out2


def test_ranked_report_never_removes_low_or_zero_score_findings(monkeypatch, tmp_path, capsys):
    db_path = str(tmp_path / "ranked_low_score.db")
    _env(monkeypatch, db_path)
    conn = init_db(db_path)
    try:
        _seed_scored_opportunity(
            conn, fingerprint="fp-low", opportunity_uid="uid-low-score", total_score=0.0,
        )
    finally:
        close_db()

    report_opportunities.main(["--ranked"])
    out = capsys.readouterr().out
    assert "uid-low-sc" in out  # UID is truncated for display elsewhere; here full row line is printed
    assert "Total rows         : 1" in out
