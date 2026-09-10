"""Tests for the shared REST rate limiter and reconciliation-aware request
path added to HyperliquidClient.

Covers: rolling-window admission/refusal (total + reconciliation sub-budget),
reservations not refunded on error, bounded single-wait-then-retry behavior,
429 cooldown (bounded Retry-After, conservative default, no retry storm),
oversized-response extra debt/cooldown, exactly-one-HTTP-attempt for a
reconciliation call (RestBudgetUnavailable when the budget can't admit it),
existing (legacy) call shapes/positional signatures preserved, cancellation
propagation, and bounded (RATE_WINDOW_SECONDS) own-budget wait-then-admit for
ordinary calls so a temporarily-full budget that frees in time is never
misreported as a failed upstream request. No real sockets/sleeps —
httpx.AsyncClient and time are fully faked/injected.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from apex.data import hyperliquid_client as hl_module
from apex.data.hyperliquid_client import (
    HyperliquidClient,
    RestBudgetUnavailable,
    RestRateLimiter,
    estimate_candle_snapshot_weight,
)


def _response(status_code=200, json_data=None, headers=None):
    request = httpx.Request("POST", "https://example.invalid/info")
    return httpx.Response(status_code, json=json_data, headers=headers or {}, request=request)


def _scripted_async_client(results):
    """Factory standing in for httpx.AsyncClient: each construction consumes
    the next scripted result (an httpx.Response to return, or an Exception
    to raise) from `results`, in order.
    """
    state = {"i": 0}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            item = results[state["i"]]
            state["i"] += 1
            if isinstance(item, BaseException):
                raise item
            return item

    return _Client


class _FakeClock:
    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _RecordingSleep:
    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


# =================================================================
# RestRateLimiter
# =================================================================

class TestRestRateLimiterAdmission:
    async def test_admits_within_total_limit(self):
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=100, reconciliation_per_minute=50, clock=clock)
        assert await limiter.acquire(40) is True
        assert await limiter.acquire(40) is True

    async def test_refuses_when_total_limit_would_be_exceeded(self):
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=100, reconciliation_per_minute=50, clock=clock)
        assert await limiter.acquire(60) is True
        assert await limiter.acquire(60) is False  # would push total to 120 > 100

    async def test_reconciliation_sub_budget_enforced_even_with_total_room(self):
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=1000, reconciliation_per_minute=50, clock=clock)
        assert await limiter.acquire(40, reconciliation=True) is True
        # Plenty of total room left, but the reconciliation sub-ceiling is hit.
        assert await limiter.acquire(20, reconciliation=True) is False

    async def test_non_reconciliation_calls_do_not_count_against_reconciliation_subbudget(self):
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=1000, reconciliation_per_minute=50, clock=clock)
        assert await limiter.acquire(500, reconciliation=False) is True
        # Ordinary traffic never touches the reconciliation sub-ceiling.
        assert await limiter.acquire(50, reconciliation=True) is True

    async def test_rolling_window_frees_capacity_after_60_seconds(self):
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=100, reconciliation_per_minute=100, clock=clock)
        assert await limiter.acquire(90) is True
        assert await limiter.acquire(20) is False
        clock.advance(60.1)
        assert await limiter.acquire(20) is True

    async def test_no_fixed_minute_boundary_burst(self):
        """Admission is judged by a rolling 60s window from *now*, not a
        fixed wall-clock minute boundary — advancing by a few seconds must
        not suddenly free the full budget.
        """
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=100, reconciliation_per_minute=100, clock=clock)
        assert await limiter.acquire(90) is True
        clock.advance(5.0)
        assert await limiter.acquire(20) is False  # still well within the same rolling window

    async def test_reservations_not_refunded_for_errors(self):
        """The limiter itself has no notion of "error" — a caller must
        never refund/undo a reservation just because the subsequent HTTP
        call failed (see HyperliquidClient._post, which never does this).
        """
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=100, reconciliation_per_minute=100, clock=clock)
        assert await limiter.acquire(90) is True
        # Nothing refunds the 90 already reserved.
        assert await limiter.acquire(20) is False


class TestRestRateLimiterWaiting:
    async def test_wait_false_never_sleeps(self):
        clock = _FakeClock()
        sleep = _RecordingSleep()
        limiter = RestRateLimiter(total_per_minute=10, reconciliation_per_minute=10, clock=clock, sleep=sleep)
        await limiter.acquire(10)
        assert await limiter.acquire(10, wait=False) is False
        assert sleep.calls == []

    async def test_wait_true_sleeps_once_then_retries_bounded(self):
        clock = _FakeClock()
        sleep = _RecordingSleep()
        limiter = RestRateLimiter(total_per_minute=10, reconciliation_per_minute=10, clock=clock, sleep=sleep)
        await limiter.acquire(10)

        async def advancing_sleep(seconds: float) -> None:
            sleep.calls.append(seconds)
            clock.advance(seconds)

        limiter._sleep = advancing_sleep
        admitted = await limiter.acquire(10, wait=True, max_wait_seconds=100.0)
        assert admitted is True
        assert len(sleep.calls) == 1  # exactly one wait, not a retry loop

    async def test_wait_bounded_by_max_wait_seconds_and_can_still_fail(self):
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=10, reconciliation_per_minute=10, clock=clock, sleep=_RecordingSleep())
        await limiter.acquire(10)
        # Real wait-until-free is ~60s; capped to a tiny max_wait_seconds
        # that never actually frees capacity, so the single retry still fails.
        admitted = await limiter.acquire(10, wait=True, max_wait_seconds=0.001)
        assert admitted is False

    async def test_default_max_wait_seconds_is_the_full_rolling_window(self):
        """Regression for the production startup defect: the documented
        default wait bound is RATE_WINDOW_SECONDS, not an arbitrarily short
        cutoff — a caller relying on the default (as HyperliquidClient._post
        does) must still observe capacity that frees just before the
        window's edge, without having to pass an explicit override."""
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=10, reconciliation_per_minute=10, clock=clock)
        await limiter.acquire(10)  # fills the budget; wait_hint is ~60s

        async def advancing_sleep(seconds: float) -> None:
            clock.advance(seconds)

        limiter._sleep = advancing_sleep
        admitted = await limiter.acquire(10, wait=True)  # no max_wait_seconds override
        assert admitted is True

    async def test_cooldown_outlasting_documented_maximum_fails_closed_not_a_retry_storm(self):
        """A cooldown longer than the documented maximum wait
        (RATE_WINDOW_SECONDS) still correctly fails closed after exactly
        one bounded wait — an honest bounded refusal, not a retry loop."""
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=1000, reconciliation_per_minute=1000, clock=clock)
        await limiter.note_429(retry_after=200.0)  # far outlasts the 60s bound

        sleep_calls = []

        async def advancing_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            clock.advance(seconds)

        limiter._sleep = advancing_sleep
        admitted = await limiter.acquire(10, wait=True)
        assert admitted is False
        assert sleep_calls == [60.0]  # exactly one bounded wait, not a loop


