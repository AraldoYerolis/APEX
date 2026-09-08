"""In-memory candle store with SQLite persistence.

Closed-candle eligibility
--------------------------
Hyperliquid's documented WS `candle` subscription and REST `candleSnapshot`
payloads (see
https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions
and .../api/info-endpoint, fetched 2026-09-07) carry only `t`/`T`/OHLCV
fields — no `closed`/`is_closed` indicator is part of that contract. A
nonstandard `closed`/`is_closed` key may still appear on some payloads, but
its presence, absence, or truth value is never trusted on its own: a
candle is only treated as closed once the observation instant (`now_ms`,
real wall-clock by default) has reached or passed its close boundary. An
explicit `False` flag is still honored as a definitive "not closed" signal.

The upstream REST example encodes `T` as an *inclusive* end-of-bar
millisecond (`T == t + duration - 1`), so the exclusive close boundary is
`T + 1`. Since `T` may be missing, malformed, or (on some producer paths)
absent altogether, the boundary is computed conservatively as the *later*
of the duration-derived boundary (`open_time + timeframe duration`, using
this store's own fixed per-timeframe duration, not a caller-supplied value)
and the `T`-derived boundary (`T + 1`) — whichever candidate is available.
If neither candidate can be determined (no usable `t`/`T`), the candle is
conservatively treated as not yet closed.

Eligibility is computed once, at the moment a candle is written via
`update()` or loaded via `load_from_db()`, and frozen on the stored record.
Passage of time alone never promotes an already-cached partial sample to
closed on a later `get_df()` call — only a fresh `update()` (or reload)
evaluated against its own observation instant can do that. A candle already
recorded as closed is never overwritten by a later update that would
evaluate as not-closed (e.g. a late/out-of-order partial re-delivery),
preserving a known-good finalized sample.

Legacy caveat: rows persisted by earlier versions of this store (which
defaulted a missing/absent flag to closed with no temporal check) may have
been written while genuinely still forming. Nothing here retroactively
re-certifies that historical SQLite data — there is no recorded ingestion
timestamp to check it against, and no migration/rewrite is performed. Only
newly written/loaded rows benefit from the corrected eligibility check.

Operational limitation — no age-only promotion, no polling: this store never
promotes a candle to closed merely because time has passed since it was last
observed; it only evaluates eligibility at the moment of an actual `update()`
(or `load_from_db()`) call, against that call's own observation instant. It
does not poll REST or assume every provider/connection eventually delivers a
final post-close update for every bar. A practical consequence is that a
final update that never arrives (a dropped message, a gap in the WS stream)
means the corresponding bar is correctly withheld forever rather than
incorrectly promoted — this is a data-availability limitation of the feed,
not a defect in this gating logic, and it is not something this fix can
detect or correct on its own. Relatedly, `is_stale()` tracks how recently any
update (open, forming, or closed) was last observed for a
symbol/timeframe — it is not a certification of the age or eligibility of
the latest closed bar, and an unstale/fresh `is_stale()` result must never be
read as proof that the latest eligible candle returned by `get_df()` is
itself recent. Nothing in this module certifies the exchange feed's
availability or the final OHLC quality of any bar beyond what is described
above; it only prevents this store from exposing a candle as closed before
its own close boundary has been reached.

Diagnostics (bounded, opt-in, default off)
-------------------------------------------
`CandleStore(conn, diagnostics_enabled=True)` additionally tracks, per
(symbol, timeframe), bounded receive/persist facts (see `CandleDiagnostics`)
so a caller can distinguish "no fresh WS traffic", "traffic present but
never eligible", and "persistence failing" from each other without changing
anything above. Disabled by default; nothing is allocated or updated unless
explicitly enabled, and a diagnostics-only failure is always caught and
logged at DEBUG rather than affecting candle handling.

Allocation is capped at `MAX_DIAGNOSTIC_PAIRS` (symbol, timeframe) pairs.
`set_diagnostics_membership()` establishes which pairs are currently
trackable (called by the caller before preload/backfill so allocation is
capped against the intended membership, not creation order); a pair outside
that membership is reported by `is_diagnostics_tracked()` as untracked
rather than silently returning "no traffic" diagnostics. `latest_received_*`
fields refresh on every valid receive (including a repeated update for the
same open_time), so they reflect live recency; `latest_received_open_time`/
`latest_eligible_open_time` (and their boundaries) only ever advance to a
strictly newer open_time, so out-of-order data can never regress them.

Membership epochs: every `replace=True` call to `set_diagnostics_membership`
(and thus `prune_diagnostics`) bumps `diagnostics_membership_generation`,
records a new `diagnostics_membership_epoch_started_ms`, and clears ALL
diagnostic records — including for a pair that stays in membership across
the replace, not only pairs actually dropped. This is deliberate: every
count this store reports for any pair is only ever "since the current
epoch began" (see get_diagnostics/is_diagnostics_tracked below), so a
retained pair keeping stale counters across a new epoch would make that
claim false. A pair dropped and later re-added, and a pair that simply
stayed in membership across a replace, therefore both start genuinely at
zero, indistinguishably. Application code (apex.main) only ever calls
`replace=True` after a WS rebuild has *actually* succeeded — a
prospective/not-yet-confirmed symbol is never added to membership
beforehand, so its preload/backfill during a still-in-flight rebuild runs
untracked, and a failed/aborted rebuild leaves the prior membership (and
its epoch/counters) completely untouched rather than partially expanded or
reset. `replace=False` never bumps the generation/epoch or clears
anything — it only ever adds to the existing membership.
"""
from __future__ import annotations

