# SQL — contracts C1 and C4

DDL only, per specs/001-procurement-agent/tasks.md T0.1 (Phase 0, contract
freeze) and the C1/C4/C8 contracts it gates. No Python, no ORM. Everything here
implements plan.md Decisions 1, 3, 3a, 3b, 3c and 9, and tasks.md WP-H (audit
log) and WP-I (runner) at the schema layer.

**This has now been applied to a live server**, twice, on different builds: a
PostgreSQL 17.10 cluster with pgvector 0.8.6, and a PostgreSQL 16 cluster with
pgvector **0.6.0** — the older pgvector matters, because
`01_extensions_and_settings.sql` guards `hnsw.iterative_scan` (a 0.8.0 GUC) and
that guard had never been exercised on a build that lacks it. See "Live
verification actually performed" below for the checklist that was run, and "What
remains unverified" at the end for what a live run still could not reach.

Decision 3 pins PostgreSQL 18. Neither run was on 18, so anything genuinely
18-specific is still unproven; nothing in these files depends on an 18-only
feature, and the newest syntax used (`UNIQUE NULLS NOT DISTINCT`, PostgreSQL 15+)
executed correctly on both.

## Apply order

Files are numbered so `psql` (or any tool that applies files in lexical order)
gets the dependency order right automatically:

| File | Contents | Depends on |
|---|---|---|
| `00_roles.sql` | `procurement_owner`, `audit_owner` (both NOLOGIN), `procurement_app` and `procurement_ingest` (LOGIN, non-superuser); unconditional attribute re-assertion | — |
| `01_extensions_and_settings.sql` | `vector`, `pg_trgm`; `audit` schema; `hnsw.iterative_scan = relaxed_order` | `00_roles.sql` |
| `02_document.sql` | `document` + RLS | `00`, `01` |
| `03_chunk.sql` | `chunk` + RLS + tsvector/trgm indexes | `01`, `02` |
| `04_claim.sql` | `claim` + RLS, plus the shared `public.reject_mutation()` trigger function | `02` |
| `05_conflict.sql` | `conflict` + RLS, `conflict_candidate` (no RLS — see below), `public.conflict_is_restricted()` | `04` |
| `06_resolution.sql` | `resolution` + RLS | `04`, `05` |
| `07_audit_event.sql` | `audit.event` + RLS, hash-chain columns, both trigger tripwires | `01`, `02` |
| `08_job.sql` | `job` (stage state machine) + RLS | `02` |
| `09_claim_natural_key_condition.sql` | fold `condition` into `claim_natural_key` | `04` |

Reserved numbers (files are **not** in this directory yet; 4a and 7 write them):

| File | Reserved for |
|---|---|
| `10_claim_resolution_link.sql` | Story 4 / P2-C6 — `claim.resolution_id` |
| `11_audit_run_event.sql` | Story 4 / P2-C7 — `audit.run_event` |
| `12_audit_event_taxonomy.sql` | Story 4 / P2-C7 — drop `web_search` from `audit.event`, drop `recorded_at` DEFAULT |
| `13_access_denylist.sql` | Story 7 — deny-list (applied empty until facts land) |
| `14_restricted_group.sql` | Story 7 outcome B — `restricted_group`; stay in `sql/proposals/` until adopted |

`15+` is unassigned. Track 0 does not write `sql/10`–`sql/14`.

## Track 0 freeze

Story 4 / 7 write the files; Track 0 freezes the shapes they must not drift from.
These strings are the pin `tests/test_phase2_contracts.py` copies verbatim from
`schema.sql_freeze`.

Reserved filenames:

- `10_claim_resolution_link.sql`
- `11_audit_run_event.sql`
- `12_audit_event_taxonomy.sql`
- `13_access_denylist.sql`
- `14_restricted_group.sql`

`10_claim_resolution_link.sql` adds `claim.resolution_id` and CHECK constraint
`claim_human_carries_resolution`:
`(extractor_version LIKE 'human:%') = (resolution_id IS NOT NULL)`

`11_audit_run_event.sql` introduces `audit.run_event` whose `event_type` values
are `web_search`, `compose_override`, `run_started`, `run_finished`.

`12_audit_event_taxonomy.sql` removes `web_search` from `audit.event` and
drops `recorded_at`'s DEFAULT.

Two shared helpers cross file boundaries: `02_document.sql` defines
`public.document_is_restricted(text)`, the confidentiality derivation every table
carrying a `document_id` keys on, and `05_conflict.sql` defines
`public.conflict_is_restricted(text)` for the two tables that do not have one.
Both are `SECURITY DEFINER`; decision 10 below says why.

Apply with, e.g.:

```sh
for f in sql/[0-9][0-9]_*.sql; do
  psql -v ON_ERROR_STOP=1 -f "$f" "$DATABASE_URL"
done
```

`ON_ERROR_STOP=1` matters: without it, `psql` keeps going past a failed
statement and a broken migration can look like it "applied."

**Who runs this.** Every file assumes it executes as a bootstrap identity with
enough privilege to `CREATE ROLE`, `CREATE EXTENSION`, `ALTER DATABASE`, and
`ALTER TABLE ... OWNER TO`. This identity is **not** `procurement_owner`,
`audit_owner`, `procurement_app` or `procurement_ingest`, is not defined by any
file here, and should not be used for anything once migrations are applied.

