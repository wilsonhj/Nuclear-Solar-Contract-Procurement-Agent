"""H.5 - what the chain verifier detects, and what it provably cannot.

Every test here runs against rows held in memory rather than against a server.
That is not a convenience: verification is a pure function of the stored
columns, and keeping it one means the guarantee - *an edit that is not re-sealed
is detected* - is checkable everywhere, including in an environment with no
database at all. Decision 9's broader wording, "a superuser can edit a row but
cannot make the chain re-verify", is not what these tests assert, because it is
not true; `verify`'s module docstring carries the correction.

The "what a chain walk provably cannot detect" section is the honest half. A
truncated tail is undetectable, and so is any re-sealed suffix - truncation is
just the case where the rewritten suffix is empty. Both are asserted rather than
argued, because a limitation that only exists in a comment is one nobody knows
they are relying on, and because a test that merely asserted a caveat *existed*
would stay green long after the caveat became false.

The other half of the honesty is `payload_not_canonical`. D-13 hashes the parsed
payload object, so the stored bytes of `payload_canonical` are not covered by
the digest and five distinct stored strings share one. That check is the only
thing pinning them, D-13 does not require it, and
`PAYLOAD_FORGERIES_THE_DIGEST_CANNOT_SEE` exists so that deleting it cannot be a
quiet change.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import types
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from procurement_agent.audit import (
    ENVELOPE_VERSION,
    ChainTip,
    DefectKind,
    StoredEvent,
    build_event,
    build_preimage,
    digest_of,
    format_recorded_at,
    verify_events,
)
from procurement_agent.audit.verify import main as verify_main

STREAM = "doc:open-1"
RECORDED_AT = datetime(2026, 8, 6, 15, 4, 5, tzinfo=UTC)


def _chain(length: int = 3, document_id: str = "open-1") -> list[StoredEvent]:
    """A well-formed chain of `length` events, built by the writer's own path."""
    stored: list[StoredEvent] = []
    tip: ChainTip | None = None
    for index in range(length):
        event = build_event(
            document_id=document_id,
            event_type="document_ingested" if index == 0 else "extraction",
            actor=f"worker-{index}",
            payload={"index": index, "value": 0.19},
            recorded_at=RECORDED_AT,
            tip=tip,
        )
        stored.append(
            StoredEvent(
                event_id=index + 1,
                seq=event.seq,
                prev_hash=event.prev_hash,
                hash=event.hash,
                event_type=event.event_type,
                actor=event.actor,
                payload_canonical=event.payload_canonical,
                recorded_at=event.recorded_at,
            )
        )
        tip = ChainTip(seq=event.seq, hash=event.hash)
    return stored


def _kinds(rows: list[StoredEvent]) -> set[DefectKind]:
    return {defect.kind for defect in verify_events(STREAM, rows).defects}


# --- the chain verifies when nothing has been touched --------------------------


def test_a_an_untouched_chain_verifies() -> None:
    report = verify_events(STREAM, _chain())
    assert report.ok
    assert report.events == 3
    assert report.defects == ()


def test_a_an_empty_stream_verifies_vacuously() -> None:
    """Zero events is not a defect, and saying so is not a formality.

    The verification CLI walks every stream it finds; a stream that reports
    "failed" because nothing has been written to it yet would train its operator
    to ignore failures, which is the only way this tool stops working.
    """
    report = verify_events(STREAM, [])
    assert report.ok
    assert report.events == 0


# --- each hashed field is actually covered by the digest -----------------------


#: One edit per field D-13 section 2 puts inside the preimage. Written as
#: callables rather than as (name, value) pairs so each is type-checked against
#: `StoredEvent` - a `**{name: value}` splat is exactly the construct that would
#: let a typo name a field that does not exist and still pass, by editing
#: nothing and then failing for the wrong reason.
HASHED_FIELD_EDITS = [
    ("actor", lambda row: dataclasses.replace(row, actor="someone-else")),
    ("event_type", lambda row: dataclasses.replace(row, event_type="resolution")),
    ("seq", lambda row: dataclasses.replace(row, seq=99)),
    (
        "recorded_at",
        lambda row: dataclasses.replace(
            row, recorded_at=datetime(2026, 8, 6, 15, 4, 6, tzinfo=UTC)
        ),
    ),
]


