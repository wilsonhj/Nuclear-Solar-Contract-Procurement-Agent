"""Severity assignment for a detected conflict (open-decisions.md section 1).

`Severity` (schema/enums.py) and the compose gate (`orchestrator.blocking_conflicts`,
`orchestrator.compose_gate_blocks`) both already existed; nothing assigned a severity
to a detected conflict, so the gate was inert regardless of what it was fed. This
module is the assignment rule.

**A lookup, not a formula.** AIAG-VDA's 2019 FMEA handbook deleted RPN (severity x
occurrence x detection) because a product collapses distinguishable risk profiles
onto one number and lets a low severity be inflated by detection. So this module
never multiplies criticality by divergence: `CRITICALITY` fixes a base severity per
field from its criticality class, small bounded modifiers adjust it, and floors clamp
the result from below. Magnitude changes the *modifier*, never the base class.

**Keyed on the frozen contract's own `key` column**, exactly like
`tolerance.FIELD_TOLERANCES` and for the identical reason: a table keyed on invented
names falls through to a default and looks like a decision while being unreachable.
`tests/test_severity.py` parses the contract and fails on any key here that is not in
it, and - the direction `tolerance.py`'s own test does not check, added here because
completeness matters more for this table - on any contract key that is not here.

**The load-bearing invariant** (open-decisions.md:49): severity is a **pure function
of `(field_name, conflict_class, condition_group, candidate_set)`** - no clock, no
reviewer identity, no queue state. `assign_severity`'s signature holds exactly these
four and nothing else. `condition_group` and `candidate_set` are both
`Sequence[ConflictCandidate]`; open-decisions.md does not fix their shape, so this is
an interpretation, made explicit rather than left implicit: `candidate_set` is the
pair actually being compared (what a `ConflictQueueEntry.candidates` holds - see
`conflict_groupings`'s "exactly one comparable pair each"), and `condition_group` is
the wider set of candidates sharing the same condition that the pair was drawn from
(what `comparison_groups` partitions into) - needed only for the ">=3 distinct values"
modifier, which is a statement about the group a pair sits in, not about the pair
itself.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from decimal import Decimal

from ...schema import ConflictCandidate, ConflictClass, Severity, SourceTier, ToleranceRule
from ...schema.field import _comparable_unit
from ..confidence import FIELD_TIERS, CriticalityTier
from ..confidence import looks_tier_a as looks_tier_a
from .tolerance import FieldTolerance, tolerance_for

# --- the criticality-class lookup ----------------------------------------------
#
# One row per contract key, grouped by the five classes open-decisions.md #1
# defines. Comments are added only where the call is not the obvious reading of
# the class description - most rows follow directly from it.

CRITICALITY: dict[str, Severity] = {
    # --- Attestation / eligibility -> CRITICAL ---
    # Certifications, BABA/FEOC status, domestic content, country of origin,
    # ERCOT compliance items - a gap here is never inferable as a negative
    # (the CUAD absence-is-the-finding rule the Severity enum itself cites).
    "certifications": Severity.CRITICAL,
    "fire_safety_certifications": Severity.CRITICAL,
    "cell_certification": Severity.CRITICAL,
    "pcs_certification": Severity.CRITICAL,
    # A UL listing is a certification in different clothes - the same
    # presence/absence stakes as the fields named "certifications".
    "ul_listing": Severity.CRITICAL,
    "domestic_content_status": Severity.CRITICAL,
    "domestic_content_percentage": Severity.CRITICAL,
    "baba_status": Severity.CRITICAL,
    "baba_certification_ref": Severity.CRITICAL,
    "feoc_pfe_status": Severity.CRITICAL,
    "country_of_origin": Severity.CRITICAL,
    "ercot_compliance_items": Severity.CRITICAL,
    # --- Commercial -> HIGH ---
    # $/W, price, all warranty terms, material-assistance cost ratio.
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
    # Not named as an example in either bucket. D-2 marks both "Contractual" -
    # they are the guaranteed degradation curve underwriting the performance
    # warranty above, not a free-standing measurement, so they are graded with
    # it rather than with the MEDIUM performance metrics. See the report for
    # this choice flagged as an ambiguity in open-decisions.md #1.
    "degradation_year_1": Severity.HIGH,
    "degradation_annual": Severity.HIGH,
    # The named exception: looks like a descriptive string field, is a
    # buildability field. D-6a: Dyn1 and Dyn11 are 60 degrees apart and cannot
    # be paralleled.
    "vector_group": Severity.HIGH,
    # --- Decision-driving performance -> MEDIUM ---
    # Nameplate power, efficiency, MVA, usable energy, RTE, cycle life,
    # ampacity, TRD - and their direct companions (a declared-band tolerance or
    # a required condition graded with the value it qualifies, not the value it
    # merely labels).
    "nameplate_power": Severity.MEDIUM,
    "power_tolerance": Severity.MEDIUM,  # qualifies nameplate_power directly
    "module_efficiency": Severity.MEDIUM,
    "stc_rating": Severity.MEDIUM,
    "nmot_rating": Severity.MEDIUM,
    # Energy-yield drivers, not label fields: population data spans p5 -0.386
    # to p95 -0.278 (tolerance.py), a real basis for ranking modules.
    "temp_coeff_pmax": Severity.MEDIUM,
    "temp_coeff_voc": Severity.MEDIUM,
    "temp_coeff_isc": Severity.MEDIUM,
    "rated_ac_power": Severity.MEDIUM,
    # Graded with rated_ac_power: a wrong temperature misattributes which of
    # three legitimate "rated" figures is meant (the Sungrow D-1 case).
    "rated_ac_power_temp": Severity.MEDIUM,
    "max_efficiency": Severity.MEDIUM,
    "cec_efficiency": Severity.MEDIUM,
    "trd_percent": Severity.MEDIUM,
    "trd_limit_applied": Severity.MEDIUM,
    # A required grid-interconnection capability, not a certification list -
    # graded as a capability spec that drives the engineering decision.
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
    "energy_density": Severity.MEDIUM,  # "footprint driver" per the contract
    "footprint_area": Severity.MEDIUM,
    # Structural/site-suitability specs: getting these wrong means specifying
    # equipment that cannot survive or perform at the site, the same
    # engineering-critical shape as ampacity.
    "design_wind_speed": Severity.MEDIUM,
    "stow_wind_speed": Severity.MEDIUM,
    "ground_coverage_ratio": Severity.MEDIUM,
    "backtracking_yield_gain": Severity.MEDIUM,
    "tracking_range": Severity.MEDIUM,
    "bearing_gear_l10_years": Severity.MEDIUM,  # an L10 design life, cycle_life's analogue
    # --- Secondary comparison -> LOW ---
    # MPPT count, harmonic spectrum, enclosure rating, conductor size,
    # protocols - comparison-relevant, not itself decision-driving.
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
    # Voltage-class/compatibility labels, not graded performance numbers - a
    # "does this fit the design" check rather than a ranked metric.
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
    "bifaciality_tolerance": Severity.LOW,  # qualifies bifaciality_factor directly
    "topology": Severity.LOW,
    "configuration": Severity.LOW,
    "modules_per_row": Severity.LOW,
    "galvanization_spec": Severity.LOW,
    "foundations_per_mw": Severity.LOW,
    # --- Descriptive -> INFORMATIONAL ---
    # Verbatim names, datasheet revision, stow strategy, support terms.
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

#: The rule for a contract key nobody has classified yet. CRITICAL, not
#: INFORMATIONAL or a mid-scale guess: `ConflictQueueEntry.severity` is required
#: specifically because a default at or below the gate threshold would make a
#: forgotten severity silently unable to block, and only the maximum defends
#: that regardless of where a caller sets the threshold. Never actually needed
#: if `test_every_contract_key_has_a_criticality` stays green - the safety net
#: is for the gap between a contract addition and this table catching up.
DEFAULT_CRITICALITY = Severity.CRITICAL

#: D-3's Tier A, **derived from `services.confidence`, not restated here.**
#:
#: This module and `services.confidence` each grew their own copy of Tier A -
#: one to floor a conflict's severity, one to gate auto-acceptance - and by the
#: time both were reviewed they disagreed in both directions. `severity` had
#: `degradation_year_1` and `degradation_annual`; `confidence` had `standards`,
#: `ride_through_standards`, `cybersecurity_standards` and
#: `seismic_qualification`. Each list was individually defensible and each was
#: missing what the other had found, which is precisely the failure mode this
#: repository keeps producing: two encodings of one spec decision, free to
#: drift, with nothing that could notice.
#:
#: D-3's Tier A is one classification, so there is one table. `confidence` owns
#: it because that is where the tier's primary consequence lives (a policy gate
#: no score can pass); the severity floor here is the second consequence of the
#: same decision. `services.confidence` imports nothing from `services`, so
#: this direction is the one that does not create a cycle.
TIER_A_FIELDS: frozenset[str] = frozenset(
    key for key, tier in FIELD_TIERS.items() if tier is CriticalityTier.A
)


def criticality_for(field_name: str) -> Severity:
    """The base severity for a canonical field, falling back to `CRITICAL`.

    Mirrors `tolerance.tolerance_for`: `field_name` is the frozen contract's
    `key`, not a descriptive name, and a key this table does not hold falls back
    - here to the maximum rather than the minimum, because an unassigned
    severity must never be the reason the gate fails to block (see
    `DEFAULT_CRITICALITY`).
    """
    return CRITICALITY.get(field_name, DEFAULT_CRITICALITY)


# --- the coarse numeric helpers the modifiers share -----------------------------


def _numeric_value(value: object) -> float | None:
    """A numeric reading of a candidate value.

    Deliberately not imported from `conflict_hitl._as_number`: that name is
    underscore-prefixed in this package's `__init__.py`, and reaching past the
    mark that it is not for reuse - rather than adding a second, smaller
    computation this module actually needs - is how a "helper" import quietly
    becomes a second copy of the parent's comparison semantics. `bool` is
    excluded for the same reason it is there: `True` would compare equal to a
    1.0 candidate value under no tolerance at all.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float | Decimal):
        number = float(value)
        return number if math.isfinite(number) else None
    return None