`00_roles.sql` specifically requires a **superuser**: it clears `SUPERUSER`,
`BYPASSRLS` and `REPLICATION` unconditionally on every run, and only a superuser
may clear those — a `CREATEROLE` service account is refused with "permission
denied to alter role" even when the attribute is already unset. See section 1 of
the verification checklist for why that is the right trade. Files `01`–`08` run
under a CI/CD service account with `CREATEDB`/`CREATEROLE` and the ability to
create extensions.

**Idempotency is asymmetric on purpose.** `00_roles.sql`'s `CREATE ROLE`
statements are guarded (`DO $$ ... IF NOT EXISTS ...`) so the file is safe to
re-run — roles are cluster-global and may legitimately already exist. The
*attributes*, by contrast, are re-asserted unconditionally on every run, because
guarding those behind the same `IF NOT EXISTS` meant a `procurement_app` that had
somehow acquired `SUPERUSER BYPASSRLS` survived a clean re-run of the one file
whose purpose is to prevent that — measured, with the migration printing no
errors. Every
`CREATE TABLE` in `02` through `08` is **not** guarded with `IF NOT EXISTS`,
and re-running this file set against a database that already has these tables
is expected to fail loudly (`relation already exists`) rather than silently
no-op. That is the intended behaviour for a numbered, forward-only migration
set — it is not a real migration tool with version tracking, and pretending
otherwise by adding `IF NOT EXISTS` everywhere would hide a re-run that
shouldn't be happening.

## Syntax verification actually performed

No live PostgreSQL server was used (per the constraint this work was done
under), but a real PostgreSQL *grammar* was available and used: `pglast`
(Python bindings over `libpg_query`, the actual Postgres parser extracted as a
standalone library) was installed into a throwaway virtualenv and used to
parse every file. `pglast.get_postgresql_version()` reports `(18, 4)` — the
exact major version plan.md Decision 3 pins — so this is not a stand-in for a
similar grammar, it is the real one.

All nine files parse cleanly under this grammar: `00_roles.sql` (6
statements), `01_extensions_and_settings.sql` (5), `02_document.sql` (11),
`03_chunk.sql` (18), `04_claim.sql` (10), `05_conflict.sql` (13),
`06_resolution.sql` (6), `07_audit_event.sql` (15), `08_job.sql` (10). This
means every `CREATE TABLE`, `CREATE INDEX`, `ALTER TABLE`, `GRANT`, `REVOKE`,
`CREATE POLICY`, `CREATE TRIGGER`, `CREATE FUNCTION` (outer syntax and
dollar-quoting), `DO` block, and `ALTER DATABASE`/`ALTER DEFAULT PRIVILEGES`
statement in this directory is syntactically valid PostgreSQL 18 — including
the `NULLS NOT DISTINCT` constraint on `audit.event` and the `GENERATED ALWAYS
AS IDENTITY` columns, both of which a pre-15 grammar would have rejected.

> **Correction.** An earlier version of this note claimed the parser had caught
> a defect: that `to_tsvector('english', chunk_text)` cannot sit inside
> `GENERATED ALWAYS AS ... STORED` because the two-argument form is `STABLE`.
> That is backwards. Postgres's own catalog marks `to_tsvector(regconfig, text)`
> **IMMUTABLE** (`pg_proc.provolatile = 'i'`); it is the *one*-argument
> `to_tsvector(text)` that is `STABLE`, because it reads
> `default_text_search_config` at runtime. Verified against a live server (see
> below). The `chunk_text_to_tsvector()` wrapper this introduced has been
> removed and the builtin two-argument call restored. Worth recording rather
> than quietly deleting: wrapping a genuinely `STABLE` function and declaring
> the wrapper `IMMUTABLE` is not a harmless workaround — it lies to the planner
> and can silently corrupt any index built on it.

One gap in this tooling, not in the DDL: `pglast`'s `parse_plpgsql` — a
separate, explicitly experimental entry point for validating procedural
function *bodies* — fails on every `RETURNS trigger` function tested,
including a bare `BEGIN END;` body with no statements at all, while the exact
same `RAISE EXCEPTION` body parses without complaint when the same function is
declared `RETURNS void` instead. That isolates the failure to the tool's
handling of trigger-implicit variables (`NEW`/`OLD`/`TG_OP`), not to anything
in `reject_mutation`, `reject_truncate`, or `job_touch_updated_at`'s actual
content — their `RAISE EXCEPTION` bodies (string concatenation, the `%`/`TG_OP`
substitution, the doubled-quote escaping) were separately confirmed valid
plpgsql via that `RETURNS void` substitution, and `NEW.updated_at :=
clock_timestamp();` was checked the same way. This was reasoned through and
spot-checked, not proven exhaustively by a tool, which is why it is called out
here rather than folded silently into "syntax verified."

## Live verification actually performed

> **This is now automated.** The table below records runs made by hand, and by
> hand was the whole problem: closing a defect against a cluster and writing the
> result here left nothing that would notice if the fix were reverted.
> `tests/test_sql_behaviour.py` runs these attacks as tests, and the `sql` job in
> `.github/workflows/ci.yml` gives it a pgvector service container on every pull
> request. Each fix below was revert-checked against that suite — put the old
> line back and a named test fails.
>
> The record stays because it says which server versions were exercised, which
> CI's single pinned image does not.
>
> One property is worth naming here because the suite had to be widened for it:
> the write-policy fix and the chunk-inheritance trigger *both* keep a restricted
> chunk restricted, so a test asserting only that outcome stayed green when the
> policy was reverted. `test_the_write_policy_alone_protects_an_unreadable_row`
> writes a column no trigger touches, which is the only way to see which layer is
> holding.

