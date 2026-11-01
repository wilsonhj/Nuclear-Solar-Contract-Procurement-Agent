"""Tolerance as the source printed it — issue #2.

There is no single industry convention. Trina prints `0~+5W`, Jinko prints
`0~+3%`, Canadian Solar prints `0~+10W`. Code assuming either convention is
wrong for at least two of the five largest suppliers, so the band is stored as
written and resolved against a nominal only at comparison time.
"""

import math

import pytest
from pydantic import ValidationError

from procurement_agent.schema import (
    CanonicalField,
    ConflictCandidate,
    DeclaredBand,
    DeclaredBandUnitError,
    SourceRef,
    SourceTier,
    ToleranceKind,
    ToleranceRule,
)
from procurement_agent.services.conflict_hitl import comparison_pairs, values_conflict
from procurement_agent.services.conflict_hitl.tolerance import FieldTolerance

#: `Power Tolerance 0 ~ +5` — Trina Vertex N NEG21C.20.
TRINA = DeclaredBand(low=0.0, high=5.0, kind=ToleranceKind.ABSOLUTE, unit="W")
#: `Power tolerance 0~+3%` — Jinko Tiger Neo 78HL4-BDV, one line covering 605–625 W.
JINKO = DeclaredBand(low=0.0, high=3.0, kind=ToleranceKind.RELATIVE)
#: `Power Tolerance 0 ~ + 10 W` — Canadian Solar HiKu6. Wider than one bin step.
CANADIAN = DeclaredBand(low=0.0, high=10.0, kind=ToleranceKind.ABSOLUTE, unit="W")


def test_a_relative_band_is_not_multiplied_out_at_extraction() -> None:
    """The contract's rule. One printed tolerance covers the whole 605–625 W
    family, so resolving early gives 18.15 W at one bin and 18.75 W at another —
    a disagreement between two rows whose source text is character-identical."""
    assert JINKO.high == 3.0
    assert JINKO.resolve(605.0, nominal_unit="W") != JINKO.resolve(625.0, nominal_unit="W")
    assert JINKO.resolve(620.0, nominal_unit="W") == pytest.approx((620.0, 638.6))


def test_percent_and_watts_are_not_interchangeable() -> None:
    """3% of 620 W is 18.6 W — nearly four 5 W bin steps. This is the whole issue."""
    assert JINKO.resolve(620.0, nominal_unit="W")[1] - 620.0 > 3 * (
        TRINA.resolve(620.0, nominal_unit="W")[1] - 620.0
    )


def test_a_relative_band_on_a_negative_nominal_is_not_inverted() -> None:
    """A Pmax temperature coefficient is negative, so `low` maps to the *upper*
    bound. Returning the offsets unswapped hands the caller an interval whose
    low exceeds its high, which intersects nothing and silently agrees with
    nothing."""
    band = DeclaredBand(low=-3.0, high=3.0, kind=ToleranceKind.RELATIVE)
    low, high = band.resolve(-0.29, nominal_unit="%/degC")
    assert low < high
    assert low == pytest.approx(-0.2987)
    assert high == pytest.approx(-0.2813)


def test_touching_ranges_agree() -> None:
    """650 W and 655 W modules both declared `0~+5W` guarantee [650,655] and
    [655,660]. A part measuring exactly 655 W satisfies both labels."""
    assert TRINA.agrees(650.0, TRINA, 655.0, nominal_unit="W", other_unit="W")
    assert not TRINA.agrees(650.0, TRINA, 656.0, nominal_unit="W", other_unit="W")


def test_a_band_wider_than_the_bin_step_overlaps_by_construction() -> None:
    """Canadian Solar's `0~+10W` is two 5 W bin steps, so a nominal 650 and a
    nominal 655 have overlapping guaranteed ranges as printed. Not a defect to
    correct — a fact the comparison has to tolerate."""
    assert CANADIAN.agrees(650.0, CANADIAN, 655.0, nominal_unit="W", other_unit="W")
    assert CANADIAN.agrees(650.0, CANADIAN, 660.0, nominal_unit="W", other_unit="W")


