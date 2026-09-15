"""Tests for the opportunity_observations schema migrations in
src/apex/db/connection.py, invoked by init_db():
_migrate_opportunity_observations_setup_family (the five-family widening)
and _migrate_opportunity_observations_score_columns (Context and ranking
v0.1's five nullable score/context columns), including their required
ordering.

Covers: a fresh database accepting all five setup families and rejecting an
unknown one, and already declaring all five score columns; an old exact
two-family table with representative ACTIVE and EXPIRED rows migrating
without data/ID/timestamp/JSON/default/constraint/index loss, ending up
with all five score columns NULL (no backfill/re-scoring); a second
init_db() call being idempotent; a current five-family database that
predates the score columns gaining exactly those five, appended at the end,
via ALTER; a database with only some score columns already present gaining
the rest; an induced score-column type mismatch failing closed without
altering anything; and the setup_family migration's own unexpected table
shape, a trigger, an unexpected existing family value, or a leftover
migration table each failing closed and leaving the original table/data
usable and unchanged (score-column migration never reached in those cases,
since it deliberately runs AFTER setup_family migration).

All tests use disposable tmp_path SQLite files only — never the production
database or any repository data file.
"""
from __future__ import annotations

import sqlite3

import pytest

from apex.db import connection as connection_module
from apex.db.connection import (
    _OPPORTUNITY_OBSERVATIONS_MIGRATION_TMP_TABLE,
    _OPPORTUNITY_OBSERVATIONS_SCORE_COLUMNS,
    _migrate_opportunity_observations_score_columns,
    close_db,
    init_db,
)

_SCORE_COLUMN_NAMES = tuple(name for name, _ in _OPPORTUNITY_OBSERVATIONS_SCORE_COLUMNS)