class TestRestRateLimiter429Cooldown:
    async def test_429_cooldown_blocks_subsequent_acquire(self):
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=1000, reconciliation_per_minute=1000, clock=clock)
        await limiter.note_429(retry_after=30.0)
        assert await limiter.acquire(10) is False
        clock.advance(29.9)
        assert await limiter.acquire(10) is False
        clock.advance(0.2)
        assert await limiter.acquire(10) is True

    async def test_429_with_no_retry_after_uses_conservative_default(self):
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=1000, reconciliation_per_minute=1000, clock=clock)
        await limiter.note_429(retry_after=None)
        clock.advance(59.9)
        assert await limiter.acquire(10) is False
        clock.advance(0.2)
        assert await limiter.acquire(10) is True

    async def test_429_does_not_refund_or_clear_existing_reservations(self):
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=100, reconciliation_per_minute=100, clock=clock)
        await limiter.acquire(90)
        await limiter.note_429(retry_after=1.0)
        clock.advance(1.1)  # cooldown elapsed
        assert await limiter.acquire(20) is False  # the earlier 90 is still counted


class TestRestRateLimiterOversizedResponse:
    async def test_oversized_response_charges_extra_debt_and_cooldown(self):
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=1000, reconciliation_per_minute=1000, clock=clock)
        await limiter.acquire(22)
        await limiter.note_oversized_response(extra_weight=50)
        # Cooldown from the oversized-response guard refuses even an
        # otherwise-affordable request.
        assert await limiter.acquire(10) is False
        clock.advance(31.0)
        assert await limiter.acquire(10) is True
        # The extra debt was actually charged (reduces remaining headroom).
        assert await limiter.acquire(1000 - 22 - 50 - 10 + 1) is False


