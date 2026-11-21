# Phase 2 execution plan — the seven stories that make the pipeline operational

**Date:** 2026-09-03 · **Baseline:** `main` at `3ba9f43` (PR #36 merged; 1039 passed, 42 skipped,
4 xfailed) · **Status:** proposed · **Supersedes nothing** — [phase-1-execution.md](phase-1-execution.md)
is complete and stays as the record of how the substrate landed.

> **This is about development-time agents, not runtime fan-out.** As in Phase 1,
> [agent-topology.md](../../docs/agent-topology.md) governs where the *pipeline* parallelises at
> run time. This document divides *building* the remaining work across concurrent agents. No
> runtime constraint changes; several are quoted below because the tracks must build to them.

Phase 1 delivered a tested policy core and one fixture-scale sanitized-PV slice. The verification
run on 2026-09-03 found the product story broken at its first operational boundary:
`services.ingestion.ingest()`, `indexing`, `retrieval`, `web_search.search_for_gap()` and
`orchestrator.run()` all raise `NotImplementedError`; there is no repository layer, no runner, no
reviewer surface, and the workbook writer stops at G.2. Seven stories close that. Each has its
own specification under [`phase-2/`](phase-2/); this document is the master: dependency table,
path ownership, merge order, the contracts frozen before anyone fans out, and the dispatch
protocol for running the stories as an agent team.

Reading order for an agent picking up one story: this file → [`phase-2/clarifications.md`](phase-2/clarifications.md)
→ [`phase-2/analysis.md`](phase-2/analysis.md) → the story file → the D-/A-/AC- IDs it cites in
[clarifications.md](clarifications.md), [analysis.md](analysis.md) and [spec.md](spec.md).

---

## The interface technique, restated for Phase 2

[tasks.md](tasks.md) fixed the rule and Phase 1 proved it: each track builds against **committed
fixture files** matching frozen schemas, and **a track's deliverable is the artifact the next
track consumes and asserts**, not "the code works". Phase 2 has more hand-offs than Phase 1 —
parsed elements feed chunking, claims feed the store, the store feeds review and composition —
so the fixture set is larger and it is frozen **first**, in Track 0, before any story fans out.

The other Phase 1 lesson that carries: **scope reconciliation by meaning, not by filename.**
Track 0 of Phase 1 fixed the files it named and left three contradictions in files it did not;
every track here that changes a contract owns the sweep for every document that restates it.

---

## Tracks

**This table is the single source for dependencies.** Prose below explains *why*; where they
disagree, the table wins. **Needs** means *cannot begin until that artifact is committed* — an
artifact dependency, not a scheduling preference. Merge ordering is stated below the table.

