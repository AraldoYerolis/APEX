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
KNOWN_SOURCES = ("ws", "backfill", "preload", "reconciliation", "unknown")

# Bound on distinct (symbol, timeframe) pairs a CandleStore will ever
# allocate diagnostics for, regardless of how many are ever observed or how
# many are named by set_diagnostics_membership().
MAX_DIAGNOSTIC_PAIRS = 120

# ---------------------------------------------------------------------------
# Closed-candle reconciliation (bounded, opt-in, default off)
# ---------------------------------------------------------------------------
# Tracks, per (symbol, timeframe), a single compact "gap window" of candidate
# missing closed bars — bars a strictly-newer live WS open_time has proven
# must exist but which this store has never recorded as closed. This is
# purely bookkeeping: it never fetches anything itself (that is
# apex.data.candle_reconciler.CandleReconciler's job) and never promotes a
# cached forming candle to closed based on elapsed time alone — the existing
# _is_eligible_closed boundary check is completely unchanged and remains the
# sole authority over what get_df()/persistence expose as closed.
#
# Only the four timeframes this store already knows a fixed duration for are
# ever tracked; membership is independently capped and pruned, separate from
# (and never reset by) the diagnostics membership epoch above.
SUPPORTED_RECONCILIATION_TIMEFRAMES = frozenset({"1m", "3m", "5m", "15m"})

# Bound on distinct (symbol, timeframe) pairs ever tracked for reconciliation
# gap windows, independent of MAX_DIAGNOSTIC_PAIRS.
MAX_RECONCILIATION_PAIRS = 120

# Bound on how many bar-slots (steps of the pair's own timeframe duration) a
# single pair's gap window may span. A rollover that would extend it further
# drops the oldest (earliest) overflow slots rather than growing unbounded.
MAX_RECONCILIATION_WINDOW_BARS = 300

# Bound on how many distinct open_times per (symbol, timeframe) pair are ever
# kept pending a persist-only retry (see retry_persist/get_persist_retry_
# candidates below) — independent of, and much smaller than, the 300-bar gap
# window cap. A pathologically-long DB outage evicts its oldest pending
# open_time rather than growing without bound; that dropped entry is never
# retried again and is counted (persist_pending_overflow_total), never
# silently treated as persisted.
MAX_PERSIST_PENDING_PER_PAIR = 5


@dataclass
class ReconciliationGapWindow:
    """One pair's compact coalesced candidate range: [start_open_time,
    end_open_time] inclusive, at the pair's own fixed timeframe step. Does
    not itself enumerate which slots within the range are still missing —
    apex.data.candle_reconciler re-checks each candidate's actual recorded
    state (see CandleStore.is_candle_closed) before ever fetching/writing.
    """

    start_open_time: int
    end_open_time: int
    first_seen_ms: int
    dropped_count: int = 0


@dataclass(frozen=True)
class ReconciliationTarget:
    """Read-only snapshot of one pair's current gap window, for a caller
    (CandleReconciler) to decide what to fetch. Never mutable, never shared
    with the store's own live dict.

    `incarnation` is a small bounded per-pair token (see
    CandleStore._reconciliation_pair_incarnation) that changes whenever this
    exact (symbol, timeframe) pair is removed from reconciliation membership
    and later re-added — so a caller keying its own retry/backoff state by
    (symbol, timeframe) can detect that a coincidentally-matching
    start_open_time does NOT mean the same logical gap window, even across
    a remove+readd between ticks.
    """

    symbol: str
    timeframe: str
    start_open_time: int
    end_open_time: int
    first_seen_ms: int
    dropped_count: int
    incarnation: int = 0


@dataclass(frozen=True)
class CandleUpdateResult:
    """Backwards-compatible richer outcome of one `update()` call.

    Existing callers ignore this return value entirely (update() previously
    always implicitly returned None) so adding it is additive. `persisted`
    is None when persistence was never attempted (not closed, or
    `persist=False`); True/False only for an actual attempted repository
    call — distinct from `is_closed`, which reflects the in-memory decision
    alone and is true regardless of whether the DB write actually succeeded.
    """

    open_time: Optional[int]
    is_closed: bool
    ignored_late_partial: bool = False
    persisted: Optional[bool] = None


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


