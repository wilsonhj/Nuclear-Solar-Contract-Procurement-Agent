"""A narrow, executable PV-module path over a trusted sanitized CSV export.

This is deliberately not a general document extractor.  It is the smallest
production-code path that exercises the existing contracts end to end: parse a
structured intake, append immutable claims, reduce them, detect conflicts, expose
a human review operation, and hand the canonical components to the workbook
writer.  PDF/OCR and model-backed extraction remain adapter work.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Protocol, cast

from ..audit import AuditConnection, AuditEvent, JsonObject, append_event
from ..schema import (
    ComponentCategory,
    ComponentInstance,
    Condition,
    ConflictCandidate,
    ConflictQueueEntry,
    DocumentType,
    MeasurementBasis,
    Resolution,
    ResolutionAction,
    SourceDocument,
    SourceRef,
    SourceTier,
    canonical_json,
    encode_value,
    is_aware,
)
from ..schema.registry import Shape, ValueType, spec_for
from .claims import HUMAN_PREFIX, FieldClaim, canonical_claims, reduce_field, stamp_queue_hits
from .identity import identity_keys
from .output import write_workbook
from .output.projection import ProjectionPolicy, project_store

__all__ = [
    "InMemoryClaimStore",
    "PendingAuditEvent",
    "SanitizedCSVError",
    "StoredVerticalSlice",
    "VerticalSliceConnection",
    "VerticalSliceResult",
    "persist_vertical_slice",
    "review_conflict",
    "run_sanitized_pv_csv",
    "stamp_queue_hits",
    "write_vertical_slice_rows",
]

EXTRACTOR_VERSION = "sanitized-pv-csv@1"
REQUIRED_COLUMNS = frozenset(
    {
        "document_id",
        "source_uri",
        "document_type",
        "supplier",
        "model",
        "nameplate",
        "field_name",
        "value",
        "unit",
        "verbatim_value",
        "basis",
        "section",
        "confidence",
    }
)


class SanitizedCSVError(ValueError):
    """The trusted intake is malformed or outside this slice's narrow contract."""


class InMemoryClaimStore:
    """An append-only reference store for immutable :class:`FieldClaim` values."""

    def __init__(self) -> None:
        self._claims: tuple[FieldClaim, ...] = ()

    @property
    def claims(self) -> tuple[FieldClaim, ...]:
        return self._claims

    def append(self, claim: FieldClaim) -> bool:
        """Append once; an exact replay is an idempotent no-op."""
        if claim in self._claims:
            return False
        self._claims = (*self._claims, claim)
        return True

    def extend(self, claims: Iterable[FieldClaim]) -> int:
        return sum(self.append(claim) for claim in claims)


class VerticalSliceConnection(AuditConnection, Protocol):
    """A transaction handle the vertical slice can commit or roll back."""

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


@dataclass(frozen=True, slots=True)
class StoredVerticalSlice:
    """Database identities written by :func:`write_vertical_slice_rows`."""

    document_ids: tuple[str, ...]
    claim_ids: tuple[int, ...]
    conflict_ids: tuple[str, ...]
    resolution_ids: tuple[int, ...]
    audit_events_to_append: tuple[PendingAuditEvent, ...]


@dataclass(frozen=True, slots=True)
class PendingAuditEvent:
    """An event to append in the caller's transaction with its business write."""

    document_id: str
    event_type: str
    actor: str
    payload: JsonObject
    recorded_at: datetime
    novelty_key: str


@dataclass(frozen=True, slots=True)
class VerticalSliceResult:
    """The complete output of the narrow path, ready to persist or render."""

    sources: tuple[SourceDocument, ...]
    claims: tuple[FieldClaim, ...]
    components: tuple[ComponentInstance, ...]
    conflicts: tuple[ConflictQueueEntry, ...]
    audit_events: tuple[PendingAuditEvent, ...]

    def workbook_projection(self, *, policy: ProjectionPolicy) -> dict[str, object]:
        return project_store(
            components=self.components,
            conflicts=self.conflicts,
            sources=self.sources,
            policy=policy,
        )

    def write_workbook(self, destination: Path, *, confidence_threshold: float) -> Path:
        return write_workbook(
            list(self.components),
            destination,
            confidence_threshold=confidence_threshold,
        )


