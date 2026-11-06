"""Structural assertions over the DDL in `sql/`.

**What this is and is not.** These are text-level checks over the migration
files. They cannot prove PostgreSQL behaves as the DDL intends - only a live
server can, and the live run that was actually performed is recorded in
sql/README.md's "Live verification actually performed" table, with the exact
commands and results.

What they *can* do is stop a fix silently regressing. Every property below
corresponds to a defect that was measured against a running cluster and then
closed; without a test, closing it is a comment in a file nobody re-runs. So each
test names the attack it descends from, and the assertion is chosen to fail if
that specific line is reverted rather than merely if the file is reformatted.

**That premise has changed.** This docstring used to end "the repo has no
live-database fixture and no driver dependency, and adding one would change the
CI story for the whole project" — which was true, and was the reason to accept
structural checks alone. The CI story has since been changed deliberately:
`tests/test_sql_behaviour.py` runs the attacks against a real server, and the
`sql` job in `.github/workflows/ci.yml` supplies one from a pgvector service
container.

So the division of labour is now explicit rather than a concession:

- **here** — text-level, no server, runs in every developer's default `pytest`.
  Catches a fix being deleted or edited.
- **`test_sql_behaviour.py`** — runs the attack. Catches a fix that was never
  right, which is the failure mode this schema actually had: `USING (true)` on a
  write policy reads as "the write path is unrestricted", not as "the
  application role can declassify every confidential row in one statement".

Both are worth keeping. The structural checks stay green without Postgres, so
they still run for a contributor who has not started one; and they fail on a
reverted line even where two independent mechanisms would keep the *behaviour*
correct, which is a real gap the behavioural suite hit and had to be widened for.
"""

from __future__ import annotations

import pathlib
import re

import pytest

SQL_DIR = pathlib.Path(__file__).parent.parent / "sql"


def _sql(name: str) -> str:
    return (SQL_DIR / name).read_text(encoding="utf-8")


def _statements(text: str) -> list[str]:
    """Statement-ish chunks with `--` comments stripped.

    Crude on purpose: these files are hand-written DDL, not arbitrary SQL, and
    the alternative is a parser dependency. Comment stripping is the part that
    matters, because every one of these files argues its case in prose that
    quotes the very statements being asserted about - matching a GRANT inside a
    comment that explains why the GRANT is narrow would invert the test.
    """
    without_comments = re.sub(r"--[^\n]*", "", text)
    return [s.strip() for s in without_comments.split(";") if s.strip()]


ALL_FILES = sorted(p.name for p in SQL_DIR.glob("0*.sql"))


def test_the_expected_files_are_all_present() -> None:
    """Guards every test below: they read files by name, so a rename would make
    them error rather than pass, but a *deletion* plus a missing assertion here
    would leave a hole."""
    assert ALL_FILES == [
        "00_roles.sql",
        "01_extensions_and_settings.sql",
        "02_document.sql",
        "03_chunk.sql",
        "04_claim.sql",
        "05_conflict.sql",
        "06_resolution.sql",
        "07_audit_event.sql",
        "08_job.sql",
        "09_claim_natural_key_condition.sql",
        "09a_hardening_catch_up.sql",
    ]


def test_claim_natural_key_includes_condition() -> None:
    """Python `FieldClaim.claim_key()` includes `grouping_key()`. SQL unique did
    not, so the first production writer `ON CONFLICT`s the Sungrow trio into
    `_existing_claim_id` and cannot persist D-1.

    Asserted on the create-time constraint *and* the forward migration, because
    a live database that already applied `04` will not re-run it. `NULLS NOT
    DISTINCT` ensures nullable `nameplate` values still deduplicate on retry.
    """
    create = _sql("04_claim.sql")
    match = re.search(
        r"CONSTRAINT claim_natural_key UNIQUE NULLS NOT DISTINCT\s*\((.*?)\)",
        create,
        re.DOTALL,
    )
    assert match is not None
    columns = [part.strip() for part in match.group(1).split(",")]
    assert "condition" in columns
    migration = _sql("09_claim_natural_key_condition.sql")
    assert "UNIQUE NULLS NOT DISTINCT" in migration
    assert "claim_natural_key" in migration
    assert re.search(r"\bcondition\b", migration) is not None


