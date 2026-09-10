"""Hyperliquid public API client."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any, Awaitable, Callable, Optional

import httpx

logger = logging.getLogger(__name__)


class RestBudgetUnavailable(Exception):
    """Raised for a reconciliation-sourced request when the shared REST
    budget cannot admit it right now (no wait/retry-storm attempted — the
    caller, e.g. CandleReconciler, is expected to defer to a later tick).
    Never raised for a non-reconciliation call, which preserves its
    existing best-effort-wait-then-fail behavior instead.
    """


# ---------------------------------------------------------------------------
# Shared rolling-window REST rate limiter
# ---------------------------------------------------------------------------
# Hyperliquid's documented aggregate REST limit is 1200 weight/minute
# (fetched from https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits
# by the coordinator, 2026-09-09); this app conservatively reserves against a
# much smaller self-imposed ceiling shared by every call this single
# HyperliquidClient instance makes (ordinary meta/backfill/candle calls and
# reconciliation alike), plus a tighter reconciliation-only sub-ceiling, so
# reconciliation can never crowd out the existing signal-scan-critical path.
# This is a per-process, per-instance guarantee only — it says nothing about
# any other client/process sharing the same IP.
DEFAULT_TOTAL_LIMIT_PER_MINUTE = 600
DEFAULT_RECONCILIATION_LIMIT_PER_MINUTE = 300
RATE_WINDOW_SECONDS = 60.0
# Hard bound on the rolling reservation history so a pathological burst of
# tiny reservations cannot grow this list without bound; large enough that
# the default limits could never legitimately fill it within one window.
MAX_RATE_HISTORY_ENTRIES = 4096
# Conservative fallback cooldown for a 429 with no usable Retry-After.
DEFAULT_429_COOLDOWN_SECONDS = 60.0
# Bounds an attacker/upstream-influenced Retry-After value to something sane.
MAX_RETRY_AFTER_SECONDS = 300.0
# Same conservative cooldown applied when a response returns unexpectedly
# more rows than were reserved for.
OVERSIZED_RESPONSE_COOLDOWN_SECONDS = 30.0


def _parse_retry_after(value: Any) -> Optional[float]:
    """Bounded, defensive parse of a Retry-After header value (seconds
    form only — the HTTP-date form is not handled, and simply falls back to
    the conservative default cooldown like any other unparseable value).
    """
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


class RestRateLimiter:
    """Rolling 60-second weighted admission gate shared across every actual
    HTTP attempt (including retries) made by one HyperliquidClient instance.

    Reservations are recorded at the moment of admission (immediately before
    the actual `client.post`, not at queue/request-construction time) and
    are never refunded for a subsequent error — an attempt that is admitted
    and then fails on the wire still counts against the window, matching
    "every attempt counts". `clock`/`sleep` are injectable so tests never
    depend on real wall-clock time or actual sleeping.
    """

    def __init__(
        self,
        total_per_minute: int = DEFAULT_TOTAL_LIMIT_PER_MINUTE,
        reconciliation_per_minute: int = DEFAULT_RECONCILIATION_LIMIT_PER_MINUTE,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._total_limit = total_per_minute
        self._recon_limit = reconciliation_per_minute
        self._clock = clock
        self._sleep = sleep
        # Lazily created so the Lock binds to whichever event loop is
        # actually running when it's first acquired, matching the same
        # rationale as apex.main._get_ws_lifecycle_lock.
        self._lock: Optional[asyncio.Lock] = None
        # [(monotonic_ts, weight, is_reconciliation), ...] oldest-first.
        self._history: list[tuple[float, int, bool]] = []
        self._cooldown_until: Optional[float] = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _prune(self, now: float) -> None:
        # Strict cutoff: an entry exactly RATE_WINDOW_SECONDS old has fully
        # aged out of the rolling window (see _time_until_free, which
        # returns exactly RATE_WINDOW_SECONDS for a just-reserved entry —
        # a `>=` cutoff here would make that wait hint perpetually
        # insufficient by one instant).
        cutoff = now - RATE_WINDOW_SECONDS
        if self._history and self._history[0][0] > cutoff:
            return
        self._history = [e for e in self._history if e[0] > cutoff]

    @staticmethod
    def _time_until_free(entries: list[tuple[float, int, bool]], now: float) -> float:
        if not entries:
            return 0.0
        oldest_ts = entries[0][0]
        return max(oldest_ts + RATE_WINDOW_SECONDS - now, 0.0)

    async def _try_reserve(self, weight: int, reconciliation: bool) -> tuple[bool, float]:
        async with self._get_lock():
            now = self._clock()
            self._prune(now)
            if self._cooldown_until is not None:
                if now < self._cooldown_until:
                    return False, self._cooldown_until - now
                self._cooldown_until = None
            total = sum(w for _, w, _ in self._history)
            if total + weight > self._total_limit:
                return False, self._time_until_free(self._history, now)
            if reconciliation:
                recon_entries = [e for e in self._history if e[2]]
                recon_total = sum(w for _, w, _ in recon_entries)
                if recon_total + weight > self._recon_limit:
                    return False, self._time_until_free(recon_entries, now)
            self._history.append((now, weight, reconciliation))
            if len(self._history) > MAX_RATE_HISTORY_ENTRIES:
                self._history = self._history[-MAX_RATE_HISTORY_ENTRIES:]
            return True, 0.0

    async def acquire(
        self,
        weight: int,
        *,
        reconciliation: bool = False,
        wait: bool = False,
        max_wait_seconds: float = RATE_WINDOW_SECONDS,
    ) -> bool:
        """Attempt to admit a request of `weight`. Non-blocking by default
        (`wait=False`) — returns immediately, admitting or refusing. With
        `wait=True`, on refusal, sleeps once (bounded to `max_wait_seconds`,
        outside any lock) and retries exactly once more — deliberately not a
        retry loop, to keep any long-lived waiting bounded (see
        "Limit long-lived waiting tasks" requirement).

        The default `max_wait_seconds` is `RATE_WINDOW_SECONDS`: a
        window-only refusal's own `wait_hint` (see `_time_until_free`) can
        never exceed the rolling window's width, so this bound is exactly
        enough to always observe genuine window-freed capacity — never an
        arbitrary shorter cutoff that would truncate the wait before
        capacity that is *about to* become available actually does, which
        previously caused an ordinary call to be misreported as a failed
        upstream request purely due to its own temporarily-full budget. A
        refusal caused instead by an active cooldown (429/oversized-response)
        that outlasts this same bound is still refused after the wait — a
        documented, bounded fail-closed outcome, not a retry storm.
        """
        admitted, wait_hint = await self._try_reserve(weight, reconciliation)
        if admitted or not wait:
            return admitted
        delay = min(wait_hint, max_wait_seconds)
        if delay > 0:
            await self._sleep(delay)
        admitted, _ = await self._try_reserve(weight, reconciliation)
        return admitted

    async def note_429(self, retry_after: Optional[float]) -> None:
        """Record a 429: apply a bounded cooldown (the response's own
        Retry-After if valid, else a conservative fixed default) during
        which every subsequent acquire() is refused outright. Never clears
        or refunds existing reservations.
        """
        async with self._get_lock():
            now = self._clock()
            cooldown = retry_after if retry_after is not None else DEFAULT_429_COOLDOWN_SECONDS
            candidate = now + cooldown
            if self._cooldown_until is None or candidate > self._cooldown_until:
                self._cooldown_until = candidate

    async def note_oversized_response(self, extra_weight: int, *, reconciliation: bool = False) -> None:
        """Charge conservative extra debt plus a cooldown when a response
        returned unexpectedly more rows than were reserved for. This is a
        defensive local guard only — it says nothing about any other
        client/process's own usage of the same shared upstream IP budget.

        `reconciliation=True` marks the extra debt as counting toward the
        reconciliation sub-budget too (not just the aggregate total) — an
        oversized reconciliation response must not be able to silently
        exceed the tighter reconciliation-only ceiling.
        """
        async with self._get_lock():
            now = self._clock()
            self._prune(now)
            if extra_weight > 0:
                self._history.append((now, extra_weight, reconciliation))
            candidate = now + OVERSIZED_RESPONSE_COOLDOWN_SECONDS
            if self._cooldown_until is None or candidate > self._cooldown_until:
                self._cooldown_until = candidate


def estimate_candle_snapshot_weight(max_rows: int) -> int:
    """Conservative reservation for a candleSnapshot request expected to
    return at most `max_rows` rows: base weight 20 plus one additional unit
    per (rounded-up) 60 rows — the coordinator's documented interpretation
    of Hyperliquid's per-60-items weight addition, not an exact measured
    provider formula. Callers with no finite bound (open-ended/unsupported
    windows) should pass the documented maximum of 5000.
    """
    max_rows = max(int(max_rows), 0)
    return 20 + math.ceil(max_rows / 60)


# Hyperliquid's documented maximum recent candle rows returned by one
# candleSnapshot request (see rate-limits/info-endpoint docs cited above).
MAX_CANDLE_SNAPSHOT_ROWS = 5000
# CandleReconciler requests at most 60 target bars but pads the boundary,
# so a response can return up to 62 rows — reserve for that, never fewer.
RECONCILIATION_REQUEST_WEIGHT = estimate_candle_snapshot_weight(62)

# Kept local to this module (not imported from apex.data.candle_store) —
# used only to compute a conservative REST weight reservation for a finite
# candleSnapshot window, mirroring candle_store.TIMEFRAME_DURATION_MS. An
# unrecognized/unsupported interval simply falls back to the open-ended
# worst-case reservation (see get_candle_snapshot) rather than guessing.
_INTERVAL_DURATION_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 3 * 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
}

# Hyperliquid's documented per-call weights (fetched by the coordinator from
# https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits,
# 2026-09-09): allMids is 2, ordinary meta/metaAndAssetCtxs is 20.
ALL_MIDS_WEIGHT = 2
META_WEIGHT = 20


class HyperliquidClient:
    def __init__(
        self,
        info_url: str = "https://api.hyperliquid.xyz/info",
        *,
        rate_limiter: Optional[RestRateLimiter] = None,
    ) -> None:
        self.info_url = info_url
        # Shared across every call this instance makes — see RestRateLimiter.
        self.rate_limiter = rate_limiter if rate_limiter is not None else RestRateLimiter()

    async def _post(
        self,
        payload: dict,
        retries: int = 3,
        *,
        weight: int = 20,
        reconciliation: bool = False,
    ) -> Optional[Any]:
        """POST to info endpoint with exponential backoff.

        Every actual HTTP attempt (including retries) is gated by the
        shared rate limiter immediately before it is made — not once at the
        top of this method — so a retry genuinely counts again. A
        reconciliation-sourced call uses exactly one HTTP attempt
        (CandleReconciler owns its own outer bounded retry/backoff across
        ticks) and never waits for budget: an unavailable reservation raises
        RestBudgetUnavailable so the caller can defer without this
        consuming a failed-attempt/backoff cycle. A non-reconciliation call
        preserves its existing capped-retry behavior, waiting (bounded to
        the rolling window's width — see RestRateLimiter.acquire) for
        budget before falling back to this attempt's normal failure path,
        so a temporarily-full own budget that frees in time is never
        reported as a failed upstream request.
        """
        effective_retries = 1 if reconciliation else retries
        delay = 1.0
        for attempt in range(effective_retries):
            admitted = await self.rate_limiter.acquire(
                weight, reconciliation=reconciliation, wait=not reconciliation,
            )
            if not admitted:
                if reconciliation:
                    raise RestBudgetUnavailable("shared REST budget unavailable")
                logger.warning(
                    f"Hyperliquid REST budget unavailable (attempt {attempt+1}); deferring"
                )
            else:
                try:
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        resp = await client.post(self.info_url, json=payload)
                    resp.raise_for_status()
                    return resp.json()
                except httpx.HTTPStatusError as e:
                    status = e.response.status_code
                    if status == 429:
                        retry_after = _parse_retry_after(e.response.headers.get("Retry-After"))
                        await self.rate_limiter.note_429(retry_after)
                        logger.warning(
                            f"Hyperliquid HTTP 429 (attempt {attempt+1}); cooling down, not retrying immediately"
                        )
                        return None
                    logger.warning(f"Hyperliquid HTTP {status} (attempt {attempt+1}): {e}")
                except httpx.TransportError as e:
                    logger.warning(f"Hyperliquid transport error (attempt {attempt+1}): {e}")
                except Exception as e:
                    logger.error(f"Hyperliquid unexpected error (attempt {attempt+1}): {e}")

            if attempt < effective_retries - 1:
                await asyncio.sleep(delay)
                delay *= 2

        if not reconciliation:
            logger.error(f"Hyperliquid request failed after {effective_retries} attempts")
        return None

    async def get_perp_meta(self) -> Optional[dict]:
        """Fetch perpetual market metadata.

        Returns dict with 'universe' key listing all perp assets.
        """
        result = await self._post({"type": "meta"}, weight=META_WEIGHT)
        if result is None:
            return None
        if not isinstance(result, dict) or "universe" not in result:
            logger.error(f"Unexpected meta response structure: {type(result)}")
            return None
        return result

    async def get_all_mids(self) -> Optional[dict[str, str]]:
        """Fetch all mid prices. Returns {symbol: mid_price_str}."""
        result = await self._post({"type": "allMids"}, weight=ALL_MIDS_WEIGHT)
        if result is None:
            return None
        if not isinstance(result, dict):
            logger.error(f"Unexpected allMids response: {type(result)}")
            return None
        return result

    async def get_candle_snapshot(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: Optional[int] = None,
        *,
        reconciliation: bool = False,
        weight: Optional[int] = None,
    ) -> Optional[list[dict]]:
        """Fetch historical candle snapshot.

        interval: '1m', '3m', '5m', '15m', etc.
        start_time / end_time: Unix milliseconds.

        Returns list of candle dicts.

        `reconciliation`/`weight` are optional, keyword-only, default-safe
        additions for the bounded closed-candle reconciliation path (see
        apex.data.candle_reconciler.CandleReconciler): every existing
        positional call site (backfill, tests) is unaffected. `weight`
        overrides the conservative timestamp-derived reservation — used by
        CandleReconciler, which always requests a small bounded row count
        and reserves a fixed, known-safe weight for it (see
        RECONCILIATION_REQUEST_WEIGHT) rather than an open-ended estimate.
        When `reconciliation=True` and this call raises
        RestBudgetUnavailable, no HTTP attempt was ever made.

        TODO: Verify exact request format against Hyperliquid docs.
        The candleSnapshot endpoint structure may differ from the info endpoint.
        Adjust payload keys if API returns errors.
        """
        payload: dict = {
            "type": "candleSnapshot",
            "req": {
                "coin": symbol,
                "interval": interval,
                "startTime": start_time,
            },
        }
        if end_time is not None:
            payload["req"]["endTime"] = end_time

        # Conservative minimum reservation derived from the actual requested
        # window (an open-ended/unsupported window falls back to the
        # documented worst-case 5000-row reservation) — an override may
        # never reserve *less* than this, regardless of what value it
        # supplies, since the response can legitimately return that many
        # rows no matter what the caller believed it would ask for.
        if end_time is not None:
            duration = _INTERVAL_DURATION_MS.get(interval)
            if duration:
                span = max(end_time - start_time, 0)
                max_rows = min(MAX_CANDLE_SNAPSHOT_ROWS, math.ceil(span / duration) + 2)
            else:
                max_rows = MAX_CANDLE_SNAPSHOT_ROWS
        else:
            max_rows = MAX_CANDLE_SNAPSHOT_ROWS
        conservative_minimum = estimate_candle_snapshot_weight(max_rows)

        if weight is None:
            effective_weight = conservative_minimum
        elif (
            not isinstance(weight, (int, float))
            or isinstance(weight, bool)
            or not math.isfinite(weight)
            or weight < 0
        ):
            # Reject an invalid override (negative, NaN, non-finite, non-
            # numeric) rather than letting it silently under-reserve the
            # shared budget — fall back to the conservative minimum instead.
            # Never echo the raw invalid value itself, only its type.
            logger.warning(f"Ignoring invalid candleSnapshot weight override (type={type(weight).__name__})")
            effective_weight = conservative_minimum
        else:
            # A valid override is rounded UP (never floored, which would
            # under-reserve a fractional weight) and never allowed below the
            # conservative minimum for this exact request window.
            effective_weight = max(math.ceil(weight), conservative_minimum)

        result = await self._post(payload, weight=effective_weight, reconciliation=reconciliation)
        if result is None:
            return None
        if not isinstance(result, list):
            logger.warning(
                f"Unexpected candleSnapshot response for {symbol}/{interval}: {type(result)}"
            )
            return None
        # Oversized-response debt/cooldown applies to EVERY candleSnapshot
        # call, not just reconciliation: if the actual weight this many rows
        # would have justified reserving exceeds what was actually reserved
        # (effective_weight), the provider returned more than this client
        # budgeted for — charge conservative extra debt and cool down (see
        # RestRateLimiter.note_oversized_response). This is a local
        # defensive guard, not a claim about any other client/process's own
        # share of the same upstream IP budget.
        actual_weight = estimate_candle_snapshot_weight(len(result))
        if actual_weight > effective_weight:
            await self.rate_limiter.note_oversized_response(
                actual_weight - effective_weight, reconciliation=reconciliation
            )
        return result

    async def get_meta_and_asset_ctxs(self) -> Optional[list]:
        """Fetch meta + asset contexts (includes open interest, funding, etc).

        Returns [meta_dict, [asset_ctx, ...]] or None.

        TODO: Confirm this endpoint is available on mainnet public API.
        The 'metaAndAssetCtxs' type may require authentication or may not exist.
        Fall back gracefully if unavailable.
        """
        result = await self._post({"type": "metaAndAssetCtxs"}, weight=META_WEIGHT)
        if result is None:
            return None
        if not isinstance(result, list) or len(result) < 2:
            logger.warning(f"Unexpected metaAndAssetCtxs structure")
            return None
        return result
