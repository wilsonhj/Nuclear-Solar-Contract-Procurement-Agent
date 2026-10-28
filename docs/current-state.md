# Current state

This audit reflects the `claude/phase-1-integration` branch as verified on 2026-08-29. It
answers the practical question a new contributor has first: what can the repository do today?

The local verification baseline is **1199 passing, 42 skipped, and 4 expected failures**
(re-measured 2026-10-28 on `main` plus the failure-taxonomy change; the narrative below was last
audited on the date above and is not re-audited by that count), plus a
clean Ruff check, a clean `ruff format --check`, and a clean strict-mypy check across `src/` and
`tests/`. The skips are intentionally DSN-gated live tests: 32 in
`tests/test_sql_behaviour.py` and 10 in `tests/test_audit_live.py`. With
`PROCUREMENT_TEST_DSN` pointed at a disposable PostgreSQL, CI runs both suites separately and
fails if either silently skips.

The implementation contains ten completely unimplemented entry points plus one explicit
unsupported workbook orientation (`suppliers_as_rows=False`). Since the previous audit, the
Python audit library and same-transaction boundary for C4, the canonical projection and initial
13-tab writer for C6, and a sanitized-PV vertical slice have landed. The general pipeline is
still not operational, but the fixture-scale slice now exercises claims, reduction, conflicts,
review, audit intents, deterministic workbook output, and a PostgreSQL persistence round trip
together.

## Executive assessment

The project is a well-researched, test-heavy policy core with one narrow executable integration
slice inside an application scaffold. Its strongest work is the domain reasoning around
provenance, conditions, tolerance, determinism, and human authority. It is not yet a usable
procurement tool because general supplier documents cannot travel through the complete pipeline.

The repository should currently be evaluated as:

- a reference implementation of several procurement comparison invariants;
- a detailed implementation specification for a larger system; and
- a starting point for fixture-driven vertical slices.

It should not yet be evaluated as:

- an installable end-user product;
- a document ingestion or RAG service;
- a secure store for confidential procurement data; or
- a completed end-to-end procurement application.

## Implemented and tested

### Canonical domain models

Pydantic models cover source documents, component instances, canonical fields, structured
conditions, declared tolerance bands, conflict candidates, queue entries, and resolutions.
Closed enums cover component categories, document types, source tiers, conflict classes,
conditions, severity, resolution actions, workbook tabs, and cell flags.

Important validators already reject unsourced values, invalid confidence values,
non-finite conditions and bands, malformed declared bands, and “resolved” fields without a
resolution.

### Deterministic identity and ordering

`ComponentInstance.ordering_key()`, condition grouping keys, candidate ordering, and conflict
pair generation are deterministic under input permutations. This is necessary for
byte-identical output later.

### Condition-aware conflict policy

The current implementation:

- compares every compatible unordered candidate pair;
- avoids treating a non-transitive relation as a partition;
- rejects incompatible conditions as a programming error;
- recognizes missing values as gaps rather than conflicts;
- detects unit mismatches before numeric tolerance;
- preserves standard editions as temporal conflicts;
- normalizes text without fuzzy-matching compliance claims;
- applies per-field exact, absolute, relative, one-sided, and declared-band rules; and
- honors the precision of the source’s printed value.

### Source authority

`assert_no_autonomous_overwrite()` prevents a web supplement from replacing a populated
system-of-record field. This is a useful safety predicate, although the future store must
make bypassing it structurally impossible.

### Composition policy

`blocking_conflicts()` and `compose_gate_blocks()` implement the compose-time severity
decision. Only unresolved conflicts strictly above the threshold block. The threshold cannot
be configured to disable the gate.

### Output primitives

`flags_for()` computes missing-data, web-supplemented, low-confidence, and unresolved states.
`expected_tabs()` exposes the fixed 13-tab order. `normalize_archive()` makes an XLSX archive
deterministic across time and platforms. `write_workbook()` now emits all thirteen tabs in the
initial suppliers-as-rows layout, with provenance comments/columns, flags, and open items.

