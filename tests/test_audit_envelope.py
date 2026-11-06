"""H.3 - the audit envelope, the preimage, and the digest D-13 section 2 froze.

**The golden digests below were derived from the decision record and `rfc8785`,
not from the code they test.** A hash test whose expected value came out of the
implementation asserts only that the implementation is deterministic, which is
the one property a wrong preimage also has. Each constant here was computed by
canonicalising D-13 section 2's object literal directly and taking SHA-256 of
the result; the implementation had not been written when they were produced.

**The trap this file exists for** is `test_a_payload_embeds_as_an_object...`.
D-13 warns that `payload` embeds as the *parsed JSON object* and not as the
`payload_canonical` string, that both readings look obvious, and that they hash
differently. So `WRONG_STRING_EMBEDDED_DIGEST` is recorded here too: the test
does not merely check the right answer, it names the wrong one and refuses it.
Without that, the failure mode is a chain that verifies under this repo's
implementation and under no other - which D-13 calls its own worst case.
"""

from __future__ import annotations

import json
import pathlib
import re
import types
from datetime import UTC, datetime, timedelta, timezone

import pytest

from procurement_agent.audit import (
    ENVELOPE_VERSION,
    EVENT_TYPES_V1,
    AuditEvent,
    ChainTip,
    JsonObject,
    UnknownEventTypeError,
    build_event,
    build_preimage,
    canonical_text,
    digest_of,
    document_id_for_stream,
    format_recorded_at,
    stream_for_document,
)
from procurement_agent.schema.encoding import encode_value

# --- the golden fixture, transcribed from D-13 section 2 -----------------------

#: Annotated rather than inferred: a heterogeneous dict literal infers as
#: `dict[str, object]`, and `JsonObject` is what makes mypy check the leaves
#: against what JCS can actually serialise. See `audit.canonical.JsonValue`.
PAYLOAD: JsonObject = {
    "field": "price_per_watt_dc",
    "value": 0.19,
    "source": {"chunk_id": "chunk-open", "tier": "system_of_record"},
}
DOCUMENT_ID = "open-1"
ACTOR = "worker-7"
RECORDED_AT = datetime(2026, 8, 6, 15, 4, 5, tzinfo=UTC)
PARENT = bytes.fromhex("aa" * 32)

#: JCS of the payload alone - what the `payload_canonical` column stores.
PAYLOAD_CANONICAL = (
    '{"field":"price_per_watt_dc",'
    '"source":{"chunk_id":"chunk-open","tier":"system_of_record"},'
    '"value":0.19}'
)

#: SHA-256 of the JCS envelope for seq 1, `prev_hash` = `aa` * 32, event type
#: `extraction`.
GOLDEN_DIGEST = "8df14c8e579fff8ff4f4b1e8c13443b51cdc6b527b2fd0bd99e6c12a36938b87"

#: The same event as a genesis: seq 0, `prev_hash` null, `document_ingested`.
GOLDEN_GENESIS_DIGEST = "77927d51b8b8731de3b3d0f1f9ccb81dbc40699ed86b7b75d6a9252fe0d4f427"

#: What the seq 1 envelope hashes to if `payload` is embedded as the
#: `payload_canonical` *string* instead of as the parsed object. Recorded so the
#: test can refuse a specific wrong answer rather than merely accept a right one.
WRONG_STRING_EMBEDDED_DIGEST = "f2782027004c29ca9b7efb270137c584fb7489617690f335d4e35494dddab39f"


def _golden_event() -> AuditEvent:
    return build_event(
        document_id=DOCUMENT_ID,
        event_type="extraction",
        actor=ACTOR,
        payload=PAYLOAD,
        recorded_at=RECORDED_AT,
        tip=ChainTip(seq=0, hash=PARENT),
    )


# --- D-13 section 2: the preimage ---------------------------------------------


def test_a_the_digest_matches_the_value_derived_from_the_decision() -> None:
    """The whole of C4 in one assertion.

    If this moves, every chain ever written becomes unverifiable, so it is the
    one value in this repo that may never be re-baselined to match the code.
    """
    event = _golden_event()
    assert event.hash.hex() == GOLDEN_DIGEST
    assert len(event.hash) == 32, "the two octet_length CHECKs in sql/07 assume SHA-256"