@pytest.mark.parametrize(
    ("field", "edit"), HASHED_FIELD_EDITS, ids=[e[0] for e in HASHED_FIELD_EDITS]
)
def test_a_editing_a_hashed_field_breaks_the_digest(
    field: str, edit: Callable[[StoredEvent], StoredEvent]
) -> None:
    """One case per field D-13 section 2 puts inside the preimage.

    Parameterised rather than written once because the failure being guarded
    against is a field quietly dropping *out* of the preimage - which no test of
    a single field would notice, and which the `...` in the sketch D-13 replaced
    made easy to do by accident.
    """
    rows = _chain()
    rows[1] = edit(rows[1])
    assert DefectKind.HASH_MISMATCH in _kinds(rows), f"{field} is not covered by the digest"


def test_a_editing_the_payload_breaks_the_digest() -> None:
    """The row content NFR-03 and NFR-02 both care about.

    `payload` is a generated column, so a tamperer has to go through
    `payload_canonical` - which is what the digest is recomputed from.
    """
    rows = _chain()
    tampered = json.loads(rows[1].payload_canonical)
    tampered["value"] = 0.11
    rows[1] = dataclasses.replace(
        rows[1], payload_canonical=json.dumps(tampered, separators=(",", ":"))
    )
    assert DefectKind.HASH_MISMATCH in _kinds(rows)


def test_a_the_recorded_at_round_trip_is_exact() -> None:
    """The hashed timestamp is a *string*; the column stores an *instant*.

    D-13 section 4 justifies pinning the format by two callers otherwise
    disagreeing. The stronger reason is this one: `recorded_at` is `timestamptz`,
    so the bytes that were hashed are not stored anywhere and the verifier has
    to reconstruct them from the instant. That is only possible because the
    format is a total function of the instant - which is what makes the pin
    load-bearing rather than tidy.
    """
    rows = _chain(1)
    assert format_recorded_at(rows[0].recorded_at) == "2026-08-06T15:04:05.000000Z"
    assert verify_events(STREAM, rows).ok


# --- the shape of the chain itself ---------------------------------------------


def test_a_a_broken_link_is_reported() -> None:
    """The linkage `audit_event_parent_exists` enforces at write time.

    Checked again here because the FK is enforced by a server that a superuser
    can also disable - Decision 9's whole argument for the chain is that it
    survives that, and it only survives it if something recomputes the link.
    """
    rows = _chain()
    rows[2] = dataclasses.replace(rows[2], prev_hash=bytes.fromhex("ff" * 32))
    assert DefectKind.BROKEN_LINK in _kinds(rows)


def test_a_a_seq_gap_is_reported() -> None:
    """`seq` is hashed, so a gap is a writer bug rather than tampering.

    The DDL constrains `seq` to be unique and to be zero exactly at genesis, but
    never to be contiguous. A writer that skipped a number would produce a chain
    that links correctly and numbers wrongly, and nothing else in the system
    would ever say so.
    """
    rows = _chain()
    rows[2] = _rehash(dataclasses.replace(rows[2], seq=7))
    assert DefectKind.SEQ_GAP in _kinds(rows)


def test_a_a_chain_that_does_not_start_at_genesis_is_reported() -> None:
    """A stream whose first row has a parent is a stream missing its head.

    Deleting the genesis row is the cheapest way to make a chain shorter, and
    every remaining link still verifies - so the *absence* has to be what is
    detected.
    """
    rows = _chain()[1:]
    assert DefectKind.BAD_GENESIS in _kinds(rows)


def test_a_an_event_type_outside_the_v1_taxonomy_is_reported() -> None:
    """D-13 section 5 forbids removing a value; this catches an addition, too.

    A row carrying a type this library does not know means either the CHECK was
    widened without amending D-13, or the row predates a removal that was not
    supposed to happen. Both are worth a line in the report.
    """
    rows = _chain()
    rows[1] = _rehash(dataclasses.replace(rows[1], event_type="workbook_composed"))
    assert DefectKind.UNKNOWN_EVENT_TYPE in _kinds(rows)