`expected_tabs()` is pinned against the thirteen literal tab names by
`test_expected_tabs_returns_all_thirteen_in_order`; truncating or reordering the helper fails.
The canonical projection in `services/output/projection.py` is implemented and pinned by a
golden JSON fixture and digest. The vertical-slice tests pin the initial writer's tab set,
provenance, open-item row, and byte-identical regeneration. Hidden state columns, advanced
navigation and visual QA, desktop Excel/LibreOffice validation, and the columns-oriented layout
remain open.

### Database schema

Nine numbered DDL files in `sql/` create `document`, `chunk`, `claim`, `conflict`,
`conflict_candidate`, `resolution`, `job` and `audit.event`. They carry
`FORCE ROW LEVEL SECURITY` on the seven tables that hold document content, owner/application
privilege separation across four roles, append-only triggers on `claim`, `resolution` and
`audit.event`, and `audit.event`'s per-document hash chain with its fork, parent-exists and
loop constraints.

Three suites cover this substrate, and the split is the point. `tests/test_sql_schema.py`
asserts the DDL *text* and needs no server. `tests/test_sql_behaviour.py` re-runs the attacks each
defence descends from against a real PostgreSQL. `tests/test_audit_live.py` separately exercises
the Python append/verify path and concurrent writers against the same kind of server. The `sql`
job in `.github/workflows/ci.yml` runs all 32 schema-behaviour tests and all 10 live audit and
integration tests; those are the 42 tests that skip in a default local run.

### Audit library

`src/procurement_agent/audit/` implements RFC 8785 canonicalisation, the versioned event
envelope and SHA-256 preimage, advisory-lock append sequencing, and chain verification with a
CLI. Unit tests cover canonicalisation, envelope compatibility, writer statement order, and
verification defects; the live suite covers real inserts and concurrency. What remains is the
application integration beyond the narrow slice. `services.transactional_audit.write_and_append_event()` binds a
business callback and its event to one caller-owned transaction, with rollback atomicity proved
against live PostgreSQL. `services.vertical_slice.persist_vertical_slice()` applies the same
transaction discipline to its concrete business writer and all applicable audit intents on one
connection, owns the commit and rollback, and returns a cleared result only after a successful
commit. The general
ingestion, extraction, conflict, review, and composition stages remain unwired.

### Sanitized PV vertical slice

`services.vertical_slice` is an executable, fixture-backed path over a trusted sanitized CSV. It
parses two source records, creates immutable claims, replays them idempotently in the in-memory
reference store, reduces a canonical PV component, constructs an inter-document conflict queue
entry, supports a minimal human resolution that selects an existing sourced candidate, builds
the canonical workbook projection, and writes the 13-tab XLSX. Its concrete PostgreSQL writer
persists documents, claims, conflicts and candidates, resolutions, and audit events in one
service-owned transaction, rolling back all of them on failure.

This does not implement general CSV intake, native PDF/Word/Excel parsing, OCR, a general
PostgreSQL repository, a reviewer API/UI, or the stage runner. It is a narrow executable proof of
the contracts, not a production ingestion route.

### Timeouts and structured logging

`config.Settings` carries an explicit wall-clock timeout for each external call
(`parse`, `embed`, `llm`, `vector_store`, `web_search`) and a `stage_timeout_seconds` budget,
validated to nest: every per-call timeout is at most the stage budget, and the stage budget is
strictly inside the 15-minute job lease (D-12e, `config.JOB_LEASE_SECONDS`). The nesting is
checked at construction, on assignment (validated before the write, so a refused value is never
stored) and on `model_copy(update=...)`, which is also the way to change coupled settings together.
`logging_config.configure_logging()` installs a one-JSON-object-per-line handler on the package
logger that masks `SecretStr` values (ADR-001 decisions 3 and 7).

**Nothing consumes either yet.** No adapter implements a port and no module logs, so no timeout
is enforced on a real call and no log line is emitted today; the settings and the handler are the
contract the first adapter and the runner must use. Exception text is not redacted by the logger.

### Failure taxonomy

