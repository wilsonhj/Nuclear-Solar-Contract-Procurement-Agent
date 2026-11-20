# Tasks: parallel work breakdown

**Spec:** [spec.md](spec.md) · **Plan:** [plan.md](plan.md) · **Decisions:** [clarifications.md](clarifications.md) · **Analysis:** [analysis.md](analysis.md)

> **Execution plans.** Phase 1 (the substrate) is recorded in
> [phase-1-execution.md](phase-1-execution.md) and is complete. The remaining work packages
> WP-A through WP-I are re-cut as seven stories with a dependency table, path ownership and an
> agent-team dispatch protocol in [phase-2-execution.md](phase-2-execution.md); per-story
> specifications, clarifications and the consistency register live under
> [`phase-2/`](phase-2/). Where this file's WP-level bullets and a story spec disagree, the story
> spec is the more recent reading and this file needs the sweep.

Structured for several teams working simultaneously. Read **Phase 0 first** — nothing
parallelises until the contracts are frozen, and the most common way a plan like this fails is
teams shipping seven incompatible versions of the same record.

> **Anti-pattern, named explicitly:** do not assign teams by pipeline stage *and* let them define
> their own interfaces. **The contracts are the deliverable of week one; the stages are the
> deliverable of week two onward.**

---

## Phase 0 — Contract freeze (BLOCKING, ~1 week, 1–2 people)

The only true serialisation point in the plan. Everyone else reviews; nobody implements against
an unfrozen contract.

