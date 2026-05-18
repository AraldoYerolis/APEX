"""Action handler logic for alert and trade actions."""
from __future__ import annotations

import logging

from apex.actions.tokens import generate_token, validate_token
from apex.config import get_settings
from apex.db.connection import get_connection
from apex.db import repository as repo
from apex.db.models import PaperTrade
from apex.utils.ids import new_uid
from apex.utils.time import utcnow_iso, minutes_from_now

logger = logging.getLogger(__name__)


def _validate(uid: str, token: str) -> tuple[bool, str]:
    settings = get_settings()
    return validate_token(uid, token, settings.action_token_secret)


async def handle_enter(alert_uid: str, token: str) -> dict:
    valid, reason = _validate(alert_uid, token)
    if not valid:
        return {
            "title": "Invalid Token",
            "html": f"<h2>Invalid Token</h2><p>{reason}</p>",
        }

    conn = get_connection()
    settings = get_settings()

    alert_row = repo.get_alert_by_uid(conn, alert_uid)
    if alert_row is None:
        return {"title": "Not Found", "html": "<h2>Alert not found</h2>"}

    if alert_row["status"] in ("SKIPPED", "EXPIRED"):
        return {
            "title": "Already Closed",
            "html": f"<h2>Alert already {alert_row['status'].lower()}</h2>",
        }

    if alert_row["status"] == "ENTERED":
        return {
            "title": "Already Entered",
            "html": "<h2>Already marked as entered</h2><p>Check your open trades.</p>",
        }

    # Mark alert as entered
    repo.update_alert_status(conn, alert_uid, "ENTERED")

    # Create paper trade
    entry_price = alert_row["entry_high"] or alert_row["reference_price"] or 0.0
    trade = PaperTrade(
        trade_uid=new_uid(),
        alert_id=alert_row["id"],
        symbol=alert_row["symbol"],
        direction=alert_row["direction"],
        setup_type=alert_row["setup_type"],
        entry_price=entry_price,
        stop_price=alert_row["stop_price"] or 0.0,
        target_1r=alert_row["target_1r"] or 0.0,
        target_2r=alert_row["target_2r"] or 0.0,
        risk_usd=alert_row["risk_usd"] or 0.0,
        suggested_notional_usd=alert_row["suggested_notional_usd"],
        opened_at=utcnow_iso(),
    )
    repo.insert_paper_trade(conn, trade)

    repo.log_event(
        conn,
        "TRADE_ENTERED",
        f"Paper trade opened: {trade.symbol} {trade.direction} @ {trade.entry_price}",
        metadata={"trade_uid": trade.trade_uid, "alert_uid": alert_uid},
    )
    logger.info(f"Paper trade created: {trade.trade_uid} for {trade.symbol} {trade.direction}")

    html = (
        f"<h2>Marked Entered ✓</h2>"
        f"<p class='symbol'>{alert_row['symbol']} {alert_row['direction']}</p>"
        f"<p class='detail'>Entry: {entry_price:.4f}<br>"
        f"Stop: {alert_row['stop_price']:.4f}<br>"
        f"Target 1: {alert_row['target_1r']:.4f}<br>"
        f"Target 2: {alert_row['target_2r']:.4f}<br>"
        f"Risk: ${alert_row['risk_usd']:.2f}</p>"
        f"<p class='detail'>A follow-up will be sent in "
        f"{settings.followup_after_enter_minutes} minutes.</p>"
    )
    return {"title": "Entered", "html": html}


async def handle_skip(alert_uid: str, token: str) -> dict:
    valid, reason = _validate(alert_uid, token)
    if not valid:
        return {"title": "Invalid Token", "html": f"<h2>Invalid Token</h2><p>{reason}</p>"}

    conn = get_connection()
    alert_row = repo.get_alert_by_uid(conn, alert_uid)
    if alert_row is None:
        return {"title": "Not Found", "html": "<h2>Alert not found</h2>"}

    if alert_row["status"] == "ENTERED":
        return {"title": "Already Entered", "html": "<h2>Already entered — cannot skip</h2>"}

    repo.update_alert_status(conn, alert_uid, "SKIPPED")
    repo.log_event(conn, "ALERT_SKIPPED", f"Alert skipped: {alert_uid}")

    return {
        "title": "Skipped",
        "html": f"<h2>Skipped ✓</h2><p class='symbol'>{alert_row['symbol']}</p>"
                f"<p>Setup skipped.</p>",
    }