class TestEstimateCandleSnapshotWeight:
    def test_base_weight_for_small_row_count(self):
        assert estimate_candle_snapshot_weight(0) == 20
        assert estimate_candle_snapshot_weight(1) == 21

    def test_one_extra_unit_per_60_rows_rounded_up(self):
        assert estimate_candle_snapshot_weight(60) == 21
        assert estimate_candle_snapshot_weight(61) == 22
        assert estimate_candle_snapshot_weight(120) == 22

    def test_reconciliation_62_row_reservation_is_22(self):
        assert estimate_candle_snapshot_weight(62) == 22

    def test_open_ended_5000_row_worst_case_is_104(self):
        assert estimate_candle_snapshot_weight(5000) == 20 + 84


# =================================================================
# HyperliquidClient
# =================================================================

class TestLegacyCallShapesPreserved:
    async def test_get_perp_meta_still_works_positionally_constructed(self, monkeypatch):
        monkeypatch.setattr(
            hl_module.httpx, "AsyncClient",
            _scripted_async_client([_response(json_data={"universe": [{"name": "BTC"}]})]),
        )
        client = HyperliquidClient("https://example.invalid/info")
        result = await client.get_perp_meta()
        assert result == {"universe": [{"name": "BTC"}]}

    async def test_get_all_mids_still_works(self, monkeypatch):
        monkeypatch.setattr(
            hl_module.httpx, "AsyncClient",
            _scripted_async_client([_response(json_data={"BTC": "50000"})]),
        )
        client = HyperliquidClient()
        assert await client.get_all_mids() == {"BTC": "50000"}

    async def test_get_candle_snapshot_positional_args_unchanged(self, monkeypatch):
        monkeypatch.setattr(
            hl_module.httpx, "AsyncClient",
            _scripted_async_client([_response(json_data=[{"t": 1, "T": 2}])]),
        )
        client = HyperliquidClient()
        result = await client.get_candle_snapshot("BTC", "1m", 1000)
        assert result == [{"t": 1, "T": 2}]

    async def test_get_candle_snapshot_with_end_time_unchanged(self, monkeypatch):
        monkeypatch.setattr(
            hl_module.httpx, "AsyncClient",
            _scripted_async_client([_response(json_data=[])]),
        )
        client = HyperliquidClient()
        result = await client.get_candle_snapshot("BTC", "1m", 1000, 2000)
        assert result == []

    async def test_legacy_retry_and_final_failure_returns_none(self, monkeypatch):
        monkeypatch.setattr(hl_module.asyncio, "sleep", _RecordingSleep())
        monkeypatch.setattr(
            hl_module.httpx, "AsyncClient",
            _scripted_async_client([httpx.TransportError("boom")] * 3),
        )
        client = HyperliquidClient()
        result = await client.get_perp_meta()
        assert result is None


