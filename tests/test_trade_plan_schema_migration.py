"""Tests for the opportunity_trade_plans / opportunity_trade_plan_outcomes
schema additions (src/apex/db/schema.sql), applied via the UNCHANGED
init_db() (src/apex/db/connection.py) — these are plain `CREATE TABLE IF NOT
EXISTS` additions, so no dedicated migration function exists: init_db's
existing unconditional `executescript(schema.sql)` call is what creates them
on both a fresh database and an existing ("legacy", pre-this-milestone) one.

Covers: fresh install creates both tables empty; a simulated legacy database
(missing these two tables) gets them added on the next init_db() call while
every historical row in unrelated tables is preserved untouched; idempotent
re-init; constraint/FK enforcement.
"""
from __future__ import annotations

import sqlite3

import pytest

from apex.db.connection import close_db, init_db
from apex.db import repository as repo
from apex.db.models import Market
from apex.opportunity.contract import Opportunity, PRIMARY_TIMEFRAME_DURATION_MS
from apex.opportunity.trade_plan import OUTCOME_HORIZON_BARS, TRADE_PLAN_CONTRACT_VERSION, TradePlan
from apex.opportunity.trade_plan_outcome import (
    TradePlanOutcomeEvaluation,
    build_initial_trade_plan_outcome,
)

NEW_TABLES = ("opportunity_trade_plans", "opportunity_trade_plan_outcomes")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _make_trade_plan(opportunity_uid: str, plan_uid: str) -> TradePlan:
    not_before = 1_700_000_000_000
    duration = PRIMARY_TIMEFRAME_DURATION_MS["5m"]
    entry, stop = 110.0, 100.0
    risk = abs(entry - stop)
    return TradePlan(
        plan_uid=plan_uid,
        opportunity_uid=opportunity_uid,
        symbol="BTC",
        direction="LONG",
        setup_family="SWEEP_RECLAIM",
        primary_timeframe="5m",
        detector_version="sweep_reclaim_v0_1",
        opportunity_contract_version="opportunity_v0_1",
        plan_contract_version=TRADE_PLAN_CONTRACT_VERSION,
        source_candle_open_time=not_before - duration,
        source_candle_close_time=not_before,
        created_at="2026-09-15T00:00:00Z",
        availability="AVAILABLE",
        unavailable_reason=None,
        entry_type="RESTING_LIMIT_AT_SOURCE_CLOSE",
        entry_price=entry,
        invalidation_price=stop,
        stop_price=stop,
        risk_distance=risk,
        target_1r_price=entry + risk,
        target_2r_price=entry + 2.0 * risk,
        target_1r_multiple=1.0,
        target_2r_multiple=2.0,
        reward_risk_1r=1.0,
        reward_risk_2r=2.0,
        evaluation_not_before_ms=not_before,
        evaluation_expiry_ms=not_before + OUTCOME_HORIZON_BARS * duration,
        provenance_json="{}",
        warnings_json="[]",
    )


def _outcome_eval(
    plan_uid: str,
    opportunity_uid: str,
    *,
    state: str,
    is_terminal: bool,
    entry_open_time=None,
    hit_1r_open_time=None,
    last_evaluated_ms=1_700_000_300_000,
    last_evaluated_open_time=1_700_000_300_000,
    terminal_open_time=None,
    terminal_reason=None,
    mfe_r=None,
    mae_r=None,
) -> TradePlanOutcomeEvaluation:
    return TradePlanOutcomeEvaluation(
        plan_uid=plan_uid,
        opportunity_uid=opportunity_uid,
        contract_version="trade_plan_outcome_v0_1",
        state=state,
        is_terminal=is_terminal,
        last_evaluated_ms=last_evaluated_ms,
        last_evaluated_open_time=last_evaluated_open_time,
        entry_open_time=entry_open_time,
        hit_1r_open_time=hit_1r_open_time,
        terminal_open_time=terminal_open_time,
        terminal_reason=terminal_reason,
        mfe_r=mfe_r,
        mae_r=mae_r,
        data_quality="COMPLETE",
        first_missing_boundary_ms=None,
        is_ambiguous=False,
        decisive_ohlc=None,
        crossed_levels=(),
        evidence_json="{}",
    )