# --- S9: a pre-existing over-privileged role must not survive a re-run ---------

_MANAGED_ROLES = ("procurement_owner", "audit_owner", "procurement_app", "procurement_ingest")


@pytest.mark.parametrize("role", _MANAGED_ROLES)
def test_every_role_has_its_attributes_reasserted_unconditionally(role: str) -> None:
    """Measured defect: `00_roles.sql` guarded creation with `IF NOT EXISTS` and
    stopped there, so attributes were applied only at first creation. Against a
    live cluster, `ALTER ROLE procurement_app SUPERUSER BYPASSRLS` followed by a
    clean re-run of the file left both attributes set and every RLS policy in the
    schema inert for that role - and the migration printed no errors.

    The `ALTER ROLE` must be a top-level statement, not inside the `DO` block
    that creates the role, or it runs only on the creation path again.
    """
    statements = _statements(_sql("00_roles.sql"))
    altered = [
        s
        for s in statements
        if re.match(rf"^ALTER ROLE\s+{role}\b", s) and not s.upper().startswith("DO")
    ]
    assert altered, f"{role} has no unconditional ALTER ROLE; a re-run cannot fix a bad attribute"
    combined = " ".join(altered).upper()
    for attribute in ("NOSUPERUSER", "NOBYPASSRLS", "NOCREATEROLE", "NOCREATEDB", "NOREPLICATION"):
        assert attribute in combined, f"{role}'s ALTER ROLE does not re-assert {attribute}"


def test_the_owner_roles_are_reasserted_nologin() -> None:
    """NOLOGIN is the whole of "nothing, ever, connects as an owner": FORCE ROW
    LEVEL SECURITY constrains owners, but an owner session can still `ALTER TABLE
    ... DISABLE TRIGGER`, which is how the append-only tripwires are bypassed."""
    text = " ".join(_statements(_sql("00_roles.sql")))
    for role in ("procurement_owner", "audit_owner"):
        match = re.search(rf"ALTER ROLE\s+{role}\s+([A-Z ]+)", text)
        assert match is not None and "NOLOGIN" in match.group(1), (
            f"{role} is not re-asserted NOLOGIN"
        )


def test_a_bad_attribute_is_asserted_against_and_not_only_altered() -> None:
    """The `ALTER ROLE`s need superuser (SUPERUSER/BYPASSRLS/REPLICATION cannot
    be cleared by a CREATEROLE service account), so on a weaker bootstrap
    identity they fail rather than fix. Reading `pg_roles` needs no privilege at
    all, so the assertion block is what makes the guarantee hold everywhere -
    and it names the problem instead of leaving a reader with "permission denied
    to alter role"."""
    text = _sql("00_roles.sql")
    assert "pg_catalog.pg_roles" in text
    assert re.search(r"rolsuper\s+OR\s+rolbypassrls", text), (
        "no assertion block reading the actual role attributes back"
    )
    assert "RAISE EXCEPTION" in text


# --- S5: the write path must be able to write MORE restricted rows -------------


def _created_roles() -> set[str]:
    return set(re.findall(r"CREATE ROLE\s+(\w+)", _sql("00_roles.sql")))


def test_a_separate_write_role_exists() -> None:
    """RLS applies a table's FOR SELECT policy as a WITH CHECK on the proposed
    row whenever an INSERT carries RETURNING or ON CONFLICT. Measured: the
    documented idempotent-ingest idiom failed for `access_restricted = true` and
    succeeded for `false` - the schema penalised the safe action.

    Matched on a whole word, not as a substring: the first version of this test
    asserted `"CREATE ROLE procurement_ingest" in text`, which a revert check
    walked straight through by renaming the role to `procurement_ingest_x` -
    still a substring match, still green, and every policy and grant below then
    referenced a role that did not exist.
    """
    assert "procurement_ingest" in _created_roles()