@dataclass(frozen=True, slots=True)
class _ParsedRow:
    document_id: str
    source_uri: str
    document_type: DocumentType
    supplier: str
    model: str
    nameplate: float
    field_name: str
    value: object
    unit: str | None
    verbatim_value: str
    condition: Condition
    section: str
    confidence: float


def run_sanitized_pv_csv(
    data: bytes,
    *,
    ingested_at: datetime,
    detected_at: datetime,
    actor: str,
    claim_store: InMemoryClaimStore | None = None,
    persist: Callable[[VerticalSliceResult], VerticalSliceResult | None] | None = None,
) -> VerticalSliceResult:
    """Parse and reduce one sanitized comparison CSV.

    ``persist`` is the explicit transaction boundary.  It is called only after
    every model, conflict and audit intent has validated; a database caller wraps
    it with the repository's transactional audit helper so the business rows and
    events commit or roll back together.
    """
    _require_aware(ingested_at, "ingested_at")
    _require_aware(detected_at, "detected_at")
    rows = _parse_rows(data)
    if not rows:
        raise SanitizedCSVError("the sanitized CSV contains no data rows")

    identities = {(row.supplier, row.model, row.nameplate) for row in rows}
    if len(identities) != 1:
        raise SanitizedCSVError(
            "this vertical slice accepts exactly one PV component identity per intake"
        )
    supplier, model, nameplate = identities.pop()

    source_rows: dict[str, list[_ParsedRow]] = {}
    for row in rows:
        source_rows.setdefault(row.document_id, []).append(row)
    sources = tuple(
        _source_document(document_id, grouped, ingested_at=ingested_at)
        for document_id, grouped in sorted(source_rows.items())
    )

    claims = tuple(canonical_claims(_claim(row) for row in rows))
    store = claim_store if claim_store is not None else InMemoryClaimStore()
    store.extend(claims)
    relevant = tuple(
        canonical_claims(
            claim
            for claim in store.claims
            if claim.document_id in source_rows
            and claim.field_name in {row.field_name for row in rows}
        )
    )

    by_field: dict[str, list[FieldClaim]] = {}
    for claim in relevant:
        by_field.setdefault(claim.field_name, []).append(claim)
    reductions = {
        field_name: reduce_field(
            field_claims,
            field_name=field_name,
            supplier=supplier,
            model=model,
            nameplate=nameplate,
            detected_at=detected_at,
        )
        for field_name, field_claims in sorted(by_field.items())
    }
    fields = {name: list(reduction.fields) for name, reduction in reductions.items()}
    flattened_conflicts = tuple(
        entry for reduction in reductions.values() for entry in reduction.conflicts
    )

    keys = identity_keys(supplier, model, nameplate)
    component = ComponentInstance(
        supplier=supplier,
        model=model,
        component_category=ComponentCategory.PV_MODULES,
        nameplate=nameplate,
        surrogate_id=keys.surrogate_id,
        manufacturer_key=keys.manufacturer_key,
        model_family=keys.model_family,
        fields=fields,
    )
    audit_events = _audit_events(
        sources=sources,
        claims=relevant,
        conflicts=flattened_conflicts,
        actor=actor,
        recorded_at=detected_at,
    )
    result = VerticalSliceResult(
        sources=sources,
        claims=relevant,
        components=(component,),
        conflicts=flattened_conflicts,
        audit_events=audit_events,
    )
    if persist is not None:
        acknowledged = persist(result)
        if isinstance(acknowledged, VerticalSliceResult):
            return acknowledged
        if acknowledged is not None:
            raise TypeError(
                "persist must return the acknowledged VerticalSliceResult or None, "
                f"not {type(acknowledged).__name__}"
            )
    return result


