"""Tests for the pure VOLATILITY_COMPRESSION and SWEEP_RECLAIM detectors
(TA Opportunity Engine v0.1). No DB, no scheduler — detectors take plain
DataFrames and return DetectorFinding objects only.
"""
from __future__ import annotations

import pandas as pd

from apex.indicators.atr import atr as compute_atr
from apex.opportunity.contract import DetectorFinding
from apex.opportunity.detectors.sweep_reclaim import detect_sweep_reclaim
from apex.opportunity.detectors.volatility_compression import (
    COMPRESSION_ATR_PERCENTILE,
    COMPRESSION_LOOKBACK,
    MIN_CANDLES_REQUIRED as VOL_COMPRESSION_MIN_CANDLES,
    RANGE_CONTRACTION_LOOKBACK,
    RANGE_CONTRACTION_RATIO,
    _episode_start_index,
    detect_volatility_compression,
)
from apex.opportunity.engine import compute_fingerprint


def _make_df(opens, highs, lows, closes, volumes=None):
    n = len(closes)
    volumes = volumes or [1000.0] * n
    return pd.DataFrame({
        "open_time": list(range(n)),
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


# ------------------------------------------------------------------ VOLATILITY_COMPRESSION

def _steady_no_compression_df() -> pd.DataFrame:
    n = 60
    opens, highs, lows, closes = [], [], [], []
    for i in range(n):
        c = 100.0 + i
        opens.append(c - 1.0)
        closes.append(c)
        highs.append(c + 1.0)
        lows.append(c - 1.0)
    return _make_df(opens, highs, lows, closes)


def test_volatility_compression_positive_case():
    df = _compression_then_breakout_df()
    findings = detect_volatility_compression("BTC", "5m", df)
    assert len(findings) == 1
    f = findings[0]
    assert f.setup_family == "VOLATILITY_COMPRESSION"
    assert f.symbol == "BTC"
    assert f.primary_timeframe == "5m"
    assert f.direction == "LONG"  # close is above the episode-start close
    assert f.measurements["contraction_ratio"] <= 0.65 or f.measurements["atr_percentile"] <= 0.25
    assert f.measurements["expansion_ratio"] >= 1.5 or f.measurements["atr_rising"] is True


def test_volatility_compression_negative_case_no_compression():
    df = _steady_no_compression_df()
    findings = detect_volatility_compression("BTC", "5m", df)
    assert findings == []


def test_volatility_compression_insufficient_data():
    df = _make_df([100.0] * 5, [100.5] * 5, [99.5] * 5, [100.0] * 5)
    assert len(df) < VOL_COMPRESSION_MIN_CANDLES
    assert detect_volatility_compression("BTC", "5m", df) == []


def test_volatility_compression_records_15m_context_and_conflict_warning():
    df = _compression_then_breakout_df()
    # 15m context in a clear downtrend — conflicts with the LONG expansion tag.
    n15 = 40
    closes15 = [200.0 - i for i in range(n15)]
    context_df = _make_df(
        [c + 0.5 for c in closes15], [c + 1.0 for c in closes15],
        [c - 1.0 for c in closes15], closes15,
    )
    findings = detect_volatility_compression("BTC", "5m", df, context_df_15m=context_df)
    assert len(findings) == 1
    f = findings[0]
    assert f.evidence["context_15m_trend_bias"] == "SHORT"
    assert any("conflicts" in w for w in f.warnings)


# --------------------------------------------- VOLATILITY_COMPRESSION episode identity

def _compression_then_breakout_df(extra_expansion_bars: int = 0) -> pd.DataFrame:
    """99 baseline bars (range=2.0), 10 compressed bars (range=0.1), then a
    breakout bar, plus `extra_expansion_bars` further expansion bars — i.e.
    the *same* continuing compression -> expansion episode, just evaluated
    at a later bar (as would happen on successive scans while it develops).

    99 baseline bars (not just enough to clear MIN_CANDLES_REQUIRED) so the
    baseline/compressed transition bar itself has a full COMPRESSION_
    LOOKBACK-bar trailing window entirely inside the baseline, making it
    verifiably NOT_COMPRESSED rather than UNKNOWN — see
    _episode_start_index's three-state classification. A thinner baseline
    leaves that transition bar without enough history to classify at all,
    which correctly (per that function's contract) withholds the finding
    rather than guessing; 99 bars mirrors a CandleStore buffer that has
    actually accumulated real history, not a razor's-edge MIN_CANDLES_
    REQUIRED fixture.
    """
    opens, highs, lows, closes = [], [], [], []
    for _ in range(99):
        opens.append(100.0)
        closes.append(100.0)
        highs.append(101.0)
        lows.append(99.0)
    for _ in range(10):
        opens.append(100.0)
        closes.append(100.0)
        highs.append(100.05)
        lows.append(99.95)
    opens.append(100.0)
    closes.append(103.0)
    highs.append(103.2)
    lows.append(99.9)
    for i in range(extra_expansion_bars):
        opens.append(103.0 + i)
        closes.append(105.0 + i)
        highs.append(105.5 + i)
        lows.append(102.5 + i)
    return _make_df(opens, highs, lows, closes)


def test_volatility_compression_same_episode_keeps_identity_across_bars():
    """Correction 3 regression: the fingerprint used to change on every
    successive bar of the *same* continuing expansion, because the episode
    start was found by walking back from the latest (ever-changing) trigger
    bar instead of the actual structural start of the compression.
    """
    finding_bar_n = detect_volatility_compression("BTC", "5m", _compression_then_breakout_df(0))[0]
    finding_bar_n1 = detect_volatility_compression("BTC", "5m", _compression_then_breakout_df(1))[0]
    finding_bar_n2 = detect_volatility_compression("BTC", "5m", _compression_then_breakout_df(2))[0]

    assert finding_bar_n.fingerprint_key == finding_bar_n1.fingerprint_key == finding_bar_n2.fingerprint_key
    assert (
        compute_fingerprint(finding_bar_n)
        == compute_fingerprint(finding_bar_n1)
        == compute_fingerprint(finding_bar_n2)
    )


def test_volatility_compression_later_separate_episode_gets_new_identity():
    """A second, later compression -> expansion after a genuine return to a
    quiet range must NOT collide with the first episode's identity.
    """
    df_episode_one = _compression_then_breakout_df(0)
    finding_one = detect_volatility_compression("BTC", "5m", df_episode_one)[0]

    opens = list(df_episode_one["open"])
    highs = list(df_episode_one["high"])
    lows = list(df_episode_one["low"])
    closes = list(df_episode_one["close"])
    for _ in range(20):  # genuine re-compression before the second breakout
        opens.append(103.0)
        closes.append(103.0)
        highs.append(103.05)
        lows.append(102.95)
    opens.append(103.0)
    closes.append(99.0)
    highs.append(103.2)
    lows.append(98.8)
    df_episode_two = _make_df(opens, highs, lows, closes)
    finding_two = detect_volatility_compression("BTC", "5m", df_episode_two)[0]

    assert finding_one.fingerprint_key != finding_two.fingerprint_key
    assert compute_fingerprint(finding_one) != compute_fingerprint(finding_two)


# --------------------------- VOLATILITY_COMPRESSION direction-invariant identity

def _compression_direction_reversal_stages() -> list[pd.DataFrame]:
    """Three real, continuous candle histories of the SAME compression
    episode (fixed episode start at open_time=99 throughout — only the
    trailing expansion bars differ): the developing move first nets LONG
    (close 103.0, above the episode-start close of 100.0), then genuinely
    reverses to net SHORT (close 99.0), then reverses back to net LONG
    (close 101.0). Not mocked — each stage is evaluated by the real
    detector; only the input candle history differs between calls.
    Empirically constructed and verified to keep one fingerprint_key
    throughout (see the direction-invariant identity tests below).
    """
    stage_long = _compression_then_breakout_df(0)  # close=103.0 -> LONG

    opens = list(stage_long["open"])
    highs = list(stage_long["high"])
    lows = list(stage_long["low"])
    closes = list(stage_long["close"])
    down_bars = [(103.0, 103.5, 100.5, 101.0), (101.0, 101.5, 98.5, 99.0)]
    for o, h, low, c in down_bars:
        opens.append(o)
        highs.append(h)
        lows.append(low)
        closes.append(c)
    stage_short = _make_df(opens, highs, lows, closes)  # close=99.0 -> SHORT

    opens = list(opens)
    highs = list(highs)
    lows = list(lows)
    closes = list(closes)
    opens.append(99.0)
    highs.append(101.5)
    lows.append(98.5)
    closes.append(101.0)
    stage_long_again = _make_df(opens, highs, lows, closes)  # close=101.0 -> LONG

    return [stage_long, stage_short, stage_long_again]


def test_volatility_compression_direction_flip_keeps_same_episode_key():
    """Direction-in-identity resolution: a real LONG -> SHORT -> LONG
    continuation of the same compression episode must report the SAME
    fingerprint_key throughout even though the descriptive `direction` tag
    changes each time — direction is evidence about the developing
    expansion, not part of this family's identity.
    """
    stages = _compression_direction_reversal_stages()
    findings = [detect_volatility_compression("BTC", "5m", df) for df in stages]
    assert [len(f) for f in findings] == [1, 1, 1]
    f_long, f_short, f_long_again = (f[0] for f in findings)

    assert f_long.direction == "LONG"
    assert f_short.direction == "SHORT"
    assert f_long_again.direction == "LONG"
    assert f_long.fingerprint_key == f_short.fingerprint_key == f_long_again.fingerprint_key


def test_volatility_compression_fingerprint_direction_invariant():
    """The engine's actual dedupe identity (compute_fingerprint), not just
    fingerprint_key, must also stay identical across a real direction flip
    of the same episode.
    """
    stages = _compression_direction_reversal_stages()
    f_long, f_short, f_long_again = (
        detect_volatility_compression("BTC", "5m", df)[0] for df in stages
    )
    fp_long = compute_fingerprint(f_long)
    fp_short = compute_fingerprint(f_short)
    fp_long_again = compute_fingerprint(f_long_again)
    assert fp_long == fp_short == fp_long_again


def test_sweep_reclaim_direction_is_part_of_identity_unchanged():
    """Contrast case: SWEEP_RECLAIM must keep direction as part of its
    fingerprint identity — unlike VOLATILITY_COMPRESSION, a LONG and a SHORT
    finding must never be treated as the same underlying event even if
    other fields coincided.
    """
    df = _sweep_reclaim_long_df()
    long_finding = [f for f in detect_sweep_reclaim("BTC", "5m", df) if f.direction == "LONG"][0]
    forced_short = DetectorFinding(**{**long_finding.__dict__, "direction": "SHORT"})
    assert compute_fingerprint(long_finding) != compute_fingerprint(forced_short)


# ------------------------------------------------- VOLATILITY_COMPRESSION Correction 5


def _atr_percentile_only_compression_df(
    n_baseline: int = 49, n_compressed: int = 15, extra_expansion_bars: int = 0
) -> pd.DataFrame:
    """A compression episode that is detectable ONLY via atr_percentile, never
    via contraction_ratio: every bar's high-low range is a constant 1.0, so
    the range-contraction ratio never moves. What varies is the gap between
    consecutive bars — large alternating gaps during `n_baseline` (big true
    range -> high ATR baseline), then flat/no gaps once compression begins
    (small true range -> ATR decays), even though high-low itself never
    changes. This reproduces the Validator's reachable case: ATR percentile
    signals compression while no individual bar (or window) ever satisfies
    the range-contraction threshold.
    """
    opens, highs, lows, closes = [], [], [], []
    for i in range(n_baseline):
        c = 100.0 if i % 2 == 0 else 110.0
        opens.append(c)
        closes.append(c)
        highs.append(c + 0.5)
        lows.append(c - 0.5)
    for _ in range(n_compressed):
        c = 100.0
        opens.append(c)
        closes.append(c)
        highs.append(c + 0.5)
        lows.append(c - 0.5)
    opens.append(100.0)
    closes.append(103.0)
    highs.append(103.5)
    lows.append(99.5)
    for i in range(extra_expansion_bars):
        opens.append(103.0 + i)
        closes.append(105.0 + i)
        highs.append(105.5 + i)
        lows.append(102.5 + i)
    return _make_df(opens, highs, lows, closes)


def test_volatility_compression_atr_percentile_only_is_detected_and_stable_across_bars():
    """Correction 5 regression (primary case): a bar-by-bar check confirms no
    bar's range-contraction ratio ever drops to/below the threshold anywhere
    in the series, yet the detector still finds compression (via
    atr_percentile alone), and the *same* episode keeps an identical
    fingerprint across successive scans as expansion continues — exercising
    detect_volatility_compression and the real fingerprint output, not the
    internal episode-boundary helper directly.
    """
    df = _atr_percentile_only_compression_df(extra_expansion_bars=0)
    ranges = df["high"] - df["low"]
    short_avg = ranges.rolling(RANGE_CONTRACTION_LOOKBACK).mean()
    long_avg = ranges.rolling(COMPRESSION_LOOKBACK).mean()
    assert not ((short_avg / long_avg) <= RANGE_CONTRACTION_RATIO).any(), (
        "fixture must not be range-contraction-driven anywhere in the series"
    )

    finding_bar_n = detect_volatility_compression("BTC", "5m", df)
    assert len(finding_bar_n) == 1
    f = finding_bar_n[0]
    assert f.measurements["atr_percentile"] <= COMPRESSION_ATR_PERCENTILE
    assert f.measurements["contraction_ratio"] > RANGE_CONTRACTION_RATIO

    finding_bar_n1 = detect_volatility_compression(
        "BTC", "5m", _atr_percentile_only_compression_df(extra_expansion_bars=1)
    )
    assert len(finding_bar_n1) == 1

    assert f.fingerprint_key == finding_bar_n1[0].fingerprint_key
    assert compute_fingerprint(f) == compute_fingerprint(finding_bar_n1[0])


def test_volatility_compression_atr_percentile_only_identity_survives_front_trim():
    """Correction 5 regression: simulate CandleStore evicting its oldest
    candles (the rolling 300-candle buffer advancing) by dropping rows from
    the front of the same underlying candle history, keeping each remaining
    row's own open_time. The episode identity found for the *same* real
    compression episode must not change just because its position in the
    DataFrame shifted.

    Uses _narrow_atr_range_compression_history (see Correction 5b below)
    rather than _atr_percentile_only_compression_df: that fixture's
    alternating +/-10 gap oscillation produces near-tied ATR values by
    construction, which turned out to be adversarial for a true
    percentile-rank walk-back (ordinal comparisons among near-tied values
    are exactly what a small EWM-initialization perturbation can flip —
    see _episode_start_index's docstring) and is not representative of
    real candle data. This fixture's smoothly varying, non-oscillating ATR
    is far more representative and is empirically stable well beyond a
    single-bar eviction.
    """
    history = _narrow_atr_range_compression_history()
    df_full = history.iloc[0:300].reset_index(drop=True)
    finding_full = detect_volatility_compression("BTC", "5m", df_full)
    assert len(finding_full) == 1

    df_trimmed = history.iloc[5:300].reset_index(drop=True)
    finding_trimmed = detect_volatility_compression("BTC", "5m", df_trimmed)
    assert len(finding_trimmed) == 1

    assert finding_full[0].fingerprint_key == finding_trimmed[0].fingerprint_key
    assert compute_fingerprint(finding_full[0]) == compute_fingerprint(finding_trimmed[0])


# --------------------------------------- VOLATILITY_COMPRESSION Correction 5b


def _narrow_atr_range_compression_history() -> pd.DataFrame:
    """Independent-review reproduction fixture (MEDIUM defect in the first
    Correction 5 fix): 300 candles of constant high-low range (2.0, so
    contraction_ratio is pinned at 1.0 and can never trigger or identify
    compression), where ATR is driven purely by a gradual, non-oscillating
    change in bar-to-bar step size — 280 bars stepping by 3.0, then 19
    bars stepping by 2.0 (ATR drifts down within roughly a 3.0-4.4 band),
    then 3 bars stepping by 2.4 (mild expansion). atr_percentile correctly
    ranks the last several bars in the bottom ~10% of their own trailing
    window; the ratio-of-averages approximation used by the first
    Correction 5 fix (ATR_CONTRACTION_RATIO <= 0.65) never drops below
    ~0.75 for this fixture, because the actual ATR values never move far
    enough in magnitude — only in rank. That mismatch is the MEDIUM defect:
    with no bar satisfying either the range or the ratio condition, the
    walk-back fell through to the rolling buffer's first row every time.
    """
    steps = [3.0] * 280 + [2.0] * 19 + [2.4] * 3
    closes = pd.Series(steps).cumsum() + 1000
    return pd.DataFrame({
        "open_time": 1700000000000 + pd.Series(range(len(steps))) * 300000,
        "open": closes,
        "high": closes + 1,
        "low": closes - 1,
        "close": closes,
        "volume": 1000.0,
    })


def test_volatility_compression_narrow_atr_range_episode_keeps_one_identity_across_buffer_advance():
    """Correction 5b regression: reproduces the independent review's exact
    finding. Sliding a 300-candle CandleStore-style window forward by 1 and
    then 2 more bars (the real production shape of the rolling buffer
    advancing — oldest candle evicted, one new candle appended) must keep
    finding the *same* compression episode with an identical fingerprint_key
    and an identical full fingerprint every time, not a new ACTIVE row on
    every scan.
    """
    history = _narrow_atr_range_compression_history()
    findings = []
    for end in (300, 301, 302):
        df = history.iloc[end - 300 : end].reset_index(drop=True)
        result = detect_volatility_compression("BTC", "5m", df)
        assert len(result) == 1, f"expected a finding for window ending at {end}"
        findings.append(result[0])

    keys = {f.fingerprint_key for f in findings}
    fingerprints = {compute_fingerprint(f) for f in findings}
    assert len(keys) == 1, f"episode key drifted across windows: {[f.fingerprint_key for f in findings]}"
    assert len(fingerprints) == 1, "fingerprint drifted across windows despite identical episode key"

    # And confirm the fixture is genuinely undetectable via range or the
    # rejected ratio approximation alone, i.e. this is a real
    # atr_percentile-only case, not accidentally range-driven.
    for f in findings:
        assert f.measurements["contraction_ratio"] == 1.0
        assert f.measurements["atr_percentile"] <= COMPRESSION_ATR_PERCENTILE


def _compression_then_breakout_with_interrupt_df(
    n_expansion_before: int = 2, n_expansion_after: int = 1
) -> pd.DataFrame:
    """Same shape as _compression_then_breakout_df (99 baseline bars — see
    that helper's docstring for why), but after `n_expansion_before`
    genuine expansion bars, ONE small/quiet bar interrupts before expansion
    resumes for `n_expansion_after` more bars — "expansion -> one
    small/compressed bar -> expansion resumes" (the LOW finding scenario)."""
    opens, highs, lows, closes = [], [], [], []
    for _ in range(99):
        opens.append(100.0)
        closes.append(100.0)
        highs.append(101.0)
        lows.append(99.0)
    for _ in range(10):
        opens.append(100.0)
        closes.append(100.0)
        highs.append(100.05)
        lows.append(99.95)
    opens.append(100.0)
    closes.append(103.0)
    highs.append(103.2)
    lows.append(99.9)
    for i in range(n_expansion_before):
        opens.append(103.0 + i)
        closes.append(105.0 + i)
        highs.append(105.5 + i)
        lows.append(102.5 + i)
    last_close = closes[-1]
    opens.append(last_close)
    closes.append(last_close + 0.02)
    highs.append(last_close + 0.05)
    lows.append(last_close - 0.05)
    start = closes[-1]
    for i in range(n_expansion_after):
        opens.append(start + i)
        closes.append(start + 2.0 + i)
        highs.append(start + 2.5 + i)
        lows.append(start - 0.5 + i)
    return _make_df(opens, highs, lows, closes)


def test_volatility_compression_interrupting_bar_currently_reanchors_episode_low_regression():
    """Correction 5b status update (LOW, documented, not fixed this pass):
    Correction 5's first draft happened to keep this scenario stable, but
    only as a side effect of approximating atr_percentile with a
    ratio-of-averages (ATR_CONTRACTION_RATIO) that diluted a lone quiet
    bar's influence through its large-range neighbors. That approximation
    is what Correction 5b removes, because it is not equivalent to the
    detector's real percentile-rank trigger and can miss genuine
    detections (the MEDIUM defect this pass fixes). The correct
    percentile-rank formula has no such dilution: a lone quiet bar's own
    ATR can rank in the bottom quartile of its trailing window regardless
    of how many expansion bars precede it, so it currently re-anchors the
    episode's identity onto itself instead of preserving the original
    episode's identity.

    This test documents that CURRENT, real behavior (not the aspirational
    fix) so a future change to it is a deliberate, visible decision rather
    than a silent regression. See _episode_start_index's docstring
    ("Because ATR is a smoothed/lagging measure...") for why suppressing
    this reading would risk suppressing genuine compression detections,
    which is out of scope for this pass to attempt.
    """
    finding_uninterrupted = detect_volatility_compression(
        "BTC", "5m", _compression_then_breakout_df(0)
    )
    assert len(finding_uninterrupted) == 1

    finding_interrupted = detect_volatility_compression(
        "BTC", "5m", _compression_then_breakout_with_interrupt_df(2, 1)
    )
    assert len(finding_interrupted) == 1

    assert finding_uninterrupted[0].fingerprint_key != finding_interrupted[0].fingerprint_key


# ------------------- VOLATILITY_COMPRESSION unverifiable-anchor withholding


def test_volatility_compression_expansion_veto_rejecting_now_withholds_rather_than_fabricates():
    """Independent-review reproduction: a newly developing signal (not a
    long-lived compression whose boundary has aged out) where BOTH the
    latest bar and every historical bar the walk-back inspects fail its
    COMPRESSED test — the latest bar specifically because the expansion
    veto rejects it (its own range, 2.0, is a genuine local expansion
    relative to the immediately preceding baseline, even though the
    top-level trigger independently flags it via atr_percentile/
    expansion_ratio). With nothing anywhere in history verified as
    COMPRESSED, there is no defensible anchor, so the finding must be
    withheld entirely — not reported under the oldest available candle's
    open_time (the previous defect) or any other substitute index.
    """
    # Matches the reviewer's construction exactly: 299 rising bars, then 2
    # flat bars with a wider range.
    baseline = [1000 + 3 * i for i in range(299)]
    closes = pd.Series(baseline + [baseline[-1]] * 2)
    widths = pd.Series([1.0] * 299 + [2.0] * 2)
    history = pd.DataFrame({
        "open_time": 1700000000000 + pd.Series(range(301)) * 300000,
        "open": closes,
        "high": closes + widths / 2,
        "low": closes - widths / 2,
        "close": closes,
        "volume": 1000.0,
    })

    for end in (300, 301):
        df = history.iloc[end - 300 : end].reset_index(drop=True)
        findings = detect_volatility_compression("BTC", "5m", df)
        assert findings == [], f"expected no finding (unverifiable anchor) for window ending at {end}"

        # White-box confirmation this is genuinely "no bar anywhere
        # verifies as compressed", not a top-level-gate rejection.
        atr_s = compute_atr(df["high"], df["low"], df["close"], 14)
        ranges = df["high"] - df["low"]
        assert _episode_start_index(df, ranges, atr_s) is None


def _unverifiable_lookback_history(
    n_ramp: int = 10, ramp_step: float = 8.0, n_quiet: int = 55, quiet_step: float = 1.0,
    n_uptick: int = 2, uptick_step: float = 1.3, width: float = 2.0,
) -> pd.DataFrame:
    """A short, brief high-ATR "ramp" (n_ramp bars) followed by a long quiet
    stretch and a small uptick that trips expansion evidence. Every bar
    from COMPRESSION_LOOKBACK - 1 onward is verifiably COMPRESSED (each
    bar's own trailing window still straddles some of the ramp), and the
    bars before that lack a full trailing window entirely — UNKNOWN, not
    NOT_COMPRESSED. The compressed run therefore extends unbroken all the
    way to the edge of available history with no observed boundary.
    """
    steps = [ramp_step] * n_ramp + [quiet_step] * n_quiet + [uptick_step] * n_uptick
    closes = pd.Series(steps).cumsum() + 1000
    n = len(steps)
    return pd.DataFrame({
        "open_time": 1700000000000 + pd.Series(range(n)) * 300000,
        "open": closes,
        "high": closes + width / 2,
        "low": closes - width / 2,
        "close": closes,
        "volume": 1000.0,
    })


def test_volatility_compression_run_extending_into_unavailable_lookback_withholds():
    """Coverage-impact case #2: a compressed run that is verified all the
    way back to the earliest bar with a full lookback window, with no
    observed NOT_COMPRESSED bar anywhere before it — the true start could
    be older than anything in the currently-held candle history. Must
    withhold rather than report the earliest classifiable (or first-row)
    candle as if it were an observed boundary.
    """
    df = _unverifiable_lookback_history()
    assert len(df) >= VOL_COMPRESSION_MIN_CANDLES

    findings = detect_volatility_compression("BTC", "5m", df)
    assert findings == []

    # White-box confirmation of *which* unresolved case this is: every
    # classifiable bar (idx >= COMPRESSION_LOOKBACK - 1) is COMPRESSED, and
    # bars before that are UNKNOWN (insufficient lookback), not
    # NOT_COMPRESSED — i.e. the run never hit an observed break; it ran out
    # of history to check.
    atr_s = compute_atr(df["high"], df["low"], df["close"], 14)
    ranges = df["high"] - df["low"]
    short_avg_range_series = ranges.rolling(RANGE_CONTRACTION_LOOKBACK).mean()
    range_ratio = short_avg_range_series / ranges.rolling(COMPRESSION_LOOKBACK).mean()
    atr_percentile_series = atr_s.rolling(COMPRESSION_LOOKBACK).apply(
        lambda window: (window <= window[-1]).mean(), raw=True
    )
    n = len(df)
    for idx in range(COMPRESSION_LOOKBACK - 1, n):
        assert pd.notna(range_ratio.iloc[idx]) and pd.notna(atr_percentile_series.iloc[idx])
    for idx in range(0, COMPRESSION_LOOKBACK - 1):
        assert pd.isna(range_ratio.iloc[idx]) or pd.isna(atr_percentile_series.iloc[idx])

    assert _episode_start_index(df, ranges, atr_s) is None


def test_volatility_compression_later_observable_episode_still_detected_after_a_withheld_scan():
    """Coverage-impact case #3: withholding an unverifiable observation is
    scan-local and stateless. A later, fully-observable episode — on the
    same symbol/timeframe, evaluated as an entirely separate call, per
    detect_volatility_compression's own pure/stateless contract — must
    still be detected and anchored normally; nothing about a prior withheld
    scan permanently suppresses future detections.
    """
    withheld_df = _unverifiable_lookback_history()
    assert detect_volatility_compression("BTC", "5m", withheld_df) == []

    observable_df = _compression_then_breakout_df(0)
    findings = detect_volatility_compression("BTC", "5m", observable_df)
    assert len(findings) == 1
    assert findings[0].fingerprint_key is not None


# ------------------------------------------------------------------ SWEEP_RECLAIM

def _sweep_reclaim_long_df() -> pd.DataFrame:
    n = 30
    base = 100.0
    opens = [base] * n
    highs = [base + 0.5] * n
    lows = [base - 0.5] * n
    closes = [base] * n
    # Confirmed pivot LOW at index 15 (needs left_bars=3, right_bars=3 clearance).
    lows[15] = base - 3.0
    highs[15] = base - 2.0
    closes[15] = base - 2.5
    opens[15] = base - 2.2
    # Sweep at index 27 (within trailing 5-bar window: indices 25-29).
    lows[27] = base - 3.5
    highs[27] = base - 2.8
    closes[27] = base - 3.0
    opens[27] = base - 2.9
    # Reclaim at index 28.
    opens[28] = base - 3.0
    closes[28] = base - 2.5
    highs[28] = base - 2.4
    lows[28] = base - 3.1
    return _make_df(opens, highs, lows, closes)


def _sweep_no_reclaim_long_df() -> pd.DataFrame:
    """Same as _sweep_reclaim_long_df but the price never reclaims — false sweep."""
    df = _sweep_reclaim_long_df()
    df.loc[28, "close"] = 100.0 - 3.2  # stays below anchor (97.0) instead of reclaiming
    df.loc[28, "high"] = 100.0 - 3.0
    df.loc[29, "close"] = 100.0 - 3.1
    df.loc[29, "high"] = 100.0 - 3.0
    df.loc[29, "low"] = 100.0 - 3.3
    return df


def _sweep_reclaim_short_df() -> pd.DataFrame:
    n = 30
    base = 100.0
    opens = [base] * n
    highs = [base + 0.5] * n
    lows = [base - 0.5] * n
    closes = [base] * n
    # Confirmed pivot HIGH at index 15.
    highs[15] = base + 3.0
    lows[15] = base + 2.0
    closes[15] = base + 2.5
    opens[15] = base + 2.2
    # Sweep at index 27.
    highs[27] = base + 3.5
    lows[27] = base + 2.8
    closes[27] = base + 3.0
    opens[27] = base + 2.9
    # Reclaim (close back below anchor) at index 28.
    opens[28] = base + 3.0
    closes[28] = base + 2.5
    lows[28] = base + 2.4
    highs[28] = base + 3.1
    return _make_df(opens, highs, lows, closes)


def test_sweep_reclaim_long():
    df = _sweep_reclaim_long_df()
    findings = detect_sweep_reclaim("BTC", "5m", df)
    long_findings = [f for f in findings if f.direction == "LONG"]
    assert len(long_findings) == 1
    f = long_findings[0]
    assert f.setup_family == "SWEEP_RECLAIM"
    assert f.anchor_open_time == 15
    assert f.anchor_price == 97.0
    assert f.measurements["sweep_low"] == 96.5
    assert f.measurements["reclaim_close"] == 97.5


def test_sweep_reclaim_short():
    df = _sweep_reclaim_short_df()
    findings = detect_sweep_reclaim("ETH", "5m", df)
    short_findings = [f for f in findings if f.direction == "SHORT"]
    assert len(short_findings) == 1
    f = short_findings[0]
    assert f.setup_family == "SWEEP_RECLAIM"
    assert f.anchor_open_time == 15
    assert f.anchor_price == 103.0
    assert f.measurements["sweep_high"] == 103.5
    assert f.measurements["reclaim_close"] == 102.5


def test_sweep_reclaim_false_sweep_no_reclaim_returns_nothing():
    df = _sweep_no_reclaim_long_df()
    findings = detect_sweep_reclaim("BTC", "5m", df)
    assert findings == []


def test_sweep_reclaim_no_anchor_pivot_returns_nothing():
    n = 30
    df = _make_df([100.0] * n, [100.5] * n, [99.5] * n, [100.0] * n)
    assert detect_sweep_reclaim("BTC", "5m", df) == []


def test_sweep_reclaim_insufficient_data_returns_nothing():
    df = _make_df([100.0] * 5, [100.5] * 5, [99.5] * 5, [100.0] * 5)
    assert detect_sweep_reclaim("BTC", "5m", df) == []


def test_sweep_reclaim_3m_vs_5m_identity():
    """Identical price geometry on different timeframe labels must stay distinguishable."""
    df = _sweep_reclaim_long_df()
    findings_3m = detect_sweep_reclaim("BTC", "3m", df)
    findings_5m = detect_sweep_reclaim("BTC", "5m", df)
    assert findings_3m[0].primary_timeframe == "3m"
    assert findings_5m[0].primary_timeframe == "5m"


def test_sweep_reclaim_anchor_identity_stable_across_subsequent_bar():
    """Correction 4C: the anchor pivot's own open_time is a fixed historical
    fact, so a subsequent closed bar continuing at/above the reclaimed level
    must not change this finding's identity (it should re-confirm the same
    row, not create a new one).
    """
    df_bar_n = _sweep_reclaim_long_df()
    finding_bar_n = [
        f for f in detect_sweep_reclaim("BTC", "5m", df_bar_n) if f.direction == "LONG"
    ][0]

    df_bar_n1 = pd.concat(
        [df_bar_n, _make_df([97.5], [97.7], [97.3], [97.5])], ignore_index=True
    )
    df_bar_n1["open_time"] = range(len(df_bar_n1))
    finding_bar_n1 = [
        f for f in detect_sweep_reclaim("BTC", "5m", df_bar_n1) if f.direction == "LONG"
    ][0]

    assert finding_bar_n.fingerprint_key == finding_bar_n1.fingerprint_key
    assert compute_fingerprint(finding_bar_n) == compute_fingerprint(finding_bar_n1)
