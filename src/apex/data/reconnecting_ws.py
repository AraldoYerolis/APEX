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

# Subscription pacing. Hyperliquid documents no per-connection subscription cap
# (its documented limit is 1000 per IP), but production has shown an unpaced
# burst of 72-80 subscribes gets the socket killed ~1.3s in, after exactly 64
# ACKs — while every session at <=64 has been healthy. Chunking spreads 80
# subscribes over ~1s to test whether the burst, rather than the count, is what
# upstream objects to. Membership and order are unchanged.
SUBSCRIPTION_CHUNK_SIZE = 16
SUBSCRIPTION_CHUNK_DELAY_SECONDS = 0.25

# The receive loop does not start until every subscription has been sent, so
# with pacing that is a ~1s window during which inbound frames only buffer.
# The websockets default max_queue=16 would pause TCP reading well inside that
# window and apply backpressure upstream — a slow-consumer disconnect that
# would be indistinguishable from the subscription ceiling under test. 512
# comfortably covers the ~160 startup frames (a response plus an initial candle
# per subscription) plus normal traffic.
WS_RECEIVE_QUEUE_SIZE = 512

# Exception text is upstream-influenced (a ConnectionClosed renders the peer's
# close reason), so it is bounded before it reaches a log line.
MAX_EXCEPTION_CHARS = 200

# Above this many subscriptions on one connection, production has been unstable.
# Observability only — nothing is truncated or blocked.
SUBSCRIPTION_COUNT_WARN_THRESHOLD = 64

# An unchanged missing-subscription gap re-warns at most this often, so a
# persistent gap can never fall silent for the life of the process.
RECONCILIATION_REWARN_SECONDS = 900.0  # 15 minutes


def _sanitize_text(value: Any, limit: int, ellipsis: str = "...(truncated)") -> str:
    """Single-line, printable, length-capped rendering of untrusted text.

    Whitespace collapses and non-printable characters are dropped, so upstream
    text can neither forge a second log line nor emit terminal escapes.
    """
    text = " ".join(str(value).split())
    text = "".join(ch for ch in text if ch.isprintable())
    if len(text) > limit:
        text = text[:limit] + ellipsis
    return text


def _sanitize_identity(value: Any) -> str:
    """Single-line, printable, bounded rendering of a subscription field."""
    return _sanitize_text(value, MAX_IDENTITY_CHARS, ellipsis="~")


