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
import logging
import re
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

import apex.data.candle_store as candle_store_module
from apex import main
from apex.config import Settings
from apex.data import market_universe
from apex.data.candle_store import CandleStore
from apex.data.reconnecting_ws import ReconnectingWebSocket


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """WS manager/membership/shutdown/lock state is process-global; isolate every test."""
    monkeypatch.setattr(main, "_ws_manager", None)
    monkeypatch.setattr(main, "_ws_symbols", [])
    monkeypatch.setattr(main, "_shutdown_started", False)
    monkeypatch.setattr(main, "_ws_lifecycle_lock", None)
    monkeypatch.setattr(main, "_candle_store", None)
    monkeypatch.setattr(main, "_candle_reconciler", None)
    monkeypatch.setattr(main, "_ws_membership_epoch", 0)
    monkeypatch.setattr(main, "_reconciliation_suspended", False)
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
        self.prune_calls: list[set] = []
        self.membership_calls: list[tuple[set, bool]] = []

    def load_from_db(self, symbol: str, timeframe: str, limit: int = 200) -> None:
        self.load_calls.append((symbol, timeframe))

    def get_diagnostics(self, symbol: str, timeframe: str):
        return None

    def is_diagnostics_tracked(self, symbol: str, timeframe: str) -> bool:
        return False

    def prune_diagnostics(self, keep: set) -> None:
        self.prune_calls.append(set(keep))

    def set_diagnostics_membership(self, keys, *, replace: bool = True) -> None:
        self.membership_calls.append((set(keys), replace))


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