| # | Track | Story spec | Needs | Team | Owns these paths |
|---|---|---|---|---|---|
| **0** | Phase 2 contract freeze — the nine additive contracts and their fixtures (§ Contracts below) | this file § Contracts + § Track 0 Verify | — | 1 | `src/procurement_agent/ports/__init__.py`, `adapters/parsed_element.py`, `adapters/capabilities.py`, `adapters/registry.py`, `adapters/vector_store/__init__.py` (`ChunkMetadata` keys only), `schema/registry.py` (optional `web_query_template` only), `schema/principal.py` (`PrincipalContext` only), `schema/chunk.py` (`ChunkRecord` only), `schema/__init__.py` (re-exports), `services/indexing/__init__.py` (`chunk()` return type only), `sql/README.md` (apply glob + reservation rows `10`–`14` only), `tests/test_sql_schema.py` / `tests/test_sql_behaviour.py` / `tests/test_audit_live.py` (apply glob only), `tests/port_contracts/`, `tests/fixtures/parsed/` (new), `tests/fixtures/chunks/` (new), `tests/fixtures/README.md`, `docs/current-state.md` (P2-A-8 baseline count), `specs/001-procurement-agent/plan.md` (P2-A-18 Decision 4 / parse extra), P2-A-24 ports-count sweep files, `specs/001-procurement-agent/phase-2/*.md` (owner of record) |
| **1a** | Parser router, spreadsheet path, Docling PDF/Word path, per-page audit, classification, ACL label at ingest | [story-1](phase-2/story-1-ingest-extract.md) §A | 0 | 2 | `services/ingestion/`, `adapters/parser/` (new backends), `tests/fixtures/ingestion/`, `tests/test_ingestion*.py` |
| **1b** | OCR adapter (PaddleOCR-VL-1.6 on vLLM) and degraded RapidOCR tier | [story-1](phase-2/story-1-ingest-extract.md) §B | 0 | 2 | `adapters/ocr/` |
| **1c** | Schema-constrained extraction, cross-read, plausibility gates, cold-start confidence, `threshold_for()` | [story-1](phase-2/story-1-ingest-extract.md) §C | 0, 1a fixture | 3 | `services/extraction/` (new), `services/confidence/`, `adapters/llm/` |
| **1d** | Gold set — 30–50 labelled documents (**human-produced**; agents build the harness only) | [story-1](phase-2/story-1-ingest-extract.md) §D | 0 | human + 3 | `tests/fixtures/gold/` (new), `tests/test_gold_set.py` |
| **2** | Chunking, triple table indexing, contextual prefix, TEI embedder/reranker adapters, pgvector store adapter, hybrid retrieval with RRF, AC-8 live test | [story-2](phase-2/story-2-index-retrieve.md) | 0, 4a (connection + principal) | 4 | `services/indexing/`, `services/retrieval/`, `adapters/embedder/`, `adapters/reranker/`, `adapters/vector_store/` (new backends), `adapters/lexical_store/` (new), `tests/test_indexing*.py`, `tests/test_retrieval*.py` |
| **3** | Gap-only web enrichment: `WebSearchPort`, gap planner, fetched-page persistence as `SourceDocument`, authority ordering, CEC cross-check, AC-2 end-to-end | [story-3](phase-2/story-3-web-enrichment.md) | 0, 4a (incl. P2-C7), 4b (runner for AC-2) | 5 | `services/web_search/`, `adapters/web_search/` (new), `tests/test_web_search*.py` |
| **4a** | Repository layer: connections, `PrincipalContext` → `SET LOCAL app.allow_restricted`, document/claim/conflict/resolution/job repositories, `sql/10`–`sql/12` | [story-4](phase-2/story-4-persistence-runner.md) §A | 0 | 6 | `services/store/` (new), `sql/10_*.sql`, `sql/11_*.sql`, `sql/12_*.sql`, `sql/README.md`, `tests/test_store*.py`, `tests/test_sql_behaviour.py` (additive), `.github/workflows/ci.yml` (sql job only) |
| **4b** | Runner: `orchestrator.run`, stage handlers, idempotency keys, lease sweeper, retry/backoff, quarantine, `attempt_failed` from the exception handler, compose stage with recorded override, CLI | [story-4](phase-2/story-4-persistence-runner.md) §B | 4a | 6 | `orchestrator/`, `cli/` (new), `pyproject.toml` (`[project.scripts]`), `tests/test_runner*.py` |
| **5** | Review service (five actions, leases, reopen cap) and the reviewer UI (FastAPI + Jinja2 + HTMX, OIDC) | [story-5](phase-2/story-5-review.md) | 0, 4a | 7 | `services/review/` (new), `ui/` (new), `tests/test_review*.py`, `tests/test_ui*.py` |
| **6** | Workbook finish: G.3 hidden state columns, G.4 three channels, G.6 LibreOffice CI gate, G.7 navigation, G.8 completeness manifest, alternate orientation, tabs 12–13 layouts, τ by field | [story-6](phase-2/story-6-workbook.md) | 0 | 8 | `services/output/`, `tests/fixtures/workbooks/`, `tests/test_workbook*.py`, `.github/workflows/ci.yml` (new `workbook` job only) |
| **7** | Access-label facts: the D-15 decision artifact, readiness for each of the three outcomes, re-arming register | [story-7](phase-2/story-7-access-labels.md) | — (human facts); code readiness needs 4a | human + 6 | `docs/decisions/ADR-002-*.md` (new), `docs/access-review.md` (new), `sql/13_access_denylist.sql` (applied, empty), `sql/proposals/restricted_group.sql` (becomes `sql/14` if outcome B is chosen), **not** overlapping 13 |

