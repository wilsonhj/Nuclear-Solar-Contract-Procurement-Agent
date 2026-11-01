"""Workers propose, they do not commit — contract C8, issue #8.

Every case marked "review" below is a defect two independent reviews found in
the first version of this module. They are kept as tests rather than as
changelog, because each one passed a green suite.
"""

import inspect
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from procurement_agent.schema import (
    CanonicalField,
    CellFlag,
    Condition,
    ConflictStatus,
    MeasurementBasis,
    PowerSide,
    Resolution,
    ResolutionAction,
    SourceRef,
    SourceTier,
    encode_value,
)
from procurement_agent.services import claims as claims_module
from procurement_agent.services.claims import (
    HUMAN_PREFIX,
    ClaimWriter,
    FieldClaim,
    ProposalError,
    _identity,
    canonical_claims,
    commit_claims,
    project,
    reduce_field,
    takes_a_write_handle,
)
from procurement_agent.services.conflict_hitl import AutonomousOverwriteError, comparison_pairs
from procurement_agent.services.output import flags_for


class _Store:
    def __init__(self) -> None:
        self.committed: dict[str, list[CanonicalField]] = {}
        self.writes = 0

    def commit(self, field_name: str, values: Sequence[CanonicalField]) -> None:
        self.committed[field_name] = list(values)
        self.writes += 1

    def current(self, field_name: str) -> list[CanonicalField]:
        return self.committed.get(field_name, [])


def _claim(
    value: object,
    *,
    tier: SourceTier = SourceTier.SYSTEM_OF_RECORD,
    doc: str = "doc-a",
    version: str = "extract@1",
    confidence: float = 0.9,
    condition: Condition | None = None,
    field: str = "nameplate_power",
    page: int | None = None,
    unit: str | None = "Wp",
    verbatim: str | None = None,
    resolution: Resolution | None = None,
) -> FieldClaim:
    payload: dict[str, object] = {
        "document_id": doc,
        "field_name": field,
        "extractor_version": version,
        "condition": condition or Condition(),
        "value": value,
        "unit": unit,
        "verbatim_value": verbatim,
        "source_tier": tier,
        "source_ref": SourceRef(document_id=doc, page=page),
        "confidence": confidence,
    }
    if resolution is not None:
        payload["resolution"] = resolution
    return FieldClaim(**payload)  # type: ignore[arg-type]


def _sungrow() -> list[FieldClaim]:
    """D-1's worked example: one datasheet, one field, three ambients."""
    return [
        _claim(
            v,
            field="rated_ac_power",
            doc="sungrow-eu-ds",
            condition=Condition(temperature_c=t),
        )
        for v, t in ((352.0, 30.0), (320.0, 40.0), (295.0, 50.0))
    ]


# --- contract C2: a claim carries its condition ---------------------------------


def test_a_claim_carries_its_condition() -> None:
    """C2, "including `condition` per D-1". The first version omitted it."""
    assert "condition" in FieldClaim.model_fields


def test_the_sungrow_trio_raises_no_conflict() -> None:
    """Review: without `condition`, every candidate came out `is_unstated()` and
    `comparison_pairs` returned **3 pairs** on the case D-1 opens with — "four
    apparent conflicts and zero real ones". The condition gate was defeated on
    the only path that reaches it."""
    candidates = [c.as_candidate() for c in _sungrow()]
    assert not any(c.condition.is_unstated() for c in candidates)
    assert comparison_pairs(candidates, field_name="rated_ac_power") == []


def test_three_ambients_are_three_claims_not_a_contradiction() -> None:
    """Review: `claim_key` was `(document_id, field_name, extractor_version)`, so
    one datasheet stating one parameter at three ambients raised `ProposalError`
    — and a test asserted that was correct."""
    assert len({c.claim_key() for c in _sungrow()}) == 3
    assert len(canonical_claims(_sungrow())) == 3


def test_the_sungrow_trio_stores_three_values() -> None:
    """`ComponentInstance.fields` is list-valued precisely so this is three
    stored values rather than one value and two discarded."""
    projected = project(_sungrow())
    assert [f.value for f in projected] == [352.0, 320.0, 295.0]
    assert [f.condition.temperature_c for f in projected] == [30.0, 40.0, 50.0]
    assert {f.conflict_status for f in projected} == {ConflictStatus.NONE}


def test_condition_reaches_the_store() -> None:
    projected = project([_claim(700.0, condition=Condition(basis=MeasurementBasis.STC))])
    assert projected[0].condition.basis is MeasurementBasis.STC


# --- what the projection decides, and what it refuses to decide -----------------


def test_a_populated_value_is_never_lost_to_a_missing_one() -> None:
    """Review: `distinct` filtered `None` before counting and the winner was the
    alphabetically-first document, so "A found nothing, B found 650 W" stored
    `None` **and** reported `NONE`. `is_missing()` then called it missing data
    and the 650 was unrecoverable."""
    projected = project([_claim(None, doc="a-datasheet"), _claim(650.0, doc="b-datasheet")])
    assert len(projected) == 1
    assert projected[0].value == 650.0
    assert projected[0].conflict_status is ConflictStatus.OPEN
    assert not projected[0].is_missing()


def test_a_web_claim_contradicting_the_record_is_queued() -> None:
    """Review: the status was computed over the system-of-record subset, so a
    contradicting web value came back `NONE`. FR-WEB-04 says that disagreement is
    queued for a human, and `assert_no_autonomous_overwrite`'s own docstring says
    the same."""
    projected = project(
        [
            _claim(650.0, doc="contract"),
            _claim(700.0, doc="web-1", tier=SourceTier.WEB_SUPPLEMENT),
        ]
    )
    assert projected[0].value == 650.0, "the record still supplies the value"
    assert projected[0].source_tier is SourceTier.SYSTEM_OF_RECORD
    assert projected[0].conflict_status is ConflictStatus.OPEN