def test_a_the_genesis_digest_matches_the_value_derived_from_the_decision() -> None:
    """Genesis is not a special case of the preimage, only of its values.

    `prev_hash` serialises as JSON `null` rather than being omitted: an absent
    key and a null key are different JCS objects, and picking the wrong one
    makes every genesis event in the system unverifiable while every other event
    is fine - a failure that would look like data corruption rather than a bug.
    """
    event = build_event(
        document_id=DOCUMENT_ID,
        event_type="document_ingested",
        actor=ACTOR,
        payload=PAYLOAD,
        recorded_at=RECORDED_AT,
        tip=None,
    )
    assert event.seq == 0
    assert event.prev_hash is None
    assert event.hash.hex() == GOLDEN_GENESIS_DIGEST
    assert '"prev_hash":null' in canonical_text(_preimage_of(event))


def test_a_payload_embeds_as_an_object_not_as_its_canonical_string() -> None:
    """D-13's named trap, asserted in all three directions.

    Positively: the envelope contains the payload's canonical text as a literal
    substring, which is only possible because JCS is context-free - a
    sub-value's serialisation never depends on its parent. Negatively: the
    digest is not the one the string reading produces. And structurally: no
    escaped quote appears after the `payload` key, which is what the string
    reading would inevitably introduce.
    """
    event = _golden_event()
    envelope = canonical_text(_preimage_of(event))

    assert event.payload_canonical == PAYLOAD_CANONICAL
    assert f'"payload":{PAYLOAD_CANONICAL}' in envelope
    assert '"payload":"' not in envelope
    assert event.hash.hex() != WRONG_STRING_EMBEDDED_DIGEST


def test_a_the_preimage_carries_exactly_d13s_eight_keys() -> None:
    """The object's keys *are* the enumeration, per D-13 section 2.

    The sketch this replaced ended in `...`, which let two implementations
    disagree about the covered field set while both believing they followed it.
    A test over the exact key set is what stops that from coming back: adding a
    field silently is the failure, so adding one has to be loud.
    """
    preimage = _preimage_of(_golden_event())
    assert set(preimage) == {
        "v",
        "stream",
        "seq",
        "event_type",
        "actor",
        "recorded_at",
        "prev_hash",
        "payload",
    }


def test_a_the_version_marker_is_inside_the_preimage() -> None:
    """D-13 section 3, "the load-bearing part".

    Without `v` inside the hash, changing the field set later invalidates every
    existing chain because a verifier cannot recompute historical digests. The
    marker has to exist before the first event is ever emitted, which is now.
    """
    preimage = _preimage_of(_golden_event())
    assert preimage["v"] == ENVELOPE_VERSION == 1
    assert '"v":1' in canonical_text(preimage)


def test_a_the_prev_hash_is_lowercase_hex() -> None:
    """D-13 section 2 says "lowercase-hex or null", and case changes the digest.

    `bytes.hex()` is lowercase, so this is free - which is exactly why it is
    worth pinning, because nothing else would notice if a later edit reached for
    a formatter that upper-cased it.
    """
    preimage = _preimage_of(_golden_event())
    assert preimage["prev_hash"] == "aa" * 32


def test_a_payload_key_order_does_not_reach_the_digest() -> None:
    """Canonicalisation's whole job, stated as a property.

    Python preserves dict insertion order, so two callers building the same
    payload in different orders would otherwise produce different digests for
    identical data - the defect JCS is here to remove.
    """
    shuffled: JsonObject = {
        "source": {"tier": "system_of_record", "chunk_id": "chunk-open"},
        "value": 0.19,
        "field": "price_per_watt_dc",
    }
    other = build_event(
        document_id=DOCUMENT_ID,
        event_type="extraction",
        actor=ACTOR,
        payload=shuffled,
        recorded_at=RECORDED_AT,
        tip=ChainTip(seq=0, hash=PARENT),
    )
    assert other.hash.hex() == GOLDEN_DIGEST


# --- D-13 section 4: the caller-supplied timestamp -----------------------------


