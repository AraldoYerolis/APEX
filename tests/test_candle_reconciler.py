"""Behavior tests for apex.data.candle_reconciler.CandleReconciler.

All network/time is fully injected — no real sockets, no real sleeps, no
real DB beyond a real sqlite3 connection via apex.db.connection.init_db
(memory-equivalent, tmp_path-backed). The HyperliquidClient is a fake/mock:
these tests are about CandleReconciler's own drain/validation/retry/backoff/
lifecycle logic, not HyperliquidClient's own request shaping (see
test_hyperliquid_client.py for that).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from apex.data.candle_reconciler import (
    BACKOFF_SCHEDULE_SECONDS,
    CandleReconciler,
    MAX_ATTEMPTS_PER_WINDOW,
    ReconcilerConfig,
)
from apex.data.candle_store import CandleStore
from apex.db.connection import init_db

ONE_MIN_MS = 60_000
BASE_MS = 1_800_000_000_000


def _store(tmp_path, name="test.db") -> CandleStore:
    conn = init_db(str(tmp_path / name))
    return CandleStore(conn, reconciliation_enabled=True)


def _forming_ws_candle(open_time, duration=ONE_MIN_MS, close=None):
    return {
        "t": open_time, "T": open_time + duration - 1,
        "o": "1", "h": "1", "l": "1", "c": close if close is not None else "1", "v": "1",
    }


def _rest_row(
    open_time, duration=ONE_MIN_MS, o="1", h="1000000", low_price="0", c="1.5", v="10",
    symbol="BTC", interval="1m",
):
    """`h`/`low_price` default to a wide envelope so a test overriding only
    `c` (a very common pattern below) always produces genuinely valid OHLC
    (h >= max(o,c,low), low <= min(o,c,h)) without also having to hand-pick a
    matching high/low — see coordinator correction G on repairing malformed
    test fixtures rather than weakening validation.
    """
    return {
        "s": symbol, "i": interval,
        "t": open_time, "T": open_time + duration - 1,
        "o": o, "h": h, "l": low_price, "c": c, "v": v,
    }


def _make_gap(store: CandleStore, symbol: str, timeframe: str, first_open: int, skip_bars: int, base_now_ms: int, duration=ONE_MIN_MS):
    """Seed a genuine reconciliation gap window: a forming WS bar at
    `first_open`, then a later WS bar `skip_bars` intervals ahead — this
    proves `first_open` (and any intervening bars) were never confirmed
    closed and registers them as candidates.
    """
    store.set_reconciliation_membership({(symbol, timeframe)}, replace=False)
    store.update(symbol, timeframe, _forming_ws_candle(first_open, duration), persist=False,
                 now_ms=base_now_ms, source="ws")
    later_open = first_open + skip_bars * duration
    store.update(symbol, timeframe, _forming_ws_candle(later_open, duration), persist=False,
                 now_ms=base_now_ms + skip_bars * duration, source="ws")


def _reconciler(store, client, *, wall_clock_ms, config=None, token_provider=None, clock=None):
    return CandleReconciler(
        store, client, config or ReconcilerConfig(),
        token_provider=token_provider,
        clock=clock or (lambda: 0.0),
        wall_clock_ms=wall_clock_ms,
    )


# =================================================================
# Row validation (direct, deterministic — see _is_valid_row)
# =================================================================

class TestRowValidation:
    def test_forming_row_rejected_at_frozen_cutoff_even_though_a_later_cutoff_would_accept_it(self):
        """The exact mechanic behind 'capture cutoff before awaiting, use
        the same cutoff for every row from that attempt': a row must be
        judged only against the cutoff it was validated with.
        """
        open_time = BASE_MS
        row = _rest_row(open_time)
        frozen_cutoff = open_time + 30_000  # well before the 1m close boundary
        later_cutoff = open_time + 70_000  # after the close boundary

        assert CandleReconciler._is_valid_row(row, "BTC", "1m", open_time, open_time, frozen_cutoff) is False
        assert CandleReconciler._is_valid_row(row, "BTC", "1m", open_time, open_time, later_cutoff) is True

    def test_mismatched_symbol_identity_rejected(self):
        row = _rest_row(BASE_MS, symbol="ETH")
        assert CandleReconciler._is_valid_row(row, "BTC", "1m", BASE_MS, BASE_MS, BASE_MS + ONE_MIN_MS) is False

    def test_mismatched_interval_identity_rejected(self):
        row = _rest_row(BASE_MS, interval="3m")
        assert CandleReconciler._is_valid_row(row, "BTC", "1m", BASE_MS, BASE_MS, BASE_MS + ONE_MIN_MS) is False

    def test_out_of_range_open_time_rejected(self):
        row = _rest_row(BASE_MS + ONE_MIN_MS)  # beyond target_end_inclusive
        assert CandleReconciler._is_valid_row(row, "BTC", "1m", BASE_MS, BASE_MS, BASE_MS + 2 * ONE_MIN_MS) is False

    def test_contradictory_close_before_open_rejected(self):
        row = _rest_row(BASE_MS)
        row["T"] = BASE_MS - 1000
        assert CandleReconciler._is_valid_row(row, "BTC", "1m", BASE_MS, BASE_MS, BASE_MS + ONE_MIN_MS) is False

    @pytest.mark.parametrize("field,value", [("o", "nan"), ("h", None), ("v", "-1")])
    def test_malformed_ohlcv_rejected(self, field, value):
        row = _rest_row(BASE_MS)
        row[field] = value
        assert CandleReconciler._is_valid_row(row, "BTC", "1m", BASE_MS, BASE_MS, BASE_MS + ONE_MIN_MS) is False

    def test_high_below_max_rejected(self):
        row = _rest_row(BASE_MS, o="10", h="5", low_price="1", c="8")
        assert CandleReconciler._is_valid_row(row, "BTC", "1m", BASE_MS, BASE_MS, BASE_MS + ONE_MIN_MS) is False

    def test_low_above_min_rejected(self):
        row = _rest_row(BASE_MS, o="5", h="10", low_price="6", c="8")
        assert CandleReconciler._is_valid_row(row, "BTC", "1m", BASE_MS, BASE_MS, BASE_MS + ONE_MIN_MS) is False

    def test_negative_volume_rejected(self):
        row = _rest_row(BASE_MS, v="-5")
        assert CandleReconciler._is_valid_row(row, "BTC", "1m", BASE_MS, BASE_MS, BASE_MS + ONE_MIN_MS) is False

    def test_valid_row_accepted(self):
        row = _rest_row(BASE_MS)
        assert CandleReconciler._is_valid_row(row, "BTC", "1m", BASE_MS, BASE_MS, BASE_MS + ONE_MIN_MS) is True

    def test_non_dict_row_rejected(self):
        assert CandleReconciler._is_valid_row("garbage", "BTC", "1m", BASE_MS, BASE_MS, BASE_MS + ONE_MIN_MS) is False

    def test_missing_T_falls_back_to_duration_derived_boundary(self):
        """No `T` at all is not itself invalid — the duration-derived
        boundary (open_time + timeframe duration) alone is a sufficient
        eligibility candidate (see CandleStore._close_boundary_ms)."""
        row = _rest_row(BASE_MS)
        del row["T"]
        assert CandleReconciler._is_valid_row(row, "BTC", "1m", BASE_MS, BASE_MS, BASE_MS + ONE_MIN_MS) is True

    def test_exclusive_convention_T_accepted(self):
        """T == t + duration (exclusive-end convention) is defensively
        tolerated alongside the documented inclusive T == t + duration - 1
        — but the boundary calc always treats T as an inclusive end (+1),
        so this convention's effective boundary is one ms later
        (t + duration + 1) than the duration-derived one alone.
        """
        row = _rest_row(BASE_MS)
        row["T"] = BASE_MS + ONE_MIN_MS
        assert CandleReconciler._is_valid_row(row, "BTC", "1m", BASE_MS, BASE_MS, BASE_MS + ONE_MIN_MS) is False
        assert CandleReconciler._is_valid_row(row, "BTC", "1m", BASE_MS, BASE_MS, BASE_MS + ONE_MIN_MS + 1) is True

    def test_T_strictly_between_the_two_supported_conventions_rejected(self):
        row = _rest_row(BASE_MS)
        row["T"] = BASE_MS + ONE_MIN_MS - 500  # neither t+duration-1 nor t+duration
        assert CandleReconciler._is_valid_row(row, "BTC", "1m", BASE_MS, BASE_MS, BASE_MS + ONE_MIN_MS) is False

    def test_off_grid_open_time_rejected(self):
        row = _rest_row(BASE_MS + 1)  # 1ms off the 1m grid
        assert CandleReconciler._is_valid_row(
            row, "BTC", "1m", BASE_MS, BASE_MS + ONE_MIN_MS, BASE_MS + 2 * ONE_MIN_MS
        ) is False

    def test_fractional_t_rejected(self):
        row = _rest_row(BASE_MS)
        row["t"] = float(BASE_MS) + 0.5
        assert CandleReconciler._is_valid_row(row, "BTC", "1m", BASE_MS, BASE_MS, BASE_MS + ONE_MIN_MS) is False

    def test_negative_t_rejected(self):
        row = _rest_row(BASE_MS)
        row["t"] = -1
        assert CandleReconciler._is_valid_row(row, "BTC", "1m", -1, -1, BASE_MS + ONE_MIN_MS) is False


class TestPerCandidateGrace:
    """Remaining-scope item 1: every requested/applied row needs its own
    five-second grace, not just the oldest candidate in a window (see
    _select_targets' coarse pre-fetch gate, which only ever looks at
    target.start_open_time).
    """

    def test_row_past_close_boundary_but_within_grace_rejected(self):
        open_time = BASE_MS
        row = _rest_row(open_time)
        close_boundary = open_time + ONE_MIN_MS
        cutoff = close_boundary + 1_000  # eligible (past boundary), but grace is 5000ms
        assert CandleReconciler._is_valid_row(
            row, "BTC", "1m", open_time, open_time, cutoff, grace_ms=5_000
        ) is False

    def test_row_past_close_boundary_and_grace_accepted(self):
        open_time = BASE_MS
        row = _rest_row(open_time)
        close_boundary = open_time + ONE_MIN_MS
        cutoff = close_boundary + 5_000  # exactly grace-cleared
        assert CandleReconciler._is_valid_row(
            row, "BTC", "1m", open_time, open_time, cutoff, grace_ms=5_000
        ) is True

    def test_zero_grace_keeps_boundary_only_behavior(self):
        """Default grace_ms=0 must reduce exactly to the pre-existing
        boundary-only eligibility test — no behavior change for a caller
        that never passes grace_ms explicitly.
        """
        open_time = BASE_MS
        row = _rest_row(open_time)
        cutoff = open_time + ONE_MIN_MS  # exactly at the close boundary
        assert CandleReconciler._is_valid_row(row, "BTC", "1m", open_time, open_time, cutoff) is True

    def test_every_row_in_a_wide_response_gated_independently_not_just_the_oldest(self):
        """The exact reproduction from the completion scope: a 1m window at
        opens t0, t0+60s, t0+120s and a frozen cutoff of t0+181s. The oldest
        bar has long cleared grace; the newest closed only one second ago
        and must still be rejected even though it is within an
        already-eligible-to-fetch wider response.
        """
        t0 = BASE_MS
        cutoff = t0 + 181_000
        rows = [
            _rest_row(t0),
            _rest_row(t0 + ONE_MIN_MS),
            _rest_row(t0 + 2 * ONE_MIN_MS),
        ]
        results = [
            CandleReconciler._is_valid_row(row, "BTC", "1m", t0, t0 + 2 * ONE_MIN_MS, cutoff, grace_ms=5_000)
            for row in rows
        ]
        assert results == [True, True, False]


class TestRequestRangeGraceNarrowing:
    """Final-gaps pass item 1: a fresh REST request must itself never ask
    for a candidate whose own post-boundary grace has not yet cleared as of
    the frozen cutoff — narrowing the OUTBOUND request (see
    CandleReconciler._grace_cleared_take), not merely filtering the
    response after the fact (see TestPerCandidateGrace/TestPerCandidateGraceFullTick
    above, which remain the response-side coverage).
    """

    async def test_fresh_request_endpoint_excludes_the_not_yet_graced_candidate(self, tmp_path):
        """Exact reproduction from the approved scope: opens t0, t0+60s,
        t0+120s at a frozen cutoff of t0+181s with the default 5s grace —
        the third candidate must not even be requested.
        """
        t0 = BASE_MS
        store = _store(tmp_path)
        _make_gap(store, "BTC", "1m", t0, skip_bars=3, base_now_ms=t0)
        cutoff_ms = t0 + 181_000
        client = AsyncMock()
        client.get_candle_snapshot = AsyncMock(return_value=[])
        reconciler = _reconciler(store, client, wall_clock_ms=lambda: cutoff_ms)

        outcome = await reconciler.tick()

        assert outcome.fetches_attempted == 1
        requested_end_ms = client.get_candle_snapshot.await_args.args[3]
        third_open = t0 + 2 * ONE_MIN_MS
        assert requested_end_ms == t0 + ONE_MIN_MS + ONE_MIN_MS - 1  # ends within the 2nd candidate's bar
        assert requested_end_ms < third_open  # third candidate's open time never even reached

    async def test_ungraced_third_candidate_never_applied_even_if_response_includes_it(self, tmp_path):
        """A permissive/misbehaving upstream response containing the
        not-yet-requested third bar must still never have it applied —
        per-row validation is unchanged and remains authoritative
        regardless of what the request itself asked for.
        """
        t0 = BASE_MS
        store = _store(tmp_path)
        _make_gap(store, "BTC", "1m", t0, skip_bars=3, base_now_ms=t0)
        cutoff_ms = t0 + 181_000
        client = AsyncMock()
        client.get_candle_snapshot = AsyncMock(return_value=[
            _rest_row(t0, c="111.0"),
            _rest_row(t0 + ONE_MIN_MS, c="222.0"),
            _rest_row(t0 + 2 * ONE_MIN_MS, c="333.0"),
        ])
        reconciler = _reconciler(store, client, wall_clock_ms=lambda: cutoff_ms)

        await reconciler.tick()

        assert store.is_candle_closed("BTC", "1m", t0) is True
        assert store.is_candle_closed("BTC", "1m", t0 + ONE_MIN_MS) is True
        assert store.is_candle_closed("BTC", "1m", t0 + 2 * ONE_MIN_MS) is False

    async def test_entirely_within_grace_window_defers_without_consuming_an_attempt(self, tmp_path):
        """A single-candidate window whose own grace has not yet cleared
        must defer entirely — no fetch, and no retry/backoff attempt is
        consumed by this empty/grace-only deferral.
        """
        t0 = BASE_MS
        store = _store(tmp_path)
        _make_gap(store, "BTC", "1m", t0, skip_bars=1, base_now_ms=t0)
        cutoff_ms = t0 + ONE_MIN_MS + 1_000  # 1s past boundary, inside the default 5s grace
        client = AsyncMock()
        reconciler = _reconciler(store, client, wall_clock_ms=lambda: cutoff_ms)

        outcome = await reconciler.tick()

        assert outcome.fetches_attempted == 0
        client.get_candle_snapshot.assert_not_awaited()
        assert reconciler.counters.fetches_attempted == 0
        rs = reconciler._retry_state.get(("BTC", "1m"))
        assert rs is None or rs.attempts == 0  # no failure attempt consumed
        assert store.get_reconciliation_targets() != []  # window untouched, still pending

    async def test_exact_grace_boundary_candidate_is_requested(self, tmp_path):
        """_grace_cleared_take's unknown-T boundary is the conservative
        (later, exclusive-convention) one: candidate_open + duration + 1,
        one ms later than the duration-derived boundary alone — so at
        exactly duration + grace (the OLD boundary+grace) nothing is
        requested yet, and no failure/backoff attempt is consumed; only at
        duration + grace + 1 (the conservative boundary+grace) does the
        SAME reconciler request and accept a row, including one using the
        exclusive T convention. The inclusive convention's own boundary
        (asserted directly via _is_valid_row) is independently unaffected by
        this pre-fetch trim change.
        """
        t0 = BASE_MS
        store = _store(tmp_path)
        _make_gap(store, "BTC", "1m", t0, skip_bars=1, base_now_ms=t0)
        cutoff = {"t": t0 + ONE_MIN_MS + 5_000}  # old duration-only boundary + grace: not yet conservative-clear
        client = AsyncMock()
        client.get_candle_snapshot = AsyncMock(return_value=[])
        reconciler = _reconciler(store, client, wall_clock_ms=lambda: cutoff["t"])

        outcome = await reconciler.tick()

        assert outcome.fetches_attempted == 0
        client.get_candle_snapshot.assert_not_awaited()
        rs = reconciler._retry_state.get(("BTC", "1m"))
        assert rs is None or rs.attempts == 0  # no failure attempt consumed
        assert store.get_reconciliation_targets() != []  # gap unchanged, still pending

        # The inclusive convention's own per-row boundary is untouched by
        # the more-conservative pre-fetch trim: it independently clears at
        # exactly this same cutoff (t + duration + grace).
        inclusive_row = _rest_row(t0, c="1.0")
        assert CandleReconciler._is_valid_row(
            inclusive_row, "BTC", "1m", t0, t0, cutoff["t"], grace_ms=5_000
        ) is True

        cutoff["t"] = t0 + ONE_MIN_MS + 5_001  # conservative (exclusive-convention) boundary + grace
        exclusive_row = _rest_row(t0, c="1.0")
        exclusive_row["T"] = t0 + ONE_MIN_MS  # exclusive convention
        client.get_candle_snapshot = AsyncMock(return_value=[exclusive_row])

        outcome2 = await reconciler.tick()

        assert outcome2.fetches_attempted == 1
        assert store.is_candle_closed("BTC", "1m", t0) is True

    async def test_no_attempt_consumed_if_wall_clock_regresses_between_selection_and_fetch(self, tmp_path):
        """Defensive: coarse target selection and this target's own fresh
        per-fetch cutoff capture (see _run_tick — captured again,
        immediately before each fetch) should never disagree with a real
        monotonic wall clock, but if they ever did, the fine-grained grace
        trim must still correctly defer (take <= 0) without consuming an
        attempt, rather than shaping a request from a stale/inconsistent
        cutoff.
        """
        t0 = BASE_MS
        store = _store(tmp_path)
        _make_gap(store, "BTC", "1m", t0, skip_bars=1, base_now_ms=t0)
        calls = {"n": 0}

        def wall_clock():
            calls["n"] += 1
            if calls["n"] == 1:
                return t0 + ONE_MIN_MS + 5_000  # coarse gate: exactly grace-cleared
            return t0 + ONE_MIN_MS + 4_999  # per-fetch capture: regressed by 1ms

        client = AsyncMock()
        reconciler = _reconciler(store, client, wall_clock_ms=wall_clock)

        outcome = await reconciler.tick()

        assert outcome.fetches_attempted == 0
        client.get_candle_snapshot.assert_not_awaited()
        rs = reconciler._retry_state.get(("BTC", "1m"))
        assert rs is None or rs.attempts == 0

    async def test_frozen_narrowed_prefix_not_expanded_by_a_later_larger_cutoff_on_retry(self, tmp_path):
        """The narrowed request/drop range from the FIRST attempt is frozen
        exactly like any other attempted_end_open_time — a later tick's
        larger cutoff (which would now also grace-clear the third
        candidate) must never expand an already-in-progress failed prefix.
        """
        t0 = BASE_MS
        store = _store(tmp_path)
        _make_gap(store, "BTC", "1m", t0, skip_bars=3, base_now_ms=t0)
        cutoff = {"t": t0 + 181_000}  # only the first two candidates clear grace
        client = AsyncMock()
        client.get_candle_snapshot = AsyncMock(return_value=None)  # fetch fails
        mono = {"t": 0.0}
        reconciler = _reconciler(
            store, client, wall_clock_ms=lambda: cutoff["t"], clock=lambda: mono["t"],
        )

        await reconciler.tick()  # 1st failed attempt; freezes the narrowed 2-bar prefix
        rs = reconciler._retry_state[("BTC", "1m")]
        frozen_end = rs.attempted_end_open_time
        assert frozen_end == t0 + ONE_MIN_MS  # not the third candidate

        mono["t"] += BACKOFF_SCHEDULE_SECONDS[0] + 0.001
        cutoff["t"] = t0 + 10 * ONE_MIN_MS  # now comfortably clears every candidate's grace
        await reconciler.tick()

        rs_after = reconciler._retry_state[("BTC", "1m")]
        assert rs_after.attempted_end_open_time == frozen_end  # unchanged despite the larger cutoff
        called_args = client.get_candle_snapshot.await_args.args
        assert called_args[3] < t0 + 2 * ONE_MIN_MS  # request never grew to cover the third candidate

    async def test_cutoff_captured_before_await_governs_narrowing_despite_wall_clock_advancing_mid_fetch(
        self, tmp_path
    ):
        """The cutoff used to shape the request (and to validate the
        response) is the one captured before the await, never a fresher
        wall-clock read taken after the fetch returns.
        """
        t0 = BASE_MS
        store = _store(tmp_path)
        _make_gap(store, "BTC", "1m", t0, skip_bars=3, base_now_ms=t0)
        cutoff = {"t": t0 + 181_000}  # only two candidates clear grace at call time
        client = AsyncMock()

        async def slow_fetch(*a, **kw):
            cutoff["t"] = t0 + 10 * ONE_MIN_MS  # wall clock races ahead mid-fetch
            return [
                _rest_row(t0, c="111.0"),
                _rest_row(t0 + ONE_MIN_MS, c="222.0"),
                _rest_row(t0 + 2 * ONE_MIN_MS, c="333.0"),
            ]

        client.get_candle_snapshot = AsyncMock(side_effect=slow_fetch)
        reconciler = _reconciler(store, client, wall_clock_ms=lambda: cutoff["t"])

        await reconciler.tick()

        called_args = client.get_candle_snapshot.await_args.args
        assert called_args[3] < t0 + 2 * ONE_MIN_MS  # request shaped against the captured cutoff
        # The third row, even present in the response and now comfortably
        # past its own grace by the new wall-clock time, is still rejected
        # — validated against the ORIGINAL captured cutoff, not the current one.
        assert store.is_candle_closed("BTC", "1m", t0 + 2 * ONE_MIN_MS) is False
        assert store.is_candle_closed("BTC", "1m", t0) is True
        assert store.is_candle_closed("BTC", "1m", t0 + ONE_MIN_MS) is True

    async def test_mixed_successful_and_failed_prefixes_in_the_same_tick(self, tmp_path):
        store = _store(tmp_path)
        _make_gap(store, "AAA", "1m", BASE_MS, skip_bars=1, base_now_ms=BASE_MS)
        _make_gap(store, "BBB", "1m", BASE_MS, skip_bars=1, base_now_ms=BASE_MS)
        cutoff_ms = BASE_MS + 10 * ONE_MIN_MS
        client = AsyncMock()

        async def fetch(symbol, *a, **kw):
            if symbol == "AAA":
                return [_rest_row(BASE_MS, c="1.0", symbol="AAA")]
            return None  # BBB's fetch fails

        client.get_candle_snapshot = AsyncMock(side_effect=fetch)
        reconciler = _reconciler(
            store, client, wall_clock_ms=lambda: cutoff_ms,
            config=ReconcilerConfig(max_fetches_per_tick=2),
        )

        outcome = await reconciler.tick()

        assert outcome.fetches_attempted == 2
        assert store.is_candle_closed("AAA", "1m", BASE_MS) is True
        remaining = {t.symbol for t in store.get_reconciliation_targets()}
        assert remaining == {"BBB"}
        assert reconciler.counters.fetches_succeeded == 1
        assert reconciler.counters.fetches_failed == 1


class TestValidateRowsContainment:
    def test_one_bad_row_does_not_abort_good_rows(self):
        client = AsyncMock()
        reconciler = CandleReconciler(CandleStore(init_db(":memory:"), reconciliation_enabled=True), client)
        good = _rest_row(BASE_MS)
        bad = {"t": "not-a-number"}
        cutoff = BASE_MS + ONE_MIN_MS
        accepted = reconciler._validate_rows([good, bad], "BTC", "1m", BASE_MS, BASE_MS, ONE_MIN_MS, cutoff)
        assert list(accepted.keys()) == [BASE_MS]

    def test_duplicate_open_times_deduplicated(self):
        client = AsyncMock()
        reconciler = CandleReconciler(CandleStore(init_db(":memory:"), reconciliation_enabled=True), client)
        row1 = _rest_row(BASE_MS, c="1.0")
        row2 = _rest_row(BASE_MS, c="2.0")
        cutoff = BASE_MS + ONE_MIN_MS
        accepted = reconciler._validate_rows([row1, row2], "BTC", "1m", BASE_MS, BASE_MS, ONE_MIN_MS, cutoff)
        assert len(accepted) == 1  # one write per unique open_time, not two


# =================================================================
# Full-tick integration: positive repair
# =================================================================

class TestPositiveRepair:
    async def test_missing_bars_repaired_with_fresh_rest_ohlc_not_cached_forming(self, tmp_path):
        store = _store(tmp_path)
        base_now = BASE_MS
        _make_gap(store, "BTC", "1m", BASE_MS, skip_bars=3, base_now_ms=base_now)
        # Sanity: 3 candidates registered (BASE_MS, +60s, +120s), all still
        # forming/never-confirmed-closed in memory, with the *stale* forming
        # value cached for the first one.
        targets = store.get_reconciliation_targets()
        assert len(targets) == 1
        assert (targets[0].start_open_time, targets[0].end_open_time) == (BASE_MS, BASE_MS + 2 * ONE_MIN_MS)

        cutoff_ms = BASE_MS + 10 * ONE_MIN_MS  # comfortably past every candidate's boundary + grace
        client = AsyncMock()
        client.get_candle_snapshot = AsyncMock(return_value=[
            _rest_row(BASE_MS, c="111.0"),
            _rest_row(BASE_MS + ONE_MIN_MS, c="222.0"),
            _rest_row(BASE_MS + 2 * ONE_MIN_MS, c="333.0"),
        ])
        reconciler = _reconciler(store, client, wall_clock_ms=lambda: cutoff_ms)

        outcome = await reconciler.tick()

        assert outcome.skipped is False
        client.get_candle_snapshot.assert_awaited_once()
        df = store.get_df("BTC", "1m", now_ms=cutoff_ms)
        assert df is not None
        closes = dict(zip(df["open_time"], df["close"]))
        assert closes[BASE_MS] == 111.0  # repaired, not the stale forming "1"
        assert closes[BASE_MS + ONE_MIN_MS] == 222.0
        assert closes[BASE_MS + 2 * ONE_MIN_MS] == 333.0
        assert store.get_reconciliation_targets() == []  # window fully resolved
        assert reconciler.counters.rows_persisted == 3
        assert reconciler.counters.fetches_succeeded == 1

    async def test_upstream_symbol_casing_used_for_request(self, tmp_path, monkeypatch):
        from apex.data import market_universe

        market_universe._record_upstream_symbol("kPEPE", "KPEPE")
        try:
            store = _store(tmp_path)
            _make_gap(store, "KPEPE", "1m", BASE_MS, skip_bars=1, base_now_ms=BASE_MS)
            cutoff_ms = BASE_MS + 10 * ONE_MIN_MS
            client = AsyncMock()
            client.get_candle_snapshot = AsyncMock(return_value=[])
            reconciler = _reconciler(store, client, wall_clock_ms=lambda: cutoff_ms)

            await reconciler.tick()

            called_symbol = client.get_candle_snapshot.await_args.args[0]
            assert called_symbol == "kPEPE"
        finally:
            market_universe._reset_upstream_symbol_map()


class TestPerCandidateGraceFullTick:
    async def test_freshly_closed_candidate_in_a_wider_eligible_window_is_withheld_until_its_own_grace_clears(
        self, tmp_path
    ):
        """Full-tick counterpart of TestPerCandidateGrace: a 3-bar gap
        window whose oldest candidate cleared grace long ago but whose
        newest candidate closed only 1s before the frozen cutoff must have
        only the first two bars actually applied — the third stays an open
        reconciliation target, never silently accepted just because it fell
        inside an already-fetchable wider response.
        """
        store = _store(tmp_path)
        _make_gap(store, "BTC", "1m", BASE_MS, skip_bars=3, base_now_ms=BASE_MS)
        cutoff = {"t": BASE_MS + 3 * ONE_MIN_MS + 1_000}  # third bar's boundary + 1s: inside its grace
        client = AsyncMock()
        client.get_candle_snapshot = AsyncMock(return_value=[
            _rest_row(BASE_MS, c="111.0"),
            _rest_row(BASE_MS + ONE_MIN_MS, c="222.0"),
            _rest_row(BASE_MS + 2 * ONE_MIN_MS, c="333.0"),
        ])
        reconciler = _reconciler(store, client, wall_clock_ms=lambda: cutoff["t"])  # default 5s grace

        await reconciler.tick()

        assert store.is_candle_closed("BTC", "1m", BASE_MS) is True
        assert store.is_candle_closed("BTC", "1m", BASE_MS + ONE_MIN_MS) is True
        assert store.is_candle_closed("BTC", "1m", BASE_MS + 2 * ONE_MIN_MS) is False
        targets = store.get_reconciliation_targets()
        assert len(targets) == 1
        assert targets[0].start_open_time == BASE_MS + 2 * ONE_MIN_MS  # window advanced past the resolved prefix

        # A later tick, once the third candidate's own grace has cleared
        # under _grace_cleared_take's conservative (exclusive-convention,
        # one-ms-later) boundary — i.e. +5_001, not the old duration-only
        # +5_000 — completes the window. Same reconciler instance reused
        # (mutable injected wall clock), not a fresh one.
        cutoff["t"] = BASE_MS + 3 * ONE_MIN_MS + 5_001
        client.get_candle_snapshot = AsyncMock(return_value=[_rest_row(BASE_MS + 2 * ONE_MIN_MS, c="333.0")])
        await reconciler.tick()
        assert store.is_candle_closed("BTC", "1m", BASE_MS + 2 * ONE_MIN_MS) is True
        assert store.get_reconciliation_targets() == []


class TestNoAgeOnlyPromotion:
    async def test_elapsed_time_alone_never_promotes_cached_forming_ohlc(self, tmp_path):
        """A candidate only ever gets closed via a genuine REST write or a
        late WS receipt — never merely because wall time advanced.
        """
        store = _store(tmp_path)
        _make_gap(store, "BTC", "1m", BASE_MS, skip_bars=2, base_now_ms=BASE_MS)
        # No reconciler tick at all — just advance time and re-check.
        assert store.get_df("BTC", "1m", now_ms=BASE_MS + 999 * ONE_MIN_MS) is None
        assert store.get_reconciliation_targets() != []  # gap still open


class TestLateWsSkip:
    async def test_late_ws_resolution_skips_the_bar_without_network_and_shrinks_window(self, tmp_path):
        store = _store(tmp_path)
        _make_gap(store, "BTC", "1m", BASE_MS, skip_bars=2, base_now_ms=BASE_MS)

        # A late, genuinely eligible WS delivery for the leading candidate.
        store.update(
            "BTC", "1m", _rest_row(BASE_MS, c="999.0"),
            persist=False, now_ms=BASE_MS + 5 * ONE_MIN_MS, source="ws",
        )
        targets = store.get_reconciliation_targets()
        assert len(targets) == 1
        assert targets[0].start_open_time == BASE_MS + ONE_MIN_MS  # leading edge advanced

        cutoff_ms = BASE_MS + 10 * ONE_MIN_MS
        client = AsyncMock()
        client.get_candle_snapshot = AsyncMock(return_value=[_rest_row(BASE_MS + ONE_MIN_MS, c="222.0")])
        reconciler = _reconciler(store, client, wall_clock_ms=lambda: cutoff_ms)

        await reconciler.tick()

        # The already-resolved bar is untouched (never overwritten).
        df = store.get_df("BTC", "1m", now_ms=cutoff_ms)
        closes = dict(zip(df["open_time"], df["close"]))
        assert closes[BASE_MS] == 999.0
        assert closes[BASE_MS + ONE_MIN_MS] == 222.0


class TestPersistFailureRetry:
    async def test_db_upsert_failure_retains_retry_and_eventually_succeeds(self, tmp_path, monkeypatch):
        import apex.data.candle_store as candle_store_module

        store = _store(tmp_path)
        _make_gap(store, "BTC", "1m", BASE_MS, skip_bars=1, base_now_ms=BASE_MS)
        cutoff_ms = BASE_MS + 10 * ONE_MIN_MS
        client = AsyncMock()
        client.get_candle_snapshot = AsyncMock(return_value=[_rest_row(BASE_MS, c="111.0")])
        reconciler = _reconciler(store, client, wall_clock_ms=lambda: cutoff_ms)

        def _broken_upsert(conn, candle):
            raise RuntimeError("disk full")

        monkeypatch.setattr(candle_store_module.repo, "upsert_candle", _broken_upsert)

        await reconciler.tick()

        # In-memory resolution happened (get_df sees it) even though the
        # DB write failed — but persistence is NOT counted as done, and the
        # store itself (not a reconciler-owned payload cache) now tracks
        # this exact open_time as pending a persist-only retry.
        df = store.get_df("BTC", "1m", now_ms=cutoff_ms)
        assert df is not None and df.iloc[0]["close"] == 111.0
        assert reconciler.counters.rows_persisted == 0
        assert store.get_persist_retry_candidates() == [("BTC", "1m", BASE_MS)]

        monkeypatch.undo()  # restore working repo.upsert_candle for the retry
        await reconciler.tick()  # drains pending retries at the top of the next tick

        assert reconciler.counters.rows_persist_retry_succeeded == 1
        assert store.get_persist_retry_candidates() == []

    async def test_stale_persist_retry_never_overwrites_newer_ws_resolved_value(self, tmp_path, monkeypatch):
        """Correction A / confirmed F1: a persist-only retry must read the
        CURRENT frozen in-memory record, never a cached copy of the row that
        originally failed to persist — so a later, more-authoritative WS
        write for the same bar can never be clobbered by a stale replay.
        """
        import apex.data.candle_store as candle_store_module

        store = _store(tmp_path)
        _make_gap(store, "BTC", "1m", BASE_MS, skip_bars=1, base_now_ms=BASE_MS)
        cutoff_ms = BASE_MS + 10 * ONE_MIN_MS
        client = AsyncMock()
        client.get_candle_snapshot = AsyncMock(return_value=[_rest_row(BASE_MS, c="111.0")])
        reconciler = _reconciler(store, client, wall_clock_ms=lambda: cutoff_ms)

        def _broken_upsert(conn, candle):
            raise RuntimeError("disk full")

        monkeypatch.setattr(candle_store_module.repo, "upsert_candle", _broken_upsert)
        await reconciler.tick()  # REST write succeeds in-memory (111.0), DB write fails, queued pending

        monkeypatch.undo()  # DB now works again
        # A newer, more-authoritative WS delivery for the SAME bar arrives
        # and persists successfully before the next reconciler tick.
        store.update(
            "BTC", "1m", _rest_row(BASE_MS, c="999.0"),
            persist=True, now_ms=cutoff_ms + 1, source="ws",
        )
        assert store.get_persist_retry_candidates() == []  # superseded, cleared

        await reconciler._drain_pending_persist_retries(monotonic_now=0.0)

        df = store.get_df("BTC", "1m", now_ms=cutoff_ms + 1)
        assert df.iloc[0]["close"] == 999.0  # never clobbered by the stale 111.0 retry

    async def test_persist_retry_targets_newest_failed_revision_not_stale_cache(self, tmp_path, monkeypatch):
        """A newer WS delivery for the same bar that ALSO fails to persist
        must become the thing retried — never the older cached REST payload.
        """
        import apex.data.candle_store as candle_store_module

        store = _store(tmp_path)
        _make_gap(store, "BTC", "1m", BASE_MS, skip_bars=1, base_now_ms=BASE_MS)
        cutoff_ms = BASE_MS + 10 * ONE_MIN_MS
        client = AsyncMock()
        client.get_candle_snapshot = AsyncMock(return_value=[_rest_row(BASE_MS, c="111.0")])
        reconciler = _reconciler(store, client, wall_clock_ms=lambda: cutoff_ms)

        def _broken_upsert(conn, candle):
            raise RuntimeError("disk full")

        monkeypatch.setattr(candle_store_module.repo, "upsert_candle", _broken_upsert)
        await reconciler.tick()  # 111.0 queued pending, DB still broken

        # Newer WS delivery for the same bar ALSO fails to persist (DB still broken).
        store.update(
            "BTC", "1m", _rest_row(BASE_MS, c="999.0"),
            persist=True, now_ms=cutoff_ms + 1, source="ws",
        )
        assert store.get_persist_retry_candidates() == [("BTC", "1m", BASE_MS)]

        monkeypatch.undo()  # DB now works
        outcome = store.retry_persist("BTC", "1m", BASE_MS)
        assert outcome is True

        persisted_rows = candle_store_module.repo.get_candles(store._conn, "BTC", "1m", limit=10)
        assert persisted_rows[0]["close"] == 999.0  # the newest revision was what got persisted

    async def test_persist_retry_exhausts_after_three_failed_local_attempts_and_drops(self, tmp_path, monkeypatch):
        """Correction item 3: a permanently broken DB must not retry a
        pending persist marker forever — after MAX_ATTEMPTS_PER_WINDOW (3)
        failed local retries (monotonic backoff schedule), the exact
        pending item is dropped via CandleStore.drop_persist_pending and
        counted as a distinct not-persisted exhaustion, never a false
        success and never retried again.
        """
        import apex.data.candle_store as candle_store_module

        store = _store(tmp_path)
        _make_gap(store, "BTC", "1m", BASE_MS, skip_bars=1, base_now_ms=BASE_MS)
        cutoff_ms = BASE_MS + 10 * ONE_MIN_MS
        client = AsyncMock()
        client.get_candle_snapshot = AsyncMock(return_value=[_rest_row(BASE_MS, c="111.0")])
        mono = {"t": 0.0}
        reconciler = _reconciler(store, client, wall_clock_ms=lambda: cutoff_ms, clock=lambda: mono["t"])

        def _broken_upsert(conn, candle):
            raise RuntimeError("disk full")

        monkeypatch.setattr(candle_store_module.repo, "upsert_candle", _broken_upsert)
        await reconciler.tick()  # REST write succeeds in-memory, DB write fails, queued pending
        assert store.get_persist_retry_candidates() == [("BTC", "1m", BASE_MS)]

        # MAX_ATTEMPTS_PER_WINDOW (3) local retry-drain attempts, all
        # failing (the initial persist failure above is separate from —
        # and does not itself count toward — this local retry budget).
        for backoff in BACKOFF_SCHEDULE_SECONDS[:MAX_ATTEMPTS_PER_WINDOW]:
            mono["t"] += backoff + 0.001
            await reconciler._drain_pending_persist_retries(monotonic_now=mono["t"])

        assert reconciler.counters.rows_persist_retry_exhausted == 1
        assert reconciler.counters.rows_persist_retry_succeeded == 0
        assert store.get_persist_retry_candidates() == []  # dropped, not retried again
        assert store.persist_exhausted_total == 1
        assert store.persist_pending_overflow_total == 0  # distinct from the unrelated cap eviction

        # A further drain does nothing — it's genuinely gone, not silently
        # re-queued or re-attempted.
        monkeypatch.undo()
        mono["t"] += 1000.0
        await reconciler._drain_pending_persist_retries(monotonic_now=mono["t"])
        assert reconciler.counters.rows_persist_retry_succeeded == 0
        assert store.persist_exhausted_total == 1

        # The in-memory record itself is untouched/retained (never lost).
        df = store.get_df("BTC", "1m", now_ms=cutoff_ms)
        assert df is not None and df.iloc[0]["close"] == 111.0

    async def test_ws_origin_persist_failure_during_pending_gap_is_retried(self, tmp_path, monkeypatch):
        """A WS-origin (not reconciliation-origin) closed-candle persist
        failure for a pair already reconciliation-tracked must also be
        picked up by the same bounded persist-retry drain."""
        import apex.data.candle_store as candle_store_module

        store = _store(tmp_path)
        store.set_reconciliation_membership({("BTC", "1m")}, replace=False)
        cutoff_ms = BASE_MS + 10 * ONE_MIN_MS
        client = AsyncMock()
        reconciler = _reconciler(store, client, wall_clock_ms=lambda: cutoff_ms)

        def _broken_upsert(conn, candle):
            raise RuntimeError("disk full")

        monkeypatch.setattr(candle_store_module.repo, "upsert_candle", _broken_upsert)
        store.update(
            "BTC", "1m", _rest_row(BASE_MS, c="42.0"),
            persist=True, now_ms=cutoff_ms, source="ws",
        )
        assert store.get_persist_retry_candidates() == [("BTC", "1m", BASE_MS)]

        monkeypatch.undo()
        await reconciler._drain_pending_persist_retries(monotonic_now=0.0)
        assert reconciler.counters.rows_persist_retry_succeeded == 1
        assert store.get_persist_retry_candidates() == []


class TestMembershipChurnBounding:
    async def test_remove_and_readd_same_open_time_between_ticks_does_not_reuse_stale_state(self, tmp_path):
        """Correction item 2: a pair removed from reconciliation membership
        and re-added (between ticks) with a gap window that happens to
        start at the exact same open_time as before must NOT reuse the old
        (possibly already-backed-off/near-exhausted) retry state — the
        store's per-pair incarnation token must force a fresh start.
        """
        store = _store(tmp_path)
        _make_gap(store, "BTC", "1m", BASE_MS, skip_bars=1, base_now_ms=BASE_MS)
        cutoff_ms = BASE_MS + 10 * ONE_MIN_MS
        client = AsyncMock()
        client.get_candle_snapshot = AsyncMock(return_value=None)  # every fetch fails
        mono = {"t": 0.0}
        reconciler = _reconciler(store, client, wall_clock_ms=lambda: cutoff_ms, clock=lambda: mono["t"])

        # Two failed attempts (not yet exhausted — MAX_ATTEMPTS_PER_WINDOW is 3).
        for backoff in BACKOFF_SCHEDULE_SECONDS[:2]:
            await reconciler.tick()
            mono["t"] += backoff + 0.001

        # Remove the pair from membership entirely, then re-add it — the
        # store drops/re-creates the gap window; seed it back to the exact
        # same start_open_time.
        store.prune_reconciliation(set())
        assert store.get_reconciliation_targets() == []
        _make_gap(store, "BTC", "1m", BASE_MS, skip_bars=1, base_now_ms=BASE_MS)
        targets = store.get_reconciliation_targets()
        assert len(targets) == 1
        assert targets[0].start_open_time == BASE_MS  # same open_time as before removal

        # If stale retry state were reused, the very next tick would look
        # like it's still backing off from the pre-removal attempts (or
        # would exhaust on its 3rd attempt right away). Instead it must
        # attempt a genuinely fresh fetch immediately.
        outcome = await reconciler.tick()
        assert outcome.fetches_attempted == 1
        assert reconciler.counters.windows_exhausted == 0

    async def test_persist_retry_incarnation_reset_on_pair_removal_and_readd(self, tmp_path, monkeypatch):
        """Remaining-scope item 2: local persist-only retry backoff/attempt
        bookkeeping must be keyed by the pair's current incarnation, not
        just (symbol, timeframe, open_time) — otherwise removing and
        re-adding a pair between ticks, landing a fresh failed persistence
        record at a coincidentally-matching open_time, would inherit the
        OLD incarnation's accumulated attempt count and could exhaust after
        only one new failure.
        """
        import apex.data.candle_store as candle_store_module

        store = _store(tmp_path)
        store.set_reconciliation_membership({("BTC", "1m")}, replace=False)
        cutoff_ms = BASE_MS + 10 * ONE_MIN_MS
        client = AsyncMock()
        mono = {"t": 0.0}
        reconciler = _reconciler(store, client, wall_clock_ms=lambda: cutoff_ms, clock=lambda: mono["t"])

        def _broken_upsert(conn, candle):
            raise RuntimeError("disk full")

        monkeypatch.setattr(candle_store_module.repo, "upsert_candle", _broken_upsert)

        store.update(
            "BTC", "1m", _rest_row(BASE_MS, c="111.0"),
            persist=True, now_ms=cutoff_ms, source="ws",
        )
        assert store.get_persist_retry_candidates() == [("BTC", "1m", BASE_MS)]

        # Two failed local retry-drain attempts (not yet exhausted —
        # MAX_ATTEMPTS_PER_WINDOW is 3).
        for backoff in BACKOFF_SCHEDULE_SECONDS[:2]:
            mono["t"] += backoff + 0.001
            await reconciler._drain_pending_persist_retries(monotonic_now=mono["t"])
        assert reconciler.counters.rows_persist_retry_exhausted == 0
        old_key = ("BTC", "1m", BASE_MS, store.get_reconciliation_incarnation("BTC", "1m"))
        assert reconciler._persist_retry_attempts.get(old_key) == 2

        # Remove the pair from reconciliation membership entirely (clears
        # the store's own persist_pending bookkeeping and bumps its
        # incarnation on re-add), then land a FRESH failed persist at the
        # exact same open_time.
        store.prune_reconciliation(set())
        assert store.get_persist_retry_candidates() == []
        store.set_reconciliation_membership({("BTC", "1m")}, replace=True)
        new_incarnation = store.get_reconciliation_incarnation("BTC", "1m")
        assert new_incarnation != 0  # freshly (re)joined -> a new nonzero incarnation

        store.update(
            "BTC", "1m", _rest_row(BASE_MS, c="222.0"),
            persist=True, now_ms=cutoff_ms + 1, source="ws",
        )
        assert store.get_persist_retry_candidates() == [("BTC", "1m", BASE_MS)]

        mono["t"] += 1000.0
        await reconciler._drain_pending_persist_retries(monotonic_now=mono["t"])

        # A single fresh failure under the new incarnation must NOT be
        # treated as the old incarnation's 3rd attempt: it must still be
        # pending, never exhausted/dropped.
        assert reconciler.counters.rows_persist_retry_exhausted == 0
        assert store.get_persist_retry_candidates() == [("BTC", "1m", BASE_MS)]
        new_key = ("BTC", "1m", BASE_MS, new_incarnation)
        assert reconciler._persist_retry_attempts.get(new_key) == 1

    async def test_over_120_pair_membership_churn_keeps_retry_state_bounded(self, tmp_path):
        """Correction item 2: cycling reconciliation membership through
        well over MAX_RECONCILIATION_PAIRS (120) distinct pairs, one at a
        time, must never let CandleReconciler's own retry-state dict grow
        past what the store's current live targets actually justify.
        """
        store = _store(tmp_path)
        cutoff_ms = BASE_MS + 10 * ONE_MIN_MS
        client = AsyncMock()
        client.get_candle_snapshot = AsyncMock(return_value=None)
        reconciler = _reconciler(store, client, wall_clock_ms=lambda: cutoff_ms)

        for i in range(150):
            sym = f"SYM{i}"
            store.prune_reconciliation({(sym, "1m")})
            _make_gap(store, sym, "1m", BASE_MS, skip_bars=1, base_now_ms=BASE_MS)
            await reconciler.tick()
            # Retry state must never exceed the store's own current live
            # target count (at most 1 pair is ever in membership here).
            assert len(reconciler._retry_state) <= len(store.get_reconciliation_targets())


# =================================================================
# Fairness, single-flight, fetch bounding
# =================================================================

class TestSingleFlightAndFetchBounding:
    async def test_overlapping_manual_ticks_are_refused(self, tmp_path):
        store = _store(tmp_path)
        _make_gap(store, "BTC", "1m", BASE_MS, skip_bars=1, base_now_ms=BASE_MS)
        client = AsyncMock()

        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_fetch(*a, **kw):
            started.set()
            await release.wait()
            return []

        client.get_candle_snapshot = AsyncMock(side_effect=slow_fetch)
        reconciler = _reconciler(store, client, wall_clock_ms=lambda: BASE_MS + 10 * ONE_MIN_MS)

        task = asyncio.create_task(reconciler.tick())
        await started.wait()
        overlapping = await reconciler.tick()
        assert overlapping.skipped is True
        assert overlapping.reason == "in_progress"

        release.set()
        await task
        assert reconciler.counters.ticks_skipped_overlap == 1

    async def test_at_most_max_fetches_per_tick(self, tmp_path):
        store = _store(tmp_path)
        for sym in ["AAA", "BBB", "CCC"]:
            _make_gap(store, sym, "1m", BASE_MS, skip_bars=1, base_now_ms=BASE_MS)
        cutoff_ms = BASE_MS + 10 * ONE_MIN_MS
        client = AsyncMock()
        client.get_candle_snapshot = AsyncMock(return_value=[])
        reconciler = _reconciler(
            store, client, wall_clock_ms=lambda: cutoff_ms,
            config=ReconcilerConfig(max_fetches_per_tick=2),
        )

        outcome = await reconciler.tick()

        assert outcome.fetches_attempted == 2
        assert client.get_candle_snapshot.await_count == 2

    async def test_fair_round_robin_avoids_starvation_across_ticks(self, tmp_path):
        store = _store(tmp_path)
        for sym in ["AAA", "BBB", "CCC", "DDD"]:
            _make_gap(store, sym, "1m", BASE_MS, skip_bars=1, base_now_ms=BASE_MS)
        cutoff_ms = BASE_MS + 10 * ONE_MIN_MS
        client = AsyncMock()
        client.get_candle_snapshot = AsyncMock(return_value=[])
        reconciler = _reconciler(
            store, client, wall_clock_ms=lambda: cutoff_ms,
            config=ReconcilerConfig(max_fetches_per_tick=2),
        )

        seen_symbols: list[str] = []
        for _ in range(2):
            await reconciler.tick()
            seen_symbols.extend(
                c.args[0] for c in client.get_candle_snapshot.await_args_list
            )
            client.get_candle_snapshot.reset_mock()

        # Every pair got a turn across the two ticks — no starvation of the
        # pairs excluded from the first tick's 2-fetch budget.
        assert len(set(seen_symbols)) == 4

    async def test_sustained_six_pair_backlog_gives_every_pair_a_turn_within_its_fair_share_of_ticks(
        self, tmp_path
    ):
        """Remaining-scope item 3 (fairness evidence gap): a backlog wider
        than a single tick's fetch budget, sustained/regrown across many
        ticks (every pair keeps producing empty responses, so every window
        stays open the whole time — never resolved, never dropped), must
        still give every pair a turn within a bounded number of ticks
        proportional to backlog size / max_fetches_per_tick — this tests
        the fairness OUTCOME (no pair waits indefinitely), not the exact
        cursor mechanics.
        """
        store = _store(tmp_path)
        symbols = [f"SYM{i}" for i in range(6)]
        for sym in symbols:
            _make_gap(store, sym, "1m", BASE_MS, skip_bars=1, base_now_ms=BASE_MS)
        cutoff_ms = BASE_MS + 10 * ONE_MIN_MS
        client = AsyncMock()
        client.get_candle_snapshot = AsyncMock(return_value=[])  # never resolves -> window stays open
        reconciler = _reconciler(
            store, client, wall_clock_ms=lambda: cutoff_ms,
            config=ReconcilerConfig(max_fetches_per_tick=2),
        )

        seen_symbols: set[str] = set()
        max_ticks = len(symbols)  # generous bound: 6 pairs / 2-per-tick = 3 ticks minimum
        for _ in range(max_ticks):
            await reconciler.tick()
            seen_symbols.update(c.args[0] for c in client.get_candle_snapshot.await_args_list)
            client.get_candle_snapshot.reset_mock()
            if seen_symbols == set(symbols):
                break

        assert seen_symbols == set(symbols)  # nobody starved across the sustained backlog

    async def test_growing_backlog_mid_sweep_does_not_starve_pairs_already_due_a_turn(self, tmp_path):
        """A backlog that keeps growing (new pairs added mid-sweep) must
        never indefinitely defer a pair that was already due its turn —
        fairness is evaluated on outcome (every pre-existing pair still
        gets serviced), not on the cursor's internal bookkeeping.
        """
        store = _store(tmp_path)
        initial = [f"OLD{i}" for i in range(4)]
        for sym in initial:
            _make_gap(store, sym, "1m", BASE_MS, skip_bars=1, base_now_ms=BASE_MS)
        cutoff_ms = BASE_MS + 10 * ONE_MIN_MS
        client = AsyncMock()
        client.get_candle_snapshot = AsyncMock(return_value=[])
        reconciler = _reconciler(
            store, client, wall_clock_ms=lambda: cutoff_ms,
            config=ReconcilerConfig(max_fetches_per_tick=2),
        )

        seen_symbols: set[str] = set()
        for tick_no in range(4):
            if tick_no == 1:
                # A fresh pair joins mid-sweep, growing the backlog.
                _make_gap(store, "NEW0", "1m", BASE_MS, skip_bars=1, base_now_ms=BASE_MS)
            await reconciler.tick()
            seen_symbols.update(c.args[0] for c in client.get_candle_snapshot.await_args_list)
            client.get_candle_snapshot.reset_mock()

        assert set(initial) <= seen_symbols  # every pre-existing pair still got a turn

    async def test_successful_resolutions_free_slots_and_repeated_rollovers_still_bound_service(self, tmp_path):
        """Final-gaps pass item 2: unlike the empty-response fixtures above,
        every fetch here actually resolves and removes its pair's window —
        proving fairness holds when the backlog is genuinely shrinking, not
        only when it is artificially held open forever. The same 6-pair
        backlog is then rolled over twice more (fresh gap windows reseeded
        after a full resolve), and each round must independently rebound
        within its fair-share tick count.
        """
        store = _store(tmp_path)
        symbols = [f"SYM{i}" for i in range(6)]
        for sym in symbols:
            _make_gap(store, sym, "1m", BASE_MS, skip_bars=1, base_now_ms=BASE_MS)
        cutoff = {"t": BASE_MS + 10 * ONE_MIN_MS}
        client = AsyncMock()

        async def fetch(symbol, timeframe, start_ms, end_ms, **kw):
            # Resolve every on-grid open in the ACTUAL requested range,
            # clamped to this pair's CURRENT live gap-window [start, end]
            # and to <=60 bars (a genuine upstream response never exceeds
            # max_bars_per_request) — not just one row. A real store's
            # window can span more than a single skipped bar once it
            # preserves a prior round's WS-max marker across a reseed (see
            # CandleStore._reconciliation_ws_observe), so a single-row reply
            # would silently under-resolve the window and stall the
            # 6-pair/3-tick full-drain guarantee this test asserts.
            live = {t.symbol: t for t in store.get_reconciliation_targets()}
            target = live.get(symbol)
            if target is None:
                return []
            duration = ONE_MIN_MS
            range_start = max(target.start_open_time, start_ms)
            offset = (range_start - target.start_open_time) % duration
            if offset:
                range_start += duration - offset
            range_end = min(target.end_open_time, end_ms)
            rows = []
            open_time = range_start
            while open_time <= range_end and len(rows) < 60:
                rows.append(_rest_row(open_time, c="1.0", symbol=symbol))
                open_time += duration
            return rows

        client.get_candle_snapshot = AsyncMock(side_effect=fetch)
        reconciler = _reconciler(
            store, client, wall_clock_ms=lambda: cutoff["t"],
            config=ReconcilerConfig(max_fetches_per_tick=2),
        )

        for round_no in range(3):  # initial sweep + two repeated rollovers
            serviced: set[str] = set()
            for _ in range(3):  # 6 pairs / 2-per-tick = 3 ticks: the bounded service guarantee
                outcome = await reconciler.tick()
                assert outcome.fetches_attempted <= 2  # concrete per-tick limit, never exceeded
                serviced.update(c.args[0] for c in client.get_candle_snapshot.await_args_list)
                client.get_candle_snapshot.reset_mock()

            assert serviced == set(symbols)  # every pair actually serviced and resolved this round
            assert store.get_reconciliation_targets() == []  # windows genuinely gone, not merely skipped

            if round_no < 2:
                base_open = BASE_MS + (round_no + 1) * 20 * ONE_MIN_MS
                for sym in symbols:
                    _make_gap(store, sym, "1m", base_open, skip_bars=1, base_now_ms=base_open)
                cutoff["t"] = base_open + 10 * ONE_MIN_MS

    async def test_repeated_arrivals_mid_sweep_do_not_starve_still_pending_original_candidates(self, tmp_path):
        """Remaining-scope item 3: unlike the fixture above (which only
        reseeds after all six pairs have fully drained), this reintroduces a
        genuinely fresh gap for an already-served pair immediately after
        EACH of the first two ticks — a repeated arrival DURING a still-
        unfinished sweep, while other pairs from the same original backlog
        remain pending and unserviced. One of those still-pending originals
        also gets a real, injected-clock backoff (its first fetch
        genuinely fails) and must be serviced again, and actually resolve,
        once that backoff expires. Uses the real store's own gap-window/
        WS-max tracking (a repeat gap's start_open_time legitimately
        preserves the pair's prior WS-max marker, exactly like the fixture
        above) and a canonical, request-range-driven mock response.

        This is ONE concrete, hand-traced scenario demonstrating the
        outcome holds here, not a claim that the round-robin cursor is
        adversarially starvation-proof for every possible interleaving.
        """
        store = _store(tmp_path)
        symbols = [f"SYM{i}" for i in range(6)]
        for sym in symbols:
            _make_gap(store, sym, "1m", BASE_MS, skip_bars=1, base_now_ms=BASE_MS)

        cutoff = {"t": BASE_MS + 10 * ONE_MIN_MS}
        mono = {"t": 0.0}
        attempt_counts: dict[str, int] = {}
        fails_once = {"SYM3"}  # this still-pending original's first fetch genuinely fails

        async def fetch(symbol, timeframe, start_ms, end_ms, **kw):
            attempt_counts[symbol] = attempt_counts.get(symbol, 0) + 1
            if symbol in fails_once and attempt_counts[symbol] == 1:
                return None  # transient failure -> a real backoff is scheduled
            # Canonical response spanning the ACTUAL requested range, clamped
            # to this pair's current live window and <=60 bars (see the
            # fixture above for why a single fixed row is not sufficient).
            live = {t.symbol: t for t in store.get_reconciliation_targets()}
            target = live.get(symbol)
            if target is None:
                return []
            duration = ONE_MIN_MS
            range_start = max(target.start_open_time, start_ms)
            offset = (range_start - target.start_open_time) % duration
            if offset:
                range_start += duration - offset
            range_end = min(target.end_open_time, end_ms)
            rows = []
            open_time = range_start
            while open_time <= range_end and len(rows) < 60:
                rows.append(_rest_row(open_time, c="1.0", symbol=symbol))
                open_time += duration
            return rows

        client = AsyncMock()
        client.get_candle_snapshot = AsyncMock(side_effect=fetch)
        reconciler = _reconciler(
            store, client, wall_clock_ms=lambda: cutoff["t"], clock=lambda: mono["t"],
            config=ReconcilerConfig(max_fetches_per_tick=2),
        )

        first_service: dict[str, int] = {}

        async def run_tick(tick_no: int) -> None:
            outcome = await reconciler.tick()
            assert outcome.fetches_attempted <= 2  # concrete per-tick cap, never exceeded
            for call in client.get_candle_snapshot.await_args_list:
                first_service.setdefault(call.args[0], tick_no)
            client.get_candle_snapshot.reset_mock()

        await run_tick(1)  # serves SYM0, SYM1 -- both resolve
        assert store.is_candle_closed("SYM0", "1m", BASE_MS) is True
        assert store.is_candle_closed("SYM1", "1m", BASE_MS) is True

        # A repeated arrival: SYM0 gets a genuinely fresh gap immediately,
        # while SYM2..SYM5 are still an unfinished, pending sweep.
        reseed_base_0 = BASE_MS + 20 * ONE_MIN_MS
        cutoff["t"] = reseed_base_0 + 10 * ONE_MIN_MS
        _make_gap(store, "SYM0", "1m", reseed_base_0, skip_bars=1, base_now_ms=reseed_base_0)

        await run_tick(2)  # serves SYM4, SYM5 -- both resolve; SYM2/SYM3 still pending
        assert store.is_candle_closed("SYM4", "1m", BASE_MS) is True
        assert store.is_candle_closed("SYM5", "1m", BASE_MS) is True

        reseed_base_4 = BASE_MS + 40 * ONE_MIN_MS
        cutoff["t"] = reseed_base_4 + 10 * ONE_MIN_MS
        _make_gap(store, "SYM4", "1m", reseed_base_4, skip_bars=1, base_now_ms=reseed_base_4)

        await run_tick(3)  # finally reaches the two still-pending originals
        assert store.is_candle_closed("SYM2", "1m", BASE_MS) is True
        assert store.is_candle_closed("SYM3", "1m", BASE_MS) is False  # genuinely failed; backoff scheduled
        rs3 = reconciler._retry_state[("SYM3", "1m")]
        assert rs3.attempts == 1
        assert rs3.next_retry_monotonic == pytest.approx(BACKOFF_SCHEDULE_SECONDS[0])

        # Every one of the original six candidates was serviced (attempted)
        # within 3 ticks -- the same fair-share bound as the sustained/
        # growing-backlog fixtures above (6 pairs / 2-per-tick), even though
        # two already-served pairs (SYM0, SYM4) were reintroduced mid-sweep.
        assert {sym: first_service[sym] for sym in symbols} == {
            "SYM0": 1, "SYM1": 1, "SYM4": 2, "SYM5": 2, "SYM2": 3, "SYM3": 3,
        }

        # Advance past SYM3's scheduled backoff and confirm it is serviced
        # again and actually resolves once the backoff genuinely expires.
        mono["t"] = BACKOFF_SCHEDULE_SECONDS[0] + 0.5
        await run_tick(4)
        assert store.is_candle_closed("SYM3", "1m", BASE_MS) is True
        assert store.is_candle_closed("SYM0", "1m", reseed_base_0) is True
        remaining = {t.symbol for t in store.get_reconciliation_targets()}
        assert "SYM3" not in remaining and "SYM0" not in remaining

    async def test_mixed_per_pair_backoff_still_bounds_service_of_non_backed_off_pairs(self, tmp_path):
        """Final-gaps pass item 2: two of six pairs genuinely fail (real
        monotonic backoff, injected clock) while the other four succeed and
        resolve — the concrete guarantee under test is that every pair NOT
        currently backed off is serviced within its fair share of ticks
        (never starved behind a backed-off pair), and once the backoff
        window genuinely elapses the remaining pairs are serviced too, all
        while per-tick fetches never exceed max_fetches_per_tick.
        """
        store = _store(tmp_path)
        symbols = [f"SYM{i}" for i in range(6)]  # SYM0/SYM1 fail; SYM2..SYM5 resolve
        for sym in symbols:
            _make_gap(store, sym, "1m", BASE_MS, skip_bars=1, base_now_ms=BASE_MS)
        cutoff_ms = BASE_MS + 10 * ONE_MIN_MS
        mono = {"t": 0.0}
        client = AsyncMock()

        async def fetch(symbol, timeframe, start_ms, end_ms, **kw):
            if symbol in ("SYM0", "SYM1") and mono["t"] < BACKOFF_SCHEDULE_SECONDS[0]:
                return None  # genuinely fails until backoff has elapsed
            live = {t.symbol: t for t in store.get_reconciliation_targets()}
            target = live.get(symbol)
            if target is None:
                return []
            return [_rest_row(target.start_open_time, c="1.0", symbol=symbol)]

        client.get_candle_snapshot = AsyncMock(side_effect=fetch)
        reconciler = _reconciler(
            store, client, wall_clock_ms=lambda: cutoff_ms, clock=lambda: mono["t"],
            config=ReconcilerConfig(max_fetches_per_tick=2),
        )

        all_seen: set[str] = set()
        for _ in range(3):  # SYM0/SYM1 (fail+backoff) then SYM2..SYM5 (resolve), 2-per-tick
            outcome = await reconciler.tick()
            assert outcome.fetches_attempted <= 2
            all_seen.update(c.args[0] for c in client.get_candle_snapshot.await_args_list)
            client.get_candle_snapshot.reset_mock()

        assert {"SYM2", "SYM3", "SYM4", "SYM5"} <= all_seen  # every non-failing pair serviced
        assert all(store.is_candle_closed(sym, "1m", BASE_MS) for sym in ("SYM2", "SYM3", "SYM4", "SYM5"))
        remaining = {t.symbol for t in store.get_reconciliation_targets()}
        assert remaining == {"SYM0", "SYM1"}  # only the genuinely backed-off pairs remain

        # While still within the backoff window, a further tick attempts
        # nothing for the two backed-off pairs (the only pairs left) —
        # concrete bound: zero wasted fetches, not merely "eventually".
        outcome = await reconciler.tick()
        assert outcome.fetches_attempted == 0
        client.get_candle_snapshot.assert_not_awaited()

        # Advance the injected monotonic clock past the first backoff step.
        mono["t"] += BACKOFF_SCHEDULE_SECONDS[0] + 0.001
        outcome = await reconciler.tick()
        assert outcome.fetches_attempted == 2  # both now-eligible pairs serviced together
        seen_after_backoff = {c.args[0] for c in client.get_candle_snapshot.await_args_list}
        assert seen_after_backoff == {"SYM0", "SYM1"}
        assert store.get_reconciliation_targets() == []  # fully drained


class TestExhaustionAndBackoff:
    async def test_three_failed_attempts_drop_window_with_monotonic_backoff(self, tmp_path):
        store = _store(tmp_path)
        _make_gap(store, "BTC", "1m", BASE_MS, skip_bars=1, base_now_ms=BASE_MS)
        cutoff_ms = BASE_MS + 10 * ONE_MIN_MS
        client = AsyncMock()
        client.get_candle_snapshot = AsyncMock(return_value=None)  # every fetch fails
        mono = {"t": 0.0}
        reconciler = _reconciler(
            store, client, wall_clock_ms=lambda: cutoff_ms, clock=lambda: mono["t"],
        )

        for expected_backoff in BACKOFF_SCHEDULE_SECONDS[:MAX_ATTEMPTS_PER_WINDOW]:
            await reconciler.tick()
            # Not yet due for retry: an immediate second tick does nothing.
            second = await reconciler.tick()
            assert second.fetches_attempted == 0
            mono["t"] += expected_backoff + 0.001

        assert reconciler.counters.windows_exhausted == 1
        assert store.get_reconciliation_targets() == []
        assert store.reconciliation_exhausted_count == 1

    async def test_late_ws_resolves_oldest_bar_during_backoff_preserves_attempt_count(self, tmp_path):
        """Correction item 3: a late WS observation resolving exactly the
        window's oldest bar during an active backoff must shrink
        start_open_time without resetting the frozen attempted prefix's
        accumulated attempts/backoff — only a start moving PAST the frozen
        end is a genuine completion that starts fresh.
        """
        store = _store(tmp_path)
        _make_gap(store, "BTC", "1m", BASE_MS, skip_bars=5, base_now_ms=BASE_MS)
        cutoff_ms = BASE_MS + 20 * ONE_MIN_MS
        client = AsyncMock()
        client.get_candle_snapshot = AsyncMock(return_value=None)  # every fetch fails
        mono = {"t": 0.0}
        reconciler = _reconciler(store, client, wall_clock_ms=lambda: cutoff_ms, clock=lambda: mono["t"])

        await reconciler.tick()  # 1st failed attempt, freezes attempted_end_open_time
        key = ("BTC", "1m")
        rs = reconciler._retry_state[key]
        assert rs.attempts == 1
        frozen_end = rs.attempted_end_open_time

        mono["t"] += BACKOFF_SCHEDULE_SECONDS[0] + 0.001
        # A late WS delivery resolves exactly the window's oldest (current
        # start) bar, shrinking start_open_time by one duration but staying
        # within the frozen attempted prefix.
        store.update(
            "BTC", "1m", _rest_row(BASE_MS, c="1.0"),
            persist=True, now_ms=cutoff_ms, source="ws",
        )
        new_start = store.get_reconciliation_targets()[0].start_open_time
        assert new_start == BASE_MS + ONE_MIN_MS
        assert new_start <= frozen_end  # still within the previously frozen prefix

        await reconciler.tick()  # 2nd failed attempt — must NOT be treated as a fresh 1st
        rs = reconciler._retry_state[key]
        assert rs.attempts == 2  # preserved/incremented, not reset to 1
        assert rs.attempted_end_open_time == frozen_end  # frozen end unchanged

        mono["t"] += BACKOFF_SCHEDULE_SECONDS[1] + 0.001
        await reconciler.tick()  # 3rd failed attempt -> exhausted
        assert reconciler.counters.windows_exhausted == 1

    async def test_wide_window_exhaustion_drops_only_attempted_prefix(self, tmp_path):
        """Correction B / confirmed F2: a window wider than
        max_bars_per_request (60) must, on exhaustion, drop only the
        actually-attempted <=60-bar prefix — bars beyond it were never
        fetched even once and must remain targetable.
        """
        store = _store(tmp_path)
        # 180-bar gap: first_open .. first_open + 179*ONE_MIN_MS.
        _make_gap(store, "BTC", "1m", BASE_MS, skip_bars=180, base_now_ms=BASE_MS)
        targets = store.get_reconciliation_targets()
        span = (targets[0].end_open_time - targets[0].start_open_time) // ONE_MIN_MS + 1
        assert span == 180

        cutoff_ms = BASE_MS + 300 * ONE_MIN_MS
        client = AsyncMock()
        client.get_candle_snapshot = AsyncMock(return_value=None)  # every fetch fails
        mono = {"t": 0.0}
        reconciler = _reconciler(store, client, wall_clock_ms=lambda: cutoff_ms, clock=lambda: mono["t"])

        for backoff in BACKOFF_SCHEDULE_SECONDS[:MAX_ATTEMPTS_PER_WINDOW]:
            await reconciler.tick()
            mono["t"] += backoff + 0.001

        assert reconciler.counters.windows_exhausted == 1
        # Only the first 60 attempted bars were dropped; the remaining 121
        # bars (indices 60..180) are still an open target for a future
        # attempt, not silently abandoned.
        remaining = store.get_reconciliation_targets()
        assert len(remaining) == 1
        assert remaining[0].start_open_time == BASE_MS + 60 * ONE_MIN_MS
        assert remaining[0].end_open_time == BASE_MS + 179 * ONE_MIN_MS

        # That remaining prefix is now genuinely fetchable (fresh window).
        client.get_candle_snapshot = AsyncMock(return_value=[])
        outcome = await reconciler.tick()
        assert outcome.fetches_attempted == 1
        called_args = client.get_candle_snapshot.await_args.args
        assert called_args[2] == BASE_MS + 60 * ONE_MIN_MS - 1  # padded_start

    async def test_growing_rollover_during_backoff_does_not_expand_frozen_request_range(self, tmp_path):
        """A WS rollover extending the store's window end mid-backoff must
        not expand the already-in-progress fixed request/drop range for a
        window already being retried.
        """
        store = _store(tmp_path)
        _make_gap(store, "BTC", "1m", BASE_MS, skip_bars=3, base_now_ms=BASE_MS)
        cutoff_ms = BASE_MS + 300 * ONE_MIN_MS
        client = AsyncMock()
        client.get_candle_snapshot = AsyncMock(return_value=None)
        mono = {"t": 0.0}
        reconciler = _reconciler(store, client, wall_clock_ms=lambda: cutoff_ms, clock=lambda: mono["t"])

        await reconciler.tick()  # 1st failed attempt freezes attempted_end_open_time
        rs = reconciler._retry_state[("BTC", "1m")]
        frozen_end = rs.attempted_end_open_time
        assert frozen_end == BASE_MS + 2 * ONE_MIN_MS  # skip_bars=3 -> 3 candidates, end = start + 2*dur

        # Rollover extends the window end further while still backing off.
        store.update(
            "BTC", "1m", _forming_ws_candle(BASE_MS + 50 * ONE_MIN_MS),
            persist=False, now_ms=BASE_MS + 50 * ONE_MIN_MS, source="ws",
        )
        mono["t"] += BACKOFF_SCHEDULE_SECONDS[0] + 0.001
        await reconciler.tick()

        # Same reconciler instance, same window_start -> retry state reused,
        # frozen end must be unchanged despite the much larger window now on
        # the store.
        rs_after = reconciler._retry_state[("BTC", "1m")]
        assert rs_after.attempted_end_open_time == frozen_end
        called_args = client.get_candle_snapshot.await_args.args
        assert called_args[3] < BASE_MS + 50 * ONE_MIN_MS  # request end never grew to the rollover

    async def test_fresh_window_after_readd_is_not_treated_as_the_exhausted_one(self, tmp_path):
        store = _store(tmp_path)
        _make_gap(store, "BTC", "1m", BASE_MS, skip_bars=1, base_now_ms=BASE_MS)
        cutoff_ms = BASE_MS + 10 * ONE_MIN_MS
        client = AsyncMock()
        client.get_candle_snapshot = AsyncMock(return_value=None)
        mono = {"t": 0.0}
        reconciler = _reconciler(store, client, wall_clock_ms=lambda: cutoff_ms, clock=lambda: mono["t"])

        for backoff in BACKOFF_SCHEDULE_SECONDS[:MAX_ATTEMPTS_PER_WINDOW]:
            await reconciler.tick()
            mono["t"] += backoff + 0.001
        assert store.get_reconciliation_targets() == []

        # A brand-new rollover creates a genuinely new window for the same
        # pair. A fresh CandleReconciler instance (its own empty retry-state
        # map) must still be able to attempt it — proving the drop above was
        # scoped to that specific exhausted window, not the pair forever.
        later_first = BASE_MS + 5 * ONE_MIN_MS
        _make_gap(store, "BTC", "1m", later_first, skip_bars=1, base_now_ms=later_first)
        assert store.get_reconciliation_targets() != []
        client.get_candle_snapshot = AsyncMock(return_value=[])
        cutoff_ms2 = later_first + 10 * ONE_MIN_MS
        reconciler2 = _reconciler(store, client, wall_clock_ms=lambda: cutoff_ms2, clock=lambda: mono["t"])

        outcome = await reconciler2.tick()

        assert outcome.fetches_attempted == 1
        client.get_candle_snapshot.assert_awaited_once()


# =================================================================
# Config validation
# =================================================================

class TestReconcilerConfigValidation:
    def test_defaults_are_valid(self):
        ReconcilerConfig()

    @pytest.mark.parametrize("field,value", [
        ("tick_interval_seconds", 0),
        ("tick_interval_seconds", -1),
        ("tick_interval_seconds", float("inf")),
        ("max_fetches_per_tick", 0),
        ("max_fetches_per_tick", 11),
        ("max_bars_per_request", 0),
        ("max_bars_per_request", 61),
        ("post_boundary_grace_ms", -1),
        ("post_boundary_grace_ms", 60_001),
    ])
    def test_invalid_values_rejected(self, field, value):
        with pytest.raises(ValueError):
            ReconcilerConfig(**{field: value})


# =================================================================
# Shutdown / cancellation
# =================================================================

class TestShutdown:
    async def test_stop_cancels_and_awaits_active_tick_no_write_after_stop(self, tmp_path):
        store = _store(tmp_path)
        _make_gap(store, "BTC", "1m", BASE_MS, skip_bars=1, base_now_ms=BASE_MS)
        cutoff_ms = BASE_MS + 10 * ONE_MIN_MS
        client = AsyncMock()

        started = asyncio.Event()

        async def hanging_fetch(*a, **kw):
            started.set()
            await asyncio.sleep(3600)  # never resolves on its own
            return [_rest_row(BASE_MS, c="1.0")]  # pragma: no cover

        client.get_candle_snapshot = AsyncMock(side_effect=hanging_fetch)
        reconciler = _reconciler(store, client, wall_clock_ms=lambda: cutoff_ms)

        task = asyncio.create_task(reconciler.tick())
        await started.wait()
        await reconciler.stop()  # cancels + awaits the child task internally

        # The child task is done and was actually cancelled (not merely
        # swallowed) — production cancellation propagation is preserved.
        assert task.done()
        assert task.cancelled()
        with pytest.raises(asyncio.CancelledError):
            await task  # re-awaiting an already-cancelled task raises again

        # No row was ever written — the hanging fetch never returned.
        assert store.get_df("BTC", "1m", now_ms=cutoff_ms) is None

    async def test_stop_is_idempotent(self, tmp_path):
        store = _store(tmp_path)
        client = AsyncMock()
        reconciler = _reconciler(store, client, wall_clock_ms=lambda: BASE_MS)
        await reconciler.stop()
        await reconciler.stop()  # must not raise

    async def test_tick_after_stop_is_a_noop(self, tmp_path):
        store = _store(tmp_path)
        _make_gap(store, "BTC", "1m", BASE_MS, skip_bars=1, base_now_ms=BASE_MS)
        client = AsyncMock()
        client.get_candle_snapshot = AsyncMock(return_value=[_rest_row(BASE_MS, c="1.0")])
        reconciler = _reconciler(store, client, wall_clock_ms=lambda: BASE_MS + 10 * ONE_MIN_MS)

        await reconciler.stop()
        outcome = await reconciler.tick()

        assert outcome.skipped is True
        assert outcome.reason == "stopped"
        client.get_candle_snapshot.assert_not_awaited()


# =================================================================
# Lifecycle token gating
# =================================================================

class TestLifecycleTokenGating:
    async def test_no_fetch_when_token_provider_returns_none(self, tmp_path):
        store = _store(tmp_path)
        _make_gap(store, "BTC", "1m", BASE_MS, skip_bars=1, base_now_ms=BASE_MS)
        client = AsyncMock()
        reconciler = _reconciler(
            store, client, wall_clock_ms=lambda: BASE_MS + 10 * ONE_MIN_MS,
            token_provider=lambda symbol: None,
        )

        outcome = await reconciler.tick()

        assert outcome.fetches_attempted == 0
        client.get_candle_snapshot.assert_not_awaited()
        assert reconciler.counters.fetches_deferred_lifecycle == 1

    async def test_response_discarded_when_token_changes_during_fetch(self, tmp_path):
        store = _store(tmp_path)
        _make_gap(store, "BTC", "1m", BASE_MS, skip_bars=1, base_now_ms=BASE_MS)
        cutoff_ms = BASE_MS + 10 * ONE_MIN_MS
        client = AsyncMock()

        token_state = {"epoch": 1}

        async def fetch_and_change_token(*a, **kw):
            token_state["epoch"] = 2  # simulate a reconnect completing mid-fetch
            return [_rest_row(BASE_MS, c="111.0")]

        client.get_candle_snapshot = AsyncMock(side_effect=fetch_and_change_token)
        reconciler = _reconciler(
            store, client, wall_clock_ms=lambda: cutoff_ms,
            token_provider=lambda symbol: token_state["epoch"],
        )

        await reconciler.tick()

        assert store.get_df("BTC", "1m", now_ms=cutoff_ms) is None  # response discarded
        assert reconciler.counters.fetches_deferred_lifecycle == 1