import logging
import math
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from apex.db import repository as repo
from apex.db.models import Candle

logger = logging.getLogger(__name__)

# Max candles kept per (symbol, timeframe) in memory
MAX_CANDLES_IN_MEMORY = 300

# Max age of latest candle before data is considered stale (seconds)
STALE_THRESHOLD_SECONDS: dict[str, int] = {
    "1m": 90,
    "3m": 200,
    "5m": 360,
    "15m": 1000,
}

# Candle duration in milliseconds per timeframe, used to derive each
# candle's own close boundary (open_time + duration) independent of any
# upstream-supplied close time. Kept local to this module (not imported from
# apex.opportunity.contract) since CandleStore is lower-level, shared
# infrastructure and must not depend on the research-only opportunity layer.
TIMEFRAME_DURATION_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 3 * 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
}


def _now_ms() -> int:
    return int(time.time() * 1000)


# Finite, bounded set of diagnostic source labels — an unrecognized/hostile
# caller-supplied `source` string is folded into "unknown" rather than
# growing `source_counts`/`source_last_at_ms` without bound.
KNOWN_SOURCES = ("ws", "backfill", "preload", "unknown")

# Bound on distinct (symbol, timeframe) pairs a CandleStore will ever
# allocate diagnostics for, regardless of how many are ever observed or how
# many are named by set_diagnostics_membership().
MAX_DIAGNOSTIC_PAIRS = 120

def _log_diag_error(context: str, exc: BaseException) -> None:
    """Fixed-text, non-throwing diagnostics-failure log line.

    Never calls `str(exc)` — that call already runs from inside an
    `except Exception` handler around a diagnostics-only step, so it must
    not be able to raise a second exception itself (a pathological/hostile
    `__str__`) or echo caller-supplied payload/secret text into the log.
    Only a fixed operation label and the exception's type name are
    rendered (every exception object exposes `type(exc).__name__` without
    executing any caller-influenced code). The logging call itself is also
    wrapped, so a broken/mocked logger can never escape into the real
    candle-handling path this guards.
    """
    try:
        exc_type = type(exc).__name__
    except Exception:  # pragma: no cover - defensive, type() essentially never raises
        exc_type = "unknown"
    try:
        logger.debug(f"Candle diagnostics failed ({context}): {exc_type}")
    except Exception:  # pragma: no cover - diagnostics logging must never break the caller
        pass


def _normalize_source(source: str) -> str:
    return source if source in KNOWN_SOURCES else "unknown"


