"""Tests for src/apex/opportunity/shadow_runtime.py — the default-off,
fail-closed runtime adapter wiring the pure shadow_alerts evaluator (and
quality_cohorts) to live APEX state. Every test uses a disposable tmp_path
SQLite database seeded directly via the repository layer plus a real
CandleStore; never the production database, network, or a real clock.
"""
from __future__ import annotations

import ast
import inspect
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta

import pytest

from apex.config import Settings
from apex.data.candle_store import CandleStore
from apex.db import repository as repo
from apex.db.connection import close_db, init_db
from apex.opportunity.contract import CONTRACT_VERSION, Opportunity
from apex.opportunity.quality_uncertainty import MIN_RESOLVED_CLUSTERS
from apex.opportunity.scoring import SCORE_VERSION
from apex.opportunity.shadow_alerts import (
    MAX_MARKET_SNAPSHOT_AGE_SECONDS,
    REASON_COOLDOWN_ACTIVE,
    REASON_EMIT_ELIGIBLE,
    REASON_MARKET_SNAPSHOT_MISSING,
    REASON_MARKET_SNAPSHOT_NOT_CURRENT,
    REASON_OPPORTUNITY_ALREADY_EMITTED,
    REASON_OVERLAPS_OPEN_CLUSTER,
    REASON_PRIOR_STATE_INVALID,
    REASON_ROLLING_DAY_CAP_REACHED,
    REASON_ROLLING_HOUR_CAP_REACHED,
)
from apex.opportunity import shadow_runtime
from apex.opportunity.shadow_runtime import (
    MULTI_TIMEFRAME_CONFLICT_EVIDENCE_KEY,
    PILOT_STATE_ACTIVE,
    PILOT_STATE_DISABLED,
    PILOT_STATE_EXPIRED,
    PILOT_STATE_GUARD_NOT_MET,
    PILOT_STATE_INVALID_NOW,
    PILOT_STATE_INVALID_SETTINGS,
    PILOT_STATE_NOT_STARTED,
    RUN_REASON_CANDIDATE_LIMIT_OVERFLOW,
    RUN_REASON_CANDIDATE_QUERY_FAILED,
    RUN_REASON_OK,
    RUN_REASON_PRIOR_LIMIT_OVERFLOW,
    RUN_REASON_PRIOR_QUERY_FAILED,
    RUN_REASON_QUALITY_LIMIT_OVERFLOW,
    RUN_REASON_QUALITY_QUERY_FAILED,
    RUN_REASON_QUALITY_REPORT_BUILD_FAILED,
    RUN_REASON_WRITE_FAILED,
    MAX_CANDIDATES_PER_RUN,
    PilotConfig,
    resolve_pilot_state,
    run_shadow_alert_runtime,
)
from apex.opportunity.trade_plan import TRADE_PLAN_CONTRACT_VERSION, TradePlan
from apex.opportunity.trade_plan_outcome import (
    TRADE_PLAN_OUTCOME_CONTRACT_VERSION,
    TradePlanOutcomeEvaluation,
)

FIXED_NOW = datetime(2026, 6, 15, 15, 0, 0, tzinfo=UTC)  # 11:00 EDT — non-quiet Detroit hours
FIXED_NOW_MS = int(FIXED_NOW.timestamp() * 1000)

PILOT_ID = "pilot-001"
PILOT_START = "2026-06-15T00:00:00Z"
PILOT_DEADLINE = "2026-06-16T00:00:00Z"  # exactly 24h after start


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


def _settings(**overrides) -> Settings:
    base = dict(
        shadow_alert_pilot_enabled=True,
        opportunity_engine_enabled=True,
        trade_plan_evidence_enabled=True,
        dry_run_mode=True,
        alerts_enabled=False,
        shadow_alert_pilot_id=PILOT_ID,
        shadow_alert_pilot_start_at=PILOT_START,
        shadow_alert_pilot_deadline_at=PILOT_DEADLINE,
        shadow_alert_pilot_fee_r=0.02,
        shadow_alert_pilot_slippage_r=0.01,
    )
    base.update(overrides)
    return Settings(**base)


def _seed_opportunity(
    conn, *, uid, symbol="BTC", direction="LONG", setup_family="SWEEP_RECLAIM",
    timeframe="5m", status="ACTIVE", last_seen_at, score_version=None,
) -> None:
    repo.insert_opportunity(
        conn,
        Opportunity(
            opportunity_uid=uid,
            fingerprint=f"fp-{uid}",
            symbol=symbol,
            direction=direction,
            setup_family=setup_family,
            detector_version="sweep_reclaim_v0_1",
            primary_timeframe=timeframe,
            first_detected_at=last_seen_at,
            last_seen_at=last_seen_at,
            source_candle_open_time=1000,
            source_candle_close_time=1300,
            status=status,
            research_only=True,
            evidence_json="{}",
            warnings_json="[]",
            measurements_json="{}",
            score_version=score_version,
        ),
    )


def _seed_plan_and_outcome(
    conn, *, uid, symbol="BTC", direction="LONG", setup_family="SWEEP_RECLAIM",
    timeframe="5m", entry=100.0, stop=90.0, t1=110.0, t2=120.0,
    not_before_ms, expiry_ms, outcome_state="PENDING_ENTRY",
    data_quality="COMPLETE", is_ambiguous=False, plan_contract_version=None,
    outcome_contract_version=None,
) -> None:
    plan = TradePlan(
        plan_uid=f"plan-{uid}",
        opportunity_uid=uid,
        symbol=symbol,
        direction=direction,
        setup_family=setup_family,
        primary_timeframe=timeframe,
        detector_version="sweep_reclaim_v0_1",
        opportunity_contract_version=CONTRACT_VERSION,
        plan_contract_version=plan_contract_version or TRADE_PLAN_CONTRACT_VERSION,
        source_candle_open_time=1000,
        source_candle_close_time=1300,
        created_at="2026-06-15T00:00:00Z",
        availability="AVAILABLE",
        unavailable_reason=None,
        entry_type="RESTING_LIMIT_AT_SOURCE_CLOSE",
        entry_price=entry,
        invalidation_price=stop,
        stop_price=stop,
        risk_distance=abs(entry - stop),
        target_1r_price=t1,
        target_2r_price=t2,
        target_1r_multiple=1.0,
        target_2r_multiple=2.0,
        reward_risk_1r=1.0,
        reward_risk_2r=2.0,
        evaluation_not_before_ms=not_before_ms,
        evaluation_expiry_ms=expiry_ms,
        provenance_json="{}",
        warnings_json="[]",
    )
    is_terminal = outcome_state in (
        "NOT_EVALUABLE", "HIT_2R", "STOPPED", "EXPIRED_UNENTERED", "EXPIRED_OPEN", "AMBIGUOUS",
    )
    outcome = TradePlanOutcomeEvaluation(
        plan_uid=plan.plan_uid,
        opportunity_uid=uid,
        contract_version=outcome_contract_version or TRADE_PLAN_OUTCOME_CONTRACT_VERSION,
        state=outcome_state,
        is_terminal=is_terminal,
        last_evaluated_ms=expiry_ms,
        last_evaluated_open_time=expiry_ms,
        entry_open_time=not_before_ms if outcome_state != "PENDING_ENTRY" else None,
        hit_1r_open_time=None,
        terminal_open_time=expiry_ms if outcome_state == "HIT_2R" else None,
        terminal_reason="HORIZON_REACHED" if outcome_state == "HIT_2R" else None,
        mfe_r=2.0 if outcome_state == "HIT_2R" else None,
        mae_r=0.0 if outcome_state == "HIT_2R" else None,
        data_quality=data_quality,
        first_missing_boundary_ms=None,
        is_ambiguous=is_ambiguous,
        decisive_ohlc=None,
        crossed_levels=(),
        evidence_json="{}",
    )
    repo.insert_trade_plan_with_outcome(conn, plan, outcome)


