"""Grouping candidate values must not depend on the order they arrive.

Issue #12. `comparable_with` is not transitive, so it cannot partition a
candidate set; `grouping_key()` can, because equality is an equivalence
relation. FR-OUT-06 requires composition to be a pure function of the store,
which fails the moment the conflict queue depends on ingest order.
"""

import itertools
import pathlib
import re
from enum import StrEnum
from typing import get_args

import pytest
from pydantic import ValidationError

from procurement_agent.schema import (
    CONDITION_DIMENSION_NAMES,
    Condition,
    ConditionDimensions,
    EfficiencyWeighting,
    MeasurementBasis,
    PowerSide,
    RteBoundary,
    StandardsRegime,
    WorkbookTab,
)
from procurement_agent.schema.field import _normalise_token

#: The dimensions these cases ask about. Every one of them is about the
#: relation itself - transitivity, order-dependence, whether a promoted dimension
#: gates at all - rather than about one parameter, so the unqualified set is the
#: right argument. Which dimensions gate which *field* is FN-2, and
#: `tests/test_condition_gate_scope.py` owns it.
ALL = CONDITION_DIMENSION_NAMES

#: The Sungrow SG350HX case: an EU sheet at 30 degC, a distributor page stating
#: no condition at all, and the CEC listing at 40 degC.
EU = Condition(temperature_c=30.0)
SILENT = Condition()
CEC = Condition(temperature_c=40.0)


def _first_fit_buckets(conditions: list[Condition]) -> list[list[Condition]]:
    """Bucket by pairwise comparability - the natural, and wrong, implementation."""
    buckets: list[list[Condition]] = []
    for candidate in conditions:
        for bucket in buckets:
            if all(candidate.comparable_with(member, dimensions=ALL) for member in bucket):
                bucket.append(candidate)
                break
        else:
            buckets.append([candidate])
    return buckets


def _key_buckets(conditions: list[Condition]) -> list[list[Condition]]:
    """Bucket by grouping key - transitive, so order cannot matter."""
    grouped: dict[tuple[object, ...], list[Condition]] = {}
    for candidate in conditions:
        grouped.setdefault(candidate.grouping_key(), []).append(candidate)
    return [grouped[key] for key in sorted(grouped, key=repr)]


def test_comparable_with_is_not_transitive() -> None:
    """Pinned deliberately: this is why `comparable_with` must not group."""
    assert EU.comparable_with(SILENT, dimensions=ALL)
    assert SILENT.comparable_with(CEC, dimensions=ALL)
    assert not EU.comparable_with(CEC, dimensions=ALL)


def test_pairwise_bucketing_is_order_dependent() -> None:
    """The defect, captured so a future refactor cannot quietly reintroduce it."""
    forward = _first_fit_buckets([EU, SILENT, CEC])
    reverse = _first_fit_buckets([CEC, SILENT, EU])
    assert [len(b) for b in forward] == [2, 1]
    assert [len(b) for b in reverse] == [2, 1]
    assert {id(c) for c in forward[0]} != {id(c) for c in reverse[0]}


def test_grouping_key_is_order_independent() -> None:
    """Every permutation of the same candidates yields the same partition."""
    partitions = {
        tuple(tuple(c.grouping_key() for c in bucket) for bucket in _key_buckets(list(order)))
        for order in itertools.permutations([EU, SILENT, CEC])
    }
    assert len(partitions) == 1


def test_grouping_key_separates_the_three_conditions() -> None:
    """Unstated conditions form their own group rather than bridging two others.

    `_key_buckets` is a local reimplementation, so this pins the *property* of
    `grouping_key`; `tests/test_comparison_pairs.py` pins the shipped
    `comparison_groups` against a literal partition."""
    assert len(_key_buckets([EU, SILENT, CEC])) == 3


def test_grouping_key_is_hashable_and_equal_for_equal_conditions() -> None:
    """Equal conditions share a key and unequal ones do not.

    The inequality half matters: with `grouping_key` replaced by `return ()` the
    equality assertions still held, and a constant key puts every candidate in
    one display group."""
    stc = Condition(basis=MeasurementBasis.STC).grouping_key()
    assert stc == Condition(basis=MeasurementBasis.STC).grouping_key()
    assert len({stc, Condition(basis=MeasurementBasis.STC).grouping_key()}) == 1
    assert stc != Condition(basis=MeasurementBasis.NOCT).grouping_key()
    assert stc != Condition().grouping_key()