`sql/` numbers are reserved by Track 0 in `sql/README.md` so parallel worktrees cannot collide:
`10`–`12` Story 4, `13` Story 7 deny-list (applied), `14` reserved for outcome B `restricted_group`,
`15+` unassigned (P2-A-21). **The apply glob must match those names.** Today's `sql/0*.sql` (and
the same glob in `tests/test_sql_schema.py`, `tests/test_sql_behaviour.py`, `tests/test_audit_live.py`)
does **not** match `10_*.sql`. Track 0 changes every copy to a two-digit glob
(`[0-9][0-9]*.sql` — **not** `[0-9][0-9]_*.sql`, which needs `_` as the third character and so drops `09a_hardening_catch_up.sql`; [A-65](analysis.md)) in the same PR as the reservation rows. Track 0 does **not**
write `sql/10`–`14`; 4a and 7 do. Until those files exist the glob change is a no-op on the
`00`–`09` set.

**Start order** — 0 first and alone; it is short (days, not weeks) because every contract in it
is additive. Then 1a, 1b, 1d, 4a, 6 and the human halves of 1d and 7 begin immediately. 1c waits
for 1a's committed parsed-elements fixture; 2, 3 and 5 wait for 4a's connection and principal
primitives; 3's query log needs 4a's `audit.run_event` (P2-C7: `sql/11`–`12` and
`append_run_event`), and its AC-2 path additionally needs 4b's runner.

**Merge order** — `0 → 4a → (1a ∥ 1b ∥ 6) → 1c → 4b → (2 ∥ 5) → 3 → 7-readiness`. 1d and 7's
human artifacts merge whenever they exist. 4a merges before the parser tracks even though they do
not depend on it, because every later track's live tests use its connection fixture and landing it
early stops three teams from writing three connection helpers.

**Counts, kept separate:** eight **ports** (Decision 10 after D-25/D-26), nine **P2-C contracts**,
eight **agent teams**, twelve **tracks**.

**Team assignments.** Eight agent teams plus two human owners (gold set; NDA/roster facts).
Team 1 holds Track 0 and is contract reviewer for every PR that touches `ports/`, `schema/` or
`sql/` — the Phase 1 rule that `schema/` is Team 1's substrate stands. Team 6 holds both halves
of Story 4 because the runner is unusable without the repositories and splitting them invites
two connection layers.

---

## Contracts frozen in Track 0

Nine additive contracts. "Additive" is load-bearing: every one extends a shape with optional
fields or adds a new Protocol beside the six, so no existing test breaks except the pins that
assert the old shape, and those are updated in the same PR. Each is specified in full in the
story that consumes it.

Track 0 writes the **Python type, the fixture and the pin** for every row. Module homes are
load-bearing (the same class of defect as P2-A-28):

- P2-C1 — `ports.ParsedElement` + `adapters/parsed_element.py` conforming in the same PR
- P2-C2 — `ChunkRecord` in `schema/chunk.py`; `ChunkMetadata` keys in `adapters/vector_store/__init__.py`;
  `chunk()` return type in `services/indexing/__init__.py` only (still raises). Track 2 owns the rest of
  `services/indexing/`.
- P2-C3 / P2-C4 — new Protocols in `ports/__init__.py` plus in-memory references
- P2-C5 — `PrincipalContext` in `schema/principal.py`, re-exported from `schema/`. 1a can compile
  before 4a merges. **4a does not define a second class.** 4a writes `open_transaction` /
  `SET LOCAL app.allow_restricted` in `services/store/principal.py` (GUC grep pin stays there).
- P2-C6/C7 — shapes frozen here (CHECK text, event types, glob). SQL files and
  `audit.append_run_event` are 4a deliverables.
- P2-C8 — prefix pin (constant or docstring test); no claim writes
- P2-C9 — optional field on `FieldSpec` in `schema/registry.py`

