"""FR-HITL-06's two invariants, over every route that reaches a `CanonicalField`.

`tests/test_schema_invariants.py` covers the constructor and plain assignment.
This file covers the routes that skip validation entirely, and the one a
validator structurally cannot see.

The invariants:

  1. `conflict_status is RESOLVED` implies `resolution is not None` - a decision
     with no record of who made it. **Since D-18 this is structural**: RESOLVED
     is derived from `resolution` being present and is not stored, so no route -
     validated, unvalidated, or a raw `__dict__` write - can produce the
     forbidden state. The tests below assert exactly that, on every route that
     used to need its own override: `model_construct`, pickle, `copy.copy`,
     `copy.deepcopy`, `model_copy(deep=True)` and `__dict__` itself.
  2. A recorded `Resolution` is never replaced or cleared. Every validator passes
     a swap, because the resulting state is perfectly legal; only a check that
     can see the *transition* catches it. FR-HITL-06: "logged immutably". This
     one is still a guard, in `__setattr__` and `evolve`.
"""

from __future__ import annotations

import copy
import pickle
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from procurement_agent.schema import (
    CanonicalField,
    ConflictStatus,
    Resolution,
    ResolutionAction,
    SourceRef,
    SourceTier,
    UnresolvedStatus,
)


def _resolution(resolved_by: str = "procurement.lead", value_after: object = 650) -> Resolution:
    return Resolution(
        action=ResolutionAction.SELECT_VALUE,
        resolved_by=resolved_by,
        resolved_at=datetime(2026, 1, 1, tzinfo=UTC),
        rationale="datasheet revision C supersedes the PO",
        value_after=value_after,
    )


def _field(**overrides: object) -> CanonicalField:
    base: dict[str, object] = {
        "value": 650,
        "source_tier": SourceTier.SYSTEM_OF_RECORD,
        "source_ref": SourceRef(document_id="doc-1"),
        "confidence": 0.9,
    }
    base.update(overrides)
    return CanonicalField(**base)  # type: ignore[arg-type]


def _resolved() -> CanonicalField:
    return _field().evolve(conflict_status=ConflictStatus.RESOLVED, resolution=_resolution())


def _poked() -> CanonicalField:
    """A field whose `__dict__` was written with the value that used to poison it.

    Before D-18 this manufactured the forbidden state; it is kept as the input
    to the route tests so they show the write is now inert rather than merely
    guarded. `conflict_status` is a computed property, and a class data
    descriptor wins over an instance `__dict__` entry, so the poke has no
    effect on what the field reports.
    """
    field = _field()
    field.__dict__["conflict_status"] = ConflictStatus.RESOLVED
    return field


# --- invariant 1: the forbidden state has no representation --------------------


def test_the_forbidden_state_cannot_be_asked_for() -> None:
    """The one place RESOLVED-with-no-resolution can still be *requested* is the
    door, and the door refuses it - constructor, `evolve`, `model_construct`."""
    with pytest.raises(ValueError, match="must carry its Resolution"):
        _field(conflict_status=ConflictStatus.RESOLVED)
    with pytest.raises(ValueError, match="must carry its Resolution"):
        _field().evolve(conflict_status=ConflictStatus.RESOLVED)
    with pytest.raises(ValueError, match="must carry its Resolution"):
        CanonicalField.model_construct(
            value=650,
            source_tier=SourceTier.SYSTEM_OF_RECORD,
            source_ref=SourceRef(document_id="doc-1"),
            confidence=0.9,
            conflict_status=ConflictStatus.RESOLVED,
            resolution=None,
        )


def test_resolved_is_not_a_storable_state() -> None:
    """The stored field is the *unresolved* state. RESOLVED is not a member, so
    the derivation has no back door."""
    assert ConflictStatus.RESOLVED.value not in {member.value for member in UnresolvedStatus}
    with pytest.raises(ValueError):
        _field(unresolved_status=ConflictStatus.RESOLVED)


def test_evolve_cannot_move_a_resolved_field_off_resolved() -> None:
    """A recorded Resolution is append-only, so the derived status is too.

    `evolve(conflict_status=OPEN)` on a resolved field used to rewrite the stored
    status while leaving `resolution` in place. The setter already refused this;
    evolve must refuse it as well.
    """
    field = _resolved()
    with pytest.raises(ValueError, match="append-only"):
        field.evolve(conflict_status=ConflictStatus.OPEN)
    assert field.conflict_status is ConflictStatus.RESOLVED
    assert field.resolution is not None


