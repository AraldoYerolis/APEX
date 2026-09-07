"""APEX application entrypoint."""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import sys
from typing import Any, Optional

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from apex.app import create_app
from apex.config import Settings, get_settings
from apex.data.candle_store import CandleStore
from apex.data.hyperliquid_client import HyperliquidClient
from apex.data.market_universe import get_upstream_symbol
from apex.data.reconnecting_ws import (
    SUBSCRIPTION_COUNT_WARN_THRESHOLD,
    ReconnectingWebSocket,
)
from apex.db.connection import init_db
from apex.db import repository as repo
from apex.logging_config import configure_logging
from apex.notifications.pushover_client import PushoverClient
from apex.opportunity.engine import run_opportunity_scan
from apex.scheduler.tasks import (
    run_followup_check,
    run_observation_evaluation,
    run_signal_scan,
    run_universe_refresh,
)

logger = logging.getLogger(__name__)

# WebSocket diagnostics: upstream payloads are untrusted free text, so cap what
# reaches the log, and cap how often, so a reconnect loop cannot flood journald.
# Per-ACK logging is DEBUG only — the INFO/WARNING signal is the per-connection
# reconciliation in ReconnectingWebSocket, which cannot develop a blind spot.
MAX_WS_PAYLOAD_CHARS = 300
MAX_WS_FIELD_CHARS = 64
WS_ERROR_LOG_BUDGET = 20
IGNORED_WS_CHANNELS = frozenset({"pong"})
# Keys that carry a rejection explanation on a subscriptionResponse.
WS_REJECTION_KEYS = ("error", "message", "reason")

# Module-level state (accessible by tasks)
_candle_store: Optional[CandleStore] = None
_ws_manager: Optional[ReconnectingWebSocket] = None
# The symbols (order-preserving snapshot not required) actually represented
# in _ws_manager's current subscription set — i.e. the last desired_ws_symbols
# a rebuild was applied for. Empty when no manager has been built yet.
_ws_symbols: list[str] = []
# Lazily created so the Lock binds to whichever event loop is actually
# running when it's first acquired (run(), or a test driving these functions
# directly), instead of binding at import time to whatever loop happens to be
# current then. See _get_ws_lifecycle_lock.
_ws_lifecycle_lock: Optional[asyncio.Lock] = None
# Set once shutdown begins, so an in-flight or subsequent universe/WS sync
# job can never start (or finish starting) a replacement WS connection
# after the process has committed to tearing down.
_shutdown_started: bool = False
_ws_log_budget: dict[str, int] = {}


def _sanitize_ws_text(value: Any, limit: int) -> str:
    """Single-line, printable, length-capped rendering of untrusted text.

    Whitespace is collapsed and non-printable characters are dropped so an
    upstream payload cannot forge extra journald lines or emit terminal
    escapes. Every logged fragment of upstream text goes through this.
    """
    text = " ".join(str(value).split())
    text = "".join(ch for ch in text if ch.isprintable())
    if len(text) > limit:
        text = text[:limit] + "...(truncated)"
    return text


def _summarize_ws_payload(data: Any) -> str:
    """Single-line, length-capped rendering of an upstream payload."""
    if isinstance(data, str):
        text = data
    else:
        try:
            text = json.dumps(data, separators=(",", ":"), sort_keys=True, default=repr)
        except RecursionError:
            # repr() would recurse too — describe the shape instead.
            text = f"<deeply nested {type(data).__name__}>"
        except (TypeError, ValueError):
            try:
                text = repr(data)
            except Exception:  # pragma: no cover - defensive
                text = f"<unrenderable {type(data).__name__}>"
    return _sanitize_ws_text(text, MAX_WS_PAYLOAD_CHARS)