| ID | Contract | Consumers | Where specified |
|---|---|---|---|
| **P2-C1** | `ParsedElement` gains optional `bbox`, `table: TableData \| None`, `page_quality: float \| None`, `role` (body/furniture/footnote/caption). Kinds stay four — cells live inside `TableData`, not as a new kind. The conformance pin on `ParsedElement.__annotations__` is updated to the new set in the same PR | 1a, 1b, 1c, 2 | [story-1 §A.0](phase-2/story-1-ingest-extract.md) |
| **P2-C2** | `ChunkRecord` in `schema/chunk.py` as in story-2 §1: `chunk_id`, `document_id`, `kind` ∈ prose/table_full/table_row/table_summary, `text`, `body`, `context_prefix`, `page`, `section`, `table_id`, `ordinal`. Replaces `chunk() -> list[str]`; `ChunkMetadata` in `adapters/vector_store/__init__.py` gains `chunk_kind`, `table_id`, `section` | 2, 4a, 6 | [story-2 §1](phase-2/story-2-index-retrieve.md) |
| **P2-C3** | `LexicalSearchPort` — a seventh Protocol beside the six, with `search_lexical(query, *, limit, filters..., allowed_document_ids)`; in-memory reference + capability row; `allowed_document_ids=None` returns nothing, same rule as `VectorStorePort` | 2 | [story-2 §3](phase-2/story-2-index-retrieve.md) |
| **P2-C4** | `WebSearchPort` — eighth Protocol: `search(query, *, limit) -> list[WebHit]` where `WebHit` is `url`, `title`, `retrieved_at`, `provider`; no snippet, no rank persisted (D-20); in-memory reference | 3 | [story-3 §2](phase-2/story-3-web-enrichment.md) |
| **P2-C5** | `PrincipalContext` in `schema/principal.py`: `(subject: str, cleared_for_restricted: bool, denied_suppliers: frozenset[str])`. Every store connection is opened **through** a principal. `SET LOCAL app.allow_restricted` is set from it in `services/store/principal.py` and nowhere else | 2, 3, 4a, 4b, 5, 7 | type: this table; GUC: [story-4 §A.1](phase-2/story-4-persistence-runner.md) |
| **P2-C6** | Claim persistence of D-16: `sql/10_claim_resolution_link.sql` adds `claim.resolution_id` (nullable FK) with `CHECK ((extractor_version LIKE 'human:%') = (resolution_id IS NOT NULL))`; insert order resolution → human claim. Track 0 freezes the CHECK text and the apply glob; 4a writes the file | 4a, 5 | [story-4 §A.3](phase-2/story-4-persistence-runner.md) |
| **P2-C7** | `audit.run_event` (`sql/11`) per D-13 and the taxonomy amendment (`sql/12`) removing `web_search` from `audit.event` and dropping `recorded_at`'s DEFAULT; `audit.append_run_event()`. Track 0 freezes the shape and glob; 4a writes the files and the function | 3, 4b, 5 | [story-4 §A.4](phase-2/story-4-persistence-runner.md) |
| **P2-C8** | `extractor_version` naming: `"<pipeline>@<semver-or-hash>"` for machines, `"human:<oidc-sub>"` per D-16, `"web:<provider>@<version>"` for Story 3, `"gold:<annotator>"` for labels that are never committed to a claim store | 1c, 3, 1d, 5 | [story-1 §C.5](phase-2/story-1-ingest-extract.md) (all four prefixes) |
| **P2-C9** | `FieldSpec.web_query_template: str \| None = None` — silence is the default; Story 3 fills templates for fields worth searching; no parallel query-key on another type | 3 | [story-3 §1](phase-2/story-3-web-enrichment.md) |

### Track 0 Verify

Track 0 has no story file; **this list is its acceptance tests** (dispatch step 2). Adding tests is
fine; removing one is a spec change. Named:

