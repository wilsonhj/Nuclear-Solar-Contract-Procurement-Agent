"""Contract C2 in machine-readable form: the canonical parameters, by category.

`contracts/canonical-parameters.md` is the frozen source of truth and stays that
way - this module is its executable transcription, and `tests/test_field_registry
.py` compares the two **row for row in both directions**. Where they disagree the
contract is right and this file is the defect.

**Why a registry at all.** `ComponentInstance.fields` is `dict[str, list[
CanonicalField]]` and `FieldClaim.field_name` is a `str`, so until now the key was
a free string at every boundary. Claims are append-only: an extractor emitting
`nameplate_power_w` writes rows that can only ever be superseded, never corrected,
and the B.9 gold set gets labelled against a key nothing downstream looks up.
There is no repair path, which is what makes this a boundary check rather than a
lint.

**The category is part of the key.** `chemistry` is a real contract key and a
nonsense one on a PV module; `insulation_type` means `oil`/`dry` on a transformer
and XLPE or USE-2 on a cable. A flat set of names admits both mistakes, so a spec
is looked up as `(key, category)` and the two `insulation_type` rows are two
specs. That is the one place the contract gives one name two closed vocabularies,
and it is the reason this is not a `frozenset[str]`.

**Placement.** `schema` sits below `services` and cannot import it (see
`component.py`), and both enforcement points - `ComponentInstance.fields` here in
`schema`, `commit_claims` over in `services.claims` - need the same answer. So the
registry belongs here, and it imports nothing but `.enums`.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .enums import ComponentCategory

__all__ = [
    "ALL_CATEGORIES",
    "CONTRACT_KEYS",
    "CONTRACT_UNIT_AMBIGUITIES",
    "FIELD_SPECS",
    "FieldScope",
    "FieldSpec",
    "OffContractFieldError",
    "Shape",
    "ValueType",
    "condition_dimensions_for",
    "is_contract_key",
    "keys_for",
    "require_contract_key",
    "spec_for",
]

_C = ComponentCategory

#: The eight tabs a per-category parameter can appear on, and the whole domain a
#: cross-cutting one appears on. Derived rather than written out, so a ninth
#: category cannot be added to `ComponentCategory` and silently miss the
#: cross-category fields the contract says are "present on every
#: `ComponentInstance` regardless of category".
ALL_CATEGORIES: frozenset[ComponentCategory] = frozenset(ComponentCategory)


class ValueType(StrEnum):
    """The `type` column's vocabulary, as the contract spells it.

    A closed set rather than the Python types themselves: `DeclaredBand` and
    `ComponentCategory` are schema objects, and importing them here to name them
    would give `registry` a dependency on `field.py` for no gain - the registry
    describes what a value *is declared to be*, it does not construct one.
    """

    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    STR = "str"
    DATE = "date"
    DECLARED_BAND = "DeclaredBand"
    COMPONENT_CATEGORY = "ComponentCategory"


class Shape(StrEnum):
    """Scalar, list or map - the distinction the 18 `list[str]` rows turn on
    (14 distinct keys: `certifications` appears on three categories, `standards`
    and `communication_protocols` on two each, and a recurring key is one spec).

    The contract's preamble spends a paragraph on it: an empty list means "we
    looked and found none stated", which is materially different from `None`
    meaning "we have not established this". A registry recording only the element
    type would erase exactly that difference, and it is the difference a missing
    UL 9540A listing lives in.
    """

    SCALAR = "scalar"
    LIST = "list"
    MAP = "map"


class FieldScope(StrEnum):
    """Which of the contract's three kinds of table a row came from.

    Kept as data because the tables differ in more than their heading: only the
    per-category tables have a `canonical unit` column, and only the per-category
    tables scope a key to particular tabs. A row that moved between sections is a
    contract change, and the bidirectional test can only see it if the section is
    part of the row.
    """

    CATEGORY = "category"
    CROSS_CATEGORY = "cross_category"
    COMPLIANCE = "compliance"


class OffContractFieldError(ValueError):
    """A field key is not in the frozen contract, or not for this category.

    A `ValueError` subclass so a pydantic validator can raise it and have it
    surface as a `ValidationError` without translation, while a plain caller can
    still catch it by name. `services.claims` re-raises it as a `ProposalError`
    to keep that module's one error type for callers.
    """


class FieldSpec(BaseModel):
    """One row of the frozen contract's parameter tables.

    Frozen, because it is a transcription of a frozen document; a mutable spec
    would let a caller widen an enum at runtime and leave no trace.

    `categories` collapses the repetition the markdown carries: `warranty_years`
    is written under four sections but is one field with one type and one unit,
    so it is one spec naming four categories rather than four specs. The
    bidirectional test expands it back out and compares row for row, so the
    collapse cannot hide a difference - if two sections ever disagree about a
    key's type, unit or enum, they become two specs, which is exactly what
    happened to `insulation_type`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    shape: Shape
    value_type: ValueType
    map_key_type: ValueType | None = None
    unit: str | None = None
    enum: frozenset[str] | None = None
    categories: frozenset[ComponentCategory]
    scope: FieldScope
    condition_dimensions: frozenset[str] = Field(
        default=frozenset(),
        description=(
            "The `ConditionDimensions` names that gate a comparison of this "
            "parameter, from the frozen contract's Conditions table. Empty means "
            "no row governs the key, so nothing gates it - see "
            "`condition_dimensions_for` for why that is the safe default and "
            "`Condition.comparable_with` for what consumes it."
        ),
    )
    web_query_template: str | None = None

    @property
    def contract_type(self) -> str:
        """The row's `type` cell, rebuilt from the parsed shape.

        Rebuilt rather than stored, so the shape model has to be faithful enough
        to round-trip: a `dict[int, float]` recorded as a bare map would fail to
        reproduce the cell, and the bidirectional test compares this string
        against the markdown.
        """
        if self.shape is Shape.LIST:
            return f"list[{self.value_type.value}]"
        if self.shape is Shape.MAP:
            # `_check_shape` has already refused a map without a key type, so the
            # assertion is documentation for the reader and a narrowing for mypy
            # rather than a runtime possibility.
            assert self.map_key_type is not None
            return f"dict[{self.map_key_type.value}, {self.value_type.value}]"
        return self.value_type.value

    @model_validator(mode="after")
    def _check_shape(self) -> FieldSpec:
        if (self.shape is Shape.MAP) != (self.map_key_type is not None):
            raise ValueError(
                f"{self.key}: a map declares both its key and value type and nothing "
                "else may declare a key type. `dict[?, float]` is not a type, and a "
                "key type on a scalar reads as a constraint that is never applied."
            )
        if self.enum is not None:
            if self.value_type is not ValueType.STR:
                raise ValueError(
                    f"{self.key}: only a string-valued field carries a closed "
                    "vocabulary; the contract declares none anywhere else."
                )
            if not self.enum:
                raise ValueError(
                    f"{self.key}: an empty vocabulary admits nothing, so every "
                    "extraction of the field would be a validation failure."
                )
        if not self.categories:
            raise ValueError(f"{self.key}: a field admitted for no category is unreachable")
        if self.scope is not FieldScope.CATEGORY and self.categories != ALL_CATEGORIES:
            raise ValueError(
                f"{self.key}: the contract says the cross-category and compliance "
                "fields are present on every ComponentInstance regardless of "
                "category, so scoping one to a subset contradicts it."
            )
        if self.unit is not None and self.scope is not FieldScope.CATEGORY:
            raise ValueError(
                f"{self.key}: only the per-category tables have a `canonical unit` "
                "column. See CONTRACT_UNIT_AMBIGUITIES for the two rows that print "
                "a unit in a table without the column to declare it in."
            )
        return self