def test_the_grouping_key_has_a_fixed_layout() -> None:
    """Pinned as a literal, because every other assertion compares one
    `grouping_key()` to another — self-consistency with no anchor, which is the
    same shape as the pair-orientation reversal that once kept 79 tests green.

    Reordering `ConditionDimensions.model_fields` changes what position a
    dimension occupies without changing any key-to-key comparison, and the
    display partition is repr-sorted on exactly that tuple."""
    assert tuple(ConditionDimensions.model_fields) == (
        "basis",
        "temperature_c",
        "side",
        "duration_h",
        "weighting",
        "standards_regime",
        "reference_temperature_c",
        "rte_boundary",
        "tap_position_pct",
        "base_mva",
    )
    assert Condition(basis=MeasurementBasis.STC).grouping_key() == (
        MeasurementBasis.STC,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )


def test_is_unstated_is_false_the_moment_any_dimension_is_known() -> None:
    """Only the all-`None` case was ever asserted, so `return True`,
    `return any(...)` and `return self.note is None` all passed. Any code
    branching on this would have treated every stated condition as unstated."""
    assert Condition().is_unstated()
    assert Condition(note="page 3").is_unstated(), "an annotation is not a dimension"
    for stated in (
        Condition(basis=MeasurementBasis.STC),
        Condition(temperature_c=30.0),
        Condition(side=PowerSide.DC),
        Condition(rte_boundary=RteBoundary.DC_ROUND_TRIP),
        Condition(tap_position_pct=0.0),
        Condition(base_mva=10.0),
    ):
        assert not stated.is_unstated(), stated


def test_grouping_key_ignores_note() -> None:
    """Free text is provenance for a human, not a comparison dimension."""
    a = Condition(temperature_c=40.0, note="page 3 table")
    b = Condition(temperature_c=40.0, note="summary block")
    assert a.grouping_key() == b.grouping_key()


def test_grouping_key_covers_every_comparable_dimension() -> None:
    """A new dimension must participate in grouping without anyone remembering.

    Asserted against `Condition`'s own fields, not against `ConditionDimensions`.
    The obvious form - comparing `len(grouping_key())` to
    `len(ConditionDimensions.model_fields)` - is a tautology, since the key is
    built by iterating exactly that. It passes even when a genuinely comparable
    dimension is added to `Condition`, which is the case it exists to catch.
    """
    annotations = set(Condition.model_fields) - set(ConditionDimensions.model_fields)
    assert annotations == {"note", "derived"}, (
        "A field added to Condition rather than ConditionDimensions is excluded "
        "from grouping and comparison. If it is a measurement condition it belongs "
        "on ConditionDimensions; if it is an annotation, list it here."
    )


def test_non_finite_conditions_are_rejected() -> None:
    """NaN would break reflexivity: NaN != NaN, so two identically-conditioned
    values would never group together, and pydantic serialises NaN to JSON null
    so the same record would group differently after a store round-trip."""
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            Condition(temperature_c=bad)


def test_grouping_is_stable_across_a_store_round_trip() -> None:
    """FR-OUT-06 purity: persisting and reloading must not move a value's group."""
    original = Condition(basis=MeasurementBasis.STC, temperature_c=25.0, base_mva=10.0)
    revived = Condition.model_validate_json(original.model_dump_json())
    assert revived.grouping_key() == original.grouping_key()


def test_vocabulary_case_does_not_split_a_group() -> None:
    """Under exact-key grouping an unnormalised variant does not raise a false
    conflict - it silently suppresses the comparison, which cannot be reviewed."""
    upper = Condition(basis="STC").grouping_key()  # type: ignore[arg-type]
    padded = Condition(basis=" stc ").grouping_key()  # type: ignore[arg-type]
    assert upper == padded
    upper_side = Condition(side="DC").grouping_key()  # type: ignore[arg-type]
    assert upper_side == Condition(side=PowerSide.DC).grouping_key()