def test_a_non_canonical_payload_text_is_reported_even_though_it_still_hashes() -> None:
    """The envelope is non-injective, and this check is the only thing pinning it.

    D-13 embeds the *parsed* payload object in the preimage, so any JSON text
    with the same parse yields the same digest - meaning the exact bytes of
    `payload_canonical` are **not** covered by the hash, despite the column
    being named for being canonical. The digest below still matches; only the
    separate canonicality check notices.

    **`payload_not_canonical` is required for the integrity claim, not an
    extra.** D-13 does not mandate it, so a second implementation written against
    the decision alone would accept every forgery in
    `PAYLOAD_FORGERIES_THE_DIGEST_CANNOT_SEE` below. Because `payload jsonb` is
    `GENERATED ALWAYS` from this column and jsonb preserves numeric literal text,
    a rewrite that survives the digest is a rewrite that changes what every human
    and every SQL query reads. Delete this check and the chain stops covering the
    stored bytes at all.
    """
    rows = _chain()
    loosened = json.dumps(json.loads(rows[1].payload_canonical), separators=(", ", ": "))
    rows[1] = dataclasses.replace(rows[1], payload_canonical=loosened)

    kinds = _kinds(rows)
    assert DefectKind.PAYLOAD_NOT_CANONICAL in kinds
    assert DefectKind.HASH_MISMATCH not in kinds, (
        "the digest is computed over the parsed object, so it cannot see this"
    )


#: Five stored byte strings that collapse onto one digest, honest form first.
#: Each right-hand text is a rewrite an attacker can make to a row that has
#: already been written, leaving the stored digest correct.
#:
#: The last two are the ones worth staring at. `{"a": 1 }` shows the digest
#: ignores framing entirely; `{"a":1,"a":2}` shows it ignores a *duplicate key*,
#: so the bytes a reader is shown and the value that was hashed need not even
#: have the same shape.
PAYLOAD_FORGERIES_THE_DIGEST_CANNOT_SEE = [
    ("float-for-int", '{"a":1}', '{"a":1.0}'),
    ("negative-zero", '{"a":0}', '{"a":-0.0}'),
    ("exponent", '{"a":1}', '{"a":1E0}'),
    ("whitespace", '{"a":1}', '{"a": 1 }'),
    ("duplicate-key", '{"a":2}', '{"a":1,"a":2}'),
]


@pytest.mark.parametrize(
    ("name", "honest", "forged"),
    PAYLOAD_FORGERIES_THE_DIGEST_CANNOT_SEE,
    ids=[case[0] for case in PAYLOAD_FORGERIES_THE_DIGEST_CANNOT_SEE],
)
def test_a_payload_rewrite_the_digest_cannot_see_is_caught_by_the_canonicality_check(
    name: str, honest: str, forged: str
) -> None:
    """Pins `payload_not_canonical` as load-bearing, one forgery at a time.

    The row under test is the forgery written out literally: a digest sealed over
    the *honest* text, and the *forged* text stored beside it. That is what an
    attacker leaves behind after one UPDATE to `payload_canonical`.

    Three assertions, and the middle one is the falsifiable one. It says the
    digest recomputation really is blind to the swap; if the preimage ever starts
    covering the stored bytes, the recomputed digest stops matching, this goes
    red, and whoever made that change learns they have re-based every chain ever
    written - the irreversible step D-13 names. The third says the canonicality
    check is what closes the gap today, so deleting it turns five accepted
    forgeries green.
    """
    assert _sealed_row(honest).hash == _sealed_row(forged).hash, (
        f"{name}: two texts, one digest - this is the collision being pinned"
    )

    row = dataclasses.replace(_sealed_row(honest), payload_canonical=forged)
    kinds = _kinds([row])
    assert DefectKind.HASH_MISMATCH not in kinds, (
        f"{name}: the digest now distinguishes {honest} from {forged}, so the preimage "
        "changed - every chain ever written no longer verifies"
    )
    assert DefectKind.PAYLOAD_NOT_CANONICAL in kinds, (
        f"{name}: {forged} is stored, {honest} is what was hashed, and nothing said so"
    )


