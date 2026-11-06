"""Conflict & HITL (FR-HITL-01 .. FR-HITL-06).

Field-level reconciliation, conflict queue, human-resolution interface,
decision logging.

This module owns the spec's hardest invariant, so it is the one place in the
scaffolding where behaviour is implemented rather than stubbed: FR-HITL-02 says
the system shall NOT auto-arbitrate web against an ingested contract or spec
sheet. `assert_no_autonomous_overwrite` is the chokepoint that enforces it, and
AC-2 tests it directly.
"""

from __future__ import annotations

import itertools
import math
import re
import unicodedata
from collections.abc import Collection, Sequence
from decimal import Decimal, InvalidOperation
from typing import TypeGuard

from pydantic import BaseModel, ConfigDict, Field

from ...schema import (
    CONDITION_DIMENSION_NAMES,
    CanonicalField,
    ConflictCandidate,
    ConflictClass,
    DeclaredBand,
    SourceTier,
    StandardsRegime,
    ToleranceCondition,
    ToleranceRule,
    canonical_json,
)
from ...schema.field import UNIT_ALIASES, normalise_unit
from ...schema.registry import condition_dimensions_for
from .severity import assign_severity as assign_severity  # re-exported: the D-3 lookup
from .tolerance import FieldTolerance
from .tolerance import tolerance_for as tolerance_for  # re-exported: the table's entry point


def _canonical(value: object) -> str:
    """One sort component: `value` as canonical JSON text.

    `encode_value` is the authority on *what* a value is (D-14, contract C4), but
    it returns JSON-representable Python, and that is not sortable. The domain
    here is polymorphic on purpose - `ConflictCandidate.value` is `object | None`,
    so one component holds a `Decimal` for one candidate and `None` for the next,
    and `sorted()` across those raises `TypeError`. Something has to turn the
    encoded form into a single ordered type.

    Canonical JSON text is that something, and it is the same device
    `encoding._sort_key` uses for both of its unordered containers, frozenset
    members and map pairs - deliberately
    the same idea rather than the same function, because that helper is private
    to `encoding` and named for the one case it serves, and importing it would
    make a narrowing there a breaking change here. The properties are what this
    needs and all of what it needs:

    * **Total.** Every encoded value has a text form, so no mixed-type pair is
      incomparable the way the underlying values are.
    * **Deterministic.** `separators` fixes the spacing, so the text depends on
      nothing but the value.
    * **Stable across processes.** Nothing in it derives from a hash seed or a
      memory address, which `repr()` of a set or a bare object does.

    `sort_keys` is redundant today and kept anyway: every object reaching here
    comes from `_encode_model`, which builds its dict in pydantic's field
    declaration order, so insertion order is already fixed. No test can therefore
    catch its removal - the reason it is written down here rather than only in a
    test is that the day an encoded object is built any other way, key order
    would start depending on construction order and nothing would say so.

    The induced order is not meaningful and is not read as a ranking anywhere -
    `"$decimal"` sorting after `null` says nothing about decimals. What it has to
    be is the *same* order every run, on every machine, which is the whole of
    what `_ordering_key` and the display partition ask of it.

    Closed world, inherited: a value outside D-14's table raises here rather than
    sorting. That is the intended direction. `repr()` would have ordered a bare
    object by `<object object at 0x7f...>`, an address that moves between runs -
    a canonical order that silently is not one - and the same value has to be
    encodable for the C6 projection anyway, so failing at the sort surfaces it at
    the earliest point rather than at artifact time.
    """
    return canonical_json(value)