def _seed_live_candidate(
    conn, *, uid, symbol="BTC", direction="LONG", setup_family="SWEEP_RECLAIM",
    timeframe="5m", entry=100.0, stop=90.0, t1=110.0, t2=120.0,
    last_seen_at=None, not_before_ms=None, expiry_ms=None,
) -> None:
    last_seen_at = last_seen_at or (FIXED_NOW - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    not_before_ms = FIXED_NOW_MS - 2_000_000 if not_before_ms is None else not_before_ms
    expiry_ms = FIXED_NOW_MS + 1_000_000 if expiry_ms is None else expiry_ms
    _seed_opportunity(
        conn, uid=uid, symbol=symbol, direction=direction, setup_family=setup_family,
        timeframe=timeframe, status="ACTIVE", last_seen_at=last_seen_at, score_version=SCORE_VERSION,
    )
    _seed_plan_and_outcome(
        conn, uid=uid, symbol=symbol, direction=direction, setup_family=setup_family,
        timeframe=timeframe, entry=entry, stop=stop, t1=t1, t2=t2,
        not_before_ms=not_before_ms, expiry_ms=expiry_ms, outcome_state="PENDING_ENTRY",
    )


def _seed_qualifying_cohort(
    conn, *, symbol="BTC", direction="LONG", setup_family="SWEEP_RECLAIM", timeframe="5m",
    count=MIN_RESOLVED_CLUSTERS,
) -> None:
    """Seeds `count` widely-spaced (distinct-cluster) clear-resolved HIT_2R
    rows for one exact cohort — enough to pass quality_uncertainty's fixed
    qualification floor (MIN_RESOLVED_CLUSTERS, ambiguity <= 10%, positive
    net expectancy/lower bound) regardless of the caller's cost assumption,
    since every row's gross R is a uniform +2R.
    """
    for i in range(count):
        uid = f"hist-{symbol}-{direction}-{setup_family}-{timeframe}-{i}"
        nb = 10_000_000 + i * 10_000_000
        exp = nb + 1_000_000
        _seed_opportunity(
            conn, uid=uid, symbol=symbol, direction=direction, setup_family=setup_family,
            timeframe=timeframe, status="EXPIRED", last_seen_at="2026-01-01T00:00:00Z",
        )
        _seed_plan_and_outcome(
            conn, uid=uid, symbol=symbol, direction=direction, setup_family=setup_family,
            timeframe=timeframe, not_before_ms=nb, expiry_ms=exp, outcome_state="HIT_2R",
        )


def _seed_ws_snapshot(
    store: CandleStore, *, symbol="BTC", timeframe="5m", now_ms, open_=105.0, high=108.0,
    low=101.0, close=105.0,
) -> None:
    store.update(
        symbol, timeframe,
        {"t": now_ms - 60_000, "T": now_ms - 1, "o": open_, "h": high, "l": low, "c": close, "v": 10},
        persist=False, now_ms=now_ms, source="ws",
    )


def _db(tmp_path, name="shadow.db"):
    return init_db(str(tmp_path / name))


def _insert_prior_decision(
    conn, *, pilot_id=PILOT_ID, run_at, opportunity_uid, symbol="BTC", direction="LONG",
    setup_family="SWEEP_RECLAIM", timeframe="5m", would_emit=True,
    not_before_ms=FIXED_NOW_MS - 5_000_000, expiry_ms=FIXED_NOW_MS - 4_000_000,
) -> None:
    """Directly inserts one shadow_alert_decisions row, bypassing
    run_shadow_alert_runtime, to simulate evidence written by an earlier
    (possibly now-restarted) process — see crash/restart reconstruction
    tests below.
    """
    record = repo.ShadowDecisionRecord(
        pilot_id=pilot_id,
        evaluator_version="shadow_alerts_v0_1",
        runtime_version="shadow_alert_runtime_v0_1",
        run_at=run_at,
        opportunity_uid=opportunity_uid,
        symbol=symbol,
        direction=direction,
        setup_family=setup_family,
        primary_timeframe=timeframe,
        evaluation_not_before_ms=not_before_ms,
        evaluation_expiry_ms=expiry_ms,
        entry_price=100.0,
        stop_price=90.0,
        target_1r_price=110.0,
        target_2r_price=120.0,
        market_snapshot_json=None,
        cost_fee_r=0.02,
        cost_slippage_r=0.01,
        cost_total_r=0.03,
        cohort_evidence_json=None,
        would_emit=would_emit,
        reason_codes_json=json.dumps([REASON_EMIT_ELIGIBLE] if would_emit else ["SOME_REASON"]),
        message="msg" if would_emit else None,
        rule_evidence_json="{}",
    )
    repo.insert_shadow_decisions(conn, [record])


def _run_at(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# Pilot-state gating (resolve_pilot_state) — default-off, every guard, and
# start/deadline/malformed/cost validation.
# ---------------------------------------------------------------------------


class TestResolvePilotState:
    def test_disabled_by_default(self):
        settings = _settings(shadow_alert_pilot_enabled=False)
        state, config = resolve_pilot_state(settings, now=FIXED_NOW)
        assert state == PILOT_STATE_DISABLED
        assert config is None

    @pytest.mark.parametrize(
        "field,value",
        [
            ("opportunity_engine_enabled", False),
            ("trade_plan_evidence_enabled", False),
            ("dry_run_mode", False),
            ("alerts_enabled", True),
        ],
    )
    def test_each_scheduler_guard_independently_fails_closed(self, field, value):
        settings = _settings(**{field: value})
        state, config = resolve_pilot_state(settings, now=FIXED_NOW)
        assert state == PILOT_STATE_GUARD_NOT_MET
        assert config is None

    def test_invalid_now_naive_datetime_fails_closed(self):
        settings = _settings()
        state, config = resolve_pilot_state(settings, now=datetime(2026, 6, 15, 15, 0, 0))
        assert state == PILOT_STATE_INVALID_NOW
        assert config is None

    def test_invalid_now_non_utc_offset_fails_closed(self):
        from datetime import timezone
        settings = _settings()
        est = timezone(timedelta(hours=-5))
        state, config = resolve_pilot_state(settings, now=datetime(2026, 6, 15, 10, 0, 0, tzinfo=est))
        assert state == PILOT_STATE_INVALID_NOW

    def test_missing_pilot_id_fails_closed(self):
        settings = _settings(shadow_alert_pilot_id="")
        state, _ = resolve_pilot_state(settings, now=FIXED_NOW)
        assert state == PILOT_STATE_INVALID_SETTINGS

    @pytest.mark.parametrize(
        "start,deadline",
        [
            ("not-a-date", PILOT_DEADLINE),
            (PILOT_START, "not-a-date"),
            ("2026-06-15 00:00:00", PILOT_DEADLINE),  # missing T/Z
            ("", PILOT_DEADLINE),
            (PILOT_START, ""),
        ],
    )
    def test_malformed_start_or_deadline_fails_closed(self, start, deadline):
        settings = _settings(shadow_alert_pilot_start_at=start, shadow_alert_pilot_deadline_at=deadline)
        state, _ = resolve_pilot_state(settings, now=FIXED_NOW)
        assert state == PILOT_STATE_INVALID_SETTINGS

    def test_reversed_start_deadline_fails_closed(self):
        settings = _settings(
            shadow_alert_pilot_start_at="2026-06-16T00:00:00Z",
            shadow_alert_pilot_deadline_at="2026-06-15T00:00:00Z",
        )
        state, _ = resolve_pilot_state(settings, now=FIXED_NOW)
        assert state == PILOT_STATE_INVALID_SETTINGS

    def test_equal_start_deadline_fails_closed(self):
        settings = _settings(
            shadow_alert_pilot_start_at=PILOT_START, shadow_alert_pilot_deadline_at=PILOT_START,
        )
        state, _ = resolve_pilot_state(settings, now=FIXED_NOW)
        assert state == PILOT_STATE_INVALID_SETTINGS

    def test_more_than_24_hours_apart_fails_closed(self):
        settings = _settings(
            shadow_alert_pilot_start_at="2026-06-15T00:00:00Z",
            shadow_alert_pilot_deadline_at="2026-06-16T00:00:01Z",
        )
        state, _ = resolve_pilot_state(settings, now=FIXED_NOW)
        assert state == PILOT_STATE_INVALID_SETTINGS

    @pytest.mark.parametrize("field", ["shadow_alert_pilot_fee_r", "shadow_alert_pilot_slippage_r"])
    def test_negative_cost_fails_closed(self, field):
        settings = _settings(**{field: -0.01})
        state, _ = resolve_pilot_state(settings, now=FIXED_NOW)
        assert state == PILOT_STATE_INVALID_SETTINGS

    @pytest.mark.parametrize("field", ["shadow_alert_pilot_fee_r", "shadow_alert_pilot_slippage_r"])
    def test_nonfinite_cost_fails_closed(self, field):
        settings = _settings(**{field: float("inf")})
        state, _ = resolve_pilot_state(settings, now=FIXED_NOW)
        assert state == PILOT_STATE_INVALID_SETTINGS

    def test_not_started_before_start(self):
        settings = _settings()
        state, config = resolve_pilot_state(settings, now=datetime(2026, 6, 14, 23, 59, 59, tzinfo=UTC))
        assert state == PILOT_STATE_NOT_STARTED
        assert isinstance(config, PilotConfig)

    def test_expired_at_deadline_exactly(self):
        settings = _settings()
        deadline = datetime(2026, 6, 16, 0, 0, 0, tzinfo=UTC)
        state, config = resolve_pilot_state(settings, now=deadline)
        assert state == PILOT_STATE_EXPIRED
        assert isinstance(config, PilotConfig)

    def test_expired_well_after_deadline_is_permanent_no_op(self):
        settings = _settings()
        far_future = datetime(2030, 1, 1, tzinfo=UTC)
        state, _ = resolve_pilot_state(settings, now=far_future)
        assert state == PILOT_STATE_EXPIRED

    def test_active_within_window(self):
        settings = _settings()
        state, config = resolve_pilot_state(settings, now=FIXED_NOW)
        assert state == PILOT_STATE_ACTIVE
        assert config.pilot_id == PILOT_ID
        assert config.total_cost_r == pytest.approx(0.03)

    def test_never_mutates_settings(self):
        settings = _settings()
        before = settings.model_dump()
        resolve_pilot_state(settings, now=FIXED_NOW)
        after = settings.model_dump()
        assert before == after


# ---------------------------------------------------------------------------
# run_shadow_alert_runtime's own defensive guard re-check (defense in depth)
# ---------------------------------------------------------------------------


class TestRuntimeGuardReCheck:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"shadow_alert_pilot_enabled": False},
            {"opportunity_engine_enabled": False},
            {"trade_plan_evidence_enabled": False},
            {"dry_run_mode": False},
            {"alerts_enabled": True},
        ],
    )
    async def test_guard_false_never_touches_the_database(self, tmp_path, overrides):
        conn = _db(tmp_path)
        try:
            settings = _settings(**overrides)

            class _ExplodingCandleStore:
                def get_latest_live_snapshot(self, *a, **kw):
                    raise AssertionError("must never be called when a guard is false")

            result = await run_shadow_alert_runtime(conn, _ExplodingCandleStore(), settings, now=FIXED_NOW)
            assert result.ran is False
            assert conn.execute("SELECT COUNT(*) FROM shadow_alert_decisions").fetchone()[0] == 0
        finally:
            close_db()


