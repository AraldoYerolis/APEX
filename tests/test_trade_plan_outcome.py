"""Tests for the prospective trade plan outcome state machine
(src/apex/opportunity/trade_plan_outcome.py).

Covers: no-look-ahead source-candle exclusion, fixed-now future/current
exclusion, later closed-candle entry, both directions, T1 milestone
continuation, direct T2, stop, expiration (both entered/unentered), MFE/MAE
tracking, deterministic full replay, both same-candle ambiguity cases with
their evidence, and gap/duplicate/unordered/off-grid/malformed-OHLC as
recoverable INSUFFICIENT_DATA with later recovery.
"""
from __future__ import annotations

import dataclasses

import pandas as pd
import pytest

from apex.opportunity.contract import PRIMARY_TIMEFRAME_DURATION_MS
from apex.opportunity.trade_plan import OUTCOME_HORIZON_BARS, TRADE_PLAN_CONTRACT_VERSION, TradePlan
from apex.opportunity.trade_plan_outcome import (
    REASON_ENTRY_AND_EXIT_SAME_CANDLE,
    REASON_HORIZON_REACHED,
    REASON_STOP_AND_TARGET_SAME_CANDLE,
    TERMINAL_OUTCOME_STATES,
    TradePlanOutcomeEvaluation,
    build_initial_trade_plan_outcome,
    evaluate_trade_plan_outcome,
)

TIMEFRAME = "5m"
DURATION = PRIMARY_TIMEFRAME_DURATION_MS[TIMEFRAME]
NOT_BEFORE = 1_700_000_000_000


def _plan(
    *,
    direction: str = "LONG",
    entry: float = 110.0,
    stop: float = 100.0,
    not_before: int = NOT_BEFORE,
    timeframe: str = TIMEFRAME,
    availability: str = "AVAILABLE",
) -> TradePlan:
    duration = PRIMARY_TIMEFRAME_DURATION_MS[timeframe]
    if availability == "UNAVAILABLE":
        return TradePlan(
            plan_uid="plan-1", opportunity_uid="opp-1", symbol="BTC", direction=direction,
            setup_family="VOLATILITY_COMPRESSION", primary_timeframe=timeframe,
            detector_version="vol_compression_v0_1_1", opportunity_contract_version="opportunity_v0_1",
            plan_contract_version=TRADE_PLAN_CONTRACT_VERSION,
            source_candle_open_time=not_before - duration, source_candle_close_time=not_before,
            created_at="2026-09-15T00:00:00Z", availability="UNAVAILABLE",
            unavailable_reason="NO_STRUCTURAL_INVALIDATION", entry_type=None, entry_price=None,
            invalidation_price=None, stop_price=None, risk_distance=None, target_1r_price=None,
            target_2r_price=None, target_1r_multiple=None, target_2r_multiple=None,
            reward_risk_1r=None, reward_risk_2r=None,
            evaluation_not_before_ms=not_before, evaluation_expiry_ms=not_before + OUTCOME_HORIZON_BARS * duration,
            provenance_json="{}", warnings_json="[]",
        )
    risk = abs(entry - stop)
    sign = 1.0 if direction == "LONG" else -1.0
    t1 = entry + sign * risk
    t2 = entry + sign * 2.0 * risk
    return TradePlan(
        plan_uid="plan-1",
        opportunity_uid="opp-1",
        symbol="BTC",
        direction=direction,
        setup_family="SWEEP_RECLAIM",
        primary_timeframe=timeframe,
        detector_version="sweep_reclaim_v0_1",
        opportunity_contract_version="opportunity_v0_1",
        plan_contract_version=TRADE_PLAN_CONTRACT_VERSION,
        source_candle_open_time=not_before - duration,
        source_candle_close_time=not_before,
        created_at="2026-09-15T00:00:00Z",
        availability="AVAILABLE",
        unavailable_reason=None,
        entry_type="RESTING_LIMIT_AT_SOURCE_CLOSE",
        entry_price=entry,
        invalidation_price=stop,
        stop_price=stop,
        risk_distance=risk,
        target_1r_price=t1,
        target_2r_price=t2,
        target_1r_multiple=1.0,
        target_2r_multiple=2.0,
        reward_risk_1r=1.0,
        reward_risk_2r=2.0,
        evaluation_not_before_ms=not_before,
        evaluation_expiry_ms=not_before + OUTCOME_HORIZON_BARS * duration,
        provenance_json="{}",
        warnings_json="[]",
    )


