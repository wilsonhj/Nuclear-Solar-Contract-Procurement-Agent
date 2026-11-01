"""H.5 - chain verification, and the CLI that runs it.

Decision 9 leans on this chain as "the only mechanism that survives the
superuser bypass - a superuser can edit a row but cannot make the chain
re-verify". **That claim is too broad, and this module does not make it.** What
is actually guaranteed is narrower:

    An edit that is not re-sealed is detected. Every field D-13 section 2 puts
    inside the preimage is recomputed from the stored columns, so changing any
    of them - or the links between rows - and leaving the digests alone produces
    a report that names the row.

That is worth having, and it is not tamper-proofing. The digest is unkeyed and
the algorithm is public, so anyone who can edit a row can also recompute its
digest; and because **nothing outside the chain pins the tip**, recomputing
forward from the edited row to the end is always available. See "what it cannot
detect" below.

**Verification is a pure function of the rows** (`verify_events`), with the
database reduced to a `SELECT`. That keeps every property testable without a
server, and it means the CLI is a transport rather than a place where checks can
hide. `verify_events` never raises: a row it cannot hash becomes a defect, since
unwinding the walk would discard the findings already made on every other row -
and on every stream after this one.

**What it cannot detect, stated plainly.** Any *suffix rewritten in place and
re-sealed*. Edit the rows, recompute their digests forward, and the chain is
internally perfect again. A truncated tail is the special case where the
rewritten suffix is empty, and it is not the cheapest one: re-attributing the
last event costs one row edited and one digest recomputed - no re-chaining, no
DELETE, no TRUNCATE. So the GRANT in `sql/07` (no DELETE, no TRUNCATE, to any
role) and the tripwire triggers, which do cover truncation, do not cover this.

The event this most matters for is the last `resolution` of every stream, which
D-13 calls "the highest-repudiation-risk record in the system: the name of the
human who shipped a workbook past unresolved conflicts" - and which is by
construction at the tip, where the rewrite is cheapest.

This is inherent to an unkeyed hash chain with no external witness, so it is a
limit of the design and not a defect in `verify_events`; the tests assert it
rather than argue it. Decision 9's position stands as the mitigation -
privilege separation is the boundary and the chain is tamper-*evidence* layered
on top - and the durable answer is shipping this log to a write-once sink or
otherwise witnessing the tip, which `sql/07` also says and which is not
attempted here. That remedy covers the whole family, not truncation alone.

⚠️ `sql/07_audit_event.sql` still carries the unqualified "cannot make the chain
re-verify" wording; it needs the same correction, and this module is not the
owner of that file.

**Confidentiality.** `sql/07`'s RLS policy hides restricted documents from
`procurement_app`, so a verification run must connect as an identity entitled to
see them and set `app.allow_restricted` - "exactly as the confidentiality model
intends, and a chain walk that skipped rows would fail to verify rather than
pass quietly". The CLI sets it; a run that could not would report broken links
on every restricted stream.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from .canonical import CanonicalisationError, JsonObject, canonical_text
from .envelope import (
    EVENT_TYPES_V1,
    KNOWN_ENVELOPE_VERSIONS,
    build_preimage,
    digest_of,
    document_id_for_stream,
    format_recorded_at,
)
from .writer import AuditConnection

__all__ = [
    "ChainDefect",
    "ChainReport",
    "DefectKind",
    "StoredEvent",
    "main",
    "read_events",
    "read_streams",
    "verify_events",
    "verify_stream",
]


class DefectKind(StrEnum):
    """What a walk can find. Named rather than free text so a report is greppable.

    A `StrEnum` because these values are printed, matched in tests and would
    plausibly be logged as structured fields - all three want the member to *be*
    its own name.
    """

    HASH_MISMATCH = "hash_mismatch"
    BROKEN_LINK = "broken_link"
    SEQ_GAP = "seq_gap"
    BAD_GENESIS = "bad_genesis"
    UNKNOWN_EVENT_TYPE = "unknown_event_type"
    PAYLOAD_NOT_JSON = "payload_not_json"
    PAYLOAD_NOT_CANONICAL = "payload_not_canonical"
    PAYLOAD_NOT_REPRESENTABLE = "payload_not_representable"
    DIGEST_NOT_RECOMPUTABLE = "digest_not_recomputable"


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """One `audit.event` row, as the verifier reads it.

    `payload` is deliberately absent. It is a generated column, and `sql/07`
    says it is "never used to reverify the hash: casting back through jsonb is
    exactly the round-trip H.2's own reasoning warns against trusting as a
    canonicalisation step". Not selecting it is how that stays true.
    """

    event_id: int
    seq: int
    prev_hash: bytes | None
    hash: bytes
    event_type: str
    actor: str
    payload_canonical: str
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class ChainDefect:
    """One finding, with enough to locate the row it was found on.

    Both `seq` and `event_id`: `seq` is the chain position a reader thinks in,
    `event_id` is what a `WHERE` clause needs, and a report that gives only one
    of them costs whoever is holding the pager the translation.
    """

    kind: DefectKind
    seq: int
    event_id: int
    detail: str

    def __str__(self) -> str:
        return f"seq {self.seq} (event_id {self.event_id}): {self.kind} - {self.detail}"


@dataclass(frozen=True, slots=True)
class ChainReport:
    stream: str
    events: int
    defects: tuple[ChainDefect, ...]

    @property
    def ok(self) -> bool:
        return not self.defects


def verify_events(stream: str, rows: Sequence[StoredEvent]) -> ChainReport:
    """Recompute every digest and every link in `rows`, in `seq` order.

    Collects defects rather than raising on the first: a walk that stops at the
    first bad row hides every row after it, and "how far does the damage go" is
    the first question anyone asks. An empty stream is not a defect - the CLI
    walks every stream it finds, and a stream reported as failing because
    nothing has been written to it yet is how an operator learns to ignore this
    tool.

    **This function does not raise for any `StoredEvent` it can be handed**, and
    that is a property rather than politeness. A row whose hashed fields have no
    JCS form used to unwind the walk, which meant one such row destroyed the
    tamper evidence on every other row in the report and stopped every later
    stream being verified at all - a strictly better outcome for a tamperer than
    being caught. Both remaining ways to reach it are reported per row instead:
    `payload_not_representable` and `digest_not_recomputable`.
    """
    defects: list[ChainDefect] = []
    previous: StoredEvent | None = None

    for row in sorted(rows, key=lambda event: event.seq):
        payload = _check_payload(row, defects)

        if row.event_type not in EVENT_TYPES_V1:
            defects.append(
                ChainDefect(
                    DefectKind.UNKNOWN_EVENT_TYPE,
                    row.seq,
                    row.event_id,
                    f"{row.event_type!r} is not in C4's v1 taxonomy; either the CHECK was "
                    "widened without amending D-13, or a value was removed",
                )
            )

        defects.extend(_check_position(row, previous))
        if payload is not None:
            defects.extend(_check_digest(stream, row, payload))
        previous = row

    return ChainReport(stream=stream, events=len(rows), defects=tuple(defects))


def _check_payload(row: StoredEvent, defects: list[ChainDefect]) -> JsonObject | None:
    """Parse `payload_canonical`, and check it deserves the name.

    Returns `None` when the payload cannot be used to recompute a digest - it
    will not parse, or it parses to something with no JCS form. Both are
    reported and the walk carries on to the next row rather than aborting; see
    `_check_digest` for why that is an integrity property and not a nicety.

    **The canonicality check is required for the integrity claim, not an extra.**
    The preimage embeds the *parsed* payload object, so any JSON text with the
    same parse produces the same digest: the exact bytes of `payload_canonical`
    are not covered by the hash, despite the column being named for being
    canonical. `{"a":1}`, `{"a":1.0}`, `{"a":1E0}`, `{"a": 1 }` and `{"a":1,"a":2}`
    are five distinct stored strings under one digest.

    That is a forgery risk, and calling it cosmetic was wrong. `payload jsonb` is
    `GENERATED ALWAYS AS (payload_canonical::jsonb)` and jsonb preserves the
    numeric literal text it is given, so an attacker who rewrites `"value":400`
    to `"value":400.0` leaves a row that reads `400.0` to every human and every
    SQL query while the digest still covers `400`. **D-13 does not mandate this
    check**, so an implementation written against the decision alone would accept
    all five; `tests/test_audit_verify.py` pins each one so removing it cannot be
    a quiet change. The digest alone does not distinguish them and - because
    changing the preimage would re-base every chain ever written - it is not
    going to start.
    """
    try:
        parsed = json.loads(row.payload_canonical)
    except ValueError as exc:
        defects.append(
            ChainDefect(
                DefectKind.PAYLOAD_NOT_JSON,
                row.seq,
                row.event_id,
                f"payload_canonical does not parse as JSON ({exc}); the generated `payload` "
                "column makes this unreachable through the DDL, so something wrote around it",
            )
        )
        return None

    if not isinstance(parsed, dict):
        defects.append(
            ChainDefect(
                DefectKind.PAYLOAD_NOT_JSON,
                row.seq,
                row.event_id,
                f"payload_canonical is a JSON {type(parsed).__name__}, not an object; "
                "D-13's preimage embeds an object",
            )
        )
        return None

    payload: JsonObject = parsed
    try:
        canonical = canonical_text(payload)
    except CanonicalisationError as exc:
        # Parses fine, has no JCS form. The generated column does *not* make this
        # unreachable the way it makes a parse failure unreachable: Postgres
        # numerics are arbitrary precision, so `{"value":10000000000000000000}`
        # is valid `jsonb` and JCS refuses it at 2**53-1.
        defects.append(
            ChainDefect(
                DefectKind.PAYLOAD_NOT_REPRESENTABLE,
                row.seq,
                row.event_id,
                f"payload_canonical parses but has no JCS form ({exc}), so this row's digest "
                "could not be recomputed and this row is unverified. `payload jsonb` accepts "
                "values JCS refuses, so this is reachable through the DDL",
            )
        )
        return None

    if canonical != row.payload_canonical:
        defects.append(
            ChainDefect(
                DefectKind.PAYLOAD_NOT_CANONICAL,
                row.seq,
                row.event_id,
                "payload_canonical is not the JCS form of its own value. The digest cannot "
                "see this, because D-13 hashes the parsed object rather than these bytes",
            )
        )
    return payload


def _check_position(row: StoredEvent, previous: StoredEvent | None) -> list[ChainDefect]:
    """Genesis, linkage and `seq` contiguity - the shape rather than the bytes.

    Contiguity is checked even though no constraint requires it. The DDL makes
    `seq` unique and zero exactly at genesis, so a writer that skipped a number
    would produce a chain that links correctly and numbers wrongly, and nothing
    else in the system would ever say so.
    """
    if previous is None:
        if row.prev_hash is not None or row.seq != 0:
            return [
                ChainDefect(
                    DefectKind.BAD_GENESIS,
                    row.seq,
                    row.event_id,
                    f"the first event in this stream has seq {row.seq} and "
                    f"{'a' if row.prev_hash else 'no'} parent; a chain starts at seq 0 with "
                    "prev_hash NULL, so its head is missing",
                )
            ]
        return []

    defects: list[ChainDefect] = []
    if row.prev_hash != previous.hash:
        defects.append(
            ChainDefect(
                DefectKind.BROKEN_LINK,
                row.seq,
                row.event_id,
                f"prev_hash {_hex(row.prev_hash)} does not name the preceding event "
                f"{previous.hash.hex()}",
            )
        )
    if row.seq != previous.seq + 1:
        defects.append(
            ChainDefect(
                DefectKind.SEQ_GAP,
                row.seq,
                row.event_id,
                f"seq jumps from {previous.seq} to {row.seq}",
            )
        )
    return defects


def _check_digest(stream: str, row: StoredEvent, payload: JsonObject) -> list[ChainDefect]:
    """Recompute the digest under every envelope version this code knows.

    A loop over one version today. It is a loop because D-13 section 3 promises
    per-version dispatch and `audit.event` stores no version, so the only way to
    identify a row's version is to find the one whose preimage reproduces its
    digest - which is sound precisely because `v` is inside the hash. See
    `envelope.KNOWN_ENVELOPE_VERSIONS`.

    ⚠️ **Accept-if-any-known-version is a downgrade surface the moment this
    tuple gains a second entry.** With one version the loop cannot do anything
    but check that one. With two, a row re-sealed under the *older, narrower*
    field set verifies as happily as one written under the current envelope -
    and combined with the unpinned tail described in this module's docstring,
    that is a forger's tool rather than a compatibility feature. Whatever adds
    the second version has to decide what stops a row moving backwards through
    it; version-in-the-digest proves which version was used, not that it was
    the version that should have been used.

    **Never raises.** Three hashed fields have domains narrower than the columns
    that hold them - `payload` (checked upstream), `recorded_at`, which must name
    an instant, and `seq bigint`, which reaches 2**63-1 where JCS stops at
    2**53-1. Letting any of those unwind the walk would destroy the findings on
    every *other* row in the report, which is a worse outcome than the unverified
    row itself.
    """
    try:
        recorded_at = format_recorded_at(row.recorded_at)
        for version in KNOWN_ENVELOPE_VERSIONS:
            preimage = build_preimage(
                version=version,
                stream=stream,
                seq=row.seq,
                event_type=row.event_type,
                actor=row.actor,
                recorded_at=recorded_at,
                prev_hash=row.prev_hash,
                payload=payload,
            )
            if digest_of(preimage) == row.hash:
                return []
    except ValueError as exc:
        # `format_recorded_at` raises `ValueError` on a naive datetime, and
        # `CanonicalisationError` - a `ValueError` subclass - covers every value
        # with no JCS form. Both mean the same thing here: the preimage this row
        # would have to reproduce cannot be built, so the row is unverifiable
        # rather than forged, and saying which is the whole job.
        return [
            ChainDefect(
                DefectKind.DIGEST_NOT_RECOMPUTABLE,
                row.seq,
                row.event_id,
                f"this row's hashed fields cannot be rebuilt into a preimage ({exc}), so its "
                "digest was not checked; the column domains are wider than D-13's, so a "
                "value the DDL accepts can land here",
            )
        ]

    return [
        ChainDefect(
            DefectKind.HASH_MISMATCH,
            row.seq,
            row.event_id,
            f"stored digest {row.hash.hex()} is not reproduced by any known envelope version "
            f"{KNOWN_ENVELOPE_VERSIONS}; this row's hashed fields have been altered",
        )
    ]


def _hex(value: bytes | None) -> str:
    return "NULL" if value is None else value.hex()


# --- reading rows --------------------------------------------------------------

_STREAMS_SQL = "SELECT DISTINCT stream FROM audit.event ORDER BY stream"

_EVENTS_SQL = """
SELECT event_id, seq, prev_hash, hash, event_type, actor, payload_canonical, recorded_at
  FROM audit.event WHERE stream = %s ORDER BY seq
