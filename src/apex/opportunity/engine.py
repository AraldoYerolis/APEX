"""TA Opportunity Engine v0.1 orchestration — additive, research-only.

Runs the VOLATILITY_COMPRESSION and SWEEP_RECLAIM detectors over 3m/5m
candles already held in the shared CandleStore (no new REST/WS calls, no
new per-symbol DB queries — see CLAUDE.md milestone spec J), using 15m
purely as recorded context, never a hard gate.

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
from dataclasses import dataclass

from apex.config import Settings
from apex.data.candle_store import CandleStore
from apex.db import repository as repo
from apex.opportunity.contract import (
    CONTRACT_VERSION,
    DIRECTION_INVARIANT_FAMILIES,
    DIRECTION_INVARIANT_FINGERPRINT_TOKEN,
    DetectorFinding,
    Opportunity,
)
from apex.opportunity.detectors.sweep_reclaim import detect_sweep_reclaim
from apex.opportunity.detectors.volatility_compression import detect_volatility_compression
from apex.utils.ids import new_uid
from apex.utils.time import minutes_ago_iso, utcnow_iso

logger = logging.getLogger(__name__)

OPPORTUNITY_TIMEFRAMES: tuple[str, ...] = ("3m", "5m")

# Enough history for both detectors' lookbacks (ATR/compression baseline is
# the larger of the two requirements) plus headroom.
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

    def log(self, logger: logging.Logger) -> None:
        logger.info(
            "[OPPORTUNITY ENGINE] "
            f"{self.markets_scanned} markets, {self.timeframe_pairs_scanned} symbol/timeframe pairs, "
            f"{self.findings} findings ({self.new_opportunities} new, {self.re_confirmed} re-confirmed), "
            f"{self.expired} expired, {self.no_data} no data, {self.stale} stale"
        )


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

    now = utcnow_iso()
    for timeframe, expiry_minutes in OPPORTUNITY_EXPIRY_MINUTES.items():
        cutoff = minutes_ago_iso(expiry_minutes)
        summary.expired += repo.expire_stale_opportunities(
            conn, primary_timeframe=timeframe, cutoff_iso=cutoff
        )

    for market in markets:
        symbol = market.symbol
        try:
            context_df_15m = candle_store.get_df(symbol, "15m")

            for timeframe in OPPORTUNITY_TIMEFRAMES:
                df = candle_store.get_df(symbol, timeframe)
                if df is None or len(df) < MIN_CANDLES_REQUIRED:
                    summary.no_data += 1
                    continue
                if candle_store.is_stale(symbol, timeframe):
                    summary.stale += 1
                    continue

                summary.timeframe_pairs_scanned += 1
                findings: list[DetectorFinding] = []
                findings.extend(
                    detect_volatility_compression(
                        symbol, timeframe, df, context_df_15m=context_df_15m
                    )
                )
                findings.extend(
                    detect_sweep_reclaim(symbol, timeframe, df, context_df_15m=context_df_15m)
                )

                for finding in findings:
                    is_new = _record_finding(conn, finding, now)
                    summary.findings += 1
                    if is_new:
                        summary.new_opportunities += 1
                    else:
                        summary.re_confirmed += 1

        except Exception as e:
            logger.error(f"Opportunity scan error for {symbol}: {e}", exc_info=True)

    summary.log(logger)
    return summary


def _record_finding(conn: sqlite3.Connection, finding: DetectorFinding, now: str) -> bool:
    """Insert or touch the opportunity row for this finding.

    Returns True if a new row was inserted, False if an existing ACTIVE
    opportunity was touched instead.
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
        return False

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
    )
    opportunity.id = repo.insert_opportunity(conn, opportunity)
    return True
