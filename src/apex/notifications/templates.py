"""Alert message templates."""
from __future__ import annotations

from typing import Optional

from apex.db.models import Alert, PaperTrade


def _fmt(price: Optional[float], decimals: int = 4) -> str:
    if price is None:
        return "N/A"
    return f"{price:.{decimals}f}"


def _action_links(
    base_url: str,
    alert_uid: str,
    token: str,
    include_enter: bool = True,
) -> str:
    if not base_url:
        return "(Action links unavailable — APEX_PUBLIC_BASE_URL not set)"
    lines = []
    if include_enter:
        lines.append(f'<a href="{base_url}/action/alerts/{alert_uid}/enter?token={token}">Enter</a>')
    lines.append(f'<a href="{base_url}/action/alerts/{alert_uid}/skip?token={token}">Skip</a>')
    lines.append(
        f'<a href="{base_url}/action/alerts/{alert_uid}/snooze-15?token={token}">Snooze 15m</a>'
    )
    return "\n".join(lines)


def format_forming_alert(
    alert: Alert,
    rsi_val: float,
    atr_val: float,
    trend_reason: str,
    token: str,
    base_url: str,
) -> tuple[str, str]:
    """Returns (title, body)."""
    direction_label = "LONG" if alert.direction == "LONG" else "SHORT"
    title = f"APEX: {alert.symbol} {direction_label} forming"

    rsi_status = "cooling" if alert.direction == "LONG" else "rising"
    bias_label = "bullish" if alert.direction == "LONG" else "bearish"

    links = _action_links(base_url, alert.alert_uid, token, include_enter=False)

    body = (
        f"<b>{alert.symbol} {direction_label}</b> setup forming\n\n"
        f"Strategy: TREND_PULLBACK\n"
        f"15m bias: {bias_label} ({trend_reason})\n"
        f"5m status: pullback near VWAP\n"
        f"RSI: {rsi_val:.1f} and {rsi_status}\n"
        f"ATR: {_fmt(atr_val)}\n"
        f"Current: {_fmt(alert.reference_price)}\n\n"
        f"This is an early warning, not an entry.\n"
        f"Use this time to open VPN / login / watch chart.\n\n"
        f"Expires: {alert.expires_at or 'N/A'}\n\n"
        f"{links}"
    )
    return title, body


def format_confirmed_alert(
    alert: Alert,
    rsi_val: float,
    atr_val: float,
    trend_reason: str,
    token: str,
    base_url: str,
) -> tuple[str, str]:
    """Returns (title, body)."""
    direction_label = "LONG" if alert.direction == "LONG" else "SHORT"
    title = f"APEX: {alert.symbol} {direction_label} confirmed"

    bias_label = "bullish" if alert.direction == "LONG" else "bearish"
    setup_desc = (
        "5m pullback reclaimed VWAP"
        if alert.direction == "LONG"
        else "5m bounce rejected VWAP"
    )

    links = _action_links(base_url, alert.alert_uid, token, include_enter=True)

    body = (
        f"<b>{alert.symbol} {direction_label}</b> confirmed setup\n\n"
        f"Strategy: TREND_PULLBACK\n"
        f"Bias: {trend_reason}\n"
        f"Setup: {setup_desc}\n"
        f"RSI: {rsi_val:.1f}\n"
        f"ATR: {_fmt(atr_val)}\n\n"
        f"Entry zone: {_fmt(alert.entry_low)} – {_fmt(alert.entry_high)}\n"
        f"Stop: {_fmt(alert.stop_price)}\n"
        f"Invalidation: {_fmt(alert.invalidation_price)}\n\n"
        f"Target 1 (1R): {_fmt(alert.target_1r)}\n"
        f"Target 2 (2R): {_fmt(alert.target_2r)}\n\n"
        f"Account: ${alert.risk_usd and alert.risk_usd / 0.01:.0f} model\n"
        f"Risk: ${_fmt(alert.risk_usd, 2)}\n"
        f"Suggested notional: ${_fmt(alert.suggested_notional_usd, 2)}\n"
        f"Stop distance: {_fmt(alert.stop_distance_pct, 2)}%\n\n"
        f"Expires in: 15 minutes\n\n"
        f"Actions:\n{links}"
    )
    return title, body


def format_followup_message(
    trade: PaperTrade,
    token: str,
    base_url: str,
) -> tuple[str, str]:
    """Returns (title, body) for outcome follow-up."""
    direction_label = trade.direction
    title = f"APEX: Mark {trade.symbol} outcome"

    if not base_url:
        links = "(Action links unavailable — APEX_PUBLIC_BASE_URL not set)"
    else:
        links = (
            f'<a href="{base_url}/action/trades/{trade.trade_uid}/win?token={token}">Win</a>\n'
            f'<a href="{base_url}/action/trades/{trade.trade_uid}/loss?token={token}">Loss</a>\n'
            f'<a href="{base_url}/action/trades/{trade.trade_uid}/breakeven?token={token}">Breakeven</a>'
        )

    body = (
        f"You entered <b>{trade.symbol} {direction_label}</b> from APEX alert.\n\n"
        f"Entry: {_fmt(trade.entry_price)}\n"
        f"Stop: {_fmt(trade.stop_price)}\n"
        f"Target 1: {_fmt(trade.target_1r)}\n"
        f"Target 2: {_fmt(trade.target_2r)}\n"
        f"Risk: ${trade.risk_usd:.2f}\n\n"
        f"Mark result:\n{links}"
    )
    return title, body
