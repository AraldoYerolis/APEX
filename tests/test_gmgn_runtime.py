"""Tests for apex.research.gmgn.runtime (default-off GMGN read-only
research runtime seam) and apex.config's gmgn_research_watchlist parsing.

Covers: disabled no-I/O (watchlist never parsed, transport never touched);
enabled-with-no-transport fail-closed behavior; deterministic fake-only
successful evaluation through the real normalize/funnel pipeline; that no
real GmgnResearchTransport/httpx/os.environ access ever occurs; the exact
four allowlisted endpoints and request identity used per candidate;
malformed/duplicate/overflow watchlist parsing; per-candidate failure
isolation (transport/envelope-shape/normalization); bounded result
collections and reason/label text; and a static forbidden-import assertion
for the runtime module itself. Every test uses an injected fake transport
(or none at all) — no real socket, environment variable, or database is
ever touched.
"""
from __future__ import annotations

import ast
import inspect
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import pytest

from apex.config import GMGN_RESEARCH_MAX_WATCHLIST_IDENTITIES, GmgnWatchlistIdentity, Settings
from apex.research.gmgn import runtime
from apex.research.gmgn.funnel import ELIGIBLE_FOR_REVIEW
from apex.research.gmgn.runtime import (
    CANDLE_ENDPOINT,
    POOL_INFO_ENDPOINT,
    SECURITY_ENDPOINT,
    STRUCTURE_ENDPOINT,
    UNAVAILABLE,
    GmgnCandidateResearchResult,
    GmgnResearchScanResult,
    MAX_REASONS,
    MAX_SUPPORTING_LABELS,
    MAX_TEXT_LENGTH,
    run_gmgn_research_scan,
)

FIXED_NOW = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
CHAIN = "solana"
ADDRESS_A = "TokenAAA111"
ADDRESS_B = "TokenBBB222"


# ---------------------------------------------------------------------------
# Fake transports
# ---------------------------------------------------------------------------


class _ExplodingTransport:
    """Raises if `get` is ever called — proves a code path never touches it."""

    async def get(self, path: str, *, params: Optional[dict] = None) -> dict:
        raise AssertionError(f"transport must not be called (path={path!r})")


class _ScriptedTransport:
    """Returns/raises a scripted response keyed by (path, requested address).
    Records every call for assertion on exact endpoints/identity used."""

    def __init__(self, script: dict[tuple[str, str], Any]) -> None:
        self._script = script
        self.calls: list[tuple[str, dict]] = []

    async def get(self, path: str, *, params: Optional[dict] = None) -> dict:
        params = dict(params or {})
        self.calls.append((path, params))
        key = (path, params.get("address"))
        if key not in self._script:
            raise AssertionError(f"unscripted call: {key}")
        result = self._script[key]
        if isinstance(result, BaseException):
            raise result
        return result


def _security_payload(chain: str, address: str, **overrides: Any) -> dict:
    data = {
        "chain": chain,
        "address": address,
        "is_open_source": True,
        "is_mintable": False,
        "is_freezable": False,
        "is_honeypot": False,
        "top10_holder_percent": 20.0,
        "lp_locked_percent": 95.0,
        "buy_tax_percent": 1.0,
        "sell_tax_percent": 1.0,
    }
    data.update(overrides)
    return {"code": 0, "data": data}


def _pool_payload(chain: str, address: str, **overrides: Any) -> dict:
    data = {
        "chain": chain,
        "address": address,
        "pool_address": "Pool1",
        "dex": "raydium",
        "quote_symbol": "SOL",
        "liquidity_usd": 50_000.0,
        "base_reserve": 1000.0,
        "quote_reserve": 500.0,
    }
    data.update(overrides)
    return {"code": 0, "data": data}


