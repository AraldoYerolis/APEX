"""Tests for action handler logic.

Covers every handler (enter, skip, snooze, win, loss, breakeven) across:
- invalid token
- missing alert / trade
- duplicate / repeated action safety
- happy-path DB side-effects (status updates, paper_trades, snoozes, app_events)
- daily risk accounting and lockout on LOSS

Handlers are called directly (not via HTTP) using monkeypatched get_connection
and get_settings so tests are isolated from global state.
"""
from __future__ import annotations

import os

import pytest

from apex.actions.handlers import (
    handle_enter,
    handle_skip,
    handle_snooze,
    handle_trade_outcome,
)
from apex.actions.tokens import generate_token
from apex.config import Settings
from apex.db.connection import close_db, init_db
from apex.db import repository as repo
from apex.db.models import Alert, PaperTrade
from apex.strategy.throttles import is_symbol_snoozed
from apex.utils.time import utcnow_iso


# ------------------------------------------------------------------ constants

# Known secret used both to generate test tokens and in the patched Settings.
TOKEN_SECRET = "test-handler-secret"

# max_daily_loss_usd = account_size_usd(100) * max_daily_planned_loss_pct(3.0) / 100 = 3.0
MAX_DAILY_LOSS = 3.0


# ------------------------------------------------------------------ helpers

