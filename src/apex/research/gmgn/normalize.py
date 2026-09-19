"""Builds immutable `contract.py` records from raw GMGN response mappings.

Only this module reads raw vendor mappings. It never trusts an optional
vendor field: every builder computes the canonical SHA-256 of the raw item
*before* dropping any unknown/unmapped fields, normalizes the chain and
preserves the contract address's exact case, and sanitizes any
attacker-controlled text (ticker, name, labels) by stripping Unicode
control/bidi-override characters and bounding length. A ticker or name can
never establish token identity — only `chain` + `contract_address` do (see
`contract.py`, `funnel.py`).
"""
from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from datetime import datetime
from typing import Any, Mapping, Optional

from apex.research.gmgn.contract import (
    PROVIDER_GMGN,
    CandleRecord,
    HolderTraderStructureRecord,
    LiquidityPoolRecord,
    SmartMoneyFlowRecord,
    TokenDiscoveryRecord,
    TokenSecurityRecord,
)
from apex.research.gmgn.contract import CONTRACT_VERSION as _CONTRACT_VERSION

MAX_SANITIZED_TEXT_LENGTH = 256

# Bidi override/isolate marks are not caught by Unicode category alone in
# every implementation; listed explicitly alongside the Cc/Cf category
# sweep for defense in depth.
_BIDI_CONTROL_CODEPOINTS: frozenset[int] = frozenset(
    {0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069}
)

_CHAIN_ALIASES: dict[str, str] = {
    "sol": "solana",
    "solana": "solana",
    "eth": "ethereum",
    "ethereum": "ethereum",
    "bsc": "bsc",
    "bnb": "bsc",
    "base": "base",
    "arb": "arbitrum",
    "arbitrum": "arbitrum",
    "tron": "tron",
    "trx": "tron",
}


class GmgnNormalizationError(ValueError):
    """Raised when a raw item is missing required identity/provenance or is
    otherwise not shaped like a valid GMGN response item. Fails closed —
    callers must treat this as "no record", never as a partial one."""


