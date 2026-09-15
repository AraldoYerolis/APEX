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


# S/R detector integration v0.1: opportunity_observations.setup_family's
# CHECK constraint widened from two to five families. SQLite cannot ALTER a
# CHECK constraint in place, so an existing (pre-widen) table must be
# rebuilt rather than patched in place — see
# _migrate_opportunity_observations_setup_family below.
_OPPORTUNITY_OBSERVATIONS_NEW_FAMILIES = (
    "VOLATILITY_COMPRESSION",
    "SWEEP_RECLAIM",
    "SUPPORT_RESISTANCE_REJECTION",
    "SUPPORT_RESISTANCE_BREAKOUT_RETEST",
    "SUPPORT_RESISTANCE_FAILED_BREAKOUT",
)

# The exact column names, in exact order, of the pre-widen
# opportunity_observations table (and of the widened table below — the
# column set/order is unchanged by this migration, only the setup_family
# CHECK constraint is). A rebuild is refused if an existing table's shape
# does not match this exactly.
_OPPORTUNITY_OBSERVATIONS_COLUMNS = (
    "id", "opportunity_uid", "fingerprint", "symbol", "direction", "setup_family",
    "detector_version", "contract_version", "primary_timeframe", "status",
    "research_only", "first_detected_at", "last_seen_at", "occurrence_count",
    "source_candle_open_time", "source_candle_close_time", "anchor_price",
    "anchor_open_time", "evidence_json", "warnings_json", "measurements_json",
    "closed_at", "created_at", "updated_at",
)

# Fixed name for the temporary rebuild table. A pre-existing table under this
# exact name is treated as a leftover from an aborted prior migration attempt
# and fails the migration closed rather than being silently reused or
# dropped (see _migrate_opportunity_observations_setup_family).
_OPPORTUNITY_OBSERVATIONS_MIGRATION_TMP_TABLE = "_opportunity_observations_migration_tmp"