def test_every_role_referenced_anywhere_is_actually_created() -> None:
    """The generalisation of the bug above, and the more valuable test: a
    `GRANT ... TO x` or `CREATE POLICY ... TO x` naming a role `00_roles.sql`
    does not create is a migration that fails on a fresh cluster, and this file
    set is applied by lexical order with no other cross-file consistency check.
    """
    created = _created_roles() | {"PUBLIC"}
    for filename in ALL_FILES:
        for statement in _statements(_sql(filename)):
            head = statement.upper()
            if not (head.startswith("GRANT") or head.startswith("CREATE POLICY")):
                continue
            for match in re.finditer(r"\bTO\s+([\w, ]+?)(?:\s+USING|\s+WITH|;|$)", statement):
                for role in match.group(1).split(","):
                    role = role.strip()
                    # Object names (GRANT ... ON TABLES TO role) and SQL keywords
                    # are filtered by requiring an identifier that is not a verb.
                    if not role or role.upper() in {"TABLES", "FUNCTIONS", "SEQUENCES"}:
                        continue
                    assert role in created, (
                        f"{filename} references role {role!r}, which 00_roles.sql "
                        f"does not create. Created roles: {sorted(created)}"
                    )


@pytest.mark.parametrize(
    ("filename", "table"),
    [("02_document.sql", "public.document"), ("03_chunk.sql", "public.chunk")],
)
def test_the_write_role_can_read_back_what_it_writes(filename: str, table: str) -> None:
    """A `FOR SELECT ... TO procurement_ingest` policy is the specific thing that
    makes `INSERT ... RETURNING` and `ON CONFLICT` work for a restricted row. An
    INSERT policy alone does not: the failure is on the read-back."""
    text = _sql(filename)
    assert re.search(
        rf"CREATE POLICY \w+ ON {re.escape(table)}\s+FOR SELECT TO procurement_ingest", text
    ), f"{table} has no read-back policy for the write role"


def test_the_read_back_policies_are_scoped_to_the_write_role() -> None:
    """The one place in this schema where a policy IS role-scoped, and the
    scoping is load-bearing: an unscoped `USING (true)` SELECT policy would hand
    procurement_app the same unrestricted read and delete the control entirely.
    """
    for filename in ALL_FILES:
        for statement in _statements(_sql(filename)):
            if not statement.upper().startswith("CREATE POLICY"):
                continue
            if "FOR SELECT" not in statement.upper():
                continue
            if re.search(r"USING\s*\(\s*true\s*\)", statement, re.IGNORECASE):
                assert "TO procurement_ingest" in statement, (
                    f"{filename}: an unscoped FOR SELECT USING (true) policy defeats "
                    f"confidentiality for every role -- {statement[:120]}"
                )


# --- S7: restricted content is not readable through the other tables -----------

#: Tables that hold document content, not just references to it, with the file
#: that creates them. Each was measured returning restricted content to
#: procurement_app for a document that role could not see.
_CONTENT_TABLES = [
    ("02_document.sql", "public.document"),
    ("03_chunk.sql", "public.chunk"),
    ("04_claim.sql", "public.claim"),
    ("05_conflict.sql", "public.conflict"),
    ("06_resolution.sql", "public.resolution"),
    ("07_audit_event.sql", "audit.event"),
    ("08_job.sql", "public.job"),
]