def canonical_json_sha256(value: Any) -> str:
    """Canonical (sorted-key, compact) SHA-256 of a JSON-serializable raw
    value — computed on the *raw* item, before any unknown/unmapped fields
    are dropped, so the hash is a faithful fingerprint of what the vendor
    actually returned."""
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def sanitize_untrusted_text(value: object, *, max_length: int = MAX_SANITIZED_TEXT_LENGTH) -> Optional[str]:
    """Best-effort, fail-safe sanitization of an untrusted vendor text field
    (ticker/name/label/etc). Strips Unicode control/format characters
    (category Cc/Cf) including bidi override/isolate marks, then bounds
    length. The result is always treated as inert descriptive text, never
    as instructions or as identity — a string like "ignore previous
    instructions" survives sanitization unchanged (aside from control-
    character stripping) and is simply stored as opaque display text.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    cleaned_chars = []
    for ch in value:
        if ord(ch) in _BIDI_CONTROL_CODEPOINTS:
            continue
        if unicodedata.category(ch) in ("Cc", "Cf"):
            continue
        cleaned_chars.append(ch)
    cleaned = "".join(cleaned_chars).strip()
    if not cleaned:
        return None
    return cleaned[:max_length]


def normalize_chain(raw_chain: object) -> str:
    """Maps a raw GMGN chain string onto the pilot's fixed normalized chain
    set. Fails closed (raises) on an unrecognized or non-string value
    rather than guessing."""
    if not isinstance(raw_chain, str):
        raise GmgnNormalizationError(f"chain must be a string, got {type(raw_chain).__name__}")
    normalized = _CHAIN_ALIASES.get(raw_chain.strip().lower())
    if normalized is None:
        raise GmgnNormalizationError(f"unrecognized/unsupported chain: {raw_chain!r}")
    return normalized


def normalize_contract_address(raw_address: object) -> str:
    """Preserves the contract address's exact case — only whitespace is
    trimmed. Fails closed on a missing/empty/non-string address."""
    if not isinstance(raw_address, str):
        raise GmgnNormalizationError(f"contract address must be a string, got {type(raw_address).__name__}")
    address = raw_address.strip()
    if not address:
        raise GmgnNormalizationError("contract address must not be empty")
    return address


def _first_present(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _get_finite(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> Optional[float]:
    value = _first_present(mapping, keys)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _get_bool(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> Optional[bool]:
    value = _first_present(mapping, keys)
    return value if isinstance(value, bool) else None


def _get_text(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> Optional[str]:
    return sanitize_untrusted_text(_first_present(mapping, keys))


def _require_mapping(raw_item: object) -> Mapping[str, Any]:
    if not isinstance(raw_item, Mapping):
        raise GmgnNormalizationError(f"raw item must be a mapping, got {type(raw_item).__name__}")
    return raw_item


def _identity(raw_item: Mapping[str, Any]) -> tuple[str, str]:
    chain = normalize_chain(_first_present(raw_item, ("chain", "chain_id")))
    address = normalize_contract_address(
        _first_present(raw_item, ("address", "contract_address", "token_address", "mint"))
    )
    return chain, address


def build_token_discovery_record(
    raw_item: Mapping[str, Any],
    *,
    source_endpoint: str,
    observed_at: datetime,
    source_timestamp: Optional[int] = None,
) -> TokenDiscoveryRecord:
    raw_item = _require_mapping(raw_item)
    raw_hash = canonical_json_sha256(raw_item)
    chain, address = _identity(raw_item)
    return TokenDiscoveryRecord(
        provider=PROVIDER_GMGN,
        contract_version=_CONTRACT_VERSION,
        chain=chain,
        contract_address=address,
        observed_at=observed_at,
        source_endpoint=source_endpoint,
        source_timestamp=source_timestamp,
        raw_response_sha256=raw_hash,
        symbol=_get_text(raw_item, ("symbol",)),
        name=_get_text(raw_item, ("name",)),
        rank_score=_get_finite(raw_item, ("rank_score", "score", "hot_score")),
        rank_label=_get_text(raw_item, ("rank_label", "tag", "label")),
    )


def build_token_security_record(
    raw_item: Mapping[str, Any],
    *,
    source_endpoint: str,
    observed_at: datetime,
    source_timestamp: Optional[int] = None,
) -> TokenSecurityRecord:
    raw_item = _require_mapping(raw_item)
    raw_hash = canonical_json_sha256(raw_item)
    chain, address = _identity(raw_item)
    return TokenSecurityRecord(
        provider=PROVIDER_GMGN,
        contract_version=_CONTRACT_VERSION,
        chain=chain,
        contract_address=address,
        observed_at=observed_at,
        source_endpoint=source_endpoint,
        source_timestamp=source_timestamp,
        raw_response_sha256=raw_hash,
        is_open_source=_get_bool(raw_item, ("is_open_source",)),
        is_mintable=_get_bool(raw_item, ("is_mintable",)),
        is_freezable=_get_bool(raw_item, ("is_freezable",)),
        is_honeypot=_get_bool(raw_item, ("is_honeypot",)),
        top10_holder_percent=_get_finite(raw_item, ("top10_holder_percent", "top_10_holder_percent")),
        lp_locked_percent=_get_finite(raw_item, ("lp_locked_percent", "lp_lock_percent")),
        buy_tax_percent=_get_finite(raw_item, ("buy_tax_percent", "buy_tax")),
        sell_tax_percent=_get_finite(raw_item, ("sell_tax_percent", "sell_tax")),
    )


def build_liquidity_pool_record(
    raw_item: Mapping[str, Any],
    *,
    source_endpoint: str,
    observed_at: datetime,
    source_timestamp: Optional[int] = None,
) -> LiquidityPoolRecord:
    raw_item = _require_mapping(raw_item)
    raw_hash = canonical_json_sha256(raw_item)
    chain, address = _identity(raw_item)
    return LiquidityPoolRecord(
        provider=PROVIDER_GMGN,
        contract_version=_CONTRACT_VERSION,
        chain=chain,
        contract_address=address,
        observed_at=observed_at,
        source_endpoint=source_endpoint,
        source_timestamp=source_timestamp,
        raw_response_sha256=raw_hash,
        pool_address=_get_text(raw_item, ("pool_address",)),
        dex=_get_text(raw_item, ("dex",)),
        quote_symbol=_get_text(raw_item, ("quote_symbol",)),
        liquidity_usd=_get_finite(raw_item, ("liquidity_usd", "liquidity")),
        base_reserve=_get_finite(raw_item, ("base_reserve",)),
        quote_reserve=_get_finite(raw_item, ("quote_reserve",)),
    )


def build_holder_trader_structure_record(
    raw_item: Mapping[str, Any],
    *,
    source_endpoint: str,
    observed_at: datetime,
    source_timestamp: Optional[int] = None,
) -> HolderTraderStructureRecord:
    raw_item = _require_mapping(raw_item)
    raw_hash = canonical_json_sha256(raw_item)
    chain, address = _identity(raw_item)
    return HolderTraderStructureRecord(
        provider=PROVIDER_GMGN,
        contract_version=_CONTRACT_VERSION,
        chain=chain,
        contract_address=address,
        observed_at=observed_at,
        source_endpoint=source_endpoint,
        source_timestamp=source_timestamp,
        raw_response_sha256=raw_hash,
        holder_count=_get_finite(raw_item, ("holder_count",)),
        top10_holder_percent=_get_finite(raw_item, ("top10_holder_percent", "top_10_holder_percent")),
        top_trader_count=_get_finite(raw_item, ("top_trader_count",)),
        top_trader_percent=_get_finite(raw_item, ("top_trader_percent",)),
        insider_percent=_get_finite(raw_item, ("insider_percent",)),
    )


def build_smart_money_flow_record(
    raw_item: Mapping[str, Any],
    *,
    source_endpoint: str,
    observed_at: datetime,
    source_timestamp: Optional[int] = None,
) -> SmartMoneyFlowRecord:
    raw_item = _require_mapping(raw_item)
    raw_hash = canonical_json_sha256(raw_item)
    chain, address = _identity(raw_item)
    return SmartMoneyFlowRecord(
        provider=PROVIDER_GMGN,
        contract_version=_CONTRACT_VERSION,
        chain=chain,
        contract_address=address,
        observed_at=observed_at,
        source_endpoint=source_endpoint,
        source_timestamp=source_timestamp,
        raw_response_sha256=raw_hash,
        smart_money_wallet_count=_get_finite(raw_item, ("smart_money_wallet_count",)),
        net_inflow_usd=_get_finite(raw_item, ("net_inflow_usd",)),
        flow_direction=_get_text(raw_item, ("flow_direction",)),
        signal_label=_get_text(raw_item, ("signal_label",)),
    )


def build_candle_record(
    raw_item: Mapping[str, Any],
    *,
    chain: str,
    contract_address: str,
    source_endpoint: str,
    observed_at: datetime,
    source_timestamp: Optional[int] = None,
) -> CandleRecord:
    """Candle rows from `/v1/market/token_kline` do not carry the token's
    own chain/address per row, so the caller (which already knows which
    token it requested candles for) supplies pre-normalized `chain`/
    `contract_address` directly rather than this function re-deriving
    them from the row.
    """
    raw_item = _require_mapping(raw_item)
    raw_hash = canonical_json_sha256(raw_item)
    return CandleRecord(
        provider=PROVIDER_GMGN,
        contract_version=_CONTRACT_VERSION,
        chain=normalize_chain(chain),
        contract_address=normalize_contract_address(contract_address),
        observed_at=observed_at,
        source_endpoint=source_endpoint,
        source_timestamp=source_timestamp,
        raw_response_sha256=raw_hash,
        interval=_get_text(raw_item, ("interval",)),
        candle_open_time=_get_finite(raw_item, ("time", "open_time", "t")),
        open=_get_finite(raw_item, ("open", "o")),
        high=_get_finite(raw_item, ("high", "h")),
        low=_get_finite(raw_item, ("low", "l")),
        close=_get_finite(raw_item, ("close", "c")),
        volume=_get_finite(raw_item, ("volume", "v")),
    )