- **P2-C1** `test_parsed_element_annotations_include_optional_bbox_table_page_quality_role` — pin equals `{kind, text, page, bbox, table, page_quality, role}`; `TextElement` in `adapters/parsed_element.py` conforms in the same PR
- **P2-C2** `test_chunk_record_fields` — `ChunkRecord` imported from `procurement_agent.schema`; fields as story-2 §1. `test_chunk_metadata_gains_chunk_kind_table_id_section`. `test_chunk_returns_list_of_chunk_record` — signature pin; still raises
- **P2-C3** `test_lexical_search_port_none_allowlist_returns_nothing` — seventh Protocol; in-memory reference + capability row; `allowed_document_ids=None` → `[]`
- **P2-C4** `test_web_hit_has_no_snippet_or_rank` — eighth Protocol; in-memory reference; fields `url`, `title`, `retrieved_at`, `provider` only
- **P2-C5** `test_principal_context_is_the_schema_type` — imported from `procurement_agent.schema`; a grep pin asserts the class body is not duplicated under `services/`
- **P2-C6 / P2-C7** `test_sql_readme_reserves_10_through_14`; `test_sql_apply_glob_is_two_digit` — `[0-9][0-9]*.sql` (it must match `09a_` as well as `10_`…`14_`; landed as `test_every_apply_glob_applies_every_sql_file`, which holds every copy to the full directory listing, A-65) in `sql/README.md` and the three test globs. `test_the_expected_files_are_all_present` still lists `00`–`09` until 4a/7 add files. Track 0 does **not** write `sql/10`–`12`
- **P2-C8** `test_extractor_version_prefixes` — the four prefixes are pinned (constant or docstring test); no claim writes
- **P2-C9** `test_fieldspec_has_optional_web_query_template` — annotation `str | None`, default `None`; no second query-key type
- **P2-A-8** `docs/current-state.md` baseline is the post-#36 count (1039 passed / 42 skipped / 4 xfailed)
- **P2-A-18** `plan.md` Decision 4 names pypdfium2, not PyMuPDF; the Word path names LibreOffice conversion (D-19)
- **P2-A-24** grep pin: Decision 10's port count reads **eight** in the files that register listed; historical Phase 1 narrative may quote "six" as what landed then, but `ports/__init__.py`, the conformance suite, ADR-001's current count, and `docs/current-state.md` must not

Fixtures in the hand-off table (parsed + chunks) are committed in this PR.

**Gates at merge:** four local gates; existing `sql` job still green (glob change is a no-op until `10` exists).

**Why these are frozen now and not discovered in-track.** Three of them (`P2-C1`, `P2-C2`,
`P2-C3`) are expressibility gaps the Phase 1 inventory found in the ports: `ParsedElement` cannot
carry a bounding box although `OCRPort.recognize`'s docstring promises one; `chunk()` returns bare
strings although `sql/03_chunk.sql` stores `chunk_kind`, `table_id` and `context_prefix`; and no
port can serve the lexical leg Decision 3b requires. Each would be "fixed" differently by the
first team to hit it. See [analysis.md P2-A-1..P2-A-3](phase-2/analysis.md).

---

## Fixture hand-offs

| Fixture (committed, canonical JSON per `tests/fixtures/README.md`) | Produced by | Consumed by | Asserts |
|---|---|---|---|
| `tests/fixtures/parsed/synthetic-pv-datasheet.json` — `ParsedElement` list with a 6-row electrical table, page numbers, one furniture element | 0 (synthetic), re-validated by 1a | 1c, 2 | `TableData` round-trips; every element has `page` |
| `tests/fixtures/parsed/synthetic-scan.json` — same content with `bbox` and `page_quality < 0.5` | 0 | 1b, 1c | D-3 hard gate "low-quality scan" is expressible |
| `tests/fixtures/chunks/synthetic-pv-datasheet.json` — `ChunkRecord` list: prose + `table_full` + 6 `table_row` + `table_summary` | 0 (synthetic), re-validated by 2 | 4a (row shape), 6 (Sources tab) | Decision 6 triple indexing shape |
| `tests/fixtures/claims/*.json` (existing two) + `synthetic-pv-datasheet.claims.json` | 1c | 4a, 5, 6 | Claims carry `P2-C8` versions; keys on-contract |
| `tests/fixtures/conflicts/*.json` (existing) | — | 5, 6 | unchanged; no `resolution` on fixtures |
| `tests/fixtures/workbooks/two-supplier-pv-store.json` + sha256 (existing) | — | 6 | D-14 bytes; **any change is a re-baseline needing permutation-only review** |
| `tests/fixtures/workbooks/two-supplier-pv-store.xlsx.sha256` (new, renderer regression) | 6 | CI | Decision 8c: xlsx hash is a renderer test, not integrity |
| `tests/fixtures/web/synthetic-hits.json` — `WebHit` list + one fetched HTML body | 3 | 3 | D-20 storage rule: no snippet, no rank |
| `tests/fixtures/gold/manifest.json` — document `content_hash` → label file; documents **not** committed | 1d (human) | 1c, CI (skips without corpus dir) | D-11; A-11/B.10 calibration input |

