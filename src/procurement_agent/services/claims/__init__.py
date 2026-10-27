"""Workers propose, they do not commit — contract C8, issue #8.

`assert_no_autonomous_overwrite` is a *value* property: a pure predicate over two
fields. It takes no store handle, performs no write, and **cannot enforce that it
is called**. With one serial writer that suffices. Under fan-out it is an honour
system every branch must remember, and it fails three further ways the guard
cannot see: it permits record-over-record (a lost update under concurrency), it
has no notion of ordering (two workers finishing in different orders give
different last-writer-wins outcomes, so the *store* becomes order-dependent and
FR-OUT-06 fails below composition), and nothing stops a branch skipping it.

C8's answer is structural: **there is no overwrite to guard.** Each extraction
appends an immutable `FieldClaim`, and the canonical values are a *projection*
over claims rather than an in-place update.

**Contract C2: a claim carries its `condition`.** The first version of this
module omitted it, and the cost was measured on D-1's own worked example — the
Sungrow SG350HX's `352 kVA @30 degC / 320 @40 degC / 295 @50 degC`. Every
candidate came out `is_unstated()`, so `comparison_pairs` returned **3 pairs**
where D-1 says zero, and `canonical_claims` raised on the trio because the key
could not tell three conditions apart from three contradictions. A claim record
without `condition` defeats the condition gate on the only path that reaches it.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Iterable, Sequence
from decimal import Decimal
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...schema import (
    HUMAN_EXTRACTOR_PREFIX,
    CanonicalField,
    ComponentCategory,
    Condition,
    ConflictCandidate,
    ConflictStatus,
    Resolution,
    SourceRef,
    SourceTier,
    ToleranceRule,
    UnresolvedStatus,
    encode_value,
)
from ...schema.registry import OffContractFieldError, require_contract_key
from ..conflict_hitl import (
    _as_number,
    _editions_by_base,
    assert_no_autonomous_overwrite,
    tolerance_for,
    values_conflict,
)

#: The `extractor_version` prefix that marks a reviewer's decision recorded as a
#: claim (D-16). One home: `schema.HUMAN_EXTRACTOR_PREFIX`. `sql/06_resolution.sql`
#: proposes `human:<resolved_by>`; the validator on `FieldClaim` is what makes
#: the convention real.
HUMAN_PREFIX = HUMAN_EXTRACTOR_PREFIX


class FieldClaim(BaseModel):
    """One extractor's assertion about one field of one document, under one
    condition.

    Immutable and never revised: a second look produces a *new* claim under a new
    `extractor_version`, so what was believed when is preserved. That is what
    makes the store append-only, and what makes re-extraction safe to run
    concurrently with anything else.
    """

    #: `extra="forbid"` because pydantic's default silently drops a *mistyped
    #: optional* field - required ones are protected only incidentally, by their
    #: own missing-field error. Append-only makes that sharp here: a claim
    #: written with `verbatim_valeu` can be superseded but never corrected, and
    #: the B.9 gold set is labelled against what was stored. The full argument is
    #: on `ComponentInstance`; this is the same decision one layer down.
    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str
    field_name: str = Field(description="The frozen contract's `key` for this parameter")
    extractor_version: str = Field(
        description=(
            "Identifies the code and prompt that produced the claim. Part of the "
            "key, so re-extraction appends rather than overwrites and a regression "
            "stays visible next to the value that replaced it."
        )
    )
    condition: Condition = Field(
        default_factory=Condition,
        description=(
            "The conditions this value holds under (D-1, contract C2). Part of the "
            "key: one datasheet stating a parameter at three ambients is three "
            "claims, not one extractor contradicting itself."
        ),
    )

    value: object | None
    unit: str | None = None
    verbatim_value: str | None = None
    source_tier: SourceTier
    source_ref: SourceRef
    confidence: float = Field(ge=0.0, le=1.0)
    resolution: Resolution | None = Field(
        default=None,
        description=(
            "Present on a **human claim** and on nothing else (D-16). A reviewer's "
            "SELECT_VALUE, ENTER_OVERRIDE or KEEP_SYSTEM_OF_RECORD decision is "
            "recorded as a claim carrying its Resolution, so the reducer sees a "
            "human decision as the highest-priority claim rather than reading the "
            "resolution table - which keeps 'the canonical value is a projection "
            "over claims' (C8) true for human overrides too. sql/06 recommends "
            "exactly this convention and says it cannot enforce it; this validator "
            "is the enforcement."
        ),
    )

    @model_validator(mode="after")
    def _human_claims_carry_their_decision(self) -> FieldClaim:
        """The D-16 convention, in both directions.

        A claim whose `extractor_version` starts with `human:` is a reviewer's
        decision and must carry the `Resolution` that produced it; a claim
        carrying a `Resolution` must be marked as human. One without the other is
        a decision with nobody behind it, or a person's name on a machine value.

        Only value-asserting actions may be claims. DEFER and
        REQUEST_MORE_WEB_SEARCH assert nothing about the world - they are events
        against the *conflict*, recorded in `resolution` and `audit.event`, and a
        claim for one would put a non-value in the value store.

        `value_after`, when the reviewer recorded one, must be the claim's value:
        a decision recorded against one number and a claim asserting another is
        the drift this whole module exists to make impossible.
        """
        if self.is_human and self.resolution is None:
            raise ValueError(
                f"a human claim ({self.extractor_version!r}) must carry its Resolution; a "
                "reviewer's value with no recorded decision is a decision with nobody "
                "behind it (FR-HITL-06)"
            )
        if self.resolution is not None:
            if not self.is_human:
                raise ValueError(
                    f"a claim carrying a Resolution must be a human claim: extractor_version "
                    f"{self.extractor_version!r} does not start with {HUMAN_PREFIX!r}"
                )
            if not self.resolution.action.asserts_a_value:
                raise ValueError(
                    f"{self.resolution.action.value} asserts no value and cannot be a claim; "
                    "it is recorded against the conflict, not in the value store (D-16)"
                )
            recorded = self.resolution.value_after
            if recorded is not None and recorded != self.value:
                raise ValueError(
                    "the Resolution's value_after must be the claim's value: "
                    f"{self.resolution.value_after!r} != {self.value!r}"
                )
        return self

    @property
    def is_human(self) -> bool:
        """Whether this claim records a reviewer's decision (D-16).

        The `human:` prefix is the primary encoding - it is the marker `sql/06`
        proposes, and the only one the `claim` table can hold until it gains a
        resolution column - so the property reads it, and the validator above
        checks that `resolution` agrees rather than defining the fact a second
        way.
        """
        return self.extractor_version.startswith(HUMAN_PREFIX)

    def claim_key(self) -> tuple[object, ...]:
        """The uniqueness key. Two claims sharing it are the same assertion.

        `condition.grouping_key()` is in it because without it the Sungrow trio -
        one document, one field, one extractor version, three ambients - is
        indistinguishable from an extractor emitting three answers for one field.

        A human claim's key also carries `resolved_at`: one reviewer may settle
        the same field twice - a reopened conflict records a *new* decision, it
        does not rewrite the old one - and without the timestamp the second
        decision would collide with the first as "one extractor, two answers".
        """
        return (
            self.document_id,
            self.field_name,
            self.extractor_version,
            self.condition.grouping_key(),
            None if self.resolution is None else self.resolution.resolved_at,
        )

    def provenance(self) -> SourceRef:
        """This claim's `source_ref` with contract C3's fourth element stamped on.

        C3 is `(document_id, page, span, extractor_version)`, where `span` is
        spelled `section` on `SourceRef`. The first three already lived there
        under those names; the fourth lived only on the claim, so the
        projection dropped it and a stored value could not be traced back to the
        code that produced it. Stamping rather than requiring callers to set it
        twice keeps the claim's own `extractor_version` the single authority.
        """
        return self.source_ref.model_copy(update={"extractor_version": self.extractor_version})

    def as_candidate(self) -> ConflictCandidate:
        """The queue's view of this claim (FR-HITL-03), condition included."""
        return ConflictCandidate(
            value=self.value,
            unit=self.unit,
            verbatim_value=self.verbatim_value,
            condition=self.condition,
            source_tier=self.source_tier,
            source_ref=self.provenance(),
            confidence=self.confidence,
        )