def _migrate_opportunity_observations_setup_family(conn: sqlite3.Connection) -> None:
    """Widen an existing opportunity_observations table's setup_family CHECK
    constraint to the current five setup families.

    Idempotent: does nothing if the table's stored CREATE TABLE SQL already
    contains all five family literals (a fresh install, or an already-
    migrated database) — migration state is read from SQLite schema
    metadata only, never inferred from row presence/absence.

    Fails closed (raises without mutating anything) on any table shape,
    trigger, existing row value, or leftover migration table that does not
    exactly match what this migration expects, rather than guessing. Once
    the rebuild itself starts, it runs inside one explicit transaction and
    rolls back on any exception — this function never catches and suppresses
    a migration failure itself; that is the caller's (init_db's) choice to
    make, and it does not catch it.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='opportunity_observations'"
    ).fetchone()
    if row is None:
        # schema.sql's CREATE TABLE IF NOT EXISTS always creates this table
        # before this function runs; this is a defensive no-op, not a path
        # expected to be exercised.
        return
    table_sql = row[0] or ""
    if all(f"'{family}'" in table_sql for family in _OPPORTUNITY_OBSERVATIONS_NEW_FAMILIES):
        return  # already widened (or a fresh five-family install)

    leftover_tmp = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (_OPPORTUNITY_OBSERVATIONS_MIGRATION_TMP_TABLE,),
    ).fetchone()
    if leftover_tmp is not None:
        raise RuntimeError(
            "opportunity_observations migration: leftover table "
            f"{_OPPORTUNITY_OBSERVATIONS_MIGRATION_TMP_TABLE!r} already exists; "
            "refusing to migrate"
        )

    triggers = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='opportunity_observations'"
    ).fetchall()
    if triggers:
        raise RuntimeError(
            "opportunity_observations migration: unexpected trigger(s) present; refusing to migrate"
        )

    columns = conn.execute("PRAGMA table_info(opportunity_observations)").fetchall()
    actual_column_names = tuple(c[1] for c in columns)
    if actual_column_names != _OPPORTUNITY_OBSERVATIONS_COLUMNS:
        raise RuntimeError(
            "opportunity_observations migration: unexpected column shape; refusing to migrate"
        )

    family_placeholders = ",".join("?" for _ in _OPPORTUNITY_OBSERVATIONS_NEW_FAMILIES)
    bad_family_count = conn.execute(
        "SELECT COUNT(*) FROM opportunity_observations "
        f"WHERE setup_family NOT IN ({family_placeholders})",
        _OPPORTUNITY_OBSERVATIONS_NEW_FAMILIES,
    ).fetchone()[0]
    if bad_family_count:
        raise RuntimeError(
            "opportunity_observations migration: existing row(s) with an unexpected "
            "setup_family; refusing to migrate"
        )

    column_list = ", ".join(_OPPORTUNITY_OBSERVATIONS_COLUMNS)
    family_list_sql = ",".join(f"'{family}'" for family in _OPPORTUNITY_OBSERVATIONS_NEW_FAMILIES)
    tmp_table = _OPPORTUNITY_OBSERVATIONS_MIGRATION_TMP_TABLE

    try:
        conn.execute("BEGIN")
        conn.execute(
            f"""
            CREATE TABLE {tmp_table} (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_uid          TEXT NOT NULL UNIQUE,
                fingerprint              TEXT NOT NULL,
                symbol                   TEXT NOT NULL,
                direction                TEXT NOT NULL CHECK(direction IN ('LONG','SHORT')),
                setup_family             TEXT NOT NULL CHECK(setup_family IN ({family_list_sql})),
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
            )
            """
        )
        conn.execute(
            f"INSERT INTO {tmp_table} ({column_list}) "
            f"SELECT {column_list} FROM opportunity_observations"
        )

        old_count = conn.execute("SELECT COUNT(*) FROM opportunity_observations").fetchone()[0]
        new_count = conn.execute(f"SELECT COUNT(*) FROM {tmp_table}").fetchone()[0]
        if old_count != new_count:
            raise RuntimeError(
                "opportunity_observations migration: row count mismatch "
                f"(old={old_count}, new={new_count}); aborting"
            )

        conn.execute("DROP TABLE opportunity_observations")
        conn.execute(f"ALTER TABLE {tmp_table} RENAME TO opportunity_observations")

        conn.execute(
            "CREATE INDEX idx_opportunity_observations_fingerprint "
            "ON opportunity_observations(fingerprint, status)"
        )
        conn.execute(
            "CREATE INDEX idx_opportunity_observations_lookup "
            "ON opportunity_observations(symbol, setup_family, primary_timeframe, status)"
        )
        conn.execute(
            "CREATE INDEX idx_opportunity_observations_uid "
            "ON opportunity_observations(opportunity_uid)"
        )

        # AUTOINCREMENT continuity: SQLite's ALTER TABLE RENAME already
        # retargets the renamed table's own sqlite_sequence row to the new
        # name, but this explicitly (re)asserts the tracked value rather
        # than relying on that alone, so the next auto-generated id is
        # guaranteed to exceed every id just copied forward.
        max_id = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM opportunity_observations"
        ).fetchone()[0]
        conn.execute(
            "UPDATE sqlite_sequence SET seq=? WHERE name='opportunity_observations' AND seq<?",
            (max_id, max_id),
        )
        conn.execute(
            """
            INSERT INTO sqlite_sequence (name, seq)
            SELECT 'opportunity_observations', ?
            WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name='opportunity_observations')
            """,
            (max_id,),
        )

        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_violations:
            raise RuntimeError(
                "opportunity_observations migration: foreign_key_check reported "
                f"{len(fk_violations)} violation(s); aborting"
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise


# Context and ranking v0.1 — the five nullable score/context columns added
# to opportunity_observations. Order matters: ALTER TABLE ADD COLUMN always
# appends at the end of the table in the order the ALTERs run, so a legacy
# database migrated by _migrate_opportunity_observations_setup_family first
# (see below) ends up with these five columns after `updated_at`, matching
# a fresh schema.sql install exactly.
_OPPORTUNITY_OBSERVATIONS_SCORE_COLUMNS: list[tuple[str, str]] = [
    ("context_json", "TEXT"),
    ("component_scores_json", "TEXT"),
    ("total_score", "REAL"),
    ("score_version", "TEXT"),
    ("score_warnings_json", "TEXT"),
]


def _migrate_opportunity_observations_score_columns(conn: sqlite3.Connection) -> None:
    """Idempotently add the five nullable Context and ranking v0.1 score
    columns to opportunity_observations.

    MUST run AFTER _migrate_opportunity_observations_setup_family (see
    init_db) — that migration requires the table's exact pre-widen
    24-column shape to decide whether a legacy two-family database needs
    rebuilding, and would fail closed (refuse to migrate) if these five
    columns already existed on such a database. Running in this order
    means: a legacy two-family database is rebuilt into the five-family
    shape first (still without these columns), and only then do these
    ALTERs run against it — identical in effect to an already-widened
    five-family database that predates this milestone, or a completely
    fresh install (whose schema.sql CREATE TABLE already declares all five
    columns, making every check below a same-type no-op).

    Column-type validation happens as a pure read (PRAGMA table_info) before
    any DDL runs, so a type mismatch on an already-present column raises
    without mutating anything. Once any ALTER is needed, all of them run
    inside one explicit transaction that rolls back atomically on any
    failure — never a partial set of added columns.
    """
    existing = conn.execute("PRAGMA table_info(opportunity_observations)").fetchall()
    existing_types = {row[1]: (row[2] or "").upper() for row in existing}

    missing: list[tuple[str, str]] = []
    for name, col_type in _OPPORTUNITY_OBSERVATIONS_SCORE_COLUMNS:
        if name in existing_types:
            if existing_types[name] != col_type:
                raise RuntimeError(
                    "opportunity_observations migration: score column "
                    f"{name!r} exists with unexpected type "
                    f"{existing_types[name]!r} (expected {col_type!r}); refusing to migrate"
                )
            continue
        missing.append((name, col_type))

    if not missing:
        return

    try:
        conn.execute("BEGIN")
        for name, col_type in missing:
            conn.execute(f"ALTER TABLE opportunity_observations ADD COLUMN {name} {col_type}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


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
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")

        schema_path = Path(__file__).parent / "schema.sql"
        with open(schema_path) as f:
            conn.executescript(f.read())
        conn.commit()

        _migrate_signal_observations(conn)
        _migrate_opportunity_observations_setup_family(conn)
        _migrate_opportunity_observations_score_columns(conn)
    except Exception:
        conn.close()
        raise

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