# ---------------------------------------------------------------------------
# Live WS snapshot: freshness/provenance, copy safety, no fallback.
# ---------------------------------------------------------------------------


class TestLiveWsSnapshot:
    def test_no_snapshot_before_any_ws_update(self, tmp_path):
        conn = _db(tmp_path)
        try:
            store = CandleStore(conn)
            assert store.get_latest_live_snapshot("BTC", "5m") is None
        finally:
            close_db()

    def test_ws_update_populates_snapshot_with_injected_now_ms(self, tmp_path):
        conn = _db(tmp_path)
        try:
            store = CandleStore(conn)
            _seed_ws_snapshot(store, now_ms=FIXED_NOW_MS)
            snap = store.get_latest_live_snapshot("BTC", "5m")
            assert snap is not None
            assert snap.received_at_ms == FIXED_NOW_MS
            assert snap.open == 105.0
            assert snap.high == 108.0
            assert snap.low == 101.0
            assert snap.close == 105.0
        finally:
            close_db()

    @pytest.mark.parametrize("source", ["backfill", "preload", "reconciliation", "unknown"])
    def test_non_ws_sources_never_populate_live_snapshot(self, tmp_path, source):
        conn = _db(tmp_path)
        try:
            store = CandleStore(conn)
            store.update(
                "BTC", "5m",
                {"t": FIXED_NOW_MS - 60_000, "T": FIXED_NOW_MS - 1, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 1},
                persist=False, now_ms=FIXED_NOW_MS, source=source,
            )
            assert store.get_latest_live_snapshot("BTC", "5m") is None
        finally:
            close_db()

    def test_ignored_late_partial_never_updates_snapshot(self, tmp_path):
        """A closed bar followed by a late/out-of-order forming re-delivery
        for the same open_time is rejected by the finalized-sample-
        protection guard in CandleStore.update — the live snapshot must not
        be updated by a rejected write.
        """
        conn = _db(tmp_path)
        try:
            store = CandleStore(conn)
            open_time = FIXED_NOW_MS - 300_000
            closed_candle = {"t": open_time, "T": open_time + 299_999, "o": 1, "h": 2, "l": 0.5, "c": 1.9, "v": 1}
            store.update("BTC", "5m", closed_candle, persist=False, now_ms=open_time + 300_000, source="ws")
            first_snap = store.get_latest_live_snapshot("BTC", "5m")
            assert first_snap is not None and first_snap.close == 1.9

            late_partial = {
                "t": open_time, "T": open_time + 299_999, "o": 1, "h": 2, "l": 0.5, "c": 1.1, "v": 1,
                "closed": False,  # explicit not-closed flag forces is_closed=False regardless of time
            }
            store.update("BTC", "5m", late_partial, persist=False, now_ms=open_time + 301_000, source="ws")
            snap_after = store.get_latest_live_snapshot("BTC", "5m")
            assert snap_after.close == 1.9  # unchanged — the late partial was rejected

        finally:
            close_db()

    def test_snapshot_is_copy_safe_across_later_updates(self, tmp_path):
        conn = _db(tmp_path)
        try:
            store = CandleStore(conn)
            _seed_ws_snapshot(store, now_ms=FIXED_NOW_MS, close=100.0)
            held = store.get_latest_live_snapshot("BTC", "5m")
            assert held.close == 100.0

            _seed_ws_snapshot(store, now_ms=FIXED_NOW_MS + 60_000, close=999.0)
            # The object held earlier must be unaffected by the later update.
            assert held.close == 100.0
            new_snap = store.get_latest_live_snapshot("BTC", "5m")
            assert new_snap.close == 999.0
        finally:
            close_db()

    async def test_missing_snapshot_suppresses_candidate_no_fabrication(self, tmp_path):
        conn = _db(tmp_path)
        try:
            _seed_qualifying_cohort(conn)
            _seed_live_candidate(conn, uid="cand-1")
            store = CandleStore(conn)  # never updated — no live snapshot at all
            settings = _settings()

            result = await run_shadow_alert_runtime(conn, store, settings, now=FIXED_NOW)
            assert result.ran is True
            row = conn.execute(
                "SELECT * FROM shadow_alert_decisions WHERE opportunity_uid='cand-1'"
            ).fetchone()
            assert row["would_emit"] == 0
            assert REASON_MARKET_SNAPSHOT_MISSING in json.loads(row["reason_codes_json"])
            assert row["message"] is None
            assert row["market_snapshot_json"] is None
        finally:
            close_db()

    async def test_stale_snapshot_suppresses_candidate(self, tmp_path):
        conn = _db(tmp_path)
        try:
            _seed_qualifying_cohort(conn)
            _seed_live_candidate(conn, uid="cand-1")
            store = CandleStore(conn)
            stale_ms = FIXED_NOW_MS - int((MAX_MARKET_SNAPSHOT_AGE_SECONDS + 5) * 1000)
            _seed_ws_snapshot(store, now_ms=stale_ms)
            settings = _settings()

            result = await run_shadow_alert_runtime(conn, store, settings, now=FIXED_NOW)
            assert result.ran is True
            row = conn.execute(
                "SELECT * FROM shadow_alert_decisions WHERE opportunity_uid='cand-1'"
            ).fetchone()
            assert row["would_emit"] == 0
            assert REASON_MARKET_SNAPSHOT_NOT_CURRENT in json.loads(row["reason_codes_json"])
        finally:
            close_db()

    async def test_same_symbol_multiple_timeframes_never_substitutes_snapshot(self, tmp_path):
        """A same-symbol batch spanning two distinct primary_timeframes (3m
        and 5m, deliberately different WS OHLC) must never let one
        timeframe's snapshot be reused for the other — both candidates are
        suppressed with REASON_MARKET_SNAPSHOT_MISSING and carry explicit
        runtime conflict evidence, while an unrelated symbol in the same
        batch remains independently evaluable.
        """
        conn = _db(tmp_path)
        try:
            _seed_qualifying_cohort(conn, symbol="BTC", timeframe="3m")
            _seed_qualifying_cohort(conn, symbol="BTC", timeframe="5m")
            _seed_qualifying_cohort(conn, symbol="ETH", timeframe="5m")
            _seed_live_candidate(conn, uid="cand-btc-3m", symbol="BTC", timeframe="3m")
            _seed_live_candidate(conn, uid="cand-btc-5m", symbol="BTC", timeframe="5m")
            _seed_live_candidate(
                conn, uid="cand-eth-5m", symbol="ETH", timeframe="5m",
                entry=200.0, stop=180.0, t1=220.0, t2=240.0,
            )
            store = CandleStore(conn)
            # Deliberately different OHLC per timeframe/symbol — proves
            # neither BTC candidate can receive the other's snapshot.
            _seed_ws_snapshot(
                store, symbol="BTC", timeframe="3m", now_ms=FIXED_NOW_MS - 5_000,
                open_=50.0, high=55.0, low=48.0, close=52.0,
            )
            _seed_ws_snapshot(
                store, symbol="BTC", timeframe="5m", now_ms=FIXED_NOW_MS - 5_000,
                open_=105.0, high=108.0, low=101.0, close=105.0,
            )
            _seed_ws_snapshot(
                store, symbol="ETH", timeframe="5m", now_ms=FIXED_NOW_MS - 5_000,
                open_=205.0, high=210.0, low=201.0, close=205.0,
            )
            settings = _settings()

            result = await run_shadow_alert_runtime(conn, store, settings, now=FIXED_NOW)
            assert result.ran is True

            for uid in ("cand-btc-3m", "cand-btc-5m"):
                row = conn.execute(
                    "SELECT * FROM shadow_alert_decisions WHERE opportunity_uid=?", (uid,)
                ).fetchone()
                assert row["would_emit"] == 0
                assert REASON_MARKET_SNAPSHOT_MISSING in json.loads(row["reason_codes_json"])
                assert row["market_snapshot_json"] is None
                evidence = json.loads(row["rule_evidence_json"])
                assert evidence[MULTI_TIMEFRAME_CONFLICT_EVIDENCE_KEY] == ["3m", "5m"]

            eth_row = conn.execute(
                "SELECT * FROM shadow_alert_decisions WHERE opportunity_uid='cand-eth-5m'"
            ).fetchone()
            assert eth_row["would_emit"] == 1
            assert eth_row["market_snapshot_json"] is not None
            eth_snapshot = json.loads(eth_row["market_snapshot_json"])
            assert eth_snapshot["current_price"] == 205.0
            eth_evidence = json.loads(eth_row["rule_evidence_json"])
            assert MULTI_TIMEFRAME_CONFLICT_EVIDENCE_KEY not in eth_evidence
        finally:
            close_db()