class _TrackingConnection(sqlite3.Connection):
    """sqlite3.Connection subclass that records whether close() was called
    on this exact instance, for proving init_db() closes the connection it
    opened when initialization fails partway through."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.close_called = False

    def close(self) -> None:
        self.close_called = True
        super().close()


# The exact pre-widen (two-family) opportunity_observations table + its three
# named indexes, duplicated here (not imported from production code) so this
# test independently pins the "old" shape it claims to simulate.
_OLD_OPPORTUNITY_OBSERVATIONS_SQL = """
CREATE TABLE opportunity_observations (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_uid          TEXT NOT NULL UNIQUE,
    fingerprint              TEXT NOT NULL,
    symbol                   TEXT NOT NULL,
    direction                TEXT NOT NULL CHECK(direction IN ('LONG','SHORT')),
    setup_family             TEXT NOT NULL CHECK(setup_family IN ('VOLATILITY_COMPRESSION','SWEEP_RECLAIM')),
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
CREATE INDEX idx_opportunity_observations_fingerprint
    ON opportunity_observations(fingerprint, status);
CREATE INDEX idx_opportunity_observations_lookup
    ON opportunity_observations(symbol, setup_family, primary_timeframe, status);
CREATE INDEX idx_opportunity_observations_uid
    ON opportunity_observations(opportunity_uid);
"""

# The pre-widen (setup_family migration) column shape — used to build the
# old two-family test database (_OLD_OPPORTUNITY_OBSERVATIONS_SQL matches
# this exactly) and as the expected shape immediately after that migration
# alone, before the score-column migration has run.
_PRE_SCORE_COLUMNS = (
    "id", "opportunity_uid", "fingerprint", "symbol", "direction", "setup_family",
    "detector_version", "contract_version", "primary_timeframe", "status",
    "research_only", "first_detected_at", "last_seen_at", "occurrence_count",
    "source_candle_open_time", "source_candle_close_time", "anchor_price",
    "anchor_open_time", "evidence_json", "warnings_json", "measurements_json",
    "closed_at", "created_at", "updated_at",
)

# The full column shape after BOTH migrations have run (init_db()'s normal,
# complete path): the five Context and ranking v0.1 score columns are always
# appended after `updated_at` by ALTER TABLE ADD COLUMN, in this fixed order.
_EXPECTED_COLUMNS = _PRE_SCORE_COLUMNS + _SCORE_COLUMN_NAMES

_EXPECTED_INDEXES = (
    "idx_opportunity_observations_fingerprint",
    "idx_opportunity_observations_lookup",
    "idx_opportunity_observations_uid",
    # opportunity_uid is declared TEXT NOT NULL UNIQUE, so SQLite always
    # creates this implicit unique-constraint index alongside the named ones.
    "sqlite_autoindex_opportunity_observations_1",
)

_ACTIVE_ROW = {
    "id": 1,
    "opportunity_uid": "uid-active-1",
    "fingerprint": "fp-active-1",
    "symbol": "BTC",
    "direction": "LONG",
    "setup_family": "SWEEP_RECLAIM",
    "detector_version": "sweep_reclaim_v0_1",
    "contract_version": "opportunity_v0_1",
    "primary_timeframe": "5m",
    "status": "ACTIVE",
    "research_only": 1,
    "first_detected_at": "2026-01-01T00:00:00Z",
    "last_seen_at": "2026-01-01T00:05:00Z",
    "occurrence_count": 2,
    "source_candle_open_time": 1000,
    "source_candle_close_time": 1300,
    "anchor_price": 97.5,
    "anchor_open_time": 100,
    "evidence_json": '{"k": "v"}',
    "warnings_json": "[]",
    "measurements_json": '{"x": 1}',
    "closed_at": None,
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:05:00Z",
}

_EXPIRED_ROW = {
    "id": 2,
    "opportunity_uid": "uid-expired-2",
    "fingerprint": "fp-expired-2",
    "symbol": "ETH",
    "direction": "SHORT",
    "setup_family": "VOLATILITY_COMPRESSION",
    "detector_version": "vol_compression_v0_1_1",
    "contract_version": "opportunity_v0_1",
    "primary_timeframe": "3m",
    "status": "EXPIRED",
    "research_only": 1,
    "first_detected_at": "2026-01-02T00:00:00Z",
    "last_seen_at": "2026-01-02T00:10:00Z",
    "occurrence_count": 3,
    "source_candle_open_time": 2000,
    "source_candle_close_time": 2180,
    "anchor_price": 210.0,
    "anchor_open_time": 1900,
    "evidence_json": '{"a": [1, 2]}',
    "warnings_json": '["conflict"]',
    "measurements_json": '{"atr_now": 1.5}',
    "closed_at": "2026-01-02T00:20:00Z",
    "created_at": "2026-01-02T00:00:00Z",
    "updated_at": "2026-01-02T00:20:00Z",
}


def _build_old_db(db_path: str, extra_sql: str = "") -> None:
    """Create a disposable SQLite file containing only the exact pre-widen
    opportunity_observations table (+ indexes), with two representative rows
    (one ACTIVE, one EXPIRED), plus any `extra_sql` (e.g. a trigger or a
    leftover migration table) executed immediately after.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_OLD_OPPORTUNITY_OBSERVATIONS_SQL)
        for row in (_ACTIVE_ROW, _EXPIRED_ROW):
            columns = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            conn.execute(
                f"INSERT INTO opportunity_observations ({columns}) VALUES ({placeholders})",
                tuple(row.values()),
            )
        if extra_sql:
            conn.executescript(extra_sql)
        conn.commit()
    finally:
        conn.close()


def _raw_connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _table_sql(conn: sqlite3.Connection, name: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row[0] if row else ""


def _index_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)
    ).fetchall()
    return {r[0] for r in rows}


def _insert_opportunity(conn: sqlite3.Connection, *, setup_family: str, uid: str) -> None:
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
            uid, f"fp-{uid}", "BTC", "LONG", setup_family,
            "test_v0_1", "opportunity_v0_1", "5m",
            "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
            1000, 1300,
        ),
    )
    conn.commit()


# ------------------------------------------------------------------ fresh database


def test_fresh_database_accepts_five_families_and_rejects_unknown(tmp_path):
    db_path = str(tmp_path / "fresh.db")
    conn = init_db(db_path)
    try:
        for i, family in enumerate(
            (
                "VOLATILITY_COMPRESSION",
                "SWEEP_RECLAIM",
                "SUPPORT_RESISTANCE_REJECTION",
                "SUPPORT_RESISTANCE_BREAKOUT_RETEST",
                "SUPPORT_RESISTANCE_FAILED_BREAKOUT",
            )
        ):
            _insert_opportunity(conn, setup_family=family, uid=f"fresh-{i}")
        rows = conn.execute("SELECT setup_family FROM opportunity_observations").fetchall()
        assert {r["setup_family"] for r in rows} == {
            "VOLATILITY_COMPRESSION",
            "SWEEP_RECLAIM",
            "SUPPORT_RESISTANCE_REJECTION",
            "SUPPORT_RESISTANCE_BREAKOUT_RETEST",
            "SUPPORT_RESISTANCE_FAILED_BREAKOUT",
        }

        with pytest.raises(sqlite3.IntegrityError):
            _insert_opportunity(conn, setup_family="NOT_A_REAL_FAMILY", uid="fresh-bad")
    finally:
        close_db()