class ReconnectingWebSocket:
    def __init__(
        self,
        url: str,
        on_message: OnMessageCallback,
        subscriptions: list[dict],
        max_backoff: float = 60.0,
        chunk_size: int = SUBSCRIPTION_CHUNK_SIZE,
        chunk_delay: float = SUBSCRIPTION_CHUNK_DELAY_SECONDS,
        diagnostics_enabled: bool = False,
    ) -> None:
        self.url = url
        self.on_message = on_message
        self.subscriptions = subscriptions
        self.max_backoff = max_backoff
        self.chunk_size = chunk_size
        self.chunk_delay = chunk_delay
        # Bounded, opt-in: only changes the missing-subscription summary's
        # detail level (see _format_missing). Default-off behavior (grouped
        # by coin only) is completely unchanged.
        self.diagnostics_enabled = diagnostics_enabled
        self._running = False
        self._connected = False
        self._task: asyncio.Task | None = None
        # Bumped each time a connection is actually established (handshake
        # completed, not merely attempted) — a plain `is_connected` boolean
        # cannot distinguish "still the same long-lived connection" from
        # "reconnected since you last looked", which a periodic diagnostics
        # snapshot needs in order to label a connection-change.
        self._connection_generation = 0
        # Per-connection diagnostics (reset at the start of every _connect).
        self._connect_started_at: float | None = None
        self._messages_received = 0
        self._subscriptions_sent = False
        self._subscriptions_sent_count = 0
        self._acked_subs: set[str] = set()
        self._last_gap_signature: str | None = None
        self._last_gap_warned_at: float | None = None
        self._gap_repeats_since_warning = 0

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def connection_generation(self) -> int:
        """How many times this manager has actually established a
        connection (handshake completed), not attempts — a diagnostics
        snapshot can compare this across samples to detect a reconnect that
        happened between two snapshots even if `is_connected` reads True at
        both sample times.

        Bounded, opt-in: only incremented when `diagnostics_enabled` is
        True (default off) — stays at 0 for the life of the process
        otherwise, matching every other diagnostics-only counter added to
        this class. Pre-existing, always-on connection bookkeeping
        (`messages_received`, `connection_age_seconds`, etc.) is unchanged.
        """
        return self._connection_generation

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
                    f"WebSocket error: {_sanitize_text(e, MAX_EXCEPTION_CHARS)} | "
                    f"type={type(e).__name__} | "
                    f"{self._describe_close(e)} | {self._describe_connection()}"
                )
            except Exception as e:
                logger.warning(
                    f"WebSocket error: {_sanitize_text(e, MAX_EXCEPTION_CHARS)} | "
                    f"type={type(e).__name__} | "
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
        """Bounded summary of the connection that just ended.

        `subs_sent` counts subscribe frames written, NOT acknowledged ones —
        acknowledgement is what the reconciliation line reports. It matters
        because a socket that dies mid-send suppresses reconciliation, and
        without this a low messages_received reads as "upstream said nothing"
        when the real story is "we never finished asking".
        """
        age = self.connection_age_seconds
        lifetime = "unknown" if age is None else f"{age:.1f}s"
        return (
            f"lifetime={lifetime} messages_received={self._messages_received} "
            f"subs_sent={self._subscriptions_sent_count}/{len(self.subscriptions)}"
        )

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
    def _format_missing(missing: set[str], expected: int, detailed: bool = False) -> str:
        """Render missing identities compactly, grouped by coin.

        Counts are always exact. Enumeration is skipped entirely when nothing
        was ACKed (that is one fact, not N facts), so a dead socket cannot
        crowd out the partial-miss case that actually identifies a bad symbol.

        `detailed` (only ever True when diagnostics are enabled) breaks each
        coin's count down by interval instead of one aggregate count per
        coin, so a gap limited to a single timeframe (e.g. one missing 5m ACK
        on an otherwise healthy symbol) is distinguishable from a fully
        missing symbol. Default behavior (`detailed=False`) is unchanged.
        """
        if not missing:
            return "missing_subs=none"
        if len(missing) == expected:
            return "missing_subs=ALL"

        if detailed:
            by_coin_interval: dict[str, dict[str, int]] = {}
            for ident in missing:
                coin, _, interval = ident.partition(":")
                intervals = by_coin_interval.setdefault(coin, {})
                intervals[interval] = intervals.get(interval, 0) + 1
            parts = [
                f"{coin}({','.join(f'{iv}:{n}' for iv, n in sorted(intervals.items()))})"
                for coin, intervals in sorted(by_coin_interval.items())
            ]
        else:
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
            summary = self._format_missing(missing, len(expected), detailed=self.diagnostics_enabled)
            line = (
                f"WS subscription reconciliation: expected={len(expected)} "
                f"acked={len(expected) - len(missing)} missing={len(missing)} {summary}"
            )
            signature = f"{len(expected)}|{len(missing)}|{summary}"
            now = time.monotonic()
            changed = signature != self._last_gap_signature
            self._last_gap_signature = signature

            if changed:
                due = True
            else:
                # An unchanged gap stays quiet, but never forever: re-state it
                # periodically so any recent log window carries the diagnosis.
                self._gap_repeats_since_warning += 1
                due = (
                    self._last_gap_warned_at is None
                    or now - self._last_gap_warned_at >= RECONCILIATION_REWARN_SECONDS
                )
                if due:
                    line += f" (repeats={self._gap_repeats_since_warning})"

            if not due:
                logger.debug(line)
                return

            self._last_gap_warned_at = now
            self._gap_repeats_since_warning = 0
            if missing:
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

    async def _send_subscriptions(self, ws: Any) -> None:
        """Send every subscription in order, pausing between chunks.

        Membership, order and payloads are identical to sending them in one
        burst — only the timing differs. The pause goes *between* chunks, never
        after the last message, so a set that fits in one chunk is unpaced.
        """
        total = len(self.subscriptions)
        sent = 0
        try:
            for sub in self.subscriptions:
                await ws.send(json.dumps({"method": "subscribe", "subscription": sub}))
                sent += 1
                if self.chunk_size > 0 and sent % self.chunk_size == 0 and sent < total:
                    await asyncio.sleep(self.chunk_delay)
        except Exception as e:
            # `sent` messages succeeded, so the failure is on the next one —
            # index `sent` (0-based), which is subscription number sent+1.
            failed = self.subscriptions[sent] if sent < total else None
            identity = self._subscription_identity(failed) if isinstance(failed, dict) else "?"
            logger.warning(
                f"WebSocket subscribe failed: {type(e).__name__}: "
                f"{_sanitize_text(e, MAX_EXCEPTION_CHARS)} | "
                f"sent={sent}/{total} failed_sub_number={sent + 1}/{total} "
                f"failed_sub={identity}"
            )
            raise
        finally:
            self._subscriptions_sent_count = sent
        self._subscriptions_sent = True
        logger.info(f"Subscribed to {total} feeds in chunks of {self.chunk_size}")

    async def _connect(self) -> None:
        logger.info(f"Connecting to WebSocket: {self.url}")
        self._connect_started_at = time.monotonic()
        self._messages_received = 0
        self._subscriptions_sent = False
        self._subscriptions_sent_count = 0
        self._acked_subs = set()
        async with websockets.connect(
            self.url,
            ping_interval=20,
            ping_timeout=10,
            max_queue=WS_RECEIVE_QUEUE_SIZE,
        ) as ws:
            self._connected = True
            if self.diagnostics_enabled:
                self._connection_generation += 1
            logger.info("WebSocket connected — subscribing to feeds")

            await self._send_subscriptions(ws)

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
