"""Trade plans and outcome evidence v0.1 — prospective, closed-candle-only,
no-look-ahead outcome evidence for an immutable TradePlan (see
opportunity/trade_plan.py).

A TradePlan never mutates. A `TradePlanOutcomeEvaluation` is a separate,
mutable research summary: nonterminal states may be updated pass over pass;
terminal states are never touched again (enforced by
db/repository.py's update_trade_plan_outcome, not by this module, which is
purely pure/stateless).

Pure, full-replay design
--------------------------
`evaluate_trade_plan_outcome` is a pure function: given a `TradePlan`, a
candle DataFrame, and one `now_ms`, it always recomputes the outcome from
scratch by walking every *expected* closed candle from
`evaluation_not_before_ms` through `min(now_ms, evaluation_expiry_ms)` — it
never reads or depends on any previously persisted outcome row. Identical
inputs therefore always yield byte-identical output; there is no incremental
state to drift or replay error to accumulate. It never reads the wall clock
itself — `now_ms` is always supplied by the caller (see
scheduler/tasks.py's run_trade_plan_outcome_evaluation, which captures one
whole-second-floored now_ms for its entire pass).

No-look-ahead / closed-candle-only
-------------------------------------
Only candles at or after `evaluation_not_before_ms` (== the plan's own
source_candle_close_time — see trade_plan.py) are ever considered, and only
through `min(now_ms, evaluation_expiry_ms)` — a still-forming or future
candle is never evaluated. If `now_ms` has not yet reached the first
evaluable candle's own close boundary, nothing is due yet: this is NOT
treated as missing data — the plan's initial logical state (PENDING_ENTRY)
is simply retained.

Ambiguity-preserving
----------------------
Two same-candle ambiguity cases are recognized and terminate the plan as
AMBIGUOUS rather than inventing an intrabar ordering the OHLC data cannot
support:

  ENTRY_AND_EXIT_SAME_CANDLE  — the same candle that first reaches the entry
                                 price also reaches the stop or either
                                 target.
  STOP_AND_TARGET_SAME_CANDLE — after entry was established on an earlier
                                 candle, a later candle reaches the stop and
                                 any not-yet-resolved target in the same
                                 candle.

Data-quality honesty
-----------------------
The bounded relevant candle sequence is independently validated: integral,
nonnegative, grid-aligned, exactly contiguous open_times beginning at
`evaluation_not_before_ms`, with finite positive OHLC satisfying
`low <= open/close <= high`. A gap, duplicate, malformed, or off-grid
candle at the first still-unresolved boundary yields the recoverable
`INSUFFICIENT_DATA` state (never evaluating past that boundary); a later
pass with the gap filled in can resume and proceed normally. Sorting,
deduplication, filling, or interpolation is never performed — a
duplicate/unordered/off-grid input is treated as data-quality evidence, not
silently repaired.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Literal, Optional

import numpy as np
import pandas as pd

from apex.opportunity.contract import PRIMARY_TIMEFRAME_DURATION_MS
from apex.opportunity.trade_plan import TradePlan

TRADE_PLAN_OUTCOME_CONTRACT_VERSION = "trade_plan_outcome_v0_1"

OutcomeState = Literal[
    "NOT_EVALUABLE",
    "PENDING_ENTRY",
    "ENTERED",
    "HIT_1R",
    "HIT_2R",
    "STOPPED",
    "EXPIRED_UNENTERED",
    "EXPIRED_OPEN",
    "AMBIGUOUS",
    "INSUFFICIENT_DATA",
]

# Terminal states are never updated again once persisted (see
# db/repository.py's update_trade_plan_outcome guard). Every other state,
# including the recoverable INSUFFICIENT_DATA, remains eligible for a later
# evaluation pass.
TERMINAL_OUTCOME_STATES: frozenset[str] = frozenset(
    {
        "NOT_EVALUABLE",
        "HIT_2R",
        "STOPPED",
        "EXPIRED_UNENTERED",
        "EXPIRED_OPEN",
        "AMBIGUOUS",
    }
)

LEVEL_STOP = "STOP"
LEVEL_TARGET_1R = "TARGET_1R"
LEVEL_TARGET_2R = "TARGET_2R"

REASON_ENTRY_AND_EXIT_SAME_CANDLE = "ENTRY_AND_EXIT_SAME_CANDLE"
REASON_STOP_AND_TARGET_SAME_CANDLE = "STOP_AND_TARGET_SAME_CANDLE"
REASON_HORIZON_REACHED = "HORIZON_REACHED"

DATA_QUALITY_COMPLETE = "COMPLETE"
DATA_QUALITY_INSUFFICIENT = "INSUFFICIENT_DATA"

REQUIRED_CANDLE_COLUMNS = ("open_time", "open", "high", "low", "close")


@dataclass(frozen=True)
class TradePlanOutcomeEvaluation:
    """Immutable result of one evaluation pass (initial or replayed) for one
    plan. Persisted via db/repository.py's insert_trade_plan_with_outcome
    (initial row) / update_trade_plan_outcome (subsequent passes, guarded).
    """

    plan_uid: str
    opportunity_uid: str
    contract_version: str
    state: OutcomeState
    is_terminal: bool
    last_evaluated_ms: Optional[int]
    last_evaluated_open_time: Optional[int]
    entry_open_time: Optional[int]
    hit_1r_open_time: Optional[int]
    terminal_open_time: Optional[int]
    terminal_reason: Optional[str]
    mfe_r: Optional[float]
    mae_r: Optional[float]
    data_quality: str
    first_missing_boundary_ms: Optional[int]
    is_ambiguous: bool
    decisive_ohlc: Optional[dict]
    crossed_levels: tuple[str, ...]
    evidence_json: str


def _row_ohlc(row: Any) -> Optional[tuple[float, float, float, float]]:
    try:
        o = row["open"]
        h = row["high"]
        low_v = row["low"]
        c = row["close"]
    except (KeyError, TypeError, IndexError):
        return None
    values = []
    for v in (o, h, low_v, c):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        fv = float(v)
        if not math.isfinite(fv) or fv <= 0:
            return None
        values.append(fv)
    o_f, h_f, l_f, c_f = values
    if not (l_f <= o_f <= h_f):
        return None
    if not (l_f <= c_f <= h_f):
        return None
    return o_f, h_f, l_f, c_f


def _validate_frame_order(df: pd.DataFrame) -> bool:
    """Whole-frame structural validation: every required column present,
    and `open_time` integral, nonnegative, and STRICTLY increasing across
    the entire supplied frame — matching CandleStore.get_df's own
    "oldest first, no duplicates" contract. This module never sorts,
    deduplicates, fills, or interpolates a frame that violates it (see
    module docstring); an unordered or duplicate-containing frame is
    treated as wholly untrustworthy rather than silently repaired.
    """
    for col in REQUIRED_CANDLE_COLUMNS:
        if col not in df.columns:
            return False
    try:
        open_times = df["open_time"].to_numpy(dtype=np.float64)
    except (TypeError, ValueError):
        return False
    if open_times.size == 0:
        return False
    if not np.all(np.isfinite(open_times)):
        return False
    if not np.all(open_times == np.floor(open_times)):
        return False
    if np.any(open_times < 0):
        return False
    if open_times.size > 1 and not np.all(np.diff(open_times) > 0):
        return False
    return True


def _build_candle_index(df: Optional[pd.DataFrame]) -> dict[int, Any]:
    """Bounded exact `open_time -> row` index built only from a structurally
    valid frame (see `_validate_frame_order`) — an empty dict (meaning
    "every expected boundary is missing") for None, a non-DataFrame, an
    empty frame, or a frame that fails the whole-frame order check.
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return {}
    if not _validate_frame_order(df):
        return {}
    return {int(row["open_time"]): row for _, row in df.iterrows()}


