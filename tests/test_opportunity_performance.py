"""Bounded performance test for the TA Opportunity Engine v0.1 detectors.

Covers roughly the production ceiling (max_symbols=30) across both
timeframes (3m, 5m) and both detector families, using only in-memory
DataFrames — no DB, no network. The threshold is deliberately generous
(two orders of magnitude above the observed runtime on a dev machine) so
this cannot become wall-clock flaky in CI.
"""
from __future__ import annotations

import time

import pandas as pd

from apex.opportunity.detectors.sweep_reclaim import detect_sweep_reclaim
from apex.opportunity.detectors.volatility_compression import detect_volatility_compression

N_SYMBOLS = 30
TIMEFRAMES = ("3m", "5m")
MAX_SECONDS = 10.0  # generous bound; actual runtime is expected to be well under 1s


def _make_df(n: int = 200) -> pd.DataFrame:
    opens, highs, lows, closes = [], [], [], []
    base = 100.0
    for i in range(n):
        c = base + (i % 7) - 3  # small deterministic oscillation, no randomness
        opens.append(c)
        closes.append(c)
        highs.append(c + 1.0)
        lows.append(c - 1.0)
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


def test_detectors_bounded_over_30_symbols_2_timeframes():
    symbols = [f"SYM{i}" for i in range(N_SYMBOLS)]
    df = _make_df()

    start = time.perf_counter()
    total_calls = 0
    for symbol in symbols:
        for timeframe in TIMEFRAMES:
            detect_volatility_compression(symbol, timeframe, df)
            detect_sweep_reclaim(symbol, timeframe, df)
            total_calls += 2
    elapsed = time.perf_counter() - start

    assert total_calls == N_SYMBOLS * len(TIMEFRAMES) * 2
    assert elapsed < MAX_SECONDS, (
        f"30 symbols x {len(TIMEFRAMES)} timeframes x 2 detector families took "
        f"{elapsed:.3f}s, expected well under {MAX_SECONDS}s"
    )