def test_model_construct_maps_the_contracts_name_rather_than_dropping_it() -> None:
    """Stock `model_construct` ignores a keyword that is not a field. Dropping
    `conflict_status` silently would downgrade OPEN to NONE on an audit path."""
    field = CanonicalField.model_construct(
        value=650,
        source_tier=SourceTier.SYSTEM_OF_RECORD,
        source_ref=SourceRef(document_id="doc-1"),
        confidence=0.9,
        conflict_status=ConflictStatus.OPEN,
    )
    assert field.conflict_status is ConflictStatus.OPEN
    resolved = CanonicalField.model_construct(
        value=650,
        source_tier=SourceTier.SYSTEM_OF_RECORD,
        source_ref=SourceRef(document_id="doc-1"),
        confidence=0.9,
        conflict_status=ConflictStatus.RESOLVED,
        resolution=_resolution(),
    )
    assert resolved.conflict_status is ConflictStatus.RESOLVED


def test_a_dict_write_cannot_fabricate_a_resolved_field() -> None:
    """The route that used to be documented as open, now closed by
    construction: writing `__dict__` was the one primitive no override could
    defend. It still runs; it no longer means anything, because RESOLVED is
    not stored."""
    poked = _poked()
    assert poked.conflict_status is ConflictStatus.NONE
    assert poked.resolution is None

    # `object.__setattr__` is not the same write as `__dict__[...]`: it still
    # dispatches to the class's data descriptor, which is now the property
    # setter, so this spelling is refused outright rather than ignored.
    by_object_setattr = _field()
    with pytest.raises(ValueError, match="must carry its Resolution"):
        object.__setattr__(by_object_setattr, "conflict_status", ConflictStatus.RESOLVED)
    assert by_object_setattr.conflict_status is ConflictStatus.NONE


@pytest.mark.parametrize(
    "route",
    [
        pytest.param(lambda f: pickle.loads(pickle.dumps(f)), id="pickle"),
        pytest.param(copy.copy, id="copy"),
        pytest.param(copy.deepcopy, id="deepcopy"),
        pytest.param(lambda f: f.model_copy(deep=True), id="model_copy-deep"),
        pytest.param(lambda f: f.model_copy(), id="model_copy-shallow"),
    ],
)
def test_no_copy_or_boundary_route_yields_the_forbidden_state(
    route: Callable[[CanonicalField], CanonicalField],
) -> None:
    """Every route that once needed its own override, asserted the same way:
    what comes out is consistent, whether the input was poked or not."""
    for field in (_poked(), _field(), _resolved()):
        out = route(field)
        assert (out.conflict_status is ConflictStatus.RESOLVED) == (out.resolution is not None)
        assert out.conflict_status is field.conflict_status
        assert out.resolution == field.resolution


def test_the_wire_shape_is_unchanged() -> None:
    """The TRS's eight keys plus `condition`, with `conflict_status` present and
    the stored `unresolved_status` absent - and the round trip preserves both
    an unresolved state and a resolved one."""
    for field in (_field(conflict_status=ConflictStatus.OPEN), _resolved()):
        dumped = field.model_dump(mode="json")
        assert "conflict_status" in dumped and "unresolved_status" not in dumped
        assert CanonicalField.model_validate(dumped).conflict_status is field.conflict_status


def test_a_resolved_field_cannot_be_moved_off_resolved() -> None:
    """Resolutions are append-only, so the derived status is too."""
    field = _resolved()
    with pytest.raises(ValueError, match="append-only"):
        field.conflict_status = ConflictStatus.OPEN
    assert field.conflict_status is ConflictStatus.RESOLVED


# --- invariant 2: a recorded decision is never overwritten ---------------------


def test_assignment_cannot_replace_a_recorded_resolution() -> None:
    """Every validator passes this: the resulting state is legal. Only a check
    that sees the transition catches it, which is why it lives in `__setattr__`.
    """
    field = _resolved()
    with pytest.raises(ValueError, match="cannot be replaced or cleared"):
        field.resolution = _resolution(resolved_by="someone.else", value_after=999)
    assert field.resolution is not None
    assert field.resolution.resolved_by == "procurement.lead"


def test_evolve_cannot_replace_a_recorded_resolution() -> None:
    """`evolve` merges into a snapshot and revalidates, so it too passes every
    validator while swapping one reviewer's decision for another's."""
    field = _resolved()
    with pytest.raises(ValueError, match="cannot be replaced or cleared"):
        field.evolve(resolution=_resolution(resolved_by="someone.else", value_after=999))
    assert field.resolution is not None
    assert field.resolution.resolved_by == "procurement.lead"


def test_evolve_cannot_clear_a_recorded_resolution() -> None:
    field = _resolved()
    with pytest.raises(ValueError):
        field.evolve(resolution=None)
    assert field.resolution is not None


def test_attaching_a_first_resolution_is_still_allowed() -> None:
    """The rule is append-only, not read-only. The legitimate transition - a
    field that has no resolution gaining one - must still work, or the guard has
    broken the pipeline it was meant to protect."""
    field = _field()
    field.resolution = _resolution()
    field.conflict_status = ConflictStatus.RESOLVED
    assert field.conflict_status is ConflictStatus.RESOLVED

    both_at_once = _field().evolve(
        conflict_status=ConflictStatus.RESOLVED, resolution=_resolution()
    )
    assert both_at_once.resolution is not None