def test_a_missing_band_on_the_other_side_is_a_point_not_a_free_pass() -> None:
    """No printed band means no evidence of one. Treating the other nominal as
    exact can raise a conflict a shared band would have absorbed — one extra
    queue item, where the permissive reading silently merges two SKUs."""
    assert TRINA.agrees(650.0, None, 653.0, nominal_unit="W", other_unit="W")
    assert not TRINA.agrees(650.0, None, 656.0, nominal_unit="W", other_unit="W")


def test_mismatched_conventions_still_compare() -> None:
    """Trina's absolute against Jinko's relative: the point of resolving late."""
    assert TRINA.agrees(650.0, JINKO, 652.0, nominal_unit="W", other_unit="W")
    assert not TRINA.agrees(650.0, JINKO, 700.0, nominal_unit="W", other_unit="W")


def test_an_absolute_band_refuses_a_nominal_in_another_unit() -> None:
    """FN-3. `unit` was validated at construction and then read by nobody:
    `resolve` and `agrees` added `low`/`high` straight onto the nominal, and
    `_compare_numbers` never looked at it either.

    A `0 ~ +5 W` band against a nominal in kW gave `resolve(0.650) ==
    (0.65, 5.65)`, so a 0.650 kW value and a 5.0 kW one - a 7.7x disagreement,
    and exactly the 1000x class of extraction error D-2 calls out - had
    intersecting guaranteed ranges and raised no conflict at all.

    Refusing is the only honest answer. There is no unit algebra in this repo
    and inventing one here would be the "conversion resolves a mismatch" move
    FR-ING-08 forbids, so a band that cannot show it is in the nominal's unit
    declines to produce a range rather than producing a wrong one.
    """
    with pytest.raises(DeclaredBandUnitError):
        TRINA.resolve(0.650, nominal_unit="kW")
    with pytest.raises(DeclaredBandUnitError):
        TRINA.resolve(650.0, nominal_unit=None)
    assert TRINA.resolve(650.0, nominal_unit="W") == (650.0, 655.0)


def test_the_absorbed_disagreement_is_the_case_this_closes() -> None:
    """The measured defect, as the comparison it silently passed."""
    with pytest.raises(DeclaredBandUnitError):
        TRINA.agrees(0.650, TRINA, 5.0, nominal_unit="kW", other_unit="kW")
    # Both in the band's own unit, the same numbers are the conflict they are.
    assert not TRINA.agrees(650.0, TRINA, 5000.0, nominal_unit="W", other_unit="W")


def test_two_bands_in_different_units_do_not_compare() -> None:
    """`0 ~ +5 W` against `0 ~ +0.01 kW` are the same guarantee and this cannot
    say so, which is the point: it declines rather than adding 0.01 to a number
    in watts."""
    kilo = DeclaredBand(low=0.0, high=0.01, kind=ToleranceKind.ABSOLUTE, unit="kW")
    with pytest.raises(DeclaredBandUnitError):
        TRINA.agrees(650.0, kilo, 650.0, nominal_unit="W", other_unit="W")


def test_the_unit_check_folds_spelling_but_not_scale() -> None:
    """The same fold the rest of the comparison uses - NFKC, case, whitespace -
    so a sheet writing `w` is not a unit error. Nothing beyond that: `Wp` is not
    accepted for `W`, because deciding they are one unit is a technical claim,
    and `test_an_absolute_band_without_a_unit_is_rejected` already says where
    the band's unit is supposed to come from - the canonical field, which knows
    its own."""
    assert TRINA.resolve(650.0, nominal_unit=" w ") == (650.0, 655.0)
    with pytest.raises(DeclaredBandUnitError):
        TRINA.resolve(650.0, nominal_unit="Wp")


def test_a_relative_band_is_scale_free_and_says_so() -> None:
    """A percentage of a quantity is a percentage of it in any unit, so `%` needs
    no unit check - and asserting that is what stops the check being copied onto
    the branch where it would be wrong. 3% of 0.620 kW and 3% of 620 W are one
    band, written twice."""
    watts = JINKO.resolve(620.0, nominal_unit="W")
    kilowatts = JINKO.resolve(0.620, nominal_unit="kW")
    assert watts == pytest.approx((620.0, 638.6))
    assert kilowatts == pytest.approx((0.620, 0.6386))
    assert JINKO.resolve(620.0, nominal_unit=None) == watts