def _describe_subscription_response(data: Any) -> str:
    """Pull method/type/coin/interval out of a subscriptionResponse payload.

    Every field is individually sanitized and capped, and the assembled line
    is capped again — the dict path carries the same guarantee as the
    free-text path rather than trusting the payload's shape.
    """
    if not isinstance(data, dict):
        return _summarize_ws_payload(data)

    sub = data.get("subscription")
    if not isinstance(sub, dict):
        return _summarize_ws_payload(data)

    parts = [f"method={_sanitize_ws_text(data.get('method', ''), MAX_WS_FIELD_CHARS)}"]
    for key in ("type", "coin", "interval"):
        if key in sub:
            parts.append(f"{key}={_sanitize_ws_text(sub[key], MAX_WS_FIELD_CHARS)}")
    for key in WS_REJECTION_KEYS:
        if key in data:
            parts.append(f"{key}={_sanitize_ws_text(data[key], MAX_WS_FIELD_CHARS)}")
    return _sanitize_ws_text(" ".join(parts), MAX_WS_PAYLOAD_CHARS)


def _subscription_response_is_rejection(data: Any) -> bool:
    """True when a subscriptionResponse carries a rejection explanation."""
    if not isinstance(data, dict):
        return False
    return any(data.get(key) for key in WS_REJECTION_KEYS)


def _take_ws_log_budget(key: str, limit: int) -> bool:
    """Return True while `key` has budget left, announcing the last one.

    The subscription list can change over the life of the process — a
    universe-triggered WS rebuild (see run_universe_ws_sync) resets this
    budget when that happens, so a budget already exhausted against the old
    list can never hide a rejection introduced by the new one. Absent a
    rebuild, budgets keep a 60s reconnect loop from flooding the journal.
    """
    used = _ws_log_budget.get(key, 0)
    if used >= limit:
        return False
    _ws_log_budget[key] = used + 1
    if used + 1 == limit:
        logger.info(f"WS {key} log budget reached ({limit}); further ones logged at DEBUG")
    return True


def _reset_ws_log_budget() -> None:
    """Clear the diagnostic log budgets (used by tests)."""
    _ws_log_budget.clear()


def _get_ws_lifecycle_lock() -> asyncio.Lock:
    """Return the module's WS lifecycle lock, creating it on first use.

    Created lazily (rather than at module import time) so it always binds to
    whatever event loop is actually running when a rebuild or shutdown first
    acquires it. Guards the WS-manager-touching critical section shared by
    run_universe_ws_sync and shutdown — nothing else (signal scans, other
    scheduler jobs) is affected.
    """
    global _ws_lifecycle_lock
    if _ws_lifecycle_lock is None:
        _ws_lifecycle_lock = asyncio.Lock()
    return _ws_lifecycle_lock


async def on_ws_message(msg: dict) -> None:
    """Handle incoming WebSocket messages."""
    global _candle_store

    channel = msg.get("channel", "")
    data = msg.get("data", {})

    if channel == "candle":
        if _candle_store is None:
            return
        # Hyperliquid candle message: data = {s: symbol, i: interval, ...candle fields}
        symbol = data.get("s", "").upper()
        interval = data.get("i", "")
        if symbol and interval:
            _candle_store.update(symbol, interval, data)
        return

    # Non-candle channels are diagnostics only — they never feed the strategy.
    if channel == "subscriptionResponse":
        detail = _describe_subscription_response(data)
        # Individual ACKs stay at DEBUG: the per-connection reconciliation in
        # ReconnectingWebSocket reports what is *missing*, which is the signal.
        # An explicit rejection is still surfaced immediately, under a budget.
        if _subscription_response_is_rejection(data) and _take_ws_log_budget(
            "subscriptionRejected", WS_ERROR_LOG_BUDGET
        ):
            logger.warning(f"WS subscriptionResponse rejected: {detail}")
        else:
            logger.debug(f"WS subscriptionResponse: {detail}")
        return

    if channel == "error":
        detail = _summarize_ws_payload(data)
        if _take_ws_log_budget("error", WS_ERROR_LOG_BUDGET):
            logger.warning(f"WS upstream error: {detail}")
        else:
            logger.debug(f"WS upstream error: {detail}")
        return

    if channel in IGNORED_WS_CHANNELS:
        logger.debug(f"WS {channel}")
        return

    logger.debug(f"WS unhandled channel '{channel}': {_summarize_ws_payload(data)}")