@pytest.mark.parametrize(("filename", "table"), _CONTENT_TABLES)
def test_every_table_holding_document_content_forces_rls(filename: str, table: str) -> None:
    """RLS on `document` and `chunk` alone implemented NFR-03 on two of the seven
    tables that hold the material. `claim.value`, `audit.event.payload`,
    `conflict.explanation`, `resolution.value_before`/`value_after` and
    `job.payload` all carry it.

    FORCE as well as ENABLE: without FORCE the policies do not apply to the table
    owner, and `procurement_owner` is a role a maintenance session can assume."""
    text = _sql(filename)
    assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in text
    assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in text, (
        f"{table} enables RLS without FORCE, so the owner role is exempt"
    )


@pytest.mark.parametrize(("filename", "table"), _CONTENT_TABLES)
def test_every_such_table_has_a_confidentiality_select_policy(filename: str, table: str) -> None:
    """Enabling RLS without a SELECT policy denies everything, which is not the
    requirement either. The policy must gate on the document's restriction and
    admit an entitled session via `app.allow_restricted`."""
    policies = [
        s
        for s in _statements(_sql(filename))
        if s.upper().startswith("CREATE POLICY")
        and f"ON {table}" in s
        and "FOR SELECT" in s.upper()
        and "TO procurement_ingest" not in s
    ]
    assert policies, f"{table} has RLS enabled but no confidentiality SELECT policy"
    combined = " ".join(policies)
    assert "app.allow_restricted" in combined, (
        f"{table}'s SELECT policy has no entitlement escape hatch"
    )
    assert re.search(r"_is_restricted\(|NOT access_restricted", combined), (
        f"{table}'s SELECT policy does not gate on the document's restriction"
    )


@pytest.mark.parametrize(("filename", "table"), _CONTENT_TABLES)
def test_every_such_table_denies_the_app_role_restricted_rows_outright(
    filename: str, table: str
) -> None:
    """Was: every policy above admits `app.allow_restricted = 'true'`, and
    `procurement_app` sets that GUC itself — by `SET`, by `set_config`, by
    `SET LOCAL`, and by `options='-c app.allow_restricted=true'` in the
    connection string, which lands during startup and so declassifies the
    session before it issues a statement. Measured: 1 visible row became 2 on
    all seven tables at once, the CONFIDENTIAL pricing chunk included.

    The fix must be a *separate* `AS RESTRICTIVE` policy, not an edit to the
    permissive ones. Permissive policies are OR'd, so nothing added to them can
    narrow the result; restrictive policies are AND'd. Deleting the GUC clause
    from the existing policies instead is not an option either — it breaks the
    chunk inheritance trigger's parent lookup, which reaches visibility through
    exactly that clause.
    """
    policies = [
        s
        for s in _statements(_sql(filename))
        if s.upper().startswith("CREATE POLICY")
        and f"ON {table}" in s
        and "AS RESTRICTIVE" in s.upper()
    ]
    assert policies, (
        f"{table} has no RESTRICTIVE policy, so procurement_app can read every "
        "restricted row on it by setting app.allow_restricted itself"
    )
    combined = " ".join(policies)
    assert "TO procurement_app" in combined, (
        f"{table}'s restrictive policy is not scoped to procurement_app. "
        "Unscoped, it also applies to procurement_owner -- and so to "
        "document_is_restricted(), which then reports every document restricted "
        "and hides the entire store from everyone."
    )
    assert "app.allow_restricted" not in combined, (
        f"{table}'s restrictive policy admits the very GUC it exists to defeat"
    )
    assert re.search(r"_is_restricted\(|NOT access_restricted", combined), (
        f"{table}'s restrictive policy does not gate on the document's restriction"
    )


