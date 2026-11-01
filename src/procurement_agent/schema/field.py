"""The canonical field object.

TRS section 5 specifies this exactly:

    Each field is an object:
    { value, unit, verbatim_value, source_tier, source_ref, confidence,
      conflict_status, resolution }

Those eight keys are reproduced verbatim. The canonical store built from them is
the single source for Excel regeneration and the audit trail, so FR-OUT-06's
determinism requirement rests on this type being stable.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    computed_field,
    field_serializer,
    field_validator,
    model_validator,
)

from .encoding import is_aware
from .enums import (
    ComponentCategory,
    ConflictClass,
    ConflictStatus,
    EfficiencyWeighting,
    MeasurementBasis,
    PowerSide,
    ResolutionAction,
    RteBoundary,
    Severity,
    SourceTier,
    StandardsRegime,
    ToleranceKind,
    UnresolvedStatus,
)

#: Every model in this module sets it. A key nobody declared is a key nobody
#: reads, and the two ways that has already cost this repo are the same shape:
#: issue #16 closed the condition *vocabulary*, so `basis="not_a_basis"` raises -
#: while `Condition(ambient_temperature_c=30.0)` was still accepted one level up,
#: because that is what pydantic's default `extra="ignore"` does. The real field
#: is `temperature_c`, the plan's own prose calls it "reference ambient", and the
#: result was an `is_unstated()` condition that reported `comparable_with` True
#: against a genuine 40 degC reading. D-1's silent merge, reached by misspelling
#: rather than by omission.
#:
#: `model_validate` is the route that matters most: a constructor typo is a bug
#: someone is about to hit, while a stray key in a row read back from the store
#: is schema drift nobody sees.
_FORBID_UNDECLARED_KEYS: Literal["forbid"] = "forbid"


class SourceRef(BaseModel):
    """Where a value came from. NFR-01 permits no unsourced values.

    A document reference carries doc_id plus page and table/section (FR-ING-07).
    A web reference carries url, page_title and retrieved_at (FR-WEB-02).
    """

    model_config = ConfigDict(frozen=True, extra=_FORBID_UNDECLARED_KEYS)

    document_id: str | None = None
    page: int | None = None
    section: str | None = Field(default=None, description="Table or section locator")
    extractor_version: str | None = Field(
        default=None,
        description=(
            "The code and prompt that produced the value. Contract C3 is "
            "`(document_id, page, span, extractor_version)`, whose `span` is the "
            "`section` field above - the one element of the four whose contract "
            "name matches no attribute here, so grepping for `span` finds nothing "
            "and reads as a missing element. This was the "
            "missing quarter: `FieldClaim` carried it, and the projection to a "
            "`CanonicalField` dropped it, so a stored value could not be traced "
            "to the extractor that produced it. A regression is then invisible "
            "in the store even though the claim recording it is still there."
        ),
    )
    bounding_box: tuple[float, float, float, float] | None = Field(
        default=None, description="OCR bbox retained per FR-ING-04"
    )

    url: str | None = None
    page_title: str | None = None
    retrieved_at: datetime | None = Field(
        default=None,
        description=(
            "When the web source was fetched (FR-WEB-02). **Timezone-aware, and "
            "checked.** A naive datetime names no instant, so `encode_value` "
            "refuses one - which meant the schema accepted a value the canonical "
            "encoder would then raise on, from the conflict sort path, where "
            "`repr()` had previously ordered it without complaint. Found by "
            "Track 1b. The constraint belongs here rather than in the encoder's "
            "caller: the honest fix is to attach the zone at the boundary that "
            "produced the timestamp, and this is the first boundary that can say "
            "so. Nothing in the repo constructs a naive one today, so this "
            "closes the hole rather than reporting one."
        ),
    )
    source_authority: str | None = Field(
        default=None,
        description=(
            "FR-WEB-05 authority tier, e.g. manufacturer datasheet, UL/TUV/Intertek, "
            "IEEE/NFPA, ERCOT/PUCT/TCEQ, IRS/Treasury"
        ),
    )

    @field_validator("retrieved_at")
    @classmethod
    def _must_name_an_instant(cls, value: datetime | None) -> datetime | None:
        """Reject a naive `retrieved_at`, for the reason `encode_value` rejects one.

        A naive datetime is a wall-clock reading with no zone, so it does not
        identify a moment - and `retrieved_at` exists precisely to say *when* a web
        value was true, which is what FR-WEB-04's temporal comparisons rest on.
        Assuming UTC here would be worse than refusing: a naive noon and an aware
        noon-UTC are not equal in Python, so two references to one fetch would
        compare unequal while encoding identically.
        """
        if value is not None and not is_aware(value):
            raise ValueError(
                "retrieved_at must be timezone-aware; a naive datetime names no "
                "instant. Attach the zone at the boundary that produced it."
            )
        return value

    @model_validator(mode="after")
    def _must_identify_a_source(self) -> SourceRef:
        """NFR-01: every value traces to a source. An empty ref is never valid."""
        if self.document_id is None and self.url is None:
            raise ValueError("SourceRef requires either document_id or url (NFR-01)")
        return self


def _aware_datetime(value: datetime, slot: str) -> datetime:
    """Reject a naive timestamp. Shared by every hashed datetime slot.

    A naive datetime names no instant, so `encode_value` refuses it. Accepting
    one here would recreate the SourceRef hole: the schema calls the value legal
    and the encoder then raises from a sort or hash path.
    """
    if not is_aware(value):
        raise ValueError(
            f"{slot} must be timezone-aware; a naive datetime names no instant. "
            "Attach the zone at the boundary that produced it."
        )
    return value


class Resolution(BaseModel):
    """A human decision on a queued conflict. Logged immutably per FR-HITL-06."""

    model_config = ConfigDict(frozen=True, extra=_FORBID_UNDECLARED_KEYS)

    action: ResolutionAction
    resolved_by: str
    resolved_at: datetime
    rationale: str
    value_before: object | None = None
    value_after: object | None = None

    @field_validator("resolved_at")
    @classmethod
    def _resolved_at_must_name_an_instant(cls, value: datetime) -> datetime:
        return _aware_datetime(value, "resolved_at")

    @field_validator("value_before", "value_after")
    @classmethod
    def _values_must_be_encodable(cls, value: object) -> object:
        return _reject_non_finite_value(value)


def _as_resolution(value: object) -> object:
    """The `Resolution` a caller meant, for comparison only.

    Coercion is attempted, never forced: a value that is not a valid
    `Resolution` comes back unchanged, so it still compares unequal. This
    decides only *whether the value differs* from a recorded decision;
    assignment itself goes on to `validate_assignment`, which parses it. Needed
    because a replay from a stored row arrives as a `dict`, and comparing that
    against a `Resolution` before coercion refused every idempotent replay
    (A-55).
    """
    if isinstance(value, Resolution) or value is None:
        return value
    try:
        return Resolution.model_validate(value)
    except ValidationError:
        return value


#: The one refusal both routes raise, so the rule is stated once.
_RESOLUTION_IS_APPEND_ONLY = (
    "a recorded Resolution cannot be replaced or cleared (FR-HITL-06: the decision "
    "log is immutable). A reopened conflict (REQUEST_MORE_WEB_SEARCH) records a NEW "
    "resolution against the conflict; it does not rewrite this one."
)


class DeclaredBandUnitError(ValueError):
    """A declared band was asked to resolve against a nominal in another unit.

    A `ValueError` so a pydantic validator could raise it, and a named subclass
    so a caller can tell "these two numbers are not in the same scale" apart from
    every other way arithmetic can fail. `IncomparableCandidatesError` is the
    same idea one layer up: refusing to answer is a different outcome from
    answering "they agree".
    """


_RATE_UNIT_ALIASES: dict[str, str] = {
    "%/k": "%/degc",
    "%/°c": "%/degc",
}


def _comparable_unit(value: str | None) -> str | None:
    """A unit folded for equality, the way `conflict_hitl._normalise_unit` folds
    one: NFKC, case, internal whitespace, slash spacing, and D-5's rate aliases.

    Spelling only. `w` and `W` are one unit; `W` and `Wp` are not, and neither
    are `W` and `kW`. Deciding that two differently-spelled units name one
    quantity is a technical claim, and the one place this repo makes such a claim
    - `%/degC` against `%/K` - is shared here so `DeclaredBand` agrees with
    `values_conflict` (D-5).
    """
    if value is None:
        return None
    folded = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    if not folded:
        return None
    compact = "/".join(part.strip() for part in folded.split("/"))
    return _RATE_UNIT_ALIASES.get(compact, compact)


class DeclaredBand(BaseModel):
    """A tolerance the source itself prints, stored as written.

    Shape fixed by the frozen contract's *Declared bands* section:
    `{ low, high, kind, unit }`. A declared band is **not** the conflict tolerance
    of clarifications.md D-2, which governs how far two independently extracted
    values may diverge before a human is asked; conflating the two was the
    original error. This one says how tightly the supplier guarantees a single
    number.

    Note on placement: issue #2 proposed a `declared_tolerance` key on
    `CanonicalField`. The contract instead types `power_tolerance` and
    `bifaciality_tolerance` *as* `DeclaredBand`, so a band is a field value like
    any other and carries its own provenance, conditions and conflict state for
    free. The contract outranks the issue (open-decisions rule 1), and a second
    home for the same object would be a second thing to keep in sync.
    """

    model_config = ConfigDict(frozen=True, extra=_FORBID_UNDECLARED_KEYS)

    low: float = Field(description="Lower offset from nominal. `0` for a one-sided `0 ~ +5 W`.")
    high: float = Field(description="Upper offset from nominal.")
    kind: ToleranceKind
    unit: str | None = Field(
        default=None,
        description=(
            "The unit `low`/`high` are expressed in. Required when kind is "
            "absolute and absent when it is relative, where percentage points of "
            "the nominal are implied - which is what makes the contract's "
            "`str | None` two states rather than an optional extra."
        ),
    )

    @field_validator("low", "high")
    @classmethod
    def _reject_non_finite(cls, value: float) -> float:
        """Same reasoning as `ConditionDimensions`: a NaN bound compares false
        against everything, so a band containing one would silently declare every
        value in agreement. `-0.0` is folded for the same repr-stability reason."""
        if not math.isfinite(value):
            raise ValueError("a declared band must have finite bounds (no NaN or infinity)")
        return value + 0.0

    @field_validator("unit", mode="before")
    @classmethod
    def _normalise_unit(cls, value: str | None) -> str | None:
        if value is None or not isinstance(value, str):
            return value
        return value.strip() or None

    @model_validator(mode="after")
    def _check_shape(self) -> DeclaredBand:
        if self.low > self.high:
            raise ValueError(f"declared band is inverted: low={self.low} > high={self.high}")
        if self.kind is ToleranceKind.ABSOLUTE and self.unit is None:
            raise ValueError(
                "an absolute declared band needs a unit; `0 ~ +5` alone is not a quantity"
            )
        if self.kind is ToleranceKind.RELATIVE and self.unit not in (None, "%"):
            raise ValueError(f"a relative declared band is in percentage points, not {self.unit!r}")
        return self

    def _require_same_unit(self, nominal_unit: str | None) -> None:
        """Refuse to add an offset in one unit onto a number in another.

        Only for an absolute band: `low`/`high` are a quantity, so `nominal +
        self.high` is meaningful exactly when both are in the same unit. A
        relative band is a percentage of whatever it is applied to and is
        therefore scale-free - 3% of 620 W and 3% of 0.620 kW are one band
        written twice - so the check would be wrong on that branch, not merely
        unnecessary.

        `unit` was validated at construction and read by nobody, which is FN-3.
        A `0 ~ +5 W` band against a nominal in kW gave `resolve(0.650) ==
        (0.65, 5.65)`, so 0.650 kW and 5.0 kW - a 7.7x disagreement, the 1000x
        extraction-error class D-2 calls out - had intersecting guaranteed ranges
        and raised nothing.

        Refusing rather than converting. There is no unit algebra here, and
        adding one would be the "a mismatch is resolved by normalising" move
        FR-ING-08 forbids; `values_conflict` already treats a unit mismatch
        between two candidates as a conflict in its own right for the same
        reason. Where the band's unit comes from is not left open either:
        `_check_shape` refuses an absolute band without one on the grounds that
        "the canonical field knows its unit", so the extractor has the right
        answer to write.
        """
        if self.kind is not ToleranceKind.ABSOLUTE:
            return
        if _comparable_unit(self.unit) != _comparable_unit(nominal_unit):
            raise DeclaredBandUnitError(
                f"a declared band in {self.unit!r} cannot be resolved against a "
                f"nominal in {nominal_unit!r}: `low`/`high` are offsets in the "
                "band's own unit, so adding them across a scale change silently "
                "widens or narrows the guarantee. Record the band in the "
                "canonical field's unit at extraction, where the source text and "
                "its unit are both in hand."
            )

    def resolve(self, nominal: float, *, nominal_unit: str | None) -> tuple[float, float]:
        """The guaranteed range around `nominal`, in the parameter's own unit.

        This is the only place a relative band is multiplied out, per the
        contract: doing it at extraction fabricates a disagreement between the
        605 W and 625 W rows of a Jinko sheet that print one identical tolerance,
        and it is circular when the disputed field *is* the nominal.

        `nominal_unit` is required, and required even for a relative band that
        ignores it: the caller always knows it - it is `CanonicalField.unit` -
        and a default would put the FN-3 hole back one call site away. See
        `_require_same_unit`.

        Returned lowest-first. A relative band on a negative nominal - a Pmax
        temperature coefficient of `-0.29 %/degC` is the real case - maps `low` to
        the *upper* bound, so returning `(low_offset, high_offset)` unswapped
        would hand the caller an inverted interval that intersects nothing.
        """
        self._require_same_unit(nominal_unit)
        if self.kind is ToleranceKind.RELATIVE:
            bounds = (nominal + nominal * self.low / 100.0, nominal + nominal * self.high / 100.0)
        else:
            bounds = (nominal + self.low, nominal + self.high)
        if not all(math.isfinite(bound) for bound in bounds):
            # `_reject_non_finite` guards the stored bounds because a non-finite
            # band "agrees" with everything; the arithmetic here can manufacture
            # one from finite inputs at absurd magnitudes, which would reach the
            # same place by another route.
            raise ValueError(
                f"resolving this band against {nominal!r} overflows to a non-finite range"
            )
        return (min(bounds), max(bounds))

    def agrees(
        self,
        nominal: float,
        other: DeclaredBand | None,
        other_nominal: float,
        *,
        nominal_unit: str | None,
        other_unit: str | None,
    ) -> bool:
        """Whether two nominals can denote the same physical part.

        True when their guaranteed ranges intersect. Closed intervals on purpose:
        a 650 W module declared `0 ~ +5 W` guarantees `[650, 655]` and a 655 W one
        guarantees `[655, 660]`, and a part measuring exactly 655 W satisfies both
        labels, so touching is agreement rather than the narrowest possible
        conflict.

        `other=None` means the other source printed no band, not that it has none:
        its nominal is treated as the exact point `[v, v]`. That is deliberately
        the strict reading - it can raise a conflict a shared band would have
        absorbed, which is one extra queue item, where the permissive reading
        silently merges two different SKUs.

        Both units are taken rather than one, even though `values_conflict` has
        already equated the two candidates' units before it gets here. That is
        the caller's invariant, not this method's, and the whole of FN-3 is that
        a unit nobody reads is a unit nobody enforces - so the two intervals are
        each checked against the band that produced them, and the resulting
        bounds are only intersected once both are known to be in one scale.
        """
        low, high = self.resolve(nominal, nominal_unit=nominal_unit)
        if other is None:
            other_low, other_high = other_nominal, other_nominal
        else:
            other_low, other_high = other.resolve(other_nominal, nominal_unit=other_unit)
        if _comparable_unit(nominal_unit) != _comparable_unit(other_unit):
            raise DeclaredBandUnitError(
                f"two guaranteed ranges in {nominal_unit!r} and {other_unit!r} do "
                "not intersect in any unit either of them names; a unit mismatch "
                "is never resolved by comparison (FR-ING-08)."
            )
        return low <= other_high and other_low <= high


class ConditionDimensions(BaseModel):
    """The machine-comparable dimensions of a measurement condition.

    Membership of this model *is* the definition of what gates comparison, so a
    new dimension participates automatically and a new human-readable annotation
    automatically does not. See clarifications.md D-1.

    Not intended as a field annotation anywhere: annotate with `Condition`. A
    field typed as this class accepts a `Condition` but serialises through the
    parent schema and silently drops `note`.

    `extra="forbid"` is set here and inherited by `Condition`, rather than
    restated there: pydantic merges the parent config, so one statement covers
    both and there is no second copy to drift. The subclass's own `note` and
    `derived` are declared fields, so forbidding *undeclared* keys does not
    touch them.
    """

    model_config = ConfigDict(frozen=True, extra=_FORBID_UNDECLARED_KEYS)

    basis: MeasurementBasis | None = Field(
        default=None,
        description=(
            "What the rating is measured against. Which tokens are legal depends "
            "on the parameter family - see the Conditions table in "
            "contracts/canonical-parameters.md rather than restating it here."
        ),
    )
    temperature_c: float | None = Field(
        default=None, description="Ambient the rating is stated at, e.g. 30 / 40 / 50"
    )
    side: PowerSide | None = Field(default=None, description="Load-bearing for BESS")
    duration_h: float | None = Field(
        default=None, description="Rated duration; BESS RTE is duration-dependent"
    )
    weighting: EfficiencyWeighting | None = None
    standards_regime: StandardsRegime | None = Field(
        default=None,
        description=(
            "Determines which multi-cooling rating is canonical, the loss reference "
            "temperature, and the vector-group phase convention. clarifications.md D-6."
        ),
    )
    reference_temperature_c: float | None = Field(
        default=None, description="Loss reference temperature: 20 + rise (IEEE) or 75 (IEC)"
    )
    rte_boundary: RteBoundary | None = Field(
        default=None,
        description=(
            "Where a BESS round-trip efficiency is measured. A dimension, not a "
            "note: four distinct boundaries are all called 'round-trip efficiency' "
            "and they are worth 2-7 percentage points. Closed rather than free "
            "text, because five spellings of one boundary form five groups and "
            "stop comparing - silent suppression one level below the `note` "
            "routing this replaced."
        ),
    )
    tap_position_pct: float | None = Field(
        default=None,
        description=(
            "Transformer tap a %Z or loss figure is stated at, as a percentage "
            "deviation from the principal tap: `0.0` is nominal, `5.0` is the +5% "
            "tap. A number rather than a name for the same reason `rte_boundary` "
            "is an enum - 'nominal', 'principal tap' and 'Nominal Tap' are one "
            "measurement, and as free text they were three groups that never met."
        ),
    )
    base_mva: float | None = Field(
        default=None,
        description=(
            "Rating the transformer's %Z and losses are referred to. A grouping "
            "dimension, not a note: IEEE refers impedance to the ONAN base and IEC "
            "to the top rating, so two %Z figures on different bases differ by "
            "1.25-1.67x - far beyond the +/-7.5% tolerance. See clarifications.md D-6."
        ),
    )

    @field_validator(
        "temperature_c",
        "duration_h",
        "reference_temperature_c",
        "base_mva",
        "tap_position_pct",
    )
    @classmethod
    def _reject_non_finite(cls, value: float | None) -> float | None:
        """NaN would break the equivalence relation `grouping_key` depends on.

        `NaN != NaN`, so two identically-conditioned values would land in separate
        groups and never compare - and pydantic serialises NaN to JSON `null`, so
        the same record would group differently before and after a store
        round-trip. That is precisely the FR-OUT-06 purity `grouping_key` exists to
        guarantee, so a non-finite condition is rejected at the boundary instead.
        """
        if value is None:
            return None
        if not math.isfinite(value):
            raise ValueError("condition dimensions must be finite (no NaN or infinity)")
        # -0.0 == 0.0 and hashes alike, so they are one dict key - but they have
        # different reprs, which flipped the order of a repr-sorted partition.
        return value + 0.0

    @field_validator(
        "basis", "side", "weighting", "standards_regime", "rte_boundary", mode="before"
    )
    @classmethod
    def _normalise_vocabulary(cls, value: object) -> object:
        """Fold a printed spelling to its member, then let pydantic coerce.

        `mode="before"`, so `"  STC "` becomes `"stc"` and *then* coerces to
        `MeasurementBasis.STC` rather than failing validation. With the
        vocabularies closed an unrecognised token now raises instead of silently
        forming its own group - the failure direction that cannot be reviewed,
        because nothing surfaces a comparison that never happened.
        """
        return _normalise_token(value)


#: Every dimension that can gate a comparison, derived from the model rather than
#: written out.
#:
#: Membership of `ConditionDimensions` *is* the definition of what gates
#: comparison (see its docstring), so a new dimension joins this set by being
#: declared and a new annotation stays out of it by living on `Condition`. A
#: hand-kept list here would be a second copy of that definition, and the one
#: that drifts.
#:
#: This is the *unqualified* question - "do these two conditions contradict
#: anywhere" - and it is the right argument only for a caller that has no field
#: in hand. The per-field answer is `schema.registry.condition_dimensions_for`;
#: passing this set where a field's own set belongs is FN-2 reinstated.
CONDITION_DIMENSION_NAMES: frozenset[str] = frozenset(ConditionDimensions.model_fields)


#: Printed spellings that denote a vocabulary member rather than a new value.
#:
#: Not convenience. Closing the vocabularies turned every unlisted spelling into
#: a hard validation failure, and these are the forms real sources print: this
#: repo's own text writes "ANSI/IEEE (C57.12.00 5.4)" and "ANSI submittals
#: declare `Dyn1`", while Fronius- and SMA-family sheets print "Euro efficiency".
#: Rejecting them would drop the document, which is a worse outcome than folding
#: a synonym whose meaning nothing disputes.
#:
#: Deliberately only exact synonyms. `gb` (GB 1094), `is` (IS 2026) and `csa` are
#: *derived* from one regime rather than names for it, and asserting which would
#: be a technical claim about a standard nobody here has read - see
#: open-decisions.md.
VOCABULARY_ALIASES: dict[str, str] = {
    "ansi": "ieee",
    "ansi/ieee": "ieee",
    "ieee/ansi": "ieee",
    "euro": "european",
    "eu": "european",
}

#: Characters PDF and XLSX extraction inserts that carry no meaning. `str.strip`
#: removes NBSP but none of these, so `﻿stc` was a hard validation failure
#: on a document whose only sin was a byte-order mark.
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍﻿­⁠"))


class VocabularyError(ValueError):
    """A condition dimension arrived as something other than text or a member."""


def _normalise_token(value: object) -> object:
    """Fold a printed token to its canonical member name.

    NFKC first, then case, then the invisible characters extraction leaves
    behind. A `StrEnum` member passes through untouched: folding it would work
    only by accident today, because all four vocabularies happen to be
    lowercase, and would silently break the first mixed-case member anyone adds.
    """
    if value is None or isinstance(value, StrEnum):
        return value
    if isinstance(value, bytes | bytearray):
        # pydantic decodes bytes for a str field, so `b"stc"` validated while
        # `b"STC"` raised - the same input, case-dependent, on the one path this
        # validator exists to make case-insensitive.
        raise VocabularyError(
            "condition vocabularies take text, not bytes; decode at the "
            "extraction boundary where the encoding is known"
        )
    if not isinstance(value, str):
        return value
    folded = unicodedata.normalize("NFKC", value).translate(_INVISIBLE).strip().casefold()
    # `dc-dc-terminals`, `DC DC terminals` and `dc_dc_terminals` are one token.
    # No member contains a hyphen or a space, so this cannot collide two distinct
    # members. Folding hyphens but not spaces split the difference in the
    # direction that drops documents: no datasheet prints `full_power`, they
    # print "Full power", and closing the vocabularies made that a hard failure.
    # Whitespace is collapsed first so `ANSI / IEEE` reaches its alias.
    # A slash separates alternatives rather than words, so whitespace around it
    # collapses away first: `ANSI / IEEE` has to reach the `ansi/ieee` alias,
    # not become `ansi_/_ieee`.
    folded = re.sub(r"\s*/\s*", "/", folded)
    folded = "_".join(folded.replace("-", " ").replace("_", " ").split())
    # An extractor emitting "" rather than None would otherwise read as a stated
    # dimension and strand the value in its own group.
    if not folded:
        return None
    return VOCABULARY_ALIASES.get(folded, folded)


class Condition(ConditionDimensions):
    """The measurement conditions a value holds under.

    A ninth key beyond the eight TRS section 5 fixes. Two values are comparable
    only when their conditions match; a mismatch is not a conflict, it is not a
    comparison. Rationale and the Sungrow SG350HX worked case: clarifications.md
    D-1. Which dimensions each parameter family requires: the Conditions table in
    contracts/canonical-parameters.md.
    """

    note: str | None = Field(default=None, description="Any condition not captured above")
    derived: frozenset[str] = Field(
        default=frozenset(),
        description=(
            "Dimensions filled by convention rather than read from the source. "
            "Lives on this subclass, not on ConditionDimensions, so it is excluded "
            "from grouping by construction: a defaulted STC value and a stated STC "
            "value group together while the provenance stays honest for a reviewer."
        ),
    )

    @field_serializer("derived")
    def _sort_derived(self, value: frozenset[str]) -> list[str]:
        """Sorted on the way out: a frozenset serialises in hash order, which is
        randomised per process, so the same record would produce different JSON
        bytes every run - defeating the FR-OUT-06 purity this model exists for."""
        return sorted(value)

    def is_unstated(self) -> bool:
        """Whether no comparable dimension is known at all.

        Detection does not partition, so nothing needs rescuing: see
        `services.conflict_hitl.comparison_pairs`, which compares an unstated
        condition against every condition it does not contradict.
        """
        return all(value is None for value in self.grouping_key())

    def grouping_key(self) -> tuple[object, ...]:
        """A canonical key for *displaying* candidates grouped by condition.

        Equality over this tuple is an equivalence relation - reflexive because
        non-finite floats are rejected at construction, and transitive because it
        is plain tuple equality - so partitioning by it does not depend on the
        order candidates arrive in. FR-OUT-06 requires that, since composition
        must be a pure function of the store.

        Grouping by this key alone is *comparison-losing*, though: a value whose
        conditions are merely less specific strands in its own group and is
        compared against nothing. That is why detection uses
        `services.conflict_hitl.comparison_pairs` and this key is for display.

        `note` and `derived` are excluded: free text is provenance for a human,
        and how a dimension came to be filled does not change what it says.
        """
        return tuple(getattr(self, name) for name in ConditionDimensions.model_fields)

    def comparable_with(self, other: Condition, *, dimensions: frozenset[str]) -> bool:
        """Whether these two specific values may be compared, on `dimensions`.

        Any of the named dimensions set on both sides must agree; a dimension
        absent on one side is unknown rather than contradictory.

        **`dimensions` is required, and it scopes the gate to the parameter in
        hand.** Every dimension used to gate every field, which meant a dimension
        that says nothing about the field silently refused a real disagreement:
        two sources naming different countries of origin produced no comparison
        at all because one sheet was IEEE and the other IEC. That is FN-2, and
        the direction tasks.md E.3a makes a spec violation - nothing surfaces a
        comparison that never happened.

        The rule is the contract's own, not an invention here. Its `note`
        paragraph fixes it: *"A dimension that changes what a number means
        belongs on `ConditionDimensions`; only an annotation belongs in `note`"*.
        For a field no Conditions row governs, no dimension changes what it
        means, so the honest gate is empty. Which dimensions govern which key is
        `schema.registry.condition_dimensions_for`, hand-assigned from the
        Conditions table and checked against it in both directions by
        `tests/test_condition_gate_scope.py`.

        Scoping the *call* rather than the type is deliberate. The frozen
        contract has already refused a per-family `Condition` - "Splitting it per
        family would give `Condition` a different shape per category, which the
        comparison logic cannot carry - this is a known limit, not an oversight"
        - so the shape is untouched and the caller says what it is asking about.
        `CONDITION_DIMENSION_NAMES` is the unqualified question, for a caller
        that genuinely wants "do these contradict anywhere".

        No default, for the reason `ConflictQueueEntry.severity` carries none: a
        default of "every dimension" is exactly the defect, and a forgotten
        argument would reinstate it at a call site with nothing to show for it.

        **This relation is deliberately not transitive, so it must never be used to
        partition a candidate set.** `@30 degC` and `@40 degC` are both comparable
        with an unstated condition but not with each other, so first-fit bucketing
        over the same three values yields different groups depending on the order
        they arrive - and therefore a different conflict queue from the same store.
        This is the detection predicate - `services.conflict_hitl.comparison_pairs`
        applies it to every unordered pair. Do not use it to bucket candidates;
        first-fit bucketing over a non-transitive relation gives a different
        conflict queue per document order. See issue #12.
        """
        unknown = dimensions - CONDITION_DIMENSION_NAMES
        if unknown:
            # `note` and `derived` are the two that would otherwise pass silently:
            # they are attributes of `Condition`, so `getattr` reads them, and the
            # contract says in as many words that `note` "does not gate
            # comparison". A misspelt dimension is the same hazard - `bassis`
            # would read as "nothing to check here" and suppress without a trace,
            # which is the failure direction that cannot be reviewed.
            raise ValueError(
                f"{sorted(unknown)} names nothing that gates comparison; the "
                f"dimensions are {sorted(CONDITION_DIMENSION_NAMES)}. `note` and "
                "`derived` are excluded by the contract: free text is provenance "
                "for a reviewer, and how a dimension came to be filled does not "
                "change what it says."
            )
        for name in dimensions:
            mine, theirs = getattr(self, name), getattr(other, name)
            if mine is not None and theirs is not None and mine != theirs:
                return False
        return True


def _reject_non_finite_value(value: object, _containers: tuple[int, ...] = ()) -> object:
    """Refuse a value the canonical encoder will refuse, at the point it is stored.

    `CanonicalField.value` is `object | None`, which is what the contract's type
    column requires - a value is a float, an int, a `DeclaredBand`, a `list[str]`
    or a `dict[int, float]` depending on the row - so pydantic has no schema to
    check finiteness against. Meanwhile `encoding.encode_value` refuses a
    non-finite float with an error message that says, in as many words, "The
    schema rejects these at construction". It did not.

    The gap is not cosmetic. A store could hold a row `project_store` cannot
    project, and the failure then surfaces at composition time - far from the
    extractor that wrote it, with the C6 digest as the symptom. NaN is the worst
    of the three because it is not equal to itself, so every "is this the value
    we stored" check the pipeline makes answers no.

    **Recursive, because the contract's own types are containers.**
    `harmonic_spectrum` is `dict[int, float]` and eighteen rows are `list[str]`,
    so a top-level check would guard the least likely case and miss the declared
    ones. This walks exactly what `encode_value` walks, which is what makes its
    claim true rather than nearly true.

    Nested `BaseModel`s are left to their own validators - `DeclaredBand` already
    rejects a non-finite bound, and re-checking it here would be a second
    statement of a rule that has a home. A cycle guard prevents infinite
    recursion on self-referential containers before they reach `encode_value`.
    """
    if isinstance(value, bool):  # before the numeric branch - bool is an int
        return value
    if isinstance(value, float | Decimal) and not math.isfinite(value):
        raise ValueError(
            f"non-finite value {value!r}: `encode_value` refuses it (contract C4 / "
            "D-14), so a field holding one is a stored row the C6 projection "
            "cannot project. NaN is additionally not equal to itself, so it "
            "compares unequal to the value it was read back from."
        )
    if isinstance(value, str | bytes | bytearray):
        return value
    if id(value) in _containers:
        return value
    nested = (*_containers, id(value))
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_non_finite_value(key, nested)
            _reject_non_finite_value(item, nested)
    elif isinstance(value, list | tuple | set | frozenset):
        for item in value:
            _reject_non_finite_value(item, nested)
    return value


class CanonicalField(BaseModel):
    """One extracted parameter, with provenance, conditions and conflict state.

    Nine keys on the wire: the eight fixed by TRS section 5, plus `condition`.
    The deviation is recorded in analysis.md A-1.

    **RESOLVED is derived, not stored** (D-18). The object holds
    `unresolved_status` - NONE, OPEN or INSUFFICIENT_EVIDENCE, typed so
    RESOLVED cannot be stored - and `resolution`; `conflict_status` is computed
    as RESOLVED exactly when a resolution is present, and serialises under the
    TRS's key so the wire shape is unchanged. FR-HITL-06's first invariant - a
    RESOLVED field carries the decision that resolved it - therefore has no
    counter-example to guard against: there is no combination of stored fields
    that reads as RESOLVED with no `Resolution`.

    `conflict_status=` is still accepted by the constructor, by `evolve` and by
    assignment, because it is the contract's name for the fact: a
    before-validator maps it onto the stored field, refusing RESOLVED with no
    resolution at the door, and refusing to move a resolved field off RESOLVED
    (the setter and evolve share that rule, since resolutions are append-only).

    Mutable, because a field is a working record that gains a resolution as the
    pipeline runs - with `validate_assignment`, so an assigned value is parsed.
    Freezing was considered and rejected: it would have made `model_copy` the
    only way to update a field.

    **What is still guarded, because it is the other invariant.** FR-HITL-06's
    second rule is that a recorded `Resolution` is never replaced or cleared.
    Every validator passes a swap, because the resulting state is legal; only a
    check that sees the *transition* catches it, so `__setattr__` and `evolve`
    refuse it and `model_copy(update=...)` - which skips both - is refused
    outright in favour of `evolve`. Writing the instance `__dict__` directly can
    still *erase* a resolution, because no Python object can defend that
    primitive; what it can no longer do is fabricate a resolved field.
    """

    model_config = ConfigDict(validate_assignment=True, extra=_FORBID_UNDECLARED_KEYS)

    value: object | None = None
    unit: str | None = Field(default=None, description="Canonical unit per FR-ING-08")
    verbatim_value: str | None = Field(
        default=None, description="Original text as written, retained per FR-ING-08"
    )
    condition: Condition = Field(
        default_factory=Condition, description="Conditions the value holds under - see D-1"
    )
    source_tier: SourceTier
    source_ref: SourceRef
    confidence: float = Field(ge=0.0, le=1.0)
    unresolved_status: UnresolvedStatus = Field(
        default=UnresolvedStatus.NONE,
        exclude=True,
        description=(
            "The conflict state while no decision is recorded: NONE, OPEN or "
            "INSUFFICIENT_EVIDENCE. Never RESOLVED - that is what `resolution` "
            "being present means, and `conflict_status` derives it. Excluded from "
            "serialisation so the wire shape stays the TRS's eight keys."
        ),
    )
    resolution: Resolution | None = None

    @field_validator("value")
    @classmethod
    def _value_must_be_encodable(cls, value: object) -> object:
        """See `_reject_non_finite_value`. Applied at the boundary that stores
        the row, so the encoder's claim that "the schema rejects these at
        construction" is true rather than aspirational."""
        return _reject_non_finite_value(value)

    @field_validator("confidence")
    @classmethod
    def _fold_negative_zero(cls, value: float) -> float:
        """`ge=0.0` does not keep `-0.0` out, because `-0.0 >= 0.0` is True.

        The repo folds `-0.0` in three other places - `ConditionDimensions`,
        `DeclaredBand` and `ComponentInstance.nameplate` - and this slot was
        missed. It is the one that matters most, because `confidence` reaches the
        C6 projection *unencoded*: `_field_row` emits it as a bare float, so
        `json.dumps` writes `-0.0` and the digest moves.

        `-0.0 == 0.0` and the two hash alike, so **every equality this codebase
        has calls the two stores identical** while their SHA-256s differ - A-6
        stated literally, and invisible to any test written as an assertion about
        values. Not producible by `confidence.fuse` today, which sums
        non-negative terms; it arrives through any boundary that deserialises a
        stored `-0.0`, which is every JSON or database round-trip.

        No finiteness check here: NaN fails `ge=0.0` and both infinities fail one
        of the two bounds, so the bounds already close that door.
        """
        return value + 0.0

    @model_validator(mode="before")
    @classmethod
    def _accept_the_contracts_name(cls, data: object) -> object:
        """Map the wire key `conflict_status` onto the stored field.

        RESOLVED with no resolution is refused here, at the door, which is the
        one place it can still be *asked for*. Moving a resolved field off
        RESOLVED is refused here too, so `evolve(conflict_status=OPEN)` cannot
        hide a recorded decision by rewriting only the stored enum.
        """
        if not isinstance(data, dict):
            return data
        if "conflict_status" not in data:
            if data.get("resolution") is not None:
                data = dict(data)
                data["unresolved_status"] = UnresolvedStatus.NONE
            return data
        data = dict(data)
        status = ConflictStatus(data.pop("conflict_status"))
        if status is ConflictStatus.RESOLVED:
            if data.get("resolution") is None:
                raise ValueError("a resolved field must carry its Resolution (FR-HITL-06)")
            data["unresolved_status"] = UnresolvedStatus.NONE
        else:
            if data.get("resolution") is not None:
                raise ValueError(
                    "a resolved field's status is derived from its Resolution and a Resolution "
                    "is append-only (FR-HITL-06); it cannot be moved back to "
                    f"{status.value}"
                )
            data["unresolved_status"] = UnresolvedStatus(status.value)
        return data

    @computed_field  # type: ignore[prop-decorator]
    @property
    def conflict_status(self) -> ConflictStatus:
        """RESOLVED exactly when a decision is recorded; otherwise the stored state."""
        if self.resolution is not None:
            return ConflictStatus.RESOLVED
        return ConflictStatus(self.unresolved_status.value)

    @conflict_status.setter
    def conflict_status(self, status: ConflictStatus) -> None:
        status = ConflictStatus(status)
        if status is ConflictStatus.RESOLVED:
            if self.resolution is None:
                raise ValueError("a resolved field must carry its Resolution (FR-HITL-06)")
            self.unresolved_status = UnresolvedStatus.NONE
            return
        if self.resolution is not None:
            raise ValueError(
                "a resolved field's status is derived from its Resolution and a Resolution "
                "is append-only (FR-HITL-06); it cannot be moved back to "
                f"{status.value}"
            )
        self.unresolved_status = UnresolvedStatus(status.value)

    def is_missing(self) -> bool:
        """FR-OUT-04 missing-data flag."""
        return self.value is None

    def is_web_supplemented(self) -> bool:
        """FR-OUT-04 web-supplemented flag."""
        return self.source_tier is SourceTier.WEB_SUPPLEMENT

    def evolve(self, **changes: object) -> Self:
        """The revalidating update `model_copy(update=...)` cannot be.

        `model_copy(update=...)` patches the copy's `__dict__` directly and
        reruns no validators, so it can swap a recorded `Resolution` for another
        with nothing seeing the transition. Merging the change into a full
        snapshot and routing it back through `model_validate` reruns every
        validator on the result, and the append-only check below sees the
        transition.

        `conflict_status=` is accepted here as it is in the constructor: the
        before-validator maps it onto the stored state, so `evolve(
        conflict_status=RESOLVED, resolution=...)` reads naturally and
        `evolve(conflict_status=RESOLVED)` alone is refused at the door.

        **The snapshot is taken from the live attributes, not from
        `model_dump()`.** `value` is typed `object | None`, so it has no schema
        to validate back against - `model_dump()` serialises whatever is in it
        and `model_validate` stores the serialised form. For a `DeclaredBand`
        that means the field silently comes back holding a `dict`:

            f.value                     # DeclaredBand
            f.evolve(confidence=0.8).value
            # {'low': 0.0, 'high': 5.0, ...}  -- no error, no warning
            f.evolve(confidence=0.8).value.resolve(650.0)
            # AttributeError: 'dict' object has no attribute 'resolve'

        That is not hypothetical: the frozen contract types `power_tolerance`
        and `bifaciality_tolerance` as `DeclaredBand`, and this module's own
        `DeclaredBand` docstring says a band is a field value like any other. It
        was also a *regression* against the `model_copy(update=...)` this method
        replaces, which shallow-copied `__dict__` and preserved the object.

        Passing live attributes keeps `value` untouched while every validator
        still runs on the result, which is the whole point of the method.
        """
        known = {*type(self).model_fields, "conflict_status"}
        unknown = set(changes) - known
        if unknown:
            # `extra="forbid"` would raise too, but this message names the field
            # *and lists the known ones*, which is the diagnosis a caller who has
            # just misspelt one needs.
            raise ValueError(
                f"{type(self).__name__}.evolve() got unknown field(s): "
                f"{sorted(unknown)}. Known fields: {sorted(known)}"
            )
        # Same rule as `__setattr__`: a recorded decision is append-only.
        # `evolve` routes through `model_validate`, which sees only the finished
        # snapshot, so it could swap one reviewer's Resolution for another's and
        # every validator would still pass. Checked here, where the transition is
        # still visible, rather than in the validator, which cannot see it.
        if "resolution" in changes and self._replaces_recorded_resolution(changes["resolution"]):
            raise ValueError(_RESOLUTION_IS_APPEND_ONLY)
        snapshot = {name: getattr(self, name) for name in type(self).model_fields}
        return type(self).model_validate({**snapshot, **changes})

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        """Refuse the one form of `model_copy` that skips validation.

        A bare `model_copy()` (or `deep=True`) duplicates data that was already
        valid, so there is nothing to recheck and pydantic's own implementation
        is used unchanged. `update=...` writes the copy's `__dict__` with no
        validator and no transition check, so it could replace a recorded
        `Resolution` unseen - the append-only rule FR-HITL-06 states. It is
        refused outright, loudly, at the call site, in favour of `evolve`.
        """
        if update is not None:
            raise TypeError(
                "CanonicalField.model_copy(update=...) skips validation and the "
                "append-only check on `resolution` (FR-HITL-06); use "
                "CanonicalField.evolve(...) instead, which runs both."
            )
        return super().model_copy(deep=deep)

    @classmethod
    def model_construct(cls, _fields_set: set[str] | None = None, **values: Any) -> Self:
        """Build without field validation, accepting the contract's name for the state.

        Stock `model_construct` ignores a keyword that is not a field, so
        `conflict_status=RESOLVED` would be dropped and the field would read
        NONE - a silent downgrade on an audit path. It is mapped instead, with
        the same refusal the constructor gives RESOLVED-with-no-resolution. No
        invariant is re-checked afterwards; there is none left to check.
        """
        if "conflict_status" in values:
            status = ConflictStatus(values.pop("conflict_status"))
            if status is ConflictStatus.RESOLVED:
                if values.get("resolution") is None:
                    raise ValueError("a resolved field must carry its Resolution (FR-HITL-06)")
                values["unresolved_status"] = UnresolvedStatus.NONE
            else:
                if values.get("resolution") is not None:
                    raise ValueError(
                        "a resolved field's status is derived from its Resolution and a "
                        "Resolution is append-only (FR-HITL-06); it cannot be moved back to "
                        f"{status.value}"
                    )
                values["unresolved_status"] = UnresolvedStatus(status.value)
        elif values.get("resolution") is not None:
            values["unresolved_status"] = UnresolvedStatus.NONE
        return super().model_construct(_fields_set, **values)

    def __setattr__(self, name: str, value: Any) -> None:
        """Refuse to overwrite a resolution that has already been recorded.

        No validator can catch this: a validator sees only the finished state,
        and a field carrying someone else's decision is a perfectly legal state.
        Only the transition is wrong. So

            field.resolution = someone_elses_decision

        passed every check and left no trace that a different reviewer's decision
        had ever been there - the exact failure FR-HITL-06 ("logged immutably")
        names, on the object the requirement is about. `ConflictQueueEntry` was
        frozen for this reason; `CanonicalField` cannot be frozen (see the class
        docstring) so it needs the rule stated rather than inherited.

        Attaching a resolution to a field that has none is the legitimate
        transition and stays allowed. Re-assigning an equal value is allowed so an
        idempotent replay is not an error. `evolve` enforces the same rule.

        Clearing a recorded resolution is the same rule from the other side:
        with RESOLVED derived from `resolution`, `field.resolution = None` would
        silently un-resolve the field, so it is refused here too.
        """
        if name == "resolution" and self._replaces_recorded_resolution(value):
            raise ValueError(_RESOLUTION_IS_APPEND_ONLY)
        super().__setattr__(name, value)
        if (
            name == "resolution"
            and getattr(self, "resolution", None) is not None
            and getattr(self, "unresolved_status", UnresolvedStatus.NONE)
            is not UnresolvedStatus.NONE
        ):
            super().__setattr__("unresolved_status", UnresolvedStatus.NONE)

    def _replaces_recorded_resolution(self, incoming: object) -> bool:
        """Whether `incoming` would change a decision already on this field.

        One definition of "the same decision" for both `evolve` and
        `__setattr__`: an equal `Resolution`, in object or serialised form, is a
        replay and not a replacement.
        """
        recorded = getattr(self, "resolution", None)
        return recorded is not None and _as_resolution(incoming) != recorded