def test_reassigning_an_equal_resolution_is_not_an_error() -> None:
    """An idempotent replay - the same decision written twice by a retried
    worker - is not tampering, and making it raise would push callers into
    checking-before-writing, which races."""
    field = _resolved()
    field.resolution = _resolution()
    assert field.evolve(resolution=_resolution()).resolution == _resolution()


# --- invariant 2, continued: replay and coercion ---------------------------------


def test_replaying_an_equal_resolution_from_its_serialised_form_is_not_an_error() -> None:
    """The same promise as the test above, at the boundary the class is built for.

    A worker replaying a decision read back from the store holds a `dict`, not a
    `Resolution`. Both routes compared the incoming value *before* pydantic
    coerced it, so `dict != Resolution` was true for two spellings of one
    decision and the immutability error fired on an idempotent replay.
    """
    field = _resolved()
    field.resolution = _resolution()
    # `Any`: a store row arrives untyped, which is the whole point of the case.
    serialised: Any = _resolution().model_dump()

    field.resolution = serialised
    assert field.resolution == _resolution()
    assert field.evolve(resolution=serialised).resolution == _resolution()


def test_a_different_resolution_in_dict_form_is_still_refused() -> None:
    """Coercing before comparing must widen *equality*, not the rule. A second
    reviewer's decision arriving as a dict is still an overwrite."""
    field = _resolved()
    field.resolution = _resolution()
    someone_else: Any = _resolution(resolved_by="someone.else").model_dump()

    with pytest.raises(ValueError, match="cannot be replaced"):
        field.resolution = someone_else
    with pytest.raises(ValueError, match="cannot be replaced"):
        field.evolve(resolution=someone_else)


def test_an_uncoercible_resolution_is_still_refused() -> None:
    """A value that is not a Resolution at all must not slip through the coercion
    attempt as 'equal'. It compares unequal and raises, as before."""
    field = _resolved()
    field.resolution = _resolution()
    not_a_resolution: Any = {"action": "not-an-action"}

    with pytest.raises(ValueError, match="cannot be replaced"):
        field.resolution = not_a_resolution


def test_a_dict_write_can_still_erase_a_decision_and_that_is_recorded() -> None:
    """What a raw `__dict__` write can still do: clear `resolution`, which
    un-resolves the field consistently. No Python object can defend that
    primitive; it is recorded here so the residual is a fact rather than an
    oversight, and so that it can no longer *fabricate* a decision is the line
    that moved."""
    cleared = _resolved()
    cleared.__dict__["resolution"] = None
    assert cleared.resolution is None
    assert cleared.conflict_status is not ConflictStatus.RESOLVED


def test_resolving_an_open_field_normalizes_unresolved_status_and_round_trips() -> None:
    """D-18: a resolved field has a single representation regardless of whether
    it was constructed resolved, evolved from OPEN, or mutated via assignment.
    Leaving `unresolved_status=OPEN` behind on an evolved field broke round-trip
    equality (`model_validate(model_dump()) != self`) and changed `encode_value`."""
    from procurement_agent.schema.encoding import encode_value

    open_field = _field(conflict_status=ConflictStatus.OPEN)
    assert open_field.unresolved_status is UnresolvedStatus.OPEN

    via_evolve_both = open_field.evolve(
        conflict_status=ConflictStatus.RESOLVED,
        resolution=_resolution(),
    )
    via_evolve_res = open_field.evolve(resolution=_resolution())
    via_assign = _field(conflict_status=ConflictStatus.OPEN)
    via_assign.resolution = _resolution()
    via_assign.conflict_status = ConflictStatus.RESOLVED

    round_tripped = CanonicalField.model_validate(via_evolve_both.model_dump(mode="json"))
    for candidate in (via_evolve_both, via_evolve_res, via_assign):
        assert candidate.unresolved_status is UnresolvedStatus.NONE
        assert candidate == round_tripped
        assert encode_value(candidate) == encode_value(round_tripped)


def test_resolution_rejects_non_finite_values_at_construction() -> None:
    """`Resolution.value_before` and `value_after` occupy the same polymorphic
    value slot as `CanonicalField.value` and must reject NaN/Infinity at the
    boundary rather than crashing `encode_value` at composition time."""
    with pytest.raises(ValueError, match="non-finite"):
        Resolution(
            action=ResolutionAction.SELECT_VALUE,
            resolved_by="procurement.lead",
            resolved_at=datetime(2026, 1, 1, tzinfo=UTC),
            rationale="invalid before",
            value_before=float("nan"),
            value_after=650.0,
        )
    with pytest.raises(ValueError, match="non-finite"):
        Resolution(
            action=ResolutionAction.SELECT_VALUE,
            resolved_by="procurement.lead",
            resolved_at=datetime(2026, 1, 1, tzinfo=UTC),
            rationale="invalid after",
            value_before=650.0,
            value_after=float("inf"),
        )
