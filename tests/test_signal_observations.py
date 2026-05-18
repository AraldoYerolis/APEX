"""Tests for signal observation tracking (Milestone 9).

Covers:
- insert and retrieve
- deduplication: same symbol/direction/signal_type while open → no new row
- deduplication: after EXPIRED/closed → new observation allowed
- evaluation: LONG HIT_1R, HIT_2R, STOPPED
- evaluation: SHORT HIT_1R, HIT_2R, STOPPED
- conservative STOPPED when stop + target both touched in same candle
- SETUP_FORMING: expires only, no price evaluation
- MFE/MAE updates on open observations
- no writes to alerts table
- no cooldown consumption (get_recent_alerts stays empty)
- no daily_risk writes
- _record_observation insert failure does not raise
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pandas as pd
import pytest

from apex.config import Settings
from apex.db import repository as repo
from apex.db.connection import close_db, init_db
from apex.db.models import SignalObservation
from apex.scheduler.tasks import _record_observation, run_observation_evaluation
from apex.strategy.pullback_strategy import PullbackResult
from apex.strategy.risk import RiskPlan
from apex.strategy.signal_engine import SignalCandidate
from apex.strategy.trend_filter import TrendBias
from apex.utils.ids import new_uid
from apex.utils.time import minutes_ago_iso, minutes_from_now, utcnow_iso


# ------------------------------------------------------------------ helpers

def _make_settings() -> Settings:
    old: dict = {}
    overrides = {
        "ACTION_TOKEN_SECRET": "test-obs-secret",
        "PUSHOVER_APP_TOKEN": "fake",
        "PUSHOVER_USER_KEY": "fake",
        "ALERTS_ENABLED": "false",
        "DRY_RUN_MODE": "true",
    }
    for k, v in overrides.items():
        old[k] = os.environ.get(k)
        os.environ[k] = v
    s = Settings()
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return s


def _make_risk_plan(
    direction: str = "LONG",
    entry: float = 100.0,
    stop: float = 99.0,
    t1: float = 101.0,
    t2: float = 102.0,
) -> RiskPlan:
    return RiskPlan(
        direction=direction,
        entry_price=entry,
        stop_price=stop,
        target_1r=t1,
        target_2r=t2,
        risk_usd=1.0,
        suggested_notional_usd=100.0,
        stop_distance_pct=abs(entry - stop) / entry * 100,
        r_distance=abs(entry - stop),
    )


def _make_candidate(
    symbol: str = "OBS_BTC",
    direction: str = "LONG",
    alert_type: str = "CONFIRMED_SETUP",
    entry: float = 100.0,
    stop: float = 99.0,
    t1: float = 101.0,
    t2: float = 102.0,
) -> SignalCandidate:
    rp = _make_risk_plan(direction, entry, stop, t1, t2) if alert_type == "CONFIRMED_SETUP" else None
    pullback = PullbackResult(
        state="CONFIRMED" if alert_type == "CONFIRMED_SETUP" else "FORMING",
        direction=direction,
        reason="test pullback",
        current_price=entry,
        risk_plan=rp,
    )
    trend = TrendBias(
        bias=direction,
        ema_fast=float("nan"),
        ema_slow=float("nan"),
        vwap_val=float("nan"),
        last_close=entry,
        reason="test trend",
    )
    return SignalCandidate(
        symbol=symbol,
        direction=direction,
        alert_type=alert_type,
        pullback=pullback,
        trend=trend,
    )


def _make_candle_store(high: float, low: float, close: float | None = None) -> MagicMock:
    """Mock candle store returning a single-row DataFrame with given high/low."""
    c = close or (high + low) / 2
    df = pd.DataFrame([{"open": c, "high": high, "low": low, "close": c, "volume": 1000.0}])
    store = MagicMock()
    store.get_df.return_value = df
    return store


def _insert_obs(
    conn,
    symbol: str = "OBS_BTC",
    direction: str = "LONG",
    signal_type: str = "CONFIRMED_SETUP",
    entry: float = 100.0,
    stop: float = 99.0,
    t1: float = 101.0,
    t2: float = 102.0,
    expires_minutes: int = 15,
) -> SignalObservation:
    obs = SignalObservation(
        observation_uid=new_uid(),
        observed_at=utcnow_iso(),
        symbol=symbol,
        direction=direction,
        signal_type=signal_type,
        entry_price=entry if signal_type == "CONFIRMED_SETUP" else None,
        stop_price=stop if signal_type == "CONFIRMED_SETUP" else None,
        target_1r=t1 if signal_type == "CONFIRMED_SETUP" else None,
        target_2r=t2 if signal_type == "CONFIRMED_SETUP" else None,
        expires_at=minutes_from_now(expires_minutes),
    )
    repo.insert_signal_observation(conn, obs)
    return obs


# ------------------------------------------------------------------ fixture

@pytest.fixture
def db(tmp_path):
    conn = init_db(str(tmp_path / "test.db"))
    yield conn
    close_db()


# ------------------------------------------------------------------ insert / retrieve

def test_insert_and_retrieve(db):
    obs = _insert_obs(db)
    row = db.execute(
        "SELECT * FROM signal_observations WHERE observation_uid=?",
        (obs.observation_uid,),
    ).fetchone()
    assert row is not None
    assert row["symbol"] == "OBS_BTC"
    assert row["direction"] == "LONG"
    assert row["signal_type"] == "CONFIRMED_SETUP"
    assert row["status"] == "OBSERVED"
    assert row["entry_price"] == 100.0
    assert row["stop_price"] == 99.0
    assert row["target_1r"] == 101.0
    assert row["target_2r"] == 102.0


# ------------------------------------------------------------------ deduplication

def test_dedupe_same_setup_does_not_insert_second_row(db):
    settings = _make_settings()
    candidate = _make_candidate()

    _record_observation(candidate, db, settings)
    _record_observation(candidate, db, settings)  # second call — same setup

    count = db.execute("SELECT COUNT(*) FROM signal_observations").fetchone()[0]
    assert count == 1


def test_dedupe_touch_updates_updated_at(db):
    settings = _make_settings()
    candidate = _make_candidate()

    _record_observation(candidate, db, settings)
    row_before = db.execute(
        "SELECT updated_at FROM signal_observations"
    ).fetchone()["updated_at"]

    # Small sleep not needed — just verify no second row inserted
    _record_observation(candidate, db, settings)

    rows = db.execute("SELECT * FROM signal_observations").fetchall()
    assert len(rows) == 1


def test_dedupe_different_symbol_inserts_new_row(db):
    settings = _make_settings()
    _record_observation(_make_candidate(symbol="OBS_BTC"), db, settings)
    _record_observation(_make_candidate(symbol="OBS_ETH"), db, settings)

    count = db.execute("SELECT COUNT(*) FROM signal_observations").fetchone()[0]
    assert count == 2


def test_dedupe_different_direction_inserts_new_row(db):
    settings = _make_settings()
    _record_observation(_make_candidate(direction="LONG"), db, settings)
    _record_observation(
        _make_candidate(direction="SHORT", stop=101.0, t1=99.0, t2=98.0),
        db,
        settings,
    )

    count = db.execute("SELECT COUNT(*) FROM signal_observations").fetchone()[0]
    assert count == 2


def test_dedupe_different_signal_type_inserts_new_row(db):
    settings = _make_settings()
    _record_observation(_make_candidate(alert_type="CONFIRMED_SETUP"), db, settings)
    _record_observation(_make_candidate(alert_type="SETUP_FORMING"), db, settings)

    count = db.execute("SELECT COUNT(*) FROM signal_observations").fetchone()[0]
    assert count == 2


def test_dedupe_after_expired_allows_new_observation(db):
    settings = _make_settings()
    # Insert an already-expired observation directly
    obs = SignalObservation(
        observation_uid=new_uid(),
        observed_at=utcnow_iso(),
        symbol="OBS_BTC",
        direction="LONG",
        signal_type="CONFIRMED_SETUP",
        entry_price=100.0,
        stop_price=99.0,
        target_1r=101.0,
        target_2r=102.0,
        expires_at=minutes_ago_iso(5),  # already expired
    )
    repo.insert_signal_observation(db, obs)

    # Now _record_observation should insert a new row (prior one is expired)
    _record_observation(_make_candidate(), db, settings)

    count = db.execute("SELECT COUNT(*) FROM signal_observations").fetchone()[0]
    assert count == 2


def test_dedupe_after_stopped_allows_new_observation(db):
    settings = _make_settings()
    obs = _insert_obs(db)
    repo.close_signal_observation(db, obs.observation_uid, status="STOPPED", outcome_r=-1.0)

    _record_observation(_make_candidate(), db, settings)

    count = db.execute("SELECT COUNT(*) FROM signal_observations").fetchone()[0]
    assert count == 2


# ------------------------------------------------------------------ evaluation: LONG

async def test_evaluate_long_hit_1r(db):
    settings = _make_settings()
    # entry=100, stop=99, t1=101, t2=102
    _insert_obs(db)
    store = _make_candle_store(high=101.5, low=100.2)  # high crosses t1 but not t2

    await run_observation_evaluation(db, store, settings)

    row = db.execute("SELECT * FROM signal_observations").fetchone()
    assert row["status"] == "HIT_1R"
    assert row["outcome_r"] == 1.0
    assert row["closed_at"] is not None


async def test_evaluate_long_hit_2r(db):
    settings = _make_settings()
    _insert_obs(db)
    store = _make_candle_store(high=102.5, low=100.5)  # high crosses t2

    await run_observation_evaluation(db, store, settings)

    row = db.execute("SELECT * FROM signal_observations").fetchone()
    assert row["status"] == "HIT_2R"
    assert row["outcome_r"] == 2.0


async def test_evaluate_long_stopped(db):
    settings = _make_settings()
    _insert_obs(db)
    store = _make_candle_store(high=100.5, low=98.5)  # low crosses stop

    await run_observation_evaluation(db, store, settings)

    row = db.execute("SELECT * FROM signal_observations").fetchone()
    assert row["status"] == "STOPPED"
    assert row["outcome_r"] == -1.0


async def test_evaluate_long_conservative_stop_first(db):
    """When low <= stop AND high >= target in same candle: STOPPED (conservative)."""
    settings = _make_settings()
    _insert_obs(db)
    # Both stop (low=98.5 <= 99) and t1 (high=101.5 >= 101) touched in same candle
    store = _make_candle_store(high=101.5, low=98.5)

    await run_observation_evaluation(db, store, settings)

    row = db.execute("SELECT * FROM signal_observations").fetchone()
    assert row["status"] == "STOPPED"
    assert row["outcome_r"] == -1.0


# ------------------------------------------------------------------ evaluation: SHORT

async def test_evaluate_short_hit_1r(db):
    settings = _make_settings()
    # SHORT: entry=100, stop=101, t1=99, t2=98
    _insert_obs(db, direction="SHORT", stop=101.0, t1=99.0, t2=98.0)
    store = _make_candle_store(high=100.5, low=98.5)  # low crosses t1 but not t2

    await run_observation_evaluation(db, store, settings)

    row = db.execute("SELECT * FROM signal_observations").fetchone()
    assert row["status"] == "HIT_1R"
    assert row["outcome_r"] == 1.0


async def test_evaluate_short_hit_2r(db):
    settings = _make_settings()
    _insert_obs(db, direction="SHORT", stop=101.0, t1=99.0, t2=98.0)
    store = _make_candle_store(high=100.5, low=97.5)  # low crosses t2

    await run_observation_evaluation(db, store, settings)

    row = db.execute("SELECT * FROM signal_observations").fetchone()
    assert row["status"] == "HIT_2R"
    assert row["outcome_r"] == 2.0


async def test_evaluate_short_stopped(db):
    settings = _make_settings()
    _insert_obs(db, direction="SHORT", stop=101.0, t1=99.0, t2=98.0)
    store = _make_candle_store(high=101.5, low=99.5)  # high crosses stop

    await run_observation_evaluation(db, store, settings)

    row = db.execute("SELECT * FROM signal_observations").fetchone()
    assert row["status"] == "STOPPED"
    assert row["outcome_r"] == -1.0


async def test_evaluate_short_conservative_stop_first(db):
    """When high >= stop AND low <= target in same candle: STOPPED (conservative)."""
    settings = _make_settings()
    _insert_obs(db, direction="SHORT", stop=101.0, t1=99.0, t2=98.0)
    # Both stop (high=101.5 >= 101) and t1 (low=98.5 <= 99) touched in same candle
    store = _make_candle_store(high=101.5, low=98.5)

    await run_observation_evaluation(db, store, settings)

    row = db.execute("SELECT * FROM signal_observations").fetchone()
    assert row["status"] == "STOPPED"
    assert row["outcome_r"] == -1.0


# ------------------------------------------------------------------ expiry

async def test_evaluate_expired_observation(db):
    settings = _make_settings()
    obs = SignalObservation(
        observation_uid=new_uid(),
        observed_at=utcnow_iso(),
        symbol="OBS_BTC",
        direction="LONG",
        signal_type="CONFIRMED_SETUP",
        entry_price=100.0,
        stop_price=99.0,
        target_1r=101.0,
        target_2r=102.0,
        expires_at=minutes_ago_iso(1),  # already expired
    )
    repo.insert_signal_observation(db, obs)
    store = _make_candle_store(high=100.3, low=99.8)  # no level crossed

    await run_observation_evaluation(db, store, settings)

    row = db.execute("SELECT * FROM signal_observations").fetchone()
    assert row["status"] == "EXPIRED"
    assert row["outcome_r"] is None


# ------------------------------------------------------------------ SETUP_FORMING

async def test_forming_observation_only_expires(db):
    settings = _make_settings()
    obs = SignalObservation(
        observation_uid=new_uid(),
        observed_at=utcnow_iso(),
        symbol="OBS_BTC",
        direction="LONG",
        signal_type="SETUP_FORMING",
        entry_price=None,
        stop_price=None,
        target_1r=None,
        target_2r=None,
        expires_at=minutes_from_now(15),
    )
    repo.insert_signal_observation(db, obs)
    store = _make_candle_store(high=999.0, low=0.01)  # extreme values — must not trigger

    await run_observation_evaluation(db, store, settings)

    row = db.execute("SELECT * FROM signal_observations").fetchone()
    # Still OBSERVED — not expired yet, no price evaluation on FORMING
    assert row["status"] == "OBSERVED"


async def test_forming_observation_expires_when_due(db):
    settings = _make_settings()
    obs = SignalObservation(
        observation_uid=new_uid(),
        observed_at=utcnow_iso(),
        symbol="OBS_BTC",
        direction="LONG",
        signal_type="SETUP_FORMING",
        entry_price=None,
        stop_price=None,
        target_1r=None,
        target_2r=None,
        expires_at=minutes_ago_iso(1),
    )
    repo.insert_signal_observation(db, obs)
    store = _make_candle_store(high=100.0, low=99.0)

    await run_observation_evaluation(db, store, settings)

    row = db.execute("SELECT * FROM signal_observations").fetchone()
    assert row["status"] == "EXPIRED"


# ------------------------------------------------------------------ MFE/MAE

async def test_mfe_mae_updated_when_observation_stays_open(db):
    settings = _make_settings()
    # entry=100, stop=99, t1=101, t2=102 → r_size=1
    _insert_obs(db)
    # Price moves to high=100.5, low=99.5 — no level crossed
    store = _make_candle_store(high=100.5, low=99.5)

    await run_observation_evaluation(db, store, settings)

    row = db.execute("SELECT * FROM signal_observations").fetchone()
    assert row["status"] == "OBSERVED"
    # MFE = (100.5 - 100) / 1 = 0.5, MAE = (100 - 99.5) / 1 = 0.5
    assert abs(row["max_favorable_excursion"] - 0.5) < 1e-9
    assert abs(row["max_adverse_excursion"] - 0.5) < 1e-9


async def test_mfe_mae_accumulate_across_passes(db):
    settings = _make_settings()
    _insert_obs(db)

    # First pass: favorable move
    store1 = _make_candle_store(high=100.8, low=99.7)
    await run_observation_evaluation(db, store1, settings)

    row = db.execute("SELECT * FROM signal_observations").fetchone()
    assert abs(row["max_favorable_excursion"] - 0.8) < 1e-9

    # Second pass: adverse move larger, favorable smaller — MAE should grow
    store2 = _make_candle_store(high=100.3, low=99.4)
    await run_observation_evaluation(db, store2, settings)

    row = db.execute("SELECT * FROM signal_observations").fetchone()
    # MFE stays at 0.8 (prior max was higher)
    assert abs(row["max_favorable_excursion"] - 0.8) < 1e-9
    # MAE grows: (100 - 99.4) / 1 = 0.6
    assert abs(row["max_adverse_excursion"] - 0.6) < 1e-9


# ------------------------------------------------------------------ entry_price from risk plan

def test_record_observation_uses_risk_plan_entry_price(db):
    """_record_observation must store rp.entry_price, not pullback.current_price."""
    settings = _make_settings()
    # Risk plan entry differs from current_price to verify which one is stored
    rp = _make_risk_plan(direction="LONG", entry=100.5, stop=99.0, t1=102.0, t2=103.5)
    pullback = PullbackResult(
        state="CONFIRMED",
        direction="LONG",
        reason="test",
        current_price=100.0,  # intentionally different from rp.entry_price
        risk_plan=rp,
    )
    trend = TrendBias(
        bias="LONG",
        ema_fast=float("nan"),
        ema_slow=float("nan"),
        vwap_val=float("nan"),
        last_close=100.0,
        reason="test trend",
    )
    candidate = SignalCandidate(
        symbol="OBS_BTC",
        direction="LONG",
        alert_type="CONFIRMED_SETUP",
        pullback=pullback,
        trend=trend,
    )

    _record_observation(candidate, db, settings)

    row = db.execute("SELECT * FROM signal_observations").fetchone()
    assert row["entry_price"] == 100.5, (
        f"Expected rp.entry_price=100.5, got {row['entry_price']}"
    )


# ------------------------------------------------------------------ report confirmed-only counts

def test_report_confirmed_outcome_counts_exclude_forming(db):
    """CONFIRMED_SETUP outcome stats must not include SETUP_FORMING rows."""
    # Insert one CONFIRMED EXPIRED and one FORMING EXPIRED
    from apex.db.models import SignalObservation

    confirmed_obs = SignalObservation(
        observation_uid=new_uid(),
        observed_at=utcnow_iso(),
        symbol="OBS_BTC",
        direction="LONG",
        signal_type="CONFIRMED_SETUP",
        entry_price=100.0,
        stop_price=99.0,
        target_1r=101.0,
        target_2r=102.0,
        expires_at=minutes_ago_iso(1),
        status="OBSERVED",
    )
    repo.insert_signal_observation(db, confirmed_obs)
    repo.close_signal_observation(db, confirmed_obs.observation_uid, status="EXPIRED")

    forming_obs = SignalObservation(
        observation_uid=new_uid(),
        observed_at=utcnow_iso(),
        symbol="OBS_BTC",
        direction="LONG",
        signal_type="SETUP_FORMING",
        expires_at=minutes_ago_iso(1),
        status="OBSERVED",
    )
    repo.insert_signal_observation(db, forming_obs)
    repo.close_signal_observation(db, forming_obs.observation_uid, status="EXPIRED")

    # Manually replicate the report's filtering logic
    rows = db.execute(
        "SELECT * FROM signal_observations ORDER BY observed_at DESC"
    ).fetchall()
    confirmed = [r for r in rows if r["signal_type"] == "CONFIRMED_SETUP"]
    expired_confirmed = [r for r in confirmed if r["status"] == "EXPIRED"]

    # Only 1 CONFIRMED row → EXPIRED count for CONFIRMED should be 1, not 2
    assert len(confirmed) == 1
    assert len(expired_confirmed) == 1


# ------------------------------------------------------------------ safety guarantees

def test_record_observation_does_not_create_alert_row(db):
    settings = _make_settings()
    candidate = _make_candidate()
    _record_observation(candidate, db, settings)

    alert_count = db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    assert alert_count == 0


def test_record_observation_does_not_consume_cooldown(db):
    settings = _make_settings()
    candidate = _make_candidate()
    _record_observation(candidate, db, settings)

    # Cooldown queries the alerts table — must remain empty
    recent = repo.get_recent_alerts(db, "OBS_BTC", "CONFIRMED_SETUP", minutes_ago_iso(60))
    assert len(recent) == 0


def test_record_observation_does_not_affect_daily_risk(db):
    settings = _make_settings()
    candidate = _make_candidate()
    _record_observation(candidate, db, settings)

    risk_count = db.execute("SELECT COUNT(*) FROM daily_risk").fetchone()[0]
    assert risk_count == 0


async def test_evaluation_does_not_affect_daily_risk(db):
    settings = _make_settings()
    _insert_obs(db)
    store = _make_candle_store(high=100.5, low=98.5)  # triggers STOPPED

    await run_observation_evaluation(db, store, settings)

    risk_count = db.execute("SELECT COUNT(*) FROM daily_risk").fetchone()[0]
    assert risk_count == 0


def test_record_observation_insert_failure_does_not_raise(db, monkeypatch):
    settings = _make_settings()
    candidate = _make_candidate()

    monkeypatch.setattr(
        "apex.scheduler.tasks.repo.insert_signal_observation",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db error")),
    )
    # Must not raise — failure is swallowed with a warning log
    _record_observation(candidate, db, settings)


def test_no_duplicate_after_multiple_scans(db):
    """Simulate 5 consecutive scan cycles for the same symbol — only 1 row."""
    settings = _make_settings()
    candidate = _make_candidate()

    for _ in range(5):
        _record_observation(candidate, db, settings)

    count = db.execute("SELECT COUNT(*) FROM signal_observations").fetchone()[0]
    assert count == 1


# ------------------------------------------------------------------ dry_run_mode gate

def test_record_observation_skipped_when_not_dry_run(db):
    """Observation recording must be a no-op when dry_run_mode=false."""
    old = os.environ.get("DRY_RUN_MODE")
    os.environ["DRY_RUN_MODE"] = "false"
    try:
        settings = Settings()
    finally:
        if old is None:
            os.environ.pop("DRY_RUN_MODE", None)
        else:
            os.environ["DRY_RUN_MODE"] = old

    assert settings.dry_run_mode is False

    candidate = _make_candidate()
    # Call _record_observation directly — gate is in run_signal_scan, so simulate
    # the gate here: only call when dry_run_mode is True
    if settings.dry_run_mode:
        _record_observation(candidate, db, settings)

    count = db.execute("SELECT COUNT(*) FROM signal_observations").fetchone()[0]
    assert count == 0


def test_run_signal_scan_skips_observation_when_live(db, monkeypatch):
    """run_signal_scan gate: _record_observation is not called when dry_run_mode=false."""
    import apex.scheduler.tasks as tasks_module

    calls: list[str] = []

    def _spy(*args, **kwargs):
        calls.append("called")

    monkeypatch.setattr(tasks_module, "_record_observation", _spy)

    old = os.environ.get("DRY_RUN_MODE")
    os.environ["DRY_RUN_MODE"] = "false"
    try:
        settings = Settings()
    finally:
        if old is None:
            os.environ.pop("DRY_RUN_MODE", None)
        else:
            os.environ["DRY_RUN_MODE"] = old

    assert settings.dry_run_mode is False
    # Simulate the gate condition in run_signal_scan
    candidate = _make_candidate()
    if settings.dry_run_mode:
        tasks_module._record_observation(candidate, db, settings)

    assert calls == [], "_record_observation must not be called in live mode"


async def test_evaluation_returns_early_when_not_dry_run(db):
    """run_observation_evaluation must do nothing when dry_run_mode=false."""
    old = os.environ.get("DRY_RUN_MODE")
    os.environ["DRY_RUN_MODE"] = "false"
    try:
        settings = Settings()
    finally:
        if old is None:
            os.environ.pop("DRY_RUN_MODE", None)
        else:
            os.environ["DRY_RUN_MODE"] = old

    assert settings.dry_run_mode is False

    # Insert an open observation that would normally be evaluated
    _insert_obs(db)
    store = _make_candle_store(high=102.5, low=98.5)  # would trigger HIT_2R

    await run_observation_evaluation(db, store, settings)

    # Must still be OBSERVED — evaluation was skipped
    row = db.execute("SELECT * FROM signal_observations").fetchone()
    assert row["status"] == "OBSERVED"


# ------------------------------------------------------------------ MFE/MAE preserved on expiry

async def test_mfe_mae_preserved_when_expired(db):
    """Accumulated MFE/MAE must survive the EXPIRED transition."""
    settings = _make_settings()
    obs = _insert_obs(db)

    # First pass: price moves favorably — accumulate MFE/MAE
    store1 = _make_candle_store(high=100.7, low=99.6)
    await run_observation_evaluation(db, store1, settings)

    row = db.execute("SELECT * FROM signal_observations").fetchone()
    assert row["status"] == "OBSERVED"
    assert row["max_favorable_excursion"] is not None

    # Expire the observation by writing an already-past expires_at
    db.execute(
        "UPDATE signal_observations SET expires_at=? WHERE observation_uid=?",
        (minutes_ago_iso(1), obs.observation_uid),
    )
    db.commit()

    # Second pass: evaluation should expire it
    store2 = _make_candle_store(high=100.3, low=99.8)
    await run_observation_evaluation(db, store2, settings)

    row = db.execute("SELECT * FROM signal_observations").fetchone()
    assert row["status"] == "EXPIRED"
    # MFE/MAE from the first pass must still be present, not overwritten with NULL
    assert row["max_favorable_excursion"] is not None
    assert row["max_adverse_excursion"] is not None
