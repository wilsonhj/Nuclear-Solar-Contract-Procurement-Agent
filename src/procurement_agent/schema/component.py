"""Component instances and source documents held in the canonical store.

TRS section 5: "Per component instance: supplier, model, component_category, and
a field set (section 7)." The per-category parameter sets from TRS section 7 are
not yet enumerated here; locking them for PV modules, inverters and BESS is the
Stage 1 exit condition, so they belong in a follow-up rather than in scaffolding.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .encoding import canonical_json, is_aware
from .enums import RESTRICTED_DOCUMENT_TYPES, ComponentCategory, ConflictStatus, DocumentType
from .field import CanonicalField
from .registry import require_contract_key


class SourceDocument(BaseModel):
    """An ingested file. Identity is content-addressed so re-ingest is a no-op.

    FR-ING-09 requires stable IDs with timestamps and source URI; NFR-05 and AC-5
    require that re-ingesting an unchanged document creates no duplicates.
    """

    # `extra="forbid"` for the reason `ComponentInstance` gives below: a mistyped
    # optional field would otherwise vanish, and every field here except
    # `access_restricted` is provenance FR-ING-09 and FR-OUT-06 report.
    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str
    content_hash: str = Field(description="Dedup key per FR-ING-09 / NFR-05")
    source_uri: str
    document_type: DocumentType
    ingested_at: datetime
    data_vintage: datetime | None = Field(
        default=None,
        description="Publication or revision date of the source, reported per FR-OUT-06",
    )
    access_restricted: bool = Field(
        default=False,
        description=(
            "NFR-03: contract and pricing documents are confidential. Enforced at "
            "retrieval time via metadata filtering, not only at the API edge."
        ),
    )

    @field_validator("ingested_at", "data_vintage")
    @classmethod
    def _must_name_an_instant(cls, value: datetime | None) -> datetime | None:
        """The same constraint `SourceRef.retrieved_at` carries, for the same
        reason: `encode_value` refuses a naive datetime because one names no
        instant, so a schema that accepts one accepts a value the canonical
        encoder cannot encode.

        Applied here as well as there because the defect is the type, not the
        file - `data_vintage` is what FR-OUT-06 reports and what temporal conflict
        detection compares, and a wall-clock reading with no zone cannot order
        two revisions. Nothing constructs a `SourceDocument` yet, which is exactly
        why this would otherwise have been found by whoever wrote the first
        ingestion boundary, after the naive timestamps were already stored.
        """
        if value is not None and not is_aware(value):
            raise ValueError(
                "SourceDocument timestamps must be timezone-aware; a naive datetime "
                "names no instant. Attach the zone at the boundary that produced it."
            )
        return value

    @model_validator(mode="after")
    def _enforce_confidential_document_restriction(self) -> SourceDocument:
        """NFR-03 / D-15: confidential document types ratchet `access_restricted=True`."""
        if self.document_type in RESTRICTED_DOCUMENT_TYPES and not self.access_restricted:
            object.__setattr__(self, "access_restricted", True)
        return self

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        """Re-validate a copy that changes fields, so the ratchet above still holds.

        Stock `model_copy(update=...)` writes `__dict__` with no validator, which
        would let a pricing document be copied out as `access_restricted=False`.
        """
        if update is None:
            return super().model_copy(deep=deep)
        return type(self).model_validate({**super().model_copy(deep=deep).__dict__, **update})

    @classmethod
    def model_construct(cls, _fields_set: set[str] | None = None, **values: Any) -> Self:
        """Build without validation, but never as an unrestricted confidential type."""
        built = super().model_construct(_fields_set, **values)
        if built.document_type in RESTRICTED_DOCUMENT_TYPES and not built.access_restricted:
            object.__setattr__(built, "access_restricted", True)
        return built


class ComponentInstance(BaseModel):
    """One supplier's offering in one category, as a set of canonical fields."""

    # Pydantic defaults to `extra="ignore"`, and every canonical-store model was
    # on it - so a mistyped *optional* field was silently dropped. (Required
    # fields were protected only incidentally, by their own missing-field error.)
    #
    # The cost here is not cosmetic: `ComponentInstance(nameplate_w=550)` leaves
    # `nameplate` at `None`, and `ordering_key()` maps that to `float("-inf")`.
    # A typo discards the bin discriminator that exists precisely because "one
    # datasheet routinely covers several SKUs".
    #
    # Same class as issue #16 one level up. #16 closed the condition
    # *vocabulary*, so `Condition(basis="not_a_real_basis")` is refused; the
    # field *name* stayed open-world, so `Condition(bassis="stc")` was accepted
    # and produced an unstated condition that compared against everything.
    # `config.py`'s `extra="ignore"` is deliberate and right - unknown
    # environment variables must not break startup - and no schema model
    # documented such a reason.
    model_config = ConfigDict(extra="forbid")

    supplier: str
    model: str
    component_category: ComponentCategory
    nameplate: float | None = Field(
        default=None,
        description=(
            "Bin discriminator. One datasheet routinely covers several SKUs - a Trina "
            "TSM-NEG21C.20 sheet spans 6 bins and 22 CEC rows - so supplier+model alone "
            "does not identify a product."
        ),
    )
    surrogate_id: str | None = Field(
        default=None,
        description=(
            "hash(normalised_supplier, normalised_model, nameplate). There is no stable "
            "ID upstream: CEC publishes none, and (Manufacturer, Model Number) is not "
            "unique - 36 duplicated pairs measured, plus 157 model numbers appearing "
            "under more than one manufacturer. See clarifications.md D-4 and D-8."
        ),
    )
    manufacturer_key: str | None = Field(
        default=None,
        description=(
            "D-4 stage 1's normalised supplier. Filled by `services.identity."
            "identity_keys`; `schema` sits below `services` and cannot import it, "
            "so the slot is declared here and populated from outside - the same "
            "arrangement `surrogate_id` already used."
        ),
    )
    model_family: str | None = Field(
        default=None,
        description="D-4 stage 2's family, i.e. the model string with the bin token masked.",
    )
    fields: dict[str, list[CanonicalField]] = Field(
        default_factory=dict,
        description=(
            "Contract key -> every conditioned value for it. **List-valued, not "
            "one entry per key.** D-1 makes `condition` part of what a value *is*, "
            "and one datasheet routinely states one parameter several times under "
            "different conditions - the Sungrow SG350HX prints `352 kVA @30 degC / "
            "320 @40 degC / 295 @50 degC` for a single `rated_ac_power`. Collapsing "
            "those to one entry either loses two real values or forces a new "
            "contract key per condition, which is what the ad-hoc encodings "
            "(`stc_rating` vs `nmot_rating`, `rated_ac_power_temp`) already do. "
            "The contract keeps those; this is the general mechanism beside them."
        ),
    )

    @model_validator(mode="after")
    def _keys_are_on_contract(self) -> ComponentInstance:
        """Contract C2's first enforcement point: `fields` is where a key is
        *used as a key*, so it is where an off-contract one has to be refused.

        Nothing checked this, and the type could not: `dict[str, ...]` admits any
        string. Downstream that is not a validation nuisance but a permanent one -
        claims are append-only, so a parameter stored under `nameplate_power_w`
        yields rows that can be superseded and never corrected, and the B.9 gold
        set gets labelled against a key no table looks up.

        **Checked against the category, not the union of all keys.** `chemistry`
        is a genuine contract key and nonsense on a PV module, and a union check
        passes it. The category is what makes a key mean something - see
        `registry.spec_for`, and the two `insulation_type` rows it exists for.

        Known limit, stated rather than implied: this runs at construction and on
        `model_validate`, so `instance.fields["junk"] = [...]` afterwards is not
        seen. Closing that needs `validate_assignment` plus a `fields` that is not
        a bare dict, which is a wider change than this contract needs - the
        boundary that matters is the one values arrive through.
        """
        for key in self.fields:
            require_contract_key(key, self.component_category)
        return self

    @field_validator("nameplate")
    @classmethod
    def _reject_non_finite(cls, value: float | None) -> float | None:
        """Same reasoning as `ConditionDimensions` and `DeclaredBand`, applied to
        the one float left in the total order.

        NaN compares false against everything, so it is not a sort key at all:
        three instances whose category, manufacturer and family tie come out in
        four different orders depending on the order they arrive in, and AC-7
        wants byte-identical output from an unchanged store. `-0.0` is folded for
        the same repr-stability reason the other two give.

        `None` stays legal - a nameplate is often not known yet, and
        `ordering_key` maps it to `-inf`, which *is* totally ordered.
        """
        if value is None:
            return None
        if not math.isfinite(value):
            raise ValueError("nameplate must be finite (no NaN or infinity)")
        return value + 0.0

    def _stored_values(self) -> str:
        """A canonical rendering of everything this instance actually says.

        Routed through `encode_value` - the one authority - rather than through
        `model_dump(mode="json")`, which is what this first shipped as and which
        was wrong in a way that mattered.

        `CanonicalField.value` is typed `object | None`, so it is the polymorphic
        slot `schema/encoding.py` exists for. Pydantic's JSON mode collapses that
        slot: `Decimal("22.00")` and the string `"22.00"` dump to the same text,
        so two instances differing only in which one they store **tie here** -
        while `services/output/projection.py` routes the same values through
        `encode_value` and renders them differently (`{"$decimal": "22.00"}`
        against `"22.00"`). A tie between rows that are not identical hands their
        relative order back to `sorted`'s stability, which is arrival order: the
        A-6 class in its mirror form, where two artifacts differing in content
        have a byte position that is not a function of content.

        The original reason for the workaround was real - `encode_value` could
        not encode a `CanonicalField`, because `model_dump()` flattened nested
        models to plain dicts and the closed world had no `dict` rule. That was
        fixed in this same change, and this call site was not revisited with it.

        `sort_keys` is belt and braces: `encode_value` already sorts a map's
        pairs by canonical text, which is what makes this a function of content
        rather than of insertion order. The contract has three dict-valued
        parameters, and a dict iterates in insertion order, so two extractions
        that read one cooling table's rows in different orders hold equal values
        that would otherwise render differently - `services.claims._render`
        records the same hazard on the claim-identity side.

        Deliberately reads `fields` and nothing else. The raw `supplier` and
        `model` are excluded because D-4 stage 1 exists to make `Trina Solar` and
        `Trina Solar Co.,Ltd` sort together, and a tie-break that read them would
        undo the entity split silently - the obvious way to make a key unique is
        the one that breaks the thing the key was normalised for.
        """
        return canonical_json(self.fields)

    def ordering_key(self) -> tuple[str, str, str, float, str, str, str]:
        """Canonical sort position for deterministic workbook regeneration.

        AC-7 requires byte-identical output from an unchanged store, which needs a
        total order over component instances. `sorted-key JSON` orders keys *within*
        an object; it says nothing about row order *across* instances.

        `surrogate_id` is the tie-break rather than the primary key because
        (category, supplier, model) is provably not unique on real data - two Adani
        entities publish `ASB-M10-144-550` with genuinely different specs (PTC 509.9
        vs 518.2). Without the tie-break the sort is unstable exactly where the data
        is most ambiguous.

        **`surrogate_id` alone was not a tie-break, and the sentence above says
        why.** It falls back to `""`, which is the value on *both* sides whenever
        nothing has run the matcher yet - the state a freshly ingested store is
        in. `sorted` is stable, so a complete tie does not reorder: it leaks
        arrival order, and the workbook differs run to run with no value having
        changed. The cited Adani pair is exactly such a tie, because PTC lives in
        `fields` and the key did not read `fields`. **The tie-break did not cover
        the scenario it was written for**, and the test covering it happened to
        give both sides a surrogate.

        So `_stored_values()` joined the key, and the order became total *up to
        equality* over everything the key read.

        **That was not the same as total over the row it orders, and the
        difference was a live A-6 defect.** `services/output/projection.py`'s
        `_component_row` emits the raw `supplier` and `model` that the paragraph
        below deliberately keeps out of the sort, so two spellings folded onto
        one `manufacturer_key` tied on all six elements and rendered different
        rows - and `sorted`'s stability handed their byte order to arrival order.
        The cited Adani pair is exactly such a tie whenever the matcher has not
        run, which is the state a freshly ingested store is in. The claim that
        stood here - that two tying instances "produce identical rows" - was
        false for the case this docstring is written around.

        The raw strings are therefore the last two elements. The order is now
        total up to equality *over the emitted row*: two instances that still tie
        agree on every field `_component_row` writes, so their relative order
        cannot be observed in the output at all. That is the strongest guarantee
        available and the only one AC-7 needs.

        D-4 stage 5 sorts on the *normalised* keys, not the raw strings: sorting on
        `supplier` puts `Trina Solar` and `Trina Solar Co.,Ltd` far apart, so the
        entity split stage 1 exists to close reopens in the row order. The raw
        strings remain the fallback for an instance nobody has run the matcher over
        - a partially-normalised store must still have a total order, and falling
        back is visible where raising would only move the failure.

        **This key is in-memory only; it can order rows and can never itself be
        projected or hashed.** An absent nameplate becomes `float("-inf")`, which
        is totally ordered and which `encode_value` refuses - no injective
        encoding of a non-finite float exists. Anything wanting canonical bytes
        for a row must build them from the instance rather than from this tuple.
        """
        return (
            self.component_category.value,
            self.manufacturer_key or self.supplier,
            self.model_family or self.model,
            self.nameplate if self.nameplate is not None else float("-inf"),
            self.surrogate_id or "",
            self._stored_values(),
            self._identity_values(),
        )

    def _identity_values(self) -> str:
        """The five raw identity strings, **last**, and only as a final tie-break.

        The paragraph above is right that *sorting on* the raw supplier would
        undo the entity split - but excluding these entirely made a different
        claim false. `services/output/projection.py:_component_row` emits all
        five unfolded, so instances differing in any of them tied on every
        element of the key and still rendered different rows. `sorted` is
        stable, so the tie handed their byte order to arrival order.

        **Three of the five are lost to a fallback operator rather than to
        omission**, which is why naming only `supplier` and `model` here did not
        finish the job. Elements 2, 3 and 5 above are written `x or y`, and each
        `or` collapses an absent value into another field's:

            manufacturer_key or supplier    model_family or model
            surrogate_id or ""

        so `manufacturer_key=None` and `manufacturer_key="Adani Solar"` produce
        one key when the supplier is `"Adani Solar"`. That is reachable rather
        than contrived: `identity_keys("sungrow", ...)` returns
        `manufacturer_key == "sungrow"`, so any supplier already in normalised
        form ties - in exactly the partially-normalised store the docstring
        above says must be supported.

        Rendered through `encode_value` rather than as bare tuple members
        because the distinction that matters here is `None` against `""`, and
        only the encoder keeps those apart (`null` versus `""`). That makes this
        element a function of the emitted row **by construction** rather than by
        enumeration - the same reason `_stored_values()` routes through the one
        authority instead of restating a rendering.

        Position is what makes it safe. Everything the entity split exists for
        is decided by elements 1-5, so two spellings of one manufacturer still
        sort adjacently; this element only ever separates rows that already
        agree on category, identity, nameplate, surrogate and every stored
        value. At that point the rows differ in the output, so leaving them
        unordered is the defect and ordering them cannot merge anything.
        """
        return canonical_json(
            [
                self.supplier,
                self.model,
                self.manufacturer_key,
                self.model_family,
                self.surrogate_id,
            ]
        )

    def unresolved_conflicts(self) -> list[str]:
        """Field names still awaiting a human decision.

        FR-HITL-05: unresolved conflicts and insufficient-evidence fields are
        flagged in the output, never silently resolved or omitted.
        """
        blocking = {ConflictStatus.OPEN, ConflictStatus.INSUFFICIENT_EVIDENCE}
        return sorted(
            {
                name
                for name, values in self.fields.items()
                for value in values
                if value.conflict_status in blocking
            }
        )