class TestSharedBudgetGatesEveryAttempt:
    async def test_every_retry_re_acquires_budget(self, monkeypatch):
        """A capped rate limiter that can admit only one call must cause the
        2nd/3rd legacy retry attempts to be refused (no HTTP call made) —
        proving every attempt, not just the first, is gated.
        """
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=20, reconciliation_per_minute=20, clock=clock, sleep=_RecordingSleep())
        monkeypatch.setattr(hl_module.asyncio, "sleep", _RecordingSleep())
        post_calls = {"n": 0}

        real_factory = _scripted_async_client([_response(status_code=500)] * 1)

        class _CountingClient(real_factory):
            async def post(self, url, json=None):
                post_calls["n"] += 1
                return await super().post(url, json=json)

        monkeypatch.setattr(hl_module.httpx, "AsyncClient", _CountingClient)
        client = HyperliquidClient(rate_limiter=limiter)
        await client._post({"type": "meta"}, retries=3, weight=20)
        # Budget only ever admitted the first attempt (weight 20 == total
        # limit); the other 2 retries were refused before any HTTP call.
        assert post_calls["n"] == 1

    async def test_admitted_reservation_persists_even_if_request_errors(self, monkeypatch):
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=20, reconciliation_per_minute=20, clock=clock)
        monkeypatch.setattr(hl_module.asyncio, "sleep", _RecordingSleep())
        monkeypatch.setattr(
            hl_module.httpx, "AsyncClient",
            _scripted_async_client([httpx.TransportError("boom")]),
        )
        client = HyperliquidClient(rate_limiter=limiter)
        await client._post({"type": "meta"}, retries=1, weight=20)
        # The one admitted-then-failed attempt still consumed the budget.
        assert await limiter.acquire(1) is False