# ---------------------------------------------------------------------------
# Happy path: exact row/dataclass mapping, cost consistency, eligible
# decision recording with message, append-only + idempotency.
# ---------------------------------------------------------------------------


class TestHappyPathAndPersistence:
    async def _run_with_qualifying_setup(self, conn, tmp_path):
        _seed_qualifying_cohort(conn)
        _seed_live_candidate(conn, uid="cand-1")
        store = CandleStore(conn)
        _seed_ws_snapshot(store, now_ms=FIXED_NOW_MS - 5_000)
        settings = _settings()
        return await run_shadow_alert_runtime(conn, store, settings, now=FIXED_NOW), settings

    async def test_eligible_candidate_is_recorded_with_message_and_full_evidence(self, tmp_path):
        conn = _db(tmp_path)
        try:
            result, settings = await self._run_with_qualifying_setup(conn, tmp_path)
            assert result.ran is True
            assert result.reason == RUN_REASON_OK
            assert result.candidate_count == 1
            assert result.would_emit_count == 1
            assert result.inserted_count == 1

            row = conn.execute(
                "SELECT * FROM shadow_alert_decisions WHERE opportunity_uid='cand-1'"
            ).fetchone()
            assert row is not None
            assert row["pilot_id"] == PILOT_ID
            assert row["would_emit"] == 1
            assert json.loads(row["reason_codes_json"]) == [REASON_EMIT_ELIGIBLE]
            assert row["message"] is not None and "RESEARCH/SHADOW-ONLY" in row["message"]
            assert row["symbol"] == "BTC"
            assert row["direction"] == "LONG"
            assert row["setup_family"] == "SWEEP_RECLAIM"
            assert row["primary_timeframe"] == "5m"
            assert row["entry_price"] == 100.0
            assert row["stop_price"] == 90.0
            assert row["target_1r_price"] == 110.0
            assert row["target_2r_price"] == 120.0
            assert row["market_snapshot_json"] is not None
            snap = json.loads(row["market_snapshot_json"])
            assert snap["symbol"] == "BTC"
            assert snap["current_price"] == 105.0
            assert row["cohort_evidence_json"] is not None
            cohort = json.loads(row["cohort_evidence_json"])
            assert cohort["distinct_resolved_clusters"] >= MIN_RESOLVED_CLUSTERS
        finally:
            close_db()

    async def test_cost_consistent_between_cohort_report_and_evaluator(self, tmp_path):
        conn = _db(tmp_path)
        try:
            result, settings = await self._run_with_qualifying_setup(conn, tmp_path)
            assert result.would_emit_count == 1
            row = conn.execute(
                "SELECT * FROM shadow_alert_decisions WHERE opportunity_uid='cand-1'"
            ).fetchone()
            total_cost_r = settings.shadow_alert_pilot_fee_r + settings.shadow_alert_pilot_slippage_r
            assert row["cost_fee_r"] == settings.shadow_alert_pilot_fee_r
            assert row["cost_slippage_r"] == settings.shadow_alert_pilot_slippage_r
            assert row["cost_total_r"] == pytest.approx(total_cost_r)
            cohort = json.loads(row["cohort_evidence_json"])
            # Every historical row is a clear +2R; net_expectancy_r must be
            # exactly gross(2.0) - total_cost_r, proving the cohort report
            # used the SAME cost the evaluator's CostEstimate carried.
            assert cohort["net_expectancy_r"] == pytest.approx(2.0 - total_cost_r)
        finally:
            close_db()

    async def test_exact_repeated_run_is_idempotent(self, tmp_path):
        conn = _db(tmp_path)
        try:
            _seed_qualifying_cohort(conn)
            _seed_live_candidate(conn, uid="cand-1")
            store = CandleStore(conn)
            _seed_ws_snapshot(store, now_ms=FIXED_NOW_MS - 5_000)
            settings = _settings()

            first = await run_shadow_alert_runtime(conn, store, settings, now=FIXED_NOW)
            assert first.inserted_count == 1

            second = await run_shadow_alert_runtime(conn, store, settings, now=FIXED_NOW)
            assert second.inserted_count == 0  # exact repeat: idempotent no-op

            rows = conn.execute(
                "SELECT * FROM shadow_alert_decisions WHERE opportunity_uid='cand-1'"
            ).fetchall()
            assert len(rows) == 1
            # The original decision (EMIT_ELIGIBLE) must survive untouched —
            # never silently overwritten by the second attempt's own
            # (now-suppressed, since prior_state now includes this uid)
            # would-have-been decision.
            assert rows[0]["would_emit"] == 1
            assert json.loads(rows[0]["reason_codes_json"]) == [REASON_EMIT_ELIGIBLE]
        finally:
            close_db()

    async def test_batch_write_is_all_or_nothing_on_failure(self, tmp_path):
        """A failure partway through the batch insert (here: a value SQLite
        cannot bind at all) must roll back every row from that batch —
        never a partially-persisted run.
        """
        conn = _db(tmp_path)
        try:
            good = repo.ShadowDecisionRecord(
                pilot_id=PILOT_ID, evaluator_version="v", runtime_version="v",
                run_at="2026-01-01T00:00:00.000Z", opportunity_uid="a", symbol="BTC",
                direction="LONG", setup_family="SWEEP_RECLAIM", primary_timeframe="5m",
                evaluation_not_before_ms=1, evaluation_expiry_ms=2, entry_price=1.0,
                stop_price=1.0, target_1r_price=1.0, target_2r_price=1.0,
                market_snapshot_json=None, cost_fee_r=0.0, cost_slippage_r=0.0,
                cost_total_r=0.0, cohort_evidence_json=None, would_emit=False,
                reason_codes_json="[]", message=None, rule_evidence_json="{}",
            )
            # cost_fee_r is an unsupported bind type (a plain object) —
            # sqlite3 raises an error trying to bind it, after the
            # first ("good") row's INSERT has already run in the same
            # still-open transaction. The exact sqlite3.Error subclass
            # (InterfaceError vs. ProgrammingError) is a driver/platform
            # detail; the behavior under test is transaction rollback.
            bad = repo.ShadowDecisionRecord(
                pilot_id=PILOT_ID, evaluator_version="v", runtime_version="v",
                run_at="2026-01-01T00:00:00.000Z", opportunity_uid="b", symbol="BTC",
                direction="LONG", setup_family="SWEEP_RECLAIM", primary_timeframe="5m",
                evaluation_not_before_ms=1, evaluation_expiry_ms=2, entry_price=1.0,
                stop_price=1.0, target_1r_price=1.0, target_2r_price=1.0,
                market_snapshot_json=None, cost_fee_r=object(), cost_slippage_r=0.0,
                cost_total_r=0.0, cohort_evidence_json=None, would_emit=False,
                reason_codes_json="[]", message=None, rule_evidence_json="{}",
            )
            with pytest.raises((sqlite3.InterfaceError, sqlite3.ProgrammingError)):
                repo.insert_shadow_decisions(conn, [good, bad])
            assert conn.execute("SELECT COUNT(*) FROM shadow_alert_decisions").fetchone()[0] == 0
        finally:
            close_db()


