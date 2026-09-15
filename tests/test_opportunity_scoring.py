"""Tests for the transparent CONTEXT-ALIGNMENT scoring in
src/apex/opportunity/scoring.py (Context and ranking v0.1).

Covers: hand-computed aligned/opposed/neutral/unavailable component
arithmetic, score bounds/clamping, direction reversal flipping
ALIGNED<->OPPOSED, explicit available/applicable-weight coverage, NULL
total_score (not a default 50) when no applicable component is available,
NOT_APPLICABLE (BTC-self) exclusion from applicable_weight, unsupported/
malformed scored_direction yielding an unscored snapshot with a warning
(never a raised exception), and deterministic JSON-safe serialization.
"""
from __future__ import annotations

import json

import pytest

from apex.opportunity.context import (
    CONTEXT_VERSION,
    OpportunityContext,
    StructureComponent,
    TrendComponent,
    VolatilityComponent,
    not_applicable_trend_component,
)
from apex.opportunity.scoring import (
    BASE_SCORE,
    BTC_ALIGNMENT_WEIGHT,
    HTF_ALIGNMENT_WEIGHT,
    INVALID_COMPONENT_DIRECTION_WARNING,
    PRIMARY_STRUCTURE_WEIGHT,
    SCORE_VERSION,
    component_scores_to_dict,
    score_opportunity,
)


def _trend(status="AVAILABLE", direction="LONG", sample_count=30) -> TrendComponent:
    return TrendComponent(
        status=status, direction=direction, reason="test", sample_count=sample_count,
        latest_close_boundary_ms=1000,
    )


def _structure(status="AVAILABLE", direction="LONG") -> StructureComponent:
    return StructureComponent(
        status=status, direction=direction, reason="test", sample_count=30,
        latest_close_boundary_ms=1000,
    )


def _volatility(status="AVAILABLE", atr_percent=1.5) -> VolatilityComponent:
    return VolatilityComponent(
        status=status, atr_percent=atr_percent, reason="test", sample_count=30,
        latest_close_boundary_ms=1000,
    )


def _context(
    *,
    scored_direction="LONG",
    symbol_htf_trend=None,
    primary_structure=None,
    btc_htf_trend=None,
    volatility=None,
    as_of_ms=1_700_000_000_000,
) -> OpportunityContext:
    return OpportunityContext(
        version=CONTEXT_VERSION,
        as_of_ms=as_of_ms,
        primary_timeframe="5m",
        scored_direction=scored_direction,
        symbol_htf_trend=symbol_htf_trend if symbol_htf_trend is not None else _trend(),
        primary_structure=primary_structure if primary_structure is not None else _structure(),
        btc_htf_trend=btc_htf_trend if btc_htf_trend is not None else _trend(),
        volatility=volatility if volatility is not None else _volatility(),
    )


# ------------------------------------------------------------------ hand-computed arithmetic


def test_all_three_components_aligned_sums_to_base_plus_all_weights():
    context = _context(
        scored_direction="LONG",
        symbol_htf_trend=_trend(direction="LONG"),
        primary_structure=_structure(direction="LONG"),
        btc_htf_trend=_trend(direction="LONG"),
    )
    result = score_opportunity(context)
    expected = BASE_SCORE + HTF_ALIGNMENT_WEIGHT + PRIMARY_STRUCTURE_WEIGHT + BTC_ALIGNMENT_WEIGHT
    assert result.total_score == pytest.approx(expected)
    assert result.total_score == 100.0  # 50 + 25 + 15 + 10 = 100, exactly at the clamp boundary
    assert result.warnings == ()
    assert result.available_weight == HTF_ALIGNMENT_WEIGHT + PRIMARY_STRUCTURE_WEIGHT + BTC_ALIGNMENT_WEIGHT
    assert result.applicable_weight == result.available_weight


def test_all_three_components_opposed_sums_to_base_minus_all_weights():
    context = _context(
        scored_direction="LONG",
        symbol_htf_trend=_trend(direction="SHORT"),
        primary_structure=_structure(direction="SHORT"),
        btc_htf_trend=_trend(direction="SHORT"),
    )
    result = score_opportunity(context)
    expected = BASE_SCORE - HTF_ALIGNMENT_WEIGHT - PRIMARY_STRUCTURE_WEIGHT - BTC_ALIGNMENT_WEIGHT
    assert expected == 0.0
    assert result.total_score == 0.0  # clamped floor, and exactly reached here


