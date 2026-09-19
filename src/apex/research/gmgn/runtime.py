"""Default-off GMGN read-only research runtime seam — fake-transport-only
for this milestone.

`run_gmgn_research_scan` is the only entry point. It re-checks
`settings.gmgn_research_enabled` at entry regardless of how/whether the
caller already gated it (defense in depth — see `apex.main`, which is the
only production caller and deliberately never injects a real transport for
this milestone). When disabled it returns immediately without parsing the
watchlist or touching `transport`. When enabled with no `transport`
injected it returns an honest bounded unavailable result and performs no
I/O. This module never constructs `apex.research.gmgn.transport.
GmgnResearchTransport` itself, imports `httpx`, reads `os.environ`/`.env`,
opens a socket, or touches a database — `transport` must always be supplied
by the caller and only needs to satisfy `SupportsGmgnResearchGet` below.

For each configured watchlist identity this calls exactly four allowlisted
GET endpoints — `/v1/token/security`, `/v1/token/pool_info`,
`/v1/market/token_top_holders`, `/v1/market/token_kline` — and feeds their
responses through the existing `apex.research.gmgn.normalize` builders and
`apex.research.gmgn.funnel.evaluate_research_candidate`. It never builds a
substitute schema or fabricates a missing value. Discovery/rank/search/
smart-money endpoints are out of scope for this v0 integration.

A transport, envelope-shape, normalization, or evaluation failure is
isolated to the one candidate it occurred on (recorded as an `UNAVAILABLE`
result) and never prevents later identities from being scanned. All result
collections and reason/label text are bounded. Raw vendor payloads,
untrusted vendor labels/text, chain/contract-address identities, and
exception text are never logged. Each enabled invocation — including the
enabled-with-no-transport fail-closed path — emits exactly one bounded
aggregate log line via a fixed-name module logger
(`apex.research.gmgn.runtime`), containing only the runtime version, a
fixed scan-outcome classification, and bounded per-status candidate counts
(`REJECTED`/`WATCH`/`ELIGIBLE_FOR_REVIEW`/`UNAVAILABLE`). The disabled path
remains a true immediate no-op: no watchlist parsing, no transport access,
and no log emission at all.

Purely research classification, like the rest of `apex.research.gmgn` — see
`apex/research/gmgn/__init__.py`. Never produces a trade, entry, stop,
target, position size, alert, or order of any kind, and never imports
`apex.notifications`, `apex.strategy`, `apex.actions`, a database
repository/write module, or a broker/exchange adapter.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Protocol, Sequence

from apex.config import GMGN_RESEARCH_MAX_WATCHLIST_IDENTITIES, GmgnWatchlistIdentity, Settings
from apex.research.gmgn.funnel import ELIGIBLE_FOR_REVIEW, REJECTED, WATCH, evaluate_research_candidate
from apex.research.gmgn.normalize import (
    GmgnNormalizationError,
    build_candle_record,
    build_holder_trader_structure_record,
    build_liquidity_pool_record,
    build_token_security_record,
)

# Fixed-name module logger — the only sink for this module's bounded
# aggregate scan-summary log line (see module docstring and
# `_log_scan_summary`). Never fed vendor payloads, labels, exception text,
# or watchlist identities.
logger = logging.getLogger("apex.research.gmgn.runtime")

RUNTIME_VERSION = "gmgn_research_runtime_v0_1"

SECURITY_ENDPOINT = "/v1/token/security"
POOL_INFO_ENDPOINT = "/v1/token/pool_info"
STRUCTURE_ENDPOINT = "/v1/market/token_top_holders"
CANDLE_ENDPOINT = "/v1/market/token_kline"

# Per-candidate funnel outcome could not be reached at all (transport,
# envelope-shape, normalization, or evaluation failure isolated to this one
# candidate) — distinct from the three apex.research.gmgn.funnel statuses,
# never itself a rejection/watch/eligibility determination.
UNAVAILABLE = "UNAVAILABLE"

MAX_CANDLES_PER_CANDIDATE = 60
CANDLE_RESOLUTION = "5m"

MAX_REASONS = 20
MAX_SUPPORTING_LABELS = 20
MAX_TEXT_LENGTH = 256

# Fixed scan-outcome classifications for the bounded aggregate log line —
# never vendor-derived. SCAN_OUTCOME_NO_TRANSPORT reuses UNAVAILABLE, this
# runtime's own fixed unavailable classification.
SCAN_OUTCOME_COMPLETED = "COMPLETED"
SCAN_OUTCOME_NO_TRANSPORT = UNAVAILABLE

# The only candidate-status values ever counted in the aggregate log line —
# the three apex.research.gmgn.funnel statuses plus this module's own
# UNAVAILABLE classification.
_KNOWN_CANDIDATE_STATUSES = (REJECTED, WATCH, ELIGIBLE_FOR_REVIEW, UNAVAILABLE)


def _bounded_text(value: str, *, max_length: int = MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str):
        return ""
    return value[:max_length]


def _bounded_text_tuple(
    values: Sequence[str], *, max_items: int, max_length: int = MAX_TEXT_LENGTH
) -> tuple[str, ...]:
    return tuple(_bounded_text(v, max_length=max_length) for v in tuple(values)[:max_items])


class SupportsGmgnResearchGet(Protocol):
    """Minimal shape required of an injected transport: a single async
    `get` method mirroring `GmgnResearchTransport.get`. This runtime never
    constructs a `GmgnResearchTransport` itself — see module docstring."""

    async def get(self, path: str, *, params: Optional[dict[str, Any]] = None) -> dict[str, Any]: ...


@dataclass(frozen=True)
class GmgnCandidateResearchResult:
    """Immutable per-candidate research outcome. `status` is one of the
    three `apex.research.gmgn.funnel` statuses (`REJECTED`, `WATCH`,
    `ELIGIBLE_FOR_REVIEW`), or `UNAVAILABLE` when a failure isolated to this
    candidate prevented reaching a funnel outcome at all. Never a trade,
    alert, order, size, or price target of any kind."""

    chain: str
    contract_address: str
    status: str
    reasons: tuple[str, ...]
    supporting_labels: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", _bounded_text_tuple(self.reasons, max_items=MAX_REASONS))
        object.__setattr__(
            self,
            "supporting_labels",
            _bounded_text_tuple(self.supporting_labels, max_items=MAX_SUPPORTING_LABELS),
        )


@dataclass(frozen=True)
class GmgnResearchScanResult:
    """Immutable outcome of one `run_gmgn_research_scan` call.
    `unavailable_reason` is set (and `candidates` always empty) exactly when
    the scan could not run at all: disabled, or enabled with no transport
    injected. Otherwise it is None and `candidates` holds at most
    GMGN_RESEARCH_MAX_WATCHLIST_IDENTITIES entries, one per configured
    watchlist identity."""

    runtime_version: str
    enabled: bool
    candidates: tuple[GmgnCandidateResearchResult, ...]
    unavailable_reason: Optional[str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "candidates", tuple(self.candidates)[:GMGN_RESEARCH_MAX_WATCHLIST_IDENTITIES]
        )
        if self.unavailable_reason is not None:
            object.__setattr__(self, "unavailable_reason", _bounded_text(self.unavailable_reason))


class _GmgnRuntimeEnvelopeError(RuntimeError):
    """Raised internally when a transport response's `data` field is
    missing or not shaped as this runtime expects for a given endpoint.
    Always caught immediately within `_research_one_candidate` — never
    escapes it, and never carries vendor-controlled text."""


def _identity_params(identity: GmgnWatchlistIdentity) -> dict[str, Any]:
    return {"chain": identity.chain, "address": identity.contract_address}


def _require_data_mapping(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise _GmgnRuntimeEnvelopeError("response envelope is not a mapping")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise _GmgnRuntimeEnvelopeError("response envelope 'data' is not an object")
    return data


def _require_data_sequence(payload: Any) -> Sequence[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        raise _GmgnRuntimeEnvelopeError("response envelope is not a mapping")
    data = payload.get("data")
    if not isinstance(data, list):
        raise _GmgnRuntimeEnvelopeError("response envelope 'data' is not a list")
    if any(not isinstance(item, Mapping) for item in data):
        raise _GmgnRuntimeEnvelopeError("response envelope 'data' contains a non-mapping item")
    return data[:MAX_CANDLES_PER_CANDIDATE]


def _unavailable(identity: GmgnWatchlistIdentity, reason: str) -> GmgnCandidateResearchResult:
    return GmgnCandidateResearchResult(
        chain=identity.chain,
        contract_address=identity.contract_address,
        status=UNAVAILABLE,
        reasons=(reason,),
        supporting_labels=(),
    )


async def _research_one_candidate(
    transport: SupportsGmgnResearchGet, identity: GmgnWatchlistIdentity, now: datetime,
) -> GmgnCandidateResearchResult:
    """Fetches, normalizes, and evaluates one watchlist identity.

    Every stage — transport request, envelope-shape check, normalization,
    evaluation — is isolated so a failure at any of them yields an
    `UNAVAILABLE` result for this identity alone rather than raising out of
    `run_gmgn_research_scan` or blocking any other identity.
    """
    params = _identity_params(identity)

    try:
        security_payload = await transport.get(SECURITY_ENDPOINT, params=params)
        pool_payload = await transport.get(POOL_INFO_ENDPOINT, params=params)
        structure_payload = await transport.get(STRUCTURE_ENDPOINT, params=params)
        candle_payload = await transport.get(
            CANDLE_ENDPOINT,
            params={**params, "resolution": CANDLE_RESOLUTION, "limit": MAX_CANDLES_PER_CANDIDATE},
        )
    except Exception:
        return _unavailable(identity, "transport request failed")

    try:
        security_item = _require_data_mapping(security_payload)
        pool_item = _require_data_mapping(pool_payload)
        structure_item = _require_data_mapping(structure_payload)
        candle_items = _require_data_sequence(candle_payload)
    except _GmgnRuntimeEnvelopeError:
        return _unavailable(identity, "response envelope did not match the expected shape")

    try:
        security = build_token_security_record(
            security_item, source_endpoint=SECURITY_ENDPOINT, observed_at=now,
        )
        liquidity = build_liquidity_pool_record(
            pool_item, source_endpoint=POOL_INFO_ENDPOINT, observed_at=now,
        )
        structure = build_holder_trader_structure_record(
            structure_item, source_endpoint=STRUCTURE_ENDPOINT, observed_at=now,
        )
        candles = tuple(
            build_candle_record(
                item,
                chain=identity.chain,
                contract_address=identity.contract_address,
                source_endpoint=CANDLE_ENDPOINT,
                observed_at=now,
            )
            for item in candle_items
        )
    except (GmgnNormalizationError, ValueError):
        # ValueError also covers a contract.py record's own field
        # validation (e.g. an out-of-range percent) — treated identically to
        # a normalization failure: isolate to this candidate, never
        # fabricate a substitute value.
        return _unavailable(identity, "vendor response failed normalization")

    try:
        evaluation = evaluate_research_candidate(
            chain=identity.chain,
            contract_address=identity.contract_address,
            security=security,
            liquidity=liquidity,
            structure=structure,
            candles=candles,
            now=now,
        )
    except Exception:
        return _unavailable(identity, "candidate evaluation failed")

    return GmgnCandidateResearchResult(
        chain=identity.chain,
        contract_address=identity.contract_address,
        status=evaluation.status,
        reasons=evaluation.reasons,
        supporting_labels=evaluation.supporting_labels,
    )


def _status_counts(candidates: Sequence[GmgnCandidateResearchResult]) -> dict[str, int]:
    counts = {status: 0 for status in _KNOWN_CANDIDATE_STATUSES}
    for candidate in candidates:
        if candidate.status in counts:
            counts[candidate.status] += 1
    return counts


def _log_scan_summary(*, scan_outcome: str, candidate_count: int, status_counts: Mapping[str, int]) -> None:
    """Emits the one bounded aggregate log line for a single enabled
    invocation. Every argument is a fixed field name, a bounded integer
    count, or a known status/outcome classification — never a chain,
    contract address, vendor payload, vendor text/label, exception text,
    credential, or environment value. See module docstring."""
    logger.info(
        "gmgn_research_scan_summary runtime_version=%s scan_outcome=%s candidate_count=%d "
        "rejected=%d watch=%d eligible_for_review=%d unavailable=%d",
        RUNTIME_VERSION,
        scan_outcome,
        candidate_count,
        status_counts[REJECTED],
        status_counts[WATCH],
        status_counts[ELIGIBLE_FOR_REVIEW],
        status_counts[UNAVAILABLE],
    )


async def run_gmgn_research_scan(
    settings: Settings,
    transport: Optional[SupportsGmgnResearchGet] = None,
    *,
    now: Optional[datetime] = None,
) -> GmgnResearchScanResult:
    """Default-off, fake-transport-only GMGN read-only research scan.

    Re-checks `settings.gmgn_research_enabled` at entry regardless of how
    it was invoked — `apex.main`'s scheduler registration is the primary
    gate, this is defense in depth. Never constructs
    `apex.research.gmgn.transport.GmgnResearchTransport`, imports `httpx`,
    reads `os.environ`/`.env`, opens a socket, or touches a database;
    `transport` must be injected by the caller (`apex.main` deliberately
    never injects a real one for this milestone).
    """
    if not settings.gmgn_research_enabled:
        return GmgnResearchScanResult(
            runtime_version=RUNTIME_VERSION, enabled=False, candidates=(), unavailable_reason=None,
        )
    if transport is None:
        _log_scan_summary(
            scan_outcome=SCAN_OUTCOME_NO_TRANSPORT, candidate_count=0, status_counts=_status_counts(()),
        )
        return GmgnResearchScanResult(
            runtime_version=RUNTIME_VERSION,
            enabled=True,
            candidates=(),
            unavailable_reason="no transport configured for this runtime invocation",
        )

    moment = now if now is not None else datetime.now(timezone.utc)
    identities = settings.gmgn_research_watchlist_identities[:GMGN_RESEARCH_MAX_WATCHLIST_IDENTITIES]

    candidates = tuple(
        [await _research_one_candidate(transport, identity, moment) for identity in identities]
    )
    _log_scan_summary(
        scan_outcome=SCAN_OUTCOME_COMPLETED,
        candidate_count=len(candidates),
        status_counts=_status_counts(candidates),
    )
    return GmgnResearchScanResult(
        runtime_version=RUNTIME_VERSION, enabled=True, candidates=candidates, unavailable_reason=None,
    )
