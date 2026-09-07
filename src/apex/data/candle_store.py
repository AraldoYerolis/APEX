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
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        # {(symbol, timeframe): deque of dicts sorted oldest-first}
        self._data: dict[tuple[str, str], list[dict]] = defaultdict(list)
        self._last_update: dict[tuple[str, str], float] = {}

    def update(
        self,
        symbol: str,
        timeframe: str,
        candle: dict,
        persist: bool = True,
        now_ms: Optional[int] = None,
    ) -> None:
        """Insert or update a candle. candle dict must have: t, T, o, h, l, c, v,
        and optionally a nonstandard closed/is_closed flag (see module
        docstring — never trusted alone; always checked against the candle's
        own close boundary as of `now_ms`, real wall-clock by default).
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

        normalized = _normalize(symbol, timeframe, candle, open_time, close_time, is_closed)

        # Find and replace if same open_time exists. A candle already
        # recorded as closed is never demoted/overwritten by an incoming
        # update that would itself evaluate as not-closed (e.g. a late or
        # out-of-order partial re-delivery) — this protects a known-good
        # finalized sample from being lost.
        for i, c in enumerate(store):
            if c["open_time"] == open_time:
                if c.get("is_closed") and not is_closed:
                    self._last_update[key] = time.time()
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
        # observation instant — never on the raw flag alone.
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
                repo.upsert_candle(self._conn, c_obj)
            except Exception as e:
                logger.warning(f"Failed to persist candle {symbol}/{timeframe}: {e}")

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