async def handle_snooze(alert_uid: str, token: str) -> dict:
    valid, reason = _validate(alert_uid, token)
    if not valid:
        return {"title": "Invalid Token", "html": f"<h2>Invalid Token</h2><p>{reason}</p>"}

    conn = get_connection()
    settings = get_settings()

    alert_row = repo.get_alert_by_uid(conn, alert_uid)
    if alert_row is None:
        return {"title": "Not Found", "html": "<h2>Alert not found</h2>"}

    symbol = alert_row["symbol"]
    until = minutes_from_now(settings.snooze_minutes)
    repo.add_snooze(conn, symbol, until, reason="user action")
    repo.update_alert_status(conn, alert_uid, "SNOOZED")
    repo.log_event(
        conn,
        "SYMBOL_SNOOZED",
        f"Snoozed {symbol} until {until}",
        metadata={"alert_uid": alert_uid},
    )

    return {
        "title": "Snoozed",
        "html": (
            f"<h2>Snoozed 15m ✓</h2>"
            f"<p class='symbol'>{symbol}</p>"
            f"<p class='detail'>No alerts for {symbol} until {until}</p>"
        ),
    }


async def handle_trade_outcome(trade_uid: str, token: str, outcome: str) -> dict:
    valid, reason = _validate(trade_uid, token)
    if not valid:
        return {"title": "Invalid Token", "html": f"<h2>Invalid Token</h2><p>{reason}</p>"}

    conn = get_connection()
    settings = get_settings()

    trade_row = repo.get_trade_by_uid(conn, trade_uid)
    if trade_row is None:
        return {"title": "Not Found", "html": "<h2>Trade not found</h2>"}

    if trade_row["status"] != "OPEN":
        return {
            "title": "Already Closed",
            "html": f"<h2>Trade already {trade_row['status'].lower()}</h2>",
        }

    repo.update_trade_status(conn, trade_uid, outcome)

    # Track daily loss for LOSS outcome
    lockout_triggered = False
    if outcome == "LOSS":
        risk_usd = trade_row["risk_usd"] or 0.0
        daily = repo.add_daily_loss(conn, risk_usd, settings.max_daily_loss_usd)
        lockout_triggered = bool(daily["lockout_active"])
        if lockout_triggered:
            repo.log_event(
                conn,
                "DAILY_LOCKOUT",
                f"Daily loss lockout triggered: ${daily['planned_loss_used_usd']:.2f} / "
                f"${daily['max_planned_loss_usd']:.2f}",
                level="WARNING",
            )
            logger.warning(
                f"Daily loss lockout triggered: "
                f"${daily['planned_loss_used_usd']:.2f} / "
                f"${daily['max_planned_loss_usd']:.2f}"
            )

    repo.log_event(
        conn,
        f"TRADE_{outcome}",
        f"Trade {trade_uid} marked {outcome}: {trade_row['symbol']} {trade_row['direction']}",
        metadata={"trade_uid": trade_uid, "lockout": lockout_triggered},
    )
    logger.info(
        f"Trade outcome: {trade_uid} → {outcome} "
        f"({trade_row['symbol']} {trade_row['direction']})"
    )

    lockout_msg = (
        "<p style='color:#e94560'><b>Daily loss limit reached. "
        "No more confirmed alerts today.</b></p>"
        if lockout_triggered
        else ""
    )

    outcome_emoji = {"WIN": "🏆", "LOSS": "📉", "BREAKEVEN": "➖"}.get(outcome, "")

    return {
        "title": outcome.capitalize(),
        "html": (
            f"<h2>{outcome_emoji} {outcome.capitalize()} ✓</h2>"
            f"<p class='symbol'>{trade_row['symbol']} {trade_row['direction']}</p>"
            f"<p class='detail'>Trade result recorded.</p>"
            f"{lockout_msg}"
        ),
    }