@pytest.mark.parametrize(("filename", "table"), _CONTENT_TABLES)
def test_the_restrictive_policies_keep_raising_restriction_possible(
    filename: str, table: str
) -> None:
    """`WITH CHECK (true)` is spelled out on every one of them, and must stay.

    PostgreSQL reuses the `USING` expression as the `WITH CHECK` when the latter
    is omitted. Measured, that makes `INSERT ... access_restricted = true` fail
    for `procurement_app` while the same insert with `false` succeeds — the
    schema penalising the *safe* action, which is the exact defect 00_roles.sql
    documents at length and solved by creating `procurement_ingest`. Restriction
    has to stay a ratchet for this role: `USING` denies it any restricted row to
    read, update or delete; `WITH CHECK (true)` leaves it free to raise
    restriction, never to lower it."""
    policies = [
        s
        for s in _statements(_sql(filename))
        if s.upper().startswith("CREATE POLICY")
        and f"ON {table}" in s
        and "AS RESTRICTIVE" in s.upper()
    ]
    for policy in policies:
        assert re.search(r"WITH CHECK\s*\(\s*true\s*\)", policy, re.IGNORECASE), (
            f"{table}'s restrictive policy omits WITH CHECK (true), so the USING "
            f"expression becomes the check and writing a MORE restricted row is "
            f"the failing direction -- {policy[:120]}"
        )


def test_the_derivation_helpers_are_security_definer_and_pinned() -> None:
    """Both helpers must see restricted parents, and neither ownership nor
    SECURITY DEFINER alone grants that: the tables they read are FORCE ROW LEVEL
    SECURITY, which applies to the owner too. `SET app.allow_restricted` is what
    grants visibility, scoped to the invocation. `search_path` is pinned per the
    standard SECURITY DEFINER hardening.

    A helper that lost the setting would report every document unrestricted -
    failing *open*, in the one function whose job is to fail closed."""
    for filename, function in (
        ("02_document.sql", "public.document_is_restricted"),
        ("05_conflict.sql", "public.conflict_is_restricted"),
    ):
        text = _sql(filename)
        body = text[text.index(f"CREATE FUNCTION {function}") :]
        body = body[: body.index("ALTER FUNCTION")]
        assert "SECURITY DEFINER" in body, f"{function} is not SECURITY DEFINER"
        assert "SET search_path" in body, f"{function} does not pin search_path"
        assert "SET app.allow_restricted" in body, (
            f"{function} cannot see restricted rows, so it reports everything unrestricted"
        )
        assert f"REVOKE EXECUTE ON FUNCTION {function}" in text


def test_every_function_pins_search_path() -> None:
    """Every `CREATE FUNCTION` across `sql/*.sql` must pin an explicit
    `SET search_path` (not just `SECURITY DEFINER` helpers) so trigger functions
    cannot resolve unqualified objects through a caller-controlled search_path."""
    function_pattern = re.compile(
        r"CREATE FUNCTION\s+([\w.]+)\s*\(.*?\$\$.*?\$\$[^;]*;",
        re.DOTALL,
    )
    found: list[str] = []
    for filename in ALL_FILES:
        without_comments = re.sub(r"--[^\n]*", "", _sql(filename))
        raw_count = len(re.findall(r"\bCREATE FUNCTION\b", without_comments))
        matches = list(function_pattern.finditer(without_comments))
        assert len(matches) == raw_count, f"{filename}: failed to parse all CREATE FUNCTION blocks"
        for match in matches:
            func_name = match.group(1)
            block = match.group(0)
            found.append(func_name)
            assert re.search(r"\bSET\s+search_path\s*=", block), (
                f"{filename}: {func_name} does not pin SET search_path"
            )
    assert len(found) >= 8


def test_public_schema_revokes_create_from_public() -> None:
    """`01_extensions_and_settings.sql` explicitly revokes `CREATE ON SCHEMA public
    FROM PUBLIC` (defense-in-depth alongside PostgreSQL 15+ defaults) and grants
    explicit `USAGE` to the managed roles."""
    statements = _statements(_sql("01_extensions_and_settings.sql"))
    assert any(
        re.match(r"^REVOKE\s+CREATE\s+ON\s+SCHEMA\s+public\s+FROM\s+PUBLIC$", s, re.IGNORECASE)
        for s in statements
    ), "01_extensions_and_settings.sql does not revoke CREATE ON SCHEMA public FROM PUBLIC"
    assert any(
        re.match(r"^GRANT\s+USAGE\s+ON\s+SCHEMA\s+public\s+TO\b", s, re.IGNORECASE)
        for s in statements
    ), "01_extensions_and_settings.sql does not explicitly grant USAGE ON SCHEMA public"