class ConflictCandidate(BaseModel):
    """One competing value for a field, as presented in the queue (FR-HITL-03)."""

    model_config = ConfigDict(frozen=True, extra=_FORBID_UNDECLARED_KEYS)

    value: object | None
    unit: str | None
    verbatim_value: str | None = Field(
        default=None, description="FR-HITL-03 requires the verbatim source text"
    )
    condition: Condition = Field(
        default_factory=Condition,
        description=(
            "The conditions this candidate holds under. Required for the queue to "
            "explain itself (FR-HITL-03): '352 kVA vs 320.865 kW' is unreadable "
            "without @30 degC / @40 degC attached. Also what "
            "services.conflict_hitl.comparison_groups partitions on."
        ),
    )
    source_tier: SourceTier
    source_ref: SourceRef
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("value")
    @classmethod
    def _value_must_be_encodable(cls, value: object) -> object:
        """The same check `CanonicalField.value` carries, applied here for the
        reason `_fold_negative_zero` gives just below: the defect is the *type*,
        not the file. A candidate's value goes through `_canonical` on every
        `comparison_pairs` call, so a non-finite one raises from the sort path
        rather than from the store - later, and further from the extractor."""
        return _reject_non_finite_value(value)

    @field_validator("confidence")
    @classmethod
    def _fold_negative_zero(cls, value: float) -> float:
        """The same fold `CanonicalField.confidence` carries, for the same reason.

        Applied here as well because the defect is the *type*, not the file: a
        candidate's confidence is one of the elements
        `conflict_hitl._ordering_key` sorts on, so an unfolded `-0.0` moves a
        candidate's position in a hashed array rather than only its rendered
        text.
        """
        return value + 0.0