def test_a_recorded_at_is_rfc_3339_utc_with_a_z_offset() -> None:
    """D-13 section 4's literal format, `2026-08-06T15:04:05.000000Z`.

    `datetime.isoformat()` emits `+00:00` and omits `.000000` when the
    microsecond field is zero, so both halves of this are things the standard
    library would have got wrong.
    """
    assert format_recorded_at(RECORDED_AT) == "2026-08-06T15:04:05.000000Z"


def test_a_recorded_at_converts_rather_than_assumes() -> None:
    """One instant must have one representation, whatever zone the caller used.

    Aware datetimes compare by instant, so `17:04:05+02:00` and `15:04:05Z` are
    a single value written two ways. Formatting without converting would give
    that one value two digests.
    """
    berlin = datetime(2026, 8, 6, 17, 4, 5, tzinfo=timezone(timedelta(hours=2)))
    assert berlin == RECORDED_AT
    assert format_recorded_at(berlin) == format_recorded_at(RECORDED_AT)


def test_a_a_naive_recorded_at_is_refused() -> None:
    """A naive datetime names no instant, so no honest encoding of it exists.

    Assuming UTC would be worse than refusing: it silently records a timestamp
    the caller did not mean into a field whose entire purpose is being
    tamper-evident.
    """
    with pytest.raises(ValueError, match="naive"):
        format_recorded_at(datetime(2026, 8, 6, 15, 4, 5))  # noqa: DTZ001


def test_a_recorded_at_agrees_with_the_d14_encoder() -> None:
    """C4 and C6 format the same instant identically, and that is not a coincidence.

    D-13 section 4 and D-14 independently specify RFC 3339 UTC with six
    microsecond digits. Two hand-written formatters agreeing today is not a
    property; this test is what makes it one, so that a drift in either decision
    surfaces here rather than as two artifacts disagreeing about one timestamp.

    They stay separate functions because the *encodings* differ - D-14 wraps the
    string in a `$datetime` tag to keep its value domain injective, and D-13
    hashes it bare - so sharing the function would couple two contracts that
    only happen to agree on this one detail.
    """
    encoded = encode_value(RECORDED_AT)
    assert isinstance(encoded, dict)
    assert encoded["$datetime"] == format_recorded_at(RECORDED_AT)


# --- D-13 section 5: the v1 taxonomy -------------------------------------------


def _ddl_event_types() -> frozenset[str]:
    """The `event_type` CHECK's values, read out of `sql/07_audit_event.sql`.

    Read rather than restated so the Python constant cannot drift from the
    constraint that actually rejects a bad INSERT. This test owns no part of
    `sql/`; it only refuses to let the two disagree in silence.
    """
    ddl = (pathlib.Path(__file__).parent.parent / "sql" / "07_audit_event.sql").read_text(
        encoding="utf-8"
    )
    match = re.search(r"event_type\s+text NOT NULL CHECK \(event_type IN \((.*?)\)\)", ddl, re.S)
    assert match is not None, "the event_type CHECK is no longer where this test looks for it"
    return frozenset(re.findall(r"'([a-z_]+)'", match.group(1)))


def test_a_the_v1_taxonomy_matches_the_ddl_exactly() -> None:
    """D-13 section 5: the seven values are version 1, additive-only.

    Two constants naming one taxonomy is a drift waiting to happen, and drift in
    the *removing* direction is what a chain cannot tolerate. Equality, not
    containment: a value the library would emit but the CHECK would reject is a
    runtime failure, and a value the CHECK allows but the library refuses to
    build is an event nobody can write.
    """
    assert EVENT_TYPES_V1 == _ddl_event_types()
    assert len(EVENT_TYPES_V1) == 7


def test_a_an_unknown_event_type_is_refused_before_the_insert() -> None:
    """Fail in Python, where the caller is, not on a CHECK three layers down.

    The constraint would catch it either way; catching it here means the error
    names the taxonomy and the decision that owns it instead of a constraint
    name.
    """
    with pytest.raises(UnknownEventTypeError, match="workbook_composed"):
        build_event(
            document_id=DOCUMENT_ID,
            event_type="workbook_composed",
            actor=ACTOR,
            payload=PAYLOAD,
            recorded_at=RECORDED_AT,
            tip=None,
        )


# --- the stream/document invariant sql/07 makes structural ---------------------