def _band(tolerance: FieldTolerance, numbers: Sequence[float]) -> float | None:
    """A coarse tolerance band in the compared quantity's own unit.

    Deliberately simpler than `conflict_hitl._effective_magnitude`: it never
    selects between a D-2 row's two conditional bands (impedance below 10%,
    IEEE vs IEC, against the CEC list), because its one caller -
    `_gross_divergence` - only needs the right answer to within a factor of a
    few, and no D-2 conditional branch differs by enough to flip an
    order-of-magnitude verdict. `None` for EXACT, DECLARED_BAND and
    NEVER_COMPARE, which have no per-field magnitude to scale: those rules are
    out of scope for that modifier rather than guessed at.

    `_reconciles` was the second caller and is no longer one. A disagreement
    band is the wrong instrument for "is this one number in two units", and
    lending it out for that question is the defect that docstring now records.
    """
    if tolerance.magnitude is None:
        return None
    magnitude = tolerance.magnitude
    if tolerance.rule in (ToleranceRule.RELATIVE, ToleranceRule.ONE_SIDED):
        magnitude *= max(abs(n) for n in numbers)
    return magnitude


#: Units that differ only by a known scale factor, mapped to a common family
#: name and the multiplier that converts a value in this unit to that family's
#: base unit. Not a general unit-conversion engine - a small, explicit table in
#: the same spirit as `tolerance.py`'s per-field rows: an invented general
#: parser would apply confidently to a unit pair nobody here has verified means
#: what the parser assumes. Covers the SI-prefixed pairs the contract's own
#: canonical units produce (W/kW/MW, VA/kVA/MVA, Wh/kWh/MWh, V/kV, A/kA), a
#: realistic price-quoting pair (`USD/W` vs `USD/kW`), and percent-vs-fraction,
#: which several fields in `CRITICALITY` above use `%` for.
_BASE_UNIT: dict[str, tuple[str, float]] = {
    "w": ("w", 1.0),
    "kw": ("w", 1e3),
    "mw": ("w", 1e6),
    "gw": ("w", 1e9),
    "va": ("va", 1.0),
    "kva": ("va", 1e3),
    "mva": ("va", 1e6),
    "wh": ("wh", 1.0),
    "kwh": ("wh", 1e3),
    "mwh": ("wh", 1e6),
    "gwh": ("wh", 1e9),
    "v": ("v", 1.0),
    "kv": ("v", 1e3),
    "a": ("a", 1.0),
    "ka": ("a", 1e3),
    "usd/w": ("usd/w", 1.0),
    "usd/kw": ("usd/w", 1e-3),
    "%": ("%", 1.0),
    "fraction": ("%", 100.0),
}