def test_a_ptc_rating_has_an_honest_encoding() -> None:
    """D-8 makes the CEC list the authority for PV and PTC is its power column;
    clarifications gates `PTC / STC` to 87-96% and tasks.md B.5 makes it a hard
    gate. Without a `ptc` member the only encodings were leaving `basis` unset —
    which made a 509.9 W PTC figure comparable with a 550 W STC one, a ~10% false
    conflict on every CEC-sourced row — or `note`, which comparison ignores."""
    ptc = Condition(basis=MeasurementBasis.PTC)
    stc = Condition(basis=MeasurementBasis.STC)
    assert not ptc.comparable_with(stc, dimensions=ALL)
    assert Condition().comparable_with(stc, dimensions=ALL), (
        "an unrecorded basis is still unknown, not a rating"
    )


def test_printed_synonyms_fold_to_their_member() -> None:
    """Closing a vocabulary turns an unlisted spelling into a dropped document,
    and these are the forms real sources print: this repo's own text writes
    "ANSI/IEEE (C57.12.00 5.4)", and Fronius- and SMA-family sheets print
    "Euro efficiency"."""
    for printed in ("ANSI", "ANSI/IEEE", "ieee/ansi"):
        assert Condition(standards_regime=printed).standards_regime is StandardsRegime.IEEE  # type: ignore[arg-type]
    for printed in ("Euro", "EU", "european"):
        assert Condition(weighting=printed).weighting is EfficiencyWeighting.EUROPEAN  # type: ignore[arg-type]


def test_the_spellings_the_alias_comment_cites_are_tested_literally() -> None:
    """A-70. The test above and the alias table both justify themselves with
    two printed forms - "Euro efficiency" and "ANSI/IEEE (C57.12.00 5.4)" - and
    neither test asserted either one, which is how both stayed rejected.

    One is now an alias. The other is pinned as refused, with the reason: a
    parenthetical-stripping rule that accepted it would also accept
    "IEC (draft)", where the parenthetical is the meaning. If an extractor
    change makes it safe to accept, delete that half of this test alongside it.
    """
    for printed in ("Euro efficiency", "European efficiency", "EURO EFFICIENCY", "euro-efficiency"):
        assert (
            Condition(weighting=printed).weighting  # type: ignore[arg-type]
            is EfficiencyWeighting.EUROPEAN
        ), printed
    for citation_bearing in ("ANSI/IEEE (C57.12.00 5.4)", "IEC (draft)", "IEC (superseded)"):
        with pytest.raises(ValidationError):
            Condition(standards_regime=citation_bearing)  # type: ignore[arg-type]


def test_the_printed_spacing_of_a_member_is_not_a_validation_failure() -> None:
    """No datasheet prints `full_power`; they print "Full power". Folding hyphens
    but not spaces split the difference in the direction that drops documents —
    the whole argument for the alias map is that rejecting a real spelling is
    worse than folding a synonym.

    A slash separates alternatives rather than words, so whitespace around it
    collapses away first, or `ANSI / IEEE` becomes `ansi_/_ieee` and misses its
    alias."""
    assert Condition(basis="Full Power").basis is MeasurementBasis.FULL_POWER  # type: ignore[arg-type]
    assert (
        Condition(rte_boundary="DC DC terminals").rte_boundary  # type: ignore[arg-type]
        is RteBoundary.DC_DC_TERMINALS
    )
    for printed in ("ANSI / IEEE", "ANSI\xa0/\xa0IEEE", "ANSI/IEEE", "ansi  /  ieee"):
        assert Condition(standards_regime=printed).standards_regime is StandardsRegime.IEEE  # type: ignore[arg-type]


def test_regimes_derived_from_a_standard_are_not_silently_mapped() -> None:
    """`gb`, `is` and `csa` name standards derived from a regime, not the regime.
    Guessing which would be a technical claim nobody here has verified, and a
    wrong guess picks the wrong multi-cooling rating silently."""
    for unmapped in ("gb", "is", "csa"):
        with pytest.raises(ValidationError):
            Condition(standards_regime=unmapped)  # type: ignore[arg-type]


def test_extraction_artefacts_do_not_split_a_group() -> None:
    """`str.strip` removes NBSP but none of the zero-width characters PDF and
    XLSX extraction leaves behind, so a byte-order mark alone was a hard
    validation failure on an otherwise clean document."""
    canonical = Condition(basis=MeasurementBasis.STC).grouping_key()
    for artefact in ("﻿stc", "stc​", "s\xadtc", "\xa0STC\xa0", "ｓｔｃ"):
        assert Condition(basis=artefact).grouping_key() == canonical, artefact  # type: ignore[arg-type]


