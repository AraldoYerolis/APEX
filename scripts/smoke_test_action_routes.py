#!/usr/bin/env python3
"""Local HTTP smoke test for APEX action routes.

Requires APEX to already be running on http://127.0.0.1:8000.

  Terminal 1:
      PYTHONPATH=src .venv/bin/python -m apex.main

  Terminal 2:
      PYTHONPATH=src .venv/bin/python scripts/smoke_test_action_routes.py

What this script does:
- Inserts synthetic smoke rows (prefixed SMOKE_) directly into the local SQLite DB
- Generates valid HMAC action tokens using ACTION_TOKEN_SECRET from .env
- Sends real HTTP GET requests against the running APEX server
- Verifies every action route (enter, skip, snooze-15, win, loss, breakeven)
- Verifies invalid-token and missing-alert/trade error paths
- Reads DB state after each route to confirm side-effects
- Restores daily_risk to its pre-smoke state in a finally block
- Cleans up all smoke rows before exit
- Prints PASS/FAIL for each check and exits non-zero on any failure

What this script does NOT do:
- Does not send Pushover notifications
- Does not enable ALERTS_ENABLED
- Does not place live trades or use private keys
- Does not change strategy logic or safety gates
- Does not permanently alter today's daily_risk row
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import httpx

from apex.actions.tokens import generate_token
from apex.config import get_settings
from apex.db.connection import close_db, init_db
from apex.db import repository as repo
from apex.db.models import Alert
from apex.utils.ids import new_uid
from apex.utils.time import minutes_from_now, utcnow_iso

BASE = "http://127.0.0.1:8000"
SMOKE_SYMBOL = "SMOKE_BTC"

_passed = 0
_failed = 0

# UIDs accumulated during the run — used for precise app_events cleanup.
_smoke_alert_uids: list[str] = []
_smoke_trade_uids: list[str] = []


# ------------------------------------------------------------------ output helpers

def _pass(label: str) -> None:
    global _passed
    _passed += 1
    print(f"  [PASS] {label}")


def _fail(label: str, detail: str = "") -> None:
    global _failed
    _failed += 1
    msg = f"  [FAIL] {label}"
    if detail:
        msg += f"\n         {detail}"
    print(msg)


def _section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


# ------------------------------------------------------------------ daily_risk snapshot/restore

def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _snapshot_daily_risk(conn) -> dict | None:
    """Return today's daily_risk row as a plain dict, or None if absent."""
    row = conn.execute(
        "SELECT * FROM daily_risk WHERE trade_date=?", (_today(),)
    ).fetchone()
    return dict(row) if row else None


def _restore_daily_risk(conn, snapshot: dict | None) -> None:
    """Restore today's daily_risk to its pre-smoke state."""
    today = _today()
    if snapshot is None:
        conn.execute("DELETE FROM daily_risk WHERE trade_date=?", (today,))
    else:
        conn.execute(
            """
            UPDATE daily_risk
            SET planned_loss_used_usd=?, lockout_active=?, updated_at=?
            WHERE trade_date=?
            """,
            (
                snapshot["planned_loss_used_usd"],
                snapshot["lockout_active"],
                snapshot.get("updated_at"),
                today,
            ),
        )
    conn.commit()


# ------------------------------------------------------------------ smoke row cleanup

def _cleanup_smoke_rows(conn) -> None:
    """Delete all rows created by this smoke run. Does not touch non-smoke rows."""

    # app_events: delete entries whose message or metadata references a smoke UID.
    # TRADE_ENTERED metadata: {"trade_uid": ..., "alert_uid": "smoke_..."}
    conn.execute(
        "DELETE FROM app_events WHERE event_type='TRADE_ENTERED' AND metadata_json LIKE '%smoke_%'"
    )
    # ALERT_SKIPPED message: "Alert skipped: smoke_..."
    conn.execute(
        "DELETE FROM app_events WHERE event_type='ALERT_SKIPPED' AND message LIKE '%smoke_%'"
    )
    # SYMBOL_SNOOZED metadata: {"alert_uid": "smoke_..."}
    conn.execute(
        "DELETE FROM app_events WHERE event_type='SYMBOL_SNOOZED' AND metadata_json LIKE '%smoke_%'"
    )
    # Trade outcome events: identified by tracked trade UIDs collected during the run.
    for uid in _smoke_trade_uids:
        conn.execute(
            "DELETE FROM app_events WHERE metadata_json LIKE ?", (f"%{uid}%",)
        )

    # Snoozes inserted for SMOKE_BTC by the snooze-15 route.
    conn.execute("DELETE FROM snoozes WHERE symbol=?", (SMOKE_SYMBOL,))

    # Paper trades linked to smoke alerts (must precede alert deletion due to FK intent).
    conn.execute(
        "DELETE FROM paper_trades WHERE alert_id IN "
        "(SELECT id FROM alerts WHERE alert_uid LIKE 'smoke_%')"
    )

    # Smoke alert rows themselves.
    conn.execute("DELETE FROM alerts WHERE alert_uid LIKE 'smoke_%'")

    conn.commit()