class ConflictQueueEntry(BaseModel):
    """An item awaiting human resolution.

    FR-HITL-03 fixes the payload: canonical field, component/supplier, all
    candidate values with verbatim source text, source tier, source authority,
    doc/page/URL, timestamps and a generated explanation.

    Frozen, because FR-HITL-06's log is immutable and freezing `Resolution` alone
    did not deliver that: the *pointer* was assignable, so a second write replaced
    a reviewer's decision with no trace the first had ever existed. Attaching a
    resolution is `model_copy(update=...)`, which produces a new record rather
    than editing one - the shape an append-only audit log wants anyway.

    Frozen is shallow, as it always is in pydantic: `candidates` is a list, and
    nothing stops a caller mutating it in place. So the guarantee this buys is
    narrow and worth stating as such - a recorded `resolution` cannot be silently
    swapped. That is one part of FR-HITL-06, not the whole of it: the requirement
    also wants a persisted, tamper-evident log, which is NFR-02 and plan Decision
    9, implemented as `audit.event` (JCS envelope, advisory lock, verify CLI).
    """

    model_config = ConfigDict(frozen=True, extra=_FORBID_UNDECLARED_KEYS)

    entry_id: str
    field_name: str
    supplier: str
    model: str
    #: The closed vocabulary, not a bare `str`: the frozen contract types this
    #: `ComponentCategory` (contracts/canonical-parameters.md), and
    #: `ComponentInstance` already did. The bare `str` here accepted a plausible
    #: label like "PV Modules" that is not a member, and `CATEGORY_TO_TAB` is keyed
    #: on the enum - so once composition reads this field, such a value routes to
    #: no tab. Closing it now makes that a construction-time error instead.
    component_category: ComponentCategory
    conflict_class: ConflictClass
    severity: Severity = Field(
        description=(
            "Drives the compose gate (issue #14). Assigned from the field's "
            "criticality tier, not from the size of the divergence - see D-3. "
            "Deliberately REQUIRED: this is a safety interlock, and any default "
            "at or below the gate threshold would make a forgotten severity "
            "silently unable to block."
        ),
    )
    candidates: list[ConflictCandidate]
    explanation: str = Field(description="Generated rationale shown to the reviewer")
    detected_at: datetime
    resolution: Resolution | None = None

    @field_validator("detected_at")
    @classmethod
    def _detected_at_must_name_an_instant(cls, value: datetime) -> datetime:
        return _aware_datetime(value, "detected_at")
