"""Closed vocabularies from TRS v2.

Every value here is enumerated explicitly in the spec. Where the spec fixes a
list, it is reproduced verbatim rather than left open, because the Excel output
(FR-OUT-02) and the conflict queue (FR-HITL-01/04) both depend on the exact set.
"""

from enum import IntEnum, StrEnum


class SourceTier(StrEnum):
    """TRS section 1, "hard rule".

    Ingested contracts and spec sheets are the system of record. Web data may
    populate an empty field but must never overwrite a system-of-record value
    (FR-WEB-03), and disagreements between the two are never auto-arbitrated
    (FR-HITL-02).
    """

    SYSTEM_OF_RECORD = "system_of_record"
    WEB_SUPPLEMENT = "web_supplement"


class ComponentCategory(StrEnum):
    """The eight component categories. Tabs 1-8 of the workbook (FR-OUT-02).

    Note: the FRD's comparison table merges cabling and combiner boxes into one
    row, giving seven. The TRS splits them into tabs 5 and 6. This follows the
    TRS. See docs/open-questions.md.
    """

    PV_MODULES = "pv_modules"
    INVERTERS_PCS = "inverters_pcs"
    TRACKERS_MOUNTING = "trackers_mounting"
    TRANSFORMERS = "transformers"
    CABLING_WIRING = "cabling_wiring"
    COMBINER_BOXES = "combiner_boxes"
    BESS = "bess"
    EMS_SCADA = "ems_scada"


class DocumentType(StrEnum):
    """The eight document types each ingested file is classified into (FR-ING-06)."""

    CONTRACT_TOS = "contract_tos"
    PURCHASE_ORDER = "purchase_order"
    ENVIRONMENTAL_REGULATION = "environmental_regulation"
    TERMS_AND_CONDITIONS = "terms_and_conditions"
    WARRANTY = "warranty"
    TECHNICAL_DOCUMENTATION = "technical_documentation"
    PRICING = "pricing"
    SPEC_SHEET = "spec_sheet"


#: Document types that contain confidential commercial or contractual terms by
#: definition (NFR-03, C-6, D-15).
RESTRICTED_DOCUMENT_TYPES: frozenset[DocumentType] = frozenset(
    {
        DocumentType.CONTRACT_TOS,
        DocumentType.PURCHASE_ORDER,
        DocumentType.PRICING,
        DocumentType.TERMS_AND_CONDITIONS,
        DocumentType.WARRANTY,
    }
)


class ConflictClass(StrEnum):
    """The five conflict classes detected at field level (FR-HITL-01)."""

    RECORD_VS_WEB = "record_vs_web"
    INTER_DOCUMENT = "inter_document"
    INTRA_DOCUMENT = "intra_document"
    TEMPORAL = "temporal"
    UNIT_NORMALIZATION = "unit_normalization"


