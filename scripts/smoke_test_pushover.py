#!/usr/bin/env python3
"""Send a test Pushover notification to verify credentials."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from apex.config import get_settings
from apex.notifications.pushover_client import PushoverClient


async def main():
    settings = get_settings()

    if not settings.pushover_app_token or not settings.pushover_user_key:
        print("ERROR: PUSHOVER_APP_TOKEN and PUSHOVER_USER_KEY must be set in .env")
        sys.exit(1)

    client = PushoverClient(
        app_token=settings.pushover_app_token,
        user_key=settings.pushover_user_key,
        device=settings.pushover_device,
    )

    print("Sending test notification via Pushover...")
    ok = await client.send(
        title="APEX Smoke Test",
        message=(
            "<b>APEX is alive!</b>\n\n"
            "This is a test notification from your APEX alert bot.\n\n"
            "If you see this, Pushover is configured correctly."
        ),
        priority=0,
    )
    if ok:
        print("SUCCESS: Pushover notification sent.")
    else:
        print("FAILED: Pushover notification failed. Check credentials and logs.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
