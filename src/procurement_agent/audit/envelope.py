"""H.3 - the audit envelope, the preimage, and the digest, per D-13 section 2.

    hash = SHA-256(JCS({
        "v": 1, "stream", "seq", "event_type", "actor",
        "recorded_at", "prev_hash": lowercase-hex or null,
        "payload": {...}
    }))

**One JCS object, never a concatenation.** The sketch this replaced was
`sha256(prev_hash || canonical_payload || ...)`, and it was wrong twice. The
trailing `...` left the field set unenumerated, so two implementations could
disagree about what is covered while both believing they followed it; and a
delimiter-free concatenation is ambiguous by construction, because distinct
field values can produce identical bytes. Wrapping the envelope in one canonical
object fixes both at once - JCS gives unambiguous framing for free, and the
object's keys *are* the enumeration.

**`payload` embeds as the parsed JSON object, not as the `payload_canonical`
string.** D-13 flags this as the one reading that looks obvious both ways and
hashes differently: one nests an object, the other nests a quoted string. The
consequence of getting it backwards is the worst outcome available here -
chains that verify under this implementation and fail under every correct one.

**So the digest is not injective over stored bytes, and something outside it has
to be.** Embedding the parse means the exact bytes of `payload_canonical` are
not covered: `{"a":1}`, `{"a":1.0}`, `{"a":1E0}`, `{"a": 1 }` and `{"a":1,"a":2}`
are five distinct stored strings with one digest. This is not academic, because
`payload jsonb` is `GENERATED ALWAYS AS (payload_canonical::jsonb)` and jsonb
preserves the numeric literal text it is given - so rewriting `"value":400` to
`"value":400.0` changes what every human and every SQL query reads while the
digest still matches.

What closes it is `verify._check_payload`'s `payload_not_canonical` check, which
recomputes the JCS form and compares the bytes. **D-13 does not require that
check, and the integrity claim does not hold without it** - an implementation
written against the decision alone accepts all five. It is named here, in the
module that creates the gap, so the two are not read separately.

Changing the preimage to embed the canonical *string* would close it at the
source and is deliberately not done: `v` exists precisely because altering the
hashed field set re-bases every chain ever written, and that is the irreversible
window D-13 names.

**Two canonicalisation schemes live in this codebase, and this is the boundary
between them** (D-13 asks for the restatement to sit in WP-H rather than in the
decision record). C4, here, uses JCS because an audit chain has to be verifiable
by an implementation that is not this one, and cross-language agreement is worth
more than Python fidelity. C6, the workbook projection in D-14, uses sorted-key
JSON with `repr` floats because ECMAScript number rules collapse `650.0` to
`650`, erasing an int/float distinction the store's `value: object` genuinely
carries. The same collapse applies to anything hashed here - which is accepted,
not overlooked. Values that must survive it are the reason
`schema.encoding.encode_value` tags `Decimal` as a string, and payloads should
be built from its output.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from ..schema.encoding import is_aware
from .canonical import JsonObject, JsonValue, canonical_text

__all__ = [
    "ENVELOPE_VERSION",
    "EVENT_TYPES_V1",
    "KNOWN_ENVELOPE_VERSIONS",
    "RUN_EVENT_TYPES_V1",
    "RUN_STREAM_PREFIX",
    "STREAM_PREFIX",
    "AuditEvent",
    "ChainTip",
    "RunAuditEvent",
    "UnknownEventTypeError",
    "build_event",
    "build_preimage",
    "build_run_event",
    "digest_of",
    "document_id_for_stream",
    "format_recorded_at",
    "run_id_for_stream",
    "stream_for_document",
    "stream_for_run",
    "taxonomy_for_stream",
]

#: D-13 section 3. Inside the preimage, and load-bearing: without it, changing
#: the hashed field set later invalidates every chain ever written, because a
#: verifier can no longer recompute historical digests. With it, evolution is
#: additive - a verifier dispatches per version, and chains still link across
#: the boundary because `prev_hash` is only bytes.
ENVELOPE_VERSION = 1

#: Every version a verifier can still recompute.
#:
#: ⚠️ **D-13 does not say how a verifier learns a row's version.** `v` lives
#: inside the digest and nowhere else - `audit.event` has no version column - so
#: the dispatch section 3 describes cannot read the version, only *test* it.
#: That is sound rather than merely expedient: because `v` is inside the hash, a
#: digest that matches under version k is proof the writer used version k. It is
#: recorded here because the alternative reading - "add a version column later"
#: - would put a forgeable discriminator outside the digest, and because a
#: second entry in this tuple is the moment the cost becomes real.
KNOWN_ENVELOPE_VERSIONS = (ENVELOPE_VERSION,)

#: D-13 section 5: C4's taxonomy for `audit.event`. Additions are allowed by
#: amendment to that decision - which is why `sql/07` chose a CHECK over a
#: native enum - and removals and renames are forbidden once any event exists,
#: because that is what a chain cannot tolerate.
#:
#: **Six values, not seven: `web_search` left with `sql/12` (P2-C7).** It was
#: never storable here. `audit.event.document_id` is `NOT NULL REFERENCES
#: public.document`, and a gap-triggered web search happens precisely because no
#: document supplied the value (A-49). That is also why removing it does not
#: breach section 5's prohibition: the prohibition protects rows that use a
#: value, and the FK made such a row unrepresentable in every deployment.
#: `sql/12` checks anyway before it drops the value. The event now lives in
#: `RUN_EVENT_TYPES_V1`.
#:
#: Still named `_V1`: this is the taxonomy's identity, not the envelope version.
#: The `"v": 1` inside the preimage is `ENVELOPE_VERSION`, and no digest changes.
#: `tests/test_audit_envelope.py` holds this set equal to the effective CHECK.
EVENT_TYPES_V1 = frozenset(
    {
        "document_ingested",
        "parse_failure",
        "extraction",
        "conflict_detected",
        "resolution",
        "attempt_failed",
    }
)

#: The run-scoped taxonomy - `audit.run_event`'s CHECK in `sql/11` (P2-C7).
#:
#: `web_search` is the gap-triggered query log (FR-WEB-01, NFR-02 names "web
#: queries" explicitly); `compose_override` is the `--accept-incomplete`
#: decision, which D-13 calls the highest-repudiation-risk record in the
#: system; `run_started` and `run_finished` bracket a run so that a chain with a
#: start and no finish is legible as an interrupted run rather than a quiet one.
#: Section 5's rules apply unchanged.
RUN_EVENT_TYPES_V1 = frozenset(
    {
        "web_search",
        "compose_override",
        "run_started",
        "run_finished",
    }
)

#: `CHECK (stream = 'doc:' || document_id)` in `sql/07`. Decision 9 chains per
#: document "not globally, so cross-document concurrency stays unconstrained",
#: and the DDL makes that structural rather than conventional.
STREAM_PREFIX = "doc:"

#: `CHECK (stream = 'run:' || run_id)` in `sql/11`. Disjoint from
#: `STREAM_PREFIX` by construction, which is what lets the stream string alone
#: say which table - and which taxonomy - a chain belongs to.
RUN_STREAM_PREFIX = "run:"


class UnknownEventTypeError(ValueError):
    """An event type outside D-13's v1 taxonomy.

    Raised in Python rather than left to the CHECK constraint so the message can
    name the taxonomy and the decision that owns it. The constraint still
    rejects it either way; this only decides which error the caller reads.
    """


@dataclass(frozen=True, slots=True)
class ChainTip:
    """The last event in a stream: everything a new event needs from its parent.

    Passed as one value rather than as a `seq` and a `prev_hash` because
    `sql/07`'s `CHECK ((prev_hash IS NULL) = (seq = 0))` ties them together, and
    two arguments is two chances for a caller to disagree with it.
    """

    seq: int
    hash: bytes


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """A row of `audit.event`, with its digest already computed.

    Frozen because the digest covers the other fields: a mutable event is one
    where `hash` can silently stop describing the thing it names.
    """

    stream: str
    document_id: str
    seq: int
    prev_hash: bytes | None
    hash: bytes
    event_type: str
    actor: str
    payload_canonical: str
    recorded_at: datetime


def stream_for_document(document_id: str) -> str:
    """The stream name for a document, in one place.

    A second call site that writes `f"doc:{document_id}"` is a second call site
    that can get it wrong, and the CHECK would then reject the INSERT with a
    constraint name rather than an explanation.
    """
    return STREAM_PREFIX + document_id


def document_id_for_stream(stream: str) -> str:
    """The inverse, for the verifier, which starts from a stream name.

    Rejects anything not document-scoped: Decision 9 forbids a global chain, and
    a verifier that accepted `global` would report cleanly on a stream the
    writer could never have created.
    """
    if not stream.startswith(STREAM_PREFIX) or len(stream) == len(STREAM_PREFIX):
        raise ValueError(
            f"{stream!r} is not a document-scoped stream. sql/07 constrains every stream to "
            f"{STREAM_PREFIX!r} || document_id; Decision 9 has no global chain."
        )
    return stream[len(STREAM_PREFIX) :]


@dataclass(frozen=True, slots=True)
class RunAuditEvent:
    """A row of `audit.run_event`, with its digest already computed.

    A separate type rather than `AuditEvent` with an optional `document_id`,
    because the two tables differ in exactly that column and an optional field
    would let a run event reach `audit.event`'s INSERT with a `None` document -
    a type error turned into a constraint violation three layers down.
    """

    stream: str
    run_id: str
    seq: int
    prev_hash: bytes | None
    hash: bytes
    event_type: str
    actor: str
    payload_canonical: str
    recorded_at: datetime


def stream_for_run(run_id: str) -> str:
    """The stream name for a run, in one place (`sql/11`'s CHECK).

    Raises:
        ValueError: on an empty run id. `run:` alone names no run, and the
            table's `CHECK (run_id <> '')` would reject it with a constraint
            name rather than this sentence.
    """
    if not run_id:
        raise ValueError("run_id must be non-empty: 'run:' alone names no run (sql/11).")
    return RUN_STREAM_PREFIX + run_id


def run_id_for_stream(stream: str) -> str:
    """The inverse of `stream_for_run`, for the verifier.

    Raises:
        ValueError: if `stream` is not run-scoped.
    """
    if not stream.startswith(RUN_STREAM_PREFIX) or len(stream) == len(RUN_STREAM_PREFIX):
        raise ValueError(
            f"{stream!r} is not a run-scoped stream. sql/11 constrains every stream to "
            f"{RUN_STREAM_PREFIX!r} || run_id."
        )
    return stream[len(RUN_STREAM_PREFIX) :]


def taxonomy_for_stream(stream: str) -> frozenset[str]:
    """The event types a stream may hold, decided by its prefix alone.

    The verifier needs this because it starts from a stream name, and a
    document chain holding `compose_override` - or a run chain holding
    `extraction` - is a defect even though the type is legal *somewhere*.

    Raises:
        ValueError: if the stream is neither document- nor run-scoped.
    """
    if stream.startswith(STREAM_PREFIX) and len(stream) > len(STREAM_PREFIX):
        return EVENT_TYPES_V1
    if stream.startswith(RUN_STREAM_PREFIX) and len(stream) > len(RUN_STREAM_PREFIX):
        return RUN_EVENT_TYPES_V1
    raise ValueError(
        f"{stream!r} is neither document-scoped ({STREAM_PREFIX!r}) nor run-scoped "
        f"({RUN_STREAM_PREFIX!r}); Decision 9 has no global chain."
    )


def format_recorded_at(moment: datetime) -> str:
    """RFC 3339 UTC with the `Z` offset and six microsecond digits (D-13 section 4).

    Two reasons the pin is load-bearing, and the second is the stronger one.
    RFC 3339 permits both `Z` and `+00:00` and `datetime.isoformat()` emits the
    latter, so unpinned, two conformant callers hash the same instant
    differently. And `audit.event.recorded_at` is a `timestamptz`: the string
    that was hashed is **not stored anywhere**, so verification has to
    reconstruct it from the instant - which is only possible because this format
    is a total function of the instant.

    Converts rather than assumes, for the reason `schema.encoding._rfc3339`
    gives: aware datetimes compare by instant, so `17:04:05+02:00` and
    `15:04:05Z` are one value written two ways and must hash once. That function
    and this one produce identical text and are still separate, because D-14
    wraps its output in a `$datetime` tag to keep C6's value domain injective
    while C4 hashes it bare; `tests/test_audit_envelope.py` asserts they agree so
    the duplication cannot drift unnoticed.

    Raises:
        ValueError: on a naive datetime. It names no instant, so no encoding of
            it is honest, and assuming UTC would quietly record a timestamp the
            caller did not mean into the one field whose purpose is being
            tamper-evident.
    """
    if not is_aware(moment):
        raise ValueError(
            f"naive datetime {moment!r}: recorded_at is hashed, so it must name an instant. "
            "Attach a timezone at the boundary that produced it."
        )
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _as_plain_json(value: JsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: _as_plain_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_as_plain_json(item) for item in value]
    return value


def _as_plain_object(payload: JsonObject) -> dict[str, JsonValue]:
    return {key: _as_plain_json(item) for key, item in payload.items()}


def build_preimage(
    *,
    version: int,
    stream: str,
    seq: int,
    event_type: str,
    actor: str,
    recorded_at: str,
    prev_hash: bytes | None,
    payload: JsonObject,
) -> dict[str, JsonValue]:
    """D-13 section 2's object, exactly.

    Written in the decision's own key order even though JCS sorts them, so this
    function can be read against the decision line by line. The order has no
    effect on the digest, which is the point of canonicalising at all.

    `prev_hash` becomes lowercase hex or JSON `null` - never an omitted key. An
    absent key and a null key are different JCS objects, so choosing wrongly
    would make every genesis event unverifiable while every other event passed,
    which reads like data corruption rather than a bug.

    Keyword-only throughout: eight arguments of which two are strings that could
    be swapped without a type error is not a positional signature.
    """
    return {
        "v": version,
        "stream": stream,
        "seq": seq,
        "event_type": event_type,
        "actor": actor,
        "recorded_at": recorded_at,
        "prev_hash": None if prev_hash is None else prev_hash.hex(),
        "payload": _as_plain_object(payload),
    }


def digest_of(preimage: JsonObject) -> bytes:
    """SHA-256 over the canonical bytes of the preimage (D-13 section 1).

    Frozen by name rather than left to be inferred from a column width: the two
    `octet_length(...) = 32` CHECKs in `sql/07` already assume it, and "the
    digest and these two CHECKs now move together or not at all".
    """
    return hashlib.sha256(canonical_text(_as_plain_object(preimage)).encode("utf-8")).digest()


def build_event(
    *,
    document_id: str,
    event_type: str,
    actor: str,
    payload: JsonObject,
    recorded_at: datetime,
    tip: ChainTip | None,
) -> AuditEvent:
    """Build the next event for a stream, digest included.

    `tip` carries the chain position: `None` is a genesis event, and the DDL's
    `CHECK ((prev_hash IS NULL) = (seq = 0))` is satisfied by construction
    rather than by a caller remembering it. The tip must come from a read taken
    *under* the advisory lock - see `writer.append_event`, which is the only
    thing that should normally call this.

    Raises:
        UnknownEventTypeError: on a type outside D-13's v1 taxonomy.
        TypeError: if `payload` is not a JSON object.
        CanonicalisationError: if any value in `payload` has no JCS form.
    """
    sealed = _seal(
        stream=stream_for_document(document_id),
        taxonomy=EVENT_TYPES_V1,
        ddl_file="sql/07_audit_event.sql and sql/12_audit_event_taxonomy.sql",
        event_type=event_type,
        actor=actor,
        payload=payload,
        recorded_at=recorded_at,
        tip=tip,
    )
    return AuditEvent(
        stream=sealed.stream,
        document_id=document_id,
        seq=sealed.seq,
        prev_hash=sealed.prev_hash,
        hash=sealed.hash,
        event_type=event_type,
        actor=actor,
        payload_canonical=sealed.payload_canonical,
        recorded_at=recorded_at,
    )


def build_run_event(
    *,
    run_id: str,
    event_type: str,
    actor: str,
    payload: JsonObject,
    recorded_at: datetime,
    tip: ChainTip | None,
) -> RunAuditEvent:
    """Build the next event for a run's chain, digest included (P2-C7).

    The same preimage, the same digest and the same taxonomy discipline as
    `build_event` - D-13 says run events reuse "WP-H's envelope and lock
    discipline verbatim" - with `run:<run_id>` as the stream and
    `RUN_EVENT_TYPES_V1` as the vocabulary. Normally reached through
    `writer.append_run_event`, which reads `tip` under the advisory lock.

    Raises:
        ValueError: on an empty `run_id`.
        UnknownEventTypeError: on a type outside the run-scoped taxonomy -
            including the document-scoped types, which belong to `build_event`.
        TypeError: if `payload` is not a JSON object.
        CanonicalisationError: if any value in `payload` has no JCS form.
    """
    sealed = _seal(
        stream=stream_for_run(run_id),
        taxonomy=RUN_EVENT_TYPES_V1,
        ddl_file="sql/11_audit_run_event.sql",
        event_type=event_type,
        actor=actor,
        payload=payload,
        recorded_at=recorded_at,
        tip=tip,
    )
    return RunAuditEvent(
        stream=sealed.stream,
        run_id=run_id,
        seq=sealed.seq,
        prev_hash=sealed.prev_hash,
        hash=sealed.hash,
        event_type=event_type,
        actor=actor,
        payload_canonical=sealed.payload_canonical,
        recorded_at=recorded_at,
    )


@dataclass(frozen=True, slots=True)
class _Sealed:
    stream: str
    seq: int
    prev_hash: bytes | None
    hash: bytes
    payload_canonical: str


def _seal(
    *,
    stream: str,
    taxonomy: frozenset[str],
    ddl_file: str,
    event_type: str,
    actor: str,
    payload: JsonObject,
    recorded_at: datetime,
    tip: ChainTip | None,
) -> _Sealed:
    """Everything the two builders share: validation, chain position, digest.

    One function so that a document event and a run event cannot be hashed by
    two implementations that drift - the property D-13 asks for when it says
    "verbatim". The stream string is the only input that differs.
    """
    if event_type not in taxonomy:
        raise UnknownEventTypeError(
            f"{event_type!r} is not in this stream's C4 v1 event taxonomy "
            f"({', '.join(sorted(taxonomy))}). Adding a value is an additive amendment "
            f"to clarifications.md D-13 plus the CHECK in {ddl_file}; removing or "
            "renaming one is forbidden once any event exists."
        )
    # `payload jsonb` would accept a bare scalar and so would JCS, so nothing
    # downstream narrows this. D-13's `"payload": {...}` is a constraint, not
    # formatting: a scalar payload produces a chain that verifies and an audit
    # record with no field names in it.
    if not isinstance(payload, Mapping):
        raise TypeError(
            f"payload must be a JSON object, not {type(payload).__name__}. D-13's preimage "
            "embeds it as an object; a scalar would canonicalise and INSERT cleanly and be "
            "unreadable forever after."
        )

    seq = 0 if tip is None else tip.seq + 1
    prev_hash = None if tip is None else tip.hash
    plain_payload = _as_plain_object(payload)
    return _Sealed(
        stream=stream,
        seq=seq,
        prev_hash=prev_hash,
        hash=digest_of(
            build_preimage(
                version=ENVELOPE_VERSION,
                stream=stream,
                seq=seq,
                event_type=event_type,
                actor=actor,
                recorded_at=format_recorded_at(recorded_at),
                prev_hash=prev_hash,
                payload=plain_payload,
            )
        ),
        # JCS of the payload alone. The verifier recomputes the envelope around
        # it; this column is never the thing that gets hashed.
        payload_canonical=canonical_text(plain_payload),
    )
