"""Tier 1 WebSocket observability tests.

Covers the diagnostics added so a controlled runtime test can identify *why*
the Hyperliquid feed drops: close code/reason, connection lifetime, message
counts, and upstream non-candle messages (subscriptionResponse / error).

These tests also pin the behaviour this change must NOT alter — backoff
semantics, candle handling, and the default `websockets` logger level.
"""
from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import time

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from apex import main
from apex.data import reconnecting_ws as rws
from apex.data.reconnecting_ws import MAX_CLOSE_REASON_CHARS, ReconnectingWebSocket
from apex.logging_config import configure_logging


async def _noop_on_message(msg: dict) -> None:
    return None


def _make_ws(**kwargs) -> ReconnectingWebSocket:
    return ReconnectingWebSocket(
        url="wss://example.invalid/ws",
        on_message=_noop_on_message,
        subscriptions=[{"type": "candle", "coin": "BTC", "interval": "1m"}],
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _reset_budgets():
    """Diagnostic log budgets are process-global; isolate every test."""
    main._reset_ws_log_budget()
    yield
    main._reset_ws_log_budget()


# ------------------------------------------------------- close-code metadata

def test_describe_close_reports_code_and_reason():
    exc = ConnectionClosedError(Close(1011, "internal error"), None)
    out = ReconnectingWebSocket._describe_close(exc)
    assert "close_code=1011" in out
    assert "internal error" in out
    assert "close_frame=rcvd" in out


def test_describe_close_reports_locally_sent_frame():
    exc = ConnectionClosedError(None, Close(1000, "bye"))
    out = ReconnectingWebSocket._describe_close(exc)
    assert "close_code=1000" in out
    assert "close_frame=sent" in out


def test_describe_close_handles_missing_close_frame():
    """The production failure mode: no close frame received or sent."""
    exc = ConnectionClosedError(None, None)
    out = ReconnectingWebSocket._describe_close(exc)
    assert "close_code=None" in out
    assert "close_frame=none" in out


def test_describe_close_does_not_use_deprecated_accessors(recwarn):
    """.code/.reason are deprecated since websockets 13.1 — must stay unused.

    Otherwise every reconnect emits a DeprecationWarning into the journal.
    """
    ReconnectingWebSocket._describe_close(ConnectionClosedError(Close(1011, "x"), None))
    ReconnectingWebSocket._describe_close(ConnectionClosedError(None, None))
    assert [w for w in recwarn if issubclass(w.category, DeprecationWarning)] == []


def test_describe_close_truncates_long_reason():
    exc = ConnectionClosedError(Close(1008, "x" * 5000), None)
    out = ReconnectingWebSocket._describe_close(exc)
    assert "...(truncated)" in out
    assert len(out) < MAX_CLOSE_REASON_CHARS + 200


def test_describe_close_survives_broken_accessors():
    """A diagnostic accessor must never break the reconnect loop."""

    class Hostile:
        """Stand-in whose frame accessors raise (a future websockets change)."""

        @property
        def rcvd(self):
            raise RuntimeError("boom")

        @property
        def sent(self):
            raise RuntimeError("boom")

    out = ReconnectingWebSocket._describe_close(Hostile())  # type: ignore[arg-type]
    assert "close_code=None" in out
    assert "close_frame=unavailable" in out


# --------------------------------------------- lifetime / message bookkeeping

def test_connection_stats_start_unset():
    ws = _make_ws()
    assert ws.messages_received == 0
    assert ws.connection_age_seconds is None
    assert "lifetime=unknown" in ws._describe_connection()


def test_describe_connection_reports_lifetime_and_count():
    ws = _make_ws()
    ws._connect_started_at = 0.0  # monotonic origin; age will be large but real
    ws._messages_received = 7
    out = ws._describe_connection()
    assert "messages_received=7" in out
    assert "lifetime=unknown" not in out
    assert ws.connection_age_seconds is not None


async def test_run_loop_logs_close_metadata(caplog):
    """A ConnectionClosed is logged with type, close code and connection stats."""
    ws = _make_ws()
    calls = {"n": 0}

    async def fake_connect():
        calls["n"] += 1
        ws._connect_started_at = 0.0
        ws._messages_received = 3
        ws._running = False  # stop after the first failure
        raise ConnectionClosedError(None, None)

    ws._connect = fake_connect  # type: ignore[method-assign]
    ws._running = True

    with caplog.at_level(logging.WARNING, logger="apex.data.reconnecting_ws"):
        await ws._run_loop()

    assert calls["n"] == 1
    msg = caplog.text
    assert "WebSocket error:" in msg          # historical grep pattern preserved
    assert "type=ConnectionClosedError" in msg
    assert "close_frame=none" in msg
    assert "messages_received=3" in msg
    assert "lifetime=" in msg


async def test_run_loop_logs_type_for_generic_exception(caplog):
    ws = _make_ws()

    async def fake_connect():
        ws._running = False
        raise OSError("network unreachable")

    ws._connect = fake_connect  # type: ignore[method-assign]
    ws._running = True

    with caplog.at_level(logging.WARNING, logger="apex.data.reconnecting_ws"):
        await ws._run_loop()

    assert "WebSocket error: network unreachable" in caplog.text
    assert "type=OSError" in caplog.text


# ------------------------------------------------ backoff must NOT be changed

async def test_backoff_semantics_unchanged(monkeypatch):
    """Regression guard: Tier 1 is observability only.

    Backoff still doubles on every failure and never resets on the failure
    path. Fixing that is Tier 2 and deliberately out of scope here.
    """
    ws = _make_ws(max_backoff=60.0)
    sleeps: list[float] = []

    async def fake_connect():
        raise ConnectionClosedError(None, None)

    async def fake_sleep(d):
        sleeps.append(d)
        if len(sleeps) >= 8:
            ws._running = False

    monkeypatch.setattr("apex.data.reconnecting_ws.random.uniform", lambda a, b: 0.0)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    ws._connect = fake_connect  # type: ignore[method-assign]
    ws._running = True

    await ws._run_loop()

    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]


# ------------------------------------------------------ candle path unchanged

class _FakeStore:
    def __init__(self):
        self.updates: list[tuple] = []

    def update(self, symbol, interval, data, persist=False):
        self.updates.append((symbol, interval, data))