# ---------------------------------------------------------------------------
# Bounded reads: exact mapping, limit overflow.
# ---------------------------------------------------------------------------


class TestBoundedReads:
    async def test_candidate_limit_overflow_aborts_whole_run(self, tmp_path):
        conn = _db(tmp_path)
        try:
            for i in range(MAX_CANDIDATES_PER_RUN + 1):
                _seed_live_candidate(conn, uid=f"cand-{i}", symbol=f"SYM{i}")
            store = CandleStore(conn)
            settings = _settings()

            result = await run_shadow_alert_runtime(conn, store, settings, now=FIXED_NOW)
            assert result.ran is False
            assert result.reason == RUN_REASON_CANDIDATE_LIMIT_OVERFLOW
            assert conn.execute("SELECT COUNT(*) FROM shadow_alert_decisions").fetchone()[0] == 0
        finally:
            close_db()

    async def test_quality_limit_overflow_aborts_whole_run(self, tmp_path, monkeypatch):
        """Bounded via monkeypatching MAX_QUALITY_REPORT_ROWS_PER_RUN down to
        a small value rather than seeding 20,001 real quality rows."""
        conn = _db(tmp_path)
        try:
            monkeypatch.setattr(shadow_runtime, "MAX_QUALITY_REPORT_ROWS_PER_RUN", 3)
            _seed_qualifying_cohort(conn, count=4)
            _seed_live_candidate(conn, uid="cand-1")
            store = CandleStore(conn)
            _seed_ws_snapshot(store, now_ms=FIXED_NOW_MS - 5_000)
            settings = _settings()

            result = await run_shadow_alert_runtime(conn, store, settings, now=FIXED_NOW)
            assert result.ran is False
            assert result.reason == RUN_REASON_QUALITY_LIMIT_OVERFLOW
            assert conn.execute("SELECT COUNT(*) FROM shadow_alert_decisions").fetchone()[0] == 0
        finally:
            close_db()

    async def test_prior_limit_overflow_aborts_whole_run(self, tmp_path, monkeypatch):
        """Bounded via monkeypatching MAX_PRIOR_DECISION_ROWS_PER_RUN down to
        a small value rather than seeding 5,001 real prior decision rows."""
        conn = _db(tmp_path)
        try:
            monkeypatch.setattr(shadow_runtime, "MAX_PRIOR_DECISION_ROWS_PER_RUN", 3)
            _seed_qualifying_cohort(conn)
            _seed_live_candidate(conn, uid="cand-1")
            for i in range(4):
                _insert_prior_decision(
                    conn, run_at=_run_at(FIXED_NOW - timedelta(hours=2, minutes=i)),
                    opportunity_uid=f"prior-{i}", symbol=f"SYM{i}",
                    not_before_ms=FIXED_NOW_MS - 5_000_000, expiry_ms=FIXED_NOW_MS - 4_000_000,
                )
            store = CandleStore(conn)
            _seed_ws_snapshot(store, now_ms=FIXED_NOW_MS - 5_000)
            settings = _settings()

            result = await run_shadow_alert_runtime(conn, store, settings, now=FIXED_NOW)
            assert result.ran is False
            assert result.reason == RUN_REASON_PRIOR_LIMIT_OVERFLOW
            # No new row from this (failed) run — only the 4 seeded fixture
            # rows remain.
            assert conn.execute("SELECT COUNT(*) FROM shadow_alert_decisions").fetchone()[0] == 4
        finally:
            close_db()

    async def test_malformed_quality_row_evaluation_window_type_mismatch_fails_closed(self, tmp_path):
        """A stored evaluation_expiry_ms that fails to compare against a
        well-typed evaluation_not_before_ms (SQLite has no column-level type
        enforcement) maps cleanly onto QualityRow but only surfaces once
        build_quality_cohort_report actually compares it — this must abort
        the whole run with a precise reason, never crash or silently
        evaluate against corrupted evidence.
        """
        conn = _db(tmp_path)
        try:
            _seed_qualifying_cohort(conn)
            _seed_live_candidate(conn, uid="cand-1")
            conn.execute(
                "UPDATE opportunity_trade_plans SET evaluation_expiry_ms='not-an-int' "
                "WHERE opportunity_uid='cand-1'"
            )
            conn.commit()
            store = CandleStore(conn)
            _seed_ws_snapshot(store, now_ms=FIXED_NOW_MS - 5_000)
            settings = _settings()

            result = await run_shadow_alert_runtime(conn, store, settings, now=FIXED_NOW)
            assert result.ran is False
            assert result.reason == RUN_REASON_QUALITY_REPORT_BUILD_FAILED
            assert conn.execute("SELECT COUNT(*) FROM shadow_alert_decisions").fetchone()[0] == 0
        finally:
            close_db()

    async def test_exact_row_mapping_reaches_contract_check_reason(self, tmp_path):
        """A wrong opportunity_contract_version stored on the row must
        surface as the exact corresponding shadow_alerts reason code,
        proving the mapping from row -> ShadowCandidate is exact (no
        silent normalization)."""
        conn = _db(tmp_path)
        try:
            _seed_qualifying_cohort(conn)
            _seed_live_candidate(conn, uid="cand-1")
            conn.execute(
                "UPDATE opportunity_observations SET contract_version='bogus_v0' "
                "WHERE opportunity_uid='cand-1'"
            )
            conn.commit()
            store = CandleStore(conn)
            _seed_ws_snapshot(store, now_ms=FIXED_NOW_MS - 5_000)
            settings = _settings()

            result = await run_shadow_alert_runtime(conn, store, settings, now=FIXED_NOW)
            assert result.ran is True
            row = conn.execute(
                "SELECT * FROM shadow_alert_decisions WHERE opportunity_uid='cand-1'"
            ).fetchone()
            assert row["would_emit"] == 0
            assert "UNSUPPORTED_OPPORTUNITY_CONTRACT_VERSION" in json.loads(row["reason_codes_json"])
        finally:
            close_db()

    async def test_unqualified_cohort_suppresses_when_history_insufficient(self, tmp_path):
        """With no clear-resolved historical evidence for this exact cohort
        (the live candidate's own row is present in the quality-report
        query, but contributes zero resolved clusters since it is still
        PENDING_ENTRY), the cohort fails quality_uncertainty's fixed
        qualification floor — evidence is present but insufficient, a
        distinct case from REASON_COHORT_EVIDENCE_MISSING (an absent
        cohort key entirely).
        """
        conn = _db(tmp_path)
        try:
            _seed_live_candidate(conn, uid="cand-1")  # no historical rows at all
            store = CandleStore(conn)
            _seed_ws_snapshot(store, now_ms=FIXED_NOW_MS - 5_000)
            settings = _settings()

            result = await run_shadow_alert_runtime(conn, store, settings, now=FIXED_NOW)
            assert result.ran is True
            row = conn.execute(
                "SELECT * FROM shadow_alert_decisions WHERE opportunity_uid='cand-1'"
            ).fetchone()
            assert row["would_emit"] == 0
            reasons = json.loads(row["reason_codes_json"])
            assert any(r.startswith("COHORT_") for r in reasons)
            cohort = json.loads(row["cohort_evidence_json"])
            assert cohort["distinct_resolved_clusters"] == 0
        finally:
            close_db()