def test_mixed_aligned_and_opposed_hand_computed():
    # htf ALIGNED (+25), structure OPPOSED (-15), btc NEUTRAL (0)
    context = _context(
        scored_direction="LONG",
        symbol_htf_trend=_trend(direction="LONG"),
        primary_structure=_structure(direction="SHORT"),
        btc_htf_trend=_trend(direction="NEUTRAL"),
    )
    result = score_opportunity(context)
    assert result.total_score == pytest.approx(50.0 + 25.0 - 15.0 + 0.0)
    assert result.total_score == 60.0

    by_name = {c.name: c for c in result.components}
    assert by_name["htf_alignment"].alignment == "ALIGNED"
    assert by_name["htf_alignment"].contribution == 25.0
    assert by_name["primary_structure"].alignment == "OPPOSED"
    assert by_name["primary_structure"].contribution == -15.0
    assert by_name["btc_alignment"].alignment == "NEUTRAL"
    assert by_name["btc_alignment"].contribution == 0.0


def test_neutral_components_contribute_zero_but_are_distinguished_from_unavailable():
    context = _context(
        scored_direction="LONG",
        symbol_htf_trend=_trend(direction="NEUTRAL"),
        primary_structure=_structure(status="UNAVAILABLE", direction=None),
        btc_htf_trend=_trend(direction="LONG"),
    )
    result = score_opportunity(context)
    assert result.total_score == pytest.approx(50.0 + 0.0 + 0.0 + 10.0)

    by_name = {c.name: c for c in result.components}
    assert by_name["htf_alignment"].status == "AVAILABLE"
    assert by_name["htf_alignment"].alignment == "NEUTRAL"
    assert by_name["primary_structure"].status == "UNAVAILABLE"
    assert by_name["primary_structure"].alignment is None
    assert by_name["primary_structure"].component_direction is None


# ------------------------------------------------------------------ direction reversal


def test_direction_reversal_flips_aligned_to_opposed():
    fixed_component = _trend(direction="LONG")
    long_context = _context(
        scored_direction="LONG", symbol_htf_trend=fixed_component,
        primary_structure=_structure(status="UNAVAILABLE"),
        btc_htf_trend=_trend(status="UNAVAILABLE"),
    )
    short_context = _context(
        scored_direction="SHORT", symbol_htf_trend=fixed_component,
        primary_structure=_structure(status="UNAVAILABLE"),
        btc_htf_trend=_trend(status="UNAVAILABLE"),
    )
    long_result = score_opportunity(long_context)
    short_result = score_opportunity(short_context)

    assert long_result.components[0].alignment == "ALIGNED"
    assert short_result.components[0].alignment == "OPPOSED"
    assert long_result.total_score == pytest.approx(50.0 + HTF_ALIGNMENT_WEIGHT)
    assert short_result.total_score == pytest.approx(50.0 - HTF_ALIGNMENT_WEIGHT)


# ------------------------------------------------------------------ coverage / NULL-all-unavailable


def test_all_applicable_components_unavailable_yields_null_not_fifty():
    context = _context(
        scored_direction="LONG",
        symbol_htf_trend=_trend(status="UNAVAILABLE", direction=None),
        primary_structure=_structure(status="UNAVAILABLE", direction=None),
        btc_htf_trend=_trend(status="UNAVAILABLE", direction=None),
    )
    result = score_opportunity(context)
    assert result.total_score is None
    assert result.available_weight == 0.0
    assert result.applicable_weight == HTF_ALIGNMENT_WEIGHT + PRIMARY_STRUCTURE_WEIGHT + BTC_ALIGNMENT_WEIGHT
    assert "ALL_APPLICABLE_COMPONENTS_UNAVAILABLE" in result.warnings


def test_btc_self_not_applicable_excluded_from_applicable_weight_and_never_null_alone():
    context = _context(
        scored_direction="LONG",
        symbol_htf_trend=_trend(status="UNAVAILABLE", direction=None),
        primary_structure=_structure(status="UNAVAILABLE", direction=None),
        btc_htf_trend=not_applicable_trend_component(),
    )
    result = score_opportunity(context)
    # Only NOT_APPLICABLE + UNAVAILABLE components -> still no available
    # weight -> NULL, but applicable_weight excludes the NOT_APPLICABLE one.
    assert result.total_score is None
    assert result.applicable_weight == HTF_ALIGNMENT_WEIGHT + PRIMARY_STRUCTURE_WEIGHT
    by_name = {c.name: c for c in result.components}
    assert by_name["btc_alignment"].status == "NOT_APPLICABLE"
    assert by_name["btc_alignment"].weight == BTC_ALIGNMENT_WEIGHT
    assert by_name["btc_alignment"].contribution == 0.0


