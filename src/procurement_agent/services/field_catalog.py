"""A composing view over the four per-field lookup tables.

`FIELD_SPECS`, `FIELD_TOLERANCES`, `CRITICALITY`, and `FIELD_TIERS` stay the
sources of truth. This module does not merge or rewrite them. It answers
"what does this key mean across those tables?" in one place so call sites do
not re-assemble the same four lookups.

`flags_for` is deliberately not wired here. That gate still has no field-name
or Tier A notion (D-29 / Phase 2 Story 6); `tests/test_confidence.py` and the
C6 digest pin the current gap.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..schema import Severity
from ..schema.registry import FIELD_SPECS, FieldSpec
from .confidence import CriticalityTier, tier_for
from .conflict_hitl.severity import criticality_for
from .conflict_hitl.tolerance import FieldTolerance, tolerance_for

__all__ = ["FieldProfile", "profile_for"]


@dataclass(frozen=True, slots=True)
class FieldProfile:
    """The four existing lookups for one contract key, composed."""

    key: str
    spec: FieldSpec | None
    tolerance: FieldTolerance
    criticality: Severity
    tier: CriticalityTier


def _unique_spec(field_name: str) -> FieldSpec | None:
    """The spec for `field_name` when exactly one exists.

    `spec_for` requires a category because `insulation_type` has two rows.
    A catalog keyed only on the contract `key` must not pick one of those.
    """
    matches = [spec for spec in FIELD_SPECS if spec.key == field_name]
    return matches[0] if len(matches) == 1 else None


def profile_for(field_name: str) -> FieldProfile:
    """Compose the existing per-field tables for `field_name`."""
    return FieldProfile(
        key=field_name,
        spec=_unique_spec(field_name),
        tolerance=tolerance_for(field_name),
        criticality=criticality_for(field_name),
        tier=tier_for(field_name),
    )
