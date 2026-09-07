"""VOLATILITY_COMPRESSION detector — pure, research-only.

Identifies a measurable range/ATR compression condition on the current bar
plus evidence that expansion is starting, i.e. a "coiled spring" transition.
This does not score conviction or imply direction beyond a simple descriptive
tag (see `direction` below) — that is later-milestone work.

Measurements are normalized rather than a single hardcoded ATR value so the
same detector is comparable across symbols/timeframes of very different
price scale:

  atr_percentile     — fraction of the last `compression_lookback` closed
                        bars whose ATR is <= the current bar's ATR. Low means
                        current volatility is unusually quiet for this symbol
                        recently (a percentile rank, not a z-score, so it
                        needs no distribution assumptions).
  contraction_ratio   — short-window average true range (last
                        `range_contraction_lookback` bars) divided by the
                        longer `compression_lookback`-bar average range. A
                        ratio well below 1 means recent ranges have
                        contracted relative to the recent baseline.

Compression is flagged if EITHER measure crosses its documented threshold
(an OR, not an AND, because a purely ATR-based measure and a purely raw-range
measure can each miss compression the other one catches, e.g. one candle
with a large wick skews ATR but not the OHLC body range). Both thresholds
and the expansion trigger below are named constants, chosen as a
first-pass, documented starting point for prospective research — not tuned
against outcome data (no outcome data exists yet for this detector).
"""
from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from apex.indicators.atr import atr
from apex.opportunity.contract import PRIMARY_TIMEFRAME_DURATION_MS, DetectorFinding, PrimaryTimeframe
from apex.strategy.trend_filter import compute_trend_bias

DETECTOR_VERSION = "vol_compression_v0_1_1"

ATR_PERIOD = 14
COMPRESSION_LOOKBACK = 50  # bars used to build the ATR/range baseline distribution
COMPRESSION_ATR_PERCENTILE = 0.25  # current ATR must rank at/below this percentile of the lookback
RANGE_CONTRACTION_LOOKBACK = 6  # short window used for the range-contraction ratio
RANGE_CONTRACTION_RATIO = 0.65  # short-window avg range / long-window avg range must be <= this
EXPANSION_RANGE_MULTIPLE = 1.5  # latest bar's range vs the short-window avg range, to flag expansion
MIN_CANDLES_REQUIRED = COMPRESSION_LOOKBACK + 1


