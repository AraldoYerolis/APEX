"""Research candidate prospective tagging — Milestone 11F.

Pure evaluator. No DB access, no side effects.

Tags future signal observations with 11F research cohort metadata so future
reports can answer: "Do the 11E promising/weak cohorts continue to validate
prospectively after they were identified?"

This is NOT a runtime filter, NOT an alert gate, NOT a trading strategy.
Matching or not matching a candidate tag has zero effect on:
  - signal generation
  - alert eligibility or delivery
  - scheduler behavior
  - candidate gate logic from 11C
  - any runtime behavior

Research candidate version: 11F_v1

How this works:
  1. _capture_signal_features() in tasks.py calls evaluate_research_candidates()
     with the freshly computed indicator values.
  2. The result is merged into the existing metadata_json alongside the 11C
     candidate gate fields (which are preserved unchanged).
  3. scripts/report_research_candidates.py reads post-11F rows and compares
     outcome statistics across research candidate tags.
  4. Nothing else changes.

Candidate tags evaluated:
  Positive/review (identified as promising in 11E):
    SESSION_US, ATR_0_50_TO_1_00, EMA_GT_0_25, SYMBOL_WLD, SYMBOL_DOGE, SYMBOL_BTC

  Negative/avoid (identified as weak in 11E):
    SESSION_LATE_US, HOUR_10_AVOID, HOUR_03_AVOID, HOUR_21_AVOID

  Neutral/context:
    DIRECTION_LONG, DIRECTION_SHORT, SESSION_ASIA, SESSION_LONDON
"""
from __future__ import annotations

from typing import Optional

RESEARCH_CANDIDATE_VERSION = "11F_v1"

# Candidate tag definitions — documented so reports can reference them.
# type: POSITIVE (promising in 11E) / NEGATIVE (weak in 11E) / NEUTRAL (context only)
CANDIDATE_DEFINITIONS: dict[str, dict] = {
    # ---- Positive / review tags ----------------------------------------
    "SESSION_US": {
        "type": "POSITIVE",
        "description": "Observed during US session (13:00–20:59 UTC)",
        "basis": "11E US session: n=80, TP1=36.2%, Policy B=+0.237R, Policy E=+0.022R",
    },
    "ATR_0_50_TO_1_00": {
        "type": "POSITIVE",
        "description": "ATR% in [0.50, 1.00]",
        "basis": "11E ATR% 0.50–1.00: n=33, TP1=27.3%, Policy B=+0.121R, Policy E=-0.023R",
    },
    "EMA_GT_0_25": {
        "type": "POSITIVE",
        "description": "EMA spread% > 0.25",
        "basis": "11E EMA% > 0.25: n=36, TP1=36.1%, Policy B=+0.167R, Policy E=-0.042R",
        "direction_concentration_note": "Direction-concentrated LONG in 11E — interpret with caution",
    },
    "SYMBOL_WLD": {
        "type": "POSITIVE",
        "description": "Symbol is WLD",
        "basis": "11E WLD: n=20, TP1=40.0%, Policy B=+0.350R, Policy E=+0.163R",
        "direction_concentration_note": "Direction-concentrated LONG in 11E — interpret with caution",
    },
    "SYMBOL_DOGE": {
        "type": "POSITIVE",
        "description": "Symbol is DOGE",
        "basis": "11E DOGE: n=40, TP1=30.0%, Policy B=+0.100R",
    },
    "SYMBOL_BTC": {
        "type": "POSITIVE",
        "description": "Symbol is BTC",
        "basis": "11E BTC: n=44, TP1=25.0%, Policy B=+0.045R",
    },
    # ---- Negative / review-avoid tags ----------------------------------
    "SESSION_LATE_US": {
        "type": "NEGATIVE",
        "description": "Observed during LATE_US session (21:00–23:59 UTC)",
        "basis": "11E LATE_US: n=67, TP1=13.4%, Policy B=-0.134R, Policy E=-0.351R",
    },
    "HOUR_10_AVOID": {
        "type": "NEGATIVE",
        "description": "Observed at UTC hour 10",
        "basis": "11E Hour 10: n=20, TP1=5.0%, Policy B=-0.150R, Policy E=-0.362R",
    },
    "HOUR_03_AVOID": {
        "type": "NEGATIVE",
        "description": "Observed at UTC hour 03",
        "basis": "11E Hour 03: n=23, TP1=8.7%, Policy B=+0.000R, Policy E=-0.250R",
    },
    "HOUR_21_AVOID": {
        "type": "NEGATIVE",
        "description": "Observed at UTC hour 21",
        "basis": "11E Hour 21: n=19, TP1=10.5%, Policy B=-0.053R, Policy E=-0.289R",
    },
    # ---- Neutral / context tags ----------------------------------------
    "DIRECTION_LONG": {
        "type": "NEUTRAL",
        "description": "Signal direction is LONG",
        "basis": "Direction context tag only",
    },
    "DIRECTION_SHORT": {
        "type": "NEUTRAL",
        "description": "Signal direction is SHORT",
        "basis": "Direction context tag only",
    },
    "SESSION_ASIA": {
        "type": "NEUTRAL",
        "description": "Observed during ASIA session (00:00–07:59 UTC)",
        "basis": "Session context tag only",
    },
    "SESSION_LONDON": {
        "type": "NEUTRAL",
        "description": "Observed during LONDON session (08:00–12:59 UTC)",
        "basis": "Session context tag only",
    },
}

