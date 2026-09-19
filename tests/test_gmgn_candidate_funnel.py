"""Tests for apex.research.gmgn.funnel (pure, deterministic research-candidate
funnel).

Covers: distinct REJECTED/WATCH/ELIGIBLE_FOR_REVIEW states, partial/stale/
contradictory evidence handling, the neutral OHLCV price/volume candle
confirmation (positive volume on every candle, strict price participation,
volume participation against the preceding mean), manipulated discovery/
smart-money labels never affecting status, identity consistency across every
supplied evidence record, and ticker/address identity separation. No
network, no vendor package, no randomness — every test asserts the same
inputs always produce the same result.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from apex.research.gmgn.contract import (
    CONTRACT_VERSION,
    PROVIDER_GMGN,
    CandleRecord,
    HolderTraderStructureRecord,
    LiquidityPoolRecord,
    SmartMoneyFlowRecord,
    TokenDiscoveryRecord,
    TokenSecurityRecord,
)
from apex.research.gmgn.funnel import (
    ELIGIBLE_FOR_REVIEW,
    REJECTED,
    WATCH,
    evaluate_research_candidate,
)

UTC_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
VALID_SHA256 = "b" * 64
CHAIN = "solana"
ADDRESS = "Aabbcc111"


def _security(**overrides) -> TokenSecurityRecord:
    kwargs = dict(
        provider=PROVIDER_GMGN,
        contract_version=CONTRACT_VERSION,
        chain=CHAIN,
        contract_address=ADDRESS,
        observed_at=UTC_NOW,
        source_endpoint="/v1/token/security",
        source_timestamp=None,
        raw_response_sha256=VALID_SHA256,
        is_open_source=True,
        is_mintable=False,
        is_freezable=False,
        is_honeypot=False,
        top10_holder_percent=10.0,
        lp_locked_percent=95.0,
        buy_tax_percent=1.0,
        sell_tax_percent=1.0,
    )
    kwargs.update(overrides)
    return TokenSecurityRecord(**kwargs)


def _liquidity(**overrides) -> LiquidityPoolRecord:
    kwargs = dict(
        provider=PROVIDER_GMGN,
        contract_version=CONTRACT_VERSION,
        chain=CHAIN,
        contract_address=ADDRESS,
        observed_at=UTC_NOW,
        source_endpoint="/v1/token/pool_info",
        source_timestamp=None,
        raw_response_sha256=VALID_SHA256,
        pool_address=None,
        dex=None,
        quote_symbol=None,
        liquidity_usd=50_000.0,
        base_reserve=None,
        quote_reserve=None,
    )
    kwargs.update(overrides)
    return LiquidityPoolRecord(**kwargs)


def _structure(**overrides) -> HolderTraderStructureRecord:
    kwargs = dict(
        provider=PROVIDER_GMGN,
        contract_version=CONTRACT_VERSION,
        chain=CHAIN,
        contract_address=ADDRESS,
        observed_at=UTC_NOW,
        source_endpoint="/v1/market/token_top_holders",
        source_timestamp=None,
        raw_response_sha256=VALID_SHA256,
        holder_count=1000,
        top10_holder_percent=10.0,
        top_trader_count=50,
        top_trader_percent=5.0,
        insider_percent=1.0,
    )
    kwargs.update(overrides)
    return HolderTraderStructureRecord(**kwargs)


def _discovery(**overrides) -> TokenDiscoveryRecord:
    kwargs = dict(
        provider=PROVIDER_GMGN,
        contract_version=CONTRACT_VERSION,
        chain=CHAIN,
        contract_address=ADDRESS,
        observed_at=UTC_NOW,
        source_endpoint="/v1/market/rank",
        source_timestamp=None,
        raw_response_sha256=VALID_SHA256,
        symbol="PEPE",
        name="Pepe",
        rank_score=99.0,
        rank_label="MOONSHOT",
    )
    kwargs.update(overrides)
    return TokenDiscoveryRecord(**kwargs)


def _smart_money(**overrides) -> SmartMoneyFlowRecord:
    kwargs = dict(
        provider=PROVIDER_GMGN,
        contract_version=CONTRACT_VERSION,
        chain=CHAIN,
        contract_address=ADDRESS,
        observed_at=UTC_NOW,
        source_endpoint="/v1/user/smartmoney",
        source_timestamp=None,
        raw_response_sha256=VALID_SHA256,
        smart_money_wallet_count=25,
        net_inflow_usd=100_000.0,
        flow_direction="inflow",
        signal_label="GUARANTEED_100X",
    )
    kwargs.update(overrides)
    return SmartMoneyFlowRecord(**kwargs)


def _candle(*, open_time, open_, high, low, close, volume, observed_at=UTC_NOW, chain=CHAIN, address=ADDRESS) -> CandleRecord:
    return CandleRecord(
        provider=PROVIDER_GMGN,
        contract_version=CONTRACT_VERSION,
        chain=chain,
        contract_address=address,
        observed_at=observed_at,
        source_endpoint="/v1/market/token_kline",
        source_timestamp=None,
        raw_response_sha256=VALID_SHA256,
        interval="1m",
        candle_open_time=open_time,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def _good_candles() -> list[CandleRecord]:
    return [
        _candle(open_time=1000, open_=1.0, high=1.2, low=0.9, close=1.0, volume=100.0),
        _candle(open_time=1060, open_=1.0, high=1.3, low=0.95, close=1.1, volume=120.0),
        _candle(open_time=1120, open_=1.1, high=1.6, low=1.05, close=1.5, volume=300.0),
    ]


def _evaluate(**overrides):
    kwargs = dict(
        chain=CHAIN,
        contract_address=ADDRESS,
        security=_security(),
        liquidity=_liquidity(),
        structure=_structure(),
        candles=_good_candles(),
        discovery=None,
        smart_money=None,
        now=UTC_NOW,
    )
    kwargs.update(overrides)
    return evaluate_research_candidate(**kwargs)


class TestEligibleBaseline:
    def test_full_valid_evidence_is_eligible(self):
        result = _evaluate()
        assert result.status == ELIGIBLE_FOR_REVIEW
        assert result.reasons == ()

    def test_eligible_with_supporting_discovery_and_smart_money(self):
        result = _evaluate(discovery=_discovery(), smart_money=_smart_money())
        assert result.status == ELIGIBLE_FOR_REVIEW
        assert any(label.startswith("discovery:") for label in result.supporting_labels)
        assert any(label.startswith("smart_money:") for label in result.supporting_labels)

    def test_same_inputs_produce_identical_result(self):
        assert _evaluate() == _evaluate()


class TestNeutralCandleConfirmation:
    def test_fewer_than_three_candles_watch(self):
        result = _evaluate(candles=_good_candles()[:2])
        assert result.status == WATCH

    def test_no_price_participation_watch(self):
        candles = [
            _candle(open_time=1000, open_=1.0, high=1.2, low=0.9, close=1.0, volume=100.0),
            _candle(open_time=1060, open_=1.0, high=1.1, low=0.9, close=0.95, volume=120.0),
            _candle(open_time=1120, open_=0.95, high=1.0, low=0.8, close=0.9, volume=300.0),
        ]
        assert _evaluate(candles=candles).status == WATCH

    def test_no_volume_participation_watch(self):
        candles = [
            _candle(open_time=1000, open_=1.0, high=1.2, low=0.9, close=1.0, volume=500.0),
            _candle(open_time=1060, open_=1.0, high=1.3, low=0.95, close=1.1, volume=500.0),
            _candle(open_time=1120, open_=1.1, high=1.6, low=1.05, close=1.5, volume=10.0),
        ]
        assert _evaluate(candles=candles).status == WATCH

    def test_zero_volume_candle_is_watch_not_rejected(self):
        candles = [
            _candle(open_time=1000, open_=1.0, high=1.2, low=0.9, close=1.0, volume=0.0),
            _candle(open_time=1060, open_=1.0, high=1.3, low=0.95, close=1.1, volume=120.0),
            _candle(open_time=1120, open_=1.1, high=1.6, low=1.05, close=1.5, volume=300.0),
        ]
        assert _evaluate(candles=candles).status == WATCH

    def test_exact_boundary_latest_volume_equal_to_mean_confirms(self):
        candles = [
            _candle(open_time=1000, open_=1.0, high=1.2, low=0.9, close=1.0, volume=100.0),
            _candle(open_time=1060, open_=1.0, high=1.3, low=0.95, close=1.1, volume=100.0),
            _candle(open_time=1120, open_=1.1, high=1.6, low=1.05, close=1.5, volume=100.0),
        ]
        assert _evaluate(candles=candles).status == ELIGIBLE_FOR_REVIEW

    def test_close_equal_to_first_close_does_not_confirm(self):
        candles = [
            _candle(open_time=1000, open_=1.0, high=1.2, low=0.9, close=1.0, volume=100.0),
            _candle(open_time=1060, open_=1.0, high=1.3, low=0.95, close=1.1, volume=120.0),
            _candle(open_time=1120, open_=1.1, high=1.6, low=0.95, close=1.0, volume=300.0),
        ]
        assert _evaluate(candles=candles).status == WATCH


class TestManipulatedLabelsNeverGate:
    def test_extreme_discovery_label_alone_does_not_make_incomplete_evidence_eligible(self):
        result = evaluate_research_candidate(
            chain=CHAIN,
            contract_address=ADDRESS,
            discovery=_discovery(rank_score=1_000_000.0, rank_label="ONE HUNDRED PERCENT GUARANTEED"),
            now=UTC_NOW,
        )
        assert result.status != ELIGIBLE_FOR_REVIEW

    def test_manipulated_labels_do_not_flip_a_rejected_result(self):
        bad_security = _security(is_honeypot=True)
        baseline = _evaluate(security=bad_security)
        manipulated = _evaluate(
            security=bad_security,
            discovery=_discovery(rank_score=999.0, rank_label="ELIGIBLE_FOR_REVIEW"),
            smart_money=_smart_money(signal_label="ELIGIBLE_FOR_REVIEW", net_inflow_usd=999_999.0),
        )
        assert baseline.status == REJECTED
        assert manipulated.status == REJECTED

    def test_manipulated_labels_do_not_flip_a_watch_result(self):
        baseline = _evaluate(candles=())
        manipulated = _evaluate(
            candles=(),
            discovery=_discovery(rank_label="ELIGIBLE_FOR_REVIEW", rank_score=1e9),
            smart_money=_smart_money(signal_label="ELIGIBLE_FOR_REVIEW"),
        )
        assert baseline.status == WATCH
        assert manipulated.status == WATCH

    def test_manipulated_labels_do_not_flip_an_eligible_result(self):
        baseline = _evaluate()
        manipulated = _evaluate(
            discovery=_discovery(rank_label="REJECTED", rank_score=-999.0),
            smart_money=_smart_money(signal_label="REJECTED", flow_direction="outflow"),
        )
        assert baseline.status == ELIGIBLE_FOR_REVIEW
        assert manipulated.status == ELIGIBLE_FOR_REVIEW


class TestRejectedStates:
    def test_honeypot_rejected(self):
        assert _evaluate(security=_security(is_honeypot=True)).status == REJECTED

    def test_mintable_rejected(self):
        assert _evaluate(security=_security(is_mintable=True)).status == REJECTED

    def test_contradictory_candle_ohlc_rejected(self):
        bad_candle = _candle(open_time=1000, open_=1.0, high=0.5, low=0.9, close=1.0, volume=100.0)
        candles = [bad_candle, *_good_candles()[1:]]
        assert _evaluate(candles=candles).status == REJECTED

    def test_non_increasing_open_time_rejected(self):
        candles = _good_candles()
        contradictory = [candles[0], candles[0], candles[2]]
        assert _evaluate(candles=contradictory).status == REJECTED

    def test_negative_volume_rejected(self):
        candles = _good_candles()
        candles[-1] = _candle(open_time=1200, open_=1.1, high=1.6, low=1.05, close=1.5, volume=-1.0)
        assert _evaluate(candles=candles).status == REJECTED

    def test_identity_mismatch_security_rejected(self):
        mismatched = _security(contract_address="SomeOtherAddress")
        assert _evaluate(security=mismatched).status == REJECTED

    def test_identity_mismatch_candle_rejected(self):
        candles = _good_candles()
        candles[0] = _candle(open_time=900, open_=1.0, high=1.2, low=0.9, close=1.0, volume=100.0, address="WrongAddress")
        assert _evaluate(candles=candles).status == REJECTED

    def test_invalid_chain_rejected(self):
        assert _evaluate(chain="dogechain").status == REJECTED

    def test_naive_now_rejected(self):
        assert _evaluate(now=datetime(2026, 1, 1)).status == REJECTED


class TestWatchStates:
    def test_missing_liquidity_evidence_watch(self):
        result = _evaluate(liquidity=None)
        assert result.status == WATCH
        assert any("liquidity" in reason for reason in result.reasons)

    def test_stale_security_evidence_watch(self):
        stale = _security(observed_at=UTC_NOW - timedelta(hours=1))
        assert _evaluate(security=stale).status == WATCH

    def test_future_dated_evidence_watch(self):
        future = _security(observed_at=UTC_NOW + timedelta(minutes=5))
        assert _evaluate(security=future).status == WATCH

    def test_partial_evidence_never_defaults_to_eligible(self):
        result = evaluate_research_candidate(chain=CHAIN, contract_address=ADDRESS, now=UTC_NOW)
        assert result.status == WATCH

    def test_unknown_risk_field_watches_rather_than_passes(self):
        unknown_lp = _security(lp_locked_percent=None)
        assert _evaluate(security=unknown_lp).status == WATCH


class TestIdentitySeparation:
    def test_ticker_never_participates_in_identity(self):
        matching = _evaluate(discovery=_discovery(symbol="PEPE", contract_address=ADDRESS))
        mismatched = _evaluate(discovery=_discovery(symbol="PEPE", contract_address="DifferentAddress"))
        assert matching.status == ELIGIBLE_FOR_REVIEW
        assert mismatched.status == REJECTED

    def test_same_contract_address_different_chain_are_independent_evaluations(self):
        address = "0xSameLookingAddress"
        solana_result = evaluate_research_candidate(
            chain="solana",
            contract_address=address,
            security=_security(chain="solana", contract_address=address),
            liquidity=_liquidity(chain="solana", contract_address=address),
            structure=_structure(chain="solana", contract_address=address),
            candles=[
                _candle(open_time=1000, open_=1.0, high=1.2, low=0.9, close=1.0, volume=100.0, address=address),
                _candle(open_time=1060, open_=1.0, high=1.3, low=0.95, close=1.1, volume=120.0, address=address),
                _candle(open_time=1120, open_=1.1, high=1.6, low=1.05, close=1.5, volume=300.0, address=address),
            ],
            now=UTC_NOW,
        )
        ethereum_result = evaluate_research_candidate(
            chain="ethereum",
            contract_address=address,
            security=_security(chain="solana", contract_address=address),
            now=UTC_NOW,
        )
        assert solana_result.status == ELIGIBLE_FOR_REVIEW
        assert ethereum_result.status == REJECTED
        assert (solana_result.chain, solana_result.contract_address) != (
            ethereum_result.chain,
            ethereum_result.contract_address,
        )