def _insert_opportunity(conn: sqlite3.Connection, uid: str) -> None:
    opp = Opportunity(
        opportunity_uid=uid,
        fingerprint=f"fp-{uid}",
        symbol="BTC",
        direction="LONG",
        setup_family="SWEEP_RECLAIM",
        detector_version="sweep_reclaim_v0_1",
        primary_timeframe="5m",
        first_detected_at="2026-09-15T00:00:00Z",
        last_seen_at="2026-09-15T00:00:00Z",
        source_candle_open_time=1_000,
        source_candle_close_time=1_300_000,
    )
    repo.insert_opportunity(conn, opp)


def test_fresh_install_creates_both_new_tables_empty(tmp_path):
    db_path = str(tmp_path / "fresh.db")
    conn = init_db(db_path)
    try:
        for table in NEW_TABLES:
            assert _table_exists(conn, table)
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count == 0
    finally:
        close_db()


def test_idempotent_reinit_does_not_raise_or_duplicate(tmp_path):
    db_path = str(tmp_path / "idempotent.db")
    conn = init_db(db_path)
    close_db()
    conn = init_db(db_path)  # second init against the same file
    try:
        for table in NEW_TABLES:
            assert _table_exists(conn, table)
    finally:
        close_db()


def test_legacy_database_gains_new_tables_preserving_historical_rows(tmp_path):
    db_path = str(tmp_path / "legacy.db")

    # Step 1: a normal fresh install, then insert a historical opportunity
    # row and a market row (simulating pre-existing data).
    conn = init_db(db_path)
    repo.upsert_market(conn, Market(symbol="BTC", is_active=True, scan_enabled=True))
    _insert_opportunity(conn, "legacy-opportunity-uid")
    close_db()

    # Step 2: simulate a database that predates this milestone by manually
    # dropping the two new tables (direct DDL on a fresh connection, not
    # through init_db/schema.sql).
    raw = sqlite3.connect(db_path)
    try:
        for table in NEW_TABLES:
            raw.execute(f"DROP TABLE IF EXISTS {table}")
        raw.commit()
    finally:
        raw.close()

    raw_check = sqlite3.connect(db_path)
    try:
        for table in NEW_TABLES:
            assert not _table_exists(raw_check, table)
        # Historical data survives the manual drop untouched.
        row = raw_check.execute(
            "SELECT opportunity_uid FROM opportunity_observations"
        ).fetchone()
        assert row[0] == "legacy-opportunity-uid"
    finally:
        raw_check.close()

    # Step 3: re-run init_db (exactly what a legacy production DB would
    # experience on next startup) — the two tables must be (re)created
    # empty, and every historical row in unrelated tables preserved.
    conn = init_db(db_path)
    try:
        for table in NEW_TABLES:
            assert _table_exists(conn, table)
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count == 0

        row = conn.execute(
            "SELECT opportunity_uid FROM opportunity_observations"
        ).fetchone()
        assert row[0] == "legacy-opportunity-uid"
        market_row = conn.execute("SELECT symbol FROM markets").fetchone()
        assert market_row[0] == "BTC"
    finally:
        close_db()


def test_direction_check_constraint_enforced(tmp_path):
    db_path = str(tmp_path / "constraints.db")
    conn = init_db(db_path)
    try:
        _insert_opportunity(conn, "opp-for-constraint-test")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO opportunity_trade_plans (
                    plan_uid, opportunity_uid, symbol, direction, setup_family,
                    primary_timeframe, detector_version, opportunity_contract_version,
                    plan_contract_version, source_candle_open_time, source_candle_close_time,
                    created_at, availability, unavailable_reason, entry_type, entry_price,
                    invalidation_price, stop_price, risk_distance,
                    target_1r_price, target_2r_price, target_1r_multiple, target_2r_multiple,
                    reward_risk_1r, reward_risk_2r,
                    evaluation_not_before_ms, evaluation_expiry_ms,
                    provenance_json, warnings_json, research_only
                ) VALUES (
                    'plan-bad-direction', 'opp-for-constraint-test', 'BTC', 'SIDEWAYS', 'SWEEP_RECLAIM',
                    '5m', 'sweep_reclaim_v0_1', 'opportunity_v0_1', 'trade_plan_v0_1', 1000, 1300000,
                    '2026-09-15T00:00:00Z', 'UNAVAILABLE', 'NO_STRUCTURAL_INVALIDATION', NULL, NULL,
                    NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1300000, 8400000, '{}', '[]', 1
                )
                """
            )
    finally:
        close_db()


def test_availability_consistency_check_constraint_enforced(tmp_path):
    """AVAILABLE must carry non-NULL price levels; this row violates that."""
    db_path = str(tmp_path / "constraints2.db")
    conn = init_db(db_path)
    try:
        _insert_opportunity(conn, "opp-for-constraint-test-2")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO opportunity_trade_plans (
                    plan_uid, opportunity_uid, symbol, direction, setup_family,
                    primary_timeframe, detector_version, opportunity_contract_version,
                    plan_contract_version, source_candle_open_time, source_candle_close_time,
                    created_at, availability, unavailable_reason, entry_type, entry_price,
                    invalidation_price, stop_price, risk_distance,
                    target_1r_price, target_2r_price, target_1r_multiple, target_2r_multiple,
                    reward_risk_1r, reward_risk_2r,
                    evaluation_not_before_ms, evaluation_expiry_ms,
                    provenance_json, warnings_json, research_only
                ) VALUES (
                    'plan-bad-avail', 'opp-for-constraint-test-2', 'BTC', 'LONG', 'SWEEP_RECLAIM',
                    '5m', 'sweep_reclaim_v0_1', 'opportunity_v0_1', 'trade_plan_v0_1', 1000, 1300000,
                    '2026-09-15T00:00:00Z', 'AVAILABLE', NULL, NULL, NULL,
                    NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1300000, 8400000, '{}', '[]', 1
                )
                """
            )
    finally:
        close_db()