async def backfill_candles(
    client: HyperliquidClient,
    candle_store: CandleStore,
    symbols: list[str],
    timeframes: list[str],
) -> None:
    """Backfill candle history from Hyperliquid snapshot endpoint."""
    import time

    # How many milliseconds back to fetch per timeframe
    tf_lookback_ms = {
        "1m": 3 * 60 * 60 * 1000,    # 3h
        "3m": 6 * 60 * 60 * 1000,    # 6h
        "5m": 12 * 60 * 60 * 1000,   # 12h
        "15m": 48 * 60 * 60 * 1000,  # 48h
    }

    now_ms = int(time.time() * 1000)

    for symbol in symbols:
        upstream = get_upstream_symbol(symbol)
        for tf in timeframes:
            try:
                lookback = tf_lookback_ms.get(tf, 12 * 60 * 60 * 1000)
                start = now_ms - lookback
                candles = await client.get_candle_snapshot(upstream, tf, start)
                if not candles:
                    logger.debug(f"No candle snapshot for {symbol}/{tf}")
                    continue

                for c in candles:
                    candle_store.update(symbol, tf, c, persist=True)

                logger.debug(f"Backfilled {len(candles)} candles for {symbol}/{tf}")
            except Exception as e:
                logger.warning(f"Backfill failed for {symbol}/{tf}: {e}")

        await asyncio.sleep(0.1)  # Gentle rate limiting


def warn_if_subscription_count_high(count: int) -> bool:
    """Warn when the subscription count is empirically risky.

    Called for every WS manager (re)build — startup and any later
    universe-triggered rebuild — via _activate_ws_manager, not only at
    startup. Observability only: nothing is truncated, blocked, or
    reordered. Returns whether a warning was emitted so the branch is
    directly testable.
    """
    if count <= SUBSCRIPTION_COUNT_WARN_THRESHOLD:
        return False
    logger.warning(
        f"  *** {count} subscriptions exceeds {SUBSCRIPTION_COUNT_WARN_THRESHOLD}: "
        "production has been unstable above this count on a single Hyperliquid "
        "connection (sockets closed early having ACKed only part of the set). "
        "This is an observed threshold, NOT a documented Hyperliquid limit. "
        "Watch the WS subscription reconciliation lines. ***"
    )
    return True


def build_ws_subscriptions(symbols: list[str], timeframes: list[str]) -> list[dict]:
    """Build Hyperliquid WebSocket subscription objects.

    `coin` uses Hyperliquid's canonical upstream spelling (e.g. "kPEPE"),
    not APEX's uppercase internal symbol — see
    apex.data.market_universe.get_upstream_symbol. Subscription count, order,
    and timeframe order are unaffected; only the `coin` value can differ from
    `symbol` for the small class of assets whose casing doesn't match.
    """
    subs = []
    for symbol in symbols:
        upstream = get_upstream_symbol(symbol)
        for tf in timeframes:
            subs.append({"type": "candle", "coin": upstream, "interval": tf})
    return subs


def _build_timeframes(settings: Settings) -> list[str]:
    """Configured candle timeframes, deduplicated, in a stable order."""
    timeframes = [
        settings.trend_timeframe,
        settings.setup_timeframe,
        settings.entry_timeframe,
        "1m",
    ]
    return list(dict.fromkeys(timeframes))


