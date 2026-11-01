"""The FieldProfile catalog is a view, not a second table."""

from __future__ import annotations

from procurement_agent.schema.enums import ComponentCategory
from procurement_agent.schema.registry import spec_for
from procurement_agent.services.confidence import DEFAULT_TIER, tier_for
from procurement_agent.services.conflict_hitl.severity import (
    DEFAULT_CRITICALITY,
    criticality_for,
)
from procurement_agent.services.conflict_hitl.tolerance import DEFAULT_TOLERANCE, tolerance_for
from procurement_agent.services.field_catalog import profile_for


def test_profile_for_nameplate_power_agrees_with_the_four_lookups() -> None:
    profile = profile_for("nameplate_power")
    assert profile.key == "nameplate_power"
    assert profile.spec == spec_for("nameplate_power", ComponentCategory.PV_MODULES)
    assert profile.tolerance == tolerance_for("nameplate_power")
    assert profile.criticality == criticality_for("nameplate_power")
    assert profile.tier == tier_for("nameplate_power")


def test_profile_for_unknown_key_returns_existing_defaults() -> None:
    profile = profile_for("not_a_contract_key")
    assert profile.spec is None
    assert profile.tolerance == DEFAULT_TOLERANCE
    assert profile.criticality == DEFAULT_CRITICALITY
    assert profile.tier == DEFAULT_TIER


def test_profile_for_does_not_pick_an_ambiguous_spec() -> None:
    """`insulation_type` has two category-specific specs; the key-only view
    must not invent a winner."""
    profile = profile_for("insulation_type")
    assert profile.spec is None
    assert profile.tolerance == tolerance_for("insulation_type")