def test_the_record_supplies_the_value_even_when_the_web_looks_better() -> None:
    """Mutation: deleting the source-tier term from `_preferred`'s sort key left
    the suite green, because in every case tested the record also happened to
    have the alphabetically-first `document_id` — the very defect this module was
    rewritten to remove, hiding inside its own regression test.

    Here the web claim wins on confidence *and* on filename, so only the
    source-of-record rule (TRS section 1, FR-HITL-02) can produce 650.
    """
    projected = project(
        [
            _claim(650.0, doc="zzz-contract", confidence=0.5),
            _claim(700.0, doc="aaa-web", tier=SourceTier.WEB_SUPPLEMENT, confidence=0.99),
        ]
    )
    assert projected[0].value == 650.0
    assert projected[0].source_tier is SourceTier.SYSTEM_OF_RECORD
    assert projected[0].conflict_status is ConflictStatus.OPEN


def test_the_canonical_value_does_not_depend_on_the_filename() -> None:
    """Review: `winners[0]` over an order led by `document_id` meant renaming a
    document changed the stored answer — and AC-7 then made it reproducibly
    wrong."""
    high = project([_claim(650.0, doc="aaa"), _claim(655.0, doc="zzz", confidence=0.5)])
    low = project([_claim(650.0, doc="mmm"), _claim(655.0, doc="bbb", confidence=0.5)])
    assert high[0].value == low[0].value == 650.0


def test_two_record_claims_that_disagree_are_not_arbitrated() -> None:
    """Picking one would be the auto-arbitration FR-HITL-02 forbids."""
    projected = project([_claim(650.0, doc="contract"), _claim(700.0, doc="datasheet")])
    assert projected[0].conflict_status is ConflictStatus.OPEN


def test_agreeing_claims_leave_the_field_clean() -> None:
    projected = project([_claim(650.0, doc="contract"), _claim(650.0, doc="datasheet")])
    assert projected[0].conflict_status is ConflictStatus.NONE


@pytest.mark.parametrize(
    "value",
    [["UL 61730", "IEC 61215"], {"onan": 100.0, "onaf": 125.0}, None, 650.0, "Dyn11"],
)
def test_every_contract_value_shape_projects(value: object) -> None:
    """Review: `set(claims)` raised `TypeError: unhashable type: 'list'`. The
    contract has at least nine list- and dict-valued parameters, and
    `certifications` is Tier A — the projection crashed on the fields the repo
    classifies as most critical."""
    projected = project([_claim(value)])
    assert projected[0].value == value


# --- append-only, and what counts as a contradiction ----------------------------


def test_an_identical_claim_from_two_workers_is_one_assertion() -> None:
    claim = _claim(650.0)
    assert len(canonical_claims([claim, claim, claim])) == 1


def test_the_same_figure_printed_twice_in_one_document_is_not_a_defect() -> None:
    """Review: any two claims sharing a key and differing in *any* field raised —
    including `source_ref.page`. A datasheet printing the same figure in a
    summary table and again in the electrical-characteristics table is normal."""
    both = canonical_claims([_claim(650.0, page=2), _claim(650.0, page=7)])
    assert len(both) == 2
    assert len(project(both)) == 1


def test_two_values_under_one_condition_is_still_a_defect() -> None:
    """The case the error was written for: one extractor version, one condition,
    two different answers."""
    with pytest.raises(ProposalError):
        canonical_claims([_claim(650.0), _claim(700.0)])


def test_a_unit_mismatch_is_not_agreement() -> None:
    """Review: `_status_for` compared `repr(value)` and nothing else, so two
    claims reading `650 W` and `650 kW` were one answer and the field was stored
    `NONE` — with whichever unit `_preferred` happened to pick.

    `values_conflict` calls exactly this a `UNIT_NORMALIZATION` conflict that "is
    never resolved by tolerance (FR-ING-08)", and `canonical_claims` already
    counts the unit as part of the asserted value. The projection was the one
    place that did not.
    """
    projected = project(
        [
            _claim(650.0, doc="contract", unit="W"),
            _claim(650.0, doc="datasheet", unit="kW"),
        ]
    )
    assert projected[0].conflict_status is ConflictStatus.OPEN


def test_two_dicts_with_the_same_entries_are_one_assertion() -> None:
    """Review: value identity was `repr`, and `repr` of a dict follows insertion
    order. Two extractions of one cooling table that read the rows in different
    orders produced values that are `==` and reprs that are not, so they counted
    as a disagreement.

    The contract has three dict-valued parameters - `rating_mva_by_cooling`,
    `harmonic_spectrum` and `ercot_compliance_items` - and this module's own
    docstring names the first of them.
    """
    left = {"onan": 100.0, "onaf": 125.0}
    right = {"onaf": 125.0, "onan": 100.0}
    assert left == right
    projected = project(
        [
            _claim(left, field="rating_mva_by_cooling", doc="d-1"),
            _claim(right, field="rating_mva_by_cooling", doc="d-2"),
        ]
    )
    assert projected[0].conflict_status is ConflictStatus.NONE

    # Same key, so the mismatch would have aborted the whole field rather than
    # merely mis-reporting it.
    assert len(canonical_claims([_claim(left, page=2), _claim(right, page=7)])) == 2


# --- one number, spelled two ways ----------------------------------------------
#
# `_render`'s docstring promises "a textual rendering of a claim value that
# respects equality" and delivered it only for container *order*. A number's
# spelling was still `repr`, so `650` from a table cell and `650.0` from a unit
# normaliser were two answers.


