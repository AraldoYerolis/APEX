#!/usr/bin/env python3
"""Send a simulated APEX alert via Pushover to test message formatting.

Does not require live market conditions or a running bot.
Uses current templates directly.

Usage:
  PYTHONPATH=src python scripts/send_simulated_alert.py --type SETUP_FORMING
  PYTHONPATH=src python scripts/send_simulated_alert.py --type CONFIRMED_SETUP
  PYTHONPATH=src python scripts/send_simulated_alert.py --type CONFIRMED_SETUP --symbol ETH --direction SHORT
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from apex.actions.tokens import generate_token
from apex.config import get_settings
from apex.db.models import Alert
from apex.notifications.pushover_client import PushoverClient
from apex.notifications.templates import format_confirmed_alert, format_forming_alert
from apex.utils.ids import new_uid
from apex.utils.time import minutes_from_now, utcnow_iso


def _build_simulated_alert(
    alert_type: str,
    symbol: str,
    direction: str,
) -> Alert:
    """Build a fake alert with plausible values for formatting tests."""
    alert_uid = new_uid()

    # Simulated prices — realistic-looking BTC/ETH figures scale poorly for all symbols,
    # so we use abstract round numbers.
    if direction == "LONG":
        ref_price = 100.00
        stop = 98.25
        t1r = 101.75
        t2r = 103.50
        entry_low = 99.80
        entry_high = 100.20
    else:
        ref_price = 100.00
        stop = 101.75
        t1r = 98.25
        t2r = 96.50
        entry_low = 99.80
        entry_high = 100.20

    stop_distance_pct = abs(ref_price - stop) / ref_price * 100
    risk_usd = 1.00  # default $100 account × 1%
    suggested_notional = risk_usd / (stop_distance_pct / 100)

    return Alert(
        alert_uid=alert_uid,
        symbol=symbol,
        direction=direction,
        alert_type=alert_type,
        setup_type="TREND_PULLBACK",
        status="SENT",
        reference_price=ref_price,
        entry_low=entry_low,
        entry_high=entry_high,
        stop_price=stop,
        target_1r=t1r,
        target_2r=t2r,
        invalidation_price=stop,
        risk_usd=risk_usd,
        suggested_notional_usd=suggested_notional,
        stop_distance_pct=stop_distance_pct,
        confidence_score=0.75,
        expires_at=minutes_from_now(15),
        sent_at=utcnow_iso(),
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Send a simulated APEX alert via Pushover")
    parser.add_argument(
        "--type",
        choices=["SETUP_FORMING", "CONFIRMED_SETUP"],
        default="CONFIRMED_SETUP",
        help="Alert type to simulate (default: CONFIRMED_SETUP)",
    )
    parser.add_argument(
        "--symbol",
        default="BTC",
        help="Symbol to use in alert (default: BTC)",
    )
    parser.add_argument(
        "--direction",
        choices=["LONG", "SHORT"],
        default="LONG",
        help="Trade direction (default: LONG)",
    )
    args = parser.parse_args()

    settings = get_settings()

    if not settings.pushover_app_token or not settings.pushover_user_key:
        print("ERROR: PUSHOVER_APP_TOKEN and PUSHOVER_USER_KEY must be set in .env")
        sys.exit(1)

    alert_type = args.type
    symbol = args.symbol.upper()
    direction = args.direction.upper()

    print(f"Simulating {alert_type} | {symbol} {direction}")
    print()

    alert = _build_simulated_alert(alert_type, symbol, direction)
    token = generate_token(alert.alert_uid, settings.action_token_secret, settings.action_token_ttl_hours)

    simulated_rsi = 48.5 if direction == "LONG" else 52.3
    simulated_atr = 1.75
    trend_reason = (
        "EMA9>EMA21 and close above VWAP" if direction == "LONG"
        else "EMA9<EMA21 and close below VWAP"
    )

    if alert_type == "CONFIRMED_SETUP":
        title, body = format_confirmed_alert(
            alert=alert,
            rsi_val=simulated_rsi,
            atr_val=simulated_atr,
            trend_reason=trend_reason,
            token=token,
            base_url=settings.apex_public_base_url,
        )
        priority = settings.pushover_confirmed_priority
    else:
        title, body = format_forming_alert(
            alert=alert,
            rsi_val=simulated_rsi,
            atr_val=simulated_atr,
            trend_reason=trend_reason,
            token=token,
            base_url=settings.apex_public_base_url,
        )
        priority = settings.pushover_default_priority

    print("--- Title ---")
    print(title)
    print()
    print("--- Body (raw) ---")
    print(body)
    print()

    pushover = PushoverClient(
        app_token=settings.pushover_app_token,
        user_key=settings.pushover_user_key,
        device=settings.pushover_device,
        default_priority=settings.pushover_default_priority,
        sound=settings.pushover_sound,
    )

    print("Sending via Pushover...")
    ok = await pushover.send(title=title, message=body, priority=priority)
    if ok:
        print(f"SUCCESS: Simulated {alert_type} sent for {symbol} {direction}.")
    else:
        print("FAILED: Pushover notification failed. Check credentials and logs.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