# --- the RLS/trigger interaction the append-only tables depend on --------------

_APPEND_ONLY = [
    ("04_claim.sql", "public.claim"),
    ("06_resolution.sql", "public.resolution"),
    ("07_audit_event.sql", "audit.event"),
]


@pytest.mark.parametrize(("filename", "table"), _APPEND_ONLY)
def test_append_only_tables_keep_rows_eligible_so_the_tripwire_still_fires(
    filename: str, table: str
) -> None:
    """The obvious move on an append-only table is to declare no UPDATE/DELETE
    policy, so a mis-grant finds zero eligible rows. Measured, that is worse:
    RLS filters rows out *before* a FOR EACH ROW trigger runs, so enabling RLS
    turned `UPDATE public.claim SET confidence = 0.1` as procurement_owner from a
    raised exception into a silent `UPDATE 0`.

    The data survived either way, but plan.md Decision 9 picks the trigger
    tripwire precisely because it is loud, and its attack matrix lists these
    cells as "Trigger: blocked". A silent no-op is not blocked, it is unmeasured
    - the empty-table trap from sql/README.md in a new costume.
    """
    text = _sql(filename)
    for verb in ("UPDATE", "DELETE"):
        assert re.search(
            rf"CREATE POLICY \w+ ON {re.escape(table)} FOR {verb} USING \(true\)", text
        ), (
            f"{table} has no permissive FOR {verb} policy, so RLS silently swallows "
            "the statement instead of letting the append-only trigger raise"
        )


@pytest.mark.parametrize(("filename", "table"), _APPEND_ONLY)
def test_append_only_tables_grant_no_write_verbs(filename: str, table: str) -> None:
    """The policies above are only safe because no role is granted the verbs.
    Privilege separation is the boundary; the trigger is the tripwire."""
    for statement in _statements(_sql(filename)):
        if not statement.upper().startswith("GRANT"):
            continue
        if f"ON {table}" not in statement:
            continue
        granted = statement.upper().split(" ON ")[0]
        assert "UPDATE" not in granted, f"{table} grants UPDATE: {statement[:100]}"
        assert "DELETE" not in granted, f"{table} grants DELETE: {statement[:100]}"
        assert "TRUNCATE" not in granted, f"{table} grants TRUNCATE: {statement[:100]}"


@pytest.mark.parametrize(("filename", "table"), _APPEND_ONLY)
def test_append_only_tables_have_a_statement_level_truncate_tripwire(
    filename: str, table: str
) -> None:
    """TRUNCATE fires no row-level triggers, so the `FOR EACH ROW` trigger cannot
    see it. `TRUNCATE public.claim CASCADE` took `resolution` and
    `conflict_candidate` with it while README claimed all three verbs refused."""
    text = _sql(filename)
    assert re.search(
        rf"CREATE TRIGGER \w+\s+BEFORE TRUNCATE ON {re.escape(table)}\s+FOR EACH STATEMENT",
        text,
    ), f"{table} has no statement-level TRUNCATE tripwire"


# --- S8: mutable tables grant column-level UPDATE, not full-table --------------

#: The mutable tables and the columns each may legitimately change after INSERT.
_COLUMN_SCOPED_UPDATE = {
    "02_document.sql": ("public.document", {"access_restricted", "data_vintage"}),
    "05_conflict.sql": (
        "public.conflict",
        {"status", "lease_owner", "lease_expires_at", "reopen_count"},
    ),
    "08_job.sql": (
        "public.job",
        {"status", "attempt", "next_attempt_at", "lease_owner", "lease_expires_at", "last_error"},
    ),
}