@pytest.mark.parametrize(
    ("left", "right", "why"),
    [
        (650, 650.0, "int from a table cell against a float from unit normalisation"),
        (650.0, 650, "and the same pair the other way round"),
        (0.0, -0.0, "the fold ConditionDimensions._reject_non_finite already does one class over"),
        (10**20, float(10**20), "an integral float wide enough that repr goes exponential"),
    ],
)
def test_one_number_spelled_two_ways_is_one_assertion(
    left: object, right: object, why: str
) -> None:
    """Review: `_render` fell through to `repr` for a number, so `650` and `650.0`
    - `==`, and the same reading - were a disagreement.

    Across documents that is a spurious OPEN conflict, sent to a human who finds
    two identical values. Within one `claim_key` it is the hard failure the
    docstring says the function exists to prevent for the dict case it did
    handle: `ProposalError: two different values share the claim key ...
    ('650','W') vs ('650.0','W')`, and the whole field is lost.
    """
    assert left == right, why
    same_key = canonical_claims([_claim(left, page=2), _claim(right, page=7)])
    assert len(project(same_key)) == 1
    across_documents = project([_claim(left, doc="contract"), _claim(right, doc="datasheet")])
    assert across_documents[0].conflict_status is ConflictStatus.NONE


def test_a_number_nested_in_a_container_is_folded_too() -> None:
    """The fold has to reach a dict value and a dict key, because that is where
    the contract's numbers actually sit: `rating_mva_by_cooling` is a dict of
    ratings, and a unit normaliser rewrites the values inside it, not the dict.

    A dict *key* too: `{1: 'a'}` and `{1.0: 'a'}` are one dict by Python's own
    reckoning - the two keys hash alike, so no dict can hold both.
    """
    by_value = project(
        [
            _claim({"onan": 100, "onaf": 125}, field="rating_mva_by_cooling", doc="d-1"),
            _claim({"onaf": 125.0, "onan": 100.0}, field="rating_mva_by_cooling", doc="d-2"),
        ]
    )
    assert by_value[0].conflict_status is ConflictStatus.NONE
    by_key = project(
        [
            _claim({1: "a"}, field="harmonic_spectrum", doc="d-1"),
            _claim({1.0: "a"}, field="harmonic_spectrum", doc="d-2"),
        ]
    )
    assert by_key[0].conflict_status is ConflictStatus.NONE


def test_a_set_and_a_frozenset_with_the_same_members_are_one_assertion() -> None:
    """`{'a'} == frozenset({'a'})` - unlike a list against a tuple, set equality
    ignores which spelling holds the members, so `certifications` read once into
    a set and once into a frozenset is one answer.

    Which spelling is *stored* is the same constraint the numeric fold carries,
    with a sharper edge: `encode_value` accepts a frozenset and refuses a bare
    set outright, so a fold inside `_identity` would collapse the two records and
    leave arrival order deciding whether the artifact encodes at all. Asserted
    on `type` and over both orders, because `==` cannot see the difference.
    """
    pair = [
        _claim({"UL 61730"}, field="certifications"),
        _claim(frozenset({"UL 61730"}), field="certifications"),
    ]
    assert len(canonical_claims(pair)) == 2, "two records, one assertion"
    for ordering in (pair, list(reversed(pair))):
        projected = project(ordering)
        assert len(projected) == 1
        assert projected[0].conflict_status is ConflictStatus.NONE
        assert type(projected[0].value) is frozenset
        assert projected[0].value == frozenset({"UL 61730"})


def test_the_fold_stops_where_python_equality_stops() -> None:
    """The three places the rule deliberately parts company with `==`, each with
    a reason this repo already acts on somewhere else.

    * **A bool is not a number here.** `True == 1`, and `severity._numeric_value`
      excludes `bool` for exactly this reason - a stray boolean must not read as
      a nameplate of 1. `encode_value` tests `bool` before `int` for the same
      reason one type layer down.
    * **A list is not a tuple**, and Python agrees - so no fold is needed, and
      the container type tag that distinguishes them must survive one that is.
    Decimal printed precision is *not* an asserted-value difference: the
    detector already folds `Decimal("22.00")` with `Decimal("22")` via
    `_as_number`, and D-2's rounding floor is read from `verbatim_value` /
    `encode_value`, not from `_asserted`.
    """
    cases: list[tuple[object, object, bool]] = [
        (True, 1, True),
        ([1], (1,), False),
    ]
    for left, right, python_calls_them_equal in cases:
        assert (left == right) is python_calls_them_equal, (
            f"{left!r} vs {right!r}: the premise of this row has changed"
        )
        pair = [_claim(left, doc="d-1"), _claim(right, doc="d-2")]
        assert project(pair)[0].conflict_status is ConflictStatus.OPEN, (
            f"{left!r} and {right!r} were folded into one assertion"
        )
        assert len(canonical_claims(pair)) == 2


def test_folding_a_number_does_not_make_the_stored_spelling_order_dependent() -> None:
    """The constraint the fold has to respect: `_render` also feeds `_identity`,
    which is the dedup key *and* the total sort order.

    Fold inside `_identity` and the two claims collapse to one row, leaving the
    survivor to `{...}.values()`'s last-wins - so `project` stores `650` or
    `650.0` depending on which worker finished first. That is not cosmetic:
    `encode_value` renders them `650` and `650.0`, `ComponentInstance
    ._stored_values` hashes that text into `ordering_key`, and the workbook's row
    order - and its AC-7 hash - would become a function of arrival order. FR-OUT-06
    is exactly the property that forbids it.

    So the fold belongs to `_asserted` ("what does this claim say"), and
    `_identity` ("which claim record is this") keeps every spelling apart. Both
    claims survive, and `_preferred` breaks the tie by `_identity`, which is
    total.

    Two claims identical in **every** field but the spelling, deliberately: vary
    the page as well and `_identity` differs whether or not it folds, the two
    rows survive either way, and the mutation this test exists for goes
    undetected. It did, on the first version of this test.

    Compared as encoded text and never as values, for the same reason:
    `CanonicalField.value` is `object | None`, so pydantic's `==` on two
    otherwise-identical fields asks `650 == 650.0` and answers True. The
    artifact is the JSON, and the JSON is `650` against `650.0`.
    """
    pair = [_claim(650), _claim(650.0)]
    assert len(canonical_claims(pair)) == 2, "two records, one assertion"
    stored = [json.dumps(encode_value(field.value)) for field in project(pair)]
    assert stored == [
        json.dumps(encode_value(field.value)) for field in project(list(reversed(pair)))
    ]
    assert stored == ["650"]