def _structure_payload(chain: str, address: str, **overrides: Any) -> dict:
    data = {
        "chain": chain,
        "address": address,
        "holder_count": 1000,
        "top10_holder_percent": 20.0,
        "top_trader_count": 50,
        "top_trader_percent": 10.0,
        "insider_percent": 1.0,
    }
    data.update(overrides)
    return {"code": 0, "data": data}


def _candle_payload(chain: str, address: str) -> dict:
    items = [
        {"chain": chain, "address": address, "interval": "5m", "time": 1000,
         "open": 1.0, "high": 1.2, "low": 0.9, "close": 1.0, "volume": 100},
        {"chain": chain, "address": address, "interval": "5m", "time": 2000,
         "open": 1.0, "high": 1.3, "low": 0.95, "close": 1.1, "volume": 120},
        {"chain": chain, "address": address, "interval": "5m", "time": 3000,
         "open": 1.1, "high": 1.4, "low": 1.0, "close": 1.2, "volume": 150},
        {"chain": chain, "address": address, "interval": "5m", "time": 4000,
         "open": 1.2, "high": 1.6, "low": 1.1, "close": 1.5, "volume": 200},
    ]
    return {"code": 0, "data": items}


def _full_success_script(chain: str, address: str) -> dict[tuple[str, str], Any]:
    return {
        (SECURITY_ENDPOINT, address): _security_payload(chain, address),
        (POOL_INFO_ENDPOINT, address): _pool_payload(chain, address),
        (STRUCTURE_ENDPOINT, address): _structure_payload(chain, address),
        (CANDLE_ENDPOINT, address): _candle_payload(chain, address),
    }


# ---------------------------------------------------------------------------
# Disabled: no I/O at all
# ---------------------------------------------------------------------------


async def test_disabled_returns_immediately_without_parsing_watchlist_or_transport(monkeypatch):
    def _boom(self) -> tuple:
        raise AssertionError("watchlist must not be parsed when disabled")

    monkeypatch.setattr(Settings, "gmgn_research_watchlist_identities", property(_boom))
    settings = Settings(gmgn_research_enabled=False, gmgn_research_watchlist=f"{CHAIN}:{ADDRESS_A}")

    result = await run_gmgn_research_scan(settings, _ExplodingTransport())

    assert result.enabled is False
    assert result.candidates == ()
    assert result.unavailable_reason is None


async def test_disabled_ignores_a_provided_transport_entirely():
    settings = Settings(gmgn_research_enabled=False)
    result = await run_gmgn_research_scan(settings, _ExplodingTransport(), now=FIXED_NOW)
    assert result.enabled is False
    assert result.candidates == ()


async def test_disabled_emits_no_gmgn_runtime_log(caplog):
    settings = Settings(gmgn_research_enabled=False)
    with caplog.at_level(logging.DEBUG, logger="apex.research.gmgn.runtime"):
        await run_gmgn_research_scan(settings, _ExplodingTransport(), now=FIXED_NOW)
    assert [r for r in caplog.records if r.name == "apex.research.gmgn.runtime"] == []


# ---------------------------------------------------------------------------
# Enabled, no transport injected: fail closed, no I/O
# ---------------------------------------------------------------------------


async def test_enabled_without_transport_returns_bounded_unavailable_result():
    settings = Settings(gmgn_research_enabled=True, gmgn_research_watchlist=f"{CHAIN}:{ADDRESS_A}")
    result = await run_gmgn_research_scan(settings, transport=None, now=FIXED_NOW)
    assert result.enabled is True
    assert result.candidates == ()
    assert result.unavailable_reason is not None
    assert len(result.unavailable_reason) <= MAX_TEXT_LENGTH


# ---------------------------------------------------------------------------
# Deterministic fake-only successful evaluation
# ---------------------------------------------------------------------------


