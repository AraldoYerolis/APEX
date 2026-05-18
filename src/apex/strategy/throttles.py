"""Alert throttle checks."""
from __future__ import annotations

import sqlite3

from apex.db import repository as repo
from apex.utils.time import hours_ago_iso, minutes_ago_iso


def can_send_alert(
    conn: sqlite3.Connection,
    symbol: str,
    alert_type: str,
    confirmed_cooldown_minutes: int,
    forming_cooldown_minutes: int,
    global_confirmed_per_hour: int,
) -> tuple[bool, str]:
    """Return (allowed, reason)."""

    # Per-symbol cooldown
    if alert_type == "CONFIRMED_SETUP":
        since = minutes_ago_iso(confirmed_cooldown_minutes)
    else:
        since = minutes_ago_iso(forming_cooldown_minutes)

    recent = repo.get_recent_alerts(conn, symbol, alert_type, since)
    if recent:
        return False, f"symbol cooldown active ({alert_type})"

    # Global confirmed per-hour cap
    if alert_type == "CONFIRMED_SETUP":
        since_1h = hours_ago_iso(1)
        count = repo.count_confirmed_alerts_since(conn, since_1h)
        if count >= global_confirmed_per_hour:
            return False, f"global confirmed alert cap reached ({count}/{global_confirmed_per_hour})"

    return True, "ok"


def is_symbol_snoozed(conn: sqlite3.Connection, symbol: str) -> bool:
    return repo.is_snoozed(conn, symbol)