def test_single_available_component_never_renormalized_to_fill_missing_weight():
    context = _context(
        scored_direction="LONG",
        symbol_htf_trend=_trend(direction="LONG"),
        primary_structure=_structure(status="UNAVAILABLE", direction=None),
        btc_htf_trend=not_applicable_trend_component(),
    )
    result = score_opportunity(context)
    # Only +25 available; must stay exactly +25 on top of base, never scaled
    # up to compensate for the other two components' unused weight.
    assert result.total_score == pytest.approx(50.0 + HTF_ALIGNMENT_WEIGHT)
    assert result.available_weight == HTF_ALIGNMENT_WEIGHT


# ------------------------------------------------------------------ bounds / clamping


def test_score_never_exceeds_100_or_drops_below_0():
    long_all_aligned = _context(
        scored_direction="LONG",
        symbol_htf_trend=_trend(direction="LONG"),
        primary_structure=_structure(direction="LONG"),
        btc_htf_trend=_trend(direction="LONG"),
    )
    result = score_opportunity(long_all_aligned)
    assert 0.0 <= result.total_score <= 100.0

    short_all_opposed = _context(
        scored_direction="SHORT",
        symbol_htf_trend=_trend(direction="LONG"),
        primary_structure=_structure(direction="LONG"),
        btc_htf_trend=_trend(direction="LONG"),
    )
    result2 = score_opportunity(short_all_opposed)
    assert 0.0 <= result2.total_score <= 100.0
    assert result2.total_score == 0.0


# ------------------------------------------------------------------ unsupported / malformed direction


def test_unsupported_direction_yields_unscored_snapshot_and_warning_not_an_exception():
    context = _context(scored_direction="SIDEWAYS")  # malformed value
    result = score_opportunity(context)
    assert result.total_score is None
    assert result.warnings == ("UNSUPPORTED_DIRECTION",)
    assert result.score_version == SCORE_VERSION


# ------------------------------------------------------------------ malformed AVAILABLE component direction


def test_available_component_with_none_direction_becomes_unavailable_not_neutral():
    """An AVAILABLE-status component whose own `direction` is None (invalid
    for AVAILABLE per context.py's contract) must never be silently scored
    as genuine NEUTRAL: it is forced UNAVAILABLE, contributes 0, is removed
    from available_weight, and carries a deterministic warning naming the
    component.
    """
    context = _context(
        scored_direction="LONG",
        symbol_htf_trend=_trend(status="AVAILABLE", direction=None),  # malformed
        primary_structure=_structure(direction="LONG"),
        btc_htf_trend=_trend(direction="LONG"),
    )
    result = score_opportunity(context)

    by_name = {c.name: c for c in result.components}
    htf = by_name["htf_alignment"]
    assert htf.status == "UNAVAILABLE"
    assert htf.alignment is None
    assert htf.contribution == 0.0
    assert htf.component_direction is None
    assert htf.warning == f"{INVALID_COMPONENT_DIRECTION_WARNING}:htf_alignment"

    # Excluded from available_weight (same as any other UNAVAILABLE
    # component), still counted in applicable_weight (it is not
    # NOT_APPLICABLE), and total_score reflects only the two genuinely
    # available/aligned components.
    assert result.available_weight == PRIMARY_STRUCTURE_WEIGHT + BTC_ALIGNMENT_WEIGHT
    assert result.applicable_weight == HTF_ALIGNMENT_WEIGHT + PRIMARY_STRUCTURE_WEIGHT + BTC_ALIGNMENT_WEIGHT
    assert result.total_score == pytest.approx(50.0 + PRIMARY_STRUCTURE_WEIGHT + BTC_ALIGNMENT_WEIGHT)
    assert result.warnings == (f"{INVALID_COMPONENT_DIRECTION_WARNING}:htf_alignment",)


def test_available_component_with_arbitrary_garbage_direction_becomes_unavailable():
    context = _context(
        scored_direction="LONG",
        symbol_htf_trend=_trend(direction="LONG"),
        primary_structure=_structure(status="AVAILABLE", direction="SIDEWAYS"),  # garbage value
        btc_htf_trend=_trend(direction="LONG"),
    )
    result = score_opportunity(context)

    by_name = {c.name: c for c in result.components}
    structure = by_name["primary_structure"]
    assert structure.status == "UNAVAILABLE"
    assert structure.alignment is None
    assert structure.contribution == 0.0
    assert structure.component_direction == "SIDEWAYS"  # preserved for debugging, not trusted
    assert structure.warning == f"{INVALID_COMPONENT_DIRECTION_WARNING}:primary_structure"
    assert result.available_weight == HTF_ALIGNMENT_WEIGHT + BTC_ALIGNMENT_WEIGHT