async def test_enabled_with_transport_reaches_eligible_for_review_deterministically():
    settings = Settings(
        gmgn_research_enabled=True, gmgn_research_watchlist=f"{CHAIN}:{ADDRESS_A}",
    )
    transport = _ScriptedTransport(_full_success_script(CHAIN, ADDRESS_A))

    result = await run_gmgn_research_scan(settings, transport, now=FIXED_NOW)

    assert result.enabled is True
    assert result.unavailable_reason is None
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.chain == CHAIN
    assert candidate.contract_address == ADDRESS_A
    assert candidate.status == ELIGIBLE_FOR_REVIEW
    assert candidate.reasons == ()


async def test_result_is_deterministic_across_repeated_calls():
    settings = Settings(gmgn_research_enabled=True, gmgn_research_watchlist=f"{CHAIN}:{ADDRESS_A}")

    result_1 = await run_gmgn_research_scan(
        settings, _ScriptedTransport(_full_success_script(CHAIN, ADDRESS_A)), now=FIXED_NOW,
    )
    result_2 = await run_gmgn_research_scan(
        settings, _ScriptedTransport(_full_success_script(CHAIN, ADDRESS_A)), now=FIXED_NOW,
    )
    assert result_1 == result_2


# ---------------------------------------------------------------------------
# Exact endpoints and identity used
# ---------------------------------------------------------------------------


async def test_exactly_the_four_allowlisted_endpoints_are_called_with_candidate_identity():
    settings = Settings(gmgn_research_enabled=True, gmgn_research_watchlist=f"{CHAIN}:{ADDRESS_A}")
    transport = _ScriptedTransport(_full_success_script(CHAIN, ADDRESS_A))

    await run_gmgn_research_scan(settings, transport, now=FIXED_NOW)

    called_paths = [path for path, _ in transport.calls]
    assert called_paths == [SECURITY_ENDPOINT, POOL_INFO_ENDPOINT, STRUCTURE_ENDPOINT, CANDLE_ENDPOINT]
    for _, params in transport.calls:
        assert params["chain"] == CHAIN
        assert params["address"] == ADDRESS_A


# ---------------------------------------------------------------------------
# Bounded aggregate observability
# ---------------------------------------------------------------------------