def _row(open_time: int, o: float, h: float, lo: float, c: float) -> dict:
    return {"open_time": open_time, "open": o, "high": h, "low": lo, "close": c}


def _df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def _ot(i: int, not_before: int = NOT_BEFORE) -> int:
    return not_before + i * DURATION


# ------------------------------------------------------------------ initial / not-evaluable


def test_build_initial_outcome_unavailable_plan_is_not_evaluable():
    plan = _plan(availability="UNAVAILABLE")
    outcome = build_initial_trade_plan_outcome(plan)
    assert outcome.state == "NOT_EVALUABLE"
    assert outcome.is_terminal is True
    assert "NOT_EVALUABLE" in TERMINAL_OUTCOME_STATES


def test_build_initial_outcome_available_plan_is_pending_entry():
    plan = _plan()
    outcome = build_initial_trade_plan_outcome(plan)
    assert outcome.state == "PENDING_ENTRY"
    assert outcome.is_terminal is False


def test_evaluate_unavailable_plan_always_not_evaluable():
    plan = _plan(availability="UNAVAILABLE")
    result = evaluate_trade_plan_outcome(plan, None, NOT_BEFORE + 100 * DURATION)
    assert result.state == "NOT_EVALUABLE"
    assert result.is_terminal is True


# ------------------------------------------------------------------ nothing due yet / no-look-ahead


def test_nothing_due_yet_is_pending_not_insufficient_data():
    plan = _plan()
    result = evaluate_trade_plan_outcome(plan, None, NOT_BEFORE)  # boundary not yet reached
    assert result.state == "PENDING_ENTRY"
    assert result.data_quality == "COMPLETE"
    assert result.first_missing_boundary_ms is None


def test_source_candle_itself_cannot_fill_plan():
    """A row at the SOURCE candle's own open_time (evaluation_not_before -
    duration) must never be considered — only evaluation_not_before onward.
    """
    plan = _plan(entry=110.0, stop=100.0)
    source_row = _row(NOT_BEFORE - DURATION, 110.0, 111.0, 105.0, 110.0)  # would "reach" entry
    df = _df([source_row])
    result = evaluate_trade_plan_outcome(plan, df, NOT_BEFORE + DURATION)
    # The one expected candle (at NOT_BEFORE) is missing from df entirely.
    assert result.state == "INSUFFICIENT_DATA"
    assert result.first_missing_boundary_ms == NOT_BEFORE


def test_fixed_now_excludes_future_candles():
    plan = _plan(entry=110.0, stop=100.0)
    rows = [
        _row(_ot(0), 111.0, 112.0, 109.0, 110.0),  # reaches entry cleanly
        _row(_ot(1), 100.0, 100.5, 99.0, 100.0),   # would stop — must be excluded by now_ms
    ]
    df = _df(rows)
    now_ms = _ot(1)  # boundary of candle 1 not yet reached (its close is at _ot(2))
    result = evaluate_trade_plan_outcome(plan, df, now_ms)
    assert result.state == "ENTERED"
    assert result.entry_open_time == _ot(0)
    assert result.last_evaluated_open_time == _ot(0)


# ------------------------------------------------------------------ entry / both directions


@pytest.mark.parametrize("direction,entry,stop", [("LONG", 110.0, 100.0), ("SHORT", 90.0, 100.0)])
def test_later_closed_candle_reaches_entry(direction, entry, stop):
    plan = _plan(direction=direction, entry=entry, stop=stop)
    if direction == "LONG":
        # A LONG resting buy-limit only fills when price dips down to it —
        # this candle must stay entirely ABOVE entry to not reach it.
        no_fill = _row(_ot(0), entry + 3.0, entry + 4.0, entry + 2.0, entry + 3.0)
    else:
        # A SHORT resting sell-limit only fills when price rallies up to it —
        # this candle must stay entirely BELOW entry to not reach it.
        no_fill = _row(_ot(0), entry - 3.0, entry - 2.0, entry - 4.0, entry - 3.0)
    rows = [
        no_fill,
        _row(_ot(1), entry, entry + 1.0, entry - 1.0, entry),  # reaches entry cleanly
    ]
    df = _df(rows)
    result = evaluate_trade_plan_outcome(plan, df, _ot(2))
    assert result.state == "ENTERED"
    assert result.entry_open_time == _ot(1)