# ---------------------------------------------------------------------------
# Query/write sqlite3.Error paths — each fails the whole run closed with no
# decision write, distinct from the bounded-overflow paths above.
# ---------------------------------------------------------------------------


class TestQueryAndWriteFailurePaths:
    async def test_candidate_query_sqlite_error_fails_closed(self, tmp_path, monkeypatch):
        conn = _db(tmp_path)
        try:
            monkeypatch.setattr(
                repo, "get_active_shadow_candidate_rows",
                lambda *a, **kw: (_ for _ in ()).throw(sqlite3.Error("boom")),
            )
            store = CandleStore(conn)
            settings = _settings()

            result = await run_shadow_alert_runtime(conn, store, settings, now=FIXED_NOW)
            assert result.ran is False
            assert result.reason == RUN_REASON_CANDIDATE_QUERY_FAILED
            assert conn.execute("SELECT COUNT(*) FROM shadow_alert_decisions").fetchone()[0] == 0
        finally:
            close_db()

    async def test_quality_query_sqlite_error_fails_closed(self, tmp_path, monkeypatch):
        conn = _db(tmp_path)
        try:
            _seed_live_candidate(conn, uid="cand-1")
            monkeypatch.setattr(
                repo, "get_shadow_quality_rows",
                lambda *a, **kw: (_ for _ in ()).throw(sqlite3.Error("boom")),
            )
            store = CandleStore(conn)
            _seed_ws_snapshot(store, now_ms=FIXED_NOW_MS - 5_000)
            settings = _settings()

            result = await run_shadow_alert_runtime(conn, store, settings, now=FIXED_NOW)
            assert result.ran is False
            assert result.reason == RUN_REASON_QUALITY_QUERY_FAILED
            assert conn.execute("SELECT COUNT(*) FROM shadow_alert_decisions").fetchone()[0] == 0
        finally:
            close_db()

    async def test_prior_query_sqlite_error_fails_closed(self, tmp_path, monkeypatch):
        conn = _db(tmp_path)
        try:
            _seed_qualifying_cohort(conn)
            _seed_live_candidate(conn, uid="cand-1")
            monkeypatch.setattr(
                repo, "get_shadow_emitted_decisions",
                lambda *a, **kw: (_ for _ in ()).throw(sqlite3.Error("boom")),
            )
            store = CandleStore(conn)
            _seed_ws_snapshot(store, now_ms=FIXED_NOW_MS - 5_000)
            settings = _settings()

            result = await run_shadow_alert_runtime(conn, store, settings, now=FIXED_NOW)
            assert result.ran is False
            assert result.reason == RUN_REASON_PRIOR_QUERY_FAILED
            assert conn.execute("SELECT COUNT(*) FROM shadow_alert_decisions").fetchone()[0] == 0
        finally:
            close_db()

    async def test_decision_write_sqlite_error_fails_closed(self, tmp_path, monkeypatch):
        conn = _db(tmp_path)
        try:
            _seed_qualifying_cohort(conn)
            _seed_live_candidate(conn, uid="cand-1")
            monkeypatch.setattr(
                repo, "insert_shadow_decisions",
                lambda *a, **kw: (_ for _ in ()).throw(sqlite3.Error("boom")),
            )
            store = CandleStore(conn)
            _seed_ws_snapshot(store, now_ms=FIXED_NOW_MS - 5_000)
            settings = _settings()

            result = await run_shadow_alert_runtime(conn, store, settings, now=FIXED_NOW)
            assert result.ran is False
            assert result.reason == RUN_REASON_WRITE_FAILED
            assert conn.execute("SELECT COUNT(*) FROM shadow_alert_decisions").fetchone()[0] == 0
        finally:
            close_db()


