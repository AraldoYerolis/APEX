"""SQLite connection and initialization."""
from __future__ import annotations

import sqlite3
from pathlib import Path

_conn: sqlite3.Connection | None = None

# Milestone 10A: new nullable columns for signal_observations.
# SQLite does not support ADD COLUMN IF NOT EXISTS, so we try each ALTER and
# swallow the OperationalError that fires when the column already exists.
_SIGNAL_OBS_NEW_COLUMNS = [
    ("hit_1r_at",              "TEXT"),
    ("hit_2r_at",              "TEXT"),
    ("stopped_at",             "TEXT"),
    ("expired_at",             "TEXT"),
    ("first_terminal_status",  "TEXT"),
    ("final_status",           "TEXT"),
    ("time_to_1r_seconds",     "REAL"),
    ("time_to_2r_seconds",     "REAL"),
    ("time_to_stop_seconds",   "REAL"),
    ("time_to_expiry_seconds", "REAL"),
    ("hit_1r_before_stop",     "INTEGER"),
    ("hit_1r_before_expiry",   "INTEGER"),
]


def _migrate_signal_observations(conn: sqlite3.Connection) -> None:
    """Add Milestone 10A columns to signal_observations on existing databases.

    Safe to run on both fresh installs (columns already in schema.sql) and
    existing databases (ALTER TABLE is a no-op when the column is present
    because the error is caught).
    """
    for col, col_type in _SIGNAL_OBS_NEW_COLUMNS:
        try:
            conn.execute(
                f"ALTER TABLE signal_observations ADD COLUMN {col} {col_type}"
            )
            conn.commit()
        except sqlite3.OperationalError:
            # "duplicate column name" — column already exists; skip.
            pass


def get_connection() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _conn


def init_db(db_path: str) -> sqlite3.Connection:
    global _conn

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    schema_path = Path(__file__).parent / "schema.sql"
    with open(schema_path) as f:
        conn.executescript(f.read())
    conn.commit()

    _migrate_signal_observations(conn)

    _conn = conn
    return conn


def is_open() -> bool:
    """Return True if a connection exists and has not been closed."""
    return _conn is not None


def close_db() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None