_POSITIVE_TAGS = frozenset(k for k, v in CANDIDATE_DEFINITIONS.items() if v["type"] == "POSITIVE")
_NEGATIVE_TAGS = frozenset(k for k, v in CANDIDATE_DEFINITIONS.items() if v["type"] == "NEGATIVE")


def _parse_hour(observed_at: Optional[str]) -> Optional[int]:
    """Extract UTC hour (0-23) from ISO8601 observed_at string. Returns None on failure."""
    if not observed_at:
        return None
    try:
        t = observed_at.strip()
        if "T" in t:
            time_part = t.split("T")[1].rstrip("Z")
        elif " " in t:
            time_part = t.split(" ")[1]
        else:
            return None
        return int(time_part.split(":")[0])
    except (IndexError, ValueError):
        return None


def evaluate_research_candidates(
    atr_pct: Optional[float],
    ema_spread_pct: Optional[float],
    observed_at: Optional[str],
    symbol: Optional[str],
    direction: Optional[str],
) -> dict:
    """Evaluate all 11F research candidate tags for a single signal feature snapshot.

    Returns a dict suitable for JSON serialisation:
      {
        "research_candidate_version": "11F_v1",
        "research_candidate_names": ["SESSION_US", ...],  # all matched tags
        "research_candidate_flags": {
          "<tag>": {"matched": <bool>, "reason": "<str>"},
          ...
        },
        "research_candidate_positive": [...],   # matched positive tags
        "research_candidate_negative": [...],   # matched negative tags
        "research_candidate_notes": [...]       # direction-concentration warnings etc.
      }

    Missing / None / invalid input values fail safely with descriptive reasons.
    A missing value causes the affected tag to be unmatched — never raises an exception.
    Has no side effects and does not access the database.
    """
    hour = _parse_hour(observed_at)
    flags: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Hour / session helpers
    # ------------------------------------------------------------------
    if observed_at is None:
        _hour_err = "observed_at missing"
    elif hour is None:
        _hour_err = f"could not parse hour from observed_at={observed_at!r}"
    else:
        _hour_err = None  # success — hour is a valid int

    def _hour_in_range(lo: int, hi_excl: int) -> tuple[bool, str]:
        if _hour_err:
            return False, _hour_err
        matched = lo <= hour < hi_excl  # type: ignore[operator]
        return matched, f"hour={hour} {'in' if matched else 'not in'} [{lo}, {hi_excl})"

    def _hour_exact(target: int) -> tuple[bool, str]:
        if _hour_err:
            return False, _hour_err
        matched = hour == target  # type: ignore[operator]
        return matched, f"hour={hour} {'==' if matched else '!='} {target}"

    # ------------------------------------------------------------------
    # Session / hour tags
    # ------------------------------------------------------------------
    m, r = _hour_in_range(13, 21)
    flags["SESSION_US"] = {"matched": m, "reason": r}

    m, r = _hour_in_range(21, 24)
    flags["SESSION_LATE_US"] = {"matched": m, "reason": r}

    m, r = _hour_exact(10)
    flags["HOUR_10_AVOID"] = {"matched": m, "reason": r}

    m, r = _hour_exact(3)
    flags["HOUR_03_AVOID"] = {"matched": m, "reason": r}

    m, r = _hour_exact(21)
    flags["HOUR_21_AVOID"] = {"matched": m, "reason": r}

    m, r = _hour_in_range(0, 8)
    flags["SESSION_ASIA"] = {"matched": m, "reason": r}

    m, r = _hour_in_range(8, 13)
    flags["SESSION_LONDON"] = {"matched": m, "reason": r}

    # ------------------------------------------------------------------
    # ATR_0_50_TO_1_00
    # ------------------------------------------------------------------
    if atr_pct is None:
        flags["ATR_0_50_TO_1_00"] = {"matched": False, "reason": "atr_pct missing"}
    elif 0.50 <= atr_pct <= 1.00:
        flags["ATR_0_50_TO_1_00"] = {
            "matched": True,
            "reason": f"atr_pct={atr_pct:.4f} in [0.50, 1.00]",
        }
    else:
        flags["ATR_0_50_TO_1_00"] = {
            "matched": False,
            "reason": f"atr_pct={atr_pct:.4f} not in [0.50, 1.00]",
        }

    # ------------------------------------------------------------------
    # EMA_GT_0_25
    # ------------------------------------------------------------------
    if ema_spread_pct is None:
        flags["EMA_GT_0_25"] = {"matched": False, "reason": "ema_spread_pct missing"}
    elif ema_spread_pct > 0.25:
        flags["EMA_GT_0_25"] = {
            "matched": True,
            "reason": f"ema_spread_pct={ema_spread_pct:.4f} > 0.25",
        }
    else:
        flags["EMA_GT_0_25"] = {
            "matched": False,
            "reason": f"ema_spread_pct={ema_spread_pct:.4f} not > 0.25",
        }

    # ------------------------------------------------------------------
    # Symbol tags
    # ------------------------------------------------------------------
    sym_upper = symbol.upper() if symbol else None
    for tag, expected in [
        ("SYMBOL_WLD",  "WLD"),
        ("SYMBOL_DOGE", "DOGE"),
        ("SYMBOL_BTC",  "BTC"),
    ]:
        if sym_upper is None:
            flags[tag] = {"matched": False, "reason": "symbol missing"}
        elif sym_upper == expected:
            flags[tag] = {"matched": True, "reason": f"symbol={symbol}"}
        else:
            flags[tag] = {"matched": False, "reason": f"symbol={symbol} != {expected}"}

    # ------------------------------------------------------------------
    # Direction tags (neutral context)
    # ------------------------------------------------------------------
    dir_upper = direction.upper() if direction else None
    if dir_upper is None:
        flags["DIRECTION_LONG"]  = {"matched": False, "reason": "direction missing"}
        flags["DIRECTION_SHORT"] = {"matched": False, "reason": "direction missing"}
    else:
        flags["DIRECTION_LONG"]  = {"matched": dir_upper == "LONG",  "reason": f"direction={direction}"}
        flags["DIRECTION_SHORT"] = {"matched": dir_upper == "SHORT", "reason": f"direction={direction}"}

    # ------------------------------------------------------------------
    # Collect results
    # ------------------------------------------------------------------
    matched_all     = [tag for tag, data in flags.items() if data["matched"]]
    positive_matched = [tag for tag in matched_all if tag in _POSITIVE_TAGS]
    negative_matched = [tag for tag in matched_all if tag in _NEGATIVE_TAGS]

    # Direction-concentration notes for matched positive tags
    notes = []
    for tag in positive_matched:
        note = CANDIDATE_DEFINITIONS[tag].get("direction_concentration_note")
        if note:
            notes.append(f"{tag}: {note}")

    return {
        "research_candidate_version": RESEARCH_CANDIDATE_VERSION,
        "research_candidate_names": matched_all,
        "research_candidate_flags": flags,
        "research_candidate_positive": positive_matched,
        "research_candidate_negative": negative_matched,
        "research_candidate_notes": notes,
    }