def _find_unique_candle(
    candle_index: dict[int, Any], open_time: int
) -> Optional[tuple[float, float, float, float]]:
    """Return this exact open_time's validated (open, high, low, close), or
    None if it is missing (including "the whole frame was untrustworthy",
    see `_build_candle_index`) or its own OHLC is malformed.
    """
    row = candle_index.get(open_time)
    if row is None:
        return None
    return _row_ohlc(row)


def _downside_trigger(direction: str, low: float, high: float, level: float) -> bool:
    """True when this candle's range reaches `level` from the entry/stop
    side (below entry for LONG, above entry for SHORT) — shared by the
    entry and stop checks, since both sit on the same side of price for a
    given direction.
    """
    return low <= level if direction == "LONG" else high >= level


def _upside_trigger(direction: str, low: float, high: float, level: float) -> bool:
    """True when this candle's range reaches `level` from the target side
    (above entry for LONG, below entry for SHORT).
    """
    return high >= level if direction == "LONG" else low <= level


def _excursion_r(
    direction: str, high: float, low: float, entry: float, risk_distance: float
) -> tuple[float, float]:
    if direction == "LONG":
        favorable = (high - entry) / risk_distance
        adverse = (entry - low) / risk_distance
    else:
        favorable = (entry - low) / risk_distance
        adverse = (high - entry) / risk_distance
    return favorable, adverse