class TestReconciliationRequestPath:
    async def test_single_http_attempt_even_on_failure(self, monkeypatch):
        post_calls = {"n": 0}
        real_factory = _scripted_async_client([httpx.TransportError("boom")] * 5)

        class _CountingClient(real_factory):
            async def post(self, url, json=None):
                post_calls["n"] += 1
                return await super().post(url, json=json)

        monkeypatch.setattr(hl_module.httpx, "AsyncClient", _CountingClient)
        client = HyperliquidClient()
        result = await client.get_candle_snapshot(
            "BTC", "1m", 1000, 2000, reconciliation=True, weight=22,
        )
        assert result is None
        assert post_calls["n"] == 1  # exactly one attempt, no outer retry loop here

    async def test_budget_unavailable_raises_without_any_http_attempt(self, monkeypatch):
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=10, reconciliation_per_minute=10, clock=clock)
        await limiter.acquire(10)  # exhaust the budget

        post_calls = {"n": 0}

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None):
                post_calls["n"] += 1
                return _response(json_data=[])

        monkeypatch.setattr(hl_module.httpx, "AsyncClient", _Client)
        client = HyperliquidClient(rate_limiter=limiter)
        with pytest.raises(RestBudgetUnavailable):
            await client.get_candle_snapshot(
                "BTC", "1m", 1000, 2000, reconciliation=True, weight=22,
            )
        assert post_calls["n"] == 0

    async def test_reconciliation_never_waits_for_budget(self, monkeypatch):
        """reconciliation calls must never wait — see 'reconciler prefers
        defer if budget unavailable'."""
        clock = _FakeClock()
        sleep = _RecordingSleep()
        limiter = RestRateLimiter(total_per_minute=10, reconciliation_per_minute=10, clock=clock, sleep=sleep)
        await limiter.acquire(10)
        client = HyperliquidClient(rate_limiter=limiter)
        with pytest.raises(RestBudgetUnavailable):
            await client.get_candle_snapshot("BTC", "1m", 1000, 2000, reconciliation=True, weight=22)
        assert sleep.calls == []

    async def test_reconciliation_reserves_fixed_weight_22(self, monkeypatch):
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=22, reconciliation_per_minute=22, clock=clock)
        monkeypatch.setattr(
            hl_module.httpx, "AsyncClient",
            _scripted_async_client([_response(json_data=[])]),
        )
        client = HyperliquidClient(rate_limiter=limiter)
        result = await client.get_candle_snapshot("BTC", "1m", 1000, 2000, reconciliation=True, weight=22)
        assert result == []
        # Exactly 22 was reserved: budget is now fully consumed.
        assert await limiter.acquire(1) is False

    async def test_oversized_reconciliation_response_charges_extra_and_cools_down(self, monkeypatch):
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=1000, reconciliation_per_minute=1000, clock=clock)
        # 200 rows -> actual weight estimate_candle_snapshot_weight(200) ==
        # 20 + ceil(200/60) == 24, genuinely exceeding the fixed 22
        # reservation (not merely a raw row-count threshold).
        big_response = [{"t": i} for i in range(200)]
        monkeypatch.setattr(
            hl_module.httpx, "AsyncClient",
            _scripted_async_client([_response(json_data=big_response)]),
        )
        client = HyperliquidClient(rate_limiter=limiter)
        result = await client.get_candle_snapshot("BTC", "1m", 1000, 2000, reconciliation=True, weight=22)
        assert result == big_response
        # Cooldown from the oversized-response guard is now active.
        assert await limiter.acquire(1) is False

    async def test_oversized_ordinary_finite_window_response_also_charges_debt(self, monkeypatch):
        """G: the oversized-response guard is not reconciliation-only — an
        ordinary (non-reconciliation) finite-window candleSnapshot response
        that returns far more rows than its own timestamp-derived
        reservation anticipated must also trigger debt/cooldown.
        """
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=1000, reconciliation_per_minute=1000, clock=clock)
        # start/end span implies very few rows (duration=60_000ms, span=1000ms
        # -> max_rows=3 -> reserved weight 21), but the provider returns 200.
        big_response = [{"t": i} for i in range(200)]
        monkeypatch.setattr(
            hl_module.httpx, "AsyncClient",
            _scripted_async_client([_response(json_data=big_response)]),
        )
        client = HyperliquidClient(rate_limiter=limiter)
        result = await client.get_candle_snapshot("BTC", "1m", 1000, 2000)
        assert result == big_response
        assert await limiter.acquire(1) is False

    async def test_reconciliation_oversized_debt_counts_toward_reconciliation_subbudget(self, monkeypatch):
        """G: reconciliation oversized-response debt must count toward BOTH
        the aggregate ceiling and the tighter reconciliation sub-ceiling."""
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=1000, reconciliation_per_minute=30, clock=clock)
        big_response = [{"t": i} for i in range(200)]  # actual weight 24 > reserved 22
        monkeypatch.setattr(
            hl_module.httpx, "AsyncClient",
            _scripted_async_client([_response(json_data=big_response)]),
        )
        client = HyperliquidClient(rate_limiter=limiter)
        await client.get_candle_snapshot("BTC", "1m", 1000, 2000, reconciliation=True, weight=22)
        # The oversized-response guard also sets a 30s cooldown, which blocks
        # *any* acquire regardless of weight/headroom until it elapses.
        assert await limiter.acquire(1, reconciliation=True) is False
        clock.advance(31.0)  # cooldown elapsed, but reservations (<60s old) remain
        # 22 reserved + 2 oversized debt = 24 already counted against the
        # reconciliation sub-budget (30) - only 6 headroom remains.
        assert await limiter.acquire(7, reconciliation=True) is False
        assert await limiter.acquire(6, reconciliation=True) is True

    async def test_invalid_weight_override_rejected_uses_conservative_fallback(self, monkeypatch):
        """G: a negative/NaN/non-numeric weight override must never be
        allowed to under-reserve the shared budget — it falls back to the
        conservative minimum computed from the actual requested window
        (start=1000/end=2000/1m -> span=1000ms -> max_rows=3 -> weight 21),
        not the raw invalid value."""
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=1000, reconciliation_per_minute=1000, clock=clock)
        monkeypatch.setattr(
            hl_module.httpx, "AsyncClient",
            _scripted_async_client([_response(json_data=[])]),
        )
        client = HyperliquidClient(rate_limiter=limiter)
        await client.get_candle_snapshot("BTC", "1m", 1000, 2000, weight=-5)
        assert await limiter.acquire(1000 - 21 + 1) is False
        assert await limiter.acquire(1000 - 21) is True

    async def test_invalid_weight_override_open_ended_window_falls_back_to_5000_row_worst_case(self, monkeypatch):
        """G: with no end_time (open-ended/unsupported window), an invalid
        override still falls back conservatively — to the documented
        worst-case 5000-row reservation (104), since no window bounds the
        actual possible row count."""
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=1000, reconciliation_per_minute=1000, clock=clock)
        monkeypatch.setattr(
            hl_module.httpx, "AsyncClient",
            _scripted_async_client([_response(json_data=[])]),
        )
        client = HyperliquidClient(rate_limiter=limiter)
        await client.get_candle_snapshot("BTC", "1m", 1000, weight=float("nan"))
        assert await limiter.acquire(1000 - 104 + 1) is False
        assert await limiter.acquire(1000 - 104) is True

    async def test_weight_override_zero_falls_back_to_conservative_minimum(self, monkeypatch):
        """G: weight=0 must never reserve zero budget for a call that could
        legitimately return rows — it is floored up to the conservative
        minimum for the actual window (21, as above), not 0."""
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=1000, reconciliation_per_minute=1000, clock=clock)
        monkeypatch.setattr(
            hl_module.httpx, "AsyncClient",
            _scripted_async_client([_response(json_data=[])]),
        )
        client = HyperliquidClient(rate_limiter=limiter)
        await client.get_candle_snapshot("BTC", "1m", 1000, 2000, weight=0)
        assert await limiter.acquire(1000 - 21 + 1) is False
        assert await limiter.acquire(1000 - 21) is True

    async def test_weight_override_one_below_minimum_is_raised_to_minimum(self, monkeypatch):
        """G: a small positive override below the conservative minimum for
        the actual window must be raised to that minimum, never honored
        as-is."""
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=1000, reconciliation_per_minute=1000, clock=clock)
        monkeypatch.setattr(
            hl_module.httpx, "AsyncClient",
            _scripted_async_client([_response(json_data=[])]),
        )
        client = HyperliquidClient(rate_limiter=limiter)
        await client.get_candle_snapshot("BTC", "1m", 1000, 2000, weight=1)
        assert await limiter.acquire(1000 - 21 + 1) is False
        assert await limiter.acquire(1000 - 21) is True

    async def test_weight_override_fraction_is_ceiled_not_floored(self, monkeypatch):
        """G: a fractional override above the conservative minimum is
        rounded UP (ceil), never floored/truncated down (which would
        under-reserve)."""
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=1000, reconciliation_per_minute=1000, clock=clock)
        monkeypatch.setattr(
            hl_module.httpx, "AsyncClient",
            _scripted_async_client([_response(json_data=[])]),
        )
        client = HyperliquidClient(rate_limiter=limiter)
        await client.get_candle_snapshot("BTC", "1m", 1000, 2000, weight=30.1)
        # ceil(30.1) == 31, not floor (30).
        assert await limiter.acquire(1000 - 31 + 1) is False
        assert await limiter.acquire(1000 - 31) is True

    async def test_weight_override_bool_rejected_uses_conservative_fallback(self, monkeypatch):
        """G: bool is an int subtype — must still be rejected as invalid,
        never silently accepted as weight=1 or weight=0."""
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=1000, reconciliation_per_minute=1000, clock=clock)
        monkeypatch.setattr(
            hl_module.httpx, "AsyncClient",
            _scripted_async_client([_response(json_data=[])]),
        )
        client = HyperliquidClient(rate_limiter=limiter)
        await client.get_candle_snapshot("BTC", "1m", 1000, 2000, weight=True)
        assert await limiter.acquire(1000 - 21 + 1) is False
        assert await limiter.acquire(1000 - 21) is True

    async def test_invalid_weight_override_never_logs_raw_value(self, monkeypatch, caplog):
        """G: an invalid override's raw value must never be echoed into
        logs — only a fixed message/type name."""
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=1000, reconciliation_per_minute=1000, clock=clock)
        monkeypatch.setattr(
            hl_module.httpx, "AsyncClient",
            _scripted_async_client([_response(json_data=[])]),
        )
        client = HyperliquidClient(rate_limiter=limiter)
        with caplog.at_level("WARNING"):
            await client.get_candle_snapshot(
                "BTC", "1m", 1000, 2000, weight="SECRET_MARKER_DO_NOT_LEAK",
            )
        assert "SECRET_MARKER_DO_NOT_LEAK" not in caplog.text


