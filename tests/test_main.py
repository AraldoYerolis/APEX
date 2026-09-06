"""Tests for the WS-manager/universe-refresh synchronization in apex.main.

Root cause under test: before this change, run_universe_refresh() re-read
scan_enabled markets on every scheduled tick, but the WS manager's
subscription list was fixed at startup and never reconciled against it — a
newly-added scan symbol got no live candles, a removed one stayed
subscribed forever, and a process that started with zero scan symbols could
never recover a WS feed later. run_universe_ws_sync() (registered in place
of calling run_universe_refresh() directly) closes that gap by comparing
the *actual* WS-subscribed symbol set (the existing scan_symbols[:30] slice,
unchanged) against each refresh's result as sets, and only rebuilding the WS
manager when that set actually changes.
"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from apex import main
from apex.config import Settings
from apex.data import market_universe
from apex.data.reconnecting_ws import ReconnectingWebSocket


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """WS manager/membership/shutdown/lock state is process-global; isolate every test."""
    monkeypatch.setattr(main, "_ws_manager", None)
    monkeypatch.setattr(main, "_ws_symbols", [])
    monkeypatch.setattr(main, "_shutdown_started", False)
    monkeypatch.setattr(main, "_ws_lifecycle_lock", None)
    monkeypatch.setattr(main, "_candle_store", None)
    main._ws_log_budget.clear()
    yield
    main._ws_log_budget.clear()


@pytest.fixture(autouse=True)
def _no_real_websocket_connections(monkeypatch):
    """Never let a test's WS manager actually try to open a socket.

    _activate_ws_manager() always constructs a real ReconnectingWebSocket;
    start() would otherwise schedule a background task that dials the real
    (or a fake-but-uninstalled) URL. Instances built directly by tests use
    the same no-op so `.stop()` behaves identically (no-op — _task is None).
    """
    monkeypatch.setattr(ReconnectingWebSocket, "start", lambda self: None)


class _FakeApp:
    def __init__(self, scan_symbols=None):
        self.state = types.SimpleNamespace(scan_symbols=scan_symbols)


class _FakeCandleStore:
    def __init__(self):
        self.load_calls: list[tuple[str, str]] = []

    def load_from_db(self, symbol: str, timeframe: str, limit: int = 200) -> None:
        self.load_calls.append((symbol, timeframe))


def _settings() -> Settings:
    return Settings()


def _timeframes(settings: Settings) -> list[str]:
    return main._build_timeframes(settings)


def _manager(symbols: list[str], timeframes: list[str]) -> ReconnectingWebSocket:
    subs = main.build_ws_subscriptions(symbols, timeframes)
    return ReconnectingWebSocket(
        url="wss://example.invalid/ws",
        on_message=main.on_ws_message,
        subscriptions=subs,
    )


async def _sync(monkeypatch, refresh_result, app=None, conn=None, client=None):
    """Run run_universe_ws_sync with run_universe_refresh mocked to `refresh_result`."""
    monkeypatch.setattr(main, "run_universe_refresh", AsyncMock(return_value=refresh_result))
    app = app if app is not None else _FakeApp()
    conn = conn if conn is not None else object()
    client = client if client is not None else AsyncMock()
    await main.run_universe_ws_sync(conn, client, _settings(), app)
    return app


# ============================================================================
# 1. Same membership, different order -> no rebuild
# ============================================================================

async def test_same_membership_different_order_no_rebuild(monkeypatch):
    settings = _settings()
    timeframes = _timeframes(settings)
    old_symbols = ["BTC", "ETH", "SOL"]
    old_manager = _manager(old_symbols, timeframes)
    stop_spy = AsyncMock(wraps=old_manager.stop)
    monkeypatch.setattr(old_manager, "stop", stop_spy)

    monkeypatch.setattr(main, "_ws_manager", old_manager)
    monkeypatch.setattr(main, "_ws_symbols", old_symbols)
    backfill_spy = AsyncMock()
    monkeypatch.setattr(main, "backfill_candles", backfill_spy)
    main._ws_log_budget["error"] = 3

    app = await _sync(monkeypatch, ["ETH", "SOL", "BTC"])  # reordered, same set

    stop_spy.assert_not_called()
    backfill_spy.assert_not_called()
    assert main._ws_manager is old_manager
    assert main._ws_symbols == old_symbols
    assert main._ws_log_budget == {"error": 3}  # diagnostics untouched
    # Broader /status snapshot still reflects the latest refresh.
    assert app.state.scan_symbols == ["ETH", "SOL", "BTC"]


# ============================================================================
# 2. Added symbol -> rebuild, preload+backfill, included in new subscriptions
# ============================================================================

async def test_added_symbol_triggers_rebuild_with_preload_and_backfill(monkeypatch):
    settings = _settings()
    timeframes = _timeframes(settings)
    old_symbols = ["BTC", "ETH"]
    old_manager = _manager(old_symbols, timeframes)
    stop_spy = AsyncMock(wraps=old_manager.stop)
    monkeypatch.setattr(old_manager, "stop", stop_spy)

    monkeypatch.setattr(main, "_ws_manager", old_manager)
    monkeypatch.setattr(main, "_ws_symbols", old_symbols)
    store = _FakeCandleStore()
    monkeypatch.setattr(main, "_candle_store", store)
    backfill_spy = AsyncMock()
    monkeypatch.setattr(main, "backfill_candles", backfill_spy)
    client = AsyncMock()

    await _sync(monkeypatch, ["BTC", "ETH", "SOL"], client=client)

    stop_spy.assert_awaited_once()
    assert main._ws_manager is not old_manager
    assert set(main._ws_symbols) == {"BTC", "ETH", "SOL"}

    # load_from_db called for the added symbol across every configured timeframe.
    assert set(store.load_calls) == {("SOL", tf) for tf in timeframes}
    assert all(sym == "SOL" for sym, _ in store.load_calls)  # not re-loaded for BTC/ETH

    backfill_spy.assert_awaited_once_with(client, store, ["SOL"], timeframes)

    coins = {s["coin"] for s in main._ws_manager.subscriptions}
    assert coins == {"BTC", "ETH", "SOL"}


# ============================================================================
# 3. Removed symbol -> rebuild, excluded from new subscriptions
# ============================================================================

async def test_removed_symbol_triggers_rebuild_and_is_excluded(monkeypatch):
    settings = _settings()
    timeframes = _timeframes(settings)
    old_symbols = ["BTC", "ETH", "SOL"]
    old_manager = _manager(old_symbols, timeframes)
    monkeypatch.setattr(main, "_ws_manager", old_manager)
    monkeypatch.setattr(main, "_ws_symbols", old_symbols)
    backfill_spy = AsyncMock()
    monkeypatch.setattr(main, "backfill_candles", backfill_spy)

    await _sync(monkeypatch, ["BTC", "ETH"])  # SOL dropped from the universe

    assert main._ws_manager is not old_manager
    assert set(main._ws_symbols) == {"BTC", "ETH"}
    backfill_spy.assert_not_called()  # nothing added, so nothing to backfill

    coins = {s["coin"] for s in main._ws_manager.subscriptions}
    assert "SOL" not in coins
    assert coins == {"BTC", "ETH"}


# ============================================================================
# 4. Top-30 membership change caused purely by reordering
# ============================================================================

async def test_top30_reorder_changes_membership_triggers_rebuild(monkeypatch):
    settings = _settings()
    timeframes = _timeframes(settings)
    universe = [f"SYM{i:02d}" for i in range(32)]  # 32 > 30: two symbols always excluded
    old_desired = universe[:30]
    old_manager = _manager(old_desired, timeframes)
    monkeypatch.setattr(main, "_ws_manager", old_manager)
    monkeypatch.setattr(main, "_ws_symbols", old_desired)
    monkeypatch.setattr(main, "backfill_candles", AsyncMock())
    monkeypatch.setattr(main, "_candle_store", _FakeCandleStore())

    # Ranking change only: SYM30 (previously rank 31, excluded) swaps with
    # SYM29 (previously rank 30, included). Same 32-symbol universe, same
    # max_symbols cap — only the ranking inside the [:30] slice changed.
    new_order = list(universe)
    new_order[29], new_order[30] = new_order[30], new_order[29]
    new_desired = new_order[:30]
    assert set(new_desired) != set(old_desired)  # sanity: the slice actually changed

    await _sync(monkeypatch, new_order)

    assert main._ws_manager is not old_manager
    assert set(main._ws_symbols) == set(new_desired)
    assert "SYM29" not in main._ws_symbols
    assert "SYM30" in main._ws_symbols


# ============================================================================
# 4b. Replacement-activation failure safety (Validator F1)
#
# Root cause: the original swap sequence stopped the old manager and only
# THEN attempted to build/start the replacement, with no failure handling —
# if that activation raised, _ws_manager was left pointing at the now-dead
# old instance while looking completely unchanged (no reassignment ever
# happened), silently leaving the feed down. The fix separates "construct"
# (side-effect-free, done before the old manager is touched) from "start"
# (done after old_manager.stop(), the only step that can meaningfully fail),
# and on a start failure attempts to restart the already-stopped old
# manager rather than leaving a stale/dead reference in place.
# ============================================================================

def _break_start(monkeypatch, manager: ReconnectingWebSocket, message: str) -> MagicMock:
    """Make this specific manager instance's start() raise `message`.

    Overrides only the instance (not the class), so other managers built in
    the same test are unaffected.
    """
    spy = MagicMock(side_effect=RuntimeError(message))
    monkeypatch.setattr(manager, "start", spy)
    return spy


async def test_replacement_start_failure_recovers_old_manager(monkeypatch, caplog):
    settings = _settings()
    timeframes = _timeframes(settings)
    old_symbols = ["BTC", "ETH"]
    old_manager = _manager(old_symbols, timeframes)
    stop_spy = AsyncMock(wraps=old_manager.stop)
    monkeypatch.setattr(old_manager, "stop", stop_spy)
    # old_manager.start() must succeed during recovery — spy, don't break it.
    old_start_spy = MagicMock(return_value=None)
    monkeypatch.setattr(old_manager, "start", old_start_spy)

    monkeypatch.setattr(main, "_ws_manager", old_manager)
    monkeypatch.setattr(main, "_ws_symbols", old_symbols)
    monkeypatch.setattr(main, "_candle_store", _FakeCandleStore())
    monkeypatch.setattr(main, "backfill_candles", AsyncMock())
    main._ws_log_budget["error"] = 7

    real_build = main._build_ws_manager
    built: list[ReconnectingWebSocket] = []

    def build_broken(subs, settings):
        replacement = real_build(subs, settings)
        _break_start(monkeypatch, replacement, "replacement start failed")
        built.append(replacement)
        return replacement

    monkeypatch.setattr(main, "_build_ws_manager", build_broken)

    with caplog.at_level("DEBUG", logger="apex.main"):
        with pytest.raises(RuntimeError, match="replacement start failed"):
            await _sync(monkeypatch, ["BTC", "ETH", "SOL"])  # SOL added -> triggers rebuild

    replacement = built[0]

    # Old manager was stopped, THEN (and only then) recovery was attempted.
    stop_spy.assert_awaited_once()
    old_start_spy.assert_called_once()

    # No overlap: the replacement's start() raised before ever creating a
    # task (broken_start never calls the real create_task), so there is
    # nothing to leave orphaned — and old_manager is the sole live manager.
    assert main._ws_manager is old_manager
    assert main._ws_symbols == old_symbols  # OLD membership, not the new one
    assert replacement is not old_manager

    # Diagnostics were not reset as though the rebuild succeeded.
    assert main._ws_log_budget == {"error": 7}

    text = caplog.text
    assert "WS rebuilt" not in text
    assert "WS manager recovered from degraded" not in text
    assert "recovered old feed" in text


async def test_replacement_and_recovery_both_fail_leaves_explicitly_degraded(monkeypatch, caplog):
    settings = _settings()
    timeframes = _timeframes(settings)
    old_symbols = ["BTC", "ETH"]
    old_manager = _manager(old_symbols, timeframes)
    stop_spy = AsyncMock(wraps=old_manager.stop)
    monkeypatch.setattr(old_manager, "stop", stop_spy)
    _break_start(monkeypatch, old_manager, "old-manager recovery start failed")

    monkeypatch.setattr(main, "_ws_manager", old_manager)
    monkeypatch.setattr(main, "_ws_symbols", old_symbols)
    monkeypatch.setattr(main, "_candle_store", _FakeCandleStore())
    monkeypatch.setattr(main, "backfill_candles", AsyncMock())
    main._ws_log_budget["error"] = 7

    real_build = main._build_ws_manager
    built: list[ReconnectingWebSocket] = []

    def build_broken(subs, settings):
        replacement = real_build(subs, settings)
        _break_start(monkeypatch, replacement, "replacement start failed")
        built.append(replacement)
        return replacement

    monkeypatch.setattr(main, "_build_ws_manager", build_broken)

    with caplog.at_level("DEBUG", logger="apex.main"):
        with pytest.raises(RuntimeError, match="old-manager recovery start failed"):
            await _sync(monkeypatch, ["BTC", "ETH", "SOL"])

    # Explicitly degraded — never falsely healthy, never claims the new
    # (never-achieved) membership, and _ws_symbols is cleared too (not left
    # pointing at symbols nothing is actually subscribed to any more, which
    # would otherwise make the *next* refresh wrongly conclude "membership
    # unchanged, nothing to do" with no manager at all).
    assert main._ws_manager is None
    assert main._ws_symbols == []

    assert main._ws_log_budget == {"error": 7}  # not reset — nothing succeeded

    text = caplog.text
    assert "WS rebuilt" not in text
    assert "WS manager recovered from degraded" not in text
    assert "WS feed is DOWN" in text

    # The replacement never got far enough to create a task — nothing to orphan.
    replacement = built[0]
    assert replacement is not old_manager


# ============================================================================
# 5. Empty refresh with a healthy existing manager -> preserved untouched
# ============================================================================

async def test_empty_refresh_preserves_existing_manager(monkeypatch):
    settings = _settings()
    timeframes = _timeframes(settings)
    old_symbols = ["BTC", "ETH"]
    old_manager = _manager(old_symbols, timeframes)
    stop_spy = AsyncMock(wraps=old_manager.stop)
    monkeypatch.setattr(old_manager, "stop", stop_spy)
    monkeypatch.setattr(main, "_ws_manager", old_manager)
    monkeypatch.setattr(main, "_ws_symbols", old_symbols)

    app = _FakeApp(scan_symbols=["BTC", "ETH"])
    await _sync(monkeypatch, [], app=app)  # refresh failure

    stop_spy.assert_not_called()
    assert main._ws_manager is old_manager
    assert main._ws_symbols == old_symbols
    assert app.state.scan_symbols == ["BTC", "ETH"]  # not falsely replaced with []


# ============================================================================
# 6. Startup-degraded recovery: manager None -> later non-empty refresh
# ============================================================================

async def test_startup_degraded_recovery_builds_manager_from_none(monkeypatch):
    assert main._ws_manager is None
    assert main._ws_symbols == []

    store = _FakeCandleStore()
    monkeypatch.setattr(main, "_candle_store", store)
    backfill_spy = AsyncMock()
    monkeypatch.setattr(main, "backfill_candles", backfill_spy)

    await _sync(monkeypatch, ["BTC", "ETH"])

    assert main._ws_manager is not None
    assert set(main._ws_symbols) == {"BTC", "ETH"}
    backfill_spy.assert_awaited_once()
    assert set(backfill_spy.await_args.args[2]) == {"BTC", "ETH"}


# ============================================================================
# 7. Canonical casing preserved through a rebuild
# ============================================================================

async def test_rebuild_preserves_canonical_kpepe_casing(monkeypatch):
    market_universe._record_upstream_symbol("kPEPE", "KPEPE")
    try:
        monkeypatch.setattr(main, "_candle_store", _FakeCandleStore())
        monkeypatch.setattr(main, "backfill_candles", AsyncMock())

        await _sync(monkeypatch, ["BTC", "KPEPE"])

        coins = {s["coin"] for s in main._ws_manager.subscriptions}
        assert "kPEPE" in coins
        assert "KPEPE" not in coins
    finally:
        market_universe._reset_upstream_symbol_map()


# ============================================================================
# 8. Log-budget reset on rebuild, not on unchanged membership
# ============================================================================

async def test_log_budget_reset_on_rebuild(monkeypatch):
    settings = _settings()
    timeframes = _timeframes(settings)
    old_symbols = ["BTC"]
    monkeypatch.setattr(main, "_ws_manager", _manager(old_symbols, timeframes))
    monkeypatch.setattr(main, "_ws_symbols", old_symbols)
    monkeypatch.setattr(main, "_candle_store", _FakeCandleStore())
    monkeypatch.setattr(main, "backfill_candles", AsyncMock())
    main._ws_log_budget["error"] = 5

    await _sync(monkeypatch, ["BTC", "ETH"])

    assert main._ws_log_budget == {}


async def test_log_budget_not_reset_on_unchanged_membership(monkeypatch):
    settings = _settings()
    timeframes = _timeframes(settings)
    symbols = ["BTC", "ETH"]
    monkeypatch.setattr(main, "_ws_manager", _manager(symbols, timeframes))
    monkeypatch.setattr(main, "_ws_symbols", symbols)
    main._ws_log_budget["error"] = 5

    await _sync(monkeypatch, list(symbols))  # identical membership

    assert main._ws_log_budget == {"error": 5}


# ============================================================================
# 9. Shutdown coordination
# ============================================================================

async def test_shutdown_flag_already_set_prevents_rebuild(monkeypatch):
    settings = _settings()
    timeframes = _timeframes(settings)
    old_manager = _manager(["BTC"], timeframes)
    monkeypatch.setattr(main, "_ws_manager", old_manager)
    monkeypatch.setattr(main, "_ws_symbols", ["BTC"])
    monkeypatch.setattr(main, "_shutdown_started", True)
    backfill_spy = AsyncMock()
    monkeypatch.setattr(main, "backfill_candles", backfill_spy)
    monkeypatch.setattr(main, "_candle_store", _FakeCandleStore())

    await _sync(monkeypatch, ["BTC", "ETH"])  # would otherwise rebuild

    assert main._ws_manager is old_manager  # no replacement — no orphan
    backfill_spy.assert_not_called()


async def test_shutdown_during_backfill_aborts_rebuild_no_orphan(monkeypatch):
    old_manager = _manager(["BTC"], _timeframes(_settings()))
    monkeypatch.setattr(main, "_ws_manager", old_manager)
    monkeypatch.setattr(main, "_ws_symbols", ["BTC"])
    monkeypatch.setattr(main, "_candle_store", _FakeCandleStore())

    async def backfill_flips_shutdown(*args, **kwargs):
        main._shutdown_started = True

    monkeypatch.setattr(main, "backfill_candles", AsyncMock(side_effect=backfill_flips_shutdown))

    await _sync(monkeypatch, ["BTC", "ETH"])

    # Aborted after backfill, before the swap: old manager is still the
    # authoritative one, never stopped, and no replacement was started.
    assert main._ws_manager is old_manager
    assert main._ws_symbols == ["BTC"]


async def test_shutdown_lock_serializes_with_inflight_rebuild(monkeypatch):
    """Shutdown's WS-stop must wait on the same lock an in-flight rebuild holds.

    Proves the critical region (backfill through the abort-or-swap decision)
    cannot race a concurrent shutdown's WS teardown / DB close: shutdown's
    stop only runs after the rebuild's lock-held section has fully finished,
    whichever way that section resolved. Here shutdown's flag flips mid-
    backfill, so the resumed rebuild aborts before ever swapping in a new
    manager (see test_shutdown_during_backfill_aborts_rebuild_no_orphan) —
    this test's distinct value is proving the *lock* actually serializes the
    two paths rather than letting them race.
    """
    old_manager = _manager(["BTC"], _timeframes(_settings()))
    monkeypatch.setattr(main, "_ws_manager", old_manager)
    monkeypatch.setattr(main, "_ws_symbols", ["BTC"])
    monkeypatch.setattr(main, "_candle_store", _FakeCandleStore())

    events: list[str] = []

    async def slow_backfill(*args, **kwargs):
        events.append("backfill_start")
        await asyncio.sleep(0.05)
        events.append("backfill_end")

    monkeypatch.setattr(main, "backfill_candles", slow_backfill)
    monkeypatch.setattr(main, "run_universe_refresh", AsyncMock(return_value=["BTC", "ETH"]))

    async def simulated_shutdown():
        await asyncio.sleep(0.01)  # let the rebuild start and take the lock first
        main._shutdown_started = True
        async with main._get_ws_lifecycle_lock():
            events.append("shutdown_stop_manager")
            if main._ws_manager:
                await main._ws_manager.stop()

    await asyncio.gather(
        main.run_universe_ws_sync(object(), AsyncMock(), _settings(), _FakeApp()),
        simulated_shutdown(),
    )

    # Shutdown's stop happened only after the rebuild's own backfill finished
    # and released the lock — not concurrently with it.
    assert events.index("backfill_end") < events.index("shutdown_stop_manager")
    # The rebuild saw the flag flip and aborted before swapping — old_manager
    # is what shutdown correctly stopped; nothing was ever orphaned.
    assert main._ws_manager is old_manager


async def test_shutdown_wins_lock_race_prevents_new_manager_creation(monkeypatch):
    """If shutdown acquires the lifecycle lock first, a rebuild that was
    waiting on it must abort once it finally acquires the lock — never
    creating (and thus never orphaning) a new manager after shutdown has
    already committed to tearing down.
    """
    old_manager = _manager(["BTC"], _timeframes(_settings()))
    monkeypatch.setattr(main, "_ws_manager", old_manager)
    monkeypatch.setattr(main, "_ws_symbols", ["BTC"])
    monkeypatch.setattr(main, "_candle_store", _FakeCandleStore())
    monkeypatch.setattr(main, "backfill_candles", AsyncMock())
    monkeypatch.setattr(main, "run_universe_refresh", AsyncMock(return_value=["BTC", "ETH"]))

    lock = main._get_ws_lifecycle_lock()
    await lock.acquire()
    try:
        sync_task = asyncio.create_task(
            main.run_universe_ws_sync(object(), AsyncMock(), _settings(), _FakeApp())
        )
        await asyncio.sleep(0.01)  # let it start and block waiting for the lock
        assert not sync_task.done()
        main._shutdown_started = True  # shutdown "wins": flips the flag while holding the lock
    finally:
        lock.release()

    await sync_task

    assert main._ws_manager is old_manager  # never replaced


# ============================================================================
# 10. Status diagnostic (app.state.scan_symbols)
# ============================================================================

async def test_successful_refresh_updates_status_state(monkeypatch):
    app = await _sync(monkeypatch, ["BTC", "ETH"])
    assert app.state.scan_symbols == ["BTC", "ETH"]


async def test_failed_refresh_does_not_replace_status_state(monkeypatch):
    app = _FakeApp(scan_symbols=["BTC", "ETH"])
    await _sync(monkeypatch, [], app=app)
    assert app.state.scan_symbols == ["BTC", "ETH"]