def test_valid_neutral_component_direction_is_unaffected_by_the_malformed_direction_guard():
    """Contrast case: a real, valid NEUTRAL direction on an AVAILABLE
    component must still score as genuine NEUTRAL (0 contribution, no
    warning) — the malformed-direction guard must not misclassify it.
    """
    context = _context(
        scored_direction="LONG",
        symbol_htf_trend=_trend(direction="NEUTRAL"),
        primary_structure=_structure(direction="LONG"),
        btc_htf_trend=_trend(direction="LONG"),
    )
    result = score_opportunity(context)

    by_name = {c.name: c for c in result.components}
    htf = by_name["htf_alignment"]
    assert htf.status == "AVAILABLE"
    assert htf.alignment == "NEUTRAL"
    assert htf.contribution == 0.0
    assert htf.warning is None
    assert result.warnings == ()


def test_mixed_malformed_and_valid_components_only_warns_for_the_malformed_one():
    context = _context(
        scored_direction="LONG",
        symbol_htf_trend=_trend(direction="LONG"),
        primary_structure=_structure(status="AVAILABLE", direction=123),  # wrong type entirely
        btc_htf_trend=_trend(direction="SHORT"),
    )
    result = score_opportunity(context)

    by_name = {c.name: c for c in result.components}
    assert by_name["htf_alignment"].warning is None
    assert by_name["btc_alignment"].warning is None
    assert by_name["primary_structure"].warning == f"{INVALID_COMPONENT_DIRECTION_WARNING}:primary_structure"
    assert result.warnings == (f"{INVALID_COMPONENT_DIRECTION_WARNING}:primary_structure",)
    # htf ALIGNED (+25), btc OPPOSED (-10), structure excluded entirely.
    assert result.total_score == pytest.approx(50.0 + HTF_ALIGNMENT_WEIGHT - BTC_ALIGNMENT_WEIGHT)


def test_all_three_components_malformed_direction_yields_null_with_all_three_warnings():
    context = _context(
        scored_direction="LONG",
        symbol_htf_trend=_trend(status="AVAILABLE", direction=None),
        primary_structure=_structure(status="AVAILABLE", direction="GARBAGE"),
        btc_htf_trend=_trend(status="AVAILABLE", direction=""),
    )
    result = score_opportunity(context)

    assert result.total_score is None
    assert result.available_weight == 0.0
    assert set(result.warnings) == {
        f"{INVALID_COMPONENT_DIRECTION_WARNING}:htf_alignment",
        f"{INVALID_COMPONENT_DIRECTION_WARNING}:primary_structure",
        f"{INVALID_COMPONENT_DIRECTION_WARNING}:btc_alignment",
        "ALL_APPLICABLE_COMPONENTS_UNAVAILABLE",
    }
    for c in result.components:
        assert c.status == "UNAVAILABLE"
        assert c.warning is not None


def test_component_scores_to_dict_includes_warning_field():
    context = _context(
        scored_direction="LONG",
        symbol_htf_trend=_trend(status="AVAILABLE", direction=None),
        primary_structure=_structure(direction="LONG"),
        btc_htf_trend=_trend(direction="LONG"),
    )
    result = score_opportunity(context)
    d = component_scores_to_dict(result)
    serialized = json.dumps(d, allow_nan=False)
    round_tripped = json.loads(serialized)

    by_name = {c["name"]: c for c in round_tripped["components"]}
    assert by_name["htf_alignment"]["warning"] == f"{INVALID_COMPONENT_DIRECTION_WARNING}:htf_alignment"
    assert by_name["primary_structure"]["warning"] is None
    assert by_name["btc_alignment"]["warning"] is None


# ------------------------------------------------------------------ serialization


def test_component_scores_to_dict_is_json_safe_and_deterministic():
    context = _context(
        scored_direction="LONG",
        symbol_htf_trend=_trend(direction="LONG"),
        primary_structure=_structure(direction="SHORT"),
        btc_htf_trend=_trend(status="UNAVAILABLE", direction=None),
    )
    result = score_opportunity(context)
    d1 = component_scores_to_dict(result)
    d2 = component_scores_to_dict(result)
    assert d1 == d2

    serialized = json.dumps(d1, allow_nan=False)
    round_tripped = json.loads(serialized)
    assert round_tripped["score_version"] == SCORE_VERSION
    assert len(round_tripped["components"]) == 3
    names = {c["name"] for c in round_tripped["components"]}
    assert names == {"htf_alignment", "primary_structure", "btc_alignment"}
