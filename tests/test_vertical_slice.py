"""Executable sanitized-PV path from intake through review and workbook output."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, LiteralString

import pytest
from openpyxl import load_workbook

from procurement_agent.audit import AuditConnection
from procurement_agent.schema import (
    ConflictClass,
    ConflictStatus,
    Resolution,
    ResolutionAction,
    Severity,
    SourceRef,
    SourceTier,
    WorkbookTab,
)
from procurement_agent.services.claims import FieldClaim, reduce_field
from procurement_agent.services.output import write_workbook
from procurement_agent.services.output.projection import ProjectionPolicy
from procurement_agent.services.vertical_slice import (
    InMemoryClaimStore,
    PendingAuditEvent,
    SanitizedCSVError,
    VerticalSliceResult,
    _candidate_key,
    _new_audit_events,
    _selected_claim_id,
    persist_vertical_slice,
    review_conflict,
    run_sanitized_pv_csv,
)

FIXTURE = Path(__file__).parent / "fixtures" / "ingestion" / "sanitized-pv-module.csv"
INGESTED_AT = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
DETECTED_AT = datetime(2026, 8, 1, 9, 1, tzinfo=UTC)


def _run(**kwargs: object) -> VerticalSliceResult:
    arguments: dict[str, object] = {
        "ingested_at": INGESTED_AT,
        "detected_at": DETECTED_AT,
        "actor": "integration-test",
    }
    arguments.update(kwargs)
    return run_sanitized_pv_csv(FIXTURE.read_bytes(), **arguments)  # type: ignore[arg-type]


def test_sanitized_csv_reaches_canonical_component_and_conflict_queue() -> None:
    result = _run()

    assert [source.document_id for source in result.sources] == [
        "syn-pv-datasheet",
        "syn-pv-purchase-order",
    ]
    assert len(result.claims) == 3
    assert all(claim.model_config["frozen"] for claim in result.claims)

    component = result.components[0]
    assert component.supplier == "Example Solar Ltd."
    assert component.model == "PV-SAN-650"
    assert component.fields["module_efficiency"][0].value == 22.5
    nameplate = component.fields["nameplate_power"][0]
    assert nameplate.value == 650.0
    assert nameplate.conflict_status is ConflictStatus.OPEN
    assert nameplate.source_ref.extractor_version == "sanitized-pv-csv@1"

    [conflict] = result.conflicts
    assert conflict.conflict_class is ConflictClass.INTER_DOCUMENT
    assert conflict.severity is Severity.HIGH
    assert [candidate.value for candidate in conflict.candidates] == [650.0, 655.0]


def test_claim_store_is_append_only_and_replay_is_idempotent() -> None:
    store = InMemoryClaimStore()
    first = _run(claim_store=store)
    second = _run(claim_store=store)

    assert store.claims == first.claims == second.claims
    assert len(store.claims) == 3


def test_projection_contains_store_policy_and_conflict() -> None:
    projection = _run().workbook_projection(
        policy=ProjectionPolicy(policy_version="test@1", confidence_threshold=0.80)
    )

    assert projection["policy"] == {
        "policy_version": "test@1",
        "confidence_threshold": 0.8,
    }
    conflicts = projection["conflicts"]
    assert isinstance(conflicts, list)
    assert conflicts[0]["field_name"] == "nameplate_power"


def test_persistence_hook_sees_only_a_fully_validated_result() -> None:
    persisted: list[VerticalSliceResult] = []
    result = _run(persist=persisted.append)

    assert persisted == [result]
    assert [event.event_type for event in result.audit_events] == [
        "document_ingested",
        "extraction",
        "document_ingested",
        "extraction",
        "conflict_detected",
    ]
    assert {event.document_id for event in result.audit_events} == {
        "syn-pv-datasheet",
        "syn-pv-purchase-order",
    }


class _Cursor:
    def __init__(self, row: Any = None) -> None:
        self._row = row

    def fetchone(self) -> Any:
        return self._row


class _AuditConnection:
    def __init__(
        self,
        *,
        autocommit: bool = False,
        fail_audit: bool = False,
        fail_commit: bool = False,
    ) -> None:
        self._autocommit = autocommit
        self.fail_audit = fail_audit
        self.fail_commit = fail_commit
        self.calls: list[str] = []
        self.tips: dict[str, tuple[int, bytes]] = {}
        self.commits = 0
        self.rollbacks = 0

    @property
    def autocommit(self) -> bool:
        return self._autocommit

    def execute(self, query: LiteralString, params: object = None) -> _Cursor:
        statement = " ".join(query.split())
        self.calls.append(statement)
        arguments = params if isinstance(params, tuple) else ()
        if statement.startswith("SELECT seq, hash FROM audit.event"):
            return _Cursor(self.tips.get(str(arguments[0])))
        if statement.startswith("INSERT INTO audit.event"):
            if self.fail_audit:
                raise RuntimeError("audit insert failed")
            self.tips[str(arguments[0])] = (int(arguments[2]), bytes(arguments[4]))
        return _Cursor()

    def commit(self) -> None:
        if self.fail_commit:
            raise RuntimeError("commit failed")
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _acknowledged(result: VerticalSliceResult) -> tuple[VerticalSliceResult, _AuditConnection]:
    conn = _AuditConnection()
    _, persisted, _ = persist_vertical_slice(
        conn,
        result,
        write=lambda connection, _: connection.execute("INSERT VERTICAL SLICE"),
    )
    return persisted, conn


def test_persistence_and_all_audit_intents_share_one_uncommitted_connection() -> None:
    result = _run()
    conn = _AuditConnection()

    def write(connection: AuditConnection, received: VerticalSliceResult) -> str:
        assert received is result
        connection.execute("INSERT VERTICAL SLICE")
        return "stored"

    stored, persisted, events = persist_vertical_slice(conn, result, write=write)

    assert stored == "stored"
    assert result.audit_events
    assert persisted.audit_events == ()
    assert len(events) == len(result.audit_events) == 5
    assert [(event.document_id, event.seq) for event in events] == [
        ("syn-pv-datasheet", 0),
        ("syn-pv-datasheet", 1),
        ("syn-pv-purchase-order", 0),
        ("syn-pv-purchase-order", 1),
        ("syn-pv-datasheet", 2),
    ]
    assert conn.calls[0] == "INSERT VERTICAL SLICE"
    assert conn.commits == 1
    assert conn.rollbacks == 0


def test_a_review_after_persistence_emits_only_its_new_resolution_intent() -> None:
    result = _run()
    persisted, conn = _acknowledged(result)
    conflict = persisted.conflicts[0]
    resolution = Resolution(
        action=ResolutionAction.SELECT_VALUE,
        resolved_by="procurement.lead",
        resolved_at=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
        rationale="The signed purchase order supersedes the draft datasheet.",
        value_before=650.0,
        value_after=655.0,
    )
    reviewed = review_conflict(persisted, entry_id=conflict.entry_id, resolution=resolution)

    human = [claim for claim in reviewed.claims if claim.extractor_version.startswith("human:")]
    assert len(human) == 1
    assert human[0].value == 655.0
    assert human[0].resolution == resolution
    assert human[0].extractor_version == "human:procurement.lead"
    assert [intent.event_type for intent in reviewed.audit_events] == ["resolution"]
    _, repersisted, events = persist_vertical_slice(
        conn,
        reviewed,
        write=lambda connection, _: connection.execute("INSERT RESOLUTION"),
    )

    assert reviewed.audit_events
    assert repersisted.audit_events == ()
    assert [(event.document_id, event.seq, event.event_type) for event in events] == [
        ("syn-pv-purchase-order", 2, "resolution")
    ]


def test_vertical_slice_refuses_autocommit_before_business_write() -> None:
    result = _run()
    conn = _AuditConnection(autocommit=True)
    write_called = False

    def write(_: AuditConnection, __: VerticalSliceResult) -> None:
        nonlocal write_called
        write_called = True

    with pytest.raises(ValueError, match="autocommit=False"):
        persist_vertical_slice(conn, result, write=write)

    assert write_called is False
    assert conn.calls == []
    assert conn.commits == 0
    assert conn.rollbacks == 1


@pytest.mark.parametrize("failure", ["audit", "commit"])
def test_persistence_failure_rolls_back_without_acknowledging(failure: str) -> None:
    result = _run()
    conn = _AuditConnection(fail_audit=failure == "audit", fail_commit=failure == "commit")

    with pytest.raises(RuntimeError, match=f"{failure} .*failed"):
        persist_vertical_slice(
            conn,
            result,
            write=lambda connection, _: connection.execute("INSERT VERTICAL SLICE"),
        )

    assert result.audit_events
    assert conn.commits == 0
    assert conn.rollbacks == 1


def test_review_selects_existing_candidate_without_rewriting_input() -> None:
    original, _ = _acknowledged(_run())
    conflict = original.conflicts[0]
    resolution = Resolution(
        action=ResolutionAction.SELECT_VALUE,
        resolved_by="procurement.lead",
        resolved_at=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
        rationale="The signed purchase order supersedes the draft datasheet.",
        value_before=650.0,
        value_after=655.0,
    )

    reviewed = review_conflict(original, entry_id=conflict.entry_id, resolution=resolution)

    assert original.conflicts[0].resolution is None
    assert original.components[0].fields["nameplate_power"][0].value == 650.0
    assert reviewed.conflicts[0].resolution == resolution
    field = reviewed.components[0].fields["nameplate_power"][0]
    assert field.value == 655.0
    assert field.conflict_status is ConflictStatus.RESOLVED
    assert field.resolution == resolution
    assert field.source_ref.document_id == "syn-pv-purchase-order"
    assert reviewed.audit_events[-1].event_type == "resolution"

    persisted_review, _ = _acknowledged(reviewed)
    with pytest.raises(ValueError, match="already has an immutable resolution"):
        review_conflict(persisted_review, entry_id=conflict.entry_id, resolution=resolution)


def test_review_refuses_unsourced_override() -> None:
    result, _ = _acknowledged(_run())
    resolution = Resolution(
        action=ResolutionAction.ENTER_OVERRIDE,
        resolved_by="procurement.lead",
        resolved_at=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
        rationale="manual override",
        value_after=660.0,
    )
    with pytest.raises(ValueError, match="selecting an existing value only"):
        review_conflict(result, entry_id=result.conflicts[0].entry_id, resolution=resolution)


def test_writer_emits_all_tabs_provenance_and_open_item(tmp_path: Path) -> None:
    workbook_path = _run().write_workbook(tmp_path / "pv.xlsx", confidence_threshold=0.80)
    workbook = load_workbook(workbook_path)

    assert workbook.sheetnames == [tab.value for tab in WorkbookTab]
    pv = workbook[WorkbookTab.PV_MODULES.value]
    assert pv.max_row == 3
    assert pv["E2"].comment is not None
    assert "syn-pv-datasheet" in pv["E2"].comment.text
    assert pv["H3"].value == "unresolved_conflict"
    open_items = workbook[WorkbookTab.CONFLICTS_OPEN_ITEMS.value]
    assert open_items.max_row == 2
    assert open_items["C2"].value == "nameplate_power"
    assert open_items["D2"].value == "open"


def test_workbook_bytes_are_deterministic(tmp_path: Path) -> None:
    result = _run()
    first = result.write_workbook(tmp_path / "first.xlsx", confidence_threshold=0.80)
    second = result.write_workbook(tmp_path / "second.xlsx", confidence_threshold=0.80)
    assert first.read_bytes() == second.read_bytes()


def test_adapter_rejects_an_off_contract_field_before_persistence() -> None:
    data = FIXTURE.read_text().replace("module_efficiency", "made_up_efficiency").encode()
    called = False

    def persist(_: VerticalSliceResult) -> None:
        nonlocal called
        called = True

    with pytest.raises(SanitizedCSVError, match="not a PV-module contract key"):
        run_sanitized_pv_csv(
            data,
            ingested_at=INGESTED_AT,
            detected_at=DETECTED_AT,
            actor="integration-test",
            persist=persist,
        )
    assert called is False


def test_document_hash_is_independent_of_row_arrival_order() -> None:
    rows = FIXTURE.read_bytes().splitlines()
    header, *body = rows
    reordered = b"\n".join([header, *reversed(body), b""])

    original = _run()
    permuted = run_sanitized_pv_csv(
        reordered,
        ingested_at=INGESTED_AT,
        detected_at=DETECTED_AT,
        actor="integration-test",
    )

    assert {source.document_id: source.content_hash for source in original.sources} == {
        source.document_id: source.content_hash for source in permuted.sources
    }
    assert original.claims == permuted.claims
    assert original.audit_events == permuted.audit_events


def test_review_settles_every_pair_in_its_condition_group() -> None:
    """Non-transitive comparability yields several queue entries for one field
    under one condition. D-16: the reviewer's decision is a claim that settles
    its group, so every entry in that group carries the decision, the field is
    RESOLVED to the chosen value, and re-reducing the store agrees."""
    third = (
        b"syn-pv-third,file:///sanitized/example-solar-pv650-third.pdf,spec_sheet,"
        b"Example Solar Ltd.,PV-SAN-650,650,nameplate_power,660,Wp,660 Wp,stc,"
        b"Electrical Data (STC),0.90"
    )
    result = run_sanitized_pv_csv(
        FIXTURE.read_bytes().rstrip() + b"\n" + third + b"\n",
        ingested_at=INGESTED_AT,
        detected_at=DETECTED_AT,
        actor="integration-test",
    )
    persisted, _ = _acknowledged(result)
    assert len([entry for entry in persisted.conflicts if entry.resolution is None]) >= 2
    target = persisted.conflicts[0]
    resolution = Resolution(
        action=ResolutionAction.SELECT_VALUE,
        resolved_by="procurement.lead",
        resolved_at=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
        rationale="Resolve one pair.",
        value_before=target.candidates[0].value,
        value_after=target.candidates[1].value,
    )

    reviewed = review_conflict(persisted, entry_id=target.entry_id, resolution=resolution)

    field_entries = [e for e in reviewed.conflicts if e.field_name == target.field_name]
    assert all(entry.resolution == resolution for entry in field_entries)
    assert reviewed.components[0].unresolved_conflicts() == []
    [field] = reviewed.components[0].fields["nameplate_power"]
    assert field.conflict_status is ConflictStatus.RESOLVED
    assert field.value == target.candidates[1].value

    again = reduce_field(
        [claim for claim in reviewed.claims if claim.field_name == "nameplate_power"],
        field_name="nameplate_power",
        supplier=target.supplier,
        model=target.model,
        nameplate=reviewed.components[0].nameplate,
        detected_at=DETECTED_AT,
    )
    assert [f.conflict_status for f in again.fields] == [ConflictStatus.RESOLVED]
    assert {entry.entry_id for entry in again.conflicts} == {e.entry_id for e in field_entries}
    assert all(entry.resolution == resolution for entry in again.conflicts)


def test_workbook_escapes_formula_shaped_supplier_and_model_text(tmp_path: Path) -> None:
    malicious = (
        FIXTURE.read_text()
        .replace("Example Solar Ltd.", "=1+1")
        .replace("PV-SAN-650", "+2+2")
        .encode()
    )
    result = run_sanitized_pv_csv(
        malicious,
        ingested_at=INGESTED_AT,
        detected_at=DETECTED_AT,
        actor="integration-test",
    )
    workbook_path = result.write_workbook(tmp_path / "formula-safe.xlsx", confidence_threshold=0.8)
    workbook = load_workbook(workbook_path, data_only=False)
    pv = workbook[WorkbookTab.PV_MODULES.value]

    assert pv["A2"].value == "'=1+1"
    assert pv["B2"].value == "'+2+2"
    assert pv["A2"].data_type == pv["B2"].data_type == "s"


def test_review_on_one_condition_group_preserves_resolution_when_another_is_open() -> None:
    """CRITICAL-3: when a field has open conflicts in two separate condition

    groups (`@STC` and `@NOCT`), resolving the `@STC` conflict first must keep
    the `@STC` `CanonicalField` `RESOLVED` with its `Resolution` attached, while
    leaving `@NOCT` `OPEN`."""
    noct_rows = (
        b"syn-pv-datasheet,file:///sanitized/example-solar-pv650-datasheet.pdf,spec_sheet,"
        b"Example Solar Ltd.,PV-SAN-650,650,nameplate_power,490,Wp,490 Wp,noct,"
        b"Electrical Data (NOCT),0.95\n"
        b"syn-pv-purchase-order,file:///sanitized/example-solar-pv650-po.pdf,purchase_order,"
        b"Example Solar Ltd.,PV-SAN-650,650,nameplate_power,505,Wp,505 Wp,noct,"
        b"Commercial Schedule,0.92\n"
    )
    result = run_sanitized_pv_csv(
        FIXTURE.read_bytes().rstrip() + b"\n" + noct_rows,
        ingested_at=INGESTED_AT,
        detected_at=DETECTED_AT,
        actor="integration-test",
    )
    persisted, _ = _acknowledged(result)
    assert len(persisted.conflicts) == 2

    stc_conflict = next(
        entry
        for entry in persisted.conflicts
        if entry.candidates[0].condition.basis is not None
        and entry.candidates[0].condition.basis.value == "stc"
    )
    resolution = Resolution(
        action=ResolutionAction.SELECT_VALUE,
        resolved_by="procurement.lead",
        resolved_at=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
        rationale="Approve STC rating from PO.",
        value_before=650.0,
        value_after=655.0,
    )
    reviewed = review_conflict(persisted, entry_id=stc_conflict.entry_id, resolution=resolution)
    by_basis = {
        field.condition.basis.value if field.condition.basis else None: field
        for field in reviewed.components[0].fields["nameplate_power"]
    }
    assert by_basis["stc"].conflict_status is ConflictStatus.RESOLVED
    assert by_basis["stc"].resolution == resolution
    assert by_basis["noct"].conflict_status is ConflictStatus.OPEN
    assert by_basis["noct"].resolution is None


def test_confidential_document_types_default_to_access_restricted() -> None:
    """SEC-CRITICAL-1 (NFR-03 / D-15): purchase orders, contracts, pricing,
    terms, and warranties are confidential by definition and must never default
    to `access_restricted=False`."""
    result = _run()
    by_id = {source.document_id: source for source in result.sources}
    assert by_id["syn-pv-datasheet"].access_restricted is False
    assert by_id["syn-pv-purchase-order"].access_restricted is True


def test_workbook_escapes_leading_whitespace_and_unit_formulas(tmp_path: Path) -> None:
    """SEC-HIGH-1: leading whitespace before `=`, `+`, `-`, `@` or pipe `|` and
    formula-shaped `unit` strings must be escaped with a leading apostrophe so
    `openpyxl` never emits an executable `<f>` formula cell."""
    result = _run()
    comp = result.components[0]
    mutated_fields = dict(comp.fields)
    mutated_fields["nameplate_power"] = [comp.fields["nameplate_power"][0].evolve(unit="=SUM(1,2)")]
    comp_with_bad_unit = comp.model_copy(
        update={
            "supplier": " =cmd|' /C calc'!A0",
            "fields": mutated_fields,
        }
    )
    workbook_path = write_workbook(
        [comp_with_bad_unit],
        tmp_path / "whitespace-formula-safe.xlsx",
        confidence_threshold=0.8,
    )
    workbook = load_workbook(workbook_path, data_only=False)
    pv = workbook[WorkbookTab.PV_MODULES.value]

    assert pv["A2"].value == "' =cmd|' /C calc'!A0"
    assert pv["A2"].data_type == "s"
    assert pv["F3"].value == "'=SUM(1,2)"
    assert pv["F3"].data_type == "s"


def test_run_sanitized_pv_csv_returns_acknowledged_result_from_persist_callback() -> None:
    """MEDIUM-10: when `persist` returns an acknowledged `VerticalSliceResult`
    (with `audit_events=()`), `run_sanitized_pv_csv` returns that acknowledged
    result so it can be passed directly into `review_conflict`."""
    conn = _AuditConnection()

    def persist_and_acknowledge(unpersisted: VerticalSliceResult) -> VerticalSliceResult:
        _, acknowledged, _ = persist_vertical_slice(
            conn,
            unpersisted,
            write=lambda connection, _: connection.execute("INSERT VERTICAL SLICE"),
        )
        return acknowledged

    acknowledged = _run(persist=persist_and_acknowledge)
    assert acknowledged.audit_events == ()

    conflict = acknowledged.conflicts[0]
    resolution = Resolution(
        action=ResolutionAction.SELECT_VALUE,
        resolved_by="procurement.lead",
        resolved_at=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
        rationale="The signed purchase order supersedes the draft datasheet.",
        value_before=650.0,
        value_after=655.0,
    )
    reviewed = review_conflict(acknowledged, entry_id=conflict.entry_id, resolution=resolution)
    assert reviewed.conflicts[0].resolution == resolution
    assert [event.event_type for event in reviewed.audit_events] == ["resolution"]


def test_review_conflict_disambiguates_same_number_unit_normalization_with_candidate_unit() -> None:
    """HIGH-7: resolving a UNIT_NORMALIZATION conflict where two candidates
    share the same numeric value (650.0) and differ in unit ('W' vs 'kW')
    succeeds when `candidate_unit='W'` is provided, and disambiguates
    `_selected_claim_id` cleanly as well."""
    base, _ = _acknowledged(_run())
    claim_w = FieldClaim(
        document_id="syn-pv-datasheet",
        field_name="nameplate_power",
        extractor_version="sanitized-pv-csv@1",
        value=650.0,
        unit="W",
        verbatim_value="650 W",
        source_tier=SourceTier.SYSTEM_OF_RECORD,
        source_ref=SourceRef(document_id="syn-pv-datasheet", section="Electrical Data"),
        confidence=0.95,
    )
    claim_kw = FieldClaim(
        document_id="syn-pv-purchase-order",
        field_name="nameplate_power",
        extractor_version="sanitized-pv-csv@1",
        value=650.0,
        unit="kW",
        verbatim_value="650 kW",
        source_tier=SourceTier.SYSTEM_OF_RECORD,
        source_ref=SourceRef(document_id="syn-pv-purchase-order", section="Schedule"),
        confidence=0.92,
    )
    reduction = reduce_field(
        (claim_w, claim_kw),
        field_name="nameplate_power",
        supplier=base.components[0].supplier,
        model=base.components[0].model,
        nameplate=base.components[0].nameplate,
        detected_at=DETECTED_AT,
    )
    assert len(reduction.conflicts) == 1
    unit_conflict = reduction.conflicts[0]
    assert unit_conflict.conflict_class is ConflictClass.UNIT_NORMALIZATION

    comp = base.components[0].model_copy(
        update={
            "fields": {
                **base.components[0].fields,
                "nameplate_power": list(reduction.fields),
            }
        }
    )
    slice_with_unit_conflict = replace(
        base,
        claims=(claim_w, claim_kw),
        components=(comp,),
        conflicts=reduction.conflicts,
        audit_events=(),
    )

    resolution = Resolution(
        action=ResolutionAction.SELECT_VALUE,
        resolved_by="procurement.lead",
        resolved_at=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
        rationale="650 W is the correct module rating; 650 kW was a unit typo.",
        value_before=650.0,
        value_after=650.0,
    )
    with pytest.raises(ValueError, match="matches more than one eligible candidate"):
        review_conflict(
            slice_with_unit_conflict,
            entry_id=unit_conflict.entry_id,
            resolution=resolution,
        )

    reviewed = review_conflict(
        slice_with_unit_conflict,
        entry_id=unit_conflict.entry_id,
        resolution=resolution,
        candidate_unit="W",
    )
    resolved_field = reviewed.components[0].fields["nameplate_power"][0]
    assert resolved_field.value == 650.0
    assert resolved_field.unit == "W"
    assert resolved_field.source_ref.document_id == "syn-pv-datasheet"
    assert resolved_field.conflict_status is ConflictStatus.RESOLVED

    candidate_claim_ids = {
        _candidate_key(claim_w.as_candidate()): 101,
        _candidate_key(claim_kw.as_candidate()): 202,
    }
    assert (
        _selected_claim_id(
            conflict=reviewed.conflicts[0],
            candidate_claim_ids=candidate_claim_ids,
            claims=reviewed.claims,
        )
        == 101
    )


def test_new_audit_events_selects_on_novelty_key_only() -> None:
    """Blueprint #6: novelty is the explicit key, not a payload excavation."""
    detected = PendingAuditEvent(
        document_id="doc-a",
        event_type="conflict_detected",
        actor="test",
        payload={"conflict": {"entry_id": "payload-must-be-ignored"}},
        recorded_at=DETECTED_AT,
        novelty_key="cf-new",
    )
    stale = PendingAuditEvent(
        document_id="doc-a",
        event_type="conflict_detected",
        actor="test",
        payload={"conflict": {"entry_id": "cf-new"}},
        recorded_at=DETECTED_AT,
        novelty_key="cf-old",
    )
    resolution = PendingAuditEvent(
        document_id="doc-a",
        event_type="resolution",
        actor="test",
        payload={"entry_id": "payload-must-be-ignored"},
        recorded_at=DETECTED_AT,
        novelty_key="cf-resolved",
    )
    selected = _new_audit_events(
        (detected, stale, resolution),
        new_document_ids=set(),
        new_claim_document_ids=set(),
        new_conflict_ids={"cf-new"},
        new_resolution_entry_ids={"cf-resolved"},
    )
    assert [event.novelty_key for event in selected] == ["cf-new", "cf-resolved"]