`failures` defines a closed set of eight failure classes with a fixed precedence (ADR-001
decision 4), root cause first, so that **every harness class outranks every data class, which
outranks every model class**: when two signals are present the harness failure is the one
reported, and an eval harness can exclude harness and unattributed attempts from an accuracy
figure instead of scoring them as misses. `classify_exception()` reads an exception's chain by
type, never by message: `__cause__`, `__context__` (unless severed with `raise ... from None`)
and the members of an `ExceptionGroup`, reading at most 64 distinct exceptions. If it cannot read
the whole chain, only the top class (`contract_violation`) is trusted and everything else becomes
`unknown`, which quarantines: a lower class found early cannot be trusted while a higher one may
sit unread. Cancellation and `KeyboardInterrupt` are not failures and also classify as `unknown`,
so the runner must handle them before classifying. `log_context()` produces `extra=` context for
the JSON logger without the message. Each class names a disposition (`retry` or `quarantine`,
tasks I.2 and I.4), a fault group, the `job.status` values it may write, and an existing
`audit.event` type.

**A type is evidence only when the type alone implies the cause.** The programming-error
invariants (`AutonomousOverwriteError`, `StoredValueLossError`, ...) are registered by type.
Validation rejections are not: `OffContractFieldError` and `VocabularyError` are raised for
supplier data, caller bugs and model output alike, so the code that knows tags its error with a
`ClassifiedError` subclass, whose tag is validated when the class is defined and is combined with
any registered type the exception also is (an `OcrTimeout(ClassifiedError, TimeoutError)` is a
`timeout`, as it would be were the two facts split across a cause chain). For the same reason `store_failure`, `adapter_failure`,
`unparseable_document` and `schema_violation` are reachable only by tag; no exception type in
the package implies them. A test fails if an exception is added to the package without being
classified or listed as unclassified by design with a reason.

Two other judgement calls are recorded rather than hidden. An LLM returning `None` (FR-RAG-04,
insufficient evidence) is a result, not a failure, and has no class. `DeclaredBandUnitError` is
unclassified for the same reason as the validation errors: nothing says whether a unit mismatch
comes from supplier data or from a caller bug, and no pipeline raise site exists to decide.

**Nothing raises into it yet.** No runner exists, so no job is retried or quarantined by class
today, and no adapter or stage tags an error; the taxonomy is the contract the C8 runner, the
first adapters and the B.9 eval harness must use.

## Declared but not operational

### Integration ports

Six Protocol interfaces define the intended parser, OCR, embedder, vector-store, reranker,
and LLM boundaries. Each has an **in-memory reference adapter** under `adapters/<port>/memory.py`
and a capability-declaring conformance suite in `tests/port_contracts/`. No *vendor* adapter
exists and no production path consumes a port, which is why NFR-04 is `partial` rather than
`enforced`: a suite passing against a reference proves the contract is expressible, not that any
real backend satisfies it.

### Service entry points

The general ingestion, indexing, retrieval, web-search, and runner entry points raise
`NotImplementedError`. The workbook writer now handles the initial suppliers-as-rows layout;
the opposite orientation explicitly remains unsupported.

### Persistence