async def test_candle_message_still_updates_store(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(main, "_candle_store", store)

    await main.on_ws_message({"channel": "candle", "data": {"s": "btc", "i": "1m", "c": "1"}})

    assert store.updates == [("BTC", "1m", {"s": "btc", "i": "1m", "c": "1"})]


async def test_candle_message_without_store_is_noop(monkeypatch):
    monkeypatch.setattr(main, "_candle_store", None)
    # Must not raise.
    await main.on_ws_message({"channel": "candle", "data": {"s": "BTC", "i": "1m"}})


# ------------------------------------------- non-candle upstream visibility

async def test_plain_subscription_ack_stays_debug(monkeypatch, caplog):
    """A plain ACK is not the signal — the reconciliation report is.

    Logging every ACK at INFO is what produced the budget blind spot; the
    per-connection reconciliation replaces it.
    """
    monkeypatch.setattr(main, "_candle_store", None)
    msg = {
        "channel": "subscriptionResponse",
        "data": {
            "method": "subscribe",
            "subscription": {"type": "candle", "coin": "KPEPE", "interval": "1m"},
        },
    }

    with caplog.at_level(logging.INFO, logger="apex.main"):
        await main.on_ws_message(msg)
    assert caplog.records == []

    with caplog.at_level(logging.DEBUG, logger="apex.main"):
        await main.on_ws_message(msg)
    text = caplog.text
    assert "method=subscribe" in text
    assert "coin=KPEPE" in text
    assert "interval=1m" in text


async def test_rejected_subscription_is_surfaced_at_warning(monkeypatch, caplog):
    """An explicit rejection must be visible without DEBUG logging."""
    monkeypatch.setattr(main, "_candle_store", None)
    msg = {
        "channel": "subscriptionResponse",
        "data": {
            "method": "subscribe",
            "subscription": {"type": "candle", "coin": "KPEPE", "interval": "1m"},
            "error": "Invalid coin KPEPE",
        },
    }

    with caplog.at_level(logging.WARNING, logger="apex.main"):
        await main.on_ws_message(msg)

    assert "rejected" in caplog.text
    assert "coin=KPEPE" in caplog.text
    assert "Invalid coin KPEPE" in caplog.text


# --------------------------------------- bounded/sanitized subscriptionResponse

async def test_subscription_response_dict_path_is_bounded(monkeypatch, caplog):
    """The dict path must obey the same cap as the free-text path."""
    monkeypatch.setattr(main, "_candle_store", None)
    msg = {
        "channel": "subscriptionResponse",
        "data": {
            "method": "subscribe",
            "subscription": {"type": "candle", "coin": "X" * 5000, "interval": "1m"},
            "error": "y" * 5000,
        },
    }

    with caplog.at_level(logging.WARNING, logger="apex.main"):
        await main.on_ws_message(msg)

    rendered = caplog.records[0].getMessage()
    assert len(rendered) <= main.MAX_WS_PAYLOAD_CHARS + 100
    assert "...(truncated)" in rendered


async def test_subscription_response_cannot_forge_log_lines(monkeypatch, caplog):
    """Embedded newlines/control chars must not produce extra journald lines."""
    monkeypatch.setattr(main, "_candle_store", None)
    msg = {
        "channel": "subscriptionResponse",
        "data": {
            "method": "subscribe",
            "subscription": {
                "type": "candle",
                "coin": "X\n2026-01-01 [INFO] apex.main: FORGED\r\tLINE\x1b[31m",
                "interval": "1m",
            },
            "error": "boom\nsecond line",
        },
    }

    with caplog.at_level(logging.WARNING, logger="apex.main"):
        await main.on_ws_message(msg)

    rendered = caplog.records[0].getMessage()
    assert "\n" not in rendered
    assert "\r" not in rendered
    assert "\t" not in rendered
    assert "\x1b" not in rendered


async def test_normal_subscription_payload_stays_readable(monkeypatch, caplog):
    """Bounding must not mangle an ordinary payload."""
    monkeypatch.setattr(main, "_candle_store", None)
    msg = {
        "channel": "subscriptionResponse",
        "data": {
            "method": "subscribe",
            "subscription": {"type": "candle", "coin": "KPEPE", "interval": "15m"},
        },
    }

    with caplog.at_level(logging.DEBUG, logger="apex.main"):
        await main.on_ws_message(msg)

    rendered = caplog.records[0].getMessage()
    assert "method=subscribe type=candle coin=KPEPE interval=15m" in rendered
    assert "truncated" not in rendered


async def test_upstream_error_is_logged_at_warning(monkeypatch, caplog):
    monkeypatch.setattr(main, "_candle_store", None)

    with caplog.at_level(logging.WARNING, logger="apex.main"):
        await main.on_ws_message({"channel": "error", "data": "Invalid subscription"})

    assert "WS upstream error" in caplog.text
    assert "Invalid subscription" in caplog.text
    assert any(r.levelno == logging.WARNING for r in caplog.records)


async def test_error_payload_is_truncated(monkeypatch, caplog):
    monkeypatch.setattr(main, "_candle_store", None)

    with caplog.at_level(logging.WARNING, logger="apex.main"):
        await main.on_ws_message({"channel": "error", "data": "y" * 10_000})

    assert "...(truncated)" in caplog.text
    assert len(caplog.text) < main.MAX_WS_PAYLOAD_CHARS + 500


async def test_non_dict_payloads_do_not_raise(monkeypatch):
    monkeypatch.setattr(main, "_candle_store", None)
    for data in ("plain string", None, 42, ["a", "b"]):
        await main.on_ws_message({"channel": "subscriptionResponse", "data": data})
        await main.on_ws_message({"channel": "error", "data": data})


async def test_pong_and_unknown_channels_stay_debug(monkeypatch, caplog):
    monkeypatch.setattr(main, "_candle_store", None)

    with caplog.at_level(logging.INFO, logger="apex.main"):
        await main.on_ws_message({"channel": "pong"})
        await main.on_ws_message({"channel": "somethingElse", "data": {"x": 1}})

    assert caplog.records == []


async def test_rejection_log_budget_downgrades_to_debug(monkeypatch, caplog):
    """A 60s reconnect loop must not flood the journal with rejection lines."""
    monkeypatch.setattr(main, "_candle_store", None)
    msg = {
        "channel": "subscriptionResponse",
        "data": {
            "method": "subscribe",
            "subscription": {"type": "candle", "coin": "BTC", "interval": "1m"},
            "error": "nope",
        },
    }

    with caplog.at_level(logging.WARNING, logger="apex.main"):
        for _ in range(main.WS_ERROR_LOG_BUDGET + 25):
            await main.on_ws_message(msg)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == main.WS_ERROR_LOG_BUDGET


async def test_deeply_nested_payload_does_not_raise(monkeypatch):
    """RecursionError from json.dumps must be caught, not escape the callback."""
    monkeypatch.setattr(main, "_candle_store", None)
    nested: dict = {}
    cursor = nested
    for _ in range(3000):
        cursor["a"] = {}
        cursor = cursor["a"]

    await main.on_ws_message({"channel": "error", "data": nested})


async def test_error_log_budget_downgrades_to_debug(monkeypatch, caplog):
    monkeypatch.setattr(main, "_candle_store", None)

    with caplog.at_level(logging.WARNING, logger="apex.main"):
        for _ in range(main.WS_ERROR_LOG_BUDGET + 10):
            await main.on_ws_message({"channel": "error", "data": "nope"})

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == main.WS_ERROR_LOG_BUDGET


# --------------------------------------------- websockets logger configuration

def test_websockets_logger_defaults_to_warning(tmp_path):
    logging.getLogger("websockets").setLevel(logging.NOTSET)
    configure_logging("INFO", log_dir=str(tmp_path / "logs"))
    assert logging.getLogger("websockets").level == logging.WARNING


def test_websockets_logger_level_is_configurable(tmp_path):
    configure_logging("INFO", log_dir=str(tmp_path / "logs"), websockets_log_level="DEBUG")
    assert logging.getLogger("websockets").level == logging.DEBUG
    # Restore the production default for any later test.
    configure_logging("INFO", log_dir=str(tmp_path / "logs"))
    assert logging.getLogger("websockets").level == logging.WARNING


def test_invalid_websockets_level_falls_back_to_warning(tmp_path):
    configure_logging("INFO", log_dir=str(tmp_path / "logs"), websockets_log_level="NOPE")
    assert logging.getLogger("websockets").level == logging.WARNING


def test_settings_ws_log_level_defaults_to_warning():
    from apex.config import Settings

    assert Settings().apex_ws_log_level == "WARNING"


def test_settings_ws_log_level_rejects_garbage():
    from apex.config import Settings

    with pytest.raises(ValueError, match="apex_ws_log_level"):
        Settings(apex_ws_log_level="LOUD")


# ============================================================================
# Expected-vs-ACKed subscription reconciliation
# ============================================================================

def _universe_subs() -> list[dict]:
    """The real production universe, in the real symbol-major order.

    KPEPE sits at symbol rank 17 of 20, i.e. subscriptions 65-68 of 80 —
    exactly where the old "first 40 responses at INFO" budget went blind.
    """
    from apex.main import build_ws_subscriptions

    universe = [
        "BTC", "ETH", "SOL", "HYPE", "ZEC", "XRP", "PONS", "LIT", "PUMP", "UNI",
        "XMR", "CASHCAT", "DOGE", "ENA", "ARB", "XPL", "KPEPE", "NEAR", "FARTCOIN", "LINK",
    ]
    return build_ws_subscriptions(universe, ["15m", "5m", "3m", "1m"])


def _ack(sub: dict) -> dict:
    return {
        "channel": "subscriptionResponse",
        "data": {"method": "subscribe", "subscription": dict(sub)},
    }


def test_identity_matches_between_outgoing_sub_and_ack():
    """Reconciliation only works if both sides produce the same identity."""
    sub = {"type": "candle", "coin": "KPEPE", "interval": "3m"}
    ws = _make_ws()
    ws._record_ack(_ack(sub))
    assert ws._acked_subs == {ReconnectingWebSocket._subscription_identity(sub)}
    assert ws._acked_subs == {"KPEPE:3m"}


def test_reconciliation_names_kpepe_despite_late_position(caplog):
    """The finding that sent this back: KPEPE is subscription 65-68 of 80.

    Every other symbol ACKs; KPEPE never does. It must be named explicitly,
    with no dependence on how far into the subscription order it sits.
    """
    subs = _universe_subs()
    assert [i for i, s in enumerate(subs) if s["coin"] == "KPEPE"] == [64, 65, 66, 67]

    ws = ReconnectingWebSocket("wss://x.invalid", _noop_on_message, subs)
    ws._subscriptions_sent = True
    for sub in subs:
        if sub["coin"] != "KPEPE":
            ws._record_ack(_ack(sub))

    with caplog.at_level(logging.WARNING, logger="apex.data.reconnecting_ws"):
        ws._log_subscription_gap()

    line = caplog.records[0].getMessage()
    assert caplog.records[0].levelno == logging.WARNING
    assert "expected=80" in line
    assert "acked=76" in line
    assert "missing=4" in line
    assert "KPEPE(4)" in line


def test_reconciliation_detects_silent_never_acked(caplog):
    """No ACK at all is reported as one fact, not 80 enumerated ones."""
    subs = _universe_subs()
    ws = ReconnectingWebSocket("wss://x.invalid", _noop_on_message, subs)
    ws._subscriptions_sent = True  # sent, but nothing came back

    with caplog.at_level(logging.WARNING, logger="apex.data.reconnecting_ws"):
        ws._log_subscription_gap()

    line = caplog.records[0].getMessage()
    assert "expected=80 acked=0 missing=80" in line
    assert "missing_subs=ALL" in line


def test_reconciliation_reports_clean_connection_at_info(caplog):
    subs = _universe_subs()
    ws = ReconnectingWebSocket("wss://x.invalid", _noop_on_message, subs)
    ws._subscriptions_sent = True
    for sub in subs:
        ws._record_ack(_ack(sub))

    with caplog.at_level(logging.INFO, logger="apex.data.reconnecting_ws"):
        ws._log_subscription_gap()

    rec = caplog.records[0]
    assert rec.levelno == logging.INFO
    assert "missing=0" in rec.getMessage()
    assert "missing_subs=none" in rec.getMessage()


def test_reconciliation_is_silent_before_subscriptions_sent(caplog):
    """A failed handshake must not report a bogus 'everything missing'."""
    ws = ReconnectingWebSocket("wss://x.invalid", _noop_on_message, _universe_subs())
    with caplog.at_level(logging.DEBUG, logger="apex.data.reconnecting_ws"):
        ws._log_subscription_gap()
    assert caplog.records == []


def test_identical_gap_on_reconnect_drops_to_debug(caplog):
    """Reconnect spam guard: the same gap repeated adds no information."""
    subs = _universe_subs()
    ws = ReconnectingWebSocket("wss://x.invalid", _noop_on_message, subs)
    ws._subscriptions_sent = True

    with caplog.at_level(logging.WARNING, logger="apex.data.reconnecting_ws"):
        ws._log_subscription_gap()
        ws._log_subscription_gap()
        ws._log_subscription_gap()

    assert len(caplog.records) == 1  # first at WARNING, repeats at DEBUG


def test_changed_gap_is_reported_again(caplog):
    """Dedupe must not swallow a gap that actually changed."""
    subs = _universe_subs()
    ws = ReconnectingWebSocket("wss://x.invalid", _noop_on_message, subs)
    ws._subscriptions_sent = True

    with caplog.at_level(logging.WARNING, logger="apex.data.reconnecting_ws"):
        ws._log_subscription_gap()               # ALL missing
        for sub in subs:
            if sub["coin"] != "KPEPE":
                ws._record_ack(_ack(sub))
        ws._log_subscription_gap()               # now only KPEPE missing

    assert len(caplog.records) == 2
    assert "missing_subs=ALL" in caplog.records[0].getMessage()
    assert "KPEPE(4)" in caplog.records[1].getMessage()


def test_missing_report_is_bounded_and_keeps_counts():
    """Truncation degrades detail but never hides that subs are missing."""
    missing = {f"COIN{i:04d}LONGNAME:{tf}" for i in range(400) for tf in ("1m", "5m")}
    out = ReconnectingWebSocket._format_missing(missing, expected=len(missing) + 1)
    assert len(out) <= rws.MAX_MISSING_REPORT_CHARS + 100
    assert "more coins" in out


def test_missing_identities_are_sanitized():
    """Symbol names originate upstream, so they are sanitized too."""
    ident = ReconnectingWebSocket._subscription_identity(
        {"type": "candle", "coin": "A\nB\tC", "interval": "1m"}
    )
    assert "\n" not in ident and "\t" not in ident

    long_ident = ReconnectingWebSocket._subscription_identity(
        {"type": "candle", "coin": "Z" * 5000, "interval": "1m"}
    )
    assert len(long_ident) <= rws.MAX_IDENTITY_CHARS + 10


def test_record_ack_ignores_malformed_messages():
    ws = _make_ws()
    for msg in (
        None, "string", 42, [],
        {"channel": "candle", "data": {"s": "BTC"}},
        {"channel": "subscriptionResponse"},
        {"channel": "subscriptionResponse", "data": "oops"},
        {"channel": "subscriptionResponse", "data": {"subscription": "oops"}},
    ):
        ws._record_ack(msg)
    assert ws._acked_subs == set()


# ============================================================================
# Real _connect() bookkeeping — no faked _connect, no hand-set counters
# ============================================================================

class _FakeWS:
    """Minimal stand-in for a websockets client connection."""

    def __init__(self, incoming: list):
        self.incoming = list(incoming)
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def __aiter__(self):
        for item in self.incoming:
            if isinstance(item, Exception):
                raise item
            yield item


class _FakeConnectCM:
    def __init__(self, ws=None, enter_exc=None):
        self.ws = ws
        self.enter_exc = enter_exc

    async def __aenter__(self):
        if self.enter_exc is not None:
            raise self.enter_exc
        return self.ws

    async def __aexit__(self, *exc_info):
        return False


class _FakeWebsocketsModule:
    """Replaces the `websockets` name inside reconnecting_ws only.

    The real library is never patched, so no other test can be affected.
    """

    def __init__(self, outcomes: list):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.connect_kwargs: list[dict] = []

    def connect(self, url, **kwargs):
        self.calls += 1
        self.connect_kwargs.append(dict(kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            return _FakeConnectCM(enter_exc=outcome)
        return _FakeConnectCM(ws=outcome)


def _install_fake_ws(monkeypatch, outcomes: list) -> _FakeWebsocketsModule:
    fake = _FakeWebsocketsModule(outcomes)
    monkeypatch.setattr(rws, "websockets", fake)
    return fake


async def test_connect_sends_every_subscription_and_counts_frames(monkeypatch):
    """The real _connect: subscriptions go out, each frame counted once."""
    subs = [
        {"type": "candle", "coin": "BTC", "interval": "1m"},
        {"type": "candle", "coin": "KPEPE", "interval": "1m"},
    ]
    frames = [json.dumps({"channel": "pong"}) for _ in range(5)]
    fake_ws = _FakeWS(frames)
    _install_fake_ws(monkeypatch, [fake_ws])

    ws = ReconnectingWebSocket("wss://x.invalid", _noop_on_message, subs)
    ws._running = True
    await ws._connect()

    assert len(fake_ws.sent) == 2                      # batching unchanged
    assert json.loads(fake_ws.sent[0])["subscription"] == subs[0]
    assert ws.messages_received == 5                   # exactly once per frame
    assert ws._subscriptions_sent is True
    assert ws.connection_age_seconds is not None


async def test_connect_resets_counters_for_each_connection(monkeypatch):
    """Second connection must not inherit the first connection's counters."""
    subs = [{"type": "candle", "coin": "BTC", "interval": "1m"}]
    first = _FakeWS([json.dumps({"channel": "pong"})] * 4)
    second = _FakeWS([json.dumps({"channel": "pong"})])
    _install_fake_ws(monkeypatch, [first, second])

    ws = ReconnectingWebSocket("wss://x.invalid", _noop_on_message, subs)
    ws._running = True

    await ws._connect()
    assert ws.messages_received == 4
    first_started = ws._connect_started_at

    await ws._connect()
    assert ws.messages_received == 1                   # reset, not accumulated
    assert ws._connect_started_at != first_started


async def test_failed_handshake_does_not_inherit_counters(monkeypatch, caplog):
    """The stale-state bug: a failed attempt must report its own zeroed state."""
    subs = [{"type": "candle", "coin": "BTC", "interval": "1m"}]
    good = _FakeWS([json.dumps({"channel": "pong"})] * 9)
    _install_fake_ws(monkeypatch, [good, OSError("handshake failed")])

    ws = ReconnectingWebSocket("wss://x.invalid", _noop_on_message, subs)
    ws._running = True

    await ws._connect()
    assert ws.messages_received == 9

    with pytest.raises(OSError):
        await ws._connect()

    # Counters belong to the *failed* attempt, not the previous good one.
    assert ws.messages_received == 0
    assert ws._subscriptions_sent is False
    assert "messages_received=0" in ws._describe_connection()


async def test_run_loop_reports_real_counters_from_connect(monkeypatch, caplog):
    """End-to-end: counters in the ConnectionClosed line come from _connect."""
    subs = [{"type": "candle", "coin": "BTC", "interval": "1m"}]
    fake_ws = _FakeWS(
        [json.dumps({"channel": "pong"})] * 3 + [ConnectionClosedError(None, None)]
    )
    _install_fake_ws(monkeypatch, [fake_ws])

    ws = ReconnectingWebSocket("wss://x.invalid", _noop_on_message, subs)
    ws._running = True

    async def stop_after(_d):
        ws._running = False

    monkeypatch.setattr(asyncio, "sleep", stop_after)

    with caplog.at_level(logging.DEBUG, logger="apex.data.reconnecting_ws"):
        await ws._run_loop()

    text = caplog.text
    assert "type=ConnectionClosedError" in text
    assert "close_frame=none" in text
    assert "messages_received=3" in text               # produced by real _connect
    assert "WS subscription reconciliation" in text


async def test_connect_records_acks_and_reconciles_missing(monkeypatch, caplog):
    """Full path: ACKs arrive as frames; the unacked symbol is named."""
    subs = [
        {"type": "candle", "coin": "BTC", "interval": "1m"},
        {"type": "candle", "coin": "ETH", "interval": "1m"},
        {"type": "candle", "coin": "KPEPE", "interval": "1m"},
    ]
    frames = [
        json.dumps(_ack(s)) for s in subs if s["coin"] != "KPEPE"
    ] + [ConnectionClosedError(None, None)]
    _install_fake_ws(monkeypatch, [_FakeWS(frames)])

    ws = ReconnectingWebSocket("wss://x.invalid", _noop_on_message, subs)
    ws._running = True

    async def stop_after(_d):
        ws._running = False

    monkeypatch.setattr(asyncio, "sleep", stop_after)

    with caplog.at_level(logging.WARNING, logger="apex.data.reconnecting_ws"):
        await ws._run_loop()

    gap = [r for r in caplog.records if "reconciliation" in r.getMessage()]
    assert len(gap) == 1
    line = gap[0].getMessage()
    assert "expected=3 acked=2 missing=1" in line
    assert "KPEPE(1)" in line


async def test_connect_still_dispatches_candles(monkeypatch):
    """Candle handling through the real _connect path is unchanged."""
    seen: list[dict] = []

    async def collect(msg: dict) -> None:
        seen.append(msg)

    candle = {"channel": "candle", "data": {"s": "BTC", "i": "1m", "c": "1"}}
    _install_fake_ws(monkeypatch, [_FakeWS([json.dumps(candle)])])

    ws = ReconnectingWebSocket("wss://x.invalid", collect, [])
    ws._running = True
    await ws._connect()

    assert seen == [candle]


async def test_connect_survives_malformed_frames(monkeypatch):
    """Bad JSON and non-dict payloads must not break the receive loop."""
    frames = ["not json at all", json.dumps("a bare string"), json.dumps({"channel": "pong"})]
    _install_fake_ws(monkeypatch, [_FakeWS(frames)])

    ws = ReconnectingWebSocket("wss://x.invalid", _noop_on_message, [])
    ws._running = True
    await ws._connect()

    assert ws.messages_received == 3


# ============================================================================
# Tier 2A: paced subscription sending
# ============================================================================

def _subs(n: int) -> list[dict]:
    """n candle subscriptions with distinguishable, ordered identities."""
    return [{"type": "candle", "coin": f"C{i:03d}", "interval": "1m"} for i in range(n)]


class _RecordingWS(_FakeWS):
    """Fake socket that records send order; optionally fails at an index."""

    def __init__(self, incoming=(), fail_at: int | None = None):
        super().__init__(list(incoming))
        self.fail_at = fail_at

    async def send(self, payload: str) -> None:
        if self.fail_at is not None and len(self.sent) == self.fail_at:
            raise ConnectionResetError("upstream went away mid-subscribe")
        self.sent.append(payload)


def _sent_subs(ws: _RecordingWS) -> list[dict]:
    return [json.loads(p)["subscription"] for p in ws.sent]


async def _run_send(monkeypatch, subs, chunk_size=16, chunk_delay=0.25, fail_at=None):
    """Drive the real _send_subscriptions with a captured sleep."""
    sleeps: list[float] = []

    async def fake_sleep(d):
        sleeps.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    ws = ReconnectingWebSocket(
        "wss://x.invalid", _noop_on_message, subs,
        chunk_size=chunk_size, chunk_delay=chunk_delay,
    )
    fake = _RecordingWS(fail_at=fail_at)
    return ws, fake, sleeps


async def test_paced_send_preserves_order_and_membership(monkeypatch):
    """Pacing must change timing only — never which subs are sent, or their order."""
    subs = _subs(80)
    ws, fake, _ = await _run_send(monkeypatch, subs)
    await ws._send_subscriptions(fake)

    assert _sent_subs(fake) == subs            # identical list, identical order
    assert len(fake.sent) == 80
    assert ws._subscriptions_sent is True
    assert ws._subscriptions_sent_count == 80


async def test_paced_send_payload_shape_unchanged(monkeypatch):
    """The wire payload must be byte-identical to the Tier 1 burst."""
    subs = _subs(3)
    ws, fake, _ = await _run_send(monkeypatch, subs)
    await ws._send_subscriptions(fake)

    expected = [json.dumps({"method": "subscribe", "subscription": s}) for s in subs]
    assert fake.sent == expected


async def test_eighty_subs_produce_four_sleeps(monkeypatch):
    """80 subs / chunk 16 -> 16,sleep,16,sleep,16,sleep,16,sleep,16 (no trailing)."""
    ws, fake, sleeps = await _run_send(monkeypatch, _subs(80))
    await ws._send_subscriptions(fake)

    assert len(fake.sent) == 80
    assert sleeps == [0.25, 0.25, 0.25, 0.25]   # 4, not 5 — none after the last chunk
    assert sum(sleeps) == pytest.approx(1.0)


async def test_no_sleep_when_set_fits_in_one_chunk(monkeypatch):
    for n in (1, 8, 15, 16):
        ws, fake, sleeps = await _run_send(monkeypatch, _subs(n))
        await ws._send_subscriptions(fake)
        assert len(fake.sent) == n
        assert sleeps == [], f"{n} subs should not pace"


async def test_partial_final_chunk_has_no_trailing_sleep(monkeypatch):
    """40 subs -> 16,sleep,16,sleep,8. Two sleeps, none after the short chunk."""
    ws, fake, sleeps = await _run_send(monkeypatch, _subs(40))
    await ws._send_subscriptions(fake)

    assert len(fake.sent) == 40
    assert sleeps == [0.25, 0.25]


async def test_chunk_size_zero_disables_pacing(monkeypatch):
    ws, fake, sleeps = await _run_send(monkeypatch, _subs(80), chunk_size=0)
    await ws._send_subscriptions(fake)
    assert len(fake.sent) == 80
    assert sleeps == []


# ------------------------------------------------- send-failure diagnostics

async def test_send_failure_reports_progress(monkeypatch, caplog):
    """A mid-subscribe failure must say how far it got and on which sub."""
    subs = _subs(80)
    ws, fake, _ = await _run_send(monkeypatch, subs, fail_at=37)

    with caplog.at_level(logging.WARNING, logger="apex.data.reconnecting_ws"):
        with pytest.raises(ConnectionResetError):
            await ws._send_subscriptions(fake)

    line = caplog.records[0].getMessage()
    assert "WebSocket subscribe failed" in line
    assert "type=ConnectionResetError" not in line      # type is rendered inline
    assert "ConnectionResetError" in line
    assert "sent=37/80" in line
    assert "failed_sub_number=38/80" in line            # 1-based: the 38th subscription
    assert "failed_sub=C037:1m" in line                 # the sub that did NOT go out
    assert ws._subscriptions_sent is False              # never claims success
    assert ws._subscriptions_sent_count == 37


async def test_send_failure_line_is_bounded(monkeypatch, caplog):
    """Failure diagnostics stay bounded like every other upstream-derived line."""
    subs = [{"type": "candle", "coin": "Z" * 5000, "interval": "1m"}] * 3
    ws, fake, _ = await _run_send(monkeypatch, subs, fail_at=0)

    with caplog.at_level(logging.WARNING, logger="apex.data.reconnecting_ws"):
        with pytest.raises(ConnectionResetError):
            await ws._send_subscriptions(fake)

    line = caplog.records[0].getMessage()
    assert len(line) < 400
    assert "\n" not in line


async def test_send_failure_propagates_to_run_loop(monkeypatch, caplog):
    """The exception must still reach _run_loop so reconnect logic is unchanged."""
    ws, fake, _ = await _run_send(monkeypatch, _subs(80), fail_at=5)
    _install_fake_ws(monkeypatch, [fake])
    ws._running = True

    async def stop_after(_d):
        ws._running = False

    monkeypatch.setattr(asyncio, "sleep", stop_after)

    with caplog.at_level(logging.WARNING, logger="apex.data.reconnecting_ws"):
        await ws._run_loop()

    text = caplog.text
    assert "WebSocket subscribe failed" in text
    assert "sent=5/80" in text
    assert "WebSocket error:" in text                   # generic handler still fired
    # A partial send must not be reported as a reconciliation gap.
    assert "reconciliation" not in text


# ------------------------------------------- reconciliation re-warning policy

def _gap_ws(subs):
    ws = ReconnectingWebSocket("wss://x.invalid", _noop_on_message, subs)
    ws._subscriptions_sent = True
    return ws


def test_identical_gap_rewarns_after_15_minutes(monkeypatch, caplog):
    """A persistent gap must never fall silent for the life of the process."""
    clock = {"t": 1000.0}
    monkeypatch.setattr("apex.data.reconnecting_ws.time.monotonic", lambda: clock["t"])
    ws = _gap_ws(_universe_subs())

    with caplog.at_level(logging.WARNING, logger="apex.data.reconnecting_ws"):
        ws._log_subscription_gap()                 # t=1000 -> WARNING (first)
        clock["t"] += 60
        ws._log_subscription_gap()                 # +1min  -> DEBUG
        clock["t"] += 60
        ws._log_subscription_gap()                 # +2min  -> DEBUG
        clock["t"] += rws.RECONCILIATION_REWARN_SECONDS
        ws._log_subscription_gap()                 # >15min -> WARNING again

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "missing_subs=ALL" in warnings[0]
    assert "repeats=3" in warnings[1]              # the suppressed occurrences


def test_identical_gap_does_not_spam_before_15_minutes(monkeypatch, caplog):
    clock = {"t": 0.0}
    monkeypatch.setattr("apex.data.reconnecting_ws.time.monotonic", lambda: clock["t"])
    ws = _gap_ws(_universe_subs())

    with caplog.at_level(logging.WARNING, logger="apex.data.reconnecting_ws"):
        for _ in range(60):                        # 60 reconnects over 14 minutes
            ws._log_subscription_gap()
            clock["t"] += 14

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_changed_gap_warns_immediately_without_waiting(monkeypatch, caplog):
    """A changed gap must not be delayed by the re-warn timer."""
    clock = {"t": 0.0}
    monkeypatch.setattr("apex.data.reconnecting_ws.time.monotonic", lambda: clock["t"])
    subs = _universe_subs()
    ws = _gap_ws(subs)

    with caplog.at_level(logging.WARNING, logger="apex.data.reconnecting_ws"):
        ws._log_subscription_gap()                 # ALL missing -> WARNING
        clock["t"] += 5                            # far inside the 15-min window
        for sub in subs:
            if sub["coin"] != "KPEPE":
                ws._record_ack(_ack(sub))
        ws._log_subscription_gap()                 # gap changed -> WARNING now

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "missing_subs=ALL" in warnings[0]
    assert "KPEPE(4)" in warnings[1]
    assert "repeats=" not in warnings[1]           # a change is not a repeat


def test_recovery_to_zero_missing_is_reported(monkeypatch, caplog):
    """Going from a gap to none must surface, not stay hidden as a 'repeat'."""
    clock = {"t": 0.0}
    monkeypatch.setattr("apex.data.reconnecting_ws.time.monotonic", lambda: clock["t"])
    subs = _universe_subs()
    ws = _gap_ws(subs)

    with caplog.at_level(logging.INFO, logger="apex.data.reconnecting_ws"):
        ws._log_subscription_gap()
        clock["t"] += 5
        for sub in subs:
            ws._record_ack(_ack(sub))
        ws._log_subscription_gap()

    assert any(
        r.levelno == logging.INFO and "missing=0" in r.getMessage()
        for r in caplog.records
    )


# --------------------------------------------------- >64 observability guard

def test_warn_helper_fires_above_threshold(caplog):
    """Behavioural: the real production branch must emit a WARNING."""
    with caplog.at_level(logging.DEBUG, logger="apex.main"):
        emitted = main.warn_if_subscription_count_high(80)

    assert emitted is True
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1                      # kills "demoted to DEBUG"
    assert "80 subscriptions exceeds 64" in warnings[0].getMessage()


def test_warn_helper_silent_at_and_below_threshold(caplog):
    with caplog.at_level(logging.DEBUG, logger="apex.main"):
        for count in (0, 1, 40, 63, 64):
            assert main.warn_if_subscription_count_high(count) is False
    assert caplog.records == []                    # kills "branch disabled"


def test_warn_helper_fires_at_sixty_five(caplog):
    """65 is the first count that warns — the boundary must be exact."""
    with caplog.at_level(logging.WARNING, logger="apex.main"):
        assert main.warn_if_subscription_count_high(65) is True
    assert "65 subscriptions exceeds 64" in caplog.text


def test_warn_helper_wording_is_honest(caplog):
    """The threshold is empirical; the log must never claim otherwise."""
    with caplog.at_level(logging.WARNING, logger="apex.main"):
        main.warn_if_subscription_count_high(80)
    msg = caplog.records[0].getMessage()
    assert "NOT a documented Hyperliquid limit" in msg
    assert "observed threshold" in msg


def test_warn_helper_does_not_touch_the_subscription_list(caplog):
    """Observability only: the guard must not truncate or reorder anything."""
    subs = main.build_ws_subscriptions([f"S{i}" for i in range(20)], ["15m", "5m", "3m", "1m"])
    before = [dict(s) for s in subs]
    with caplog.at_level(logging.WARNING, logger="apex.main"):
        main.warn_if_subscription_count_high(len(subs))
    assert subs == before
    assert len(subs) == 80


def test_warn_fires_once_per_startup(caplog):
    """It lives on the startup path, not per-reconnect."""
    src = pathlib.Path(main.__file__).read_text()
    assert src.count("warn_if_subscription_count_high(") == 2   # def + one call site
    run_body = src[src.index("async def run()"):]
    assert run_body.count("warn_if_subscription_count_high(") == 1


# ------------------------------------- pacing must not disturb reconciliation

async def test_reconciliation_still_identifies_missing_after_paced_send(monkeypatch):
    """End-to-end: paced send, partial ACKs, gap still named correctly."""
    subs = _universe_subs()
    frames = [json.dumps(_ack(s)) for s in subs if s["coin"] != "KPEPE"]
    frames.append(ConnectionClosedError(None, None))

    sleeps: list[float] = []

    async def fake_sleep(d):
        sleeps.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    _install_fake_ws(monkeypatch, [_FakeWS(frames)])

    ws = ReconnectingWebSocket("wss://x.invalid", _noop_on_message, subs)
    ws._running = True
    with pytest.raises(ConnectionClosedError):
        await ws._connect()

    assert ws._subscriptions_sent is True
    assert ws._subscriptions_sent_count == 80
    assert sleeps == [0.25, 0.25, 0.25, 0.25]
    expected = ws._expected_identities()
    assert sorted(expected - ws._acked_subs) == [
        "KPEPE:15m", "KPEPE:1m", "KPEPE:3m", "KPEPE:5m",
    ]


# ============================================================================
# Tier 2A corrections
# ============================================================================

# ------------------------------------------- F3: receive-queue confound removed

async def test_connect_raises_receive_queue_limit(monkeypatch):
    """The paced send window must not starve the socket reader.

    websockets defaults to max_queue=16, which would pause TCP reading part-way
    through the ~1s pacing window and apply backpressure upstream — a
    slow-consumer disconnect indistinguishable from the ceiling under test.
    """
    fake = _install_fake_ws(monkeypatch, [_FakeWS([])])
    ws = ReconnectingWebSocket("wss://x.invalid", _noop_on_message, _subs(80))
    ws._running = True

    async def no_sleep(_d):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    await ws._connect()

    assert fake.calls == 1
    kwargs = fake.connect_kwargs[0]
    assert kwargs["max_queue"] == rws.WS_RECEIVE_QUEUE_SIZE
    assert kwargs["max_queue"] == 512
    # Headroom for ~160 startup frames (a response + a candle per subscription).
    assert kwargs["max_queue"] > 2 * len(ws.subscriptions)


async def test_connect_leaves_other_connect_params_untouched(monkeypatch):
    """Only max_queue is added — ping/size behaviour must be unchanged."""
    fake = _install_fake_ws(monkeypatch, [_FakeWS([])])
    ws = ReconnectingWebSocket("wss://x.invalid", _noop_on_message, [])
    ws._running = True
    await ws._connect()

    kwargs = fake.connect_kwargs[0]
    assert kwargs["ping_interval"] == 20
    assert kwargs["ping_timeout"] == 10
    assert "max_size" not in kwargs
    assert set(kwargs) == {"ping_interval", "ping_timeout", "max_queue"}


# ------------------------------------ F1: hostile exception text stays bounded

async def test_subscribe_failure_bounds_hostile_exception_text(monkeypatch, caplog):
    """A ConnectionClosed renders the peer's close reason — bound it.

    This fails against the pre-correction implementation, which interpolated
    the exception raw (measured: 5149 chars with an embedded newline).
    """
    hostile = ConnectionClosedError(
        Close(1008, "R" * 5000 + "\nWARNING apex.main: FORGED LINE\r\t\x1b[31m"), None
    )

    class _HostileWS(_FakeWS):
        async def send(self, payload):
            raise hostile

    monkeypatch.setattr(asyncio, "sleep", lambda _d: asyncio.sleep(0))
    ws = ReconnectingWebSocket("wss://x.invalid", _noop_on_message, _subs(80))

    with caplog.at_level(logging.WARNING, logger="apex.data.reconnecting_ws"):
        with pytest.raises(ConnectionClosedError):
            await ws._send_subscriptions(_HostileWS([]))

    line = caplog.records[0].getMessage()
    assert len(line) < 500, f"unbounded: {len(line)} chars"
    assert "\n" not in line
    assert "\r" not in line
    assert "\t" not in line
    assert "\x1b" not in line
    assert "FORGED LINE" not in line.split("|")[0] or "...(truncated)" in line
    # Diagnostics survive the sanitization.
    assert "ConnectionClosedError" in line
    assert "sent=0/80" in line
    assert "failed_sub_number=1/80" in line


def test_sanitize_text_helper_is_bounded_and_single_line():
    out = rws._sanitize_text("A\nB\r\tC\x00\x1b[31m" + "Z" * 5000, rws.MAX_EXCEPTION_CHARS)
    assert len(out) <= rws.MAX_EXCEPTION_CHARS + 20
    assert "\n" not in out and "\r" not in out and "\x1b" not in out
    assert "...(truncated)" in out


# ------------------------- F5: unambiguous failing subscription number

async def test_failure_reports_human_readable_subscription_number(monkeypatch, caplog):
    """`failed_sub_number=65/80` — the 65th subscription, not a bare index."""
    subs = _universe_subs()
    ws, fake, _ = await _run_send(monkeypatch, subs, fail_at=64)

    with caplog.at_level(logging.WARNING, logger="apex.data.reconnecting_ws"):
        with pytest.raises(ConnectionResetError):
            await ws._send_subscriptions(fake)

    line = caplog.records[0].getMessage()
    assert "sent=64/80" in line
    assert "failed_sub_number=65/80" in line          # 1-based, unambiguous
    assert "failed_at_index" not in line              # the ambiguous form is gone
    assert "failed_sub=KPEPE:15m" in line             # subs[64] is KPEPE:15m
    assert subs[64] == {"type": "candle", "coin": "KPEPE", "interval": "15m"}


# ---------------------------- F4: send progress on the close diagnostic

async def test_close_diagnostic_reports_send_progress(monkeypatch, caplog):
    """A socket that dies mid-send suppresses reconciliation — subs_sent explains why."""
    subs = _subs(80)
    fake = _RecordingWS(fail_at=64)
    _install_fake_ws(monkeypatch, [fake])

    ws = ReconnectingWebSocket("wss://x.invalid", _noop_on_message, subs)
    ws._running = True

    async def stop_after(_d):
        ws._running = False

    monkeypatch.setattr(asyncio, "sleep", stop_after)

    with caplog.at_level(logging.WARNING, logger="apex.data.reconnecting_ws"):
        await ws._run_loop()

    text = caplog.text
    assert "subs_sent=64/80" in text                  # the number that explains it
    assert "messages_received=0" in text              # no longer misleading on its own
    assert "reconciliation" not in text               # correctly suppressed


def test_subs_sent_accurate_before_any_send():
    ws = ReconnectingWebSocket("wss://x.invalid", _noop_on_message, _subs(80))
    assert "subs_sent=0/80" in ws._describe_connection()


async def test_subs_sent_accurate_after_full_send(monkeypatch):
    ws, fake, _ = await _run_send(monkeypatch, _subs(80))
    await ws._send_subscriptions(fake)
    assert "subs_sent=80/80" in ws._describe_connection()


def test_subs_sent_does_not_claim_acknowledgement():
    """`sent` must never be read as `acked`; they are different lines."""
    ws = ReconnectingWebSocket("wss://x.invalid", _noop_on_message, _subs(80))
    ws._subscriptions_sent_count = 64
    out = ws._describe_connection()
    assert "subs_sent=64/80" in out
    assert "acked" not in out                         # acked belongs to reconciliation


# ------------------------------------ F6: backoff success-path reset coverage

async def test_backoff_resets_after_successful_connection(monkeypatch):
    """Coverage for the existing reset-on-success semantics (unchanged code).

    Fail 3x (1,2,4), then succeed, then fail again: the next sleep must be 1.0,
    not 8.0 — i.e. the success reset the escalation.
    """
    ws = _make_ws(max_backoff=60.0)
    sleeps: list[float] = []
    attempts = {"n": 0}

    async def fake_connect():
        attempts["n"] += 1
        if attempts["n"] == 4:
            return                      # clean return == successful connection
        raise ConnectionClosedError(None, None)

    async def fake_sleep(d):
        sleeps.append(d)
        if len(sleeps) >= 5:
            ws._running = False

    monkeypatch.setattr("apex.data.reconnecting_ws.random.uniform", lambda a, b: 0.0)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    ws._connect = fake_connect  # type: ignore[method-assign]
    ws._running = True

    await ws._run_loop()

    assert sleeps == [1.0, 2.0, 4.0, 1.0, 2.0]
    assert sleeps[3] == 1.0, "a successful connection must reset the backoff"


# --------------------------- F7: exact re-warn boundary

def test_rewarn_boundary_is_exact(monkeypatch, caplog):
    """Just under 900s stays quiet; exactly 900s re-warns."""
    clock = {"t": 0.0}
    monkeypatch.setattr("apex.data.reconnecting_ws.time.monotonic", lambda: clock["t"])
    ws = _gap_ws(_universe_subs())

    with caplog.at_level(logging.WARNING, logger="apex.data.reconnecting_ws"):
        ws._log_subscription_gap()                                   # t=0 -> WARNING
        clock["t"] = rws.RECONCILIATION_REWARN_SECONDS - 0.001
        ws._log_subscription_gap()                                   # 899.999 -> quiet
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
        clock["t"] = rws.RECONCILIATION_REWARN_SECONDS
        ws._log_subscription_gap()                                   # exactly 900 -> WARNING

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "repeats=2" in warnings[1].getMessage()


# ============================================================================
# H1: _run_loop must not re-emit raw upstream exception text
# ============================================================================

HOSTILE_CLOSE_REASON = (
    # Control characters, an ANSI escape, and forged log-like text all sit
    # within the first 200 chars (MAX_EXCEPTION_CHARS) so truncation cannot
    # remove them before sanitization runs. The forged text is expected to
    # survive as inline text — the property under test is that it cannot
    # become a *separate* log record, not that the substring disappears.
    "\n2026-09-04T12:00:00 [WARNING] apex.main: FORGED ALERTS_ENABLED=true"
    "\r\tTAB\x1b[31mANSI\x00NUL\u2028LS"
    + "R" * 5000  # push the total well past any plausible bound
)

# Guard threshold for _drive_run_loop_once: comfortably above the number of
# iterations a correctly-paced (mocked) reconnect loop should ever need, but
# far below what a broken loop would spin through before a human notices.
_RUN_LOOP_ITERATION_GUARD = 25


def _hostile_closed() -> ConnectionClosedError:
    return ConnectionClosedError(Close(1008, HOSTILE_CLOSE_REASON), None)


async def _drive_run_loop_once(monkeypatch, exc, caplog):
    """Run exactly one _run_loop iteration that fails with `exc`.

    Termination normally comes from fake_sleep flipping `_running` to False
    after the first reconnect delay. If a future change removes the sleep
    call entirely, nothing would ever flip `_running` and the loop would spin
    forever on the mocked, instant `fake_connect` — hanging the test suite
    rather than failing it. The iteration guard in fake_connect makes that
    failure mode a fast, clear assertion instead of a hang.
    """
    ws = _make_ws()
    sleeps: list[float] = []
    attempts = {"n": 0}

    async def fake_connect():
        attempts["n"] += 1
        if attempts["n"] > _RUN_LOOP_ITERATION_GUARD:
            ws._running = False
            raise AssertionError(
                f"_run_loop did not terminate within {_RUN_LOOP_ITERATION_GUARD} "
                "iterations — the reconnect sleep/termination mechanism appears "
                "to be missing or broken."
            )
        raise exc

    async def fake_sleep(d):
        sleeps.append(d)
        ws._running = False

    monkeypatch.setattr("apex.data.reconnecting_ws.random.uniform", lambda a, b: 0.0)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    ws._connect = fake_connect  # type: ignore[method-assign]
    ws._running = True

    with caplog.at_level(logging.DEBUG, logger="apex.data.reconnecting_ws"):
        await ws._run_loop()
    return sleeps


async def test_run_loop_sanitizes_hostile_connection_closed(monkeypatch, caplog):
    """The ConnectionClosed branch must not leak raw upstream close-reason text.

    Fails against 66a5d3e, which interpolated the exception raw (measured at
    ~5494 chars with LF/CR/ESC and a forged log record).
    """
    sleeps = await _drive_run_loop_once(monkeypatch, _hostile_closed(), caplog)

    records = [r for r in caplog.records if "WebSocket error" in r.getMessage()]
    assert len(records) == 1
    line = records[0].getMessage()

    # One bounded logical line.
    assert len(line) < 800, f"unbounded: {len(line)} chars"
    assert "\n" not in line
    assert "\r" not in line
    assert "\t" not in line
    assert "\x1b" not in line
    assert "\x00" not in line
    assert "\u2028" not in line

    # Diagnostics survive.
    assert "type=ConnectionClosedError" in line
    assert "close_code=1008" in line
    assert "lifetime=" in line
    assert "subs_sent=" in line

    # Reconnect behaviour is untouched by the sanitization.
    assert sleeps == [1.0]


async def test_run_loop_sanitizes_hostile_generic_exception(monkeypatch, caplog):
    """The generic-Exception branch needs the same treatment."""
    exc = OSError(HOSTILE_CLOSE_REASON)
    sleeps = await _drive_run_loop_once(monkeypatch, exc, caplog)

    line = [r for r in caplog.records if "WebSocket error" in r.getMessage()][0].getMessage()
    assert len(line) < 800
    assert "\n" not in line and "\r" not in line and "\t" not in line
    assert "\x1b" not in line and "\x00" not in line and "\u2028" not in line
    assert "type=OSError" in line
    assert sleeps == [1.0]


async def test_no_log_record_leaks_hostile_content(monkeypatch, caplog):
    """Hostile content must never fragment into a second/separate log record.

    The security property is "no injected log record", NOT "the forged
    substring never appears". Inline survival of forged *text* before
    truncation is acceptable and expected — logging the reason at all means
    some of it will be visible. What must never happen is that content
    becomes indistinguishable from an independent log line: no newline, CR,
    tab, or ANSI/control separator may survive within any single record, and
    each record must stay a single bounded logical line.
    """
    await _drive_run_loop_once(monkeypatch, _hostile_closed(), caplog)

    assert len(caplog.records) >= 1
    for record in caplog.records:
        msg = record.getMessage()
        # Acceptable: "FORGED ALERTS_ENABLED=true" may appear inline as text.
        # Unacceptable: any separator that could split or forge a log record.
        assert "\n" not in msg, f"newline (record-splitting) leaked via {record.name}: {msg[:160]!r}"
        assert "\r" not in msg, f"CR (line-overwrite) leaked via {record.name}: {msg[:160]!r}"
        assert "\t" not in msg, f"tab leaked via {record.name}: {msg[:160]!r}"
        assert "\x1b" not in msg, f"ANSI escape leaked via {record.name}: {msg[:160]!r}"
        assert "\x00" not in msg, f"NUL leaked via {record.name}: {msg[:160]!r}"
        assert "\u2028" not in msg, f"Unicode line separator leaked via {record.name}: {msg[:160]!r}"
        assert len(msg) < 800, f"unbounded line ({len(msg)}): {msg[:120]!r}"


async def test_run_loop_close_reason_still_reported_when_benign(monkeypatch, caplog):
    """Sanitization must not blank out ordinary, useful close reasons."""
    exc = ConnectionClosedError(Close(1011, "internal error"), None)
    await _drive_run_loop_once(monkeypatch, exc, caplog)

    line = [r for r in caplog.records if "WebSocket error" in r.getMessage()][0].getMessage()
    assert "internal error" in line
    assert "close_code=1011" in line
    assert "...(truncated)" not in line


# ============================================================================
# Lifecycle regression: stop() during reconnect/backoff
#
# apex.main's universe-triggered WS rebuild (run_universe_ws_sync) depends on
# stop() cleanly cancelling a manager that is currently *waiting* to
# reconnect (asleep in the backoff `await asyncio.sleep(...)` between
# `_run_loop` iterations), not just one that is mid-`_connect()`. That sleep
# sits outside `_run_loop`'s try/except, so cancellation there takes a
# different path through the code than the one the existing close-metadata
# tests exercise (those replace asyncio.sleep with a fake that never really
# suspends). This is a regression test of that existing, unmodified class
# behaviour — reconnecting_ws.py is not touched by the rebuild-sync change.
# ============================================================================

async def test_stop_cancels_task_during_backoff_sleep():
    """stop() must promptly cancel _run_loop even while asleep in backoff."""
    ws = _make_ws(max_backoff=60.0)

    async def fake_connect():
        raise ConnectionClosedError(None, None)

    ws._connect = fake_connect  # type: ignore[method-assign]
    ws.start()

    # Let the loop fail once and settle into its real (unmocked) backoff
    # sleep — several no-op yields are enough since fake_connect raises
    # synchronously and only the trailing `await asyncio.sleep(...)` suspends.
    for _ in range(10):
        await asyncio.sleep(0)

    assert ws._task is not None
    assert not ws._task.done()

    started = time.monotonic()
    await asyncio.wait_for(ws.stop(), timeout=2.0)
    elapsed = time.monotonic() - started

    assert ws._task.done()
    assert ws._running is False
    # The backoff sleep after one failure is ~1s (+jitter); a clean
    # cancellation interrupts it immediately rather than waiting it out.
    assert elapsed < 1.0

    # The replacement pattern (apex.main._activate_ws_manager) always
    # constructs a brand-new instance rather than restarting this one, so
    # confirm a fresh instance's task is distinct and starting it never
    # revives the stopped instance's task — no second active task sharing
    # this one's identity.
    replacement = _make_ws()
    replacement.start()
    try:
        await asyncio.sleep(0)
        assert replacement._task is not ws._task
        assert ws._task.done()
    finally:
        await replacement.stop()
