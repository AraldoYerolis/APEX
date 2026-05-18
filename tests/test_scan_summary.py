"""Tests for scan summary accounting and alert gate behavior.

Covers:
- ScanSummary counter properties
- _send_alert returns correct AlertOutcome for each gate
- SUPPRESSED_DISABLED does NOT write an alert row to DB (no cooldown consumed)
- SUPPRESSED_TYPE does NOT write an alert row to DB (no cooldown consumed)
- SENT writes the alert row and returns "SENT"
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from apex.db.connection import init_db
from apex.scheduler.tasks import ScanSummary, _send_alert
from apex.strategy.pullback_strategy import PullbackResult
from apex.strategy.risk import RiskPlan
from apex.strategy.signal_engine import SignalCandidate
from apex.strategy.trend_filter import TrendBias


# ------------------------------------------------------------------ helpers

def _make_settings(
    alerts_enabled: bool = True,
    dry_run_mode: bool = False,
    alert_types_enabled: str = "CONFIRMED_SETUP",
):
    """Build a minimal Settings-like object for tests without touching .env."""
    from apex.config import Settings
    import os

    # Override via env-level injection — pydantic-settings reads from os.environ
    # when no .env file supplies the key.
    old = {}
    overrides = {
        "ALERTS_ENABLED": str(alerts_enabled).lower(),
        "DRY_RUN_MODE": str(dry_run_mode).lower(),
        "ALERT_TYPES_ENABLED": alert_types_enabled,
        # Suppress Pushover warnings (tokens not needed for these tests)
        "PUSHOVER_APP_TOKEN": "fake-token",
        "PUSHOVER_USER_KEY": "fake-user",
    }
    for k, v in overrides.items():
        old[k] = os.environ.get(k)
        os.environ[k] = v

    # Force a fresh Settings parse (bypass lru_cache)
    s = Settings()

    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    return s


def _make_candidate(alert_type: str = "CONFIRMED_SETUP", direction: str = "LONG") -> SignalCandidate:
    risk_plan = RiskPlan(
        direction=direction,
        entry_price=100.0,
        stop_price=98.0,
        target_1r=102.0,
        target_2r=104.0,
        risk_usd=1.0,
        suggested_notional_usd=50.0,
        stop_distance_pct=2.0,
        r_distance=2.0,
    )
    pullback = PullbackResult(
        state="CONFIRMED" if alert_type == "CONFIRMED_SETUP" else "FORMING",
        direction=direction,
        reason="test setup",
        rsi_val=50.0,
        atr_val=1.5,
        vwap_val=99.5,
        current_price=100.0,
        risk_plan=risk_plan,
    )
    trend = TrendBias(
        bias="LONG" if direction == "LONG" else "SHORT",
        ema_fast=105.0,
        ema_slow=100.0,
        vwap_val=99.5,
        last_close=100.0,
        reason="EMA9>EMA21 and close above VWAP",
    )
    return SignalCandidate(
        symbol="BTC",
        direction=direction,
        alert_type=alert_type,
        pullback=pullback,
        trend=trend,
    )


def _make_pushover(succeeds: bool = True):
    mock = AsyncMock()
    mock.send = AsyncMock(return_value=succeeds)
    return mock


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "test.db"))
    yield c
    c.close()


# ------------------------------------------------------------------ ScanSummary

def test_scan_summary_candidates_property():
    s = ScanSummary(
        markets_scanned=10,
        sent=2,
        send_failed=1,
        suppressed_type=1,
        suppressed_disabled=3,
    )
    assert s.candidates == 7


def test_scan_summary_zero():
    s = ScanSummary()
    assert s.candidates == 0
    assert s.sent == 0


def test_scan_summary_log_dry_run(caplog):
    """Log line must show [DRY RUN] prefix and never say 'sent' when sent=0."""
    import logging
    s = ScanSummary(
        markets_scanned=9,
        sent=0,
        suppressed_disabled=4,
        suppressed_type=1,
    )
    with caplog.at_level(logging.INFO):
        from apex.scheduler.tasks import logger as task_logger
        s.log(task_logger, dry_run=True)

    assert len(caplog.records) == 1
    msg = caplog.records[0].message
    assert "[DRY RUN]" in msg
    assert "0 sent" in msg
    assert "4 dry-run would-be" in msg
    assert "1 suppressed by type" in msg
    assert "9 markets scanned" in msg


def test_scan_summary_log_live(caplog):
    """Log line must NOT show [DRY RUN] in live mode."""
    import logging
    s = ScanSummary(markets_scanned=5, sent=2)
    with caplog.at_level(logging.INFO):
        from apex.scheduler.tasks import logger as task_logger
        s.log(task_logger, dry_run=False)

    msg = caplog.records[0].message
    assert "[DRY RUN]" not in msg
    assert "2 sent" in msg


# ------------------------------------------------------------------ _send_alert gate: SUPPRESSED_TYPE

@pytest.mark.asyncio
async def test_suppressed_type_no_db_write(conn):
    """When alert_type not in ALERT_TYPES_ENABLED, no alert row is written."""
    settings = _make_settings(
        alerts_enabled=True,
        alert_types_enabled="CONFIRMED_SETUP",  # SETUP_FORMING excluded
    )
    candidate = _make_candidate(alert_type="SETUP_FORMING")
    pushover = _make_pushover()

    outcome = await _send_alert(candidate, conn, settings, pushover)

    assert outcome == "SUPPRESSED_TYPE"
    pushover.send.assert_not_called()

    # No alert row in DB
    rows = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    assert rows == 0


@pytest.mark.asyncio
async def test_suppressed_type_does_not_consume_cooldown(conn):
    """Type-suppressed signal: running it twice should not block a second scan."""
    settings = _make_settings(
        alerts_enabled=True,
        alert_types_enabled="CONFIRMED_SETUP",
    )
    candidate = _make_candidate(alert_type="SETUP_FORMING")
    pushover = _make_pushover()

    # Call twice — if cooldown were consumed the second would be throttled by engine,
    # but since no DB row exists the throttle check has nothing to find.
    outcome1 = await _send_alert(candidate, conn, settings, pushover)
    outcome2 = await _send_alert(candidate, conn, settings, pushover)

    assert outcome1 == "SUPPRESSED_TYPE"
    assert outcome2 == "SUPPRESSED_TYPE"
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0


# ------------------------------------------------------------------ _send_alert gate: SUPPRESSED_DISABLED

@pytest.mark.asyncio
async def test_suppressed_disabled_no_db_alert_row(conn):
    """When ALERTS_ENABLED=false, no alerts row is written (cooldown not consumed)."""
    settings = _make_settings(
        alerts_enabled=False,
        dry_run_mode=True,
        alert_types_enabled="CONFIRMED_SETUP",
    )
    candidate = _make_candidate(alert_type="CONFIRMED_SETUP")
    pushover = _make_pushover()

    outcome = await _send_alert(candidate, conn, settings, pushover)

    assert outcome == "SUPPRESSED_DISABLED"
    pushover.send.assert_not_called()

    # No alert row written — cooldown slots untouched
    alert_rows = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    assert alert_rows == 0


@pytest.mark.asyncio
async def test_suppressed_disabled_writes_app_event(conn):
    """Suppressed alert should still log an app_event for observability."""
    settings = _make_settings(alerts_enabled=False, alert_types_enabled="CONFIRMED_SETUP")
    candidate = _make_candidate(alert_type="CONFIRMED_SETUP")
    pushover = _make_pushover()

    await _send_alert(candidate, conn, settings, pushover)

    events = conn.execute(
        "SELECT event_type FROM app_events WHERE event_type='ALERT_SUPPRESSED'"
    ).fetchall()
    assert len(events) == 1


@pytest.mark.asyncio
async def test_suppressed_disabled_does_not_consume_cooldown(conn):
    """Two dry-run suppressed alerts for same symbol should not block each other
    via the throttle (because no alert row is in DB)."""
    settings = _make_settings(alerts_enabled=False, alert_types_enabled="CONFIRMED_SETUP")
    candidate = _make_candidate(alert_type="CONFIRMED_SETUP")
    pushover = _make_pushover()

    o1 = await _send_alert(candidate, conn, settings, pushover)
    o2 = await _send_alert(candidate, conn, settings, pushover)

    assert o1 == "SUPPRESSED_DISABLED"
    assert o2 == "SUPPRESSED_DISABLED"
    # Still no alert rows — cooldown state is clean
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0


# ------------------------------------------------------------------ _send_alert: SENT path

@pytest.mark.asyncio
async def test_sent_writes_alert_row(conn):
    """When ALERTS_ENABLED=true and Pushover succeeds, alert row is written."""
    settings = _make_settings(alerts_enabled=True, alert_types_enabled="CONFIRMED_SETUP")
    candidate = _make_candidate(alert_type="CONFIRMED_SETUP")
    pushover = _make_pushover(succeeds=True)

    outcome = await _send_alert(candidate, conn, settings, pushover)

    assert outcome == "SENT"
    pushover.send.assert_called_once()
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_send_failed_returns_send_failed(conn):
    """When Pushover fails, outcome is SEND_FAILED and alert row still exists."""
    settings = _make_settings(alerts_enabled=True, alert_types_enabled="CONFIRMED_SETUP")
    candidate = _make_candidate(alert_type="CONFIRMED_SETUP")
    pushover = _make_pushover(succeeds=False)

    outcome = await _send_alert(candidate, conn, settings, pushover)

    assert outcome == "SEND_FAILED"
    # Alert was persisted before the send attempt
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_sent_consumes_cooldown(conn):
    """A real SENT alert should write a DB row that blocks the next throttle check."""
    from apex.strategy.throttles import can_send_alert

    settings = _make_settings(alerts_enabled=True, alert_types_enabled="CONFIRMED_SETUP")
    candidate = _make_candidate(alert_type="CONFIRMED_SETUP")
    pushover = _make_pushover(succeeds=True)

    await _send_alert(candidate, conn, settings, pushover)

    # Now the throttle should block a second alert for the same symbol
    allowed, reason = can_send_alert(
        conn,
        symbol="BTC",
        alert_type="CONFIRMED_SETUP",
        confirmed_cooldown_minutes=30,
        forming_cooldown_minutes=15,
        global_confirmed_per_hour=5,
    )
    assert not allowed
    assert "cooldown" in reason.lower()
