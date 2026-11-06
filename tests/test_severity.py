"""Whether a detected conflict is worth holding the workbook for — open-decisions.md
section 1.

`Severity` (schema/enums.py) and the compose gate (`orchestrator.compose_gate_blocks`)
both already existed; nothing assigned a severity to a detected conflict, so the gate
was inert regardless of what it was fed. This is the assignment rule closing that gap.

The load-bearing invariant (open-decisions.md:49): severity is a **pure function of
`(field_name, conflict_class, condition_group, candidate_set)`** — no clock, no
reviewer identity, no queue state. `test_module_source_never_touches_a_clock_random_
source_or_reviewer_identity` below is the test that actually enforces it, rather than
one that merely fails to violate it by omission.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import re

import pytest

from procurement_agent.schema import (
    Condition,
    ConflictCandidate,
    ConflictClass,
    Severity,
    SourceRef,
    SourceTier,
    ToleranceRule,
)
from procurement_agent.services.confidence import TIER_A_EXCLUSIONS
from procurement_agent.services.conflict_hitl import severity as severity_module
from procurement_agent.services.conflict_hitl.severity import (
    CRITICALITY,
    DEFAULT_CRITICALITY,
    TIER_A_FIELDS,
    _gross_divergence,
    _reconciles,
    _unit_mismatch_reconciles,
    assign_severity,
    criticality_for,
    looks_tier_a,
)
from procurement_agent.services.conflict_hitl.tolerance import FieldTolerance, tolerance_for


def _c(
    value: object,
    *,
    unit: str | None = "Wp",
    tier: SourceTier = SourceTier.SYSTEM_OF_RECORD,
    doc: str = "doc-a",
    condition: Condition | None = None,
) -> ConflictCandidate:
    return ConflictCandidate(
        value=value,
        unit=unit,
        condition=condition or Condition(),
        source_tier=tier,
        source_ref=SourceRef(document_id=doc),
        confidence=0.9,
    )


def _contract_keys() -> set[str]:
    """Mirrors `test_values_conflict._contract_keys` exactly: the criticality
    table has to be keyed the same way the tolerance table is, on pain of
    repeating the invented-name defect `test_every_tolerance_key_is_a_contract_key`
    exists to catch (19 of 20 rows silently unreachable)."""
    contract = pathlib.Path(__file__).parent.parent / (
        "specs/001-procurement-agent/contracts/canonical-parameters.md"
    )
    text = contract.read_text(encoding="utf-8")
    keys = {m.group(1) for m in re.finditer(r"^\|\s*`([a-z0-9_]+)`\s*\|", text, re.MULTILINE)}
    assert len(keys) > 50, "the contract's parameter tables did not parse"
    return keys


# --- keyed on the frozen contract, checked in both directions -----------------


def test_every_criticality_key_is_a_contract_key() -> None:
    """The defect this exists to prevent: a table keyed on invented names falls
    through to a default for every real field and nobody notices, because the
    fallback still returns *a* severity."""
    unknown = set(CRITICALITY) - _contract_keys()
    assert not unknown, (
        f"criticality rows keyed on names the frozen contract does not have: {unknown}"
    )


def test_every_contract_key_has_a_criticality() -> None:
    """The reverse direction. A tolerance row that is merely absent falls back to
    `DEFAULT_TOLERANCE`, which is a safe direction (exact, not lenient) - but a
    forgotten criticality row falls back to `DEFAULT_CRITICALITY`, and the only
    way to know a field was actually classified rather than caught by the
    fallback is to check every contract key is present here explicitly."""
    missing = _contract_keys() - set(CRITICALITY)
    assert not missing, f"contract keys with no assigned criticality: {missing}"


def test_tier_a_fields_are_contract_keys() -> None:
    unknown = TIER_A_FIELDS - _contract_keys()
    assert not unknown, f"TIER_A_FIELDS names a field the contract does not have: {unknown}"


def test_no_tier_a_field_is_missing_from_the_set() -> None:
    """The direction the test above cannot cover, and the one that costs.

    A subset check catches a typo. It cannot catch an *omission*, and an omitted
    field simply does not get the floor. Three were missing on that basis:
    `material_assistance_cost_ratio` (FEOC), `degradation_year_1` and
    `degradation_annual` (warranty terms - the criticality table's own comment
    calls them the guaranteed curve underwriting the performance warranty).
    """
    missing = {
        key
        for key in _contract_keys()
        if looks_tier_a(key) and key not in TIER_A_EXCLUSIONS and key not in TIER_A_FIELDS
    }
    assert not missing, (
        f"contract fields in a D-3 Tier A category but unfloored: {sorted(missing)}. "
        "Add them to TIER_A_FIELDS, or record a reason in TIER_A_EXCLUSIONS."
    )


def test_the_patterns_actually_recognise_the_floored_fields() -> None:
    """Otherwise the check above passes vacuously: it asserts an empty set, so
    breaking the detector makes it succeed rather than fail."""
    unrecognised = {key for key in TIER_A_FIELDS if not looks_tier_a(key)}
    assert not unrecognised, (
        f"floored fields the D-3 category patterns cannot find: {sorted(unrecognised)}"
    )


def test_the_exclusion_list_is_not_a_way_to_lose_a_field() -> None:
    assert set(TIER_A_EXCLUSIONS) <= _contract_keys()
    assert all(reason.strip() for reason in TIER_A_EXCLUSIONS.values())


@pytest.mark.parametrize(
    ("field_name", "unit_a", "value_a", "unit_b", "value_b"),
    [
        ("degradation_year_1", "%", 2.0, "fraction", 0.02),
        ("degradation_annual", "%", 0.55, "fraction", 0.0055),
        ("material_assistance_cost_ratio", "%", 45.0, "fraction", 0.45),
        ("country_of_origin", "%", 45.0, "fraction", 0.45),
    ],
)
def test_a_tier_a_conflict_never_falls_below_the_compose_gate(
    field_name: str, unit_a: str, value_a: float, unit_b: str, value_b: float
) -> None:
    """The concrete cost of the omission, pinned as behaviour rather than as
    set membership.

    `config.compose_gate_threshold` defaults to MEDIUM and the gate blocks
    *strictly above* it, so a `%`-vs-`fraction` extraction artefact scoring LOW
    means the workbook ships with an unresolved contractual conflict.
    """
    pair = [_c(value_a, unit=unit_a), _c(value_b, unit=unit_b)]
    assigned = assign_severity(field_name, ConflictClass.UNIT_NORMALIZATION, pair, pair)
    assert assigned >= Severity.HIGH, (
        f"{field_name} scored {assigned.name}; the -2 unit-reconciliation "
        "discount took a Tier A field below the gate"
    )


# --- base severity: the criticality-class lookup -------------------------------


#: Every contract key's criticality class, pinned independently of the module
#: under test.
#:
#: **Why all 124 and not a representative sample.** The previous version of this
#: file listed 36 hand-picked keys in a `parametrize`, and mutation testing
#: measured what that bought: bumping each `CRITICALITY` value one step and
#: re-running the whole suite, **81 of 120 one-step mutants survived** - the
#: table's values were, for two thirds of the contract, asserted by nothing.
#: Both membership directions were already checked
#: (`test_every_criticality_key_is_a_contract_key` and its reverse), so a typo in
#: a *key* was caught while a wrong *value* was not, which is the half that
#: decides whether the compose gate blocks.
#:
#: This is a second, independent transcription rather than a derivation from
#: `CRITICALITY`: a test that reads the table it is checking asserts only that
#: the table equals itself. Changing a classification therefore takes two edits,
#: which is the intended cost - `DEFAULT_CRITICALITY`'s docstring calls the base
#: class a safety interlock, and an interlock nobody has to look twice at to
#: move is not one.
EXPECTED_CRITICALITY: dict[str, Severity] = {
    # --- Attestation / eligibility -> CRITICAL (12 keys) ---
    "certifications": Severity.CRITICAL,
    "fire_safety_certifications": Severity.CRITICAL,
    "cell_certification": Severity.CRITICAL,
    "pcs_certification": Severity.CRITICAL,
    "ul_listing": Severity.CRITICAL,
    "domestic_content_status": Severity.CRITICAL,
    "domestic_content_percentage": Severity.CRITICAL,
    "baba_status": Severity.CRITICAL,
    "baba_certification_ref": Severity.CRITICAL,
    "feoc_pfe_status": Severity.CRITICAL,
    "country_of_origin": Severity.CRITICAL,
    "ercot_compliance_items": Severity.CRITICAL,
    # --- Commercial (+ the two named exceptions) -> HIGH (14 keys) ---
    "price_per_watt_dc": Severity.HIGH,
    "price_per_watt_ac": Severity.HIGH,
    "price_per_metre": Severity.HIGH,
    "product_warranty_years": Severity.HIGH,
    "performance_warranty_years": Severity.HIGH,
    "performance_warranty_end_output": Severity.HIGH,
    "warranty_years": Severity.HIGH,
    "corrosion_warranty_years": Severity.HIGH,
    "degradation_warranty_years": Severity.HIGH,
    "degradation_warranty_cycles": Severity.HIGH,
    "material_assistance_cost_ratio": Severity.HIGH,
    "degradation_year_1": Severity.HIGH,
    "degradation_annual": Severity.HIGH,
    "vector_group": Severity.HIGH,
    # --- Decision-driving performance -> MEDIUM (40 keys) ---
    "nameplate_power": Severity.MEDIUM,
    "power_tolerance": Severity.MEDIUM,
    "module_efficiency": Severity.MEDIUM,
    "stc_rating": Severity.MEDIUM,
    "nmot_rating": Severity.MEDIUM,
    "temp_coeff_pmax": Severity.MEDIUM,
    "temp_coeff_voc": Severity.MEDIUM,
    "temp_coeff_isc": Severity.MEDIUM,
    "rated_ac_power": Severity.MEDIUM,
    "rated_ac_power_temp": Severity.MEDIUM,
    "max_efficiency": Severity.MEDIUM,
    "cec_efficiency": Severity.MEDIUM,
    "trd_percent": Severity.MEDIUM,
    "trd_limit_applied": Severity.MEDIUM,
    "reactive_capability_at_zero_output": Severity.MEDIUM,
    "ride_through_coordination": Severity.MEDIUM,
    "automatic_generation_control": Severity.MEDIUM,
    "ercot_telemetry": Severity.MEDIUM,
    "pmu_support": Severity.MEDIUM,
    "rating_mva": Severity.MEDIUM,
    "rating_mva_by_cooling": Severity.MEDIUM,
    "impedance_percent": Severity.MEDIUM,
    "no_load_loss": Severity.MEDIUM,
    "load_loss": Severity.MEDIUM,
    "efficiency": Severity.MEDIUM,
    "ampacity": Severity.MEDIUM,
    "usable_energy_per_container": Severity.MEDIUM,
    "nameplate_energy_per_container": Severity.MEDIUM,
    "power_rating": Severity.MEDIUM,
    "c_rate": Severity.MEDIUM,
    "round_trip_efficiency": Severity.MEDIUM,
    "cycle_life": Severity.MEDIUM,
    "energy_density": Severity.MEDIUM,
    "footprint_area": Severity.MEDIUM,
    "design_wind_speed": Severity.MEDIUM,
    "stow_wind_speed": Severity.MEDIUM,
    "ground_coverage_ratio": Severity.MEDIUM,
    "backtracking_yield_gain": Severity.MEDIUM,
    "tracking_range": Severity.MEDIUM,
    "bearing_gear_l10_years": Severity.MEDIUM,
    # --- Secondary comparison -> LOW (48 keys) ---
    "mppt_count": Severity.LOW,
    "mppt_voltage_min": Severity.LOW,
    "mppt_voltage_max": Severity.LOW,
    "harmonic_spectrum": Severity.LOW,
    "thd_percent": Severity.LOW,
    "k_factor": Severity.LOW,
    "filtering_provisions": Severity.LOW,
    "dc_injection": Severity.LOW,
    "flicker_pst": Severity.LOW,
    "flicker_plt": Severity.LOW,
    "communication_protocols": Severity.LOW,
    "protocols": Severity.LOW,
    "cybersecurity_standards": Severity.LOW,
    "inverter_integration": Severity.LOW,
    "bess_integration": Severity.LOW,
    "ride_through_standards": Severity.LOW,
    "enclosure_rating": Severity.LOW,
    "conductor_size": Severity.LOW,
    "conductor_material": Severity.LOW,
    "conductor_area": Severity.LOW,
    "insulation_type": Severity.LOW,
    "shielding": Severity.LOW,
    "standards": Severity.LOW,
    "cooling_classes": Severity.LOW,
    "seismic_qualification": Severity.LOW,
    "max_system_voltage": Severity.LOW,
    "max_dc_voltage": Severity.LOW,
    "voltage_class": Severity.LOW,
    "voltage_hv": Severity.LOW,
    "voltage_lv": Severity.LOW,
    "load_factor": Severity.LOW,
    "input_count": Severity.LOW,
    "fuse_rating": Severity.LOW,
    "continuous_current": Severity.LOW,
    "string_monitoring": Severity.LOW,
    "surge_protection": Severity.LOW,
    "disconnect_type": Severity.LOW,
    "chemistry": Severity.LOW,
    "thermal_management": Severity.LOW,
    "augmentation_plan": Severity.LOW,
    "cell_technology": Severity.LOW,
    "bifaciality_factor": Severity.LOW,
    "bifaciality_tolerance": Severity.LOW,
    "topology": Severity.LOW,
    "configuration": Severity.LOW,
    "modules_per_row": Severity.LOW,
    "galvanization_spec": Severity.LOW,
    "foundations_per_mw": Severity.LOW,
    # --- Descriptive -> INFORMATIONAL (10 keys) ---
    "supplier": Severity.INFORMATIONAL,
    "supplier_verbatim": Severity.INFORMATIONAL,
    "model": Severity.INFORMATIONAL,
    "model_verbatim": Severity.INFORMATIONAL,
    "component_category": Severity.INFORMATIONAL,
    "datasheet_revision": Severity.INFORMATIONAL,
    "datasheet_date": Severity.INFORMATIONAL,
    "stow_strategy": Severity.INFORMATIONAL,
    "support_terms": Severity.INFORMATIONAL,
    "plant_controller_model": Severity.INFORMATIONAL,
}


def test_every_criticality_value_is_pinned() -> None:
    """The whole table, key by key, in both directions at once.

    A one-step mutation of any single row now fails here. Reported as three
    separate diffs rather than one `assert a == b`, because a 124-entry dict
    comparison prints as an unreadable wall on failure and the useful question is
    always which rows moved.
    """
    missing = sorted(set(EXPECTED_CRITICALITY) - set(CRITICALITY))
    extra = sorted(set(CRITICALITY) - set(EXPECTED_CRITICALITY))
    changed = sorted(
        (key, EXPECTED_CRITICALITY[key].name, CRITICALITY[key].name)
        for key in set(EXPECTED_CRITICALITY) & set(CRITICALITY)
        if CRITICALITY[key] is not EXPECTED_CRITICALITY[key]
    )
    assert not missing, f"contract keys dropped from CRITICALITY: {missing}"
    assert not extra, f"CRITICALITY rows this test does not pin: {extra}"
    assert not changed, (
        "criticality class changed for (key, pinned, actual): "
        f"{changed}. If the reclassification is intended, update "
        "EXPECTED_CRITICALITY above and say why in the module comment."
    )


def test_the_pinned_table_covers_the_whole_contract() -> None:
    """Guards the guard: `test_every_criticality_value_is_pinned` compares two
    dicts, so deleting a row from *both* would leave it green. This ties the
    pinned table to the frozen contract itself, the same way
    `test_every_contract_key_has_a_criticality` ties the real one."""
    assert set(EXPECTED_CRITICALITY) == _contract_keys()


def test_vector_group_is_high_not_descriptive() -> None:
    """D-6a: Dyn1 and Dyn11 are 60 degrees apart and cannot be paralleled - a
    buildability field, not the string label it resembles (open-decisions.md #1)."""
    assert criticality_for("vector_group") is Severity.HIGH


def test_unmapped_field_defaults_to_critical_not_low() -> None:
    """`ConflictQueueEntry.severity` is required specifically because a default
    at or below the gate threshold would make a forgotten severity silently
    unable to block. The same reasoning applies one level down: a criticality
    row nobody has written yet must not silently produce a severity the gate
    would wave through."""
    assert criticality_for("a_field_nobody_has_classified_yet") is Severity.CRITICAL
    assert DEFAULT_CRITICALITY is Severity.CRITICAL


# --- assign_severity: the base class carries through with no modifiers --------


def test_assign_severity_returns_the_base_class_with_no_modifiers() -> None:
    pair = [_c(650.0, doc="doc-a"), _c(651.0, doc="doc-b")]
    result = assign_severity("nameplate_power", ConflictClass.INTRA_DOCUMENT, pair, pair)
    assert result is Severity.MEDIUM


# --- the four bounded modifiers -------------------------------------------------


def test_gross_divergence_adds_one() -> None:
    """+1: divergence >= 10x the D-2 tolerance - usually a decimal-comma error,
    D-5's highest-risk trap (open-decisions.md #1). nameplate_power's tolerance
    is +/-1 W; 650 vs 6.5 is two orders of magnitude apart."""
    pair = [_c(650.0, doc="doc-a"), _c(6.5, doc="doc-b")]
    result = assign_severity("nameplate_power", ConflictClass.INTRA_DOCUMENT, pair, pair)
    assert result is Severity.HIGH  # MEDIUM(2) + 1


def test_small_divergence_does_not_add() -> None:
    pair = [_c(650.0, doc="doc-a"), _c(651.0, doc="doc-b")]
    result = assign_severity("nameplate_power", ConflictClass.INTRA_DOCUMENT, pair, pair)
    assert result is Severity.MEDIUM


def test_gross_divergence_boundary_is_at_exactly_ten_times_tolerance() -> None:
    """Pinned at the boundary, not deep in the middle of it: a divergence of
    exactly 10 W against nameplate_power's +/-1 W band must trigger (`>=`, not
    `>`), and 9.9 W must not - catching an off-by-one on the threshold or the
    comparison operator that a grossly-oversized example would not notice."""
    at_boundary = [_c(650.0, doc="doc-a"), _c(660.0, doc="doc-b")]
    just_under = [_c(650.0, doc="doc-a"), _c(659.9, doc="doc-b")]
    assert (
        assign_severity("nameplate_power", ConflictClass.INTRA_DOCUMENT, at_boundary, at_boundary)
        is Severity.HIGH
    )
    assert (
        assign_severity("nameplate_power", ConflictClass.INTRA_DOCUMENT, just_under, just_under)
        is Severity.MEDIUM
    )


def test_gross_divergence_scales_a_relative_band_by_magnitude() -> None:
    """rated_ac_power's D-2 tolerance is RELATIVE (0.01), so the check has to
    scale that fraction by the compared magnitude before multiplying by ten -
    not apply the bare 0.01 as if it were an absolute band in kVA. 10050 vs
    10000 is 0.5%, nowhere near even the field's own 1% band, let alone ten
    times it; an implementation that forgot to scale would see a fixed 0.01 kVA
    band and misread this as grossly divergent."""
    pair = [_c(10000.0, unit="kVA", doc="doc-a"), _c(10050.0, unit="kVA", doc="doc-b")]
    result = assign_severity("rated_ac_power", ConflictClass.INTRA_DOCUMENT, pair, pair)
    assert result is Severity.MEDIUM


def test_inter_document_both_system_of_record_adds_one() -> None:
    """+1: inter-document where BOTH sides are system_of_record - two ingested
    authorities disagreeing is worse than a contract disagreeing with a web
    supplement, which FR-WEB-03 already treats as lower-tier."""
    pair = [
        _c(650.0, tier=SourceTier.SYSTEM_OF_RECORD, doc="doc-a"),
        _c(651.0, tier=SourceTier.SYSTEM_OF_RECORD, doc="doc-b"),
    ]
    result = assign_severity("nameplate_power", ConflictClass.INTER_DOCUMENT, pair, pair)
    assert result is Severity.HIGH


def test_record_vs_web_does_not_get_the_inter_document_modifier() -> None:
    pair = [
        _c(650.0, tier=SourceTier.SYSTEM_OF_RECORD, doc="doc-a"),
        _c(651.0, tier=SourceTier.WEB_SUPPLEMENT, doc="doc-b"),
    ]
    result = assign_severity("nameplate_power", ConflictClass.RECORD_VS_WEB, pair, pair)
    assert result is Severity.MEDIUM


def test_inter_document_with_one_web_supplement_does_not_add() -> None:
    """Both sides have to be system_of_record; INTER_DOCUMENT alone is not enough."""
    pair = [
        _c(650.0, tier=SourceTier.SYSTEM_OF_RECORD, doc="doc-a"),
        _c(651.0, tier=SourceTier.WEB_SUPPLEMENT, doc="doc-b"),
    ]
    result = assign_severity("nameplate_power", ConflictClass.INTER_DOCUMENT, pair, pair)
    assert result is Severity.MEDIUM


def test_three_distinct_values_in_a_group_adds_one_matcher_failure() -> None:
    """+1: >=3 distinct values in one group is a matcher failure, not a data
    disagreement. `condition_group` carries the wider comparable set the pair
    was drawn from; `candidate_set` is the pair itself."""
    group = [_c(650.0, doc="doc-a"), _c(651.0, doc="doc-b"), _c(700.0, doc="doc-c")]
    pair = group[:2]
    result = assign_severity("nameplate_power", ConflictClass.INTRA_DOCUMENT, group, pair)
    assert result is Severity.HIGH


def test_two_distinct_values_in_a_group_does_not_add() -> None:
    group = [_c(650.0, doc="doc-a"), _c(651.0, doc="doc-b")]
    result = assign_severity("nameplate_power", ConflictClass.INTRA_DOCUMENT, group, group)
    assert result is Severity.MEDIUM


def test_repeated_values_in_a_group_do_not_inflate_the_distinct_count() -> None:
    """Three candidates, two of which agree, is two distinct values - not three -
    so the matcher-failure modifier must not fire. 652 is close enough to 650
    that it also does not trip the separate gross-divergence modifier, so this
    test isolates the distinct-value count."""
    group = [_c(650.0, doc="doc-a"), _c(650.0, doc="doc-b"), _c(652.0, doc="doc-c")]
    pair = [group[0], group[2]]
    result = assign_severity("nameplate_power", ConflictClass.INTRA_DOCUMENT, group, pair)
    assert result is Severity.MEDIUM


def test_unit_mismatch_that_reconciles_after_conversion_subtracts_two() -> None:
    """-2: a unit mismatch that reconciles after conversion is an extraction
    defect - fix the normaliser, do not hold the workbook. 650 W and 0.65 kW are
    the same value."""
    pair = [_c(650.0, unit="W", doc="doc-a"), _c(0.65, unit="kW", doc="doc-b")]
    result = assign_severity("nameplate_power", ConflictClass.UNIT_NORMALIZATION, pair, pair)
    assert result is Severity.INFORMATIONAL  # MEDIUM(2) - 2 = 0


def test_unit_mismatch_that_does_not_reconcile_keeps_full_severity() -> None:
    """652 W != 650 W once converted: the units differ *and*, even accounting
    for that, the physical quantity still differs by more than nameplate_power's
    own +/-1 W band - a real disagreement wearing a unit-mismatch conflict_class,
    not an extraction defect - so no discount. Chosen close enough that it also
    does not trip the separate gross-divergence modifier, to isolate this one."""
    pair = [_c(650.0, unit="W", doc="doc-a"), _c(0.652, unit="kW", doc="doc-b")]
    result = assign_severity("nameplate_power", ConflictClass.UNIT_NORMALIZATION, pair, pair)
    assert result is Severity.MEDIUM


def test_unit_mismatch_reconciles_for_an_exact_tolerance_field() -> None:
    """`max_system_voltage`'s D-2 rule is EXACT - discrete 1000/1500 V, no D-2
    magnitude - which is also true of *every* Tier A and CRITICAL field: none of
    pricing, warranty-term counts, domestic content, BABA/FEOC or certifications
    has a D-2 numeric row, so all of them fall back to EXACT too. Pinned on a
    non-floored EXACT field so this reconciliation path is verified on its own,
    not masked by a floor that would hold the result at the same value whether
    or not the discount actually fired. 1500 V and 1.5 kV are the same value.

    Named a "fallback" while `_band` had first refusal; the epsilon test is now
    the only test `_reconciles` runs, for EXACT and banded rows alike.
    """
    pair = [_c(1500.0, unit="V", doc="doc-a"), _c(1.5, unit="kV", doc="doc-b")]
    result = assign_severity("max_system_voltage", ConflictClass.UNIT_NORMALIZATION, pair, pair)
    assert result is Severity.INFORMATIONAL  # LOW(1) - 2, clamped to 0


def test_unit_mismatch_reconciles_requires_every_pair_not_just_one() -> None:
    """White-box, on the internal helper directly: a three-candidate set where
    two pairs reconcile after conversion but the third genuinely disagrees must
    not read as reconciled as a whole - a mixed signal is not evidence the
    entire conflict is just a units artefact, and this policy has no clean
    black-box angle through `assign_severity` alone, since a third distinct
    candidate also trips the separate matcher-failure modifier."""
    reconciling = [_c(650.0, unit="W", doc="doc-a"), _c(0.65, unit="kW", doc="doc-b")]
    disagreeing = _c(700.0, unit="W", doc="doc-c")
    assert not _unit_mismatch_reconciles(
        "nameplate_power", ConflictClass.UNIT_NORMALIZATION, [*reconciling, disagreeing]
    )
    assert _unit_mismatch_reconciles(
        "nameplate_power", ConflictClass.UNIT_NORMALIZATION, reconciling
    )


@pytest.mark.parametrize(
    ("field_name", "value_a", "unit_a", "value_b", "unit_b", "apart"),
    [
        # ABSOLUTE: nameplate_power's D-2 band is +/-1 Wp; 0.5 W inside it.
        ("nameplate_power", 650.0, "W", 0.6505, "kW", "0.5 W"),
        # RELATIVE: rated_ac_power's band is 1% of the compared value; 0.9% inside it.
        ("rated_ac_power", 100.0, "kVA", 100900.0, "VA", "0.9%"),
        # ONE_SIDED: no_load_loss's band is 15%; 14.85% inside it.
        ("no_load_loss", 100.0, "kW", 114850.0, "W", "14.85%"),
    ],
)
def test_a_disagreement_inside_the_d2_band_is_not_a_unit_reconciliation(
    field_name: str, value_a: float, unit_a: str, value_b: float, unit_b: str, apart: str
) -> None:
    """`_reconciles` asks whether this is the **same number** printed in a
    different unit - its own docstring says so, and adds "not whether two
    independent measurements are close enough to agree". It did exactly the
    latter for every non-EXACT rule: `_band` handed back the field's full D-2
    *disagreement* tolerance, so the `math.isclose` representation-noise test
    the docstring describes was unreachable for any banded row.

    A pair that is **both** a unit error and a real disagreement then took the
    full -2 "just fix the normaliser" discount. `no_load_loss` at 100 kW against
    114850 W is 14.85% apart and scored INFORMATIONAL; the identical pair classed
    INTER_DOCUMENT scores HIGH. The module's own docstring calls understating
    "the unsafe direction here".

    Bounded today and deliberately pinned as such by
    `test_the_only_high_or_critical_banded_fields_are_floored` below: every
    banded field except the two Tier A degradation rows is already MEDIUM, so
    this mis-graded the review queue rather than unblocking composition. It
    would unblock it the moment the compose-gate threshold drops or a HIGH field
    gains a D-2 band.
    """
    pair = [_c(value_a, unit=unit_a, doc="doc-a"), _c(value_b, unit=unit_b, doc="doc-b")]
    assert not _unit_mismatch_reconciles(field_name, ConflictClass.UNIT_NORMALIZATION, pair), (
        f"{field_name}: values {apart} apart are not one number in two units"
    )
    assert (
        assign_severity(field_name, ConflictClass.UNIT_NORMALIZATION, pair, pair) is Severity.MEDIUM
    )


@pytest.mark.parametrize(
    ("field_name", "value_a", "unit_a", "value_b", "unit_b"),
    [
        ("nameplate_power", 650.0, "W", 0.65, "kW"),
        ("rated_ac_power", 100.0, "kVA", 100000.0, "VA"),
        ("no_load_loss", 100.0, "kW", 100000.0, "W"),
    ],
)
def test_a_banded_field_still_reconciles_a_genuine_unit_only_mismatch(
    field_name: str, value_a: float, unit_a: str, value_b: float, unit_b: str
) -> None:
    """The narrowing above must not become "banded fields never reconcile".

    Every pair here *is* one number printed in two units, so the -2 still fires
    and each field drops MEDIUM(2) - 2 to INFORMATIONAL. Without this the fix
    would be indistinguishable from deleting the modifier for the 19 banded
    contract keys, which is the mirror defect: overstating a pure extraction
    artefact holds the workbook for a normaliser bug.
    """
    pair = [_c(value_a, unit=unit_a, doc="doc-a"), _c(value_b, unit=unit_b, doc="doc-b")]
    assert _unit_mismatch_reconciles(field_name, ConflictClass.UNIT_NORMALIZATION, pair)
    assert (
        assign_severity(field_name, ConflictClass.UNIT_NORMALIZATION, pair, pair)
        is Severity.INFORMATIONAL
    )


def test_a_never_compare_field_never_reconciles() -> None:
    """`NEVER_COMPARE` marks a field whose name is shared by genuinely different
    physical quantities, so "these convert to the same number" is the
    coincidence the rule exists to distrust, not evidence of one reading.

    White-box on `_reconciles` because `NEVER_COMPARABLE` is empty today -
    deliberately, and kept as a live extension point with its own test in
    `tolerance.py`. The band comparison used to refuse these rows as a side
    effect of their carrying no magnitude; narrowing the test to representation
    noise removes that side effect, so the refusal has to be stated. Without it
    the fix would hand a -2 discount to exactly the fields the table says must
    never be compared.
    """
    never = FieldTolerance(rule=ToleranceRule.NEVER_COMPARE, basis="test")
    pair = (_c(100.0, unit="kVA", doc="doc-a"), _c(100000.0, unit="VA", doc="doc-b"))
    assert _reconciles(*pair, tolerance_for("rated_ac_power")), (
        "the same pair reconciles under an ordinary rule, so the refusal below "
        "is the NEVER_COMPARE guard and not the unit table declining"
    )
    assert not _reconciles(*pair, never)


def test_the_only_high_or_critical_banded_fields_are_floored() -> None:
    """The bound that made the defect above a mis-grading rather than a hole in
    the compose gate, asserted rather than asserted-in-prose.

    A HIGH or CRITICAL field carrying a numeric D-2 band is the configuration
    where a wrongly-granted -2 reaches composition, because only there can the
    discount cross `config.compose_gate_threshold`. Today exactly two such
    fields exist and both are Tier A, so the HIGH floor catches them anyway.
    This test fails when that stops being true - a new banded row on a HIGH
    field, or a Tier A reclassification - which is the moment the narrowing
    above stops being a queue-quality fix and starts being a gate fix.
    """
    banded_high = {
        field
        for field, base in CRITICALITY.items()
        if base >= Severity.HIGH and tolerance_for(field).magnitude is not None
    }
    assert banded_high == {"degradation_year_1", "degradation_annual"}
    assert banded_high <= TIER_A_FIELDS


def test_a_bool_is_not_read_as_a_number() -> None:
    """`True == 1` in Python, so an unguarded numeric reader would treat a
    stray boolean as a nameplate_power reading of 1.0 and misread the 649 W
    gap against a real 650 W candidate as a gross divergence. `ConflictCandidate
    .value` is untyped (`object | None`), so nothing upstream of this module
    guarantees a numeric field's candidates are never a bool."""
    pair = [_c(650.0, doc="doc-a"), _c(True, unit=None, doc="doc-b")]
    result = assign_severity("nameplate_power", ConflictClass.INTRA_DOCUMENT, pair, pair)
    assert result is Severity.MEDIUM


def test_modifiers_sum_and_clamp_rather_than_multiply() -> None:
    """Stacking every +1 modifier must not overshoot the enum - summed and
    clamped, per open-decisions.md #1, not multiplied (the deleted-RPN lesson)."""
    group = [
        _c(650.0, tier=SourceTier.SYSTEM_OF_RECORD, doc="doc-a"),
        _c(6.5, tier=SourceTier.SYSTEM_OF_RECORD, doc="doc-b"),  # gross divergence
        _c(700.0, tier=SourceTier.SYSTEM_OF_RECORD, doc="doc-c"),  # 3rd distinct value
    ]
    pair = group[:2]
    result = assign_severity("nameplate_power", ConflictClass.INTER_DOCUMENT, group, pair)
    # base MEDIUM(2) + gross(1) + inter-doc-record(1) + matcher(1) = 5, clamped to 4
    assert result is Severity.CRITICAL


# --- floors nothing can lower ---------------------------------------------------


def test_tier_a_floor_holds_price_at_high_despite_the_unit_discount() -> None:
    """Any D-3 Tier A field floors at HIGH. Without the floor, HIGH(3) - 2 would
    be LOW(1); a pricing conflict must never read as merely comparison-relevant."""
    pair = [_c(0.35, unit="USD/W", doc="doc-a"), _c(350.0, unit="USD/kW", doc="doc-b")]
    result = assign_severity("price_per_watt_dc", ConflictClass.UNIT_NORMALIZATION, pair, pair)
    assert result is Severity.HIGH


def test_certification_floor_holds_domestic_content_at_critical() -> None:
    """Certification presence-vs-absence is CRITICAL, always: 'not extracted' is
    not 'not certified'. Without the floor, CRITICAL(4) - 2 would be MEDIUM(2)."""
    pair = [_c(55.0, unit="%", doc="doc-a"), _c(0.55, unit="fraction", doc="doc-b")]
    result = assign_severity(
        "domestic_content_percentage", ConflictClass.UNIT_NORMALIZATION, pair, pair
    )
    assert result is Severity.CRITICAL


def test_floors_do_not_lower_a_field_that_already_exceeds_them() -> None:
    """A floor is a minimum, not a reset: stacking positive modifiers on a
    Tier A field must still reach CRITICAL, not stick at the HIGH floor.

    The `assign_severity` assertion is deliberately not the whole test.
    `price_per_watt_dc` bases at HIGH(3) and the other two modifiers already sum
    to 5, which clamps to CRITICAL on their own - so the result is CRITICAL
    whether or not gross divergence fires, and an earlier version of this test
    annotated the 0.35/35.0 pair `# 100x gross` while passing unchanged when the
    values were replaced with 0.35/0.36/0.37. The comment documented behaviour
    the code did not have: `_gross_divergence` could not fire for this field at
    all, because `price_per_watt_dc`'s D-2 rule is EXACT and `_band` returns
    `None` there. The white-box assertions are what make the annotation
    load-bearing rather than decorative.
    """
    group = [
        _c(0.35, unit="USD/W", tier=SourceTier.SYSTEM_OF_RECORD, doc="doc-a"),
        _c(35.0, unit="USD/W", tier=SourceTier.SYSTEM_OF_RECORD, doc="doc-b"),  # 100x gross
        _c(9.0, unit="USD/W", tier=SourceTier.SYSTEM_OF_RECORD, doc="doc-c"),  # 3rd distinct
    ]
    pair = group[:2]
    assert _gross_divergence("price_per_watt_dc", pair), (
        "the '100x gross' annotation on doc-b is only true if the modifier fires"
    )
    assert not _gross_divergence(
        "price_per_watt_dc", [group[0], _c(0.36, unit="USD/W", doc="doc-x")]
    )
    result = assign_severity("price_per_watt_dc", ConflictClass.INTER_DOCUMENT, group, pair)
    assert result is Severity.CRITICAL


# --- gross divergence where the field has no D-2 band --------------------------


def test_gross_divergence_fires_on_a_decimal_slip_for_an_exact_field() -> None:
    """The modifier's own docstring names the decimal-comma error as its purpose,
    and `price_per_watt_dc` is the field most likely to carry one. It could not
    fire there: `tolerance.py` defaults every field D-2 does not cover to EXACT,
    `_band` returns `None` for EXACT, and the band branch bailed out. Measured
    over the contract that was 105 of the 124 keys in `CRITICALITY`, including 22
    of the 24 `TIER_A_FIELDS` and all 12 whose base class is CRITICAL - the
    modifier was inert for precisely the fields the floors exist to protect.

    Black-box through `assign_severity`, on an INTRA_DOCUMENT pair so no other
    modifier can contribute: base HIGH(3) + gross(1) = CRITICAL(4)."""
    pair = [_c(0.35, unit="USD/W", doc="doc-a"), _c(35.0, unit="USD/W", doc="doc-b")]
    assert (
        assign_severity("price_per_watt_dc", ConflictClass.INTRA_DOCUMENT, pair, pair)
        is Severity.CRITICAL
    )


def test_the_fallback_ignores_an_ordinary_disagreement() -> None:
    """The other half of the same claim. A ratio test that fired on any
    disagreement would mark every conflict gross and make the modifier
    meaningless in the opposite direction. 0.19 vs 0.35 USD/W is 1.8x - a real
    commercial disagreement, not a misplaced decimal point - and a 25 vs 30 year
    warranty is 1.2x. Neither may fire."""
    prices = [_c(0.19, unit="USD/W", doc="doc-a"), _c(0.35, unit="USD/W", doc="doc-b")]
    assert not _gross_divergence("price_per_watt_dc", prices)
    warranty = [_c(25.0, unit="yr", doc="doc-a"), _c(30.0, unit="yr", doc="doc-b")]
    assert not _gross_divergence("warranty_years", warranty)
    assert (
        assign_severity("warranty_years", ConflictClass.INTRA_DOCUMENT, warranty, warranty)
        is Severity.HIGH
    )


def test_the_fallback_boundary_is_at_exactly_ten_times() -> None:
    """Pinned at the boundary rather than deep inside it, matching
    `test_gross_divergence_boundary_is_at_exactly_ten_times_tolerance` for the
    band branch: exactly 10x must fire (`>=`, not `>`), 9.9x must not."""
    at_boundary = [_c(2.5, unit="yr", doc="doc-a"), _c(25.0, unit="yr", doc="doc-b")]
    just_under = [_c(2.5, unit="yr", doc="doc-a"), _c(24.75, unit="yr", doc="doc-b")]
    assert _gross_divergence("warranty_years", at_boundary)
    assert not _gross_divergence("warranty_years", just_under)


def test_the_fallback_does_not_fire_across_zero_or_a_sign_flip() -> None:
    """A ratio is meaningless where the values straddle zero, and a sign error is
    a different defect with a different fix than a misplaced decimal point, so
    the fallback declines rather than guessing. `domestic_content_percentage` has
    no D-2 band, so this exercises the fallback branch and not the band one."""
    across_zero = [_c(0.0, unit="%", doc="doc-a"), _c(55.0, unit="%", doc="doc-b")]
    sign_flip = [_c(-55.0, unit="%", doc="doc-a"), _c(55.0, unit="%", doc="doc-b")]
    assert not _gross_divergence("domestic_content_percentage", across_zero)
    assert not _gross_divergence("domestic_content_percentage", sign_flip)


def test_a_d2_band_still_wins_over_the_ratio_fallback() -> None:
    """The fallback must not silently override D-2. `rated_ac_power` carries a
    RELATIVE 1% band, and 10000 vs 10050 kVA is 1.005x - inside the band, where
    the band branch is both correct and more informative than any ratio. The
    fallback exists only for fields that have no band to consult."""
    pair = [_c(10000.0, unit="kVA", doc="doc-a"), _c(10050.0, unit="kVA", doc="doc-b")]
    assert not _gross_divergence("rated_ac_power", pair)


def test_the_modifier_is_reachable_for_every_floored_field() -> None:
    """The inertness was a property of the *table*, not of one example, so it is
    checked against the table. Every Tier A field and every CRITICAL-base field
    must be able to reach the modifier on a 100x pair; before the fallback, none
    of the 22 band-less Tier A fields could."""
    floored = TIER_A_FIELDS | {
        key for key, value in CRITICALITY.items() if value is Severity.CRITICAL
    }
    unreachable = [
        key
        for key in sorted(floored)
        if not _gross_divergence(
            key, [_c(1.0, unit=None, doc="doc-a"), _c(100.0, unit=None, doc="doc-b")]
        )
    ]
    assert not unreachable, f"gross divergence still cannot fire for: {unreachable}"


# --- the load-bearing invariant: a pure function of exactly these four --------


def test_signature_admits_no_hidden_inputs() -> None:
    """A fifth parameter - `resolved_by`, `now`, a settings object - would let a
    reviewer or the moment of computation change severity, which is exactly what
    open-decisions.md:49 forbids: the same store must produce the same workbook.
    A defaultable parameter is equally dangerous, because nobody has to pass it
    for it to exist."""
    params = inspect.signature(assign_severity).parameters
    assert list(params) == ["field_name", "conflict_class", "condition_group", "candidate_set"]
    for parameter in params.values():
        assert parameter.default is inspect.Parameter.empty, (
            f"{parameter.name} has a default, which is how a hidden fifth input "
            "is smuggled in unnoticed"
        )


def test_module_never_imports_a_clock_random_source_or_reads_reviewer_state() -> None:
    """Static, not behavioural, and deliberately so.

    A runtime monkeypatch of `time.time` cannot catch `from time import time as
    _now` bound at import time, and open-decisions.md:49's ban is on the
    *capability* to read these, not on whether today's implementation happens to
    exercise it on the inputs a particular test constructs. Walking the parsed
    AST for actual `import`/`from` statements and attribute accesses - rather
    than grepping the raw source text, which would also trip on this module's
    own docstring explaining the invariant in prose - closes that gap without
    that false positive: a future edit that adds `import time` to weight a
    recent divergence more heavily, or that reads `.resolved_by` /
    `.resolved_at` off a queue entry, fails this test the moment it is written,
    regardless of whether any test remembers to patch the right symbol
    afterward.
    """
    tree = ast.parse(inspect.getsource(severity_module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden_modules = {"time", "datetime", "random", "uuid"}
    hit_modules = imported & forbidden_modules
    assert not hit_modules, (
        f"severity.py imports {hit_modules} - a clock or randomness source a "
        "pure severity function must never touch"
    )

    attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    forbidden_attrs = {"resolution", "resolved_by", "resolved_at"}
    hit_attrs = attrs & forbidden_attrs
    assert not hit_attrs, (
        f"severity.py reads {hit_attrs} - a reviewer-identity or queue-state "
        "field a pure severity function must never see"
    )


def test_repeated_calls_with_equal_but_distinct_objects_agree() -> None:
    """Two calls built from freshly-constructed, merely-equal (not
    identical-by-id) objects must agree - ruling out any dependency on object
    identity or a hidden cache keyed on it."""

    def build() -> tuple[str, ConflictClass, list[ConflictCandidate], list[ConflictCandidate]]:
        pair = [_c(650.0, doc="doc-a"), _c(6.5, doc="doc-b")]
        return ("nameplate_power", ConflictClass.INTER_DOCUMENT, pair, pair)

    first = assign_severity(*build())
    second = assign_severity(*build())
    assert first == second


def test_result_does_not_depend_on_candidate_order() -> None:
    """Order is arrival order. FR-OUT-06 requires composition - and therefore the
    severity it reads - to be a pure function of the store's *content*, not the
    order candidates happened to arrive in; the same reasoning `_ordering_key`
    exists for elsewhere in this package."""
    a = _c(650.0, tier=SourceTier.SYSTEM_OF_RECORD, doc="doc-a")
    b = _c(6.5, tier=SourceTier.SYSTEM_OF_RECORD, doc="doc-b")
    forward = assign_severity("nameplate_power", ConflictClass.INTER_DOCUMENT, [a, b], [a, b])
    backward = assign_severity("nameplate_power", ConflictClass.INTER_DOCUMENT, [b, a], [b, a])
    assert forward == backward


def test_unit_reconciliation_normalizes_slash_spacing_and_nfkc() -> None:
    """`conflict_hitl._normalise_unit` and `schema/field.py:_comparable_unit`
    fold slash spacing (`'USD / W'` -> `'usd/w'`) and NFKC compatibility chars.
    `severity.py` must normalize units the same way before looking up
    `_BASE_UNIT`, otherwise `'USD / W'` vs `'USD/kW'` on an identical price misses
    the unit-reconciliation discount and triggers a false +1 gross-divergence
    penalty."""
    from procurement_agent.services.conflict_hitl import tolerance_for

    a = _c(0.35, unit="USD / W", doc="doc-a")
    b = _c(350.0, unit="USD/kW", doc="doc-b")
    pair = [a, b]
    assert severity_module._reconciles(a, b, tolerance_for("price_per_watt_dc"))
    assert severity_module._scaled_numbers(pair) == pytest.approx([0.35, 0.35])