@dataclass
class CandleDiagnostics:
    """Bounded, per-(symbol, timeframe) receive/persist diagnostic facts.

    Only allocated and updated when a `CandleStore` is constructed with
    `diagnostics_enabled=True` (default off). Never influences eligibility,
    persistence decisions, or any value returned by `get_df`/`is_stale` —
    strictly observational bookkeeping alongside the existing logic.

    `latest_received_open_time`/`latest_eligible_open_time` (and their
    `*_boundary_ms` companions) advance only when a newly observed
    `open_time` is *greater* than the one already recorded (event/open-time
    ordering), not by arrival order — so a late or out-of-order message (a
    redelivered older bar, or a rejected late partial after an already-closed
    sample) can never falsely regress either marker past a newer bar already
    seen. `latest_received_at_ms`/`latest_received_source` are different: they
    refresh on *every* valid receive, including a repeated update for the
    same open_time (e.g. a still-forming bar ticking) — this is what lets a
    caller tell a genuinely stale WS stream from one that is alive but simply
    hasn't rolled a new bar yet.
    """

    latest_received_open_time: Optional[int] = None
    latest_received_boundary_ms: Optional[int] = None
    latest_received_at_ms: Optional[int] = None
    latest_received_source: Optional[str] = None
    received_eligible_count: int = 0
    received_noneligible_count: int = 0
    latest_eligible_open_time: Optional[int] = None
    latest_eligible_boundary_ms: Optional[int] = None
    latest_eligible_at_ms: Optional[int] = None
    latest_eligible_source: Optional[str] = None
    # Counts only an actual repository call attempt — incremented once
    # Candle() construction has already succeeded, immediately before
    # repo.upsert_candle is called. Always equal to
    # persist_successes + persist_failures; a construction failure
    # (persist_construction_failures) never reaches this point and never
    # increments it.
    persist_attempts: int = 0
    persist_successes: int = 0
    persist_failures: int = 0
    # Candle() construction raised before any repository call was even
    # attempted — distinct from persist_failures, which is reserved for an
    # actual repository write that failed.
    persist_construction_failures: int = 0
    # A late/out-of-order update rejected by the finalized-sample-protection
    # guard (see CandleStore.update): correctly never a persist attempt.
    ignored_late_partial_count: int = 0
    # A cached forming candle whose bar rolled over to a newer open_time
    # before that older bar ever became eligible, observed on the live WS
    # path. Provisional: it is not proof a late final update for that older
    # bar will never still arrive. Preload/backfill rollovers are not
    # counted here — replaying historical/bulk data in bulk order says
    # nothing about a live feed gap.
    rollover_observations: int = 0
    source_counts: dict[str, int] = field(default_factory=dict)
    source_last_at_ms: dict[str, int] = field(default_factory=dict)

    def record_receive(
        self,
        open_time: int,
        boundary_ms: Optional[int],
        is_closed: bool,
        now_ms: int,
        source: str,
    ) -> None:
        src = _normalize_source(source)
        self.source_counts[src] = self.source_counts.get(src, 0) + 1
        self.source_last_at_ms[src] = now_ms

        # Refreshed on every valid receive, including a repeat of the same
        # open_time — see class docstring.
        self.latest_received_at_ms = now_ms
        self.latest_received_source = src

        is_new_max = self.latest_received_open_time is None or open_time > self.latest_received_open_time
        if is_new_max:
            if (
                self.latest_received_open_time is not None
                and self.latest_eligible_open_time != self.latest_received_open_time
                and src == "ws"
            ):
                self.rollover_observations += 1
            self.latest_received_open_time = open_time
            self.latest_received_boundary_ms = boundary_ms

        if is_closed:
            self.received_eligible_count += 1
            if self.latest_eligible_open_time is None or open_time > self.latest_eligible_open_time:
                self.latest_eligible_open_time = open_time
                self.latest_eligible_boundary_ms = boundary_ms
                self.latest_eligible_at_ms = now_ms
                self.latest_eligible_source = src
        else:
            self.received_noneligible_count += 1

    def record_ignored_late_partial(self) -> None:
        self.ignored_late_partial_count += 1

    def record_persist_attempt(self) -> None:
        self.persist_attempts += 1

    def record_persist_success(self) -> None:
        self.persist_successes += 1

    def record_persist_failure(self) -> None:
        self.persist_failures += 1

    def record_persist_construction_failure(self) -> None:
        self.persist_construction_failures += 1


