"""H.4 and H.5 against a running PostgreSQL - including the merge-blocking load test.

`sql/README.md` names concurrency as the one property its behaviour suite leaves
unproven: Decision 9's measured failure - 8 concurrent writers producing 42
silent forks - "is a property of the *caller's* advisory-lock discipline, not of
this DDL, and nothing here exercises it". This file is what exercises it.

**The control is the point.** `test_a_without_the_lock_...` runs byte-identical
code with `lock_stream` omitted and asserts that it *does* fork. Without that,
`test_a_eight_concurrent_writers_produce_no_forks` proves nothing: a load test
that never actually races passes just as green as one that races and is
protected, and this repo has been burned by a skipped test read as a pass. The
two tests differ in exactly one call.

**Why the deliberate hold.** Both paths sleep between reading the chain tip and
inserting. Relying on scheduler luck to hit a microsecond-wide window makes the
control flaky, and a flaky control is worse than none - it eventually gets
deleted. Widening the window makes the race certain in both directions, which
keeps the comparison fair: the locked path holds the lock across the same sleep
and must still produce zero forks.

**Skipped unless `PROCUREMENT_TEST_DSN` points at a disposable database**, on
the same terms as `tests/test_sql_behaviour.py` - see that file's docstring for
how to raise one locally, and note its warning that a Unix socket with `trust`
cannot reproduce a CI authentication failure. The database is dropped and
recreated. Never point this at anything you care about.

A skip here is not a pass. If this file skipped, the advisory lock is unproven
and H.4 is not done, whatever the rest of the suite says.
"""

from __future__ import annotations

import os
import pathlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from procurement_agent.audit import (
    DefectKind,
    append_event,
    build_event,
    insert_event,
    lock_stream,
    read_chain_tip,
    stream_for_document,
    verify_stream,
)
from procurement_agent.audit.verify import main as verify_main
from procurement_agent.schema import Resolution, ResolutionAction
from procurement_agent.services.transactional_audit import write_and_append_event
from procurement_agent.services.vertical_slice import (
    persist_vertical_slice,
    review_conflict,
    run_sanitized_pv_csv,
    write_vertical_slice_rows,
)

if TYPE_CHECKING:
    import psycopg
else:
    psycopg = pytest.importorskip("psycopg", reason="the `store` extra is not installed")

DSN = os.environ.get("PROCUREMENT_TEST_DSN")

pytestmark = pytest.mark.skipif(
    not DSN, reason="PROCUREMENT_TEST_DSN is unset; the advisory lock is NOT proven by this run"
)

SQL_DIR = pathlib.Path(__file__).parent.parent / "sql"
DOCUMENT_ID = "audit-doc-1"
STREAM = stream_for_document(DOCUMENT_ID)
ATOMIC_DOCUMENT_ID = "audit-atomic-doc"
PV_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "ingestion" / "sanitized-pv-module.csv"

#: Decision 9's own measurement used eight. Kept identical so a regression here
#: is comparable with the number in `tasks.md` H.4 rather than merely "some".
WRITERS = 8
EVENTS_PER_WRITER = 6

#: Seconds held between reading the chain tip and inserting. Long enough that
#: every unlocked writer is guaranteed to read the same stale tip, short enough
#: that the locked run - which serialises, so pays it 48 times - stays about a
#: second.
HOLD = 0.02


def _connect(*, autocommit: bool = True) -> psycopg.Connection:
    assert DSN is not None
    return psycopg.connect(DSN, autocommit=autocommit)


@pytest.fixture(scope="session")
def audit_schema() -> None:
    """Apply `sql/` into a freshly dropped database, then seed one document.

    From the same files a deployment would use, for `test_sql_behaviour.py`'s
    reason: a schema test run against a hand-built approximation of the schema
    tests the approximation. The document exists because `audit.event.document_id`
    is `NOT NULL REFERENCES public.document`.
    """
    with _connect() as conn:
        conn.execute("DROP SCHEMA IF EXISTS audit CASCADE")
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT USAGE, CREATE ON SCHEMA public TO PUBLIC")
        for path in sorted(SQL_DIR.glob("0*.sql")):
            conn.execute(path.read_text(encoding="utf-8"))
        conn.execute(
            """
            INSERT INTO public.document
                (document_id, content_hash, source_uri, document_type,
                 ingested_at, access_restricted)
            VALUES (%s, 'hash-audit', 'file:///audit.pdf', 'spec_sheet', now(), false)
            ON CONFLICT (content_hash) DO NOTHING
            """,
            (DOCUMENT_ID,),
        )