class ConflictStatus(StrEnum):
    """Lifecycle of a field's conflict state.

    NONE and INSUFFICIENT_EVIDENCE are not named as such in the TRS, but the
    field object carries a `conflict_status` for every field, so the
    no-conflict and the FR-RAG-04 "insufficient evidence" states need
    representation alongside the open/resolved states.
    """

    NONE = "none"
    OPEN = "open"
    RESOLVED = "resolved"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class UnresolvedStatus(StrEnum):
    """The conflict states a field can *store* (D-18).

    RESOLVED is not a member: it is derived from `CanonicalField.resolution`
    being present. Typing the stored field as `ConflictStatus` left RESOLVED
    representable, so `evolve(conflict_status=OPEN)` could rewrite the stored
    enum while leaving the decision in place.
    """

    NONE = "none"
    OPEN = "open"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class MeasurementBasis(StrEnum):
    """What a rating is measured against (contract, Conditions table).

    One vocabulary spanning three families, because `basis` is one field:
    PV electrical, inverter MPPT window, and BESS energy. Kept as a single enum
    rather than three so `Condition` stays one shape across categories; which
    tokens are legal for which family is the contract's table, not this type's job.
    """

    # PV electrical
    STC = "stc"
    NMOT = "nmot"
    NOCT = "noct"
    BNPI = "bnpi"
    BSTC = "bstc"
    """Bifacial STC per IEC TS 60904-1-2, which the contract cites for
    bifaciality alongside BNPI. Omitting it left one of the two bifacial ratings
    unrepresentable."""

    PTC = "ptc"
    """PVUSA Test Conditions - the CEC list's own power column, and D-8 makes
    that list the authority for PV. Not an optional extra: clarifications.md's
    cross-validation table gates `PTC / STC` to 87-96% and tasks.md B.5 makes it
    a hard gate, so a corpus built from the CEC list carries a PTC power for
    every module. Its absence had no honest encoding - leaving `basis` unset
    made a 509.9 W PTC figure comparable with a 550 W STC one, a ~10% false
    conflict on every row."""

    # Inverter MPPT window - `500-1500 V` and `860-1330 V` are different fields,
    # not a discrepancy.
    FULL_RANGE = "full_range"
    FULL_POWER = "full_power"

    # BESS energy. BOL and EOL differ ~26% on real projects.
    NAMEPLATE = "nameplate"
    BOL = "bol"
    FAT = "fat"
    SAT_1MO = "sat_1mo"
    SAT_3MO = "sat_3mo"
    SAT = "sat"
    """Site acceptance, epoch not stated. A third member rather than an alias of
    either dated one: `basis` is a grouping dimension, so aliasing `sat` onto
    `sat_1mo` would silently merge one-month and three-month measurements - the
    same class of merge `bol`-vs-`eol` exists to prevent. Now that the vocabulary
    is closed, its absence would instead be a validation failure on any datasheet
    that prints "SAT" undated. See open-decisions.md decision 6."""

    EOL = "eol"

    # BESS cycle life. The contract writes this family as "EOL SOH threshold
    # (60/70/80%)" - a percentage, where every other `basis` value is a token.
    # Encoded as tokens so the field keeps one type; flagged in open-decisions
    # because it is an interpretation of a frozen contract, not a reading of it.
    SOH_60 = "soh_60"
    SOH_70 = "soh_70"
    SOH_80 = "soh_80"


class ToleranceKind(StrEnum):
    """How a printed tolerance band is expressed (contract, "Declared bands").

    Both are in current use across the five largest module suppliers, and they
    are not interchangeable: Jinko's `0~+3%` on a 620 W module is `0~+18.6 W`,
    nearly four bin steps, where Trina's `0~+5W` is 0.8%. Code assuming either
    convention is wrong for at least two of the five. See issue #2.
    """

    ABSOLUTE = "absolute"
    """Offsets in the parameter's own unit, e.g. `0 ~ +5 W`."""

    RELATIVE = "relative"
    """Offsets in percentage points of the nominal, e.g. `0~+3%`. Resolved
    against the nominal at comparison time, never at extraction - the contract's
    rule, because one printed tolerance covers a whole 605-625 W family and
    multiplying early fabricates a disagreement between rows whose source text is
    character-identical."""


class ToleranceRule(StrEnum):
    """How far two extracted values of one field may diverge (clarifications D-2).

    Distinct from `ToleranceKind`, which describes a band the *source prints*.
    This one is a policy about extraction disagreement; that one is data about
    the product. Conflating them was the original error - see the contract's
    "Declared bands" section.

    D-2's heading says "three kinds" but its table carries four, and two further
    rows are neither a number nor a band. All six are represented here rather
    than folded, because each changes the comparison rather than its threshold.
    """

    EXACT = "exact"
    """Catalog and label fields. A difference *is* a different product - nobody
    measured it, it was chosen. 99.1% of 21,989 CEC PV rows have a nameplate that
    is an exact multiple of 5 W."""

    ABSOLUTE = "absolute"
    """Small-magnitude quantities: temperature coefficients, efficiencies in
    percentage points. A relative band is meaningless at these magnitudes."""

    RELATIVE = "relative"
    """Large-magnitude measured quantities where uncertainty scales with value."""

    ONE_SIDED = "one_sided"
    """Transformer losses and no-load current. Both IEC and IEEE state these as
    upper limits only, and IEC 60076-1 Table 1 notes that an omitted direction is
    unrestricted - so being under guarantee is never a nonconformity. A symmetric
    band is wrong in both regimes."""

    DECLARED_BAND = "declared_band"
    """Compare guaranteed ranges, not numbers. The source prints its own
    tolerance and it supersedes any config default for that field."""

    NEVER_COMPARE = "never_compare"
    """Different physical quantities that share a field name in careless sources -
    inverter kVA against kW. Not a wide tolerance: not a comparison."""

    SET_EQUAL = "set_equal"
    """`list[str]` contract fields (D-17). Order is typography; compare as a set
    of normalised elements. Containment is a conflict: for an attestation,
    absence is the finding."""