Run twice, against throwaway clusters:

- **PostgreSQL 17.10, pgvector 0.8.6, pg_trgm 1.6** — the original run.
- **PostgreSQL 16, pgvector 0.6.0** — re-run in full after the review fixes
  below. pgvector 0.6.0 predates `hnsw.iterative_scan` (0.8.0), so this run is
  the one that actually exercises `01_extensions_and_settings.sql`'s version
  guard; it emits its `NOTICE` and skips, and all nine files apply with no error
  under `ON_ERROR_STOP=1`.
- **PostgreSQL 16.10, pgvector 0.8.6** (the CI-pinned image), 2026-08-19 — the
  run behind the `<table>_app_never_restricted` rows below. Both directions were
  measured: the four declassification vectors against the files as they stood,
  and again with the seven restrictive policies in place.

**Every mutation check below was re-run with real rows present.** This is not a
formality. Two ways this kind of checklist lies to you, both hit while running
it:

- A `FOR EACH ROW` trigger on an **empty table never fires**, so
  `UPDATE claim SET ...` against zero rows *succeeds* and reads as "append-only
  is not enforced." A checklist that seeds no data proves nothing here.
- **RLS filters rows out before a row-level trigger runs**, which is the same
  trap wearing a different hat. Enabling row-level security on `claim`,
  `resolution` and `audit.event` turned `UPDATE public.claim SET confidence = 0.1`
  as `procurement_owner` from a raised exception into a silent `UPDATE 0` — the
  data survived, but Decision 9's attack matrix lists that cell as
  "Trigger: blocked", and a silent no-op is not blocked, it is unmeasured. Each
  of those three tables therefore carries a permissive `FOR UPDATE`/`FOR DELETE`
  policy whose only purpose is to keep rows eligible so the tripwire still
  speaks. No role is granted either verb.

| Property | Source | Result |
|---|---|---|
| all nine files apply under `ON_ERROR_STOP=1`, on pgvector 0.6.0 | T0.1 | applied |
| `01`'s pgvector version guard skips cleanly below 0.8.0 | Decision 3a | `NOTICE`, no error |
| `procurement_app` may `INSERT`/`SELECT` on `audit.event` | Decision 9 | passes |
| `procurement_app` cannot `UPDATE`/`DELETE`/`TRUNCATE` it | Decision 9, T0.1 | all three refused |
| the trigger tripwire stops `UPDATE`/`DELETE` as the table owner | Decision 9 | refused, with rows present |
| the trigger tripwire stops even a superuser `UPDATE` | Decision 9 | refused |
| `claim`/`resolution` reject `UPDATE`/`DELETE`/`TRUNCATE` with rows present | C8 | all refused |
| `TRUNCATE ... CASCADE` on `claim` is refused | C8 | refused |
| a hash-chain fork is loud, not silent | Decision 9 | refused, `audit_event_no_fork` |
| a duplicate *genesis* row is also refused | `NULLS NOT DISTINCT` | refused |
| `prev_hash` naming a parent that never existed | Decision 9 | refused, FK |
| a second disconnected root in one stream | Decision 9 | refused, FK |
| two events in one stream sharing a `hash` | Decision 9 | refused, `audit_event_hash_unique` |
| a valid chain still appends, and a new document still starts one | Decision 9 | both allowed |
| duplicate `content_hash` refused | NFR-05, AC-5 | refused |
| app cannot declassify or delete a row it cannot read | Decision 3c | `UPDATE 0`, `DELETE 0` |
| a chunk written for a restricted document inherits the flag | NFR-03, C7 | inherited |
| no `hnsw`/`ivfflat` index exists | Decision 3a | 0 found |
| `FORCE ROW LEVEL SECURITY` on all seven content tables | Decision 3c, NFR-03 | all forced |
| restricted `claim`/`conflict`/`resolution`/`job`/`audit.event` hidden from `procurement_app` | NFR-03, AC-8 | 0 rows each |
| unrestricted rows in those tables stay visible | — | visible |
| an entitled session (`app.allow_restricted`) sees them again | Decision 3c | visible |
| ~~`procurement_app` can set `app.allow_restricted` itself~~ | NFR-03, AC-8 | **was 1→2 rows on all seven tables**; now 1 |
| …by `set_config(...)`, and by `SET LOCAL` in a transaction | NFR-03 | was 2, now 1 |
| …by `options='-c app.allow_restricted=true'` in the **connection string** | NFR-03 | was 2 *with no statement issued*, now 1 |
| the app role can still `INSERT` a row it is labelling restricted | 00_roles.sql | allowed — the safe action must not be the failing one |
| a mis-granted `procurement_ingest` membership needs **no `SET ROLE`** | Decision 9 | 1→2 rows without it; restrictive policy holds it at 1 |
| a `SECURITY DEFINER` accessor owned by a superuser ignores every policy | Decision 3c | owned by `procurement_owner`: 1 row; by `postgres`: **2, silently** |
| reclassifying a document takes effect immediately, no backfill | — | confirmed |
| `procurement_ingest` can `INSERT ... ON CONFLICT`/`RETURNING` a restricted row | NFR-05 | allowed |
| `procurement_app` cannot rewrite `job.idempotency_key`/`stage`/`payload`/`created_at` | I.2 | refused |
| `procurement_app` can still perform a job state transition | I.2 | allowed, `updated_at` stamped |
| re-running `00_roles.sql` clears a `SUPERUSER BYPASSRLS` on `procurement_app` | Decision 3c | cleared |
| re-running `02`–`08` fails loudly | forward-only migrations | `relation already exists` |
| both owner roles `NOLOGIN`, app roles non-superuser | Decision 9 | confirmed |
| `payload` jsonb derives from `payload_canonical` text | Decision 9 | confirmed |