class TestOrdinaryCallWaitsForOwnBudgetInsteadOfFalseFailure:
    """Regression coverage for the production startup defect: an ordinary
    call must wait (bounded) for its own temporarily-full rolling budget
    to free rather than being reported as a failed upstream request, while
    reconciliation calls still never wait and a genuinely-unobtainable
    budget still fails closed, bounded."""

    async def test_near_full_window_waits_then_makes_exactly_one_http_attempt(self, monkeypatch, caplog):
        clock = _FakeClock()

        async def advancing_sleep(seconds: float) -> None:
            clock.advance(seconds)

        limiter = RestRateLimiter(
            total_per_minute=20, reconciliation_per_minute=20, clock=clock, sleep=advancing_sleep,
        )
        await limiter.acquire(20)  # fills the budget; ~60s until it frees

        post_calls = {"n": 0}
        real_factory = _scripted_async_client([_response(json_data={"universe": []})])

        class _CountingClient(real_factory):
            async def post(self, url, json=None):
                post_calls["n"] += 1
                return await super().post(url, json=json)

        monkeypatch.setattr(hl_module.httpx, "AsyncClient", _CountingClient)
        monkeypatch.setattr(hl_module.asyncio, "sleep", _RecordingSleep())
        client = HyperliquidClient(rate_limiter=limiter)
        with caplog.at_level("ERROR"):
            result = await client.get_perp_meta()
        assert result == {"universe": []}
        assert post_calls["n"] == 1
        assert "Hyperliquid request failed" not in caplog.text

    async def test_cooldown_clearing_within_bound_waits_then_succeeds(self, monkeypatch, caplog):
        clock = _FakeClock()

        async def advancing_sleep(seconds: float) -> None:
            clock.advance(seconds)

        limiter = RestRateLimiter(
            total_per_minute=1000, reconciliation_per_minute=1000, clock=clock, sleep=advancing_sleep,
        )
        await limiter.note_429(retry_after=45.0)  # clears within the 60s bound

        post_calls = {"n": 0}
        real_factory = _scripted_async_client([_response(json_data={"BTC": "1"})])

        class _CountingClient(real_factory):
            async def post(self, url, json=None):
                post_calls["n"] += 1
                return await super().post(url, json=json)

        monkeypatch.setattr(hl_module.httpx, "AsyncClient", _CountingClient)
        monkeypatch.setattr(hl_module.asyncio, "sleep", _RecordingSleep())
        client = HyperliquidClient(rate_limiter=limiter)
        with caplog.at_level("ERROR"):
            result = await client.get_all_mids()
        assert result == {"BTC": "1"}
        assert post_calls["n"] == 1
        assert "Hyperliquid request failed" not in caplog.text

    async def test_reconciliation_still_never_waits_even_near_full_window(self, monkeypatch):
        clock = _FakeClock()
        sleep = _RecordingSleep()
        limiter = RestRateLimiter(total_per_minute=20, reconciliation_per_minute=20, clock=clock, sleep=sleep)
        await limiter.acquire(20)
        client = HyperliquidClient(rate_limiter=limiter)
        with pytest.raises(RestBudgetUnavailable):
            await client.get_candle_snapshot("BTC", "1m", 1000, 2000, reconciliation=True, weight=20)
        assert sleep.calls == []

    async def test_bounded_fail_closed_when_capacity_never_obtained_within_maximum(self, monkeypatch, caplog):
        """If capacity genuinely cannot be obtained within the documented
        maximum wait (here, a cooldown that outlasts it on every attempt),
        an ordinary call still correctly fails closed — returning None, not
        raising — with each attempt's wait bounded, never growing."""
        clock = _FakeClock()
        sleep_calls: list[float] = []

        async def advancing_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            clock.advance(seconds)

        limiter = RestRateLimiter(
            total_per_minute=1000, reconciliation_per_minute=1000, clock=clock, sleep=advancing_sleep,
        )
        await limiter.note_429(retry_after=200.0)  # outlasts the 60s bound

        monkeypatch.setattr(hl_module.asyncio, "sleep", _RecordingSleep())
        client = HyperliquidClient(rate_limiter=limiter)
        with caplog.at_level("ERROR"):
            result = await client.get_perp_meta()
        assert result is None
        assert "Hyperliquid request failed after 3 attempts" in caplog.text
        assert all(s <= 60.0 for s in sleep_calls)  # each wait individually bounded


