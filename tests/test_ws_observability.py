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

    def connect(self, url, **kwargs):
        self.calls += 1
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
