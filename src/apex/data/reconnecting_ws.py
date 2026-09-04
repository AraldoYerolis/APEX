"""Reconnecting WebSocket manager for Hyperliquid candle feeds."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any, Callable, Coroutine

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

OnMessageCallback = Callable[[dict], Coroutine[Any, Any, None]]

# Upstream close reasons are attacker-influenced free text; keep them bounded.
MAX_CLOSE_REASON_CHARS = 200
# Cap on the rendered missing-subscription list. Counts are always reported in
# full, so truncation degrades detail but never hides that subscriptions are
# missing. Grouping by coin keeps a 30-symbol universe well inside this.
MAX_MISSING_REPORT_CHARS = 600
MAX_IDENTITY_CHARS = 48


def _sanitize_identity(value: Any) -> str:
    """Single-line, printable, bounded rendering of a subscription field."""
    text = " ".join(str(value).split())
    text = "".join(ch for ch in text if ch.isprintable())
    if len(text) > MAX_IDENTITY_CHARS:
        text = text[:MAX_IDENTITY_CHARS] + "~"
    return text


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
        # Per-connection diagnostics (reset at the start of every _connect).
        self._connect_started_at: float | None = None
        self._messages_received = 0
        self._subscriptions_sent = False
        self._acked_subs: set[str] = set()
        self._last_gap_signature: str | None = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def messages_received(self) -> int:
        """Raw frames received during the current/most recent connection."""
        return self._messages_received

    @property
    def connection_age_seconds(self) -> float | None:
        """Seconds since the current/most recent connection attempt began."""
        if self._connect_started_at is None:
            return None
        return time.monotonic() - self._connect_started_at

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
            except ConnectionClosed as e:
                logger.warning(
                    f"WebSocket error: {e} | type={type(e).__name__} | "
                    f"{self._describe_close(e)} | {self._describe_connection()}"
                )
            except Exception as e:
                logger.warning(
                    f"WebSocket error: {e} | type={type(e).__name__} | "
                    f"{self._describe_connection()}"
                )
            finally:
                self._connected = False
                self._log_subscription_gap()

            if not self._running:
                break

            jitter = random.uniform(0, backoff * 0.3)
            sleep_time = min(backoff + jitter, self.max_backoff)
            logger.info(f"Reconnecting in {sleep_time:.1f}s...")
            await asyncio.sleep(sleep_time)
            backoff = min(backoff * 2, self.max_backoff)

    def _describe_connection(self) -> str:
        """Bounded summary of the connection that just ended."""
        age = self.connection_age_seconds
        lifetime = "unknown" if age is None else f"{age:.1f}s"
        return f"lifetime={lifetime} messages_received={self._messages_received}"

    # ------------------------------------------------ subscription reconciliation

    @staticmethod
    def _subscription_identity(sub: dict) -> str:
        """Stable identity for a subscription, from its own payload shape.

        Applied to both the outgoing payload and the `subscription` object
        echoed back in a subscriptionResponse, so the two sets are comparable.
        """
        sub_type = _sanitize_identity(sub.get("type", "?"))
        if sub_type == "candle":
            coin = _sanitize_identity(sub.get("coin", "?"))
            interval = _sanitize_identity(sub.get("interval", "?"))
            return f"{coin}:{interval}"
        parts = [sub_type]
        for key in ("coin", "interval"):
            if key in sub:
                parts.append(_sanitize_identity(sub[key]))
        return ":".join(parts)

    def _expected_identities(self) -> set[str]:
        return {self._subscription_identity(s) for s in self.subscriptions if isinstance(s, dict)}

    def _record_ack(self, msg: Any) -> None:
        """Record a subscriptionResponse ACK. Must never raise."""
        if not isinstance(msg, dict) or msg.get("channel") != "subscriptionResponse":
            return
        data = msg.get("data")
        if not isinstance(data, dict):
            return
        sub = data.get("subscription")
        if isinstance(sub, dict):
            self._acked_subs.add(self._subscription_identity(sub))

    @staticmethod
    def _format_missing(missing: set[str], expected: int) -> str:
        """Render missing identities compactly, grouped by coin.

        Counts are always exact. Enumeration is skipped entirely when nothing
        was ACKed (that is one fact, not N facts), so a dead socket cannot
        crowd out the partial-miss case that actually identifies a bad symbol.
        """
        if not missing:
            return "missing_subs=none"
        if len(missing) == expected:
            return "missing_subs=ALL"

        by_coin: dict[str, int] = {}
        for ident in missing:
            coin = ident.split(":", 1)[0]
            by_coin[coin] = by_coin.get(coin, 0) + 1

        parts = [f"{coin}({n})" for coin, n in sorted(by_coin.items())]
        text = ",".join(parts)
        if len(text) > MAX_MISSING_REPORT_CHARS:
            kept: list[str] = []
            used = 0
            for part in parts:
                if used + len(part) + 1 > MAX_MISSING_REPORT_CHARS:
                    break
                kept.append(part)
                used += len(part) + 1
            text = ",".join(kept) + f",...(+{len(parts) - len(kept)} more coins)"
        return f"missing_subs={text}"

    def _log_subscription_gap(self) -> None:
        """Reconcile expected vs ACKed subscriptions for the connection that ended.

        This is the diagnostic that survives every rejection style: a negative
        response, a separate error message, or upstream silently never ACKing.
        A subscription is reported as missing purely by absence.
        """
        try:
            if not self._subscriptions_sent:
                return
            expected = self._expected_identities()
            missing = expected - self._acked_subs
            summary = self._format_missing(missing, len(expected))
            line = (
                f"WS subscription reconciliation: expected={len(expected)} "
                f"acked={len(expected) - len(missing)} missing={len(missing)} {summary}"
            )
            signature = f"{len(expected)}|{len(missing)}|{summary}"
            repeated = signature == self._last_gap_signature
            self._last_gap_signature = signature
            if repeated:
                # Identical gap on a reconnect adds no information; stay quiet.
                logger.debug(line)
            elif missing:
                logger.warning(line)
            else:
                logger.info(line)
        except Exception as e:  # pragma: no cover - diagnostics must never break the loop
            logger.debug(f"Subscription reconciliation failed: {e}")

    @staticmethod
    def _describe_close(exc: ConnectionClosed) -> str:
        """Close code/reason from a ConnectionClosed, defensively and bounded.

        Reads the rcvd/sent Close frames rather than the .code/.reason
        shims, which are deprecated since websockets 13.1 and would emit a
        DeprecationWarning on every reconnect. `close_frame=none` is itself
        the diagnostic: neither peer sent a close frame (abnormal closure).
        """
        code: Any = None
        reason = ""
        source = "none"
        try:
            if exc.rcvd is not None:
                frame, source = exc.rcvd, "rcvd"
            elif exc.sent is not None:
                frame, source = exc.sent, "sent"
            else:
                frame = None
            if frame is not None:
                code = frame.code
                reason = frame.reason or ""
        except Exception:  # pragma: no cover - defensive
            source = "unavailable"
        if len(reason) > MAX_CLOSE_REASON_CHARS:
            reason = reason[:MAX_CLOSE_REASON_CHARS] + "...(truncated)"
        return f"close_code={code} close_reason={reason!r} close_frame={source}"

    async def _connect(self) -> None:
        logger.info(f"Connecting to WebSocket: {self.url}")
        self._connect_started_at = time.monotonic()
        self._messages_received = 0
        self._subscriptions_sent = False
        self._acked_subs = set()
        async with websockets.connect(self.url, ping_interval=20, ping_timeout=10) as ws:
            self._connected = True
            logger.info("WebSocket connected — subscribing to feeds")

            for sub in self.subscriptions:
                await ws.send(json.dumps({"method": "subscribe", "subscription": sub}))
            self._subscriptions_sent = True

            async for raw in ws:
                if not self._running:
                    break
                self._messages_received += 1
                try:
                    msg = json.loads(raw)
                    self._record_ack(msg)
                    await self.on_message(msg)
                except json.JSONDecodeError as e:
                    logger.warning(f"Invalid JSON from WS: {e}")
                except Exception as e:
                    logger.error(f"Error processing WS message: {e}")