---

## Runtime constraints the tracks build to

Quoted so no track has to rediscover them. Sources: [agent-topology.md](../../docs/agent-topology.md),
[architecture.md](../../docs/architecture.md), the D-IDs.

1. **Workers propose; a single reducer commits.** Claims are append-only proposals; the canonical
   value is `services.claims.project()` over claims, never a stored field. Story 4 therefore
   creates **no canonical-field table**; `detect_conflicts` is the serial per-field reducer.
2. **An ensemble may decide whether to surface, never which value wins.** Union, not vote (E.3a).
   Stage 3's exit threshold is *100 % of injected conflicts surfaced*.
3. **Composition is serial and pure.** `compose_workbook` fans out nothing (FR-OUT-06).
4. **Audit emission originates in the committing worker's transaction** (A-52, Decision 9),
   `advisory lock → tip → INSERT` as its own statements, JCS envelope, caller-supplied
   `recorded_at`. `attempt_failed` is the one event written from an exception handler in a new
   transaction.
5. **RLS is the boundary; `allowed_document_ids` is scoping within an entitlement** (D-15).
   `None` means "forgotten", returns nothing.
6. **Every value has a source** (NFR-01). Web values are `SourceDocument`s too (Story 3).
7. **Sync ports** (Decision 10). Concurrency is processes and executors around the boundary, sized
   by `Settings.max_concurrent_parse` / `max_concurrent_llm` / `web_search_rate_limit_per_minute`.

---

## Agent-team dispatch protocol

How the eight teams run the twelve tracks concurrently without the failure modes Phase 1 recorded.

**Isolation.** One git worktree per track (`.claude/worktrees/` is already the convention); one
branch per track named `phase-2/<track>`; **no agent runs `pytest` in a tree another agent is
editing** — measured in this project's history to produce untrustworthy signals.

**Per-track loop** (the `/verification-before-completion` discipline, made mechanical):

1. Read the story spec and every D-/A-/Q- ID it cites. If a cited ID contradicts the spec, stop
   and file a `P2-A-n` in [analysis.md](phase-2/analysis.md) rather than picking one.