@runtime_checkable
class ClaimWriter(Protocol):
    """The only handle that can put canonical values in the store.

    Deliberately not passed to anything a worker calls.
    """

    def commit(self, field_name: str, values: Sequence[CanonicalField]) -> None: ...

    def current(self, field_name: str) -> Sequence[CanonicalField]: ...


class ProposalError(RuntimeError):
    """Raised when a claim set cannot be projected to canonical values."""


class StoredValueLossError(ProposalError):
    """Raised when committing a projection would delete a value already stored.

    A subclass rather than a separate hierarchy, because it is the same
    programming error as the rest: the reducer was handed something other than
    the complete claim set for the field.
    """


def _render(value: object, _containers: tuple[int, ...] = (), *, fold_equal: bool = False) -> str:
    """A textual rendering of a claim value. Two modes, because two questions.

    `repr` answers neither. A dict reprs in *insertion* order, so two extractions
    that read one cooling table's rows in different orders give values that are
    `==` and reprs that are not. The contract has three dict-valued parameters -
    `rating_mva_by_cooling`, `harmonic_spectrum`, `ercot_compliance_items` - and
    under `repr` such a pair counted as a disagreement: an OPEN conflict between
    two identical values, or a `ProposalError` losing the whole field when the
    two shared a claim key. Canonicalising container order fixes that, and is
    the whole of the default mode.

    **`fold_equal=True` additionally folds one number's spellings together.**
    That is the mode `_asserted` wants, because "what does this claim say" is a
    question about the number and not about how it was typed. One extractor
    emits `650` from a table cell, another `650.0` after unit normalisation, and
    under `repr` those were two answers - the same OPEN-conflict-or-lost-field
    pair of costs as the paragraph above, on a plain scalar rather than a dict.
    `ConditionDimensions._reject_non_finite` already folds `-0.0` to `0.0` one
    class over for this reason; nothing folded numeric type here.

    **The modes are separate because `_asserted` must fold and `_identity` must
    not.** `_identity` is the dedup key *and* the total sort order. Fold there
    and two claims differing only in spelling collapse to one row, leaving which
    spelling survives to arrival order - and it is not cosmetic which does:
    `encode_value` writes `650` and `650.0` as different JSON,
    `ComponentInstance._stored_values` hashes that text into `ordering_key`, and
    the workbook's byte output would become a function of which worker finished
    first. That is the FR-OUT-06 failure this module's own docstring opens with.
    So the fold answers "is this the same value" and never "is this the same
    record", and both claims survive to be tied apart by the unfolded order.

    **Where the fold stops short of `==`,** in both cases because the difference
    `==` discards is read by something downstream:

    * `bool`. `True == 1`, and `severity._numeric_value` excludes `bool` for
      exactly this reason - a stray boolean must not read as a nameplate of 1.
      `encode_value` tests `bool` before `int` one layer down for the same
      reason. `repr` already separates them and no branch below rejoins them.

    Decimal printed precision is *not* folded away from the *record* (`_identity`
    still `repr`s it) but *is* folded for the assertion: `values_conflict` already
    treats `650`, `650.0` and `Decimal("650")` as one number via `_as_number`, and
    D-2's rounding floor is read from `verbatim_value` / `encode_value`, not from
    `_asserted`. Folding here at every depth closes the nested
    `{"ONAN": 30}` vs `30.0` split.

    A `list` against a `tuple` needs no rule at all: `[1] != (1,)`, so the type
    tag telling them apart *is* `==`'s verdict rather than a departure from it.
    A `set` against a `frozenset` is the opposite case - those are `==` - so the
    folding mode gives the two one tag.

    Containers are walked rather than repr'd whole, so both the canonicalisation
    and the fold reach a nested dict - which is where the contract's numbers
    actually sit. The cycle guard is not tidiness: `repr` already handles a
    self-referential value, and a hand-rolled walk that did not would trade a
    false conflict for a `RecursionError`.
    """
    if id(value) in _containers:
        return "..."
    nested = (*_containers, id(value))
    if isinstance(value, dict):
        entries = sorted(
            (_render(k, nested, fold_equal=fold_equal), _render(v, nested, fold_equal=fold_equal))
            for k, v in value.items()
        )
        return "{" + ", ".join(f"{key}: {item}" for key, item in entries) + "}"
    if isinstance(value, list | tuple):
        members = (_render(v, nested, fold_equal=fold_equal) for v in value)
        return f"{type(value).__name__}[" + ", ".join(members) + "]"
    if isinstance(value, set | frozenset):
        # One tag for both under the fold: `{'a'} == frozenset({'a'})`, unlike the
        # list/tuple pair above. Outside it the spelling is part of the record,
        # and it is a spelling with consequences - `encode_value` accepts a
        # frozenset and refuses a bare set.
        tag = "set" if fold_equal else type(value).__name__
        sorted_members = sorted(_render(v, nested, fold_equal=fold_equal) for v in value)
        return f"{tag}[" + ", ".join(sorted_members) + "]"
    if fold_equal:
        # `_as_number` is the gate `values_conflict` uses: bool and non-finite
        # out, int / float / Decimal in. `normalize()` folds the trailing zero
        # `:f` would keep, so `650.0` and `Decimal("650")` are one answer.
        # Rendered from the original rather than from the float `_as_number`
        # returns, so an exact Decimal is not first rounded through binary.
        if _as_number(value) is not None:
            folded = Decimal(str(value))
            if folded.is_signed() and folded == 0:
                folded = folded.copy_abs()
            return f"num:{folded.normalize():f}"
    return repr(value)


