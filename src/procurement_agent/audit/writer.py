"""H.4 - the caller sequence, with the advisory lock as its own statement.

`sql/07_audit_event.sql` states the measured finding this module exists to
implement, and states why it cannot live in the schema: an advisory lock taken
inside a trigger does not work, because the statement snapshot a trigger's
queries run against is taken *before* the trigger body acquires the lock, so a
concurrent waiter still reads a stale chain tip. Eight concurrent writers
produced 42 silent forks under that design. There is no way to express "take
this lock first" as a constraint or a trigger without reintroducing the bug, so
the discipline is "enforced by code review and by the Python client library, NOT
by this schema". This is that library.

    1. SELECT pg_advisory_xact_lock(hashtext(stream)::bigint);  -- own statement
    2. SELECT hash FROM audit.event WHERE stream = $1 ORDER BY seq DESC LIMIT 1;
    3. build the envelope and the digest, in Python (H.2, H.3)
    4. INSERT INTO audit.event (...) VALUES (...);              -- same txn

**Step 1 is separate for a reason that survives refactoring.** Folding it into
step 4 as a CTE or a scalar subquery puts the lock and the read of the tip in
one statement, which gets one snapshot - the same bug in different clothes. The
statement order is asserted in `tests/test_audit_writer.py` without a server,
and its consequence is measured in `tests/test_audit_live.py` with one.

**No commit here.** `sql/07` requires the audit row to land in the same
transaction as the business write it describes: "Rollback erasing the audit
record is correct, not a bug: if the extraction rolled back, it did not happen".
So the caller owns the transaction, and this module only ever adds to it.

**This also means a future parallel caller must call `append_event` itself, once
per worker transaction - never through a shared observer.** Nothing here assumes
a single caller; a hook or callback shared across worker processes would not see
what a sibling process just committed, the same race one layer up. See
`docs/architecture.md`'s "Persistence and execution" section for the argument and
an external example of the failure mode.

**No psycopg import.** The connection is a structural `Protocol`. `psycopg` sits
behind the `store` extra for NFR-04, and C4's envelope is not swappable
infrastructure - it should be importable, and testable, without a driver
present.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, LiteralString, Protocol

from .canonical import JsonObject
from .envelope import (
    AuditEvent,
    ChainTip,
    RunAuditEvent,
    build_event,
    build_run_event,
    stream_for_document,
    stream_for_run,
)

__all__ = [
    "AuditConnection",
    "append_event",
    "append_run_event",
    "insert_event",
    "insert_run_event",
    "lock_stream",
    "read_chain_tip",
    "read_run_chain_tip",
]

#: Step 1. `hashtext` is an internal PostgreSQL function whose value is not
#: promised stable across major versions, which does not matter here: the lock
#: only has to make concurrent sessions *on one server* agree, and they always
#: read the same implementation. The cast is to `bigint` because `hashtext`
#: returns `integer` and `pg_advisory_xact_lock` has an ambiguous two-argument
#: overload that an unqualified `integer` would select.
#:
#: A hash collision between two streams costs serialisation between two
#: unrelated documents and nothing else - Decision 9 wanted cross-document
#: concurrency unconstrained, and a 32-bit key mostly delivers that. Correctness
#: does not depend on the key being unique, only on it being a function of the
#: stream.
_LOCK_SQL: LiteralString = "SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)"

#: Step 2. Ordered by `seq` rather than by `event_id`: `seq` is what the digest
#: covers and what `audit_event_seq_unique` constrains, while `event_id` is an
#: identity column whose order reflects insertion, not chain position.
_TIP_SQL: LiteralString = (
    "SELECT seq, hash FROM audit.event WHERE stream = %s ORDER BY seq DESC LIMIT 1"
)

#: Step 4. `payload` is omitted because it is `GENERATED ALWAYS`, and
#: `recorded_at` is supplied because D-13 section 4 hashes it - a defaulted
#: timestamp cannot be pre-computed by the caller.
_INSERT_SQL: LiteralString = """
INSERT INTO audit.event
    (stream, document_id, seq, prev_hash, hash, event_type, actor,
     payload_canonical, recorded_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

#: Steps 2 and 4 for `audit.run_event` (`sql/11`, P2-C7). Separate constants
#: rather than a table-name parameter: `LiteralString` exists so that no SQL in
#: this module is ever assembled from a runtime string, and keeping that true is
#: worth two more lines.
_RUN_TIP_SQL: LiteralString = (
    "SELECT seq, hash FROM audit.run_event WHERE stream = %s ORDER BY seq DESC LIMIT 1"
)

_RUN_INSERT_SQL: LiteralString = """
INSERT INTO audit.run_event
    (stream, run_id, seq, prev_hash, hash, event_type, actor,
     payload_canonical, recorded_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


class AuditCursor(Protocol):
    """The one cursor method this package uses."""

    def fetchone(self) -> Any: ...


class AuditConnection(Protocol):
    """The connection surface the caller sequence needs, and nothing more.

    A `Protocol` rather than `psycopg.Connection` for two reasons that point the
    same way. It keeps `procurement_agent.audit` importable without the `store`
    extra, which matters because every work package emits events and only some
    of them own a database handle. And it makes the statement sequence testable
    against a recording stand-in, so H.4's property is checkable in an
    environment with no PostgreSQL - which is exactly the environment where a
    lock bug would otherwise go unnoticed.
    """

    @property
    def autocommit(self) -> bool: ...

    def execute(self, query: LiteralString, params: Sequence[Any] | None = None) -> AuditCursor: ...


def lock_stream(conn: AuditConnection, stream: str) -> None:
    """Step 1: take the per-stream advisory lock, as its own statement.

    Must be called before the chain tip is read, in the same transaction as the
    INSERT that follows. `pg_advisory_xact_lock` releases when that transaction
    ends, which is the whole design: no unlock call to forget, and no lock left
    held by a crashed worker.

    Raises:
        ValueError: if the connection is in autocommit mode. This is the one
            configuration where the lock is issued and does nothing - each
            statement is its own transaction, so the lock is acquired and
            released before the tip is read, restoring the exact race while
            every statement-order check still passes. It has to be refused
            rather than documented.
    """
    if conn.autocommit:
        raise ValueError(
            "audit appends need a connection with autocommit disabled. "
            "pg_advisory_xact_lock is released at the end of its transaction, so under "
            "autocommit it is dropped before the chain tip is read - the lock appears in "
            "the statement log and prevents nothing. sql/07 also requires the audit row to "
            "commit with the business write it describes, which autocommit cannot do."
        )
    conn.execute(_LOCK_SQL, (stream,))


def read_chain_tip(conn: AuditConnection, stream: str) -> ChainTip | None:
    """Step 2: the last event in `stream`, or `None` if it has none yet.

    `None` rather than a zero-valued tip: a sentinel would give the first event
    a `prev_hash` of 32 zero bytes, which satisfies the `octet_length` CHECK and
    fails later and less legibly at the parent foreign key.

    Only meaningful under the lock from step 1. Outside it, the answer is a
    guess that another writer may already have invalidated.
    """
    row = conn.execute(_TIP_SQL, (stream,)).fetchone()
    if row is None:
        return None
    return ChainTip(seq=int(row[0]), hash=bytes(row[1]))


def insert_event(conn: AuditConnection, event: AuditEvent) -> None:
    """Step 4: append the event, in the caller's transaction.

    Does not commit - see the module docstring. The caller decides, because the
    caller is the one who knows whether the business write succeeded.
    """
    conn.execute(
        _INSERT_SQL,
        (
            event.stream,
            event.document_id,
            event.seq,
            event.prev_hash,
            event.hash,
            event.event_type,
            event.actor,
            event.payload_canonical,
            event.recorded_at,
        ),
    )


def append_event(
    conn: AuditConnection,
    *,
    document_id: str,
    event_type: str,
    actor: str,
    payload: JsonObject,
    recorded_at: datetime,
) -> AuditEvent:
    """All four steps, in order. The entry point every emitter should use.

    The pieces are public because the verifier and the load test need them
    separately, but a caller that assembles them by hand is a caller who can
    leave out step 1 - which is why this exists and why it is the documented
    path.

    Returns the event as written, so a caller can log the digest without reading
    the row back.
    """
    stream = stream_for_document(document_id)
    lock_stream(conn, stream)
    tip = read_chain_tip(conn, stream)
    event = build_event(
        document_id=document_id,
        event_type=event_type,
        actor=actor,
        payload=payload,
        recorded_at=recorded_at,
        tip=tip,
    )
    insert_event(conn, event)
    return event


def read_run_chain_tip(conn: AuditConnection, stream: str) -> ChainTip | None:
    """Step 2 for a run chain. Same contract as `read_chain_tip`."""
    row = conn.execute(_RUN_TIP_SQL, (stream,)).fetchone()
    if row is None:
        return None
    return ChainTip(seq=int(row[0]), hash=bytes(row[1]))


def insert_run_event(conn: AuditConnection, event: RunAuditEvent) -> None:
    """Step 4 for a run chain, in the caller's transaction. Does not commit."""
    conn.execute(
        _RUN_INSERT_SQL,
        (
            event.stream,
            event.run_id,
            event.seq,
            event.prev_hash,
            event.hash,
            event.event_type,
            event.actor,
            event.payload_canonical,
            event.recorded_at,
        ),
    )


def append_run_event(
    conn: AuditConnection,
    *,
    run_id: str,
    event_type: str,
    actor: str,
    payload: JsonObject,
    recorded_at: datetime,
) -> RunAuditEvent:
    """All four steps for `audit.run_event` - P2-C7's named entry point.

    The signature is the one story 4 section A.4 freezes:
    `append_run_event(conn, *, run_id, event_type, actor, payload, recorded_at)`.
    The sequence is `append_event`'s exactly, and for the same reasons: the lock
    is its own statement, the tip is read under it, nothing commits here. The
    lock is taken through the same `lock_stream`, keyed on the `run:` stream
    string - so a run's chain serialises against itself and against nothing
    else, barring a 32-bit `hashtext` collision, which costs waiting and never
    correctness.

    Gap-triggered web searches, the `--accept-incomplete` override, and run
    start and finish land here (D-13). ⚠️ This table has no row-level security:
    a payload must reference documents and conflicts by id, never quote their
    content. See `sql/11_audit_run_event.sql`.
    """
    stream = stream_for_run(run_id)
    lock_stream(conn, stream)
    tip = read_run_chain_tip(conn, stream)
    event = build_run_event(
        run_id=run_id,
        event_type=event_type,
        actor=actor,
        payload=payload,
        recorded_at=recorded_at,
        tip=tip,
    )
    insert_run_event(conn, event)
    return event