def _scaled_numbers(candidates: Sequence[ConflictCandidate]) -> list[float] | None:
    """Numeric values, converted to a common unit when every candidate's unit
    resolves to the same `_BASE_UNIT` family; otherwise the raw values as
    printed. `None` if fewer than two candidates carry a number at all.

    The raw fallback is correct whenever both sides already share one unit -
    the ordinary case, e.g. two `Wp` candidates need no conversion - and a
    deliberate best-effort otherwise: reconciling an unrecognised unit pair is
    out of scope for this module's coarse checks (see `_band`), not something
    to guess a conversion for. Used by `_gross_divergence`, so that a candidate
    pair which merely *looks* far apart in unconverted units - 650 W against
    0.65, unit kW - is not read as a data error on top of the unit mismatch
    `_unit_mismatch_reconciles` already accounts for separately.
    """
    numbers: list[float] = []
    for candidate in candidates:
        number = _numeric_value(candidate.value)
        if number is None:
            return None
        numbers.append(number)
    if len(numbers) < 2:
        return None
    units = [c.unit for c in candidates]
    if all(unit is not None for unit in units):
        bases = [_BASE_UNIT.get(_comparable_unit(unit) or "") for unit in units if unit is not None]
        if len(bases) == len(units) and all(base is not None for base in bases):
            families = {base[0] for base in bases if base is not None}
            if len(families) == 1:
                return [
                    number * base[1]
                    for number, base in zip(numbers, bases, strict=True)
                    if base is not None
                ]
    return numbers


