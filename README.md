# Procurement Agent

Procurement Agent is a human-in-the-loop document intelligence system for comparing
solar and energy-storage equipment. The intended product ingests supplier files, extracts
facts with provenance, supplements missing facts from public sources, surfaces
disagreements for review, and renders a deterministic 13-tab Excel comparison.

The system is designed for a 500 MW solar-plus-storage procurement in ERCOT, but its
source-of-record, provenance, conflict, and review patterns are meant to be reusable in
other evidence-heavy procurement workflows.

> [!IMPORTANT]
> This repository is a **pre-alpha implementation**, not a working end-to-end application.
> The domain model, policy algorithms, and one sanitized-PV CSV vertical slice are implemented
> and tested. General document ingestion, extraction adapters, retrieval,
> web enrichment, the review UI, and orchestration are not yet operational.

## Why this project exists

Supplier evidence arrives as contracts, spreadsheets, native PDFs, scans, images, and
Word documents. The same specification can be stated under different test conditions,
units, product revisions, and system boundaries. A useful comparison must therefore do
more than copy values into a table:

- keep every value tied to its source;
- preserve the ingested documents as the system of record;
- compare values only when their conditions are compatible;
- expose disagreement instead of silently choosing a winner;
- let a human make and explain the final decision; and
- regenerate the same artifact from the same canonical state.

The tool supports procurement decisions. It does not place orders, negotiate, select a
supplier, or make an autonomous procurement decision.

## Current state

This table is a summary derived from [Current state](docs/current-state.md), which is the
source of truth. If the two disagree, that document is right and this one is stale.

| Area | State |
|---|---|
| Canonical Pydantic schema, provenance, closed vocabularies | Implemented and tested |
| Condition-aware comparison and canonical ordering | Implemented and tested |
| Per-field conflict tolerance and declared supplier bands | Implemented and tested |
| Source-of-record overwrite guard | Implemented and tested |
| Compose-time severity gate | Implemented and tested |
| Output flags, 13-tab suppliers-as-rows writer, deterministic XLSX normalization | Implemented and tested |
| Parser, OCR, embedder, vector-store, reranker, and LLM interfaces | Declared as Protocols |
| Ingestion, indexing, retrieval, web enrichment | Stubs; raise `NotImplementedError` |
| Sanitized PV CSV → claims → conflicts → review → workbook slice | Implemented and tested; fixture-scale with in-memory and PostgreSQL persistence |
| PostgreSQL schema, audit hash chain, row-level ACL enforcement | SQL implemented; schema, audit append/verify, and the narrow slice's transactional writer asserted against a live server in CI |
| Audit library and same-transaction write boundary | Implemented and tested; the narrow slice persists documents, claims, conflicts/candidates, resolutions, and audit events transactionally; general stages remain unwired |
| Python persistence layer, worker runner | Narrow sanitized-PV PostgreSQL writer implemented; no general repository layer or worker runner |
| Conflict queue and review | Queue construction and minimal resolution operation, with narrow PostgreSQL persistence; no API/UI |
| 13-tab workbook writer | Initial deterministic suppliers-as-rows writer implemented; advanced G.3–G.8 features remain |
| CLI or deployable service | Not implemented |

See [Current state](docs/current-state.md) for the code-to-spec audit and
[Requirements traceability](docs/requirements-traceability.md) for each FR, NFR, and
acceptance criterion.

## System design

The target flow is:

```text
                         append-only claims
                                │
documents ─► ingest ─► extract ─┼─► reduce ─► canonical store
                    │           │                    │
                    └─► index/retrieve               ├─► detect conflicts
                                                     │          │
public sources ─► gap-only web enrichment ───────────┘          ▼
                                                        review queue
                                                             │
                                           compose-time severity gate
                                                             │
                                                             ▼
                                                  13-tab workbook
```

That full diagram does not run end to end yet. A deliberately narrow path does:
`services.vertical_slice.run_sanitized_pv_csv()` accepts the committed trusted CSV fixture,
creates immutable claims, reduces canonical fields, constructs a conflict queue entry, supports
selection of an existing candidate by a reviewer, and writes the deterministic 13-tab workbook.
It is a contract-integration slice, not a general supplier-document pipeline: PDF/OCR, vendor
adapters, general PostgreSQL repositories, an API/UI, and the runner are still absent.