def _make_settings() -> Settings:
    """Build a Settings with a known token secret and suppressed stderr warnings."""
    old: dict = {}
    overrides = {
        "ACTION_TOKEN_SECRET": TOKEN_SECRET,
        "PUSHOVER_APP_TOKEN": "fake",
        "PUSHOVER_USER_KEY": "fake",
        "APEX_PUBLIC_BASE_URL": "http://localhost:8000",
        "ALERTS_ENABLED": "true",
        "DRY_RUN_MODE": "false",
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


def _token(uid: str) -> str:
    return generate_token(uid, TOKEN_SECRET, ttl_hours=24)


def _insert_alert(
    conn,
    uid: str,
    status: str = "SENT",
    symbol: str = "BTC",
    direction: str = "LONG",
) -> int:
    alert = Alert(
        alert_uid=uid,
        symbol=symbol,
        direction=direction,
        alert_type="CONFIRMED_SETUP",
        status=status,
        reference_price=100.0,
        entry_low=99.95,
        entry_high=100.05,
        stop_price=98.0,
        target_1r=102.0,
        target_2r=104.0,
        risk_usd=1.0,
        suggested_notional_usd=50.0,
        sent_at=utcnow_iso(),
    )
    return repo.insert_alert(conn, alert)


def _insert_trade(
    conn,
    uid: str,
    alert_id: int,
    risk_usd: float = 1.0,
    status: str = "OPEN",
) -> int:
    trade = PaperTrade(
        trade_uid=uid,
        alert_id=alert_id,
        symbol="BTC",
        direction="LONG",
        setup_type="TREND_PULLBACK",
        entry_price=100.05,
        stop_price=98.0,
        target_1r=102.0,
        target_2r=104.0,
        risk_usd=risk_usd,
        opened_at=utcnow_iso(),
        status=status,
    )
    return repo.insert_paper_trade(conn, trade)


# ------------------------------------------------------------------ fixture

@pytest.fixture
def db(tmp_path, monkeypatch):
    """Isolated DB with handlers' get_connection and get_settings patched."""
    conn = init_db(str(tmp_path / "test.db"))
    settings = _make_settings()
    monkeypatch.setattr("apex.actions.handlers.get_connection", lambda: conn)
    monkeypatch.setattr("apex.actions.handlers.get_settings", lambda: settings)
    yield conn
    close_db()


# ================================================================== handle_enter

@pytest.mark.asyncio
async def test_enter_invalid_token(db):
    result = await handle_enter("some-uid", "bad:token")
    assert result["title"] == "Invalid Token"
    assert db.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_enter_alert_not_found(db):
    uid = "enter-missing"
    result = await handle_enter(uid, _token(uid))
    assert result["title"] == "Not Found"


@pytest.mark.asyncio
async def test_enter_already_entered_is_idempotent(db):
    uid = "enter-already-entered"
    _insert_alert(db, uid, status="ENTERED")
    result = await handle_enter(uid, _token(uid))
    assert result["title"] == "Already Entered"
    # No paper trade created by the duplicate action
    assert db.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_enter_already_skipped_returns_closed(db):
    uid = "enter-skipped"
    _insert_alert(db, uid, status="SKIPPED")
    result = await handle_enter(uid, _token(uid))
    assert result["title"] == "Already Closed"


@pytest.mark.asyncio
async def test_enter_already_expired_returns_closed(db):
    uid = "enter-expired"
    _insert_alert(db, uid, status="EXPIRED")
    result = await handle_enter(uid, _token(uid))
    assert result["title"] == "Already Closed"


@pytest.mark.asyncio
async def test_enter_success_creates_trade_and_event(db):
    uid = "enter-ok"
    _insert_alert(db, uid, status="SENT")
    result = await handle_enter(uid, _token(uid))

    assert result["title"] == "Entered"
    # Alert status updated
    row = db.execute("SELECT status FROM alerts WHERE alert_uid=?", (uid,)).fetchone()
    assert row["status"] == "ENTERED"
    # Paper trade row created
    assert db.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 1
    # App event written
    events = db.execute(
        "SELECT event_type FROM app_events WHERE event_type='TRADE_ENTERED'"
    ).fetchall()
    assert len(events) == 1


# ================================================================== handle_skip

@pytest.mark.asyncio
async def test_skip_invalid_token(db):
    result = await handle_skip("uid", "bad:token")
    assert result["title"] == "Invalid Token"


@pytest.mark.asyncio
async def test_skip_alert_not_found(db):
    uid = "skip-missing"
    result = await handle_skip(uid, _token(uid))
    assert result["title"] == "Not Found"


@pytest.mark.asyncio
async def test_skip_already_entered_cannot_skip(db):
    uid = "skip-entered"
    _insert_alert(db, uid, status="ENTERED")
    result = await handle_skip(uid, _token(uid))
    assert result["title"] == "Already Entered"
    # Status not changed to SKIPPED
    row = db.execute("SELECT status FROM alerts WHERE alert_uid=?", (uid,)).fetchone()
    assert row["status"] == "ENTERED"


@pytest.mark.asyncio
async def test_skip_success(db):
    uid = "skip-ok"
    _insert_alert(db, uid, status="SENT")
    result = await handle_skip(uid, _token(uid))

    assert result["title"] == "Skipped"
    row = db.execute("SELECT status FROM alerts WHERE alert_uid=?", (uid,)).fetchone()
    assert row["status"] == "SKIPPED"
    events = db.execute(
        "SELECT event_type FROM app_events WHERE event_type='ALERT_SKIPPED'"
    ).fetchall()
    assert len(events) == 1


# ================================================================== handle_snooze

@pytest.mark.asyncio
async def test_snooze_invalid_token(db):
    result = await handle_snooze("uid", "bad:token")
    assert result["title"] == "Invalid Token"


@pytest.mark.asyncio
async def test_snooze_alert_not_found(db):
    uid = "snooze-missing"
    result = await handle_snooze(uid, _token(uid))
    assert result["title"] == "Not Found"


@pytest.mark.asyncio
async def test_snooze_success(db):
    uid = "snooze-ok"
    _insert_alert(db, uid, status="SENT", symbol="ETH")
    result = await handle_snooze(uid, _token(uid))

    assert result["title"] == "Snoozed"
    # Alert status updated
    row = db.execute("SELECT status FROM alerts WHERE alert_uid=?", (uid,)).fetchone()
    assert row["status"] == "SNOOZED"
    # Snooze row inserted — is_snoozed checks the snoozes table
    assert is_symbol_snoozed(db, "ETH")
    # Other symbols not affected
    assert not is_symbol_snoozed(db, "BTC")
    # App event written
    events = db.execute(
        "SELECT event_type FROM app_events WHERE event_type='SYMBOL_SNOOZED'"
    ).fetchall()
    assert len(events) == 1


# ================================================================== handle_trade_outcome

@pytest.mark.asyncio
async def test_trade_outcome_invalid_token(db):
    result = await handle_trade_outcome("uid", "bad:token", "WIN")
    assert result["title"] == "Invalid Token"


@pytest.mark.asyncio
async def test_trade_outcome_not_found(db):
    uid = "trade-missing"
    result = await handle_trade_outcome(uid, _token(uid), "WIN")
    assert result["title"] == "Not Found"


@pytest.mark.asyncio
async def test_trade_outcome_already_closed_is_idempotent(db):
    uid = "trade-closed"
    alert_id = _insert_alert(db, "alert-for-closed")
    _insert_trade(db, uid, alert_id, status="WIN")
    result = await handle_trade_outcome(uid, _token(uid), "LOSS")
    assert result["title"] == "Already Closed"
    # Status not changed
    row = db.execute(
        "SELECT status FROM paper_trades WHERE trade_uid=?", (uid,)
    ).fetchone()
    assert row["status"] == "WIN"


@pytest.mark.asyncio
async def test_trade_win_success(db):
    uid = "trade-win"
    alert_id = _insert_alert(db, "alert-win")
    _insert_trade(db, uid, alert_id)
    result = await handle_trade_outcome(uid, _token(uid), "WIN")

    assert result["title"] == "Win"
    row = db.execute(
        "SELECT status FROM paper_trades WHERE trade_uid=?", (uid,)
    ).fetchone()
    assert row["status"] == "WIN"
    # WIN does not touch daily_risk
    assert db.execute("SELECT COUNT(*) FROM daily_risk").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_trade_breakeven_success(db):
    uid = "trade-be"
    alert_id = _insert_alert(db, "alert-be")
    _insert_trade(db, uid, alert_id)
    result = await handle_trade_outcome(uid, _token(uid), "BREAKEVEN")

    assert result["title"] == "Breakeven"
    row = db.execute(
        "SELECT status FROM paper_trades WHERE trade_uid=?", (uid,)
    ).fetchone()
    assert row["status"] == "BREAKEVEN"
    # BREAKEVEN does not touch daily_risk
    assert db.execute("SELECT COUNT(*) FROM daily_risk").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_trade_loss_increments_daily_risk(db):
    uid = "trade-loss"
    alert_id = _insert_alert(db, "alert-loss")
    _insert_trade(db, uid, alert_id, risk_usd=1.0)
    result = await handle_trade_outcome(uid, _token(uid), "LOSS")

    assert result["title"] == "Loss"
    risk_row = db.execute("SELECT * FROM daily_risk").fetchone()
    assert risk_row is not None
    assert risk_row["planned_loss_used_usd"] == pytest.approx(1.0)
    assert not risk_row["lockout_active"]


@pytest.mark.asyncio
async def test_trade_loss_triggers_lockout(db):
    """A single LOSS that equals max_daily_loss_usd (3.0) triggers lockout."""
    uid = "trade-lockout"
    alert_id = _insert_alert(db, "alert-lockout")
    _insert_trade(db, uid, alert_id, risk_usd=MAX_DAILY_LOSS)
    result = await handle_trade_outcome(uid, _token(uid), "LOSS")

    assert result["title"] == "Loss"
    assert "daily loss limit" in result["html"].lower()
    risk_row = db.execute("SELECT * FROM daily_risk").fetchone()
    assert bool(risk_row["lockout_active"])
    # Lockout event logged
    events = db.execute(
        "SELECT event_type FROM app_events WHERE event_type='DAILY_LOCKOUT'"
    ).fetchall()
    assert len(events) == 1


@pytest.mark.asyncio
async def test_trade_loss_accumulates_across_trades(db):
    """Two partial losses that together reach the limit trigger lockout."""
    alert_id1 = _insert_alert(db, "alert-acc1")
    alert_id2 = _insert_alert(db, "alert-acc2")
    _insert_trade(db, "t1", alert_id1, risk_usd=2.0)
    _insert_trade(db, "t2", alert_id2, risk_usd=1.0)

    r1 = await handle_trade_outcome("t1", _token("t1"), "LOSS")
    r2 = await handle_trade_outcome("t2", _token("t2"), "LOSS")

    assert r1["title"] == "Loss"
    assert r2["title"] == "Loss"
    assert "daily loss limit" in r2["html"].lower()

    risk_row = db.execute("SELECT * FROM daily_risk").fetchone()
    assert bool(risk_row["lockout_active"])
    assert risk_row["planned_loss_used_usd"] == pytest.approx(3.0)