def persist_vertical_slice[ResultT](
    conn: VerticalSliceConnection,
    result: VerticalSliceResult,
    *,
    write: Callable[[AuditConnection, VerticalSliceResult], ResultT],
) -> tuple[ResultT, VerticalSliceResult, tuple[AuditEvent, ...]]:
    """Persist the slice and every audit intent in one owned transaction.

    The business callback and every applicable intent run on the same connection
    before a single commit.  The concrete PostgreSQL writer reports only events
    whose business rows were newly applied, so exact retries are idempotent on
    both sides of the transaction.  This service boundary owns the commit and
    clears the outbox only after PostgreSQL confirms success.  Any business,
    audit or commit failure rolls back and returns no acknowledged state.
    """
    if not result.audit_events:
        raise ValueError("a vertical-slice persistence must carry at least one audit event")
    try:
        if conn.autocommit:
            raise ValueError("vertical-slice persistence requires autocommit=False")
        written = write(conn, result)
        intents = (
            written.audit_events_to_append
            if isinstance(written, StoredVerticalSlice)
            else result.audit_events
        )
        events: list[AuditEvent] = []
        for intent in intents:
            events.append(
                append_event(
                    conn,
                    document_id=intent.document_id,
                    event_type=intent.event_type,
                    actor=intent.actor,
                    payload=intent.payload,
                    recorded_at=intent.recorded_at,
                )
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return written, replace(result, audit_events=()), tuple(events)


def write_vertical_slice_rows(
    conn: AuditConnection, result: VerticalSliceResult
) -> StoredVerticalSlice:
    """Write the narrow slice into the repository's existing PostgreSQL schema.

    This is the concrete business callback for :func:`persist_vertical_slice`.
    It never commits: the surrounding service commits these rows and the audit
    chain together.  Documents and claims use their schema-level natural keys,
    while conflict candidates reference the immutable claim rows they display.
    A reviewed result additionally appends its immutable resolution and marks
    the queue item resolved.
    """
    if len(result.components) != 1:
        raise ValueError("the sanitized PV repository writer requires exactly one component")
    component = result.components[0]

    document_ids: list[str] = []
    new_document_ids: set[str] = set()
    for source in result.sources:
        row = conn.execute(
            """
            INSERT INTO public.document
                (document_id, content_hash, source_uri, document_type,
                 ingested_at, data_vintage, access_restricted)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (content_hash) DO NOTHING
            RETURNING document_id
            """,
            (
                source.document_id,
                source.content_hash,
                source.source_uri,
                source.document_type.value,
                source.ingested_at,
                source.data_vintage,
                source.access_restricted,
            ),
        ).fetchone()
        stored_document_id = (
            str(row[0])
            if row is not None
            else _existing_document_id(conn, content_hash=source.content_hash)
        )
        if row is not None:
            new_document_ids.add(stored_document_id)
        if stored_document_id != source.document_id:
            raise ValueError(
                f"content hash {source.content_hash!r} already belongs to document "
                f"{stored_document_id!r}, not {source.document_id!r}"
            )
        document_ids.append(stored_document_id)

    claim_ids: list[int] = []
    new_claim_document_ids: set[str] = set()
    candidate_claim_ids: dict[str, int] = {}
    for claim in result.claims:
        row = conn.execute(
            """
            INSERT INTO public.claim
                (document_id, component_category, supplier, model, nameplate,
                 field, extractor_version, value, unit, verbatim_value,
                 condition, source_tier, source_ref, confidence)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s,
                    %s::jsonb, %s, %s::jsonb, %s)
            ON CONFLICT ON CONSTRAINT claim_natural_key DO NOTHING
            RETURNING claim_id
            """,
            (
                claim.document_id,
                component.component_category.value,
                component.supplier,
                component.model,
                component.nameplate,
                claim.field_name,
                claim.extractor_version,
                _json(claim.value),
                claim.unit,
                claim.verbatim_value,
                _json(claim.condition),
                claim.source_tier.value,
                _json(claim.provenance()),
                claim.confidence,
            ),
        ).fetchone()
        claim_id = (
            int(row[0])
            if row is not None
            else _existing_claim_id(conn, claim=claim, component=component)
        )
        if row is not None:
            new_claim_document_ids.add(claim.document_id)
        claim_ids.append(claim_id)
        candidate_claim_ids[_candidate_key(claim.as_candidate())] = claim_id

    conflict_ids: list[str] = []
    new_conflict_ids: set[str] = set()
    resolution_ids: list[int] = []
    new_resolution_entry_ids: set[str] = set()
    for conflict in result.conflicts:
        conflict_row = conn.execute(
            """
            INSERT INTO public.conflict
                (entry_id, field_name, supplier, model, component_category,
                 conflict_class, severity, explanation, detected_at, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (entry_id) DO NOTHING
            RETURNING entry_id
            """,
            (
                conflict.entry_id,
                conflict.field_name,
                conflict.supplier,
                conflict.model,
                conflict.component_category.value,
                conflict.conflict_class.value,
                int(conflict.severity),
                conflict.explanation,
                conflict.detected_at,
                "pending",
            ),
        ).fetchone()
        if conflict_row is not None:
            new_conflict_ids.add(conflict.entry_id)
        conflict_ids.append(conflict.entry_id)
        for ordinal, candidate in enumerate(conflict.candidates):
            candidate_claim_id = candidate_claim_ids.get(_candidate_key(candidate))
            if candidate_claim_id is None:
                raise ValueError(
                    f"conflict {conflict.entry_id!r} contains a candidate with no stored claim"
                )
            conn.execute(
                """
                INSERT INTO public.conflict_candidate (entry_id, claim_id, ordinal)
                VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (conflict.entry_id, candidate_claim_id, ordinal),
            )

        resolution = conflict.resolution
        if resolution is None:
            continue
        selected_claim_id = _selected_claim_id(
            conflict=conflict,
            candidate_claim_ids=candidate_claim_ids,
            claims=result.claims,
        )
        existing_resolution_id = _find_existing_resolution_id(
            conn,
            conflict=conflict,
            selected_claim_id=selected_claim_id,
        )
        if existing_resolution_id is not None:
            resolution_ids.append(existing_resolution_id)
            continue
        transitioned = conn.execute(
            """
            UPDATE public.conflict
               SET status = 'resolved', lease_owner = NULL, lease_expires_at = NULL
             WHERE entry_id = %s AND status = 'pending'
            RETURNING entry_id
            """,
            (conflict.entry_id,),
        ).fetchone()
        if transitioned is None:
            existing_resolution_id = _find_existing_resolution_id(
                conn,
                conflict=conflict,
                selected_claim_id=selected_claim_id,
            )
            if existing_resolution_id is not None:
                resolution_ids.append(existing_resolution_id)
                continue
            raise ValueError(
                f"conflict {conflict.entry_id!r} is no longer pending; "
                "refusing a stale or competing resolution"
            )
        row = conn.execute(
            """
            INSERT INTO public.resolution
                (entry_id, action, resolved_by, resolved_at, rationale,
                 value_before, value_after, selected_claim_id)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
            RETURNING resolution_id
            """,
            (
                conflict.entry_id,
                resolution.action.value,
                resolution.resolved_by,
                resolution.resolved_at,
                resolution.rationale,
                _optional_json(resolution.value_before),
                _optional_json(resolution.value_after),
                selected_claim_id,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("resolution insert returned no database identity")
        resolution_id = int(row[0])
        resolution_ids.append(resolution_id)
        new_resolution_entry_ids.add(conflict.entry_id)

    return StoredVerticalSlice(
        document_ids=tuple(document_ids),
        claim_ids=tuple(claim_ids),
        conflict_ids=tuple(conflict_ids),
        resolution_ids=tuple(resolution_ids),
        audit_events_to_append=_new_audit_events(
            result.audit_events,
            new_document_ids=new_document_ids,
            new_claim_document_ids=new_claim_document_ids,
            new_conflict_ids=new_conflict_ids,
            new_resolution_entry_ids=new_resolution_entry_ids,
        ),
    )


def _existing_document_id(conn: AuditConnection, *, content_hash: str) -> str:
    row = conn.execute(
        "SELECT document_id FROM public.document WHERE content_hash = %s",
        (content_hash,),
    ).fetchone()
    if row is None:
        raise RuntimeError("document insert reported a conflict but no existing row was found")
    return str(row[0])


def _existing_claim_id(
    conn: AuditConnection, *, claim: FieldClaim, component: ComponentInstance
) -> int:
    row = conn.execute(
        """
        SELECT claim_id FROM public.claim
         WHERE document_id = %s AND component_category = %s
           AND supplier = %s AND model = %s
           AND nameplate IS NOT DISTINCT FROM %s
           AND field = %s AND extractor_version = %s
           AND value = %s::jsonb AND unit IS NOT DISTINCT FROM %s
           AND verbatim_value IS NOT DISTINCT FROM %s
           AND condition = %s::jsonb AND source_tier = %s
           AND source_ref = %s::jsonb AND confidence = %s
        """,
        (
            claim.document_id,
            component.component_category.value,
            component.supplier,
            component.model,
            component.nameplate,
            claim.field_name,
            claim.extractor_version,
            _json(claim.value),
            claim.unit,
            claim.verbatim_value,
            _json(claim.condition),
            claim.source_tier.value,
            _json(claim.provenance()),
            claim.confidence,
        ),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"claim key for {claim.document_id!r}/{claim.field_name!r} already exists "
            "with different immutable content"
        )
    return int(row[0])


def _find_existing_resolution_id(
    conn: AuditConnection,
    *,
    conflict: ConflictQueueEntry,
    selected_claim_id: int | None,
) -> int | None:
    resolution = conflict.resolution
    assert resolution is not None
    row = conn.execute(
        """
        SELECT resolution_id FROM public.resolution
         WHERE entry_id = %s AND action = %s AND resolved_by = %s
           AND resolved_at = %s AND rationale = %s
           AND value_before IS NOT DISTINCT FROM %s::jsonb
           AND value_after IS NOT DISTINCT FROM %s::jsonb
           AND selected_claim_id IS NOT DISTINCT FROM %s
         ORDER BY resolution_id LIMIT 1
        """,
        (
            conflict.entry_id,
            resolution.action.value,
            resolution.resolved_by,
            resolution.resolved_at,
            resolution.rationale,
            _optional_json(resolution.value_before),
            _optional_json(resolution.value_after),
            selected_claim_id,
        ),
    ).fetchone()
    return None if row is None else int(row[0])


def _new_audit_events(
    intents: tuple[PendingAuditEvent, ...],
    *,
    new_document_ids: set[str],
    new_claim_document_ids: set[str],
    new_conflict_ids: set[str],
    new_resolution_entry_ids: set[str],
) -> tuple[PendingAuditEvent, ...]:
    selected: list[PendingAuditEvent] = []
    for intent in intents:
        if intent.event_type == "document_ingested":
            include = intent.novelty_key in new_document_ids
        elif intent.event_type == "extraction":
            include = intent.novelty_key in new_claim_document_ids
        elif intent.event_type == "conflict_detected":
            include = intent.novelty_key in new_conflict_ids
        elif intent.event_type == "resolution":
            include = intent.novelty_key in new_resolution_entry_ids
        else:
            include = True
        if include:
            selected.append(intent)
    return tuple(selected)


def _values_match(left: object, right: object) -> bool:
    return isinstance(left, bool) is isinstance(right, bool) and left == right


def _selected_claim_id(
    *,
    conflict: ConflictQueueEntry,
    candidate_claim_ids: dict[str, int],
    claims: Sequence[FieldClaim],
) -> int | None:
    """The stored candidate claim a value-selecting resolution chose.

    Read from the human claim that carries the decision (D-16 point 8: its
    provenance *is* the selected candidate's), never from the component's
    current fields, which a later review of the same field changes.
    """
    resolution = conflict.resolution
    assert resolution is not None
    if resolution.action not in {
        ResolutionAction.SELECT_VALUE,
        ResolutionAction.KEEP_SYSTEM_OF_RECORD,
    }:
        return None
    deciding = [claim for claim in claims if claim.is_human and claim.resolution == resolution]
    matching_candidates = [
        candidate
        for candidate in conflict.candidates
        if any(
            _values_match(candidate.value, claim.value)
            and candidate.unit == claim.unit
            and candidate.source_ref == claim.source_ref
            for claim in deciding
        )
    ]
    matches = [candidate_claim_ids[_candidate_key(candidate)] for candidate in matching_candidates]
    if len(matches) > 1:
        raise ValueError("a stored candidate resolution must identify exactly one claim")
    return matches[0] if matches else None


def _candidate_key(candidate: ConflictCandidate) -> str:
    return canonical_json(candidate)


def _json(value: object) -> str:
    return canonical_json(value)


def _optional_json(value: object | None) -> str | None:
    return None if value is None else _json(value)


def review_conflict(
    result: VerticalSliceResult,
    *,
    entry_id: str,
    resolution: Resolution,
    candidate_unit: str | None = None,
) -> VerticalSliceResult:
    """Apply the slice's minimal review action without mutating its input.

    The first operational review path supports choosing an existing candidate.
    Override entry needs a separately sourced value, and silently inventing
    provenance here would violate NFR-01.
    """
    if result.audit_events:
        raise ValueError(
            "persist and acknowledge the current audit intents before starting human review"
        )
    if resolution.action not in {
        ResolutionAction.SELECT_VALUE,
        ResolutionAction.KEEP_SYSTEM_OF_RECORD,
    }:
        raise ValueError(
            "the initial review path supports selecting an existing value only; "
            "override and re-search require their own sourced persistence paths"
        )
    matches = [entry for entry in result.conflicts if entry.entry_id == entry_id]
    if len(matches) != 1:
        raise KeyError(f"expected one conflict with entry_id {entry_id!r}, found {len(matches)}")
    target = matches[0]
    if target.resolution is not None:
        raise ValueError(f"conflict {entry_id!r} already has an immutable resolution")
    eligible = [
        candidate
        for candidate in target.candidates
        if resolution.action is ResolutionAction.SELECT_VALUE
        or candidate.source_tier is SourceTier.SYSTEM_OF_RECORD
    ]
    selected_candidates = [
        candidate
        for candidate in eligible
        if _values_match(candidate.value, resolution.value_after)
        and (candidate_unit is None or candidate.unit == candidate_unit)
    ]
    if not selected_candidates:
        raise ValueError("value_after must identify an eligible existing conflict candidate")
    if len(selected_candidates) > 1:
        raise ValueError(
            "value_after matches more than one eligible candidate; this minimal review "
            "operation cannot choose provenance unambiguously"
        )
    selected = selected_candidates[0]

    document_id = selected.source_ref.document_id
    if document_id is None:
        raise ValueError(
            "the selected candidate has no source document; a human claim needs one (NFR-01)"
        )
    human_claim = FieldClaim(
        document_id=document_id,
        field_name=target.field_name,
        extractor_version=f"{HUMAN_PREFIX}{resolution.resolved_by}",
        condition=selected.condition,
        value=selected.value,
        unit=selected.unit,
        verbatim_value=selected.verbatim_value,
        source_tier=selected.source_tier,
        source_ref=selected.source_ref,
        confidence=1.0,
        resolution=resolution,
    )
    # The review is re-reduced from claims rather than patched onto the fields
    # (D-16, C8): the same `reduce_field` that built the queue decides which
    # fields and entries the decision settles, so a later re-run agrees with it.
    components: list[ComponentInstance] = []
    settled: dict[str, Resolution] = {}
    for component in result.components:
        if component.supplier != target.supplier or component.model != target.model:
            components.append(component)
            continue
        reduction = reduce_field(
            [*(c for c in result.claims if c.field_name == target.field_name), human_claim],
            field_name=target.field_name,
            supplier=target.supplier,
            model=target.model,
            nameplate=component.nameplate,
            detected_at=target.detected_at,
            component_category=target.component_category,
            entry_id_prefix=target.entry_id.split("-", 1)[0] + "-",
        )
        settled.update(
            {entry.entry_id: entry.resolution for entry in reduction.conflicts if entry.resolution}
        )
        fields = {name: list(values) for name, values in component.fields.items()}
        fields[target.field_name] = list(reduction.fields)
        snapshot = {name: getattr(component, name) for name in type(component).model_fields}
        components.append(ComponentInstance.model_validate({**snapshot, "fields": fields}))
    if settled.get(entry_id) != resolution:
        raise RuntimeError(f"re-reducing after review did not settle {entry_id!r}")

    conflicts = tuple(
        entry.model_copy(update={"resolution": settled[entry.entry_id]})
        if entry.resolution is None and entry.entry_id in settled
        else entry
        for entry in result.conflicts
    )
    event = PendingAuditEvent(
        document_id=document_id,
        event_type="resolution",
        actor=resolution.resolved_by,
        payload=cast(
            JsonObject,
            {
                "entry_id": entry_id,
                "field_name": target.field_name,
                "resolution": encode_value(resolution),
            },
        ),
        recorded_at=resolution.resolved_at,
        novelty_key=entry_id,
    )
    return replace(
        result,
        claims=(*result.claims, human_claim),
        components=tuple(components),
        conflicts=conflicts,
        audit_events=(*result.audit_events, event),
    )


def _parse_rows(data: bytes) -> list[_ParsedRow]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SanitizedCSVError("sanitized CSV must be UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text), strict=True)
    columns = frozenset(reader.fieldnames or ())
    missing = sorted(REQUIRED_COLUMNS - columns)
    extra = sorted(columns - REQUIRED_COLUMNS)
    if missing or extra:
        raise SanitizedCSVError(
            f"CSV columns differ from the frozen intake (missing={missing}, extra={extra})"
        )
    parsed: list[_ParsedRow] = []
    for line_number, raw in enumerate(reader, start=2):
        try:
            field_name = raw["field_name"].strip()
            spec = spec_for(field_name, ComponentCategory.PV_MODULES)
            if spec is None:
                raise SanitizedCSVError(f"{field_name!r} is not a PV-module contract key")
            value = _parse_value(raw["value"], spec.shape, spec.value_type)
            unit = raw["unit"].strip() or None
            if unit != spec.unit:
                raise SanitizedCSVError(
                    f"{field_name!r} requires canonical unit {spec.unit!r}, got {unit!r}"
                )
            parsed.append(
                _ParsedRow(
                    document_id=_required(raw, "document_id"),
                    source_uri=_required(raw, "source_uri"),
                    document_type=DocumentType(_required(raw, "document_type")),
                    supplier=_required(raw, "supplier"),
                    model=_required(raw, "model"),
                    nameplate=float(_required(raw, "nameplate")),
                    field_name=field_name,
                    value=value,
                    unit=unit,
                    verbatim_value=_required(raw, "verbatim_value"),
                    condition=Condition(
                        basis=(
                            MeasurementBasis(raw["basis"].strip()) if raw["basis"].strip() else None
                        )
                    ),
                    section=_required(raw, "section"),
                    confidence=float(_required(raw, "confidence")),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, SanitizedCSVError):
                raise SanitizedCSVError(f"line {line_number}: {exc}") from exc
            raise SanitizedCSVError(f"line {line_number}: {exc}") from exc
    return parsed


def _parse_value(raw: str, shape: Shape, value_type: ValueType) -> object:
    if shape is not Shape.SCALAR:
        raise SanitizedCSVError("the initial CSV adapter accepts scalar contract fields only")
    text = raw.strip()
    if value_type is ValueType.FLOAT:
        return float(text)
    if value_type is ValueType.INT:
        return int(text)
    if value_type is ValueType.BOOL:
        if text.casefold() not in {"true", "false"}:
            raise SanitizedCSVError(f"boolean value must be true or false, got {text!r}")
        return text.casefold() == "true"
    if value_type is ValueType.STR:
        return text
    raise SanitizedCSVError(f"the initial CSV adapter does not parse {value_type.value}")


def _required(row: dict[str, str], name: str) -> str:
    value = row[name].strip()
    if not value:
        raise SanitizedCSVError(f"{name} is required")
    return value


def _source_document(
    document_id: str, rows: list[_ParsedRow], *, ingested_at: datetime
) -> SourceDocument:
    uris = {row.source_uri for row in rows}
    types = {row.document_type for row in rows}
    if len(uris) != 1 or len(types) != 1:
        raise SanitizedCSVError(
            f"document_id {document_id!r} must identify exactly one URI and document type"
        )
    encoded_rows = [_row_content(row) for row in rows]
    canonical_rows = json.dumps(
        sorted(
            encoded_rows,
            key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")),
        ),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return SourceDocument(
        document_id=document_id,
        content_hash="sha256:" + hashlib.sha256(canonical_rows).hexdigest(),
        source_uri=next(iter(uris)),
        document_type=next(iter(types)),
        ingested_at=ingested_at,
    )


def _claim(row: _ParsedRow) -> FieldClaim:
    return FieldClaim(
        document_id=row.document_id,
        field_name=row.field_name,
        extractor_version=EXTRACTOR_VERSION,
        condition=row.condition,
        value=row.value,
        unit=row.unit,
        verbatim_value=row.verbatim_value,
        source_tier=SourceTier.SYSTEM_OF_RECORD,
        source_ref=SourceRef(document_id=row.document_id, section=row.section),
        confidence=row.confidence,
    )


def _row_content(row: _ParsedRow) -> dict[str, object]:
    return {
        "document_id": row.document_id,
        "source_uri": row.source_uri,
        "document_type": row.document_type.value,
        "supplier": row.supplier,
        "model": row.model,
        "nameplate": row.nameplate,
        "field_name": row.field_name,
        "value": encode_value(row.value),
        "unit": row.unit,
        "verbatim_value": row.verbatim_value,
        "condition": encode_value(row.condition),
        "section": row.section,
        "confidence": row.confidence,
    }


def _audit_events(
    *,
    sources: tuple[SourceDocument, ...],
    claims: tuple[FieldClaim, ...],
    conflicts: tuple[ConflictQueueEntry, ...],
    actor: str,
    recorded_at: datetime,
) -> tuple[PendingAuditEvent, ...]:
    events: list[PendingAuditEvent] = []
    for source in sources:
        source_claims = [claim for claim in claims if claim.document_id == source.document_id]
        events.append(
            PendingAuditEvent(
                document_id=source.document_id,
                event_type="document_ingested",
                actor=actor,
                payload=cast(JsonObject, {"source": encode_value(source)}),
                recorded_at=source.ingested_at,
                novelty_key=source.document_id,
            )
        )
        events.append(
            PendingAuditEvent(
                document_id=source.document_id,
                event_type="extraction",
                actor=actor,
                payload=cast(JsonObject, {"claims": encode_value(source_claims)}),
                recorded_at=recorded_at,
                novelty_key=source.document_id,
            )
        )
    for conflict in conflicts:
        document_id = next(
            candidate.source_ref.document_id
            for candidate in conflict.candidates
            if candidate.source_ref.document_id is not None
        )
        events.append(
            PendingAuditEvent(
                document_id=document_id,
                event_type="conflict_detected",
                actor=actor,
                payload=cast(JsonObject, {"conflict": encode_value(conflict)}),
                recorded_at=recorded_at,
                novelty_key=conflict.entry_id,
            )
        )
    return tuple(events)


def _require_aware(value: datetime, name: str) -> None:
    if not is_aware(value):
        raise ValueError(f"{name} must be timezone-aware")