def _safe_int(value) -> Optional[int]:
    """Parse a raw t/T timestamp field into a non-negative millisecond-epoch
    int, or None if the shape is not a genuine timestamp. Rejected rather
    than silently truncated/coerced: bool (an int subtype), non-finite
    (inf/nan) floats, fractional values (a real epoch-ms timestamp is always
    integral), and negative values. Accepted: a non-negative int, an
    integral-valued float, or a numeric string of either form (including
    "0").
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, str):
            value = float(value) if any(ch in value for ch in ".eE") else int(value)
        if isinstance(value, float):
            if not math.isfinite(value) or not value.is_integer():
                return None
            value = int(value)
        if not isinstance(value, int) or value < 0:
            return None
        return value
    except (TypeError, ValueError, OverflowError):
        return None


def _close_boundary_ms(open_time: Optional[int], close_time: Optional[int], timeframe: str) -> Optional[int]:
    """Conservative exclusive close boundary: the later of the
    duration-derived and T-derived (T+1, inclusive-end convention)
    candidates, whichever are available. None if neither is available.

    A `close_time` before `open_time` is not merely "insufficient on its
    own" — it is self-contradictory raw data (the record disagrees with
    itself about when the bar started/ended), so it withholds the candle
    outright: this returns None immediately, before the duration-derived
    candidate is even considered, rather than silently falling back to
    duration-only eligibility for a record already known to be malformed.
    The T-derived candidate is otherwise only trusted for a *known*
    timeframe (one with its own fixed duration here) — an unsupported/
    unrecognized timeframe has no independently known duration to
    sanity-check an arbitrary T against, so T alone is never sufficient to
    call such a candle closed.
    """
    if open_time is not None and close_time is not None and close_time < open_time:
        return None
    candidates: list[int] = []
    duration = TIMEFRAME_DURATION_MS.get(timeframe)
    if open_time is not None and duration is not None:
        candidates.append(open_time + duration)
    if close_time is not None and open_time is not None and duration is not None:
        candidates.append(close_time + 1)
    if not candidates:
        return None
    return max(candidates)


def _is_eligible_closed(
    raw_flag,
    open_time: Optional[int],
    close_time: Optional[int],
    timeframe: str,
    now_ms: int,
) -> bool:
    """True only if the candle is not explicitly flagged open AND the
    observation instant has reached its close boundary. A missing or
    explicit-True flag never bypasses the boundary check (guards against a
    stale asserted-true flag on a still-forming candle).
    """
    if raw_flag is False:
        return False
    boundary = _close_boundary_ms(open_time, close_time, timeframe)
    if boundary is None:
        return False
    return now_ms >= boundary


@dataclass
class CandleKey:
    symbol: str
    timeframe: str


class CandleStore:
    def __init__(self, conn: sqlite3.Connection, diagnostics_enabled: bool = False) -> None:
        self._conn = conn
        # {(symbol, timeframe): deque of dicts sorted oldest-first}
        self._data: dict[tuple[str, str], list[dict]] = defaultdict(list)
        self._last_update: dict[tuple[str, str], float] = {}
        # Bounded, opt-in diagnostics (see CandleDiagnostics). Disabled by
        # default: nothing is allocated or updated unless explicitly enabled.
        self._diagnostics_enabled = diagnostics_enabled
        self._diagnostics: dict[tuple[str, str], CandleDiagnostics] = {}
        # The current capped set of pairs allocation is permitted for. None
        # until set_diagnostics_membership() is first called — allocation
        # then falls back to a simple size cap (see _get_or_create_diag).
        self._diagnostics_membership: Optional[set[tuple[str, str]]] = None
        # Bumped every time membership is *replaced* (not expanded) — lets a
        # caller/log line distinguish "membership just changed" from "same
        # membership, still running" across snapshots.
        self._diagnostics_membership_generation: int = 0
        # Wall-clock start of the current membership epoch (set whenever
        # `diagnostics_membership_generation` bumps). Every observation
        # count reported for a pair is only ever what was actually recorded
        # since this timestamp — a pair's earlier history from a since-
        # superseded membership epoch (e.g. before a removal/re-add) is
        # never reconstructed or blended in. None until the first
        # replace=True call.
        self._diagnostics_membership_epoch_started_ms: Optional[int] = None

    @property
    def diagnostics_enabled(self) -> bool:
        return self._diagnostics_enabled

    @property
    def diagnostics_membership_generation(self) -> int:
        return self._diagnostics_membership_generation

    @property
    def diagnostics_membership_epoch_started_ms(self) -> Optional[int]:
        return self._diagnostics_membership_epoch_started_ms

    def get_diagnostics(self, symbol: str, timeframe: str) -> Optional[CandleDiagnostics]:
        """Return the live, mutable CandleDiagnostics object for one
        (symbol, timeframe) pair — not a copy. Callers must treat it as
        read-only and must never mutate it (including its source_counts/
        source_last_at_ms dicts); every current caller only reads it, and a
        deep-copy here would be unnecessary overhead on a path that can run
        every few minutes over up to MAX_DIAGNOSTIC_PAIRS entries.

        None when diagnostics are disabled, when the pair is outside the
        current capped membership (see is_diagnostics_tracked — an
        allocation-overflow pair, never to be confused with zero-traffic),
        or when nothing has been observed for a tracked pair yet — that last
        case is the only one callers should treat as a zero-traffic row.
        """
        return self._diagnostics.get((symbol, timeframe))

    def is_diagnostics_tracked(self, symbol: str, timeframe: str) -> bool:
        """Whether (symbol, timeframe) is within the current capped
        diagnostic allocation — i.e. eligible to report real "zero traffic"
        rather than being silently dropped by the allocation cap. False when
        diagnostics are disabled.
        """
        if not self._diagnostics_enabled:
            return False
        key = (symbol, timeframe)
        if self._diagnostics_membership is not None:
            return key in self._diagnostics_membership
        return key in self._diagnostics or len(self._diagnostics) < MAX_DIAGNOSTIC_PAIRS

    def set_diagnostics_membership(self, keys, *, replace: bool = True) -> None:
        """Establish/extend the current capped diagnostic membership.

        Must be called with the intended pair set *before* the caller's
        initial preload/backfill for those pairs, so allocation is capped
        against the intended membership rather than arrival/creation order.

        `replace=True` (default) sets membership to exactly `keys` (capped
        at MAX_DIAGNOSTIC_PAIRS) and starts a brand-new global diagnostics
        epoch: ALL existing CandleDiagnostics records are cleared, not just
        those for a pair actually dropped from membership — a pair that
        stays in membership across the replace starts this new epoch at
        zero exactly like a symbol removed and later re-added, so every
        count this store ever reports stays honestly scoped to "since the
        current epoch began" with no exception. Bumps
        `diagnostics_membership_generation` and records a new
        `diagnostics_membership_epoch_started_ms`.

        `replace=False` adds `keys` to the existing membership (still capped)
        without removing anything already tracked or bumping the membership
        generation/epoch. Application code (see apex.main) intentionally
        never calls this with a *prospective, not-yet-confirmed* symbol set
        (e.g. candidates for a WS rebuild that might still fail) — expanding
        membership for a symbol before its subscription is actually live
        would let a failed/aborted rebuild permanently occupy capped
        allocation slots with pairs that were never really tracked. Only a
        confirmed-successful `replace=True` call ever grows membership to
        include a new pair in practice; `replace=False` remains available as
        a general primitive for a caller that already knows a set is live.

        A no-op when diagnostics are disabled.
        """
        if not self._diagnostics_enabled:
            return
        try:
            new_keys = list(dict.fromkeys(keys))
            if replace:
                ordered: list[tuple[str, str]] = list(new_keys)
            else:
                # No explicit membership established yet: treat whatever is
                # already allocated as implicitly "existing" so an expand
                # call can never make an already-tracked pair look dropped.
                existing = (
                    list(self._diagnostics_membership)
                    if self._diagnostics_membership is not None
                    else list(self._diagnostics.keys())
                )
                seen = set(existing)
                ordered = existing + [k for k in new_keys if k not in seen]
            accepted = set(ordered[:MAX_DIAGNOSTIC_PAIRS])
            new_epoch_ms = _now_ms() if replace else None

            # `accepted`/`new_epoch_ms` are fully computed above before
            # anything here mutates committed state, so a failure partway
            # through building `keys` (e.g. a hostile/broken iterator)
            # leaves membership/epoch state completely untouched rather
            # than half-applied (see test_broken_set_membership_is_contained).
            if replace:
                # New global epoch: clear every diagnostics record, not
                # just ones for a pair actually dropped — see module/
                # method docstrings for why a retained pair must also
                # restart at zero.
                self._diagnostics = {}
                self._diagnostics_membership_generation += 1
                self._diagnostics_membership_epoch_started_ms = new_epoch_ms

            self._diagnostics_membership = accepted
        except Exception as e:  # pragma: no cover - diagnostics must never break the caller
            _log_diag_error("set_membership", e)

    def prune_diagnostics(self, keep: set) -> None:
        """Drop diagnostic state for pairs outside the current membership.

        Bounds diagnostic memory to the current capped WS membership rather
        than accumulating forever across universe refreshes. A no-op when
        diagnostics are disabled (nothing was ever allocated).
        """
        self.set_diagnostics_membership(keep, replace=True)

    def _get_or_create_diag(self, key: tuple[str, str]) -> Optional[CandleDiagnostics]:
        if not self._diagnostics_enabled:
            return None
        try:
            diag = self._diagnostics.get(key)
            if diag is not None:
                return diag
            if self._diagnostics_membership is not None:
                if key not in self._diagnostics_membership:
                    return None
            elif len(self._diagnostics) >= MAX_DIAGNOSTIC_PAIRS:
                return None
            diag = CandleDiagnostics()
            self._diagnostics[key] = diag
            return diag
        except Exception as e:  # pragma: no cover - diagnostics must never break candle handling
            _log_diag_error("allocate", e)
            return None

    @staticmethod
    def _safe_diag(fn, context: str) -> None:
        """Run a diagnostics-only callback; never let it affect candle handling."""
        try:
            fn()
        except Exception as e:  # pragma: no cover - diagnostics must never break candle handling
            _log_diag_error(context, e)

    def update(
        self,
        symbol: str,
        timeframe: str,
        candle: dict,
        persist: bool = True,
        now_ms: Optional[int] = None,
        source: str = "unknown",
    ) -> None:
        """Insert or update a candle. candle dict must have: t, T, o, h, l, c, v,
        and optionally a nonstandard closed/is_closed flag (see module
        docstring — never trusted alone; always checked against the candle's
        own close boundary as of `now_ms`, real wall-clock by default).

        `source` labels where this observation came from ("ws", "backfill",
        or the caller's own label) for bounded diagnostics only (see
        CandleDiagnostics); it has no effect on eligibility or persistence.
        """
        key = (symbol, timeframe)
        store = self._data[key]

        open_time = _safe_int(candle.get("t", candle.get("open_time")))
        if open_time is None:
            raw_t = candle.get("t", candle.get("open_time"))
            logger.warning(
                f"Dropping candle with unusable open_time for {symbol}/{timeframe} "
                f"(raw type={type(raw_t).__name__})"
            )
            return
        close_time = _safe_int(candle.get("T", candle.get("close_time")))
        raw_flag = candle.get("closed", candle.get("is_closed"))
        effective_now_ms = _now_ms() if now_ms is None else now_ms
        is_closed = _is_eligible_closed(raw_flag, open_time, close_time, timeframe, effective_now_ms)
        boundary_ms = _close_boundary_ms(open_time, close_time, timeframe)

        normalized = _normalize(symbol, timeframe, candle, open_time, close_time, is_closed)

        # Diagnostic valid-receive bookkeeping only happens once
        # normalization has actually succeeded — _normalize() above raises
        # straight out of update() on a bad OHLC field (e.g. a non-numeric
        # value), and that original exception must propagate unchanged,
        # with no diagnostics entry even allocated for the rejected update
        # (see CandleDiagnostics.record_receive and its callers).
        diag = self._get_or_create_diag(key)
        if diag is not None:
            self._safe_diag(
                lambda: diag.record_receive(open_time, boundary_ms, is_closed, effective_now_ms, source),
                "receive",
            )

        # Find and replace if same open_time exists. A candle already
        # recorded as closed is never demoted/overwritten by an incoming
        # update that would itself evaluate as not-closed (e.g. a late or
        # out-of-order partial re-delivery) — this protects a known-good
        # finalized sample from being lost.
        for i, c in enumerate(store):
            if c["open_time"] == open_time:
                if c.get("is_closed") and not is_closed:
                    self._last_update[key] = time.time()
                    if diag is not None:
                        self._safe_diag(diag.record_ignored_late_partial, "ignored_late_partial")
                    return
                store[i] = normalized
                break
        else:
            store.append(normalized)
            store.sort(key=lambda x: x["open_time"])

        # Trim to max size
        if len(store) > MAX_CANDLES_IN_MEMORY:
            self._data[key] = store[-MAX_CANDLES_IN_MEMORY:]

        self._last_update[key] = time.time()

        # Persist only candles that are actually closed as of this update's
        # observation instant — never on the raw flag alone. An update that
        # was ignored above (the finalized-sample-protection early return)
        # never reaches here, so it is correctly never counted as a
        # persistence attempt.
        if persist and is_closed:
            try:
                c_obj = Candle(
                    symbol=symbol,
                    timeframe=timeframe,
                    open_time=open_time,
                    close_time=close_time if close_time is not None else open_time,
                    open=float(candle.get("o", candle.get("open", 0))),
                    high=float(candle.get("h", candle.get("high", 0))),
                    low=float(candle.get("l", candle.get("low", 0))),
                    close=float(candle.get("c", candle.get("close", 0))),
                    volume=float(candle.get("v", candle.get("volume", 0))),
                    is_closed=True,
                )
            except Exception as e:
                # Failed before any repository call was made — distinct from
                # an actual write failure (see persist_construction_failures)
                # and never counted as a persist_attempts (that counter means
                # an actual repository call; see CandleDiagnostics).
                if diag is not None:
                    self._safe_diag(diag.record_persist_construction_failure, "persist_construction_failure")
                logger.warning(f"Failed to persist candle {symbol}/{timeframe}: {e}")
            else:
                if diag is not None:
                    self._safe_diag(diag.record_persist_attempt, "persist_attempt")
                try:
                    repo.upsert_candle(self._conn, c_obj)
                except Exception as e:
                    if diag is not None:
                        self._safe_diag(diag.record_persist_failure, "persist_failure")
                    logger.warning(f"Failed to persist candle {symbol}/{timeframe}: {e}")
                else:
                    if diag is not None:
                        self._safe_diag(diag.record_persist_success, "persist_success")

    def load_from_db(
        self, symbol: str, timeframe: str, limit: int = 200, now_ms: Optional[int] = None
    ) -> None:
        """Preload candles from SQLite into memory.

        Historical rows are preserved (no legacy rewrite/delete), but the
        `is_closed` bit stored in the DB is not trusted on its own: it is
        re-checked against each row's own close boundary as of `now_ms`
        (real wall-clock by default), the same conservative rule applied to
        live updates. This cannot retroactively certify a row that was
        genuinely still forming when originally persisted by an older
        version of this store (no ingestion timestamp is recorded to check
        against) — it only guards against exposing a row whose close
        boundary is still in the future relative to `now_ms`.
        """
        effective_now_ms = _now_ms() if now_ms is None else now_ms
        rows = repo.get_candles(self._conn, symbol, timeframe, limit=limit)
        # Allocated lazily, only once a row is actually about to be recorded
        # — an empty DB result must never allocate a CandleDiagnostics entry
        # on its own, or a caller distinguishing "observed" from "zero
        # traffic" (object-presence) would wrongly count an empty preload as
        # a receive. Mirrors the same lazy-allocation-on-actual-receive
        # pattern already used in update().
        diag: Optional[CandleDiagnostics] = None
        for row in reversed(rows):  # rows come newest-first; reverse to oldest-first
            # DB is_closed=1 is not trusted alone (None -> still boundary-
            # checked); DB is_closed=0 is an explicit, definitive negative.
            raw_flag = None if bool(row["is_closed"]) else False
            is_closed = _is_eligible_closed(
                raw_flag,
                row["open_time"],
                row["close_time"],
                timeframe,
                effective_now_ms,
            )
            if diag is None:
                diag = self._get_or_create_diag((symbol, timeframe))
            if diag is not None:
                row_open_time = row["open_time"]
                row_boundary_ms = _close_boundary_ms(row["open_time"], row["close_time"], timeframe)
                self._safe_diag(
                    lambda ot=row_open_time, b=row_boundary_ms, ic=is_closed: diag.record_receive(
                        ot, b, ic, effective_now_ms, "preload"
                    ),
                    "receive",
                )
            self._data[(symbol, timeframe)].append({
                "symbol": row["symbol"],
                "timeframe": row["timeframe"],
                "open_time": row["open_time"],
                "close_time": row["close_time"],
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
                "is_closed": is_closed,
            })

    def get_df(
        self, symbol: str, timeframe: str, now_ms: Optional[int] = None
    ) -> Optional[pd.DataFrame]:
        """Return a DataFrame of closed candles (oldest first), or None if empty.

        `is_closed` eligibility is normally whatever was frozen at
        ingestion/load time (see module docstring) and this call does not
        by itself promote anything merely because time has passed. Callers
        that need every candle returned across one logical operation (e.g.
        many symbols in a single scan) to be judged against one consistent
        instant — so a candle that only becomes closed partway through that
        operation is not exposed with a close time later than a timestamp
        already captured before the operation started — may pass `now_ms`
        to additionally require each candle's own close boundary to be at
        or before it.
        """
        key = (symbol, timeframe)
        candles = [c for c in self._data[key] if c.get("is_closed")]
        if now_ms is not None:
            candles = [
                c for c in candles
                if (_close_boundary_ms(c.get("open_time"), c.get("close_time"), timeframe) or 0) <= now_ms
            ]
        if not candles:
            return None
        df = pd.DataFrame(candles)
        df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
        required = ["open", "high", "low", "close", "volume"]
        for col in required:
            if col not in df.columns:
                return None
        return df[["open_time", "open", "high", "low", "close", "volume"]].copy()

    def is_stale(self, symbol: str, timeframe: str) -> bool:
        key = (symbol, timeframe)
        last = self._last_update.get(key)
        if last is None:
            return True
        threshold = STALE_THRESHOLD_SECONDS.get(timeframe, 400)
        return (time.time() - last) > threshold

    def candle_count(self, symbol: str, timeframe: str) -> int:
        return len(self._data.get((symbol, timeframe), []))


def _normalize(
    symbol: str,
    timeframe: str,
    c: dict,
    open_time: int,
    close_time: Optional[int],
    is_closed: bool,
) -> dict:
    """Normalize raw WS candle dict to a consistent internal format.

    `open_time`/`close_time`/`is_closed` are pre-computed by the caller
    (see `update()`/`load_from_db()`) so eligibility is decided once, in one
    place, against one observation instant — this function only shapes the
    OHLCV payload around that decision.
    """
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "open_time": open_time,
        "close_time": close_time,
        "open": float(c.get("o", c.get("open", 0))),
        "high": float(c.get("h", c.get("high", 0))),
        "low": float(c.get("l", c.get("low", 0))),
        "close": float(c.get("c", c.get("close", 0))),
        "volume": float(c.get("v", c.get("volume", 0))),
        "is_closed": is_closed,
    }