`tests/test_sql_schema.py` is the CI-side companion: it cannot execute SQL, so it
asserts the *structure* each of these properties rests on, and fails if a fix
above is reverted. The behaviour is proven here; the regression guard is there.

### Still unproven by the above

~~Concurrency.~~ **Proven 2026-08-12**, outside this DDL as predicted. Decision 9's
measured failure — 8 concurrent writers producing 42 silent forks — is a property
of the *caller's* advisory-lock discipline, and `audit/writer.py` now supplies it
as its own statement before the INSERT. `tests/test_audit_live.py` reproduces all
three arms against a live server: locked gives 48/48 rows and no forks; unlocked
with these constraints in place gives 6/48 rows and 42 unique-violation errors,
so the DDL converts silent to loud exactly as claimed; unlocked with the fork
constraints dropped gives 48/48 rows and **41 silent forks that fail
verification** — Decision 9's original setup, reproduced within one.

What the `<table>_app_never_restricted` policies do **not** cover, stated so the
scope is not read wider than it is. They bind `procurement_app` and every role
that inherits it. They are inert for an actual superuser and for any role holding
`BYPASSRLS`, which is why `00_roles.sql` re-asserts `NOSUPERUSER NOBYPASSRLS` on
every apply and raises if either survived; and they stop applying to a session
that has `SET ROLE`d to a role outside `procurement_app`'s membership, which is
why the same file revokes every project-role membership unconditionally and two
tests assert the result. The policy is the layer that holds when those fail — it
is not a replacement for them.

Performance of the confidentiality derivation. `document_is_restricted` and
`conflict_is_restricted` are `STABLE` SQL functions called from RLS policies, so
the planner may call them once per distinct argument per statement rather than
once per row — but that was not measured, and neither was the join in
`conflict_is_restricted` at queue scale. Correctness was the goal here; if the
queue view becomes slow, the answer is a materialised restriction column on
`conflict` maintained by a trigger on `conflict_candidate`, not a weaker policy.

## Verification checklist (T0.1)

### 1. Migrations apply cleanly

Done — see "Live verification actually performed" above. Nine files, no error
output, on PostgreSQL 17.10/pgvector 0.8.6 and PostgreSQL 16/pgvector 0.6.0. Re-run
the apply loop against an empty database as the bootstrap identity, with
`ON_ERROR_STOP=1`, whenever these files change.

Note that `00_roles.sql` now needs a **superuser** bootstrap identity: it clears
`SUPERUSER`, `BYPASSRLS` and `REPLICATION` unconditionally, and only a superuser
may clear those — a `CREATEROLE` service account is refused with "permission
denied to alter role" even when the attribute is already unset. That is the
correct trade, because an identity that cannot clear `BYPASSRLS` cannot honestly
promise it is unset; the file's assertion block reads `pg_roles` back (which needs
no privilege) and raises if anything forbidden survived. Files `01`–`08` still
apply under the weaker CI identity described under "Who runs this".

### 2. `procurement_app` cannot `UPDATE`/`DELETE`/`TRUNCATE` `audit.event`

Once applied, run as the bootstrap identity:

```sql
SET ROLE procurement_app;

-- Should succeed: INSERT and SELECT are the only granted privileges.
INSERT INTO audit.event
    (stream, document_id, seq, prev_hash, hash, event_type, actor, payload_canonical)
VALUES (
    'doc:smoke-test', 'smoke-test', 0, NULL,
    decode(repeat('00', 32), 'hex'), 'document_ingested', 'verification-checklist', '{}'
);

-- Each of the following three must fail with "permission denied for table
-- event" -- the privilege check fires before any trigger body ever runs,
-- because procurement_app was never granted UPDATE, DELETE, or TRUNCATE.
UPDATE audit.event SET actor = 'x' WHERE event_id = 1;
DELETE FROM audit.event WHERE event_id = 1;
TRUNCATE audit.event;

RESET ROLE;
```

A stronger version of the same check (proves the trigger tripwire independent
of the grant, in case a future migration mistakenly widens the grant):

```sql
GRANT UPDATE, DELETE, TRUNCATE ON audit.event TO procurement_app; -- as bootstrap, temporarily
SET ROLE procurement_app;
UPDATE audit.event SET actor = 'x' WHERE event_id = 1;   -- now expect the
DELETE FROM audit.event WHERE event_id = 1;              -- trigger's RAISE
TRUNCATE audit.event;                                    -- EXCEPTION instead
RESET ROLE;
REVOKE UPDATE, DELETE, TRUNCATE ON audit.event FROM procurement_app; -- put it back
```

Also confirm the same shape holds for `claim` and `resolution` (this schema
applies the same reasoning to both, beyond the literal T0.1 ask — see decision
12 below).

### 3. RLS spot check (Decision 3c, AC-8)

Seed a restricted document **and at least one dependent row in every content
table** first. Checking only `document` is how the original review passed while
`claim`, `conflict`, `resolution`, `job` and `audit.event` were all returning the
same document's content in full.