# ------------------------------------------------------------------ old database migration


def test_old_two_family_table_migrates_preserving_data_and_allows_new_families(tmp_path):
    db_path = str(tmp_path / "old.db")
    _build_old_db(db_path)

    conn = init_db(db_path)
    try:
        table_sql = _table_sql(conn, "opportunity_observations")
        for family in (
            "VOLATILITY_COMPRESSION",
            "SWEEP_RECLAIM",
            "SUPPORT_RESISTANCE_REJECTION",
            "SUPPORT_RESISTANCE_BREAKOUT_RETEST",
            "SUPPORT_RESISTANCE_FAILED_BREAKOUT",
        ):
            assert f"'{family}'" in table_sql

        assert _index_names(conn, "opportunity_observations") == set(_EXPECTED_INDEXES)

        columns = conn.execute("PRAGMA table_info(opportunity_observations)").fetchall()
        assert tuple(c["name"] for c in columns) == _EXPECTED_COLUMNS

        rows = {
            r["opportunity_uid"]: dict(r)
            for r in conn.execute("SELECT * FROM opportunity_observations").fetchall()
        }
        assert set(rows) == {"uid-active-1", "uid-expired-2"}
        for expected in (_ACTIVE_ROW, _EXPIRED_ROW):
            actual = rows[expected["opportunity_uid"]]
            for key, value in expected.items():
                assert actual[key] == value, f"{expected['opportunity_uid']}.{key} changed"
            # Context and ranking v0.1's five score columns are additive-only:
            # a legacy row migrated here gets them as NULL, never backfilled
            # or re-scored from data that didn't exist at first detection.
            for score_column in _SCORE_COLUMN_NAMES:
                assert actual[score_column] is None, f"{score_column} was backfilled"

        # New families can now be inserted; unknown families are still rejected.
        _insert_opportunity(conn, setup_family="SUPPORT_RESISTANCE_REJECTION", uid="new-1")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_opportunity(conn, setup_family="NOT_A_REAL_FAMILY", uid="new-bad")

        # AUTOINCREMENT advances above the prior maximum explicit id (2).
        new_row = conn.execute(
            "SELECT id FROM opportunity_observations WHERE opportunity_uid='new-1'"
        ).fetchone()
        assert new_row["id"] > 2
    finally:
        close_db()


def test_second_init_db_call_is_idempotent(tmp_path):
    db_path = str(tmp_path / "idempotent.db")
    _build_old_db(db_path)

    conn = init_db(db_path)
    rows_after_first = {
        r["opportunity_uid"]: dict(r)
        for r in conn.execute("SELECT * FROM opportunity_observations").fetchall()
    }
    table_sql_after_first = _table_sql(conn, "opportunity_observations")
    close_db()

    conn2 = init_db(db_path)
    try:
        rows_after_second = {
            r["opportunity_uid"]: dict(r)
            for r in conn2.execute("SELECT * FROM opportunity_observations").fetchall()
        }
        assert rows_after_second == rows_after_first
        assert _table_sql(conn2, "opportunity_observations") == table_sql_after_first
        assert _index_names(conn2, "opportunity_observations") == set(_EXPECTED_INDEXES)
        columns = conn2.execute("PRAGMA table_info(opportunity_observations)").fetchall()
        assert tuple(c["name"] for c in columns) == _EXPECTED_COLUMNS

        # Still usable and still enforcing the widened constraint.
        _insert_opportunity(conn2, setup_family="SUPPORT_RESISTANCE_FAILED_BREAKOUT", uid="post-idempotent")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_opportunity(conn2, setup_family="NOT_A_REAL_FAMILY", uid="post-idempotent-bad")
    finally:
        close_db()


# ------------------------------------------------------------------ fail-closed guards


