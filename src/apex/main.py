"""APEX application entrypoint."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any, Optional

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from apex.app import create_app
from apex.config import get_settings
from apex.data.candle_store import CandleStore
from apex.data.hyperliquid_client import HyperliquidClient
from apex.data.reconnecting_ws import ReconnectingWebSocket
from apex.db.connection import init_db
from apex.db import repository as repo
from apex.logging_config import configure_logging
from apex.notifications.pushover_client import PushoverClient
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

    The subscription list is fixed for the life of the process, so the first
    connection's responses are enough to diagnose a rejected subscription.
    Budgets keep a 60s reconnect loop from flooding the journal.
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
        for tf in timeframes:
            try:
                lookback = tf_lookback_ms.get(tf, 12 * 60 * 60 * 1000)
                start = now_ms - lookback
                candles = await client.get_candle_snapshot(symbol, tf, start)
                if not candles:
                    logger.debug(f"No candle snapshot for {symbol}/{tf}")
                    continue

                for c in candles:
                    candle_store.update(symbol, tf, c, persist=True)

                logger.debug(f"Backfilled {len(candles)} candles for {symbol}/{tf}")
            except Exception as e:
                logger.warning(f"Backfill failed for {symbol}/{tf}: {e}")

        await asyncio.sleep(0.1)  # Gentle rate limiting


def build_ws_subscriptions(symbols: list[str], timeframes: list[str]) -> list[dict]:
    """Build Hyperliquid WebSocket subscription objects."""
    subs = []
    for symbol in symbols:
        for tf in timeframes:
            subs.append({"type": "candle", "coin": symbol, "interval": tf})
    return subs


async def run() -> None:
    global _candle_store, _ws_manager

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

    timeframes = [
        settings.trend_timeframe,
        settings.setup_timeframe,
        settings.entry_timeframe,
        "1m",
    ]
    # Deduplicate
    timeframes = list(dict.fromkeys(timeframes))

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
        subs = build_ws_subscriptions(scan_symbols[:30], timeframes)
        _ws_manager = ReconnectingWebSocket(
            url=settings.hyperliquid_ws_url,
            on_message=on_ws_message,
            subscriptions=subs,
        )
        _ws_manager.start()
        logger.info(f"WebSocket started with {len(subs)} subscriptions")

    # Set up scheduler
    scheduler = AsyncIOScheduler()

    # Universe refresh every 15 minutes
    scheduler.add_job(
        run_universe_refresh,
        "interval",
        minutes=15,
        args=[conn, client, settings],
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

    scheduler.start()
    logger.info("Scheduler started")

    repo.log_event(conn, "STARTUP", "APEX started successfully")

    # Create and run FastAPI app
    app = create_app(settings=settings, conn=conn)
    app.state.scheduler = scheduler
    app.state.scan_symbols = scan_symbols

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
        #   1. Stop the scheduler first so no new jobs fire against a closing conn.
        #   2. Stop the WebSocket manager.
        #   3. Log SHUTDOWN while the connection is still open.
        #   4. Close the DB last.
        # Each step is wrapped individually so a failure in one never prevents
        # the rest from running (and never produces an unhandled traceback on exit).
        try:
            scheduler.shutdown(wait=False)
        except Exception as e:
            logger.warning(f"Scheduler shutdown error: {e}")

        try:
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


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(0)


if __name__ == "__main__":
    main()
