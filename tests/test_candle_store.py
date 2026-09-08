"""Regression tests for CandleStore closed-candle eligibility.

Root cause (see PILOT-DIAGNOSIS-REVIEW.md / research pilot 13/16 violations):
CandleStore used to default a missing closed/is_closed flag to True and
persist/expose whatever it received as closed, with no check against the
candle's own close boundary. Hyperliquid's documented WS/REST candle payload
(t/T/OHLCV) carries no closed/is_closed field at all, so that default
silently exposed still-forming candles — including to the shared 3m/5m
primary path and the 15m context path — as if they were final.

These tests pin the corrected contract: a candle is eligible only once the
observation instant (`now_ms`) has reached its own close boundary (the later
of open_time+duration and T+1, the exclusive-boundary form of Hyperliquid's
documented inclusive-end T), a candle explicitly flagged closed=False is
always rejected regardless of time, a flag that is merely missing or
asserted True never bypasses the boundary check, and time passing alone
never promotes an already-cached partial sample — only a fresh eligible
update can.

All timestamps here are explicit and deterministic (`now_ms` is always
passed explicitly to `update`/`load_from_db`/`get_df`); no wall-clock/sleep
dependence.
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

import apex.data.candle_store as candle_store_module
from apex.data.candle_store import CandleStore
from apex.db import repository as repo
from apex.db.connection import init_db
from apex.db.models import Candle

ONE_MIN_MS = 60_000
THREE_MIN_MS = 3 * 60_000
FIVE_MIN_MS = 5 * 60_000
FIFTEEN_MIN_MS = 15 * 60_000

# A fixed reference "now" used to build open/close times relative to it,
# so every test is about relative timing, not real wall-clock values.
BASE_MS = 1_800_000_000_000  # arbitrary fixed epoch-ms anchor


def _candle(open_time, duration, **overrides):
    c = {
        "t": open_time,
        "T": open_time + duration - 1,  # Hyperliquid's documented inclusive-end T
        "o": "100", "h": "101", "l": "99", "c": "100.5", "v": "10",
    }
    c.update(overrides)
    return c


def _store(tmp_path, name="test.db") -> CandleStore:
    conn = init_db(str(tmp_path / name))
    return CandleStore(conn)


# --------------------------------------------------------------- missing flag

class TestMissingFlagDefaultsToTemporalCheck:
    def test_missing_flag_current_bar_not_exposed(self, tmp_path):
        """The exact pilot bug: current, still-forming candle (no closed key)
        must NOT be exposed just because the key is absent.
        """
        store = _store(tmp_path)
        open_time = BASE_MS
        c = _candle(open_time, THREE_MIN_MS)
        assert "closed" not in c and "is_closed" not in c

        # now_ms is inside the candle's lifetime (still forming).
        now_ms = open_time + 42_000
        store.update("BTC", "3m", c, persist=False, now_ms=now_ms)

        df = store.get_df("BTC", "3m")
        assert df is None

    def test_missing_flag_past_bar_is_exposed(self, tmp_path):
        store = _store(tmp_path)
        open_time = BASE_MS
        c = _candle(open_time, THREE_MIN_MS)

        now_ms = open_time + THREE_MIN_MS  # exactly at boundary: closed
        store.update("BTC", "3m", c, persist=False, now_ms=now_ms)

        df = store.get_df("BTC", "3m")
        assert df is not None
        assert len(df) == 1
        assert df.iloc[0]["open_time"] == open_time


# --------------------------------------------------------------- explicit flags

class TestExplicitFlags:
    def test_explicit_true_still_requires_boundary_current_bar_rejected(self, tmp_path):
        """A stale asserted-true flag on a still-forming candle must not be
        trusted on its own.
        """
        store = _store(tmp_path)
        open_time = BASE_MS
        c = _candle(open_time, FIVE_MIN_MS, closed=True)

        now_ms = open_time + 10_000  # well inside the 5m bar
        store.update("ETH", "5m", c, persist=False, now_ms=now_ms)

        assert store.get_df("ETH", "5m") is None

    def test_explicit_true_past_bar_is_exposed(self, tmp_path):
        store = _store(tmp_path)
        open_time = BASE_MS
        c = _candle(open_time, FIVE_MIN_MS, closed=True)

        now_ms = open_time + FIVE_MIN_MS + 1_000
        store.update("ETH", "5m", c, persist=False, now_ms=now_ms)

        df = store.get_df("ETH", "5m")
        assert df is not None and len(df) == 1

    def test_explicit_false_always_rejected_even_past_boundary(self, tmp_path):
        """Explicit False must be honored as a definitive negative even once
        wall-clock time has passed the nominal close boundary.
        """
        store = _store(tmp_path)
        open_time = BASE_MS
        c = _candle(open_time, ONE_MIN_MS, closed=False)

        now_ms = open_time + 10 * ONE_MIN_MS  # long past boundary
        store.update("SOL", "1m", c, persist=False, now_ms=now_ms)

        assert store.get_df("SOL", "1m") is None

    def test_explicit_false_vs_missing_same_boundary_differ(self, tmp_path):
        """Same open/close/now, only the flag differs: explicit False must
        exclude while a missing flag (subject only to the boundary) includes.
        """
        store = _store(tmp_path)
        open_time = BASE_MS
        now_ms = open_time + ONE_MIN_MS + 5_000  # past boundary either way

        c_missing = _candle(open_time, ONE_MIN_MS)
        store.update("AAA", "1m", c_missing, persist=False, now_ms=now_ms)
        assert store.get_df("AAA", "1m") is not None

        c_false = _candle(open_time, ONE_MIN_MS, closed=False)
        store.update("BBB", "1m", c_false, persist=False, now_ms=now_ms)
        assert store.get_df("BBB", "1m") is None


# --------------------------------------------------------------- boundary exactness

class TestExactBoundary:
    def test_one_ms_before_boundary_not_closed(self, tmp_path):
        store = _store(tmp_path)
        open_time = BASE_MS
        c = _candle(open_time, THREE_MIN_MS)
        store.update("BTC", "3m", c, persist=False, now_ms=open_time + THREE_MIN_MS - 1)
        assert store.get_df("BTC", "3m") is None

    def test_exact_boundary_ms_is_closed(self, tmp_path):
        store = _store(tmp_path)
        open_time = BASE_MS
        c = _candle(open_time, THREE_MIN_MS)
        store.update("BTC", "3m", c, persist=False, now_ms=open_time + THREE_MIN_MS)
        assert store.get_df("BTC", "3m") is not None

    def test_inclusive_T_boundary_honored_when_duration_derived_would_differ(self, tmp_path):
        """T is documented as an inclusive end-of-bar ms (T == t+duration-1).
        A candle whose T implies a *later* boundary than the duration-derived
        one must still not be exposed until the later (more conservative)
        boundary is reached.
        """
        store = _store(tmp_path)
        open_time = BASE_MS
        # T implies a boundary 5s later than open_time+duration.
        c = {
            "t": open_time,
            "T": open_time + THREE_MIN_MS - 1 + 5_000,
            "o": "1", "h": "1", "l": "1", "c": "1", "v": "1",
        }
        # now_ms reaches the duration-derived boundary but not T+1.
        store.update("BTC", "3m", c, persist=False, now_ms=open_time + THREE_MIN_MS)
        assert store.get_df("BTC", "3m") is None

        store.update("BTC", "3m", c, persist=False, now_ms=open_time + THREE_MIN_MS + 5_000)
        assert store.get_df("BTC", "3m") is not None


# --------------------------------------------------------------- malformed timestamps

class TestMalformedTimestamps:
    def test_missing_open_time_is_dropped_not_stored(self, tmp_path):
        store = _store(tmp_path)
        c = {"T": BASE_MS, "o": "1", "h": "1", "l": "1", "c": "1", "v": "1"}
        store.update("BTC", "3m", c, persist=False, now_ms=BASE_MS + FIVE_MIN_MS)
        assert store.candle_count("BTC", "3m") == 0
        assert store.get_df("BTC", "3m") is None

    def test_non_numeric_open_time_is_dropped(self, tmp_path):
        store = _store(tmp_path)
        c = {"t": "not-a-number", "T": BASE_MS, "o": "1", "h": "1", "l": "1", "c": "1", "v": "1"}
        store.update("BTC", "3m", c, persist=False, now_ms=BASE_MS + FIVE_MIN_MS)
        assert store.candle_count("BTC", "3m") == 0

    def test_missing_close_time_falls_back_to_duration_derived_boundary(self, tmp_path):
        store = _store(tmp_path)
        open_time = BASE_MS
        c = {"t": open_time, "o": "1", "h": "1", "l": "1", "c": "1", "v": "1"}

        store.update("BTC", "3m", c, persist=False, now_ms=open_time + THREE_MIN_MS - 1)
        assert store.get_df("BTC", "3m") is None

        store.update("BTC", "3m", c, persist=False, now_ms=open_time + THREE_MIN_MS)
        assert store.get_df("BTC", "3m") is not None

    def test_non_numeric_close_time_ignored_duration_still_governs(self, tmp_path):
        store = _store(tmp_path)
        open_time = BASE_MS
        c = {
            "t": open_time, "T": "garbage",
            "o": "1", "h": "1", "l": "1", "c": "1", "v": "1",
        }
        store.update("BTC", "3m", c, persist=False, now_ms=open_time + THREE_MIN_MS)
        assert store.get_df("BTC", "3m") is not None

    def test_unknown_timeframe_with_no_T_never_closes(self, tmp_path):
        """No duration is known for an unrecognized timeframe string; without
        a usable T, there is no basis to ever call it closed.
        """
        store = _store(tmp_path)
        open_time = BASE_MS
        c = {"t": open_time, "o": "1", "h": "1", "l": "1", "c": "1", "v": "1"}
        store.update("BTC", "7m", c, persist=False, now_ms=open_time + 10 * FIFTEEN_MIN_MS)
        assert store.get_df("BTC", "7m") is None


# --------------------------------------------------------------- partial -> final finalization

class TestPartialToFinalFinalization:
    def test_time_alone_does_not_promote_cached_partial(self, tmp_path):
        """A partial sample cached from an earlier not-yet-closed update must
        stay excluded from get_df even after real/observed time has passed
        its boundary, until a fresh eligible update actually arrives.
        """
        store = _store(tmp_path)
        open_time = BASE_MS
        c = _candle(open_time, THREE_MIN_MS)

        store.update("BTC", "3m", c, persist=False, now_ms=open_time + 30_000)
        assert store.get_df("BTC", "3m") is None

        # No new update() call — only ask get_df with no cutoff (default path
        # a live caller would use). Passage of time alone (nothing re-checks
        # a stored record against a later clock read at get_df time) must
        # not have flipped it to closed.
        assert store.get_df("BTC", "3m") is None

    def test_later_eligible_update_finalizes_it(self, tmp_path):
        store = _store(tmp_path)
        open_time = BASE_MS
        partial = _candle(open_time, THREE_MIN_MS, c="100.1")
        final = _candle(open_time, THREE_MIN_MS, c="100.9")

        store.update("BTC", "3m", partial, persist=False, now_ms=open_time + 30_000)
        assert store.get_df("BTC", "3m") is None

        store.update("BTC", "3m", final, persist=False, now_ms=open_time + THREE_MIN_MS)
        df = store.get_df("BTC", "3m")
        assert df is not None
        assert len(df) == 1
        assert df.iloc[0]["close"] == 100.9


# --------------------------------------------------------------- known-good finalized sample protection

class TestFinalizedSampleProtection:
    def test_late_partial_does_not_overwrite_finalized_sample(self, tmp_path):
        store = _store(tmp_path)
        open_time = BASE_MS
        final = _candle(open_time, THREE_MIN_MS, c="100.9")
        late_partial = _candle(open_time, THREE_MIN_MS, c="999", closed=False)

        store.update("BTC", "3m", final, persist=False, now_ms=open_time + THREE_MIN_MS)
        store.update("BTC", "3m", late_partial, persist=False, now_ms=open_time + THREE_MIN_MS + 1_000)

        df = store.get_df("BTC", "3m")
        assert df is not None
        assert len(df) == 1
        assert df.iloc[0]["close"] == 100.9

    def test_duplicate_finalized_update_is_idempotent(self, tmp_path):
        store = _store(tmp_path)
        open_time = BASE_MS
        final = _candle(open_time, THREE_MIN_MS, c="100.9")

        now_ms = open_time + THREE_MIN_MS
        store.update("BTC", "3m", final, persist=False, now_ms=now_ms)
        store.update("BTC", "3m", dict(final), persist=False, now_ms=now_ms + 1_000)

        df = store.get_df("BTC", "3m")
        assert len(df) == 1


# --------------------------------------------------------------- duplicate / out-of-order

class TestDuplicateAndOutOfOrderUpdates:
    def test_out_of_order_updates_stay_chronologically_sorted(self, tmp_path):
        store = _store(tmp_path)
        t0, t1, t2 = BASE_MS, BASE_MS + THREE_MIN_MS, BASE_MS + 2 * THREE_MIN_MS
        now_ms = t2 + THREE_MIN_MS  # all three fully closed by now

        store.update("BTC", "3m", _candle(t1, THREE_MIN_MS), persist=False, now_ms=now_ms)
        store.update("BTC", "3m", _candle(t0, THREE_MIN_MS), persist=False, now_ms=now_ms)
        store.update("BTC", "3m", _candle(t2, THREE_MIN_MS), persist=False, now_ms=now_ms)

        df = store.get_df("BTC", "3m")
        assert list(df["open_time"]) == [t0, t1, t2]

    def test_repeated_identical_update_does_not_duplicate_row(self, tmp_path):
        store = _store(tmp_path)
        open_time = BASE_MS
        c = _candle(open_time, THREE_MIN_MS)
        now_ms = open_time + THREE_MIN_MS

        for _ in range(3):
            store.update("BTC", "3m", dict(c), persist=False, now_ms=now_ms)

        assert store.candle_count("BTC", "3m") == 1


# --------------------------------------------------------------- persisted preload

class TestPersistedPreload:
    def test_only_closed_candles_are_persisted(self, tmp_path):
        store = _store(tmp_path)
        open_time = BASE_MS
        c = _candle(open_time, THREE_MIN_MS)

        # Still forming: must not be persisted at all.
        store.update("BTC", "3m", c, persist=True, now_ms=open_time + 30_000)

        # Reload from the same underlying sqlite connection to check
        # nothing was written.
        fresh = CandleStore(store._conn)
        fresh.load_from_db("BTC", "3m", now_ms=open_time + 40_000)
        assert fresh.get_df("BTC", "3m") is None

    def test_closed_candle_persists_and_reloads_as_closed(self, tmp_path):
        store = _store(tmp_path)
        open_time = BASE_MS
        c = _candle(open_time, THREE_MIN_MS)
        now_ms = open_time + THREE_MIN_MS

        store.update("BTC", "3m", c, persist=True, now_ms=now_ms)

        fresh = CandleStore(store._conn)
        fresh.load_from_db("BTC", "3m", now_ms=now_ms + 1_000)
        df = fresh.get_df("BTC", "3m")
        assert df is not None
        assert len(df) == 1

    def test_preload_is_closed_bit_alone_does_not_authorize_before_boundary(self, tmp_path):
        """Defense-in-depth: even a DB row flagged is_closed=1, if reloaded
        at an observation instant still before its own close boundary, must
        not be exposed. (In practice this store never persists such a row
        itself; this guards the preload path independently of how the bit
        got set.)
        """
        store = _store(tmp_path)
        open_time = BASE_MS

        repo.upsert_candle(
            store._conn,
            Candle(
                symbol="BTC", timeframe="3m", open_time=open_time,
                close_time=open_time + THREE_MIN_MS - 1,
                open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0,
                is_closed=True,
            ),
        )

        fresh = CandleStore(store._conn)
        fresh.load_from_db("BTC", "3m", now_ms=open_time + 30_000)  # still inside the bar
        assert fresh.get_df("BTC", "3m") is None

        fresh2 = CandleStore(store._conn)
        fresh2.load_from_db("BTC", "3m", now_ms=open_time + THREE_MIN_MS)
        assert fresh2.get_df("BTC", "3m") is not None

    def test_valid_historical_data_without_nonstandard_flag_remains_usable(self, tmp_path):
        """Old rows persisted without ever having carried closed/is_closed
        semantics in the raw payload must still load and serve normally —
        this fix must not solve the problem by dropping all such history.
        """
        store = _store(tmp_path)
        open_time = BASE_MS
        c = {
            "t": open_time, "T": open_time + THREE_MIN_MS - 1,
            "o": "1", "h": "1", "l": "1", "c": "1", "v": "1",
        }
        now_ms = open_time + THREE_MIN_MS
        store.update("BTC", "3m", c, persist=True, now_ms=now_ms)

        fresh = CandleStore(store._conn)
        fresh.load_from_db("BTC", "3m", now_ms=now_ms + 10 * THREE_MIN_MS)
        df = fresh.get_df("BTC", "3m")
        assert df is not None and len(df) == 1


# --------------------------------------------------------------- 15m context / shared consumers

class TestSharedConsumersAndContext:
    def test_15m_context_candle_subject_to_same_gating(self, tmp_path):
        store = _store(tmp_path)
        open_time = BASE_MS
        c = _candle(open_time, FIFTEEN_MIN_MS)

        store.update("BTC", "15m", c, persist=False, now_ms=open_time + 60_000)
        assert store.get_df("BTC", "15m") is None

        store.update("BTC", "15m", c, persist=False, now_ms=open_time + FIFTEEN_MIN_MS)
        assert store.get_df("BTC", "15m") is not None

    def test_primary_3m_and_5m_both_gated_independently(self, tmp_path):
        store = _store(tmp_path)
        open_time = BASE_MS
        c3 = _candle(open_time, THREE_MIN_MS)
        c5 = _candle(open_time, FIVE_MIN_MS)

        now_ms = open_time + THREE_MIN_MS + 1  # past 3m boundary, inside 5m bar
        store.update("BTC", "3m", c3, persist=False, now_ms=now_ms)
        store.update("BTC", "5m", c5, persist=False, now_ms=now_ms)

        assert store.get_df("BTC", "3m") is not None
        assert store.get_df("BTC", "5m") is None


# --------------------------------------------------------------- consistent per-scan cutoff (get_df now_ms)

class TestConsistentScanCutoff:
    def test_now_ms_cutoff_excludes_candle_closed_after_cutoff_even_if_flagged_closed_in_store(
        self, tmp_path
    ):
        """Simulates the mid-scan race: a candle becomes closed (a fresh,
        eligible update lands) *after* a scan's cutoff was captured. A
        caller passing that earlier cutoff into get_df must not see it, even
        though the store itself now holds it as closed.
        """
        store = _store(tmp_path)
        open_time = BASE_MS
        c = _candle(open_time, THREE_MIN_MS)

        scan_cutoff_ms = open_time + THREE_MIN_MS - 500  # captured just before close

        # A concurrent update arrives and closes it slightly after the cutoff.
        store.update("BTC", "3m", c, persist=False, now_ms=open_time + THREE_MIN_MS)
        assert store.get_df("BTC", "3m") is not None  # no cutoff: visible

        # But judged against the earlier scan cutoff, it must not be exposed.
        assert store.get_df("BTC", "3m", now_ms=scan_cutoff_ms) is None

    def test_now_ms_cutoff_includes_candle_closed_before_cutoff(self, tmp_path):
        store = _store(tmp_path)
        open_time = BASE_MS
        c = _candle(open_time, THREE_MIN_MS)
        now_ms = open_time + THREE_MIN_MS

        store.update("BTC", "3m", c, persist=False, now_ms=now_ms)
        assert store.get_df("BTC", "3m", now_ms=now_ms + 5_000) is not None

    def test_ordinary_callers_get_df_without_now_ms_unaffected(self, tmp_path):
        """Public get_df interface stays compatible for callers that never
        pass now_ms (e.g. the existing signal scan path).
        """
        store = _store(tmp_path)
        open_time = BASE_MS
        c = _candle(open_time, THREE_MIN_MS)
        store.update("BTC", "3m", c, persist=False, now_ms=open_time + THREE_MIN_MS)

        df = store.get_df("BTC", "3m")
        assert df is not None
        assert list(df.columns) == ["open_time", "open", "high", "low", "close", "volume"]


# --------------------------------------------------------------- timestamp input validation

class TestTimestampValidation:
    """_safe_int/_close_boundary_ms must conservatively reject shapes that
    would otherwise silently coerce into a plausible-looking epoch value,
    rather than truncating/coercing them into an eligibility decision.
    """

    @pytest.mark.parametrize("raw_t", [True, False])
    def test_bool_open_time_rejected(self, tmp_path, raw_t):
        store = _store(tmp_path)
        c = {"t": raw_t, "T": BASE_MS, "o": "1", "h": "1", "l": "1", "c": "1", "v": "1"}
        store.update("BTC", "3m", c, persist=False, now_ms=BASE_MS + FIVE_MIN_MS)
        assert store.candle_count("BTC", "3m") == 0

    @pytest.mark.parametrize("raw_t", [float("inf"), float("-inf"), float("nan")])
    def test_nonfinite_open_time_rejected(self, tmp_path, raw_t):
        store = _store(tmp_path)
        c = {"t": raw_t, "T": BASE_MS, "o": "1", "h": "1", "l": "1", "c": "1", "v": "1"}
        store.update("BTC", "3m", c, persist=False, now_ms=BASE_MS + FIVE_MIN_MS)
        assert store.candle_count("BTC", "3m") == 0

    def test_fractional_open_time_rejected(self, tmp_path):
        store = _store(tmp_path)
        c = {
            "t": BASE_MS + 0.5, "T": BASE_MS + THREE_MIN_MS,
            "o": "1", "h": "1", "l": "1", "c": "1", "v": "1",
        }
        store.update("BTC", "3m", c, persist=False, now_ms=BASE_MS + FIVE_MIN_MS)
        assert store.candle_count("BTC", "3m") == 0

    def test_negative_open_time_rejected(self, tmp_path):
        store = _store(tmp_path)
        c = {"t": -1, "T": BASE_MS, "o": "1", "h": "1", "l": "1", "c": "1", "v": "1"}
        store.update("BTC", "3m", c, persist=False, now_ms=BASE_MS + FIVE_MIN_MS)
        assert store.candle_count("BTC", "3m") == 0

    def test_zero_open_time_accepted(self, tmp_path):
        """Legitimate zero open_time (e.g. a fixture epoch anchor) must
        remain usable, not be rejected alongside genuinely negative values.
        """
        store = _store(tmp_path)
        c = {"t": 0, "T": THREE_MIN_MS - 1, "o": "1", "h": "1", "l": "1", "c": "1", "v": "1"}
        store.update("BTC", "3m", c, persist=False, now_ms=THREE_MIN_MS)
        df = store.get_df("BTC", "3m")
        assert df is not None
        assert df.iloc[0]["open_time"] == 0

    def test_integral_float_open_time_accepted(self, tmp_path):
        store = _store(tmp_path)
        open_time = BASE_MS
        c = {
            "t": float(open_time), "T": float(open_time + THREE_MIN_MS - 1),
            "o": "1", "h": "1", "l": "1", "c": "1", "v": "1",
        }
        store.update("BTC", "3m", c, persist=False, now_ms=open_time + THREE_MIN_MS)
        assert store.get_df("BTC", "3m") is not None

    def test_numeric_string_millisecond_open_time_accepted(self, tmp_path):
        store = _store(tmp_path)
        open_time = BASE_MS
        c = {
            "t": str(open_time), "T": str(open_time + THREE_MIN_MS - 1),
            "o": "1", "h": "1", "l": "1", "c": "1", "v": "1",
        }
        store.update("BTC", "3m", c, persist=False, now_ms=open_time + THREE_MIN_MS)
        assert store.get_df("BTC", "3m") is not None

    def test_negative_close_time_rejected_duration_still_governs(self, tmp_path):
        store = _store(tmp_path)
        open_time = BASE_MS
        c = {"t": open_time, "T": -5, "o": "1", "h": "1", "l": "1", "c": "1", "v": "1"}
        store.update("BTC", "3m", c, persist=False, now_ms=open_time + THREE_MIN_MS)
        assert store.get_df("BTC", "3m") is not None

    def test_contradictory_close_time_before_open_time_withheld_even_past_duration(self, tmp_path):
        """T < t is self-contradictory raw data — the whole candle is
        withheld outright, not merely stripped of its T candidate and left
        to the duration-derived boundary. Unlike a missing/unusable T (which
        still permits duration-only eligibility, see
        test_missing_close_time_falls_back_to_duration_derived_boundary),
        this record is known-malformed and must never become eligible, not
        even once the nominal duration has long elapsed.
        """
        store = _store(tmp_path)
        open_time = BASE_MS
        c = {"t": open_time, "T": open_time - 1000, "o": "1", "h": "1", "l": "1", "c": "1", "v": "1"}

        store.update("BTC", "3m", c, persist=False, now_ms=open_time + 1_000)
        assert store.get_df("BTC", "3m") is None

        store.update("BTC", "3m", c, persist=False, now_ms=open_time + THREE_MIN_MS)
        assert store.get_df("BTC", "3m") is None

        store.update("BTC", "3m", c, persist=False, now_ms=open_time + 10 * THREE_MIN_MS)
        assert store.get_df("BTC", "3m") is None
        assert store.candle_count("BTC", "3m") == 1  # not dropped from the store, just never eligible

        # Not persisted either, even with persist=True and time far past the
        # nominal duration.
        store2 = _store(tmp_path, name="test2.db")
        store2.update("BTC", "3m", c, persist=True, now_ms=open_time + 10 * THREE_MIN_MS)
        assert repo.get_candles(store2._conn, "BTC", "3m", limit=10) == []

    def test_unsupported_timeframe_with_close_time_never_closes(self, tmp_path):
        """An unrecognized timeframe has no independently known duration to
        sanity-check T against, so an arbitrary T must never be sufficient
        on its own to call it closed.
        """
        store = _store(tmp_path)
        open_time = BASE_MS
        c = {"t": open_time, "T": open_time + 1000, "o": "1", "h": "1", "l": "1", "c": "1", "v": "1"}
        store.update("BTC", "7m", c, persist=False, now_ms=open_time + 10 * FIFTEEN_MIN_MS)
        assert store.get_df("BTC", "7m") is None


# --------------------------------------------------------------- base-compatible public-API repro

def test_missing_flag_future_public_api(tmp_path, monkeypatch):
    """Base-compatible reproduction of the original pilot bug via the
    original, always-supported public API (no now_ms kwarg at all): freeze
    the wall clock that candle_store.py reads by default, and use a
    still-forming bar (its close boundary is still in the future relative to
    that frozen clock). Neither update() nor get_df() is passed now_ms here,
    so this test also loads and runs unchanged against the pre-fix module
    (which has no now_ms parameter at all) — it would fail the final
    assertion there (the forming candle gets exposed), not raise a
    TypeError from an unsupported kwarg.
    """
    import apex.data.candle_store as candle_store

    frozen_now_s = BASE_MS / 1000.0  # candle_store's default clock is time.time() (seconds)
    monkeypatch.setattr(candle_store.time, "time", lambda: frozen_now_s)

    store = _store(tmp_path)
    open_time = BASE_MS  # opens exactly "now": well inside its own still-forming 3m bar
    c = _candle(open_time, THREE_MIN_MS)
    assert "closed" not in c and "is_closed" not in c

    store.update("BTC", "3m", c, persist=False)  # no now_ms: real-clock default path

    df = store.get_df("BTC", "3m")  # no now_ms: real-clock default path
    assert df is None


# =================================================================
# Candle-feed diagnostics (bounded, opt-in, default off)
#
# These pin the diagnostics contract from CANDLE-FEED-INSTRUMENTATION-
# PROPOSAL.md: disabled by default and purely observational (never changes
# eligibility, persistence, or get_df/candle_count output); source
# provenance (ws/backfill/preload) tracked separately; latest-received/
# latest-eligible markers advance by open-time (event) ordering, never by
# arrival order, so a late/out-of-order message cannot falsely advance
# either one; persistence success is only ever recorded for an actual
# successful repository call, never for an update ignored by the existing
# finalized-sample-protection guard; and a rollover to a newer bar before
# the prior one was ever confirmed eligible is tracked as a provisional
# "rollover observation", not a certified loss.
# =================================================================

class TestDiagnosticsDisabledByDefault:
    def test_default_constructor_diagnostics_disabled(self, tmp_path):
        store = _store(tmp_path)
        assert store.diagnostics_enabled is False

    def test_disabled_mode_never_allocates_diagnostics(self, tmp_path):
        store = _store(tmp_path)
        open_time = BASE_MS
        c = _candle(open_time, THREE_MIN_MS)
        store.update("BTC", "3m", c, persist=True, now_ms=open_time + THREE_MIN_MS)
        assert store.get_diagnostics("BTC", "3m") is None

    def test_prune_diagnostics_is_noop_when_disabled(self, tmp_path):
        store = _store(tmp_path)
        store.prune_diagnostics(set())  # must not raise

    def test_disabled_and_enabled_modes_produce_identical_functional_output(self, tmp_path):
        """Diagnostics must be purely observational: identical get_df/
        candle_count output for the same update sequence whether enabled or
        disabled — this is a behavior-equivalence check, not a counter check.
        """
        open_time = BASE_MS
        events = [
            (_candle(open_time, THREE_MIN_MS, c="1"), open_time + 30_000),
            (_candle(open_time, THREE_MIN_MS, c="2"), open_time + THREE_MIN_MS),
        ]

        store_disabled = CandleStore(init_db(str(tmp_path / "disabled.db")), diagnostics_enabled=False)
        store_enabled = CandleStore(init_db(str(tmp_path / "enabled.db")), diagnostics_enabled=True)

        for candle, now_ms in events:
            store_disabled.update("BTC", "3m", dict(candle), persist=True, now_ms=now_ms)
            store_enabled.update("BTC", "3m", dict(candle), persist=True, now_ms=now_ms, source="ws")

        df_disabled = store_disabled.get_df("BTC", "3m")
        df_enabled = store_enabled.get_df("BTC", "3m")
        assert df_disabled is not None and df_enabled is not None
        assert df_disabled.equals(df_enabled)
        assert store_disabled.candle_count("BTC", "3m") == store_enabled.candle_count("BTC", "3m")


class TestSourceProvenance:
    def test_ws_and_backfill_sources_tracked_separately(self, tmp_path):
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        open_time = BASE_MS
        now_ms = open_time + THREE_MIN_MS
        store.update("BTC", "3m", _candle(open_time, THREE_MIN_MS), persist=True, now_ms=now_ms, source="ws")

        diag = store.get_diagnostics("BTC", "3m")
        assert diag is not None
        assert diag.source_counts == {"ws": 1}
        assert diag.latest_received_source == "ws"

        open_time2 = open_time + THREE_MIN_MS
        store.update(
            "BTC", "3m", _candle(open_time2, THREE_MIN_MS),
            persist=True, now_ms=open_time2 + THREE_MIN_MS, source="backfill",
        )
        diag = store.get_diagnostics("BTC", "3m")
        assert diag.source_counts == {"ws": 1, "backfill": 1}
        assert diag.latest_received_source == "backfill"

    def test_preload_from_db_is_tagged_with_preload_source(self, tmp_path):
        store = _store(tmp_path)  # diagnostics disabled here; only used to seed the DB
        open_time = BASE_MS
        now_ms = open_time + THREE_MIN_MS
        store.update("BTC", "3m", _candle(open_time, THREE_MIN_MS), persist=True, now_ms=now_ms, source="ws")

        fresh = CandleStore(store._conn, diagnostics_enabled=True)
        fresh.load_from_db("BTC", "3m", now_ms=now_ms + 10_000)

        diag = fresh.get_diagnostics("BTC", "3m")
        assert diag is not None
        assert diag.source_counts == {"preload": 1}
        assert diag.latest_received_source == "preload"
        assert diag.latest_eligible_open_time == open_time
        assert diag.latest_eligible_source == "preload"

    def test_default_source_is_unknown_when_unspecified(self, tmp_path):
        """Existing/base-compatible callers that never pass `source` must not
        break — the default label is 'unknown', not an error.
        """
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        open_time = BASE_MS
        store.update("BTC", "3m", _candle(open_time, THREE_MIN_MS), persist=False, now_ms=open_time + THREE_MIN_MS)

        diag = store.get_diagnostics("BTC", "3m")
        assert diag.source_counts == {"unknown": 1}


class TestPersistenceDiagnostics:
    def test_successful_persist_recorded(self, tmp_path):
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        open_time = BASE_MS
        store.update(
            "BTC", "3m", _candle(open_time, THREE_MIN_MS),
            persist=True, now_ms=open_time + THREE_MIN_MS, source="ws",
        )

        diag = store.get_diagnostics("BTC", "3m")
        assert (diag.persist_attempts, diag.persist_successes, diag.persist_failures) == (1, 1, 0)

    def test_persist_failure_recorded_and_existing_warning_log_unchanged(self, tmp_path, monkeypatch, caplog):
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        open_time = BASE_MS

        def _broken_upsert(conn, candle):
            raise RuntimeError("disk full")

        monkeypatch.setattr(candle_store_module.repo, "upsert_candle", _broken_upsert)

        with caplog.at_level(logging.WARNING, logger="apex.data.candle_store"):
            store.update(
                "BTC", "3m", _candle(open_time, THREE_MIN_MS),
                persist=True, now_ms=open_time + THREE_MIN_MS, source="ws",
            )

        diag = store.get_diagnostics("BTC", "3m")
        assert (diag.persist_attempts, diag.persist_successes, diag.persist_failures) == (1, 0, 1)
        # Existing persistence error handling/logging is unchanged by diagnostics.
        assert "Failed to persist candle BTC/3m: disk full" in caplog.text

    def test_ignored_late_partial_after_closed_sample_is_not_a_persist_attempt(self, tmp_path):
        """Persistence success/attempt means an actual repository call — the
        finalized-sample-protection early return (see
        TestFinalizedSampleProtection) never reaches the persist block at
        all, so it must never be counted as an attempt.
        """
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        open_time = BASE_MS
        final = _candle(open_time, THREE_MIN_MS, c="100.9")
        late_partial = _candle(open_time, THREE_MIN_MS, c="999", closed=False)

        store.update("BTC", "3m", final, persist=True, now_ms=open_time + THREE_MIN_MS, source="ws")
        store.update(
            "BTC", "3m", late_partial, persist=True,
            now_ms=open_time + THREE_MIN_MS + 1_000, source="ws",
        )

        diag = store.get_diagnostics("BTC", "3m")
        assert diag.persist_attempts == 1
        assert diag.persist_successes == 1
        # The ignored late partial is still visible as a received, non-eligible update.
        assert diag.received_noneligible_count == 1
        assert diag.ignored_late_partial_count == 1

    def test_candle_construction_failure_distinct_from_repo_write_failure(self, tmp_path, monkeypatch):
        """A Candle() construction failure never reaches repo.upsert_candle
        at all — it must be distinguishable from an actual repository write
        failure (persist_failures), not folded into the same counter.

        `o="not-a-number"` cannot be used here: `_normalize()` already calls
        `float(...)` on the OHLC fields *before* the persist block is ever
        reached (see update()), so a non-numeric OHLC value raises straight
        out of update() itself, never reaching the try/except this test is
        meant to exercise. Valid OHLC is used instead, and the imported
        `Candle` constructor itself is monkeypatched to raise.
        """
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        open_time = BASE_MS
        good = _candle(open_time, THREE_MIN_MS)  # valid OHLC throughout

        upsert_spy = MagicMock()
        monkeypatch.setattr(candle_store_module.repo, "upsert_candle", upsert_spy)

        def _broken_candle_ctor(*args, **kwargs):
            raise ValueError("construction boom")

        monkeypatch.setattr(candle_store_module, "Candle", _broken_candle_ctor)

        store.update("BTC", "3m", good, persist=True, now_ms=open_time + THREE_MIN_MS, source="ws")

        diag = store.get_diagnostics("BTC", "3m")
        # persist_attempts counts only an actual repository call (see
        # CandleDiagnostics.persist_attempts) — a construction failure never
        # reaches that point, so attempts stays 0 here, not 1.
        assert diag.persist_attempts == 0
        assert diag.persist_construction_failures == 1
        assert diag.persist_failures == 0
        assert diag.persist_successes == 0
        upsert_spy.assert_not_called()

    def test_invalid_ohlc_raises_without_diagnostic_receive_or_persist_counts(self, tmp_path):
        """A bad OHLC field makes `_normalize()` raise straight out of
        `update()` (see test_candle_construction_failure_distinct_from_repo_write_failure's
        own note on this). Before this fix, diagnostic receive bookkeeping
        ran *before* `_normalize()`, so it still counted a rejected update
        even though the original exception propagated. It must count and
        allocate nothing for the rejected update, preserve the original
        exception, and still record a later, genuinely valid update
        normally.
        """
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        open_time = BASE_MS
        bad = _candle(open_time, THREE_MIN_MS, o="not-a-number")

        with pytest.raises(ValueError):
            store.update(
                "BTC", "3m", bad, persist=True,
                now_ms=open_time + THREE_MIN_MS, source="ws",
            )

        # Nothing was recorded — no diagnostics entry was even allocated
        # for the rejected update.
        assert store.get_diagnostics("BTC", "3m") is None

        open_time2 = open_time + THREE_MIN_MS
        store.update(
            "BTC", "3m", _candle(open_time2, THREE_MIN_MS),
            persist=False, now_ms=open_time2 + THREE_MIN_MS, source="ws",
        )
        diag = store.get_diagnostics("BTC", "3m")
        assert diag is not None
        assert diag.received_eligible_count == 1
        assert diag.received_noneligible_count == 0
        assert diag.persist_attempts == 0
        assert diag.persist_construction_failures == 0

    def test_persist_false_never_touches_persist_counters(self, tmp_path):
        """persist=False must never increment any persist_* counter, even
        for an otherwise-eligible candle — the whole persist block is
        skipped entirely (see update()'s `if persist and is_closed:` gate).
        """
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        open_time = BASE_MS
        store.update(
            "BTC", "3m", _candle(open_time, THREE_MIN_MS),
            persist=False, now_ms=open_time + THREE_MIN_MS, source="ws",
        )

        diag = store.get_diagnostics("BTC", "3m")
        assert (
            diag.persist_attempts,
            diag.persist_successes,
            diag.persist_failures,
            diag.persist_construction_failures,
        ) == (0, 0, 0, 0)


class TestLatestEligibleOrdering:
    def test_late_eligible_redelivery_of_older_bar_does_not_regress_latest(self, tmp_path):
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        t0, t1 = BASE_MS, BASE_MS + THREE_MIN_MS
        now_ms = t1 + THREE_MIN_MS

        # A newer bar (t1) is processed first...
        store.update("BTC", "3m", _candle(t1, THREE_MIN_MS), persist=False, now_ms=now_ms, source="ws")
        assert store.get_diagnostics("BTC", "3m").latest_eligible_open_time == t1

        # ...then a late-arriving eligible redelivery for the OLDER bar (t0)
        # must not regress latest_eligible_open_time backwards.
        store.update("BTC", "3m", _candle(t0, THREE_MIN_MS), persist=False, now_ms=now_ms, source="ws")
        diag = store.get_diagnostics("BTC", "3m")
        assert diag.latest_eligible_open_time == t1
        assert diag.received_eligible_count == 2  # both were received & genuinely eligible

    def test_later_eligible_update_advances_latest(self, tmp_path):
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        open_time = BASE_MS

        store.update(
            "BTC", "3m", _candle(open_time, THREE_MIN_MS, c="1"),
            persist=False, now_ms=open_time + 30_000, source="ws",
        )
        assert store.get_diagnostics("BTC", "3m").latest_eligible_open_time is None

        store.update(
            "BTC", "3m", _candle(open_time, THREE_MIN_MS, c="2"),
            persist=False, now_ms=open_time + THREE_MIN_MS, source="ws",
        )
        assert store.get_diagnostics("BTC", "3m").latest_eligible_open_time == open_time


class TestRolloverObservations:
    def test_rollover_to_new_bar_without_prior_eligibility_is_counted(self, tmp_path):
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        t0, t1 = BASE_MS, BASE_MS + THREE_MIN_MS

        # t0 forming, never confirmed closed...
        store.update("BTC", "3m", _candle(t0, THREE_MIN_MS), persist=False, now_ms=t0 + 30_000, source="ws")
        assert store.get_diagnostics("BTC", "3m").rollover_observations == 0

        # ...then t1 (a newer bar) arrives before t0 was ever confirmed eligible.
        store.update("BTC", "3m", _candle(t1, THREE_MIN_MS), persist=False, now_ms=t1 + 30_000, source="ws")
        assert store.get_diagnostics("BTC", "3m").rollover_observations == 1

    def test_no_rollover_when_prior_bar_closed_before_next_bar_arrives(self, tmp_path):
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        t0, t1 = BASE_MS, BASE_MS + THREE_MIN_MS

        store.update("BTC", "3m", _candle(t0, THREE_MIN_MS), persist=False, now_ms=t0 + THREE_MIN_MS, source="ws")
        store.update("BTC", "3m", _candle(t1, THREE_MIN_MS), persist=False, now_ms=t1 + THREE_MIN_MS, source="ws")

        assert store.get_diagnostics("BTC", "3m").rollover_observations == 0


class TestEmptyPreloadDoesNotFalselyReportObserved:
    """Regression: load_from_db used to allocate a CandleDiagnostics entry
    unconditionally, before checking whether the DB actually returned any
    rows — an empty preload (a very common case: first run, or a newly
    added symbol with no history yet) therefore got object-presence
    (non-None) mislabeled as "observed" traffic. Allocation must only
    happen once an actual row is about to be recorded.
    """

    def test_empty_db_preload_leaves_diagnostics_none(self, tmp_path):
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        store.load_from_db("BTC", "3m", now_ms=BASE_MS)  # no rows in the DB at all
        assert store.get_diagnostics("BTC", "3m") is None

    def test_empty_preload_then_snapshot_then_actual_receive_recorded_correctly(self, tmp_path):
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)

        store.load_from_db("BTC", "3m", now_ms=BASE_MS)
        assert store.get_diagnostics("BTC", "3m") is None  # "snapshot" #1: genuinely zero traffic

        open_time = BASE_MS
        store.update(
            "BTC", "3m", _candle(open_time, THREE_MIN_MS),
            persist=False, now_ms=open_time + THREE_MIN_MS, source="ws",
        )

        diag = store.get_diagnostics("BTC", "3m")  # "snapshot" #2: real traffic now
        assert diag is not None
        assert diag.source_counts == {"ws": 1}
        assert diag.latest_received_source == "ws"

    def test_empty_preload_does_not_alter_original_preload_or_candle_outcomes(self, tmp_path):
        """The fix must be diagnostics-only: get_df/candle_count for an empty
        preload stay exactly as before.
        """
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        store.load_from_db("BTC", "3m", now_ms=BASE_MS)
        assert store.candle_count("BTC", "3m") == 0
        assert store.get_df("BTC", "3m") is None

    def test_nonempty_preload_still_allocates_and_records(self, tmp_path):
        """Sanity: the fix must not also break the already-covered nonempty
        preload path (see TestSourceProvenance.test_preload_from_db_is_tagged_with_preload_source).
        """
        store = _store(tmp_path)  # diagnostics disabled here; only used to seed the DB
        open_time = BASE_MS
        now_ms = open_time + THREE_MIN_MS
        store.update("BTC", "3m", _candle(open_time, THREE_MIN_MS), persist=True, now_ms=now_ms, source="ws")

        fresh = CandleStore(store._conn, diagnostics_enabled=True)
        fresh.load_from_db("BTC", "3m", now_ms=now_ms + 10_000)

        diag = fresh.get_diagnostics("BTC", "3m")
        assert diag is not None
        assert diag.source_counts == {"preload": 1}


class TestZeroTrafficAndPruning:
    def test_zero_traffic_pair_returns_none(self, tmp_path):
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        assert store.get_diagnostics("BTC", "3m") is None

    def test_prune_diagnostics_resets_epoch_and_respects_membership(self, tmp_path):
        """prune_diagnostics (replace=True) starts a brand-new global
        diagnostics epoch: it clears ALL diagnostic records, including for a
        pair (BTC) that stays in membership, not only the pair (ETH) actually
        dropped — see set_diagnostics_membership's docstring. Membership
        itself, and the underlying candle cache, are unaffected by the
        epoch reset; only diagnostics bookkeeping is.
        """
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        open_time = BASE_MS
        store.update("BTC", "3m", _candle(open_time, THREE_MIN_MS), persist=False, now_ms=open_time + THREE_MIN_MS, source="ws")
        store.update("ETH", "3m", _candle(open_time, THREE_MIN_MS), persist=False, now_ms=open_time + THREE_MIN_MS, source="ws")

        # Sanity: both pairs have real pre-prune diagnostics, so the
        # post-prune "absent" assertions below actually exercise the reset
        # rather than trivially matching a never-populated pair.
        assert store.get_diagnostics("BTC", "3m") is not None
        assert store.get_diagnostics("ETH", "3m") is not None

        store.prune_diagnostics({("BTC", "3m")})

        # New epoch: every diagnostic record is cleared, including BTC's,
        # even though BTC stays in membership across the replace.
        assert store.get_diagnostics("BTC", "3m") is None
        assert store.get_diagnostics("ETH", "3m") is None

        # Membership is unaffected by the epoch reset: BTC is still
        # trackable (eligible to report real zero-traffic), ETH is not.
        assert store.is_diagnostics_tracked("BTC", "3m") is True
        assert store.is_diagnostics_tracked("ETH", "3m") is False

        # The candle cache is separate from diagnostics bookkeeping and must
        # be completely untouched by a diagnostics-only epoch reset.
        assert store.candle_count("BTC", "3m") == 1
        assert store.candle_count("ETH", "3m") == 1

        # A fresh valid update for BTC (still in membership) allocates a new
        # CandleDiagnostics and reports counts scoped to the new epoch only
        # — the pre-prune "ws": 1 receive must not be carried over.
        next_open_time = open_time + THREE_MIN_MS
        store.update(
            "BTC", "3m", _candle(next_open_time, THREE_MIN_MS),
            persist=False, now_ms=next_open_time + THREE_MIN_MS, source="ws",
        )
        new_diag = store.get_diagnostics("BTC", "3m")
        assert new_diag is not None
        assert new_diag.source_counts == {"ws": 1}
        assert new_diag.received_eligible_count == 1

        # ETH stays outside the current membership: a subsequent valid
        # receive still cannot allocate diagnostics for it.
        store.update(
            "ETH", "3m", _candle(next_open_time, THREE_MIN_MS),
            persist=False, now_ms=next_open_time + THREE_MIN_MS, source="ws",
        )
        assert store.get_diagnostics("ETH", "3m") is None


class TestDiagnosticsContainment:
    def test_broken_diagnostics_recording_does_not_break_candle_handling(self, tmp_path, monkeypatch, caplog):
        """Diagnostic failures must be contained: candle storage/persistence
        must succeed exactly as it would without diagnostics, and the
        failure is only ever visible at DEBUG.
        """
        def _broken_record_receive(self, *args, **kwargs):
            raise RuntimeError("diagnostics boom")

        monkeypatch.setattr(candle_store_module.CandleDiagnostics, "record_receive", _broken_record_receive)

        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        open_time = BASE_MS

        with caplog.at_level(logging.DEBUG, logger="apex.data.candle_store"):
            store.update(
                "BTC", "3m", _candle(open_time, THREE_MIN_MS),
                persist=True, now_ms=open_time + THREE_MIN_MS, source="ws",
            )

        df = store.get_df("BTC", "3m")
        assert df is not None and len(df) == 1
        assert repo.get_candles(store._conn, "BTC", "3m", limit=10) != []
        assert "Candle diagnostics failed" in caplog.text

    def test_broken_allocation_does_not_break_candle_handling(self, tmp_path, monkeypatch, caplog):
        """A failure allocating a new CandleDiagnostics entry (not just
        recording into an already-allocated one) must be contained too:
        update()/_get_or_create_diag must not abort ingestion or persistence.
        """
        def _broken_diag_ctor(*a, **kw):
            raise RuntimeError("allocation boom")

        monkeypatch.setattr(candle_store_module, "CandleDiagnostics", _broken_diag_ctor)

        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        open_time = BASE_MS

        with caplog.at_level(logging.DEBUG, logger="apex.data.candle_store"):
            store.update(
                "BTC", "3m", _candle(open_time, THREE_MIN_MS),
                persist=True, now_ms=open_time + THREE_MIN_MS, source="ws",
            )

        df = store.get_df("BTC", "3m")
        assert df is not None and len(df) == 1
        assert repo.get_candles(store._conn, "BTC", "3m", limit=10) != []
        assert "Candle diagnostics failed" in caplog.text
        assert store.get_diagnostics("BTC", "3m") is None  # allocation never succeeded

    def test_load_from_db_survives_broken_diagnostics_allocation(self, tmp_path, monkeypatch):
        """load_from_db's own _get_or_create_diag call must not abort the
        entire preload when diagnostics allocation fails internally.
        """
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=False)
        open_time = BASE_MS
        store.update("BTC", "3m", _candle(open_time, THREE_MIN_MS), persist=True, now_ms=open_time + THREE_MIN_MS)

        def _broken_diag_ctor(*a, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(candle_store_module, "CandleDiagnostics", _broken_diag_ctor)

        fresh = CandleStore(store._conn, diagnostics_enabled=True)
        fresh.load_from_db("BTC", "3m", now_ms=open_time + THREE_MIN_MS + 1_000)  # must not raise

        df = fresh.get_df("BTC", "3m")
        assert df is not None and len(df) == 1

    def test_hostile_str_exception_during_diagnostics_is_contained(self, tmp_path, monkeypatch, caplog):
        """A caught diagnostics-path exception whose own __str__ raises must
        never escape the diagnostics except handler — _log_diag_error never
        calls str(exc), only type(exc).__name__ and a fixed label.
        """

        class HostileError(RuntimeError):
            def __str__(self):
                raise RuntimeError("str() itself blew up")

        def _broken_record_receive(self, *args, **kwargs):
            raise HostileError("boom")

        monkeypatch.setattr(candle_store_module.CandleDiagnostics, "record_receive", _broken_record_receive)

        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        open_time = BASE_MS

        with caplog.at_level(logging.DEBUG, logger="apex.data.candle_store"):
            store.update(  # must not raise despite the hostile __str__
                "BTC", "3m", _candle(open_time, THREE_MIN_MS),
                persist=True, now_ms=open_time + THREE_MIN_MS, source="ws",
            )

        df = store.get_df("BTC", "3m")
        assert df is not None and len(df) == 1
        assert repo.get_candles(store._conn, "BTC", "3m", limit=10) != []
        assert "Candle diagnostics failed" in caplog.text
        assert "HostileError" in caplog.text  # type name still surfaces safely

    def test_diagnostic_error_logger_itself_raising_does_not_escape(self, tmp_path, monkeypatch):
        """A broken logger.debug (the diagnostics-failure log call itself)
        must not be allowed to escape into candle handling either.
        """

        def _broken_record_receive(self, *args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(candle_store_module.CandleDiagnostics, "record_receive", _broken_record_receive)
        monkeypatch.setattr(
            candle_store_module.logger, "debug",
            MagicMock(side_effect=RuntimeError("logger boom")),
        )

        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        open_time = BASE_MS

        store.update(  # must not raise despite both the diagnostic AND its own error log failing
            "BTC", "3m", _candle(open_time, THREE_MIN_MS),
            persist=True, now_ms=open_time + THREE_MIN_MS, source="ws",
        )

        df = store.get_df("BTC", "3m")
        assert df is not None and len(df) == 1
        assert repo.get_candles(store._conn, "BTC", "3m", limit=10) != []


# =================================================================
# Diagnostics: allocation cap, membership, provenance, recency
# =================================================================

class TestAllocationCap:
    def test_allocation_capped_at_max_diagnostic_pairs(self, tmp_path, monkeypatch):
        """Actual diagnostic memory (not just snapshot output) is capped."""
        monkeypatch.setattr(candle_store_module, "MAX_DIAGNOSTIC_PAIRS", 5)
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        open_time = BASE_MS

        for i in range(10):
            store.update(
                f"SYM{i}", "3m", _candle(open_time, THREE_MIN_MS),
                persist=False, now_ms=open_time + THREE_MIN_MS, source="ws",
            )

        allocated = sum(1 for i in range(10) if store.get_diagnostics(f"SYM{i}", "3m") is not None)
        assert allocated == 5

    def test_pairs_beyond_cap_are_untracked_not_zero_traffic(self, tmp_path, monkeypatch):
        """A dropped/untracked pair must be distinguishable from a tracked
        pair that legitimately has no traffic yet.
        """
        monkeypatch.setattr(candle_store_module, "MAX_DIAGNOSTIC_PAIRS", 1)
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        open_time = BASE_MS

        store.update("BTC", "3m", _candle(open_time, THREE_MIN_MS), persist=False, now_ms=open_time + THREE_MIN_MS, source="ws")
        # ETH/3m never gets allocated: the cap was already reached by BTC/3m.
        store.update("ETH", "3m", _candle(open_time, THREE_MIN_MS), persist=False, now_ms=open_time + THREE_MIN_MS, source="ws")

        assert store.is_diagnostics_tracked("BTC", "3m") is True
        assert store.get_diagnostics("BTC", "3m") is not None

        assert store.is_diagnostics_tracked("ETH", "3m") is False
        assert store.get_diagnostics("ETH", "3m") is None  # untracked, not "zero traffic"

    def test_default_cap_is_120_pairs(self, tmp_path):
        """Pins the documented default cap (no monkeypatching): a membership
        of more than 120 pairs is truncated to exactly 120 tracked.
        """
        assert candle_store_module.MAX_DIAGNOSTIC_PAIRS == 120
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        keys = [(f"SYM{i:04d}", "3m") for i in range(150)]

        store.set_diagnostics_membership(keys)

        tracked = sum(1 for k in keys if store.is_diagnostics_tracked(*k))
        assert tracked == 120
        assert store.is_diagnostics_tracked("SYM0000", "3m") is True
        assert store.is_diagnostics_tracked("SYM0149", "3m") is False

    def test_set_diagnostics_membership_enforces_cap_before_any_traffic(self, tmp_path, monkeypatch):
        """The cap applies at membership-registration time, not only once
        traffic starts arriving.
        """
        monkeypatch.setattr(candle_store_module, "MAX_DIAGNOSTIC_PAIRS", 2)
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)

        keys = [("BTC", "3m"), ("ETH", "3m"), ("SOL", "3m")]
        store.set_diagnostics_membership(keys)

        assert store.is_diagnostics_tracked("BTC", "3m") is True
        assert store.is_diagnostics_tracked("ETH", "3m") is True
        assert store.is_diagnostics_tracked("SOL", "3m") is False


class TestMembershipEpochs:
    def test_prune_then_readd_starts_fresh_epoch(self, tmp_path):
        """A symbol removed via prune and later re-added must start with
        zeroed diagnostic counters, not resume stale ones.
        """
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        open_time = BASE_MS

        store.update("BTC", "3m", _candle(open_time, THREE_MIN_MS), persist=False, now_ms=open_time + THREE_MIN_MS, source="ws")
        assert store.get_diagnostics("BTC", "3m").received_eligible_count == 1

        store.prune_diagnostics(set())  # BTC/3m removed from membership
        assert store.get_diagnostics("BTC", "3m") is None
        assert store.is_diagnostics_tracked("BTC", "3m") is False

        store.set_diagnostics_membership({("BTC", "3m")}, replace=False)
        assert store.is_diagnostics_tracked("BTC", "3m") is True

        open_time2 = open_time + THREE_MIN_MS
        store.update("BTC", "3m", _candle(open_time2, THREE_MIN_MS), persist=False, now_ms=open_time2 + THREE_MIN_MS, source="ws")
        diag = store.get_diagnostics("BTC", "3m")
        assert diag.received_eligible_count == 1  # fresh epoch, not 2

    def test_expand_membership_does_not_drop_existing_pairs(self, tmp_path):
        """replace=False (used before preloading newly-added pairs) must
        never remove already-tracked pairs.
        """
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        open_time = BASE_MS
        store.update("BTC", "3m", _candle(open_time, THREE_MIN_MS), persist=False, now_ms=open_time + THREE_MIN_MS, source="ws")

        store.set_diagnostics_membership({("ETH", "3m")}, replace=False)

        assert store.get_diagnostics("BTC", "3m") is not None  # untouched
        assert store.is_diagnostics_tracked("BTC", "3m") is True
        assert store.is_diagnostics_tracked("ETH", "3m") is True

    def test_replace_membership_bumps_generation_expand_does_not(self, tmp_path):
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        assert store.diagnostics_membership_generation == 0

        store.set_diagnostics_membership({("BTC", "3m")}, replace=False)
        assert store.diagnostics_membership_generation == 0

        store.set_diagnostics_membership({("BTC", "3m")}, replace=True)
        assert store.diagnostics_membership_generation == 1

        store.set_diagnostics_membership({("BTC", "3m")}, replace=True)
        assert store.diagnostics_membership_generation == 2

    def test_disabled_membership_calls_are_noop(self, tmp_path):
        store = _store(tmp_path)
        store.set_diagnostics_membership({("BTC", "3m")})  # must not raise
        assert store.diagnostics_membership_generation == 0
        assert store.is_diagnostics_tracked("BTC", "3m") is False

    def test_replace_membership_sets_epoch_started_timestamp(self, tmp_path, monkeypatch):
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        assert store.diagnostics_membership_epoch_started_ms is None

        monkeypatch.setattr(candle_store_module, "_now_ms", lambda: 12345)
        store.set_diagnostics_membership({("BTC", "3m")}, replace=True)
        assert store.diagnostics_membership_epoch_started_ms == 12345

    def test_expand_membership_does_not_change_epoch_timestamp(self, tmp_path, monkeypatch):
        """replace=False (a general primitive, no longer used by application
        code before an unconfirmed rebuild — see run_universe_ws_sync) must
        not itself start a new epoch.
        """
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)

        monkeypatch.setattr(candle_store_module, "_now_ms", lambda: 111)
        store.set_diagnostics_membership({("BTC", "3m")}, replace=True)

        monkeypatch.setattr(candle_store_module, "_now_ms", lambda: 222)
        store.set_diagnostics_membership({("ETH", "3m")}, replace=False)

        assert store.diagnostics_membership_epoch_started_ms == 111
        assert store.diagnostics_membership_generation == 1

    def test_old_membership_at_cap_swap_starts_fresh_epoch_for_retained_and_new_pairs(
        self, tmp_path, monkeypatch
    ):
        """Old membership fills the cap; replacing it with a set that swaps
        one symbol for another must still respect the cap, drop the removed
        pair, and start a genuinely fresh (zero-traffic) global epoch for
        EVERY pair in the new membership — including one that stayed in
        membership across the replace (BTC/ETH here), not just the newly
        added one (XRP) — since every count this store reports is only ever
        "since the current epoch began" (see module docstring).
        """
        monkeypatch.setattr(candle_store_module, "MAX_DIAGNOSTIC_PAIRS", 3)
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        open_time = BASE_MS

        old_keys = [("BTC", "3m"), ("ETH", "3m"), ("SOL", "3m")]
        store.set_diagnostics_membership(old_keys)
        for sym, tf in old_keys:
            store.update(
                sym, tf, _candle(open_time, THREE_MIN_MS),
                persist=False, now_ms=open_time + THREE_MIN_MS, source="ws",
            )
        assert all(store.get_diagnostics(sym, tf) is not None for sym, tf in old_keys)

        # Swap SOL for XRP, still exactly at the cap (simulates a successful
        # WS rebuild's final replace=True prune to the new actual membership).
        new_keys = [("BTC", "3m"), ("ETH", "3m"), ("XRP", "3m")]
        store.set_diagnostics_membership(new_keys, replace=True)

        assert store.is_diagnostics_tracked("SOL", "3m") is False
        assert store.get_diagnostics("SOL", "3m") is None  # dropped, not just untracked-but-cached

        # Retained pairs restart at a fresh epoch too — not preserved.
        assert store.is_diagnostics_tracked("BTC", "3m") is True
        assert store.get_diagnostics("BTC", "3m") is None
        assert store.is_diagnostics_tracked("ETH", "3m") is True
        assert store.get_diagnostics("ETH", "3m") is None

        assert store.is_diagnostics_tracked("XRP", "3m") is True
        assert store.get_diagnostics("XRP", "3m") is None  # fresh epoch: no traffic yet

        # A fresh receive for the retained BTC pair now starts genuinely at
        # 1, not resuming the pre-replace count of 1 into a 2.
        open_time2 = open_time + THREE_MIN_MS
        store.update(
            "BTC", "3m", _candle(open_time2, THREE_MIN_MS),
            persist=False, now_ms=open_time2 + THREE_MIN_MS, source="ws",
        )
        assert store.get_diagnostics("BTC", "3m").received_eligible_count == 1

    def test_full_cap_swap_retained_and_new_pairs_all_start_fresh_epoch(self, tmp_path):
        """Same as above but exercised at the real, untouched default cap
        (120 pairs, no monkeypatching) with a full-size membership: a
        replace=True that retains most pairs and adds a few new ones must
        still reset every retained pair's counters, not only the new ones.
        """
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        assert candle_store_module.MAX_DIAGNOSTIC_PAIRS == 120
        open_time = BASE_MS

        old_keys = [(f"SYM{i:04d}", "3m") for i in range(120)]
        store.set_diagnostics_membership(old_keys)
        for sym, tf in old_keys:
            store.update(
                sym, tf, _candle(open_time, THREE_MIN_MS),
                persist=False, now_ms=open_time + THREE_MIN_MS, source="ws",
            )
        assert all(store.get_diagnostics(sym, tf) is not None for sym, tf in old_keys)

        # Drop the last 10, keep the first 110, add 10 brand-new ones —
        # still exactly at the 120 cap.
        retained_keys = old_keys[:110]
        new_keys = [(f"NEW{i:04d}", "3m") for i in range(10)]
        store.set_diagnostics_membership(retained_keys + new_keys, replace=True)

        for sym, tf in retained_keys:
            assert store.is_diagnostics_tracked(sym, tf) is True
            assert store.get_diagnostics(sym, tf) is None  # fresh epoch, not preserved

        for sym, tf in new_keys:
            assert store.is_diagnostics_tracked(sym, tf) is True
            assert store.get_diagnostics(sym, tf) is None  # fresh epoch, no traffic yet

        for sym, tf in old_keys[110:]:
            assert store.is_diagnostics_tracked(sym, tf) is False
            assert store.get_diagnostics(sym, tf) is None  # dropped entirely

    def test_untracked_prospective_symbol_preload_is_not_recorded_or_reconstructed(self, tmp_path):
        """A symbol observed *before* it is added to membership (e.g. a
        prospective WS-rebuild candidate preloaded while the rebuild is
        still in flight — see run_universe_ws_sync) must not be tracked at
        all, and once it does join membership it starts a genuinely fresh
        epoch rather than having that earlier untracked activity blended in.
        """
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        store.set_diagnostics_membership({("BTC", "3m")})  # old/current membership only

        open_time = BASE_MS
        store.update(
            "ETH", "3m", _candle(open_time, THREE_MIN_MS),
            persist=False, now_ms=open_time + THREE_MIN_MS, source="ws",
        )
        assert store.is_diagnostics_tracked("ETH", "3m") is False
        assert store.get_diagnostics("ETH", "3m") is None

        # Only once actually added to membership (post-swap) does it become
        # tracked — and it starts at zero, not with the earlier activity.
        store.set_diagnostics_membership({("BTC", "3m"), ("ETH", "3m")}, replace=True)
        assert store.is_diagnostics_tracked("ETH", "3m") is True
        assert store.get_diagnostics("ETH", "3m") is None

    def test_broken_set_membership_is_contained(self, tmp_path, caplog):
        """A failure while establishing/replacing membership (e.g. around a
        universe refresh) must never raise out of set_diagnostics_membership.
        """
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)

        def _hostile_keys():
            yield ("BTC", "3m")
            raise RuntimeError("membership boom")

        with caplog.at_level(logging.DEBUG, logger="apex.data.candle_store"):
            store.set_diagnostics_membership(_hostile_keys())  # must not raise

        assert "Candle diagnostics failed (set_membership)" in caplog.text
        # Nothing was committed from a failed membership update.
        assert store.diagnostics_membership_generation == 0


class TestFiniteSourceLabels:
    def test_hostile_source_label_folds_into_unknown(self, tmp_path):
        """source_counts must stay bounded to the known category set even
        against an arbitrary/hostile caller-supplied source string.
        """
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        open_time = BASE_MS
        hostile = "ws\n[WARNING] FORGED" + "X" * 5000

        store.update("BTC", "3m", _candle(open_time, THREE_MIN_MS), persist=False, now_ms=open_time + THREE_MIN_MS, source=hostile)

        diag = store.get_diagnostics("BTC", "3m")
        assert set(diag.source_counts) <= set(candle_store_module.KNOWN_SOURCES)
        assert diag.source_counts == {"unknown": 1}
        assert diag.latest_received_source == "unknown"

    def test_source_counts_never_exceed_known_source_count(self, tmp_path):
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        open_time = BASE_MS
        for i, src in enumerate(["ws", "backfill", "preload", "unknown", "made-up-1", "made-up-2"]):
            store.update(
                "BTC", "3m", _candle(open_time + i * THREE_MIN_MS, THREE_MIN_MS),
                persist=False, now_ms=open_time + (i + 1) * THREE_MIN_MS, source=src,
            )
        diag = store.get_diagnostics("BTC", "3m")
        assert len(diag.source_counts) <= len(candle_store_module.KNOWN_SOURCES)


class TestReceiveRecency:
    def test_repeated_same_open_update_refreshes_receive_time_not_max_open(self, tmp_path):
        """A still-forming bar ticking repeatedly must refresh the recency
        marker every time, while the max-open marker (used for rollover/
        ordering) stays exactly where it was — never regressed, never
        spuriously advanced by a repeat.
        """
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        open_time = BASE_MS
        c = _candle(open_time, THREE_MIN_MS)

        store.update("BTC", "3m", dict(c), persist=False, now_ms=open_time + 10_000, source="ws")
        diag = store.get_diagnostics("BTC", "3m")
        assert diag.latest_received_at_ms == open_time + 10_000
        assert diag.latest_received_open_time == open_time

        store.update("BTC", "3m", dict(c), persist=False, now_ms=open_time + 20_000, source="ws")
        diag = store.get_diagnostics("BTC", "3m")
        assert diag.latest_received_at_ms == open_time + 20_000  # refreshed
        assert diag.latest_received_open_time == open_time  # unchanged (same bar)

    def test_out_of_order_repeat_never_regresses_max_open(self, tmp_path):
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        t0, t1 = BASE_MS, BASE_MS + THREE_MIN_MS

        store.update("BTC", "3m", _candle(t1, THREE_MIN_MS), persist=False, now_ms=t1 + 30_000, source="ws")
        assert store.get_diagnostics("BTC", "3m").latest_received_open_time == t1

        # An older, already-superseded bar arrives again — recency refreshes,
        # max-open must not regress to the older open_time.
        store.update("BTC", "3m", _candle(t0, THREE_MIN_MS), persist=False, now_ms=t1 + 40_000, source="ws")
        diag = store.get_diagnostics("BTC", "3m")
        assert diag.latest_received_open_time == t1
        assert diag.latest_received_at_ms == t1 + 40_000  # recency still refreshes


class TestBoundaryFields:
    def test_received_and_eligible_boundaries_are_populated(self, tmp_path):
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        open_time = BASE_MS
        now_ms = open_time + THREE_MIN_MS

        store.update("BTC", "3m", _candle(open_time, THREE_MIN_MS), persist=False, now_ms=now_ms, source="ws")

        diag = store.get_diagnostics("BTC", "3m")
        assert diag.latest_received_boundary_ms == open_time + THREE_MIN_MS
        assert diag.latest_eligible_boundary_ms == open_time + THREE_MIN_MS

    def test_forming_candle_has_received_boundary_but_no_eligible_boundary(self, tmp_path):
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        open_time = BASE_MS

        store.update("BTC", "3m", _candle(open_time, THREE_MIN_MS), persist=False, now_ms=open_time + 10_000, source="ws")

        diag = store.get_diagnostics("BTC", "3m")
        assert diag.latest_received_boundary_ms == open_time + THREE_MIN_MS
        assert diag.latest_eligible_boundary_ms is None


class TestRolloverSourceRestriction:
    def test_preload_rollover_is_not_counted(self, tmp_path):
        """Replaying historical data out of finalization order says nothing
        about a live WS gap — only genuine WS rollovers are counted.
        """
        store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
        t0, t1 = BASE_MS, BASE_MS + THREE_MIN_MS

        store.update("BTC", "3m", _candle(t0, THREE_MIN_MS), persist=False, now_ms=t0 + 30_000, source="preload")
        store.update("BTC", "3m", _candle(t1, THREE_MIN_MS), persist=False, now_ms=t1 + 30_000, source="backfill")

        assert store.get_diagnostics("BTC", "3m").rollover_observations == 0