def test_a_declared_band_conflict_reaches_the_comparison_with_its_unit() -> None:
    """Wiring, not just the method: `_compare_numbers` has to hand the
    candidates' own units to `agrees`, or the check sits in `DeclaredBand` and
    nothing ever asks it. This is the second half of FN-3 - the first fix made
    the method able to refuse, and only this makes it get asked."""
    tolerance = FieldTolerance(rule=ToleranceRule.DECLARED_BAND)

    def candidate(value: float, unit: str, doc: str) -> ConflictCandidate:
        return ConflictCandidate(
            value=value,
            unit=unit,
            source_tier=SourceTier.SYSTEM_OF_RECORD,
            source_ref=SourceRef(document_id=doc),
            confidence=0.9,
        )

    same = values_conflict(
        candidate(650.0, "W", "a"),
        candidate(656.0, "W", "b"),
        tolerance=tolerance,
        band_a=TRINA,
        band_b=TRINA,
    )
    assert same.conflicts, "[650,655] and [656,661] are disjoint"

    with pytest.raises(DeclaredBandUnitError):
        values_conflict(
            candidate(0.650, "kW", "a"),
            candidate(5.0, "kW", "b"),
            tolerance=tolerance,
            band_a=TRINA,
            band_b=TRINA,
        )


def test_an_absolute_band_without_a_unit_is_rejected() -> None:
    """`0 ~ +5` is not a quantity. The canonical field knows its unit, so an
    extractor emitting the band has one to write."""
    with pytest.raises(ValidationError):
        DeclaredBand(low=0.0, high=5.0, kind=ToleranceKind.ABSOLUTE)


def test_a_relative_band_may_not_carry_a_physical_unit() -> None:
    """`3` with unit `W` alongside kind=relative is the extractor confusing the
    two conventions, which is exactly the defect #2 reports."""
    with pytest.raises(ValidationError):
        DeclaredBand(low=0.0, high=3.0, kind=ToleranceKind.RELATIVE, unit="W")
    assert DeclaredBand(low=0.0, high=3.0, kind=ToleranceKind.RELATIVE, unit="%").unit == "%"


def test_an_inverted_band_is_rejected() -> None:
    """`0 ~ +5` read backwards produces an empty interval that agrees with
    nothing, including itself — every comparison would become a conflict."""
    with pytest.raises(ValidationError):
        DeclaredBand(low=5.0, high=0.0, kind=ToleranceKind.ABSOLUTE, unit="W")


def test_a_non_finite_bound_is_rejected() -> None:
    """A NaN bound compares false against everything, so `low <= other_high`
    fails both ways and the band would silently declare every value in
    agreement — the failure direction that cannot be reviewed."""
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            DeclaredBand(low=0.0, high=bad, kind=ToleranceKind.ABSOLUTE, unit="W")


def test_a_band_is_reflexive() -> None:
    """A value must agree with itself, or `power_tolerance` could never round-trip
    through the store without raising a conflict against its own prior state."""
    for band in (TRINA, JINKO, CANADIAN):
        for nominal in (650.0, -0.29, 0.0):
            assert band.agrees(nominal, band, nominal, nominal_unit=band.unit, other_unit=band.unit)


def test_a_band_survives_a_store_round_trip() -> None:
    """FR-OUT-06: composition is a pure function of the store, so a band that
    reloads differently moves the conflict queue."""
    revived = DeclaredBand.model_validate_json(JINKO.model_dump_json())
    assert revived == JINKO
    assert revived.resolve(620.0, nominal_unit="W") == JINKO.resolve(620.0, nominal_unit="W")


def test_a_band_is_a_field_value_not_a_second_key_on_the_field() -> None:
    """The contract types `power_tolerance` *as* a DeclaredBand rather than
    hanging a `declared_tolerance` off every field (issue #2 proposed the
    latter). It therefore carries its own provenance and conflict state, and
    there is no second copy to keep in sync."""
    field = CanonicalField(
        value=TRINA,
        unit=None,
        source_tier=SourceTier.SYSTEM_OF_RECORD,
        source_ref=SourceRef(document_id="trina-neg21c"),
        confidence=0.95,
    )
    assert isinstance(field.value, DeclaredBand)
    assert "declared_tolerance" not in CanonicalField.model_fields


