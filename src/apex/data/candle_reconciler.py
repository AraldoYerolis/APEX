"""Bounded, opt-in authoritative closed-candle reconciliation.

Purpose (see coordinator brief): production has observed continuous WS
traffic/rollovers while an eligible closed bar is still missing from
CandleStore. This module periodically drains CandleStore's own bounded gap
windows (see apex.data.candle_store's reconciliation section) and fetches
*fresh* REST candleSnapshot rows for exactly those missing bars — it never
promotes a cached forming candle to closed based on elapsed time, and never
implements its own closed/eligibility test: every row is checked against
CandleStore's own `is_eligible_closed` before ever being written.

Scope and limitations (bounded catch-up, not a live-feed guarantee):
- Serial, single-flight, at most `max_fetches_per_tick` (default 2) REST
  fetches per tick, at most `max_bars_per_request` (default 60) bars per
  fetch, ticking every `tick_interval_seconds` (default 10s). The fair
  round-robin cursor advances by exactly how many targets were actually
  serviced each tick (not a fixed 1), so a full 120-pair backlog's sweep
  genuinely takes roughly ten minutes (120 / 2 fetches-per-tick * 10s ~= 10
  minutes), not double that from overlapping served prefixes — this is
  still deliberately bounded catch-up, not a promise of latest-minute
  completeness for a live signal feed.
- A window that fails 3 fetch attempts (monotonic 30s/60s/120s backoff) is
  explicitly dropped (not retried again) so a persistently-broken pair can
  never starve the rest of the backlog; a newer rollover for that pair still
  starts a genuinely fresh window afterward.
- Fetched rows only ever repair the CandleStore's own in-memory/DB state;
  they are validated (identity, timestamp, OHLCV sanity, closed-as-of-a-
  frozen-cutoff) before ever reaching `CandleStore.update()`, and a single
  malformed row never aborts the rest of a response's good rows.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from apex.data.candle_store import (
    CandleStore,
    ReconciliationTarget,
    TIMEFRAME_DURATION_MS,
    close_boundary_ms,
    is_eligible_closed,
)
from apex.data.hyperliquid_client import HyperliquidClient, RestBudgetUnavailable
from apex.data.market_universe import get_upstream_symbol

logger = logging.getLogger(__name__)

# Reconciliation always requests <=60 bars but pads the boundary, so a
# response can legitimately return up to 62 rows — reserve a fixed,
# known-safe weight for that rather than an open-ended estimate (see
# apex.data.hyperliquid_client.RECONCILIATION_REQUEST_WEIGHT).
_RECONCILIATION_WEIGHT = 22

# Fixed monotonic backoff schedule (seconds) applied after the 1st, 2nd, and
# 3rd consecutive failed fetch attempt for the *same* gap window (identified
# by its start_open_time — see _RetryState). A 3rd failure drops the window.
BACKOFF_SCHEDULE_SECONDS = (30.0, 60.0, 120.0)
MAX_ATTEMPTS_PER_WINDOW = 3

# Bounded per-tick work for the persist-only retry drain — a permanently
# broken DB does not get replayed against unbounded pending state every
# tick forever (see CandleStore.get_persist_retry_candidates, which is
# itself bounded to MAX_RECONCILIATION_PAIRS x MAX_PERSIST_PENDING_PER_PAIR).
MAX_PERSIST_RETRIES_PER_TICK = 5


def _positive_finite(name: str, value: float, *, minimum: float, maximum: float) -> float:
    if not math.isfinite(value) or value < minimum or value > maximum:
        raise ValueError(
            f"{name}={value!r} must be a finite number in [{minimum}, {maximum}]"
        )
    return value


@dataclass(frozen=True)
class ReconcilerConfig:
    """Validated, hard-ceilinged tuning for one CandleReconciler instance.

    No field here is sourced from apex.config.Settings — per the approved
    scope, only the boolean enable flag is a runtime setting; every other
    knob is a fixed, code-reviewed default, not operator-configurable.
    """

    tick_interval_seconds: float = 10.0
    max_fetches_per_tick: int = 2
    max_bars_per_request: int = 60
    post_boundary_grace_ms: int = 5000

    def __post_init__(self) -> None:
        _positive_finite("tick_interval_seconds", self.tick_interval_seconds, minimum=1.0, maximum=300.0)
        if not isinstance(self.max_fetches_per_tick, int) or not (1 <= self.max_fetches_per_tick <= 10):
            raise ValueError(f"max_fetches_per_tick={self.max_fetches_per_tick!r} must be an int in [1, 10]")
        if not isinstance(self.max_bars_per_request, int) or not (1 <= self.max_bars_per_request <= 60):
            raise ValueError(f"max_bars_per_request={self.max_bars_per_request!r} must be an int in [1, 60]")
        if not isinstance(self.post_boundary_grace_ms, int) or not (0 <= self.post_boundary_grace_ms <= 60_000):
            raise ValueError(
                f"post_boundary_grace_ms={self.post_boundary_grace_ms!r} must be an int in [0, 60000]"
            )


@dataclass
class ReconcilerCounters:
    """Bounded, purely observational counters — never consulted for
    correctness, only for tests/future diagnostics wiring.
    """

    ticks_run: int = 0
    ticks_skipped_overlap: int = 0
    ticks_skipped_stopped: int = 0
    fetches_attempted: int = 0
    fetches_succeeded: int = 0
    fetches_failed: int = 0
    fetches_deferred_budget: int = 0
    fetches_deferred_lifecycle: int = 0
    rows_resolved_in_memory: int = 0
    rows_persisted: int = 0
    rows_persist_retry_queued: int = 0
    rows_persist_retry_succeeded: int = 0
    rows_persist_retry_exhausted: int = 0
    rows_rejected_invalid: int = 0
    windows_exhausted: int = 0


@dataclass
class _RetryState:
    window_start: int
    attempts: int = 0
    next_retry_monotonic: float = 0.0
    # Frozen on the FIRST attempt against this exact window_start and reused
    # unchanged by every subsequent retry (see _process_target) — a later WS
    # rollover extending the store's window end must never expand an
    # already-in-progress fixed <=max_bars_per_request request/drop range.
    attempted_end_open_time: Optional[int] = None
    # The ReconciliationTarget.incarnation this state was created against
    # (see CandleStore._reconciliation_pair_incarnation) — a mismatch means
    # this pair was removed and re-added since, so a coincidentally-matching
    # window_start must never reuse stale attempts/backoff/frozen end.
    incarnation: int = 0


@dataclass(frozen=True)
class TickOutcome:
    skipped: bool
    reason: Optional[str] = None
    fetches_attempted: int = 0


TokenProvider = Callable[[str], Any]


class CandleReconciler:
    """Owns periodic single-flight drain of CandleStore's bounded gap
    windows: fair ordering, coalescing range cursors (delegated to
    CandleStore's own compact window + acknowledge API), retry/backoff, and
    counters. Serial concurrency=1 by design — no per-pending-pair child
    task, no speculative parallelism.
    """

    def __init__(
        self,
        store: CandleStore,
        client: HyperliquidClient,
        config: Optional[ReconcilerConfig] = None,
        *,
        token_provider: Optional[TokenProvider] = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self.store = store
        self.client = client
        self.config = config or ReconcilerConfig()
        # None means "no lifecycle gating" (e.g. a standalone/test
        # reconciler not wired to apex.main's WS lifecycle) — production
        # wiring (see apex.main) always supplies the real provider, which
        # fails closed (returns None) whenever a fresh token can't be
        # produced for an enabled-manager instance.
        self._token_provider = token_provider
        self._clock = clock
        self._wall_clock_ms = wall_clock_ms

        self.counters = ReconcilerCounters()
        self._retry_state: dict[tuple[str, str], _RetryState] = {}
        # Persist-only retry backoff/attempt bookkeeping, keyed by
        # (symbol, timeframe, open_time, incarnation) — no cached payload
        # (see CandleStore.get_persist_retry_candidates/retry_persist). The
        # incarnation (see CandleStore.get_reconciliation_incarnation) is
        # part of the key, exactly like _RetryState.incarnation above, so a
        # pair removed and re-added between ticks — where the store's own
        # persist-pending bookkeeping is cleared but a fresh failed
        # persistence record can land on a coincidentally-matching
        # (symbol, timeframe, open_time) — never inherits stale
        # attempts/backoff from the pair's prior incarnation. Pruned every
        # drain to whatever the store currently reports pending (under the
        # CURRENT incarnation only), so a resolved/evicted/membership-pruned/
        # stale-incarnation entry never lingers.
        self._persist_retry_backoff: dict[tuple[str, str, int, int], float] = {}
        self._persist_retry_attempts: dict[tuple[str, str, int, int], int] = {}
        self._round_robin_cursor = 0
        self._in_progress = False
        self._stopped = False
        self._active_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------ lifecycle

    async def stop(self) -> None:
        """Idempotent, cancellation-safe shutdown: prevents any further
        tick from starting, and cancels+awaits an in-flight one so the
        caller (apex.main) can safely close the DB immediately afterward.
        """
        if self._stopped:
            return
        self._stopped = True
        task = self._active_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - defensive, tick() itself never raises
                pass

    # ------------------------------------------------------------------ tick

    async def tick(self) -> TickOutcome:
        """Single-flight scheduler entry point. Overlapping calls (manual or
        via the scheduler) are refused outright rather than queued.
        """
        if self._stopped:
            self.counters.ticks_skipped_stopped += 1
            return TickOutcome(skipped=True, reason="stopped")
        if self._in_progress:
            self.counters.ticks_skipped_overlap += 1
            return TickOutcome(skipped=True, reason="in_progress")
        self._in_progress = True
        self._active_task = asyncio.current_task()
        try:
            return await self._run_tick()
        finally:
            self._in_progress = False
            self._active_task = None

    async def _run_tick(self) -> TickOutcome:
        self.counters.ticks_run += 1
        if self._stopped:
            return TickOutcome(skipped=True, reason="stopped")

        cutoff_ms = self._wall_clock_ms()
        monotonic_now = self._clock()

        await self._drain_pending_persist_retries(monotonic_now)

        if self._stopped:
            return TickOutcome(skipped=True, reason="stopped")

        targets = self._select_targets(monotonic_now, cutoff_ms)
        fetches = 0
        for target in targets:
            if fetches >= self.config.max_fetches_per_tick:
                break
            if self._stopped:
                break
            cutoff_ms = self._wall_clock_ms()  # captured fresh for each fetch, before any await
            monotonic_now = self._clock()
            attempted = await self._process_target(target, cutoff_ms, monotonic_now)
            if attempted:
                fetches += 1

        # Advance the fair round-robin cursor by exactly how many targets
        # were actually serviced this tick (not a fixed 1) — otherwise
        # consecutive ticks' served windows overlap by
        # max_fetches_per_tick - 1 and full-backlog coverage takes roughly
        # twice as many ticks as documented.
        self._advance_round_robin(fetches)

        return TickOutcome(skipped=False, fetches_attempted=fetches)

    # ------------------------------------------------------- target selection

    def _select_targets(self, monotonic_now: float, cutoff_ms: int) -> list[ReconciliationTarget]:
        all_targets = self.store.get_reconciliation_targets()

        # Prune retry state for any pair that no longer has a live gap
        # window (fully resolved, dropped, or removed from membership) —
        # otherwise a capped store does not bound this reconciler-local
        # dict, since it was previously only ever cleared by this exact
        # pair's own window fully exhausting.
        live_keys = {(t.symbol, t.timeframe) for t in all_targets}
        for key in list(self._retry_state):
            if key not in live_keys:
                del self._retry_state[key]

        eligible: list[ReconciliationTarget] = []
        for target in all_targets:
            duration = TIMEFRAME_DURATION_MS.get(target.timeframe)
            if not duration:
                continue
            # Gated on the OLDEST actually-requestable candidate
            # (start_open_time), not the newest addition to the window
            # (end_open_time): an actively/repeatedly-extending window would
            # otherwise keep re-arming the grace gate on every rollover and
            # perpetually defer old, already-eligible bars.
            close_boundary = target.start_open_time + duration
            if cutoff_ms < close_boundary + self.config.post_boundary_grace_ms:
                continue  # still within the post-boundary grace period
            key = (target.symbol, target.timeframe)
            rs = self._retry_state.get(key)
            if rs is None or rs.incarnation != target.incarnation:
                # Never seen, or this pair was removed and re-added since —
                # a coincidentally-matching window_start must never reuse
                # stale attempts/backoff/frozen end.
                rs = _RetryState(window_start=target.start_open_time, incarnation=target.incarnation)
                self._retry_state[key] = rs
            elif rs.window_start != target.start_open_time:
                if rs.attempted_end_open_time is not None and target.start_open_time <= rs.attempted_end_open_time:
                    # Still within the previously frozen attempted prefix
                    # (e.g. a late WS resolved exactly the window's oldest
                    # bar, shrinking start without completing the whole
                    # frozen range) — preserve accumulated attempts/backoff
                    # and the frozen end, only track the new start.
                    rs.window_start = target.start_open_time
                else:
                    # The previously frozen prefix is genuinely exhausted or
                    # fully resolved (start moved past attempted_end_open_
                    # time), or there was never a frozen prefix yet — start
                    # fresh.
                    rs = _RetryState(window_start=target.start_open_time, incarnation=target.incarnation)
                    self._retry_state[key] = rs
            if monotonic_now < rs.next_retry_monotonic:
                continue  # still backing off
            eligible.append(target)

        if not eligible:
            return []

        # Fair round-robin: rotate the starting point each tick so a
        # backlog larger than max_fetches_per_tick cannot starve later
        # pairs by always serving the same earliest-first prefix. The
        # cursor itself is advanced by the caller (_run_tick) once it knows
        # how many targets were actually serviced this tick.
        eligible.sort(key=lambda t: (t.first_seen_ms, t.symbol, t.timeframe))
        n = len(eligible)
        start = self._round_robin_cursor % n
        ordered = eligible[start:] + eligible[:start]
        return ordered

    def _advance_round_robin(self, served: int) -> None:
        if served > 0:
            self._round_robin_cursor += served

    # ------------------------------------------------------------- fetching

    async def _process_target(self, target: ReconciliationTarget, cutoff_ms: int, monotonic_now: float) -> bool:
        key = (target.symbol, target.timeframe)
        duration = TIMEFRAME_DURATION_MS[target.timeframe]

        # Freeze the fixed <=max_bars_per_request request prefix on the
        # FIRST attempt against this window_start and reuse it unchanged on
        # every retry (see _RetryState.attempted_end_open_time) — a later WS
        # rollover extending target.end_open_time must never expand an
        # already-in-progress request/drop range, reset attempts/backoff, or
        # cause the newly-added unattempted bars to be abandoned alongside a
        # genuinely exhausted prefix.
        rs = self._retry_state.get(key)
        rs_matches = (
            rs is not None
            and rs.window_start == target.start_open_time
            and rs.incarnation == target.incarnation
        )
        if rs_matches and rs.attempted_end_open_time is not None:
            request_end_open = rs.attempted_end_open_time
            take = (request_end_open - target.start_open_time) // duration + 1
        else:
            span_steps = (target.end_open_time - target.start_open_time) // duration + 1
            take = min(span_steps, self.config.max_bars_per_request)
            take = self._grace_cleared_take(target.start_open_time, take, duration, target.timeframe, cutoff_ms)
            if take <= 0:
                # Nothing in this window has cleared its own post-boundary
                # grace yet as of this frozen cutoff — defer without
                # freezing a request/drop range and without consuming a
                # retry/backoff attempt (see _grace_cleared_take).
                return False
            request_end_open = target.start_open_time + (take - 1) * duration
            if rs_matches:
                rs.attempted_end_open_time = request_end_open

        # Request end no later than cutoff-1; pad the start by 1ms to
        # tolerate undocumented startTime inclusivity, then filter the
        # response strictly back to the intended [start, request_end_open]
        # open-time range regardless of what padding the provider honors.
        padded_start = max(target.start_open_time - 1, 0)
        desired_end_ms = min(request_end_open + duration - 1, cutoff_ms - 1)
        if desired_end_ms < padded_start:
            return False  # nothing safely fetchable yet under this cutoff

        upstream_symbol = get_upstream_symbol(target.symbol)

        token_before = self._token_provider(target.symbol) if self._token_provider else None
        if self._token_provider is not None and token_before is None:
            # Fail closed: an enabled lifecycle-gated reconciler with no
            # usable token (manager down/disconnected, symbol no longer
            # subscribed, shutdown in progress) must not fetch or write.
            self.counters.fetches_deferred_lifecycle += 1
            return False

        self.counters.fetches_attempted += 1
        try:
            response = await self.client.get_candle_snapshot(
                upstream_symbol,
                target.timeframe,
                padded_start,
                desired_end_ms,
                reconciliation=True,
                weight=_RECONCILIATION_WEIGHT,
            )
        except RestBudgetUnavailable:
            self.counters.fetches_deferred_budget += 1
            return False
        except Exception as e:  # pragma: no cover - defensive, client already contains its own errors
            logger.warning(f"Reconciliation fetch raised unexpectedly for {target.symbol}/{target.timeframe}: {e}")
            response = None

        if self._stopped:
            return True  # attempt counted; no further writes after stop

        # Re-check the lifecycle token in the same no-await section as the
        # row validation/store writes below — a changed token, a removed
        # symbol, or a stop must prevent every response write.
        if self._token_provider is not None:
            token_after = self._token_provider(target.symbol)
            if token_after is None or token_after != token_before:
                self.counters.fetches_deferred_lifecycle += 1
                return True  # attempt counted; response discarded, not applied

        made_progress = self._apply_response(
            target, response, upstream_symbol, request_end_open, take, cutoff_ms
        )

        self._record_attempt_outcome(key, target, made_progress)
        return True

    def _grace_cleared_take(
        self, start_open_time: int, take: int, duration: int, timeframe: str, cutoff_ms: int
    ) -> int:
        """Trim a candidate `take` (bar count from `start_open_time`) down to
        however many leading candidates have already cleared this
        reconciler's post_boundary_grace_ms as of the frozen `cutoff_ms` —
        so a fresh request is never even shaped to ask for a bar that
        per-row validation (see _is_valid_row's identical grace check)
        would reject anyway (remaining-scope item 1).

        Each candidate's boundary is derived via CandleStore's own
        `close_boundary_ms` (the sole boundary authority — never
        reimplemented here), called with the actual upstream T for a
        not-yet-fetched candidate still unknown, so a synthetic
        `candidate_open + duration` is passed as close_time — the later
        (exclusive-convention) boundary of the two REST T conventions this
        module accepts (see _is_valid_row): `close_boundary_ms` then
        resolves to `candidate_open + duration + 1`, one ms later than the
        inclusive convention's own boundary. This is deliberately the
        conservative (latest-possible) estimate rather than the earliest:
        it never shapes a request for a candidate until cutoff has cleared
        grace under EITHER supported convention, so a request is never even
        attempted for a bar the real per-row check (which sees the actual
        T) would still reject as not-yet-grace-clear under the exclusive
        convention. A candidate that turns out to use the inclusive
        convention (whose real boundary is one ms earlier) was always
        already grace-clear by the time this trim admits it, so no
        otherwise-fetchable candidate is ever excluded — only its request is
        deferred by at most 1ms relative to the old duration-only estimate.
        Only ever shrinks `take` (from its trailing/newest end) — never
        grows it beyond what the caller already capped by
        max_bars_per_request/the window's own span, and never touches an
        already-frozen attempted_end_open_time (see caller).
        """
        grace_ms = self.config.post_boundary_grace_ms
        while take > 0:
            candidate_open = start_open_time + (take - 1) * duration
            boundary = close_boundary_ms(candidate_open, candidate_open + duration, timeframe)
            if boundary is not None and cutoff_ms >= boundary + grace_ms:
                return take
            take -= 1
        return 0

    def _apply_response(
        self,
        target: ReconciliationTarget,
        response: Any,
        upstream_symbol: str,
        request_end_open: int,
        take: int,
        cutoff_ms: int,
    ) -> bool:
        """Validate, dedup, and write every acceptable row; then advance the
        window past the longest resolved contiguous prefix starting at
        target.start_open_time. Returns whether any real progress was made
        this attempt (used to decide whether to reset or advance the
        retry/backoff state).
        """
        duration = TIMEFRAME_DURATION_MS[target.timeframe]

        if response is None:
            self.counters.fetches_failed += 1
            accepted: dict[int, dict] = {}
        else:
            self.counters.fetches_succeeded += 1
            accepted = self._validate_rows(
                response, upstream_symbol, target.timeframe, target.start_open_time,
                request_end_open, duration, cutoff_ms, self.config.post_boundary_grace_ms,
            )

        for open_time in sorted(accepted):
            if self.store.is_candle_closed(target.symbol, target.timeframe, open_time):
                # Already resolved (e.g. a late WS receipt) — never
                # overwrite an already-closed bar just because it appeared
                # in this broad range.
                self.counters.rows_resolved_in_memory += 1
                continue
            self._write_row(target.symbol, target.timeframe, accepted[open_time], cutoff_ms)

        # Determine the longest resolved contiguous prefix from
        # target.start_open_time, re-checking actual store state (not just
        # what this response happened to contain) so a bar resolved via a
        # completely separate path still counts.
        resolved_through: Optional[int] = None
        candidate = target.start_open_time
        end_scan = target.start_open_time + (take - 1) * duration
        while candidate <= end_scan:
            if not self.store.is_candle_closed(target.symbol, target.timeframe, candidate):
                break
            resolved_through = candidate
            candidate += duration

        if resolved_through is not None:
            self.store.acknowledge_reconciliation_progress(
                target.symbol, target.timeframe, resolved_through
            )
            return True
        return False

    def _write_row(self, symbol: str, timeframe: str, row: dict, cutoff_ms: int) -> None:
        result = self.store.update(symbol, timeframe, row, persist=True, now_ms=cutoff_ms, source="reconciliation")
        if result is None:
            return
        if result.persisted is True:
            self.counters.rows_persisted += 1
        elif result.persisted is False:
            # No local queueing/payload cache here: CandleStore itself now
            # tracks this (symbol, timeframe, open_time) as pending a
            # persist-only retry (see CandleStore._track_persist_pending) —
            # _drain_pending_persist_retries below reads that bounded index
            # back, never a cached copy of this row.
            self.counters.rows_persist_retry_queued += 1

    async def _drain_pending_persist_retries(self, monotonic_now: float) -> None:
        """Retry persisting already-validated, already-in-memory-closed
        records whose DB write previously failed — no REST call, no
        re-validation, no re-promotion of cached forming data, and no
        overwrite of in-memory OHLC: each retry reads and writes exactly
        the CURRENT frozen in-memory record for that open_time (see
        CandleStore.retry_persist), so a superseding successful WS/
        reconciliation write for the same bar is never clobbered by a
        stale retry, and a superseding *failed* write's newer data is what
        gets retried.

        Bounded per-tick work (MAX_PERSIST_RETRIES_PER_TICK) with monotonic
        backoff per entry, not a full replay every tick. Every candidate is
        also subject to the same lifecycle/membership/stop check as a fetch,
        immediately before its synchronous DB write, with no await in
        between.
        """
        pending = self.store.get_persist_retry_candidates()
        # Build the incarnation-qualified key for each currently-pending
        # triple fresh every drain (never cached) — a stale-incarnation
        # local entry (pair removed and re-added since it was recorded)
        # naturally falls outside this set and is pruned below exactly like
        # a resolved/evicted one, rather than being matched and reused.
        keyed_pending: list[tuple[tuple[str, str, int, int], str, str, int]] = []
        for symbol, timeframe, open_time in pending:
            incarnation = self.store.get_reconciliation_incarnation(symbol, timeframe)
            keyed_pending.append(((symbol, timeframe, open_time, incarnation), symbol, timeframe, open_time))
        pending_key_set = {pkey for pkey, _, _, _ in keyed_pending}
        for pkey in list(self._persist_retry_backoff):
            if pkey not in pending_key_set:
                self._persist_retry_backoff.pop(pkey, None)
                self._persist_retry_attempts.pop(pkey, None)

        attempted = 0
        for pkey, symbol, timeframe, open_time in keyed_pending:
            if attempted >= MAX_PERSIST_RETRIES_PER_TICK:
                break
            if self._stopped:
                return
            if monotonic_now < self._persist_retry_backoff.get(pkey, 0.0):
                continue
            if self._token_provider is not None and self._token_provider(symbol) is None:
                # Suspended (e.g. mid rebuild/backfill), disconnected, or the
                # symbol is no longer subscribed — defer this pair's retry
                # without consuming its backoff/attempt budget.
                self.counters.fetches_deferred_lifecycle += 1
                continue
            attempted += 1
            outcome = self.store.retry_persist(symbol, timeframe, open_time)
            if outcome is True:
                self.counters.rows_persist_retry_succeeded += 1
                self._persist_retry_backoff.pop(pkey, None)
                self._persist_retry_attempts.pop(pkey, None)
            elif outcome is False:
                attempts = self._persist_retry_attempts.get(pkey, 0) + 1
                if attempts >= MAX_ATTEMPTS_PER_WINDOW:
                    # Finite local retry-attempt budget exhausted for this
                    # exact pending item: drop it via the Store's own API
                    # (never replay/overwrite cached memory here) and count
                    # it distinctly from both success and the unrelated
                    # per-pair overflow eviction.
                    self.store.drop_persist_pending(symbol, timeframe, open_time)
                    self.counters.rows_persist_retry_exhausted += 1
                    self._persist_retry_backoff.pop(pkey, None)
                    self._persist_retry_attempts.pop(pkey, None)
                else:
                    self._persist_retry_attempts[pkey] = attempts
                    backoff = BACKOFF_SCHEDULE_SECONDS[min(attempts - 1, len(BACKOFF_SCHEDULE_SECONDS) - 1)]
                    self._persist_retry_backoff[pkey] = monotonic_now + backoff
            else:  # None: no longer pending (resolved/evicted elsewhere)
                self._persist_retry_backoff.pop(pkey, None)
                self._persist_retry_attempts.pop(pkey, None)

    def _record_attempt_outcome(self, key: tuple[str, str], target: ReconciliationTarget, made_progress: bool) -> None:
        rs = self._retry_state.get(key)
        if rs is None or rs.window_start != target.start_open_time or rs.incarnation != target.incarnation:
            return
        if made_progress:
            rs.attempts = 0
            rs.next_retry_monotonic = 0.0
            return
        rs.attempts += 1
        if rs.attempts >= MAX_ATTEMPTS_PER_WINDOW:
            # Drop only the actually-attempted frozen prefix, never the
            # full (possibly much wider, e.g. rollover-grown) window — bars
            # beyond attempted_end_open_time were never fetched even once
            # and must remain in the window for a future attempt.
            drop_through = rs.attempted_end_open_time
            if drop_through is None:  # pragma: no cover - defensive, always set by _process_target
                drop_through = target.end_open_time
            self.store.acknowledge_reconciliation_progress(
                target.symbol, target.timeframe, drop_through, dropped=True,
            )
            self.counters.windows_exhausted += 1
            del self._retry_state[key]
            return
        backoff = BACKOFF_SCHEDULE_SECONDS[min(rs.attempts - 1, len(BACKOFF_SCHEDULE_SECONDS) - 1)]
        rs.next_retry_monotonic = self._clock() + backoff

    # ---------------------------------------------------------- row validation

    def _validate_rows(
        self,
        response: Any,
        upstream_symbol: str,
        timeframe: str,
        target_start: int,
        target_end_inclusive: int,
        duration: int,
        cutoff_ms: int,
        grace_ms: int = 0,
    ) -> dict[int, dict]:
        if not isinstance(response, list):
            self.counters.rows_rejected_invalid += 1
            return {}
        accepted: dict[int, dict] = {}
        for row in response:
            try:
                if not self._is_valid_row(
                    row, upstream_symbol, timeframe, target_start, target_end_inclusive, cutoff_ms, grace_ms
                ):
                    self.counters.rows_rejected_invalid += 1
                    continue
                t = int(row["t"])
                accepted[t] = row  # de-dup: last identical/duplicate open_time wins, one write only
            except Exception:  # a single malformed row must never abort the rest
                self.counters.rows_rejected_invalid += 1
                continue
        return accepted

    @staticmethod
    def _is_valid_row(
        row: Any,
        upstream_symbol: str,
        timeframe: str,
        target_start: int,
        target_end_inclusive: int,
        cutoff_ms: int,
        grace_ms: int = 0,
    ) -> bool:
        if not isinstance(row, dict):
            return False

        # Exact upstream identity is REQUIRED — a missing/null s or i must
        # never silently pass, since that would mean accepting a row this
        # code never actually verified came from the requested coin/interval.
        if row.get("s") != upstream_symbol:
            return False
        if row.get("i") != timeframe:
            return False

        duration = TIMEFRAME_DURATION_MS.get(timeframe)
        if not duration:
            return False

        t = row.get("t")
        if isinstance(t, bool) or not isinstance(t, (int, float)):
            return False
        if isinstance(t, float) and (not math.isfinite(t) or not t.is_integer()):
            return False
        t = int(t)
        if t < 0 or t < target_start or t > target_end_inclusive:
            return False
        if (t - target_start) % duration != 0:
            return False  # off-grid open_time — never promoted to a candle

        T = row.get("T")
        T_val: Optional[int] = None
        if T is not None:
            if isinstance(T, bool) or not isinstance(T, (int, float)):
                return False
            if isinstance(T, float) and (not math.isfinite(T) or not T.is_integer()):
                return False
            T_val = int(T)
            # Strict/canonical T: only the documented inclusive-end
            # convention (T == t + duration - 1) or a defensively-tolerated
            # exclusive convention (T == t + duration) are accepted — any
            # other value in between (or outside) is rejected outright
            # rather than treated as a "close enough" continuous range.
            # CandleStore.is_eligible_closed (via _close_boundary_ms) is the
            # sole eligibility authority regardless of which of these two
            # supported values is present.
            if T_val != t + duration - 1 and T_val != t + duration:
                return False

        o_raw, h_raw, low_raw, c_raw, v_raw = row.get("o"), row.get("h"), row.get("l"), row.get("c"), row.get("v")
        if any(isinstance(x, bool) for x in (o_raw, h_raw, low_raw, c_raw, v_raw)):
            return False
        try:
            o, h, low, c, v = float(o_raw), float(h_raw), float(low_raw), float(c_raw), float(v_raw)
        except (TypeError, ValueError):
            return False
        if not all(math.isfinite(x) for x in (o, h, low, c, v)):
            return False
        if v < 0:
            return False
        if h < max(o, c, low):
            return False
        if low > min(o, c, h):
            return False

        if not is_eligible_closed(t, T_val, timeframe, cutoff_ms):
            return False  # forming as of the frozen cutoff — never promoted

        # Per-candidate post-boundary grace: is_eligible_closed above already
        # guarantees a usable boundary exists and cutoff_ms has reached it,
        # so `boundary` here is never None — but this bar's OWN boundary,
        # not the window's oldest candidate's, must also have cleared the
        # configured grace as of the same frozen cutoff (see module
        # docstring / ReconcilerConfig.post_boundary_grace_ms). Gating only
        # the oldest requested candidate (as target selection does, cheaply,
        # to decide whether to fetch at all) would otherwise let a bar that
        # closed only moments ago slip through inside a wider, already-
        # eligible request range.
        boundary = close_boundary_ms(t, T_val, timeframe)
        if boundary is None or cutoff_ms < boundary + grace_ms:
            return False

        return True