async def run() -> None:
    global _candle_store, _ws_manager, _ws_symbols, _shutdown_started

    settings = get_settings()

    # Configure logging
    configure_logging(
        settings.apex_log_level,
        websockets_log_level=settings.apex_ws_log_level,
    )

    logger.info("=" * 60)
    logger.info("APEX starting up")
    logger.info(f"  env                  = {settings.apex_env}")
    logger.info(f"  db                   = {settings.apex_db_path}")
    logger.info(f"  scan_mode            = {settings.scan_mode}")
    logger.info(f"  account_size         = ${settings.account_size_usd}")
    logger.info(f"  max_risk_per_trade   = ${settings.max_risk_usd:.2f}")
    logger.info(f"  max_daily_loss       = ${settings.max_daily_loss_usd:.2f}")
    logger.info(f"  alerts_enabled       = {settings.alerts_enabled}")
    logger.info(f"  dry_run_mode         = {settings.dry_run_mode}")
    logger.info(f"  alert_types_enabled  = {settings.alert_types_enabled_list or '(none)'}")
    logger.info(
        f"  pushover_configured  = "
        f"{bool(settings.pushover_app_token and settings.pushover_user_key)}"
    )
    logger.info(
        f"  action_links         = "
        f"{'enabled' if settings.apex_public_base_url else 'DISABLED (APEX_PUBLIC_BASE_URL not set)'}"
    )
    if not settings.alerts_enabled:
        logger.warning(
            "  *** ALERTS_ENABLED=false: strategy alerts will be logged but NOT sent ***"
        )
    if settings.dry_run_mode:
        logger.warning(
            "  *** DRY_RUN_MODE=true: suppressed-alert log lines will show [DRY RUN] ***"
        )
    logger.info("=" * 60)

    # Initialize DB
    conn = init_db(settings.apex_db_path)
    logger.info("Database initialized")

    # Initialize components
    client = HyperliquidClient(settings.hyperliquid_info_url)
    _candle_store = CandleStore(conn)
    pushover = PushoverClient(
        app_token=settings.pushover_app_token,
        user_key=settings.pushover_user_key,
        device=settings.pushover_device,
        default_priority=settings.pushover_default_priority,
        sound=settings.pushover_sound,
    )

    # Refresh market universe
    logger.info("Fetching market universe...")
    scan_symbols = await run_universe_refresh(conn, client, settings)

    if not scan_symbols:
        logger.warning("No scan-enabled symbols found. Check config and connectivity.")
        # Allow startup to continue in degraded mode
    else:
        logger.info(f"Scan-enabled symbols ({len(scan_symbols)}): {', '.join(scan_symbols[:15])}"
                    + (f" ... +{len(scan_symbols)-15} more" if len(scan_symbols) > 15 else ""))

    timeframes = _build_timeframes(settings)

    # Preload candles from DB
    logger.info("Preloading candles from DB...")
    for sym in scan_symbols[:20]:  # limit initial load
        for tf in timeframes:
            _candle_store.load_from_db(sym, tf)

    # Backfill from API
    if scan_symbols:
        logger.info(f"Backfilling candle history for {min(len(scan_symbols), 10)} symbols...")
        await backfill_candles(client, _candle_store, scan_symbols[:10], timeframes)

    # Start WebSocket
    if scan_symbols:
        desired_ws_symbols = scan_symbols[:30]
        subs = build_ws_subscriptions(desired_ws_symbols, timeframes)
        _ws_manager = _build_ws_manager(subs, settings)
        _start_ws_manager(_ws_manager)
        _ws_symbols = desired_ws_symbols

    # Create FastAPI app now so the scheduled universe/WS sync job below can
    # update app.state.scan_symbols as the universe changes, not just at
    # startup.
    app = create_app(settings=settings, conn=conn)
    app.state.scan_symbols = scan_symbols

    # Set up scheduler
    scheduler = AsyncIOScheduler()

    # Universe refresh + WS membership sync every 15 minutes
    scheduler.add_job(
        run_universe_ws_sync,
        "interval",
        minutes=15,
        args=[conn, client, settings, app],
        id="universe_refresh",
    )

    # Signal scan every 60 seconds
    scheduler.add_job(
        run_signal_scan,
        "interval",
        seconds=60,
        args=[conn, _candle_store, settings, pushover],
        id="signal_scan",
    )

    # Follow-up check every 5 minutes
    scheduler.add_job(
        run_followup_check,
        "interval",
        minutes=5,
        args=[conn, settings, pushover],
        id="followup_check",
    )

    # Observation evaluation every 60 seconds — dry-run mode only.
    # Gated here so the job is never registered in live-alert mode.
    # run_observation_evaluation also has a defensive early return for safety.
    if settings.dry_run_mode:
        scheduler.add_job(
            run_observation_evaluation,
            "interval",
            seconds=60,
            args=[conn, _candle_store, settings],
            id="observation_evaluation",
        )

    # TA Opportunity Engine v0.1 — additive, research-only (src/apex/opportunity/).
    # Gated here AND inside run_opportunity_scan itself (defense in depth, same
    # pattern as run_observation_evaluation above): never registered unless
    # explicitly enabled AND dry_run_mode is true. Never touches the existing
    # signal/alert path or Pushover.
    if settings.opportunity_engine_enabled and settings.dry_run_mode:
        scheduler.add_job(
            run_opportunity_scan,
            "interval",
            seconds=60,
            args=[conn, _candle_store, settings],
            id="opportunity_scan",
        )

    scheduler.start()
    logger.info("Scheduler started")

    repo.log_event(conn, "STARTUP", "APEX started successfully")

    app.state.scheduler = scheduler

    config = uvicorn.Config(
        app=app,
        host=settings.apex_host,
        port=settings.apex_port,
        log_level=settings.apex_log_level.lower(),
        access_log=False,
    )
    server = uvicorn.Server(config)

    try:
        await server.serve()
    finally:
        # Shutdown order matters:
        #   1. Mark shutdown as begun *before* anything else, so an in-flight
        #      or subsequently-invoked run_universe_ws_sync can never start
        #      (or finish starting) a replacement WS connection afterward.
        #   2. Stop the scheduler so no new jobs fire against a closing conn.
        #   3. Stop the WebSocket manager, holding the same lifecycle lock a
        #      sync rebuild holds — so an in-flight rebuild's DB/WS-touching
        #      critical section finishes (or aborts via the shutdown flag)
        #      before the WS manager is torn down here.
        #   4. Log SHUTDOWN while the connection is still open.
        #   5. Close the DB last.
        # Each step is wrapped individually so a failure in one never prevents
        # the rest from running (and never produces an unhandled traceback on exit).
        _shutdown_started = True

        try:
            scheduler.shutdown(wait=False)
        except Exception as e:
            logger.warning(f"Scheduler shutdown error: {e}")

        try:
            async with _get_ws_lifecycle_lock():
                if _ws_manager:
                    await _ws_manager.stop()
        except Exception as e:
            logger.warning(f"WebSocket shutdown error: {e}")

        try:
            repo.log_event(conn, "SHUTDOWN", "APEX stopped")
        except Exception as e:
            logger.warning(f"Could not write SHUTDOWN event: {e}")

        try:
            from apex.db.connection import close_db
            close_db()
        except Exception as e:
            logger.warning(f"DB close error: {e}")

        logger.info("APEX shutdown complete")