Human review is deliberately detached from pipeline execution. There is no
`await_human_resolution` stage and there will not be one: one unresolved conflict on document 7
must not stall documents 8 to 400, and "defer" is a mandated resolution action that a blocking
workflow cannot express. Instead the gate is a *query* at composition time — this part is
implemented and tested, in `orchestrator.compose_gate_blocks()`. It blocks only on unresolved
conflicts strictly above the configured severity, and the threshold cannot be set to a value
that disables it.

The source-of-record rule is the other. `assert_no_autonomous_overwrite()` is implemented and
tested; the planned store makes it structural rather than a guard.

The runtime uses PostgreSQL as the single durable store for documents, claims, chunks, jobs,
conflicts, resolutions, and an append-only audit log. `sql/00`–`08` define every one of those
tables and are applied against a live server on each CI run. The audit package can append and
verify events through a caller-supplied connection. The sanitized-PV writer uses that primitive
to persist its narrow document/claim/conflict/resolution slice transactionally; there is no
general persistence layer. Workers will claim jobs with `FOR UPDATE SKIP
LOCKED`; no separate workflow framework is planned.
Heavy integrations sit behind six synchronous Protocol interfaces so an installation can choose
parsers, OCR, models, and storage adapters without changing the domain core. Those six
Protocols are declared, and each has an in-memory reference adapter plus a conformance suite; no
vendor adapter implements any of them yet.

Read [Architecture](docs/architecture.md) for the component boundaries, invariants,
storage model, and important design trade-offs.

## The source-of-record rule

| Tier | Examples | Authority |
|---|---|---|
| `system_of_record` | Ingested contracts and supplier specifications | Authoritative; never overwritten by the web |
| `web_supplement` | Manufacturer pages, certification lists, public datasets | May fill a gap; disagreement becomes a conflict |

A public value can fill an empty field. It cannot replace an ingested value. The current core
enforces this rule at one chokepoint, `assert_no_autonomous_overwrite`, tested directly in
[`tests/test_source_of_record_rule.py`](tests/test_source_of_record_rule.py). The sanitized slice
also demonstrates append-only claims and reducer projection in memory. The production store must
make that split structural — a guard can detect a bad call but cannot prevent another code path
from bypassing it.

## Quick start

Requirements:

