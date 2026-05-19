"""Tests for Milestone 10B-Prep: snapshot script, --since filtering, docs.

Covers:
- create_apex_snapshot runs without modifying the DB
- report_signal_observations --since filters correctly
- debug_signal_conditions --since filters correctly
- docs/AGENT_ARCHITECTURE.md and docs/AGENT_BACKLOG.md exist
"""
from __future__ import annotations

import io
import os
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from apex.db import repository as repo
from apex.db.connection import close_db, init_db
from apex.db.models import SignalObservation
from apex.utils.ids import new_uid
from apex.utils.time import minutes_ago_iso, minutes_from_now, utcnow_iso


# ------------------------------------------------------------------ helpers

def _make_env_overrides(monkeypatch, db_path: str) -> None:
    monkeypatch.setenv("APEX_DB_PATH", db_path)
    monkeypatch.setenv("ACTION_TOKEN_SECRET", "test-10b-secret")
    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "fake")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "fake")
    monkeypatch.setenv("ALERTS_ENABLED", "false")
    monkeypatch.setenv("DRY_RUN_MODE", "true")


def _insert_obs(conn, observed_at: str, symbol: str = "BTC") -> None:
    obs = SignalObservation(
        observation_uid=new_uid(),
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


# ------------------------------------------------------------------ docs exist

def test_agent_architecture_doc_exists():
    p = Path(__file__).resolve().parents[1] / "docs" / "AGENT_ARCHITECTURE.md"
    assert p.exists(), f"Missing: {p}"
    content = p.read_text()
    assert "Core Design Principle" in content
    assert "Safety Rules" in content
    assert "Signal Debug Agent" in content
    assert "Approval Flow" in content


def test_agent_backlog_doc_exists():
    p = Path(__file__).resolve().parents[1] / "docs" / "AGENT_BACKLOG.md"
    assert p.exists(), f"Missing: {p}"
    content = p.read_text()
    assert "10B" in content
    assert "10C" in content
    assert "13A" in content
    assert "14+" in content
    assert "Live Execution" in content


# ------------------------------------------------------------------ --since filtering: report

def test_report_since_filters_old_observations(tmp_path, monkeypatch):
    """--since should exclude observations older than the cutoff."""
    db_path = str(tmp_path / "report_since.db")
    _make_env_overrides(monkeypatch, db_path)

    conn = init_db(db_path)
    # Old observation: 2 hours ago
    _insert_obs(conn, minutes_ago_iso(120), symbol="OLD_SYM")
    # Recent observation: now
    _insert_obs(conn, utcnow_iso(), symbol="NEW_SYM")
    close_db()

    import scripts.report_signal_observations as mod

    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main(["--since", minutes_ago_iso(60)])

    output = buf.getvalue()
    assert "APEX Signal Observations Report" in output
    assert "Filtered since" in output
    # Total should be 1 (only the recent one)
    assert "Total observations  : 1" in output


def test_report_since_all_excluded_shows_no_observations(tmp_path, monkeypatch):
    """--since far in the future returns zero observations without crashing."""
    db_path = str(tmp_path / "report_since_empty.db")
    _make_env_overrides(monkeypatch, db_path)

    conn = init_db(db_path)
    _insert_obs(conn, minutes_ago_iso(120))
    close_db()

    import scripts.report_signal_observations as mod

    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main(["--since", minutes_from_now(60)])

    output = buf.getvalue()
    assert "No signal observations recorded yet" in output


def test_report_without_since_returns_all(tmp_path, monkeypatch):
    """Without --since, all observations are included."""
    db_path = str(tmp_path / "report_no_since.db")
    _make_env_overrides(monkeypatch, db_path)

    conn = init_db(db_path)
    _insert_obs(conn, minutes_ago_iso(120))
    _insert_obs(conn, utcnow_iso())
    close_db()

    import scripts.report_signal_observations as mod

    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main([])

    output = buf.getvalue()
    assert "Total observations  : 2" in output


# ------------------------------------------------------------------ --since filtering: debug

def test_debug_since_filters_old_observations(tmp_path, monkeypatch):
    """debug --since should exclude observations older than the cutoff."""
    db_path = str(tmp_path / "debug_since.db")
    _make_env_overrides(monkeypatch, db_path)

    conn = init_db(db_path)
    _insert_obs(conn, minutes_ago_iso(120), symbol="OLD_SYM")
    _insert_obs(conn, utcnow_iso(), symbol="NEW_SYM")
    close_db()

    import scripts.debug_signal_conditions as mod

    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main(["--since", minutes_ago_iso(60)])

    output = buf.getvalue()
    assert "APEX Debug: Signal Conditions" in output
    assert "Filtered since" in output
    # Funnel should show 1 confirmed observation
    assert "CONFIRMED_SETUP recorded    :     1" in output


def test_debug_without_since_returns_all(tmp_path, monkeypatch):
    """Without --since, all observations are included."""
    db_path = str(tmp_path / "debug_no_since.db")
    _make_env_overrides(monkeypatch, db_path)

    conn = init_db(db_path)
    _insert_obs(conn, minutes_ago_iso(120))
    _insert_obs(conn, utcnow_iso())
    close_db()

    import scripts.debug_signal_conditions as mod

    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main([])

    output = buf.getvalue()
    assert "CONFIRMED_SETUP recorded    :     2" in output


# ------------------------------------------------------------------ snapshot script

def test_snapshot_creates_file(tmp_path, monkeypatch):
    """create_apex_snapshot writes a non-empty file at the specified path."""
    db_path = str(tmp_path / "snap.db")
    out_path = str(tmp_path / "snap.txt")
    _make_env_overrides(monkeypatch, db_path)

    conn = init_db(db_path)
    _insert_obs(conn, utcnow_iso())
    close_db()

    import scripts.create_apex_snapshot as mod

    # Capture the printed "Snapshot written to: ..." line
    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main(["--out", out_path])

    assert "Snapshot written to:" in buf.getvalue()
    p = Path(out_path)
    assert p.exists()
    content = p.read_text()
    assert "APEX Status Snapshot" in content
    assert "Created:" in content


def test_snapshot_does_not_modify_db(tmp_path, monkeypatch):
    """Snapshot script must not change any observation rows."""
    db_path = str(tmp_path / "snap_readonly.db")
    out_path = str(tmp_path / "snap_readonly.txt")
    _make_env_overrides(monkeypatch, db_path)

    conn = init_db(db_path)
    _insert_obs(conn, utcnow_iso())
    # Capture the observation status before snapshot
    row_before = conn.execute("SELECT status, updated_at FROM signal_observations").fetchone()
    close_db()

    import scripts.create_apex_snapshot as mod

    io.StringIO()
    with redirect_stdout(io.StringIO()):
        mod.main(["--out", out_path])

    # Re-open DB and verify row is unchanged
    conn2 = init_db(db_path)
    row_after = conn2.execute("SELECT status, updated_at FROM signal_observations").fetchone()
    assert row_after["status"] == row_before["status"]
    assert row_after["updated_at"] == row_before["updated_at"]
    close_db()


def test_snapshot_includes_git_section(tmp_path, monkeypatch):
    """Snapshot output includes a Git section."""
    db_path = str(tmp_path / "snap_git.db")
    out_path = str(tmp_path / "snap_git.txt")
    _make_env_overrides(monkeypatch, db_path)

    conn = init_db(db_path)
    close_db()

    import scripts.create_apex_snapshot as mod

    with redirect_stdout(io.StringIO()):
        mod.main(["--out", out_path])

    content = Path(out_path).read_text()
    assert "Git" in content
    assert "branch" in content


def test_snapshot_with_since(tmp_path, monkeypatch):
    """Snapshot --since is forwarded to report and debug scripts."""
    db_path = str(tmp_path / "snap_since.db")
    out_path = str(tmp_path / "snap_since.txt")
    _make_env_overrides(monkeypatch, db_path)

    conn = init_db(db_path)
    _insert_obs(conn, utcnow_iso())
    close_db()

    import scripts.create_apex_snapshot as mod

    with redirect_stdout(io.StringIO()):
        mod.main(["--out", out_path, "--since", minutes_ago_iso(30)])

    content = Path(out_path).read_text()
    assert "Filtered since" in content


def test_snapshot_handles_no_observations(tmp_path, monkeypatch):
    """Snapshot runs cleanly when the DB has no observations yet."""
    db_path = str(tmp_path / "snap_empty.db")
    out_path = str(tmp_path / "snap_empty.txt")
    _make_env_overrides(monkeypatch, db_path)

    conn = init_db(db_path)
    close_db()

    import scripts.create_apex_snapshot as mod

    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main(["--out", out_path])

    assert Path(out_path).exists()
    content = Path(out_path).read_text()
    assert "APEX Status Snapshot" in content


def test_snapshot_default_path_under_tmp(tmp_path, monkeypatch):
    """Without --out, snapshot is written under /tmp."""
    db_path = str(tmp_path / "snap_default.db")
    _make_env_overrides(monkeypatch, db_path)

    conn = init_db(db_path)
    close_db()

    import scripts.create_apex_snapshot as mod

    created_path: list[str] = []

    original_write = Path.write_text

    def _patched_write(self, content, *a, **kw):
        created_path.append(str(self))
        return original_write(self, content, *a, **kw)

    monkeypatch.setattr(Path, "write_text", _patched_write)

    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main([])

    assert created_path, "write_text was never called"
    assert created_path[0].startswith("/tmp/apex_snapshot_")
    assert created_path[0].endswith(".txt")
    # Clean up
    Path(created_path[0]).unlink(missing_ok=True)