def _ordering_key(candidate: ConflictCandidate) -> tuple[str, ...]:
    """A total order over candidates that depends only on their content.

    Needed because FR-OUT-06 makes composition a pure function of the store: any
    list the queue payload is built from has to be arranged by what a candidate
    *is*, never by when it arrived.

    Every field of the candidate participates. An earlier version keyed on only
    five of them, so two candidates differing solely in `verbatim_value` - which
    FR-HITL-03 *requires* the queue to carry - tied, and `sorted` being stable
    then preserved arrival order in both the list and each pair's orientation.
    A key that is not total is not a canonical order.

    Every element goes through `_canonical` rather than being folded through
    `x or ""`, which was the same defect wearing different clothes: `None` and
    `""` are distinct candidate states, and mapping both onto the empty string
    tied two candidates that genuinely differ. The encoding keeps them apart -
    `null` and `""` - which is the property that matters, not the spelling.

    **`repr()` was the A-50 defect and is why `_canonical` exists.** `repr` of an
    enum member is `<MeasurementBasis.STC: 'stc'>`, so the key embedded a class
    name and a member name: renaming `STC` moved this key, hence the sort order,
    hence the row order of every artifact composed from it, while not one
    measurement in the store had changed. That is the same class as the unpinned
    `openpyxl`, and as the `repr(grouping_key())` the display partition below
    *used to* carry - `comparison_groups` was converged onto `_canonical` in the
    same change that fixed this one, so the pointer is history rather than
    somewhere to look. Third recurrence, and the reason the question to ask of
    anything entering a hashed artifact is *could this change without the data
    changing?*

    `source_ref` goes in as the model, not as `model_dump(mode="json")`. Pydantic's
    JSON mode is a second encoder: it would serialise a `Decimal` field added to
    `SourceRef` later by its own rules rather than under the `$decimal` tag, so
    two distinct precisions would collide here while staying distinct in the
    audit preimage. One authority or none.

    `schema.field._normalise_token` establishes that the substitution is real -
    it exists partly to handle an extractor emitting `""` where `None` is meant -
    but it takes the **opposite** policy, collapsing `""` to `None` so an empty
    token cannot read as a stated dimension. That is right there and wrong here:
    normalising is a choice a vocabulary boundary gets to make, and this key is
    not a boundary. It has to order faithfully whatever the model actually holds,
    and two candidates that differ are two candidates. Nothing normalises a
    candidate's `unit` or `verbatim_value` on the way in, so preserving the
    distinction is the only way the order stays total.
    """
    return (
        _canonical(candidate.condition.grouping_key()),
        _canonical(candidate.value),
        _canonical(candidate.unit),
        _canonical(candidate.source_tier),
        _canonical(candidate.source_ref),
        _canonical(candidate.verbatim_value),
        _canonical(candidate.confidence),
        _canonical(candidate.condition.note),
        # The frozenset goes in whole: `encode_value` sorts its members by the
        # same canonical text, so the pre-sort this line used to do would now be a
        # second ordering rule saying the same thing.
        _canonical(candidate.condition.derived),
    )


def comparison_pairs(
    candidates: Sequence[ConflictCandidate], *, field_name: str
) -> list[tuple[ConflictCandidate, ConflictCandidate]]:
    """Every pair of candidates for `field_name` that may be compared like for like.

    **Pairs, not groups.** Comparability is genuinely not transitive - `@30 degC`
    and `@40 degC` are each comparable with an unstated condition but not with
    each other - and that is a fact about the domain, not a defect to design
    around. A partition has to be transitive, so any attempt to express this as
    groups must either bucket by arrival order (nondeterministic) or partition on
    exact condition equality (which strands values whose conditions are merely
    *less* specific, and silently drops the comparison). Pairs carry the relation
    exactly as it is.

    Both failures were shipped here before, so they are worth naming. First-fit
    bucketing over `comparable_with` gave a different conflict queue depending on
    document order. Exact-key grouping then made it worse: a supply agreement
    stating `650 W` with no test condition stranded alone while the datasheet and
    CEC listing, both marked STC, compared only with each other - so the
    system-of-record value FR-HITL-02 exists to protect reached no queue entry and
    the compose gate never fired. A visible false positive had become an invisible
    false negative, and a reviewer cannot dismiss what they never see.

    Deterministic because the result is a function of the candidate *set*: every
    unordered pair is generated once, each pair is internally ordered by
    `_ordering_key`, and the list is sorted by the same key. No pair is emitted
    twice, so a disagreement between two unstated values is raised once no matter
    how many stated conditions sit alongside it.

    `Condition.grouping_key()` remains the right tool for *displaying* candidates
    grouped by condition; it is not the right tool for deciding what to compare.

    **`field_name` is required, and it is the third shipped failure here.** The
    gate used to apply every `ConditionDimensions` field to every key, so a
    dimension that says nothing about the parameter in hand still refused the
    comparison: `country_of_origin` China against Vietnam produced *no pair at
    all* because one sheet was IEEE and the other IEC. That is a CRITICAL,
    BABA/FEOC-relevant field reaching no queue entry, so the compose gate never
    fires - the same invisible false negative as the exact-key grouping above,
    one layer down. FN-2.

    A default would have preserved it. Every call site would keep the
    field-agnostic gate, silently, and the argument that fixes the defect would
    be the one nobody passes. `ConflictQueueEntry.severity` carries no default
    for the same reason: a safety interlock that can be forgotten is not one.

    Which dimensions govern which key is `registry.condition_dimensions_for`,
    transcribed by hand from the frozen contract's Conditions table and checked
    against it in both directions. Deriving it from key patterns was measured and
    rejected - a pattern written for Imp swallows `impedance_percent` and hands
    Transformer %Z the PV `basis` dimension, which is a *new* suppression
    introduced by the fix. A key with no row gates on nothing, which is the noise
    direction rather than the suppressing one.
    """
    dimensions = condition_dimensions_for(field_name)
    ordered = sorted(candidates, key=_ordering_key)
    return [
        (left, right)
        for left, right in itertools.combinations(ordered, 2)
        if left.condition.comparable_with(right.condition, dimensions=dimensions)
    ]