- Python 3.12 or 3.13
- [uv](https://docs.astral.sh/uv/)

Clone the repository and install the development environment:

```bash
git clone https://github.com/wilsonhj/Nuclear-Solar-Contract-Procurement-Agent.git
cd Nuclear-Solar-Contract-Procurement-Agent
uv sync --extra dev
```

Run the validation suite:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

All four, not three: `ruff format --check` is the one that gets dropped from an informal
retelling, and leaving it off once shipped a file the other three accepted.

The package exposes domain models, policy helpers, and the fixture-scale vertical slice; there is
no working pipeline command yet. This example exercises the system-of-record guard:

```python
from procurement_agent.schema import CanonicalField, SourceRef, SourceTier
from procurement_agent.services.conflict_hitl import (
    AutonomousOverwriteError,
    assert_no_autonomous_overwrite,
)

contract_value = CanonicalField(
    value=650,
    unit="W",
    verbatim_value="650 W",
    source_tier=SourceTier.SYSTEM_OF_RECORD,
    source_ref=SourceRef(document_id="supplier-spec.pdf", page=3),
    confidence=0.98,
)

web_value = CanonicalField(
    value=655,
    unit="W",
    verbatim_value="655 W",
    source_tier=SourceTier.WEB_SUPPLEMENT,
    source_ref=SourceRef(url="https://manufacturer.example/module"),
    confidence=0.90,
)

try:
    assert_no_autonomous_overwrite(contract_value, web_value)
except AutonomousOverwriteError:
    # A production reducer would create a conflict for human review.
    pass
```

Copy `.env.example` to `.env` only when developing an adapter. For contract and pricing
documents the LLM **and embedding** endpoints must be self-hosted or enterprise, with no
third-party training on contract data. Both endpoints, not just the generative one: an embedder
receives the same contract text and is the easier of the two to point at a public API by
accident.

Nothing under `docs/source/` is committed — the source requirements documents are marked
"Confidential — internal & supplier use" (FRD) and "Confidential — engineering" (TRS). See
[`.gitignore`](.gitignore), which also excludes ingested supplier material and generated
workbooks per NFR-03.

### Optional dependency groups

The core remains intentionally small. Install integrations only when working on them:

| Extra | Purpose |
|---|---|
| `parse` | Docling and pandas document paths |
| `extract` | Schema-constrained extraction client |
| `store` | PostgreSQL and pgvector |
| `solar` | Live, pinned CEC equipment-list processing |
| `dev` | pytest, Ruff, mypy, and type stubs |

Name every extra you want in one command — `uv sync` is an exact sync and removes anything not
named:

```bash
uv sync --extra dev --extra parse
```

### Dependency license gate

**Apache-2.0, MIT and BSD only.** Copyleft and revenue-capped licenses are disqualifying, and
this constrains what may be proposed as an adapter, not merely what ships in core.

The gate has already rejected several of the components most likely to be suggested for this
problem, each verified at its own `LICENSE` file rather than from a summary:

| Component | License | Verdict |
|---|---|---|
| Marker (marker-pdf) | code Apache-2.0, **weights RAIL-M**, free only under $5M revenue | rejected — revenue-capped |
| Surya | GPL-3.0 | rejected — copyleft |
| MinerU | AGPL-3.0 | rejected — copyleft |
| olmOCR | AI Pubs RAIL-M, revenue cap | rejected |
| PyMuPDF | AGPL-3.0 or Artifex commercial | rejected — was declared as an extra, then removed |
| ParadeDB / `pg_search`, VectorChord-bm25 | AGPL-3.0 (VectorChord also ELv2) | rejected — copyleft |
| Jina embeddings v5, NV-Embed-v2 | non-commercial | rejected |

The consequence to internalize: **there is no permissively licensed true-BM25 for PostgreSQL**,
which is why retrieval uses `tsvector` and `pg_trgm` instead, and why FR-RAG-03's BM25 clause is
registered as a reversal rather than implemented. The full gate with rationale is in
[plan.md](specs/001-procurement-agent/plan.md).

## Repository guide

```text
src/procurement_agent/
├── config.py                Settings, PROCUREMENT_ prefix, bounds enforced
├── audit/                   RFC 8785 envelope, hash-chain append and verification
├── schema/                  canonical domain objects and vocabularies
├── ports/                   six swappable integration interfaces
├── services/
│   ├── ingestion/           content routing and extraction entry points
│   ├── indexing/            chunking and index entry points
│   ├── retrieval/           retrieval entry point
│   ├── web_search/          gap-only public-source enrichment
│   ├── claims/              append-only claim record and its projection
│   ├── confidence/          confidence fusion and the Tier A review gate
│   ├── identity/            deterministic supplier and model matching
│   ├── conflict_hitl/       implemented comparison and conflict policy
│   ├── output/              projection, flags, archive normalization, initial writer
│   ├── transactional_audit.py  same-transaction business/audit boundary
│   └── vertical_slice.py    sanitized PV CSV integration path
└── orchestrator/            stage vocabulary and compose-time gate

sql/                         nine numbered DDL files, applied in lexical order,
                             plus a README recording the decisions the specs
                             did not settle. Only the audit library reaches them.
.github/workflows/ci.yml     four blocking gates, plus live schema and audit-chain suites

docs/
├── current-state.md         what works today; the source of truth for status
├── architecture.md          intended production design and its invariants
├── development.md           setup, conventions, and change recipes
├── requirements-traceability.md  every FR/NFR/AC mapped to code and tests
├── agent-topology.md        where the pipeline may fan out, and where it must not
├── defaults.md              proposed values for unresolved decisions
└── open-questions.md        SUPERSEDED by clarifications.md, except for a short
                             carried-forward list at its foot

specs/001-procurement-agent/ normative requirements, decisions, and work plan
tests/                       unit and policy regression tests
```

Start with:

- [Current state](docs/current-state.md) — what works, what is missing, and the highest-risk gaps;
- [Architecture](docs/architecture.md) — intended production design and its invariants;
- [Development guide](docs/development.md) — setup, repository conventions, and change recipes;
- [Contributing](CONTRIBUTING.md) — how to choose and submit work;
- [Requirements traceability](docs/requirements-traceability.md) — requirement-level status;
- [Agent topology](docs/agent-topology.md) — safe fan-out points and required serialization;
- [Researched defaults](docs/defaults.md) — proposed values for unresolved decisions; and
- [Specification index](specs/001-procurement-agent/spec.md) — the normative problem definition.

When two artifacts disagree, they are ranked. Highest first:

1. frozen contracts under `specs/001-procurement-agent/contracts/`;
2. `spec.md`, for externally visible requirements;
3. adopted resolutions in `clarifications.md`;
4. technical decisions in `plan.md`;
5. work decomposition in `tasks.md`; and
6. explanatory code comments and contributor docs — including this file.

`spec.md` is rank 2 and is the artifact most often left out of an informal retelling of this
order, which is how a plan-level decision ends up quietly overriding a requirement. If following
the higher rank would break a lower-ranked decision, register the deviation in `analysis.md`
rather than picking one silently; see
[Specification authority](docs/architecture.md#specification-authority).

Do not infer completion from a design document — use the current-state and traceability
documents.

## Build plan

Five stages, each with the threshold that has to be met before the next one starts. No stage
below is complete; the fixture-scale PV slice demonstrates parts of Stages 1–3 without meeting
their corpus, format, or production-store thresholds. Phase 0 of
[tasks.md](specs/001-procurement-agent/tasks.md) gates all of it. Phase 0 is itself part-done:
three contracts are done and five are partial. C4 now has its Python audit library and C6 has its
canonical projection and golden fixture, but neither has its application-facing completion.

| Stage | Scope | Exit threshold |
|---|---|---|
| 1 | Ingestion + OCR for all formats; lock the canonical schema for PV, inverters, BESS | ≥90% field-level extraction on 20–30 real datasheets, with provenance |
| 2 | Chunking, hybrid retrieval + reranker; Excel writer for the first 3 categories | deterministic regeneration; no unsourced cells |
| 3 | Supplement-only web search; conflict detection; queue + resolution UI | 100% of injected web-vs-spec conflicts surfaced; zero auto-overwrites |
| 4 | Remaining categories; Compliance Matrix and Tax Incentives tabs | all 13 tabs present |
| 5 | Retrieval-time access control, immutable audit log, dedup, self-hosted endpoints | audit, security and idempotency requirements verified |

These stage numbers are load-bearing: [agent-topology.md](docs/agent-topology.md) argues about
concurrency in terms of "Stage 1", "Stage 3's exit threshold" and "Stage 4 work", and this table
is where those resolve. It is a **different axis** from `tasks.md`, which cuts the same work into
Phase 0 (contract freeze) and nine parallel work packages WP-A … WP-I. Stages answer *what is
good enough to move on*; work packages answer *who can build in parallel right now*. Neither
replaces the other, and neither is a schedule.

LLM extraction is imperfect, and the Stage 1 threshold above is the only place that gets
measured. Plan for 92–97% exact match on headline numerics from clean text-layer tables, 80–90%
from scans, and 70–85% on conditional fields — and treat all of those as extrapolations, because
[D-11](specs/001-procurement-agent/clarifications.md) records that no public benchmark exists
for this task and the 30–50 document labelled gold set has not been built. Human review of
flagged fields is mandatory, not optional.

## Contributing

Contributions are welcome, especially small vertical slices that turn a declared interface
into tested behavior. Before starting, read [CONTRIBUTING.md](CONTRIBUTING.md) and choose an
issue whose prerequisite contracts are already settled. Changes that affect a canonical
record, audit envelope, or workbook projection require a written contract decision first, and
a new dependency must clear the license gate above.

The project’s most important near-term milestone is to freeze the remaining shared contracts —
five of the eight are still unfinished (C1, C3 and C5 are done). A fixture-backed vertical slice
from trusted CSV input to a reviewable claim and deterministic workbook has landed; the general
document-ingestion path remains to be built. Read the five partials by
their evidence rather than their marker: C4 has a tested audit library and transaction boundary
used by the narrow slice but not the unimplemented general stages; C6 has a tested projection
and initial XLSX writer but not the advanced workbook gates; and C8 has a database job design
but no Python runner.

## Security and data handling

The intended workload contains confidential contracts and prices. The production design
requires:

- self-hosted or enterprise inference **and embedding** for confidential material;
- document-level access labels applied at ingest and enforced during retrieval;
- a non-owner, non-superuser application database role;
- immutable, hash-chained audit events; and
- no secrets, source documents, or generated procurement workbooks in git.

Three of those five exist in the database schema and are asserted against a live server on every
CI run: the access labels (row-level security on all seven tables holding document content,
`FORCE`d so the owner is not exempt), the restricted application role, and the audit hash chain.
The audit canonicalisation, append, and verification library is also exercised there, including
concurrent writers.
The last one is in force today and is not conditional on anything shipping — `.gitignore`
excludes `docs/source/`, ingested supplier material, and generated workbooks.

**What does not exist is a general application path through that substrate.** The audit package accepts
a connection supplied by its caller, and `services.transactional_audit` binds a callback and its
event to that caller-owned transaction. The sanitized-PV slice has a concrete PostgreSQL writer
and a service-owned atomic commit/rollback boundary; it also accepts a caller-supplied callback
for alternate stores. That writer is deliberately slice-specific: the general stages remain
unwired, and there is no general persistence/session layer.
`services/retrieval.retrieve`
— where the access labels would be enforced *at retrieval time*, which is what NFR-03 actually
asks for — raises `NotImplementedError`. Self-hosted endpoints remain an `.env.example`
convention with no check. Do not use the current repository with real confidential procurement
data.

## Scope boundaries and regulatory disclaimer

Out of scope: placing orders, signing contracts, committing spend, automatically choosing
between conflicting data or recommending a winner, ERP posting, price negotiation, engineering
or yield modelling, and legal review. The tool informs these; it does not perform them.

> [!IMPORTANT]
> **Regulatory and tax content is reported as status, not advice.** Tax, environmental and grid
> rules are evolving through 2026. Confirm against primary sources and with tax and legal
> counsel before relying on any of it for filings.
>
> This is not boilerplate. Two of the thirteen workbook tabs — **Compliance Matrix** and
> **Tax Incentives** — exist to present exactly this material, per-supplier and in a form that
> invites being read as a determination. AC-6 requires the compliance tab to test TRD against
> the correct IEEE 2800 voltage-class limit, where getting the measure wrong "produces a
> compliance matrix that passes suppliers it should fail"; BABA applicability is still recorded
> as `unconfirmed` because it depends on project funding nobody has confirmed. The output is an
> input to a decision made by people who are accountable for it.

## License

**Apache-2.0.** See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Apache-2.0 rather than MIT for one specific reason: its express patent grant (§3). Document
layout analysis, OCR and table extraction are patent-active territory, and this tool is meant
to run inside other companies' infrastructure — MIT would leave every adopter relying on an
implied licence at best. The same clause carries a defensive retaliation trigger, which only
affects a party that sues, and is a large part of why corporate intake processes prefer
Apache-2.0 over MIT rather than merely tolerating it.

The choice is deliberately *permissive*, not reciprocal. Nothing here obliges anyone to
contribute back. MPL-2.0 was the considered alternative — file-level reciprocity, still
OSI-approved, still enterprise-tractable — and was not taken because adoption matters more
here than preventing a fork.

Note that this is independent of the dependency gate above. Permissive licences are
sublicensable, so the inbound rule (Apache-2.0, MIT, BSD only) neither forces nor forbids any
particular outbound choice; they are orthogonal.

Contributions are accepted under a **DCO**, not a CLA — see [CONTRIBUTING.md](CONTRIBUTING.md).
