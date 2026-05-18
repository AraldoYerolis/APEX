"""Pushover notification client."""
from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"


class PushoverClient:
    def __init__(
        self,
        app_token: str,
        user_key: str,
        device: str = "",
        default_priority: int = 0,
        sound: str = "pushover",
    ) -> None:
        self.app_token = app_token
        self.user_key = user_key
        self.device = device
        self.default_priority = default_priority
        self.sound = sound
        self._enabled = bool(app_token and user_key)

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def send(
        self,
        title: str,
        message: str,
        priority: Optional[int] = None,
        url: Optional[str] = None,
        url_title: Optional[str] = None,
        html: int = 1,
    ) -> bool:
        """Send a Pushover notification. Returns True on success."""
        if not self._enabled:
            logger.warning("Pushover not configured — skipping notification")
            return False

        payload: dict = {
            "token": self.app_token,
            "user": self.user_key,
            "title": title,
            "message": message,
            "priority": priority if priority is not None else self.default_priority,
            "sound": self.sound,
            "html": html,
        }
        if self.device:
            payload["device"] = self.device
        if url:
            payload["url"] = url
        if url_title:
            payload["url_title"] = url_title

        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(PUSHOVER_API_URL, data=payload)
                if resp.status_code == 200:
                    logger.info(f"Pushover sent: {title!r}")
                    return True
                elif resp.status_code == 429:
                    logger.warning(f"Pushover rate limited (attempt {attempt+1})")
                else:
                    logger.error(
                        f"Pushover error {resp.status_code}: {resp.text} (attempt {attempt+1})"
                    )
            except httpx.TransportError as e:
                logger.warning(f"Pushover transport error (attempt {attempt+1}): {e}")

        logger.error(f"Pushover failed after 3 attempts: {title!r}")
        return False