def test_unexpected_column_shape_fails_closed(tmp_path):
    db_path = str(tmp_path / "bad_shape.db")
    _build_old_db(db_path, extra_sql="ALTER TABLE opportunity_observations ADD COLUMN extra_col TEXT;")

    try:
        with pytest.raises(RuntimeError, match="unexpected column shape"):
            init_db(db_path)
    finally:
        close_db()

    # Original table/data must be left usable and unchanged.
    raw = _raw_connect(db_path)
    try:
        rows = raw.execute("SELECT opportunity_uid FROM opportunity_observations").fetchall()
        assert {r["opportunity_uid"] for r in rows} == {"uid-active-1", "uid-expired-2"}
        assert "extra_col" in _table_sql(raw, "opportunity_observations")
    finally:
        raw.close()


def test_trigger_present_fails_closed(tmp_path):
    db_path = str(tmp_path / "bad_trigger.db")
    trigger_sql = """
    CREATE TRIGGER trg_opportunity_observations_noop
    AFTER INSERT ON opportunity_observations
    BEGIN
        SELECT 1;
    END;
    """
    _build_old_db(db_path, extra_sql=trigger_sql)

    try:
        with pytest.raises(RuntimeError, match="trigger"):
            init_db(db_path)
    finally:
        close_db()

    raw = _raw_connect(db_path)
    try:
        rows = raw.execute("SELECT opportunity_uid FROM opportunity_observations").fetchall()
        assert {r["opportunity_uid"] for r in rows} == {"uid-active-1", "uid-expired-2"}
        triggers = raw.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='opportunity_observations'"
        ).fetchall()
        assert len(triggers) == 1
    finally:
        raw.close()


def test_unexpected_existing_family_value_fails_closed(tmp_path):
    db_path = str(tmp_path / "bad_family.db")
    # A raw table without the setup_family CHECK, so an out-of-allowlist
    # value can be inserted directly to simulate corrupted existing data
    # (the column-shape check alone would not catch this).
    conn = sqlite3.connect(db_path)
    unchecked_sql = _OLD_OPPORTUNITY_OBSERVATIONS_SQL.replace(
        "setup_family             TEXT NOT NULL CHECK(setup_family IN ('VOLATILITY_COMPRESSION','SWEEP_RECLAIM')),",
        "setup_family             TEXT NOT NULL,",
    )
    conn.executescript(unchecked_sql)
    for row in (_ACTIVE_ROW, _EXPIRED_ROW):
        columns = ", ".join(row.keys())
        placeholders = ", ".join("?" for _ in row)
        conn.execute(
            f"INSERT INTO opportunity_observations ({columns}) VALUES ({placeholders})",
            tuple(row.values()),
        )
    conn.execute(
        "UPDATE opportunity_observations SET setup_family='ROGUE_FAMILY' WHERE opportunity_uid='uid-active-1'"
    )
    conn.commit()
    conn.close()

    try:
        with pytest.raises(RuntimeError, match="setup_family"):
            init_db(db_path)
    finally:
        close_db()

    raw = _raw_connect(db_path)
    try:
        row = raw.execute(
            "SELECT setup_family FROM opportunity_observations WHERE opportunity_uid='uid-active-1'"
        ).fetchone()
        assert row["setup_family"] == "ROGUE_FAMILY"
        assert raw.execute("SELECT COUNT(*) FROM opportunity_observations").fetchone()[0] == 2
    finally:
        raw.close()


def test_migration_failure_closes_connection_and_leaves_global_unset(tmp_path, monkeypatch):
    db_path = str(tmp_path / "close_on_fail.db")
    _build_old_db(db_path, extra_sql="ALTER TABLE opportunity_observations ADD COLUMN extra_col TEXT;")

    created: list[_TrackingConnection] = []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        kwargs["factory"] = _TrackingConnection
        conn = real_connect(*args, **kwargs)
        created.append(conn)
        return conn

    monkeypatch.setattr(connection_module.sqlite3, "connect", tracking_connect)

    try:
        with pytest.raises(RuntimeError, match="unexpected column shape"):
            init_db(db_path)

        assert len(created) == 1
        assert created[0].close_called is True
        assert connection_module._conn is None
    finally:
        close_db()

    # Legacy table/data must remain usable and unchanged from a separately
    # opened connection, using the real (unpatched) sqlite3.connect.
    raw = real_connect(db_path)
    raw.row_factory = sqlite3.Row
    try:
        rows = raw.execute("SELECT opportunity_uid FROM opportunity_observations").fetchall()
        assert {r["opportunity_uid"] for r in rows} == {"uid-active-1", "uid-expired-2"}
        assert "extra_col" in _table_sql(raw, "opportunity_observations")
    finally:
        raw.close()