class ToleranceCondition(StrEnum):
    """Discriminators for the D-2 rows that state two bands, not one.

    Four rows of D-2's table are conditional. Transcribing only the first branch
    is silent: the comparison applies a band that is right half the time and
    nothing marks which half. Each member here is evaluated against the two
    candidates being compared, so the alternate branch is selected from the data
    rather than assumed.
    """

    IMPEDANCE_BELOW_10_PCT = "impedance_below_10_pct"
    """IEC 60076-1 Table 1 item 3a: +/-7.5% if Z>=10%, +/-10% if Z<10%."""

    REGIME_IEEE = "regime_ieee"
    """Transformer total losses: +10% under IEC, +6% under IEEE. Read from
    `Condition.standards_regime`, which D-6 already makes load-bearing."""

    AGAINST_CEC_LIST = "against_cec_list"
    """CEC weighted efficiency: 0.1 pp datasheet-to-datasheet, 0.25 pp against
    the CEC list, whose headline column is quantized to 0.5 pp - only 21 distinct
    values across 2,104 rows. Read from `SourceRef.source_authority`."""


class PowerSide(StrEnum):
    """Which side of the PCS a BESS rating is taken on. Straddling it is ~28%."""

    AC = "ac"
    DC = "dc"


class EfficiencyWeighting(StrEnum):
    """Inverter efficiency weighting. 99.02 max / 98.5 CEC / 98.8 European is one
    product, not three."""

    MAX = "max"
    CEC = "cec"
    EUROPEAN = "european"


class StandardsRegime(StrEnum):
    """IEEE lists multi-cooling ratings base-first, IEC top-first. "Take the first
    number" is right for one and wrong for the other."""

    IEEE = "ieee"
    IEC = "iec"


class RteBoundary(StrEnum):
    """Where a BESS round-trip efficiency is measured (clarifications.md).

    Closed, because leaving it an open string reproduced one level down the
    silent suppression #16 set out to fix: five spellings of the Tesla Megablock
    boundary formed five groups, so two extractions of the *same* boundary
    stopped comparing at all. That is strictly worse than the `note` routing it
    replaced, which at least compared them.

    "No boundary stated" is `None`, not a member - absent is unknown, not a
    fifth boundary.
    """

    DC_DC_TERMINALS = "dc_dc_terminals"
    """DC-DC at container terminals. CATL EnerC+ quotes 95% here."""

    DC_ROUND_TRIP = "dc_round_trip"
    """DC round trip. Powin quotes 93/94/95% at 2h/3h/4h."""

    AC_AC_LV_INCL_THERMAL = "ac_ac_lv_incl_thermal"
    """AC-AC at 480 V including thermal. Tesla MP2XL: 91.7% at 2h, 93.7% at 4h."""

    AC_AC_MV_INCL_AUX = "ac_ac_mv_incl_aux"
    """AC-AC at medium voltage including auxiliaries. Tesla Megablock: 91%.

    Bridging from DC-DC costs ~2-4 pp (PCS) + 1-2 pp (aux) + ~1 pp (MV
    transformer), which is why "CATL 95% beats Tesla 93.7%" is meaningless."""