def detect_volatility_compression(
    symbol: str,
    timeframe: PrimaryTimeframe,
    df: pd.DataFrame,
    *,
    context_df_15m: Optional[pd.DataFrame] = None,
) -> list[DetectorFinding]:
    """Evaluate the most recent closed bar of `df` for compression->expansion.

    `df` must have columns open/high/low/close/volume/open_time, oldest
    first, closed candles only (the shape CandleStore.get_df returns).
    Returns 0 or 1 findings — this detector only ever reports the *current*
    moment, not a historical replay.
    """
    if df is None or len(df) < MIN_CANDLES_REQUIRED:
        return []

    atr_s = atr(df["high"], df["low"], df["close"], ATR_PERIOD)
    atr_now = float(atr_s.iloc[-1])
    atr_prev = float(atr_s.iloc[-2])
    if math.isnan(atr_now) or math.isnan(atr_prev):
        return []

    recent_atr_window = atr_s.tail(COMPRESSION_LOOKBACK)
    atr_percentile = float((recent_atr_window <= atr_now).mean())

    ranges = df["high"] - df["low"]
    short_avg_range = float(ranges.tail(RANGE_CONTRACTION_LOOKBACK).mean())
    long_avg_range = float(ranges.tail(COMPRESSION_LOOKBACK).mean())
    if long_avg_range <= 0 or short_avg_range <= 0:
        return []
    contraction_ratio = short_avg_range / long_avg_range

    is_compressed = (
        atr_percentile <= COMPRESSION_ATR_PERCENTILE or contraction_ratio <= RANGE_CONTRACTION_RATIO
    )
    if not is_compressed:
        return []

    latest_range = float(ranges.iloc[-1])
    expansion_ratio = latest_range / short_avg_range
    atr_rising = atr_now > atr_prev
    expansion_evidence = expansion_ratio >= EXPANSION_RANGE_MULTIPLE or atr_rising
    if not expansion_evidence:
        # Still compressed but nothing yet suggests a transition — v0.1 only
        # reports the developing-expansion moment, not the quiet state alone.
        return []

    episode_start_idx = _episode_start_index(df, ranges, atr_s)
    if episode_start_idx is None:
        # No defensible episode boundary exists in the available history —
        # withhold rather than fabricate an identity. See
        # _episode_start_index's docstring ("Coverage impact").
        return []

    last = df.iloc[-1]
    episode_start_open_time = int(df["open_time"].iloc[episode_start_idx])

    # Descriptive tag only (which way the developing expansion currently
    # leans) — not a forecast, not coupled to the 15m/5m trend filter, and
    # carries no scoring weight. Anchored to net movement since the episode's
    # compression began (episode-start close vs current close), not the
    # triggering bar's own body: a single candle's open/close color is noisy
    # and can flip from one bar to the next within the *same* continuing
    # expansion. See Correction 3.
    #
    # Correction 5 (direction-in-identity review) — RESOLVED in
    # vol_compression_v0_1_1: compression itself is direction-neutral — it
    # is a statement about range/ATR contraction, not about which way price
    # will eventually break. `direction` here is evidence/metadata about the
    # developing expansion, not a defining property of the compression
    # episode. engine.compute_fingerprint now treats VOLATILITY_COMPRESSION
    # as direction-invariant (contract.DIRECTION_INVARIANT_FAMILIES),
    # substituting a fixed token for the direction component of the
    # fingerprint hash, so a LONG finding and a later SHORT finding for the
    # *same* episode (same fingerprint_key) share one fingerprint and one
    # ACTIVE opportunity_uid instead of the direction flip fragmenting the
    # episode's identity. SWEEP_RECLAIM is deliberately excluded from that
    # set and keeps direction as part of its identity unchanged, since a
    # LONG sweep off a pivot low and a SHORT sweep off a pivot high are
    # genuinely different structural events.
    #
    # This is a shared-contract change (engine.py, contract.py), which is
    # why the detector version advances here: rows recorded under the prior
    # DETECTOR_VERSION "vol_compression_v0_1" predate this immutable-
    # identity/snapshot convention and must not be read as if they had it
    # (see engine.py's "Compression's immutable first-detection snapshot"
    # docstring and scripts/report_opportunities.py).
    episode_start_close = float(df["close"].iloc[episode_start_idx])
    direction = "LONG" if float(last["close"]) >= episode_start_close else "SHORT"

    evidence: dict = {
        "atr_period": ATR_PERIOD,
        "compression_lookback": COMPRESSION_LOOKBACK,
        "compression_atr_percentile_threshold": COMPRESSION_ATR_PERCENTILE,
        "range_contraction_lookback": RANGE_CONTRACTION_LOOKBACK,
        "range_contraction_ratio_threshold": RANGE_CONTRACTION_RATIO,
        "expansion_range_multiple_threshold": EXPANSION_RANGE_MULTIPLE,
    }
    warnings: list[str] = []
    if context_df_15m is not None and not context_df_15m.empty:
        trend_15m = compute_trend_bias(context_df_15m).bias
        evidence["context_15m_trend_bias"] = trend_15m
        if trend_15m != "NONE" and trend_15m != direction:
            warnings.append(
                f"15m trend bias ({trend_15m}) conflicts with developing-expansion tag ({direction})"
            )

    measurements = {
        "atr_now": atr_now,
        "atr_prev": atr_prev,
        "atr_percentile": atr_percentile,
        "contraction_ratio": contraction_ratio,
        "short_avg_range": short_avg_range,
        "long_avg_range": long_avg_range,
        "latest_range": latest_range,
        "expansion_ratio": expansion_ratio,
        "atr_rising": atr_rising,
    }

    finding = DetectorFinding(
        symbol=symbol,
        direction=direction,
        setup_family="VOLATILITY_COMPRESSION",
        detector_version=DETECTOR_VERSION,
        primary_timeframe=timeframe,
        source_candle_open_time=int(last["open_time"]),
        # CandleStore.get_df() does not expose the candle's real close_time
        # to detectors (see candle_store.py), so this is derived rather than
        # duplicating open_time under a misleading name (Correction 5).
        source_candle_close_time=int(last["open_time"]) + PRIMARY_TIMEFRAME_DURATION_MS[timeframe],
        # Fingerprint key: the open_time of the earliest bar (walking back
        # from now) in the unbroken tight-range streak this bar belongs to.
        # A break in contraction resets the streak, so a later, unrelated
        # compression on the same symbol/timeframe gets a new identity
        # instead of colliding with this one (see engine.py) — direction is
        # deliberately NOT part of this identity for this family (Correction
        # 5, resolved), so a same-episode direction flip still collapses
        # into the same fingerprint.
        fingerprint_key=str(episode_start_open_time),
        anchor_price=float(last["close"]),
        anchor_open_time=int(last["open_time"]),
        evidence=evidence,
        warnings=warnings,
        measurements=measurements,
    )
    return [finding]