def _asserted(claim: FieldClaim) -> tuple[str, str]:
    """What the claim actually *says*: a value, in a unit.

    The unit belongs here because `650 W` and `650 kW` are two different answers.
    `values_conflict` classes exactly that as a `UNIT_NORMALIZATION` conflict
    which "is never resolved by tolerance (FR-ING-08)", so a projection that
    compared values alone reported two claims a unit apart as agreement and then
    stored one of the two units.

    `verbatim_value` is deliberately **not** part of it. It is the source text at
    a location, which is provenance in exactly the way `source_ref.page` is: a
    datasheet printing `650 W` in a summary table and `650` in the electrical
    table has stated one figure twice, not contradicted itself.

    **`fold_equal=True` for the same reason `verbatim_value` is excluded.** How
    a number was typed is not what the claim says: `650` from a table cell and
    `650.0` from a unit normaliser are one reading, and the spelling belongs to
    the record - `_identity` - rather than to the assertion. SET_EQUAL fields
    (D-17) are compared as a set of normalised bases, not as list order.
    """
    rendered = (
        _set_answer(claim.value)
        if tolerance_for(claim.field_name).rule is ToleranceRule.SET_EQUAL
        else _render(claim.value, fold_equal=True)
    )
    return (rendered, claim.unit or "")