class TestCancellationDuringBudgetWait:
    async def test_cancelled_error_propagates_while_waiting_for_admission(self):
        """CancelledError raised while sleeping for own-budget admission
        (not merely while blocked on the HTTP call) must still propagate,
        never be swallowed as an ordinary budget-unavailable refusal."""
        clock = _FakeClock()

        async def cancelling_sleep(seconds: float) -> None:
            raise asyncio.CancelledError()

        limiter = RestRateLimiter(
            total_per_minute=20, reconciliation_per_minute=20, clock=clock, sleep=cancelling_sleep,
        )
        await limiter.acquire(20)  # fills the budget so the next call must wait
        client = HyperliquidClient(rate_limiter=limiter)
        with pytest.raises(asyncio.CancelledError):
            await client.get_perp_meta()


class Test429Handling:
    async def test_429_returns_none_without_retry_storm(self, monkeypatch):
        post_calls = {"n": 0}
        real_factory = _scripted_async_client(
            [_response(status_code=429, headers={"Retry-After": "5"})] * 3
        )

        class _CountingClient(real_factory):
            async def post(self, url, json=None):
                post_calls["n"] += 1
                return await super().post(url, json=json)

        monkeypatch.setattr(hl_module.httpx, "AsyncClient", _CountingClient)
        monkeypatch.setattr(hl_module.asyncio, "sleep", _RecordingSleep())
        client = HyperliquidClient()
        result = await client._post({"type": "meta"}, retries=3, weight=20)
        assert result is None
        assert post_calls["n"] == 1  # no immediate-loop retry after a 429

    async def test_429_sets_cooldown_blocking_subsequent_calls(self, monkeypatch):
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=1000, reconciliation_per_minute=1000, clock=clock)
        monkeypatch.setattr(
            hl_module.httpx, "AsyncClient",
            _scripted_async_client([_response(status_code=429, headers={"Retry-After": "10"})]),
        )
        client = HyperliquidClient(rate_limiter=limiter)
        await client._post({"type": "meta"}, retries=1, weight=20)
        assert await limiter.acquire(1) is False
        clock.advance(10.1)
        assert await limiter.acquire(1) is True

    async def test_invalid_retry_after_falls_back_to_conservative_default(self, monkeypatch):
        clock = _FakeClock()
        limiter = RestRateLimiter(total_per_minute=1000, reconciliation_per_minute=1000, clock=clock)
        monkeypatch.setattr(
            hl_module.httpx, "AsyncClient",
            _scripted_async_client([_response(status_code=429, headers={"Retry-After": "not-a-number"})]),
        )
        client = HyperliquidClient(rate_limiter=limiter)
        await client._post({"type": "meta"}, retries=1, weight=20)
        clock.advance(59.9)
        assert await limiter.acquire(1) is False
        clock.advance(0.2)
        assert await limiter.acquire(1) is True

    async def test_never_echoes_raw_response_body_in_logs(self, monkeypatch, caplog):
        monkeypatch.setattr(
            hl_module.httpx, "AsyncClient",
            _scripted_async_client(
                [_response(status_code=429, json_data={"secret": "SECRET_MARKER_DO_NOT_LEAK"})]
            ),
        )
        client = HyperliquidClient()
        with caplog.at_level("WARNING"):
            await client._post({"type": "meta"}, retries=1, weight=20)
        assert "SECRET_MARKER_DO_NOT_LEAK" not in caplog.text


class TestCancellationPropagates:
    async def test_cancelled_error_propagates_out_of_post(self, monkeypatch):
        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None):
                raise asyncio.CancelledError()

        monkeypatch.setattr(hl_module.httpx, "AsyncClient", _Client)
        client = HyperliquidClient()
        with pytest.raises(asyncio.CancelledError):
            await client._post({"type": "meta"}, retries=1, weight=20)
