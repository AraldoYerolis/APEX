"""SQLite connection and initialization."""
from __future__ import annotations

import sqlite3
from pathlib import Path

_conn: sqlite3.Connection | None = None


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