# ------------------------------------------------------------------ DB query helpers

def _insert_smoke_alert(conn, uid: str, status: str = "SENT") -> int:
    """Insert a minimal smoke alert row. Tracks uid for cleanup."""
    _smoke_alert_uids.append(uid)
    alert = Alert(
        alert_uid=uid,
        symbol=SMOKE_SYMBOL,
        direction="LONG",
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
        stop_distance_pct=2.0,
        expires_at=minutes_from_now(60),
        sent_at=utcnow_iso(),
    )
    return repo.insert_alert(conn, alert)


def _get_alert_status(conn, uid: str) -> str | None:
    row = conn.execute(
        "SELECT status FROM alerts WHERE alert_uid=?", (uid,)
    ).fetchone()
    return row["status"] if row else None


def _get_trade_uid_for_alert(conn, alert_uid: str) -> str | None:
    row = conn.execute(
        """
        SELECT pt.trade_uid FROM paper_trades pt
        JOIN alerts a ON pt.alert_id = a.id
        WHERE a.alert_uid = ?
        """,
        (alert_uid,),
    ).fetchone()
    trade_uid = row["trade_uid"] if row else None
    if trade_uid:
        _smoke_trade_uids.append(trade_uid)
    return trade_uid


def _get_trade_status(conn, trade_uid: str) -> str | None:
    row = conn.execute(
        "SELECT status FROM paper_trades WHERE trade_uid=?", (trade_uid,)
    ).fetchone()
    return row["status"] if row else None


def _get_daily_risk(conn) -> dict | None:
    row = conn.execute(
        "SELECT * FROM daily_risk WHERE trade_date=?", (_today(),)
    ).fetchone()
    return dict(row) if row else None


# ------------------------------------------------------------------ HTTP helpers

def _get(client: httpx.Client, path: str, token: str) -> httpx.Response:
    return client.get(
        f"{BASE}{path}", params={"token": token}, follow_redirects=True
    )


def _assert_http(resp: httpx.Response, label: str) -> bool:
    if resp.status_code != 200:
        _fail(label, f"HTTP {resp.status_code}")
        return False
    return True


def _assert_contains(resp: httpx.Response, text: str, label: str) -> bool:
    if text.lower() in resp.text.lower():
        _pass(label)
        return True
    snippet = resp.text[:200].replace("\n", " ")
    _fail(label, f"expected '{text}' in response. Got: {snippet}")
    return False


def _check_server(client: httpx.Client) -> bool:
    try:
        resp = client.get(f"{BASE}/health", timeout=3)
        if resp.status_code == 200:
            _pass("Server health check — APEX is running")
            return True
        _fail("Server health check", f"HTTP {resp.status_code}")
        return False
    except httpx.ConnectError:
        _fail(
            "Server health check",
            f"Cannot connect to {BASE}. Start APEX first:\n"
            "         PYTHONPATH=src .venv/bin/python -m apex.main",
        )
        return False


# ------------------------------------------------------------------ smoke tests

