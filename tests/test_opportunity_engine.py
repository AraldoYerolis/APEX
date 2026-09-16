"""Tests for the TA Opportunity Engine v0.1 orchestration, storage, and
safety gating (src/apex/opportunity/engine.py).

Covers: default-disabled config, dry_run_mode gating, deterministic
dedupe, independent setups not collapsing, 3m/5m identity, storage
insert/read/update, non-interference with every other table, and the
existing TREND_PULLBACK signal path staying unchanged.
"""
from __future__ import annotations

import asyncio
import json

import pandas as pd
import pytest

from apex.config import get_settings
from apex.data.candle_store import CandleStore
from apex.db import repository as repo
from apex.db.connection import close_db, init_db
from apex.db.models import Market
from apex.notifications.pushover_client import PushoverClient
from apex.opportunity.context import (
    StructureComponent,
    TrendComponent,
    VolatilityComponent,
    not_applicable_trend_component,
)
from apex.opportunity.contract import DetectorFinding, Opportunity
from apex.opportunity.detectors.volatility_compression import detect_volatility_compression
import apex.opportunity.engine as opportunity_engine
from apex.opportunity.engine import (
    BTC_SYMBOL,
    ContextInputs,
    OpportunityScanSummary,
    _record_finding,
    compute_fingerprint,
    run_opportunity_scan,
)
from apex.opportunity.scoring import SCORE_VERSION
from apex.scheduler.tasks import run_signal_scan
from apex.utils.time import utc_from_iso


# ------------------------------------------------------------------ helpers

def _env(monkeypatch, db_path: str, *, opportunity_enabled: bool = True, dry_run: bool = True) -> None:
    monkeypatch.setenv("APEX_DB_PATH", db_path)
    monkeypatch.setenv("ACTION_TOKEN_SECRET", "test-opportunity-secret")
    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "fake")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "fake")
    monkeypatch.setenv("ALERTS_ENABLED", "false")
    monkeypatch.setenv("DRY_RUN_MODE", "true" if dry_run else "false")
    monkeypatch.setenv("OPPORTUNITY_ENGINE_ENABLED", "true" if opportunity_enabled else "false")