# ---------------------------------------------------------------------------
# Crash/restart reconstruction of prior state from the evidence table.
# ---------------------------------------------------------------------------


class TestPriorStateReconstruction:
    async def test_already_emitted_uid_is_suppressed_on_reconstruction(self, tmp_path):
        conn = _db(tmp_path)
        try:
            _seed_qualifying_cohort(conn)
            _seed_live_candidate(conn, uid="cand-1")
            _insert_prior_decision(
                conn, run_at=_run_at(FIXED_NOW - timedelta(hours=2)), opportunity_uid="cand-1",
                not_before_ms=FIXED_NOW_MS - 5_000_000, expiry_ms=FIXED_NOW_MS - 4_000_000,
            )
            store = CandleStore(conn)
            _seed_ws_snapshot(store, now_ms=FIXED_NOW_MS - 5_000)
            settings = _settings()

            result = await run_shadow_alert_runtime(conn, store, settings, now=FIXED_NOW)
            assert result.ran is True
            row = conn.execute(
                "SELECT * FROM shadow_alert_decisions WHERE opportunity_uid='cand-1' "
                "AND run_at != ?", (_run_at(FIXED_NOW - timedelta(hours=2)),),
            ).fetchone()
            assert row["would_emit"] == 0
            assert REASON_OPPORTUNITY_ALREADY_EMITTED in json.loads(row["reason_codes_json"])
        finally:
            close_db()

    async def test_cooldown_reconstructed_from_prior_emission(self, tmp_path):
        conn = _db(tmp_path)
        try:
            _seed_qualifying_cohort(conn)
            _seed_live_candidate(conn, uid="cand-new")
            _insert_prior_decision(
                conn, run_at=_run_at(FIXED_NOW - timedelta(minutes=10)), opportunity_uid="cand-old",
                symbol="BTC", direction="LONG",
                not_before_ms=FIXED_NOW_MS - 5_000_000, expiry_ms=FIXED_NOW_MS - 4_000_000,
            )
            store = CandleStore(conn)
            _seed_ws_snapshot(store, now_ms=FIXED_NOW_MS - 5_000)
            settings = _settings()

            result = await run_shadow_alert_runtime(conn, store, settings, now=FIXED_NOW)
            assert result.ran is True
            row = conn.execute(
                "SELECT * FROM shadow_alert_decisions WHERE opportunity_uid='cand-new'"
            ).fetchone()
            assert row["would_emit"] == 0
            assert REASON_COOLDOWN_ACTIVE in json.loads(row["reason_codes_json"])
        finally:
            close_db()

    async def test_open_cluster_window_reconstructed_from_prior_emission(self, tmp_path):
        conn = _db(tmp_path)
        try:
            _seed_qualifying_cohort(conn)
            _seed_live_candidate(
                conn, uid="cand-new", not_before_ms=FIXED_NOW_MS - 2_000_000, expiry_ms=FIXED_NOW_MS + 1_000_000,
            )
            # Prior emission's window still overlaps "now" (its own expiry
            # is in the future) — must suppress cand-new as overlapping,
            # even though the prior decision was cooldown-eligible-elapsed.
            _insert_prior_decision(
                conn, run_at=_run_at(FIXED_NOW - timedelta(hours=2)), opportunity_uid="cand-old",
                symbol="BTC", direction="LONG",
                not_before_ms=FIXED_NOW_MS - 3_000_000, expiry_ms=FIXED_NOW_MS + 500_000,
            )
            store = CandleStore(conn)
            _seed_ws_snapshot(store, now_ms=FIXED_NOW_MS - 5_000)
            settings = _settings()

            result = await run_shadow_alert_runtime(conn, store, settings, now=FIXED_NOW)
            assert result.ran is True
            row = conn.execute(
                "SELECT * FROM shadow_alert_decisions WHERE opportunity_uid='cand-new'"
            ).fetchone()
            assert row["would_emit"] == 0
            reasons = json.loads(row["reason_codes_json"])
            assert REASON_OVERLAPS_OPEN_CLUSTER in reasons
        finally:
            close_db()

    async def test_rolling_hour_cap_reconstructed_across_symbols(self, tmp_path):
        conn = _db(tmp_path)
        try:
            _seed_qualifying_cohort(conn, symbol="AVAX")
            _seed_live_candidate(conn, uid="cand-new", symbol="AVAX")
            for i, sym in enumerate(["BTC", "ETH", "SOL"]):
                _insert_prior_decision(
                    conn, run_at=_run_at(FIXED_NOW - timedelta(minutes=10 + i * 10)),
                    opportunity_uid=f"hour-cap-{sym}", symbol=sym, direction="LONG",
                    not_before_ms=FIXED_NOW_MS - 5_000_000, expiry_ms=FIXED_NOW_MS - 4_000_000,
                )
            store = CandleStore(conn)
            _seed_ws_snapshot(store, symbol="AVAX", now_ms=FIXED_NOW_MS - 5_000)
            settings = _settings()

            result = await run_shadow_alert_runtime(conn, store, settings, now=FIXED_NOW)
            assert result.ran is True
            row = conn.execute(
                "SELECT * FROM shadow_alert_decisions WHERE opportunity_uid='cand-new'"
            ).fetchone()
            assert row["would_emit"] == 0
            assert REASON_ROLLING_HOUR_CAP_REACHED in json.loads(row["reason_codes_json"])
        finally:
            close_db()

    async def test_rolling_day_cap_reconstructed_without_hour_cap(self, tmp_path):
        conn = _db(tmp_path)
        try:
            _seed_qualifying_cohort(conn, symbol="MATIC")
            _seed_live_candidate(conn, uid="cand-new", symbol="MATIC")
            symbols = ["BTC", "ETH", "SOL", "AVAX", "DOGE", "XRP", "ADA", "DOT", "LINK", "UNI"]
            for i, sym in enumerate(symbols):
                # More than one hour before `now` each — never counts toward
                # the rolling 1h cap, only the rolling Detroit-day cap.
                _insert_prior_decision(
                    conn, run_at=_run_at(FIXED_NOW - timedelta(minutes=70 + i * 10)),
                    opportunity_uid=f"day-cap-{sym}", symbol=sym, direction="LONG",
                    not_before_ms=FIXED_NOW_MS - 9_000_000, expiry_ms=FIXED_NOW_MS - 8_000_000,
                )
            store = CandleStore(conn)
            _seed_ws_snapshot(store, symbol="MATIC", now_ms=FIXED_NOW_MS - 5_000)
            settings = _settings()

            result = await run_shadow_alert_runtime(conn, store, settings, now=FIXED_NOW)
            assert result.ran is True
            row = conn.execute(
                "SELECT * FROM shadow_alert_decisions WHERE opportunity_uid='cand-new'"
            ).fetchone()
            assert row["would_emit"] == 0
            reasons = json.loads(row["reason_codes_json"])
            assert REASON_ROLLING_DAY_CAP_REACHED in reasons
            assert REASON_ROLLING_HOUR_CAP_REACHED not in reasons
        finally:
            close_db()

    async def test_malformed_prior_history_suppresses_whole_batch(self, tmp_path):
        """A stored run_at that fails to parse is passed through, never
        repaired/dropped — shadow_alerts' own prior-state validation must
        catch it and suppress every candidate in the batch.
        """
        conn = _db(tmp_path)
        try:
            _seed_qualifying_cohort(conn)
            _seed_live_candidate(conn, uid="cand-1")
            _seed_live_candidate(conn, uid="cand-2", symbol="ETH", entry=200.0, stop=180.0, t1=220.0, t2=240.0)
            _insert_prior_decision(
                conn, run_at=_run_at(FIXED_NOW - timedelta(hours=2)), opportunity_uid="cand-old",
            )
            conn.execute(
                "UPDATE shadow_alert_decisions SET run_at='not-a-real-timestamp' "
                "WHERE opportunity_uid='cand-old'"
            )
            conn.commit()
            store = CandleStore(conn)
            _seed_ws_snapshot(store, symbol="BTC", now_ms=FIXED_NOW_MS - 5_000)
            _seed_ws_snapshot(
                store, symbol="ETH", now_ms=FIXED_NOW_MS - 5_000, open_=205.0, high=210.0, low=201.0, close=205.0,
            )
            settings = _settings()

            result = await run_shadow_alert_runtime(conn, store, settings, now=FIXED_NOW)
            assert result.ran is True
            for uid in ("cand-1", "cand-2"):
                row = conn.execute(
                    "SELECT * FROM shadow_alert_decisions WHERE opportunity_uid=? AND run_at != ?",
                    (uid, _run_at(FIXED_NOW - timedelta(hours=2))),
                ).fetchone()
                assert row["would_emit"] == 0
                assert json.loads(row["reason_codes_json"]) == [REASON_PRIOR_STATE_INVALID]
        finally:
            close_db()