def test_bytes_are_rejected_rather_than_half_normalised() -> None:
    """pydantic decodes bytes for a str field, so `b"stc"` validated while
    `b"STC"` raised — the same input, case-dependent, on the one path this
    validator exists to make case-insensitive."""
    for raw in (b"stc", b"STC"):
        with pytest.raises(ValidationError):
            Condition(basis=raw)  # type: ignore[arg-type]


def test_an_enum_member_is_never_case_folded() -> None:
    """Folding members works only by accident today, because all five
    vocabularies happen to be lowercase — so asserting it through `Condition`
    alone is vacuous, and a mutant that folds members survives it.

    `WorkbookTab.BESS == "BESS"` shows how easily lowercase stops being true, so
    the invariant is checked against a mixed-case member directly: a member goes
    through untouched, and only text is folded."""
    for member in MeasurementBasis:
        assert Condition(basis=member).basis is member
    assert _normalise_token(WorkbookTab.BESS) is WorkbookTab.BESS
    assert _normalise_token(WorkbookTab.BESS.value) == "bess"


def test_base_mva_is_a_grouping_dimension_not_a_note() -> None:
    """IEEE refers %Z to the ONAN base and IEC to the top rating, so two figures
    on different bases differ by 1.25-1.67x - far beyond the +/-7.5% tolerance.
    Left in `note` they would have grouped together and been compared."""
    assert Condition(base_mva=10.0).grouping_key() != Condition(base_mva=12.5).grouping_key()


def test_derived_does_not_split_a_group() -> None:
    """A defaulted STC value and a stated STC value are the same measurement."""
    stated = Condition(basis=MeasurementBasis.STC)
    defaulted = Condition(basis=MeasurementBasis.STC, derived=frozenset({"basis"}))
    assert stated.grouping_key() == defaulted.grouping_key()
    # `derived` is excluded from the key by living on the subclass, so the check
    # that means something is that it survives a store round-trip - echoing the
    # constructor kwarg back only tests pydantic.
    assert Condition.model_validate_json(defaulted.model_dump_json()).derived == frozenset(
        {"basis"}
    )


def test_an_unrecognised_vocabulary_token_is_rejected() -> None:
    """Issue #16. The frozen contract's own rule: "Extraction returning a value
    outside the set is a validation failure, not a silent pass-through." With a
    bare `str` it was exactly that pass-through — and under exact-key grouping a
    junk token does not raise a false conflict, it silently forms its own group
    and suppresses the comparison, which nothing surfaces."""
    for field, junk in [
        ("basis", "not_a_real_basis"),
        ("side", "middle"),
        ("weighting", "japanese"),
        ("rte_boundary", "somewhere_else"),
        ("standards_regime", "din"),
    ]:
        with pytest.raises(ValidationError):
            Condition(**{field: junk})  # type: ignore[arg-type]


def test_vocabulary_normalisation_runs_before_enum_coercion() -> None:
    """`"  STC "` has to fold to `stc` and *then* coerce, or closing the
    vocabulary would break every datasheet that prints conditions in caps."""
    # Raw strings on purpose: this is the coercion path, so the arguments are
    # deliberately not enum members.
    assert Condition(basis="  STC ").basis is MeasurementBasis.STC  # type: ignore[arg-type]
    assert Condition(side="DC").side is PowerSide.DC  # type: ignore[arg-type]
    weighting = Condition(weighting=" European").weighting  # type: ignore[arg-type]
    assert weighting is EfficiencyWeighting.EUROPEAN
    regime = Condition(standards_regime="IEC").standards_regime  # type: ignore[arg-type]
    assert regime is StandardsRegime.IEC


def test_an_undated_sat_is_legal_and_never_aliased() -> None:
    """Closing the vocabulary turns a missing member into a validation failure, so
    a datasheet printing "SAT" with no epoch needs one of its own (decision 6).
    It must not alias onto either dated member: `basis` is a grouping dimension,
    so aliasing would silently merge one-month and three-month measurements."""
    undated = Condition(basis="SAT")  # type: ignore[arg-type]
    assert undated.basis is MeasurementBasis.SAT, "the printed form has to reach the member"
    assert undated.grouping_key() != Condition(basis=MeasurementBasis.SAT_1MO).grouping_key()
    assert undated.grouping_key() != Condition(basis=MeasurementBasis.SAT_3MO).grouping_key()