def _set_answer(value: object) -> str:
    """Canonical assertion for a SET_EQUAL field: order is typography.

    Same split `_compare_sets` uses, so `project()` agrees with the detector.
    Lists are not sorted in `_render` / `render_value`: those must keep agreeing
    with `==` for `_identity`.
    """
    if not isinstance(value, list | tuple | set | frozenset):
        return _render(value, fold_equal=True)
    editions = _editions_by_base(value)
    parts: list[str] = []
    for base in sorted(editions):
        years = tuple(sorted(year for year in editions[base] if year is not None))
        parts.append(f"{base}:{','.join(years)}")
    return "set[" + "|".join(parts) + "]"


def _identity(claim: FieldClaim) -> tuple[str, ...]:
    """A hashable, total rendering of a claim's content.

    Rendered rather than the object itself, because `FieldClaim.value` is typed
    `object | None` and the contract has at least nine list- and dict-valued
    parameters - `certifications`, `ul_listing`, `standards`,
    `rating_mva_by_cooling`, `ercot_compliance_items`. An earlier version used
    `set(claims)` and raised `TypeError: unhashable type: 'list'` on
    `certifications`, which this module's own tier table calls Tier A. The
    projection crashed on the fields it classifies as most critical.

    **Rendered without `_render`'s equality fold, and that is the load-bearing
    half of the split.** This is the dedup key and the total sort order, so it
    has to separate every claim record that is not byte-for-byte the same one -
    including two whose values are `==` but spelled differently. Folding here
    would collapse them to one row and hand the choice of surviving spelling to
    arrival order, which `encode_value` then carries into the workbook's bytes.
    `_asserted` is where the fold belongs; see `_render`.
    """
    return (
        claim.document_id,
        claim.field_name,
        claim.extractor_version,
        json.dumps(
            encode_value(claim.condition.grouping_key()),
            sort_keys=True,
            separators=(",", ":"),
        ),
        _render(claim.value),
        claim.unit or "",
        claim.verbatim_value or "",
        claim.source_tier.value,
        repr(claim.source_ref.model_dump(mode="json")),
        repr(claim.confidence),
        ""
        if claim.resolution is None
        else json.dumps(
            encode_value(claim.resolution.model_dump(mode="json")),
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def canonical_claims(claims: Iterable[FieldClaim]) -> list[FieldClaim]:
    """Claims in canonical order, with exact duplicates collapsed.

    Two workers producing byte-identical claims is one assertion twice - what an
    idempotency key is for. Two claims sharing a `claim_key` and differing *only*
    in provenance or confidence are also one assertion: a datasheet printing the
    same figure in a summary table and again in the electrical-characteristics
    table yields two claims differing in `source_ref.page`, and that is normal
    rather than a defect. Only a difference in the **asserted value** - see
    `_asserted`: the value and the unit it is in - is a genuine same-key
    contradiction.
    """
    ordered = sorted({_identity(c): c for c in claims}.values(), key=_identity)
    asserted: dict[tuple[object, ...], tuple[str, str]] = {}
    for claim in ordered:
        signature = _asserted(claim)
        previous = asserted.get(claim.claim_key())
        if previous is not None and previous != signature:
            raise ProposalError(
                f"two different values share the claim key {claim.claim_key()}: "
                f"{previous} vs {signature}. One extractor version produced two "
                "answers for one field under one condition."
            )
        asserted[claim.claim_key()] = signature
    return ordered


def _status_for(group: Sequence[FieldClaim]) -> ConflictStatus:
    """Whether a set of like-for-like claims disagrees.

    Computed over **every** claim, not over the winning tier. Restricting it to
    the system-of-record subset meant a web value contradicting the record came
    back `NONE` - the projection arbitrated in one direction and then reported no
    conflict, which is exactly what FR-WEB-04 says must be queued for a human.

    A claim with no value counts as a distinct answer rather than being filtered
    out. "Extractor A found nothing, extractor B found 650 W" is the commonest
    real disagreement in this domain, and dropping the `None` first made it
    invisible.

    Over `_asserted`, the same signature `canonical_claims` uses. Comparing
    `repr(value)` alone read `650 W` and `650 kW` as one answer and stored the
    pair as agreement, which is the unit conflict FR-ING-08 says tolerance may
    never absorb.
    """
    if _human_decision(group) is not None:
        # D-16: a reviewer's decision settles the group. A later extraction that
        # disagrees does not reopen it - reopening is a human action
        # (REQUEST_MORE_WEB_SEARCH, capped by task F.3), not a side effect of
        # re-running an extractor.
        return ConflictStatus.RESOLVED
    members = list(group)
    # SET_EQUAL's "missing edition is unknown" is not an equivalence, so a
    # canonical string cannot be the status. Pairwise `values_conflict` is the
    # detector, and Open Items must agree with the queue.
    if tolerance_for(members[0].field_name).rule is ToleranceRule.SET_EQUAL:
        field_name = members[0].field_name
        row = tolerance_for(field_name)
        for index, left in enumerate(members):
            for right in members[index + 1 :]:
                if values_conflict(
                    left.as_candidate(),
                    right.as_candidate(),
                    tolerance=row,
                    field_name=field_name,
                ).conflicts:
                    return ConflictStatus.OPEN
        return ConflictStatus.NONE
    answers = {_asserted(claim) for claim in members}
    return ConflictStatus.OPEN if len(answers) > 1 else ConflictStatus.NONE


def _human_decision(group: Sequence[FieldClaim]) -> FieldClaim | None:
    """The reviewer's claim that settles this group, if there is one (D-16).

    The latest `resolved_at` wins - stored data, not the clock - because a
    reopened conflict records a new decision rather than rewriting the old one.
    `_status_for` and `_preferred` both go through this, so a RESOLVED status
    and the claim whose decision is copied onto the field come from one
    computation and cannot disagree.
    """
    humans = [claim for claim in group if claim.is_human]
    if not humans:
        return None
    return max(humans, key=lambda c: (c.resolution.resolved_at, _identity(c)))  # type: ignore[union-attr]


def _preferred(group: Sequence[FieldClaim]) -> FieldClaim:
    """Which claim supplies the canonical value for a condition group.

    The source-of-record rule first (TRS section 1), then *a stated value beats a
    missing one*, then confidence, then the canonical order as a last resort. The
    first version took `winners[0]` straight from an order led by `document_id`,
    which meant a populated value lost to a `None` from an alphabetically earlier
    filename - and renaming a file changed the canonical answer.

    This does not arbitrate a genuine disagreement: `_status_for` has already
    marked the group OPEN, so a human still decides. It only picks what to show
    beside the conflict.

    **A human claim outranks everything** (D-16): the decision is the
    reviewer's, so tier and confidence are irrelevant, and `_human_decision`
    picks the latest.
    """
    decided = _human_decision(group)
    if decided is not None:
        return decided
    return sorted(
        group,
        key=lambda c: (
            c.source_tier is not SourceTier.SYSTEM_OF_RECORD,
            c.value is None,
            -c.confidence,
            _identity(c),
        ),
    )[0]


def project(claims: Sequence[FieldClaim]) -> list[CanonicalField]:
    """The canonical values implied by a set of claims. A pure function.

    Returns **one `CanonicalField` per condition group**, matching
    `ComponentInstance.fields` being list-valued: the Sungrow trio is three
    stored values, not one value and two discarded.

    No store handle, no write, no clock - given the same claims it returns the
    same list, which is the half of FR-OUT-06 that lives below composition.

    Claims are grouped by `condition.grouping_key()`, which is the display
    partition and *not* the comparison relation. Deciding which stored values
    disagree is `comparison_pairs`' job; this only decides what is stored.

    **One field per call, and it is checked.** The grouping key is the condition
    and nothing else, so claims about two different parameters with the same
    condition - and the commonest condition by far is the empty one - landed in
    one group: one of the two values was discarded and the survivor came back
    OPEN, reporting a conflict between a nameplate and an efficiency. That is not
    a comparison, and it was silent.
    """
    fields = sorted({claim.field_name for claim in claims})
    if len(fields) > 1:
        raise ProposalError(
            f"a projection covers one field, not {fields}. Claims are grouped by "
            "condition alone, so mixing fields silently discards values and "
            "reports a conflict between two different parameters."
        )
    ordered = canonical_claims(claims)
    if not ordered:
        return []

    groups: dict[object, list[FieldClaim]] = {}
    for claim in ordered:
        groups.setdefault(claim.condition.grouping_key(), []).append(claim)

    projected: list[CanonicalField] = []
    for key in sorted(groups, key=repr):
        group = groups[key]
        chosen = _preferred(group)
        status = _status_for(group)
        projected.append(
            CanonicalField(
                value=chosen.value,
                unit=chosen.unit,
                verbatim_value=chosen.verbatim_value,
                condition=chosen.condition,
                source_tier=chosen.source_tier,
                source_ref=chosen.provenance(),
                confidence=chosen.confidence,
                unresolved_status=(
                    UnresolvedStatus.NONE
                    if status is ConflictStatus.RESOLVED
                    else UnresolvedStatus(status.value)
                ),
                resolution=chosen.resolution,
            )
        )
    return projected


def commit_claims(
    field_name: str,
    claims: Sequence[FieldClaim],
    *,
    writer: ClaimWriter,
    category: ComponentCategory | None = None,
) -> list[CanonicalField]:
    """The single serial reducer. The only path from a claim to a stored value.

    Calls `assert_no_autonomous_overwrite` once at a chokepoint rather than N
    times by convention. With append-only claims there is usually nothing to
    catch - that is the point - but it stays because the projection can still be
    asked to replace a stored system-of-record value with a web-only one when the
    record claim is dropped from a later run.

    **The claim set has to be complete, and both halves of that are checked.**
    `commit` replaces the whole field, so a projection is a statement about every
    condition group and not only the ones it mentions:

    - A claim naming another field would be stored under this one, so the store
      would hold a percentage labelled as watts.
    - A stored group the projection does not cover is *deleted* by the commit,
      and the guard could not see it: keyed on the condition group, it only ever
      compared groups present in both. A web claim under a new condition
      therefore removed a system-of-record value under the old one - the
      autonomous overwrite FR-WEB-03 and FR-HITL-02 forbid, reached by changing
      the condition instead of the value.

    Raised rather than merged. Claims are append-only and `project` is a pure
    function of them, so a group can only vanish because the caller passed a
    subset; folding the leftovers back in would hide that and make the store a
    function of commit history rather than of the claims.

    **`field_name` has to be a contract key**, and that is checked first - before
    the foreign-field check, before the projection, and before the empty-claim-set
    early return. Order is load-bearing: an invented key arriving with no claims
    is exactly what a broken extractor produces, and a check placed after the
    early return would wave it through. Contract C2, and the reason it is a
    boundary rather than a lint is that claims are append-only: there is no
    correcting a row once it is written under a key nothing looks up.

    `category` is optional because the store is keyed on the field name alone and
    a caller reducing one field does not always have a `ComponentInstance` in
    hand. Passing it is strictly stronger and should be preferred: without it the
    check is membership in the union of all keys, which admits `chemistry` for an
    inverter. The other enforcement point (`ComponentInstance.fields`) always has
    the category and always uses it, so the weaker form here is a narrower window
    rather than an open one.
    """
    try:
        require_contract_key(field_name, category)
    except OffContractFieldError as exc:
        raise ProposalError(str(exc)) from exc
    foreign = sorted({claim.field_name for claim in claims} - {field_name})
    if foreign:
        raise ProposalError(
            f"claims about {foreign} cannot be committed under {field_name!r}; "
            "the field name is the store key."
        )
    projected = project(claims)
    if not projected:
        return []
    existing = {field.condition.grouping_key(): field for field in writer.current(field_name)}
    covered = {field.condition.grouping_key() for field in projected}
    dropped = sorted((key for key in existing if key not in covered), key=repr)
    if dropped:
        raise StoredValueLossError(
            f"committing this projection would drop {len(dropped)} stored condition "
            f"group(s) of {field_name!r}: {dropped}. The reducer takes the complete "
            "claim set for a field, because `commit` replaces it."
        )
    for field in projected:
        assert_no_autonomous_overwrite(existing.get(field.condition.grouping_key()), field)
    writer.commit(field_name, projected)
    return projected


#: Parameter names that denote a store write handle.
#:
#: Substring matching was the first attempt and it failed both ways: `restore`
#: matched on `store`, while `storage`, `db`, `repo`, `sink`, `session` and
#: `history` did not. Exact names plus the annotation check is narrower and
#: honest about what it can see.
WRITE_HANDLE_NAMES: frozenset[str] = frozenset({"writer", "store", "storage", "claim_writer"})


def takes_a_write_handle(func: Callable[..., object]) -> bool:
    """Whether `func` can reach the store *through its signature*.

    **This is a lint, not a proof, and the difference matters.** A worker can
    still hold a writer as a module global, a closure cell or `self.writer`, and
    no signature check sees any of those. The unreachability property C8 actually
    wants is architectural - workers live in a package that cannot import the
    store - and this catches only the commonest way to break it. Claiming more
    for it than that would be the honour system with extra steps.
    """
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins have no signature
        return False
    return any(
        name in WRITE_HANDLE_NAMES or "ClaimWriter" in str(parameter.annotation)
        for name, parameter in parameters.items()
    )