@pytest.fixture
def empty_chain(audit_schema: None) -> Any:
    """An empty `audit.event`, before and after.

    Teardown disables the append-only trigger to clear the table, exactly as
    `test_sql_behaviour.py`'s `chain` fixture does - the trigger is a tripwire
    for the application roles, and a fixture is not one of them.
    """
    yield
    with _connect() as conn:
        conn.execute("ALTER TABLE audit.event DISABLE TRIGGER audit_event_no_mutation")
        conn.execute("DELETE FROM audit.event")
        conn.execute("ALTER TABLE audit.event ENABLE TRIGGER audit_event_no_mutation")


@pytest.fixture
def unconstrained_chain(empty_chain: None) -> Any:
    """`audit.event` with its two fork constraints dropped, restored afterwards.

    Needed to reproduce Decision 9's *original* measurement rather than a
    softened version of it. With `audit_event_no_fork` in place a fork surfaces
    as a unique violation - loud, which is what that constraint is for and
    explicitly "necessary, insufficient". Dropping it is what lets the control
    test count the forks the way the original 42 was counted: silently written,
    discovered later.

    `audit_event_seq_unique` goes too, because unlocked writers compute the same
    `seq` from the same stale tip and would otherwise collide there first.
    """
    with _connect() as conn:
        conn.execute("ALTER TABLE audit.event DROP CONSTRAINT audit_event_no_fork")
        conn.execute("ALTER TABLE audit.event DROP CONSTRAINT audit_event_seq_unique")
    yield
    with _connect() as conn:
        conn.execute("ALTER TABLE audit.event DISABLE TRIGGER audit_event_no_mutation")
        conn.execute("DELETE FROM audit.event")
        conn.execute("ALTER TABLE audit.event ENABLE TRIGGER audit_event_no_mutation")
        conn.execute(
            "ALTER TABLE audit.event ADD CONSTRAINT audit_event_no_fork "
            "UNIQUE NULLS NOT DISTINCT (stream, prev_hash)"
        )
        conn.execute(
            "ALTER TABLE audit.event ADD CONSTRAINT audit_event_seq_unique UNIQUE (stream, seq)"
        )


def _writer(worker: int, barrier: threading.Barrier, *, take_lock: bool) -> list[str]:
    """One writer's whole life: `EVENTS_PER_WRITER` appends, one per transaction.

    Returns the errors it saw rather than raising them, so a failing run reports
    how many writers collided instead of whichever one happened to finish first.
    """
    errors: list[str] = []
    with _connect(autocommit=False) as conn:
        barrier.wait()
        for index in range(EVENTS_PER_WRITER):
            try:
                if take_lock:
                    lock_stream(conn, STREAM)
                tip = read_chain_tip(conn, STREAM)
                # The window Decision 9 measured. Inside the lock when there is
                # one; unprotected when there is not.
                time.sleep(HOLD)
                event = build_event(
                    document_id=DOCUMENT_ID,
                    event_type="extraction",
                    actor=f"worker-{worker}",
                    payload={"worker": worker, "index": index},
                    recorded_at=datetime.now(UTC),
                    tip=tip,
                )
                insert_event(conn, event)
                conn.commit()
            except psycopg.errors.Error as exc:  # noqa: PERF203
                conn.rollback()
                errors.append(type(exc).__name__)
    return errors


def _run(*, take_lock: bool) -> list[str]:
    barrier = threading.Barrier(WRITERS)
    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        results = pool.map(lambda w: _writer(w, barrier, take_lock=take_lock), range(WRITERS))
    return [error for errors in results for error in errors]


def _forks(conn: psycopg.Connection) -> int:
    """Rows that share a parent: the definition of a fork, counted directly."""
    row = conn.execute(
        """
        SELECT coalesce(sum(children - 1), 0) FROM (
            SELECT count(*) AS children FROM audit.event
             WHERE stream = %s GROUP BY prev_hash HAVING count(*) > 1
        ) AS forked
        """,
        (STREAM,),
    ).fetchone()
    assert row is not None
    return int(row[0])


# --- H.4: the property sql/README.md names as unproven -------------------------


