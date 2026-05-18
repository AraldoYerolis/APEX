"""Reconnecting WebSocket manager for Hyperliquid candle feeds."""
from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any, Callable, Coroutine

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

OnMessageCallback = Callable[[dict], Coroutine[Any, Any, None]]


class ReconnectingWebSocket:
    def __init__(
        self,
        url: str,
        on_message: OnMessageCallback,
        subscriptions: list[dict],
        max_backoff: float = 60.0,
    ) -> None:
        self.url = url
        self.on_message = on_message
        self.subscriptions = subscriptions
        self.max_backoff = max_backoff
        self._running = False
        self._connected = False
        self._task: asyncio.Task | None = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                await self._connect()
                backoff = 1.0  # reset on successful connection
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"WebSocket error: {e}")
            finally:
                self._connected = False

            if not self._running:
                break

            jitter = random.uniform(0, backoff * 0.3)
            sleep_time = min(backoff + jitter, self.max_backoff)
            logger.info(f"Reconnecting in {sleep_time:.1f}s...")
            await asyncio.sleep(sleep_time)
            backoff = min(backoff * 2, self.max_backoff)

    async def _connect(self) -> None:
        logger.info(f"Connecting to WebSocket: {self.url}")
        async with websockets.connect(self.url, ping_interval=20, ping_timeout=10) as ws:
            self._connected = True
            logger.info("WebSocket connected — subscribing to feeds")

            for sub in self.subscriptions:
                await ws.send(json.dumps({"method": "subscribe", "subscription": sub}))

            async for raw in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw)
                    await self.on_message(msg)
                except json.JSONDecodeError as e:
                    logger.warning(f"Invalid JSON from WS: {e}")
                except Exception as e:
                    logger.error(f"Error processing WS message: {e}")
