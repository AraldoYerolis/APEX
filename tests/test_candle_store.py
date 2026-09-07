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

import pytest

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