def _evidence_json(
    plan: TradePlan,
    *,
    now_ms: Optional[int],
    last_evaluated_open_time: Optional[int],
    entry_open_time: Optional[int],
    hit_1r_open_time: Optional[int],
    terminal_open_time: Optional[int],
    terminal_reason: Optional[str],
    mfe_r: Optional[float],
    mae_r: Optional[float],
    data_quality: str,
    first_missing_boundary_ms: Optional[int],
    is_ambiguous: bool,
    decisive_ohlc: Optional[dict],
    crossed_levels: tuple[str, ...],
) -> str:
    payload = {
        "plan_uid": plan.plan_uid,
        "opportunity_uid": plan.opportunity_uid,
        "outcome_contract_version": TRADE_PLAN_OUTCOME_CONTRACT_VERSION,
        "evaluation_not_before_ms": plan.evaluation_not_before_ms,
        "evaluation_expiry_ms": plan.evaluation_expiry_ms,
        "last_evaluated_ms": now_ms,
        "last_evaluated_open_time": last_evaluated_open_time,
        "entry_open_time": entry_open_time,
        "hit_1r_open_time": hit_1r_open_time,
        "terminal_open_time": terminal_open_time,
        "terminal_reason": terminal_reason,
        "mfe_r": mfe_r,
        "mae_r": mae_r,
        "data_quality": data_quality,
        "first_missing_boundary_ms": first_missing_boundary_ms,
        "is_ambiguous": is_ambiguous,
        "decisive_ohlc": decisive_ohlc,
        "crossed_levels": list(crossed_levels),
        "plan_levels": {
            "entry_price": plan.entry_price,
            "stop_price": plan.stop_price,
            "target_1r_price": plan.target_1r_price,
            "target_2r_price": plan.target_2r_price,
        },
    }
    return json.dumps(payload, sort_keys=True, allow_nan=False)


def _result(
    plan: TradePlan,
    *,
    state: OutcomeState,
    now_ms: Optional[int],
    last_evaluated_open_time: Optional[int] = None,
    entry_open_time: Optional[int] = None,
    hit_1r_open_time: Optional[int] = None,
    terminal_open_time: Optional[int] = None,
    terminal_reason: Optional[str] = None,
    mfe_r: Optional[float] = None,
    mae_r: Optional[float] = None,
    data_quality: str = DATA_QUALITY_COMPLETE,
    first_missing_boundary_ms: Optional[int] = None,
    is_ambiguous: bool = False,
    decisive_ohlc: Optional[dict] = None,
    crossed_levels: tuple[str, ...] = (),
) -> TradePlanOutcomeEvaluation:
    evidence = _evidence_json(
        plan,
        now_ms=now_ms,
        last_evaluated_open_time=last_evaluated_open_time,
        entry_open_time=entry_open_time,
        hit_1r_open_time=hit_1r_open_time,
        terminal_open_time=terminal_open_time,
        terminal_reason=terminal_reason,
        mfe_r=mfe_r,
        mae_r=mae_r,
        data_quality=data_quality,
        first_missing_boundary_ms=first_missing_boundary_ms,
        is_ambiguous=is_ambiguous,
        decisive_ohlc=decisive_ohlc,
        crossed_levels=crossed_levels,
    )
    return TradePlanOutcomeEvaluation(
        plan_uid=plan.plan_uid,
        opportunity_uid=plan.opportunity_uid,
        contract_version=TRADE_PLAN_OUTCOME_CONTRACT_VERSION,
        state=state,
        is_terminal=state in TERMINAL_OUTCOME_STATES,
        last_evaluated_ms=now_ms,
        last_evaluated_open_time=last_evaluated_open_time,
        entry_open_time=entry_open_time,
        hit_1r_open_time=hit_1r_open_time,
        terminal_open_time=terminal_open_time,
        terminal_reason=terminal_reason,
        mfe_r=mfe_r,
        mae_r=mae_r,
        data_quality=data_quality,
        first_missing_boundary_ms=first_missing_boundary_ms,
        is_ambiguous=is_ambiguous,
        decisive_ohlc=decisive_ohlc,
        crossed_levels=crossed_levels,
        evidence_json=evidence,
    )


