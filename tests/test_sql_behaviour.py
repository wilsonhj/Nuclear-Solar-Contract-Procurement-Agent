"""What the DDL actually *does*, measured against a running PostgreSQL.

`test_sql_schema.py` next door asserts the DDL's text. That catches a fix being
reverted, and it cannot catch a fix that was never right: every security defect
this schema shipped was one where the SQL read correctly and the server behaved
otherwise. `USING (true)` on a write policy looks like "the write path is
unrestricted", not like "the application role can declassify every confidential
row in one statement" - but that is what it did, and only a live server said so.

So each test here descends from a defect that was *measured* against a cluster:
the attack is run, and the assertion is that it now fails. The docstring on each
one names what it used to do.

**Skipped unless `PROCUREMENT_TEST_DSN` points at a disposable database.** CI
supplies one from a `pgvector/pgvector` service container. Locally, over TCP
with password authentication, which is what CI does:

    echo postgres > /tmp/pg/pw
    initdb -D /tmp/pg/data -U postgres --auth-host=scram-sha-256 --pwfile=/tmp/pg/pw
    pg_ctl -D /tmp/pg/data -o '-p 5433 -h 127.0.0.1' start
    PGPASSWORD=postgres psql -h 127.0.0.1 -p 5433 -U postgres -c 'create database procurement'
    PROCUREMENT_TEST_DSN='postgresql://postgres:postgres@127.0.0.1:5433/procurement' \
        uv run pytest tests/test_sql_behaviour.py

**Use TCP and a password, not a Unix socket with `trust`.** The first version of
this docstring recommended the socket, and the socket never exercises
authentication: the suite passed locally and failed 8 of 23 in CI, because the
DDL creates its roles without passwords and the container's `pg_hba.conf`
requires `scram-sha-256`. A local setup that cannot reproduce a CI failure is
worse than no local setup, because it is trusted. See `TEST_ROLE_PASSWORD`.

The database is **dropped and recreated per session**. Never point this at
anything you care about; the fixture says so again at the point of use.

One trap is load-bearing enough to state here rather than in a comment. A
`FOR EACH ROW` trigger never fires on an empty table, so an append-only check
against zero rows passes vacuously - it was the first thing that fooled the
original hand-run checklist. `seeded` below therefore inserts real rows, and
every mutation test depends on it.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    # mypy needs the real module to resolve `psycopg.Connection` and the
    # `psycopg.errors.*` classes. `importorskip` returns an untyped module
    # object, so the annotations below would be unresolvable names without this.
    import psycopg
    import psycopg.conninfo
    import psycopg.sql
else:
    psycopg = pytest.importorskip("psycopg", reason="the `store` extra is not installed")
    # Importing the parent does not bind the submodule, and the fixture below
    # uses psycopg.sql for safe identifier quoting on ALTER ROLE and
    # psycopg.conninfo to fold extra libpq parameters into the supplied DSN.
    import psycopg.conninfo  # noqa: F401,F811
    import psycopg.sql  # noqa: F401,F811

DSN = os.environ.get("PROCUREMENT_TEST_DSN")

pytestmark = pytest.mark.skipif(
    not DSN, reason="PROCUREMENT_TEST_DSN is unset; live-database checks skipped"
)

SQL_DIR = pathlib.Path(__file__).parent.parent / "sql"

#: 32-byte digests, written out rather than generated, so a chain in a failure
#: message is readable and the same run twice produces the same bytes.
GENESIS_HASH = bytes.fromhex("aa" * 32)
CHILD_HASH = bytes.fromhex("bb" * 32)
FABRICATED = bytes.fromhex("ff" * 32)


#: Password given to the two LOGIN roles for the duration of the test run.
#:
#: `00_roles.sql` deliberately creates them **without** one — credentials are a
#: deployment concern and belong nowhere near version control. That is correct,
#: and it means a harness connecting over TCP has to supply them: the pgvector
#: image's `pg_hba.conf` ends in `host all all all scram-sha-256`, so a
#: passwordless role gets `FATAL: password authentication failed ... has no
#: password assigned`.
#:
#: This was not hypothetical. A first version of this file passed locally
#: against a Unix socket with `trust` auth and failed 8 of 23 in CI for exactly
#: that reason — the local run never exercised authentication at all.
#:
#: A literal rather than a generated secret, because this only ever reaches a
#: database the fixture drops and rebuilds, and a reader should be able to
#: connect by hand while debugging a failure.
TEST_ROLE_PASSWORD = "procurement-test-only"

#: The roles the DDL creates with LOGIN. The two owner roles are NOLOGIN by
#: design (Decision 9) and must stay that way — `test_owner_roles_cannot_log_in`
#: asserts it — so they are deliberately absent.
LOGIN_ROLES = ("procurement_app", "procurement_ingest")


#: The seven tables that hold document content, each seeded by `seeded_all`
#: below with exactly one open row and one restricted one. Every confidentiality
#: assertion in this file is "the restricted one is not in the answer", so the
#: expected count for an unentitled principal is 1 and for an entitled one 2.
CONTENT_TABLES = (
    "public.document",
    "public.chunk",
    "public.claim",
    "public.conflict",
    "public.resolution",
    "public.job",
    "audit.event",
)


def _connect(
    *, user: str = "postgres", autocommit: bool = True, options: str | None = None
) -> psycopg.Connection:
    """Shared plumbing. Prefer `_superuser()` or `_entitled()` — they say which.

    `postgres` is a **superuser**, and a superuser bypasses row-level security
    outright, FORCE ROW LEVEL SECURITY included — plan.md Decision 9 says so in
    as many words. A confidentiality assertion made from such a connection
    therefore proves nothing about any policy, and `SET app.allow_restricted` on
    one is a no-op.

    `options` is libpq's connection-string `options` parameter, which is how a
    GUC arrives **before the session's first statement runs**. Exactly one test
    uses it, and it is the vector no application-side guard can cover.
    """
    assert DSN is not None
    params: dict[str, str] = {}
    if user != "postgres":
        params["user"] = user
        params["password"] = TEST_ROLE_PASSWORD
    if options is not None:
        params["options"] = options
    # Folded into the DSN rather than passed as keywords, so `options` travels
    # by exactly the route the attack uses: a libpq connection parameter applied
    # during startup, not a statement the session runs afterwards.
    return psycopg.connect(psycopg.conninfo.make_conninfo(DSN, **params), autocommit=autocommit)


def _superuser(*, autocommit: bool = True) -> psycopg.Connection:
    """The bootstrap identity, named for what it is rather than for what it is
    standing in for.

    Reach for this only where the operation needs privileges no project role
    has. In this file that is exactly two things: applying or dropping schemas,
    and `DELETE FROM public.document` — that table deliberately declares no
    DELETE policy at all (02_document.sql: "There is no DELETE path"), so under
    FORCE ROW LEVEL SECURITY the delete is a silent zero for every role
    including its own owner, and a fixture teardown that leaked rows would look
    exactly like a passing teardown.

    Everywhere else, use `_entitled()`.
    """
    return _connect(autocommit=autocommit)


@contextlib.contextmanager
def _entitled(role: str = "procurement_owner") -> Iterator[psycopg.Connection]:
    """A real, RLS-subject principal holding the confidentiality entitlement.

    Connects as the bootstrap identity and immediately `SET ROLE`s to `role`,
    which every owner role must be reached through: they are NOLOGIN by design
    (Decision 9) and nothing ever connects as one. After the `SET ROLE`,
    `current_user` is a non-superuser with no BYPASSRLS, so FORCE ROW LEVEL
    SECURITY applies to it and `SET app.allow_restricted` is load-bearing.

    **This is the entire point of the helper.** Eleven call sites in this file
    used to run `SET app.allow_restricted = 'true'` on a superuser connection
    whose local variable was named `owner` — which reads as `procurement_owner`
    and was not. A superuser bypasses RLS, so the GUC did nothing, and any test
    that depended on it or on RLS applying could not have failed.
    `test_the_row_level_tripwire_still_raises` below is the sharpest case: it
    exists precisely to catch RLS silently swallowing an append-only trigger,
    and it was running on the one identity RLS does not apply to.

    The `SET ROLE` is asserted rather than assumed, because a superuser's
    `SET ROLE` to a NOLOGIN role succeeds quietly and the whole helper is
    worthless if it ever stopped.
    """
    with _connect() as conn:
        conn.execute(psycopg.sql.SQL("SET ROLE {}").format(psycopg.sql.Identifier(role)))
        current = conn.execute("SELECT current_user").fetchone()
        assert current is not None and current[0] == role, (
            f"SET ROLE {role} did not take effect; current_user is {current}"
        )
        conn.execute("SET app.allow_restricted = 'true'")
        yield conn


@pytest.fixture(scope="session")
def schema() -> None:
    """Apply the nine files in order into a **freshly dropped** database.

    In order, and from the same files a deployment would use — not from a dump.
    A schema test that runs against a hand-built approximation of the schema
    tests the approximation.
    """
    with _superuser() as conn:
        conn.execute("DROP SCHEMA IF EXISTS audit CASCADE")
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        # Restore the ACL a stock database ships with. From PostgreSQL 15 a
        # freshly CREATE'd `public` grants nothing to PUBLIC, so without this
        # every role below loses USAGE and the whole suite fails with
        # "permission denied for schema public" — a fixture artefact that looks
        # exactly like a privilege defect in the DDL.
        #
        # Worth noting rather than only working around: no file in `sql/` grants
        # USAGE on `public` (only on `audit`, in 07). The DDL therefore relies on
        # the ambient schema ACL. That is ordinary practice, but it means a
        # cluster hardened with `REVOKE ALL ON SCHEMA public FROM PUBLIC` would
        # break the application with nothing in these files to explain why.
        conn.execute("GRANT USAGE, CREATE ON SCHEMA public TO PUBLIC")
        for path in sorted(SQL_DIR.glob("0*.sql")):
            conn.execute(path.read_text(encoding="utf-8"))

        # Credentials, supplied by the harness rather than by the DDL — see
        # TEST_ROLE_PASSWORD. Done after the files are applied so the roles
        # exist, and only for the two LOGIN roles: setting a password on a
        # NOLOGIN owner would not grant it a login, but writing that line would
        # suggest otherwise to the next reader.
        for role in LOGIN_ROLES:
            conn.execute(
                psycopg.sql.SQL("ALTER ROLE {} WITH PASSWORD {}").format(
                    psycopg.sql.Identifier(role),
                    psycopg.sql.Literal(TEST_ROLE_PASSWORD),
                )
            )


@pytest.fixture
def seeded(schema: None) -> Iterator[None]:
    """One open document and one restricted one, each with a chunk and a claim.

    **Rows, not an empty table.** A `FOR EACH ROW` trigger cannot fire without
    them, so every append-only assertion below would pass against nothing.

    Seeded through `_entitled()`, not as a superuser: the `ON CONFLICT` clauses
    below read the proposed row back, RLS applies a `FOR SELECT` policy to it,
    and `SET app.allow_restricted` is what lets the restricted half through. On
    a superuser connection none of that is exercised, so the fixture could not
    tell a working entitlement from a bypassed one.
    """
    with _entitled() as conn:
        conn.execute(
            """
            INSERT INTO public.document
                (document_id, content_hash, source_uri, document_type,
                 ingested_at, access_restricted)
            VALUES ('open-1',  'hash-open',   'file:///open.pdf',   'spec_sheet', now(), false),
                   ('secret-1','hash-secret', 'file:///secret.pdf', 'pricing',    now(), true)
            ON CONFLICT (content_hash) DO NOTHING
            """
        )
        conn.execute(
            """
            INSERT INTO public.chunk
                (chunk_id, document_id, chunk_kind, source_tier, chunk_text, embedding)
            VALUES ('chunk-open',   'open-1',  'prose', 'system_of_record',
                    'JKM610N-66HL4M-V', array_fill(0.1, array[1024])::vector),
                   ('chunk-secret', 'secret-1','prose', 'system_of_record',
                    'CONFIDENTIAL 0.19 per watt', array_fill(0.2, array[1024])::vector)
            ON CONFLICT (chunk_id) DO NOTHING
            """
        )
        conn.execute(
            """
            INSERT INTO public.claim
                (document_id, component_category, supplier, model, field,
                 extractor_version, value, condition, source_tier, source_ref,
                 confidence, extracted_at)
            VALUES ('open-1',  'pv_modules','Trina','TSM-700','nameplate_power','v1',
                    '700'::jsonb,'{}'::jsonb,'system_of_record',
                    '{"document_id":"open-1"}'::jsonb, 0.9, now()),
                   ('secret-1','pv_modules','Trina','TSM-700','price_per_watt_dc','v1',
                    '0.19'::jsonb,'{}'::jsonb,'system_of_record',
                    '{"document_id":"secret-1"}'::jsonb, 0.9, now())
            """
        )
    yield
    # Teardown runs as the superuser, and only because `public.document`
    # declares no DELETE policy: under FORCE ROW LEVEL SECURITY that makes
    # `DELETE FROM public.document` a silent zero for every project role, so an
    # entitled teardown would leak rows into the next test while looking clean.
    # `SET app.allow_restricted` is deliberately absent -- it would be a no-op
    # here, and writing it would imply otherwise.
    with _superuser() as conn:
        conn.execute("ALTER TABLE public.claim DISABLE TRIGGER claim_no_mutation")
        conn.execute("ALTER TABLE public.chunk DISABLE TRIGGER chunk_inherit_access_restricted")
        conn.execute("DELETE FROM public.claim")
        conn.execute("DELETE FROM public.chunk")
        conn.execute("DELETE FROM public.document")
        conn.execute("ALTER TABLE public.claim ENABLE TRIGGER claim_no_mutation")
        conn.execute("ALTER TABLE public.chunk ENABLE TRIGGER chunk_inherit_access_restricted")


# --- confidentiality: the two defects that made RLS decorative -------------------


def test_the_app_role_cannot_declassify_rows_it_cannot_read(seeded: None) -> None:
    """Was: `UPDATE public.chunk SET access_restricted = false` returned
    `UPDATE 2` as `procurement_app` and declassified a row that role could not
    SELECT one statement earlier. The write policies were `USING (true)`, which
    is permissive and OR's with the confidentiality policy, and a WHERE-less
    UPDATE needs no SELECT.

    The standing grant is now SELECT-only, so the same statement is refused
    before row-level security runs. The restricted row stays restricted.
    """
    with _connect(user="procurement_app") as app:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute("UPDATE public.chunk SET access_restricted = false")
        visible = app.execute("SELECT chunk_id FROM public.chunk").fetchall()
    assert [row[0] for row in visible] == ["chunk-open"]

    with _entitled() as owner:
        still = owner.execute(
            "SELECT count(*) FROM public.chunk WHERE access_restricted"
        ).fetchone()
    assert still is not None and still[0] == 1


def test_the_app_role_cannot_mutate_chunks(seeded: None) -> None:
    """SEC-HIGH-2: `procurement_app` is SELECT-only on `chunk`."""
    with _connect(user="procurement_app") as app:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute("UPDATE public.chunk SET section = 'tampered'")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute("DELETE FROM public.chunk")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute(
                """
                INSERT INTO public.chunk
                    (chunk_id, document_id, chunk_kind, source_tier, chunk_text, embedding)
                VALUES ('chunk-app', 'open-1', 'prose', 'system_of_record',
                        'no', array_fill(0.3, array[1024])::vector)
                """
            )


def test_the_write_policy_alone_protects_an_unreadable_row(seeded: None) -> None:
    """Isolates the RLS policy from the inheritance trigger.

    `procurement_app` is SELECT-only on `chunk`. The standing GRANT no longer
    reaches this UPDATE, so the test re-grants `section` for the duration: that
    is the only way to prove the restrictive policy still refuses an unreadable
    row independently of the grant, which is what used to go green when the
    policy was `USING (true)` and the inheritance trigger covered for it.
    """
    with _superuser() as conn:
        conn.execute("GRANT UPDATE (section) ON public.chunk TO procurement_app")
    try:
        with _connect(user="procurement_app") as app:
            app.execute("UPDATE public.chunk SET section = 'tampered'")
        with _entitled() as owner:
            rows = owner.execute(
                "SELECT chunk_id, section FROM public.chunk ORDER BY chunk_id"
            ).fetchall()
    finally:
        with _superuser() as conn:
            conn.execute("REVOKE UPDATE ON public.chunk FROM procurement_app")
    by_id: dict[str, str | None] = dict(rows)
    assert by_id["chunk-secret"] is None, "an unreadable row was modified"
    assert by_id["chunk-open"] == "tampered", "a readable row must still be writable"


def test_the_document_write_policy_alone_protects_an_unreadable_row(seeded: None) -> None:
    """Same isolation for `document`, whose UPDATE grant is column-level.
    `data_vintage` is in that grant and no trigger derives it."""
    with _connect(user="procurement_app") as app:
        app.execute("UPDATE public.document SET data_vintage = now()")
    with _entitled() as owner:
        rows = owner.execute(
            "SELECT document_id, data_vintage FROM public.document ORDER BY document_id"
        ).fetchall()
    by_id: dict[str, object] = dict(rows)
    assert by_id["secret-1"] is None, "an unreadable document was modified"
    assert by_id["open-1"] is not None, "a readable document must still be writable"


def test_the_write_policy_alone_protects_an_unreadable_delete(seeded: None) -> None:
    """Same policy defect, the other verb. The standing GRANT no longer includes
    DELETE for `procurement_app`; the temporary grant isolates the policy."""
    with _superuser() as conn:
        conn.execute("GRANT DELETE ON public.chunk TO procurement_app")
    try:
        with _connect(user="procurement_app") as app:
            app.execute("DELETE FROM public.chunk")
        with _entitled() as owner:
            remaining = owner.execute("SELECT chunk_id FROM public.chunk").fetchall()
    finally:
        with _superuser() as conn:
            conn.execute("REVOKE DELETE ON public.chunk FROM procurement_app")
    assert [row[0] for row in remaining] == ["chunk-secret"]


def test_a_chunk_inherits_its_parent_documents_restriction(seeded: None) -> None:
    """Was: `chunk.access_restricted` defaulted to false with nothing tying it to
    the parent, so an indexer that forgot the flag left confidential text
    world-readable while the document itself was correctly hidden. Chunks are the
    retrieval unit, so this is where NFR-03 is actually decided."""
    with _entitled() as owner:
        owner.execute(
            """
            INSERT INTO public.chunk
                (chunk_id, document_id, chunk_kind, source_tier, chunk_text, embedding)
            VALUES ('chunk-forgot', 'secret-1', 'prose', 'system_of_record',
                    'CONFIDENTIAL: price 0.19/W', array_fill(0.3, array[1024])::vector)
            """
        )
        inherited = owner.execute(
            "SELECT access_restricted FROM public.chunk WHERE chunk_id = 'chunk-forgot'"
        ).fetchone()
    assert inherited is not None and inherited[0] is True

    with _connect(user="procurement_app") as app:
        seen = app.execute(
            "SELECT count(*) FROM public.chunk WHERE chunk_id = 'chunk-forgot'"
        ).fetchone()
    assert seen is not None and seen[0] == 0


def test_restriction_can_only_increase(seeded: None) -> None:
    """The inheritance rule is OR, not assignment, so a chunk may be *more*
    restricted than its parent but never less — even for an entitled session."""
    with _entitled() as owner:
        owner.execute(
            "UPDATE public.chunk SET access_restricted = false WHERE chunk_id = 'chunk-secret'"
        )
        after = owner.execute(
            "SELECT access_restricted FROM public.chunk WHERE chunk_id = 'chunk-secret'"
        ).fetchone()
    assert after is not None and after[0] is True


def test_claims_do_not_leak_a_restricted_documents_values(seeded: None) -> None:
    """Was: no RLS on `claim`, so `SELECT document_id, field, value FROM claim`
    as `procurement_app` returned `0.19` for a document that role could not
    see."""
    with _connect(user="procurement_app") as app:
        rows = app.execute("SELECT document_id FROM public.claim").fetchall()
    assert [row[0] for row in rows] == ["open-1"]


# --- the application role cannot switch its own confidentiality off -------------
#
# Every confidentiality policy in this schema ends `OR
# current_setting('app.allow_restricted', true) = 'true'`, and procurement_app
# can set that GUC itself. Measured against a live server before the RESTRICTIVE
# policies existed, on all seven content tables at once:
#
#                                     document chunk claim conflict resolution job event
#     plain read (no GUC)                    1     1     1        1          1   1     1
#     SET app.allow_restricted = 'true'      2     2     2        2          2   2     2
#     GUC in the CONNECTION STRING           2     2     2        2          2   2     2
#     set_config('app...', 'true', false)    2     2     2        2          2   2     2
#     SET LOCAL inside a transaction         2     2     2        2          2   2     2
#
# The connection-string row is the one that matters most, and it is the reason
# these tests exist rather than an application-side "RESET on pool checkout"
# guard: the GUC is already in place before the pool issues its first statement,
# so there is no moment at which application code could reset it. Anyone holding
# the application DSN had full disclosure with **no statement issued at all**.


@pytest.fixture
def seeded_all(seeded: None) -> Iterator[None]:
    """Extends `seeded` to all seven content tables — one open row, one
    restricted — so the confidentiality assertions below are not confined to the
    two tables the original defect was found on.

    A separate fixture rather than more rows in `seeded`, because `conflict`,
    `resolution`, `job` and `audit.event` bring append-only triggers and FK
    orderings that only these tests need to unwind.
    """
    with _entitled() as conn:
        secret_claim, open_claim = (
            conn.execute(
                "SELECT claim_id FROM public.claim WHERE document_id = %s", (doc,)
            ).fetchone()
            for doc in ("secret-1", "open-1")
        )
        assert secret_claim is not None and open_claim is not None
        conn.execute(
            """
            INSERT INTO public.conflict
                (entry_id, component_category, supplier, model, field_name,
                 conflict_class, severity, status, explanation, detected_at)
            VALUES ('cf-secret','pv_modules','Trina','TSM-700','price_per_watt_dc',
                    'record_vs_web', 4, 'pending',
                    'CONFIDENTIAL: record says 0.19, web says 0.35', now()),
                   ('cf-open','pv_modules','Trina','TSM-700','nameplate_power',
                    'record_vs_web', 1, 'pending', 'an open disagreement', now())
            """
        )
        conn.execute(
            "INSERT INTO public.conflict_candidate (entry_id, claim_id, ordinal)"
            " VALUES ('cf-secret', %s, 0), ('cf-open', %s, 0)",
            (secret_claim[0], open_claim[0]),
        )
        conn.execute(
            """
            INSERT INTO public.resolution
                (entry_id, resolved_by, resolved_at, action, value_before, value_after,
                 rationale)
            VALUES ('cf-secret','alice',now(),'enter_override','0.35'::jsonb,'0.19'::jsonb,
                    'CONFIDENTIAL rationale'),
                   ('cf-open','alice',now(),'enter_override','600'::jsonb,'700'::jsonb,
                    'an open rationale')
            """
        )
        conn.execute(
            """
            INSERT INTO public.job (stage, document_id, status, idempotency_key, payload)
            VALUES ('extract','secret-1','pending','idem-secret','{"secret":"0.19"}'::jsonb),
                   ('extract','open-1','pending','idem-open','{"open":"700"}'::jsonb)
            """
        )
    with _connect(user="procurement_ingest") as ingest:
        ingest.execute(
            """
            INSERT INTO audit.event
                (stream, document_id, seq, prev_hash, hash, event_type, actor,
                 payload_canonical, recorded_at)
            VALUES ('doc:secret-1','secret-1',0,NULL,%s,'extraction','test',
                    '{"price_per_watt_dc":0.19}', now()),
                   ('doc:open-1','open-1',0,NULL,%s,'extraction','test',
                    '{"nameplate_power":700}', now())
            """,
            (GENESIS_HASH, CHILD_HASH),
        )
    yield
    # `audit.event` and `resolution` are append-only, so their row triggers have
    # to be disabled first — but both carry a permissive `FOR DELETE USING
    # (true)` tripwire policy, so an entitled owner can clear them.
    with _entitled("audit_owner") as conn:
        conn.execute("ALTER TABLE audit.event DISABLE TRIGGER audit_event_no_mutation")
        conn.execute("DELETE FROM audit.event")
        conn.execute("ALTER TABLE audit.event ENABLE TRIGGER audit_event_no_mutation")
    with _entitled() as conn:
        conn.execute("ALTER TABLE public.resolution DISABLE TRIGGER resolution_no_mutation")
        conn.execute("DELETE FROM public.resolution")
        conn.execute("ALTER TABLE public.resolution ENABLE TRIGGER resolution_no_mutation")
        conn.execute("DELETE FROM public.conflict_candidate")
    # `conflict` and `job` declare no DELETE policy at all — like `document`,
    # nothing in the spec ever removes one — so under FORCE ROW LEVEL SECURITY
    # the delete is a silent zero for every project role, owner included, and
    # this teardown would leak rows while looking like it had worked. Which is
    # how it was found: `DELETE FROM public.document` in `seeded`'s teardown
    # then failed on `job_document_id_fkey`, one fixture further out.
    with _superuser() as conn:
        conn.execute("DELETE FROM public.conflict")
        conn.execute("DELETE FROM public.job")


def _app_row_counts(conn: psycopg.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in CONTENT_TABLES:
        seen = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
        assert seen is not None
        counts[table] = seen[0]
    return counts


ONE_EACH = dict.fromkeys(CONTENT_TABLES, 1)


def test_the_app_role_sees_exactly_the_open_row_on_every_content_table(
    seeded_all: None,
) -> None:
    """The control for everything below. A policy that denied all seven tables
    outright would satisfy every declassification test on this page and be
    useless, so the open row has to still be there."""
    with _connect(user="procurement_app") as app:
        assert _app_row_counts(app) == ONE_EACH


def test_setting_the_guc_does_not_declassify_the_app_role(seeded_all: None) -> None:
    """Was: `SET app.allow_restricted = 'true'` as `procurement_app` returned the
    restricted row on all seven tables, including the CONFIDENTIAL pricing chunk.
    The GUC is an entitlement the application asserts *about its caller*; it was
    never a boundary the application itself was on the far side of."""
    with _connect(user="procurement_app") as app:
        app.execute("SET app.allow_restricted = 'true'")
        assert _app_row_counts(app) == ONE_EACH
        leaked = app.execute("SELECT chunk_text FROM public.chunk").fetchall()
    assert not any("CONFIDENTIAL" in row[0] for row in leaked), leaked


def test_the_connection_string_cannot_declassify_the_app_role(seeded_all: None) -> None:
    """**The vector no application-side guard can cover**, and the reason this
    had to be fixed in the schema rather than in a pool checkout hook:

        psql "user=procurement_app dbname=procurement \\
              options='-c app.allow_restricted=true'" -c "SELECT ..."

    libpq applies `options` during connection startup, so the session is already
    declassified before it runs a statement. A guard that issues `RESET` on
    checkout races a value that was never absent, and a guard that issues it
    before every query is one forgotten call site away from nothing."""
    with _connect(user="procurement_app", options="-c app.allow_restricted=true") as app:
        arrived = app.execute("SELECT current_setting('app.allow_restricted', true)").fetchone()
        assert arrived is not None and arrived[0] == "true", (
            "the vector itself stopped working, so this test is no longer testing it"
        )
        assert _app_row_counts(app) == ONE_EACH


def test_set_config_is_no_better_than_set(seeded_all: None) -> None:
    """`SET` is not the only spelling. `set_config(..., false)` is session-scoped
    and reaches the same GUC through a function call rather than a utility
    statement, which is exactly the shape a statement-text denylist misses."""
    with _connect(user="procurement_app") as app:
        app.execute("SELECT set_config('app.allow_restricted', 'true', false)")
        assert _app_row_counts(app) == ONE_EACH


def test_set_local_inside_a_transaction_is_no_better(seeded_all: None) -> None:
    """`SET LOCAL` is the spelling sql/README.md recommends to the application,
    so it is the one an attacker reads there first."""
    with _connect(user="procurement_app", autocommit=False) as app:
        app.execute("SET LOCAL app.allow_restricted = 'true'")
        assert _app_row_counts(app) == ONE_EACH
        app.rollback()


def test_the_ingest_role_is_still_entitled(seeded_all: None) -> None:
    """The restrictive policies are scoped `TO procurement_app`, and that scoping
    is load-bearing rather than incidental: `procurement_ingest` writes restricted
    rows and reads them back through `RETURNING`, so widening the scope would
    break the ingest path the confidentiality model depends on. It would also
    break `document_is_restricted()` itself, which runs as `procurement_owner`.
    """
    with _connect(user="procurement_ingest") as ingest:
        assert _app_row_counts(ingest) == dict.fromkeys(CONTENT_TABLES, 2)


def test_a_membership_misgrant_no_longer_fails_open(seeded_all: None) -> None:
    """`00_roles.sql` said a mis-granted `GRANT procurement_ingest TO
    procurement_app` costs "one `SET ROLE`". Measured, it costs **nothing**:
    RLS role matching follows *inherited* membership (`has_privs_of_role`), so
    the permissive `..._ingest_select ... USING (true)` policies apply to
    `procurement_app` with no `SET ROLE` issued and `current_user` still reading
    `procurement_app`. That made a membership strictly worse than the GUC
    escape — silent, and invisible to every role-attribute check.

    A RESTRICTIVE policy is AND'd with the OR of the permissive set, so it
    survives that: the mis-grant degrades from fail-open to fail-closed. This
    does not replace `test_no_project_role_is_a_member_of_another`, which still
    demands the membership be absent; it is the layer that holds if it is not.
    """
    with _superuser() as su:
        su.execute("GRANT procurement_ingest TO procurement_app")
    try:
        with _connect(user="procurement_app") as app:
            who = app.execute("SELECT current_user").fetchone()
            assert who is not None and who[0] == "procurement_app", (
                "no SET ROLE was issued; if this fails the premise has changed"
            )
            assert _app_row_counts(app) == ONE_EACH
    finally:
        with _superuser() as su:
            su.execute("REVOKE procurement_ingest FROM procurement_app")


def test_the_derivation_helpers_are_not_owned_by_a_role_that_bypasses_rls(
    schema: None,
) -> None:
    """**A SECURITY DEFINER function is only a control if RLS still applies to
    its owner.** Measured on this schema: the same read accessor owned by
    `procurement_owner` returns one row (FORCE ROW LEVEL SECURITY applies to
    owners), and owned by a superuser returns two — the CONFIDENTIAL chunk
    included — silently, with nothing in the function text to warn a reader.

    `document_is_restricted()` and `conflict_is_restricted()` are the two places
    this schema depends on that distinction. Both must stay owned by a role that
    is neither SUPERUSER nor BYPASSRLS, or their `SET app.allow_restricted` stops
    being what grants them visibility and they become a hole instead of a hinge.
    """
    with _superuser() as conn:
        rows = conn.execute(
            """
            SELECT p.proname, r.rolname, r.rolsuper, r.rolbypassrls
            FROM pg_proc p
            JOIN pg_roles r ON r.oid = p.proowner
            WHERE p.prosecdef
              AND p.pronamespace IN ('public'::regnamespace, 'audit'::regnamespace)
            ORDER BY p.proname
            """
        ).fetchall()
    assert rows, "no SECURITY DEFINER functions found; the schema changed shape"
    offenders = [(name, owner) for name, owner, sup, bypass in rows if sup or bypass]
    assert offenders == [], (
        f"SECURITY DEFINER functions owned by an RLS-exempt role: {offenders}. "
        "Such a function returns every row regardless of any policy, including "
        "the RESTRICTIVE ones, and reads as if it were filtering."
    )


# --- append-only: the verb that fires no row triggers ----------------------------


@pytest.mark.parametrize(
    ("table", "table_owner"),
    [
        ("public.claim", "procurement_owner"),
        ("public.resolution", "procurement_owner"),
        ("audit.event", "audit_owner"),
    ],
)
def test_truncate_is_refused(seeded: None, table: str, table_owner: str) -> None:
    """Was: refused on `audit.event` only. TRUNCATE fires no row-level triggers,
    so the `FOR EACH ROW` append-only trigger could not see it, and
    `TRUNCATE public.claim CASCADE` succeeded — taking `resolution` and
    `conflict_candidate` with it, i.e. the immutable record of what a human
    decided. `ON DELETE RESTRICT` does not help: CASCADE truncates children
    rather than deleting through the constraint.

    Run as each table's actual owner rather than as a superuser. TRUNCATE is an
    owner-only verb, so the owner is the weakest principal that can reach this
    tripwire at all — and `audit.event`'s owner is `audit_owner`, not
    `procurement_owner`, which is the whole point of Decision 9 keeping the two
    blast radii in separate roles."""
    with _entitled(table_owner) as owner:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            owner.execute(f"TRUNCATE {table} CASCADE")


@pytest.mark.parametrize(
    "verb", ["UPDATE public.claim SET confidence = 0.1", "DELETE FROM public.claim"]
)
def test_the_row_level_tripwire_still_raises(seeded: None, verb: str) -> None:
    """The regression guard for the RLS work: enabling RLS on an append-only
    table filters rows *before* a `FOR EACH ROW` trigger runs, which turned this
    from a raised exception into a silent `UPDATE 0`. The data was safe either
    way; Decision 9 picks the trigger precisely because it is loud.

    **This test could not fail until `_entitled()` existed.** It ran on the
    superuser connection, and a superuser bypasses RLS entirely — so the one
    interaction it exists to guard, RLS removing rows before the trigger sees
    them, was never in play. `procurement_owner` is subject to FORCE ROW LEVEL
    SECURITY, so dropping `claim_tripwire_update` now turns this red."""
    with _entitled() as owner:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            owner.execute(verb)


# --- the hash chain has to be walkable, not merely fork-free --------------------


def _event(conn: psycopg.Connection, seq: int, prev: bytes | None, digest: bytes) -> None:
    conn.execute(
        """
        INSERT INTO audit.event
            (stream, document_id, seq, prev_hash, hash, event_type, actor,
             payload_canonical, recorded_at)
        VALUES ('doc:open-1', 'open-1', %s, %s, %s, 'extraction', 'test', '{}', now())
        """,
        (seq, prev, digest),
    )


@pytest.fixture
def chain(seeded: None) -> Iterator[None]:
    """The chain tests run as `procurement_ingest`, the identity 07's own header
    names as the audit appender — not as a superuser. The constraints under test
    are not RLS, but running them on the one principal that bypasses RLS would
    leave the whole file with no coverage of the append path a worker actually
    uses. Teardown needs `audit_owner`: disabling a trigger is an owner-only
    verb, and `audit.event` is deliberately owned by neither of the other three
    roles."""
    with _connect(user="procurement_ingest") as conn:
        _event(conn, 0, None, GENESIS_HASH)
        _event(conn, 1, GENESIS_HASH, CHILD_HASH)
    yield
    with _entitled("audit_owner") as conn:
        conn.execute("ALTER TABLE audit.event DISABLE TRIGGER audit_event_no_mutation")
        conn.execute("DELETE FROM audit.event")
        conn.execute("ALTER TABLE audit.event ENABLE TRIGGER audit_event_no_mutation")


def test_a_valid_chain_appends(chain: None) -> None:
    with _connect(user="procurement_ingest") as conn:
        count = conn.execute("SELECT count(*) FROM audit.event").fetchone()
    assert count is not None and count[0] == 2


def test_a_fabricated_parent_is_refused(chain: None) -> None:
    """Was: accepted. `prev_hash` named a digest that never existed, producing a
    chain that can never be walked — while `UNIQUE (stream, prev_hash)` saw
    nothing wrong, because it only catches *two children of one parent*."""
    with (
        _connect(user="procurement_ingest") as conn,
        pytest.raises(psycopg.errors.ForeignKeyViolation),
    ):
        _event(conn, 7, FABRICATED, bytes.fromhex("cc" * 32))


def test_a_second_disconnected_root_is_refused(chain: None) -> None:
    """Was: accepted. A fork made by *starting a new segment* rather than by
    branching an existing one — exactly what the fork constraint's own comment
    claimed to prevent."""
    with (
        _connect(user="procurement_ingest") as conn,
        pytest.raises(psycopg.errors.ForeignKeyViolation),
    ):
        _event(conn, 900, bytes.fromhex("ee" * 32), bytes.fromhex("dd" * 32))


def test_a_chain_loop_is_refused(chain: None) -> None:
    """Was: accepted. Two events in one stream sharing a `hash`; nothing was
    unique on `hash` at all."""
    with _connect(user="procurement_ingest") as conn, pytest.raises(psycopg.errors.UniqueViolation):
        _event(conn, 2, CHILD_HASH, GENESIS_HASH)


def test_a_fork_is_still_refused(chain: None) -> None:
    """Regression: the property that already worked must survive the two new
    constraints."""
    with _connect(user="procurement_ingest") as conn, pytest.raises(psycopg.errors.UniqueViolation):
        _event(conn, 5, GENESIS_HASH, bytes.fromhex("99" * 32))


def test_a_duplicate_genesis_is_still_refused(chain: None) -> None:
    """Regression on `NULLS NOT DISTINCT`: an ordinary UNIQUE treats two NULLs as
    distinct and would let one stream grow two unrelated roots."""
    with _connect(user="procurement_ingest") as conn, pytest.raises(psycopg.errors.UniqueViolation):
        _event(conn, 0, None, bytes.fromhex("88" * 32))


# --- ingest: making a row MORE restricted must not be the failing direction ------


def test_a_restricted_document_can_be_ingested_idempotently(schema: None) -> None:
    """Was: `INSERT ... RETURNING` and `INSERT ... ON CONFLICT DO NOTHING` both
    failed for `access_restricted = true`, because a `FOR SELECT` policy applies
    to the new row whenever the statement reads it back. The direction was
    backwards for a confidentiality control — making a row *more* restricted
    failed while making it less succeeded."""
    with _connect(user="procurement_ingest") as ingest:
        returned = ingest.execute(
            """
            INSERT INTO public.document
                (document_id, content_hash, source_uri, document_type,
                 ingested_at, access_restricted)
            VALUES ('ingest-1', 'hash-ingest', 'file:///x.pdf', 'pricing', now(), true)
            RETURNING document_id
            """
        ).fetchone()
        assert returned is not None and returned[0] == "ingest-1"

        # NFR-05 / AC-5: re-ingesting an unchanged document is a no-op.
        ingest.execute(
            """
            INSERT INTO public.document
                (document_id, content_hash, source_uri, document_type,
                 ingested_at, access_restricted)
            VALUES ('ingest-1', 'hash-ingest', 'file:///x.pdf', 'pricing', now(), true)
            ON CONFLICT (content_hash) DO NOTHING
            """
        )

    with _connect(user="procurement_app") as app:
        seen = app.execute(
            "SELECT count(*) FROM public.document WHERE document_id = 'ingest-1'"
        ).fetchone()
    assert seen is not None and seen[0] == 0, "the ingest role's write must stay restricted"

    # Superuser, and only because `public.document` has no DELETE policy: see
    # `_superuser()`. The GUC that used to sit here was a no-op on this identity.
    with _superuser() as conn:
        conn.execute("ALTER TABLE public.document DISABLE TRIGGER USER")
        conn.execute("DELETE FROM public.document WHERE document_id = 'ingest-1'")
        conn.execute("ALTER TABLE public.document ENABLE TRIGGER USER")


def test_the_app_role_cannot_escalate_to_the_ingest_role(schema: None) -> None:
    """The ingest role is only a boundary if the application cannot become it."""
    with _connect(user="procurement_app") as app:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute("SET ROLE procurement_ingest")


# --- privilege hygiene and the standing decisions -------------------------------


def test_no_ann_index_exists_anywhere(schema: None) -> None:
    """plan.md Decision 3a: exact pgvector search. Measured at 50,000 chunks,
    HNSW with a selective filter silently returned 5 rows for a LIMIT 10."""
    with _superuser() as conn:
        found = conn.execute(
            "SELECT indexname FROM pg_indexes WHERE indexdef ~* '(hnsw|ivfflat)'"
        ).fetchall()
    assert found == []


def test_owner_roles_cannot_log_in_and_the_app_is_unprivileged(schema: None) -> None:
    with _superuser() as conn:
        rows = conn.execute(
            """
            SELECT rolname, rolcanlogin, rolsuper, rolbypassrls
            FROM pg_roles WHERE rolname LIKE 'procurement%' OR rolname = 'audit_owner'
            ORDER BY rolname
            """
        ).fetchall()
    by_name: dict[str, tuple[str, bool, bool, bool]] = {r[0]: r for r in rows}
    for owner in ("procurement_owner", "audit_owner"):
        assert by_name[owner][1] is False, f"{owner} must be NOLOGIN"
    for unprivileged in ("procurement_app", "procurement_ingest"):
        assert by_name[unprivileged][2] is False, f"{unprivileged} must not be superuser"
        assert by_name[unprivileged][3] is False, f"{unprivileged} must not have BYPASSRLS"


def test_a_duplicate_content_hash_is_refused(seeded: None) -> None:
    """NFR-05 / AC-5: re-ingesting an unchanged document creates no duplicate."""
    with _connect(user="procurement_ingest") as conn, pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            """
            INSERT INTO public.document
                (document_id, content_hash, source_uri, document_type, ingested_at)
            VALUES ('open-2', 'hash-open', 'file:///again.pdf', 'spec_sheet', now())
            """
        )


def test_no_project_role_is_a_member_of_another(schema: None) -> None:
    """Decision 9's boundary is defeated by a membership as surely as by
    `SUPERUSER`, and much more quietly.

    `00_roles.sql` re-asserts role *attributes* on every run, which closed one
    hole. A membership is not an attribute:

        GRANT procurement_ingest TO procurement_app;

    survived a clean re-run of that file with every attribute check still
    passing, and let `procurement_app` execute `SET ROLE procurement_ingest`.
    Measured before the fix; `current_user` came back `procurement_ingest`.

    `test_the_app_role_cannot_escalate_to_the_ingest_role` next door does go red
    in that state, but it names the *consequence*. This names the cause, so a
    failure points at `pg_auth_members` rather than leaving a reader to work
    backwards from a denied `SET ROLE`.
    """
    with _superuser() as conn:
        memberships = conn.execute(
            """
            SELECT g.rolname, m.rolname
            FROM pg_auth_members am
            JOIN pg_roles g ON am.roleid = g.oid
            JOIN pg_roles m ON am.member = m.oid
            WHERE g.rolname LIKE 'procurement%' OR g.rolname = 'audit_owner'
            ORDER BY 1, 2
            """
        ).fetchall()
    assert memberships == [], (
        f"project roles are members of one another: {memberships}. A membership "
        "hands over every privilege of the granting role for no cost at all -- "
        "RLS role matching follows inherited membership, so the granting role's "
        "policies apply with no SET ROLE issued -- and it survives re-running "
        "00_roles.sql."
    )