2. Write the failing tests named in the story's **Verify** section first (TDD red). Track 0's
   Verify is [this file](#track-0-verify). A story's Verify list is its acceptance test list;
   adding tests is fine; removing one is a spec change.
3. Implement to green. Run all four gates locally: `uv run pytest`, `uv run ruff check .`,
   `uv run ruff format --check .`, `uv run mypy --strict`. If `sql/` was touched, run the live
   suites against a disposable PostgreSQL with `PROCUREMENT_TEST_DSN` set and confirm the
   **count** of passed tests rose — not that the run was green.
4. Before requesting review, produce the evidence block the story asks for: test counts before and
   after, the fixture diff (must be additive or permutation-only where the story says so), and the
   list of documents swept for meaning.
5. Request review from the contract owner (Team 1) for any change under `ports/`, `schema/`,
   `sql/`, `adapters/capabilities.py`, `adapters/registry.py`; from the consuming track for any
   fixture change.

**Integration agent.** One agent owns the merge order. It merges a track only when: its PR is
green on **every** CI job bound to the head SHA — today `checks` (py3.12 and py3.13) and `sql`;
after Track 6, also `workbook` — not a PR-level summary (Phase 1 recorded `gh pr checks` reporting
a stale commit); the passed-test count is ≥ the count on `main` plus the
story's minimum new tests; and the story's **Done means** artifact is committed. After each merge
it re-runs the full gate set on the integration branch and records counts in the story file's
status line.

**Verification agent** (the `/verification` full-story pass, run after each merge wave). It
re-walks the product story end to end — CSV/PDF in → claims → queue → review → workbook — against
the integration branch and reports the **first broken boundary** with evidence, then stops. The
report format is the one used on 2026-09-03 (Flow Status table with evidence per boundary).

**Humans in the loop.** Two deliverables cannot be produced by any agent and are gates, not
tasks: the gold set (Track 1d) and the D-15 facts (Track 7). Both have a **harness half** agents
build now and a **content half** a person supplies. The plan is written so that the content half
arriving late costs a configuration change, not a redesign.

---

## Clarifications

Numbering continues from Phase 1 (Q-1..Q-4). Full text and options are in
[phase-2/clarifications.md](phase-2/clarifications.md). **D-19 – D-30 were ratified on
2026-09-03 and are now D-19 – D-30 in [clarifications.md](clarifications.md)**; the D-entry is the
authority and the story specs cite it. Q-1 remains open; D-20's commercial half remains with the
product owner.

| # | Question | Blocks | Decision |
|---|---|---|---|
| Q-1 | D-15's two facts (NDA scope; evaluator recusal) | 7 (content); nothing else — code is built ready for all three outcomes | Carried from Phase 1; **open** |
| Q-5 | Word documents carry no page number in Docling provenance; FR-ING-05 promises one | 1a | **Ratified → D-19.** Convert `.docx` → PDF (LibreOffice headless) at ingest and parse the PDF; record `section` always, `page` from the PDF |
| Q-6 | Brave's API terms forbid storing results beyond transient use; FR-WEB-02 requires an immutable query log | 3 | **Ratified → D-20 (engineering rule; commercial half open).** Persist the query string and the **fetched page** (as a `SourceDocument`); never persist provider rank, snippet or result metadata; confirm with counsel before the first production query |
| Q-7 | Reviewer surface stack (plan never chose one) | 5 | **Ratified → D-21.** FastAPI + Jinja2 + HTMX, server-rendered, Authlib OIDC, sync handlers; no JS build |
| Q-8 | Extraction LLM (plan never named one) | 1c | **Ratified → D-22.** `Qwen3-30B-A3B-Instruct-2507` on vLLM with the documented launch flags; dense `Qwen3-32B` as alternate |
| Q-9 | `ParsedElement` shape extension (P2-C1) | 0 | **Ratified → D-23.** Additive optional fields; kinds unchanged; pin updated in-PR |
| Q-10 | Gold-label representation | 1d | **Ratified → D-24.** `FieldClaim` with `extractor_version="gold:<annotator>"`, never committed to a store; documents live outside the repo under `PROCUREMENT_GOLD_CORPUS_DIR` |
| Q-11 | Where the lexical leg lives (P2-C3) | 0, 2 | **Ratified → D-25.** New `LexicalSearchPort`, not a method on `VectorStorePort` |
| Q-12 | CEC weekly pull scheduling — it is not a document job | 3, 4b | **Ratified → D-26.** CLI subcommand + external scheduler; not a `Stage` |
| Q-13 | How a human claim row links to its resolution (P2-C6) | 4a, 5 | **Ratified → D-27.** `claim.resolution_id` FK + CHECK; resolution inserted first |
| Q-14 | CLI framework | 4b | **Ratified → D-28.** stdlib `argparse`; core stays thin |
| Q-15 | A-51: does `ProjectionPolicy` embed the τ table by value or by name? | 6, 1c | **Ratified → D-29, amends D-14.** By value — the hash must change when τ changes |
| Q-16 | What happens at ingest when the access-review register is stale | 7, 1a | **Ratified → D-30.** Warn and label restricted; never block ingest |

---

## Cross-artifact analysis

The full register is [phase-2/analysis.md](phase-2/analysis.md) (P2-A-1 … P2-A-28). The items
that shape this plan:

- **Three expressibility gaps in the ports** (P2-A-1..3) are why Track 0 exists.
- **`tasks.md` says AC-2 is "passing"; `docs/current-state.md` says "partial"** (P2-A-13). Two
  status documents disagreeing is the defect `current-state.md` itself warns about. Story 3 owns
  the correction; until then, `current-state.md` is right.
- **`Stage` has no `reduce` member** (P2-A-5). Runtime constraint 1 places the reducer inside
  `detect_conflicts`; the story says so explicitly so no one adds a seventh stage and breaks the
  `sql/08` CHECK.
- **CI runs `pgvector/pgvector:0.8.6-pg16`; the plan says PostgreSQL 18** (P2-A-19). Not a
  blocker; recorded so the first `pg18`-only feature does not surprise anyone.

### Path collisions

- **Track 0 and Tracks 1a/1b/2/3 on `adapters/`** — 0 owns `parsed_element.py`, `capabilities.py`,
  `registry.py`, and `vector_store/__init__.py` (`ChunkMetadata` keys only); the others add backend
  files only. Registering a new backend edits `registry.py`, which is 0's file: **1a, 1b, 2 and 3
  append entries to `registry.py` in their own PRs after 0 merges**, one `AdapterEntry` block each,
  no other edits.
- **Track 0 and Track 2 on `services/indexing/__init__.py`** — 0 changes `chunk()`'s return type to
  `list[ChunkRecord]` (still raises); 2 implements the body and the rest of the package.
- **Track 0 and Track 2 on `services/indexing/__init__.py`** — 0 changes `chunk()`'s return type to
  `list[ChunkRecord]` (still raises); 2 implements the body and the rest of the package.
- **Track 0 and Track 4a on `sql/README.md` and the apply globs** — 0 lands the two-digit glob and
  reservation rows `10`–`14`; 4a adds the `10`–`12` file-description rows when it writes the SQL
  and extends `test_the_expected_files_are_all_present`. 7 adds the `13` row the same way.
- **Tracks 4a and 6 on `.github/workflows/ci.yml`** — 4a edits the `sql` job; 6 adds a `workbook`
  job. Disjoint hunks; merge 4a first.
- **Tracks 1c and 3 on `services/claims/`** — neither owns it. Both produce claims through the
  existing `FieldClaim`; any needed change to `claims/` is a Team 1 contract PR.
- **Tracks 4b and 5 on `sql/05_conflict.sql` semantics** — 4a owns the repository that leases;
  5 calls it. 5 does not write SQL.

### Windows, ranked by reversibility

| Window | Arms at | Reversible? |
|---|---|---|
| `extractor_version` naming (P2-C8) | 1c's first claim write against a real store | **No.** Claims are append-only; a renamed scheme leaves two vocabularies in the table forever |
| `audit.run_event` preimage and taxonomy (P2-C7) | 3's first web query or 4b's first `--accept-incomplete` | **No.** Same as D-13's `"v"` marker |
| Chunk identity scheme (P2-C2 `chunk_id`) | 2's first `upsert` | Yes at cost — full re-index |
| C7 label model | 1a's first ingest | Yes at cost — ~40 policies (D-15's own estimate) |
| Reviewer identity as OIDC `sub` (D-12a) | 5's first resolution | **No.** `resolved_by` is immutable |

