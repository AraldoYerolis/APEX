"""Tests for correct shutdown ordering.

Documents the bug (close_db before log_event raises ProgrammingError) and
confirms the fix (log_event before close_db succeeds cleanly).
"""
from __future__ import annotations

import pytest

from apex.db import repository as repo
from apex.db.connection import close_db, init_db, is_open


@pytest.fixture
def fresh_conn(tmp_path):
    """Each test gets its own isolated DB."""
    c = init_db(str(tmp_path / "test.db"))
    yield c
    # Guard: close only if still open (some tests close it intentionally)
    if is_open():
        close_db()


# ------------------------------------------------------------------ is_open

def test_is_open_after_init(fresh_conn):
    assert is_open() is True


def test_is_open_false_after_close(fresh_conn):
    close_db()
    assert is_open() is False


# ------------------------------------------------------------------ ordering: correct (log then close)

def test_log_event_before_close_succeeds(fresh_conn):
    """The correct shutdown order: write event, then close — no exception raised."""
    # Both calls must complete without raising.
    repo.log_event(fresh_conn, "SHUTDOWN", "APEX stopped")
    close_db()
    assert is_open() is False


def test_log_event_on_open_conn_succeeds(fresh_conn):
    """log_event works normally when the connection is open."""
    repo.log_event(fresh_conn, "STARTUP", "test event", metadata={"key": "val"})
    row = fresh_conn.execute(
        "SELECT * FROM app_events WHERE event_type='STARTUP'"
    ).fetchone()
    assert row is not None
    assert row["message"] == "test event"


# ------------------------------------------------------------------ ordering: broken (close then log)

def test_log_event_after_close_raises(fresh_conn):
    """The broken order: close first, then log — raises ProgrammingError.

    This documents the original bug.  The fix is to never call close_db()
    before repo.log_event() in the shutdown sequence.
    """
    close_db()
    with pytest.raises(Exception):
        # sqlite3.ProgrammingError: Cannot operate on a closed database.
        repo.log_event(fresh_conn, "SHUTDOWN", "APEX stopped")


# ------------------------------------------------------------------ close_db idempotence

def test_close_db_twice_is_safe(fresh_conn):
    """Calling close_db twice must not raise."""
    close_db()
    close_db()  # second call is a no-op
    assert is_open() is False


def test_close_db_then_init_works(tmp_path):
    """close_db followed by init_db produces a fresh usable connection."""
    c1 = init_db(str(tmp_path / "db1.db"))
    assert is_open()
    close_db()
    assert not is_open()

    c2 = init_db(str(tmp_path / "db2.db"))
    assert is_open()
    repo.log_event(c2, "TEST", "re-init works")
    close_db()