def test_a_value_rewrite_that_survives_the_digest_still_changes_what_sql_reads() -> None:
    """The concrete reason the check above is integrity and not housekeeping.

    `payload jsonb GENERATED ALWAYS AS (payload_canonical::jsonb)` preserves the
    numeric literal text it is given, so rewriting `400` to `400.0` leaves a row
    that reads `400.0` to every human and every `payload->>'value'` query while
    the digest still covers `400`. The digest is silent; only
    `payload_not_canonical` is not.
    """
    rows = _chain(1)
    rows[0] = dataclasses.replace(rows[0], payload_canonical='{"index":0,"value":400.0}')
    honest = dataclasses.replace(rows[0], payload_canonical='{"index":0,"value":400}')

    assert _rehash(honest).hash == _rehash(rows[0]).hash, "the digest sees the rewrite"
    kinds = _kinds([_rehash(rows[0])])
    assert kinds == {DefectKind.PAYLOAD_NOT_CANONICAL}


def test_a_payload_text_that_is_not_json_is_reported() -> None:
    """`payload jsonb` makes this unreachable through the DDL, which is the point.

    A row in this state means something wrote to the table without going through
    the generated column - so the verifier must report it rather than raise, or
    one corrupt row stops the walk and hides every row after it.
    """
    rows = _chain()
    rows[1] = dataclasses.replace(rows[1], payload_canonical="{not json")
    assert DefectKind.PAYLOAD_NOT_JSON in _kinds(rows)


# --- a row the verifier cannot hash must be a finding, never an exception ------
#
# `_check_payload` degrades a *parse* failure to `payload_not_json` and says why:
# a walk that aborts "stops the walk and hides every row after it". The sibling
# case - text that parses perfectly but has no JCS form - took the abort anyway,
# and the damage is not a false pass. Poison one row and every finding on every
# *other* row is destroyed with it, along with every stream after this one.

#: Valid `jsonb`, and outside JCS's integer domain. Postgres numerics are
#: arbitrary precision, so `payload jsonb GENERATED ALWAYS AS
#: (payload_canonical::jsonb)` - the column that makes `payload_not_json`
#: unreachable through the DDL - does **not** make this unreachable.
UNCANONICALISABLE_PAYLOAD = '{"value":10000000000000000000}'


def test_a_payload_with_no_canonical_form_is_reported_rather_than_raised() -> None:
    """The case the parse guard's own reasoning covers but its `except` did not.

    `json.loads` accepts this text and returns an object, so the parse guard
    passes it through; `canonical_text` on that object then raises out of
    `verify_events`. Everything the module promises - "collects defects rather
    than raising on the first", a report an operator can read - is lost for the
    whole run, not just for this row.
    """
    rows = _chain()
    rows[1] = dataclasses.replace(rows[1], payload_canonical=UNCANONICALISABLE_PAYLOAD)
    assert DefectKind.PAYLOAD_NOT_REPRESENTABLE in _kinds(rows)


def test_a_poisoned_row_does_not_destroy_the_evidence_on_a_tampered_one() -> None:
    """Why the abort is an integrity defect and not merely an ergonomic one.

    A tamperer who edits one row and poisons another gets the *tamper* finding
    suppressed: the walk unwinds before any `ChainDefect` is returned, so a
    report that would have named seq 1 names nothing at all. The exit code still
    reddens CI, which is why this survived - but an operator reading the output
    loses the only pointer to the row that was actually changed.
    """
    rows = _chain(4)
    rows[1] = dataclasses.replace(rows[1], actor="mallory")
    rows[3] = dataclasses.replace(rows[3], payload_canonical=UNCANONICALISABLE_PAYLOAD)

    defects = verify_events(STREAM, rows).defects
    assert DefectKind.HASH_MISMATCH in {defect.kind for defect in defects}, (
        "the row-1 tamper evidence was destroyed by the row-3 poison"
    )
    assert {defect.seq for defect in defects} == {1, 3}


def test_a_naive_recorded_at_is_reported_rather_than_raised() -> None:
    """`format_recorded_at` raises by design; the verifier must absorb it.

    Refusing a naive datetime is right for the *writer* - it names no instant, so
    no encoding of it is honest. But `verify_events` is public and pure, and the
    verifier's job on a row it cannot hash is to say so, not to abandon the walk.
    `timestamptz` makes this unreachable from the table, exactly as the parse
    failure above is unreachable, and that is the same reason it must be handled.
    """
    rows = _chain()
    rows[1] = dataclasses.replace(rows[1], recorded_at=datetime(2026, 8, 6, 15, 4, 5))
    assert DefectKind.DIGEST_NOT_RECOMPUTABLE in _kinds(rows)