def test_a_self_referential_value_does_not_blow_the_stack() -> None:
    """Whatever renders a claim's value has to be total. `repr` guards its own
    cycles; a hand-rolled canonical rendering has to guard them too."""
    looping: list[object] = []
    looping.append(looping)
    assert len(canonical_claims([_claim(looping)])) == 1


def test_the_same_figure_printed_twice_may_differ_in_source_text() -> None:
    """Review: `verbatim_value` was part of the same-key contradiction signature,
    so the summary table's `650 W` and the electrical table's `650` raised
    `ProposalError` and the whole field was lost.

    `test_the_same_figure_printed_twice_in_one_document_is_not_a_defect` passed
    only because both claims carried `verbatim_value=None`: it varied `page`,
    which was already excluded, and never the thing that still failed. Verbatim
    text is the source text at a location, which is provenance in exactly the way
    the page number is.
    """
    both = canonical_claims(
        [_claim(650.0, page=2, verbatim="650 W"), _claim(650.0, page=7, verbatim="650")]
    )
    assert len(both) == 2
    assert len(project(both)) == 1


def test_re_extraction_appends_rather_than_overwrites() -> None:
    assert len(canonical_claims([_claim(650.0, version="v1"), _claim(655.0, version="v2")])) == 2


def test_the_projection_does_not_depend_on_completion_order() -> None:
    """Two workers finishing in different orders must not give different stores."""
    trio = _sungrow()
    assert project(trio) == project(list(reversed(trio)))


def test_a_claim_cannot_be_revised_in_place() -> None:
    with pytest.raises(ValidationError):
        _claim(650.0).value = 700.0


def test_a_claim_refuses_a_field_name_it_does_not_have() -> None:
    """Pydantic's default is `extra="ignore"`, so a *mistyped optional* field was
    dropped in silence - required fields are protected only incidentally, by
    their own missing-field error.

    Append-only makes that sharp here in a way it is not elsewhere: a claim
    written with `verbatim_valeu` cannot be corrected, only superseded, and the
    B.9 gold set is labelled against whatever was stored. Same class as issue
    #16 one level up, which closed the condition *vocabulary* while leaving the
    field *name* open-world. The full argument lives on `ComponentInstance`.

    Both halves are asserted: the typo is refused, and the correctly-spelled
    field it was a typo of is still accepted - a rejection that came from the
    field being unknown to the model rather than from `extra="forbid"` would
    prove nothing.
    """
    with pytest.raises(ValidationError, match="verbatim_valeu"):
        FieldClaim(
            document_id="doc-a",
            field_name="nameplate_power",
            extractor_version="extract@1",
            value=650.0,
            verbatim_valeu="650 W",  # type: ignore[call-arg]
            source_tier=SourceTier.SYSTEM_OF_RECORD,
            source_ref=SourceRef(document_id="doc-a"),
            confidence=0.9,
        )
    assert _claim(650.0, verbatim="650 W").verbatim_value == "650 W"


# --- the structural property ----------------------------------------------------


def test_only_the_reducer_takes_a_write_handle() -> None:
    public = {
        name: obj
        for name, obj in vars(claims_module).items()
        if not name.startswith("_") and inspect.isfunction(obj)
    }
    assert "commit_claims" in public
    writers = {name for name, obj in public.items() if takes_a_write_handle(obj)}
    assert writers == {"commit_claims"}, sorted(writers)


@pytest.mark.parametrize("name", ["writer", "store", "storage", "claim_writer"])
def test_the_common_write_handle_names_are_detected(name: str) -> None:
    """Review: substring matching flagged `restore` and missed `storage`."""
    assert takes_a_write_handle(eval(f"lambda {name}: None"))  # noqa: S307


def test_an_annotated_handle_is_detected_whatever_it_is_called() -> None:
    def worker(sink: ClaimWriter) -> None: ...

    assert takes_a_write_handle(worker)


def test_a_lookalike_name_is_not_flagged() -> None:
    """`restore` and `history` contain no write handle."""
    assert not takes_a_write_handle(lambda restore, history: None)


def test_the_lint_does_not_claim_to_be_a_proof() -> None:
    """Review: the first version was described as asserting the property "for
    every present and future worker". A signature check cannot see a module
    global, a closure cell or `self.writer`, and saying otherwise is the honour
    system with extra steps."""
    captured = _Store()

    def worker_with_a_closure() -> None:
        captured.commit("x", [])

    assert not takes_a_write_handle(worker_with_a_closure)
    assert "lint, not a proof" in (takes_a_write_handle.__doc__ or "")


def test_the_write_handle_is_keyword_only() -> None:
    assert (
        inspect.signature(commit_claims).parameters["writer"].kind is inspect.Parameter.KEYWORD_ONLY
    )


def test_the_reducer_applies_the_guard_per_condition() -> None:
    store = _Store()
    commit_claims("nameplate_power", [_claim(650.0)], writer=store)
    with pytest.raises(AutonomousOverwriteError):
        commit_claims(
            "nameplate_power",
            [_claim(700.0, tier=SourceTier.WEB_SUPPLEMENT, doc="web-1")],
            writer=store,
        )
    assert store.writes == 1, "the rejected commit must not have reached the store"