def _build_ws_manager(subs: list[dict], settings: Settings) -> ReconnectingWebSocket:
    """Construct (but do not start) a new WS manager for `subs`.

    Construction has no observable side effect — no task, no network — so a
    rebuild can safely prepare the replacement with this *before* stopping
    the old manager, and only stop the old one once the replacement is
    ready to start. See _start_ws_manager and run_universe_ws_sync.
    """
    return ReconnectingWebSocket(
        url=settings.hyperliquid_ws_url,
        on_message=on_ws_message,
        subscriptions=subs,
    )


def _start_ws_manager(manager: ReconnectingWebSocket) -> None:
    """Start an already-constructed WS manager and run its diagnostics.

    Shared by the initial startup path, every later universe-triggered
    rebuild, and old-manager recovery after a failed rebuild, so
    warn_if_subscription_count_high fires from exactly one call site
    regardless of how many times a WS manager is (re)started over the
    process's life.
    """
    manager.start()
    logger.info(f"WebSocket started with {len(manager.subscriptions)} subscriptions")
    warn_if_subscription_count_high(len(manager.subscriptions))


async def run_universe_ws_sync(
    conn: sqlite3.Connection,
    client: HyperliquidClient,
    settings: Settings,
    app: Any,
) -> None:
    """Refresh the market universe and reconcile the WS manager's membership.

    Registered as the scheduled universe-refresh job in place of calling
    run_universe_refresh() directly. run_universe_refresh's return contract
    is unchanged and untouched here: this only consumes the symbol list it
    already returns and keeps the live WS subscription set — and
    app.state.scan_symbols — in sync with it, instead of both only ever
    reflecting the symbols seen at process startup.

    An empty return from run_universe_refresh() means a refresh failure, not
    "scan nothing" — it must never tear down a healthy WS manager or replace
    a previously-remembered good membership. See run_universe_refresh's
    docstring in apex.scheduler.tasks.
    """
    global _ws_manager, _ws_symbols

    scan_symbols = await run_universe_refresh(conn, client, settings)

    if not scan_symbols:
        logger.warning(
            f"Universe refresh returned no symbols; preserving existing WS "
            f"subscriptions ({len(_ws_symbols)} symbols) in degraded mode"
        )
        return

    app.state.scan_symbols = scan_symbols

    timeframes = _build_timeframes(settings)
    desired_ws_symbols = scan_symbols[:30]
    desired_set = set(desired_ws_symbols)
    current_set = set(_ws_symbols)

    if desired_set == current_set:
        logger.debug(
            f"Universe refresh: WS membership unchanged ({len(desired_set)} symbols)"
        )
        return

    if _shutdown_started:
        logger.info("Shutdown in progress; skipping WS membership rebuild")
        return

    added = sorted(desired_set - current_set)
    removed = sorted(current_set - desired_set)
    logger.info(
        f"WS membership changing: +{len(added)} -{len(removed)} symbols "
        f"(old={len(current_set)} new={len(desired_set)})"
    )

    async with _get_ws_lifecycle_lock():
        if _shutdown_started:
            logger.info("Shutdown started before WS rebuild could proceed; aborting")
            return

        # Preload/backfill newly-added symbols while the OLD manager (if any)
        # is still running, so existing feeds stay live during this work.
        if added and _candle_store is not None:
            for sym in added:
                for tf in timeframes:
                    _candle_store.load_from_db(sym, tf)
            await backfill_candles(client, _candle_store, added, timeframes)

        if _shutdown_started:
            logger.info("Shutdown started during WS resync backfill; aborting rebuild")
            return

        # Prepare the replacement before touching the old manager at all —
        # construction is side-effect-free, so a failure here (or anything
        # above) leaves the old feed completely undisturbed.
        old_manager = _ws_manager
        subs = build_ws_subscriptions(desired_ws_symbols, timeframes)
        replacement = _build_ws_manager(subs, settings)

        if old_manager is not None:
            await old_manager.stop()

        try:
            _start_ws_manager(replacement)
        except Exception as replacement_err:
            logger.error(
                f"WS replacement failed to start "
                f"({len(desired_ws_symbols)} symbols desired): {replacement_err}"
            )
            try:
                await replacement.stop()
            except Exception as cleanup_err:
                logger.debug(f"Replacement cleanup after failed start: {cleanup_err}")

            if old_manager is None:
                # No prior manager and no replacement — explicitly degraded,
                # never falsely "healthy". _ws_symbols must not go on
                # claiming a membership nothing is actually subscribed to.
                _ws_manager = None
                _ws_symbols = []
                logger.error(
                    "WS replacement failed with no prior manager to recover; "
                    "WS feed is DOWN (degraded, no active manager)"
                )
                raise replacement_err

            try:
                # Safe: old_manager.stop() above already completed, so this
                # starts a fresh task on a fully-stopped instance — not a
                # concurrent double-start.
                _start_ws_manager(old_manager)
            except Exception as recovery_err:
                _ws_manager = None
                _ws_symbols = []
                logger.error(
                    "WS old-feed recovery also failed after replacement failure; "
                    f"WS feed is DOWN (degraded, no active manager): "
                    f"replacement_error={replacement_err} recovery_error={recovery_err}"
                )
                raise recovery_err from replacement_err

            # Recovered the old feed: membership stays at the old, still-
            # actually-subscribed set. Do not claim the refresh succeeded —
            # no diagnostics reset, no success log, and the failure still
            # propagates so the scheduler's own error handling sees it.
            _ws_manager = old_manager
            logger.warning(
                f"WS replacement failed; recovered old feed "
                f"({len(_ws_symbols)} symbols) — refresh to "
                f"{len(desired_ws_symbols)} symbols was NOT applied"
            )
            raise replacement_err
        else:
            _ws_manager = replacement
            _ws_symbols = desired_ws_symbols
            _reset_ws_log_budget()

    if not current_set:
        logger.info(
            f"WS manager recovered from degraded/startup state: "
            f"{len(subs)} subscriptions across {len(desired_ws_symbols)} symbols"
        )
    else:
        logger.info(
            f"WS rebuilt: {len(subs)} subscriptions across {len(desired_ws_symbols)} "
            f"symbols (+{len(added)} -{len(removed)})"
        )


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(0)


if __name__ == "__main__":
    main()
