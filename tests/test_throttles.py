"""Tests for alert throttles and daily lockout."""
import sqlite3
import pytest

from apex.db.connection import init_db
from apex.db import repository as repo
from apex.db.models import Alert
from apex.strategy.throttles import can_send_alert, is_symbol_snoozed
from apex.utils.time import utcnow_iso, minutes_from_now, minutes_ago_iso


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "test.db")
    c = init_db(db_path)
    yield c
    c.close()


def _insert_test_alert(conn, symbol, alert_type, sent_at=None):
    alert = Alert(
        alert_uid=f"test-{symbol}-{alert_type}-{sent_at or utcnow_iso()}",
        symbol=symbol,
        direction="LONG",
        alert_type=alert_type,
        sent_at=sent_at or utcnow_iso(),
    )
    repo.insert_alert(conn, alert)


# ------------------------------------------------------------------ cooldown

def test_confirmed_cooldown_blocks():
    """Second confirmed alert within cooldown window should be blocked."""
    import sqlite3, tempfile
    from apex.db.connection import init_db

    with tempfile.TemporaryDirectory() as d:
        conn = init_db(f"{d}/test.db")
        _insert_test_alert(conn, "BTC", "CONFIRMED_SETUP")

        allowed, reason = can_send_alert(
            conn, "BTC", "CONFIRMED_SETUP",
            confirmed_cooldown_minutes=30,
            forming_cooldown_minutes=15,
            global_confirmed_per_hour=5,
        )
        assert not allowed
        assert "cooldown" in reason.lower()


def test_forming_cooldown_blocks():
    import sqlite3, tempfile
    from apex.db.connection import init_db

    with tempfile.TemporaryDirectory() as d:
        conn = init_db(f"{d}/test.db")
        _insert_test_alert(conn, "ETH", "SETUP_FORMING")

        allowed, reason = can_send_alert(
            conn, "ETH", "SETUP_FORMING",
            confirmed_cooldown_minutes=30,
            forming_cooldown_minutes=15,
            global_confirmed_per_hour=5,
        )
        assert not allowed


def test_cooldown_allows_after_window(conn):
    """Alert sent 35 minutes ago should not block a new one (30-min cooldown)."""
    old_sent = minutes_ago_iso(35)
    _insert_test_alert(conn, "SOL", "CONFIRMED_SETUP", sent_at=old_sent)

    allowed, reason = can_send_alert(
        conn, "SOL", "CONFIRMED_SETUP",
        confirmed_cooldown_minutes=30,
        forming_cooldown_minutes=15,
        global_confirmed_per_hour=5,
    )
    assert allowed


def test_global_cap_blocks(conn):
    """Sending 5 confirmed alerts should trigger global cap."""
    for i in range(5):
        _insert_test_alert(conn, f"COIN{i}", "CONFIRMED_SETUP")

    allowed, reason = can_send_alert(
        conn, "BTC", "CONFIRMED_SETUP",
        confirmed_cooldown_minutes=30,
        forming_cooldown_minutes=15,
        global_confirmed_per_hour=5,
    )
    assert not allowed
    assert "cap" in reason.lower()


# ------------------------------------------------------------------ snooze

def test_snooze_blocks_symbol(conn):
    repo.add_snooze(conn, "BTC", minutes_from_now(15), "test")
    assert is_symbol_snoozed(conn, "BTC")


def test_snooze_expired_does_not_block(conn):
    repo.add_snooze(conn, "BTC", minutes_ago_iso(1), "expired")
    assert not is_symbol_snoozed(conn, "BTC")


def test_snooze_only_affects_symbol(conn):
    repo.add_snooze(conn, "BTC", minutes_from_now(15), "test")
    assert not is_symbol_snoozed(conn, "ETH")


# ------------------------------------------------------------------ daily lockout

def test_daily_lockout_triggered(conn):
    max_loss = 3.0
    repo.add_daily_loss(conn, 3.0, max_loss)  # exactly at limit
    assert repo.is_daily_lockout_active(conn, max_loss)


def test_daily_lockout_not_triggered_below_limit(conn):
    max_loss = 3.0
    repo.add_daily_loss(conn, 2.5, max_loss)
    assert not repo.is_daily_lockout_active(conn, max_loss)


def test_daily_loss_accumulates(conn):
    max_loss = 3.0
    repo.add_daily_loss(conn, 1.0, max_loss)
    repo.add_daily_loss(conn, 1.0, max_loss)
    assert not repo.is_daily_lockout_active(conn, max_loss)
    repo.add_daily_loss(conn, 1.0, max_loss)
    assert repo.is_daily_lockout_active(conn, max_loss)