| ID | Contract | Gates | Status |
|---|---|---|---|
| **C1** | Postgres schema: `document`, `chunk`, `claim`, `conflict`, `resolution`, `audit.event` | Every WP | **done** — all six, plus `job` and `conflict_candidate`, in `sql/00`–`08`; both T0.1 checks recorded live-verified in `sql/README.md`, and CI reapplies all nine files each run |
| **C2** | Claim/extraction record — Pydantic + JSON Schema, including `condition` per D-1 | B, E, G | **partial** — `condition` landed on `FieldClaim` and `CanonicalField`, and the **field registry landed 2026-08-12**: 125 `FieldSpec`s over 124 contract keys in `schema/registry.py`, bidirectionally tested against the frozen markdown, with off-contract keys refused at both points that use a key as a key (`ComponentInstance.fields`, `commit_claims`). `FIELD_TOLERANCES` and the confidence Tier A table now derive from it rather than restating it. Per-category *models* still do not exist — the registry types the keys, it does not generate a model per category |
| **C3** | Provenance reference — `(document_id, page, span, extractor_version)` | A, B, D, F, G | **done** — all four on `SourceRef`, **`span` under the name `section`**, stamped by `FieldClaim.provenance()` and pinned by `test_source_ref_carries_c3s_four_elements` |
| **C4** | Audit event envelope + `event_type` taxonomy + canonicalisation rule | All | **partial — decision and document-event library halves are closed.** [D-13](clarifications.md) settles RFC 8785 via pinned `rfc8785`, a versioned JCS preimage, SHA-256, and the additive v1 taxonomy. `audit/` supplies canonicalisation, envelope/preimage, advisory-lock append, and chain verification; `services.transactional_audit` supplies a live-tested caller-owned transaction primitive. The sanitized-PV slice wraps that primitive in a service-owned atomic commit/rollback boundary and appends all document-scoped intents with its supplied business write. **P2-C7 landed 2026-11-20:** `audit.run_event` (`sql/11`) and `append_run_event` give run-scoped events — gap-triggered web searches, the `--accept-incomplete` override, run start/finish — their own chain on the same envelope; `sql/12` removes `web_search` from `audit.event` (the A-49 taxonomy fix) and adds `no_self_parent` to both tables; `verify.py` walks both stream kinds. `recorded_at`'s default was already gone (`09a`), so that item was stale. **What remains is integration into the general stages**, which waits on the stages themselves (story 4b), plus one open decision recorded as [A-67](analysis.md): `audit.run_event` has no row-level security, so its emitters must not quote document content |
| **C5** | Conflict record + the five resolution action shapes | E, F, G | **done** — `ConflictQueueEntry`, `ResolutionAction` |
| **C6** | Canonical workbook projection — sorted-key JSON, floats via `repr()` | G | **partial — the hashed artifact and initial renderer exist.** [D-14](clarifications.md) (adopted 2026-08-07, amended 2026-08-12) freezes the bytes, shape, `encode_value()`, policy-inside-the-hash and store-derived `generated_on`; `services/output/projection.py` and `tests/fixtures/workbooks/` implement and pin it. `write_workbook()` now emits the deterministic suppliers-as-rows 13-tab artifact and the sanitized-PV slice tests byte identity. G.3–G.4 and G.6–G.8, the alternate orientation, and the gating desktop test remain (`services.claims.project` is C8's claims→fields projection, a different thing) |
| **C7** | Retrieval interface + ACL/labelling model | A, C | **partial** — RLS enforces the one label the schema has (`access_restricted`). [D-15](clarifications.md) adopts that model **provisionally** and closes T0.4's written-decision criterion, but two facts remain outstanding and they are facts rather than preferences: whether any executed NDA exceeds "need to know", and whether any evaluator is conflicted with a specific bidder. Either yes makes the label `restricted_group` |
| **C8** | Stage runner contract — job states, claim/lease semantics, idempotency key, **plus the append-only claim invariant below** | A, B, D, E, I | **partial** — the append-only invariant is enforced in both halves; `job` is the DDL's own proposal and `orchestrator.run` raises `NotImplementedError` |

Three of the eight are done and five are partial; none is now untouched. Read the five partials
carefully rather than by their marker. **C8 is the one with a finished SQL half and an unstarted
Python half** — `sql/08_job.sql` has the states, the lease and the idempotency key, and WP-I's
runner is a file nobody has opened yet, so it is the contract most likely to be mistaken for
finished because `sql/` is the visible artifact. C4 was in that pair until 2026-08-12, when
WP-H's library landed. The sanitized-PV slice now calls its transaction boundary; what remains is
general-stage integration and the separately chained run-event path.

> **C8 invariant — workers propose, they do not commit.** Each extraction writes an **immutable
> claim row** keyed by `(document_id, field, extractor_version)`; the canonical value is a
> **projection over claims**, never an in-place update. Only the reducer takes a store write
> handle, and it calls `assert_no_autonomous_overwrite` internally.
>
> This is a *structural* property; the guard alone gives only a *value* property. It is a pure
> predicate over two fields — it takes no store handle, performs no write, and **cannot enforce
> that it is called**. It also deliberately permits record-over-record (inter-document conflict is
> the queue's job), which under concurrent writers is a lost update it passes silently. And it has
> no notion of ordering: two workers finishing in different orders yield different
> last-writer-wins outcomes, making the store itself non-deterministic — which defeats FR-OUT-06
> even though composition is a pure function of that store.
>
> With append-only claims there is no overwrite to guard. The unreachability test ("the write API
> is not reachable from worker context") becomes writable once C1 and C8 land, and should gate
> them.

**C1, C2, C3 and C7 are the expensive ones to change.** C1 and C3 have since landed, which makes
them *more* expensive rather than retiring the warning — there is now DDL applied to a live
cluster, a CI suite pinning its behaviour, and a test asserting C3's fourth element survives the
projection. C2 and C7 are the two partials with the most remaining contract work, and they are
where the remaining contract effort is best spent.

> ⚠️ **C4 must ship before any stage emits events.** Changing the hashed field set later
> invalidates every existing chain. WP-H ships its library first, even if thin. **The table
> shipped without the library.** `sql/07_audit_event.sql` defines the envelope, the `event_type`
> CHECK and `payload_canonical`. **[D-13](clarifications.md) (adopted 2026-08-07) closed the
> decision half**: the scheme is RFC 8785, the preimage is one JCS object carrying `"v": 1`, the
> digest is SHA-256, and `sql/07`'s caller-sequence comment now carries that object literally —
> so the bytes the `hash` column covers are **defined**. The taxonomy is version 1 with
> additive-only amendment.
>
> That library landed 2026-08-12: `src/procurement_agent/audit/` supplies H.2's canonicalisation
> (`rfc8785==0.1.4`, pinned exactly and conformance-tested against RFC 8785's published vectors),
> H.3's envelope and preimage, H.4's pre-INSERT advisory lock — load-tested against a live server,
> where dropping it reproduced 41 silent forks — and H.5's chain-verification CLI. The bar this
> paragraph set is met: an event can now be computed, so one may be emitted.
>
> The sanitized-PV path now emits every document-scoped intent through
> `persist_vertical_slice()` on the same service-owned transaction as its supplied business write;
> it commits the pair atomically and returns cleared pending intents only after commit succeeds.
> C4 stays partial because the general stages do not use it and the separately chained
> `audit.run_event` path, A-49 taxonomy fix, and removal of `recorded_at`'s stale `DEFAULT` remain.

> ⚠️ **C7 is a single decision constraining two work packages at opposite ends of the pipeline**
> (labelling at ingest, enforcement at retrieval). This is the most common place this kind of
> plan breaks. **The enforcement mechanism has now landed ahead of the decision**: `sql/`
> applies `FORCE ROW LEVEL SECURITY` to seven tables, keyed on the single boolean
> `SourceDocument.access_restricted` and gated by an `app.allow_restricted` session GUC, and
> `sql/README.md` says in as many words that this is C7 "implemented at its frozen minimum, not
> guessed at in full". That was the right way to have built it, and **T0.4 is now written as
> [D-15](clarifications.md) (2026-08-07)**, which ratifies that minimum rather than replacing it:
> one document-level label, per-principal clearance from the OIDC subject, labelling at ingest
> failing closed, and `VectorStorePort.search(allowed_document_ids=...)` as scoping *within* an
> entitlement rather than the boundary. It is **provisional** — two facts about NDA scope and
> evaluator conflicts remain outstanding, and either one turns the label into
> `restricted_group`.

### Phase 0 tasks

- **T0.1** Write the Postgres DDL for C1, including `audit.event` with hash-chain columns and
  privilege separation per plan Decision 9. → verify: migrations apply cleanly; app role cannot
  `UPDATE`/`DELETE`/`TRUNCATE` `audit.event`.
- **T0.2** Add `condition` to `CanonicalField` and define the condition vocabulary per parameter
  family (D-1). → verify: the Sungrow SG350HX case (four apparent conflicts, zero real) resolves
  to zero conflicts in a fixture test.
- **T0.3** Define the per-field tolerance table from D-2 as data, not code. → verify: a table-driven
  test asserts nameplate Pmax at ±1 W does not merge adjacent 5 W bins.
- **T0.4** ~~Decide the ACL/labelling model (C7)~~ → **written as [D-15](clarifications.md), provisionally adopted 2026-08-07.** The verify criterion is met; the model is contingent on two outstanding facts recorded in that decision.
- **T0.5** ~~Freeze the canonical workbook projection format (C6)~~ → **frozen as [D-14](clarifications.md), adopted 2026-08-07, and verified.** `services/output/projection.py` implements the canonical bytes; `tests/fixtures/workbooks/two-supplier-pv-store.json` and its SHA-256 file are the golden fixture and digest. The initial XLSX writer has since landed; its remaining G.3–G.4 and G.6–G.8 work is outside this projection-format gate.
- **T0.6** Publish fixture sets for every contract (see below).

### The decoupling technique: frozen fixtures, not running code

Each team builds against **committed fixture files** matching the frozen schemas. WP-B ships
golden claim JSON; WP-E consumes it and ships golden conflict JSON; WP-F and WP-G consume that.
**Nobody waits on anybody's service.** The fixtures are the contract made executable, and they
double as the regression suite.

---

## Phase 1 — Parallel work packages

All nine run concurrently once Phase 0 lands.

### WP-A · Ingest & storage
**Depends:** C1, C3, C7

- **A.1** Content-signature router (FR-ING-01). Route by magic bytes, never extension.
- **A.2** Spreadsheet path — openpyxl/pandas direct, bypassing document parsing (plan Decision 4).
  Preserve sheet names, headers, merged cells, numeric typing (FR-ING-02).
- **A.3** Text-layer PDF path — Docling + TableFormer ACCURATE (FR-ING-03).
- **A.4** Scanned/image path — PaddleOCR-VL-1.6 on vLLM, bounding boxes retained (FR-ING-04).
  → **Trina datasheets are image-only PDFs with no text layer.** This is a tier-1 supplier, not
  an edge case. Use one as the acceptance fixture.
- **A.5** `.docx` path (FR-ING-05).
- **A.6** Per-page audit loop — zero characters, <10% of raw text count, or zero table cells on a
  detected table region → re-run on next engine. `PARSE_FAILED` regions are recorded, never
  dropped.
- **A.7** Dual-parse reconciliation on table-critical pages; emit cell-level disagreement as a
  **confidence feature**, not just a log line.
- **A.8** Document classification into the eight types (FR-ING-06).
- **A.9** Content hashing + stable IDs; re-ingest is a no-op (FR-ING-09, NFR-05). → verify: **AC-5**.
- **A.10** ACL labelling at ingest per C7.
- **A.11** Text normalisation per D-5 — NFKD, ligature folding, `–`→`-`, `℃`→`°C`, and
  **decimal-comma handling for IEC-sourced documents**. → verify: `10,5 kV` parses as 10.5, not
  105. This is the highest-risk lexical trap in the spec.

### WP-B · Extraction & confidence
**Depends:** C2, C3, C4

- **B.1** vLLM structured outputs (xgrammar) with `logprobs` enabled. → ⚠️ **`json_schema` mode,
  never Instructor's `TOOLS` mode** — tool calls do not emit logprobs, and using them throws away
  the entire confidence signal (plan Decision 7).
- **B.2** Per-category Pydantic extraction models from
  [contracts/canonical-parameters.md](contracts/canonical-parameters.md).
- **B.3** Unit normalisation per D-5. → ⚠️ **`%/°C` ≡ `%/K` requires NO conversion.** Hard-code as
  aliases; routing through a temperature converter applies +273.15 and silently destroys the value.
- **B.4** Condition extraction (D-1) — STC vs NOCT, `@30°C` vs `@40°C`, AC vs DC side, BOL vs EOL.
- **B.5** Domain plausibility rules as both feature and hard gate: `Voc > Vmp`, `Isc > Imp`,
  `Pmax ≈ Vmp × Imp ± 0.5%`, `PTC/STC ∈ [87%, 96%]`, `|γPmax| ∈ [0.15, 0.70]`, γPmax and βVoc
  negative, αIsc positive.
- **B.6** Cross-read disagreement — two structurally asymmetric extractions (field-guided and
  document-guided), scored for agreement.
- **B.7** Rule-based cold-start confidence score (D-3). **Do not implement self-reported
  confidence** (0.692 AUC, banned) **or self-consistency N=5** (0.744 AUC at 5× cost).
- **B.8** Log every human correction as a training label.
- **B.9** ⚠️ **Build the 30–50 document labelled gold set. This is task one of week one**, ahead of
  any optimisation. No public benchmark exists for this task (D-11); every accuracy figure in the
  plan is extrapolated. Seed deliberately with poor scans and unusual layouts.
- **B.10** Define `threshold_for(field_name)` — the tiered τ that WP-G consumes (see A-11).

### WP-C · Index, retrieval & access control
**Depends:** C1, C7

- **C.1** Structure-aware chunking (D/plan Decision 6). **Tables are never token-chunked.**
- **C.2** Triple table indexing — `table_full`, `table_row` (column names inlined per row),
  `table_summary`. → verify: "what is the Voc of module X" retrieves the right row.
- **C.3** Contextual retrieval — prepend document/section context before embedding.
- **C.4** Qwen3-Embedding-4B at 1024 dims via MRL truncation + renormalisation.
- **C.5** Hybrid retrieval: dense + `tsvector` + `pg_trgm`, fused with RRF (k=60).
  → verify: `"JKM610N-66HL4M-V"` retrieves `"JKM610N 66HL4M V"`.
- **C.6** bge-reranker-v2-m3 over top-50 → top-5–8.
- **C.7** `FORCE ROW LEVEL SECURITY`; app connects as non-owner, non-superuser. → verify: **AC-8**.
- **C.8** ⚠️ **No ANN index** (plan Decision 3a). Set `hnsw.iterative_scan = relaxed_order` in
  `postgresql.conf` anyway, so a future index isn't silently wrong.
- **C.9** ⚠️ **Regression test asserting `len(results) == k`** on a filtered query. Every pgvector
  filtered-search failure mode presents as a short result set — measured, a top-10 request
  returned 5 rows with no error, and 0 rows with an ACL array. **Nothing else catches this.**

### WP-D · Web enrichment
**Depends:** C2, C3, C4

- **D.1** Gap-triggered query planning (FR-WEB-01).
- **D.2** Tag every result `web_supplement` with URL, title, timestamp; log the query (FR-WEB-02).
- **D.3** Source-authority capture and ordering (FR-WEB-05).
- **D.4** ⚠️ Route every write through `assert_no_autonomous_overwrite`. → verify: **AC-2**.
- **D.5** CEC cross-check integration (D-8) — weekly pull of the four XLSX exports, surrogate ID,
  alias seeding from the `Notes` column. → ⚠️ CEC is **not** an authority for utility-scale BESS.
  → ⚠️ Do **not** use pvlib's bundled CEC data: frozen at 2019, **zero modules ≥600 W** (D-8a).

### WP-E · Conflict detection
**Depends:** C2, C5 · *Most parallelisable — a pure function, fully fixture-testable.*

- **E.1** `values_conflict()` per the D-2 three-kind model with the rounding floor.
- **E.2** Condition-matching gate — **mismatched conditions are not a conflict, they are not a
  comparison** (D-1). This gate runs before any tolerance check.
- **E.3** The five conflict classes (FR-HITL-01).
- **E.3a** ⚠️ **If any ensemble is ever used here, aggregate by UNION, never by vote.** Stage 3's
  exit threshold is *100% of injected conflicts surfaced*. A 2-of-3 vote that suppresses a real
  conflict is a spec violation, not a false positive saved. Written down now because the verifier
  pattern is a natural thing to reach for later.
- **E.4** Identity resolution per D-4 — four stages, `(manufacturer, token)`-keyed suffix rules,
  electrical corroboration. → ⚠️ verify with the **Trina split-entity case**: bins 635–725 W under
  `Trina Solar Co.,Ltd`, bins 730/735/740 under `Trina Solar`. Looking up
  (`Trina Solar`, `TSM-700NEG21C.20`) must not return empty.
- **E.5** Generated conflict explanations (FR-HITL-03).
- **E.6** Transformer-specific gates per D-6: standards-regime detection (IEEE vs IEC) before any
  comparison of MVA, %Z or losses; `Dyn1` vs `Dyn11` treated as **incompatible, not equivalent**.
- **E.7** BESS boundary gate per D-7 — RTE and energy comparisons across differing boundaries are
  *"requires boundary normalisation"*, not a numeric conflict.

### WP-F · HITL queue service & review UI
**Depends:** C5, C8

- **F.1** Conflict table with lease-based claiming (`FOR UPDATE SKIP LOCKED`, 15-min lease).
- **F.2** The five resolution actions. `keep_sor` writes an explicit resolution — *not* a no-op, so
  "we looked and chose the contract value" stays distinguishable from "nobody looked".
- **F.3** `request_more_web_search` reopen cap at 3, then force a terminal decision.
- **F.4** Lease sweeper. **Conflicts themselves never expire** (D-12c) — they age, and age is a
  reported metric.
- **F.5** Review UI. ⚠️ **You will build this regardless** — no generic approval page serves
  provenance-carrying extracted-value review. This removes the "free UI" argument for every
  orchestration framework considered.
- **F.6** Implement D-10: Excel is read-only output; resolution happens in the application.

### WP-G · Workbook composition & determinism
**Depends:** C6 · *Fully decoupled — build against the golden projection from day one.*

- **G.1** Canonical sorted-key JSON projection; floats via `repr()`. **This is the hashed artifact
  of record**, not the xlsx — `%.16g` maps `0.1+0.2` and `0.3` to identical bytes, so a workbook
  hash cannot distinguish two genuinely different stored numbers.
- **G.2** ~~All 13 tabs (FR-OUT-02).~~ → implemented by the initial writer and pinned by `test_writer_emits_all_tabs_provenance_and_open_item`; **AC-3 remains partial** until all formatting/desktop gates pass.
- **G.3** Hidden parallel state columns for provenance. → ⚠️ **no blank column between value and
  state blocks**, and `auto_filter.ref` **must span the hidden columns**. Both fail silently on
  first sort. Assert both in the generator.
- **G.4** Three orthogonal visual channels — fill=origin, font=confidence, border=conflict
  (plan Decision 8b). → verify: greyscale separation and WCAG AA contrast.
- **G.5** Deterministic render — `ExcelWriter` direct (bypassing `save_workbook`, which re-stamps
  `modified = now()` *after* you set it), zip normalisation at epoch 1980-01-01 **12:00**.
  → ⚠️ `ZipFile(compresslevel=)` is **silently ignored** with a hand-built `ZipInfo`; set
  `zi._compresslevel`. → the initial writer now passes a two-generation byte-identity test;
  **AC-7 remains partial** until the production-store and G.6 desktop gates pass.
- **G.6** ⚠️ **GATING: open the generated workbook in real desktop Excel and LibreOffice.** The
  determinism recipe was validated only by openpyxl round-trip and OPC structural checks. Test
  `[Content_Types].xml`-first ordering and the 1980 timestamps before this ships. A verified
  fallback exists (post-save `core.xml` rewrite) — see [analysis.md A-9](analysis.md).
- **G.7** Conflicts and Sources tab layouts with bidirectional `MATCH`-based navigation.
- **G.8** Completeness manifest listing every unresolved conflict.

### WP-H · Audit log
**Depends:** C1, C4 · *Ships a library the other packages call. Must land first.*

- **H.1** DDL + privilege separation (`NOLOGIN` owner role, app gets `INSERT, SELECT` only).
- **H.2** RFC 8785 canonicalisation **in Python, not SQL** — `jsonb` normalises key order but
  preserves numeric formatting, so `1.0` and `1.00` stay textually distinct.
- **H.3** Per-document hash chaining (`stream = 'doc:1234'`).
- **H.4** ⚠️ **Advisory lock as its own statement before the INSERT** — a lock *inside* the trigger
  does not work, because the statement snapshot is taken before the trigger acquires it. Measured:
  8 concurrent writers produced **42 silent forks** without this. Add `UNIQUE(stream, prev_hash)`
  so any remaining fork is loud.
- **H.5** Chain verification CLI.
- **H.6** `BEFORE TRUNCATE` statement-level trigger as a secondary tripwire — **not** the boundary.
  Document that `session_replication_role='replica'` bypasses triggers entirely and leaves no DDL
  trace.

### WP-I · Runner & stage state machine
**Depends:** C8

- **I.1** Job table + `FOR UPDATE SKIP LOCKED` worker loop (plan Decision 1).
- **I.2** Per-stage retry with backoff; **every stage must be independently idempotent** — retries
  are at-least-once and do not undo side effects.
- **I.3** Compose-time gate: query for unresolved conflicts above severity, overridable with a
  recorded `--accept-incomplete` decision (plan Decision 2). **No blocking interrupt.**
- **I.4** Poison-message handling and quarantine.
- **I.5** Observability; verify NFR-06 and NFR-07 against a real corpus.

---

## Genuine sequential dependencies

These cannot be parallelised away. Plan for them rather than discovering them.

1. **Phase 0 precedes all implementation.** The only true serialisation point.
2. **C7 (ACL model) blocks both WP-A and WP-C** — opposite ends of the pipeline, one decision.
3. **WP-H ships its library before any stage emits events.** Changing the hashed field set later
   invalidates every existing chain.
4. **Real extraction output is needed to *tune* conflict detection, not to *build* it.** WP-E
   builds against fixtures on day one; only its thresholds wait on WP-B. **Sequence the tuning,
   not the build.**
5. **Real conflicts are needed to *validate* the review UI**, same shape. WP-F builds against
   synthetic conflicts; usability review waits on WP-E.
6. **The determinism golden hash can only be locked once the workbook's shape settles.** WP-G's
   normalisation layer is buildable immediately; G.6 is the gate.

---

## Acceptance criteria ownership

| AC | Owner | Status |
|---|---|---|
| AC-1 scanned spec sheet → fields with provenance, low confidence to review | WP-A + WP-B | ☐ needs corpus |
| AC-2 web contradiction raises conflict, record unchanged | WP-D + WP-E | **passing** |
| AC-3 all 13 tabs with conditional formatting | WP-G | partial |
| AC-4 every cell resolves to a source | WP-G | **passing** (schema level) |
| AC-5 re-ingest creates no duplicates | WP-A | ☐ |
| AC-6 TRD against correct voltage-class limit; tax status per supplier | WP-B + WP-G | ☐ |
| AC-7 byte-identical regeneration | WP-G | ☐ gated on G.6 |
| AC-8 unclear user cannot influence retrieved results | WP-C | ☐ |

---

## Suggested team allocation

| Team | Packages | Rationale |
|---|---|---|
| 1 | Phase 0, then WP-H, WP-I | Owns the contracts, so owns the substrate everyone builds on |
| 2 | WP-A | Largest surface; parser router is core IP |
| 3 | WP-B | Deepest domain work; owns the gold set |
| 4 | WP-C | Self-contained; owns the pgvector findings |
| 5 | WP-D + WP-E | Both pure-ish functions over claims; share the tolerance table |
| 6 | WP-F + WP-G | Both consume the canonical projection; share the reviewer's mental model |