def build_initial_trade_plan_outcome(plan: TradePlan) -> TradePlanOutcomeEvaluation:
    """Deterministic initial outcome row for a brand-new plan, before any
    evaluation pass has run: `NOT_EVALUABLE` (terminal) for an UNAVAILABLE
    plan, `PENDING_ENTRY` (nonterminal) for an AVAILABLE one.
    """
    state: OutcomeState = "NOT_EVALUABLE" if plan.availability == "UNAVAILABLE" else "PENDING_ENTRY"
    return _result(plan, state=state, now_ms=None)


def evaluate_trade_plan_outcome(
    plan: TradePlan,
    df: Optional[pd.DataFrame],
    now_ms: int,
) -> TradePlanOutcomeEvaluation:
    """Pure, full-replay evaluation of `plan` against `df` as of `now_ms`.
    See module docstring for the complete contract.
    """
    if plan.availability != "AVAILABLE":
        return _result(plan, state="NOT_EVALUABLE", now_ms=now_ms)

    assert plan.evaluation_not_before_ms is not None
    assert plan.evaluation_expiry_ms is not None
    assert plan.entry_price is not None and plan.stop_price is not None
    assert plan.target_1r_price is not None and plan.target_2r_price is not None
    assert plan.risk_distance is not None

    duration = PRIMARY_TIMEFRAME_DURATION_MS[plan.primary_timeframe]
    not_before = plan.evaluation_not_before_ms
    expiry = plan.evaluation_expiry_ms
    end_ms = min(now_ms, expiry)

    if end_ms < not_before + duration:
        # Nothing due yet — never treated as missing data.
        return _result(plan, state="PENDING_ENTRY", now_ms=now_ms)

    expected_count = (end_ms - not_before) // duration
    direction = plan.direction
    entry_price = plan.entry_price
    stop_price = plan.stop_price
    t1_price = plan.target_1r_price
    t2_price = plan.target_2r_price
    risk_distance = plan.risk_distance

    entered = False
    entry_open_time: Optional[int] = None
    hit_1r_open_time: Optional[int] = None
    mfe = 0.0
    mae = 0.0
    have_excursion = False
    last_evaluated_open_time: Optional[int] = None

    candle_index = _build_candle_index(df)

    for i in range(expected_count):
        open_time = not_before + i * duration
        candle = _find_unique_candle(candle_index, open_time)
        if candle is None:
            return _result(
                plan,
                state="INSUFFICIENT_DATA",
                now_ms=now_ms,
                last_evaluated_open_time=last_evaluated_open_time,
                entry_open_time=entry_open_time,
                hit_1r_open_time=hit_1r_open_time,
                mfe_r=(mfe if have_excursion else None),
                mae_r=(mae if have_excursion else None),
                data_quality=DATA_QUALITY_INSUFFICIENT,
                first_missing_boundary_ms=open_time,
            )

        o, h, low_v, c = candle
        last_evaluated_open_time = open_time

        if not entered:
            if not _downside_trigger(direction, low_v, h, entry_price):
                continue
            entered = True
            entry_open_time = open_time
            stop_hit = _downside_trigger(direction, low_v, h, stop_price)
            t1_hit = _upside_trigger(direction, low_v, h, t1_price)
            t2_hit = _upside_trigger(direction, low_v, h, t2_price)
            if stop_hit or t1_hit or t2_hit:
                crossed = []
                if stop_hit:
                    crossed.append(LEVEL_STOP)
                if t1_hit:
                    crossed.append(LEVEL_TARGET_1R)
                if t2_hit:
                    crossed.append(LEVEL_TARGET_2R)
                return _result(
                    plan,
                    state="AMBIGUOUS",
                    now_ms=now_ms,
                    last_evaluated_open_time=last_evaluated_open_time,
                    entry_open_time=entry_open_time,
                    terminal_open_time=open_time,
                    terminal_reason=REASON_ENTRY_AND_EXIT_SAME_CANDLE,
                    is_ambiguous=True,
                    decisive_ohlc={"open": o, "high": h, "low": low_v, "close": c},
                    crossed_levels=tuple(crossed),
                )
            # Entered cleanly this candle — never used for MFE/MAE (see
            # module docstring).
            continue

        # Already entered (possibly already HIT_1R) — evaluate this candle.
        stop_hit = _downside_trigger(direction, low_v, h, stop_price)
        t1_hit = _upside_trigger(direction, low_v, h, t1_price)
        t2_hit = _upside_trigger(direction, low_v, h, t2_price)
        t1_already = hit_1r_open_time is not None
        unresolved_target_hit = t2_hit or (t1_hit and not t1_already)

        if stop_hit and unresolved_target_hit:
            crossed = [LEVEL_STOP]
            if t1_hit and not t1_already:
                crossed.append(LEVEL_TARGET_1R)
            if t2_hit:
                crossed.append(LEVEL_TARGET_2R)
            return _result(
                plan,
                state="AMBIGUOUS",
                now_ms=now_ms,
                last_evaluated_open_time=last_evaluated_open_time,
                entry_open_time=entry_open_time,
                hit_1r_open_time=hit_1r_open_time,
                terminal_open_time=open_time,
                terminal_reason=REASON_STOP_AND_TARGET_SAME_CANDLE,
                mfe_r=(mfe if have_excursion else None),
                mae_r=(mae if have_excursion else None),
                is_ambiguous=True,
                decisive_ohlc={"open": o, "high": h, "low": low_v, "close": c},
                crossed_levels=tuple(crossed),
            )

        cur_favorable, cur_adverse = _excursion_r(direction, h, low_v, entry_price, risk_distance)
        mfe = max(mfe, cur_favorable, 0.0)
        mae = max(mae, cur_adverse, 0.0)
        have_excursion = True

        if t2_hit:
            if not t1_already:
                hit_1r_open_time = open_time
            return _result(
                plan,
                state="HIT_2R",
                now_ms=now_ms,
                last_evaluated_open_time=last_evaluated_open_time,
                entry_open_time=entry_open_time,
                hit_1r_open_time=hit_1r_open_time,
                terminal_open_time=open_time,
                mfe_r=mfe,
                mae_r=mae,
            )
        if stop_hit:
            return _result(
                plan,
                state="STOPPED",
                now_ms=now_ms,
                last_evaluated_open_time=last_evaluated_open_time,
                entry_open_time=entry_open_time,
                hit_1r_open_time=hit_1r_open_time,
                terminal_open_time=open_time,
                mfe_r=mfe,
                mae_r=mae,
            )
        if t1_hit and not t1_already:
            hit_1r_open_time = open_time

    # Every expected candle processed with no terminal event.
    if end_ms >= expiry:
        state: OutcomeState = "EXPIRED_OPEN" if entered else "EXPIRED_UNENTERED"
        return _result(
            plan,
            state=state,
            now_ms=now_ms,
            last_evaluated_open_time=last_evaluated_open_time,
            entry_open_time=entry_open_time,
            hit_1r_open_time=hit_1r_open_time,
            terminal_open_time=last_evaluated_open_time,
            terminal_reason=REASON_HORIZON_REACHED,
            mfe_r=(mfe if have_excursion else None),
            mae_r=(mae if have_excursion else None),
        )

    nonterminal_state: OutcomeState = (
        "HIT_1R" if hit_1r_open_time is not None else ("ENTERED" if entered else "PENDING_ENTRY")
    )
    return _result(
        plan,
        state=nonterminal_state,
        now_ms=now_ms,
        last_evaluated_open_time=last_evaluated_open_time,
        entry_open_time=entry_open_time,
        hit_1r_open_time=hit_1r_open_time,
        mfe_r=(mfe if have_excursion else None),
        mae_r=(mae if have_excursion else None),
    )
