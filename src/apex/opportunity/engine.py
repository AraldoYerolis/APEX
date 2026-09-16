"""TA Opportunity Engine v0.1 orchestration — additive, research-only.

Runs the VOLATILITY_COMPRESSION, SWEEP_RECLAIM, SUPPORT_RESISTANCE_REJECTION,
SUPPORT_RESISTANCE_BREAKOUT_RETEST, and SUPPORT_RESISTANCE_FAILED_BREAKOUT
detectors, in that fixed order, over 3m/5m candles already held in the
shared CandleStore (no new REST/WS calls, no new per-symbol DB queries — see
CLAUDE.md milestone spec J), using 15m purely as recorded context for the
first two detectors, never a hard gate. The three Support/Resistance
detectors do not take a 15m context argument at all — they evaluate `df`
alone, at their own default parameters.

Detector error containment
---------------------------
Each of the five detector calls is isolated individually: an exception
raised by one detector for one symbol/timeframe increments
`OpportunityScanSummary.detector_errors`, is logged once via
`logger.error(..., exc_info=True)` naming the detector, symbol, and
timeframe, and does not prevent the remaining detectors for that
symbol/timeframe (or any other symbol/timeframe) from running. This sits
*inside* the existing outer per-symbol try/except below, which remains the
backstop for context/data/staleness/persistence failures (e.g. a bad 15m
context fetch, a `_record_finding` failure) — it is not broadened to also
swallow detector exceptions, since those are now handled at their own,
finer granularity instead.

This module is the only place that writes to opportunity_observations. It
never touches signal_observations, signal_features, alerts, paper_trades,
daily_risk, or snoozes, and never calls PushoverClient or any notification
path — it has no import of apex.notifications at all.

Fingerprint / dedupe semantics
-------------------------------
A finding's fingerprint is `sha1(contract_version | setup_family |
detector_version | symbol | primary_timeframe | direction_component |
fingerprint_key)`. `fingerprint_key` is family-specific and chosen by the
detector (see detectors/*) to identify "the same underlying structural
event" rather than "the same symbol+family+direction forever" (which would
collapse independent future setups into one row):

  SWEEP_RECLAIM         — the anchor pivot's open_time. As long as
                          re-detections reference the same prior structural
                          level, they collapse into one row; a different
                          (newer) pivot becomes a different fingerprint.
                          `direction_component` is the finding's real LONG/
                          SHORT direction here — direction is genuinely
                          structural for this family (a LONG sweep off a
                          pivot low and a SHORT sweep off a pivot high are
                          different structural events even when other fields
                          coincide), so it stays part of identity unchanged.
  VOLATILITY_COMPRESSION — the open_time of the start of the current
                          contraction episode (walking back from the
                          triggering bar to the earliest unbroken tight-range
                          bar). A break in contraction resets the episode, so
                          a later, unrelated compression gets a new identity.
                          `direction_component` is the fixed
                          DIRECTION_INVARIANT_FINGERPRINT_TOKEN (contract.py)
                          for this family, never the finding's actual
                          direction: compression is a statement about range/
                          ATR contraction, not about which way price will
                          break, so direction is descriptive evidence, not
                          identity (see detectors/volatility_compression.py).
                          A LONG finding and a later SHORT finding for the
                          *same* episode therefore share one fingerprint and
                          one ACTIVE opportunity_uid.

On each scan, a finding whose fingerprint matches an existing status='ACTIVE'
row is a re-confirmation: last_seen_at and occurrence_count are updated in
place (no new row). A finding with no matching ACTIVE row is a new
opportunity, even if a fingerprint collision exists among EXPIRED rows —
fingerprint is intentionally NOT a DB-level unique constraint (only
opportunity_uid is), because in principle recurrence after a gap long
enough to have expired is a fresh occurrence worth its own row, not a
resurrection of the old one.

Compression's immutable first-detection snapshot
--------------------------------------------------
For VOLATILITY_COMPRESSION, a re-confirmation must never overwrite the
row's first-detection snapshot: direction, source_candle_open_time/
source_candle_close_time, anchor_price/anchor_open_time, evidence_json, and
warnings_json. This was already true for every family before this
correction — touch_opportunity()'s UPDATE (see repository.py) has only ever
set last_seen_at, occurrence_count, measurements_json, and updated_at, so
those columns were already immutable post-insert by construction, not
newly frozen here. measurements_json is the one field touch_opportunity()
*does* overwrite on every call, and it was NOT previously immutable for
compression — this correction is what freezes it there: for
VOLATILITY_COMPRESSION, _record_finding now passes back the
*already-stored* measurements_json instead of the incoming finding's, so
the full snapshot (not just the five already-immutable columns) stays
frozen at its first-detection value. SWEEP_RECLAIM is unchanged and keeps
refreshing measurements_json with each incoming finding.

A practical consequence: a stored VOLATILITY_COMPRESSION row's `direction`
column reflects the direction observed at first detection, not necessarily
the latest directional assessment of the episode — it must never be
presented as a live re-assessment. Only the compression detector version
that introduced this snapshot behavior (vol_compression_v0_1_1 — see
DETECTOR_VERSION in detectors/volatility_compression.py) is affected; rows
recorded under the prior vol_compression_v0_1 detector_version predate this
convention and do not carry the same guarantee.

Lifecycle: ACTIVE rows not re-confirmed within OPPORTUNITY_EXPIRY_MINUTES
(per timeframe) are marked EXPIRED at the start of each scan. v0.1 has no
"CLOSED" status — there is no target/stop outcome model for opportunities
yet (out of scope, see CLAUDE.md milestone spec M), so ACTIVE/EXPIRED is
the whole lifecycle.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, get_args

from apex.config import Settings
from apex.data.candle_store import CandleStore
from apex.db import repository as repo
from apex.opportunity.context import (
    StructureComponent,
    TrendComponent,
    VolatilityComponent,
    assemble_context,
    compute_primary_structure_component,
    compute_symbol_trend_component,
    compute_volatility_component,
    context_to_dict,
    not_applicable_trend_component,
)
from apex.opportunity.contract import (
    CONTRACT_VERSION,
    DIRECTION_INVARIANT_FAMILIES,
    DIRECTION_INVARIANT_FINGERPRINT_TOKEN,
    DetectorFinding,
    Opportunity,
    SetupFamily,
)
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
from apex.opportunity.scoring import SCORE_VERSION, component_scores_to_dict, score_opportunity
from apex.opportunity.trade_plan import build_trade_plan
from apex.opportunity.trade_plan_outcome import build_initial_trade_plan_outcome
from apex.utils.ids import new_uid
from apex.utils.time import minutes_ago_iso

logger = logging.getLogger(__name__)

OPPORTUNITY_TIMEFRAMES: tuple[str, ...] = ("3m", "5m")

# Reference symbol for the BTC 15m trend context component (see context.py's
# btc_htf_trend / scoring.py's btc_alignment). Matches the literal "BTC"
# already used for macro context elsewhere (scheduler/tasks.py) rather than
# introducing a new settings knob for this milestone.
BTC_SYMBOL = "BTC"

# Context and ranking v0.1 — a finding's setup_family is only ever a
# detector-assigned literal today, but _build_score_snapshot below still
# validates it against the sealed contract before scoring (typing.get_args,
# never a hand-maintained duplicate list), so an unsupported/malformed
# family degrades to an explicit unscored warning instead of silently
# scoring under assumptions that don't hold for it. This never widens what
# is persisted to opportunity_observations.setup_family itself (schema.sql's
# own CHECK constraint is untouched).
SUPPORTED_SETUP_FAMILIES = frozenset(get_args(SetupFamily))

# Enough history for all five detectors' lookbacks (VOLATILITY_COMPRESSION's
# baseline window remains the largest of the five requirements) plus
# headroom.
MIN_CANDLES_REQUIRED = 60

# A timeframe's ACTIVE opportunity expires if not re-confirmed within this
# many minutes — roughly 5 missed re-confirmation scans at each timeframe's
# own candle cadence.
OPPORTUNITY_EXPIRY_MINUTES: dict[str, int] = {
    "3m": 15,
    "5m": 25,
}


@dataclass
class OpportunityScanSummary:
    markets_scanned: int = 0
    timeframe_pairs_scanned: int = 0
    no_data: int = 0
    stale: int = 0
    findings: int = 0
    new_opportunities: int = 0
    re_confirmed: int = 0
    expired: int = 0
    detector_errors: int = 0
    # Context and ranking v0.1 — optional-enrichment failure counters,
    # entirely separate from detector_errors. Neither ever skips
    # _record_finding or any other finding/market: an enrichment failure
    # only ever degrades that one opportunity's context/score to NULL (see
    # _record_finding / _build_score_snapshot below). context_errors covers
    # BOTH a failing candle fetch (BTC or symbol 15m get_df) AND a failing
    # context *computation* (compute_symbol_trend_component,
    # compute_primary_structure_component, compute_volatility_component) —
    # each of the four call sites is caught individually and substitutes a
    # directly-constructed typed UNAVAILABLE fallback (never a second call
    # into the same failing computation) so the remaining healthy
    # components, later timeframes, and later markets are unaffected.
    context_errors: int = 0
    score_errors: int = 0
    # Trade plans and outcome evidence v0.1 — optional-enrichment counters,
    # entirely separate from detector_errors/context_errors/score_errors.
    # A plan-creation failure is contained (see engine._maybe_create_trade_plan)
    # and never suppresses the opportunity row or any other market/timeframe.
    trade_plans_created: int = 0
    trade_plan_errors: int = 0

    def log(self, logger: logging.Logger) -> None:
        logger.info(
            "[OPPORTUNITY ENGINE] "
            f"{self.markets_scanned} markets, {self.timeframe_pairs_scanned} symbol/timeframe pairs, "
            f"{self.findings} findings ({self.new_opportunities} new, {self.re_confirmed} re-confirmed), "
            f"{self.expired} expired, {self.no_data} no data, {self.stale} stale, "
            f"{self.detector_errors} detector errors, "
            f"{self.context_errors} context fetch/computation errors, "
            f"{self.score_errors} scoring errors, "
            f"{self.trade_plans_created} trade plans created, "
            f"{self.trade_plan_errors} trade plan errors"
        )


@dataclass
class ContextInputs:
    """Shared, already-computed context components for one (symbol,
    timeframe) pair's findings — built at most once per (symbol, timeframe)
    per scan (see run_opportunity_scan) and reused across every finding
    that timeframe produces. Only `_record_finding`'s NEW-row path ever
    reads this; a re-confirmation (touch) never does (see the module
    docstring's immutable first-detection snapshot policy).
    """

    as_of_ms: int
    primary_timeframe: str
    symbol_htf_trend: TrendComponent
    primary_structure: StructureComponent
    btc_htf_trend: TrendComponent
    volatility: VolatilityComponent


def _ms_to_iso(ms: int) -> str:
    """ISO UTC string (utcnow_iso's whole-second format) for a millisecond
    epoch value. `run_opportunity_scan` always passes an already
    whole-second-floored `now_ms` (see below), so this is an exact
    round-trip of that instant, not a truncation of a more precise one —
    first_detected_at/last_seen_at and every candle-eligibility now_ms for
    the scan are therefore derived from the same single clock read AND the
    same whole-second value, rather than an independent, later
    time.time()/datetime.now() call.
    """
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_detect(
    detector_name: str,
    symbol: str,
    timeframe: str,
    summary: OpportunityScanSummary,
    call: Callable[[], list[DetectorFinding]],
) -> list[DetectorFinding]:
    """Run one detector call in isolation.

    An exception here must never abort the remaining detectors for this
    symbol/timeframe (nor the outer per-symbol backstop it runs inside, see
    run_opportunity_scan) — it is counted and logged at detector/symbol/
    timeframe granularity instead, and treated as "no findings" for this one
    detector call.
    """
    try:
        return call()
    except Exception:
        summary.detector_errors += 1
        logger.error(
            f"Opportunity detector error: detector={detector_name} symbol={symbol} "
            f"timeframe={timeframe}",
            exc_info=True,
        )
        return []


def compute_fingerprint(finding: DetectorFinding) -> str:
    """Family-specific identity: direction is part of identity for every
    family except DIRECTION_INVARIANT_FAMILIES (contract.py), where it is
    replaced with a fixed token so it never affects the hash. SWEEP_RECLAIM
    is not in that set, so its formula (field order, separator, and use of
    the finding's real direction) is unchanged from before this function
    became family-aware.
    """
    direction_component = (
        DIRECTION_INVARIANT_FINGERPRINT_TOKEN
        if finding.setup_family in DIRECTION_INVARIANT_FAMILIES
        else finding.direction
    )
    raw = "|".join(
        [
            CONTRACT_VERSION,
            finding.setup_family,
            finding.detector_version,
            finding.symbol,
            finding.primary_timeframe,
            direction_component,
            finding.fingerprint_key,
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


async def run_opportunity_scan(
    conn: sqlite3.Connection,
    candle_store: CandleStore,
    settings: Settings,
) -> OpportunityScanSummary:
    """Additive, research-only scan across all scan-enabled markets.

    Defensive guard mirrors run_observation_evaluation in scheduler/tasks.py:
    main.py only registers this job when opportunity_engine_enabled AND
    dry_run_mode are both true, and this function re-checks both so it stays
    safe even if called directly outside that scheduler wiring.
    """
    summary = OpportunityScanSummary()
    if not settings.opportunity_engine_enabled or not settings.dry_run_mode:
        return summary

    markets = repo.get_scan_enabled_markets(conn)
    if not markets:
        return summary
    summary.markets_scanned = len(markets)

    # Read the wall clock exactly once for the whole scan, then derive both
    # the ISO first_detected_at/last_seen_at stamp and every candle-
    # eligibility now_ms from that single instant (never a second,
    # independent clock read) — so every candle-eligibility check for this
    # entire scan (see candle_store.get_df's now_ms) is judged against the
    # same instant as first_detected_at/last_seen_at below. Without this, a
    # bar that only crosses its own close boundary partway through a
    # multi-symbol scan (get_df is called once per symbol, and a scan can
    # take several seconds) could be exposed as closed with a close time
    # later than `now`, reproducing the exact violation this fix targets.
    #
    # Deliberately floored to the whole second (never a finer-grained
    # millisecond cutoff): first_detected_at/last_seen_at are persisted as
    # whole-second ISO strings, so a sub-second-precise cutoff could still
    # expose a candle whose close boundary falls later within that same
    # second than the second-only timestamp that gets recorded for it. Using
    # the floored value as the candle-eligibility cutoff too (not just for
    # the ISO stamp) means the recorded timestamp is never later than the
    # boundary actually used to judge eligibility — a conservative choice
    # that can only delay a bar's first appearance by up to ~1s, never
    # expose one early.
    now_ms = int(time.time()) * 1000
    now = _ms_to_iso(now_ms)
    for timeframe, expiry_minutes in OPPORTUNITY_EXPIRY_MINUTES.items():
        cutoff = minutes_ago_iso(expiry_minutes)
        summary.expired += repo.expire_stale_opportunities(
            conn, primary_timeframe=timeframe, cutoff_iso=cutoff
        )

    # BTC 15m context is fetched at most once per scan, using the same
    # now_ms as every other read this scan — never per-symbol, never a
    # second independent clock/read. A missing or failing read degrades
    # gracefully (btc_df_15m stays None -> every symbol's btc_htf_trend
    # component becomes UNAVAILABLE, never an aborted scan).
    try:
        btc_df_15m = candle_store.get_df(BTC_SYMBOL, "15m", now_ms=now_ms)
    except Exception:
        summary.context_errors += 1
        logger.error("Opportunity BTC 15m context fetch error", exc_info=True)
        btc_df_15m = None
    try:
        btc_trend_component = compute_symbol_trend_component(btc_df_15m, now_ms)
    except Exception:
        # Caught separately from the fetch above: a failure HERE means the
        # frame was fetched fine but the pure computation itself raised. A
        # typed UNAVAILABLE fallback is constructed directly — this must
        # never call compute_symbol_trend_component(btc_df_15m, now_ms)
        # again to "recover", since that is the exact call that just failed.
        summary.context_errors += 1
        logger.error("Opportunity BTC 15m trend computation error", exc_info=True)
        btc_trend_component = TrendComponent(
            status="UNAVAILABLE",
            direction=None,
            reason="COMPUTATION_FAILED",
            sample_count=0,
            latest_close_boundary_ms=None,
        )

    for market in markets:
        symbol = market.symbol
        try:
            # The symbol's own 15m context fetch and its own trend
            # computation are each isolated in their own try/except (not the
            # outer per-symbol one below), so either one failing degrades
            # gracefully instead of aborting this symbol's otherwise-valid
            # 3m/5m primary detection entirely.
            #
            # Computed once per symbol (not per timeframe): symbol_htf_trend
            # always reads the same 15m frame regardless of 3m vs 5m, and
            # symbol_btc_htf_trend is either the shared once-per-scan BTC
            # component or the fixed NOT_APPLICABLE constant for BTC itself.
            #
            # When symbol IS the BTC reference symbol, its own 15m trend is
            # IDENTICAL to btc_trend_component (same frame, same now_ms) —
            # reuse that already-computed result directly rather than
            # calling compute_symbol_trend_component a second time on the
            # same inputs (BTC trend is computed at most once per scan).
            if symbol == BTC_SYMBOL:
                context_df_15m = btc_df_15m
                symbol_htf_trend = btc_trend_component
            else:
                try:
                    context_df_15m = candle_store.get_df(symbol, "15m", now_ms=now_ms)
                except Exception:
                    summary.context_errors += 1
                    logger.error(
                        f"Opportunity 15m context fetch error for {symbol}", exc_info=True
                    )
                    context_df_15m = None
                try:
                    symbol_htf_trend = compute_symbol_trend_component(context_df_15m, now_ms)
                except Exception:
                    # Caught separately from the fetch above (own try/except,
                    # not the outer per-symbol backstop): a failure here must
                    # degrade only this symbol's own trend to UNAVAILABLE, not
                    # abort this symbol's otherwise-valid 3m/5m primary
                    # detection below. A typed UNAVAILABLE fallback is
                    # constructed directly — never a second call into the
                    # same failing computation.
                    summary.context_errors += 1
                    logger.error(
                        f"Opportunity 15m trend computation error for {symbol}", exc_info=True
                    )
                    symbol_htf_trend = TrendComponent(
                        status="UNAVAILABLE",
                        direction=None,
                        reason="COMPUTATION_FAILED",
                        sample_count=0,
                        latest_close_boundary_ms=None,
                    )
            symbol_btc_htf_trend = (
                not_applicable_trend_component() if symbol == BTC_SYMBOL else btc_trend_component
            )

            for timeframe in OPPORTUNITY_TIMEFRAMES:
                df = candle_store.get_df(symbol, timeframe, now_ms=now_ms)
                if df is None or len(df) < MIN_CANDLES_REQUIRED:
                    summary.no_data += 1
                    continue
                if candle_store.is_stale(symbol, timeframe):
                    summary.stale += 1
                    continue

                summary.timeframe_pairs_scanned += 1
                findings: list[DetectorFinding] = []
                findings.extend(
                    _safe_detect(
                        "volatility_compression",
                        symbol,
                        timeframe,
                        summary,
                        lambda: detect_volatility_compression(
                            symbol, timeframe, df, context_df_15m=context_df_15m
                        ),
                    )
                )
                findings.extend(
                    _safe_detect(
                        "sweep_reclaim",
                        symbol,
                        timeframe,
                        summary,
                        lambda: detect_sweep_reclaim(
                            symbol, timeframe, df, context_df_15m=context_df_15m
                        ),
                    )
                )
                findings.extend(
                    _safe_detect(
                        "support_resistance_rejection",
                        symbol,
                        timeframe,
                        summary,
                        lambda: detect_support_resistance_rejection(symbol, timeframe, df),
                    )
                )
                findings.extend(
                    _safe_detect(
                        "support_resistance_breakout_retest",
                        symbol,
                        timeframe,
                        summary,
                        lambda: detect_support_resistance_breakout_retest(symbol, timeframe, df),
                    )
                )
                findings.extend(
                    _safe_detect(
                        "support_resistance_failed_breakout",
                        symbol,
                        timeframe,
                        summary,
                        lambda: detect_support_resistance_failed_breakout(symbol, timeframe, df),
                    )
                )

                # primary_structure/volatility are only computed "as
                # needed" — i.e. lazily, once per eligible (symbol,
                # timeframe), and only when there is at least one finding
                # to score (skipped entirely on a no-findings timeframe).
                context_inputs: Optional[ContextInputs] = None
                if findings:
                    # Each computation is caught in its own try/except so a
                    # failure here can only ever degrade that one component
                    # to a directly-constructed typed UNAVAILABLE fallback
                    # (never a second call into the same failing
                    # computation) — it must never discard the findings
                    # already collected above for this (symbol, timeframe),
                    # nor prevent the _record_finding loop below from
                    # running for them.
                    try:
                        primary_structure = compute_primary_structure_component(
                            df, timeframe, now_ms
                        )
                    except Exception:
                        summary.context_errors += 1
                        logger.error(
                            "Opportunity primary structure computation error for "
                            f"{symbol}/{timeframe}",
                            exc_info=True,
                        )
                        primary_structure = StructureComponent(
                            status="UNAVAILABLE",
                            direction=None,
                            reason="COMPUTATION_FAILED",
                            sample_count=0,
                            latest_close_boundary_ms=None,
                        )
                    try:
                        volatility = compute_volatility_component(df, timeframe, now_ms)
                    except Exception:
                        summary.context_errors += 1
                        logger.error(
                            f"Opportunity volatility computation error for {symbol}/{timeframe}",
                            exc_info=True,
                        )
                        volatility = VolatilityComponent(
                            status="UNAVAILABLE",
                            atr_percent=None,
                            reason="COMPUTATION_FAILED",
                            sample_count=0,
                            latest_close_boundary_ms=None,
                        )
                    context_inputs = ContextInputs(
                        as_of_ms=now_ms,
                        primary_timeframe=timeframe,
                        symbol_htf_trend=symbol_htf_trend,
                        primary_structure=primary_structure,
                        btc_htf_trend=symbol_btc_htf_trend,
                        volatility=volatility,
                    )

                for finding in findings:
                    is_new = _record_finding(
                        conn,
                        finding,
                        now,
                        context_inputs=context_inputs,
                        summary=summary,
                        settings=settings,
                    )
                    summary.findings += 1
                    if is_new:
                        summary.new_opportunities += 1
                    else:
                        summary.re_confirmed += 1

        except Exception as e:
            logger.error(f"Opportunity scan error for {symbol}: {e}", exc_info=True)

    summary.log(logger)
    return summary


def _build_score_snapshot(
    finding: DetectorFinding,
    context_inputs: Optional[ContextInputs],
) -> tuple[Optional[str], Optional[str], Optional[float], Optional[str], Optional[str]]:
    """Build the one-time, immutable first-detection context/score snapshot
    for a NEW opportunity row only — reconfirmations (touch_opportunity)
    never call this again, for any family (see module docstring).

    Returns `(context_json, component_scores_json, total_score,
    score_version, score_warnings_json)`.

    `context_inputs is None` (no enrichment supplied at all — e.g. a unit
    test calling `_record_finding` directly) is not an error: it silently
    yields all-NULL fields with no warning, distinct from an enrichment
    that was actually attempted and failed below (which still yields NULL
    context/score, but tags score_version and records an explicit
    warning). Any exception here is contained — the caller still inserts
    the original detector finding either way.
    """
    if context_inputs is None:
        return None, None, None, None, None
    if finding.setup_family not in SUPPORTED_SETUP_FAMILIES:
        # Defensive: setup_family is only ever detector-assigned today (the
        # five literals in contract.SetupFamily), but this is verified
        # against the sealed contract (typing.get_args) rather than assumed,
        # so an unsupported/malformed value never reaches assemble_context/
        # score_opportunity — it yields the same NULL-enrichment-plus-
        # explicit-warning outcome as any other contained scoring failure,
        # and never loosens schema.sql's own setup_family CHECK constraint.
        logger.error(
            "Opportunity scoring error: unsupported setup_family="
            f"{finding.setup_family} symbol={finding.symbol} "
            f"timeframe={finding.primary_timeframe}"
        )
        return None, None, None, SCORE_VERSION, json.dumps(["UNSUPPORTED_SETUP_FAMILY"])
    try:
        context = assemble_context(
            as_of_ms=context_inputs.as_of_ms,
            primary_timeframe=context_inputs.primary_timeframe,
            scored_direction=finding.direction,
            symbol_htf_trend=context_inputs.symbol_htf_trend,
            primary_structure=context_inputs.primary_structure,
            btc_htf_trend=context_inputs.btc_htf_trend,
            volatility=context_inputs.volatility,
        )
        result = score_opportunity(context)
        context_json = json.dumps(context_to_dict(context), allow_nan=False)
        component_scores_json = json.dumps(component_scores_to_dict(result), allow_nan=False)
        score_warnings_json = json.dumps(list(result.warnings), allow_nan=False)
        return (
            context_json,
            component_scores_json,
            result.total_score,
            result.score_version,
            score_warnings_json,
        )
    except Exception:
        logger.error(
            "Opportunity context/scoring error: "
            f"symbol={finding.symbol} timeframe={finding.primary_timeframe} "
            f"family={finding.setup_family}",
            exc_info=True,
        )
        return None, None, None, SCORE_VERSION, json.dumps(["CONTEXT_SCORING_FAILED"])


def _maybe_create_trade_plan(
    conn: sqlite3.Connection,
    finding: DetectorFinding,
    opportunity_uid: str,
    fingerprint: str,
    now: str,
    settings: Optional[Settings],
    summary: Optional[OpportunityScanSummary],
) -> None:
    """Create the one immutable trade plan (+ initial outcome row) for a
    brand-new opportunity only — never called for a reconfirmation (see
    _record_finding's NEW-row branch below). Fully additive and isolated:
    any failure here is counted/logged and never raises out to the caller,
    so a plan-creation failure can never suppress the opportunity row
    already committed, nor any other finding/market/timeframe.

    Rechecks all three safety guards itself (trade_plan_evidence_enabled AND
    opportunity_engine_enabled AND dry_run_mode) — defense in depth mirroring
    run_opportunity_scan's own top-of-function guard, so this stays safe
    even if _record_finding is ever called directly (e.g. by a test) without
    going through that guard.
    """
    if settings is None:
        return
    if not (
        settings.trade_plan_evidence_enabled
        and settings.opportunity_engine_enabled
        and settings.dry_run_mode
    ):
        return
    try:
        plan = build_trade_plan(
            finding,
            plan_uid=new_uid(),
            opportunity_uid=opportunity_uid,
            fingerprint=fingerprint,
            created_at=now,
        )
        outcome = build_initial_trade_plan_outcome(plan)
        repo.insert_trade_plan_with_outcome(conn, plan, outcome)
        if summary is not None:
            summary.trade_plans_created += 1
    except Exception:
        if summary is not None:
            summary.trade_plan_errors += 1
        logger.error(
            "Trade plan creation error: opportunity_uid=%s symbol=%s family=%s timeframe=%s",
            opportunity_uid,
            finding.symbol,
            finding.setup_family,
            finding.primary_timeframe,
            exc_info=True,
        )


def _record_finding(
    conn: sqlite3.Connection,
    finding: DetectorFinding,
    now: str,
    *,
    context_inputs: Optional[ContextInputs] = None,
    summary: Optional[OpportunityScanSummary] = None,
    settings: Optional[Settings] = None,
) -> bool:
    """Insert or touch the opportunity row for this finding.

    Returns True if a new row was inserted, False if an existing ACTIVE
    opportunity was touched instead. `context_inputs`/`summary`/`settings`
    are optional keyword-only additions so existing callers that pass only
    `(conn, finding, now)` keep working unchanged — see
    _build_score_snapshot / _maybe_create_trade_plan.
    """
    fingerprint = compute_fingerprint(finding)
    existing = repo.get_active_opportunity_by_fingerprint(conn, fingerprint)
    if existing is not None:
        if finding.setup_family in DIRECTION_INVARIANT_FAMILIES:
            # Compression's first-detection snapshot is immutable (see
            # engine.py module docstring). touch_opportunity() never touches
            # direction/source_candle_*/anchor_*/evidence_json/warnings_json
            # for any family, but it does overwrite measurements_json — so
            # for this family we pass the already-stored value straight
            # back instead of the incoming finding's, keeping it frozen too.
            measurements_json = existing["measurements_json"]
        else:
            measurements_json = json.dumps(finding.measurements)
        repo.touch_opportunity(
            conn,
            existing["opportunity_uid"],
            last_seen_at=now,
            occurrence_count=int(existing["occurrence_count"]) + 1,
            measurements_json=measurements_json,
        )
        # touch_opportunity() never touches context_json/component_scores_json/
        # total_score/score_version/score_warnings_json for any family — the
        # first-detection snapshot (including NULL, if that is what was first
        # recorded) stays frozen exactly as inserted.
        return False

    context_json, component_scores_json, total_score, score_version, score_warnings_json = (
        _build_score_snapshot(finding, context_inputs)
    )
    if context_inputs is not None and context_json is None and summary is not None:
        # An enrichment attempt was made (context_inputs supplied) but
        # failed (contained inside _build_score_snapshot) — distinct from
        # "no enrichment supplied at all", which is not an error.
        summary.score_errors += 1

    opportunity = Opportunity(
        opportunity_uid=new_uid(),
        fingerprint=fingerprint,
        symbol=finding.symbol,
        direction=finding.direction,
        setup_family=finding.setup_family,
        detector_version=finding.detector_version,
        primary_timeframe=finding.primary_timeframe,
        first_detected_at=now,
        last_seen_at=now,
        source_candle_open_time=finding.source_candle_open_time,
        source_candle_close_time=finding.source_candle_close_time,
        anchor_price=finding.anchor_price,
        anchor_open_time=finding.anchor_open_time,
        evidence_json=json.dumps(finding.evidence),
        warnings_json=json.dumps(finding.warnings),
        measurements_json=json.dumps(finding.measurements),
        context_json=context_json,
        component_scores_json=component_scores_json,
        total_score=total_score,
        score_version=score_version,
        score_warnings_json=score_warnings_json,
    )
    opportunity.id = repo.insert_opportunity(conn, opportunity)
    _maybe_create_trade_plan(
        conn, finding, opportunity.opportunity_uid, fingerprint, now, settings, summary
    )
    return True