def test_a_the_stream_form_matches_the_ddl_check() -> None:
    """`CHECK (stream = 'doc:' || document_id)`, expressed once in Python.

    A function rather than a `f"doc:{...}"` at each call site: the constraint is
    structural in the DDL precisely so a caller cannot forget it, and a second
    place that builds the string is a second place that can get it wrong.
    """
    assert stream_for_document("open-1") == "doc:open-1"
    assert document_id_for_stream("doc:open-1") == "open-1"

    event = _golden_event()
    assert event.stream == "doc:" + event.document_id


def test_a_a_stream_that_is_not_document_scoped_is_refused() -> None:
    """Decision 9: never a global chain, and the DDL makes that structural.

    Mirrored here so a caller that hand-builds a stream name gets the same
    answer from the library that it would get from the constraint.
    """
    with pytest.raises(ValueError, match="doc:"):
        document_id_for_stream("global")


# --- chain position ------------------------------------------------------------


def test_a_the_tip_decides_seq_and_prev_hash_together() -> None:
    """`CHECK ((prev_hash IS NULL) = (seq = 0))`, satisfied by construction.

    The DDL ties the two redundant ordering mechanisms together so a caller bug
    that disagrees between them fails loudly at INSERT. Deriving both from one
    `tip` argument means there is no way for a caller to disagree at all.
    """
    genesis = build_event(
        document_id=DOCUMENT_ID,
        event_type="document_ingested",
        actor=ACTOR,
        payload=PAYLOAD,
        recorded_at=RECORDED_AT,
        tip=None,
    )
    assert (genesis.seq, genesis.prev_hash) == (0, None)

    child = build_event(
        document_id=DOCUMENT_ID,
        event_type="extraction",
        actor=ACTOR,
        payload=PAYLOAD,
        recorded_at=RECORDED_AT,
        tip=ChainTip(seq=genesis.seq, hash=genesis.hash),
    )
    assert child.seq == 1
    assert child.prev_hash == genesis.hash


def test_a_a_payload_that_is_not_an_object_is_refused() -> None:
    """`payload jsonb` accepts a bare scalar; D-13's preimage names an object.

    `{...}` in the decision is not decoration - a scalar payload would still
    canonicalise and still INSERT, and the chain would be valid, so nothing
    downstream would ever report it. The narrowing has to happen here.
    """
    with pytest.raises(TypeError, match="object"):
        build_event(
            document_id=DOCUMENT_ID,
            event_type="extraction",
            actor=ACTOR,
            payload="not an object",  # type: ignore[arg-type]
            recorded_at=RECORDED_AT,
            tip=None,
        )


def _preimage_of(event: AuditEvent) -> JsonObject:
    """Rebuild an event's preimage the way a verifier would.

    Goes back through `payload_canonical` rather than reusing the payload the
    test passed in, because that is the only path a verifier has - and it is
    where the object-versus-string reading would diverge if it were wrong.
    """
    return build_preimage(
        version=ENVELOPE_VERSION,
        stream=event.stream,
        seq=event.seq,
        event_type=event.event_type,
        actor=event.actor,
        recorded_at=format_recorded_at(event.recorded_at),
        prev_hash=event.prev_hash,
        payload=json.loads(event.payload_canonical),
    )


def test_a_the_verifier_path_reproduces_the_writer_path() -> None:
    """Rebuilding from stored columns must give the stored digest back.

    This is the property H.5 depends on, asserted without a database: if the
    writer and the verifier construct the preimage differently, every chain in
    the system fails to verify on the first walk.
    """
    event = _golden_event()
    assert digest_of(_preimage_of(event)) == event.hash


def test_build_event_accepts_mapping_proxy_payload() -> None:
    """Read-only MappingProxyType payloads satisfy JsonObject = Mapping[str, JsonValue]
    and must be accepted at runtime by build_event."""
    readonly_payload: JsonObject = types.MappingProxyType({"a": 1})
    event = build_event(
        document_id=DOCUMENT_ID,
        event_type="extraction",
        actor=ACTOR,
        payload=readonly_payload,
        recorded_at=RECORDED_AT,
        tip=None,
    )
    assert event.payload_canonical == '{"a":1}'
