"""Candidate runtime gate evaluation — Milestone 11C / 11G.

DRY-RUN PROSPECTIVE TAGGING ONLY.

Gates are evaluated at observation time and stored in
signal_features.metadata_json. They do NOT:
  - suppress observations
  - alter signal generation
  - block or modify alerts
  - change any config value
  - write to any table other than the metadata_json field already
    captured as part of the normal signal_features insert

Gate version: 11G_v1

Semantics (11G):
  - `candidate_gate_passed` is STRICT ALL — True only when every gate
    (A, B, C, AND D) passes. Older rows tagged `11C_v1` used ANY
    semantics; readers MUST branch on `candidate_gate_version` to
    interpret correctly.
  - `candidate_gate_any_passed` preserves the legacy 11C ANY value
    (True if at least one gate passes) so downstream readers can
    reproduce the old metric without reparsing the per-gate map.

How this works:
  1. _capture_signal_features() in tasks.py calls evaluate_candidate_gates()
     with the freshly computed indicator values.
  2. The result is JSON-serialised and stored in signal_features.metadata_json.
  3. scripts/report_candidate_gates.py reads those rows and produces cohort
     statistics for each gate (GATE_A, GATE_B, GATE_C, GATE_D).
  4. Nothing else changes.
"""
from __future__ import annotations

from typing import Optional

GATE_VERSION = "11G_v1"
LEGACY_GATE_VERSION_ANY = "11C_v1"  # pre-11G rows used `any(...)` semantics

# Gate definitions — documented here so the report can reference them.
GATE_DEFINITIONS = {
    "GATE_A_ATR_0_10_TO_1_00": "0.10 <= atr_pct <= 1.00",
    "GATE_B_EMA_SPREAD_NEG_0_25_TO_0": "-0.25 <= ema_spread_pct < 0",
    "GATE_C_RSI_50_TO_60": "50 <= rsi_val <= 60",
    "GATE_D_ATR_AND_EMA_COMBINED": "Gate A AND Gate B",
}


def evaluate_candidate_gates(
    atr_pct: Optional[float],
    ema_spread_pct: Optional[float],
    rsi_val: Optional[float],
) -> dict:
    """Evaluate all candidate gates for a single signal feature snapshot.

    Returns a dict suitable for JSON serialisation:
      {
        "candidate_gate_version": "11G_v1",
        "candidate_gate_passed":     <bool>,   # True only if ALL gates pass
        "candidate_gate_any_passed": <bool>,   # True if ANY gate passes (legacy 11C semantics)
        "gates": {
          "<gate_name>": {"passed": <bool>, "reason": "<str>"},
          ...
        }
      }

    Missing / None feature values cause the affected gate(s) to fail
    with a descriptive reason rather than raising an exception.
    """
    gates: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Gate A — ATR sanity gate: 0.10 <= atr_pct <= 1.00
    # ------------------------------------------------------------------
    if atr_pct is None:
        gates["GATE_A_ATR_0_10_TO_1_00"] = {
            "passed": False,
            "reason": "atr_pct missing",
        }
    elif 0.10 <= atr_pct <= 1.00:
        gates["GATE_A_ATR_0_10_TO_1_00"] = {
            "passed": True,
            "reason": f"atr_pct={atr_pct:.4f} in [0.10, 1.00]",
        }
    else:
        gates["GATE_A_ATR_0_10_TO_1_00"] = {
            "passed": False,
            "reason": f"atr_pct={atr_pct:.4f} not in [0.10, 1.00]",
        }

    # ------------------------------------------------------------------
    # Gate B — EMA pullback band: -0.25 <= ema_spread_pct < 0
    # ------------------------------------------------------------------
    if ema_spread_pct is None:
        gates["GATE_B_EMA_SPREAD_NEG_0_25_TO_0"] = {
            "passed": False,
            "reason": "ema_spread_pct missing",
        }
    elif -0.25 <= ema_spread_pct < 0:
        gates["GATE_B_EMA_SPREAD_NEG_0_25_TO_0"] = {
            "passed": True,
            "reason": f"ema_spread_pct={ema_spread_pct:.4f} in [-0.25, 0)",
        }
    else:
        gates["GATE_B_EMA_SPREAD_NEG_0_25_TO_0"] = {
            "passed": False,
            "reason": f"ema_spread_pct={ema_spread_pct:.4f} not in [-0.25, 0)",
        }

    # ------------------------------------------------------------------
    # Gate C — RSI middle strength: 50 <= rsi_val <= 60
    # ------------------------------------------------------------------
    if rsi_val is None:
        gates["GATE_C_RSI_50_TO_60"] = {
            "passed": False,
            "reason": "rsi_val missing",
        }
    elif 50 <= rsi_val <= 60:
        gates["GATE_C_RSI_50_TO_60"] = {
            "passed": True,
            "reason": f"rsi_val={rsi_val:.2f} in [50, 60]",
        }
    else:
        gates["GATE_C_RSI_50_TO_60"] = {
            "passed": False,
            "reason": f"rsi_val={rsi_val:.2f} not in [50, 60]",
        }

    # ------------------------------------------------------------------
    # Gate D — Combined: Gate A AND Gate B
    # ------------------------------------------------------------------
    gate_a_passed = gates["GATE_A_ATR_0_10_TO_1_00"]["passed"]
    gate_b_passed = gates["GATE_B_EMA_SPREAD_NEG_0_25_TO_0"]["passed"]

    missing = []
    if atr_pct is None:
        missing.append("atr_pct")
    if ema_spread_pct is None:
        missing.append("ema_spread_pct")

    if missing:
        gates["GATE_D_ATR_AND_EMA_COMBINED"] = {
            "passed": False,
            "reason": f"{', '.join(missing)} missing",
        }
    elif gate_a_passed and gate_b_passed:
        gates["GATE_D_ATR_AND_EMA_COMBINED"] = {
            "passed": True,
            "reason": "Gate A and Gate B both pass",
        }
    else:
        fail_reasons = []
        if not gate_a_passed:
            fail_reasons.append(gates["GATE_A_ATR_0_10_TO_1_00"]["reason"])
        if not gate_b_passed:
            fail_reasons.append(gates["GATE_B_EMA_SPREAD_NEG_0_25_TO_0"]["reason"])
        gates["GATE_D_ATR_AND_EMA_COMBINED"] = {
            "passed": False,
            "reason": "; ".join(fail_reasons),
        }

    gate_pass_flags = [g["passed"] for g in gates.values()]
    return {
        "candidate_gate_version": GATE_VERSION,
        "candidate_gate_passed": all(gate_pass_flags),
        "candidate_gate_any_passed": any(gate_pass_flags),
        "gates": gates,
    }