"""


def read_streams(conn: AuditConnection) -> list[str]:
    cursor = conn.execute(_STREAMS_SQL)
    return [str(row[0]) for row in _rows(cursor)]


def read_events(conn: AuditConnection, stream: str) -> list[StoredEvent]:
    cursor = conn.execute(_EVENTS_SQL, (stream,))
    return [
        StoredEvent(
            event_id=int(row[0]),
            seq=int(row[1]),
            prev_hash=None if row[2] is None else bytes(row[2]),
            hash=bytes(row[3]),
            event_type=str(row[4]),
            actor=str(row[5]),
            payload_canonical=str(row[6]),
            recorded_at=row[7],
        )
        for row in _rows(cursor)
    ]


def _rows(cursor: Any) -> list[Any]:
    """Drain a cursor through `fetchone`.

    `AuditConnection` promises only `fetchone`, deliberately - the same minimal
    surface that lets `tests/test_audit_writer.py` assert the caller sequence
    against a stand-in with no driver installed. Paying for that here is the
    right side of the trade: audit chains are per document and short.
    """
    rows: list[Any] = []
    while (row := cursor.fetchone()) is not None:
        rows.append(row)
    return rows


def verify_stream(conn: AuditConnection, stream: str) -> ChainReport:
    return verify_events(stream, read_events(conn, stream))


# --- the CLI --------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """Verify one stream, or every stream, and exit non-zero if any fails.

    The exit code is the interface. This runs from cron or from a release check,
    where nobody reads the output until it is already too late, so a verifier
    that prints "FAILED" and exits 0 has the longest possible time to detection
    of any failure mode available to it.
    """
    parser = argparse.ArgumentParser(
        prog="python -m procurement_agent.audit",
        description="Verify the audit.event hash chains (WP-H H.5, contract C4).",
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--stream",
        action="append",
        dest="streams",
        metavar="doc:ID",
        help="Verify only this stream. Repeatable. Defaults to every stream present.",
    )
    args = parser.parse_args(argv)
    if args.dsn is not None:
        print(
            "error: --dsn is refused because a connection string on the command line "
            "is visible in the process list. Set PROCUREMENT_DATABASE_URL instead.",
            file=sys.stderr,
        )
        return 2
    dsn = os.environ.get("PROCUREMENT_DATABASE_URL")
    if not dsn:
        print(
            "error: a database connection string is required via PROCUREMENT_DATABASE_URL.",
            file=sys.stderr,
        )
        return 2

    # A stream that cannot exist would otherwise return zero rows and report OK,
    # which is the one output an operator must never get from a typo. Checked
    # before connecting, so the answer does not depend on reaching a server.
    for stream in args.streams or ():
        try:
            document_id_for_stream(stream)
        except ValueError as exc:
            print(exc)
            return 2

    # Imported here rather than at module scope so the verification logic above
    # stays importable without the `store` extra - see writer.AuditConnection.
    try:
        import psycopg
    except ImportError:  # pragma: no cover - depends on the install, not the code
        print("psycopg is not installed; install the `store` extra to run the verifier.")
        return 2

    failed = 0
    with psycopg.connect(dsn, autocommit=True) as conn:
        # sql/07: the verifier runs as an operator identity that sets this,
        # "exactly as the confidentiality model intends".
        conn.execute("SET app.allow_restricted = 'true'")
        requested = list(args.streams or ())
        streams = requested or read_streams(conn)

        # An empty result is not a pass, and both ways of reaching one used to
        # look like one.
        #
        # The prefix check above says a stream that cannot exist "would otherwise
        # return zero rows and report OK, which is the one output an operator
        # must never get from a typo" - but `document_id_for_stream` validates
        # only the `doc:` prefix, and document ids are UUIDs. The realistic typo
        # is *inside* the id, which passes the prefix check and lands in exactly
        # the state the comment forbids. A cron pinned to one document reported
        # success forever.
        if not streams:
            # Nothing discovered, and no stream was named. Legitimate on a fresh
            # deployment, so this is not a failure - but printing nothing made
            # "I verified nothing" and "I verified everything and it was fine"
            # the same output, and only one of those is a guarantee.
            print("no audit streams found; nothing was verified.")
            return 0

        for stream in streams:
            report = verify_stream(conn, stream)
            if stream in requested and report.events == 0:
                # Asked for by name and absent. Unlike the discovery case there
                # is a caller expecting a guarantee about this specific stream,
                # and it cannot be given - whether the cause is a typo or a
                # missing chain, silence is the wrong answer.
                print(f"{stream}: NOT FOUND (0 events)")
                failed += 1
                continue
            status = "OK" if report.ok else "FAILED"
            print(f"{report.stream}: {status} ({report.events} events)")
            for defect in report.defects:
                print(f"  {defect}")
            if not report.ok:
                failed += 1

    if failed:
        print(f"{failed} of {len(streams)} stream(s) failed verification.")
    return 1 if failed else 0