def _conditions_table() -> list[tuple[str, str]]:
    """`(family, required-fields cell)` for every row of the contract's Conditions
    table.

    The row filter is `lstrip().startswith("|")` with a pipe count, not
    `startswith("| ")`: a row written without the space after the leading pipe
    renders identically in GFM, and the stricter filter silently dropped it from
    every check below — a row nothing validates is exactly the hiding place the
    `note` routing used.
    """
    contract = pathlib.Path(__file__).parent.parent / (
        "specs/001-procurement-agent/contracts/canonical-parameters.md"
    )
    table = contract.read_text(encoding="utf-8").split("## Conditions", 1)[1]
    rows = []
    for line in table.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or stripped.count("|") < 4:
            continue
        cells = stripped.split("|")
        if set(cells[1].strip()) <= {"-", ":"}:  # the header separator
            continue
        rows.append((cells[1].strip(), cells[2]))
    assert len(rows) > 5, "the Conditions table did not parse; the check below would be vacuous"
    return rows


def _vocabulary_of(field_name: str) -> set[str]:
    """The closed set behind a `ConditionDimensions` field, from its annotation.

    Read from the type rather than a hand-kept mapping, so a new closed
    vocabulary is covered without anyone registering it.
    """
    annotation = ConditionDimensions.model_fields[field_name].annotation
    enums = [
        arg for arg in get_args(annotation) if isinstance(arg, type) and issubclass(arg, StrEnum)
    ]
    assert enums, f"the table gives `{field_name}` a vocabulary but its type is open"
    return {member.value for member in enums[0]}


def test_the_contract_conditions_table_names_only_real_fields() -> None:
    """Every backticked name in the table is a field that actually gates
    comparison, or — inside an `in {...}` clause — a member of that field's closed
    vocabulary. Otherwise the contract describes a rule the code does not
    implement, which is how the `note` routing survived in the first place."""
    fields: set[str] = set()
    for _family, cell in _conditions_table():
        required = cell
        for head, listed in re.findall(r"`([a-z_]+)`\s*∈\s*\{([^}]*)\}", required):
            named = set(re.findall(r"`([a-z_0-9]+)`", listed))
            vocabulary = _vocabulary_of(head)
            assert named <= vocabulary, f"`{head}` in the table admits {named - vocabulary}"
            fields.add(head)
            required = required.replace(f"{{{listed}}}", "")
        fields |= set(re.findall(r"`([a-z_]+)`", required))

    unknown = fields - set(ConditionDimensions.model_fields)
    assert not unknown, f"the Conditions table names {unknown}, which gate nothing"
    assert {"rte_boundary", "tap_position_pct", "base_mva"} <= fields, (
        "the three dimensions promoted out of `note` must appear in the table"
    )


def test_the_table_accounts_for_every_vocabulary_member() -> None:
    """The reverse direction, which the forward check cannot see: a member added
    to an enum and never written into the contract is invisible to a reader
    diffing the two, and by the repo's own rule 1 the contract is what governs.

    `sat` shipped in exactly that state — in `MeasurementBasis`, absent from the
    table, while open-decisions claimed it had been applied to both."""
    for field_name in ("basis", "side", "weighting", "standards_regime", "rte_boundary"):
        listed: set[str] = set()
        for _family, cell in _conditions_table():
            for head, members in re.findall(r"`([a-z_]+)`\s*∈\s*\{([^}]*)\}", cell):
                if head == field_name:
                    listed |= set(re.findall(r"`([a-z_0-9]+)`", members))
        missing = _vocabulary_of(field_name) - listed
        assert not missing, f"`{field_name}` has members no row of the table names: {missing}"


def test_no_two_families_claim_the_same_basis_token() -> None:
    """`basis` is one field across all eight categories, so the per-family sets
    are documentation rather than enforcement — but a token appearing in two
    families would mean two different measurements sharing one value, which is
    the merge `bol` vs `eol` exists to prevent. Disjointness is the part that can
    be enforced, and it is what stops a PV row quietly gaining `eol`."""
    per_family: dict[str, set[str]] = {}
    for family, cell in _conditions_table():
        for head, members in re.findall(r"`([a-z_]+)`\s*∈\s*\{([^}]*)\}", cell):
            if head == "basis":
                per_family[family] = set(re.findall(r"`([a-z_0-9]+)`", members))
    assert len(per_family) >= 3, "expected several families to declare a basis vocabulary"
    for (one, first), (other, second) in itertools.combinations(per_family.items(), 2):
        assert not first & second, f"{one} and {other} both claim {first & second}"