def test_two_bands_order_canonically_as_candidates() -> None:
    """`_ordering_key` reprs the value, so bands must order without a special
    case — otherwise two candidates differing only in their band tie, and stable
    sort leaks arrival order back into the queue (the `verbatim_value` bug)."""

    def candidate(band: DeclaredBand, doc: str) -> ConflictCandidate:
        return ConflictCandidate(
            value=band,
            unit=None,
            source_tier=SourceTier.SYSTEM_OF_RECORD,
            source_ref=SourceRef(document_id=doc),
            confidence=0.9,
        )

    a, b = candidate(TRINA, "datasheet"), candidate(CANADIAN, "supply-agreement")
    forward = comparison_pairs([a, b], field_name="power_tolerance")
    backward = comparison_pairs([b, a], field_name="power_tolerance")
    assert len(forward) == 1
    assert [(x.value, y.value) for x, y in forward] == [(x.value, y.value) for x, y in backward]


def test_resolving_a_band_cannot_manufacture_an_infinite_range() -> None:
    """`_reject_non_finite` guards the stored bounds because a non-finite band
    agrees with everything. The arithmetic in `resolve` reaches the same place by
    another route: a ±3% band on a 1e308 nominal overflows to (-inf, inf), and
    `agrees` then returns True against any value at all."""
    band = DeclaredBand(low=-3.0, high=3.0, kind=ToleranceKind.RELATIVE)
    with pytest.raises(ValueError):
        band.resolve(1e308, nominal_unit="W")
    wide = DeclaredBand(low=-1e308, high=1e308, kind=ToleranceKind.ABSOLUTE, unit="W")
    with pytest.raises(ValueError):
        wide.resolve(1e308, nominal_unit="W")
    # 1e300 does *not* overflow that band — checked, so the case above is the
    # boundary rather than a guess at one.
    assert all(map(math.isfinite, wide.resolve(1e300, nominal_unit="W")))
    # Ordinary magnitudes are untouched.
    assert band.resolve(650.0, nominal_unit="W") == pytest.approx((630.5, 669.5))


def test_declared_band_agrees_across_kelvin_and_celsius_rate_aliases() -> None:
    """D-5: `%/degC`, `%/K`, and `%/°C` are spellings of the same unit interval.
    `_comparable_unit` in `schema/field.py` must fold them just like
    `conflict_hitl._normalise_unit` does, and `values_conflict` on
    `nameplate_power` must honour an explicit `band_a`/`band_b` without requiring
    the caller to pass `FIELD_TOLERANCES['power_tolerance']`."""
    from procurement_agent.services.conflict_hitl import tolerance_for, values_conflict

    coeff_band = DeclaredBand(low=-0.02, high=0.02, kind=ToleranceKind.ABSOLUTE, unit="%/degC")
    assert coeff_band.agrees(-0.29, None, -0.30, nominal_unit="%/degC", other_unit="%/K")

    a = ConflictCandidate(
        value=650.0,
        unit="W",
        source_tier=SourceTier.SYSTEM_OF_RECORD,
        source_ref=SourceRef(document_id="datasheet"),
        confidence=0.95,
    )
    b = ConflictCandidate(
        value=654.0,
        unit="W",
        source_tier=SourceTier.SYSTEM_OF_RECORD,
        source_ref=SourceRef(document_id="contract"),
        confidence=0.95,
    )
    verdict = values_conflict(
        a,
        b,
        field_name="nameplate_power",
        tolerance=tolerance_for("nameplate_power"),
        band_a=TRINA,
    )
    assert not verdict.conflicts

    # And comparing two `DeclaredBand` candidates directly on `power_tolerance`
    # must never embed `<ToleranceKind...>` enum reprs in `verdict.reason`.
    band_cand_a = ConflictCandidate(
        value=TRINA,
        unit=None,
        source_tier=SourceTier.SYSTEM_OF_RECORD,
        source_ref=SourceRef(document_id="d1"),
        confidence=0.9,
    )
    band_cand_b = ConflictCandidate(
        value=CANADIAN,
        unit=None,
        source_tier=SourceTier.SYSTEM_OF_RECORD,
        source_ref=SourceRef(document_id="d2"),
        confidence=0.9,
    )
    band_verdict = values_conflict(
        band_cand_a,
        band_cand_b,
        field_name="power_tolerance",
        tolerance=tolerance_for("power_tolerance"),
    )
    assert "<" not in band_verdict.reason
