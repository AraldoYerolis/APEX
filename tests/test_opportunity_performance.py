"""Bounded performance test for the TA Opportunity Engine v0.1 detectors
plus the Context and ranking v0.1 context/scoring computation.

Covers roughly the production ceiling (max_symbols=30) across both
timeframes (3m, 5m) and all five detector families, using only in-memory
DataFrames — no DB, no network. 300 closed candles are used (rather than the
prior 200) so every Support/Resistance detector's own minimum window
(S/R zone construction plus its reserved event tail — see
support_resistance.py / the three support_resistance_*.py detectors) is
comfortably satisfied and each one performs real zone construction, not a
row-count short-circuit. The threshold is deliberately generous (well above
the observed runtime on a dev machine) so this cannot become wall-clock
flaky in CI.

Context and ranking v0.1 extension: the SAME measured region also exercises
once-per-scan BTC 15m trend, once-per-symbol own 15m trend, per-(symbol,
timeframe) primary structure and descriptive volatility, and a real
assemble_context/score_opportunity call per setup family — mirroring the
per-pair cost shape engine.run_opportunity_scan actually does (see
engine.py's "Compute 15m trend once per symbol, BTC trend once per scan,
primary structure/volatility once per eligible (symbol,timeframe)"
contract), at the same 30-symbol x 2-timeframe scale, still under the
unchanged 10-second ceiling. Context/scoring inputs use their own
deterministic, grid-aligned, closed-at-one-fixed-now_ms fixtures (separate
from the detector fixture below) so every context.py component reliably
resolves AVAILABLE with real bounded work — never a malformed/short/stale
shortcut — regardless of the detector fixture's own timestamp scheme.
"""
from __future__ import annotations

import time
from typing import get_args

import pandas as pd

from apex.opportunity.context import (
    TIMEFRAME_DURATION_MS,
    assemble_context,
    compute_primary_structure_component,
    compute_symbol_trend_component,
    compute_volatility_component,
)
from apex.opportunity.contract import CONTRACT_VERSION, DetectorFinding, SetupFamily
from apex.opportunity.detectors.support_resistance_breakout_retest import (
    detect_support_resistance_breakout_retest,
)
from apex.opportunity.detectors.support_resistance_failed_breakout import (
    detect_support_resistance_failed_breakout,
)
from apex.opportunity.detectors.support_resistance_rejection import (
    detect_support_resistance_rejection,
)
from apex.opportunity.detectors.sweep_reclaim import detect_sweep_reclaim
from apex.opportunity.detectors.volatility_compression import detect_volatility_compression
from apex.opportunity.scoring import score_opportunity
from apex.opportunity.trade_plan import build_trade_plan
from apex.opportunity.trade_plan_outcome import evaluate_trade_plan_outcome

N_SYMBOLS = 30
TIMEFRAMES = ("3m", "5m")
N_DETECTORS = 5
N_CANDLES = 300
MAX_SECONDS = 10.0  # generous bound; actual runtime is expected to be well under 1s

FAMILIES: tuple[str, ...] = get_args(SetupFamily)

# Fixed, whole-millisecond "now" for every context/scoring computation below,
# divisible by every relevant timeframe duration (3m=180_000ms,
# 5m=300_000ms, 15m=900_000ms; LCM=900_000ms) so a grid built as
# `now_ms - n*duration + i*duration` satisfies context.py's own
# `open_time % duration == 0` alignment check for every timeframe used here,
# and the last candle's own close boundary lands exactly at now_ms (closed,
# not stale).
NOW_MS = 1_800_000_000_000
assert NOW_MS % TIMEFRAME_DURATION_MS["15m"] == 0


def _make_df(n: int = N_CANDLES) -> pd.DataFrame:
    """Existing detector-only fixture, unchanged: not grid-aligned to any
    real timeframe duration (open_time is a plain row index) — fine here
    since raw detector calls never route through context.py's own window
    validation, only through their own S/R/pivot logic.
    """
    opens, highs, lows, closes = [], [], [], []
    base = 100.0
    for i in range(n):
        c = base + (i % 7) - 3  # small deterministic oscillation, no randomness
        opens.append(c)
        closes.append(c)
        highs.append(c + 1.0)
        lows.append(c - 1.0)

    # Two confirmed resistance-pivot touches (price spikes) well before the
    # tail, spaced far enough apart to cluster into one Support/Resistance
    # zone (see support_resistance.py) — gives the three S/R detectors real
    # zone construction to do, not just an empty-zones no-op.
    touch_a, touch_b = n - 60, n - 40
    highs[touch_a] = base + 6.0
    closes[touch_a] = base + 5.0
    opens[touch_a] = base + 4.8
    lows[touch_a] = base + 4.5
    highs[touch_b] = base + 6.1
    closes[touch_b] = base + 5.1
    opens[touch_b] = base + 4.9
    lows[touch_b] = base + 4.6

    # A pivot low + sweep + reclaim near the tail so SWEEP_RECLAIM has
    # something to find on every symbol/timeframe pair, not just a no-op.
    lows[n - 15] = base - 6.0
    highs[n - 15] = base - 4.5
    closes[n - 15] = base - 5.0
    opens[n - 15] = base - 4.8
    lows[n - 3] = base - 6.5
    highs[n - 3] = base - 5.0
    closes[n - 3] = base - 5.5
    opens[n - 3] = base - 5.2
    closes[n - 2] = base - 4.0
    opens[n - 2] = base - 5.5
    highs[n - 2] = base - 3.9
    lows[n - 2] = base - 5.6
    return pd.DataFrame({
        "open_time": list(range(n)),
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": [1000.0] * n,
    })


