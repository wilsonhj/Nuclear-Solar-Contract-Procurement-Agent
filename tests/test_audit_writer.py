"""H.4 - the statement sequence, asserted without a database.

Decision 9's measured finding is that an advisory lock taken *inside* a trigger
does not work: the statement snapshot a trigger's queries run against is taken
before the trigger body acquires the lock, so a concurrent waiter still reads a
stale chain tip. Eight concurrent writers produced 42 silent forks under that
design. `sql/07_audit_event.sql` says in as many words that the fix cannot be
expressed as a constraint or a trigger, and is "enforced by code review and by
the Python client library, NOT by this schema".

That makes the shape of the caller sequence a property of *this* package and of
nothing else - so it is tested here, against a recording stand-in, and the
property holds in any environment. `tests/test_audit_concurrency.py` proves the
lock actually prevents forks, which needs a live server; this file proves the
lock is issued, in the right place, as its own statement, which does not.

The distinction matters because the two failures look nothing alike. A lock
folded into the INSERT would pass a load test on a fast machine and fail on a
slow one; a missing lock fails only under contention. Neither is visible in a
diff. Statement order is.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, LiteralString

import pytest

from procurement_agent.audit import (
    ChainTip,
    JsonObject,
    append_event,
    append_run_event,
    read_chain_tip,
)

RECORDED_AT = datetime(2026, 8, 6, 15, 4, 5, tzinfo=UTC)
PAYLOAD: JsonObject = {"field": "price_per_watt_dc", "value": 0.19}
PARENT = bytes.fromhex("aa" * 32)


class _Cursor:
    """Returns one canned row, then nothing."""

    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    def fetchone(self) -> tuple[Any, ...] | None:
        row, self._row = self._row, None
        return row


class _RecordingConnection:
    """Records every statement in order and answers the tip read.

    Deliberately not a mock: the assertions below are about the sequence of
    statements, so the stand-in's whole job is to be a list of them.
    """

    def __init__(self, *, tip: tuple[int, bytes] | None = None, autocommit: bool = False) -> None:
        self.statements: list[tuple[str, Sequence[object] | None]] = []
        self._tip = tip
        self._autocommit = autocommit

    @property
    def autocommit(self) -> bool:
        return self._autocommit

    def execute(self, query: LiteralString, params: Sequence[object] | None = None) -> _Cursor:
        self.statements.append((query, params))
        if "FROM audit.event" in query or "FROM audit.run_event" in query:
            return _Cursor(self._tip)
        return _Cursor(None)

    def commit(self) -> None:  # pragma: no cover - reaching it is the failure
        raise AssertionError(
            "append_event committed. sql/07 requires the audit row to land in the "
            "SAME transaction as the business write, so the caller owns the commit."
        )


def _append(conn: _RecordingConnection) -> None:
    append_event(
        conn,
        document_id="open-1",
        event_type="extraction",
        actor="worker-7",
        payload=PAYLOAD,
        recorded_at=RECORDED_AT,
    )


def test_a_the_advisory_lock_is_the_first_statement() -> None:
    """Step 1 of sql/07's caller sequence, before anything reads the chain.

    A lock taken after the tip read would be worse than no lock at all: it looks
    correct in review and serialises the writers *after* the race it was meant
    to prevent has already happened.
    """
    conn = _RecordingConnection(tip=(0, PARENT))
    _append(conn)
    assert "pg_advisory_xact_lock" in conn.statements[0][0]


def test_a_the_advisory_lock_is_its_own_statement() -> None:
    """`H.4`, stated exactly: its own statement, not folded into the INSERT.

    Folding it into the INSERT as a CTE or a scalar subquery reintroduces the
    measured bug in a new costume - the whole statement gets one snapshot, so
    the tip the INSERT reads is again the one from before the lock was held.
    """
    conn = _RecordingConnection(tip=(0, PARENT))
    _append(conn)

    lock_statement = conn.statements[0][0]
    assert "INSERT" not in lock_statement.upper()
    assert "FROM audit.event" not in lock_statement

    for query, _ in conn.statements[1:]:
        assert "pg_advisory" not in query, "the lock must not also appear inside another statement"


def test_a_the_statement_sequence_is_lock_then_read_then_insert() -> None:
    """All four steps of sql/07's caller sequence, in order and with no extras.

    An exact count as well as an order: an additional statement between the lock
    and the INSERT is how a well-meant `SELECT` for logging turns into a longer
    critical section, and an additional statement *before* the lock is how the
    property is lost entirely.
    """
    conn = _RecordingConnection(tip=(0, PARENT))
    _append(conn)

    assert len(conn.statements) == 3
    assert "pg_advisory_xact_lock" in conn.statements[0][0]
    assert "FROM audit.event" in conn.statements[1][0]
    assert conn.statements[2][0].lstrip().upper().startswith("INSERT")


def test_a_the_lock_key_is_derived_from_the_stream() -> None:
    """Decision 9: chain per document, "so cross-document concurrency stays
    unconstrained".

    Both halves are needed, and finding that out cost a surviving mutant.
    Asserting only the bound parameter passes against
    `pg_advisory_xact_lock(1), %s::text IS NOT NULL` - the stream is still
    passed, and still ignored, and every writer in the system serialises behind
    one key. No load test catches that either, because a global lock prevents
    forks perfectly well; it is only a correctness bug in the sense that it is
    not what Decision 9 asked for. So the statement text is pinned to `sql/07`'s
    caller sequence, which names `hashtext(stream)::bigint` literally.
    """
    conn = _RecordingConnection(tip=(0, PARENT))
    _append(conn)
    assert "hashtext(%s)" in conn.statements[0][0]
    assert conn.statements[0][1] == ("doc:open-1",)


def test_a_an_autocommit_connection_is_refused() -> None:
    """The one configuration where the lock is issued and does nothing.

    `pg_advisory_xact_lock` releases at the end of its transaction. Under
    autocommit each statement *is* a transaction, so the lock is taken and
    dropped before the tip is read - restoring the exact race, while every
    statement-order assertion above still passes. It has to be refused rather
    than documented.
    """
    conn = _RecordingConnection(tip=(0, PARENT), autocommit=True)
    with pytest.raises(ValueError, match="autocommit"):
        _append(conn)
    assert conn.statements == [], "nothing may be issued once the connection is known unusable"


def test_a_a_stream_with_no_rows_reads_as_no_tip() -> None:
    """The genesis path: an empty stream is `None`, never a zero-valued tip.

    Returning something falsy-but-present here would produce a `prev_hash` of
    32 zero bytes on the first event, which satisfies the `octet_length` CHECK
    and fails only later, at the parent foreign key.
    """
    conn = _RecordingConnection(tip=None)
    assert read_chain_tip(conn, "doc:open-1") is None


def test_a_a_stream_with_rows_reads_as_its_tip() -> None:
    conn = _RecordingConnection(tip=(4, PARENT))
    assert read_chain_tip(conn, "doc:open-1") == ChainTip(seq=4, hash=PARENT)


# --- run-scoped events (P2-C7) ----------------------------------------------------


def _append_run(conn: _RecordingConnection) -> None:
    append_run_event(
        conn,
        run_id="run-1",
        event_type="compose_override",
        actor="alice@example.invalid",
        payload=PAYLOAD,
        recorded_at=RECORDED_AT,
    )


def test_append_run_event_runs_the_same_three_statements_on_its_own_table() -> None:
    """D-13: "reusing WP-H's envelope and lock discipline verbatim".

    The same lock statement, keyed on the `run:` stream; the tip read and the
    INSERT against `audit.run_event` and **never** `audit.event`. A run event
    written to the document table would fail on the document FK - loudly - but
    a tip read from the wrong table would succeed and return the wrong parent.
    """
    conn = _RecordingConnection(tip=(0, PARENT))
    _append_run(conn)

    assert len(conn.statements) == 3
    lock, read, insert = conn.statements
    assert "pg_advisory_xact_lock" in lock[0] and "hashtext(%s)" in lock[0]
    assert lock[1] == ("run:run-1",)
    assert "FROM audit.run_event" in read[0] and read[1] == ("run:run-1",)
    assert insert[0].lstrip().upper().startswith("INSERT INTO AUDIT.RUN_EVENT")
    assert all("audit.event" not in query for query, _ in conn.statements)


def test_append_event_never_touches_the_run_table() -> None:
    """The converse, so neither entry point can drift into the other's table."""
    conn = _RecordingConnection(tip=(0, PARENT))
    _append(conn)
    assert all("run_event" not in query for query, _ in conn.statements)


def test_append_run_event_refuses_autocommit_too() -> None:
    """The lock-is-a-no-op configuration is refused for run chains as well; it
    comes from the shared `lock_stream`, so this pins that it stays shared."""
    conn = _RecordingConnection(autocommit=True)
    with pytest.raises(ValueError, match="autocommit"):
        _append_run(conn)
    assert conn.statements == []


def test_append_run_event_chains_off_the_tip_it_read() -> None:
    conn = _RecordingConnection(tip=(4, PARENT))
    _append_run(conn)
    params = conn.statements[2][1]
    assert params is not None
    stream, run_id, seq, prev_hash = params[0], params[1], params[2], params[3]
    assert (stream, run_id, seq, prev_hash) == ("run:run-1", "run-1", 5, PARENT)