#: Relative and absolute slack allowed when deciding two numbers are the *same*
#: number. Sized for the conversion arithmetic in `_reconciles` and for nothing
#: else: `0.1 MW` scaled by 1e6 is 100000.00000000001, not 100000.0. Emphatically
#: not a disagreement tolerance - D-2's bands are that, and `_reconciles` is
#: documented at length for why the two questions must not share an answer.
RECONCILIATION_EPSILON = 1e-9


def _reconciles(a: ConflictCandidate, b: ConflictCandidate, tolerance: FieldTolerance) -> bool:
    """Whether `a` and `b` are the **same number** printed in a different unit.

    Requires both units to be individually recognised in `_BASE_UNIT` and to
    share a family - unlike `_scaled_numbers`'s raw fallback, an unrecognised
    unit pair here must read as "not shown to reconcile", not as "reconciles by
    default", because this result feeds a severity *discount*.

    **The comparison is representation noise, never a D-2 band**, and that
    distinction is the whole of this function. "Reconciles after conversion" is a
    strictly narrower question than D-2's disagreement tolerance: it asks whether
    this is one number spelled two ways, not whether two independent measurements
    are close enough to agree. The second question belongs to `values_conflict`,
    and a D-2 band is its answer.

    An earlier version reached for `_band` first and so asked the second question
    for every non-EXACT rule, leaving the representation-noise test reachable
    only for EXACT rows. `no_load_loss` carries a ONE_SIDED 15% band, so 100 kW
    against 114850 W - 14.85% apart, a unit error *and* a real disagreement -
    collected the full -2 "just fix the normaliser" discount and scored
    INFORMATIONAL. The identical pair classed INTER_DOCUMENT scores HIGH, and
    understating is the direction this module's docstring calls unsafe.

    **That mis-graded the queue; it did not unblock composition**, and the fix is
    reported as the queue-quality fix it is. The only two HIGH-or-CRITICAL fields
    carrying a numeric D-2 band are `degradation_year_1` and `degradation_annual`
    - both Tier A, both held at HIGH by the floor below whatever this returns -
    and every other banded field is already MEDIUM-or-under, so no wrongly
    granted discount could cross `config.compose_gate_threshold`. That is a
    property of today's two tables rather than of this code, so
    `test_the_only_high_or_critical_banded_fields_are_floored` asserts it and
    fails the day a HIGH field gains a band.

    A value one side rounded (`115 kW` against `114850 W`) is not the same
    number, and declining the discount there is the conservative direction.

    NEVER_COMPARE is refused outright. That rule marks a field whose name is
    shared by genuinely different physical quantities, so "these convert to the
    same number" is a coincidence the rule exists to distrust rather than
    evidence of one reading. The band test used to refuse those rows as a side
    effect of their having no magnitude; refusing them explicitly keeps that now
    that a magnitude is no longer what is being asked for.
    """
    if tolerance.rule is ToleranceRule.NEVER_COMPARE:
        return False
    if a.value is None or b.value is None or a.unit is None or b.unit is None:
        return False
    base_a = _BASE_UNIT.get(_comparable_unit(a.unit) or "")
    base_b = _BASE_UNIT.get(_comparable_unit(b.unit) or "")
    if base_a is None or base_b is None or base_a[0] != base_b[0]:
        return False
    number_a, number_b = _numeric_value(a.value), _numeric_value(b.value)
    if number_a is None or number_b is None:
        return False
    common_a, common_b = number_a * base_a[1], number_b * base_b[1]
    return math.isclose(
        common_a, common_b, rel_tol=RECONCILIATION_EPSILON, abs_tol=RECONCILIATION_EPSILON
    )