def test_opportunity_uid_foreign_key_enforced(tmp_path):
    db_path = str(tmp_path / "fk.db")
    conn = init_db(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO opportunity_trade_plans (
                    plan_uid, opportunity_uid, symbol, direction, setup_family,
                    primary_timeframe, detector_version, opportunity_contract_version,
                    plan_contract_version, source_candle_open_time, source_candle_close_time,
                    created_at, availability, unavailable_reason, entry_type, entry_price,
                    invalidation_price, stop_price, risk_distance,
                    target_1r_price, target_2r_price, target_1r_multiple, target_2r_multiple,
                    reward_risk_1r, reward_risk_2r,
                    evaluation_not_before_ms, evaluation_expiry_ms,
                    provenance_json, warnings_json, research_only
                ) VALUES (
                    'plan-fk-orphan', 'does-not-exist', 'BTC', 'LONG', 'SWEEP_RECLAIM',
                    '5m', 'sweep_reclaim_v0_1', 'opportunity_v0_1', 'trade_plan_v0_1', 1000, 1300000,
                    '2026-09-15T00:00:00Z', 'UNAVAILABLE', 'NO_STRUCTURAL_INVALIDATION', NULL, NULL,
                    NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1300000, 8400000, '{}', '[]', 1
                )
                """
            )
    finally:
        close_db()


def test_outcome_state_check_constraint_enforced(tmp_path):
    db_path = str(tmp_path / "outcome_constraint.db")
    conn = init_db(db_path)
    try:
        _insert_opportunity(conn, "opp-for-outcome-constraint")
        conn.execute(
            """
            INSERT INTO opportunity_trade_plans (
                plan_uid, opportunity_uid, symbol, direction, setup_family,
                primary_timeframe, detector_version, opportunity_contract_version,
                plan_contract_version, source_candle_open_time, source_candle_close_time,
                created_at, availability, unavailable_reason, entry_type, entry_price,
                invalidation_price, stop_price, risk_distance,
                target_1r_price, target_2r_price, target_1r_multiple, target_2r_multiple,
                reward_risk_1r, reward_risk_2r,
                evaluation_not_before_ms, evaluation_expiry_ms,
                provenance_json, warnings_json, research_only
            ) VALUES (
                'plan-for-outcome', 'opp-for-outcome-constraint', 'BTC', 'LONG', 'SWEEP_RECLAIM',
                '5m', 'sweep_reclaim_v0_1', 'opportunity_v0_1', 'trade_plan_v0_1', 1000, 1300000,
                '2026-09-15T00:00:00Z', 'UNAVAILABLE', 'NO_STRUCTURAL_INVALIDATION', NULL, NULL,
                NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1300000, 8400000, '{}', '[]', 1
            )
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO opportunity_trade_plan_outcomes (
                    plan_uid, opportunity_uid, contract_version, state, evidence_json
                ) VALUES ('plan-for-outcome', 'opp-for-outcome-constraint', 'trade_plan_outcome_v0_1',
                          'NOT_A_REAL_STATE', '{}')
                """
            )
    finally:
        close_db()


# ------------------------------------------------------------------ insert_trade_plan_with_outcome
# atomicity (src/apex/db/repository.py)


def test_insert_trade_plan_with_outcome_succeeds_for_valid_pair(tmp_path):
    db_path = str(tmp_path / "atomic_success.db")
    conn = init_db(db_path)
    try:
        _insert_opportunity(conn, "opp-atomic-success")
        plan = _make_trade_plan("opp-atomic-success", "plan-atomic-success")
        outcome = build_initial_trade_plan_outcome(plan)

        repo.insert_trade_plan_with_outcome(conn, plan, outcome)

        plan_row = conn.execute(
            "SELECT * FROM opportunity_trade_plans WHERE plan_uid=?", ("plan-atomic-success",)
        ).fetchone()
        outcome_row = conn.execute(
            "SELECT * FROM opportunity_trade_plan_outcomes WHERE plan_uid=?",
            ("plan-atomic-success",),
        ).fetchone()
        assert plan_row is not None
        assert outcome_row is not None
        assert outcome_row["state"] == "PENDING_ENTRY"
    finally:
        close_db()


def test_insert_trade_plan_with_outcome_rolls_back_atomically_on_outcome_insert_failure(tmp_path):
    """Forces the second (outcome) INSERT to fail, via a temp SQLite trigger,
    after the first (plan) INSERT has already succeeded within the same
    implicit transaction. Proves the failure is rolled back explicitly —
    not merely left uncommitted — by performing an unrelated write (with its
    own commit()) afterward on the same connection and confirming it does
    not resurrect the failed pair, then re-verifies via a fresh connection
    that nothing was ever persisted to disk.
    """
    db_path = str(tmp_path / "atomic_fail.db")
    conn = init_db(db_path)
    try:
        _insert_opportunity(conn, "opp-atomic-fail")
        plan = _make_trade_plan("opp-atomic-fail", "plan-atomic-fail")
        outcome = build_initial_trade_plan_outcome(plan)

        conn.execute(
            """
            CREATE TEMP TRIGGER trg_force_outcome_insert_failure
            BEFORE INSERT ON opportunity_trade_plan_outcomes
            BEGIN
                SELECT RAISE(ABORT, 'forced test failure: outcome insert');
            END
            """
        )

        with pytest.raises(sqlite3.IntegrityError, match="forced test failure"):
            repo.insert_trade_plan_with_outcome(conn, plan, outcome)

        # An unrelated write + its own commit() on the SAME connection must
        # not resurrect (i.e. accidentally commit) the rolled-back pair.
        repo.upsert_market(conn, Market(symbol="ETH", is_active=True, scan_enabled=True))

        assert (
            conn.execute(
                "SELECT COUNT(*) FROM opportunity_trade_plans WHERE plan_uid=?",
                ("plan-atomic-fail",),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM opportunity_trade_plan_outcomes WHERE plan_uid=?",
                ("plan-atomic-fail",),
            ).fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM markets WHERE symbol='ETH'").fetchone()[0] == 1
    finally:
        close_db()

    # Re-verify on disk via a completely fresh connection.
    raw = sqlite3.connect(db_path)
    try:
        assert (
            raw.execute(
                "SELECT COUNT(*) FROM opportunity_trade_plans WHERE plan_uid=?",
                ("plan-atomic-fail",),
            ).fetchone()[0]
            == 0
        )
        assert (
            raw.execute(
                "SELECT COUNT(*) FROM opportunity_trade_plan_outcomes WHERE plan_uid=?",
                ("plan-atomic-fail",),
            ).fetchone()[0]
            == 0
        )
    finally:
        raw.close()


# ------------------------------------------------------------------ update_trade_plan_outcome
# terminal + monotonic guarantees (src/apex/db/repository.py)


def test_update_trade_plan_outcome_terminal_guard_second_update_is_noop(tmp_path):
    db_path = str(tmp_path / "terminal_guard.db")
    conn = init_db(db_path)
    try:
        _insert_opportunity(conn, "opp-terminal")
        plan = _make_trade_plan("opp-terminal", "plan-terminal")
        repo.insert_trade_plan_with_outcome(conn, plan, build_initial_trade_plan_outcome(plan))

        stopped = _outcome_eval(
            plan.plan_uid,
            plan.opportunity_uid,
            state="STOPPED",
            is_terminal=True,
            entry_open_time=1_700_000_300_000,
            terminal_open_time=1_700_000_600_000,
            terminal_reason="test",
            mfe_r=0.2,
            mae_r=1.0,
        )
        assert repo.update_trade_plan_outcome(conn, stopped) is True

        row_after_first = dict(
            conn.execute(
                "SELECT * FROM opportunity_trade_plan_outcomes WHERE plan_uid=?", (plan.plan_uid,)
            ).fetchone()
        )

        # A later pass tries to resurrect the already-terminal row.
        resurrection_attempt = _outcome_eval(
            plan.plan_uid,
            plan.opportunity_uid,
            state="HIT_2R",
            is_terminal=True,
            entry_open_time=1_700_000_300_000,
            hit_1r_open_time=1_700_000_400_000,
            terminal_open_time=1_700_000_900_000,
            terminal_reason="different",
            mfe_r=2.0,
            mae_r=0.0,
        )
        assert repo.update_trade_plan_outcome(conn, resurrection_attempt) is False

        row_after_second = dict(
            conn.execute(
                "SELECT * FROM opportunity_trade_plan_outcomes WHERE plan_uid=?", (plan.plan_uid,)
            ).fetchone()
        )
        assert row_after_second == row_after_first
        assert row_after_second["state"] == "STOPPED"
    finally:
        close_db()


def test_update_trade_plan_outcome_preserves_earlier_entry_and_hit_1r_milestones(tmp_path):
    db_path = str(tmp_path / "monotonic.db")
    conn = init_db(db_path)
    try:
        _insert_opportunity(conn, "opp-monotonic")
        plan = _make_trade_plan("opp-monotonic", "plan-monotonic")
        repo.insert_trade_plan_with_outcome(conn, plan, build_initial_trade_plan_outcome(plan))

        first_pass = _outcome_eval(
            plan.plan_uid,
            plan.opportunity_uid,
            state="HIT_1R",
            is_terminal=False,
            entry_open_time=1_700_000_300_000,
            hit_1r_open_time=1_700_000_400_000,
        )
        assert repo.update_trade_plan_outcome(conn, first_pass) is True

        # A later, still-nonterminal pass supplies different/absent milestone
        # timestamps — the already-recorded ones must never be overwritten.
        second_pass = _outcome_eval(
            plan.plan_uid,
            plan.opportunity_uid,
            state="ENTERED",
            is_terminal=False,
            entry_open_time=9_999_999_999_999,
            hit_1r_open_time=None,
        )
        assert repo.update_trade_plan_outcome(conn, second_pass) is True

        row = conn.execute(
            "SELECT entry_open_time, hit_1r_open_time, state "
            "FROM opportunity_trade_plan_outcomes WHERE plan_uid=?",
            (plan.plan_uid,),
        ).fetchone()
        assert row["entry_open_time"] == 1_700_000_300_000
        assert row["hit_1r_open_time"] == 1_700_000_400_000
        assert row["state"] == "ENTERED"
    finally:
        close_db()


def test_update_trade_plan_outcome_touches_only_its_own_row(tmp_path):
    db_path = str(tmp_path / "no_unrelated_changes.db")
    conn = init_db(db_path)
    try:
        _insert_opportunity(conn, "opp-scoped")
        plan = _make_trade_plan("opp-scoped", "plan-scoped")
        repo.insert_trade_plan_with_outcome(conn, plan, build_initial_trade_plan_outcome(plan))
        repo.upsert_market(conn, Market(symbol="ETH", is_active=True, scan_enabled=True))

        opp_before = dict(
            conn.execute(
                "SELECT * FROM opportunity_observations WHERE opportunity_uid=?",
                (plan.opportunity_uid,),
            ).fetchone()
        )
        market_before = dict(
            conn.execute("SELECT * FROM markets WHERE symbol='ETH'").fetchone()
        )
        plan_row_before = dict(
            conn.execute(
                "SELECT * FROM opportunity_trade_plans WHERE plan_uid=?", (plan.plan_uid,)
            ).fetchone()
        )

        evaluation = _outcome_eval(
            plan.plan_uid,
            plan.opportunity_uid,
            state="ENTERED",
            is_terminal=False,
            entry_open_time=1_700_000_300_000,
        )
        assert repo.update_trade_plan_outcome(conn, evaluation) is True

        opp_after = dict(
            conn.execute(
                "SELECT * FROM opportunity_observations WHERE opportunity_uid=?",
                (plan.opportunity_uid,),
            ).fetchone()
        )
        market_after = dict(conn.execute("SELECT * FROM markets WHERE symbol='ETH'").fetchone())
        plan_row_after = dict(
            conn.execute(
                "SELECT * FROM opportunity_trade_plans WHERE plan_uid=?", (plan.plan_uid,)
            ).fetchone()
        )

        assert opp_after == opp_before
        assert market_after == market_before
        assert plan_row_after == plan_row_before  # immutable plan row untouched

        outcome_row = conn.execute(
            "SELECT state FROM opportunity_trade_plan_outcomes WHERE plan_uid=?", (plan.plan_uid,)
        ).fetchone()
        assert outcome_row["state"] == "ENTERED"  # the actual write did happen
    finally:
        close_db()


def test_update_trade_plan_outcome_mfe_mae_never_decrease_or_become_null(tmp_path):
    db_path = str(tmp_path / "mfe_mae_monotonic.db")
    conn = init_db(db_path)
    try:
        _insert_opportunity(conn, "opp-mfe-mae")
        plan = _make_trade_plan("opp-mfe-mae", "plan-mfe-mae")
        repo.insert_trade_plan_with_outcome(conn, plan, build_initial_trade_plan_outcome(plan))

        first_pass = _outcome_eval(
            plan.plan_uid,
            plan.opportunity_uid,
            state="HIT_1R",
            is_terminal=False,
            entry_open_time=1_700_000_300_000,
            hit_1r_open_time=1_700_000_400_000,
            last_evaluated_ms=1_700_000_400_000,
            last_evaluated_open_time=1_700_000_400_000,
            mfe_r=0.5,
            mae_r=0.3,
        )
        assert repo.update_trade_plan_outcome(conn, first_pass) is True

        row_after_first = conn.execute(
            "SELECT mfe_r, mae_r FROM opportunity_trade_plan_outcomes WHERE plan_uid=?",
            (plan.plan_uid,),
        ).fetchone()
        assert row_after_first["mfe_r"] == pytest.approx(0.5)
        assert row_after_first["mae_r"] == pytest.approx(0.3)

        # A later recoverable pass replays against a shorter/gapped frame:
        # reports a LOWER mfe_r and a NULL mae_r. Those two fields must be
        # preserved at their prior values, while other intended nonterminal
        # fields (state, last_evaluated_ms) still update normally.
        second_pass = _outcome_eval(
            plan.plan_uid,
            plan.opportunity_uid,
            state="ENTERED",
            is_terminal=False,
            entry_open_time=1_700_000_300_000,
            hit_1r_open_time=1_700_000_400_000,
            last_evaluated_ms=1_700_000_500_000,
            last_evaluated_open_time=1_700_000_500_000,
            mfe_r=0.2,
            mae_r=None,
        )
        assert repo.update_trade_plan_outcome(conn, second_pass) is True

        row_after_second = conn.execute(
            "SELECT state, last_evaluated_ms, mfe_r, mae_r "
            "FROM opportunity_trade_plan_outcomes WHERE plan_uid=?",
            (plan.plan_uid,),
        ).fetchone()
        assert row_after_second["mfe_r"] == pytest.approx(0.5)
        assert row_after_second["mae_r"] == pytest.approx(0.3)
        assert row_after_second["state"] == "ENTERED"
        assert row_after_second["last_evaluated_ms"] == 1_700_000_500_000

        # A genuinely HIGHER value still updates normally.
        third_pass = _outcome_eval(
            plan.plan_uid,
            plan.opportunity_uid,
            state="HIT_1R",
            is_terminal=False,
            entry_open_time=1_700_000_300_000,
            hit_1r_open_time=1_700_000_400_000,
            last_evaluated_ms=1_700_000_600_000,
            last_evaluated_open_time=1_700_000_600_000,
            mfe_r=0.9,
            mae_r=0.7,
        )
        assert repo.update_trade_plan_outcome(conn, third_pass) is True

        row_after_third = conn.execute(
            "SELECT mfe_r, mae_r FROM opportunity_trade_plan_outcomes WHERE plan_uid=?",
            (plan.plan_uid,),
        ).fetchone()
        assert row_after_third["mfe_r"] == pytest.approx(0.9)
        assert row_after_third["mae_r"] == pytest.approx(0.7)
    finally:
        close_db()