def conflict_groupings(
    candidates: Sequence[ConflictCandidate], *, field_name: str
) -> list[tuple[ConflictCandidate, ConflictCandidate]]:
    """The candidate sets a `ConflictQueueEntry` may be built from: **exactly one
    comparable pair each**.

    A contract, not an implementation detail, because the alternative is provably
    impossible. Take the Sungrow trio - `352 W @30 degC`, an unqualified `320 W`
    from a supply agreement, `320.865 W @40 degC`. Comparability has edges A-B and
    B-C but not A-C. Enumerate every partition of three elements:

        {A,B,C}        asserts A-C   - the false conflict Condition exists to stop
        {A,B} {C}      loses B-C
        {B,C} {A}      loses A-B
        {A,C} {B}      asserts A-C
        {A} {B} {C}    loses both

    **No partition works.** An entry whose `candidates` list has length n asserts
    that all C(n,2) members are like-for-like - that is what asking a human to
    choose among them means - so a non-transitive relation cannot be covered by
    disjoint sets. Duplication is forced by the structure, not chosen: B must
    appear in two entries.

    Connected-component folding is the trap, because it looks like the natural
    answer and collapses to `{A,B,C}` here - identical to the union, identical to
    the forbidden case. Grouping by exact condition is the opposite trap: three
    singletons and no conflict raised at all.

    This does not breach FR-HITL-03, whose "all candidate values" is scoped to *a
    conflict*; it redefines what a conflict is. Two consequences it does not
    solve, which contract C5 still owes a rule for:

    - **Cross-entry resolution coherence.** With B in two entries a reviewer can
      select A in one and C in the other, leaving one field with two contradictory
      canonical values. Nothing constrains that today.
    - **Queue inflation.** n mutually comparable candidates yield C(n,2) entries
      rather than one, against a review budget `Settings.review_budget_fraction`
      explicitly meters.

    `field_name` is passed straight through, and is required here for the reason
    it is required there: this is the function a `ConflictQueueEntry` is built
    from, so a field-agnostic gate at this boundary is the one that reaches the
    queue - or rather, does not.
    """
    return comparison_pairs(candidates, field_name=field_name)