def _make_context_primary_df(timeframe: str, now_ms: int, n: int = N_CANDLES) -> pd.DataFrame:
    """Deterministic, grid-aligned, closed-at-`now_ms` primary-timeframe
    fixture used ONLY for context.py's compute_primary_structure_component/
    compute_volatility_component — a flat baseline with two well-separated,
    ascending confirmed HIGH pivots and two ascending confirmed LOW pivots
    near the start of the window (comfortably >=3 confirming bars before
    the tail), guaranteeing a real AVAILABLE LONG structure and a real,
    finite descriptive ATR% every time, independent of the unrelated
    detector-only fixture above.
    """
    duration = TIMEFRAME_DURATION_MS[timeframe]
    start_open_time = now_ms - n * duration

    opens = [100.0] * n
    highs = [100.5] * n
    lows = [99.5] * n
    closes = [100.0] * n

    # context.py bounds primary-structure/volatility windows to the last
    # MAX_STRUCTURE_ROWS/MAX_VOLATILITY_ROWS (200) eligible rows — with
    # n=300 total rows that retained window is exactly the LAST 200 rows
    # (positional indices 100..299), so these override pivots must sit
    # within that tail, not near the very start of the full 300-row frame.
    overrides = {
        n - 190: {"low": 97.0, "open": 99.0, "close": 98.0, "high": 99.5},
        n - 180: {"high": 103.0, "low": 100.5, "open": 101.0, "close": 102.0},
        n - 170: {"low": 98.0, "open": 99.5, "close": 98.5, "high": 100.0},
        n - 160: {"high": 105.0, "low": 102.0, "open": 103.0, "close": 104.0},
    }
    for idx, fields in overrides.items():
        opens[idx] = fields["open"]
        highs[idx] = fields["high"]
        lows[idx] = fields["low"]
        closes[idx] = fields["close"]

    open_times = [start_open_time + i * duration for i in range(n)]
    return pd.DataFrame({
        "open_time": open_times,
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": [1000.0] * n,
    })


def _make_15m_df(now_ms: int, n: int = 100) -> pd.DataFrame:
    """Deterministic, grid-aligned, closed-at-`now_ms` 15m fixture (a clean
    uptrend) used for both the once-per-scan BTC trend and the
    once-per-symbol own trend — real, finite EMA9/EMA21/VWAP96 arithmetic,
    bounded to context.py's own last-96-row trend window.
    """
    duration = TIMEFRAME_DURATION_MS["15m"]
    start_open_time = now_ms - n * duration
    closes = [100.0 + i for i in range(n)]
    open_times = [start_open_time + i * duration for i in range(n)]
    return pd.DataFrame({
        "open_time": open_times,
        "open": closes,
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "volume": [1000.0] * n,
    })


def _dummy_finding_for_family(family: str, symbol: str, timeframe: str, source_close_time: int) -> DetectorFinding:
    """Deterministic representative finding per family, valid enough to
    exercise build_trade_plan's real (non-short-circuit) formula path for
    every non-compression family — mirrors the scoring loop's own rationale
    just above (one representative case per family, not a dependency on the
    detector fixture above actually producing one of every family).
    """
    duration = TIMEFRAME_DURATION_MS[timeframe]
    common = dict(
        symbol=symbol,
        direction="LONG",
        setup_family=family,
        detector_version="perf_v0_1",
        primary_timeframe=timeframe,
        source_candle_open_time=source_close_time - duration,
        source_candle_close_time=source_close_time,
        fingerprint_key="perf-fixture",
    )
    if family == "SWEEP_RECLAIM":
        return DetectorFinding(
            measurements={"reclaim_close": 110.0, "sweep_low": 100.0}, evidence={}, warnings=[], **common
        )
    if family == "VOLATILITY_COMPRESSION":
        return DetectorFinding(measurements={}, evidence={}, warnings=[], **common)
    return DetectorFinding(
        evidence={"source_close": 105.0, "zone_lower": 100.0, "zone_upper": 108.0},
        measurements={}, warnings=[], **common
    )