# ---------------------------------------------------------------------------
# Import isolation — AST-based static check plus a dynamic import-poisoning
# proof (mirrors tests/test_shadow_alerts.py and tests/test_gmgn_runtime.py).
# ---------------------------------------------------------------------------


class TestImportIsolation:
    def test_module_has_no_forbidden_static_imports(self):
        from apex.opportunity import shadow_runtime

        source = inspect.getsource(shadow_runtime)
        tree = ast.parse(source)
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)

        forbidden_prefixes = (
            "apex.notifications", "apex.actions", "apex.strategy",
            "httpx", "socket",
        )
        for module in imported_modules:
            for forbidden in forbidden_prefixes:
                assert module != forbidden and not module.startswith(forbidden + "."), (
                    f"shadow_runtime.py must never import {module!r}"
                )

    async def test_still_works_when_notifications_actions_strategy_imports_are_poisoned(self, tmp_path):
        class _BoomFinder:
            def find_module(self, fullname, path=None):
                forbidden = ("apex.notifications", "apex.actions", "apex.strategy")
                if any(fullname == f or fullname.startswith(f + ".") for f in forbidden):
                    raise AssertionError(f"must never import {fullname!r}")

        finder = _BoomFinder()
        sys.meta_path.insert(0, finder)
        try:
            for name in list(sys.modules):
                if any(
                    name == f or name.startswith(f + ".")
                    for f in ("apex.notifications", "apex.actions", "apex.strategy")
                ):
                    del sys.modules[name]

            conn = _db(tmp_path)
            try:
                _seed_qualifying_cohort(conn)
                _seed_live_candidate(conn, uid="cand-1")
                store = CandleStore(conn)
                _seed_ws_snapshot(store, now_ms=FIXED_NOW_MS - 5_000)
                settings = _settings()

                result = await run_shadow_alert_runtime(conn, store, settings, now=FIXED_NOW)
                assert result.ran is True
                assert result.would_emit_count == 1
            finally:
                close_db()
        finally:
            sys.meta_path.remove(finder)


# ---------------------------------------------------------------------------
# Schema additivity — fresh and legacy databases, idempotent re-init,
# constraints/indexes.
# ---------------------------------------------------------------------------


class TestSchemaAdditivity:
    def test_fresh_database_has_table_and_indexes(self, tmp_path):
        conn = _db(tmp_path)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='shadow_alert_decisions'"
            ).fetchone()
            assert row is not None
            index_names = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='shadow_alert_decisions'"
                ).fetchall()
            }
            assert "idx_shadow_alert_decisions_pilot_emitted" in index_names
            assert "idx_shadow_alert_decisions_pilot_uid" in index_names
        finally:
            close_db()

    def test_legacy_database_without_table_gains_it_on_init(self, tmp_path):
        db_path = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE markets (id INTEGER PRIMARY KEY, symbol TEXT UNIQUE NOT NULL)")
        conn.execute("INSERT INTO markets (symbol) VALUES ('BTC')")
        conn.commit()
        conn.close()

        conn2 = init_db(db_path)
        try:
            row = conn2.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='shadow_alert_decisions'"
            ).fetchone()
            assert row is not None
            markets = conn2.execute("SELECT symbol FROM markets").fetchall()
            assert [r[0] for r in markets] == ["BTC"]
        finally:
            close_db()

    def test_idempotent_reinit_preserves_existing_rows(self, tmp_path):
        db_path = str(tmp_path / "reinit.db")
        conn = init_db(db_path)
        _seed_qualifying_cohort(conn, count=1)
        close_db()

        conn2 = init_db(db_path)
        try:
            count = conn2.execute("SELECT COUNT(*) FROM opportunity_observations").fetchone()[0]
            assert count == 1
        finally:
            close_db()

    def test_unique_constraint_enforces_idempotency_at_db_level(self, tmp_path):
        conn = _db(tmp_path)
        try:
            record = repo.ShadowDecisionRecord(
                pilot_id=PILOT_ID, evaluator_version="v", runtime_version="v",
                run_at="2026-01-01T00:00:00.000Z", opportunity_uid="dup", symbol="BTC",
                direction="LONG", setup_family="SWEEP_RECLAIM", primary_timeframe="5m",
                evaluation_not_before_ms=1, evaluation_expiry_ms=2, entry_price=1.0,
                stop_price=1.0, target_1r_price=1.0, target_2r_price=1.0,
                market_snapshot_json=None, cost_fee_r=0.0, cost_slippage_r=0.0,
                cost_total_r=0.0, cohort_evidence_json=None, would_emit=False,
                reason_codes_json="[]", message=None, rule_evidence_json="{}",
            )
            first = repo.insert_shadow_decisions(conn, [record])
            second = repo.insert_shadow_decisions(conn, [record])
            assert first == 1
            assert second == 0
            assert conn.execute("SELECT COUNT(*) FROM shadow_alert_decisions").fetchone()[0] == 1
        finally:
            close_db()

    async def test_never_writes_to_unrelated_tables(self, tmp_path):
        conn = _db(tmp_path)
        try:
            _seed_qualifying_cohort(conn)
            _seed_live_candidate(conn, uid="cand-1")
            store = CandleStore(conn)
            _seed_ws_snapshot(store, now_ms=FIXED_NOW_MS - 5_000)
            settings = _settings()

            result = await run_shadow_alert_runtime(conn, store, settings, now=FIXED_NOW)
            assert result.would_emit_count == 1

            for table in ("alerts", "paper_trades", "daily_risk", "signal_observations"):
                after = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                assert after == 0
            # opportunity/plan/outcome tables are read-only inputs to this
            # runtime — only the seeded rows exist, never mutated/expanded.
            assert conn.execute("SELECT COUNT(*) FROM opportunity_observations").fetchone()[0] == (
                MIN_RESOLVED_CLUSTERS + 1
            )
        finally:
            close_db()
