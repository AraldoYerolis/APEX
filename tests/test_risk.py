"""Tests for risk calculations and daily loss tracking."""
import pytest

from apex.strategy.risk import calculate_risk_plan


def test_long_risk_plan():
    plan = calculate_risk_plan(
        direction="LONG",
        entry_price=100.0,
        stop_price=98.0,
        account_size_usd=100.0,
        risk_per_trade_pct=1.0,
        max_stop_distance_pct=4.0,
    )
    assert plan is not None
    assert plan.direction == "LONG"
    assert plan.target_1r == pytest.approx(102.0)
    assert plan.target_2r == pytest.approx(104.0)
    assert plan.risk_usd == pytest.approx(1.0)
    assert plan.stop_distance_pct == pytest.approx(2.0)
    assert plan.suggested_notional_usd == pytest.approx(50.0)  # $1 / 2% = $50


def test_short_risk_plan():
    plan = calculate_risk_plan(
        direction="SHORT",
        entry_price=100.0,
        stop_price=102.0,
        account_size_usd=100.0,
        risk_per_trade_pct=1.0,
        max_stop_distance_pct=4.0,
    )
    assert plan is not None
    assert plan.direction == "SHORT"
    assert plan.target_1r == pytest.approx(98.0)
    assert plan.target_2r == pytest.approx(96.0)
    assert plan.risk_usd == pytest.approx(1.0)


def test_stop_too_wide_rejected():
    plan = calculate_risk_plan(
        direction="LONG",
        entry_price=100.0,
        stop_price=90.0,  # 10% stop — exceeds 4% max
        account_size_usd=100.0,
        risk_per_trade_pct=1.0,
        max_stop_distance_pct=4.0,
    )
    assert plan is None


def test_invalid_long_stop_above_entry():
    plan = calculate_risk_plan(
        direction="LONG",
        entry_price=100.0,
        stop_price=105.0,  # stop above entry for long — invalid
        account_size_usd=100.0,
        risk_per_trade_pct=1.0,
        max_stop_distance_pct=4.0,
    )
    assert plan is None


def test_invalid_short_stop_below_entry():
    plan = calculate_risk_plan(
        direction="SHORT",
        entry_price=100.0,
        stop_price=95.0,  # stop below entry for short — invalid
        account_size_usd=100.0,
        risk_per_trade_pct=1.0,
        max_stop_distance_pct=4.0,
    )
    assert plan is None


def test_risk_scales_with_account_size():
    plan = calculate_risk_plan(
        direction="LONG",
        entry_price=100.0,
        stop_price=99.0,
        account_size_usd=1000.0,
        risk_per_trade_pct=1.0,
        max_stop_distance_pct=4.0,
    )
    assert plan is not None
    assert plan.risk_usd == pytest.approx(10.0)
    assert plan.suggested_notional_usd == pytest.approx(1000.0)  # $10 / 1% = $1000