@pytest.mark.parametrize("filename", sorted(_COLUMN_SCOPED_UPDATE))
def test_update_is_granted_column_by_column(filename: str) -> None:
    """`08_job.sql` granted full-table UPDATE while `document` and `conflict` in
    the same file set had already established the opposite discipline. Measured
    as procurement_app: `UPDATE public.job SET idempotency_key = 'HIJACKED'`
    succeeded.

    That key is the whole of I.2's at-least-once guarantee - rewriting it makes
    `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING` stop deduplicating, so
    the retry it exists to absorb becomes a second live job racing the first. The
    same worker whose bug corrupts the key is the one whose retries it protects
    against."""
    table, allowed = _COLUMN_SCOPED_UPDATE[filename]
    grants = [
        s
        for s in _statements(_sql(filename))
        if s.upper().startswith("GRANT") and f"ON {table}" in s and "UPDATE" in s.upper()
    ]
    assert grants, f"{table} has no UPDATE grant at all"
    for statement in grants:
        match = re.search(r"UPDATE\s*\(([^)]*)\)", statement, re.IGNORECASE)
        assert match is not None, (
            f"{table} grants full-table UPDATE, which includes every column the "
            f"row's identity depends on: {statement[:120]}"
        )
        columns = {c.strip() for c in match.group(1).split(",")}
        assert columns <= allowed, (
            f"{table} grants UPDATE on unexpected columns: {columns - allowed}"
        )


def test_the_job_identity_columns_are_never_updatable() -> None:
    """Named individually rather than left to the set comparison above, because
    these are the columns whose mutation is silently corrupting rather than
    merely wrong: they define which unit of work the row *is*, not how far it has
    got."""
    _, allowed = _COLUMN_SCOPED_UPDATE["08_job.sql"]
    for column in (
        "idempotency_key",
        "stage",
        "document_id",
        "payload",
        "created_at",
        "updated_at",
    ):
        assert column not in allowed


def test_recorded_at_has_no_clock_default() -> None:
    """D-13 section 4 hashes recorded_at. A DEFAULT clock_timestamp() lets a
    forgotten INSERT succeed with a timestamp the caller never hashed."""
    without_comments = re.sub(r"--[^\n]*", "", _sql("07_audit_event.sql"))
    column = re.search(
        r"recorded_at\s+timestamptz\s+NOT NULL(?:\s+DEFAULT\s+[^,\n]+)?",
        without_comments,
    )
    assert column is not None
    assert "DEFAULT" not in column.group(0)
    assert "clock_timestamp()" not in without_comments


def test_app_role_chunk_grant_is_select_only() -> None:
    """SEC-HIGH-2: procurement_app reads chunks; procurement_ingest mutates them."""
    grants = [
        s
        for s in _statements(_sql("03_chunk.sql"))
        if s.upper().startswith("GRANT") and "ON public.chunk" in s and "procurement_app" in s
    ]
    assert grants == ["GRANT SELECT ON public.chunk TO procurement_app"]


def test_conflict_candidate_forces_rls_via_parent_conflict() -> None:
    """SEC-HIGH-3: junction visibility inherits `conflict` without calling
    `conflict_is_restricted` on this table (that path recurses)."""
    text = _sql("05_conflict.sql")
    assert "ALTER TABLE public.conflict_candidate ENABLE ROW LEVEL SECURITY" in text
    assert "ALTER TABLE public.conflict_candidate FORCE ROW LEVEL SECURITY" in text
    policies = [
        s
        for s in _statements(text)
        if s.upper().startswith("CREATE POLICY") and "ON public.conflict_candidate" in s
    ]
    assert policies
    combined = " ".join(policies)
    assert "conflict_is_restricted" not in combined
    assert re.search(r"EXISTS\s*\(\s*SELECT 1\s+FROM public\.conflict", combined)
    assert "TO procurement_owner, audit_owner" in combined or (
        "TO procurement_owner" in combined and "TO audit_owner" in combined
    )