```sql
SET ROLE procurement_app;
-- No app.allow_restricted set: only unrestricted rows should be visible,
-- in every one of these.
SELECT count(*) FROM document      WHERE access_restricted;          -- expect 0
SELECT count(*) FROM chunk         WHERE access_restricted;          -- expect 0
SELECT count(*) FROM claim         WHERE document_id = 'doc-secret'; -- expect 0
SELECT count(*) FROM job           WHERE document_id = 'doc-secret'; -- expect 0
SELECT count(*) FROM audit.event   WHERE document_id = 'doc-secret'; -- expect 0
SELECT count(*) FROM conflict;     -- expect 0 for a restricted conflict
SELECT count(*) FROM resolution;   -- expect 0 for a restricted conflict

SET LOCAL app.allow_restricted = 'true';
SELECT count(*) FROM claim WHERE document_id = 'doc-secret';         -- the real count
RESET ROLE;
```

The write path is a separate check, and the one that regressed silently before:

```sql
SET ROLE procurement_ingest;
-- Must succeed. Under procurement_app this raises "new row violates row-level
-- security policy", because RLS applies the SELECT policy to the read-back.
INSERT INTO document (document_id, content_hash, source_uri, document_type,
                      access_restricted)
VALUES ('d1', 'h1', 'file:///x', 'pricing', true)
ON CONFLICT (content_hash) DO NOTHING
RETURNING document_id;
RESET ROLE;
```

## What remains unverified

Everything under "Live verification actually performed" above was executed. What
follows is what a live run on 16 and 17 still could not reach.

- **PostgreSQL 18 itself.** Decision 3 pins 18; the runs were on 17.10 and 16.
  Nothing here uses an 18-only feature and the newest syntax
  (`UNIQUE NULLS NOT DISTINCT`, PostgreSQL 15+) executed correctly on both, but
  "verified on 18" is not a claim this file makes.