# ------------------------------------------------------------------ T1 milestone / direct T2 / stop


@pytest.mark.parametrize("direction,entry,stop,t1", [("LONG", 110.0, 100.0, 120.0), ("SHORT", 90.0, 100.0, 80.0)])
def test_hit_1r_is_nonterminal_milestone(direction, entry, stop, t1):
    plan = _plan(direction=direction, entry=entry, stop=stop)
    rows = [
        _row(_ot(0), entry, entry + 0.5, entry - 0.5, entry),  # entry, clean
        _row(_ot(1), entry, t1, entry - 0.5, entry) if direction == "LONG"
        else _row(_ot(1), entry, entry + 0.5, t1, entry),  # reaches T1 only
    ]
    df = _df(rows)
    result = evaluate_trade_plan_outcome(plan, df, _ot(2))
    assert result.state == "HIT_1R"
    assert result.is_terminal is False
    assert result.hit_1r_open_time == _ot(1)


def test_direct_t2_hit_records_implicit_t1():
    plan = _plan(entry=110.0, stop=100.0)  # t1=120, t2=130
    rows = [
        _row(_ot(0), 110.0, 110.5, 109.5, 110.0),
        _row(_ot(1), 110.0, 131.0, 109.0, 130.0),  # jumps straight through T1 and T2
    ]
    df = _df(rows)
    result = evaluate_trade_plan_outcome(plan, df, _ot(2))
    assert result.state == "HIT_2R"
    assert result.is_terminal is True
    assert result.hit_1r_open_time == _ot(1)
    assert result.terminal_open_time == _ot(1)


def test_stop_preserves_t1_reached_flag():
    plan = _plan(entry=110.0, stop=100.0)  # t1=120, t2=130
    rows = [
        _row(_ot(0), 110.0, 110.5, 109.5, 110.0),  # entry
        _row(_ot(1), 110.0, 120.5, 109.0, 120.0),  # HIT_1R
        _row(_ot(2), 105.0, 106.0, 99.0, 100.0),   # then STOPPED
    ]
    df = _df(rows)
    result = evaluate_trade_plan_outcome(plan, df, _ot(3))
    assert result.state == "STOPPED"
    assert result.hit_1r_open_time == _ot(1)
    assert result.terminal_open_time == _ot(2)


def test_stop_without_ever_reaching_t1():
    plan = _plan(entry=110.0, stop=100.0)
    rows = [
        _row(_ot(0), 110.0, 110.5, 109.5, 110.0),
        _row(_ot(1), 105.0, 106.0, 99.0, 100.0),
    ]
    df = _df(rows)
    result = evaluate_trade_plan_outcome(plan, df, _ot(2))
    assert result.state == "STOPPED"
    assert result.hit_1r_open_time is None


# ------------------------------------------------------------------ expiration


def test_expired_unentered_after_horizon_with_complete_data():
    # entry=110 is BELOW this price band (112-115): low never reaches it, so
    # the LONG entry trigger (low <= entry) never fires.
    plan = _plan(entry=110.0, stop=100.0)
    rows = [_row(_ot(i), 113.0, 115.0, 112.0, 113.0) for i in range(OUTCOME_HORIZON_BARS)]
    df = _df(rows)
    end_ms = NOT_BEFORE + OUTCOME_HORIZON_BARS * DURATION
    result = evaluate_trade_plan_outcome(plan, df, end_ms)
    assert result.state == "EXPIRED_UNENTERED"
    assert result.is_terminal is True
    assert result.terminal_reason == REASON_HORIZON_REACHED


def test_expired_open_after_horizon_with_complete_data():
    plan = _plan(entry=110.0, stop=100.0)
    rows = [_row(_ot(0), 110.0, 110.5, 109.5, 110.0)]  # entry, clean
    rows += [_row(_ot(i), 111.0, 112.0, 110.0, 111.0) for i in range(1, OUTCOME_HORIZON_BARS)]
    df = _df(rows)
    end_ms = NOT_BEFORE + OUTCOME_HORIZON_BARS * DURATION
    result = evaluate_trade_plan_outcome(plan, df, end_ms)
    assert result.state == "EXPIRED_OPEN"
    assert result.is_terminal is True
    assert result.entry_open_time == _ot(0)