def run(conn, client: httpx.Client, settings) -> None:

    def token(uid: str) -> str:
        return generate_token(
            uid, settings.action_token_secret, settings.action_token_ttl_hours
        )

    def bad_token() -> str:
        # Syntactically valid format (expiry:sig) but wrong signature.
        expiry = int(time.time()) + 3600
        return f"{expiry}:deadbeefdeadbeefdeadbeefdeadbeef"

    # ---------------------------------------------------------------- enter

    _section("Alert: /enter")

    uid_enter = f"smoke_{new_uid()}"
    _insert_smoke_alert(conn, uid_enter)

    resp = _get(client, f"/action/alerts/{uid_enter}/enter", token(uid_enter))
    if _assert_http(resp, "enter: HTTP 200"):
        _assert_contains(resp, "Entered", "enter: response title=Entered")

    if _get_alert_status(conn, uid_enter) == "ENTERED":
        _pass("enter: DB alert status=ENTERED")
    else:
        _fail("enter: DB alert status=ENTERED",
              f"got {_get_alert_status(conn, uid_enter)}")

    trade_uid_win = _get_trade_uid_for_alert(conn, uid_enter)
    if trade_uid_win:
        _pass("enter: DB paper_trade row created")
    else:
        _fail("enter: DB paper_trade row created", "no paper_trade found")

    # Duplicate enter → idempotent
    resp2 = _get(client, f"/action/alerts/{uid_enter}/enter", token(uid_enter))
    if _assert_http(resp2, "enter (duplicate): HTTP 200"):
        _assert_contains(resp2, "Already Entered",
                         "enter (duplicate): response=Already Entered")

    # ---------------------------------------------------------------- skip

    _section("Alert: /skip")

    uid_skip = f"smoke_{new_uid()}"
    _insert_smoke_alert(conn, uid_skip)

    resp = _get(client, f"/action/alerts/{uid_skip}/skip", token(uid_skip))
    if _assert_http(resp, "skip: HTTP 200"):
        _assert_contains(resp, "Skipped", "skip: response title=Skipped")

    if _get_alert_status(conn, uid_skip) == "SKIPPED":
        _pass("skip: DB alert status=SKIPPED")
    else:
        _fail("skip: DB alert status=SKIPPED",
              f"got {_get_alert_status(conn, uid_skip)}")

    # ---------------------------------------------------------------- snooze-15

    _section("Alert: /snooze-15")

    uid_snooze = f"smoke_{new_uid()}"
    _insert_smoke_alert(conn, uid_snooze)

    resp = _get(client, f"/action/alerts/{uid_snooze}/snooze-15", token(uid_snooze))
    if _assert_http(resp, "snooze: HTTP 200"):
        _assert_contains(resp, "Snoozed", "snooze: response title=Snoozed")

    if _get_alert_status(conn, uid_snooze) == "SNOOZED":
        _pass("snooze: DB alert status=SNOOZED")
    else:
        _fail("snooze: DB alert status=SNOOZED",
              f"got {_get_alert_status(conn, uid_snooze)}")

    snooze_row = conn.execute(
        "SELECT id FROM snoozes WHERE symbol=? AND snoozed_until > ?",
        (SMOKE_SYMBOL, utcnow_iso()),
    ).fetchone()
    if snooze_row:
        _pass("snooze: DB snooze row inserted with future expiry")
    else:
        _fail("snooze: DB snooze row inserted with future expiry")

    # ---------------------------------------------------------------- trade: win

    _section("Trade: /win")

    if trade_uid_win:
        resp = _get(client, f"/action/trades/{trade_uid_win}/win", token(trade_uid_win))
        if _assert_http(resp, "win: HTTP 200"):
            _assert_contains(resp, "Win", "win: response title=Win")

        if _get_trade_status(conn, trade_uid_win) == "WIN":
            _pass("win: DB trade status=WIN")
        else:
            _fail("win: DB trade status=WIN",
                  f"got {_get_trade_status(conn, trade_uid_win)}")

        # Duplicate win → idempotent
        resp2 = _get(client, f"/action/trades/{trade_uid_win}/win", token(trade_uid_win))
        if _assert_http(resp2, "win (duplicate): HTTP 200"):
            _assert_contains(resp2, "Already Closed",
                             "win (duplicate): response=Already Closed")
    else:
        _fail("win: skipped — no trade_uid from enter step")
        _fail("win (duplicate): skipped")

    # ---------------------------------------------------------------- trade: breakeven

    _section("Trade: /breakeven")

    uid_be = f"smoke_{new_uid()}"
    _insert_smoke_alert(conn, uid_be)
    _get(client, f"/action/alerts/{uid_be}/enter", token(uid_be))
    trade_uid_be = _get_trade_uid_for_alert(conn, uid_be)

    if trade_uid_be:
        resp = _get(client, f"/action/trades/{trade_uid_be}/breakeven",
                    token(trade_uid_be))
        if _assert_http(resp, "breakeven: HTTP 200"):
            _assert_contains(resp, "Breakeven",
                             "breakeven: response title=Breakeven")

        if _get_trade_status(conn, trade_uid_be) == "BREAKEVEN":
            _pass("breakeven: DB trade status=BREAKEVEN")
        else:
            _fail("breakeven: DB trade status=BREAKEVEN",
                  f"got {_get_trade_status(conn, trade_uid_be)}")
    else:
        _fail("breakeven: skipped — no trade_uid from enter step")

    # ---------------------------------------------------------------- trade: loss + daily risk

    _section("Trade: /loss + daily risk")

    uid_loss = f"smoke_{new_uid()}"
    _insert_smoke_alert(conn, uid_loss)
    _get(client, f"/action/alerts/{uid_loss}/enter", token(uid_loss))
    trade_uid_loss = _get_trade_uid_for_alert(conn, uid_loss)

    if trade_uid_loss:
        resp = _get(client, f"/action/trades/{trade_uid_loss}/loss",
                    token(trade_uid_loss))
        if _assert_http(resp, "loss: HTTP 200"):
            _assert_contains(resp, "Loss", "loss: response title=Loss")

        if _get_trade_status(conn, trade_uid_loss) == "LOSS":
            _pass("loss: DB trade status=LOSS")
        else:
            _fail("loss: DB trade status=LOSS",
                  f"got {_get_trade_status(conn, trade_uid_loss)}")

        daily = _get_daily_risk(conn)
        if daily and daily["planned_loss_used_usd"] > 0:
            _pass(
                f"loss: DB daily_risk incremented "
                f"(used=${daily['planned_loss_used_usd']:.2f} / "
                f"max=${daily['max_planned_loss_usd']:.2f})"
            )
        else:
            _fail("loss: DB daily_risk incremented", f"got {daily}")
    else:
        _fail("loss: skipped — no trade_uid from enter step")

    # ---------------------------------------------------------------- error paths

    _section("Error paths")

    # Invalid token on alert route
    uid_badt = f"smoke_{new_uid()}"
    _insert_smoke_alert(conn, uid_badt)
    resp = _get(client, f"/action/alerts/{uid_badt}/enter", bad_token())
    if _assert_http(resp, "invalid token: HTTP 200"):
        _assert_contains(resp, "Invalid Token",
                         "invalid token: response=Invalid Token")

    # Missing alert UID
    missing_uid = f"smoke_missing_{new_uid()}"
    resp = _get(client, f"/action/alerts/{missing_uid}/enter", token(missing_uid))
    if _assert_http(resp, "missing alert: HTTP 200"):
        _assert_contains(resp, "Not Found",
                         "missing alert: response=Not Found")

    # Missing trade UID
    missing_trade = f"smoke_missing_trade_{new_uid()}"
    resp = _get(client, f"/action/trades/{missing_trade}/win", token(missing_trade))
    if _assert_http(resp, "missing trade: HTTP 200"):
        _assert_contains(resp, "Not Found",
                         "missing trade: response=Not Found")


