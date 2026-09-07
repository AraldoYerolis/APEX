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

from apex.config import get_settings
from apex.data.candle_store import CandleStore
from apex.db import repository as repo
from apex.db.connection import close_db, init_db
from apex.db.models import Market
from apex.notifications.pushover_client import PushoverClient
from apex.opportunity.contract import DetectorFinding, Opportunity
from apex.opportunity.detectors.volatility_compression import detect_volatility_compression
import apex.opportunity.engine as opportunity_engine
from apex.opportunity.engine import _record_finding, compute_fingerprint, run_opportunity_scan
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
    """Correction 2: the 15m context fetch used to sit outside the per-symbol
    try/except, so a single symbol's get_df("...", "15m") raising aborted the
    whole scan (every remaining symbol silently got no evaluation at all).
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

        # The scan itself completed (no exception propagated) and the good
        # symbol was still fully evaluated.
        assert summary.markets_scanned == 2
        assert summary.findings > 0

        rows = repo.get_opportunities(conn)
        symbols_with_findings = {r["symbol"] for r in rows}
        assert "ZZZ_GOOD" in symbols_with_findings
        assert "AAA_BAD" not in symbols_with_findings

        # The existing APEX signal scanner is a wholly separate code path
        # (scheduler/tasks.py) and is unaffected by this change or failure.
        pushover = PushoverClient(app_token="fake", user_key="fake")
        signal_summary = asyncio.run(run_signal_scan(conn, store, settings, pushover))
        assert signal_summary.markets_scanned == 2
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