def comparison_groups(candidates: Sequence[ConflictCandidate]) -> list[list[ConflictCandidate]]:
    """Candidates partitioned by exact condition, for display only.

    Deterministic - the partition is by `grouping_key()` equality and both the
    groups and their members are canonically sorted. Use `comparison_pairs` to
    decide what to compare: this partition strands a candidate whose condition is
    merely less specific than its neighbours', which is a presentation choice, not
    a comparison rule.

    The group order comes from `_canonical`, not `repr`, for the reason
    `_ordering_key` gives: a grouping key is mostly enum members, and `repr` names
    them by class and member name, so renaming one silently reordered the display.
    The keys cannot be sorted directly - a tuple position holds a member for one
    condition and `None` for another, and `sorted` raises on that pair - so the
    encoding is what makes an order exist at all, not merely an honest one.
    """
    grouped: dict[tuple[object, ...], list[ConflictCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.condition.grouping_key(), []).append(candidate)
    return [sorted(grouped[key], key=_ordering_key) for key in sorted(grouped, key=_canonical)]


class AutonomousOverwriteError(RuntimeError):
    """Raised when something tries to overwrite a system-of-record value with web data.

    This is a programming error, not a data condition. Reaching it means a code
    path bypassed the conflict queue, which FR-WEB-03 and FR-HITL-02 forbid.
    """


def assert_no_autonomous_overwrite(
    existing: CanonicalField | None, incoming: CanonicalField
) -> None:
    """Guard the hard rule from TRS section 1.

    Web data may populate an empty field, but may never replace a value that
    came from an ingested contract or spec sheet. A disagreement between the two
    is queued for a human instead (FR-WEB-04).

    **A human's decision is the one thing that may.** The rule is against
    *autonomous* overwrite - the word is in the name - and a reviewer choosing
    the web candidate in the queue is FR-HITL-04's SELECT_VALUE, the opposite of
    autonomous. A field carrying a `Resolution` is that decision projected (D-16),
    so it passes; a web value with no decision behind it still does not.
    """
    if existing is None or existing.value is None:
        return
    if incoming.resolution is not None:
        return
    if (
        existing.source_tier is SourceTier.SYSTEM_OF_RECORD
        and incoming.source_tier is SourceTier.WEB_SUPPLEMENT
    ):
        raise AutonomousOverwriteError(
            "web_supplement may not overwrite a system_of_record value; "
            "raise a conflict instead (FR-WEB-03, FR-HITL-02)"
        )


class IncomparableCandidatesError(ValueError):
    """Raised when `values_conflict` is asked to compare a pair that is not a
    comparison at all - mismatched conditions, or a field D-2 marks never-compare.

    A programming error, not a data condition: candidates reach the comparison
    through `comparison_pairs`, which already applies the condition gate. Raising
    rather than returning "no conflict" on purpose - the two are indistinguishable
    to a caller, and "we did not compare these" silently rendered as "these agree"
    is the invisible false negative this module exists to prevent.
    """


class ConflictVerdict(BaseModel):
    """The outcome of comparing two candidates for one field."""

    model_config = ConfigDict(frozen=True)

    conflicts: bool
    conflict_class: ConflictClass | None = Field(
        default=None, description="Which of the five FR-HITL-01 classes; None when no conflict"
    )
    reason: str = Field(description="Why, in the terms FR-HITL-03 requires the queue to explain")


_EDITION = re.compile(r"\s*:\s*(\d{4})\s*$")


def _normalise_text(value: str) -> str:
    """Fold to a comparable form: NFKC, case, and internal whitespace.

    No fuzzy matching, deliberately. A fuzzy match that silently equates two
    certifications is unrecoverable; a false conflict is one extra queue item.
    """
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


#: Why `schema.field.UNIT_ALIASES` folds the spellings it does: the frozen contract
#: declares them **one unit**. Not a unit-conversion table, and deliberately not the start of one.
#:
#: A temperature *coefficient* is per-degree-*interval*, and one degree Celsius is
#: one kelvin as an interval, so `%/degC`, `%/K` and `%/°C` name the same quantity
#: and need no conversion at all. Three documents say so: the frozen contract's
#: Conditions table ("`%/degC` = `%/K`"), tasks.md B.3, and clarifications.md
#: under the heading "The unit conversion that must NOT happen".
#:
#: **Never route these through a temperature converter.** A generic unit library
#: applies the +273.15 offset, and `-0.29 %/K` silently becomes `272.86` - a
#: number that passes every plausibility gate B.5 states except the sign. Folding
#: the spelling is the whole of the fix; there is no arithmetic here.
#:
#: Without it, every temperature-coefficient comparison between a K-quoting source
#: and a degC-quoting one came back a `UNIT_NORMALIZATION` conflict. All three
#: coefficients are on contract and every PV datasheet prints them, so that is a
#: queue item per module per pair on a field where nothing disagreed. FN-5.
#:
#: The degree-sign row carries the sign because a datasheet prints it; U+2103 is
#: absent because NFKC already folds it to `°C`, and listing it would suggest the
#: table is doing work `_normalise_text` has already done.
_UNIT_ALIASES = UNIT_ALIASES
_normalise_unit = normalise_unit


def _split_edition(value: str) -> tuple[str, str | None]:
    """`IEC 61215:2021` -> `("iec 61215", "2021")`.

    The year is stripped so an edition difference is not read as a different
    standard, and *retained* so it becomes a TEMPORAL conflict rather than
    disappearing. Dropping it would silently equate a 2016 certification with a
    2021 one, which is a compliance claim nobody made.
    """
    text = _normalise_text(value)
    match = _EDITION.search(text)
    if match is None:
        return text, None
    return _EDITION.sub("", text), match.group(1)


def _is_sequence(value: object) -> TypeGuard[Collection[object]]:
    return isinstance(value, list | tuple | set | frozenset)


def _editions_by_base(values: Collection[object]) -> dict[str, set[str | None]]:
    """Each element split into `(standard, edition)` and grouped by standard."""
    grouped: dict[str, set[str | None]] = {}
    for element in values:
        text = element if isinstance(element, str) else _canonical(element)
        base, year = _split_edition(text)
        grouped.setdefault(base, set()).add(year)
    return grouped


def _as_number(value: object) -> float | None:
    """A numeric reading of a candidate value, or None if it is not a number.

    `bool` is excluded even though it is an `int`: `True` comparing equal to a
    1.0 nameplate is nonsense that no tolerance would catch.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float | Decimal):
        number = float(value)
        return number if math.isfinite(number) else None
    return None


def _places(text: str) -> int:
    try:
        exponent = Decimal(text).as_tuple().exponent
    except InvalidOperation:
        return 0
    return max(0, -exponent) if isinstance(exponent, int) else 0


#: The two ways a run of digits and commas can be read, because a comma is
#: genuinely ambiguous and neither reading is right on its own.
#:
#: Grouped treats `,\d{3}` as a thousands separator, which bare digit runs get
#: wrong: they split `125,000.50` into `125` and `000.50`, so no token equals the
#: parsed value and the scan falls through to `repr(float)` and its lost trailing
#: zero. Ungrouped stops at every comma, which the grouped reading gets wrong in
#: the other direction: two table cells that lost their separating whitespace
#: render as `650,700.20`, and reading that as one number loses the 700.20 that is
#: actually there.
#:
#: Both are tried. A token only counts when it equals the parsed value, so the
#: extra readings cannot invent a match - `_decimals` is choosing between
#: interpretations of the same text, not trusting either one.
_NUMBER_TOKENS = (
    re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?(?:[eE][+-]?\d+)?"),
    re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"),
)


def _decimals(candidate: ConflictCandidate) -> int:
    """Printed decimal places — D-2's `decimals_a` / `decimals_b`.

    `verbatim_value` is the source text, so where it carries a number it is the
    authority and the parsed value is only a fallback. Taking the *maximum* over
    both instead looks conservative and is not: `float(22)` reprs as `22.0`, so
    every integer-printed value would claim one decimal it never had, and a sheet
    printing `22` would be held to a precision it cannot express.

    **The verbatim number has to be the value.** Reading the first numeric token
    unconditionally meant a leading qualifier decided the precision of the whole
    comparison - `"@ 25 degC: 22.35 %"` reported zero places, widening the floor
    to 0.5 and absorbing a 0.45 pp disagreement that is 4.5x the D-2 band. A
    thousands separator did it too, since the regex stopped at the comma. Fewer
    decimals means a *wider* floor, so this direction hides conflicts, and it
    turns on the extractor's formatting rather than on the data. Every candidate
    number in the text is tried, and one that does not equal `value` is not the
    value.

    Trying every token fixed the qualifier but not the separator, and the two
    failures are not the same shape: a leading qualifier offers a *wrong* token,
    while `125,000.50` offers *no* token equal to the value, so the scan found
    nothing and fell back to `repr(float)` - which drops the printed trailing
    zero and reports one decimal place where the source printed two. Under EXACT
    there is no magnitude to mask it, and EXACT is the default rule for every
    contract key D-2's table does not cover.

    A comma cannot be resolved by picking one reading, so `_NUMBER_TOKENS` holds
    both and every match from either is tried. Requiring the token to equal the
    parsed value is what makes that safe.

    An `int` and a `Decimal` report their own precision rather than going through
    `float`: `Decimal("650")` is the natural representation of exactly the
    catalog values D-2 calls EXACT, and `repr(float(...))` would give it a
    decimal place it never had - the `650` vs `650.0` case D-2 names.
    """
    number = _as_number(candidate.value)
    if candidate.verbatim_value is not None and number is not None:
        printed: int | None = None
        for pattern in _NUMBER_TOKENS:
            for found in pattern.finditer(candidate.verbatim_value):
                text = found.group().replace(",", "")
                try:
                    matches = float(text) == number
                except ValueError:  # pragma: no cover - the regex only matches numerals
                    continue
                if not matches:
                    continue
                # The most precise reading wins. Under-reporting is the direction
                # that widens the floor and hides conflicts, and if any reading of
                # this text prints n decimals then the source did print them.
                places = _places(text)
                printed = places if printed is None else max(printed, places)
        if printed is not None:
            return printed
    value = candidate.value
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return 0
    if isinstance(value, Decimal):
        return _places(str(value))
    return 0 if number is None else _places(repr(number))


def _rounding_floor(a: ConflictCandidate, b: ConflictCandidate) -> float:
    """D-2: `1/2 x 10^(-min(decimals_a, decimals_b))`.

    Two values are never in conflict by less than the precision the coarser
    source printed. Without it `650` and `650.4` would disagree under a 0.1 pp
    band that the coarser sheet could not have expressed.
    """
    return 0.5 * 10.0 ** (-min(_decimals(a), _decimals(b)))


def _classify(a: ConflictCandidate, b: ConflictCandidate) -> ConflictClass:
    """Which of the five FR-HITL-01 classes a disagreement belongs to.

    Derived from the candidates rather than passed in, so the class cannot drift
    from the evidence it describes.
    """
    if a.source_tier is not b.source_tier:
        return ConflictClass.RECORD_VS_WEB
    a_doc = a.source_ref.document_id or a.source_ref.url
    b_doc = b.source_ref.document_id or b.source_ref.url
    if a_doc == b_doc:
        return ConflictClass.INTRA_DOCUMENT
    return ConflictClass.INTER_DOCUMENT


def values_conflict(
    a: ConflictCandidate,
    b: ConflictCandidate,
    *,
    tolerance: FieldTolerance,
    field_name: str | None = None,
    band_a: DeclaredBand | None = None,
    band_b: DeclaredBand | None = None,
) -> ConflictVerdict:
    """Whether two candidate values for one field actually disagree (FR-WEB-04).

    The TRS says "beyond tolerance" and never defines tolerance; clarifications
    D-2 does, per field and in four kinds, and `tolerance` is a row of that table
    (`tolerance_for(field_name)`). There is no global default parameter here on
    purpose - a single float was the defect D-2 exists to remove, and leaving one
    in the signature would let it come back as a call-site default.

    `band_a`/`band_b` carry *declared* bands. When either is supplied it governs
    the comparison whatever the field's tolerance rule (a printed band supersedes
    the configured one); a DECLARED_BAND field with neither falls back to
    printed precision. They
    are parameters rather than attributes of the candidate because the band lives
    on a different canonical field (`power_tolerance`) than the value it
    qualifies (`nameplate_power_w`); only a caller holding the whole component
    can pair them.

    Answers the four questions the stub left open:

    - **Relative or absolute?** Both, per field, plus one-sided and two rules
      that are not thresholds at all. See `ToleranceRule`.
    - **Different units?** Never resolved by tolerance. A unit mismatch is a
      `UNIT_NORMALIZATION` conflict in its own right - normalising here would
      hide an extraction defect behind a successful comparison.
    - **Strings?** Normalise, then exact-match, with the edition year split off
      and kept so `IEC 61215:2021` vs `IEC 61215:2016` is TEMPORAL rather than a
      string mismatch, and `IEC 61215:2021` vs `IEC 61215` is not a conflict.
      No fuzzy matching.
    - **Missing on one side?** Not a conflict - a gap. It flags MISSING_DATA and
      triggers the FR-WEB-01 search; `RECORD_VS_WEB` needs both sides to hold a
      value.

    `field_name` scopes the condition guard below to the dimensions that actually
    govern this parameter, so it agrees with the gate `comparison_pairs` applied
    when it selected the pair. Scoping only that one would have turned a fixed
    suppression into a crash: `comparison_pairs` would hand over a
    `country_of_origin` pair tagged with two standards regimes and this guard
    would refuse it.

    Optional, unlike on `comparison_pairs`, because two callers have no contract
    field to name - `services.identity` corroborates Voc and Isc, which are not
    contract keys, and the tests construct ad-hoc tolerances. Omitting it applies
    **every** dimension, which is today's behaviour and deliberately the strict
    reading: this guard *raises*, so an over-wide gate here is loud where the
    same over-wide gate in `comparison_pairs` was silent. The asymmetry between
    the two defaults is the asymmetry between refusing and dropping.
    """
    if tolerance.rule is ToleranceRule.NEVER_COMPARE:
        raise IncomparableCandidatesError(
            "this field names two different physical quantities; comparing them is "
            "not a wide tolerance, it is not a comparison (D-2)"
        )
    dimensions = (
        CONDITION_DIMENSION_NAMES if field_name is None else condition_dimensions_for(field_name)
    )
    if not a.condition.comparable_with(b.condition, dimensions=dimensions):
        raise IncomparableCandidatesError(
            f"conditions do not match ({a.condition.grouping_key()} vs "
            f"{b.condition.grouping_key()}); a mismatch is not a conflict, it is "
            "not a comparison (D-1). Use comparison_pairs to select candidates."
            + (
                ""
                if field_name is not None
                else " No field_name was given, so every dimension gated this "
                "comparison; pass one if only some of them govern the parameter."
            )
        )

    if a.value is None or b.value is None:
        return ConflictVerdict(
            conflicts=False,
            reason="one side has no value; a gap flags MISSING_DATA and triggers the "
            "FR-WEB-01 search rather than raising a conflict",
        )

    # `None` is compared as its own value rather than skipping the check. The
    # guard read `a.unit is not None and b.unit is not None and ...`, which put
    # **the permissive branch on the suppressing side**: one missing unit skipped
    # the gate entirely and the pair fell through to a numeric comparison in
    # whatever unit each side happened to be in. Measured: `0.35` against
    # `0.35 USD/kW` returned "no conflict" on a 1000x price error, on a Tier A
    # field. The old form also produced an inversion - `Wp` vs `W` raised a
    # conflict while `None` vs `kW` did not - and tasks.md E.3a makes suppression
    # a spec violation where noise is only a cost.
    #
    # Two absent units still compare equal, which is load-bearing rather than
    # incidental: every text-valued contract field carries `unit=None` on both
    # sides, and `a.unit != b.unit` alone would turn each of them into a unit
    # conflict.
    unit_a = _normalise_unit(a.unit) if a.unit is not None else None
    unit_b = _normalise_unit(b.unit) if b.unit is not None else None
    if unit_a != unit_b:
        stated = f"units differ ({a.unit!r} vs {b.unit!r})"
        missing = (
            f"one side has no unit ({a.unit!r} vs {b.unit!r}), so the two numbers name "
            "quantities that cannot be shown to be in the same scale"
        )
        return ConflictVerdict(
            conflicts=True,
            conflict_class=ConflictClass.UNIT_NORMALIZATION,
            reason=(stated if unit_a is not None and unit_b is not None else missing)
            + "; a unit mismatch is never resolved by tolerance (FR-ING-08)",
        )

    if tolerance.rule is ToleranceRule.SET_EQUAL:
        return _compare_sets(a, b)

    number_a, number_b = _as_number(a.value), _as_number(b.value)
    if number_a is None or number_b is None:
        if isinstance(a.value, str) and isinstance(b.value, str):
            return _compare_text(a, b)
        if number_a is None and number_b is None and a.value == b.value:
            return ConflictVerdict(conflicts=False, reason="values are equal")
        if _is_sequence(a.value) and _is_sequence(b.value):
            return ConflictVerdict(
                conflicts=True,
                conflict_class=_classify(a, b),
                reason=(
                    f"lists differ as written ({_canonical(a.value)} vs "
                    f"{_canonical(b.value)}); this field has "
                    "no SET_EQUAL tolerance row, so element order counts"
                ),
            )
        return ConflictVerdict(
            conflicts=True,
            conflict_class=_classify(a, b),
            reason=(
                "values are not comparable as numbers or text "
                f"({_canonical(a.value)} vs {_canonical(b.value)})"
            ),
        )

    return _compare_numbers(a, b, number_a, number_b, tolerance, band_a, band_b)


def _compare_sets(a: ConflictCandidate, b: ConflictCandidate) -> ConflictVerdict:
    """D-17: a `list[str]` field compared as a set of normalised elements.

    Order is typography. Each element gets the same treatment a single string
    does - `_normalise_text` and `_split_edition` - so a certification list is
    compared standard by standard, and an edition difference on one standard is
    a TEMPORAL conflict on that element rather than a set mismatch. Containment
    is a conflict: for attestations, absence is the finding (FR-HITL-01), and
    `{UL 1741}` against `{UL 1741, IEC 61215}` is one source claiming a
    certification the other does not.
    """
    if not (_is_sequence(a.value) and _is_sequence(b.value)):
        return ConflictVerdict(
            conflicts=True,
            conflict_class=_classify(a, b),
            reason=(
                f"a SET_EQUAL field holds a non-list value ({_canonical(a.value)} vs "
                f"{_canonical(b.value)}); "
                "an extractor emitted a scalar where the contract types a list"
            ),
        )

    left, right = _editions_by_base(a.value), _editions_by_base(b.value)
    only_left = sorted(left.keys() - right.keys())
    only_right = sorted(right.keys() - left.keys())
    if only_left or only_right:
        return ConflictVerdict(
            conflicts=True,
            conflict_class=_classify(a, b),
            reason=f"sets differ after normalisation (only in first: {only_left}; only in "
            f"second: {only_right}); containment is a conflict, because for an "
            "attestation absence is the finding",
        )
    for base in sorted(left):
        years_a = {year for year in left[base] if year is not None}
        years_b = {year for year in right[base] if year is not None}
        if years_a and years_b and years_a != years_b:
            return ConflictVerdict(
                conflicts=True,
                conflict_class=ConflictClass.TEMPORAL,
                reason=f"same standards, different edition of {base!r} "
                f"({sorted(years_a)} vs {sorted(years_b)}); an edition difference is a "
                "temporal conflict, not a set mismatch",
            )
    return ConflictVerdict(conflicts=False, reason="sets match after normalisation")


def _compare_text(a: ConflictCandidate, b: ConflictCandidate) -> ConflictVerdict:
    assert isinstance(a.value, str) and isinstance(b.value, str)
    base_a, year_a = _split_edition(a.value)
    base_b, year_b = _split_edition(b.value)
    if base_a != base_b:
        return ConflictVerdict(
            conflicts=True,
            conflict_class=_classify(a, b),
            reason=f"text differs after normalisation ({base_a!r} vs {base_b!r}); "
            "no fuzzy matching - a silently equated certification is unrecoverable",
        )
    if year_a is not None and year_b is not None and year_a != year_b:
        return ConflictVerdict(
            conflicts=True,
            conflict_class=ConflictClass.TEMPORAL,
            reason=f"same standard, different edition ({year_a} vs {year_b}); an edition "
            "difference is a temporal conflict, not a string mismatch",
        )
    return ConflictVerdict(conflicts=False, reason="text matches after normalisation")


def _compare_numbers(
    a: ConflictCandidate,
    b: ConflictCandidate,
    number_a: float,
    number_b: float,
    tolerance: FieldTolerance,
    band_a: DeclaredBand | None,
    band_b: DeclaredBand | None,
) -> ConflictVerdict:
    conflict_class = _classify(a, b)
    floor = _rounding_floor(a, b)

    if tolerance.rule is ToleranceRule.DECLARED_BAND or band_a is not None or band_b is not None:
        if band_a is None and band_b is None:
            return ConflictVerdict(
                conflicts=abs(number_a - number_b) > floor,
                conflict_class=conflict_class if abs(number_a - number_b) > floor else None,
                reason="declared-band field with no band on either side; compared at "
                "printed precision only",
            )
        reference = band_a if band_a is not None else band_b
        assert reference is not None
        # The units come from the candidates, which is the half of FN-3 that
        # `DeclaredBand` cannot do for itself: `unit` was validated at
        # construction and then read by nobody, because nothing ever handed
        # `resolve` a nominal's unit to check it against. A `0 ~ +5 W` band
        # against a value in kW resolved to `(0.65, 5.65)` and absorbed a 7.7x
        # disagreement. `values_conflict` has already equated `a.unit` and
        # `b.unit` above, so a raise from here means the *band's* unit disagrees
        # with the field's - a data defect worth surfacing, not a verdict.
        agrees = (
            band_a.agrees(number_a, band_b, number_b, nominal_unit=a.unit, other_unit=b.unit)
            if band_a is not None
            else reference.agrees(
                number_b, band_a, number_a, nominal_unit=b.unit, other_unit=a.unit
            )
        )
        return ConflictVerdict(
            conflicts=not agrees,
            conflict_class=None if agrees else conflict_class,
            reason="guaranteed ranges "
            + ("intersect" if agrees else "are disjoint")
            + "; a printed band supersedes the config default for this field",
        )

    if tolerance.rule is ToleranceRule.ONE_SIDED:
        return _compare_one_sided(a, b, number_a, number_b, tolerance, conflict_class, floor)

    magnitude = _effective_magnitude(tolerance, a, b, number_a, number_b)
    if tolerance.rule is ToleranceRule.RELATIVE:
        # Referred to the larger magnitude, so the test does not depend on which
        # candidate is named first. Against the smaller it is asymmetric, and an
        # asymmetric predicate would make the queue depend on argument order -
        # the same defect `_ordering_key` exists to keep out.
        magnitude *= max(abs(number_a), abs(number_b))
    band = max(magnitude, floor)
    difference = abs(number_a - number_b)
    conflicts = difference > band
    return ConflictVerdict(
        conflicts=conflicts,
        conflict_class=conflict_class if conflicts else None,
        reason=f"|{number_a} - {number_b}| = {difference:g} vs band {band:g} "
        f"(max of {tolerance.rule.value} {magnitude:g} and rounding floor {floor:g})",
    )


def _effective_magnitude(
    tolerance: FieldTolerance,
    a: ConflictCandidate,
    b: ConflictCandidate,
    number_a: float,
    number_b: float,
) -> float:
    """The band D-2 gives *for this pair*, not just for this field.

    Four D-2 rows state two bands rather than one, and encoding only the first is
    silent: the comparison then applies a band that is right half the time with
    nothing marking which half. IEC's impedance tolerance widens from 7.5% to 10%
    below Z=10%, and a transformer's total-loss allowance is 6% under IEEE where
    it is 10% under IEC - a wrong branch is a real conflict suppressed or a false
    one raised, either way invisibly.

    Each discriminator is read from data the candidates already carry, so nothing
    has to be threaded in: the compared magnitude, `Condition.standards_regime`
    (which D-6 already makes load-bearing), and `SourceRef.source_authority`.
    """
    magnitude = tolerance.magnitude or 0.0
    if tolerance.alternate_when is None or tolerance.alternate_magnitude is None:
        return magnitude

    if tolerance.alternate_when is ToleranceCondition.IMPEDANCE_BELOW_10_PCT:
        selected = max(abs(number_a), abs(number_b)) < 10.0
    elif tolerance.alternate_when is ToleranceCondition.REGIME_IEEE:
        selected = StandardsRegime.IEEE in {
            a.condition.standards_regime,
            b.condition.standards_regime,
        }
    else:  # AGAINST_CEC_LIST
        selected = any(
            candidate.source_ref.source_authority is not None
            and "cec" in candidate.source_ref.source_authority.casefold()
            for candidate in (a, b)
        )
    return tolerance.alternate_magnitude if selected else magnitude


def _compare_one_sided(
    a: ConflictCandidate,
    b: ConflictCandidate,
    number_a: float,
    number_b: float,
    tolerance: FieldTolerance,
    conflict_class: ConflictClass,
    floor: float,
) -> ConflictVerdict:
    """`conflict <=> (measured - declared) > tolerance` (D-2).

    Both IEC and IEEE state loss and no-load-current limits in one direction
    only, and IEC 60076-1 Table 1 notes that an omitted direction is
    unrestricted: being *under* guarantee is never a nonconformity, so a test
    report below a contract value is not a disagreement.

    Which side is the declared one comes from the source tier - a contract or
    spec sheet is the guarantee, web data is the report against it.

    **The allowance does not apply declared-against-declared.** IEC's +15% is how
    far a *measured* loss may exceed a *guaranteed* one; it says nothing about two
    documents disagreeing on what was guaranteed. Two datasheets stating 100 kW
    and 109 kW of no-load loss are two declarations, and a 9% gap between them is
    an inter-document conflict a reviewer has to see - applying the measurement
    allowance symmetrically silently absorbed it. So when neither side is
    identifiable as the measurement, the comparison falls back to printed
    precision, which is what docs/defaults.md said the rule should be all along.
    """
    if a.source_tier is b.source_tier:
        difference = abs(number_a - number_b)
        conflicts = difference > floor
        return ConflictVerdict(
            conflicts=conflicts,
            conflict_class=conflict_class if conflicts else None,
            reason=f"both candidates are {a.source_tier.value}, so neither is the "
            f"measurement; the one-sided allowance does not apply between two "
            f"declarations and |{number_a} - {number_b}| = {difference:g} is compared "
            f"at printed precision {floor:g}",
        )
    declared, measured = (
        (number_a, number_b)
        if a.source_tier is SourceTier.SYSTEM_OF_RECORD
        else (number_b, number_a)
    )
    band = max(_effective_magnitude(tolerance, a, b, number_a, number_b) * abs(declared), floor)
    excess = measured - declared
    conflicts = excess > band
    return ConflictVerdict(
        conflicts=conflicts,
        conflict_class=conflict_class if conflicts else None,
        reason=f"declared {declared:g}, measured {measured:g}, excess {excess:g} vs "
        f"one-sided limit {band:g}; under guarantee is never a nonconformity",
    )