# --- the four bounded modifiers, open-decisions.md #1 --------------------------


#: The order-of-magnitude ratio `_gross_divergence` falls back to when a field
#: has no D-2 band to scale. Ten, so the two branches state the same threshold in
#: the two different currencies available: "ten times the tolerance" where a
#: tolerance exists, "ten times the value" where none does. A decimal-comma or
#: decimal-point slip - D-5's named trap, and the modifier's stated purpose -
#: moves a value by a factor of exactly 10, 100 or 1000, so 10 is the smallest
#: ratio that catches every one of them and the largest that catches none of the
#: ordinary disagreements this table is otherwise full of (a 25-year versus
#: 30-year warranty is 1.2x; 0.19 versus 0.35 USD/W is 1.8x).
GROSS_DIVERGENCE_RATIO = 10.0


def _gross_divergence(field_name: str, candidate_set: Sequence[ConflictCandidate]) -> bool:
    """+1: divergence >= 10x the D-2 tolerance - usually a decimal-comma error,
    D-5's highest-risk trap.

    **Two branches, because most contract keys have no D-2 band.** `_band`
    returns `None` for EXACT and DECLARED_BAND, and `tolerance.py` defaults every
    field D-2 does not cover to EXACT - which is 105 of the 124 keys in
    `CRITICALITY`, including 22 of the 24 `TIER_A_FIELDS` and *all twelve*
    fields whose base class is CRITICAL. With only the band branch this modifier
    could not fire for any of them, so the docstring above named the
    decimal-comma trap as its purpose while being structurally unable to detect
    one on the single most likely field to carry it:

        assign_severity("price_per_watt_dc", INTER_DOCUMENT,
                        [0.35 USD/W, 35.0 USD/W], ...)   # a 100x slip
        # _gross_divergence(...) -> False

    An absent tolerance means "no per-field magnitude to scale", not "no
    magnitude at all": the candidates carry one. So where `_band` has nothing to
    offer, the fallback compares the values to each other -
    `max / min >= GROSS_DIVERGENCE_RATIO` - which is scale-free and needs no
    per-field number to be invented. This is the same reasoning `_reconciles`
    already applies one function up, where EXACT falls back to numeric equality
    rather than reading "no D-2 band" as "cannot compare".

    Deliberately **not** applied when a band exists: a band is D-2's own
    statement of how far this specific quantity may legitimately vary, and a
    ratio test would silently override it. `rated_ac_power` at 10000 vs 10050
    kVA is 1.005x and well inside its 1% band; the band branch is the correct
    and more informative test wherever one is available.

    The fallback is skipped when the two sides straddle zero or either is zero -
    a ratio is meaningless there, and a temperature coefficient of -0.29 against
    +0.29 is a sign error, which is a different defect with a different fix.
    """
    numbers = _scaled_numbers(candidate_set)
    if numbers is None:
        return False
    low, high = min(numbers), max(numbers)
    band = _band(tolerance_for(field_name), numbers)
    if band is not None and band > 0:
        return (high - low) >= GROSS_DIVERGENCE_RATIO * band
    # No usable D-2 band: fall back to an order-of-magnitude ratio.
    if low <= 0.0 or high <= 0.0:
        return False
    return (high / low) >= GROSS_DIVERGENCE_RATIO