def is_eligible_closed(
    open_time: Optional[int], close_time: Optional[int], timeframe: str, now_ms: int
) -> bool:
    """Public wrapper around this store's own closed-candle eligibility
    predicate, for a caller (apex.data.candle_reconciler.CandleReconciler)
    that has no nonstandard closed/is_closed flag to consider — REST
    candleSnapshot rows carry none (see module docstring) — so there is no
    `raw_flag` to pass. Exists so that caller never implements its own,
    potentially weaker, close test.
    """
    return _is_eligible_closed(None, open_time, close_time, timeframe, now_ms)


def close_boundary_ms(
    open_time: Optional[int], close_time: Optional[int], timeframe: str
) -> Optional[int]:
    """Public wrapper around this store's own close-boundary calculation
    (see `_close_boundary_ms`), for a caller that needs the boundary value
    itself rather than just a closed/not-closed verdict — e.g.
    apex.data.candle_reconciler.CandleReconciler layering its own additional
    post-boundary grace buffer on top of (never instead of) this store's
    eligibility authority.
    """
    return _close_boundary_ms(open_time, close_time, timeframe)


@dataclass
class CandleKey:
    symbol: str
    timeframe: str


class CandleStore:
    def __init__(
        self,
        conn: sqlite3.Connection,
        diagnostics_enabled: bool = False,
        reconciliation_enabled: bool = False,
    ) -> None:
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

        # Bounded, opt-in closed-candle reconciliation tracking (see module
        # docstring section above). Disabled by default: no per-pair
        # tracking dict is even allocated, and no ordinary ingestion
        # decision (eligibility, persistence, get_df output) ever changes
        # based on this flag — it only gates whether gap-window bookkeeping
        # happens alongside the existing logic. Entirely independent of
        # diagnostics: never allocated/reset by a diagnostics membership
        # epoch, and vice versa.
        self._reconciliation_enabled = reconciliation_enabled
        self._reconciliation_ws_max_open: dict[tuple[str, str], int] = {}
        self._reconciliation_windows: dict[tuple[str, str], ReconciliationGapWindow] = {}
        self._reconciliation_membership: Optional[set[tuple[str, str]]] = None
        self._reconciliation_overflow_dropped_total: int = 0
        self._reconciliation_exhausted_count: int = 0
        # Persist-only retry bookkeeping (see retry_persist/_track_persist_
        # pending below): {(symbol, timeframe): {open_time, ...}}, no cached
        # payload — a retry always reads the CURRENT frozen in-memory record
        # for that open_time, never a stale copy.
        self._persist_pending: dict[tuple[str, str], set[int]] = {}
        self._persist_pending_overflow_total: int = 0
        self._persist_exhausted_total: int = 0
        # Bumped on every `replace=True` set_reconciliation_membership call
        # (mirrors diagnostics_membership_generation) — used only to mint a
        # fresh per-pair incarnation token below, never itself exposed to
        # bound anything on its own.
        self._reconciliation_membership_epoch: int = 0
        # Bounded (pruned in lockstep with membership, exactly like
        # _reconciliation_windows/_reconciliation_ws_max_open/_persist_
        # pending above) per-pair incarnation token — see ReconciliationTarget
        # docstring. A key only ever gets a NEW value here when it (re)enters
        # membership after not being present immediately prior, never on an
        # unchanged/retained refresh (so retained-pair retry age/gaps are
        # never spuriously invalidated).
        self._reconciliation_pair_incarnation: dict[tuple[str, str], int] = {}

    @property
    def diagnostics_enabled(self) -> bool:
        return self._diagnostics_enabled

    @property
    def reconciliation_enabled(self) -> bool:
        return self._reconciliation_enabled

    @property
    def reconciliation_overflow_dropped_total(self) -> int:
        return self._reconciliation_overflow_dropped_total

    @property
    def reconciliation_exhausted_count(self) -> int:
        return self._reconciliation_exhausted_count

    @property
    def persist_pending_overflow_total(self) -> int:
        return self._persist_pending_overflow_total

    @property
    def persist_exhausted_total(self) -> int:
        return self._persist_exhausted_total

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

    # ----------------------------------------------------- reconciliation

    def set_reconciliation_membership(self, keys, *, replace: bool = True) -> None:
        """Establish/extend the current capped reconciliation membership.

        Independent of diagnostics membership: never reset by a diagnostics
        membership-epoch replace, and never itself touches diagnostics.
        Unsupported timeframes (anything outside
        SUPPORTED_RECONCILIATION_TIMEFRAMES) are silently filtered out —
        this store has no fixed duration for them, so a gap window could
        never be computed anyway.

        `replace=True` (default) sets membership to exactly the filtered,
        capped `keys` and drops (not merely hides) gap-window/WS-max state
        for any pair no longer in it — a pair later re-added starts
        genuinely fresh rather than resuming stale work. A pair that stays
        in membership across the replace keeps its existing gap window and
        WS-max marker untouched (unlike the diagnostics epoch reset).

        `replace=False` only ever adds to the existing membership, exactly
        mirroring set_diagnostics_membership's same-named parameter.

        A no-op when reconciliation is disabled.
        """
        if not self._reconciliation_enabled:
            return
        try:
            filtered = [
                k for k in dict.fromkeys(keys) if k[1] in SUPPORTED_RECONCILIATION_TIMEFRAMES
            ]
            if replace:
                accepted = set(filtered[:MAX_RECONCILIATION_PAIRS])
                prior_membership = self._reconciliation_membership or set()
                self._reconciliation_membership_epoch += 1
                for key in list(self._reconciliation_windows.keys()):
                    if key not in accepted:
                        del self._reconciliation_windows[key]
                for key in list(self._reconciliation_ws_max_open.keys()):
                    if key not in accepted:
                        del self._reconciliation_ws_max_open[key]
                for key in list(self._persist_pending.keys()):
                    if key not in accepted:
                        del self._persist_pending[key]
                for key in list(self._reconciliation_pair_incarnation.keys()):
                    if key not in accepted:
                        del self._reconciliation_pair_incarnation[key]
                for key in accepted:
                    if key not in prior_membership:
                        # Newly (re)joining this replace — including a pair
                        # that was previously removed and is only now coming
                        # back — mints a fresh incarnation. A retained pair
                        # (already in prior_membership) is left untouched.
                        self._reconciliation_pair_incarnation[key] = self._reconciliation_membership_epoch
                self._reconciliation_membership = accepted
            else:
                existing = (
                    set(self._reconciliation_membership)
                    if self._reconciliation_membership is not None
                    else set(self._reconciliation_windows) | set(self._reconciliation_ws_max_open)
                )
                ordered = list(existing) + [k for k in filtered if k not in existing]
                accepted = set(ordered[:MAX_RECONCILIATION_PAIRS])
                for key in accepted:
                    if key not in existing and key not in self._reconciliation_pair_incarnation:
                        self._reconciliation_pair_incarnation[key] = self._reconciliation_membership_epoch
                self._reconciliation_membership = accepted
        except Exception as e:  # pragma: no cover - reconciliation bookkeeping must never break the caller
            _log_diag_error("reconciliation_set_membership", e)

    def prune_reconciliation(self, keep: set) -> None:
        """Drop reconciliation state for pairs outside the current membership."""
        self.set_reconciliation_membership(keep, replace=True)

    def is_reconciliation_tracked(self, symbol: str, timeframe: str) -> bool:
        """Whether (symbol, timeframe) is within the current capped
        reconciliation allocation. False when reconciliation is disabled or
        the timeframe is unsupported.
        """
        if not self._reconciliation_enabled:
            return False
        if timeframe not in SUPPORTED_RECONCILIATION_TIMEFRAMES:
            return False
        key = (symbol, timeframe)
        if self._reconciliation_membership is not None:
            return key in self._reconciliation_membership
        # No explicit membership ever established (e.g. direct/test
        # construction) — bound allocation across ALL reconciliation
        # bookkeeping dicts together (windows, WS-max markers, and
        # persist-pending), not just WS-max markers alone: a caller that
        # only ever writes via update(source="reconciliation") never
        # populates _reconciliation_ws_max_open at all, so consulting only
        # its length would never actually cap anything.
        if key in self._reconciliation_windows or key in self._reconciliation_ws_max_open:
            return True
        if key in self._persist_pending:
            return True
        allocated = (
            set(self._reconciliation_windows)
            | set(self._reconciliation_ws_max_open)
            | set(self._persist_pending)
        )
        return len(allocated) < MAX_RECONCILIATION_PAIRS

    def get_reconciliation_ws_max_open(self, symbol: str, timeframe: str) -> Optional[int]:
        """The highest open_time ever observed on the live WS path for this
        pair (source="ws" only — never advanced by REST/preload/backfill).
        """
        return self._reconciliation_ws_max_open.get((symbol, timeframe))

    def get_reconciliation_incarnation(self, symbol: str, timeframe: str) -> int:
        """The current bounded per-pair incarnation token (see
        `ReconciliationTarget.incarnation`) for (symbol, timeframe), 0 if
        the pair has never (re)joined reconciliation membership. Lets a
        caller (CandleReconciler) key its own persist-only retry bookkeeping
        by the same identity used for fetch-retry state, so a pair removed
        and re-added between ticks never carries over stale local retry
        history for a coincidentally-matching open_time.
        """
        return self._reconciliation_pair_incarnation.get((symbol, timeframe), 0)

    def get_reconciliation_targets(self) -> list[ReconciliationTarget]:
        """Read-only snapshot of every pair currently holding an open gap
        window. Empty when reconciliation is disabled. The caller
        (CandleReconciler) must re-check each candidate's actual closed
        state (see is_candle_closed) before ever fetching/writing — a
        pair's presence here only means "this store observed proof of a gap
        at some point", not "still definitely missing right now".
        """
        if not self._reconciliation_enabled:
            return []
        return [
            ReconciliationTarget(
                symbol=sym,
                timeframe=tf,
                start_open_time=w.start_open_time,
                end_open_time=w.end_open_time,
                first_seen_ms=w.first_seen_ms,
                dropped_count=w.dropped_count,
                incarnation=self._reconciliation_pair_incarnation.get((sym, tf), 0),
            )
            for (sym, tf), w in self._reconciliation_windows.items()
        ]

    def is_candle_closed(self, symbol: str, timeframe: str, open_time: int) -> bool:
        """Whether this exact open_time is already recorded closed in
        memory — used by CandleReconciler to avoid ever re-fetching or
        overwriting an already-resolved bar.
        """
        return self._is_candle_closed_in_memory((symbol, timeframe), open_time)

    def _is_candle_closed_in_memory(self, key: tuple[str, str], open_time: int) -> bool:
        for c in self._data.get(key, ()):
            if c["open_time"] == open_time:
                return bool(c.get("is_closed"))
        return False

    def acknowledge_reconciliation_progress(
        self,
        symbol: str,
        timeframe: str,
        resolved_up_to_open_time_inclusive: int,
        *,
        dropped: bool = False,
    ) -> None:
        """Advance (or fully clear) a pair's gap window past every candidate
        up to and including `resolved_up_to_open_time_inclusive`.

        `dropped=True` marks this as an exhausted-window drop (after
        CandleReconciler's own bounded retry/backoff budget for this exact
        window is spent) rather than a genuine resolution — counted
        separately (see reconciliation_exhausted_count) so it is
        distinguishable from real progress, but the window is cleared
        identically either way: a dropped window must never be reoffered as
        a target at the next rollover (see get_reconciliation_targets).
        A no-op when reconciliation is disabled or no window exists for
        this pair.
        """
        if not self._reconciliation_enabled:
            return
        key = (symbol, timeframe)
        window = self._reconciliation_windows.get(key)
        if window is None:
            return
        duration = TIMEFRAME_DURATION_MS.get(timeframe)
        if duration is None:
            return
        if dropped:
            self._reconciliation_exhausted_count += 1
        new_start = resolved_up_to_open_time_inclusive + duration
        if new_start > window.start_open_time:
            window.start_open_time = new_start
        if window.start_open_time > window.end_open_time:
            del self._reconciliation_windows[key]

    def _record_reconciliation_ws_observation(
        self, key: tuple[str, str], timeframe: str, open_time: int, now_ms: int
    ) -> None:
        """Advance this pair's WS-only max open_time and, if it strictly
        advanced, register never-confirmed-closed bars between the previous
        max (inclusive) and the new one (exclusive) as reconciliation
        candidates. Only ever called for source="ws" — REST/preload/backfill
        observations never move this marker (see module docstring: "Track
        valid live WS maximum open time separately from REST/preload").

        `open_time` is only trusted if it is a genuine, aligned, non-negative
        open time on this timeframe's own grid and no more than one bar
        ahead of `now_ms` (a defensible observation-time bound) — an
        off-grid or implausible-future value must never poison this marker
        (or the store's own max-open bookkeeping) rather than merely being
        rejected by row validation elsewhere.

        A jump spanning more than MAX_RECONCILIATION_WINDOW_BARS candidate
        slots keeps only the NEWEST `MAX_RECONCILIATION_WINDOW_BARS` of them
        (nearest the new max) — the older remainder is explicitly counted via
        `reconciliation_overflow_dropped_total` rather than silently advancing
        the max marker past an unaccounted-for gap.
        """
        if not self.is_reconciliation_tracked(*key):
            return
        try:
            duration = TIMEFRAME_DURATION_MS.get(timeframe)
            if not duration or duration <= 0:
                return
            if open_time < 0 or open_time % duration != 0:
                return  # off-grid WS t must not poison tracking
            if open_time > now_ms + duration:
                return  # implausible future timestamp

            prior_max = self._reconciliation_ws_max_open.get(key)
            if prior_max is None:
                self._reconciliation_ws_max_open[key] = open_time
                return
            if open_time <= prior_max:
                return

            total_gap_steps = (open_time - prior_max) // duration
            skip_steps = 0
            start_candidate = prior_max
            if total_gap_steps > MAX_RECONCILIATION_WINDOW_BARS:
                # Keep only the newest MAX_RECONCILIATION_WINDOW_BARS
                # candidates; the older skip_steps ones are dropped and
                # explicitly counted rather than left uncounted once the
                # max marker advances past them.
                skip_steps = total_gap_steps - MAX_RECONCILIATION_WINDOW_BARS
                start_candidate = prior_max + skip_steps * duration
                # A jump this large alone already fills the entire cap with
                # strictly newer candidates, so any pre-existing window for
                # this pair (necessarily entirely older, since the WS-max
                # marker only ever advances) is unconditionally superseded.
                # Drop it and count its own full span exactly once HERE,
                # rather than leaving it in place to be re-discovered and
                # re-counted by _add_reconciliation_candidate's own
                # incremental per-candidate trim below (which would count
                # the same already-known-dropped span a second time).
                old_window = self._reconciliation_windows.pop(key, None)
                if old_window is not None:
                    old_span = (
                        old_window.end_open_time - old_window.start_open_time
                    ) // duration + 1
                    self._reconciliation_overflow_dropped_total += old_span
                self._reconciliation_overflow_dropped_total += skip_steps

            candidate = start_candidate
            steps = 0
            max_steps = total_gap_steps - skip_steps
            while candidate < open_time and steps < max_steps:
                if not self._is_candle_closed_in_memory(key, candidate):
                    self._add_reconciliation_candidate(key, candidate, now_ms)
                candidate += duration
                steps += 1
            self._reconciliation_ws_max_open[key] = open_time
        except Exception as e:  # pragma: no cover - reconciliation bookkeeping must never break the caller
            _log_diag_error("reconciliation_ws_observe", e)

    def _add_reconciliation_candidate(self, key: tuple[str, str], open_time: int, now_ms: int) -> None:
        window = self._reconciliation_windows.get(key)
        if window is None:
            self._reconciliation_windows[key] = ReconciliationGapWindow(
                start_open_time=open_time, end_open_time=open_time, first_seen_ms=now_ms,
            )
            return
        if open_time < window.start_open_time:
            window.start_open_time = open_time
        if open_time > window.end_open_time:
            window.end_open_time = open_time
        duration = TIMEFRAME_DURATION_MS.get(key[1])
        if not duration:
            return
        span_steps = (window.end_open_time - window.start_open_time) // duration + 1
        if span_steps > MAX_RECONCILIATION_WINDOW_BARS:
            overflow = span_steps - MAX_RECONCILIATION_WINDOW_BARS
            window.start_open_time += overflow * duration
            window.dropped_count += overflow
            self._reconciliation_overflow_dropped_total += overflow

    def _maybe_advance_reconciliation_window(self, key: tuple[str, str], resolved_open_time: int) -> None:
        """Auto-collapse the leading edge of a gap window when the bar at
        exactly its current start becomes closed in memory, from *any*
        source — this is what lets a late eligible WS receipt resolve a
        tracked gap without CandleReconciler ever making a network call.
        """
        if not self._reconciliation_enabled:
            return
        try:
            window = self._reconciliation_windows.get(key)
            if window is None or resolved_open_time != window.start_open_time:
                return
            duration = TIMEFRAME_DURATION_MS.get(key[1])
            if not duration:
                return
            window.start_open_time += duration
            if window.start_open_time > window.end_open_time:
                del self._reconciliation_windows[key]
        except Exception as e:  # pragma: no cover - reconciliation bookkeeping must never break the caller
            _log_diag_error("reconciliation_advance", e)

    def update(
        self,
        symbol: str,
        timeframe: str,
        candle: dict,
        persist: bool = True,
        now_ms: Optional[int] = None,
        source: str = "unknown",
    ) -> Optional[CandleUpdateResult]:
        """Insert or update a candle. candle dict must have: t, T, o, h, l, c, v,
        and optionally a nonstandard closed/is_closed flag (see module
        docstring — never trusted alone; always checked against the candle's
        own close boundary as of `now_ms`, real wall-clock by default).

        `source` labels where this observation came from ("ws", "backfill",
        or the caller's own label) for bounded diagnostics only (see
        CandleDiagnostics); it has no effect on eligibility or persistence.

        Returns a `CandleUpdateResult` describing the in-memory/persistence
        outcome (None only when the row was dropped outright for an
        unusable open_time). Every existing caller predates this return
        value and ignores it — purely additive/backwards-compatible.
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
            return None
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
                    return CandleUpdateResult(open_time=open_time, is_closed=False, ignored_late_partial=True)
                store[i] = normalized
                break
        else:
            store.append(normalized)
            store.sort(key=lambda x: x["open_time"])

        # Trim to max size
        if len(store) > MAX_CANDLES_IN_MEMORY:
            self._data[key] = store[-MAX_CANDLES_IN_MEMORY:]

        self._last_update[key] = time.time()

        # Reconciliation gap-window bookkeeping (see module docstring
        # section above). WS-only max-open tracking/candidate registration
        # happens regardless of persist outcome — it is about in-memory
        # visibility, which is already decided above. A no-op when
        # reconciliation is disabled.
        if self._reconciliation_enabled:
            if source == "ws":
                self._record_reconciliation_ws_observation(key, timeframe, open_time, effective_now_ms)
            if is_closed:
                self._maybe_advance_reconciliation_window(key, open_time)

        # Persist only candles that are actually closed as of this update's
        # observation instant — never on the raw flag alone. An update that
        # was ignored above (the finalized-sample-protection early return)
        # never reaches here, so it is correctly never counted as a
        # persistence attempt.
        persisted: Optional[bool] = None
        if persist and is_closed:
            persisted = self._persist_record(symbol, timeframe, normalized, diag)
            # Only confirmed active/supported reconciliation membership ever
            # allocates a persist-retry pending marker — an arbitrary
            # backfill/unsupported-timeframe/removed pair must never grow
            # this bookkeeping just because reconciliation happens to be
            # enabled globally (see is_reconciliation_tracked's unified
            # bound across windows/WS-max/persist-pending allocation).
            if self._reconciliation_enabled and self.is_reconciliation_tracked(symbol, timeframe):
                self._track_persist_pending(key, open_time, persisted)

        return CandleUpdateResult(open_time=open_time, is_closed=is_closed, persisted=persisted)

    def _persist_record(
        self, symbol: str, timeframe: str, record: dict, diag: Optional[CandleDiagnostics] = None
    ) -> bool:
        """Actual repository write for one already-frozen closed record
        (either the just-normalized row from `update()`, or the current
        in-memory record for a later persist-only retry — see
        `retry_persist`). Never re-derives eligibility; the caller is solely
        responsible for only calling this on a record already decided
        closed.
        """
        try:
            c_obj = Candle(
                symbol=symbol,
                timeframe=timeframe,
                open_time=record["open_time"],
                close_time=record["close_time"] if record["close_time"] is not None else record["open_time"],
                open=float(record["open"]),
                high=float(record["high"]),
                low=float(record["low"]),
                close=float(record["close"]),
                volume=float(record["volume"]),
                is_closed=True,
            )
        except Exception as e:
            # Failed before any repository call was made — distinct from an
            # actual write failure (see persist_construction_failures) and
            # never counted as a persist_attempts (that counter means an
            # actual repository call; see CandleDiagnostics).
            if diag is not None:
                self._safe_diag(diag.record_persist_construction_failure, "persist_construction_failure")
            logger.warning(f"Failed to persist candle {symbol}/{timeframe}: {e}")
            return False

        if diag is not None:
            self._safe_diag(diag.record_persist_attempt, "persist_attempt")
        try:
            repo.upsert_candle(self._conn, c_obj)
        except Exception as e:
            if diag is not None:
                self._safe_diag(diag.record_persist_failure, "persist_failure")
            logger.warning(f"Failed to persist candle {symbol}/{timeframe}: {e}")
            return False
        if diag is not None:
            self._safe_diag(diag.record_persist_success, "persist_success")
        return True

    def _track_persist_pending(self, key: tuple[str, str], open_time: int, persisted: bool) -> None:
        """Bounded, no-payload persist-retry bookkeeping (see
        `retry_persist`). A successful persist clears any stale pending
        marker for this exact open_time (it may have been queued by an
        earlier failed attempt for the same bar); a failed persist marks it
        pending, evicting the pair's oldest pending open_time if already at
        the per-pair cap.
        """
        pending = self._persist_pending.get(key)
        if persisted:
            if pending and open_time in pending:
                pending.discard(open_time)
                if not pending:
                    del self._persist_pending[key]
            return
        if pending is None:
            pending = set()
            self._persist_pending[key] = pending
        if open_time not in pending and len(pending) >= MAX_PERSIST_PENDING_PER_PAIR:
            oldest = min(pending)
            pending.discard(oldest)
            self._persist_pending_overflow_total += 1
        pending.add(open_time)

    def get_persist_retry_candidates(self) -> list[tuple[str, str, int]]:
        """Bounded snapshot of every (symbol, timeframe, open_time) whose
        last persist attempt failed and has not since been superseded by a
        successful persist of that exact bar. No payload — a caller
        (CandleReconciler) retries via `retry_persist`, which always reads
        the CURRENT frozen in-memory record, never a cached copy. Empty when
        reconciliation is disabled.
        """
        if not self._reconciliation_enabled:
            return []
        out: list[tuple[str, str, int]] = []
        for (sym, tf), opens in self._persist_pending.items():
            for ot in sorted(opens):
                out.append((sym, tf, ot))
        return out

    def retry_persist(self, symbol: str, timeframe: str, open_time: int) -> Optional[bool]:
        """Persist-only retry of the CURRENT in-memory closed record at this
        exact open_time — no re-fetch, no re-normalization/promotion of a
        cached forming payload, and no overwrite of in-memory OHLC (only a
        repository write of exactly what is already frozen in memory).

        Returns True on a successful write (pending marker cleared), False
        on another failed attempt (stays pending), or None if this
        open_time is no longer a pending closed record for this pair
        (already resolved by a newer successful persist, evicted by the
        per-pair cap, or the pair was pruned from membership) — the caller
        should stop retrying it.
        """
        key = (symbol, timeframe)
        pending = self._persist_pending.get(key)
        if not pending or open_time not in pending:
            return None
        record = None
        for c in self._data.get(key, ()):
            if c["open_time"] == open_time:
                record = c
                break
        if record is None or not record.get("is_closed"):
            # No longer a valid closed record to retry (evicted from the
            # in-memory window, or — defensively — somehow not closed);
            # never persisted, so drop the pending marker rather than retry
            # forever.
            pending.discard(open_time)
            if not pending:
                del self._persist_pending[key]
            return None
        diag = self._get_or_create_diag(key)
        ok = self._persist_record(symbol, timeframe, record, diag)
        if ok:
            pending.discard(open_time)
            if not pending:
                del self._persist_pending[key]
            return True
        return False

    def drop_persist_pending(self, symbol: str, timeframe: str, open_time: int) -> bool:
        """Drop exactly this pending persist-retry marker WITHOUT another
        attempt — used by a caller (CandleReconciler) that has exhausted its
        own bounded local retry-attempt budget for this exact open_time.

        Distinct from a successful persist: this bar's in-memory closed
        record is retained (never lost/overwritten), but it is never
        retried again and is counted separately (`persist_exhausted_total`)
        so it is never confused with either a real success or the
        unrelated `persist_pending_overflow_total` (per-pair cap eviction).
        Returns False (no-op) if this exact open_time was not actually
        pending — e.g. already resolved/evicted/pruned elsewhere.
        """
        key = (symbol, timeframe)
        pending = self._persist_pending.get(key)
        if not pending or open_time not in pending:
            return False
        pending.discard(open_time)
        if not pending:
            del self._persist_pending[key]
        self._persist_exhausted_total += 1
        return True

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