def test_a_web_value_under_a_new_condition_is_not_an_overwrite() -> None:
    """The guard is keyed per condition group: a web claim at 40 degC does not
    overwrite a record claim at 30 degC, because it does not replace it.

    Review: this asserted only `store.writes == 2`, which a store that had just
    *deleted* the 30 degC record value satisfies exactly as well as one that kept
    it. `commit` replaces the whole field, so the write count says nothing about
    what survived it. The end state is the claim, so the end state is what is
    asserted.
    """
    store = _Store()
    record = _claim(352.0, field="rated_ac_power", condition=Condition(temperature_c=30.0))
    commit_claims("rated_ac_power", [record], writer=store)
    commit_claims(
        "rated_ac_power",
        [
            record,
            _claim(
                320.0,
                field="rated_ac_power",
                condition=Condition(temperature_c=40.0),
                tier=SourceTier.WEB_SUPPLEMENT,
            ),
        ],
        writer=store,
    )
    assert store.writes == 2
    stored = {
        f.condition.temperature_c: (f.value, f.source_tier) for f in store.current("rated_ac_power")
    }
    assert stored == {
        30.0: (352.0, SourceTier.SYSTEM_OF_RECORD),
        40.0: (320.0, SourceTier.WEB_SUPPLEMENT),
    }


def test_a_later_run_may_not_drop_a_stored_condition_group() -> None:
    """Review: `commit` replaces the whole field, and the guard is keyed on the
    condition group, so it only ever saw groups present in *both*. A web claim
    under a condition group the record does not have therefore reached
    `writer.commit` unopposed and the record value was deleted with it — the
    autonomous overwrite FR-WEB-03 and FR-HITL-02 forbid, achieved by changing
    the condition rather than the value.

    The projection is a pure function of the claim set, and claims are
    append-only, so a group can only vanish because the caller passed a subset.
    That is a programming error and it is raised as one; merging the leftovers
    back in would make the store a function of commit history instead.
    """
    store = _Store()
    commit_claims(
        "rated_ac_power",
        [_claim(352.0, field="rated_ac_power", condition=Condition(temperature_c=30.0))],
        writer=store,
    )
    with pytest.raises(ProposalError, match="drop|complete"):
        commit_claims(
            "rated_ac_power",
            [
                _claim(
                    320.0,
                    field="rated_ac_power",
                    condition=Condition(temperature_c=40.0),
                    tier=SourceTier.WEB_SUPPLEMENT,
                    doc="web-1",
                )
            ],
            writer=store,
        )
    assert store.writes == 1
    assert [f.value for f in store.current("rated_ac_power")] == [352.0]


def test_the_projection_is_for_one_field_at_a_time() -> None:
    """Review: `project` grouped by condition alone and ignored `field_name`
    entirely, so two claims about *different parameters* with the same (commonly
    empty) condition landed in one group. One of the two values was then silently
    discarded and the survivor was reported OPEN — a conflict between a nameplate
    and an efficiency, which is not a comparison at all.
    """
    with pytest.raises(ProposalError, match="one field"):
        project(
            [
                _claim(650.0, field="nameplate_power"),
                _claim(21.5, field="module_efficiency"),
            ]
        )


def test_the_reducer_refuses_a_claim_about_another_field() -> None:
    """The field name is the store key. Nothing checked that the claims agreed
    with it, so a `module_efficiency` claim committed cleanly under
    `nameplate_power` and the store held a percentage labelled as watts."""
    store = _Store()
    with pytest.raises(ProposalError, match="module_efficiency"):
        commit_claims("nameplate_power", [_claim(21.5, field="module_efficiency")], writer=store)
    assert store.writes == 0


def test_a_store_satisfies_the_protocol_structurally() -> None:
    assert isinstance(_Store(), ClaimWriter)


def test_no_claims_is_no_value() -> None:
    assert project([]) == []
    assert commit_claims("nameplate_power", [], writer=_Store()) == []


# --- contract C3: provenance survives the projection -----------------------------


def test_source_ref_carries_c3s_four_elements() -> None:
    """C3 is `(document_id, page, span, extractor_version)`, and `span` is the
    only one of the four whose contract name is not the attribute name: it is
    spelled `section`.

    That mismatch is not cosmetic. An audit of this repository grepped `src/`
    for a `span` field, found none, and concluded C3 had been marked **done**
    with an element missing — a reasonable inference from four of the five
    places C3 was cited, none of which mentioned the rename. The mapping was
    recorded in exactly one paragraph of `current-state.md`, which is not where
    someone checking the claim looks.

    So this pins the mapping somewhere executable. It also fails if `section` is
    ever renamed away without the contract being renamed with it, which is the
    direction that would make the audit's conclusion retroactively correct.
    """
    fields = set(SourceRef.model_fields)
    assert {"document_id", "page", "section", "extractor_version"} <= fields
    assert "span" not in fields


def test_the_extractor_version_reaches_the_store() -> None:
    """C3 is `(document_id, page, span, extractor_version)`, whose `span` is the
    `section` field on `SourceRef`. The first three lived there; the fourth lived
    only on the claim, so `project` dropped it
    and a stored value could not be traced to the code that produced it. A
    regression is then invisible in the store while the claim recording it is
    still sitting there."""
    projected = project([_claim(650.0, version="extract@3", page=4)])
    assert projected[0].source_ref.extractor_version == "extract@3"
    assert projected[0].source_ref.page == 4


def test_the_queue_entry_carries_it_too() -> None:
    """A reviewer comparing two candidates needs to see which extractor produced
    each, or 'these disagree' is missing the commonest explanation."""
    candidate = _claim(650.0, version="extract@3").as_candidate()
    assert candidate.source_ref.extractor_version == "extract@3"


def test_the_claim_is_the_single_authority_for_it() -> None:
    """Stamped from the claim rather than set twice, so the two cannot drift."""
    claim = FieldClaim(
        document_id="doc-a",
        field_name="nameplate_power",
        extractor_version="extract@3",
        value=650.0,
        source_tier=SourceTier.SYSTEM_OF_RECORD,
        source_ref=SourceRef(document_id="doc-a", extractor_version="extract@1"),
        confidence=0.9,
    )
    assert claim.provenance().extractor_version == "extract@3"