def _make_df(opens, highs, lows, closes, volumes=None):
    n = len(closes)
    volumes = volumes or [1000.0] * n
    return pd.DataFrame({
        "open_time": list(range(n)),
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


def _sweep_reclaim_df(n: int = 65) -> pd.DataFrame:
    """>= engine.MIN_CANDLES_REQUIRED bars containing a confirmed pivot low,
    a sweep below it, and a reclaim, all within the trailing 5-bar window.
    """
    pad = n - 30
    assert pad >= 0
    base = 100.0
    opens = [base] * pad
    highs = [base + 0.5] * pad
    lows = [base - 0.5] * pad
    closes = [base] * pad

    seg_opens = [base] * 30
    seg_highs = [base + 0.5] * 30
    seg_lows = [base - 0.5] * 30
    seg_closes = [base] * 30
    seg_lows[15] = base - 3.0
    seg_highs[15] = base - 2.0
    seg_closes[15] = base - 2.5
    seg_opens[15] = base - 2.2
    seg_lows[27] = base - 3.5
    seg_highs[27] = base - 2.8
    seg_closes[27] = base - 3.0
    seg_opens[27] = base - 2.9
    seg_opens[28] = base - 3.0
    seg_closes[28] = base - 2.5
    seg_highs[28] = base - 2.4
    seg_lows[28] = base - 3.1

    opens += seg_opens
    highs += seg_highs
    lows += seg_lows
    closes += seg_closes
    return _make_df(opens, highs, lows, closes)


def _load_into_store(store: CandleStore, symbol: str, timeframe: str, df: pd.DataFrame) -> None:
    for _, row in df.iterrows():
        store.update(
            symbol,
            timeframe,
            {
                "open_time": int(row["open_time"]),
                "close_time": int(row["open_time"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
                "closed": True,
            },
            persist=False,
        )


def _seed_market(conn, symbol: str) -> None:
    repo.upsert_market(conn, Market(symbol=symbol, is_active=True, scan_enabled=True))


def _other_table_counts(conn) -> dict:
    tables = ["alerts", "signal_observations", "signal_features", "paper_trades", "daily_risk"]
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def _make_finding(**overrides) -> DetectorFinding:
    base = dict(
        symbol="BTC",
        direction="LONG",
        setup_family="SWEEP_RECLAIM",
        detector_version="sweep_reclaim_v0_1",
        primary_timeframe="5m",
        source_candle_open_time=1000,
        source_candle_close_time=1000,
        fingerprint_key="anchor-100",
        anchor_price=97.0,
        anchor_open_time=100,
        evidence={},
        warnings=[],
        measurements={"x": 1},
    )
    base.update(overrides)
    return DetectorFinding(**base)


def _make_compression_finding(**overrides) -> DetectorFinding:
    base = dict(
        symbol="BTC",
        direction="LONG",
        setup_family="VOLATILITY_COMPRESSION",
        detector_version="vol_compression_v0_1_1",
        primary_timeframe="5m",
        source_candle_open_time=2000,
        source_candle_close_time=2300,
        fingerprint_key="episode-500",
        anchor_price=101.0,
        anchor_open_time=2000,
        evidence={"k": "v"},
        warnings=[],
        measurements={"atr_now": 1.0},
    )
    base.update(overrides)
    return DetectorFinding(**base)


def _compression_then_breakout_df(extra_expansion_bars: int = 0) -> pd.DataFrame:
    """Same construction as tests/test_opportunity_detectors.py's helper of
    the same name: 99 baseline bars (range=2.0), 10 compressed bars
    (range=0.1), then a breakout bar, plus `extra_expansion_bars` further
    expansion bars — i.e. the *same* continuing compression -> expansion
    episode evaluated at a later bar. Duplicated locally rather than
    imported, matching this test suite's existing per-file fixture
    convention (see _make_df above).
    """
    opens, highs, lows, closes = [], [], [], []
    for _ in range(99):
        opens.append(100.0)
        closes.append(100.0)
        highs.append(101.0)
        lows.append(99.0)
    for _ in range(10):
        opens.append(100.0)
        closes.append(100.0)
        highs.append(100.05)
        lows.append(99.95)
    opens.append(100.0)
    closes.append(103.0)
    highs.append(103.2)
    lows.append(99.9)
    for i in range(extra_expansion_bars):
        opens.append(103.0 + i)
        closes.append(105.0 + i)
        highs.append(105.5 + i)
        lows.append(102.5 + i)
    return _make_df(opens, highs, lows, closes)


def _compression_direction_reversal_stages() -> list[pd.DataFrame]:
    """Three real, continuous candle histories of the SAME compression
    episode (fixed episode start — only the trailing expansion bars
    differ): net LONG (close 103.0), then a genuine reversal to net SHORT
    (close 99.0), then back to net LONG (close 101.0). See the identical
    fixture and its derivation in test_opportunity_detectors.py.
    """
    stage_long = _compression_then_breakout_df(0)

    opens = list(stage_long["open"])
    highs = list(stage_long["high"])
    lows = list(stage_long["low"])
    closes = list(stage_long["close"])
    for o, h, low, c in [(103.0, 103.5, 100.5, 101.0), (101.0, 101.5, 98.5, 99.0)]:
        opens.append(o)
        highs.append(h)
        lows.append(low)
        closes.append(c)
    stage_short = _make_df(opens, highs, lows, closes)

    opens, highs, lows, closes = list(opens), list(highs), list(lows), list(closes)
    opens.append(99.0)
    highs.append(101.5)
    lows.append(98.5)
    closes.append(101.0)
    stage_long_again = _make_df(opens, highs, lows, closes)

    return [stage_long, stage_short, stage_long_again]


def _downtrend_context_15m_df() -> pd.DataFrame:
    """A clean downtrend -> deterministic SHORT 15m trend bias, held fixed
    across all three detection stages below so any warnings_json difference
    between stages comes only from the compression finding's own direction
    tag flipping (LONG vs SHORT) against this unchanging context, not from
    the context itself changing.
    """
    n15 = 40
    closes15 = [200.0 - i for i in range(n15)]
    return _make_df(
        [c + 0.5 for c in closes15], [c + 1.0 for c in closes15],
        [c - 1.0 for c in closes15], closes15,
    )


# ------------------------------------------------------------------ default-disabled / gating

def test_opportunity_engine_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("APEX_DB_PATH", str(tmp_path / "default.db"))
    monkeypatch.setenv("ACTION_TOKEN_SECRET", "test-secret")
    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "fake")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "fake")
    monkeypatch.setenv("ALERTS_ENABLED", "false")
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    monkeypatch.delenv("OPPORTUNITY_ENGINE_ENABLED", raising=False)
    settings = get_settings()
    assert settings.opportunity_engine_enabled is False


def test_no_scan_when_engine_disabled(monkeypatch, tmp_path):
    db_path = str(tmp_path / "disabled.db")
    _env(monkeypatch, db_path, opportunity_enabled=False, dry_run=True)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        _seed_market(conn, "BTC")
        store = CandleStore(conn)
        _load_into_store(store, "BTC", "5m", _sweep_reclaim_df())

        summary = asyncio.run(run_opportunity_scan(conn, store, settings))
        assert summary.markets_scanned == 0
        assert repo.get_opportunities(conn) == []
    finally:
        close_db()


def test_no_scan_when_dry_run_mode_false(monkeypatch, tmp_path):
    db_path = str(tmp_path / "livemode.db")
    _env(monkeypatch, db_path, opportunity_enabled=True, dry_run=False)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        _seed_market(conn, "BTC")
        store = CandleStore(conn)
        _load_into_store(store, "BTC", "5m", _sweep_reclaim_df())

        summary = asyncio.run(run_opportunity_scan(conn, store, settings))
        assert summary.markets_scanned == 0
        assert repo.get_opportunities(conn) == []
    finally:
        close_db()


# ------------------------------------------------------------------ dedupe / fingerprint semantics

def test_deterministic_dedupe_touches_instead_of_duplicating(monkeypatch, tmp_path):
    db_path = str(tmp_path / "dedupe.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        finding = _make_finding()
        is_new_1 = _record_finding(conn, finding, "2026-01-01T00:00:00Z")
        is_new_2 = _record_finding(conn, finding, "2026-01-01T00:01:00Z")

        assert is_new_1 is True
        assert is_new_2 is False

        rows = repo.get_opportunities(conn)
        assert len(rows) == 1
        assert rows[0]["occurrence_count"] == 2
        assert rows[0]["last_seen_at"] == "2026-01-01T00:01:00Z"
        assert rows[0]["first_detected_at"] == "2026-01-01T00:00:00Z"
    finally:
        close_db()


def test_separate_setups_do_not_collapse(monkeypatch, tmp_path):
    """Two distinct anchors (different fingerprint_key) for the same
    symbol/direction/family/timeframe must produce two independent rows.
    """
    db_path = str(tmp_path / "separate.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        finding_a = _make_finding(fingerprint_key="anchor-100", anchor_open_time=100)
        finding_b = _make_finding(fingerprint_key="anchor-500", anchor_open_time=500)

        _record_finding(conn, finding_a, "2026-01-01T00:00:00Z")
        _record_finding(conn, finding_b, "2026-01-01T00:05:00Z")

        rows = repo.get_opportunities(conn)
        assert len(rows) == 2
        fingerprints = {r["fingerprint"] for r in rows}
        assert len(fingerprints) == 2
        assert compute_fingerprint(finding_a) != compute_fingerprint(finding_b)
    finally:
        close_db()


def test_3m_vs_5m_stored_as_distinct_opportunities(monkeypatch, tmp_path):
    db_path = str(tmp_path / "tf_identity.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        finding_3m = _make_finding(primary_timeframe="3m")
        finding_5m = _make_finding(primary_timeframe="5m")

        assert compute_fingerprint(finding_3m) != compute_fingerprint(finding_5m)

        _record_finding(conn, finding_3m, "2026-01-01T00:00:00Z")
        _record_finding(conn, finding_5m, "2026-01-01T00:00:00Z")

        rows = repo.get_opportunities(conn)
        assert len(rows) == 2
        timeframes = {r["primary_timeframe"] for r in rows}
        assert timeframes == {"3m", "5m"}
    finally:
        close_db()


# ------------------------------------------------------------------ storage insert/read/update

def test_storage_insert_read_update(monkeypatch, tmp_path):
    db_path = str(tmp_path / "storage.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        opp = Opportunity(
            opportunity_uid="uid-1",
            fingerprint="fp-1",
            symbol="BTC",
            direction="LONG",
            setup_family="SWEEP_RECLAIM",
            detector_version="sweep_reclaim_v0_1",
            primary_timeframe="5m",
            first_detected_at="2026-01-01T00:00:00Z",
            last_seen_at="2026-01-01T00:00:00Z",
            source_candle_open_time=1000,
            source_candle_close_time=1000,
            anchor_price=97.0,
            anchor_open_time=100,
            evidence_json="{}",
            warnings_json="[]",
            measurements_json="{}",
        )
        row_id = repo.insert_opportunity(conn, opp)
        assert row_id is not None

        active = repo.get_active_opportunity_by_fingerprint(conn, "fp-1")
        assert active is not None
        assert active["opportunity_uid"] == "uid-1"
        assert active["occurrence_count"] == 1
        assert active["status"] == "ACTIVE"

        repo.touch_opportunity(
            conn, "uid-1",
            last_seen_at="2026-01-01T00:05:00Z",
            occurrence_count=2,
            measurements_json='{"x": 2}',
        )
        touched = repo.get_active_opportunity_by_fingerprint(conn, "fp-1")
        assert touched["occurrence_count"] == 2
        assert touched["last_seen_at"] == "2026-01-01T00:05:00Z"

        expired_count = repo.expire_stale_opportunities(
            conn, primary_timeframe="5m", cutoff_iso="2026-01-01T00:10:00Z"
        )
        assert expired_count == 1
        assert repo.get_active_opportunity_by_fingerprint(conn, "fp-1") is None

        all_rows = repo.get_opportunities(conn, status="EXPIRED")
        assert len(all_rows) == 1
        assert all_rows[0]["closed_at"] is not None
    finally:
        close_db()


def test_expired_opportunity_recurrence_creates_new_row_not_a_revival(monkeypatch, tmp_path):
    """Correction 4B: a finding that recurs with the same fingerprint after
    its previous row has EXPIRED must get a brand-new opportunity_uid — the
    old EXPIRED row must never be revived/touched again, and both rows must
    stay independently queryable (full history, not silently collapsed).
    """
    db_path = str(tmp_path / "expired_recurrence.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        finding = _make_finding(primary_timeframe="5m")
        fingerprint = compute_fingerprint(finding)

        # 1. Inserted.
        is_new_1 = _record_finding(conn, finding, "2026-01-01T00:00:00Z")
        assert is_new_1 is True
        first_uid = repo.get_active_opportunity_by_fingerprint(conn, fingerprint)["opportunity_uid"]

        # 2. Touched while still ACTIVE (re-confirmation, not a new row).
        is_new_touch = _record_finding(conn, finding, "2026-01-01T00:05:00Z")
        assert is_new_touch is False
        touched = repo.get_active_opportunity_by_fingerprint(conn, fingerprint)
        assert touched["opportunity_uid"] == first_uid
        assert touched["occurrence_count"] == 2

        # 3. Row expires (not re-confirmed within the timeframe's window).
        expired_count = repo.expire_stale_opportunities(
            conn, primary_timeframe="5m", cutoff_iso="2026-01-01T00:10:00Z"
        )
        assert expired_count == 1
        assert repo.get_active_opportunity_by_fingerprint(conn, fingerprint) is None

        # 4. Same fingerprint recurs later -> a NEW row, old EXPIRED row untouched.
        is_new_2 = _record_finding(conn, finding, "2026-02-01T00:00:00Z")
        assert is_new_2 is True
        second_active = repo.get_active_opportunity_by_fingerprint(conn, fingerprint)
        second_uid = second_active["opportunity_uid"]

        assert second_uid != first_uid
        assert second_active["occurrence_count"] == 1
        assert second_active["first_detected_at"] == "2026-02-01T00:00:00Z"

        # Old row was not revived: still EXPIRED with its original values.
        expired_rows = repo.get_opportunities(conn, status="EXPIRED")
        assert len(expired_rows) == 1
        assert expired_rows[0]["opportunity_uid"] == first_uid
        assert expired_rows[0]["occurrence_count"] == 2
        assert expired_rows[0]["last_seen_at"] == "2026-01-01T00:05:00Z"

        # Both rows share the same fingerprint but are distinct opportunity_uids,
        # and full history remains queryable.
        all_rows = repo.get_opportunities(conn)
        assert len(all_rows) == 2
        uids = {r["opportunity_uid"] for r in all_rows}
        assert uids == {first_uid, second_uid}
        fingerprints = {r["fingerprint"] for r in all_rows}
        assert fingerprints == {fingerprint}
    finally:
        close_db()


# ---------------------------- VOLATILITY_COMPRESSION direction-neutral identity

def test_compression_long_then_short_then_long_shares_fingerprint_and_uid(monkeypatch, tmp_path):
    """A real LONG -> SHORT -> LONG continuation of the same compression
    episode (real detector output, only the trailing candles differ between
    calls — see _compression_direction_reversal_stages) must collapse into
    ONE ACTIVE opportunity: identical fingerprint throughout, one
    opportunity_uid, and occurrence_count advancing with each
    reconfirmation, exactly like a same-direction reconfirmation would.
    """
    db_path = str(tmp_path / "compression_direction_flip.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        context_15m = _downtrend_context_15m_df()
        stage_long, stage_short, stage_long_again = _compression_direction_reversal_stages()

        f_long = detect_volatility_compression("BTC", "5m", stage_long, context_df_15m=context_15m)[0]
        f_short = detect_volatility_compression("BTC", "5m", stage_short, context_df_15m=context_15m)[0]
        f_long_again = detect_volatility_compression(
            "BTC", "5m", stage_long_again, context_df_15m=context_15m
        )[0]

        assert f_long.direction == "LONG"
        assert f_short.direction == "SHORT"
        assert f_long_again.direction == "LONG"
        fp = compute_fingerprint(f_long)
        assert compute_fingerprint(f_short) == fp
        assert compute_fingerprint(f_long_again) == fp

        is_new_1 = _record_finding(conn, f_long, "2026-01-01T00:00:00Z")
        is_new_2 = _record_finding(conn, f_short, "2026-01-01T00:05:00Z")
        is_new_3 = _record_finding(conn, f_long_again, "2026-01-01T00:10:00Z")

        assert (is_new_1, is_new_2, is_new_3) == (True, False, False)

        rows = repo.get_opportunities(conn)
        assert len(rows) == 1
        row = rows[0]
        assert row["fingerprint"] == fp
        assert row["occurrence_count"] == 3
        assert row["first_detected_at"] == "2026-01-01T00:00:00Z"
        assert row["last_seen_at"] == "2026-01-01T00:10:00Z"
    finally:
        close_db()


def test_compression_reconfirmation_cannot_overwrite_initial_snapshot(monkeypatch, tmp_path):
    """Explicit snapshot-immutability check: after the SHORT reconfirmation
    (a real, differently-shaped finding — later source candle, different
    anchor, different measurements, and a different warnings_json than the
    initial LONG finding, since the fixed downtrend 15m context now
    conflicts with SHORT but conflicted with the initial LONG), every
    first-detection field must still equal the ORIGINAL LONG finding's
    values, not the SHORT finding's.
    """
    db_path = str(tmp_path / "compression_snapshot_immutable.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        context_15m = _downtrend_context_15m_df()
        stage_long, stage_short, _ = _compression_direction_reversal_stages()

        f_long = detect_volatility_compression("BTC", "5m", stage_long, context_df_15m=context_15m)[0]
        f_short = detect_volatility_compression("BTC", "5m", stage_short, context_df_15m=context_15m)[0]

        # Sanity: the two real findings genuinely differ in every field this
        # test asserts stays frozen — otherwise "unchanged after touch"
        # would prove nothing.
        assert f_long.source_candle_open_time != f_short.source_candle_open_time
        assert f_long.anchor_open_time != f_short.anchor_open_time
        assert f_long.anchor_price != f_short.anchor_price
        assert f_long.measurements != f_short.measurements
        assert f_long.warnings != f_short.warnings  # LONG conflicts w/ SHORT context; SHORT does not
        assert f_long.direction != f_short.direction

        _record_finding(conn, f_long, "2026-01-01T00:00:00Z")
        _record_finding(conn, f_short, "2026-01-01T00:05:00Z")

        row = repo.get_opportunities(conn)[0]
        assert row["direction"] == f_long.direction
        assert row["source_candle_open_time"] == f_long.source_candle_open_time
        assert row["source_candle_close_time"] == f_long.source_candle_close_time
        assert row["anchor_price"] == f_long.anchor_price
        assert row["anchor_open_time"] == f_long.anchor_open_time
        assert json.loads(row["evidence_json"]) == f_long.evidence
        assert json.loads(row["warnings_json"]) == f_long.warnings
        assert json.loads(row["measurements_json"]) == f_long.measurements
        assert row["first_detected_at"] == "2026-01-01T00:00:00Z"

        # Only activity fields moved.
        assert row["last_seen_at"] == "2026-01-01T00:05:00Z"
        assert row["occurrence_count"] == 2
    finally:
        close_db()


def test_compression_symbol_timeframe_episode_key_and_detector_version_remain_distinct(
    monkeypatch, tmp_path
):
    """Removing direction from compression identity must not collapse any
    OTHER identity component: distinct symbol, timeframe, episode key
    (fingerprint_key), or detector_version must each still yield a distinct
    fingerprint — including old vs new compression detector versions, so a
    pre-existing 'vol_compression_v0_1' row is never treated as the same
    identity as a 'vol_compression_v0_1_1' finding for the same episode.
    """
    base = _make_compression_finding()
    variants = {
        "symbol": _make_compression_finding(symbol="ETH"),
        "timeframe": _make_compression_finding(primary_timeframe="3m"),
        "episode_key": _make_compression_finding(fingerprint_key="episode-999"),
        "detector_version": _make_compression_finding(detector_version="vol_compression_v0_1"),
    }
    base_fp = compute_fingerprint(base)
    for name, variant in variants.items():
        assert compute_fingerprint(variant) != base_fp, f"{name} did not change the fingerprint"


def test_compression_expiration_then_opposite_direction_recurrence_creates_new_uid_and_snapshot(
    monkeypatch, tmp_path
):
    """Expiration followed by an opposite-direction recurrence of the same
    episode key must behave like any other post-expiry recurrence
    (Correction 4B): a brand-new opportunity_uid and a fresh first-detection
    snapshot, while the expired row is left completely unchanged — direction
    is not part of compression identity, so the SAME fingerprint recurs
    despite the direction flip, and it is EXPIRED status alone (not
    direction) that determines whether this is a touch or a new row.
    """
    db_path = str(tmp_path / "compression_expiry_reversal.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        finding_long = _make_compression_finding(direction="LONG", measurements={"m": 1})
        fingerprint = compute_fingerprint(finding_long)

        is_new_1 = _record_finding(conn, finding_long, "2026-01-01T00:00:00Z")
        assert is_new_1 is True
        first_uid = repo.get_active_opportunity_by_fingerprint(conn, fingerprint)["opportunity_uid"]

        expired_count = repo.expire_stale_opportunities(
            conn, primary_timeframe="5m", cutoff_iso="2026-01-01T00:10:00Z"
        )
        assert expired_count == 1
        assert repo.get_active_opportunity_by_fingerprint(conn, fingerprint) is None

        finding_short = _make_compression_finding(direction="SHORT", measurements={"m": 2})
        assert compute_fingerprint(finding_short) == fingerprint  # same episode identity, opposite dir

        is_new_2 = _record_finding(conn, finding_short, "2026-02-01T00:00:00Z")
        assert is_new_2 is True
        second_active = repo.get_active_opportunity_by_fingerprint(conn, fingerprint)
        assert second_active["opportunity_uid"] != first_uid
        assert second_active["direction"] == "SHORT"
        assert second_active["occurrence_count"] == 1
        assert second_active["first_detected_at"] == "2026-02-01T00:00:00Z"
        assert json.loads(second_active["measurements_json"]) == {"m": 2}

        expired_rows = repo.get_opportunities(conn, status="EXPIRED")
        assert len(expired_rows) == 1
        assert expired_rows[0]["opportunity_uid"] == first_uid
        assert expired_rows[0]["direction"] == "LONG"
        assert json.loads(expired_rows[0]["measurements_json"]) == {"m": 1}
    finally:
        close_db()


# ------------------------------- SWEEP_RECLAIM identity/reconfirmation contrast

def test_sweep_reclaim_opposite_directions_stay_separate_same_direction_refreshes(
    monkeypatch, tmp_path
):
    """Contrast case confirming SWEEP_RECLAIM is untouched by the
    compression identity change: two findings sharing every field except
    direction must produce two independent ACTIVE rows (direction remains
    structural identity for this family), and re-confirming one of them
    with new measurements must REFRESH measurements_json — unlike
    compression, which freezes it.
    """
    db_path = str(tmp_path / "sweep_direction_contrast.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        finding_long = _make_finding(direction="LONG", fingerprint_key="anchor-A")
        finding_short = _make_finding(direction="SHORT", fingerprint_key="anchor-A")
        assert compute_fingerprint(finding_long) != compute_fingerprint(finding_short)

        _record_finding(conn, finding_long, "2026-01-01T00:00:00Z")
        _record_finding(conn, finding_short, "2026-01-01T00:00:00Z")

        rows = repo.get_opportunities(conn)
        assert len(rows) == 2
        assert {r["direction"] for r in rows} == {"LONG", "SHORT"}

        # Same-direction reconfirmation still refreshes measurements_json.
        refreshed = _make_finding(
            direction="LONG", fingerprint_key="anchor-A", measurements={"x": 999}
        )
        is_new = _record_finding(conn, refreshed, "2026-01-01T00:05:00Z")
        assert is_new is False

        long_row = repo.get_active_opportunity_by_fingerprint(
            conn, compute_fingerprint(finding_long)
        )
        assert long_row["occurrence_count"] == 2
        assert json.loads(long_row["measurements_json"]) == {"x": 999}
    finally:
        close_db()


# ------------------------------------------------------------------ per-symbol failure isolation

class _RaisingOn15mCandleStore:
    """Wraps a real CandleStore, raising only for one symbol's 15m context
    fetch — every other call (other symbols, and this symbol's own 3m/5m
    frames) delegates through untouched.
    """

    def __init__(self, inner: CandleStore, bad_symbol: str):
        self._inner = inner
        self._bad_symbol = bad_symbol

    def get_df(self, symbol: str, timeframe: str, now_ms=None):
        if symbol == self._bad_symbol and timeframe == "15m":
            raise RuntimeError(f"simulated 15m context fetch failure for {symbol}")
        return self._inner.get_df(symbol, timeframe, now_ms=now_ms)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_one_symbols_15m_context_failure_does_not_abort_remaining_symbols(monkeypatch, tmp_path):
    """Correction 2 (original): the 15m context fetch used to sit outside
    the per-symbol try/except, so a single symbol's get_df("...", "15m")
    raising aborted the whole scan (every remaining symbol silently got no
    evaluation at all).

    Context and ranking v0.1 correction: the fix above only isolated
    symbols from EACH OTHER — a failing symbol's own 15m fetch still sat
    before its per-timeframe loop and aborted that ONE symbol's otherwise-
    valid 3m/5m primary detection entirely. The 15m context fetch is now
    isolated in its own try/except per symbol, so AAA_BAD's real 3m/5m
    findings are still detected and recorded — only its context/score
    enrichment degrades (symbol_htf_trend UNAVAILABLE, counted in
    summary.context_errors), it does not disappear.
    """
    db_path = str(tmp_path / "isolation_15m.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        _seed_market(conn, "AAA_BAD")
        _seed_market(conn, "ZZZ_GOOD")
        store = CandleStore(conn)
        for symbol in ("AAA_BAD", "ZZZ_GOOD"):
            _load_into_store(store, symbol, "5m", _sweep_reclaim_df())
            _load_into_store(store, symbol, "3m", _sweep_reclaim_df())

        wrapped = _RaisingOn15mCandleStore(store, bad_symbol="AAA_BAD")

        summary = asyncio.run(run_opportunity_scan(conn, wrapped, settings))

        # The scan itself completed (no exception propagated) and BOTH
        # symbols were still fully evaluated for 3m/5m primary detection.
        assert summary.markets_scanned == 2
        assert summary.findings > 0
        assert summary.context_errors == 1  # exactly AAA_BAD's own 15m fetch failure

        rows = repo.get_opportunities(conn)
        symbols_with_findings = {r["symbol"] for r in rows}
        assert "ZZZ_GOOD" in symbols_with_findings
        assert "AAA_BAD" in symbols_with_findings

        # AAA_BAD's rows still got a context/score snapshot attempt — its
        # symbol_htf_trend degrades to UNAVAILABLE (no 15m data at all,
        # independent of the simulated exception), but total_score is not
        # simply dropped: the other components (primary_structure,
        # btc_alignment) can still be available/scored.
        bad_row = next(r for r in rows if r["symbol"] == "AAA_BAD")
        assert bad_row["context_json"] is not None
        bad_context = json.loads(bad_row["context_json"])
        assert bad_context["symbol_htf_trend"]["status"] == "UNAVAILABLE"

        # The existing APEX signal scanner is a wholly separate code path
        # (scheduler/tasks.py) and is unaffected by this change or failure.
        pushover = PushoverClient(app_token="fake", user_key="fake")
        signal_summary = asyncio.run(run_signal_scan(conn, store, settings, pushover))
        assert signal_summary.markets_scanned == 2
    finally:
        close_db()


# ------------------------------------------------------------------ context COMPUTATION failure containment
#
# Contrast with test_one_symbols_15m_context_failure_does_not_abort_remaining_symbols
# above (which injects a candle *fetch* failure): these inject a failure in
# the pure *computation* itself at each of the four call sites
# (compute_symbol_trend_component for the once-per-scan BTC frame,
# compute_symbol_trend_component for a symbol's own 15m frame,
# compute_primary_structure_component, compute_volatility_component). Each
# must be caught in its own try/except, counted in context_errors, and
# degrade only that one component to a directly-constructed UNAVAILABLE
# fallback — never abort the scan, never drop findings already detected for
# the affected (symbol, timeframe) pair, and never affect later pairs.


def test_btc_trend_computation_failure_is_contained(monkeypatch, tmp_path, caplog):
    db_path = str(tmp_path / "btc_trend_computation_failure.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        _seed_market(conn, "AAA")
        _seed_market(conn, "ZZZ")
        store = CandleStore(conn)
        for symbol in ("AAA", "ZZZ"):
            _load_into_store(store, symbol, "5m", _sweep_reclaim_df())
            _load_into_store(store, symbol, "3m", _sweep_reclaim_df())

        real_compute = opportunity_engine.compute_symbol_trend_component
        call_count = {"n": 0}

        def fake(df, now_ms):
            call_count["n"] += 1
            if call_count["n"] == 1:  # the once-per-scan BTC frame, before any symbol
                raise RuntimeError("simulated BTC trend computation failure")
            return real_compute(df, now_ms)

        monkeypatch.setattr(opportunity_engine, "compute_symbol_trend_component", fake)

        with caplog.at_level("ERROR", logger="apex.opportunity.engine"):
            summary = asyncio.run(run_opportunity_scan(conn, store, settings))

        assert summary.context_errors == 1
        assert summary.findings > 0

        rows = repo.get_opportunities(conn)
        # Real findings survive for BOTH symbols' BOTH timeframes — the
        # once-per-scan BTC-trend failure happens before any symbol is
        # evaluated, so it must never suppress any (symbol, timeframe) pair.
        by_symbol_tf = {(r["symbol"], r["primary_timeframe"]) for r in rows}
        assert by_symbol_tf == {("AAA", "3m"), ("AAA", "5m"), ("ZZZ", "3m"), ("ZZZ", "5m")}

        for row in rows:
            context = json.loads(row["context_json"])
            assert context["btc_htf_trend"]["status"] == "UNAVAILABLE"
            assert context["btc_htf_trend"]["reason"] == "COMPUTATION_FAILED"

        error_records = [r for r in caplog.records if r.levelname == "ERROR"]
        assert any("BTC" in r.getMessage() for r in error_records)
    finally:
        close_db()


def test_symbol_trend_computation_failure_is_contained_to_one_symbol(monkeypatch, tmp_path, caplog):
    db_path = str(tmp_path / "symbol_trend_computation_failure.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        _seed_market(conn, "AAA")
        _seed_market(conn, "ZZZ")
        store = CandleStore(conn)
        for symbol in ("AAA", "ZZZ"):
            _load_into_store(store, symbol, "5m", _sweep_reclaim_df())
            _load_into_store(store, symbol, "3m", _sweep_reclaim_df())

        real_compute = opportunity_engine.compute_symbol_trend_component
        call_count = {"n": 0}

        def fake(df, now_ms):
            call_count["n"] += 1
            # Call 1 is the once-per-scan BTC frame (no BTC market seeded
            # here, so it must not raise); call 2 is the FIRST per-symbol
            # own-trend computation — exactly one symbol's.
            if call_count["n"] == 2:
                raise RuntimeError("simulated symbol trend computation failure")
            return real_compute(df, now_ms)

        monkeypatch.setattr(opportunity_engine, "compute_symbol_trend_component", fake)

        with caplog.at_level("ERROR", logger="apex.opportunity.engine"):
            summary = asyncio.run(run_opportunity_scan(conn, store, settings))

        assert summary.context_errors == 1
        assert summary.findings > 0

        rows = repo.get_opportunities(conn)
        by_symbol_tf = {(r["symbol"], r["primary_timeframe"]) for r in rows}
        # Both symbols' 3m AND 5m findings survive — including BOTH
        # timeframes of the ONE symbol whose own-trend computation failed,
        # since that failure happens once per symbol, before its 3m/5m loop.
        assert by_symbol_tf == {("AAA", "3m"), ("AAA", "5m"), ("ZZZ", "3m"), ("ZZZ", "5m")}

        contexts_by_symbol: dict[str, list[dict]] = {"AAA": [], "ZZZ": []}
        for row in rows:
            contexts_by_symbol[row["symbol"]].append(json.loads(row["context_json"]))

        failed_symbols = {
            symbol for symbol, ctxs in contexts_by_symbol.items()
            if all(c["symbol_htf_trend"]["reason"] == "COMPUTATION_FAILED" for c in ctxs)
        }
        assert len(failed_symbols) == 1  # exactly one symbol hit the failure
        other_symbol = ({"AAA", "ZZZ"} - failed_symbols).pop()
        assert all(
            c["symbol_htf_trend"]["reason"] != "COMPUTATION_FAILED"
            for c in contexts_by_symbol[other_symbol]
        )

        error_records = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(error_records) == 1
    finally:
        close_db()


def test_primary_structure_computation_failure_is_contained_and_findings_still_recorded(
    monkeypatch, tmp_path, caplog
):
    db_path = str(tmp_path / "primary_structure_computation_failure.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        _seed_market(conn, "AAA")
        _seed_market(conn, "ZZZ")
        store = CandleStore(conn)
        for symbol in ("AAA", "ZZZ"):
            _load_into_store(store, symbol, "5m", _sweep_reclaim_df())
            _load_into_store(store, symbol, "3m", _sweep_reclaim_df())

        real_compute = opportunity_engine.compute_primary_structure_component
        call_count = {"n": 0}

        def fake(df, timeframe, now_ms):
            call_count["n"] += 1
            if call_count["n"] == 1:  # the first (symbol, timeframe) pair with findings
                raise RuntimeError("simulated primary structure computation failure")
            return real_compute(df, timeframe, now_ms)

        monkeypatch.setattr(opportunity_engine, "compute_primary_structure_component", fake)

        with caplog.at_level("ERROR", logger="apex.opportunity.engine"):
            summary = asyncio.run(run_opportunity_scan(conn, store, settings))

        assert summary.context_errors == 1
        assert summary.findings > 0

        rows = repo.get_opportunities(conn)
        by_symbol_tf = {(r["symbol"], r["primary_timeframe"]) for r in rows}
        # The already-detected findings for the AFFECTED (symbol, timeframe)
        # pair are still recorded (not discarded by the enrichment failure),
        # and every later pair is unaffected.
        assert by_symbol_tf == {("AAA", "3m"), ("AAA", "5m"), ("ZZZ", "3m"), ("ZZZ", "5m")}

        reasons = {
            (row["symbol"], row["primary_timeframe"]):
                json.loads(row["context_json"])["primary_structure"]["reason"]
            for row in rows
        }
        failed_pairs = [k for k, reason in reasons.items() if reason == "COMPUTATION_FAILED"]
        assert len(failed_pairs) == 1

        error_records = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(error_records) == 1
        assert "primary structure" in error_records[0].getMessage()
    finally:
        close_db()


def test_volatility_computation_failure_is_contained_and_findings_still_recorded(
    monkeypatch, tmp_path, caplog
):
    db_path = str(tmp_path / "volatility_computation_failure.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        _seed_market(conn, "AAA")
        _seed_market(conn, "ZZZ")
        store = CandleStore(conn)
        for symbol in ("AAA", "ZZZ"):
            _load_into_store(store, symbol, "5m", _sweep_reclaim_df())
            _load_into_store(store, symbol, "3m", _sweep_reclaim_df())

        real_compute = opportunity_engine.compute_volatility_component
        call_count = {"n": 0}

        def fake(df, timeframe, now_ms):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated volatility computation failure")
            return real_compute(df, timeframe, now_ms)

        monkeypatch.setattr(opportunity_engine, "compute_volatility_component", fake)

        with caplog.at_level("ERROR", logger="apex.opportunity.engine"):
            summary = asyncio.run(run_opportunity_scan(conn, store, settings))

        assert summary.context_errors == 1
        assert summary.findings > 0

        rows = repo.get_opportunities(conn)
        by_symbol_tf = {(r["symbol"], r["primary_timeframe"]) for r in rows}
        assert by_symbol_tf == {("AAA", "3m"), ("AAA", "5m"), ("ZZZ", "3m"), ("ZZZ", "5m")}

        reasons = {
            (row["symbol"], row["primary_timeframe"]):
                json.loads(row["context_json"])["volatility"]["reason"]
            for row in rows
        }
        failed_pairs = [k for k, reason in reasons.items() if reason == "COMPUTATION_FAILED"]
        assert len(failed_pairs) == 1

        error_records = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(error_records) == 1
        assert "volatility" in error_records[0].getMessage()
    finally:
        close_db()


# ------------------------------------------------------------------ single timestamp authority

class _CapturingCandleStore:
    """Wraps a real CandleStore, recording every get_df() call's
    (symbol, timeframe, now_ms) so a test can assert the scan threads one
    consistent cutoff through every 3m/5m primary + 15m context read.
    """

    def __init__(self, inner: CandleStore):
        self._inner = inner
        self.calls: list[tuple[str, str, object]] = []

    def get_df(self, symbol: str, timeframe: str, now_ms=None):
        self.calls.append((symbol, timeframe, now_ms))
        return self._inner.get_df(symbol, timeframe, now_ms=now_ms)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _AdvancingClock:
    """A fake time.time() returning a strictly increasing sequence that
    crosses a whole-second boundary between any two successive calls — so a
    regression that re-reads the wall clock more than once per scan would
    observe a later second on its second read than its first.
    """

    def __init__(self, start: float, step: float):
        self._next = start
        self._step = step
        self.call_count = 0

    def __call__(self) -> float:
        self.call_count += 1
        t = self._next
        self._next += self._step
        return t


def test_single_clock_read_drives_every_cutoff_across_a_second_boundary(monkeypatch, tmp_path):
    """Correction 1: the scan must read the wall clock exactly once and
    derive both first_detected_at/last_seen_at and every candle-eligibility
    now_ms from that single instant, never a second/later independent read —
    even across a whole-second boundary. Every get_df call (3m, 5m primary +
    15m context) must use the identical now_ms, and no persisted
    opportunity's first_detected_at may be derived from an instant that
    postdates the one that gated the candles it is based on.
    """
    db_path = str(tmp_path / "single_clock.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        _seed_market(conn, "BTC")
        store = CandleStore(conn)
        _load_into_store(store, "BTC", "5m", _sweep_reclaim_df())
        _load_into_store(store, "BTC", "3m", _sweep_reclaim_df())
        _load_into_store(store, "BTC", "15m", _sweep_reclaim_df())
        capturing = _CapturingCandleStore(store)

        # Crosses a whole-second boundary between any two successive reads.
        clock = _AdvancingClock(start=1_700_000_000.900, step=0.300)
        monkeypatch.setattr(opportunity_engine.time, "time", clock)

        summary = asyncio.run(run_opportunity_scan(conn, capturing, settings))
        assert summary.findings > 0  # sanity: a real finding was recorded

        # The patched clock was actually exercised (proves the patch took
        # effect; is_stale() legitimately reads it too and is out of scope
        # for this correction, so this is a floor, not an exact count).
        assert clock.call_count >= 1

        # Every get_df call (3m, 5m primary + 15m context) used the identical
        # now_ms — not independently re-derived per symbol/timeframe.
        now_ms_values = {c[2] for c in capturing.calls}
        assert len(now_ms_values) == 1
        (now_ms,) = now_ms_values
        assert {(c[0], c[1]) for c in capturing.calls} == {
            ("BTC", "15m"), ("BTC", "3m"), ("BTC", "5m"),
        }

        # first_detected_at is derived from that same now_ms — which is
        # itself already whole-second-floored before being used as any
        # candle-eligibility cutoff (see run_opportunity_scan) — not an
        # independent later read (e.g. a reintroduced separate
        # datetime.now()-based utcnow_iso() call would diverge wildly from
        # the patched clock here, since it draws from the real system clock
        # instead).
        rows = repo.get_opportunities(conn)
        assert len(rows) >= 1
        for row in rows:
            observed_ms = int(utc_from_iso(row["first_detected_at"]).timestamp() * 1000)
            assert observed_ms == now_ms
            # The source candle this finding is based on cannot postdate the
            # persisted first-detection timestamp.
            assert row["source_candle_close_time"] <= observed_ms
    finally:
        close_db()


def test_whole_second_floor_withholds_fractional_second_boundary_candle(monkeypatch, tmp_path):
    """Correction A: now_ms is deliberately floored to the whole second
    BEFORE being used as the candle-eligibility cutoff (not just for the
    persisted ISO stamp). Without that floor, a candle whose close boundary
    falls at h:m:s.500 would already be visible to a scan whose real wall
    clock reads h:m:s.900 within the same second — even though the
    persisted whole-second first_detected_at (h:m:s.000) cannot represent an
    instant later than itself. This uses a real CandleStore with >= 60
    synthetic eligible history candles plus one extra, non-second-aligned
    boundary candle, and patched (not real) detectors so the assertion is
    about exactly which candles reached the detector, not about detector
    internals.
    """
    THREE_MIN_MS = 3 * 60_000
    db_path = str(tmp_path / "fractional_boundary.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        _seed_market(conn, "BTC")
        store = CandleStore(conn)

        second_start_s = 1_700_000_000
        SECOND_MS = second_start_s * 1000

        # >= MIN_CANDLES_REQUIRED history of closed, non-second-aligned 3m
        # candles, all comfortably closed before the second boundary.
        n_history = opportunity_engine.MIN_CANDLES_REQUIRED
        base_open = SECOND_MS - (n_history + 1) * THREE_MIN_MS + 137
        for i in range(n_history):
            open_time = base_open + i * THREE_MIN_MS
            store.update(
                "BTC", "3m",
                {
                    "t": open_time, "T": open_time + THREE_MIN_MS - 1,
                    "o": "100", "h": "101", "l": "99", "c": "100.5", "v": "10",
                },
                persist=False,
                now_ms=SECOND_MS,
            )

        # One extra candle whose duration-derived close boundary lands at
        # SECOND_MS + 500 — after the floored cutoff (SECOND_MS) but before
        # the real wall clock this test patches below (SECOND_MS + 900).
        boundary_open_time = SECOND_MS - THREE_MIN_MS + 500
        store.update(
            "BTC", "3m",
            {
                "t": boundary_open_time, "T": boundary_open_time + THREE_MIN_MS - 1,
                "o": "100", "h": "101", "l": "99", "c": "999", "v": "10",
            },
            persist=False,
            now_ms=SECOND_MS + 900,  # already past its own boundary in real time
        )

        captured_dfs: dict[str, pd.DataFrame] = {}

        def fake_compression(symbol, timeframe, df, context_df_15m=None):
            captured_dfs[timeframe] = df
            last_open_time = int(df.iloc[-1]["open_time"])
            return [_make_compression_finding(
                symbol=symbol,
                primary_timeframe=timeframe,
                source_candle_open_time=last_open_time,
                source_candle_close_time=last_open_time + THREE_MIN_MS,
                fingerprint_key=f"boundary-{last_open_time}",
            )]

        def fake_sweep(symbol, timeframe, df, context_df_15m=None):
            return []

        monkeypatch.setattr(opportunity_engine, "detect_volatility_compression", fake_compression)
        monkeypatch.setattr(opportunity_engine, "detect_sweep_reclaim", fake_sweep)

        # Real wall clock reads second_start_s + 0.900; the whole-second
        # cutoff must therefore be SECOND_MS, not SECOND_MS + 900.
        monkeypatch.setattr(opportunity_engine.time, "time", lambda: second_start_s + 0.900)

        summary = asyncio.run(run_opportunity_scan(conn, store, settings))

        # Detector calls actually happened with real, non-trivial input —
        # not a swallowed engine exception silently producing zero findings.
        assert summary.findings > 0
        assert "3m" in captured_dfs
        df_3m = captured_dfs["3m"]
        assert len(df_3m) >= opportunity_engine.MIN_CANDLES_REQUIRED

        # The fractional-second boundary candle must be absent: its close
        # boundary (SECOND_MS + 500) is later than the floored cutoff
        # (SECOND_MS) even though it is earlier than the real wall clock
        # (SECOND_MS + 900).
        assert boundary_open_time not in set(df_3m["open_time"])

        rows = repo.get_opportunities(conn)
        assert len(rows) >= 1
        for row in rows:
            observed_ms = int(utc_from_iso(row["first_detected_at"]).timestamp() * 1000)
            assert observed_ms == SECOND_MS
            assert row["source_candle_close_time"] <= observed_ms
    finally:
        close_db()


# ------------------------------------------------------------------ non-interference with the existing signal path

def test_no_writes_to_other_tables(monkeypatch, tmp_path):
    db_path = str(tmp_path / "isolation.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        _seed_market(conn, "BTC")
        store = CandleStore(conn)
        _load_into_store(store, "BTC", "5m", _sweep_reclaim_df())
        _load_into_store(store, "BTC", "3m", _sweep_reclaim_df())

        before = _other_table_counts(conn)
        summary = asyncio.run(run_opportunity_scan(conn, store, settings))
        after = _other_table_counts(conn)

        assert summary.findings > 0  # sanity: the engine actually did something
        assert before == after
        assert all(v == 0 for v in after.values())
    finally:
        close_db()


def _confirmed_trend_pullback_15m_df() -> pd.DataFrame:
    """50 candles of a clean uptrend — deterministically yields LONG trend
    bias (EMA9 > EMA21, close above VWAP) with the project's default
    indicator settings (ema_fast=9, ema_slow=21, vwap_lookback_candles=96).
    """
    closes = [100 + i for i in range(50)]
    return _make_df(closes, [c * 1.003 for c in closes], [c * 0.997 for c in closes], closes)


def _confirmed_trend_pullback_5m_df() -> pd.DataFrame:
    """52 candles — an uptrend leg, a pullback leg, then a small reclaim —
    deterministically yields a CONFIRMED LONG TREND_PULLBACK (RSI turning up
    out of the pullback zone while price reclaims VWAP) under the project's
    default indicator settings. Empirically pinned; see Correction 4A.
    """
    up = [100 + i * 0.5 for i in range(30)]
    down = [up[-1] - i * 0.3 for i in range(1, 21)]
    lastup = [down[-1] + 0.8 * 0.3, down[-1] + 0.8]
    closes = up + down + lastup
    return _make_df(closes, [c * 1.003 for c in closes], [c * 0.997 for c in closes], closes)


def test_existing_signal_path_unchanged_by_opportunity_scan(monkeypatch, tmp_path):
    """Exercises a real, non-suppressed TREND_PULLBACK CONFIRMED_SETUP through
    run_signal_scan — reaching evaluate_pullback, _record_observation,
    _capture_signal_features, can_send_alert, and _send_alert (blocked only
    by the ALERTS_ENABLED=false gate, as dry-run safety requires) — and
    proves running the opportunity engine in between changes none of it.

    An earlier version of this test used flat candles, which short-circuit
    at the trend-bias gate in evaluate_symbol and never reach any of the
    above; that could not actually prove non-interference with the parts of
    the existing signal path most worth protecting.
    """
    db_path = str(tmp_path / "signal_path.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        _seed_market(conn, "BTC")
        store = CandleStore(conn)
        _load_into_store(store, "BTC", "15m", _confirmed_trend_pullback_15m_df())
        _load_into_store(store, "BTC", "5m", _confirmed_trend_pullback_5m_df())
        # 3m: real SWEEP_RECLAIM shape so the opportunity scan does real work
        # in between, not a silent no-op.
        _load_into_store(store, "BTC", "3m", _sweep_reclaim_df())

        pushover = PushoverClient(app_token="fake", user_key="fake")

        summary_before = asyncio.run(run_signal_scan(conn, store, settings, pushover))
        counts_before_opp = _other_table_counts(conn)

        opp_summary = asyncio.run(run_opportunity_scan(conn, store, settings))
        counts_after_opp = _other_table_counts(conn)

        summary_after = asyncio.run(run_signal_scan(conn, store, settings, pushover))

        # Sanity: the opportunity scan actually ran (not a disabled/no-op path).
        assert opp_summary.markets_scanned == 1

        # The TREND_PULLBACK candidate is real and reaches every downstream
        # stage, but ALERTS_ENABLED=false means it is never actually sent.
        assert summary_before.markets_scanned == summary_after.markets_scanned == 1
        assert summary_before.no_signal == summary_after.no_signal == 0
        assert summary_before.candidates == summary_after.candidates == 1
        assert summary_before.suppressed_disabled == summary_after.suppressed_disabled == 1
        assert summary_before.sent == summary_after.sent == 0

        # The opportunity scan running in between must not touch any of the
        # existing-signal-path tables it observed before it ran.
        assert counts_before_opp == counts_after_opp

        # _record_observation + _capture_signal_features were reached exactly
        # once (the second run_signal_scan call dedupes against the still-open
        # observation rather than inserting again).
        assert counts_after_opp["signal_observations"] == 1
        assert counts_after_opp["signal_features"] == 1
        assert counts_after_opp["alerts"] == 0
    finally:
        close_db()


# ------------------------------------------------------------------ SUPPORT_RESISTANCE detector wiring


def test_all_five_detectors_called_once_in_fixed_order_with_same_df(monkeypatch, tmp_path):
    """The complete fixed detector order is: volatility compression,
    sweep/reclaim, S/R rejection, S/R breakout retest, S/R failed breakout.
    All five must receive the identical already-fetched `df` object for a
    given symbol/timeframe (not a fresh copy per detector); only the first
    two receive `context_df_15m` — the three S/R detectors must be called
    with no such keyword at all.
    """
    db_path = str(tmp_path / "five_detectors_order.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        _seed_market(conn, "BTC")
        store = CandleStore(conn)
        _load_into_store(store, "BTC", "5m", _sweep_reclaim_df())
        _load_into_store(store, "BTC", "3m", _sweep_reclaim_df())
        _load_into_store(store, "BTC", "15m", _sweep_reclaim_df())

        calls: list[tuple[str, str, str]] = []
        seen_dfs: dict[str, pd.DataFrame] = {}

        def _make_context_fake(name):
            def fake(symbol, timeframe, df, context_df_15m=None):
                calls.append((name, symbol, timeframe))
                seen_dfs.setdefault(timeframe, df)
                assert df is seen_dfs[timeframe]
                assert context_df_15m is not None
                return []

            return fake

        def _make_no_context_fake(name):
            def fake(symbol, timeframe, df, **kwargs):
                calls.append((name, symbol, timeframe))
                seen_dfs.setdefault(timeframe, df)
                assert df is seen_dfs[timeframe]
                assert kwargs == {}
                return []

            return fake

        monkeypatch.setattr(
            opportunity_engine, "detect_volatility_compression",
            _make_context_fake("volatility_compression"),
        )
        monkeypatch.setattr(
            opportunity_engine, "detect_sweep_reclaim", _make_context_fake("sweep_reclaim")
        )
        monkeypatch.setattr(
            opportunity_engine, "detect_support_resistance_rejection",
            _make_no_context_fake("support_resistance_rejection"),
        )
        monkeypatch.setattr(
            opportunity_engine, "detect_support_resistance_breakout_retest",
            _make_no_context_fake("support_resistance_breakout_retest"),
        )
        monkeypatch.setattr(
            opportunity_engine, "detect_support_resistance_failed_breakout",
            _make_no_context_fake("support_resistance_failed_breakout"),
        )

        summary = asyncio.run(run_opportunity_scan(conn, store, settings))

        assert summary.detector_errors == 0
        expected_order = [
            "volatility_compression",
            "sweep_reclaim",
            "support_resistance_rejection",
            "support_resistance_breakout_retest",
            "support_resistance_failed_breakout",
        ]
        for timeframe in ("3m", "5m"):
            tf_calls = [c for c in calls if c[2] == timeframe]
            assert [c[0] for c in tf_calls] == expected_order
            assert all(c[1] == "BTC" for c in tf_calls)
    finally:
        close_db()


def test_all_three_support_resistance_families_persist_end_to_end_with_exact_family_values(
    monkeypatch, tmp_path
):
    db_path = str(tmp_path / "sr_persist.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        _seed_market(conn, "BTC")
        store = CandleStore(conn)
        _load_into_store(store, "BTC", "5m", _sweep_reclaim_df())
        _load_into_store(store, "BTC", "3m", _sweep_reclaim_df())
        _load_into_store(store, "BTC", "15m", _sweep_reclaim_df())

        rejection_finding = _make_finding(
            setup_family="SUPPORT_RESISTANCE_REJECTION",
            detector_version="support_resistance_rejection_v0_1",
            fingerprint_key="rejection-1",
            direction="SHORT",
            anchor_price=105.0,
            evidence={"zone_lower": 104.5, "zone_upper": 105.5},
        )
        breakout_retest_finding = _make_finding(
            setup_family="SUPPORT_RESISTANCE_BREAKOUT_RETEST",
            detector_version="support_resistance_breakout_retest_v0_1",
            fingerprint_key="breakout-retest-1",
            direction="LONG",
        )
        failed_breakout_finding = _make_finding(
            setup_family="SUPPORT_RESISTANCE_FAILED_BREAKOUT",
            detector_version="support_resistance_failed_breakout_v0_1",
            fingerprint_key="failed-breakout-1",
            direction="SHORT",
        )

        monkeypatch.setattr(opportunity_engine, "detect_volatility_compression", lambda *a, **k: [])
        monkeypatch.setattr(opportunity_engine, "detect_sweep_reclaim", lambda *a, **k: [])
        monkeypatch.setattr(
            opportunity_engine, "detect_support_resistance_rejection",
            lambda *a, **k: [rejection_finding],
        )
        monkeypatch.setattr(
            opportunity_engine, "detect_support_resistance_breakout_retest",
            lambda *a, **k: [breakout_retest_finding],
        )
        monkeypatch.setattr(
            opportunity_engine, "detect_support_resistance_failed_breakout",
            lambda *a, **k: [failed_breakout_finding],
        )

        summary = asyncio.run(run_opportunity_scan(conn, store, settings))
        assert summary.detector_errors == 0

        # Each fake always returns the identical finding object regardless of
        # which timeframe invoked it, so the 3m and 5m calls share one
        # fingerprint (primary_timeframe is a field of the finding, not of
        # the call) and collapse into a single re-confirmed row per family —
        # exactly three rows, one per S/R family.
        rows = repo.get_opportunities(conn)
        assert len(rows) == 3
        families = {r["setup_family"] for r in rows}
        assert families == {
            "SUPPORT_RESISTANCE_REJECTION",
            "SUPPORT_RESISTANCE_BREAKOUT_RETEST",
            "SUPPORT_RESISTANCE_FAILED_BREAKOUT",
        }

        rejection_row = next(r for r in rows if r["setup_family"] == "SUPPORT_RESISTANCE_REJECTION")
        assert rejection_row["direction"] == "SHORT"
        assert rejection_row["detector_version"] == "support_resistance_rejection_v0_1"
        assert rejection_row["anchor_price"] == 105.0
        assert json.loads(rejection_row["evidence_json"]) == {
            "zone_lower": 104.5, "zone_upper": 105.5,
        }
        assert rejection_row["occurrence_count"] == 2  # touched once more (3m then 5m)
    finally:
        close_db()


def test_one_detector_exception_increments_counter_and_does_not_suppress_others(
    monkeypatch, tmp_path, caplog
):
    """A single SUPPORT_RESISTANCE_REJECTION exception must be isolated at
    detector/symbol/timeframe granularity: it must not prevent the other
    four detectors from running for that same timeframe, nor prevent the
    next timeframe (or the outer per-symbol scan) from completing.
    """
    db_path = str(tmp_path / "detector_error.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        _seed_market(conn, "BTC")
        store = CandleStore(conn)
        _load_into_store(store, "BTC", "5m", _sweep_reclaim_df())
        _load_into_store(store, "BTC", "3m", _sweep_reclaim_df())
        _load_into_store(store, "BTC", "15m", _sweep_reclaim_df())

        other_calls: list[tuple[str, str]] = []

        def _raiser(symbol, timeframe, df, **kwargs):
            raise RuntimeError(f"simulated failure for {symbol}/{timeframe}")

        def _recorder(name):
            def fake(symbol, timeframe, df, **kwargs):
                other_calls.append((name, timeframe))
                return []

            return fake

        monkeypatch.setattr(
            opportunity_engine, "detect_support_resistance_rejection", _raiser
        )
        monkeypatch.setattr(
            opportunity_engine, "detect_volatility_compression",
            _recorder("volatility_compression"),
        )
        monkeypatch.setattr(
            opportunity_engine, "detect_sweep_reclaim", _recorder("sweep_reclaim")
        )
        monkeypatch.setattr(
            opportunity_engine, "detect_support_resistance_breakout_retest",
            _recorder("support_resistance_breakout_retest"),
        )
        monkeypatch.setattr(
            opportunity_engine, "detect_support_resistance_failed_breakout",
            _recorder("support_resistance_failed_breakout"),
        )

        with caplog.at_level("ERROR", logger="apex.opportunity.engine"):
            summary = asyncio.run(run_opportunity_scan(conn, store, settings))

        # One raise per eligible timeframe (3m and 5m both qualify).
        assert summary.detector_errors == 2
        assert summary.markets_scanned == 1
        assert summary.timeframe_pairs_scanned == 2

        for name in (
            "volatility_compression",
            "sweep_reclaim",
            "support_resistance_breakout_retest",
            "support_resistance_failed_breakout",
        ):
            assert {tf for (n, tf) in other_calls if n == name} == {"3m", "5m"}, name

        error_records = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(error_records) == 2
        for record in error_records:
            message = record.getMessage()
            assert "support_resistance_rejection" in message
            assert "BTC" in message
        logged_timeframes = {
            tf for r in error_records for tf in ("3m", "5m") if tf in r.getMessage()
        }
        assert logged_timeframes == {"3m", "5m"}
    finally:
        close_db()


# ==================================================================
# Context and ranking v0.1
# ==================================================================
#
# _record_finding-level tests below construct ContextInputs directly
# (hand-built context.py component dataclasses) rather than through real
# candle data — engine.py's own responsibility here is correctly wiring
# context_inputs through to assemble_context/score_opportunity and
# persisting/freezing the result, which is exactly what these prove;
# context.py's OWN component-computation correctness (real candle data,
# pivots, trend bias, ATR%) is covered by tests/test_opportunity_context.py,
# and scoring.py's own arithmetic by tests/test_opportunity_scoring.py.


def _trend_component(status="AVAILABLE", direction="LONG") -> TrendComponent:
    return TrendComponent(
        status=status, direction=direction, reason="test", sample_count=30,
        latest_close_boundary_ms=1000,
    )


def _structure_component(status="AVAILABLE", direction="LONG") -> StructureComponent:
    return StructureComponent(
        status=status, direction=direction, reason="test", sample_count=30,
        latest_close_boundary_ms=1000,
    )


def _volatility_component(status="AVAILABLE", atr_percent=1.5) -> VolatilityComponent:
    return VolatilityComponent(
        status=status, atr_percent=atr_percent, reason="test", sample_count=30,
        latest_close_boundary_ms=1000,
    )


def _context_inputs(**overrides) -> ContextInputs:
    base = dict(
        as_of_ms=1_700_000_000_000,
        primary_timeframe="5m",
        symbol_htf_trend=_trend_component(),
        primary_structure=_structure_component(),
        btc_htf_trend=_trend_component(),
        volatility=_volatility_component(),
    )
    base.update(overrides)
    return ContextInputs(**base)


# ------------------------------------------------------------------ _record_finding score/context wiring


def test_new_finding_with_context_inputs_persists_populated_score_snapshot(monkeypatch, tmp_path):
    db_path = str(tmp_path / "score_snapshot_new.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        finding = _make_finding(direction="LONG")
        context_inputs = _context_inputs(
            symbol_htf_trend=_trend_component(direction="LONG"),
            primary_structure=_structure_component(direction="LONG"),
            btc_htf_trend=_trend_component(direction="LONG"),
        )
        is_new = _record_finding(conn, finding, "2026-01-01T00:00:00Z", context_inputs=context_inputs)
        assert is_new is True

        row = repo.get_opportunities(conn)[0]
        assert row["context_json"] is not None
        assert row["component_scores_json"] is not None
        assert row["score_version"] == SCORE_VERSION
        assert row["total_score"] == 100.0  # 50 base + 25 + 15 + 10, all aligned
        assert json.loads(row["score_warnings_json"]) == []

        context = json.loads(row["context_json"])
        assert context["scored_direction"] == "LONG"
        assert context["symbol_htf_trend"]["direction"] == "LONG"
    finally:
        close_db()


def test_record_finding_without_context_inputs_leaves_score_fields_null_with_no_warning(
    monkeypatch, tmp_path
):
    db_path = str(tmp_path / "score_snapshot_no_enrichment.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        finding = _make_finding()
        is_new = _record_finding(conn, finding, "2026-01-01T00:00:00Z")  # no context_inputs at all
        assert is_new is True

        row = repo.get_opportunities(conn)[0]
        assert row["context_json"] is None
        assert row["component_scores_json"] is None
        assert row["total_score"] is None
        assert row["score_version"] is None
        assert row["score_warnings_json"] is None
    finally:
        close_db()


def test_btc_self_finding_records_not_applicable_btc_alignment(monkeypatch, tmp_path):
    db_path = str(tmp_path / "btc_self.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        finding = _make_finding(symbol=BTC_SYMBOL, direction="LONG")
        context_inputs = _context_inputs(btc_htf_trend=not_applicable_trend_component())
        _record_finding(conn, finding, "2026-01-01T00:00:00Z", context_inputs=context_inputs)

        row = repo.get_opportunities(conn)[0]
        components = json.loads(row["component_scores_json"])["components"]
        btc_component = next(c for c in components if c["name"] == "btc_alignment")
        assert btc_component["status"] == "NOT_APPLICABLE"
        assert btc_component["contribution"] == 0.0

        context = json.loads(row["context_json"])
        assert context["btc_htf_trend"]["status"] == "NOT_APPLICABLE"
    finally:
        close_db()


def test_reconfirmation_never_touches_score_or_context_fields(monkeypatch, tmp_path):
    """Immutable first-detection snapshot policy generalized to ALL
    families, not just VOLATILITY_COMPRESSION: a re-confirmation of an
    already-ACTIVE opportunity must leave context_json/component_scores_json/
    total_score/score_version/score_warnings_json exactly as first inserted,
    even when the reconfirming call supplies DIFFERENT context_inputs.
    """
    db_path = str(tmp_path / "score_snapshot_frozen.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        finding = _make_finding(direction="LONG")
        first_inputs = _context_inputs(symbol_htf_trend=_trend_component(direction="LONG"))
        _record_finding(conn, finding, "2026-01-01T00:00:00Z", context_inputs=first_inputs)
        first_row = repo.get_opportunities(conn)[0]

        # A materially different context on the reconfirming call — if this
        # leaked through, the assertions below would catch it.
        second_inputs = _context_inputs(symbol_htf_trend=_trend_component(direction="SHORT"))
        is_new = _record_finding(
            conn, finding, "2026-01-01T00:05:00Z", context_inputs=second_inputs
        )
        assert is_new is False

        second_row = repo.get_opportunities(conn)[0]
        assert second_row["context_json"] == first_row["context_json"]
        assert second_row["component_scores_json"] == first_row["component_scores_json"]
        assert second_row["total_score"] == first_row["total_score"]
        assert second_row["score_version"] == first_row["score_version"]
        assert second_row["score_warnings_json"] == first_row["score_warnings_json"]
        assert second_row["last_seen_at"] == "2026-01-01T00:05:00Z"  # activity fields still moved
        assert second_row["occurrence_count"] == 2
    finally:
        close_db()


def test_reconfirmation_of_compression_also_leaves_score_fields_frozen(monkeypatch, tmp_path):
    """Same guarantee as above, specifically for the DIRECTION_INVARIANT
    VOLATILITY_COMPRESSION family, whose existing measurements_json
    freezing this milestone must not disturb."""
    db_path = str(tmp_path / "compression_score_frozen.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        finding_long = _make_compression_finding(direction="LONG")
        first_inputs = _context_inputs(symbol_htf_trend=_trend_component(direction="LONG"))
        _record_finding(conn, finding_long, "2026-01-01T00:00:00Z", context_inputs=first_inputs)
        first_row = repo.get_opportunities(conn)[0]
        assert first_row["total_score"] is not None

        finding_short = _make_compression_finding(direction="SHORT")  # same episode, opposite dir
        second_inputs = _context_inputs(symbol_htf_trend=_trend_component(direction="SHORT"))
        is_new = _record_finding(
            conn, finding_short, "2026-01-01T00:05:00Z", context_inputs=second_inputs
        )
        assert is_new is False

        second_row = repo.get_opportunities(conn)[0]
        assert second_row["total_score"] == first_row["total_score"]
        assert second_row["context_json"] == first_row["context_json"]
        assert json.loads(second_row["context_json"])["scored_direction"] == "LONG"
    finally:
        close_db()


def test_enrichment_exception_is_contained_finding_still_inserted_and_score_error_counted(
    monkeypatch, tmp_path
):
    """A raising assemble_context/score_opportunity must never drop the
    underlying detector finding — only its own score/context degrade to
    NULL, with an explicit warning and a counted, logged failure."""
    db_path = str(tmp_path / "enrichment_failure.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        monkeypatch.setattr(
            opportunity_engine,
            "assemble_context",
            lambda **kwargs: (_ for _ in ()).throw(RuntimeError("simulated context failure")),
        )

        finding = _make_finding()
        summary = OpportunityScanSummary()
        is_new = _record_finding(
            conn, finding, "2026-01-01T00:00:00Z",
            context_inputs=_context_inputs(), summary=summary,
        )
        assert is_new is True  # the finding itself is still recorded
        assert summary.score_errors == 1

        row = repo.get_opportunities(conn)[0]
        assert row["context_json"] is None
        assert row["component_scores_json"] is None
        assert row["total_score"] is None
        assert row["score_version"] == SCORE_VERSION
        assert json.loads(row["score_warnings_json"]) == ["CONTEXT_SCORING_FAILED"]
    finally:
        close_db()


def test_mixed_context_availability_findings_all_still_inserted(monkeypatch, tmp_path):
    """One finding whose enrichment succeeds and one whose enrichment
    raises, recorded back-to-back — neither is dropped, only the failing
    one's score/context is NULL."""
    db_path = str(tmp_path / "mixed_context.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        good_finding = _make_finding(fingerprint_key="anchor-good")
        bad_finding = _make_finding(fingerprint_key="anchor-bad")

        _record_finding(
            conn, good_finding, "2026-01-01T00:00:00Z", context_inputs=_context_inputs()
        )

        monkeypatch.setattr(
            opportunity_engine,
            "score_opportunity",
            lambda context: (_ for _ in ()).throw(RuntimeError("simulated scoring failure")),
        )
        _record_finding(
            conn, bad_finding, "2026-01-01T00:00:00Z", context_inputs=_context_inputs()
        )

        rows = repo.get_opportunities(conn)
        assert len(rows) == 2
        by_score_presence = {r["total_score"] is not None for r in rows}
        assert by_score_presence == {True, False}
    finally:
        close_db()


# ------------------------------------------------------------------ BTC 15m context: once-per-scan, missing, self


def test_btc_15m_fetched_at_most_once_per_scan_and_reused_for_btc_symbol(monkeypatch, tmp_path):
    db_path = str(tmp_path / "btc_once.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        _seed_market(conn, BTC_SYMBOL)
        _seed_market(conn, "ETH")
        store = CandleStore(conn)
        for symbol in (BTC_SYMBOL, "ETH"):
            _load_into_store(store, symbol, "5m", _sweep_reclaim_df())
            _load_into_store(store, symbol, "3m", _sweep_reclaim_df())
            _load_into_store(store, symbol, "15m", _sweep_reclaim_df())

        capturing = _CapturingCandleStore(store)
        summary = asyncio.run(run_opportunity_scan(conn, capturing, settings))
        assert summary.markets_scanned == 2

        fifteen_m_symbols = [c[0] for c in capturing.calls if c[1] == "15m"]
        # Exactly one 15m read for BTC (the shared once-per-scan frame,
        # reused for BTC's own symbol_htf_trend context too — never a
        # second, redundant get_df call), and exactly one for ETH (its own
        # ordinary once-per-symbol 15m read).
        assert fifteen_m_symbols.count(BTC_SYMBOL) == 1
        assert fifteen_m_symbols.count("ETH") == 1
    finally:
        close_db()


def test_btc_missing_does_not_crash_scan_and_marks_btc_component_unavailable(monkeypatch, tmp_path):
    db_path = str(tmp_path / "btc_missing.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        _seed_market(conn, "ETH")  # no BTC market/candles at all in this scan
        store = CandleStore(conn)
        _load_into_store(store, "ETH", "5m", _sweep_reclaim_df())
        _load_into_store(store, "ETH", "3m", _sweep_reclaim_df())

        summary = asyncio.run(run_opportunity_scan(conn, store, settings))
        assert summary.markets_scanned == 1
        assert summary.findings > 0
        assert summary.context_errors == 0  # missing data (None), not a raised fetch error

        rows = repo.get_opportunities(conn)
        assert len(rows) > 0
        context = json.loads(rows[0]["context_json"])
        assert context["btc_htf_trend"]["status"] == "UNAVAILABLE"
    finally:
        close_db()


class _RaisingOnBTC15mCandleStore:
    """Wraps a real CandleStore, raising only for the BTC reference
    symbol's own 15m read — every other call delegates through untouched.
    """

    def __init__(self, inner: CandleStore):
        self._inner = inner

    def get_df(self, symbol: str, timeframe: str, now_ms=None):
        if symbol == BTC_SYMBOL and timeframe == "15m":
            raise RuntimeError("simulated BTC 15m context fetch failure")
        return self._inner.get_df(symbol, timeframe, now_ms=now_ms)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_btc_15m_fetch_error_is_isolated_counted_and_does_not_abort_the_scan(
    monkeypatch, tmp_path, caplog
):
    db_path = str(tmp_path / "btc_fetch_error.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        _seed_market(conn, "ETH")
        store = CandleStore(conn)
        _load_into_store(store, "ETH", "5m", _sweep_reclaim_df())
        _load_into_store(store, "ETH", "3m", _sweep_reclaim_df())
        wrapped = _RaisingOnBTC15mCandleStore(store)

        with caplog.at_level("ERROR", logger="apex.opportunity.engine"):
            summary = asyncio.run(run_opportunity_scan(conn, wrapped, settings))

        assert summary.markets_scanned == 1
        assert summary.findings > 0  # ETH's own primary detection unaffected
        assert summary.context_errors == 1
        assert summary.detector_errors == 0  # a distinct, separate counter

        rows = repo.get_opportunities(conn)
        context = json.loads(rows[0]["context_json"])
        assert context["btc_htf_trend"]["status"] == "UNAVAILABLE"

        error_records = [r for r in caplog.records if r.levelname == "ERROR"]
        assert any("BTC" in r.getMessage() for r in error_records)
    finally:
        close_db()


def test_btc_symbol_findings_get_not_applicable_btc_alignment_end_to_end(monkeypatch, tmp_path):
    db_path = str(tmp_path / "btc_self_e2e.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        _seed_market(conn, BTC_SYMBOL)
        store = CandleStore(conn)
        _load_into_store(store, BTC_SYMBOL, "5m", _sweep_reclaim_df())
        _load_into_store(store, BTC_SYMBOL, "3m", _sweep_reclaim_df())
        _load_into_store(store, BTC_SYMBOL, "15m", _sweep_reclaim_df())

        summary = asyncio.run(run_opportunity_scan(conn, store, settings))
        assert summary.findings > 0

        rows = repo.get_opportunities(conn)
        assert all(r["symbol"] == BTC_SYMBOL for r in rows)
        for row in rows:
            context = json.loads(row["context_json"])
            assert context["btc_htf_trend"]["status"] == "NOT_APPLICABLE"
            components = json.loads(row["component_scores_json"])["components"]
            btc_component = next(c for c in components if c["name"] == "btc_alignment")
            assert btc_component["status"] == "NOT_APPLICABLE"
    finally:
        close_db()


# ------------------------------------------------------------------ unsupported setup_family / five-family parity

FIVE_FAMILIES = [
    "VOLATILITY_COMPRESSION",
    "SWEEP_RECLAIM",
    "SUPPORT_RESISTANCE_REJECTION",
    "SUPPORT_RESISTANCE_BREAKOUT_RETEST",
    "SUPPORT_RESISTANCE_FAILED_BREAKOUT",
]


def test_build_score_snapshot_rejects_unsupported_setup_family_with_explicit_warning():
    """_build_score_snapshot validates setup_family against the sealed
    contract (SetupFamily via typing.get_args) before ever calling
    assemble_context/score_opportunity — an unsupported/malformed family
    yields the same NULL-enrichment-plus-explicit-warning outcome as any
    other contained scoring failure, tested directly against the helper.
    """
    finding = _make_finding(setup_family="NOT_A_REAL_FAMILY", detector_version="fake_v1")
    context_json, component_scores_json, total_score, score_version, score_warnings_json = (
        opportunity_engine._build_score_snapshot(finding, _context_inputs())
    )
    assert context_json is None
    assert component_scores_json is None
    assert total_score is None
    assert score_version == SCORE_VERSION
    assert json.loads(score_warnings_json) == ["UNSUPPORTED_SETUP_FAMILY"]


@pytest.mark.parametrize("family", FIVE_FAMILIES)
def test_build_score_snapshot_scores_every_one_of_the_five_valid_families_identically(family):
    """All five current families share identical context rules and scoring
    weights (sealed design) — no family-quality adapter anywhere."""
    finding = _make_finding(
        setup_family=family, detector_version=f"{family.lower()}_v_test", direction="LONG"
    )
    context_inputs = _context_inputs(
        symbol_htf_trend=_trend_component(direction="LONG"),
        primary_structure=_structure_component(direction="LONG"),
        btc_htf_trend=_trend_component(direction="LONG"),
    )
    context_json, component_scores_json, total_score, score_version, score_warnings_json = (
        opportunity_engine._build_score_snapshot(finding, context_inputs)
    )
    assert total_score == 100.0  # 50 base + 25 + 15 + 10, all aligned — identical for every family
    assert score_version == SCORE_VERSION
    assert json.loads(score_warnings_json) == []
    assert context_json is not None
    assert component_scores_json is not None


# ------------------------------------------------------------------ five-family score-snapshot immutability / legacy NULL


@pytest.mark.parametrize("family", FIVE_FAMILIES)
def test_first_detection_score_snapshot_immutable_across_all_five_families(monkeypatch, tmp_path, family):
    """Generalizes test_reconfirmation_never_touches_score_or_context_fields
    (SWEEP_RECLAIM) and test_reconfirmation_of_compression_also_leaves_score_fields_frozen
    (VOLATILITY_COMPRESSION) to all five families in one parameterized
    sweep: a reconfirmation supplying a materially DIFFERENT context must
    never move context_json/component_scores_json/total_score/
    score_version/score_warnings_json away from the first-detection value,
    for any family — while existing measurements_json refresh-vs-freeze
    semantics (compression frozen, others refreshed) stay untouched by this
    test, since it reconfirms with the identical finding object either way.
    """
    db_path = str(tmp_path / f"immutable_{family.lower()}.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        finding = _make_finding(setup_family=family, detector_version=f"{family.lower()}_v_test")
        first_inputs = _context_inputs(symbol_htf_trend=_trend_component(direction="LONG"))
        is_new = _record_finding(conn, finding, "2026-01-01T00:00:00Z", context_inputs=first_inputs)
        assert is_new is True
        first_row = repo.get_opportunities(conn)[0]
        assert first_row["total_score"] is not None

        second_inputs = _context_inputs(symbol_htf_trend=_trend_component(direction="SHORT"))
        is_new_2 = _record_finding(
            conn, finding, "2026-01-01T00:05:00Z", context_inputs=second_inputs
        )
        assert is_new_2 is False

        second_row = repo.get_opportunities(conn)[0]
        assert second_row["context_json"] == first_row["context_json"]
        assert second_row["component_scores_json"] == first_row["component_scores_json"]
        assert second_row["total_score"] == first_row["total_score"]
        assert second_row["score_version"] == first_row["score_version"]
        assert second_row["score_warnings_json"] == first_row["score_warnings_json"]
        assert second_row["last_seen_at"] == "2026-01-01T00:05:00Z"  # activity fields still moved
        assert second_row["occurrence_count"] == 2
    finally:
        close_db()


@pytest.mark.parametrize("family", FIVE_FAMILIES)
def test_legacy_null_score_snapshot_never_backfilled_on_reconfirmation_across_all_five_families(
    monkeypatch, tmp_path, family
):
    """A finding first recorded with NO context_inputs at all (equivalent to
    a legacy pre-milestone row's permanently-NULL score) must stay NULL
    forever, even when a LATER reconfirmation of the same opportunity
    supplies real context_inputs — the score snapshot is only ever
    attempted at first detection, never manufactured retroactively from
    future context, for any of the five families.
    """
    db_path = str(tmp_path / f"legacy_null_{family.lower()}.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        finding = _make_finding(setup_family=family, detector_version=f"{family.lower()}_v_test")
        is_new = _record_finding(conn, finding, "2026-01-01T00:00:00Z")  # no context_inputs at all
        assert is_new is True
        first_row = repo.get_opportunities(conn)[0]
        assert first_row["total_score"] is None
        assert first_row["context_json"] is None

        is_new_2 = _record_finding(
            conn, finding, "2026-01-01T00:05:00Z", context_inputs=_context_inputs()
        )
        assert is_new_2 is False

        second_row = repo.get_opportunities(conn)[0]
        assert second_row["total_score"] is None
        assert second_row["context_json"] is None
        assert second_row["component_scores_json"] is None
        assert second_row["score_version"] is None
        assert second_row["score_warnings_json"] is None
        assert second_row["last_seen_at"] == "2026-01-01T00:05:00Z"  # activity fields still moved
        assert second_row["occurrence_count"] == 2
    finally:
        close_db()


# ------------------------------------------------------------------ repo.get_ranked_opportunities


def _insert_scored_opportunity(conn, **overrides) -> None:
    base = dict(
        opportunity_uid=f"uid-{overrides.get('fingerprint', 'x')}",
        fingerprint=overrides.get("fingerprint", "fp-x"),
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
    )
    base.update(overrides)
    repo.insert_opportunity(conn, Opportunity(**base))


def test_ranked_opportunities_orders_by_score_before_limit_over_full_filtered_set(
    monkeypatch, tmp_path
):
    db_path = str(tmp_path / "ranked_order_limit.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        # Deliberately inserted in an order that does NOT match score order,
        # so a bug that sorted only an already-limited/incidental-order
        # batch would fail this.
        scores = [10.0, 90.0, 50.0, 70.0, 30.0]
        for i, score in enumerate(scores):
            _insert_scored_opportunity(
                conn, opportunity_uid=f"uid-{i}", fingerprint=f"fp-{i}",
                total_score=score, score_version=SCORE_VERSION,
            )

        ranked = repo.get_ranked_opportunities(conn, limit=3)
        assert [r["total_score"] for r in ranked] == [90.0, 70.0, 50.0]
    finally:
        close_db()


def test_ranked_opportunities_unscored_and_unknown_version_sort_after_scored(monkeypatch, tmp_path):
    db_path = str(tmp_path / "ranked_unscored_last.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        _insert_scored_opportunity(
            conn, opportunity_uid="uid-scored", fingerprint="fp-scored",
            total_score=40.0, score_version=SCORE_VERSION,
        )
        _insert_scored_opportunity(
            conn, opportunity_uid="uid-null", fingerprint="fp-null",
            total_score=None, score_version=None,
        )
        _insert_scored_opportunity(
            conn, opportunity_uid="uid-old-version", fingerprint="fp-old-version",
            total_score=95.0, score_version="context_alignment_v0_0_pretend_old",
        )

        ranked = repo.get_ranked_opportunities(conn)
        uids_in_order = [r["opportunity_uid"] for r in ranked]
        assert uids_in_order[0] == "uid-scored"
        # An old/unknown score_version sorts AFTER the current-version
        # scored row regardless of its own (higher) numeric total_score.
        assert set(uids_in_order[1:]) == {"uid-null", "uid-old-version"}
    finally:
        close_db()


def test_ranked_opportunities_out_of_range_and_nonnumeric_scores_sort_after_valid(
    monkeypatch, tmp_path
):
    """Extends test_ranked_opportunities_unscored_and_unknown_version_sort_after_scored:
    a stored total_score that is numerically out of [0, 100] or not numeric
    at all (both possible via direct row manipulation, not through the
    engine's own clamped scoring path) must also sort after every valid,
    current-version, in-range scored row — never ahead of it by raw
    numeric value.
    """
    db_path = str(tmp_path / "ranked_invalid_scores.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        _insert_scored_opportunity(
            conn, opportunity_uid="uid-valid", fingerprint="fp-valid",
            total_score=10.0, score_version=SCORE_VERSION,
        )
        _insert_scored_opportunity(
            conn, opportunity_uid="uid-too-high", fingerprint="fp-too-high",
            total_score=150.0, score_version=SCORE_VERSION,
        )
        _insert_scored_opportunity(
            conn, opportunity_uid="uid-too-low", fingerprint="fp-too-low",
            total_score=-5.0, score_version=SCORE_VERSION,
        )
        _insert_scored_opportunity(
            conn, opportunity_uid="uid-nonnumeric", fingerprint="fp-nonnumeric",
            total_score="not-a-number", score_version=SCORE_VERSION,
        )

        ranked = repo.get_ranked_opportunities(conn)
        uids_in_order = [r["opportunity_uid"] for r in ranked]
        assert uids_in_order[0] == "uid-valid"
        assert set(uids_in_order[1:]) == {"uid-too-high", "uid-too-low", "uid-nonnumeric"}
    finally:
        close_db()


def test_ranked_opportunities_deterministic_tie_break_last_seen_at(monkeypatch, tmp_path):
    db_path = str(tmp_path / "ranked_ties_last_seen.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        # Identical score/version/symbol; differ only by last_seen_at -> DESC.
        _insert_scored_opportunity(
            conn, opportunity_uid="uid-earlier", fingerprint="fp-earlier",
            total_score=50.0, score_version=SCORE_VERSION,
            last_seen_at="2026-01-01T00:00:00Z",
        )
        _insert_scored_opportunity(
            conn, opportunity_uid="uid-later", fingerprint="fp-later",
            total_score=50.0, score_version=SCORE_VERSION,
            last_seen_at="2026-01-01T00:10:00Z",
        )

        ranked = repo.get_ranked_opportunities(conn)
        assert [r["opportunity_uid"] for r in ranked] == ["uid-later", "uid-earlier"]
    finally:
        close_db()


def test_ranked_opportunities_deterministic_tie_break_symbol(monkeypatch, tmp_path):
    db_path = str(tmp_path / "ranked_ties_symbol.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        # Identical score/version/last_seen_at; differ only by symbol -> ASC.
        _insert_scored_opportunity(
            conn, opportunity_uid="uid-zzz", fingerprint="fp-zzz", symbol="ZZZ",
            total_score=50.0, score_version=SCORE_VERSION,
            last_seen_at="2026-01-01T00:10:00Z",
        )
        _insert_scored_opportunity(
            conn, opportunity_uid="uid-aaa", fingerprint="fp-aaa", symbol="AAA",
            total_score=50.0, score_version=SCORE_VERSION,
            last_seen_at="2026-01-01T00:10:00Z",
        )

        ranked = repo.get_ranked_opportunities(conn)
        assert [r["opportunity_uid"] for r in ranked] == ["uid-aaa", "uid-zzz"]
    finally:
        close_db()


def test_ranked_opportunities_default_status_active_excludes_expired(monkeypatch, tmp_path):
    db_path = str(tmp_path / "ranked_status_default.db")
    _env(monkeypatch, db_path)
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        _insert_scored_opportunity(
            conn, opportunity_uid="uid-active", fingerprint="fp-active",
            total_score=60.0, score_version=SCORE_VERSION, status="ACTIVE",
        )
        _insert_scored_opportunity(
            conn, opportunity_uid="uid-expired", fingerprint="fp-expired",
            total_score=90.0, score_version=SCORE_VERSION, status="EXPIRED",
        )

        default_ranked = repo.get_ranked_opportunities(conn)
        assert [r["opportunity_uid"] for r in default_ranked] == ["uid-active"]

        expired_ranked = repo.get_ranked_opportunities(conn, status="EXPIRED")
        assert [r["opportunity_uid"] for r in expired_ranked] == ["uid-expired"]
    finally:
        close_db()


# ============================================================================
# Trade plans and outcome evidence v0.1 — engine wiring
# ============================================================================


def test_new_opportunity_creates_exactly_one_trade_plan(monkeypatch, tmp_path):
    db_path = str(tmp_path / "trade_plan_new.db")
    _env(monkeypatch, db_path)
    monkeypatch.setenv("TRADE_PLAN_EVIDENCE_ENABLED", "true")
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        finding = _make_finding(
            setup_family="SWEEP_RECLAIM",
            measurements={"reclaim_close": 110.0, "sweep_low": 100.0},
        )
        is_new = _record_finding(conn, finding, "2026-01-01T00:00:00Z", settings=settings)
        assert is_new is True

        opp_row = repo.get_opportunities(conn)[0]
        plan_row = repo.get_trade_plan_by_opportunity_uid(conn, opp_row["opportunity_uid"])
        assert plan_row is not None
        assert plan_row["availability"] == "AVAILABLE"
        assert plan_row["entry_price"] == 110.0
        assert plan_row["stop_price"] == 100.0

        outcome_row = conn.execute(
            "SELECT * FROM opportunity_trade_plan_outcomes WHERE plan_uid=?",
            (plan_row["plan_uid"],),
        ).fetchone()
        assert outcome_row["state"] == "PENDING_ENTRY"

        assert conn.execute("SELECT COUNT(*) FROM opportunity_trade_plans").fetchone()[0] == 1
    finally:
        close_db()


def test_unavailable_plan_created_for_compression(monkeypatch, tmp_path):
    db_path = str(tmp_path / "trade_plan_compression.db")
    _env(monkeypatch, db_path)
    monkeypatch.setenv("TRADE_PLAN_EVIDENCE_ENABLED", "true")
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        finding = _make_compression_finding()
        _record_finding(conn, finding, "2026-01-01T00:00:00Z", settings=settings)

        plan_row = conn.execute("SELECT * FROM opportunity_trade_plans").fetchone()
        assert plan_row["availability"] == "UNAVAILABLE"
        assert plan_row["unavailable_reason"] == "NO_STRUCTURAL_INVALIDATION"
        assert plan_row["entry_price"] is None

        outcome_row = conn.execute("SELECT * FROM opportunity_trade_plan_outcomes").fetchone()
        assert outcome_row["state"] == "NOT_EVALUABLE"
    finally:
        close_db()


def test_reconfirmation_does_not_duplicate_trade_plan(monkeypatch, tmp_path):
    db_path = str(tmp_path / "trade_plan_reconfirm.db")
    _env(monkeypatch, db_path)
    monkeypatch.setenv("TRADE_PLAN_EVIDENCE_ENABLED", "true")
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        finding = _make_finding(
            setup_family="SWEEP_RECLAIM",
            measurements={"reclaim_close": 110.0, "sweep_low": 100.0},
        )
        _record_finding(conn, finding, "2026-01-01T00:00:00Z", settings=settings)
        _record_finding(conn, finding, "2026-01-01T00:05:00Z", settings=settings)  # reconfirmation

        assert conn.execute("SELECT COUNT(*) FROM opportunity_trade_plans").fetchone()[0] == 1
    finally:
        close_db()


def test_historical_opportunity_never_gets_backfilled_plan(monkeypatch, tmp_path):
    """A row created before the flag was enabled (or with settings omitted
    entirely) must never gain a plan on a later reconfirmation, even once
    the flag is on — plans are only ever created on the NEW-opportunity
    branch.
    """
    db_path = str(tmp_path / "trade_plan_no_backfill.db")
    _env(monkeypatch, db_path)
    settings_no_plan = get_settings()
    conn = init_db(settings_no_plan.apex_db_path)
    try:
        finding = _make_finding(
            setup_family="SWEEP_RECLAIM",
            measurements={"reclaim_close": 110.0, "sweep_low": 100.0},
        )
        _record_finding(conn, finding, "2026-01-01T00:00:00Z")  # settings=None -> no plan
        assert conn.execute("SELECT COUNT(*) FROM opportunity_trade_plans").fetchone()[0] == 0

        monkeypatch.setenv("TRADE_PLAN_EVIDENCE_ENABLED", "true")
        get_settings.cache_clear()
        settings_with_plan = get_settings()

        _record_finding(conn, finding, "2026-01-01T00:05:00Z", settings=settings_with_plan)
        assert conn.execute("SELECT COUNT(*) FROM opportunity_trade_plans").fetchone()[0] == 0
    finally:
        close_db()


def test_contained_plan_failure_does_not_suppress_opportunity(monkeypatch, tmp_path):
    db_path = str(tmp_path / "trade_plan_failure.db")
    _env(monkeypatch, db_path)
    monkeypatch.setenv("TRADE_PLAN_EVIDENCE_ENABLED", "true")
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(opportunity_engine, "build_trade_plan", _boom)

        finding = _make_finding(
            setup_family="SWEEP_RECLAIM",
            measurements={"reclaim_close": 110.0, "sweep_low": 100.0},
        )
        summary = OpportunityScanSummary()
        is_new = _record_finding(
            conn, finding, "2026-01-01T00:00:00Z", settings=settings, summary=summary
        )
        assert is_new is True
        assert len(repo.get_opportunities(conn)) == 1
        assert conn.execute("SELECT COUNT(*) FROM opportunity_trade_plans").fetchone()[0] == 0
        assert summary.trade_plan_errors == 1
        assert summary.trade_plans_created == 0
    finally:
        close_db()


@pytest.mark.parametrize(
    "trade_plan_flag,engine_flag,dry_run_flag",
    [
        (False, True, True),
        (True, False, True),
        (True, True, False),
    ],
)
def test_trade_plan_guard_recheck_prevents_writes_when_any_flag_false(
    monkeypatch, tmp_path, trade_plan_flag, engine_flag, dry_run_flag
):
    db_path = str(
        tmp_path / f"guard_{int(trade_plan_flag)}_{int(engine_flag)}_{int(dry_run_flag)}.db"
    )
    _env(monkeypatch, db_path, opportunity_enabled=engine_flag, dry_run=dry_run_flag)
    monkeypatch.setenv("TRADE_PLAN_EVIDENCE_ENABLED", "true" if trade_plan_flag else "false")
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        finding = _make_finding(
            setup_family="SWEEP_RECLAIM",
            measurements={"reclaim_close": 110.0, "sweep_low": 100.0},
        )
        _record_finding(conn, finding, "2026-01-01T00:00:00Z", settings=settings)
        assert conn.execute("SELECT COUNT(*) FROM opportunity_trade_plans").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM opportunity_trade_plan_outcomes").fetchone()[0] == 0
    finally:
        close_db()


def test_run_opportunity_scan_creates_trade_plan_end_to_end(monkeypatch, tmp_path):
    db_path = str(tmp_path / "trade_plan_e2e.db")
    _env(monkeypatch, db_path)
    monkeypatch.setenv("TRADE_PLAN_EVIDENCE_ENABLED", "true")
    settings = get_settings()
    conn = init_db(settings.apex_db_path)
    try:
        _seed_market(conn, "BTC")
        store = CandleStore(conn)
        _load_into_store(store, "BTC", "5m", _sweep_reclaim_df())

        summary = asyncio.run(run_opportunity_scan(conn, store, settings))
        assert summary.trade_plans_created >= 1
        assert summary.trade_plan_errors == 0
        stored = conn.execute("SELECT COUNT(*) FROM opportunity_trade_plans").fetchone()[0]
        assert stored == summary.trade_plans_created
    finally:
        close_db()