def test_a_seq_beyond_the_jcs_integer_domain_is_reported_rather_than_raised() -> None:
    """The third hashed field with a domain narrower than its column.

    `seq bigint` reaches 2**63-1; JCS stops at 2**53-1. So `seq`, like `payload`
    and `recorded_at`, is a value the DDL will store and the canonicaliser will
    refuse - which is the general shape of this defect, and the reason the guard
    belongs around the digest recomputation rather than around any one field.
    """
    rows = _chain()
    rows[1] = dataclasses.replace(rows[1], seq=2**53)
    assert DefectKind.DIGEST_NOT_RECOMPUTABLE in _kinds(rows)


def test_a_every_defect_names_the_row_it_was_found_on() -> None:
    """A report that says "failed" and not "where" costs an operator the walk.

    The chain can be long, and the row that fails is the row that was edited.
    """
    rows = _chain()
    rows[1] = dataclasses.replace(rows[1], actor="someone-else")
    (defect,) = verify_events(STREAM, rows).defects
    assert defect.seq == 1
    assert defect.event_id == 2


# --- what a chain walk provably cannot detect ----------------------------------


def test_a_a_truncated_tail_is_not_detectable_and_that_is_expected() -> None:
    """The limit of the mechanism, asserted so nobody has to rediscover it.

    Dropping the last N events leaves a chain that is internally perfect. No
    hash chain can detect this without an external witness of the expected
    length, and this repo has none. What covers it is the GRANT in `sql/07`
    (no DELETE, no TRUNCATE to any role) plus the tripwire triggers - which is
    Decision 9's own position that privilege separation is the boundary and the
    chain is tamper-*evidence* layered on top, not a replacement for it.
    """
    assert verify_events(STREAM, _chain()[:2]).ok


def test_a_re_sealed_suffix_is_not_detectable_and_that_is_expected() -> None:
    """The limitation the module docstring used to understate, made executable.

    Nothing outside the chain pins the tip, so a *suffix can be rewritten in
    place and re-sealed*: edit the rows, recompute their digests forward, and the
    walk has nothing left to object to. Truncation is not the only undetectable
    edit - it is the cheapest one. The tail case below costs a forger one row and
    one digest: no re-chaining, no DELETE, no TRUNCATE, so neither the GRANT nor
    the tripwire triggers that cover truncation touch it.

    That matters most for the last `resolution` event of a stream, which D-13
    calls "the highest-repudiation-risk record in the system: the name of the
    human who shipped a workbook past unresolved conflicts". Re-attributing it is
    exactly case A.

    This is inherent to an unkeyed chain with no external witness, so it is
    asserted rather than fixed. If it ever starts failing, something now pins the
    tip - and `verify`'s stated guarantee should be widened to say so.
    """
    tail = _chain(5)
    tail[4] = _rehash(dataclasses.replace(tail[4], actor="not-the-approver"))
    assert verify_events(STREAM, tail).ok, "[A] a re-sealed tail event"

    suffix = _chain(5)
    suffix[3] = _rehash(dataclasses.replace(suffix[3], actor="mallory"))
    suffix[4] = _rehash(dataclasses.replace(suffix[4], prev_hash=suffix[3].hash))
    assert verify_events(STREAM, suffix).ok, "[B] a re-sealed two-row suffix"

    whole = _chain(5)
    parent: bytes | None = None
    for index, row in enumerate(whole):
        whole[index] = _rehash(dataclasses.replace(row, actor="mallory", prev_hash=parent))
        parent = whole[index].hash
    assert verify_events(STREAM, whole).ok, "[C] a re-sealed whole chain"

    # The control. The same edit *without* the re-seal is caught, which is what
    # confirms the three above pass because of the re-seal and not because the
    # edit was never applied.
    unsealed = _chain(5)
    unsealed[4] = dataclasses.replace(unsealed[4], actor="not-the-approver")
    assert _kinds(unsealed) == {DefectKind.HASH_MISMATCH}