def test_a_claim_identity_encodes_the_grouping_key_without_repr() -> None:
    """A-50 closed enum `repr` on the HITL sort path; `_identity` still used it.
    Member names then leak into the reducer order: renaming `PowerSide.AC` would
    reorder claims without the data changing."""
    claim = _claim(650.0, condition=Condition(basis=MeasurementBasis.STC, side=PowerSide.AC))
    encoded_group = json.dumps(
        encode_value(claim.condition.grouping_key()),
        sort_keys=True,
        separators=(",", ":"),
    )
    assert _identity(claim)[3] == encoded_group
    assert "<" not in _identity(claim)[3]


def test_every_spelling_of_one_figure_is_one_answer() -> None:
    """`values_conflict` already treats 650 / 650.0 / Decimal("650") as one
    number. `_asserted` still `repr`s Decimal, so the reducer reports OPEN for a
    pair the detector calls equal."""
    claims = [
        _claim(650, doc="d0"),
        _claim(650.0, doc="d1"),
        _claim(Decimal("650"), doc="d2"),
        _claim(Decimal("650.0"), doc="d3"),
    ]
    assert [f.conflict_status for f in project(claims)] == [ConflictStatus.NONE]


def test_nested_numeric_spellings_are_one_answer() -> None:
    """Top-level fold is not enough: `rating_mva_by_cooling` stores the numbers
    as dict values, including Decimal from parsed tables."""
    projected = project(
        [
            _claim({"ONAN": 30, "ONAF": 40}, doc="d1", field="rating_mva_by_cooling"),
            _claim(
                {"ONAN": Decimal("30.0"), "ONAF": Decimal("40.0")},
                doc="d2",
                field="rating_mva_by_cooling",
            ),
        ]
    )
    assert [f.conflict_status for f in projected] == [ConflictStatus.NONE]


def test_reordered_certifications_are_one_answer() -> None:
    """D-17: order is typography for `list[str]` contract fields. SET_EQUAL in
    the detector is not enough if `project()` still stores OPEN."""
    projected = project(
        [
            _claim(["UL 1741", "IEC 61215"], doc="d1", field="certifications", unit=None),
            _claim(["IEC 61215", "UL 1741"], doc="d2", field="certifications", unit=None),
        ]
    )
    assert [f.conflict_status for f in projected] == [ConflictStatus.NONE]


def test_an_unqualified_certification_matches_a_dated_one_in_the_projection() -> None:
    """`_compare_sets` treats a missing edition as unknown, not different.
    Canonicalising years into `_asserted` made `project()` OPEN for a pair the
    detector calls no conflict - Open Items with no queue entry, on a CRITICAL
    field."""
    projected = project(
        [
            _claim(["IEC 61215"], doc="d1", field="certifications", unit=None),
            _claim(["IEC 61215:2021"], doc="d2", field="certifications", unit=None),
        ]
    )
    assert [f.conflict_status for f in projected] == [ConflictStatus.NONE]


def test_dated_edition_mismatch_is_still_open_in_the_projection() -> None:
    """The unqualified-vs-dated fold must not hide 2016 vs 2021."""
    projected = project(
        [
            _claim(["IEC 61215:2016"], doc="d1", field="certifications", unit=None),
            _claim(["IEC 61215:2021"], doc="d2", field="certifications", unit=None),
        ]
    )
    assert [f.conflict_status for f in projected] == [ConflictStatus.OPEN]


def test_cross_condition_queue_hits_mark_canonical_fields_open() -> None:
    """`project()` statuses inside a grouping_key. `comparison_pairs` still
    compares unstated vs stated. Copy those hits onto the matching
    CanonicalFields so `flags_for` / Open Items see D-1."""
    unstated = _claim(650.0, condition=Condition())
    stated = _claim(
        700.0,
        doc="doc-b",
        condition=Condition(basis=MeasurementBasis.STC),
        tier=SourceTier.WEB_SUPPLEMENT,
    )
    fields = project([unstated, stated])
    assert all(field.conflict_status is ConflictStatus.NONE for field in fields)
    pairs = comparison_pairs(
        [unstated.as_candidate(), stated.as_candidate()],
        field_name="nameplate_power",
    )
    assert pairs
    from procurement_agent.services.vertical_slice import stamp_queue_hits

    stamped = stamp_queue_hits(fields, pairs)
    assert {field.conflict_status for field in stamped} == {ConflictStatus.OPEN}
    for field in stamped:
        assert CellFlag.UNRESOLVED_CONFLICT in flags_for(field, confidence_threshold=0.8)


# --- D-16: a reviewer's decision is a claim, and it settles the group ----------
#
# A recorded decision was discarded by the next reducer run and a human claim
# reopened the conflict it settled. sql/06 recommended the human-as-claim
# convention and said it could not enforce it; FieldClaim now does.


def _decision(
    value: object,
    *,
    action: ResolutionAction = ResolutionAction.SELECT_VALUE,
    by: str = "procurement.lead",
    at: datetime = datetime(2026, 8, 1, tzinfo=UTC),
) -> Resolution:
    return Resolution(
        action=action,
        resolved_by=by,
        resolved_at=at,
        rationale="datasheet revision C supersedes the PO",
        value_after=value,
    )


def _human(
    value: object,
    *,
    doc: str = "contract",
    by: str = "procurement.lead",
    at: datetime = datetime(2026, 8, 1, tzinfo=UTC),
    tier: SourceTier = SourceTier.SYSTEM_OF_RECORD,
    action: ResolutionAction = ResolutionAction.SELECT_VALUE,
) -> FieldClaim:
    return _claim(
        value,
        doc=doc,
        tier=tier,
        version=f"{HUMAN_PREFIX}{by}",
        confidence=1.0,
        resolution=_decision(value, action=action, by=by, at=at),
    )


def test_a_human_claim_settles_a_disagreement() -> None:
    """The decision half of source authority. Two records disagree; a reviewer
    picks one; the stored field is RESOLVED and carries the decision."""
    projected = project(
        [_claim(650.0, doc="contract"), _claim(700.0, doc="datasheet"), _human(650.0)]
    )
    assert [f.conflict_status for f in projected] == [ConflictStatus.RESOLVED]
    assert projected[0].value == 650.0
    assert projected[0].resolution is not None
    assert projected[0].resolution.resolved_by == "procurement.lead"