- **pgvector 0.8.5's packaging.** Whether `CREATE EXTENSION vector` at 0.8.5
  requires superuser or is installable by a plain database-owning role (i.e.
  whether pgvector's control file marks itself `trusted`) — the runs installed it
  as superuser, so the weaker-privilege path is still untested.
- **`ALTER DATABASE ... SET hnsw.iterative_scan = 'relaxed_order'`.** On 0.6.0
  the version guard skips it, which is the branch that was exercised. That it is
  accepted as a placeholder GUC on 0.8.x without a `shared_preload_libraries`
  change, and takes effect for new sessions, was reasoned about rather than run.
- **Concurrency**, in full — see "Still unproven by the above".
- **Query performance under the confidentiality derivation** — see the same
  section.
- **Whether `procurement_app` and `procurement_ingest` are actually deployed as
  separate connections.** The schema now depends on that split: `procurement_ingest`
  may read every restricted row, and a deployment that pointed the retrieval path
  at it would have no confidentiality control at all while every policy in this
  directory still looked correct. Nothing in DDL can check this; it is a
  deployment property, and it is the single most important thing to get right
  when wiring the application up.
- **`conflict_candidate` has no RLS**, deliberately. A policy on it calling
  `conflict_is_restricted` — which reads it — is a genuine infinite recursion,
  which PostgreSQL rejects at query time rather than at `CREATE POLICY`. The
  residual exposure is that an unentitled role can see that some conflict has
  some number of candidate rows; it recovers no value, unit, verbatim text or
  document id, because `claim` and `conflict` are both gated. Small, real, and
  recorded rather than closed.

## Design decisions made here that the specs did not settle

Contracts C1/C4/C8 fix a lot, but not everything a working schema needs. Every
place this file set had to invent something, it is listed here so a reviewer
can check exactly these choices rather than re-deriving the whole schema.

1. **Schema placement.** Core tables (`document`, `chunk`, `claim`, `conflict`,
   `conflict_candidate`, `resolution`, `job`) live in `public`; only the audit
   trail gets its own schema. Tasks.md and the plan write every core table name
   unqualified and only ever qualify `audit.event` — this follows that literally
   rather than inventing a dedicated `app`/`core` schema, which would also have
   satisfied the contract but was not asked for.

2. **`claim`'s natural key is extended beyond tasks.md:31's literal wording.**
   The C8 invariant text says claims are keyed
   `(document_id, field, extractor_version)`. Read literally, that collides
   across every SKU one multi-bin datasheet describes (a Trina
   `TSM-NEG21C.20` sheet spans 6 bins and 22 CEC rows per
   `schema/component.py`'s own docstring). `claim_natural_key` adds
   `(component_category, supplier, model, nameplate)` so the constraint means
   what C8 intends. Two follow-on gaps this introduces, left open rather than
   silently "solved":
   - `nameplate` NULLs are not collapsed (no `NULLS NOT DISTINCT`), so a
     category with no bin discriminator and more than one same-supplier-model
     instance per document is not fully protected against key collision.
   - The key assumes `extractor_version` is fine-grained enough to
     distinguish genuinely different extraction strategies for the same field
     (e.g. WP-B B.6's field-guided vs. document-guided cross-read) so they land
     as distinct rows rather than colliding.

3. **`conflict`/`resolution` reference `claim` rows through a junction table**
   (`conflict_candidate`), rather than storing a denormalised copy of
   `ConflictCandidate`. This is safe only because `claim` rows are immutable —
   there is no staleness a snapshot would have protected against — and a join
   reconstructs the exact Pydantic shape at read time. `ConflictQueueEntry`
   itself has no notion of this reference; it is this schema's own
   normalisation.

4. **`resolution.selected_claim_id` and the human-override-as-claim
   convention are documented, not enforced.** A `select_value`/
   `enter_override` resolution is expected to also write a new `claim` row so
   the "canonical value is a projection over claims" invariant holds for human
   decisions too, not only machine ones. Enforcing this would need a
   cross-table trigger; judged too fragile relative to the benefit, so it is a
   comment, not a constraint.

5. **`audit.event`'s `event_type` taxonomy is invented outright — now v1, not
   frozen.** ✅ **Settled by [D-13](../specs/001-procurement-agent/clarifications.md)
   (adopted 2026-08-07).** The half of this that was open is closed: the RFC 8785
   canonicalisation rule and the exact preimage are defined, so the bytes `hash`
   covers are no longer undefined. The Python envelope still does not exist, so
   nothing emits an event yet, but that is now missing code rather than a missing
   decision.

   The seven values (`document_ingested`, `parse_failure`, `extraction`,
   `web_search`, `conflict_detected`, `resolution`, `attempt_failed`) are **version
   1**, and D-13 makes additions additive-only by amendment rather than freezing
   the list — this table chose a CHECK over a native enum precisely so values could
   be added, and an absolute freeze would break the day a `workbook_composed` event
   is needed. **Removing or renaming is forbidden once any event exists** — that is
   what breaks chains. Before the first emit, when no chain exists to break, an
   amendment may still remove a value; that is how the unreachable `web_search`
   entry in decision 6 should go.

6. **Web searches and cross-document queries have no home here, and the reason
   changed.** ⚠️ **Corrected by [A-49](../specs/001-procurement-agent/analysis.md).**
   This decision previously said NFR-02 wants "every ... query" audited. It does
   not. `spec.md:153` says "**web** queries, extractions, conflicts and
   resolutions"; `plan.md:66` paraphrases that as "every extraction, **query**,
   conflict and resolution", dropping *web*, and this decision inherited the
   paraphrase. `spec.md` governs, so cross-document **retrieval** queries are
   plausibly not NFR-02 events at all — logging them is observability, not an audit
   obligation.

   The gap NFR-02 *does* create is the opposite one, and this table cannot hold it:
   `web_search` is in the taxonomy above, but `document_id` is `NOT NULL` against a
   real `document` row, and a gap-triggered search happens precisely *because* no
   document supplied the value. D-13 sends run-scoped events to a separately chained
   `audit.run_event` table keyed `run:<id>`, which is where web searches land.
   Removing the unreachable `web_search` value from this CHECK is an additive-only
   amendment for WP-H to make when it writes the emitter.

7. **Hash algorithm ~~assumed~~ pinned to SHA-256** (the two
   `octet_length(...) = 32` `CHECK`s on `audit.event.prev_hash`/`hash`).
   ✅ **Settled by D-13 §1**, which names the digest rather than leaving it inferred
   from a column width. Plan.md Decision 9 measures chain-append performance but
   never named one; that gap is closed. These two constraints and the digest now
   move together or not at all.

8. **`stream = 'doc:' || document_id` is enforced structurally**, via a CHECK
   tying `stream` to a real `document.document_id` through a foreign key. This
   is the strongest possible reading of "never globally" — it makes a non-
   document-scoped stream impossible without a migration, not just
   discouraged. Flagged because it is a real restriction, not only a safety
   net: this table cannot be reused for any future non-document audit stream
   as-is.

9. **C7 (the ACL/labelling model) is implemented at its frozen minimum, not
   guessed at in full.** ✅ **The decision caught up: T0.4 is written as
   [D-15](../specs/001-procurement-agent/clarifications.md), provisionally adopted
   2026-08-07.** It ratifies exactly what landed here rather than replacing it —
   one document-level label, per-principal clearance, labelling at ingest failing
   closed. It stays **provisional** because two facts remain outstanding, and they
   are facts rather than preferences: whether any executed NDA exceeds "need to
   know", and whether any evaluator is conflicted with a specific bidder. Either
   yes makes the label `restricted_group`. The enforcement mechanism arrived ahead
   of the decision it implements, which is unusual but turned out well. Rather than inventing a labels/tenant model with
   no contract behind it, RLS on all seven content tables — `document`, `chunk`,
   `claim`, `conflict`, `resolution`, `job` and `audit.event` — enforces only the
   one ACL primitive the frozen
   Pydantic schema already commits to — `SourceDocument.access_restricted` — 
   gated by a session boolean GUC (`app.allow_restricted`) the application must
   `SET LOCAL` per transaction based on the caller's real authorization.

   **Nothing enforces "real", and nothing can.** The GUC is an assertion the
   application makes *about its caller*; the database has no way to check it,
   and until 2026-08-19 there was nothing stopping the application asserting it
   about itself. Measured, `procurement_app` set the GUC and read every
   restricted row on all seven tables — by `SET`, by `set_config`, by `SET
   LOCAL`, and, worst, by `options='-c app.allow_restricted=true'` in the
   connection string, which arrives during startup and so declassifies the
   session **before it issues a statement**. That last vector is why the fix
   could not be an application-side "RESET on pool checkout" guard: there is no
   moment at which such a guard runs first.

   What now holds the line is a second, `AS RESTRICTIVE` policy per table
   scoped `TO procurement_app` (`<table>_app_never_restricted`). Permissive
   policies are OR'd, so nothing added to the eleven existing ones could ever
   narrow them; a restrictive policy is AND'd, and closes the escape without
   touching any of them and without disturbing ingest. So the GUC's honest
   reading is now: it is what entitles `procurement_ingest`, the two owner
   roles, and an operator identity — never the serving role, which cannot
   assert it about itself no matter how it is delivered. The full argument, and
   why editing the existing policies instead *breaks ingest*, is on
   `document_app_never_restricted` in `02_document.sql`.

   `VectorStorePort.search`'s fine-grained `allowed_document_ids` allowlist
   remains a WHERE-clause concern in the store adapter, not RLS, because RLS
   is a poor mechanism for a large, dynamic, per-request ID set.

10. **RLS is applied to all seven tables that hold document content**, not only
    to `document` and `chunk`. An earlier version of this row argued the
    opposite — that the other five are "not part of the measured retrieval-time
    access-control concern, and adding RLS to them would be inventing scope
    Decision 3c never asked for." Measured against a live server, that reading
    was wrong, and expensively so. As `procurement_app`, against a document that
    role could not see:

    ```
    SELECT document_id FROM document WHERE document_id = 'doc-secret';  -- (0 rows)
    SELECT field, value, verbatim_value FROM claim
      WHERE document_id = 'doc-secret';
    --  price_per_watt_dc | 0.19 | CONFIDENTIAL 0.19 USD/W
    SELECT payload FROM audit.event WHERE document_id = 'doc-secret';
    --  {"price_per_watt_dc": 0.19}
    SELECT explanation FROM conflict;
    --  CONFIDENTIAL: record says 0.19, web says 0.35
    SELECT value_before, value_after FROM resolution;   --  0.35 | 0.19
    SELECT payload FROM job;                            --  {"secret": "0.19"}
    ```

    The distinction the old row rested on — retrieval path versus not — is a
    distinction about *tables*, and AC-8 is a claim about *content*: "a user
    without clearance for a confidential document cannot cause its content to
    influence any retrieved result." `claim.value` is not a reference to the
    document's content, it *is* the document's content, extracted. So is
    `audit.event.payload`, and `conflict.explanation` quotes the disputed values
    verbatim because it is written for a human to read.

    Nor did this need C7 to be unfrozen. The derivation is the document each row
    already names — `public.document_is_restricted(document_id)`, a `STABLE`
    `SECURITY DEFINER` helper in `02_document.sql` — with no new column, no
    labels model, and no guess about the ACL scheme. `conflict` and `resolution`
    have no `document_id` and cannot (an inter-document conflict spans several),
    so they key on `public.conflict_is_restricted(entry_id)`, which ORs over the
    documents of the conflict's candidate claims. That is computed on read rather
    than stored, because a `conflict` row is inserted *before* its
    `conflict_candidate` rows exist, so there is nothing to derive from at INSERT
    time — and deriving on read means a reclassification takes effect immediately
    across every dependent table, with no backfill to forget.

    `conflict_candidate` is the one table left out, and for a mechanical reason
    rather than a judgement — see "What remains unverified".

11. **RLS policies are not scoped `TO procurement_app`** — with one deliberate
    exception, the `procurement_ingest` policies added under decision 22 below,
    where the scoping *is* the control. With
    `FORCE ROW LEVEL SECURITY`, every non-superuser role — including
    `procurement_owner` if anyone ever `SET ROLE`s into it for maintenance —
    is subject to RLS. A policy scoped only to `procurement_app` would leave
    every other role staring at zero rows with no applicable policy. The
    policies here apply to `PUBLIC` (i.e. every role, the default when no `TO`
    clause is given) so the behaviour is uniform and not a surprise during
    ops. Only an actual PostgreSQL superuser bypasses this, by Postgres design,
    not by anything in this schema.

12. **Trigger tripwires were added to `claim` and `resolution`, not only to
    `audit.event`.** Tasks.md's literal ask for a `BEFORE TRUNCATE` tripwire is
    scoped to C4. Decision 9's stated reasoning — "it is free and catches a
    mis-grant" — applies identically to every append-only table this schema
    has, so the same `BEFORE UPDATE OR DELETE` trigger (`public.reject_mutation`)
    is reused on `claim` and `resolution`. This is a deliberate extension
    beyond the literal ask, flagged so it can be reviewed and removed if judged
    unwanted.

    **Correction.** The row above originally read that `claim` refused all three
    of `UPDATE`, `DELETE` and `TRUNCATE`. It refused two. `TRUNCATE` fires no
    row-level triggers at all, so a `FOR EACH ROW` trigger cannot see it, and
    `TRUNCATE public.claim CASCADE` succeeded — taking `resolution` and
    `conflict_candidate` with it, i.e. the immutable record of what a human
    decided. `ON DELETE RESTRICT` on the child FKs does not help, because
    `TRUNCATE CASCADE` truncates the children rather than deleting through the
    constraint. `audit.event` had the correct statement-level companion from the
    start; `claim` and `resolution` now have it too
    (`public.reject_truncate`).

13. **`document` allows narrow, column-level `UPDATE`** on `access_restricted`
    and `data_vintage` only, for post-ingest correction. Everything else about
    `document`, including `content_hash` and `document_id`, has no UPDATE path
    at all. The contract does not say whether `document` should be as strictly
    immutable as `claim`; this is a judgement call.

14. **`chunk` denormalises more than FR-RAG-02's literal list.** FR-RAG-02
    names "doc ID, chunk ID, component category, supplier, doc type, page,
    source URI, timestamps, source-tier flag" as indexing metadata. `model`
    and `access_restricted` are added here for symmetry with `supplier` and
    with `document.access_restricted` respectively — reasonable, but beyond
    what was literally specified.

15. **Contextual retrieval's prepended context is a separate column
    (`chunk.context_prefix`) from the verbatim chunk text (`chunk_text`).**
    Decision 6 says to prepend context before embedding but does not say
    whether the stored/cited text should include it. Splitting them means a
    citation always shows a reviewer the real source text, never a generated
    framing sentence; the lexical indexes (`tsv`, trigram) also run over
    `chunk_text` only for the same reason, which is itself a decision (Decision
    3b's own motivating example — part-number matching — lives in verbatim
    text).

16. **`chunk_kind` includes a `prose` member not named in plan.md.** Decision 6
    names only the three `table_*` kinds; `prose` is this schema's own name
    for everything else.

17. **`job` has no dependency/DAG table.** Stage sequencing (e.g. do not
    enqueue `extract` until the matching `ingest` job succeeded) is assumed to
    be the worker's responsibility at enqueue time — a job succeeding triggers
    the next stage's job being inserted — rather than a modelled graph. Decision
    1's "hand-rolled state machine, not a workflow framework" reads as
    consistent with keeping this out of the schema, but tasks.md does not say
    so explicitly.

18. **`job`'s 15-minute lease duration reuses D-12e's number.** D-12e is
    titled generically ("claim lease duration") and is cited explicitly by
    WP-F F.1 for the `conflict` table; tasks.md never gives `job` its own
    lease duration, so this schema reuses the same figure rather than
    inventing a second one.

19. **`procurement_app`'s authentication is left unconfigured.** No password,
    certificate, or IAM binding is set anywhere in this file set; `00_roles.sql`
    creates the role `LOGIN`-capable but unable to actually log in until an
    operator configures one out of band. This is deliberate — nothing here
    should ever risk shipping a credential into version control — but it means
    "migrations apply cleanly" alone does not mean the application can connect
    yet.

20. **The bootstrap/migration identity itself is undefined by this file set**
    (see "Who runs this" above). Some deployments will use the cluster
    superuser; others a scoped CI role. Either works with these files as
    written, but this schema does not attempt to create or constrain that
    identity.

21. **`chunk.tsv` calls `to_tsvector('english', chunk_text)` directly**, with no
    wrapper function. An earlier version of this row described a
    `chunk_text_to_tsvector()` wrapper as a "required fix", on the mistaken
    belief that the two-argument `to_tsvector(regconfig, text)` is `STABLE` and
    therefore illegal in a `GENERATED ALWAYS AS ... STORED` column. That is
    backwards: PostgreSQL's own catalog marks the two-argument form **IMMUTABLE**
    (`pg_proc.provolatile = 'i'`), and it is the *one*-argument `to_tsvector(text)`
    that is `STABLE`, because it reads `default_text_search_config` at runtime.
    Verified by the table creating successfully on a live server. The wrapper was
    removed; this row is kept rather than deleted because wrapping a genuinely
    `STABLE` function and declaring the wrapper `IMMUTABLE` is not a harmless
    workaround — it lies to the planner and can silently corrupt any index built
    on it.

22. **`procurement_ingest` is a fourth role, and the write path connects as it.**
    RLS applies a table's `FOR SELECT` policy as a `WITH CHECK` against the
    proposed row whenever an INSERT carries `RETURNING` or `ON CONFLICT`, so the
    idempotent-ingest idiom this schema documents failed for exactly the
    confidential documents NFR-03 exists to protect:

    ```
    INSERT INTO document (..., access_restricted) VALUES (..., true)
      ON CONFLICT (content_hash) DO NOTHING;
    -- ERROR: new row violates row-level security policy for table "document"
    ```

    while the identical statement with `false` succeeded. Writing a *more*
    confidential row failed and writing a *less* confidential one worked.

    Two alternatives were measured before choosing a role. An `xmin =
    pg_current_xact_id_if_assigned()::xid` read-back policy ("let a session read
    rows it wrote itself") does **not** work — the policy is evaluated against the
    in-memory proposed tuple, whose system columns are not yet set — and it fails
    in the direction of looking correct, which is why it is recorded here.
    Reusing `app.allow_restricted` on the ingest path does work, and was rejected:
    it makes the writer assert full retrieval entitlement to every restricted
    document, on the same role and connection pool that serves user queries, and
    `SET` without `LOCAL` persists for the life of a pooled session. A separate
    role makes the entitlement a static, auditable property of a principal no
    user request is served by — which is the same argument Decision 9 already
    rests on: "the boundary that actually holds ... is privilege separation."

    The cost is real and is called out under "What remains unverified": the
    schema now depends on a deployment property no DDL can check.

23. **Append-only tables carry permissive `FOR UPDATE`/`FOR DELETE` policies.**
    Counter-intuitive, and the reason is measured rather than theoretical: RLS
    removes rows from consideration *before* a `FOR EACH ROW` trigger runs, so
    enabling RLS on `claim`/`resolution`/`audit.event` turned their append-only
    tripwires from a raised exception into a silent `UPDATE 0`. Neither verb is
    granted to any role, so these policies are reachable only in the mis-grant
    case they exist to make audible. Decision 9 chose the trigger because it is
    loud; a policy that silences it is not a hardening.