def _both_system_of_record_inter_document(
    conflict_class: ConflictClass, candidate_set: Sequence[ConflictCandidate]
) -> bool:
    """+1: inter-document where BOTH sides are system_of_record - two ingested
    authorities disagreeing, not a contract disagreeing with a lower tier."""
    return (
        conflict_class is ConflictClass.INTER_DOCUMENT
        and len(candidate_set) >= 2
        and all(c.source_tier is SourceTier.SYSTEM_OF_RECORD for c in candidate_set)
    )


def _distinct_value_count(candidates: Sequence[ConflictCandidate]) -> int:
    """Distinct non-missing values, without requiring `value` to be hashable
    (some contract fields are `dict`, e.g. `harmonic_spectrum`)."""
    seen: list[object] = []
    for candidate in candidates:
        if candidate.value is None:
            continue
        if candidate.value not in seen:
            seen.append(candidate.value)
    return len(seen)


def _matcher_failure(condition_group: Sequence[ConflictCandidate]) -> bool:
    """+1: >=3 distinct values in one group - a matcher failure, not a data
    disagreement."""
    return _distinct_value_count(condition_group) >= 3


def _unit_mismatch_reconciles(
    field_name: str, conflict_class: ConflictClass, candidate_set: Sequence[ConflictCandidate]
) -> bool:
    """-2: a unit mismatch that reconciles after conversion is an extraction
    defect - fix the normaliser, do not hold the workbook. Requires every pair
    in `candidate_set` to reconcile, not merely one: a mixed signal (some pairs
    explained by units, others not) is not evidence the whole conflict is just
    a units artefact, and understating severity is the unsafe direction here."""
    if conflict_class is not ConflictClass.UNIT_NORMALIZATION or len(candidate_set) < 2:
        return False
    tolerance = tolerance_for(field_name)
    pairs = list(itertools.combinations(candidate_set, 2))
    return all(_reconciles(a, b, tolerance) for a, b in pairs)


def _clamp(value: int) -> int:
    return max(int(Severity.INFORMATIONAL), min(int(Severity.CRITICAL), value))


# --- the entry point ------------------------------------------------------------


def assign_severity(
    field_name: str,
    conflict_class: ConflictClass,
    condition_group: Sequence[ConflictCandidate],
    candidate_set: Sequence[ConflictCandidate],
) -> Severity:
    """Severity for one detected conflict (open-decisions.md #1).

    A pure function of exactly these four arguments - see the module docstring
    for what `condition_group` and `candidate_set` are read as, and
    `tests/test_severity.py` for the tests enforcing the purity this signature
    only states.

    Base severity comes from `criticality_for(field_name)` - the field's
    criticality class, never the size of the disagreement. Four bounded
    modifiers are summed (not multiplied - the deleted-RPN lesson) and the
    total is clamped to the enum's range. Two floors then apply, and nothing
    computed above can lower a field past them: any D-3 Tier A field floors at
    HIGH, and any field whose base class is CRITICAL (attestation/eligibility -
    certification presence-vs-absence chief among them) floors at CRITICAL.
    Both floors are independent, explicit checks rather than an emergent
    property of the base table, so a future edit that reclassifies a Tier A or
    attestation field cannot silently drop its floor along with it.
    """
    base = criticality_for(field_name)
    modifier = 0
    if _gross_divergence(field_name, candidate_set):
        modifier += 1
    if _both_system_of_record_inter_document(conflict_class, candidate_set):
        modifier += 1
    if _matcher_failure(condition_group):
        modifier += 1
    if _unit_mismatch_reconciles(field_name, conflict_class, candidate_set):
        modifier -= 2

    value = _clamp(int(base) + modifier)
    if field_name in TIER_A_FIELDS:
        value = max(value, int(Severity.HIGH))
    if criticality_for(field_name) is Severity.CRITICAL:
        value = max(value, int(Severity.CRITICAL))
    return Severity(value)