def test_a_the_cli_refuses_a_stream_that_cannot_exist(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo must not come back as a clean bill of health.

    `--stream global` would otherwise select zero rows and report OK, which is
    the single worst output this tool can produce: it is indistinguishable from
    a verified chain, and it is what an operator would see after mistyping a
    document id. Checked before connecting, so this test needs no server - and
    so the answer never depends on reaching one.
    """
    monkeypatch.setenv("PROCUREMENT_DATABASE_URL", "postgresql:///unused")
    assert verify_main(["--stream", "global"]) == 2
    assert "doc:" in capsys.readouterr().out


def _rehash(row: StoredEvent) -> StoredEvent:
    """Recompute a row's own digest after editing it, so it is self-consistent.

    Used where the point of the test is a *structural* defect rather than a
    digest one: without this the row would fail on `hash_mismatch` first and the
    test would pass for the wrong reason. It goes through `build_preimage`
    rather than `build_event` deliberately - this is a forger's path, not a
    writer's, and the writer must not grow a `seq` override or a taxonomy escape
    hatch merely so a test can take it.
    """
    preimage = build_preimage(
        version=ENVELOPE_VERSION,
        stream=STREAM,
        seq=row.seq,
        event_type=row.event_type,
        actor=row.actor,
        recorded_at=format_recorded_at(row.recorded_at),
        prev_hash=row.prev_hash,
        payload=json.loads(row.payload_canonical),
    )
    return dataclasses.replace(row, hash=digest_of(preimage))


def _sealed_row(payload_text: str) -> StoredEvent:
    """A genesis row whose digest is honestly computed over `payload_text`'s parse.

    The forger's row: the stored digest is genuine, so the chain walk has nothing
    to object to unless something looks at the stored *bytes*.
    """
    return _rehash(
        StoredEvent(
            event_id=1,
            seq=0,
            prev_hash=None,
            hash=b"\x00" * 32,
            event_type="document_ingested",
            actor="worker-0",
            payload_canonical=payload_text,
            recorded_at=RECORDED_AT,
        )
    )


# --- the CLI's "verified nothing" outputs ---------------------------------------


class _EmptyCursor:
    """Every query returns no rows - an empty log, or a stream that is not there."""

    def fetchone(self) -> None:
        return None


class _EmptyConn:
    def execute(self, query: object, params: object = None) -> _EmptyCursor:
        return _EmptyCursor()

    def __enter__(self) -> _EmptyConn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def _empty_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reachable server holding nothing, injected as `psycopg`.

    `verify.main` imports psycopg inside the function so the module stays
    importable without the `store` extra, which is also what makes this
    substitutable without a database.
    """
    fake = types.ModuleType("psycopg")
    fake.connect = lambda *a, **k: _EmptyConn()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    monkeypatch.setenv("PROCUREMENT_DATABASE_URL", "postgresql:///unused")


def test_a_named_stream_that_is_not_there_is_a_failure_not_an_ok(
    _empty_server: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The half `test_a_stream_that_cannot_exist_is_refused` does not cover.

    That test's docstring names the hazard correctly - "what an operator would
    see after mistyping a document id" - but `document_id_for_stream` only
    validates the `doc:` **prefix**. Document ids are UUIDs, so the realistic
    typo is *inside* the id, which passes the prefix check, selects zero rows,
    and produced `OK (0 events)` with exit 0: the one output the comment above
    that check says an operator must never get.

    A cron pinned to a single document would report success forever.
    """
    code = verify_main(["--stream", "doc:11111111-1111-4111-8111-111111111112"])
    out = capsys.readouterr().out
    assert "OK" not in out, out
    assert code != 0


def test_an_empty_log_says_so_rather_than_printing_nothing(
    _empty_server: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no `--stream` and nothing discovered, `main` printed **nothing at
    all** and exited 0 - indistinguishable from a clean pass over a real log.

    Exit 0 stays: an empty log is a legitimate state for a fresh deployment, and
    failing CI on it would be wrong. What must change is the silence, because
    "I verified nothing" and "I verified everything and it was fine" had the
    same output.
    """
    code = verify_main([])
    out = capsys.readouterr().out
    assert out.strip(), "printed nothing at all"
    assert "no audit streams" in out.lower()
    assert code == 0


# --- one unhashable row must not cost the rest of the run -----------------------


class _ScriptedCursor:
    """Replays a fixed row list through the one method `AuditCursor` promises."""

    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = list(rows)

    def fetchone(self) -> tuple[object, ...] | None:
        return self._rows.pop(0) if self._rows else None


def _as_selected(event: StoredEvent) -> tuple[object, ...]:
    """`StoredEvent` back into the column order `verify._EVENTS_SQL` selects."""
    return (
        event.event_id,
        event.seq,
        event.prev_hash,
        event.hash,
        event.event_type,
        event.actor,
        event.payload_canonical,
        event.recorded_at,
    )


class _ScriptedConn:
    """A reachable server holding the streams it is given, in the order given."""

    def __init__(self, log: dict[str, list[StoredEvent]]) -> None:
        self._log = log

    def execute(self, query: object, params: tuple[str, ...] | None = None) -> _ScriptedCursor:
        if params is not None:
            (stream,) = params
            return _ScriptedCursor([_as_selected(event) for event in self._log.get(stream, [])])
        if "DISTINCT" in str(query):
            return _ScriptedCursor([(name,) for name in self._log])
        return _ScriptedCursor([])  # the `SET app.allow_restricted` statement

    def __enter__(self) -> _ScriptedConn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_a_stream_the_verifier_cannot_hash_does_not_stop_the_streams_after_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The blast radius of one poisoned row is a whole verification run.

    `main` walks the streams it discovers in one loop, so a `verify_stream` that
    raises takes the process down before any later stream is read. An attacker
    who wants a tampered row in `doc:open-2` unexamined does not have to touch
    it: poisoning any row in any alphabetically earlier stream is enough, and
    costs one UPDATE to a column the DDL will happily accept.

    Both streams must appear in the output, and the tamper in the second must be
    named - not merely counted.
    """
    poisoned = _chain(2, document_id="open-1")
    poisoned[1] = dataclasses.replace(poisoned[1], payload_canonical=UNCANONICALISABLE_PAYLOAD)
    tampered = _chain(2, document_id="open-2")
    tampered[1] = dataclasses.replace(tampered[1], actor="mallory")

    log = {"doc:open-1": poisoned, "doc:open-2": tampered}
    fake = types.ModuleType("psycopg")
    fake.connect = lambda *a, **k: _ScriptedConn(log)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    monkeypatch.setenv("PROCUREMENT_DATABASE_URL", "postgresql:///unused")

    code = verify_main([])
    out = capsys.readouterr().out

    assert "doc:open-2" in out, "the run died on doc:open-1 and never reached doc:open-2"
    assert str(DefectKind.HASH_MISMATCH) in out, "the tamper in doc:open-2 was never reported"
    assert str(DefectKind.PAYLOAD_NOT_REPRESENTABLE) in out
    assert code == 1


def test_cli_falls_back_to_procurement_database_url_env(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The connection string comes from `PROCUREMENT_DATABASE_URL`. `--dsn` is
    refused so the secret never appears in the process list. With neither the
    flag nor the variable, the CLI exits 2."""
    monkeypatch.delenv("PROCUREMENT_DATABASE_URL", raising=False)
    assert verify_main([]) == 2
    err_out = capsys.readouterr()
    assert "PROCUREMENT_DATABASE_URL" in (err_out.out + err_out.err)

    captured_dsn: list[str] = []

    def _fake_connect(dsn: str, **kwargs: object) -> _EmptyConn:
        captured_dsn.append(dsn)
        return _EmptyConn()

    fake = types.ModuleType("psycopg")
    fake.connect = _fake_connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    monkeypatch.setenv("PROCUREMENT_DATABASE_URL", "postgresql://env-host/procurement")

    assert verify_main([]) == 0
    assert captured_dsn == ["postgresql://env-host/procurement"]
    assert verify_main(["--dsn", "postgresql://user:secret@localhost/db"]) == 2
    refused = capsys.readouterr()
    assert "--dsn is refused" in (refused.out + refused.err)
    assert captured_dsn == ["postgresql://env-host/procurement"]
