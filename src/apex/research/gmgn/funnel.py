"""Pure, deterministic research-candidate funnel for the GMGN pilot.

This is research classification only — it never produces a trade, entry,
stop, target, position size, leverage, alert, or order of any kind. The
only outputs are one of three statuses (`REJECTED`, `WATCH`,
`ELIGIBLE_FOR_REVIEW`) plus human-readable reasons and supporting labels.

Discovery/rank/signal/smart-money vendor labels and scores
(`TokenDiscoveryRecord`, `SmartMoneyFlowRecord`) may only nominate/support
a candidate — they are surfaced as `supporting_labels` for context but can
never, by themselves, gate a candidate into `ELIGIBLE_FOR_REVIEW`, and a
manipulated/extreme label or score in either of them has zero effect on
the status computed below. Reaching `ELIGIBLE_FOR_REVIEW` requires:
consistent identity across every evidence record supplied, freshness,
complete security/liquidity/holder-trader evidence, passing every hard-risk
check, and independent deterministic candle confirmation. Missing,
contradictory, or stale evidence always rejects or withholds — it never
defaults to eligible.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

from apex.research.gmgn.contract import (
    SUPPORTED_CHAINS,
    CandleRecord,
    HolderTraderStructureRecord,
    LiquidityPoolRecord,
    ResearchRecord,
    SmartMoneyFlowRecord,
    TokenDiscoveryRecord,
    TokenSecurityRecord,
)

CandidateStatus = str  # one of the three literal values below, kept as str
# so callers do not need typing_extensions for Literal narrowing.
REJECTED: CandidateStatus = "REJECTED"
WATCH: CandidateStatus = "WATCH"
ELIGIBLE_FOR_REVIEW: CandidateStatus = "ELIGIBLE_FOR_REVIEW"

FUNNEL_VERSION = "gmgn_research_funnel_v0_1"

MAX_STALENESS_SECONDS = 15 * 60  # evidence older than this is withheld, not trusted
MAX_TOP10_HOLDER_PERCENT = 50.0
MIN_LP_LOCKED_PERCENT = 80.0
MAX_BUY_TAX_PERCENT = 10.0
MAX_SELL_TAX_PERCENT = 10.0
MIN_LIQUIDITY_USD = 5_000.0
MIN_CANDLES_FOR_CONFIRMATION = 3


@dataclass(frozen=True)
class CandidateEvaluation:
    """Immutable, research-only funnel outcome. Never contains a trade,
    alert, order, size, or price target of any kind."""

    funnel_version: str
    status: CandidateStatus
    chain: str
    contract_address: str
    reasons: tuple[str, ...]
    supporting_labels: tuple[str, ...]


def _seconds_between(a: datetime, b: datetime) -> float:
    return abs((a - b).total_seconds())


def _identity_matches(record: ResearchRecord, chain: str, contract_address: str) -> bool:
    return record.chain == chain and record.contract_address == contract_address


def _collect_supporting_labels(
    discovery: Optional[TokenDiscoveryRecord],
    smart_money: Optional[SmartMoneyFlowRecord],
) -> list[str]:
    labels: list[str] = []
    if discovery is not None:
        if discovery.rank_label:
            labels.append(f"discovery:rank_label={discovery.rank_label}")
        if discovery.rank_score is not None:
            labels.append(f"discovery:rank_score={discovery.rank_score}")
    if smart_money is not None:
        if smart_money.signal_label:
            labels.append(f"smart_money:signal_label={smart_money.signal_label}")
        if smart_money.flow_direction:
            labels.append(f"smart_money:flow_direction={smart_money.flow_direction}")
    return labels


def _confirm_candles(
    candles: Sequence[CandleRecord],
    *,
    chain: str,
    contract_address: str,
    now: datetime,
    max_staleness_seconds: float,
) -> tuple[bool, bool, list[str]]:
    """Returns `(confirmed, contradictory, reasons)`. `contradictory` marks
    internally-inconsistent candle data (a hard reject), distinct from
    merely-incomplete or unconvincing candle evidence (a withhold).

    Structural validity (identity, freshness, completeness, strictly
    increasing open_time, internally consistent OHLC, non-negative volume)
    is necessary but not sufficient for confirmation. Confirmation also
    requires a simple, deterministic, neutral price/volume condition: at
    least `MIN_CANDLES_FOR_CONFIRMATION` candles, every candle carrying
    strictly positive volume, the latest close strictly greater than the
    first close (recent price participation), and the latest volume at
    least the mean volume of the preceding candles (recent volume
    participation). Vendor discovery/rank/smart-money labels play no part
    in this evaluation — only the candle OHLCV evidence itself.
    """
    reasons: list[str] = []
    if len(candles) < MIN_CANDLES_FOR_CONFIRMATION:
        return False, False, [f"fewer than {MIN_CANDLES_FOR_CONFIRMATION} candles available"]

    previous_open_time: Optional[int] = None
    for candle in candles:
        if not _identity_matches(candle, chain, contract_address):
            return False, True, ["candle identity does not match candidate identity"]
        if _seconds_between(now, candle.observed_at) > max_staleness_seconds or candle.observed_at > now:
            reasons.append("stale or future-dated candle observation")
            return False, False, reasons
        if None in (candle.open, candle.high, candle.low, candle.close, candle.volume):
            reasons.append("candle missing required OHLCV field")
            return False, False, reasons
        if candle.volume < 0:
            return False, True, ["candle has negative volume"]
        lo, hi = candle.low, candle.high
        body_low = min(candle.open, candle.close)
        body_high = max(candle.open, candle.close)
        if not (lo <= body_low <= body_high <= hi):
            return False, True, ["candle OHLC values are internally inconsistent"]
        if candle.candle_open_time is None:
            reasons.append("candle missing open_time")
            return False, False, reasons
        if previous_open_time is not None and candle.candle_open_time <= previous_open_time:
            return False, True, ["candle open_time is not strictly increasing"]
        previous_open_time = candle.candle_open_time

    if any(candle.volume <= 0 for candle in candles):
        reasons.append("candle volume is not positive")
        return False, False, reasons

    first_candle = candles[0]
    latest_candle = candles[-1]
    preceding_volumes = [candle.volume for candle in candles[:-1]]
    mean_preceding_volume = sum(preceding_volumes) / len(preceding_volumes)

    price_participation = latest_candle.close > first_candle.close
    volume_participation = mean_preceding_volume > 0 and latest_candle.volume >= mean_preceding_volume
    if not (price_participation and volume_participation):
        reasons.append("neutral price/volume confirmation not met")
        return False, False, reasons

    return True, False, []


def evaluate_research_candidate(
    *,
    chain: str,
    contract_address: str,
    security: Optional[TokenSecurityRecord] = None,
    liquidity: Optional[LiquidityPoolRecord] = None,
    structure: Optional[HolderTraderStructureRecord] = None,
    candles: Sequence[CandleRecord] = (),
    discovery: Optional[TokenDiscoveryRecord] = None,
    smart_money: Optional[SmartMoneyFlowRecord] = None,
    now: datetime,
    max_staleness_seconds: float = MAX_STALENESS_SECONDS,
) -> CandidateEvaluation:
    """Pure function: same inputs always produce the same
    `CandidateEvaluation`. No I/O, no clock reads (caller supplies `now`),
    no randomness."""
    supporting_labels = tuple(_collect_supporting_labels(discovery, smart_money))

    def _result(status: CandidateStatus, reasons: list[str]) -> CandidateEvaluation:
        return CandidateEvaluation(
            funnel_version=FUNNEL_VERSION,
            status=status,
            chain=chain,
            contract_address=contract_address,
            reasons=tuple(reasons),
            supporting_labels=supporting_labels,
        )

    if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(None):
        return _result(REJECTED, ["now must be a timezone-aware UTC datetime"])
    if chain not in SUPPORTED_CHAINS or not isinstance(contract_address, str) or not contract_address.strip():
        return _result(REJECTED, ["invalid candidate identity (chain/contract_address)"])

    # ---- Identity consistency across every supplied evidence record -----
    provided: list[tuple[str, ResearchRecord]] = []
    for name, record in (
        ("discovery", discovery),
        ("security", security),
        ("liquidity", liquidity),
        ("structure", structure),
        ("smart_money", smart_money),
    ):
        if record is not None:
            provided.append((name, record))
    for name, record in provided:
        if not _identity_matches(record, chain, contract_address):
            return _result(REJECTED, [f"{name} evidence identity does not match candidate identity"])
    for candle in candles:
        if not _identity_matches(candle, chain, contract_address):
            return _result(REJECTED, ["candle evidence identity does not match candidate identity"])

    # ---- Freshness (only required/hard-risk-bearing evidence gates) -----
    stale_reasons: list[str] = []
    for name, record in provided:
        if name in ("discovery", "smart_money"):
            continue  # support-only context never gates on its own
        if record.observed_at > now:
            stale_reasons.append(f"{name} evidence is timestamped in the future")
        elif _seconds_between(now, record.observed_at) > max_staleness_seconds:
            stale_reasons.append(f"{name} evidence is stale")
    if stale_reasons:
        return _result(WATCH, stale_reasons)

    # ---- Completeness: security/liquidity/structure are all required ----
    missing = [
        name
        for name, record in (("security", security), ("liquidity", liquidity), ("structure", structure))
        if record is None
    ]
    if missing:
        return _result(WATCH, [f"missing required evidence: {', '.join(missing)}"])

    # mypy/type-narrowing helpers — completeness above guarantees non-None.
    assert security is not None and liquidity is not None and structure is not None

    # ---- Hard-risk checks (single-direction: True/exceeding == bad) -----
    reject_reasons: list[str] = []
    watch_reasons: list[str] = []

    def _bool_risk(value: Optional[bool], *, bad_reason: str, unknown_reason: str) -> None:
        if value is None:
            watch_reasons.append(unknown_reason)
        elif value is True:
            reject_reasons.append(bad_reason)

    def _max_risk(value: Optional[float], *, limit: float, bad_reason: str, unknown_reason: str) -> None:
        if value is None:
            watch_reasons.append(unknown_reason)
        elif value > limit:
            reject_reasons.append(bad_reason)

    def _min_risk(value: Optional[float], *, limit: float, bad_reason: str, unknown_reason: str) -> None:
        if value is None:
            watch_reasons.append(unknown_reason)
        elif value < limit:
            reject_reasons.append(bad_reason)

    _bool_risk(
        security.is_honeypot,
        bad_reason="security: honeypot flagged", unknown_reason="security: honeypot status unknown",
    )
    _bool_risk(
        security.is_mintable,
        bad_reason="security: mint authority still active", unknown_reason="security: mint authority status unknown",
    )
    _bool_risk(
        security.is_freezable,
        bad_reason="security: token is freezable", unknown_reason="security: freezable status unknown",
    )
    _max_risk(
        security.top10_holder_percent,
        limit=MAX_TOP10_HOLDER_PERCENT,
        bad_reason=f"security: top10 holder percent exceeds {MAX_TOP10_HOLDER_PERCENT}",
        unknown_reason="security: top10 holder percent unknown",
    )
    _min_risk(
        security.lp_locked_percent,
        limit=MIN_LP_LOCKED_PERCENT,
        bad_reason=f"security: lp locked percent below {MIN_LP_LOCKED_PERCENT}",
        unknown_reason="security: lp locked percent unknown",
    )
    _max_risk(
        security.buy_tax_percent,
        limit=MAX_BUY_TAX_PERCENT,
        bad_reason=f"security: buy tax exceeds {MAX_BUY_TAX_PERCENT}",
        unknown_reason="security: buy tax unknown",
    )
    _max_risk(
        security.sell_tax_percent,
        limit=MAX_SELL_TAX_PERCENT,
        bad_reason=f"security: sell tax exceeds {MAX_SELL_TAX_PERCENT}",
        unknown_reason="security: sell tax unknown",
    )
    _min_risk(
        liquidity.liquidity_usd,
        limit=MIN_LIQUIDITY_USD,
        bad_reason=f"liquidity: below {MIN_LIQUIDITY_USD} USD",
        unknown_reason="liquidity: liquidity_usd unknown",
    )
    if structure.top10_holder_percent is None:
        watch_reasons.append("structure: top10 holder percent unknown")
    elif structure.top10_holder_percent > MAX_TOP10_HOLDER_PERCENT:
        reject_reasons.append(f"structure: top10 holder percent exceeds {MAX_TOP10_HOLDER_PERCENT}")

    if reject_reasons:
        return _result(REJECTED, reject_reasons)

    # ---- Independent deterministic candle confirmation -------------------
    confirmed, contradictory, candle_reasons = _confirm_candles(
        candles,
        chain=chain,
        contract_address=contract_address,
        now=now,
        max_staleness_seconds=max_staleness_seconds,
    )
    if contradictory:
        return _result(REJECTED, candle_reasons)
    if not confirmed:
        return _result(WATCH, watch_reasons + candle_reasons)

    if watch_reasons:
        return _result(WATCH, watch_reasons)

    return _result(ELIGIBLE_FOR_REVIEW, [])
