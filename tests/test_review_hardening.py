"""Regression tests for the defects the PR #47 review found and fixed."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from procurement_agent.config import Settings
from procurement_agent.schema import (
    ConflictStatus,
    DocumentType,
    Resolution,
    ResolutionAction,
    SourceDocument,
    SourceRef,
    SourceTier,
)
from procurement_agent.schema.enums import RESTRICTED_DOCUMENT_TYPES
from procurement_agent.schema.field import normalise_unit
from procurement_agent.services.claims import FieldClaim, reduce_field
from procurement_agent.services.vertical_slice import (
    _candidate_key,
    _selected_claim_id,
    review_conflict,
    run_sanitized_pv_csv,
)

FIXTURE = Path(__file__).parent / "fixtures" / "ingestion" / "sanitized-pv-module.csv"
AT = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


def _claim(value: float, unit: str, doc: str, tier: SourceTier) -> FieldClaim:
    return FieldClaim(
        document_id=doc,
        field_name="usable_energy_per_container",
        extractor_version="test@1",
        value=value,
        unit=unit,
        verbatim_value=f"{value} {unit}",
        source_tier=tier,
        source_ref=SourceRef(document_id=doc, section="Spec"),
        confidence=0.9,
    )


def test_mega_and_milli_are_different_units() -> None:
    """Casefolding `MWh` and `mWh` to one spelling absorbed a 10^9 unit
    conflict (FR-ING-08). The M prefix keeps its case; other case does not."""
    assert normalise_unit("MWh") != normalise_unit("mWh")
    assert normalise_unit("kW") == normalise_unit("KW")
    assert normalise_unit("%/K") == normalise_unit("% / °C")

    reduction = reduce_field(
        [
            _claim(100.0, "MWh", "doc-a", SourceTier.SYSTEM_OF_RECORD),
            _claim(100.0, "mWh", "doc-b", SourceTier.WEB_SUPPLEMENT),
        ],
        field_name="usable_energy_per_container",
        supplier="S",
        model="M",
        nameplate=None,
        detected_at=AT,
        entry_id_prefix="bess-",
    )
    assert len(reduction.conflicts) == 1
    assert [f.conflict_status for f in reduction.fields] == [ConflictStatus.OPEN]


@pytest.mark.parametrize("document_type", sorted(RESTRICTED_DOCUMENT_TYPES))
def test_confidential_types_stay_restricted_through_copy_and_construct(
    document_type: DocumentType,
) -> None:
    """NFR-03 / D-15: the ratchet held only on the validated constructor."""
    open_doc = SourceDocument(
        document_id="d",
        content_hash="h",
        source_uri="file:///d",
        document_type=DocumentType.SPEC_SHEET,
        ingested_at=AT,
    )
    copied = open_doc.model_copy(update={"document_type": document_type})
    assert copied.access_restricted
    assert copied.model_copy(update={"access_restricted": False}).access_restricted
    built = SourceDocument.model_construct(document_type=document_type, access_restricted=False)
    assert built.access_restricted
    explicit = SourceDocument(
        document_id="d",
        content_hash="h",
        source_uri="file:///d",
        document_type=document_type,
        ingested_at=AT,
        access_restricted=False,
    )
    assert explicit.access_restricted


def test_selected_claim_comes_from_the_deciding_human_claim_not_current_fields() -> None:
    """A later review changes the component's fields; reading the selected
    candidate back from them made the first resolution unpersistable."""
    result = run_sanitized_pv_csv(
        FIXTURE.read_bytes(), ingested_at=AT, detected_at=AT, actor="test"
    )
    result = replace(result, audit_events=())
    entry = result.conflicts[0]
    chosen = entry.candidates[1]
    resolution = Resolution(
        action=ResolutionAction.SELECT_VALUE,
        resolved_by="lead",
        resolved_at=AT,
        rationale="PO governs.",
        value_before=entry.candidates[0].value,
        value_after=chosen.value,
    )
    reviewed = review_conflict(result, entry_id=entry.entry_id, resolution=resolution)
    ids = {_candidate_key(c): n for n, c in enumerate(entry.candidates, start=1)}
    stale_component = replace(reviewed, components=result.components)
    assert (
        _selected_claim_id(
            conflict=reviewed.conflicts[0],
            candidate_claim_ids=ids,
            claims=stale_component.claims,
        )
        == ids[_candidate_key(chosen)]
    )


def test_review_rereduction_agrees_with_the_queue() -> None:
    """Re-reducing the claims after a review used to report the reviewed pair
    as pending again under the same entry_id, while the field said RESOLVED."""
    result = replace(
        run_sanitized_pv_csv(FIXTURE.read_bytes(), ingested_at=AT, detected_at=AT, actor="t"),
        audit_events=(),
    )
    entry = result.conflicts[0]
    resolution = Resolution(
        action=ResolutionAction.SELECT_VALUE,
        resolved_by="lead",
        resolved_at=AT,
        rationale="PO governs.",
        value_after=entry.candidates[1].value,
    )
    reviewed = review_conflict(result, entry_id=entry.entry_id, resolution=resolution)
    again = reduce_field(
        [c for c in reviewed.claims if c.field_name == entry.field_name],
        field_name=entry.field_name,
        supplier=entry.supplier,
        model=entry.model,
        nameplate=reviewed.components[0].nameplate,
        detected_at=AT,
    )
    assert [e.resolution for e in again.conflicts] == [resolution]
    assert [f.conflict_status for f in again.fields] == [ConflictStatus.RESOLVED]


def test_persist_must_return_a_result_or_none() -> None:
    with pytest.raises(TypeError, match="persist must return"):
        run_sanitized_pv_csv(
            FIXTURE.read_bytes(),
            ingested_at=AT,
            detected_at=AT,
            actor="t",
            persist=lambda result: (result, "receipt"),  # type: ignore[arg-type,return-value]
        )


@pytest.mark.parametrize(
    "endpoint", ["user:pw@api.example.com/v1", "api.example.com/v1", "ftp://api.example.com"]
)
def test_endpoints_must_be_absolute_http_urls(endpoint: str) -> None:
    with pytest.raises(ValueError, match="http"):
        Settings(llm_endpoint=endpoint)
