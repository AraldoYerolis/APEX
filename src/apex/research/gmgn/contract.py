"""Versioned, immutable research record contracts for the GMGN meme-coin
research pilot.

Every record here is a frozen dataclass carrying strict identity/provenance:
which vendor, which contract version, the normalized chain, the full
case-preserved contract address, when APEX observed it (UTC), which
allowlisted endpoint it came from, the vendor's own optional timestamp for
the data, and a canonical SHA-256 of the raw response item it was built
from. None of this is a signal, alert, or trade instruction — see
`apex/research/gmgn/__init__.py` and `funnel.py`.

Field values here are intentionally the pilot's own explicit, documented
mapping of a small subset of GMGN's response shape (not a copy of vendor
code, and not a claim of completeness) — see `normalize.py`, which is the
only place that reads raw vendor mappings and constructs these records.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import ClassVar, Optional

CONTRACT_VERSION = "gmgn_research_v0_1"

PROVIDER_GMGN = "gmgn"

# Canonical normalized chain identifiers this pilot understands. normalize.py
# maps GMGN's own raw chain strings onto this fixed set; any unrecognized raw
# chain value fails closed rather than being guessed at (see
# normalize.normalize_chain).
SUPPORTED_CHAINS: frozenset[str] = frozenset(
    {"solana", "ethereum", "bsc", "base", "arbitrum", "tron"}
)

GET = "GET"
POST = "POST"

# Single canonical source of truth for the exact (method, path) allowlist.
# transport.py imports this rather than keeping its own copy, so the set
# enforced before any I/O and the set accepted as valid record provenance
# can never drift apart.
ALLOWED_ENDPOINTS: frozenset[tuple[str, str]] = frozenset(
    {
        (GET, "/v1/token/info"),
        (GET, "/v1/token/security"),
        (GET, "/v1/token/pool_info"),
        (GET, "/v1/market/token_top_holders"),
        (GET, "/v1/market/token_top_traders"),
        (GET, "/v1/market/token_kline"),
        (GET, "/v1/market/rank"),
        (GET, "/v1/market/search"),
        (GET, "/v1/user/wallet_activity"),
        (GET, "/v1/user/wallet_stats"),
        (GET, "/v1/user/kol"),
        (GET, "/v1/user/smartmoney"),
        (GET, "/v1/user/created_tokens"),
        (POST, "/v1/user/wallet_profits"),
        (POST, "/v1/trenches"),
        (POST, "/v1/market/token_signal"),
        (POST, "/v1/market/hot_searches"),
    }
)
ALLOWED_SOURCE_PATHS: frozenset[str] = frozenset(path for _, path in ALLOWED_ENDPOINTS)

_SHA256_HEX_LENGTH = 64
_HEX_DIGITS = frozenset("0123456789abcdef")
MAX_TEXT_FIELD_LENGTH = 256


# ---------------------------------------------------------------------------
# Shared field validation — used by every record's __post_init__ below.
# ---------------------------------------------------------------------------

def _require_finite(field_name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite real number, got {type(value).__name__}")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"{field_name} must be finite, got {numeric!r}")
    return numeric


def _require_optional_finite(field_name: str, value: Optional[object]) -> Optional[float]:
    if value is None:
        return None
    return _require_finite(field_name, value)


def _require_optional_nonneg_int(field_name: str, value: Optional[object]) -> Optional[int]:
    if value is None:
        return None
    numeric = _require_finite(field_name, value)
    if numeric < 0:
        raise ValueError(f"{field_name} must be >= 0, got {numeric!r}")
    if numeric != math.trunc(numeric):
        raise ValueError(f"{field_name} must be an integral value, got {numeric!r}")
    return int(numeric)


def _require_optional_percent(
    field_name: str, value: Optional[object], *, max_value: float = 100.0
) -> Optional[float]:
    if value is None:
        return None
    numeric = _require_finite(field_name, value)
    if numeric < 0.0 or numeric > max_value:
        raise ValueError(f"{field_name} must be within [0, {max_value}], got {numeric!r}")
    return numeric


def _require_optional_bounded_text(field_name: str, value: Optional[object]) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string or None")
    if len(value) > MAX_TEXT_FIELD_LENGTH:
        raise ValueError(f"{field_name} exceeds max length {MAX_TEXT_FIELD_LENGTH}")
    return value


def _require_nonempty_str(field_name: str, value: object) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_sha256_hex(field_name: str, value: object) -> str:
    if not isinstance(value, str) or len(value) != _SHA256_HEX_LENGTH:
        raise ValueError(f"{field_name} must be a {_SHA256_HEX_LENGTH}-char hex sha256 digest")
    lowered = value.lower()
    if not set(lowered) <= _HEX_DIGITS:
        raise ValueError(f"{field_name} must be hex-encoded")
    return lowered


def _require_utc_datetime(field_name: str, value: object) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{field_name} must be a timezone-aware UTC datetime")
    return value


def _require_source_endpoint(field_name: str, value: object) -> str:
    path = _require_nonempty_str(field_name, value)
    if path not in ALLOWED_SOURCE_PATHS:
        raise ValueError(f"{field_name} {path!r} is not an allowlisted GMGN research endpoint")
    return path


def _require_chain(field_name: str, value: object) -> str:
    chain = _require_nonempty_str(field_name, value)
    if chain not in SUPPORTED_CHAINS:
        raise ValueError(f"{field_name} {chain!r} is not a supported normalized chain")
    return chain


def _require_contract_address(field_name: str, value: object) -> str:
    # Full case preserved deliberately: some chains treat case as identity-
    # relevant (checksums) — never lowercase/uppercase this.
    return _require_nonempty_str(field_name, value)


@dataclass(frozen=True)
class ResearchRecord:
    """Shared identity/provenance carried by every GMGN research record.

    `source_timestamp` is the vendor's own optional Unix-seconds timestamp
    for the underlying data (distinct from `observed_at`, which is always
    APEX's own UTC capture time). `raw_response_sha256` is the canonical
    SHA-256 of the raw response item this record was built from, computed
    before any unknown/unmapped fields were dropped (see
    `normalize.canonical_json_sha256`).
    """

    provider: str
    contract_version: str
    chain: str
    contract_address: str
    observed_at: datetime
    source_endpoint: str
    source_timestamp: Optional[int]
    raw_response_sha256: str

    # Leaf subclasses may restrict which of the allowlisted endpoints are
    # valid provenance for that record type; empty means "any allowlisted
    # endpoint" (only this base class itself uses that default). ClassVar
    # so dataclass does not treat it as a constructor field (a dataclass
    # field with a default here would force every subclass field to also
    # have a default, which they deliberately do not).
    _ALLOWED_SOURCE_ENDPOINTS: ClassVar[frozenset[str]] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _require_nonempty_str("provider", self.provider))
        if self.provider != PROVIDER_GMGN:
            raise ValueError(f"provider must be {PROVIDER_GMGN!r}, got {self.provider!r}")
        object.__setattr__(
            self, "contract_version", _require_nonempty_str("contract_version", self.contract_version)
        )
        if self.contract_version != CONTRACT_VERSION:
            raise ValueError(
                f"contract_version must be exactly {CONTRACT_VERSION!r}, got {self.contract_version!r}"
            )
        object.__setattr__(self, "chain", _require_chain("chain", self.chain))
        object.__setattr__(
            self, "contract_address", _require_contract_address("contract_address", self.contract_address)
        )
        object.__setattr__(self, "observed_at", _require_utc_datetime("observed_at", self.observed_at))
        object.__setattr__(
            self, "source_endpoint", _require_source_endpoint("source_endpoint", self.source_endpoint)
        )
        allowed_for_type = type(self)._ALLOWED_SOURCE_ENDPOINTS
        if allowed_for_type and self.source_endpoint not in allowed_for_type:
            raise ValueError(
                f"source_endpoint {self.source_endpoint!r} is not valid provenance for "
                f"{type(self).__name__}"
            )
        object.__setattr__(
            self, "source_timestamp", _require_optional_nonneg_int("source_timestamp", self.source_timestamp)
        )
        object.__setattr__(
            self, "raw_response_sha256", _require_sha256_hex("raw_response_sha256", self.raw_response_sha256)
        )


@dataclass(frozen=True)
class TokenDiscoveryRecord(ResearchRecord):
    """Basic token identity/discovery listing from `/v1/token/info`,
    `/v1/market/rank`, or `/v1/market/search`.

    Purely a nomination signal: `symbol`/`name`/`rank_score`/`rank_label`
    are untrusted, sanitized display text/scores and can never by
    themselves establish token identity or pass a candidate (see
    `funnel.py`).
    """

    symbol: Optional[str]
    name: Optional[str]
    rank_score: Optional[float]
    rank_label: Optional[str]

    _ALLOWED_SOURCE_ENDPOINTS = frozenset(
        {"/v1/token/info", "/v1/market/rank", "/v1/market/search"}
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "symbol", _require_optional_bounded_text("symbol", self.symbol))
        object.__setattr__(self, "name", _require_optional_bounded_text("name", self.name))
        object.__setattr__(self, "rank_score", _require_optional_finite("rank_score", self.rank_score))
        object.__setattr__(
            self, "rank_label", _require_optional_bounded_text("rank_label", self.rank_label)
        )


@dataclass(frozen=True)
class TokenSecurityRecord(ResearchRecord):
    """Contract/security posture from `/v1/token/security`.

    Boolean fields are phrased so that `True` always means "riskier" (e.g.
    `is_mintable=True` means mint authority is still active) to keep
    `funnel.py`'s hard-risk checks single-direction and easy to audit.
    """

    is_open_source: Optional[bool]
    is_mintable: Optional[bool]
    is_freezable: Optional[bool]
    is_honeypot: Optional[bool]
    top10_holder_percent: Optional[float]
    lp_locked_percent: Optional[float]
    buy_tax_percent: Optional[float]
    sell_tax_percent: Optional[float]

    _ALLOWED_SOURCE_ENDPOINTS = frozenset({"/v1/token/security"})

    def __post_init__(self) -> None:
        super().__post_init__()
        for name in ("is_open_source", "is_mintable", "is_freezable", "is_honeypot"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"{name} must be a bool or None")
        for name in (
            "top10_holder_percent",
            "lp_locked_percent",
            "buy_tax_percent",
            "sell_tax_percent",
        ):
            object.__setattr__(self, name, _require_optional_percent(name, getattr(self, name)))


@dataclass(frozen=True)
class LiquidityPoolRecord(ResearchRecord):
    """Liquidity/pool composition from `/v1/token/pool_info`."""

    pool_address: Optional[str]
    dex: Optional[str]
    quote_symbol: Optional[str]
    liquidity_usd: Optional[float]
    base_reserve: Optional[float]
    quote_reserve: Optional[float]

    _ALLOWED_SOURCE_ENDPOINTS = frozenset({"/v1/token/pool_info"})

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(
            self, "pool_address", _require_optional_bounded_text("pool_address", self.pool_address)
        )
        object.__setattr__(self, "dex", _require_optional_bounded_text("dex", self.dex))
        object.__setattr__(
            self, "quote_symbol", _require_optional_bounded_text("quote_symbol", self.quote_symbol)
        )
        for name in ("liquidity_usd", "base_reserve", "quote_reserve"):
            object.__setattr__(self, name, _require_optional_finite(name, getattr(self, name)))


@dataclass(frozen=True)
class HolderTraderStructureRecord(ResearchRecord):
    """Holder/trader concentration structure from
    `/v1/market/token_top_holders` or `/v1/market/token_top_traders`.
    """

    holder_count: Optional[int]
    top10_holder_percent: Optional[float]
    top_trader_count: Optional[int]
    top_trader_percent: Optional[float]
    insider_percent: Optional[float]

    _ALLOWED_SOURCE_ENDPOINTS = frozenset(
        {"/v1/market/token_top_holders", "/v1/market/token_top_traders"}
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        for name in ("holder_count", "top_trader_count"):
            object.__setattr__(self, name, _require_optional_nonneg_int(name, getattr(self, name)))
        for name in ("top10_holder_percent", "top_trader_percent", "insider_percent"):
            object.__setattr__(self, name, _require_optional_percent(name, getattr(self, name)))


@dataclass(frozen=True)
class SmartMoneyFlowRecord(ResearchRecord):
    """Smart-money/KOL flow context from `/v1/user/smartmoney`,
    `/v1/user/wallet_activity`, or `/v1/market/token_signal`.

    Like `TokenDiscoveryRecord`, this is support/nomination context only —
    see `funnel.py`.
    """

    smart_money_wallet_count: Optional[int]
    net_inflow_usd: Optional[float]
    flow_direction: Optional[str]
    signal_label: Optional[str]

    _ALLOWED_SOURCE_ENDPOINTS = frozenset(
        {"/v1/user/smartmoney", "/v1/user/wallet_activity", "/v1/market/token_signal"}
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(
            self,
            "smart_money_wallet_count",
            _require_optional_nonneg_int("smart_money_wallet_count", self.smart_money_wallet_count),
        )
        object.__setattr__(
            self, "net_inflow_usd", _require_optional_finite("net_inflow_usd", self.net_inflow_usd)
        )
        object.__setattr__(
            self, "flow_direction", _require_optional_bounded_text("flow_direction", self.flow_direction)
        )
        object.__setattr__(
            self, "signal_label", _require_optional_bounded_text("signal_label", self.signal_label)
        )


@dataclass(frozen=True)
class CandleRecord(ResearchRecord):
    """A single OHLCV candle from `/v1/market/token_kline`, used only for
    independent deterministic confirmation in `funnel.py` — never as an
    entry/exit/trade signal.
    """

    interval: Optional[str]
    candle_open_time: Optional[int]
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    close: Optional[float]
    volume: Optional[float]

    _ALLOWED_SOURCE_ENDPOINTS = frozenset({"/v1/market/token_kline"})

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "interval", _require_optional_bounded_text("interval", self.interval))
        object.__setattr__(
            self, "candle_open_time", _require_optional_nonneg_int("candle_open_time", self.candle_open_time)
        )
        for name in ("open", "high", "low", "close", "volume"):
            object.__setattr__(self, name, _require_optional_finite(name, getattr(self, name)))