async def test_enabled_without_transport_emits_one_bounded_aggregate_log_without_leaking_address(caplog):
    settings = Settings(gmgn_research_enabled=True, gmgn_research_watchlist=f"{CHAIN}:{ADDRESS_A}")

    with caplog.at_level(logging.INFO, logger="apex.research.gmgn.runtime"):
        await run_gmgn_research_scan(settings, transport=None, now=FIXED_NOW)

    records = [r for r in caplog.records if r.name == "apex.research.gmgn.runtime"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert ADDRESS_A not in message
    assert CHAIN not in message
    assert "candidate_count=0" in message
    assert UNAVAILABLE in message


async def test_enabled_with_transport_logs_only_bounded_aggregate_counts_and_statuses(caplog):
    settings = Settings(gmgn_research_enabled=True, gmgn_research_watchlist=f"{CHAIN}:{ADDRESS_A}")
    transport = _ScriptedTransport(_full_success_script(CHAIN, ADDRESS_A))

    with caplog.at_level(logging.INFO, logger="apex.research.gmgn.runtime"):
        result = await run_gmgn_research_scan(settings, transport, now=FIXED_NOW)

    assert result.candidates[0].status == ELIGIBLE_FOR_REVIEW
    records = [r for r in caplog.records if r.name == "apex.research.gmgn.runtime"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert ADDRESS_A not in message
    assert CHAIN not in message
    assert "raydium" not in message
    assert "candidate_count=1" in message
    assert "eligible_for_review=1" in message
    assert "rejected=0" in message
    assert "watch=0" in message
    assert "unavailable=0" in message


# ---------------------------------------------------------------------------
# No real transport/httpx/os.environ ever touched
# ---------------------------------------------------------------------------


async def test_never_constructs_the_real_gmgn_transport(monkeypatch):
    from apex.research.gmgn.transport import GmgnResearchTransport

    def _boom(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("must never construct GmgnResearchTransport")

    monkeypatch.setattr(GmgnResearchTransport, "__init__", _boom)

    settings = Settings(gmgn_research_enabled=True, gmgn_research_watchlist=f"{CHAIN}:{ADDRESS_A}")
    result = await run_gmgn_research_scan(
        settings, _ScriptedTransport(_full_success_script(CHAIN, ADDRESS_A)), now=FIXED_NOW,
    )
    assert result.enabled is True


def test_runtime_module_has_no_forbidden_static_imports():
    source = inspect.getsource(runtime)
    tree = ast.parse(source)

    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    forbidden_prefixes = (
        "httpx",
        "os",
        "socket",
        "sqlite3",
        "apex.notifications",
        "apex.strategy",
        "apex.actions",
        "apex.db",
    )
    for module in imported_modules:
        for forbidden in forbidden_prefixes:
            assert module != forbidden and not module.startswith(forbidden + "."), (
                f"runtime.py must never import {module!r} (forbidden: {forbidden!r})"
            )

    assert "GmgnResearchTransport(" not in source

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "environ"
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
        ):
            raise AssertionError("runtime.py must never reference os.environ")


# ---------------------------------------------------------------------------
# Watchlist parsing: malformed / duplicate / overflow / case preservation
# ---------------------------------------------------------------------------


class TestWatchlistParsing:
    def test_two_distinct_identities_preserved_in_order(self):
        settings = Settings(gmgn_research_watchlist=f"{CHAIN}:{ADDRESS_A},{CHAIN}:{ADDRESS_B}")
        identities = settings.gmgn_research_watchlist_identities
        assert identities == (
            GmgnWatchlistIdentity(chain=CHAIN, contract_address=ADDRESS_A),
            GmgnWatchlistIdentity(chain=CHAIN, contract_address=ADDRESS_B),
        )

    def test_duplicate_exact_identity_is_deduplicated_first_seen_order(self):
        settings = Settings(
            gmgn_research_watchlist=f"{CHAIN}:{ADDRESS_B},{CHAIN}:{ADDRESS_A},{CHAIN}:{ADDRESS_B}"
        )
        identities = settings.gmgn_research_watchlist_identities
        assert identities == (
            GmgnWatchlistIdentity(chain=CHAIN, contract_address=ADDRESS_B),
            GmgnWatchlistIdentity(chain=CHAIN, contract_address=ADDRESS_A),
        )

    def test_address_case_is_preserved_exactly(self):
        settings = Settings(gmgn_research_watchlist=f"{CHAIN}:AaBbCc0011")
        identities = settings.gmgn_research_watchlist_identities
        assert identities[0].contract_address == "AaBbCc0011"

    @pytest.mark.parametrize(
        "raw",
        [
            "solana-MissingColon",
            f"{CHAIN}:",
            ":OnlyAddress",
            "unsupportedchain:AAA",
            "solana:AAA:extra:colons",
        ],
    )
    def test_malformed_or_unsupported_segment_fails_closed_to_empty(self, raw):
        settings = Settings(gmgn_research_watchlist=raw)
        assert settings.gmgn_research_watchlist_identities == ()

    @pytest.mark.parametrize(
        "raw",
        [
            f",{CHAIN}:{ADDRESS_A}",  # leading comma
            f"{CHAIN}:{ADDRESS_A},",  # trailing comma
            f"{CHAIN}:{ADDRESS_A},,{CHAIN}:{ADDRESS_B}",  # interior doubled comma
            f"{CHAIN}:{ADDRESS_A},   ,{CHAIN}:{ADDRESS_B}",  # whitespace-only segment
        ],
    )
    def test_empty_or_whitespace_only_segment_fails_closed_to_empty(self, raw):
        settings = Settings(gmgn_research_watchlist=raw)
        assert settings.gmgn_research_watchlist_identities == ()

    def test_overflow_beyond_max_fails_closed_to_empty(self):
        raw = ",".join(f"{CHAIN}:Token{i}" for i in range(GMGN_RESEARCH_MAX_WATCHLIST_IDENTITIES + 1))
        settings = Settings(gmgn_research_watchlist=raw)
        assert settings.gmgn_research_watchlist_identities == ()

    def test_exactly_max_identities_is_accepted(self):
        raw = ",".join(f"{CHAIN}:Token{i}" for i in range(GMGN_RESEARCH_MAX_WATCHLIST_IDENTITIES))
        settings = Settings(gmgn_research_watchlist=raw)
        assert len(settings.gmgn_research_watchlist_identities) == GMGN_RESEARCH_MAX_WATCHLIST_IDENTITIES

    def test_empty_watchlist_is_empty(self):
        settings = Settings(gmgn_research_watchlist="")
        assert settings.gmgn_research_watchlist_identities == ()

    def test_tickers_never_establish_identity(self):
        # A bare ticker with no chain/address shape must never be accepted.
        settings = Settings(gmgn_research_watchlist="DOGE")
        assert settings.gmgn_research_watchlist_identities == ()


# ---------------------------------------------------------------------------
# Per-candidate failure isolation
# ---------------------------------------------------------------------------


async def test_transport_failure_on_one_candidate_does_not_block_the_next():
    settings = Settings(
        gmgn_research_enabled=True,
        gmgn_research_watchlist=f"{CHAIN}:{ADDRESS_A},{CHAIN}:{ADDRESS_B}",
    )
    script = {
        (SECURITY_ENDPOINT, ADDRESS_A): RuntimeError("vendor boom"),
        **_full_success_script(CHAIN, ADDRESS_B),
    }
    transport = _ScriptedTransport(script)

    result = await run_gmgn_research_scan(settings, transport, now=FIXED_NOW)

    assert len(result.candidates) == 2
    first, second = result.candidates
    assert first.chain == CHAIN and first.contract_address == ADDRESS_A
    assert first.status == UNAVAILABLE
    assert first.reasons != ()
    assert second.contract_address == ADDRESS_B
    assert second.status == ELIGIBLE_FOR_REVIEW


async def test_envelope_shape_failure_is_isolated_to_one_candidate():
    settings = Settings(
        gmgn_research_enabled=True,
        gmgn_research_watchlist=f"{CHAIN}:{ADDRESS_A},{CHAIN}:{ADDRESS_B}",
    )
    script = dict(_full_success_script(CHAIN, ADDRESS_B))
    script[(SECURITY_ENDPOINT, ADDRESS_A)] = {"code": 0, "data": "not-a-mapping"}
    script[(POOL_INFO_ENDPOINT, ADDRESS_A)] = _pool_payload(CHAIN, ADDRESS_A)
    script[(STRUCTURE_ENDPOINT, ADDRESS_A)] = _structure_payload(CHAIN, ADDRESS_A)
    script[(CANDLE_ENDPOINT, ADDRESS_A)] = _candle_payload(CHAIN, ADDRESS_A)
    transport = _ScriptedTransport(script)

    result = await run_gmgn_research_scan(settings, transport, now=FIXED_NOW)

    assert len(result.candidates) == 2
    assert result.candidates[0].status == UNAVAILABLE
    assert result.candidates[1].status == ELIGIBLE_FOR_REVIEW


async def test_normalization_failure_is_isolated_to_one_candidate():
    settings = Settings(
        gmgn_research_enabled=True,
        gmgn_research_watchlist=f"{CHAIN}:{ADDRESS_A},{CHAIN}:{ADDRESS_B}",
    )
    script = dict(_full_success_script(CHAIN, ADDRESS_B))
    # top10_holder_percent above the [0, 100] contract.py bound raises ValueError
    # inside the record's own field validation during normalization.
    script[(SECURITY_ENDPOINT, ADDRESS_A)] = _security_payload(
        CHAIN, ADDRESS_A, top10_holder_percent=250.0
    )
    script[(POOL_INFO_ENDPOINT, ADDRESS_A)] = _pool_payload(CHAIN, ADDRESS_A)
    script[(STRUCTURE_ENDPOINT, ADDRESS_A)] = _structure_payload(CHAIN, ADDRESS_A)
    script[(CANDLE_ENDPOINT, ADDRESS_A)] = _candle_payload(CHAIN, ADDRESS_A)
    transport = _ScriptedTransport(script)

    result = await run_gmgn_research_scan(settings, transport, now=FIXED_NOW)

    assert len(result.candidates) == 2
    assert result.candidates[0].status == UNAVAILABLE
    assert result.candidates[1].status == ELIGIBLE_FOR_REVIEW


async def test_non_mapping_candle_item_makes_candidate_unavailable_and_next_candidate_still_completes():
    settings = Settings(
        gmgn_research_enabled=True,
        gmgn_research_watchlist=f"{CHAIN}:{ADDRESS_A},{CHAIN}:{ADDRESS_B}",
    )
    script = dict(_full_success_script(CHAIN, ADDRESS_B))
    script[(SECURITY_ENDPOINT, ADDRESS_A)] = _security_payload(CHAIN, ADDRESS_A)
    script[(POOL_INFO_ENDPOINT, ADDRESS_A)] = _pool_payload(CHAIN, ADDRESS_A)
    script[(STRUCTURE_ENDPOINT, ADDRESS_A)] = _structure_payload(CHAIN, ADDRESS_A)
    malformed_candles = _candle_payload(CHAIN, ADDRESS_A)
    malformed_candles["data"] = [malformed_candles["data"][0], "not-a-mapping", *malformed_candles["data"][1:]]
    script[(CANDLE_ENDPOINT, ADDRESS_A)] = malformed_candles
    transport = _ScriptedTransport(script)

    result = await run_gmgn_research_scan(settings, transport, now=FIXED_NOW)

    assert len(result.candidates) == 2
    assert result.candidates[0].status == UNAVAILABLE
    assert result.candidates[1].status == ELIGIBLE_FOR_REVIEW


# ---------------------------------------------------------------------------
# Bounded result collections and reason/label text
# ---------------------------------------------------------------------------


class TestBounding:
    def test_candidate_reasons_and_labels_are_bounded(self):
        result = GmgnCandidateResearchResult(
            chain=CHAIN,
            contract_address=ADDRESS_A,
            status=UNAVAILABLE,
            reasons=tuple(f"reason-{i}-" + "x" * 1000 for i in range(100)),
            supporting_labels=tuple(f"label-{i}-" + "y" * 1000 for i in range(100)),
        )
        assert len(result.reasons) == MAX_REASONS
        assert len(result.supporting_labels) == MAX_SUPPORTING_LABELS
        assert all(len(r) <= MAX_TEXT_LENGTH for r in result.reasons)
        assert all(len(label) <= MAX_TEXT_LENGTH for label in result.supporting_labels)

    def test_scan_result_candidates_are_bounded_to_max_watchlist_size(self):
        overflow_candidates = tuple(
            GmgnCandidateResearchResult(
                chain=CHAIN, contract_address=f"Token{i}", status=UNAVAILABLE,
                reasons=(), supporting_labels=(),
            )
            for i in range(GMGN_RESEARCH_MAX_WATCHLIST_IDENTITIES + 5)
        )
        result = GmgnResearchScanResult(
            runtime_version="test", enabled=True, candidates=overflow_candidates, unavailable_reason=None,
        )
        assert len(result.candidates) == GMGN_RESEARCH_MAX_WATCHLIST_IDENTITIES

    def test_scan_result_unavailable_reason_is_bounded(self):
        result = GmgnResearchScanResult(
            runtime_version="test", enabled=True, candidates=(),
            unavailable_reason="z" * 10_000,
        )
        assert len(result.unavailable_reason) <= MAX_TEXT_LENGTH