def _dummy_outcome_df(timeframe: str, not_before: int) -> pd.DataFrame:
    """One closed candle exactly at evaluation_not_before, reaching the
    LONG entry price used by _dummy_finding_for_family above — enough for
    evaluate_trade_plan_outcome to do real (ENTERED) bounded work rather
    than a trivial "nothing due yet" short-circuit.
    """
    return pd.DataFrame({
        "open_time": [not_before],
        "open": [108.0], "high": [111.0], "low": [104.0], "close": [108.0],
    })


def test_detectors_and_context_scoring_bounded_over_30_symbols_2_timeframes():
    symbols = [f"SYM{i}" for i in range(N_SYMBOLS)]
    df = _make_df()
    context_dfs = {tf: _make_context_primary_df(tf, NOW_MS) for tf in TIMEFRAMES}
    df_15m = _make_15m_df(NOW_MS)

    start = time.perf_counter()
    total_calls = 0
    total_plans = 0

    # Once per scan (see engine.py: BTC trend computed at most once/scan).
    btc_trend = compute_symbol_trend_component(df_15m, NOW_MS)
    assert btc_trend.status == "AVAILABLE"
    assert btc_trend.direction == "LONG"

    for symbol in symbols:
        # Once per symbol (see engine.py: symbol's own 15m trend computed
        # once per symbol, reused across both its 3m and 5m timeframes).
        symbol_trend = compute_symbol_trend_component(df_15m, NOW_MS)
        assert symbol_trend.status == "AVAILABLE"
        assert symbol_trend.direction == "LONG"

        for timeframe in TIMEFRAMES:
            detect_volatility_compression(symbol, timeframe, df)
            detect_sweep_reclaim(symbol, timeframe, df)
            detect_support_resistance_rejection(symbol, timeframe, df)
            detect_support_resistance_breakout_retest(symbol, timeframe, df)
            detect_support_resistance_failed_breakout(symbol, timeframe, df)
            total_calls += N_DETECTORS

            # Once per (symbol, timeframe) — real bounded work, not a
            # malformed/short/stale shortcut (both assert AVAILABLE with a
            # real, finite result below).
            context_df = context_dfs[timeframe]
            structure = compute_primary_structure_component(context_df, timeframe, NOW_MS)
            volatility = compute_volatility_component(context_df, timeframe, NOW_MS)
            assert structure.status == "AVAILABLE"
            assert structure.direction == "LONG"
            assert volatility.status == "AVAILABLE"
            assert volatility.atr_percent is not None

            # A real assemble_context + score_opportunity call per setup
            # family (scoring rules are identical across all five — see
            # scoring.py — so one deterministic representative context per
            # family suffices, rather than requiring the detectors above to
            # have actually produced a finding of every family on this
            # synthetic fixture).
            for _family in FAMILIES:
                context = assemble_context(
                    as_of_ms=NOW_MS,
                    primary_timeframe=timeframe,
                    scored_direction="LONG",
                    symbol_htf_trend=symbol_trend,
                    primary_structure=structure,
                    btc_htf_trend=btc_trend,
                    volatility=volatility,
                )
                result = score_opportunity(context)
                assert result.total_score is not None
                assert result.total_score == 100.0  # all three components aligned LONG

                # Trade plans and outcome evidence v0.1 — same per-(symbol,
                # timeframe, family) cost shape as engine._maybe_create_trade_plan
                # (one build_trade_plan call) plus one evaluate_trade_plan_outcome
                # replay, added to the SAME measured region and bound.
                finding = _dummy_finding_for_family(_family, symbol, timeframe, NOW_MS)
                plan = build_trade_plan(
                    finding,
                    plan_uid=f"{symbol}-{timeframe}-{_family}",
                    opportunity_uid=f"{symbol}-{timeframe}-{_family}-opp",
                    fingerprint="perf-fp",
                    created_at="2026-09-15T00:00:00Z",
                )
                total_plans += 1
                if plan.availability == "AVAILABLE":
                    outcome_df = _dummy_outcome_df(timeframe, NOW_MS)
                    outcome = evaluate_trade_plan_outcome(
                        plan, outcome_df, NOW_MS + TIMEFRAME_DURATION_MS[timeframe]
                    )
                    assert outcome.state in ("ENTERED", "AMBIGUOUS")

    elapsed = time.perf_counter() - start

    assert total_calls == N_SYMBOLS * len(TIMEFRAMES) * N_DETECTORS
    assert len(FAMILIES) == N_DETECTORS
    assert total_plans == N_SYMBOLS * len(TIMEFRAMES) * N_DETECTORS
    assert plan.opportunity_contract_version == CONTRACT_VERSION
    assert elapsed < MAX_SECONDS, (
        f"30 symbols x {len(TIMEFRAMES)} timeframes x ({N_DETECTORS} detectors + context + "
        f"scoring + trade plan/outcome per family) took {elapsed:.3f}s, expected well under {MAX_SECONDS}s"
    )