# ------------------------------------------------------------------ entrypoint

def main() -> None:
    settings = get_settings()
    conn = init_db(settings.apex_db_path)

    print("=" * 52)
    print("APEX Action Route Smoke Test")
    print("=" * 52)
    print(f"DB:     {settings.apex_db_path}")
    print(f"Server: {BASE}")
    print(f"Symbol: {SMOKE_SYMBOL}  (all rows cleaned up after run)")

    daily_snapshot = _snapshot_daily_risk(conn)
    if daily_snapshot:
        print(
            f"daily_risk snapshot: used=${daily_snapshot['planned_loss_used_usd']:.2f}, "
            f"lockout={bool(daily_snapshot['lockout_active'])}"
        )
    else:
        print("daily_risk snapshot: none (row will be deleted after run)")

    try:
        with httpx.Client(timeout=10) as client:
            if not _check_server(client):
                print("\nABORTED: start APEX before running this script.")
                sys.exit(1)

            run(conn, client, settings)

    finally:
        print("\n--- Cleanup ---")
        _cleanup_smoke_rows(conn)
        print("  Smoke rows deleted (alerts, paper_trades, snoozes, app_events)")
        _restore_daily_risk(conn, daily_snapshot)
        if daily_snapshot:
            print(
                f"  daily_risk restored: used=${daily_snapshot['planned_loss_used_usd']:.2f}, "
                f"lockout={bool(daily_snapshot['lockout_active'])}"
            )
        else:
            print("  daily_risk row removed (was not present before smoke run)")
        close_db()
        print("  DB connection closed")

    print()
    print("=" * 52)
    total = _passed + _failed
    print(f"Results: {_passed}/{total} passed, {_failed} failed")
    print("=" * 52)

    if _failed:
        print("\nSome checks failed. See [FAIL] lines above.")
        sys.exit(1)
    else:
        print("\nAll checks passed.")


if __name__ == "__main__":
    main()