async def test_replacement_start_failure_preserves_old_diagnostic_membership(monkeypatch, tmp_path):
    """A failed rebuild must leave the OLD, still-live diagnostic membership
    intact — pruning to the new (never-achieved) membership only ever
    happens after a confirmed-successful swap.
    """
    from apex.db.connection import init_db as real_init_db

    settings = Settings(candle_diagnostics_enabled=True)
    timeframes = _timeframes(settings)
    old_symbols = ["BTC", "ETH"]
    old_manager = _manager(old_symbols, timeframes)
    monkeypatch.setattr(old_manager, "stop", AsyncMock(wraps=old_manager.stop))
    monkeypatch.setattr(old_manager, "start", MagicMock(return_value=None))

    monkeypatch.setattr(main, "_ws_manager", old_manager)
    monkeypatch.setattr(main, "_ws_symbols", old_symbols)

    store = CandleStore(real_init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
    store.set_diagnostics_membership({(sym, tf) for sym in old_symbols for tf in timeframes})
    open_time = 1_800_000_000_000
    store.update("BTC", "3m", {"t": open_time, "T": open_time + 179_999, "o": "1", "h": "1", "l": "1", "c": "1", "v": "1"}, persist=False, now_ms=open_time + 180_000, source="ws")
    monkeypatch.setattr(main, "_candle_store", store)
    monkeypatch.setattr(main, "backfill_candles", AsyncMock())

    real_build = main._build_ws_manager

    def build_broken(subs, settings):
        replacement = real_build(subs, settings)
        _break_start(monkeypatch, replacement, "replacement start failed")
        return replacement

    monkeypatch.setattr(main, "_build_ws_manager", build_broken)
    monkeypatch.setattr(main, "run_universe_refresh", AsyncMock(return_value=["BTC", "ETH", "SOL"]))

    with pytest.raises(RuntimeError, match="replacement start failed"):
        # Called directly (not via _sync, which always builds its own
        # diagnostics-disabled default Settings) so `settings`
        # (candle_diagnostics_enabled=True) actually governs this call.
        await main.run_universe_ws_sync(object(), AsyncMock(), settings, _FakeApp())

    # Old membership (including its accumulated diagnostic facts) is intact.
    assert store.is_diagnostics_tracked("BTC", "3m") is True
    diag = store.get_diagnostics("BTC", "3m")
    assert diag is not None and diag.received_eligible_count == 1
    # The prune-to-new-membership call never happened.
    assert store.diagnostics_membership_generation == 1

    # SOL (the prospective added symbol) never had its diagnostic membership
    # expanded before the swap was confirmed to succeed — it stays fully
    # untracked, not a dangling allocation-cap-occupying candidate.
    assert store.is_diagnostics_tracked("SOL", "3m") is False
    assert store.get_diagnostics("SOL", "3m") is None


async def test_successful_rebuild_starts_fresh_epoch_for_added_symbol(monkeypatch, tmp_path):
    """A newly added symbol is untracked during preload/backfill (no
    membership expansion for a prospective symbol) and only becomes tracked,
    in a fresh zero-traffic epoch, once the swap that actually subscribes to
    it has succeeded.
    """
    from apex.db.connection import init_db as real_init_db

    settings = Settings(candle_diagnostics_enabled=True)
    timeframes = main._build_timeframes(settings)
    old_symbols = ["BTC", "ETH"]
    old_manager = _manager(old_symbols, timeframes)
    monkeypatch.setattr(old_manager, "stop", AsyncMock(wraps=old_manager.stop))
    monkeypatch.setattr(main, "_ws_manager", old_manager)
    monkeypatch.setattr(main, "_ws_symbols", old_symbols)

    store = CandleStore(real_init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
    store.set_diagnostics_membership({(sym, tf) for sym in old_symbols for tf in timeframes})
    monkeypatch.setattr(main, "_candle_store", store)

    tracked_during_preload: dict = {}

    async def spying_backfill(client, candle_store, symbols, tfs):
        for sym in symbols:
            for tf in tfs:
                tracked_during_preload[(sym, tf)] = candle_store.is_diagnostics_tracked(sym, tf)

    monkeypatch.setattr(main, "backfill_candles", spying_backfill)
    monkeypatch.setattr(main, "run_universe_refresh", AsyncMock(return_value=["BTC", "ETH", "SOL"]))

    await main.run_universe_ws_sync(object(), AsyncMock(), settings, _FakeApp())

    # Untracked while the rebuild was still in flight.
    assert tracked_during_preload[("SOL", timeframes[0])] is False

    # Tracked, in a fresh (zero-traffic) epoch, only after the swap succeeded.
    assert store.is_diagnostics_tracked("SOL", timeframes[0]) is True
    assert store.get_diagnostics("SOL", timeframes[0]) is None


async def test_snapshot_reports_observed_zero_after_successful_swap_with_no_new_traffic(
    monkeypatch, caplog, tmp_path
):
    """A diagnostics snapshot taken right after a successful WS membership
    swap (prune_diagnostics -> CandleStore.set_diagnostics_membership's
    fresh global epoch) must honestly report observed=0 for every tracked
    pair before any new traffic arrives — even for a pair (BTC) that was
    already carrying traffic under the OLD, now-superseded epoch. Proves
    the snapshot's "since epoch began" claim is actually true end to end,
    not just at the CandleStore-unit level.
    """
    from apex.db.connection import init_db as real_init_db

    settings = Settings(candle_diagnostics_enabled=True)
    timeframes = main._build_timeframes(settings)
    old_symbols = ["BTC", "ETH"]
    old_manager = _manager(old_symbols, timeframes)
    monkeypatch.setattr(old_manager, "stop", AsyncMock(wraps=old_manager.stop))
    monkeypatch.setattr(main, "_ws_manager", old_manager)
    monkeypatch.setattr(main, "_ws_symbols", old_symbols)

    store = CandleStore(real_init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
    store.set_diagnostics_membership({(sym, tf) for sym in old_symbols for tf in timeframes})
    # Pre-swap traffic under the OLD epoch for BTC, which stays in
    # membership across the swap below.
    open_time = 1_800_000_000_000
    store.update(
        "BTC", "3m",
        {"t": open_time, "T": open_time + 179_999, "o": "1", "h": "1", "l": "1", "c": "1", "v": "1"},
        persist=False, now_ms=open_time + 180_000, source="ws",
    )
    assert store.get_diagnostics("BTC", "3m").received_eligible_count == 1
    monkeypatch.setattr(main, "_candle_store", store)
    monkeypatch.setattr(main, "backfill_candles", AsyncMock())
    monkeypatch.setattr(main, "run_universe_refresh", AsyncMock(return_value=["BTC", "ETH", "SOL"]))

    await main.run_universe_ws_sync(object(), AsyncMock(), settings, _FakeApp())

    assert set(main._ws_symbols) == {"BTC", "ETH", "SOL"}  # swap actually succeeded

    with caplog.at_level(logging.INFO, logger="apex.main"):
        main._log_candle_diagnostics_snapshot(settings)

    header = caplog.records[0].getMessage()
    expected_pairs = len(main._ws_symbols) * len(timeframes)
    assert (
        f"selected={expected_pairs} tracked={expected_pairs} observed=0 "
        f"zero_traffic={expected_pairs} allocation_overflow=0"
    ) in header
    for rec in caplog.records[1:]:
        assert "no traffic observed" in rec.getMessage()


async def test_prune_diagnostics_failure_does_not_break_successful_swap(monkeypatch, caplog):
    """A diagnostics-only failure pruning membership after a successful swap
    must never surface as a failed universe/WS refresh — the swap itself
    already succeeded (see F3/requirement 4 containment).
    """
    settings = Settings(candle_diagnostics_enabled=True)
    timeframes = main._build_timeframes(settings)
    old_symbols = ["BTC", "ETH"]
    old_manager = _manager(old_symbols, timeframes)
    monkeypatch.setattr(main, "_ws_manager", old_manager)
    monkeypatch.setattr(main, "_ws_symbols", old_symbols)

    class _BrokenPruneStore(_FakeCandleStore):
        def prune_diagnostics(self, keep: set) -> None:
            raise RuntimeError("prune boom")

    store = _BrokenPruneStore()
    monkeypatch.setattr(main, "_candle_store", store)
    monkeypatch.setattr(main, "backfill_candles", AsyncMock())
    monkeypatch.setattr(main, "run_universe_refresh", AsyncMock(return_value=["BTC", "ETH", "SOL"]))

    with caplog.at_level(logging.DEBUG, logger="apex.main"):
        await main.run_universe_ws_sync(object(), AsyncMock(), settings, _FakeApp())  # must not raise

    assert set(main._ws_symbols) == {"BTC", "ETH", "SOL"}  # swap succeeded despite prune failure
    assert "Candle diagnostics prune failed" in caplog.text


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


# ============================================================================
# Candle-feed diagnostics wiring (bounded, opt-in, default off)
# ============================================================================

# ------------------------------------------- backfill source tagging

async def test_backfill_candles_tags_backfill_source(monkeypatch):
    calls: list[tuple] = []

    class _RecordingStore:
        def update(self, symbol, tf, c, persist=True, source="unknown"):
            calls.append((symbol, tf, source))

    client = AsyncMock()
    client.get_candle_snapshot = AsyncMock(
        return_value=[{"t": 1, "T": 2, "o": "1", "h": "1", "l": "1", "c": "1", "v": "1"}]
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    await main.backfill_candles(client, _RecordingStore(), ["BTC"], ["1m"])

    assert calls == [("BTC", "1m", "backfill")]


# ------------------------------------------- diagnostics_enabled wiring

def test_build_ws_manager_wires_diagnostics_enabled():
    manager = main._build_ws_manager([], Settings(candle_diagnostics_enabled=True))
    assert manager.diagnostics_enabled is True


def test_build_ws_manager_diagnostics_disabled_by_default():
    manager = main._build_ws_manager([], Settings())
    assert manager.diagnostics_enabled is False


# ------------------------------------------- diagnostic membership pruning

async def test_rebuild_prunes_diagnostics_when_enabled(monkeypatch):
    settings = Settings(candle_diagnostics_enabled=True)
    timeframes = main._build_timeframes(settings)
    old_symbols = ["BTC", "ETH"]
    old_manager = _manager(old_symbols, timeframes)
    monkeypatch.setattr(main, "_ws_manager", old_manager)
    monkeypatch.setattr(main, "_ws_symbols", old_symbols)
    store = _FakeCandleStore()
    monkeypatch.setattr(main, "_candle_store", store)
    monkeypatch.setattr(main, "backfill_candles", AsyncMock())
    monkeypatch.setattr(main, "run_universe_refresh", AsyncMock(return_value=["BTC", "ETH", "SOL"]))

    await main.run_universe_ws_sync(object(), AsyncMock(), settings, _FakeApp())

    assert len(store.prune_calls) == 1
    expected = {(sym, tf) for sym in ("BTC", "ETH", "SOL") for tf in timeframes}
    assert store.prune_calls[0] == expected


async def test_rebuild_does_not_prune_diagnostics_when_disabled(monkeypatch):
    old_symbols = ["BTC", "ETH"]
    old_manager = _manager(old_symbols, _timeframes(_settings()))
    monkeypatch.setattr(main, "_ws_manager", old_manager)
    monkeypatch.setattr(main, "_ws_symbols", old_symbols)
    store = _FakeCandleStore()
    monkeypatch.setattr(main, "_candle_store", store)
    monkeypatch.setattr(main, "backfill_candles", AsyncMock())

    await _sync(monkeypatch, ["BTC", "ETH", "SOL"])  # default settings: diagnostics disabled

    assert store.prune_calls == []


async def test_unchanged_membership_does_not_prune_even_when_enabled(monkeypatch):
    """Pruning only happens on an actual rebuild, not on every refresh tick."""
    settings = Settings(candle_diagnostics_enabled=True)
    timeframes = main._build_timeframes(settings)
    symbols = ["BTC", "ETH"]
    monkeypatch.setattr(main, "_ws_manager", _manager(symbols, timeframes))
    monkeypatch.setattr(main, "_ws_symbols", symbols)
    store = _FakeCandleStore()
    monkeypatch.setattr(main, "_candle_store", store)
    monkeypatch.setattr(main, "run_universe_refresh", AsyncMock(return_value=list(symbols)))

    await main.run_universe_ws_sync(object(), AsyncMock(), settings, _FakeApp())

    assert store.prune_calls == []


# ------------------------------------------- periodic candle diagnostics snapshot

class _StubManager:
    def __init__(self, connected: bool, connection_generation: int = 1):
        self.is_connected = connected
        self.connection_generation = connection_generation


def test_logger_identity_is_fixed_apex_main():
    """Must be a fixed "apex.main" identity, not __name__-derived — production
    runs this module via `python -m apex.main`, under which __name__ would be
    "__main__" rather than "apex.main", diverging from every other context
    (tests, `from apex import main`) that imports it normally.
    """
    assert main.logger.name == "apex.main"


async def test_snapshot_job_is_noop_when_disabled(caplog):
    with caplog.at_level(logging.INFO, logger="apex.main"):
        await main.run_candle_diagnostics_snapshot(Settings())
    assert caplog.records == []


def test_snapshot_header_reports_no_manager_and_zero_pairs(monkeypatch, caplog):
    monkeypatch.setattr(main, "_ws_manager", None)
    monkeypatch.setattr(main, "_ws_symbols", [])
    monkeypatch.setattr(main, "_candle_store", None)
    settings = Settings(candle_diagnostics_enabled=True)

    with caplog.at_level(logging.INFO, logger="apex.main"):
        main._log_candle_diagnostics_snapshot(settings)

    assert len(caplog.records) == 1
    line = caplog.records[0].getMessage()
    assert "manager=no_manager" in line
    assert "connection_generation=none" in line
    assert "membership_generation=none" in line
    assert "sample_time_ms=" in line
    assert (
        "selected=0 tracked=0 observed=0 zero_traffic=0 "
        "allocation_overflow=0 emitted=0 output_truncation=0"
    ) in line


def test_snapshot_header_uses_apex_main_logger(monkeypatch, caplog):
    """The header line is emitted under the fixed "apex.main" logger name
    (matching production execution via `python -m apex.main`), not whatever
    __name__ happened to resolve to at import time.
    """
    monkeypatch.setattr(main, "_ws_manager", None)
    monkeypatch.setattr(main, "_ws_symbols", [])
    monkeypatch.setattr(main, "_candle_store", None)
    settings = Settings(candle_diagnostics_enabled=True)

    with caplog.at_level(logging.INFO, logger="apex.main"):
        main._log_candle_diagnostics_snapshot(settings)

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.name == "apex.main"
    assert record.getMessage().startswith("Candle diagnostics snapshot:")


def test_snapshot_reports_disconnected_manager(monkeypatch, caplog):
    monkeypatch.setattr(main, "_ws_manager", _StubManager(connected=False))
    monkeypatch.setattr(main, "_ws_symbols", [])
    monkeypatch.setattr(main, "_candle_store", None)
    settings = Settings(candle_diagnostics_enabled=True)

    with caplog.at_level(logging.INFO, logger="apex.main"):
        main._log_candle_diagnostics_snapshot(settings)

    line = caplog.records[0].getMessage()
    assert "manager=disconnected" in line
    assert "connection_generation=1" in line


def test_snapshot_emits_zero_traffic_pairs_for_selected_membership(monkeypatch, caplog, tmp_path):
    """Zero-message pairs must still appear — no reliance on per-tick throttling."""
    from apex.db.connection import init_db

    monkeypatch.setattr(main, "_ws_manager", _StubManager(connected=True))
    monkeypatch.setattr(main, "_ws_symbols", ["BTC"])
    real_store = CandleStore(init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
    monkeypatch.setattr(main, "_candle_store", real_store)
    settings = Settings(candle_diagnostics_enabled=True)

    with caplog.at_level(logging.INFO, logger="apex.main"):
        main._log_candle_diagnostics_snapshot(settings)

    timeframes = main._build_timeframes(settings)
    assert len(caplog.records) == 1 + len(timeframes)
    header = caplog.records[0].getMessage()
    assert (
        f"selected={len(timeframes)} tracked={len(timeframes)} observed=0 "
        f"zero_traffic={len(timeframes)} allocation_overflow=0 "
        f"emitted={len(timeframes)} output_truncation=0"
    ) in header
    for rec in caplog.records[1:]:
        assert "no traffic observed" in rec.getMessage()


def test_snapshot_honors_pair_and_line_caps(monkeypatch, caplog, tmp_path):
    """Cap enforcement is the store's own job (CandleStore.is_diagnostics_tracked)
    — exercised here against a real, bounded CandleStore rather than a
    store-agnostic positional guess (see test_snapshot_reports_store_unavailable_honestly
    for the separate, genuinely-no-store case).
    """
    from apex.db.connection import init_db as real_init_db

    monkeypatch.setattr(candle_store_module, "MAX_DIAGNOSTIC_PAIRS", 5)
    monkeypatch.setattr(main, "MAX_DIAGNOSTIC_SNAPSHOT_LINES", 3)
    monkeypatch.setattr(main, "_ws_manager", _StubManager(connected=True))
    monkeypatch.setattr(main, "_ws_symbols", [f"SYM{i}" for i in range(10)])
    settings = Settings(
        candle_diagnostics_enabled=True,
        trend_timeframe="1m", setup_timeframe="1m", entry_timeframe="1m",
    )
    assert main._build_timeframes(settings) == ["1m"]  # sanity: exactly one timeframe

    real_store = CandleStore(real_init_db(str(tmp_path / "test.db")), diagnostics_enabled=True)
    real_store.set_diagnostics_membership([(f"SYM{i}", "1m") for i in range(10)])
    monkeypatch.setattr(main, "_candle_store", real_store)

    with caplog.at_level(logging.INFO, logger="apex.main"):
        main._log_candle_diagnostics_snapshot(settings)

    header = caplog.records[0].getMessage()
    assert (
        "selected=10 tracked=5 observed=0 zero_traffic=5 "
        "allocation_overflow=5 emitted=2 output_truncation=3"
    ) in header
    assert len(caplog.records) == 3  # header + 2 pair lines (line budget = 3 - 1)


def test_snapshot_reports_store_unavailable_honestly(monkeypatch, caplog):
    """With no CandleStore to consult at all, the snapshot must report
    honestly (nothing tracked, nothing claimed zero-traffic) rather than
    inventing tracking/zero-traffic via a store-agnostic positional guess —
    see Astra reconciliation: "no false zero-traffic with unavailable
    store/manager".
    """
    monkeypatch.setattr(main, "_ws_manager", _StubManager(connected=True))
    monkeypatch.setattr(main, "_ws_symbols", [f"SYM{i}" for i in range(10)])
    monkeypatch.setattr(main, "_candle_store", None)
    settings = Settings(
        candle_diagnostics_enabled=True,
        trend_timeframe="1m", setup_timeframe="1m", entry_timeframe="1m",
    )

    with caplog.at_level(logging.INFO, logger="apex.main"):
        main._log_candle_diagnostics_snapshot(settings)

    header = caplog.records[0].getMessage()
    assert "membership_generation=none" in header
    assert "membership_epoch_started_ms=none" in header
    assert (
        "selected=10 tracked=0 observed=0 zero_traffic=0 "
        "allocation_overflow=10 emitted=0 output_truncation=0"
    ) in header
    assert len(caplog.records) == 1  # header only — nothing to enumerate without a store


def test_snapshot_header_respects_line_cap(monkeypatch, caplog):
    """The header line itself must obey MAX_DIAGNOSTIC_LINE_CHARS, with
    truncation visibly indicated, just like every pair line."""
    monkeypatch.setattr(
        main, "_ws_manager", _StubManager(connected=True, connection_generation="X" * 1000)
    )
    monkeypatch.setattr(main, "_ws_symbols", [])
    monkeypatch.setattr(main, "_candle_store", None)
    settings = Settings(candle_diagnostics_enabled=True)

    with caplog.at_level(logging.INFO, logger="apex.main"):
        main._log_candle_diagnostics_snapshot(settings)

    header = caplog.records[0].getMessage()
    assert len(header) <= main.MAX_DIAGNOSTIC_LINE_CHARS
    assert "...(truncated)" in header


def test_diagnostics_pair_line_includes_boundaries_sources_and_timestamps():
    """The rendered log line — not just the CandleDiagnostics object — must
    carry enough bounded detail to tell a stale WS stream from a recent
    backfill/preload, per-source, with real receive/eligible boundaries.
    """
    from apex.data.candle_store import CandleDiagnostics

    diag = CandleDiagnostics()
    diag.record_receive(1_000, 1_180_000, False, 1_050, "ws")
    diag.record_receive(1_180_000, 1_360_000, True, 1_180_500, "ws")

    line = main._candle_diagnostics_pair_line("BTC", "3m", diag)

    assert "received_open=1180000" in line
    assert "received_boundary=1360000" in line
    assert "received_at=1180500" in line
    assert "received_source=ws" in line
    assert "eligible_open=1180000" in line
    assert "eligible_boundary=1360000" in line
    assert "eligible_at=1180500" in line
    assert "eligible_source=ws" in line
    assert "rollover=" in line
    assert "ignored_late=" in line
    assert "ws=2@1180500" in line
    assert "backfill=0@-" in line
    assert len(line) <= main.MAX_DIAGNOSTIC_LINE_CHARS


def test_diagnostics_pair_line_distinguishes_receive_and_eligible_sources():
    """received_* and eligible_* can describe different underlying events
    (e.g. a live tick on a still-forming bar vs. this pair's last actual
    close) — including, here, from two different sources entirely.
    """
    from apex.data.candle_store import CandleDiagnostics

    diag = CandleDiagnostics()
    # Eligible (closed) receive from backfill...
    diag.record_receive(1_000, 1_180_000, True, 1_050, "backfill")
    # ...then a later, still-forming (non-eligible) live WS tick for a newer bar.
    diag.record_receive(1_180_000, 1_360_000, False, 2_000, "ws")

    line = main._candle_diagnostics_pair_line("BTC", "3m", diag)

    assert "received_source=ws" in line       # the latest raw receive
    assert "eligible_source=backfill" in line  # the latest *eligible* receive


def test_diagnostics_pair_line_stays_within_cap_for_realistic_mixed_source_values():
    """Realistic 13-digit epoch-ms timestamps and large counters across
    every known source must still fit the 512-char cap (or be explicitly,
    visibly truncated — never silently dropped)."""
    from apex.data.candle_store import CandleDiagnostics

    diag = CandleDiagnostics()
    diag.source_counts = {"ws": 123_456, "backfill": 98_765, "preload": 54_321, "unknown": 12}
    diag.source_last_at_ms = {
        "ws": 1_800_123_456_789,
        "backfill": 1_800_123_456_000,
        "preload": 1_800_123_450_000,
        "unknown": 1_800_123_400_000,
    }
    diag.latest_received_open_time = 1_800_123_456_789
    diag.latest_received_boundary_ms = 1_800_123_540_000
    diag.latest_received_at_ms = 1_800_123_456_800
    diag.latest_received_source = "ws"
    diag.received_eligible_count = 999_999
    diag.received_noneligible_count = 888_888
    diag.latest_eligible_open_time = 1_800_123_360_000
    diag.latest_eligible_boundary_ms = 1_800_123_420_000
    diag.latest_eligible_at_ms = 1_800_123_420_100
    diag.latest_eligible_source = "backfill"
    diag.persist_attempts = 999_999
    diag.persist_successes = 999_998
    diag.persist_failures = 1
    diag.persist_construction_failures = 0
    diag.rollover_observations = 12_345
    diag.ignored_late_partial_count = 6_789

    line = main._candle_diagnostics_pair_line("VERYLONGSYMBOLNAME12", "15m", diag)

    assert len(line) <= main.MAX_DIAGNOSTIC_LINE_CHARS
    # If the cap was actually hit, truncation must be visibly indicated —
    # never a silently dropped field.
    if len(line) == main.MAX_DIAGNOSTIC_LINE_CHARS:
        assert "...(truncated)" in line


def test_diagnostics_pair_line_starts_with_exactly_two_spaces():
    """Requirement: the line must begin with exactly two ASCII spaces
    followed immediately by the sanitized symbol/timeframe — not swallowed
    by whitespace-collapsing sanitization applied to the whole line.
    """
    line = main._candle_diagnostics_pair_line("BTC", "1m", None)
    assert line[:2] == "  "
    assert line[2] != " "
    assert line == "  BTC/1m: no traffic observed"


def test_diagnostics_pair_line_sanitizes_malicious_identifiers():
    line = main._candle_diagnostics_pair_line(
        "BTC\n2026-01-01 [WARNING] apex.main: FORGED\r\tX\x1b[31m", "1m", None
    )
    assert "\n" not in line
    assert "\r" not in line
    assert "\t" not in line
    assert "\x1b" not in line
    # Injection cannot escape or shift the fixed two-space prefix either.
    assert line[:2] == "  "
    assert line[2] != " "
    assert len(line) <= main.MAX_DIAGNOSTIC_LINE_CHARS


def test_diagnostics_pair_line_prefix_preserved_at_realistic_cap_length(monkeypatch):
    """The two-space prefix must survive, and truncation must actually be
    exercised (not just permitted), when the body exceeds
    MAX_DIAGNOSTIC_LINE_CHARS. A realistic body is well under the real
    512-char budget, so this forces a small cap to deterministically drive
    the body over budget rather than merely asserting `len(line) <= cap`.
    """
    from apex.data.candle_store import CandleDiagnostics

    monkeypatch.setattr(main, "MAX_DIAGNOSTIC_LINE_CHARS", 50)

    diag = CandleDiagnostics()
    diag.source_counts = {"ws": 123_456, "backfill": 98_765, "preload": 54_321, "unknown": 12}
    diag.source_last_at_ms = {
        "ws": 1_800_123_456_789,
        "backfill": 1_800_123_456_000,
        "preload": 1_800_123_450_000,
        "unknown": 1_800_123_400_000,
    }
    diag.latest_received_open_time = 1_800_123_456_789
    diag.latest_received_boundary_ms = 1_800_123_540_000
    diag.latest_received_at_ms = 1_800_123_456_800
    diag.latest_received_source = "ws"
    diag.received_eligible_count = 999_999
    diag.received_noneligible_count = 888_888
    diag.latest_eligible_open_time = 1_800_123_360_000
    diag.latest_eligible_boundary_ms = 1_800_123_420_000
    diag.latest_eligible_at_ms = 1_800_123_420_100
    diag.latest_eligible_source = "backfill"
    diag.persist_attempts = 999_999
    diag.persist_successes = 999_998
    diag.persist_failures = 1
    diag.persist_construction_failures = 0
    diag.rollover_observations = 12_345
    diag.ignored_late_partial_count = 6_789

    line = main._candle_diagnostics_pair_line("VERYLONGSYMBOLNAME12", "15m", diag)

    assert line[:2] == "  "
    assert line[2] != " "
    assert len(line) == main.MAX_DIAGNOSTIC_LINE_CHARS
    assert line.endswith("...(truncated)")


async def test_snapshot_job_contains_failures_and_never_raises(monkeypatch, caplog):
    class _BrokenStore:
        def get_diagnostics(self, symbol, timeframe):
            raise RuntimeError("boom")

    monkeypatch.setattr(main, "_ws_manager", _StubManager(connected=True))
    monkeypatch.setattr(main, "_ws_symbols", ["BTC"])
    monkeypatch.setattr(main, "_candle_store", _BrokenStore())
    settings = Settings(candle_diagnostics_enabled=True)

    with caplog.at_level(logging.DEBUG, logger="apex.main"):
        await main.run_candle_diagnostics_snapshot(settings)  # must not raise

    assert "Candle diagnostics snapshot failed" in caplog.text


class _TrackedBrokenStore(_FakeCandleStore):
    """A snapshot store that reports every pair as diagnostics-tracked, so
    run_candle_diagnostics_snapshot's membership/is_diagnostics_tracked
    checks (consulted before get_diagnostics) don't short-circuit the
    snapshot with an unrelated AttributeError, but whose get_diagnostics
    always raises the given exception — so the snapshot-failure path
    deterministically observes exactly that exception.
    """

    def __init__(self, exc: BaseException):
        super().__init__()
        self._exc = exc
        self.diagnostics_membership_generation = 1
        self.diagnostics_membership_epoch_started_ms = 1

    def is_diagnostics_tracked(self, symbol: str, timeframe: str) -> bool:
        return True

    def get_diagnostics(self, symbol: str, timeframe: str):
        raise self._exc


async def test_snapshot_failure_is_warning_visible_at_default_info_level(monkeypatch, caplog):
    """The contained snapshot failure must be visible at the default INFO
    level (not require DEBUG to be enabled), contain the exception type, and
    never leak the exception message or any caller/upstream payload text.
    """

    monkeypatch.setattr(main, "_ws_manager", _StubManager(connected=True))
    monkeypatch.setattr(main, "_ws_symbols", ["BTC"])
    monkeypatch.setattr(
        main,
        "_candle_store",
        _TrackedBrokenStore(ValueError("SECRET_MARKER_DO_NOT_LEAK upstream_payload=xyz")),
    )
    settings = Settings(candle_diagnostics_enabled=True)

    with caplog.at_level(logging.INFO, logger="apex.main"):
        await main.run_candle_diagnostics_snapshot(settings)  # must not raise

    matching = [r for r in caplog.records if "Candle diagnostics snapshot failed" in r.getMessage()]
    assert len(matching) == 1
    record = matching[0]
    assert record.levelno == logging.WARNING
    assert "ValueError" in record.getMessage()
    assert "SECRET_MARKER_DO_NOT_LEAK" not in caplog.text
    assert "upstream_payload" not in caplog.text


async def test_snapshot_failure_sanitizes_hostile_exception_type_name(monkeypatch, caplog):
    """A hostile custom metaclass can make `type(exc).__name__` an unbounded,
    multi-line, control-character-laden, attacker-controlled string instead
    of a normal class name. The WARNING must stay a single line, bounded to
    the fixed label plus at most 64 safe ASCII characters, and must never
    let that value escape the log line or leak embedded secret text.
    """

    class _HostileNameMeta(type):
        @property
        def __name__(cls):
            return (
                "Hostile\n2026-01-01 [CRITICAL] apex.main: "
                "SECRET_MARKER_DO_NOT_LEAK\r\x1b[31m" + "A" * 500
            )

    class _HostileNameError(Exception, metaclass=_HostileNameMeta):
        pass

    monkeypatch.setattr(main, "_ws_manager", _StubManager(connected=True))
    monkeypatch.setattr(main, "_ws_symbols", ["BTC"])
    monkeypatch.setattr(main, "_candle_store", _TrackedBrokenStore(_HostileNameError()))
    settings = Settings(candle_diagnostics_enabled=True)

    with caplog.at_level(logging.INFO, logger="apex.main"):
        await main.run_candle_diagnostics_snapshot(settings)  # must not raise

    matching = [r for r in caplog.records if "Candle diagnostics snapshot failed" in r.getMessage()]
    assert len(matching) == 1
    record = matching[0]
    assert record.levelno == logging.WARNING

    message = record.getMessage()
    assert "\n" not in message
    assert "\r" not in message

    label = "Candle diagnostics snapshot failed: "
    assert message.startswith(label)
    payload = message[len(label) :]
    assert len(payload) <= 64
    assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", payload)
    # The hostile __name__ property fails _safe_exc_type_name's fullmatch
    # check, so the emitted type must be exactly the "unknown" fallback —
    # anything else means this test passed on an unrelated exception.
    assert payload == "unknown"

    assert "SECRET_MARKER_DO_NOT_LEAK" not in caplog.text
    assert "\x1b" not in caplog.text


async def test_snapshot_failure_warning_does_not_change_prune_failure_level(monkeypatch, caplog):
    """Raising the snapshot-failure path to WARNING must not raise the level
    of the shared `_log_diag_error` helper used by unrelated prune/membership
    failures — those stay DEBUG-only.
    """
    settings = Settings(candle_diagnostics_enabled=True)
    timeframes = main._build_timeframes(settings)
    old_symbols = ["BTC", "ETH"]
    old_manager = _manager(old_symbols, timeframes)
    monkeypatch.setattr(main, "_ws_manager", old_manager)
    monkeypatch.setattr(main, "_ws_symbols", old_symbols)

    class _BrokenPruneStore(_FakeCandleStore):
        def prune_diagnostics(self, keep: set) -> None:
            raise RuntimeError("prune boom")

    store = _BrokenPruneStore()
    monkeypatch.setattr(main, "_candle_store", store)
    monkeypatch.setattr(main, "backfill_candles", AsyncMock())
    monkeypatch.setattr(main, "run_universe_refresh", AsyncMock(return_value=["BTC", "ETH", "SOL"]))

    with caplog.at_level(logging.INFO, logger="apex.main"):
        await main.run_universe_ws_sync(object(), AsyncMock(), settings, _FakeApp())

    # Still invisible at INFO — the prune failure was never promoted to WARNING.
    assert "Candle diagnostics prune failed" not in caplog.text


class _FakeScheduler:
    """Captures scheduler.add_job() calls in place of a real AsyncIOScheduler."""

    def __init__(self):
        self.job_ids: list[str] = []

    def add_job(self, func, trigger, **kwargs) -> None:
        self.job_ids.append(kwargs.get("id"))

    def start(self) -> None:
        pass

    def shutdown(self, wait: bool = False) -> None:
        pass


class _FakeUvicornServer:
    """Stands in for uvicorn.Server: no real socket bind/serve loop."""

    def __init__(self, config):
        self.config = config

    async def serve(self) -> None:
        return None


async def _run_startup_with_mocks(
    monkeypatch, tmp_path, *,
    candle_diagnostics_enabled: bool,
    candle_reconciliation_enabled: bool = False,
) -> "_FakeScheduler":
    """Drive the real run() startup path with every external-I/O boundary
    mocked (network refresh/backfill, uvicorn serve, log file setup) so the
    scheduler's actual job registration can be observed directly, rather
    than inferred from source text.
    """
    fake_scheduler = _FakeScheduler()
    monkeypatch.setattr(main, "AsyncIOScheduler", lambda: fake_scheduler)
    monkeypatch.setattr(main, "configure_logging", lambda *a, **kw: None)
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: Settings(
            apex_db_path=str(tmp_path / "apex.db"),
            candle_diagnostics_enabled=candle_diagnostics_enabled,
            candle_reconciliation_enabled=candle_reconciliation_enabled,
        ),
    )
    monkeypatch.setattr(main, "run_universe_refresh", AsyncMock(return_value=["BTC"]))
    monkeypatch.setattr(main, "backfill_candles", AsyncMock())
    monkeypatch.setattr(main.uvicorn, "Server", _FakeUvicornServer)

    await main.run()
    return fake_scheduler


async def test_scheduler_registers_candle_diagnostics_job_when_enabled(monkeypatch, tmp_path):
    """Dynamic replacement for the old source-text guard: actually run
    startup and observe the scheduler's real add_job() calls.
    """
    scheduler = await _run_startup_with_mocks(monkeypatch, tmp_path, candle_diagnostics_enabled=True)
    assert "candle_diagnostics_snapshot" in scheduler.job_ids
    assert scheduler.job_ids.count("candle_diagnostics_snapshot") == 1


async def test_scheduler_omits_candle_diagnostics_job_when_disabled(monkeypatch, tmp_path):
    scheduler = await _run_startup_with_mocks(monkeypatch, tmp_path, candle_diagnostics_enabled=False)
    assert "candle_diagnostics_snapshot" not in scheduler.job_ids
    # The unrelated always-on jobs must still register normally.
    assert set(scheduler.job_ids) >= {"universe_refresh", "signal_scan", "followup_check"}


async def test_startup_sets_diagnostics_membership_before_preload(monkeypatch, tmp_path):
    """The real CandleStore's diagnostic membership must already be
    established (capped, epoch bumped) by the time run() reaches preload —
    not populated implicitly/incidentally by the first observations.
    """
    await _run_startup_with_mocks(monkeypatch, tmp_path, candle_diagnostics_enabled=True)

    timeframes = main._build_timeframes(Settings())
    assert main._candle_store.diagnostics_membership_generation == 1
    assert main._candle_store.diagnostics_membership_epoch_started_ms is not None
    for tf in timeframes:
        assert main._candle_store.is_diagnostics_tracked("BTC", tf) is True


async def test_startup_does_not_set_membership_when_disabled(monkeypatch, tmp_path):
    await _run_startup_with_mocks(monkeypatch, tmp_path, candle_diagnostics_enabled=False)
    assert main._candle_store.diagnostics_enabled is False
    assert main._candle_store.diagnostics_membership_generation == 0


# ============================================================================
# Closed-candle reconciliation: lifecycle token, connection-epoch subclass,
# scheduler wiring, shutdown (diagnostics off throughout — reconciliation is
# an entirely independent feature flag).
# ============================================================================

class _FakeSuperConnect:
    """Stands in for ReconnectingWebSocket._connect: an awaitable coroutine
    function with no socket I/O at all, so _ConnectionEpochWebSocket's own
    override can be tested in isolation.
    """

    def __init__(self):
        self.calls = 0

    async def __call__(self, *_self_and_args, **kwargs):
        self.calls += 1


class TestConnectionEpochWebSocket:
    async def test_epoch_increments_before_each_connect_attempt(self, monkeypatch):
        fake_connect = _FakeSuperConnect()
        monkeypatch.setattr(ReconnectingWebSocket, "_connect", fake_connect)

        manager = main._ConnectionEpochWebSocket(
            url="wss://example.invalid/ws", on_message=main.on_ws_message, subscriptions=[],
        )
        assert manager.connection_attempt_epoch == 0

        await manager._connect()
        assert manager.connection_attempt_epoch == 1
        assert fake_connect.calls == 1

        await manager._connect()
        assert manager.connection_attempt_epoch == 2
        assert fake_connect.calls == 2

    async def test_epoch_increments_even_if_connect_raises(self, monkeypatch):
        async def _raising_connect(*a, **kw):
            raise RuntimeError("handshake failed")

        monkeypatch.setattr(ReconnectingWebSocket, "_connect", _raising_connect)
        manager = main._ConnectionEpochWebSocket(
            url="wss://example.invalid/ws", on_message=main.on_ws_message, subscriptions=[],
        )
        with pytest.raises(RuntimeError):
            await manager._connect()
        assert manager.connection_attempt_epoch == 1  # bumped before the attempt, not after success

    def test_plain_reconnecting_websocket_has_no_connection_attempt_epoch_attribute(self):
        manager = ReconnectingWebSocket(
            url="wss://example.invalid/ws", on_message=main.on_ws_message, subscriptions=[],
        )
        assert not hasattr(manager, "connection_attempt_epoch")

    def test_diagnostics_connection_generation_semantics_unaffected(self, monkeypatch):
        """The pre-existing diagnostics-only connection_generation counter
        (gated by diagnostics_enabled, bumped only inside _connect's real
        handshake path) must be completely untouched by this subclass.
        """
        manager = main._ConnectionEpochWebSocket(
            url="wss://example.invalid/ws", on_message=main.on_ws_message, subscriptions=[],
            diagnostics_enabled=True,
        )
        assert manager.connection_generation == 0
        assert manager.connection_attempt_epoch == 0


class TestBuildWsManagerReconciliationClass:
    def test_uses_epoch_tracking_subclass_when_reconciliation_enabled(self):
        manager = main._build_ws_manager([], Settings(candle_reconciliation_enabled=True))
        assert isinstance(manager, main._ConnectionEpochWebSocket)

    def test_uses_plain_class_when_reconciliation_disabled(self):
        manager = main._build_ws_manager([], Settings(candle_reconciliation_enabled=False))
        assert type(manager) is ReconnectingWebSocket


class TestReconciliationLifecycleToken:
    def test_none_when_shutdown_started(self, monkeypatch):
        monkeypatch.setattr(main, "_shutdown_started", True)
        monkeypatch.setattr(main, "_ws_manager", _StubManager(connected=True))
        monkeypatch.setattr(main, "_ws_symbols", ["BTC"])
        assert main._reconciliation_lifecycle_token("BTC") is None

    def test_none_when_no_manager(self, monkeypatch):
        monkeypatch.setattr(main, "_ws_manager", None)
        assert main._reconciliation_lifecycle_token("BTC") is None

    def test_none_when_disconnected(self, monkeypatch):
        monkeypatch.setattr(main, "_ws_manager", _StubManager(connected=False))
        monkeypatch.setattr(main, "_ws_symbols", ["BTC"])
        assert main._reconciliation_lifecycle_token("BTC") is None

    def test_none_when_symbol_not_subscribed(self, monkeypatch):
        monkeypatch.setattr(main, "_ws_manager", _StubManager(connected=True))
        monkeypatch.setattr(main, "_ws_symbols", ["ETH"])
        assert main._reconciliation_lifecycle_token("BTC") is None

    def test_fails_closed_when_manager_lacks_connection_attempt_epoch(self, monkeypatch):
        """A plain ReconnectingWebSocket (no epoch tracking) must never be
        silently treated as valid — this should never happen in practice
        for a reconciliation-enabled runtime (see _build_ws_manager), but
        the check fails closed regardless.
        """
        monkeypatch.setattr(main, "_ws_manager", _StubManager(connected=True))
        monkeypatch.setattr(main, "_ws_symbols", ["BTC"])
        assert main._reconciliation_lifecycle_token("BTC") is None

    def test_valid_token_for_connected_subscribed_symbol(self, monkeypatch):
        manager = main._ConnectionEpochWebSocket(
            url="wss://example.invalid/ws", on_message=main.on_ws_message, subscriptions=[],
        )
        manager._connected = True
        monkeypatch.setattr(main, "_ws_manager", manager)
        monkeypatch.setattr(main, "_ws_symbols", ["BTC"])
        monkeypatch.setattr(main, "_ws_membership_epoch", 5)

        token = main._reconciliation_lifecycle_token("BTC")

        assert token is not None
        assert token.manager_id == id(manager)
        assert token.connection_attempt_epoch == 0
        assert token.membership_epoch == 5

    async def test_token_changes_across_a_reconnect(self, monkeypatch):
        manager = main._ConnectionEpochWebSocket(
            url="wss://example.invalid/ws", on_message=main.on_ws_message, subscriptions=[],
        )
        manager._connected = True
        monkeypatch.setattr(main, "_ws_manager", manager)
        monkeypatch.setattr(main, "_ws_symbols", ["BTC"])

        token_before = main._reconciliation_lifecycle_token("BTC")
        monkeypatch.setattr(ReconnectingWebSocket, "_connect", _FakeSuperConnect())
        await manager._connect()  # simulates a reconnect attempt
        token_after = main._reconciliation_lifecycle_token("BTC")

        assert token_before != token_after

    def test_token_changes_across_manager_replacement_same_membership(self, monkeypatch):
        """Manager identity alone must invalidate a token across a
        stop-then-replace with a brand-new instance (ABA protection), even
        if every other field happened to coincide.
        """
        manager_a = main._ConnectionEpochWebSocket(
            url="wss://example.invalid/ws", on_message=main.on_ws_message, subscriptions=[],
        )
        manager_a._connected = True
        monkeypatch.setattr(main, "_ws_manager", manager_a)
        monkeypatch.setattr(main, "_ws_symbols", ["BTC"])
        token_a = main._reconciliation_lifecycle_token("BTC")

        manager_b = main._ConnectionEpochWebSocket(
            url="wss://example.invalid/ws", on_message=main.on_ws_message, subscriptions=[],
        )
        manager_b._connected = True
        monkeypatch.setattr(main, "_ws_manager", manager_b)
        token_b = main._reconciliation_lifecycle_token("BTC")

        assert token_a != token_b


class TestMembershipEpochBumpOnRebuild:
    async def test_unchanged_membership_does_not_bump_epoch(self, monkeypatch):
        settings = Settings()
        timeframes = main._build_timeframes(settings)
        symbols = ["BTC", "ETH"]
        monkeypatch.setattr(main, "_ws_manager", _manager(symbols, timeframes))
        monkeypatch.setattr(main, "_ws_symbols", symbols)
        monkeypatch.setattr(main, "_ws_membership_epoch", 3)

        await _sync(monkeypatch, list(symbols))  # identical membership

        assert main._ws_membership_epoch == 3

    async def test_successful_rebuild_bumps_epoch(self, monkeypatch):
        old_symbols = ["BTC", "ETH"]
        old_manager = _manager(old_symbols, _timeframes(_settings()))
        monkeypatch.setattr(main, "_ws_manager", old_manager)
        monkeypatch.setattr(main, "_ws_symbols", old_symbols)
        monkeypatch.setattr(main, "_candle_store", _FakeCandleStore())
        monkeypatch.setattr(main, "backfill_candles", AsyncMock())
        monkeypatch.setattr(main, "_ws_membership_epoch", 3)

        await _sync(monkeypatch, ["BTC", "ETH", "SOL"])

        assert main._ws_membership_epoch == 4

    async def test_replacement_failure_and_recovery_still_bumps_epoch(self, monkeypatch):
        """The epoch bump happens at the top of the rebuild's lock-held
        section, before the old manager is even touched — it must apply
        regardless of how the attempt eventually concludes.
        """
        old_symbols = ["BTC", "ETH"]
        old_manager = _manager(old_symbols, _timeframes(_settings()))
        monkeypatch.setattr(old_manager, "start", MagicMock(return_value=None))
        monkeypatch.setattr(main, "_ws_manager", old_manager)
        monkeypatch.setattr(main, "_ws_symbols", old_symbols)
        monkeypatch.setattr(main, "_candle_store", _FakeCandleStore())
        monkeypatch.setattr(main, "backfill_candles", AsyncMock())
        monkeypatch.setattr(main, "_ws_membership_epoch", 3)

        real_build = main._build_ws_manager

        def build_broken(subs, settings):
            replacement = real_build(subs, settings)
            _break_start(monkeypatch, replacement, "replacement start failed")
            return replacement

        monkeypatch.setattr(main, "_build_ws_manager", build_broken)

        with pytest.raises(RuntimeError, match="replacement start failed"):
            await _sync(monkeypatch, ["BTC", "ETH", "SOL"])

        assert main._ws_membership_epoch == 4


class TestReconciliationSuspendedDuringRebuild:
    async def test_token_is_none_while_backfill_in_progress_even_with_unchanged_manager_state(self, monkeypatch):
        """Correction D / rejected F7 blanket-compliance: a tick whose
        lifecycle token is captured AFTER the epoch bump but WHILE backfill
        is still awaited must be blocked — even though the old, still-
        connected manager and its connection_attempt_epoch have not
        actually changed, so a naive before/after token-equality check alone
        would NOT catch this.
        """
        manager = main._ConnectionEpochWebSocket(
            url="wss://example.invalid/ws", on_message=main.on_ws_message, subscriptions=[],
        )
        manager._connected = True
        monkeypatch.setattr(main, "_ws_manager", manager)
        monkeypatch.setattr(main, "_ws_symbols", ["BTC"])
        monkeypatch.setattr(main, "_candle_store", _FakeCandleStore())

        backfill_started = asyncio.Event()
        backfill_release = asyncio.Event()

        async def slow_backfill(client, store, symbols, timeframes):
            backfill_started.set()
            await backfill_release.wait()

        monkeypatch.setattr(main, "backfill_candles", slow_backfill)
        monkeypatch.setattr(main, "run_universe_refresh", AsyncMock(return_value=["BTC", "ETH"]))

        task = asyncio.create_task(
            main.run_universe_ws_sync(object(), AsyncMock(), _settings(), _FakeApp())
        )
        await backfill_started.wait()

        # Before/after token fields (manager identity, connection_attempt_
        # epoch) are unchanged at this instant — the old manager is still
        # the live one — yet the token must still be refused.
        assert main._reconciliation_lifecycle_token("BTC") is None

        backfill_release.set()
        await task
        # Rebuild completed successfully; suspension itself is lifted
        # afterward (the freshly-swapped-in manager is not yet marked
        # connected in this test, since .start() is a no-op fixture, so a
        # full token still requires is_connected -- but suspension no
        # longer contributes to the None result).
        assert main._reconciliation_suspended is False

    async def test_pending_persist_retry_deferred_while_suspended(self, monkeypatch):
        """The same suspension must gate the persist-only retry path too,
        not merely a fresh REST fetch."""
        monkeypatch.setattr(main, "_reconciliation_suspended", True)
        monkeypatch.setattr(main, "_ws_manager", main._ConnectionEpochWebSocket(
            url="wss://example.invalid/ws", on_message=main.on_ws_message, subscriptions=[],
        ))
        main._ws_manager._connected = True
        monkeypatch.setattr(main, "_ws_symbols", ["BTC"])
        assert main._reconciliation_lifecycle_token("BTC") is None


class TestReconciliationMembershipPruneOnRebuild:
    async def test_rebuild_prunes_reconciliation_membership_when_enabled(self, monkeypatch, tmp_path):
        from apex.db.connection import init_db as real_init_db
        from apex.data.candle_store import CandleStore

        settings = Settings(candle_reconciliation_enabled=True)
        timeframes = main._build_timeframes(settings)
        old_symbols = ["BTC", "ETH"]
        old_manager = _manager(old_symbols, timeframes)
        monkeypatch.setattr(main, "_ws_manager", old_manager)
        monkeypatch.setattr(main, "_ws_symbols", old_symbols)

        store = CandleStore(real_init_db(str(tmp_path / "test.db")), reconciliation_enabled=True)
        store.set_reconciliation_membership({(sym, tf) for sym in old_symbols for tf in timeframes})
        monkeypatch.setattr(main, "_candle_store", store)
        monkeypatch.setattr(main, "backfill_candles", AsyncMock())
        monkeypatch.setattr(main, "run_universe_refresh", AsyncMock(return_value=["BTC", "ETH", "SOL"]))

        await main.run_universe_ws_sync(object(), AsyncMock(), settings, _FakeApp())

        from apex.data.candle_store import SUPPORTED_RECONCILIATION_TIMEFRAMES
        for tf in timeframes:
            if tf in SUPPORTED_RECONCILIATION_TIMEFRAMES:
                assert store.is_reconciliation_tracked("SOL", tf) is True

    async def test_rebuild_does_not_prune_reconciliation_when_disabled(self, monkeypatch):
        old_symbols = ["BTC", "ETH"]
        old_manager = _manager(old_symbols, _timeframes(_settings()))
        monkeypatch.setattr(main, "_ws_manager", old_manager)
        monkeypatch.setattr(main, "_ws_symbols", old_symbols)
        store = _FakeCandleStore()
        monkeypatch.setattr(main, "_candle_store", store)
        monkeypatch.setattr(main, "backfill_candles", AsyncMock())

        await _sync(monkeypatch, ["BTC", "ETH", "SOL"])  # default settings: reconciliation disabled

        # _FakeCandleStore has no prune_reconciliation method at all — if
        # main.py called it unconditionally this would raise AttributeError
        # and fail the test.


class TestReconciliationFeedDownAndRecoveryHistory:
    """Remaining-scope item 3: total feed-down/recovery/retained-history
    regressions for `_feed_down_reconciliation_cleanup` and its NOT being
    invoked on a successful old-manager recovery.
    """

    async def test_recovered_old_feed_retains_reconciliation_membership_and_pending_state(
        self, monkeypatch
    ):
        """A failed replacement that successfully recovers the OLD manager
        must leave the still-live old reconciliation membership (and any
        gap window already tracked for it) completely untouched — pruning
        only ever happens after a confirmed-successful swap, and total
        feed-down cleanup only ever happens when recovery itself also
        fails.
        """
        from apex.db.connection import init_db as real_init_db

        settings = Settings(candle_reconciliation_enabled=True)
        timeframes = _timeframes(settings)
        old_symbols = ["BTC", "ETH"]
        old_manager = _manager(old_symbols, timeframes)
        monkeypatch.setattr(old_manager, "stop", AsyncMock(wraps=old_manager.stop))
        monkeypatch.setattr(old_manager, "start", MagicMock(return_value=None))  # recovery succeeds

        monkeypatch.setattr(main, "_ws_manager", old_manager)
        monkeypatch.setattr(main, "_ws_symbols", old_symbols)

        store = CandleStore(real_init_db(":memory:"), reconciliation_enabled=True)
        store.set_reconciliation_membership({(sym, "1m") for sym in old_symbols})
        # Seed a genuine tracked gap window for BTC/1m so "retained" means
        # more than bare membership — the actual candidate state survives.
        open_time = 1_800_000_000_000
        store.update("BTC", "1m", {"t": open_time, "T": open_time + 59_999, "o": "1", "h": "1", "l": "1", "c": "1", "v": "1"}, persist=False, now_ms=open_time, source="ws")
        store.update("BTC", "1m", {"t": open_time + 120_000, "T": open_time + 179_999, "o": "1", "h": "1", "l": "1", "c": "1", "v": "1"}, persist=False, now_ms=open_time + 120_000, source="ws")
        assert store.get_reconciliation_targets() != []

        monkeypatch.setattr(main, "_candle_store", store)
        monkeypatch.setattr(main, "backfill_candles", AsyncMock())

        real_build = main._build_ws_manager

        def build_broken(subs, settings):
            replacement = real_build(subs, settings)
            _break_start(monkeypatch, replacement, "replacement start failed")
            return replacement

        monkeypatch.setattr(main, "_build_ws_manager", build_broken)
        monkeypatch.setattr(main, "run_universe_refresh", AsyncMock(return_value=["BTC", "ETH", "SOL"]))

        with pytest.raises(RuntimeError, match="replacement start failed"):
            await main.run_universe_ws_sync(object(), AsyncMock(), settings, _FakeApp())

        assert main._ws_manager is old_manager  # recovered, still live
        assert store.is_reconciliation_tracked("BTC", "1m") is True
        assert store.get_reconciliation_targets() != []  # gap window untouched

    async def test_total_feed_down_clears_reconciliation_membership_and_pending_state(
        self, monkeypatch
    ):
        """When BOTH the replacement AND old-manager recovery fail, the
        feed is explicitly down — `_feed_down_reconciliation_cleanup` must
        empty the store's reconciliation membership (and thus any pending
        gap window/persist-retry state), not leave stale targets/pending
        markers for symbols nothing is actually subscribed to any more.
        """
        from apex.db.connection import init_db as real_init_db

        settings = Settings(candle_reconciliation_enabled=True)
        timeframes = _timeframes(settings)
        old_symbols = ["BTC", "ETH"]
        old_manager = _manager(old_symbols, timeframes)
        monkeypatch.setattr(old_manager, "stop", AsyncMock(wraps=old_manager.stop))
        _break_start(monkeypatch, old_manager, "old-manager recovery start failed")

        monkeypatch.setattr(main, "_ws_manager", old_manager)
        monkeypatch.setattr(main, "_ws_symbols", old_symbols)

        store = CandleStore(real_init_db(":memory:"), reconciliation_enabled=True)
        store.set_reconciliation_membership({(sym, "1m") for sym in old_symbols})
        open_time = 1_800_000_000_000
        store.update("BTC", "1m", {"t": open_time, "T": open_time + 59_999, "o": "1", "h": "1", "l": "1", "c": "1", "v": "1"}, persist=False, now_ms=open_time, source="ws")
        store.update("BTC", "1m", {"t": open_time + 120_000, "T": open_time + 179_999, "o": "1", "h": "1", "l": "1", "c": "1", "v": "1"}, persist=False, now_ms=open_time + 120_000, source="ws")
        assert store.get_reconciliation_targets() != []

        monkeypatch.setattr(main, "_candle_store", store)
        monkeypatch.setattr(main, "backfill_candles", AsyncMock())

        real_build = main._build_ws_manager

        def build_broken(subs, settings):
            replacement = real_build(subs, settings)
            _break_start(monkeypatch, replacement, "replacement start failed")
            return replacement

        monkeypatch.setattr(main, "_build_ws_manager", build_broken)
        monkeypatch.setattr(main, "run_universe_refresh", AsyncMock(return_value=["BTC", "ETH", "SOL"]))

        with pytest.raises(RuntimeError, match="old-manager recovery start failed"):
            await main.run_universe_ws_sync(object(), AsyncMock(), settings, _FakeApp())

        assert main._ws_manager is None  # explicitly degraded
        assert store.is_reconciliation_tracked("BTC", "1m") is False
        assert store.get_reconciliation_targets() == []


class TestCancellationDuringFailedReplacementCleanup:
    async def test_cancellation_during_replacement_stop_cleanup_still_releases_suspension(
        self, monkeypatch
    ):
        """A cancellation raised from inside the failed-replacement
        `.stop()` cleanup call (see run_universe_ws_sync's outer
        try/finally docstring: "including a BaseException/cancellation
        raised from deep inside (e.g. during the replacement.stop()
        cleanup below)") must still unconditionally release
        `_reconciliation_suspended` and propagate as genuine cancellation —
        the narrow `except Exception` around that cleanup call must never
        swallow it.
        """
        settings = Settings(candle_reconciliation_enabled=True)
        timeframes = _timeframes(settings)
        old_symbols = ["BTC", "ETH"]
        old_manager = _manager(old_symbols, timeframes)
        monkeypatch.setattr(old_manager, "stop", AsyncMock(wraps=old_manager.stop))
        monkeypatch.setattr(main, "_ws_manager", old_manager)
        monkeypatch.setattr(main, "_ws_symbols", old_symbols)
        monkeypatch.setattr(main, "_candle_store", _FakeCandleStore())
        monkeypatch.setattr(main, "backfill_candles", AsyncMock())
        monkeypatch.setattr(main, "run_universe_refresh", AsyncMock(return_value=["BTC", "ETH", "SOL"]))

        real_build = main._build_ws_manager

        def build_broken(subs, settings):
            replacement = real_build(subs, settings)
            _break_start(monkeypatch, replacement, "replacement start failed")
            monkeypatch.setattr(replacement, "stop", AsyncMock(side_effect=asyncio.CancelledError()))
            return replacement

        monkeypatch.setattr(main, "_build_ws_manager", build_broken)

        assert main._reconciliation_suspended is False
        with pytest.raises(asyncio.CancelledError):
            await main.run_universe_ws_sync(object(), AsyncMock(), settings, _FakeApp())

        assert main._reconciliation_suspended is False  # released despite the cancellation

    async def test_cancellation_while_replacement_stop_cleanup_is_genuinely_blocked(
        self, monkeypatch
    ):
        """Final-gaps pass item 3: unlike the test above (which raises
        CancelledError directly from a mocked `.stop()`, proving only that
        `finally` propagation works), this drives a REAL suspended await
        inside the cleanup call — proving cancellation delivered while
        `run_universe_ws_sync` is genuinely blocked inside
        `replacement.stop()` still unconditionally releases
        `_reconciliation_suspended` and still propagates as cancellation.
        """
        settings = Settings(candle_reconciliation_enabled=True)
        timeframes = _timeframes(settings)
        old_symbols = ["BTC", "ETH"]
        old_manager = _manager(old_symbols, timeframes)
        monkeypatch.setattr(old_manager, "stop", AsyncMock(wraps=old_manager.stop))
        monkeypatch.setattr(main, "_ws_manager", old_manager)
        monkeypatch.setattr(main, "_ws_symbols", old_symbols)
        monkeypatch.setattr(main, "_candle_store", _FakeCandleStore())
        monkeypatch.setattr(main, "backfill_candles", AsyncMock())
        monkeypatch.setattr(main, "run_universe_refresh", AsyncMock(return_value=["BTC", "ETH", "SOL"]))

        entered_cleanup = asyncio.Event()
        release_cleanup = asyncio.Event()

        async def blocked_stop():
            entered_cleanup.set()
            await release_cleanup.wait()  # never set — only cancellation ends this

        real_build = main._build_ws_manager

        def build_broken(subs, settings):
            replacement = real_build(subs, settings)
            _break_start(monkeypatch, replacement, "replacement start failed")
            monkeypatch.setattr(replacement, "stop", AsyncMock(side_effect=blocked_stop))
            return replacement

        monkeypatch.setattr(main, "_build_ws_manager", build_broken)

        assert main._reconciliation_suspended is False
        task = asyncio.create_task(
            main.run_universe_ws_sync(object(), AsyncMock(), settings, _FakeApp())
        )
        try:
            await asyncio.wait_for(entered_cleanup.wait(), timeout=5.0)

            # Still genuinely suspended in the outer try/finally, blocked
            # deep inside replacement.stop() — not yet unwound.
            assert main._reconciliation_suspended is True
            assert task.done() is False

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            assert task.cancelled() is True  # genuine cancellation, not swallowed
            assert main._reconciliation_suspended is False  # outer finally still ran
        finally:
            if not task.done():  # pragma: no cover - defensive cleanup on assertion failure
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task


class TestReconciliationSchedulerWiring:
    async def test_scheduler_registers_reconciliation_job_when_enabled(self, monkeypatch, tmp_path):
        scheduler = await _run_startup_with_mocks(
            monkeypatch, tmp_path, candle_diagnostics_enabled=False, candle_reconciliation_enabled=True,
        )
        assert "candle_reconciliation" in scheduler.job_ids
        assert scheduler.job_ids.count("candle_reconciliation") == 1

    async def test_scheduler_omits_reconciliation_job_when_disabled(self, monkeypatch, tmp_path):
        scheduler = await _run_startup_with_mocks(
            monkeypatch, tmp_path, candle_diagnostics_enabled=False, candle_reconciliation_enabled=False,
        )
        assert "candle_reconciliation" not in scheduler.job_ids

    async def test_disabled_reconciliation_constructs_no_reconciler_and_no_extra_state(self, monkeypatch, tmp_path):
        await _run_startup_with_mocks(
            monkeypatch, tmp_path, candle_diagnostics_enabled=False, candle_reconciliation_enabled=False,
        )
        assert main._candle_reconciler is None
        assert main._candle_store.reconciliation_enabled is False

    async def test_enabled_reconciliation_constructs_reconciler_with_membership(self, monkeypatch, tmp_path):
        await _run_startup_with_mocks(
            monkeypatch, tmp_path, candle_diagnostics_enabled=False, candle_reconciliation_enabled=True,
        )
        assert main._candle_reconciler is not None
        assert main._candle_store.reconciliation_enabled is True
        assert main._candle_store.is_reconciliation_tracked("BTC", "1m") is True


class TestReconciliationTickWrapper:
    async def test_noop_when_reconciler_is_none(self):
        await main.run_candle_reconciliation_tick()  # must not raise

    async def test_noop_when_shutdown_started(self, monkeypatch):
        fake_reconciler = AsyncMock()
        monkeypatch.setattr(main, "_candle_reconciler", fake_reconciler)
        monkeypatch.setattr(main, "_shutdown_started", True)
        await main.run_candle_reconciliation_tick()
        fake_reconciler.tick.assert_not_called()

    async def test_delegates_to_reconciler_tick(self, monkeypatch):
        fake_reconciler = AsyncMock()
        monkeypatch.setattr(main, "_candle_reconciler", fake_reconciler)
        await main.run_candle_reconciliation_tick()
        fake_reconciler.tick.assert_awaited_once()


class TestReconciliationShutdown:
    async def test_run_shutdown_calls_reconciler_stop(self, monkeypatch, tmp_path):
        fake_scheduler = _FakeScheduler()
        monkeypatch.setattr(main, "AsyncIOScheduler", lambda: fake_scheduler)
        monkeypatch.setattr(main, "configure_logging", lambda *a, **kw: None)
        monkeypatch.setattr(
            main, "get_settings",
            lambda: Settings(
                apex_db_path=str(tmp_path / "apex.db"), candle_reconciliation_enabled=True,
            ),
        )
        monkeypatch.setattr(main, "run_universe_refresh", AsyncMock(return_value=["BTC"]))
        monkeypatch.setattr(main, "backfill_candles", AsyncMock())

        stop_spy = AsyncMock()

        class _ServeThenCaptureReconciler:
            def __init__(self, config):
                self.config = config

            async def serve(self):
                # Reconciler exists by now (constructed during startup);
                # patch its stop() before run()'s finally block runs.
                monkeypatch.setattr(main._candle_reconciler, "stop", stop_spy)

        monkeypatch.setattr(main.uvicorn, "Server", _ServeThenCaptureReconciler)

        await main.run()

        stop_spy.assert_awaited_once()