def test_final_candle_missing_at_expiry_boundary_is_insufficient_data_not_premature_expiry():
    """The very last expected candle before the expiry boundary being absent
    must never be short-circuited into EXPIRED_UNENTERED/EXPIRED_OPEN — it is
    exactly the same "still-unresolved boundary" case as any other missing
    candle, and stays recoverable until that one candle actually arrives.
    """
    # entry=110 is BELOW this price band (112-115): low never reaches it, so
    # the LONG entry trigger (low <= entry) never fires for any candle here —
    # mirrors test_expired_unentered_after_horizon_with_complete_data, minus
    # its final expected candle.
    plan = _plan(entry=110.0, stop=100.0)
    rows = [_row(_ot(i), 113.0, 115.0, 112.0, 113.0) for i in range(OUTCOME_HORIZON_BARS - 1)]
    df = _df(rows)
    end_ms = NOT_BEFORE + OUTCOME_HORIZON_BARS * DURATION  # exactly the expiry boundary
    result = evaluate_trade_plan_outcome(plan, df, end_ms)
    assert result.state == "INSUFFICIENT_DATA"
    assert result.state not in ("EXPIRED_UNENTERED", "EXPIRED_OPEN")
    assert result.is_terminal is False
    assert result.first_missing_boundary_ms == _ot(OUTCOME_HORIZON_BARS - 1)

    # Repair: the missing final candle arrives. Same now_ms, same plan —
    # deterministic evaluation now proceeds all the way to a correct expiry.
    repaired_rows = rows + [_row(_ot(OUTCOME_HORIZON_BARS - 1), 113.0, 115.0, 112.0, 113.0)]
    repaired_df = _df(repaired_rows)
    repaired = evaluate_trade_plan_outcome(plan, repaired_df, end_ms)
    assert repaired.state == "EXPIRED_UNENTERED"
    assert repaired.is_terminal is True
    assert repaired.terminal_reason == REASON_HORIZON_REACHED


# ------------------------------------------------------------------ MFE / MAE


def test_mfe_mae_track_running_max_nonnegative():
    plan = _plan(entry=110.0, stop=100.0)  # risk = 10
    rows = [
        _row(_ot(0), 110.0, 110.5, 109.5, 110.0),  # entry
        _row(_ot(1), 110.0, 113.0, 108.0, 112.0),  # +0.3R favorable, +0.2R adverse
        _row(_ot(2), 112.0, 112.5, 111.0, 112.0),  # smaller move — must not regress running max
    ]
    df = _df(rows)
    result = evaluate_trade_plan_outcome(plan, df, _ot(3))
    assert result.state == "ENTERED"
    assert result.mfe_r == pytest.approx(0.3)
    assert result.mae_r == pytest.approx(0.2)
    assert result.mfe_r >= 0
    assert result.mae_r >= 0


# ------------------------------------------------------------------ determinism


def test_deterministic_replay_identical_inputs_identical_output():
    plan = _plan(entry=110.0, stop=100.0)
    rows = [
        _row(_ot(0), 110.0, 110.5, 109.5, 110.0),
        _row(_ot(1), 110.0, 120.5, 109.0, 120.0),
    ]
    df = _df(rows)
    result_a = evaluate_trade_plan_outcome(plan, df, _ot(2))
    result_b = evaluate_trade_plan_outcome(plan, df.copy(), _ot(2))
    assert result_a == result_b
    assert result_a.evidence_json == result_b.evidence_json


# ------------------------------------------------------------------ ambiguity


def test_entry_and_exit_same_candle_is_ambiguous():
    plan = _plan(entry=110.0, stop=100.0)  # t1=120
    rows = [_row(_ot(0), 110.0, 121.0, 109.0, 115.0)]  # reaches entry AND T1 same candle
    df = _df(rows)
    result = evaluate_trade_plan_outcome(plan, df, _ot(1))
    assert result.state == "AMBIGUOUS"
    assert result.is_terminal is True
    assert result.is_ambiguous is True
    assert result.terminal_reason == REASON_ENTRY_AND_EXIT_SAME_CANDLE
    assert "TARGET_1R" in result.crossed_levels
    assert result.decisive_ohlc is not None