The tables exist (see [Database schema](#database-schema)). The audit library reads and writes
`audit.event` through a connection supplied by its caller, and the generic transactional-audit
service binds that event to a callback on the same connection. Its live tests use `psycopg`.
There is still no general repository layer, connection or session management, or store adapter
for documents, chunks, and jobs. The sanitized-PV writer is the narrow exception: it persists
documents, claims, conflicts/candidates, resolutions, and audit events transactionally.

`sql/` is also not a migration tool. It is a numbered, forward-only file set applied by `psql` in
lexical order, with no version tracking: re-running `02`–`08` against a database that already has
the tables is *expected* to fail with `relation already exists` rather than no-op. That is
deliberate and documented in `sql/README.md`; it is not the same thing as having migrations, and
a project that wants rollback or a schema-version table still has to choose one.

### Human review

The conflict and resolution data shapes exist, and the sanitized-PV slice constructs a queue
entry and supports selecting an existing sourced candidate without mutating the input. There is
no durable queue lease service, reviewer API, authentication, or UI, and the other resolution
actions remain outside the slice.

### Security

The design calls for access labels, retrieval-time filtering, forced PostgreSQL RLS, a
non-owner application role, and self-hosted or enterprise inference. Two of the five now hold at
the database: `FORCE ROW LEVEL SECURITY` is applied to all seven content-bearing tables, and
`procurement_app` is a non-owner, non-superuser `LOGIN` role with `NOBYPASSRLS` re-asserted on
every apply. Both are exercised against a live server in CI.

The other three do not. Access *labels* are one boolean —
`SourceDocument.access_restricted`, derived through `public.document_is_restricted()` and gated
by an `app.allow_restricted` session GUC the caller must `SET LOCAL` — which is C7's minimum, not
C7's model. Retrieval-time filtering has no code, because no retrieval path exists to filter.
Inference hosting is a deployment choice nothing in the repository makes.

The distinction that matters: a defence enforced in the schema is only reached by a caller that
connects. The audit library proves that one low-level path; no application service connects yet.

## Acceptance status

The most useful summary is the acceptance boundary. There are eight acceptance criteria, listed
at [`spec.md` § Acceptance criteria](../specs/001-procurement-agent/spec.md); this table carries
one row per ID, and its status words are the ones
[requirements-traceability.md](requirements-traceability.md) defines — **enforced** means a test
covers the behaviour, **partial** means some of the cited artifact is tested, **declared** means
a signature exists, **open** means there is no home in the code yet.

| ID | Criterion | What holds today | Status |
|---|---|---|---|
| AC-1 | Scanned spec sheet yields sourced fields; low confidence routes to review | Nothing. No document reaches an extractor | open |
| AC-2 | A web value contradicting a record value raises a conflict, record unchanged | `assert_no_autonomous_overwrite()` at unit level. Nothing drives a contradiction from ingestion through to a queue entry | partial |
| AC-3 | All 13 tabs, with conditional formatting for the four cell states | The initial writer emits all thirteen tabs, provenance, flags, and an open-item row; unit tests cover all four states, while writer-level coverage does not yet exercise every style and desktop Excel/LibreOffice remains unrun | partial |
| AC-4 | Every output cell resolves to a source | The `SourceRef` validator, at model level | enforced |
| AC-5 | Re-ingesting an unchanged document creates no duplicates | `sql/02_document.sql`'s `UNIQUE (content_hash)`, exercised against a live server by `test_a_duplicate_content_hash_is_refused`. The store half only: `services/ingestion.ingest`, the thing that would re-ingest, raises `NotImplementedError` | partial |
| AC-6 | Inverter TRD against the IEEE 2800 limit; tax status per supplier | Nothing | open |
| AC-7 | Two generations from an unchanged store are byte-identical | The sanitized-PV slice writes two byte-identical workbooks and persists its narrow store transactionally; archive normalization remains independently tested. No general production store is wired, and the desktop Excel/LibreOffice gate is unrun | partial |
| AC-8 | An uncleared user cannot influence any retrieved result | Forced RLS is live-tested, and `VectorStorePort.search(allowed_document_ids=...)` is exercised against the in-memory reference — including that filtering happens inside top-k. No production retrieval path or vendor adapter exists | partial |

Read that table as three groups, because they fail differently. AC-4 is genuinely covered.
AC-2, AC-3, AC-5, AC-7 and AC-8 each have a tested *half* and an unbuilt one — that split is the
pattern to distrust in any status claim about this repository, because the tested half is the
one that gets quoted. AC-5 is the variant worth naming separately: its tested half is the
*store*, not a policy function, so the thing under test is a live database constraint and the
missing half is the caller. AC-1 and AC-6 have no implementing path at all.

> **Corrected 2026-08-05.** This row read "Nothing. `content_hash` exists but carries no
> uniqueness constraint / **open**". The constraint had landed in `e7da9ad`, an ancestor of this
> audit's own stated baseline, so the row was wrong when written rather than merely stale — and
> [requirements-traceability.md](requirements-traceability.md) already carried the correct
> `partial`. Two status documents disagreeing about one criterion is worse than either being
> wrong alone, because the disagreement is invisible unless someone reads both.

See [requirements-traceability.md](requirements-traceability.md) for the requirement-level
mapping behind these.

## Design and documentation debt

### License and contributor governance

The repository is licensed under Apache-2.0. `pyproject.toml` declares it and the repository
contains both `LICENSE` and `NOTICE`. Contributions use a DCO as documented in
`CONTRIBUTING.md`.

### Remaining contributor governance

Before a broad public launch, maintainers should still add:

- a code of conduct;
- a security reporting policy;
- an issue and pull-request template;
- a release and compatibility policy.

The license and DCO choices are settled; the remaining launch policies are still maintainer
decisions.

### Specification drift

The plan and the code disagreed on lexical retrieval in both directions at once. Plan Decision
3b chooses PostgreSQL `tsvector` and `pg_trgm`; `services/retrieval` and `services/indexing`
still advertised the TRS's BM25 (and `services/indexing`, an HNSW/IVF index that Decision 3a
also reverses), while `ports` described the embedding model's sparse output, which is a
Decision 5 contingency rather than Decision 3b's choice. All three docstrings are corrected as
part of this documentation change. The remaining mismatch is inside `spec.md`'s own deviation
note, which needs a spec edit; [architecture.md](architecture.md#the-fr-rag-03-lexical-drift-and-exactly-what-is-left-of-it)
lays out all three positions and which one to build to.

The traceability record also contains historical statuses that must be kept in sync as
features land.

An implementation should cite the controlling decision and register the deviation in the same
pull request. The register is
[`specs/001-procurement-agent/analysis.md`](../specs/001-procurement-agent/analysis.md) — a
numbered `A-n` finding list with a status column, not a separate file. FR-RAG-03's own reversal
is filed there as A-24.

### Contracts are only partially frozen

[`tasks.md` Phase 0](../specs/001-procurement-agent/tasks.md) enumerates eight shared contracts,
C1–C8. **Five of the eight are unfinished**: C1, C3 and C5 are done and the other five are
partial. The distinction matters — a *partial* contract is the more dangerous kind, because
there is enough of it to build against and not enough to be stable:

| ID | Contract | Status in `tasks.md` |
|---|---|---|
| C1 | Postgres schema (`document`, `chunk`, `claim`, `conflict`, `resolution`, `audit.event`) | **done** — all six, plus `job` and `conflict_candidate`, in `sql/00`–`08` |
| C2 | Claim/extraction record | partial — `condition` and the closed field registry have landed; per-category models still do not exist |
| C3 | Provenance reference `(document_id, page, span, extractor_version)` | **done** — all four on `SourceRef`; `span` is the `section` field |
| C4 | Audit event envelope and `event_type` taxonomy | partial — D-13, the Python canonicalisation/envelope/writer/verifier library, and a same-transaction service boundary used by the narrow slice have landed; the general stages remain unwired |
| C5 | Conflict record and the five resolution action shapes | **done** |
| C6 | Canonical workbook projection | partial — D-14, the canonical projection and golden fixture, and an initial deterministic 13-tab writer have landed; advanced workbook gates remain |
| C7 | Retrieval interface and ACL/labelling model | partial — RLS enforces the one label that exists; **D-15** (2026-08-07) adopts that model *provisionally*, contingent on two outstanding facts |
| C8 | Stage runner contract, including the append-only claim invariant | partial — append-only enforced; job states are the DDL's own proposal, no runner |

The evidence behind each, since “done” is the word this repository has most often been wrong
about:

- **C1** — `sql/02`–`08` create every table C1 names. Both of T0.1's checks were run against a
  live cluster and are recorded in `sql/README.md`. The first — the files apply cleanly — is now
  continuous: `test_sql_behaviour.py`'s `schema` fixture reapplies all nine into a freshly
  dropped database on every CI run, so a file that stops applying fails the job. The second —
  `procurement_app` cannot `UPDATE`, `DELETE` or `TRUNCATE` `audit.event` — is guarded rather
  than re-run: `test_sql_schema.py` asserts the narrow grant, and `test_truncate_is_refused`
  exercises the tripwire as the owner, not as `procurement_app`. The absence of Alembic is not a
  gap in C1: C1 asks for the schema, and T0.1 asks that migrations apply, which they do.
- **C3** — `SourceRef` carries `document_id`, `page`, `section` (C3's span) and
  `extractor_version`; `FieldClaim.provenance()` stamps the fourth on, and
  `test_the_extractor_version_reaches_the_store` fails if the projection drops it again.
- **C2** — `condition` is on `FieldClaim`, on `CanonicalField` and on the `claim` table, and
  `test_the_sungrow_trio_raises_no_conflict` covers D-1's worked example. What is left is
  per-category models: `ComponentInstance.fields` is a generic
  `dict[str, list[CanonicalField]]`, and `schema/component.py`'s own docstring says the TRS
  section 7 field sets “are not yet enumerated here”.
- **C4** — `sql/07_audit_event.sql` has the envelope columns, the seven-value `event_type`
  CHECK and `payload_canonical`. D-13 defines RFC 8785 canonicalisation, a versioned JCS
  preimage, and SHA-256. `src/procurement_agent/audit/` implements that contract, appends under
  a pre-insert advisory lock, and verifies chains. `services.transactional_audit` provides and
  live-tests a same-transaction callback boundary. The sanitized-PV slice uses it; the general
  business stages do not yet exist to use it.
- **C7** — argued at length in `tasks.md`. RLS on seven tables, an `app.allow_restricted`
  entitlement, a `procurement_ingest` write role and a chunk-inheritance trigger are real
  enforcement, and they enforce exactly one boolean. `sql/README.md` states that this is C7 “at
  its frozen minimum, not guessed at in full”. **T0.4's deliverable now exists as D-15
  (2026-08-07)**, which ratifies that minimum *provisionally* — the model is adopted, but it is
  contingent on two outstanding facts about NDA scope and evaluator conflicts, which are facts to
  look up rather than preferences to choose.
- **C8** — `sql/04_claim.sql` refuses `UPDATE`/`DELETE`/`TRUNCATE` on `claim` and
  `services/claims` projects canonical values over appended claims, so the append-only invariant
  holds in both halves. The rest does not: `job` carries the five statuses, the lease pair and a
  `UNIQUE` idempotency key, but its own `COMMENT` says it is “this file's own C8-consistent
  design, not a mapping of an existing frozen type”, there is no Pydantic model for it, and
  `orchestrator.run()` raises `NotImplementedError`.
- **C6** — **format frozen by D-14 (2026-08-07); the projection, golden fixture, and initial
  writer have landed.** `services/output/projection.py` emits the canonical bytes with policy and
  computed flags inside the hash and a store-derived `generated_on`. `write_workbook()` emits the
  suppliers-as-rows 13-tab artifact and passes a byte-identity test through the PV slice. Hidden
  state columns/navigation, complete visual QA, the columns orientation, and the gating desktop
  Excel/LibreOffice test remain. `services.claims.project` is C8's claims-to-fields projection,
  a different thing from C6's whole-store-to-hashed-artifact projection.

The partial marker hides materially different gaps. C4 has a complete low-level library and a
transaction boundary used by the narrow slice but not the general stages; C6 has a canonical
hashed projection and initial renderer but not its advanced/desktop gates; C8 has a database job
design and append-only claim invariant but no runner. Read the evidence rather than assuming
every partial contract is at the same stage.

These are shared boundaries. Building multiple adapters before freezing them would create
incompatible records and rework.

### No representative corpus

The plan requires a 30–50 document labeled set, deliberately including poor scans and unusual
layouts. Without it, extraction accuracy, review thresholds, retrieval recall, and throughput
claims cannot be validated.

## Recommended delivery sequence

The fastest path to a useful contributor demo is not to implement every service in parallel.
Build one tested vertical slice:

1. close the remaining factual and shape decisions in C2, C7, and C8;
2. extend the PV slice from trusted CSV to one sanitized native document and parser adapter;
3. extend the narrow PostgreSQL writer into general repositories for documents, claims, conflicts,
   resolutions, and audit events;
4. expose the existing review operation through a minimal authenticated API and durable queue;
5. put the compose-time severity gate in front of workbook generation;
6. validate access isolation and transaction rollback through that complete database path; and
7. run the output through desktop Excel and LibreOffice before expanding formats/categories.

After that slice is stable, add document formats and component families behind the same
contracts.

## Good first contributions

Changes that are useful without depending on unfinished storage contracts include:

- a *vendor* adapter behind any of the six ports — the conformance suite and in-memory
  references now exist to write one against;
- table-driven tests that close remaining schema and traceability gaps;
- sanitized golden fixtures for the existing conflict policy;
- documentation checks and link validation; and
- proposals for the unresolved shared contracts.

Do not build a second workbook renderer beside the initial one. C6's projection and
suppliers-as-rows writer now exist; extend them against the open G.3–G.8 gates and add regression
tests for each feature. The database shape is likewise settled by C1: build against `sql/`
rather than beside it, and treat changes there as contract changes under
[CONTRIBUTING.md](../CONTRIBUTING.md).