def _episode_start_index(df: pd.DataFrame, ranges: pd.Series, atr_s: pd.Series) -> Optional[int]:
    """Locate the verified structural start of the current compression
    episode, or None if no defensible boundary exists in the available
    history.

    Returns a *positional* index into `df` (never persisted itself — only
    used as this call's lookup key to read off the real `open_time`/`close`
    at that position, which is what becomes the durable identity), or
    `None` when the walk cannot establish a boundary it can stand behind.
    Callers must treat `None` as "withhold this finding" rather than
    substitute any index — see `detect_volatility_compression`.

    Each bar is classified into one of three states, computed from a
    trailing COMPRESSION_LOOKBACK-bar window ending AT that bar (not at
    "now" — a "now"-anchored reference shifts every new bar, which would
    reclassify old, settled bars on every scan):

      - COMPRESSED   — `range_ratio` (short/long average-range ratio,
                        matching the top-level `contraction_ratio`) or
                        `atr_percentile` (the literal percentile-rank
                        formula the top-level trigger uses, not an
                        approximation of it) crosses its threshold, AND the
                        bar's own raw range is not itself an obvious
                        expansion/breakout candle (>= EXPANSION_RANGE_
                        MULTIPLE times the *preceding* RANGE_CONTRACTION_
                        LOOKBACK bars' average range — the same expansion
                        test the top-level trigger uses, evaluated locally
                        so it does not drift as "now" advances). This veto
                        stops a smoothed ATR reading from bridging across a
                        real breakout candle, which would otherwise wrongly
                        merge two separate episodes or let one lone quiet
                        bar re-anchor an ongoing expansion — see the LOW
                        limitation below for where it does not fully help.
      - NOT_COMPRESSED — full lookback is available and neither condition
                        above holds (or the expansion veto fired): a
                        directly observed, reliable boundary.
      - UNKNOWN        — fewer than COMPRESSION_LOOKBACK bars precede this
                        position in `df`, so `range_ratio`/`atr_percentile`
                        cannot be computed at all (pandas `.rolling(...)`
                        returns NaN, never silently substituted with a
                        default). Position 0 is always UNKNOWN. This is a
                        third state, never collapsed into NOT_COMPRESSED.

    Two-phase backward walk from the latest bar:

    1. Skip trailing NOT_COMPRESSED bars to reach the most recent bar this
       function can verify as COMPRESSED. If UNKNOWN is reached before any
       COMPRESSED bar is found, nothing in the available history can be
       verified as part of a compressed regime — return None.
    2. Extend back through the unbroken run of COMPRESSED bars. Stop at the
       first NOT_COMPRESSED bar and return its successor: a directly
       observed boundary. If UNKNOWN is reached instead — the compressed
       run extends to the edge of currently-held history with no observed
       break — also return None: the true start could be older than
       anything CandleStore currently holds, and reporting the oldest
       visible bar as "the start" would both overclaim precision we do not
       have and drift on every scan as the buffer's front edge evicts
       candles (a new "oldest bar" every time), reproducing the exact
       instability this function exists to prevent. Never substitute
       position 0, or the first position with a full lookback window, for
       an observed boundary.

    Coverage impact: a finding is withheld whenever neither phase finds a
    directly observed boundary — a newly developing signal with no
    verifiable quiet bar behind it (see the expansion-veto case in
    `detect_volatility_compression`'s docstring for a real example), and a
    long-lived compression whose start predates the visible 300-candle
    buffer. Both are real detector output lost in exchange for never
    reporting a fabricated or unstable identity. A later, independently
    observable episode on the same symbol/timeframe is unaffected and
    reports normally once it has its own verified boundary.

    Known limitation (LOW, not addressed this pass): the expansion veto
    only inspects a bar's own range, not its ATR, so a single genuinely
    quiet bar sandwiched inside an established expansion run can still
    independently qualify as COMPRESSED via `atr_percentile` and become
    phase 1's stopping point, re-anchoring identity onto itself instead of
    the episode's true start ("expansion -> one small bar -> expansion
    resumes"). Also vetoing on ATR would risk suppressing genuine
    compression detections, which this function does not do.

    ATR's own numerical footing: ATR is an EWM recursion seeded at the
    first row of whatever series it is computed over, so trimming the
    buffer can shift a given bar's ATR value by a small amount depending on
    where the recursion now starts. This is a continuous, decaying effect
    with no fixed cutoff — it does not vanish after some fixed number of
    bars — and it can occasionally flip a genuinely borderline
    `atr_percentile` classification near the buffer's older edge. It is
    unrelated to, and not fixed by, the window-alignment behavior above.
    """
    n = len(df)

    short_avg_range_series = ranges.rolling(RANGE_CONTRACTION_LOOKBACK).mean()
    # The bar's own trailing window is excluded from its veto reference
    # (shift(1)) so one huge bar can't dilute its own average enough to
    # dodge the veto.
    local_expansion_ref = short_avg_range_series.shift(1)
    range_ratio = short_avg_range_series / ranges.rolling(COMPRESSION_LOOKBACK).mean()
    atr_percentile_series = atr_s.rolling(COMPRESSION_LOOKBACK).apply(
        lambda window: (window <= window[-1]).mean(), raw=True
    )

    def bar_state(idx: int) -> Optional[bool]:
        """True = COMPRESSED, False = NOT_COMPRESSED, None = UNKNOWN."""
        range_ratio_value = range_ratio.iloc[idx]
        atr_percentile_value = atr_percentile_series.iloc[idx]
        if pd.isna(range_ratio_value) or pd.isna(atr_percentile_value):
            return None
        ref = local_expansion_ref.iloc[idx]
        if pd.notna(ref) and float(ranges.iloc[idx]) >= EXPANSION_RANGE_MULTIPLE * float(ref):
            return False
        return bool(
            range_ratio_value <= RANGE_CONTRACTION_RATIO
            or atr_percentile_value <= COMPRESSION_ATR_PERCENTILE
        )

    # Phase 1: find the most recent verified-COMPRESSED bar.
    idx = n - 1
    while True:
        state = bar_state(idx)
        if state is True:
            break
        if state is None or idx == 0:
            return None
        idx -= 1

    # Phase 2: extend back through the unbroken COMPRESSED run. `idx` here
    # is always >= COMPRESSION_LOOKBACK - 1 (phase 1 can only break with a
    # full-lookback bar), so `idx - 1` reaching 0 always resolves to
    # UNKNOWN before it could resolve to True — the loop always terminates
    # via one of the explicit returns below, never by falling off the end.
    while True:
        prior_state = bar_state(idx - 1)
        if prior_state is True:
            idx -= 1
            continue
        if prior_state is False:
            return idx
        return None