def test_stop_and_target_same_candle_is_ambiguous():
    plan = _plan(entry=110.0, stop=100.0)  # t1=120, t2=130
    rows = [
        _row(_ot(0), 110.0, 110.5, 109.5, 110.0),  # clean entry
        _row(_ot(1), 110.0, 131.0, 99.0, 110.0),   # reaches stop AND T1/T2 same candle
    ]
    df = _df(rows)
    result = evaluate_trade_plan_outcome(plan, df, _ot(2))
    assert result.state == "AMBIGUOUS"
    assert result.terminal_reason == REASON_STOP_AND_TARGET_SAME_CANDLE
    assert set(result.crossed_levels) == {"STOP", "TARGET_1R", "TARGET_2R"}
    assert result.decisive_ohlc is not None


# ------------------------------------------------------------------ data-quality: gap / duplicate /
# unordered / off-grid / malformed OHLC


def test_gap_yields_insufficient_data_then_recovers():
    plan = _plan(entry=110.0, stop=100.0)
    rows = [
        _row(_ot(0), 110.0, 110.5, 109.5, 110.0),  # entry
        # _ot(1) missing entirely
        _row(_ot(2), 110.0, 111.0, 109.0, 110.0),
    ]
    df = _df(rows)
    result = evaluate_trade_plan_outcome(plan, df, _ot(3))
    assert result.state == "INSUFFICIENT_DATA"
    assert result.first_missing_boundary_ms == _ot(1)
    assert result.entry_open_time == _ot(0)  # already-known milestone preserved in this pass's evidence

    # Recovery: the missing candle now arrives.
    recovered_rows = rows[:1] + [_row(_ot(1), 110.0, 110.4, 109.6, 110.0)] + rows[1:]
    recovered_df = _df(recovered_rows)
    recovered = evaluate_trade_plan_outcome(plan, recovered_df, _ot(3))
    assert recovered.state != "INSUFFICIENT_DATA"
    assert recovered.entry_open_time == _ot(0)


def test_duplicate_open_time_yields_insufficient_data():
    plan = _plan(entry=110.0, stop=100.0)
    rows = [
        _row(_ot(0), 110.0, 110.5, 109.5, 110.0),
        _row(_ot(0), 110.0, 110.5, 109.5, 110.0),  # duplicate open_time
    ]
    df = _df(rows)
    result = evaluate_trade_plan_outcome(plan, df, _ot(1))
    assert result.state == "INSUFFICIENT_DATA"
    assert result.first_missing_boundary_ms == _ot(0)


def test_unordered_frame_yields_insufficient_data():
    plan = _plan(entry=110.0, stop=100.0)
    rows = [
        _row(_ot(1), 110.0, 110.5, 109.5, 110.0),
        _row(_ot(0), 110.0, 110.5, 109.5, 110.0),  # out of order
    ]
    df = _df(rows)
    result = evaluate_trade_plan_outcome(plan, df, _ot(2))
    assert result.state == "INSUFFICIENT_DATA"
    assert result.first_missing_boundary_ms == _ot(0)


def test_off_grid_candle_treated_as_missing():
    plan = _plan(entry=110.0, stop=100.0)
    rows = [_row(NOT_BEFORE + DURATION // 2, 110.0, 110.5, 109.5, 110.0)]  # off the expected grid
    df = _df(rows)
    result = evaluate_trade_plan_outcome(plan, df, _ot(1))
    assert result.state == "INSUFFICIENT_DATA"
    assert result.first_missing_boundary_ms == NOT_BEFORE


def test_malformed_ohlc_treated_as_missing():
    plan = _plan(entry=110.0, stop=100.0)
    rows = [_row(_ot(0), 110.0, 100.0, 200.0, 110.0)]  # low > high: malformed
    df = _df(rows)
    result = evaluate_trade_plan_outcome(plan, df, _ot(1))
    assert result.state == "INSUFFICIENT_DATA"
    assert result.first_missing_boundary_ms == _ot(0)


# ------------------------------------------------------------------ safety boundary


def test_no_score_or_sizing_fields_on_outcome_evaluation():
    field_names = {f.name for f in dataclasses.fields(TradePlanOutcomeEvaluation)}
    for name in field_names:
        lowered = name.lower()
        assert "score" not in lowered
        for forbidden in ("account", "risk_usd", "quantity", "notional", "margin", "leverage"):
            assert forbidden not in lowered