class Severity(IntEnum):
    """How much a single unresolved conflict should hold up the workbook.

    An `IntEnum` with **higher = worse**, stated explicitly because the compose
    gate compares against a threshold and a bare `int` cannot carry the direction
    of the scale - a P1/P2/P3 reading would invert it. See issue #14.

    Severity is a property of the *field*, not of the size of the disagreement:
    a 1% divergence on domestic-content status matters more than a 5% divergence
    on module weight. The assignment rule lives with the tolerance table
    (clarifications.md D-2/D-3); this enum only fixes the vocabulary and ordering.
    """

    INFORMATIONAL = 0
    """Descriptive fields. Surfaced in the workbook, never blocks composition."""

    LOW = 1
    """Comparison-relevant but not decision-driving: dimensions, weight, comms."""

    MEDIUM = 2
    """Decision-driving specs: nameplate power, efficiency, capacity, cycle life."""

    HIGH = 3
    """Money or eligibility: pricing, $/W, warranty terms, domestic content."""

    CRITICAL = 4
    """Presence or absence of a certification. Never inferable from a gap -
    'not extracted' is not 'not certified' (the CUAD absence-is-the-finding rule)."""


class ResolutionAction(StrEnum):
    """The five actions a human may take on a queued conflict (FR-HITL-04)."""

    SELECT_VALUE = "select_value"
    ENTER_OVERRIDE = "enter_override"
    KEEP_SYSTEM_OF_RECORD = "keep_system_of_record"
    REQUEST_MORE_WEB_SEARCH = "request_more_web_search"
    DEFER = "defer"

    @property
    def asserts_a_value(self) -> bool:
        """Whether this action states what the field's value *is* (D-16).

        Three do, and may therefore be recorded as a human claim. The two that
        do not - REQUEST_MORE_WEB_SEARCH and DEFER - are events against the
        conflict, recorded in `resolution` and `audit.event` but never in the
        value store. Spelled as the complement so that a sixth action added
        here is settling by default and its author has to say otherwise.
        """
        return self not in (ResolutionAction.REQUEST_MORE_WEB_SEARCH, ResolutionAction.DEFER)


class WorkbookTab(StrEnum):
    """All thirteen tabs, in order (FR-OUT-02). AC-3 checks every one is present."""

    PV_MODULES = "PV Modules"
    INVERTERS_PCS = "Inverters-PCS"
    TRACKERS_MOUNTING = "Trackers & Mounting"
    TRANSFORMERS = "Transformers"
    CABLING_WIRING = "Cabling & Wiring"
    COMBINER_BOXES = "Combiner Boxes"
    BESS = "BESS"
    EMS_SCADA = "EMS-SCADA & Controls"
    EXECUTIVE_SUMMARY = "Executive Summary"
    CONFLICTS_OPEN_ITEMS = "Conflicts & Open Items"
    SOURCES_PROVENANCE = "Sources & Provenance"
    COMPLIANCE_MATRIX = "Compliance Matrix"
    TAX_INCENTIVES = "Tax Incentives"


#: Category tabs in workbook order, so the writer and AC-3 agree on the mapping.
CATEGORY_TO_TAB: dict[ComponentCategory, WorkbookTab] = {
    ComponentCategory.PV_MODULES: WorkbookTab.PV_MODULES,
    ComponentCategory.INVERTERS_PCS: WorkbookTab.INVERTERS_PCS,
    ComponentCategory.TRACKERS_MOUNTING: WorkbookTab.TRACKERS_MOUNTING,
    ComponentCategory.TRANSFORMERS: WorkbookTab.TRANSFORMERS,
    ComponentCategory.CABLING_WIRING: WorkbookTab.CABLING_WIRING,
    ComponentCategory.COMBINER_BOXES: WorkbookTab.COMBINER_BOXES,
    ComponentCategory.BESS: WorkbookTab.BESS,
    ComponentCategory.EMS_SCADA: WorkbookTab.EMS_SCADA,
}


class CellFlag(StrEnum):
    """The four states conditional formatting must distinguish (FR-OUT-04)."""

    UNRESOLVED_CONFLICT = "unresolved_conflict"
    WEB_SUPPLEMENTED = "web_supplemented"
    LOW_CONFIDENCE = "low_confidence"
    MISSING_DATA = "missing_data"