def test_a_eight_concurrent_writers_produce_no_forks(empty_chain: None) -> None:
    """The measurement Decision 9 asked for, with the lock taken as its own statement.

    Four assertions, because each one fails differently. No errors at all: the
    lock must *prevent* the race, not merely convert it into a loud unique
    violation the caller has to retry. Every event present: a writer that
    silently gave up would also produce a fork-free chain. Contiguous `seq`: the
    chain is one line, not one line plus survivors. And the chain verifies,
    which is the only assertion that covers the digests as well as the shape.
    """
    errors = _run(take_lock=True)
    assert errors == []

    with _connect() as conn:
        row = conn.execute(
            "SELECT count(*), count(DISTINCT seq), min(seq), max(seq) FROM audit.event "
            "WHERE stream = %s",
            (STREAM,),
        ).fetchone()
        assert row == (WRITERS * EVENTS_PER_WRITER, WRITERS * EVENTS_PER_WRITER, 0, 47)
        assert _forks(conn) == 0

        report = verify_stream(conn, STREAM)
        assert report.ok, report.defects
        assert report.events == WRITERS * EVENTS_PER_WRITER


def test_a_without_the_lock_concurrent_writers_fork_the_chain(
    unconstrained_chain: None,
) -> None:
    """The control. Same code, `lock_stream` omitted, fork constraints dropped.

    This is Decision 9's original measurement reproduced: writers reading the
    same stale tip and writing siblings of it, silently. If this test ever goes
    green-by-passing - zero forks - then the load above is not racing and the
    protection it claims to demonstrate is unmeasured, so the assertion is
    written in the direction that makes a vacuous run fail.
    """
    errors = _run(take_lock=False)
    assert errors == [], "with both fork constraints dropped, nothing should refuse a write"

    with _connect() as conn:
        assert _forks(conn) > 0, (
            "no fork occurred without the lock, so this run did not reproduce the race "
            "and the locked test above proves nothing"
        )
        report = verify_stream(conn, STREAM)
        assert not report.ok
        assert DefectKind.BROKEN_LINK in {defect.kind for defect in report.defects}


def test_a_without_the_lock_the_fork_constraint_fires(empty_chain: None) -> None:
    """ "Necessary, insufficient", measured rather than quoted.

    With `audit_event_no_fork` in place and no advisory lock, the race still
    happens - it is merely loud. That is precisely why `sql/07` says the
    constraint "catches a fork after the fact, it does not prevent the race that
    causes one, which is why step 1 still matters".
    """
    errors = _run(take_lock=False)
    assert errors, "the constraint did not fire, so this run did not reproduce the race"
    assert {"UniqueViolation"} <= set(errors)


# --- H.3 and H.5 end to end ----------------------------------------------------


def test_a_append_event_writes_a_chain_the_verifier_accepts(empty_chain: None) -> None:
    """The library's own entry point, from an empty stream to a verified chain.

    `test_audit_writer.py` pins the statement sequence without a server; this
    pins that the statements are ones PostgreSQL actually accepts - the CHECK
    tying `stream` to `document_id`, the genesis rule tying `prev_hash` to
    `seq = 0`, and the `octet_length(hash) = 32` the digest choice has to match.
    """
    with _connect(autocommit=False) as conn:
        for index in range(3):
            append_event(
                conn,
                document_id=DOCUMENT_ID,
                event_type="document_ingested" if index == 0 else "extraction",
                actor="worker-0",
                payload={"index": index},
                recorded_at=datetime.now(UTC),
            )
        conn.commit()

        report = verify_stream(conn, STREAM)
        assert report.ok, report.defects
        assert report.events == 3


def test_a_the_generated_payload_column_matches_what_was_hashed(empty_chain: None) -> None:
    """`payload` is generated from `payload_canonical`, so the two cannot drift.

    Asserted against a real server because the claim is about a `GENERATED
    ALWAYS AS (payload_canonical::jsonb) STORED` column, which no Python test
    can evaluate - and because `jsonb` round-tripping is the exact step H.2's
    reasoning warns against trusting as a canonicalisation.
    """
    with _connect(autocommit=False) as conn:
        event = append_event(
            conn,
            document_id=DOCUMENT_ID,
            event_type="extraction",
            actor="worker-0",
            payload={"b": 2, "a": 1.5},
            recorded_at=datetime.now(UTC),
        )
        conn.commit()
        row = conn.execute(
            "SELECT payload_canonical, payload FROM audit.event WHERE stream = %s", (STREAM,)
        ).fetchone()

    assert row is not None
    assert row[0] == event.payload_canonical == '{"a":1.5,"b":2}'
    assert row[1] == {"a": 1.5, "b": 2}