def test_leftover_migration_tmp_table_fails_closed(tmp_path):
    db_path = str(tmp_path / "leftover_tmp.db")
    leftover_sql = f"CREATE TABLE {_OPPORTUNITY_OBSERVATIONS_MIGRATION_TMP_TABLE} (id INTEGER);"
    _build_old_db(db_path, extra_sql=leftover_sql)

    try:
        with pytest.raises(RuntimeError, match="leftover table"):
            init_db(db_path)
    finally:
        close_db()

    raw = _raw_connect(db_path)
    try:
        rows = raw.execute("SELECT opportunity_uid FROM opportunity_observations").fetchall()
        assert {r["opportunity_uid"] for r in rows} == {"uid-active-1", "uid-expired-2"}
        # Old two-family constraint is still in force (migration never ran).
        assert "SUPPORT_RESISTANCE_REJECTION" not in _table_sql(raw, "opportunity_observations")
        leftover = raw.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (_OPPORTUNITY_OBSERVATIONS_MIGRATION_TMP_TABLE,),
        ).fetchone()
        assert leftover is not None
    finally:
        raw.close()


# ------------------------------------------------------------------ score-column migration
# (Context and ranking v0.1's _migrate_opportunity_observations_score_columns)


# A "current" (already five-family-widened) opportunity_observations table
# that predates Context and ranking v0.1 — i.e. it has all five setup
# families in its CHECK constraint already, but none of the five score
# columns. Distinct from _OLD_OPPORTUNITY_OBSERVATIONS_SQL (which is the
# pre-widen two-family shape the setup_family migration itself targets).
_FIVE_FAMILY_NO_SCORE_COLUMNS_SQL = """
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
CREATE INDEX idx_opportunity_observations_fingerprint
    ON opportunity_observations(fingerprint, status);
CREATE INDEX idx_opportunity_observations_lookup
    ON opportunity_observations(symbol, setup_family, primary_timeframe, status);
CREATE INDEX idx_opportunity_observations_uid
    ON opportunity_observations(opportunity_uid);
"""


def _build_five_family_no_score_columns_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_FIVE_FAMILY_NO_SCORE_COLUMNS_SQL)
        columns = ", ".join(_ACTIVE_ROW.keys())
        placeholders = ", ".join("?" for _ in _ACTIVE_ROW)
        conn.execute(
            f"INSERT INTO opportunity_observations ({columns}) VALUES ({placeholders})",
            tuple(_ACTIVE_ROW.values()),
        )
        conn.commit()
    finally:
        conn.close()


def test_fresh_database_already_has_all_five_score_columns_with_correct_types(tmp_path):
    db_path = str(tmp_path / "fresh_score_columns.db")
    conn = init_db(db_path)
    try:
        columns = {
            row["name"]: row["type"]
            for row in conn.execute("PRAGMA table_info(opportunity_observations)").fetchall()
        }
        for name, col_type in _OPPORTUNITY_OBSERVATIONS_SCORE_COLUMNS:
            assert columns[name].upper() == col_type
    finally:
        close_db()


