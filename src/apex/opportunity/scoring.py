"""Transparent CONTEXT-ALIGNMENT research scoring — Context and ranking v0.1.

Turns a `context.OpportunityContext` snapshot into a named, auditable sum
of direction-relative alignment contributions. This is a research
CONTEXT-ALIGNMENT score, not a probability, confidence rating,
profitability estimate, actionable/tradeable threshold, or a normalized
comparison of detector pattern strength across setup families — see
CONTEXT_LIMITATIONS_NOTE (context.py) and the ranked report's own printed
disclaimer.

Every supported setup family starts from the same neutral `BASE_SCORE`
(50.0). There is no family-quality adapter that maps a family's own
detector-specific evidence into a comparable conviction value, no RSI
gate, no alert coupling, and no minimum-score detection gate anywhere in
this module — context rules and scoring weights are identical across all
five current families (see engine.py).

`total_score` is NULL (never a default 50.0) whenever none of the
applicable components is AVAILABLE — "unscored" and "neutral 50" are
different, distinguishable outcomes. Missing weight is never renormalized
onto the remaining components: a single available +25 contribution stays
exactly +25, it is never scaled up to compensate for the other two
components being unavailable/not-applicable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from apex.opportunity.context import ComponentStatus, OpportunityContext

SCORE_VERSION = "context_alignment_v0_1"

BASE_SCORE = 50.0
SCORE_MIN = 0.0
SCORE_MAX = 100.0

HTF_ALIGNMENT_WEIGHT = 25.0
PRIMARY_STRUCTURE_WEIGHT = 15.0
BTC_ALIGNMENT_WEIGHT = 10.0

_OPPOSITE_DIRECTION: dict[str, str] = {"LONG": "SHORT", "SHORT": "LONG"}

# The only component-direction values context.py's own components ever
# legitimately produce for an AVAILABLE component (see context.py's
# ComponentDirection). Anything else — including None — reaching here on an
# AVAILABLE-status component is a malformed/invalid direction, never a
# genuine neutral read (see _score_component below).
_VALID_COMPONENT_DIRECTIONS = frozenset({"LONG", "SHORT", "NEUTRAL"})
INVALID_COMPONENT_DIRECTION_WARNING = "INVALID_COMPONENT_DIRECTION"

Alignment = Optional[str]  # "ALIGNED" | "OPPOSED" | "NEUTRAL" | None


@dataclass(frozen=True)
class ComponentScore:
    name: str
    weight: float
    status: ComponentStatus
    component_direction: Optional[str]
    alignment: Alignment
    contribution: float
    # Set only for the malformed-direction case below (see
    # _score_component); None for every other component, including a
    # genuinely UNAVAILABLE/NOT_APPLICABLE one.
    warning: Optional[str] = None


@dataclass(frozen=True)
class ScoringResult:
    score_version: str
    scored_direction: str
    as_of_ms: int
    base_score: float
    total_score: Optional[float]
    components: tuple[ComponentScore, ...]
    available_weight: float
    applicable_weight: float
    warnings: tuple[str, ...]


def _score_component(name: str, weight: float, component, direction: str) -> ComponentScore:
    if component.status == "NOT_APPLICABLE":
        return ComponentScore(
            name=name, weight=weight, status="NOT_APPLICABLE",
            component_direction=None, alignment=None, contribution=0.0,
        )
    if component.status != "AVAILABLE":
        return ComponentScore(
            name=name, weight=weight, status="UNAVAILABLE",
            component_direction=None, alignment=None, contribution=0.0,
        )

    comp_dir = component.direction
    if comp_dir not in _VALID_COMPONENT_DIRECTIONS:
        # AVAILABLE per the upstream component's own status, but its
        # direction value is malformed (None or anything other than
        # LONG/SHORT/NEUTRAL) — never silently treated as a genuine
        # NEUTRAL: forced UNAVAILABLE (excluding it from available_weight,
        # same as any other unavailable component) with a deterministic,
        # per-component warning surfaced in the final ScoringResult.
        return ComponentScore(
            name=name, weight=weight, status="UNAVAILABLE",
            component_direction=comp_dir, alignment=None, contribution=0.0,
            warning=f"{INVALID_COMPONENT_DIRECTION_WARNING}:{name}",
        )
    if comp_dir == direction:
        return ComponentScore(
            name=name, weight=weight, status="AVAILABLE",
            component_direction=comp_dir, alignment="ALIGNED", contribution=weight,
        )
    if comp_dir == _OPPOSITE_DIRECTION.get(direction):
        return ComponentScore(
            name=name, weight=weight, status="AVAILABLE",
            component_direction=comp_dir, alignment="OPPOSED", contribution=-weight,
        )
    # NEUTRAL (the only remaining legitimate value) contributes 0.0 but is
    # still distinguished from UNAVAILABLE via `status`/`alignment`.
    return ComponentScore(
        name=name, weight=weight, status="AVAILABLE",
        component_direction=comp_dir, alignment="NEUTRAL", contribution=0.0,
    )


def score_opportunity(context: OpportunityContext) -> ScoringResult:
    """Pure. `total_score = round(clamp(BASE_SCORE + sum(component
    contributions), 0, 100), 1)`, or None if no applicable component is
    AVAILABLE. Reads `context.scored_direction` (never a separate
    parameter) so the score can never drift from the direction the
    snapshot itself records.
    """
    direction = context.scored_direction

    if direction not in ("LONG", "SHORT"):
        return ScoringResult(
            score_version=SCORE_VERSION,
            scored_direction=str(direction),
            as_of_ms=context.as_of_ms,
            base_score=BASE_SCORE,
            total_score=None,
            components=(),
            available_weight=0.0,
            applicable_weight=0.0,
            warnings=("UNSUPPORTED_DIRECTION",),
        )

    htf = _score_component("htf_alignment", HTF_ALIGNMENT_WEIGHT, context.symbol_htf_trend, direction)
    structure = _score_component(
        "primary_structure", PRIMARY_STRUCTURE_WEIGHT, context.primary_structure, direction
    )
    btc = _score_component("btc_alignment", BTC_ALIGNMENT_WEIGHT, context.btc_htf_trend, direction)
    components = (htf, structure, btc)

    applicable_weight = sum(c.weight for c in components if c.status != "NOT_APPLICABLE")
    available_weight = sum(c.weight for c in components if c.status == "AVAILABLE")
    # Deterministic, order-preserving (htf, structure, btc) per-component
    # warnings — currently only ever populated by the malformed-direction
    # case in _score_component above.
    component_warnings = tuple(c.warning for c in components if c.warning is not None)

    if available_weight <= 0.0:
        return ScoringResult(
            score_version=SCORE_VERSION,
            scored_direction=direction,
            as_of_ms=context.as_of_ms,
            base_score=BASE_SCORE,
            total_score=None,
            components=components,
            available_weight=available_weight,
            applicable_weight=applicable_weight,
            warnings=component_warnings + ("ALL_APPLICABLE_COMPONENTS_UNAVAILABLE",),
        )

    raw_total = BASE_SCORE + sum(c.contribution for c in components)
    total_score = round(min(SCORE_MAX, max(SCORE_MIN, raw_total)), 1)

    return ScoringResult(
        score_version=SCORE_VERSION,
        scored_direction=direction,
        as_of_ms=context.as_of_ms,
        base_score=BASE_SCORE,
        total_score=total_score,
        components=components,
        available_weight=available_weight,
        applicable_weight=applicable_weight,
        warnings=component_warnings,
    )


def component_scores_to_dict(result: ScoringResult) -> dict:
    """Deterministic, JSON-serializable (safe under `json.dumps(...,
    allow_nan=False)`) representation of a scoring attempt's components and
    coverage — persisted separately from `context_json` (see engine.py).
    """
    return {
        "score_version": result.score_version,
        "scored_direction": result.scored_direction,
        "as_of_ms": result.as_of_ms,
        "base_score": result.base_score,
        "available_weight": result.available_weight,
        "applicable_weight": result.applicable_weight,
        "components": [
            {
                "name": c.name,
                "weight": c.weight,
                "status": c.status,
                "component_direction": c.component_direction,
                "alignment": c.alignment,
                "contribution": c.contribution,
                "warning": c.warning,
            }
            for c in result.components
        ],
    }