def test_an_idempotent_rerun_keeps_the_decision() -> None:
    """Re-committing the identical, complete claim set must not turn a RESOLVED
    field back into an OPEN one."""
    store = _Store()
    claims = [_claim(650.0, doc="contract"), _claim(700.0, doc="datasheet"), _human(650.0)]
    first = commit_claims("nameplate_power", claims, writer=store)
    second = commit_claims("nameplate_power", claims, writer=store)
    assert first == second
    assert store.committed["nameplate_power"][0].conflict_status is ConflictStatus.RESOLVED
    assert store.committed["nameplate_power"][0].resolution is not None


def test_a_later_extraction_does_not_reopen_a_settled_group() -> None:
    """Reopening is a human action (REQUEST_MORE_WEB_SEARCH, task F.3), not a
    side effect of re-running an extractor with a new version."""
    projected = project(
        [
            _claim(650.0, doc="contract"),
            _claim(700.0, doc="datasheet"),
            _human(650.0),
            _claim(710.0, doc="datasheet", version="extract@2"),
        ]
    )
    assert projected[0].conflict_status is ConflictStatus.RESOLVED
    assert projected[0].value == 650.0


def test_the_latest_decision_wins() -> None:
    """A reopened conflict records a NEW decision. `resolved_at` is stored data,
    so the order is a function of the store and not of arrival."""
    earlier = _human(650.0, at=datetime(2026, 8, 1, tzinfo=UTC))
    later = _human(700.0, at=datetime(2026, 8, 9, tzinfo=UTC))
    forward = project([_claim(650.0, doc="contract"), earlier, later])
    backward = project([later, _claim(650.0, doc="contract"), earlier])
    assert forward[0].value == backward[0].value == 700.0
    assert forward[0].resolution is not None
    assert forward[0].resolution.resolved_at == later.resolution.resolved_at  # type: ignore[union-attr]


def test_two_decisions_by_one_reviewer_are_two_claims_not_a_contradiction() -> None:
    """Without `resolved_at` in the claim key the second decision collided with
    the first as 'one extractor, two answers' and raised ProposalError."""
    earlier = _human(650.0, at=datetime(2026, 8, 1, tzinfo=UTC))
    later = _human(700.0, at=datetime(2026, 8, 9, tzinfo=UTC))
    assert earlier.claim_key() != later.claim_key()
    assert len(canonical_claims([earlier, later])) == 2


def test_a_human_may_select_the_web_value() -> None:
    """The guard is against *autonomous* overwrite. A reviewer choosing the web
    candidate in the queue is FR-HITL-04's SELECT_VALUE."""
    store = _Store()
    commit_claims("nameplate_power", [_claim(650.0, doc="contract")], writer=store)
    committed = commit_claims(
        "nameplate_power",
        [
            _claim(650.0, doc="contract"),
            _claim(700.0, doc="web-1", tier=SourceTier.WEB_SUPPLEMENT),
            _human(700.0, doc="web-1", tier=SourceTier.WEB_SUPPLEMENT),
        ],
        writer=store,
    )
    assert committed[0].value == 700.0
    assert committed[0].source_tier is SourceTier.WEB_SUPPLEMENT
    assert committed[0].conflict_status is ConflictStatus.RESOLVED


def test_a_web_value_with_no_decision_behind_it_is_still_refused() -> None:
    """The exception is for a decision, not for a tier."""
    store = _Store()
    commit_claims("nameplate_power", [_claim(650.0, doc="contract")], writer=store)
    with pytest.raises(AutonomousOverwriteError):
        commit_claims(
            "nameplate_power",
            [_claim(700.0, doc="web-1", tier=SourceTier.WEB_SUPPLEMENT)],
            writer=store,
        )


def test_a_human_prefix_without_a_decision_is_rejected() -> None:
    """A reviewer's value with no recorded decision is a decision with nobody
    behind it (FR-HITL-06)."""
    with pytest.raises(ValidationError, match="must carry its Resolution"):
        _claim(650.0, version=f"{HUMAN_PREFIX}someone")


def test_a_decision_on_a_machine_claim_is_rejected() -> None:
    """A Resolution on an extract@1 claim is a person's name on a machine value."""
    with pytest.raises(ValidationError, match="must be a human claim"):
        _claim(650.0, version="extract@1", resolution=_decision(650.0))


@pytest.mark.parametrize(
    "action", [ResolutionAction.DEFER, ResolutionAction.REQUEST_MORE_WEB_SEARCH]
)
def test_an_action_that_asserts_no_value_cannot_be_a_claim(action: ResolutionAction) -> None:
    """DEFER and REQUEST_MORE_WEB_SEARCH are events against the conflict."""
    with pytest.raises(ValidationError, match="asserts no value"):
        _human(650.0, action=action)


def test_a_decision_recorded_against_a_different_value_is_rejected() -> None:
    """value_after and the claim's value must agree."""
    with pytest.raises(ValidationError, match="value_after must be the claim's value"):
        _claim(650.0, version=f"{HUMAN_PREFIX}procurement.lead", resolution=_decision(655.0))


def test_field_claim_still_forbids_an_undeclared_key() -> None:
    """Keep extra=forbid: a typo on an optional field must not vanish."""
    with pytest.raises(ValidationError):
        FieldClaim(
            document_id="doc-a",
            field_name="nameplate_power",
            extractor_version="extract@1",
            value=650.0,
            source_tier=SourceTier.SYSTEM_OF_RECORD,
            source_ref=SourceRef(document_id="doc-a"),
            confidence=0.9,
            verbatim_valeu="650",  # type: ignore[call-arg]
        )