def test_a_business_write_and_its_audit_event_roll_back_together(audit_schema: None) -> None:
    """Decision 9's atomicity claim, measured across an actual rollback.

    Fake-connection tests can prove statement order and absence of an internal
    commit.  Only a server can prove that the business row and audit row really
    occupy one transaction and disappear together.
    """
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO public.document
                (document_id, content_hash, source_uri, document_type,
                 ingested_at, access_restricted)
            VALUES (%s, %s, %s, 'spec_sheet', now(), false)
            """,
            (ATOMIC_DOCUMENT_ID, "hash-audit-atomic", "file:///audit-atomic.pdf"),
        )

    changed_at = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    with _connect(autocommit=False) as conn:

        def update_document(transaction: Any) -> None:
            transaction.execute(
                "UPDATE public.document SET data_vintage = %s WHERE document_id = %s",
                (changed_at, ATOMIC_DOCUMENT_ID),
            )

        write_and_append_event(
            conn,
            write=update_document,
            document_id=ATOMIC_DOCUMENT_ID,
            event_type="extraction",
            actor="vertical-slice",
            payload={"field": "nameplate_power", "claim_count": 1},
            recorded_at=changed_at,
        )
        conn.rollback()

    with _connect() as conn:
        row = conn.execute(
            """
            SELECT d.data_vintage,
                   (SELECT count(*) FROM audit.event WHERE document_id = d.document_id)
              FROM public.document d
             WHERE d.document_id = %s
            """,
            (ATOMIC_DOCUMENT_ID,),
        ).fetchone()

    assert row == (None, 0)


def test_a_pv_slice_persists_review_and_verified_audit_chains(empty_chain: None) -> None:
    """The concrete PV path reaches every business table and commits with audit."""
    ingested_at = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)
    detected_at = datetime(2026, 8, 30, 9, 1, tzinfo=UTC)
    result = run_sanitized_pv_csv(
        PV_FIXTURE.read_bytes(),
        ingested_at=ingested_at,
        detected_at=detected_at,
        actor="live-integration-test",
    )

    with _connect(autocommit=False) as conn:
        stored, persisted, events = persist_vertical_slice(
            conn,
            result,
            write=write_vertical_slice_rows,
        )

    assert stored.document_ids == ("syn-pv-datasheet", "syn-pv-purchase-order")
    assert len(stored.claim_ids) == 3
    assert len(stored.conflict_ids) == 1
    assert stored.resolution_ids == ()
    assert persisted.audit_events == ()
    assert len(events) == 5

    with _connect(autocommit=False) as conn:
        replayed_store, replayed, replay_events = persist_vertical_slice(
            conn,
            result,
            write=write_vertical_slice_rows,
        )
    assert replayed_store.audit_events_to_append == ()
    assert replayed.audit_events == ()
    assert replay_events == ()

    [conflict] = persisted.conflicts
    reviewed = review_conflict(
        persisted,
        entry_id=conflict.entry_id,
        resolution=Resolution(
            action=ResolutionAction.SELECT_VALUE,
            resolved_by="procurement.lead",
            resolved_at=datetime(2026, 8, 30, 10, 0, tzinfo=UTC),
            rationale="The signed purchase order supersedes the draft datasheet.",
            value_before=650.0,
            value_after=655.0,
        ),
    )
    with _connect(autocommit=False) as conn:
        reviewed_store, acknowledged, review_events = persist_vertical_slice(
            conn,
            reviewed,
            write=write_vertical_slice_rows,
        )

    assert len(reviewed_store.resolution_ids) == 1
    assert acknowledged.audit_events == ()
    assert [event.event_type for event in review_events] == ["resolution"]

    with _connect(autocommit=False) as conn:
        replayed_review_store, _, replayed_review_events = persist_vertical_slice(
            conn,
            reviewed,
            write=write_vertical_slice_rows,
        )
    assert replayed_review_store.resolution_ids == reviewed_store.resolution_ids
    assert replayed_review_store.audit_events_to_append == ()
    assert replayed_review_events == ()

    competing_review = review_conflict(
        persisted,
        entry_id=conflict.entry_id,
        resolution=Resolution(
            action=ResolutionAction.SELECT_VALUE,
            resolved_by="another.reviewer",
            resolved_at=datetime(2026, 8, 30, 10, 1, tzinfo=UTC),
            rationale="The datasheet should remain authoritative.",
            value_before=655.0,
            value_after=650.0,
        ),
    )
    with _connect(autocommit=False) as conn:
        with pytest.raises(ValueError, match="stale or competing resolution"):
            persist_vertical_slice(
                conn,
                competing_review,
                write=write_vertical_slice_rows,
            )

    with _connect() as conn:
        counts = conn.execute(
            """
            SELECT
              (SELECT count(*) FROM public.document
                WHERE document_id IN ('syn-pv-datasheet', 'syn-pv-purchase-order')),
              (SELECT count(*) FROM public.claim
                WHERE document_id IN ('syn-pv-datasheet', 'syn-pv-purchase-order')),
              (SELECT count(*) FROM public.conflict WHERE entry_id = %s),
              (SELECT count(*) FROM public.conflict_candidate WHERE entry_id = %s),
              (SELECT count(*) FROM public.resolution WHERE entry_id = %s),
              (SELECT count(*) FROM audit.event
                WHERE document_id IN ('syn-pv-datasheet', 'syn-pv-purchase-order'))
            """,
            (conflict.entry_id, conflict.entry_id, conflict.entry_id),
        ).fetchone()
        assert counts == (2, 4, 1, 2, 1, 6)
        status = conn.execute(
            "SELECT status FROM public.conflict WHERE entry_id = %s",
            (conflict.entry_id,),
        ).fetchone()
        assert status == ("resolved",)
        for document_id in ("syn-pv-datasheet", "syn-pv-purchase-order"):
            report = verify_stream(conn, stream_for_document(document_id))
            assert report.ok, report.defects


def test_a_a_tampered_row_fails_verification(empty_chain: None) -> None:
    """Decision 9's central claim: a superuser can edit a row but cannot make the
    chain re-verify.

    The edit below needs the append-only trigger disabled, which is itself one
    of the bypasses `sql/07` documents - so this test performs the attack that
    file says it cannot stop, and asserts the chain notices.
    """
    with _connect(autocommit=False) as conn:
        for index in range(2):
            append_event(
                conn,
                document_id=DOCUMENT_ID,
                event_type="document_ingested" if index == 0 else "extraction",
                actor="worker-0",
                payload={"index": index},
                recorded_at=datetime.now(UTC),
            )
        conn.commit()

    with _connect() as conn:
        conn.execute("ALTER TABLE audit.event DISABLE TRIGGER audit_event_no_mutation")
        conn.execute(
            "UPDATE audit.event SET actor = 'not-me' WHERE seq = 1 AND stream = %s", (STREAM,)
        )
        conn.execute("ALTER TABLE audit.event ENABLE TRIGGER audit_event_no_mutation")

        report = verify_stream(conn, STREAM)
        assert not report.ok
        assert DefectKind.HASH_MISMATCH in {defect.kind for defect in report.defects}


# --- H.5: the CLI ---------------------------------------------------------------


def test_a_the_cli_exits_zero_on_a_good_chain(empty_chain: None) -> None:
    with _connect(autocommit=False) as conn:
        append_event(
            conn,
            document_id=DOCUMENT_ID,
            event_type="document_ingested",
            actor="worker-0",
            payload={"index": 0},
            recorded_at=datetime.now(UTC),
        )
        conn.commit()

    assert DSN is not None
    os.environ["PROCUREMENT_DATABASE_URL"] = DSN
    try:
        assert verify_main([]) == 0
    finally:
        os.environ.pop("PROCUREMENT_DATABASE_URL", None)


def test_a_the_cli_exits_nonzero_on_a_tampered_chain(
    empty_chain: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """An exit code, not just a message.

    This runs from cron or from a release check, where nobody reads the output
    until it is already too late. A verifier that prints "FAILED" and exits 0 is
    the failure mode with the longest possible time to detection.
    """
    with _connect(autocommit=False) as conn:
        append_event(
            conn,
            document_id=DOCUMENT_ID,
            event_type="document_ingested",
            actor="worker-0",
            payload={"index": 0},
            recorded_at=datetime.now(UTC),
        )
        conn.commit()

    with _connect() as conn:
        conn.execute("ALTER TABLE audit.event DISABLE TRIGGER audit_event_no_mutation")
        conn.execute("UPDATE audit.event SET actor = 'not-me' WHERE stream = %s", (STREAM,))
        conn.execute("ALTER TABLE audit.event ENABLE TRIGGER audit_event_no_mutation")

    assert DSN is not None
    os.environ["PROCUREMENT_DATABASE_URL"] = DSN
    try:
        assert verify_main(["--stream", STREAM]) == 1
    finally:
        os.environ.pop("PROCUREMENT_DATABASE_URL", None)
    assert "hash_mismatch" in capsys.readouterr().out