def test_the_table_routes_no_dimension_through_prose() -> None:
    """The original defect was prose — "plus boundary in `note`" — not a wrong
    token, so a check that only validates backticked names cannot see it.

    Two rules: the required-fields cell may not mention `note` at all, and any
    snake_case name in it must be backticked, so a dimension cannot enter as
    running text the way the RTE boundary and the transformer tap did."""
    for family, cell in _conditions_table():
        assert "note" not in cell.casefold(), (
            f"{family} routes a condition through `note`, which comparison ignores"
        )
        bare = re.sub(r"`[^`]*`", "", cell)
        assert not re.search(r"\b[a-z]+_[a-z_]+\b", bare), (
            f"{family} names a dimension outside backticks: {bare.strip()!r}"
        )


def test_cycle_life_names_tokens_not_a_percentage() -> None:
    """The one place the table wrote a `basis` vocabulary as prose — "EOL SOH
    threshold (60/70/80%)" — where every other row writes tokens. Prose backticks
    no member, so the vocabulary check passes it vacuously."""
    cycle_life = next(cell for family, cell in _conditions_table() if "cycle life" in family)
    assert re.search(r"`soh_60`.*`soh_70`.*`soh_80`", cycle_life), (
        "cycle life must name its `basis` vocabulary as tokens, not as a percentage"
    )


def test_rte_boundary_gates_comparison() -> None:
    """Four distinct boundaries are all called "round-trip efficiency", worth 2-7
    percentage points. The contract routed this through `note`, which comparison
    ignores, so CATL's 95% DC-DC figure compared as like-for-like with Tesla's
    91% AC-AC-including-auxiliaries one and fabricated a 4 pp conflict."""
    dc = Condition(duration_h=4.0, rte_boundary=RteBoundary.DC_DC_TERMINALS)
    ac = Condition(duration_h=4.0, rte_boundary=RteBoundary.AC_AC_MV_INCL_AUX)
    assert not dc.comparable_with(ac, dimensions=ALL)
    assert dc.grouping_key() != ac.grouping_key()


def test_two_spellings_of_one_boundary_still_compare() -> None:
    """The regression that closing this vocabulary prevents. As free text,
    `AC-AC at MV, incl. aux` and `ac-ac_mv_incl_aux` were two groups that never
    met — so two extractions of the *same* Tesla Megablock boundary stopped
    comparing, which is strictly worse than the `note` routing it replaced: that
    at least compared them. A dismissible false conflict had become an
    unreviewable non-comparison."""
    spellings = ["AC-AC-MV-INCL-AUX", " ac_ac_mv_incl_aux ", "Ac_Ac-Mv_Incl-Aux"]
    keys = {Condition(rte_boundary=text).grouping_key() for text in spellings}  # type: ignore[arg-type]
    assert len(keys) == 1
    assert keys == {Condition(rte_boundary=RteBoundary.AC_AC_MV_INCL_AUX).grouping_key()}


def test_a_boundary_outside_the_four_is_rejected() -> None:
    """clarifications.md enumerates exactly four. "No boundary stated" is None,
    not a fifth member: absent is unknown, not a distinct measurement."""
    with pytest.raises(ValidationError):
        Condition(rte_boundary="somewhere_else")  # type: ignore[arg-type]
    assert Condition(rte_boundary=None).rte_boundary is None


def test_tap_position_gates_comparison() -> None:
    """Nominal-tap and +5%-tap impedance are not the same measurement, and both
    the comparison and the display partition have to know it."""
    nominal = Condition(tap_position_pct=0.0)
    raised = Condition(tap_position_pct=5.0)
    assert not nominal.comparable_with(raised, dimensions=ALL)
    assert nominal.grouping_key() != raised.grouping_key()


def test_tap_position_is_a_number_so_spelling_cannot_split_it() -> None:
    """`nominal`, `Nominal Tap` and `principal tap` are one measurement. As free
    text they were three groups; as a percentage deviation there is one value."""
    assert (
        Condition(tap_position_pct=0.0).grouping_key()
        == Condition(tap_position_pct=-0.0).grouping_key()
    )
    with pytest.raises(ValidationError):
        Condition(tap_position_pct=float("nan"))