def test_project_agrees_with_values_conflict_on_d2_tolerance_and_unit_aliases() -> None:
    """CRITICAL-2: `_status_for` used `values_conflict` only for `SET_EQUAL` and
    fell back to exact `_asserted` string/unit equality for every other rule.
    Claims agreeing within D-2's numeric band (`650.0 Wp` vs `650.4 Wp` on
    `nameplate_power` ±1.0 Wp) or D-5's unit aliases (`%/degC` vs `%/K` on
    `temp_coeff_pmax`) were marked `ConflictStatus.OPEN` by `project()` while
    `values_conflict` returned `conflicts=False`, creating unresolvable ghost
    OPEN fields with zero queue entries."""
    within_band = project(
        [
            _claim(650.0, doc="contract", unit="Wp", verbatim="650.0 Wp"),
            _claim(650.4, doc="datasheet", unit="Wp", verbatim="650.4 Wp"),
        ]
    )
    assert [f.conflict_status for f in within_band] == [ConflictStatus.NONE]

    aliased_units = project(
        [
            _claim(-0.29, doc="contract", field="temp_coeff_pmax", unit="%/degC"),
            _claim(-0.29, doc="datasheet", field="temp_coeff_pmax", unit="%/K"),
        ]
    )
    assert [f.conflict_status for f in aliased_units] == [ConflictStatus.NONE]

    # And within a single document/extractor, printing `%/degC` in a summary and
    # `%/K` in a table is one assertion, not a ProposalError.
    same_doc_aliases = canonical_claims(
        [
            _claim(-0.29, doc="datasheet", field="temp_coeff_pmax", unit="%/degC", page=1),
            _claim(-0.29, doc="datasheet", field="temp_coeff_pmax", unit="%/K", page=4),
        ]
    )
    assert len(same_doc_aliases) == 2


def test_missing_vs_populated_on_set_equal_field_is_still_open() -> None:
    """CRITICAL-2: on `SET_EQUAL` fields (`certifications`, `ul_listing`),
    `_status_for` delegated to `values_conflict`, which returns `conflicts=False`
    when either side is `None`. So `[None, ['UL 1741']]` returned `NONE` on a
    Tier A CRITICAL field while scalar fields returned `OPEN`."""
    projected = project(
        [
            _claim(None, doc="a-datasheet", field="certifications", unit=None),
            _claim(["UL 1741"], doc="b-datasheet", field="certifications", unit=None),
        ]
    )
    assert len(projected) == 1
    assert projected[0].value == ["UL 1741"]
    assert projected[0].conflict_status is ConflictStatus.OPEN


def test_claim_identity_distinguishes_none_from_empty_and_condition_notes() -> None:
    """HIGH-6: `_identity` folded `None` and `""` on `unit`/`verbatim_value` and
    dropped `condition.note`/`condition.derived`, causing distinct claims to
    collide in `canonical_claims` and making the surviving claim depend on
    arrival order."""
    c_none = _claim(650.0, doc="d1", verbatim=None)
    c_empty = _claim(650.0, doc="d1", verbatim="")
    assert len(canonical_claims([c_none, c_empty])) == 2

    c_note_a = _claim(
        650.0,
        doc="d1",
        condition=Condition(basis=MeasurementBasis.STC, note="note A"),
    )
    c_note_b = _claim(
        650.0,
        doc="d1",
        condition=Condition(basis=MeasurementBasis.STC, note="note B"),
    )
    assert len(canonical_claims([c_note_a, c_note_b])) == 2


def test_field_claim_rejects_non_finite_value_at_construction() -> None:
    """LOW-12: `FieldClaim.value` must reject NaN/Infinity at construction."""
    with pytest.raises(ValidationError, match="non-finite"):
        _claim(float("nan"))


def test_reduce_field_unifies_projection_conflict_detection_and_cross_condition_stamping() -> None:
    """Candidate 1: `reduce_field` returns both stamped `CanonicalField`s and
    `ConflictQueueEntry` rows in one call without manual `stamp_queue_hits`
    plumbing."""
    unstated = _claim(650.0, doc="doc-a", condition=Condition())
    stated = _claim(
        700.0,
        doc="doc-b",
        condition=Condition(basis=MeasurementBasis.STC),
        tier=SourceTier.WEB_SUPPLEMENT,
    )
    reduction = reduce_field(
        [unstated, stated],
        field_name="nameplate_power",
        supplier="Example Solar Ltd.",
        model="PV-SAN-650",
        nameplate=650.0,
        detected_at=datetime(2026, 8, 1, 9, 1, tzinfo=UTC),
    )
    assert len(reduction.fields) == 2
    assert {f.conflict_status for f in reduction.fields} == {ConflictStatus.OPEN}
    assert len(reduction.conflicts) == 1
    assert reduction.conflicts[0].field_name == "nameplate_power"


def test_reduce_field_entry_id_includes_component_identity() -> None:
    """MEDIUM-9: the same field/pair on two SKUs must not share an entry_id."""
    unstated = _claim(650.0, doc="doc-a", condition=Condition())
    stated = _claim(
        700.0,
        doc="doc-b",
        condition=Condition(basis=MeasurementBasis.STC),
        tier=SourceTier.WEB_SUPPLEMENT,
    )
    detected_at = datetime(2026, 8, 1, 9, 1, tzinfo=UTC)
    first = reduce_field(
        [unstated, stated],
        field_name="nameplate_power",
        supplier="Example Solar Ltd.",
        model="PV-SAN-650",
        nameplate=650.0,
        detected_at=detected_at,
    )
    second = reduce_field(
        [unstated, stated],
        field_name="nameplate_power",
        supplier="Other Solar Ltd.",
        model="PV-SAN-700",
        nameplate=700.0,
        detected_at=detected_at,
    )
    same = reduce_field(
        [unstated, stated],
        field_name="nameplate_power",
        supplier="Example Solar Ltd.",
        model="PV-SAN-650",
        nameplate=650.0,
        detected_at=detected_at,
    )
    assert first.conflicts[0].entry_id != second.conflicts[0].entry_id
    assert first.conflicts[0].entry_id == same.conflicts[0].entry_id