#: Every row of the contract's parameter tables, in the order the document
#: writes them, so a reviewer can read the two side by side.
FIELD_SPECS: tuple[FieldSpec, ...] = (
    # --- 1. PV Modules ---
    FieldSpec(
        key="nameplate_power",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="Wp",
        categories=frozenset({_C.PV_MODULES}),
        # "PV power, efficiency, all electrical". Trina prints STC 695 W and
        # NOCT 531 W side by side; a naive table grab returns 531, and the
        # BNPI/bifacial-gain table reads 30% higher again.
        condition_dimensions=frozenset({"basis"}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="power_tolerance",
        shape=Shape.SCALAR,
        value_type=ValueType.DECLARED_BAND,
        categories=frozenset({_C.PV_MODULES}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="cell_technology",
        shape=Shape.SCALAR,
        value_type=ValueType.STR,
        enum=frozenset({"n_topcon", "perc", "hjt", "ibc", "cdte", "other"}),
        categories=frozenset({_C.PV_MODULES}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="module_efficiency",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="%",
        categories=frozenset({_C.PV_MODULES}),
        # The same row: an efficiency is measured under a basis.
        condition_dimensions=frozenset({"basis"}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="temp_coeff_pmax",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="%/degC",
        categories=frozenset({_C.PV_MODULES}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="temp_coeff_voc",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="%/degC",
        categories=frozenset({_C.PV_MODULES}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="temp_coeff_isc",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="%/degC",
        categories=frozenset({_C.PV_MODULES}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="bifaciality_factor",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="%",
        categories=frozenset({_C.PV_MODULES}),
        # `bnpi` and `bstc` are basis members precisely because bifacial gain
        # is quoted against them (IEC TS 60904-1-2).
        condition_dimensions=frozenset({"basis"}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="bifaciality_tolerance",
        shape=Shape.SCALAR,
        value_type=ValueType.DECLARED_BAND,
        categories=frozenset({_C.PV_MODULES}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="stc_rating",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="Wp",
        categories=frozenset({_C.PV_MODULES}),
        # One of the contract's own "ad-hoc encodings" of a condition, kept as
        # a key. Still a PV power, so the row still governs it: a `bstc` figure
        # filed here is not comparable with an `stc` one.
        condition_dimensions=frozenset({"basis"}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="nmot_rating",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="Wp",
        categories=frozenset({_C.PV_MODULES}),
        # As `stc_rating`.
        condition_dimensions=frozenset({"basis"}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="max_system_voltage",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="V",
        categories=frozenset({_C.PV_MODULES, _C.COMBINER_BOXES}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="degradation_year_1",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="%",
        categories=frozenset({_C.PV_MODULES}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="degradation_annual",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="%/yr",
        categories=frozenset({_C.PV_MODULES}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="product_warranty_years",
        shape=Shape.SCALAR,
        value_type=ValueType.INT,
        unit="yr",
        categories=frozenset({_C.PV_MODULES}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="performance_warranty_years",
        shape=Shape.SCALAR,
        value_type=ValueType.INT,
        unit="yr",
        categories=frozenset({_C.PV_MODULES}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="performance_warranty_end_output",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="%",
        categories=frozenset({_C.PV_MODULES}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="certifications",
        shape=Shape.LIST,
        value_type=ValueType.STR,
        categories=frozenset({_C.PV_MODULES, _C.INVERTERS_PCS, _C.COMBINER_BOXES}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="domestic_content_status",
        shape=Shape.SCALAR,
        value_type=ValueType.STR,
        enum=frozenset({"qualified", "not_qualified", "unconfirmed"}),
        categories=frozenset({_C.PV_MODULES}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="price_per_watt_dc",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="USD/W",
        categories=frozenset({_C.PV_MODULES}),
        scope=FieldScope.CATEGORY,
    ),
    # --- 2. Inverters / PCS ---
    FieldSpec(
        key="topology",
        shape=Shape.SCALAR,
        value_type=ValueType.STR,
        enum=frozenset({"central", "string", "pcs"}),
        categories=frozenset({_C.INVERTERS_PCS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="rated_ac_power",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="kVA",
        categories=frozenset({_C.INVERTERS_PCS}),
        # "Inverter rated power". `352 kVA @30 degC / 320 @40 / 295 @50` are
        # all "rated"; comparing two points of a derating curve invents a
        # conflict out of two correct numbers. D-1's opening case.
        condition_dimensions=frozenset({"temperature_c"}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="rated_ac_power_temp",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="degC",
        categories=frozenset({_C.INVERTERS_PCS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="max_dc_voltage",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="V",
        categories=frozenset({_C.INVERTERS_PCS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="mppt_count",
        shape=Shape.SCALAR,
        value_type=ValueType.INT,
        categories=frozenset({_C.INVERTERS_PCS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="mppt_voltage_min",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="V",
        categories=frozenset({_C.INVERTERS_PCS}),
        # "Inverter MPPT window", whose `basis` vocabulary is `full_range` /
        # `full_power`. `500-1500 V` and `860-1330 V` are different fields,
        # not a discrepancy.
        condition_dimensions=frozenset({"basis"}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="mppt_voltage_max",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="V",
        categories=frozenset({_C.INVERTERS_PCS}),
        # As `mppt_voltage_min`.
        condition_dimensions=frozenset({"basis"}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="max_efficiency",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="%",
        categories=frozenset({_C.INVERTERS_PCS}),
        # "Inverter efficiency". 99.02% max, 98.5% CEC and 98.8% European are
        # one product. There is no `european_efficiency` key, so a Euro figure
        # has to land in one of these two and only `weighting` keeps it apart.
        condition_dimensions=frozenset({"weighting"}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="cec_efficiency",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="%",
        categories=frozenset({_C.INVERTERS_PCS}),
        # As `max_efficiency`.
        condition_dimensions=frozenset({"weighting"}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="ride_through_standards",
        shape=Shape.LIST,
        value_type=ValueType.STR,
        categories=frozenset({_C.INVERTERS_PCS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="reactive_capability_at_zero_output",
        shape=Shape.SCALAR,
        value_type=ValueType.BOOL,
        categories=frozenset({_C.INVERTERS_PCS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="trd_percent",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="%",
        categories=frozenset({_C.INVERTERS_PCS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="trd_limit_applied",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="%",
        categories=frozenset({_C.INVERTERS_PCS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="harmonic_spectrum",
        shape=Shape.MAP,
        value_type=ValueType.FLOAT,
        map_key_type=ValueType.INT,
        unit="%",
        categories=frozenset({_C.INVERTERS_PCS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="thd_percent",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="%",
        categories=frozenset({_C.INVERTERS_PCS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="filtering_provisions",
        shape=Shape.LIST,
        value_type=ValueType.STR,
        enum=frozenset({"lcl", "active", "passive", "none"}),
        categories=frozenset({_C.INVERTERS_PCS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="dc_injection",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="%",
        categories=frozenset({_C.INVERTERS_PCS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="flicker_pst",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        categories=frozenset({_C.INVERTERS_PCS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="flicker_plt",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        categories=frozenset({_C.INVERTERS_PCS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="communication_protocols",
        shape=Shape.LIST,
        value_type=ValueType.STR,
        categories=frozenset({_C.INVERTERS_PCS, _C.TRACKERS_MOUNTING}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="warranty_years",
        shape=Shape.SCALAR,
        value_type=ValueType.INT,
        unit="yr",
        categories=frozenset(
            {_C.INVERTERS_PCS, _C.TRACKERS_MOUNTING, _C.TRANSFORMERS, _C.COMBINER_BOXES}
        ),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="price_per_watt_ac",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="USD/W",
        categories=frozenset({_C.INVERTERS_PCS}),
        scope=FieldScope.CATEGORY,
    ),
    # --- 3. Trackers & Mounting ---
    FieldSpec(
        key="configuration",
        shape=Shape.SCALAR,
        value_type=ValueType.STR,
        enum=frozenset({"1p_tracker", "2p_tracker", "fixed_tilt"}),
        categories=frozenset({_C.TRACKERS_MOUNTING}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="tracking_range",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="deg",
        categories=frozenset({_C.TRACKERS_MOUNTING}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="modules_per_row",
        shape=Shape.SCALAR,
        value_type=ValueType.INT,
        categories=frozenset({_C.TRACKERS_MOUNTING}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="backtracking_yield_gain",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="%",
        categories=frozenset({_C.TRACKERS_MOUNTING}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="design_wind_speed",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="m/s",
        categories=frozenset({_C.TRACKERS_MOUNTING}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="stow_wind_speed",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="m/s",
        categories=frozenset({_C.TRACKERS_MOUNTING}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="stow_strategy",
        shape=Shape.SCALAR,
        value_type=ValueType.STR,
        categories=frozenset({_C.TRACKERS_MOUNTING}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="ground_coverage_ratio",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        categories=frozenset({_C.TRACKERS_MOUNTING}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="foundations_per_mw",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="1/MW",
        categories=frozenset({_C.TRACKERS_MOUNTING}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="galvanization_spec",
        shape=Shape.SCALAR,
        value_type=ValueType.STR,
        categories=frozenset({_C.TRACKERS_MOUNTING}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="corrosion_warranty_years",
        shape=Shape.SCALAR,
        value_type=ValueType.INT,
        unit="yr",
        categories=frozenset({_C.TRACKERS_MOUNTING}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="bearing_gear_l10_years",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="yr",
        categories=frozenset({_C.TRACKERS_MOUNTING}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="enclosure_rating",
        shape=Shape.SCALAR,
        value_type=ValueType.STR,
        categories=frozenset({_C.TRACKERS_MOUNTING, _C.COMBINER_BOXES}),
        scope=FieldScope.CATEGORY,
    ),
    # --- 4. Transformers ---
    FieldSpec(
        key="insulation_type",
        shape=Shape.SCALAR,
        value_type=ValueType.STR,
        enum=frozenset({"oil", "dry"}),
        categories=frozenset({_C.TRANSFORMERS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="rating_mva",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="MVA",
        categories=frozenset({_C.TRANSFORMERS}),
        # "Transformer MVA". IEEE lists base-first, IEC top-first, so "take the
        # first number" is right for one regime and wrong for the other.
        condition_dimensions=frozenset({"standards_regime"}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="cooling_classes",
        shape=Shape.LIST,
        value_type=ValueType.STR,
        categories=frozenset({_C.TRANSFORMERS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="rating_mva_by_cooling",
        shape=Shape.MAP,
        value_type=ValueType.FLOAT,
        map_key_type=ValueType.STR,
        unit="MVA",
        categories=frozenset({_C.TRANSFORMERS}),
        # The same row's "plus cooling class per rating" - this key *is* the
        # per-cooling map, so the regime still selects which stage is canonical.
        condition_dimensions=frozenset({"standards_regime"}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="voltage_hv",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="kV",
        categories=frozenset({_C.TRANSFORMERS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="voltage_lv",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="kV",
        categories=frozenset({_C.TRANSFORMERS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="vector_group",
        shape=Shape.SCALAR,
        value_type=ValueType.STR,
        categories=frozenset({_C.TRANSFORMERS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="impedance_percent",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="%",
        categories=frozenset({_C.TRANSFORMERS}),
        # "Transformer %Z", all three of its dimensions. %Z scales linearly
        # with the MVA base - 1.25-1.67x between regimes, against a +/-7.5%
        # tolerance - and nominal-tap and +5%-tap impedance are not the same
        # measurement. All three were promoted out of `note` under issue #16,
        # and a promotion that reaches no key leaves the merge it was meant
        # to stop.
        condition_dimensions=frozenset({"standards_regime", "base_mva", "tap_position_pct"}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="k_factor",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        categories=frozenset({_C.TRANSFORMERS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="no_load_loss",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="kW",
        categories=frozenset({_C.TRANSFORMERS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="load_loss",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="kW",
        categories=frozenset({_C.TRANSFORMERS}),
        # "Transformer losses": IEEE states load loss at `20 + rise`
        # (75/85/95 degC), IEC at 75 regardless. `no_load_loss` is
        # deliberately NOT given this dimension - the same row says "No-load
        # loss is **not** temperature-corrected", so gating it would suppress
        # a real disagreement between two figures that are comparable.
        condition_dimensions=frozenset({"reference_temperature_c"}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="efficiency",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="%",
        categories=frozenset({_C.TRANSFORMERS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="standards",
        shape=Shape.LIST,
        value_type=ValueType.STR,
        categories=frozenset({_C.TRANSFORMERS, _C.CABLING_WIRING}),
        scope=FieldScope.CATEGORY,
    ),
    # --- 5. Cabling & Wiring ---
    FieldSpec(
        key="conductor_material",
        shape=Shape.SCALAR,
        value_type=ValueType.STR,
        enum=frozenset({"copper", "aluminium"}),
        categories=frozenset({_C.CABLING_WIRING}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="conductor_size",
        shape=Shape.SCALAR,
        value_type=ValueType.STR,
        categories=frozenset({_C.CABLING_WIRING}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="conductor_area",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="mm2",
        categories=frozenset({_C.CABLING_WIRING}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="voltage_class",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="V",
        categories=frozenset({_C.CABLING_WIRING}),
        scope=FieldScope.CATEGORY,
    ),
    # The second `insulation_type`, and the reason a spec is keyed on (key,
    # category). A transformer's is `oil` or `dry`; a cable's is XLPE, PV wire or
    # USE-2, which the contract prints as examples rather than as an `enum:`
    # clause. One spec per key would hand the cable the transformer's closed
    # vocabulary and reject every cable datasheet in the corpus.
    FieldSpec(
        key="insulation_type",
        shape=Shape.SCALAR,
        value_type=ValueType.STR,
        categories=frozenset({_C.CABLING_WIRING}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="ampacity",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="A",
        categories=frozenset({_C.CABLING_WIRING}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="load_factor",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        categories=frozenset({_C.CABLING_WIRING}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="ul_listing",
        shape=Shape.LIST,
        value_type=ValueType.STR,
        categories=frozenset({_C.CABLING_WIRING}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="shielding",
        shape=Shape.SCALAR,
        value_type=ValueType.STR,
        categories=frozenset({_C.CABLING_WIRING}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="price_per_metre",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="USD/m",
        categories=frozenset({_C.CABLING_WIRING}),
        scope=FieldScope.CATEGORY,
    ),
    # --- 6. Combiner Boxes ---
    FieldSpec(
        key="input_count",
        shape=Shape.SCALAR,
        value_type=ValueType.INT,
        categories=frozenset({_C.COMBINER_BOXES}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="fuse_rating",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="A",
        categories=frozenset({_C.COMBINER_BOXES}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="continuous_current",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="A",
        categories=frozenset({_C.COMBINER_BOXES}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="string_monitoring",
        shape=Shape.SCALAR,
        value_type=ValueType.BOOL,
        categories=frozenset({_C.COMBINER_BOXES}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="surge_protection",
        shape=Shape.SCALAR,
        value_type=ValueType.BOOL,
        categories=frozenset({_C.COMBINER_BOXES}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="disconnect_type",
        shape=Shape.SCALAR,
        value_type=ValueType.STR,
        categories=frozenset({_C.COMBINER_BOXES}),
        scope=FieldScope.CATEGORY,
    ),
    # --- 7. BESS ---
    FieldSpec(
        key="chemistry",
        shape=Shape.SCALAR,
        value_type=ValueType.STR,
        enum=frozenset({"lfp", "nmc", "other"}),
        categories=frozenset({_C.BESS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="usable_energy_per_container",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="MWh",
        categories=frozenset({_C.BESS}),
        # "BESS energy". BOL and EOL differ ~26% on real projects, and AC vs
        # DC straddles the PCS.
        condition_dimensions=frozenset({"side", "basis"}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="nameplate_energy_per_container",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="MWh",
        categories=frozenset({_C.BESS}),
        # As `usable_energy_per_container`.
        condition_dimensions=frozenset({"side", "basis"}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="power_rating",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="MW",
        categories=frozenset({_C.BESS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="c_rate",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        categories=frozenset({_C.BESS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="round_trip_efficiency",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="%",
        categories=frozenset({_C.BESS}),
        # "BESS RTE". Four distinct boundaries are all called "round-trip
        # efficiency", worth 2-7 pp, and RTE is duration-dependent even at one
        # boundary.
        condition_dimensions=frozenset({"side", "duration_h", "rte_boundary"}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="cycle_life",
        shape=Shape.SCALAR,
        value_type=ValueType.INT,
        categories=frozenset({_C.BESS}),
        # "BESS cycle life": the `soh_60` / `soh_70` / `soh_80` threshold the
        # count is quoted to. Frequently omitted, which makes the number
        # uncomparable - but "absent is unknown", so an omission still compares.
        condition_dimensions=frozenset({"basis"}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="augmentation_plan",
        shape=Shape.SCALAR,
        value_type=ValueType.STR,
        categories=frozenset({_C.BESS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="degradation_warranty_years",
        shape=Shape.SCALAR,
        value_type=ValueType.INT,
        unit="yr",
        categories=frozenset({_C.BESS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="degradation_warranty_cycles",
        shape=Shape.SCALAR,
        value_type=ValueType.INT,
        categories=frozenset({_C.BESS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="thermal_management",
        shape=Shape.SCALAR,
        value_type=ValueType.STR,
        enum=frozenset({"air", "liquid", "other"}),
        categories=frozenset({_C.BESS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="fire_safety_certifications",
        shape=Shape.LIST,
        value_type=ValueType.STR,
        categories=frozenset({_C.BESS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="cell_certification",
        shape=Shape.LIST,
        value_type=ValueType.STR,
        categories=frozenset({_C.BESS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="pcs_certification",
        shape=Shape.LIST,
        value_type=ValueType.STR,
        categories=frozenset({_C.BESS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="seismic_qualification",
        shape=Shape.SCALAR,
        value_type=ValueType.STR,
        categories=frozenset({_C.BESS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="energy_density",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="MWh/m2",
        categories=frozenset({_C.BESS}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="footprint_area",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        unit="m2",
        categories=frozenset({_C.BESS}),
        scope=FieldScope.CATEGORY,
    ),
    # --- 8. EMS / SCADA & Controls ---
    FieldSpec(
        key="plant_controller_model",
        shape=Shape.SCALAR,
        value_type=ValueType.STR,
        categories=frozenset({_C.EMS_SCADA}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="ride_through_coordination",
        shape=Shape.SCALAR,
        value_type=ValueType.BOOL,
        categories=frozenset({_C.EMS_SCADA}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="automatic_generation_control",
        shape=Shape.SCALAR,
        value_type=ValueType.BOOL,
        categories=frozenset({_C.EMS_SCADA}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="protocols",
        shape=Shape.LIST,
        value_type=ValueType.STR,
        categories=frozenset({_C.EMS_SCADA}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="ercot_telemetry",
        shape=Shape.SCALAR,
        value_type=ValueType.BOOL,
        categories=frozenset({_C.EMS_SCADA}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="pmu_support",
        shape=Shape.SCALAR,
        value_type=ValueType.BOOL,
        categories=frozenset({_C.EMS_SCADA}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="cybersecurity_standards",
        shape=Shape.LIST,
        value_type=ValueType.STR,
        categories=frozenset({_C.EMS_SCADA}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="inverter_integration",
        shape=Shape.LIST,
        value_type=ValueType.STR,
        categories=frozenset({_C.EMS_SCADA}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="bess_integration",
        shape=Shape.LIST,
        value_type=ValueType.STR,
        categories=frozenset({_C.EMS_SCADA}),
        scope=FieldScope.CATEGORY,
    ),
    FieldSpec(
        key="support_terms",
        shape=Shape.SCALAR,
        value_type=ValueType.STR,
        categories=frozenset({_C.EMS_SCADA}),
        scope=FieldScope.CATEGORY,
    ),
    # --- Cross-category fields ---
    FieldSpec(
        key="supplier",
        shape=Shape.SCALAR,
        value_type=ValueType.STR,
        categories=ALL_CATEGORIES,
        scope=FieldScope.CROSS_CATEGORY,
    ),
    FieldSpec(
        key="supplier_verbatim",
        shape=Shape.SCALAR,
        value_type=ValueType.STR,
        categories=ALL_CATEGORIES,
        scope=FieldScope.CROSS_CATEGORY,
    ),
    FieldSpec(
        key="model",
        shape=Shape.SCALAR,
        value_type=ValueType.STR,
        categories=ALL_CATEGORIES,
        scope=FieldScope.CROSS_CATEGORY,
    ),
    FieldSpec(
        key="model_verbatim",
        shape=Shape.SCALAR,
        value_type=ValueType.STR,
        categories=ALL_CATEGORIES,
        scope=FieldScope.CROSS_CATEGORY,
    ),
    FieldSpec(
        key="component_category",
        shape=Shape.SCALAR,
        value_type=ValueType.COMPONENT_CATEGORY,
        categories=ALL_CATEGORIES,
        scope=FieldScope.CROSS_CATEGORY,
    ),
    FieldSpec(
        key="datasheet_revision",
        shape=Shape.SCALAR,
        value_type=ValueType.STR,
        categories=ALL_CATEGORIES,
        scope=FieldScope.CROSS_CATEGORY,
    ),
    FieldSpec(
        key="datasheet_date",
        shape=Shape.SCALAR,
        value_type=ValueType.DATE,
        categories=ALL_CATEGORIES,
        scope=FieldScope.CROSS_CATEGORY,
    ),
    # --- Compliance and tax fields ---
    FieldSpec(
        key="baba_status",
        shape=Shape.SCALAR,
        value_type=ValueType.STR,
        enum=frozenset({"compliant", "non_compliant", "not_applicable", "unconfirmed"}),
        categories=ALL_CATEGORIES,
        scope=FieldScope.COMPLIANCE,
    ),
    FieldSpec(
        key="baba_certification_ref",
        shape=Shape.SCALAR,
        value_type=ValueType.STR,
        categories=ALL_CATEGORIES,
        scope=FieldScope.COMPLIANCE,
    ),
    FieldSpec(
        key="country_of_origin",
        shape=Shape.SCALAR,
        value_type=ValueType.STR,
        categories=ALL_CATEGORIES,
        scope=FieldScope.COMPLIANCE,
    ),
    FieldSpec(
        key="domestic_content_percentage",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        categories=ALL_CATEGORIES,
        scope=FieldScope.COMPLIANCE,
    ),
    FieldSpec(
        key="feoc_pfe_status",
        shape=Shape.SCALAR,
        value_type=ValueType.STR,
        enum=frozenset({"qualified", "not_qualified", "unconfirmed"}),
        categories=ALL_CATEGORIES,
        scope=FieldScope.COMPLIANCE,
    ),
    FieldSpec(
        key="material_assistance_cost_ratio",
        shape=Shape.SCALAR,
        value_type=ValueType.FLOAT,
        categories=ALL_CATEGORIES,
        scope=FieldScope.COMPLIANCE,
    ),
    FieldSpec(
        key="ercot_compliance_items",
        shape=Shape.MAP,
        value_type=ValueType.STR,
        map_key_type=ValueType.STR,
        categories=ALL_CATEGORIES,
        scope=FieldScope.COMPLIANCE,
    ),
)


#: Where the frozen contract is ambiguous, recorded rather than resolved here.
#:
#: The repo's rule is that the contract governs and code that disagrees is the
#: defect, so a reading is never quietly adopted. The Compliance and tax table has
#: **no `canonical unit` column** - its three columns are key, type, notes - and
#: two of its rows put a bare `%` in the notes cell where every per-category row
#: would have put it in the unit column. The registry follows the column that
#: exists, so both specs carry `unit=None`, and the rows are named here so the
#: next reader finds the question instead of the silence.
#:
#: Resolving it is a contract edit (add the column, or move the unit into prose),
#: not a code change, and it is deliberately left to the contract's owner.
CONTRACT_UNIT_AMBIGUITIES: dict[str, str] = {
    "domestic_content_percentage": (
        "Declared `float` with a bare `%` in the notes cell of a table that has no "
        "canonical-unit column. Read as unitless here. The neighbouring "
        "`domestic_content_status` is categorical, so nothing else in the pair "
        "settles it."
    ),
    "material_assistance_cost_ratio": (
        "Same shape: `%` in the notes cell, plus 'threshold differs for solar vs "
        "storage', which is a second fact the row has nowhere to put. A ratio "
        "stated in percent and one stated as a fraction differ by 100x, and this "
        "field decides a tax position."
    ),
}


def _index() -> dict[ComponentCategory, dict[str, FieldSpec]]:
    """`(category, key) -> spec`, built once and checked for collisions.

    A dict comprehension would let a duplicate resolve to whichever spec came
    last, silently. With `insulation_type` deliberately declared twice that is a
    live hazard rather than a hypothetical one, so the collision is raised at
    import - the earliest point anything can observe it.
    """
    index: dict[ComponentCategory, dict[str, FieldSpec]] = {c: {} for c in ComponentCategory}
    for spec in FIELD_SPECS:
        for category in spec.categories:
            if spec.key in index[category]:
                raise ValueError(
                    f"{spec.key!r} is declared twice for {category.value}; one of the "
                    "two would win silently and the other would never be consulted."
                )
            index[category][spec.key] = spec
    return index


_BY_CATEGORY = _index()

#: Every key the contract defines, for any category. Answers "is this a contract
#: key at all" - which is all a per-key table such as `FIELD_TOLERANCES` can ask,
#: since D-2 assigns a tolerance to a parameter rather than to a tab.
CONTRACT_KEYS: frozenset[str] = frozenset(spec.key for spec in FIELD_SPECS)


#: `key -> the dimensions that gate it`, folded once from the specs.
#:
#: Built here rather than looked up per call because `comparison_pairs` asks
#: C(n,2) times per field, and because folding is where a disagreement between
#: two specs for one key has to be caught: `insulation_type` is deliberately
#: declared twice, and a union would silently widen the gate of the tab whose row
#: says less. Widening the gate is the suppressing direction, so it raises.
def _condition_dimensions() -> dict[str, frozenset[str]]:
    folded: dict[str, frozenset[str]] = {}
    for spec in FIELD_SPECS:
        if spec.key in folded and folded[spec.key] != spec.condition_dimensions:
            raise ValueError(
                f"{spec.key!r} has two specs whose condition dimensions differ "
                f"({sorted(folded[spec.key])} vs {sorted(spec.condition_dimensions)}). "
                "One key gets one gate, because `comparison_pairs` is given a field "
                "name and not a category; reconcile the two rows, or the tab whose "
                "row says less would quietly inherit the other's suppression."
            )
        folded[spec.key] = spec.condition_dimensions
    return folded


_CONDITION_DIMENSIONS = _condition_dimensions()


def condition_dimensions_for(key: str) -> frozenset[str]:
    """The `ConditionDimensions` names that gate a comparison of `key`.

    **Total, and empty for anything the contract does not govern.** An unknown
    key gates on nothing rather than raising or gating on everything, and the
    direction is the point: an over-wide gate refuses a comparison, and nothing
    surfaces a comparison that never happened, so the failure cannot be reviewed.
    An under-wide gate raises a false conflict, which is one queue item a
    reviewer dismisses. tasks.md E.3a makes the first a spec violation where the
    second is only a cost. `tolerance_for` falls back for the same reason.

    Keyed on the bare `key` rather than `(key, category)` even though the
    registry is indexed both ways, because `comparison_pairs` is given a field
    name and there is no category at that boundary. Only `insulation_type` has
    two specs and both gate on nothing; `_condition_dimensions` raises if that
    ever stops being true rather than picking one.
    """
    return _CONDITION_DIMENSIONS.get(key, frozenset())


def keys_for(category: ComponentCategory) -> frozenset[str]:
    """Every key legal on a `ComponentInstance` of this category.

    The per-category parameters plus the cross-category and compliance ones, which
    the contract says are present on every instance regardless of category.
    """
    return frozenset(_BY_CATEGORY[category])


def spec_for(key: str, category: ComponentCategory) -> FieldSpec | None:
    """The spec for `key` on `category`, or `None` if the category does not have it.

    The category is required rather than optional, and that is the whole point:
    `insulation_type` has two specs, so a lookup without a category would have to
    pick one, and picking is how a cable acquires a transformer's vocabulary.
    Callers that only need "is this a contract key" want `is_contract_key`.
    """
    return _BY_CATEGORY[category].get(key)


def is_contract_key(key: str, category: ComponentCategory | None = None) -> bool:
    """Whether `key` is a contract key - for `category` if one is given.

    `category=None` is the weaker question and it is answered honestly: it tests
    membership in the union, which passes `chemistry` on a PV module. Callers that
    know the category should pass it; the union form exists for the tables that
    are genuinely per-key rather than per-tab.
    """
    if category is None:
        return key in CONTRACT_KEYS
    return key in _BY_CATEGORY[category]


def require_contract_key(key: str, category: ComponentCategory | None = None) -> None:
    """Raise unless `key` is on contract - for `category` if one is given.

    A guard rather than a lookup, so the two enforcement points share one message
    and one failure mode. The message distinguishes the two ways to be off
    contract, because "the contract does not have `chemistry`" is false and would
    send a reader to the wrong document: the contract has it, on another tab.
    """
    if is_contract_key(key, category):
        return
    if category is not None and key in CONTRACT_KEYS:
        admitted = sorted(c.value for c in ComponentCategory if key in _BY_CATEGORY[c])
        raise OffContractFieldError(
            f"{key!r} is a contract key but not for {category.value}; the frozen "
            f"contract lists it under {admitted}. The category is part of the key - "
            "see contracts/canonical-parameters.md."
        )
    raise OffContractFieldError(
        f"{key!r} is not in the frozen contract"
        + (f" for {category.value}" if category is not None else "")
        + ". Add the parameter to contracts/canonical-parameters.md and to "
        "schema/registry.py before anything writes it: claims are append-only, so a "
        "row under an invented key can only be superseded, never corrected."
    )