### Risks specific to this plan

- **Track 0 is small and everyone waits on it.** Same shape as Phase 1's Track 1a. Mitigation:
  every contract is additive and its fixture is synthetic; nothing in 0 needs a live service.
- **Two human gates.** The plan degrades gracefully without them — 1c ships with rule-based
  cold-start confidence and Tier-A-always-review (D-3), and 7's code readiness ships with the
  boolean — but AC-1 and AC-6 cannot be *demonstrated* without the gold set, and the label model
  cannot be *confirmed* without the facts. State this in status reports rather than letting
  "harness done" read as "story done".
- **Vendor adapters need vendor services.** CI cannot run vLLM, TEI or PaddleOCR. Every vendor
  adapter ships with (a) the in-memory reference passing the conformance suite for the *contract*,
  (b) a recorded-response test for the *wire shape*, and (c) a DSN-style env-gated live test that
  skips locally and is required on a self-hosted runner. NFR-04 moves to `enforced` only when (c)
  has run against each real backend at least once and the run is recorded.
- **A-50's class will recur a fifth time.** Anything entering a hashed artifact — projection,
  audit payload, `chunk_id`, idempotency key — must answer *could this change without the data
  changing?* Story 4's idempotency key and Story 2's `chunk_id` are the new candidates.

### Not covered

Tabs 12–13 *content* beyond layout (the compliance and tax specialist team of agent-topology
Stage 4) — it needs the extraction path to exist first and is Phase 3. Multi-supplier identity
resolution tuning (D-4 Stage 4 thresholds) waits on the gold set. Migration tooling for `sql/`
(a version table, rollback) is still a decision nobody has taken; Phase 2 keeps the forward-only
numbered files and adds `10`–`13` (`13` is the deny-list, applied empty; `14` is reserved if
outcome B is chosen).