def test_current_five_family_database_gains_exactly_the_five_score_columns(tmp_path):
    db_path = str(tmp_path / "five_family_no_score.db")
    _build_five_family_no_score_columns_db(db_path)

    conn = init_db(db_path)
    try:
        columns = conn.execute("PRAGMA table_info(opportunity_observations)").fetchall()
        assert tuple(c["name"] for c in columns) == _PRE_SCORE_COLUMNS + _SCORE_COLUMN_NAMES

        row = conn.execute(
            "SELECT * FROM opportunity_observations WHERE opportunity_uid='uid-active-1'"
        ).fetchone()
        for score_column in _SCORE_COLUMN_NAMES:
            assert row[score_column] is None

        # Still usable for real inserts including the new columns.
        conn.execute(
            """
            INSERT INTO opportunity_observations (
                opportunity_uid, fingerprint, symbol, direction, setup_family,
                detector_version, contract_version, primary_timeframe,
                first_detected_at, last_seen_at,
                source_candle_open_time, source_candle_close_time,
                total_score, score_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "uid-scored", "fp-scored", "BTC", "LONG", "SWEEP_RECLAIM",
                "test_v0_1", "opportunity_v0_1", "5m",
                "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
                1000, 1300, 62.5, "context_alignment_v0_1",
            ),
        )
        conn.commit()
        scored_row = conn.execute(
            "SELECT total_score, score_version FROM opportunity_observations WHERE opportunity_uid='uid-scored'"
        ).fetchone()
        assert scored_row["total_score"] == 62.5
        assert scored_row["score_version"] == "context_alignment_v0_1"
    finally:
        close_db()


def test_partial_score_columns_already_present_gains_only_the_rest(tmp_path):
    db_path = str(tmp_path / "partial_score_columns.db")
    _build_five_family_no_score_columns_db(db_path)

    # Simulate a database that already gained ONE of the five columns
    # (e.g. an interrupted prior migration) before init_db() ever runs.
    raw = sqlite3.connect(db_path)
    try:
        raw.execute("ALTER TABLE opportunity_observations ADD COLUMN total_score REAL")
        raw.commit()
    finally:
        raw.close()

    conn = init_db(db_path)
    try:
        columns = conn.execute("PRAGMA table_info(opportunity_observations)").fetchall()
        names = tuple(c["name"] for c in columns)
        # total_score was added first (manually, above); the migration only
        # appends the remaining four after it, in their fixed order.
        assert names == _PRE_SCORE_COLUMNS + ("total_score", "context_json", "component_scores_json", "score_version", "score_warnings_json")
        for score_column in _SCORE_COLUMN_NAMES:
            assert score_column in names
    finally:
        close_db()


def test_induced_score_column_type_mismatch_fails_closed_without_altering_anything(tmp_path):
    db_path = str(tmp_path / "score_column_type_mismatch.db")
    _build_five_family_no_score_columns_db(db_path)

    # Simulate a corrupted/foreign migration: total_score exists with the
    # WRONG declared type (TEXT instead of REAL).
    raw = sqlite3.connect(db_path)
    try:
        raw.execute("ALTER TABLE opportunity_observations ADD COLUMN total_score TEXT")
        raw.commit()
    finally:
        raw.close()

    try:
        with pytest.raises(RuntimeError, match="total_score"):
            init_db(db_path)
    finally:
        close_db()

    # No column was added at all — the type check runs as a pure read
    # before any ALTER, so this fails before mutating anything.
    raw = _raw_connect(db_path)
    try:
        columns = raw.execute("PRAGMA table_info(opportunity_observations)").fetchall()
        names = tuple(c["name"] for c in columns)
        assert names == _PRE_SCORE_COLUMNS + ("total_score",)
        row = raw.execute(
            "SELECT opportunity_uid FROM opportunity_observations WHERE opportunity_uid='uid-active-1'"
        ).fetchone()
        assert row is not None
    finally:
        raw.close()


class _FailingOnSecondAlterConnection(sqlite3.Connection):
    """sqlite3.Connection subclass that raises on the SECOND ALTER TABLE
    statement it executes (a real DDL execution failure partway through the
    migration's ALTER sequence), letting every other statement (BEGIN,
    PRAGMA, the first ALTER, SELECT, rollback) run for real."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._alter_count = 0

    def execute(self, sql, *args, **kwargs):
        if sql.strip().upper().startswith("ALTER TABLE"):
            self._alter_count += 1
            if self._alter_count == 2:
                raise sqlite3.OperationalError("simulated mid-migration failure")
        return super().execute(sql, *args, **kwargs)


def test_score_column_migration_rolls_back_atomically_on_mid_alter_failure(tmp_path):
    """A failure partway through the ALTER sequence (not a pre-flight type
    mismatch) must roll back every ALTER already issued in that same call —
    never a partially-added set of columns.
    """
    db_path = str(tmp_path / "score_column_mid_failure.db")
    _build_five_family_no_score_columns_db(db_path)

    conn = sqlite3.connect(db_path, factory=_FailingOnSecondAlterConnection)
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(sqlite3.OperationalError, match="simulated mid-migration failure"):
            _migrate_opportunity_observations_score_columns(conn)

        columns = conn.execute("PRAGMA table_info(opportunity_observations)").fetchall()
        names = tuple(c["name"] for c in columns)
        # The first ALTER (context_json) must have been rolled back along
        # with the second (which raised) — all-or-nothing, not partial.
        assert names == _PRE_SCORE_COLUMNS
        row = conn.execute(
            "SELECT opportunity_uid FROM opportunity_observations WHERE opportunity_uid='uid-active-1'"
        ).fetchone()
        assert row is not None
    finally:
        conn.close()
