"""APEX application entrypoint."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import sys
import time
from typing import Any, Optional

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from apex.app import create_app
from apex.config import Settings, get_settings
from apex.data.candle_store import KNOWN_SOURCES, CandleDiagnostics, CandleStore
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

# Fixed name, not __name__: production runs this module via
# `python -m apex.main`, under which __name__ is "__main__" rather than
# "apex.main" — a dynamic identity would silently diverge between that and
# every other context (tests, `from apex import main`) that imports it
# normally.
logger = logging.getLogger("apex.main")

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

# Candle-feed diagnostics (bounded, local, default-off — see
# settings.candle_diagnostics_enabled). Job is only ever registered when
# enabled; these bounds hold whether or not it ever fires. The per-pair
# allocation cap (MAX_DIAGNOSTIC_PAIRS) lives in and is enforced by
# apex.data.candle_store itself (via CandleStore.is_diagnostics_tracked) —
# this module only ever asks the store, never re-implements the cap.
# MAX_DIAGNOSTIC_LINE_CHARS is a hard per-line cap enforced via
# _sanitize_diag_text (see below), not _sanitize_ws_text — the latter's
# truncation marker is intentionally allowed to land up to 14 chars past
# its own limit (fine for WS ACK/error logging, which has no downstream
# cap) and would silently violate this one.
CANDLE_DIAGNOSTICS_SNAPSHOT_MINUTES = 5
MAX_DIAGNOSTIC_SNAPSHOT_LINES = 120
MAX_DIAGNOSTIC_LINE_CHARS = 512

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


def _log_diag_error(label: str, exc: BaseException) -> None:
    """Fixed-text, non-throwing diagnostics-failure log line.

    Never calls `str(exc)` — it already runs from inside an `except
    Exception` handler around a diagnostics-only step, so it must not be
    able to raise a second exception itself (a hostile/pathological
    `__str__`) or echo caller-supplied payload text into the log. Only a
    fixed label and the exception's type name are rendered; the logging
    call itself is wrapped too, so a broken logger can never escape into
    the real candle/WS processing this guards. Mirrors
    apex.data.candle_store._log_diag_error for the identical rationale.
    """
    try:
        exc_type = type(exc).__name__
    except Exception:  # pragma: no cover - defensive, type() essentially never raises
        exc_type = "unknown"
    try:
        logger.debug(f"Candle diagnostics {label} failed: {exc_type}")
    except Exception:  # pragma: no cover - diagnostics logging must never break the caller
        pass


_SAFE_EXC_TYPE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def _safe_exc_type_name(exc: BaseException) -> str:
    """Bounded, plain-ASCII exception type name, safe to interpolate as-is.

    `type(exc).__name__` looks like a trusted built-in attribute, but a
    hostile custom metaclass can turn it into a property that raises,
    returns a non-string object, or returns attacker-controlled text of
    unbounded length, non-ASCII content, or embedded newlines/control
    characters. None of that may reach a log line, so the raw value is used
    only when it already matches a strict single-token identifier pattern
    (`fullmatch`, so no partial-match/trailing-newline loophole); anything
    else — including the access itself raising — falls back to a fixed
    placeholder.
    """
    try:
        name = type(exc).__name__
    except Exception:
        return "unknown"
    if not isinstance(name, str):
        return "unknown"
    if not _SAFE_EXC_TYPE_NAME_RE.fullmatch(name):
        return "unknown"
    return name


def _log_diag_snapshot_failure(exc: BaseException) -> None:
    """Fixed-text WARNING for a contained run_candle_diagnostics_snapshot failure.

    Deliberately separate from `_log_diag_error` (which stays DEBUG-only, and
    is unaffected by this function): a failed snapshot means an entire
    diagnostics interval produced no output at all, which is worth surfacing
    at the default INFO level, unlike a single prune/membership failure
    elsewhere that leaves the snapshot itself unaffected. Same non-throwing,
    `str(exc)`-free rationale as `_log_diag_error` — only a fixed label and
    the exception's type name are ever rendered, and here the type name is
    additionally passed through `_safe_exc_type_name` so a hostile exception
    type can never inject unbounded, non-ASCII, or multi-line content into
    this WARNING (visible by default, unlike the DEBUG-only paths).
    """
    try:
        exc_type = _safe_exc_type_name(exc)
    except Exception:  # pragma: no cover - defensive, _safe_exc_type_name never raises
        exc_type = "unknown"
    try:
        logger.warning(f"Candle diagnostics snapshot failed: {exc_type}")
    except Exception:  # pragma: no cover - diagnostics logging must never break the caller
        pass


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


def _sanitize_diag_text(value: Any, limit: int) -> str:
    """Diagnostics-only sibling of `_sanitize_ws_text`, for exactly one
    difference: the truncation marker is kept INSIDE `limit` rather than
    appended after it. `_sanitize_ws_text` can return up to
    `limit + len("...(truncated)")` (14) characters — acceptable for WS
    ACK/error log lines, which have no hard downstream cap, but wrong for
    a candle diagnostics line, which must never exceed
    MAX_DIAGNOSTIC_LINE_CHARS end to end. Same whitespace-collapse/
    printable-only cleanup otherwise. Handles a small positive `limit`
    (too small to fit the marker) by falling back to a bare hard
    truncation, still within `limit`, rather than returning a marker
    longer than what was asked for. Every diagnostics-only rendering in
    this module goes through this — the WS ACK/error transport/logging
    path above (`_sanitize_ws_text` and its callers) is untouched.
    """
    text = " ".join(str(value).split())
    text = "".join(ch for ch in text if ch.isprintable())
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    marker = "...(truncated)"
    if len(marker) >= limit:
        return text[:limit]
    return text[: limit - len(marker)] + marker


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
            _candle_store.update(symbol, interval, data, source="ws")
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
                    candle_store.update(symbol, tf, c, persist=True, source="backfill")

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
    _candle_store = CandleStore(conn, diagnostics_enabled=settings.candle_diagnostics_enabled)
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
    desired_ws_symbols = scan_symbols[:30] if scan_symbols else []

    # Establish the diagnostic membership (capped) BEFORE preload/backfill
    # below, so allocation is capped against the intended WS membership
    # rather than creation order. No-op when diagnostics are disabled.
    if settings.candle_diagnostics_enabled:
        _candle_store.set_diagnostics_membership(
            (sym, tf) for sym in desired_ws_symbols for tf in timeframes
        )

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

    # Candle-feed diagnostics snapshot — bounded, local, research-only.
    # Gated here AND inside run_candle_diagnostics_snapshot itself (same
    # defense-in-depth pattern as the jobs above): never registered, and
    # never accumulates diagnostic state, unless explicitly enabled.
    if settings.candle_diagnostics_enabled:
        scheduler.add_job(
            run_candle_diagnostics_snapshot,
            "interval",
            minutes=CANDLE_DIAGNOSTICS_SNAPSHOT_MINUTES,
            args=[settings],
            id="candle_diagnostics_snapshot",
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
        diagnostics_enabled=settings.candle_diagnostics_enabled,
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
            # Diagnostic membership is intentionally NOT expanded for these
            # prospective symbols here — only a confirmed-successful swap
            # (the replace=True prune_diagnostics call below) ever grows
            # membership to include them. Expanding it beforehand would let
            # a failed/aborted rebuild permanently occupy capped allocation
            # slots with candidates that were never actually subscribed;
            # this preload/backfill therefore runs untracked for a symbol
            # not yet in membership (is_diagnostics_tracked is False for it
            # until the swap succeeds) — a known, documented gap, not a bug.
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
            if settings.candle_diagnostics_enabled and _candle_store is not None:
                # Bound diagnostic memory to the current membership rather
                # than accumulating forever across refreshes (no-op if
                # diagnostics are disabled — guarded above regardless). A
                # diagnostics-only failure here must never surface as a
                # failed universe/WS refresh — the swap above already
                # succeeded.
                try:
                    _candle_store.prune_diagnostics(
                        {(sym, tf) for sym in desired_ws_symbols for tf in timeframes}
                    )
                except Exception as e:
                    _log_diag_error("prune", e)

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


def _candle_diagnostics_pair_line(
    symbol: str, timeframe: str, diag: Optional[CandleDiagnostics]
) -> str:
    """Bounded, sanitized single-line rendering of one pair's diagnostics.

    No OHLC/payload content is ever included — only counts, open_time/
    boundary integers, and short source/state labels. `symbol`/`timeframe`
    come from the current WS membership, not raw upstream text, but are
    sanitized defensively anyway, matching every other rendered identifier
    in this module. Only ever called for a pair known to be within the
    current diagnostic allocation (see is_diagnostics_tracked) — `diag is
    None` here means "tracked but nothing observed yet", genuine zero
    traffic, never a dropped/untracked pair (those are counted, not
    enumerated — see _log_candle_diagnostics_snapshot).

    Field legend: `received_open`/`received_boundary` are this pair's
    maximum observed open_time/close-boundary-ms so far — they advance
    only when a newly observed open_time is strictly greater than the one
    already recorded (see CandleDiagnostics), so a late/out-of-order
    redelivery can never regress them. `received_at`/`received_source`
    are different: they describe the single latest arrival and refresh on
    *every* valid receive, including a repeat of the same open_time (a
    still-forming bar ticking) or an older bar arriving after a newer one
    was already seen — so `received_at`/`received_source` can describe an
    older bar than `received_open`/`received_boundary` already reflect,
    not necessarily the same event. `eligible_*` mirrors this shape but
    scoped to eligible/closed receives only: `eligible_open`/
    `eligible_boundary` are the maximum observed *eligible* open_time/
    boundary, and `eligible_at`/`eligible_source` describe that same
    maximum-advancing receive — unlike `received_*`, all four `eligible_*`
    fields always describe one consistent event, since they only ever
    advance together. `persist=
    attempts/successes/failures/construction_failures`: `attempts` counts
    only an actual repository call (== successes + failures always); a
    `Candle()` construction failure is counted separately and never
    reaches — never increments — `attempts`. `sources=[...]` gives the
    same last-seen-ms recency broken down per KNOWN_SOURCES label.
    """
    sym = _sanitize_diag_text(symbol, MAX_WS_FIELD_CHARS)
    tf = _sanitize_diag_text(timeframe, MAX_WS_FIELD_CHARS)
    if diag is None:
        body = f"{sym}/{tf}: no traffic observed"
    else:
        sources = " ".join(
            f"{s}={diag.source_counts.get(s, 0)}@{diag.source_last_at_ms.get(s, '-')}"
            for s in KNOWN_SOURCES
        )
        body = (
            f"{sym}/{tf}: "
            f"received_open={diag.latest_received_open_time} "
            f"received_boundary={diag.latest_received_boundary_ms} "
            f"received_at={diag.latest_received_at_ms} "
            f"received_source={diag.latest_received_source} "
            f"eligible={diag.received_eligible_count} "
            f"noneligible={diag.received_noneligible_count} "
            f"eligible_open={diag.latest_eligible_open_time} "
            f"eligible_boundary={diag.latest_eligible_boundary_ms} "
            f"eligible_at={diag.latest_eligible_at_ms} "
            f"eligible_source={diag.latest_eligible_source} "
            f"persist={diag.persist_attempts}/{diag.persist_successes}/"
            f"{diag.persist_failures}/{diag.persist_construction_failures} "
            f"rollover={diag.rollover_observations} "
            f"ignored_late={diag.ignored_late_partial_count} "
            f"sources=[{sources}]"
        )
    # The 2-space prefix is fixed and applied AFTER sanitizing the body on
    # its own reduced budget, rather than sanitizing "  " + body as one
    # string — _sanitize_diag_text's whitespace-collapse
    # (" ".join(text.split())) would otherwise strip a leading prefix as
    # leading whitespace. Reserving 2 chars from the budget up front keeps
    # the fixed MAX_DIAGNOSTIC_LINE_CHARS total cap exact either way.
    prefix = "  "
    body_budget = max(MAX_DIAGNOSTIC_LINE_CHARS - len(prefix), 0)
    return prefix + _sanitize_diag_text(body, body_budget)


def _log_candle_diagnostics_snapshot(settings: Settings) -> None:
    """Build and emit one bounded candle-feed diagnostics snapshot.

    Read-only over existing module/candle-store state: never touches candle
    eligibility, persistence, subscriptions, or the WS connection itself.
    Always emits a header line — even with no manager, no store, no
    membership, or zero traffic — so a gap in this log is itself
    informative rather than silence masquerading as "nothing to report" (no
    per-tick throttling is involved; this runs purely on the scheduler's
    own interval).

    All counts below (`observed`/`zero_traffic` in particular) describe
    only what was actually recorded since `membership_epoch_started_ms` —
    the wall-clock time of the store's current membership epoch (see
    CandleStore's membership-epoch docstring). Preload/backfill history
    from a prior, since-superseded epoch (e.g. before a symbol was removed
    and later re-added) is never reconstructed or blended into these
    counts.

    Neither a missing WS manager nor a missing candle store is ever
    invented as "zero traffic": with no manager, `_ws_symbols` is not used
    at all (a stale snapshot must never be reported as currently
    subscribed); with no store, nothing is claimed tracked (all `selected`
    pairs fold into `allocation_overflow`, never into `zero_traffic`).

    The desired denominator (current WS-subscribed symbols x configured
    timeframes, `selected`) can exceed both the store's allocation cap and
    the per-snapshot line budget. Reported honestly rather than silently
    dropping the difference:
      - `tracked`: pairs actually within the store's capped allocation.
      - `allocation_overflow`: selected pairs outside that cap, or selected
        at all with no store to consult — never rendered as a "no traffic"
        line, since they were never tracked.
      - `observed`: tracked pairs with at least one recorded receive since
        the current membership epoch began.
      - `zero_traffic`: tracked pairs with no traffic yet this epoch
        (legitimately rendered as "no traffic observed").
      - `emitted`/`output_truncation`: how many tracked pairs actually got a
        rendered line under the MAX_DIAGNOSTIC_SNAPSHOT_LINES budget.
    """
    timeframes = _build_timeframes(settings)

    if _ws_manager is None:
        manager_state = "no_manager"
        connection_generation: Any = "none"
        # No manager: never claim a possibly-stale _ws_symbols snapshot as
        # currently subscribed. Every failure/recovery path that sets
        # _ws_manager to None also clears _ws_symbols, but this stays
        # honest even if that invariant is ever violated elsewhere.
        symbols: list[str] = []
    else:
        symbols = list(_ws_symbols)
        if _ws_manager.is_connected:
            manager_state = "connected"
        else:
            manager_state = "disconnected"
        connection_generation = _ws_manager.connection_generation

    if _candle_store is not None:
        membership_generation: Any = _candle_store.diagnostics_membership_generation
        membership_epoch_started_ms: Any = _candle_store.diagnostics_membership_epoch_started_ms
    else:
        membership_generation = "none"
        membership_epoch_started_ms = "none"

    pairs = [(sym, tf) for sym in symbols for tf in timeframes]
    selected = len(pairs)

    if _candle_store is not None:
        tracked_pairs = [p for p in pairs if _candle_store.is_diagnostics_tracked(*p)]
    else:
        # No store to consult at all: never invent tracking/zero-traffic for
        # a pair nothing can actually confirm — report honestly as
        # untracked (folded into allocation_overflow below) rather than a
        # false positional guess.
        tracked_pairs = []
    tracked = len(tracked_pairs)
    allocation_overflow = selected - tracked

    diags = [
        (_candle_store.get_diagnostics(*p) if _candle_store is not None else None)
        for p in tracked_pairs
    ]
    observed = sum(1 for d in diags if d is not None)
    zero_traffic = tracked - observed

    # The header line itself counts against the total line budget.
    pair_line_budget = max(MAX_DIAGNOSTIC_SNAPSHOT_LINES - 1, 0)
    emitted_pairs = list(zip(tracked_pairs, diags))[:pair_line_budget]
    emitted = len(emitted_pairs)
    output_truncation = tracked - emitted

    sample_time_ms = int(time.time() * 1000)
    header_line = (
        f"Candle diagnostics snapshot: sample_time_ms={sample_time_ms} "
        f"manager={manager_state} connection_generation={connection_generation} "
        f"membership_generation={membership_generation} "
        f"membership_epoch_started_ms={membership_epoch_started_ms} "
        f"selected={selected} tracked={tracked} observed={observed} "
        f"zero_traffic={zero_traffic} allocation_overflow={allocation_overflow} "
        f"emitted={emitted} output_truncation={output_truncation}"
    )
    logger.info(_sanitize_diag_text(header_line, MAX_DIAGNOSTIC_LINE_CHARS))

    for (symbol, tf), diag in emitted_pairs:
        logger.info(_candle_diagnostics_pair_line(symbol, tf, diag))


async def run_candle_diagnostics_snapshot(settings: Settings) -> None:
    """Scheduled entry point for the bounded candle-feed diagnostics snapshot.

    Disabled-mode-safe: returns immediately (and is never registered as a
    scheduler job in the first place — see run()) unless
    settings.candle_diagnostics_enabled is true. Any failure building or
    emitting the snapshot is caught and logged as a fixed-format WARNING
    (visible at the default INFO level — see _log_diag_snapshot_failure) so a
    diagnostics-only bug can never interrupt the scheduler or any other job,
    while still being visible without raising log verbosity.
    """
    if not settings.candle_diagnostics_enabled:
        return
    try:
        _log_candle_diagnostics_snapshot(settings)
    except Exception as e:  # pragma: no cover - diagnostics must never break the scheduler
        _log_diag_snapshot_failure(e)


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(0)


if __name__ == "__main__":
    main()
