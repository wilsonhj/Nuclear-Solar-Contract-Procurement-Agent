# Implementation Plan: Procurement Agent

**Spec:** [spec.md](spec.md) · **Decisions:** [clarifications.md](clarifications.md) · **Work breakdown:** [tasks.md](tasks.md)
**Date:** 2026-07-28

The TRS deliberately defers stack selection: *"vector DB, OCR engine, LLM, and framework
selection are design decisions."* This document makes those decisions. Every one carries a
rationale and a confidence level; the low-confidence ones are the ones to revisit first.

---

## Technical context

| | |
|---|---|
| Language | Python 3.12 (`>=3.12,<3.14`) |
| Package manager | uv |
| Primary datastore | PostgreSQL 18 + pgvector 0.8.5 |
| Scale | Hundreds of documents, ~10k–100k chunks. Batch/offline ingestion. |
| Deployment | Single-node is sufficient. Confidential inference must be self-hostable. |
| Licence posture | Apache-2.0 / MIT / BSD only. Copyleft and revenue-capped licences are disqualifying. |

---

## Licence gate — components rejected

Research verified licences at primary sources rather than assuming. Several widely recommended
components fail the gate, and they are named here so nobody re-proposes them.

| Component | Actual licence | Verdict |
|---|---|---|
| Marker (marker-pdf) | Code Apache-2.0, **weights RAIL-M, free only under $5M revenue** | Rejected — revenue-capped |
| Jina embeddings v5 | non-commercial — **licence name unverified**, see below | Rejected |
| NV-Embed-v2 | CC BY-NC | Rejected — non-commercial |
| ParadeDB / `pg_search` | **AGPL-3.0** | Rejected — copyleft |
| VectorChord-bm25 | AGPL-3.0 / ELv2 | Rejected — copyleft |
| MinerU | AGPL-3.0 | Rejected — copyleft |
| Surya (and Marker via Surya) | GPL-3.0 | Rejected — copyleft |
| olmOCR | AI Pubs Rail-M, revenue cap | Rejected |

**Consequence to internalise: there is no permissively licensed true-BM25 for PostgreSQL in
2026.** Both credible extensions are AGPL. This shapes the retrieval design below — and pushes
it toward a better answer for this corpus, not a worse one.

> ⚠️ **Re-verify licence *names* at the `LICENSE` file before any of these reach procurement
> paperwork.** Independent review found that the widely repeated "CC BY-NC 4.0" label for Jina
> embeddings v4 is **wrong** — it is actually the Qwen Research License (Alibaba Cloud, Chinese
> jurisdiction). Our v5 entry comes from the same class of secondary source and may carry the
> same error. The **rejection** is unaffected either way; the licence name is what would end up
> in a contract. This applies to every row above that was not read from a `LICENSE` file.

---

## Decision 1 — Orchestration: a Postgres state machine, not a workflow framework

**Chosen:** hand-rolled stage state machine over a Postgres job table with a
`SELECT … FOR UPDATE SKIP LOCKED` worker loop. **Confidence: medium-high.**

**Rejected: LangGraph as the pipeline orchestrator.** This reverses the direction the reference
memo pointed and contradicts the `agent` extra currently in `pyproject.toml`, which is corrected
as part of this plan.

The argument is not that LangGraph is bad. It is that two requirements already force canonical
state into your own tables:

- NFR-02 demands an immutable audit log of every extraction, query, conflict and resolution.
  Those facts are therefore **already durable rows in Postgres**.
- FR-OUT-06 demands the workbook be regenerable from the canonical store, which makes
  composition a **pure function of that store**.

Once both hold, a workflow checkpointer is a *second copy of state you already own*, in a
library-versioned schema (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`,
`checkpoint_migrations`) that application code is explicitly not supposed to query. You would
be maintaining consistency between two sources of truth for a seven-stage linear pipeline.

And the corollary that closes it: **if composition is a pure function of the store, "resume the
workflow" is unnecessary machinery.** You re-run the function.

Supporting points:
- The agentic part isn't agentic. Schema-constrained extraction is a structured-output call in a
  loop. It needs retries and idempotency, not cyclic graph control flow — LangGraph's actual
  differentiator goes unused.
- LangGraph OSS has **no thread-listing API**. You cannot ask "which threads have pending
  interrupts?" So you build the queue table anyway; the framework adds a second thing to keep in
  sync with it.
- LangGraph OSS ships no scheduler. You build the runner either way.

**Alternative if the team would rather not own the durability layer:** DBOS Transact 2.28.0
(MIT) — the same Postgres state machine, hardened, for the price of two decorators, with no
infrastructure beyond the Postgres you already need. **Confidence: medium** (smaller ecosystem).

**Also rejected:** Celery (adds a mandatory broker, still hand-write the state machine),
Temporal (correct but disproportionate; its determinism sandbox draws Activity boundaries that
fight NFR-04), Prefect (its `pause_flow_run` is non-durable and `suspend_flow_run` re-executes
the flow from the top; both are flow-level, so the gate cannot sit inside a stage), Dagster
(HITL gap open since 2023).

> Scale check, stated plainly: hundreds of documents, offline, batch, one approval gate. Every
> framework considered is built for problems two to four orders of magnitude larger.

**Keep LangGraph on the shelf.** If extraction later becomes genuinely agentic — open-ended tool
loops, dynamic re-planning — adopt it *inside* the extract stage as a library, never as the
pipeline owner.

---

## Decision 2 — The human gate does not block the pipeline

**Chosen:** conflict resolution is **detached** from the pipeline. The gate is a policy check at
compose time — a query, not an interrupt. **Confidence: high.**

Reasons, in order of force:

1. Composition is already a pure function of the store. Nothing needs resuming; you re-run it.
2. Blocking couples ingestion throughput to human availability. One unresolved conflict on
   document 7 must not stall documents 8–400.
3. **"Defer" is one of the five mandated resolution actions** (FR-HITL-04). A blocking workflow
   has no coherent semantics for indefinite deferral.
4. Abandonment stops being a failure mode. There is no stuck pipeline if nothing is waiting —
   only a workbook with a known-incomplete manifest.
5. The resolution row *is* the durable artifact (NFR-02). A checkpointed pause is a redundant
   second one.

`compose_workbook` runs against whatever is resolved now and emits a **completeness manifest**
listing every unresolved conflict with its id, field, severity and status. Composition is gated
only on unresolved conflicts above a severity threshold, and that gate is overridable by a
recorded, audited decision.

**Consequence for the scaffolding:** `orchestrator.INTERRUPTING_STAGES` contained
`AWAIT_HUMAN_RESOLUTION`. Under this decision that stage is not part of the pipeline at all, so
both were removed and replaced by `compose_gate_blocks()`. Recorded in
[analysis.md](analysis.md) A-3.

---

## Decision 3 — Single datastore: PostgreSQL 18 + pgvector 0.8.5

**Chosen:** one Postgres instance holds canonical records, chunks, embeddings, the conflict
queue and the audit log. No second database. **Confidence: high.**

The win is transactional: an extraction, its provenance, its confidence and the reviewer's
correction commit or roll back together. That property is worth more than any retrieval delta a
dedicated vector store would provide at this scale.

**Migration trigger (write this into the runbook):** move the vector leg to Qdrant if chunk
count exceeds ~5M, true sparse vectors become necessary, or p95 hybrid latency exceeds 300 ms.
`VectorStorePort` makes that a config change.

### Decision 3a — No ANN index. Exact search.

**Confidence: high.** This is the most counter-intuitive decision here and it is the best
evidenced.

pgvector's filtered search is a **post-filter by default, and the mitigation is off by default.**
Measured on 50,000 chunks with a 1% selective filter, `LIMIT 10`, HNSW index,
`hnsw.iterative_scan = off`:

```
Limit (actual rows=5.00 loops=1)          <-- asked for 10, got 5
  -> Index Scan using chunks_hnsw on chunks (actual rows=5.00)
       Filter: (tenant = 7)
       Rows Removed by Filter: 395
```

**It silently returned 5 rows for a top-10 request.** No error, no warning. With an ACL array on
a GIN index it returned **zero**.

Measured alternatives at our actual scale:

| Approach | Correct top-k? | Latency | Index size |
|---|---|---|---|
| **Exact scan + ACL filter** | **100% recall** | **3.5–5.5 ms** | **0** |
| HNSW + RLS, iterative on | approximate | 1.4–2.2 ms | 364 MB |
| HNSW + RLS, iterative off | **5 of 10** | 0.2 ms | 364 MB |
| HNSW + ACL array, iterative off | **0 of 10** | 0.7 ms | 364 MB |

At tens of thousands of vectors, exact search with an ACL predicate runs in ~4 ms with
guaranteed top-k, against an HNSW index that costs 364 MB — larger than the 202 MB table
itself — and silently under-returns. Worst case, an unfiltered scan, is 130–180 ms, trivial
beside the LLM call it feeds.

The strongest evidence: **left at defaults, the PG18 planner chose the sequential scan over the
HNSW index on its own.**

Required regardless: set `hnsw.iterative_scan = relaxed_order` in `postgresql.conf` now, so that
if anyone adds an ANN index later the default is not silently wrong. And if one is ever added,
note that **`ef_construction >= 2 * m` is a hard enforced constraint, not README guidance** —
pgvector raises an `ERROR` at index build. pgvector's own defaults (`m=16`,
`ef_construction=64`) satisfy it; a hand-tuned `m=48` with the default `ef_construction` would
not. And **add a regression test
asserting `len(results) == k`** on a filtered query — every failure mode above presents as a
short result set and nothing else catches it.

### Decision 3b — Hybrid retrieval without BM25

**Chosen:** `pgvector` dense + Postgres `tsvector`/GIN full-text + `pg_trgm` trigram, fused with
Reciprocal Rank Fusion (k=60), then reranked. **Confidence: high.**

BM25 is unavailable under our licence posture, and at 10k–100k chunks the ranking *function*
barely matters, because the cross-encoder reranker determines final order far better than either
BM25 or `ts_rank_cd` would. What matters from the lexical leg is **recall into the candidate
set**, which `tsvector` provides.

More importantly: for datasheets the retrieval failure that actually bites is
`"JKM610N-66HL4M-V"` not matching `"JKM610N 66HL4M V"`. **BM25 does not fix that. `pg_trgm`
does**, and it ships with Postgres under the same permissive licence. The licence constraint
pushes us toward the better answer here.

### Decision 3c — Access control via FORCE ROW LEVEL SECURITY

**Confidence: high.** RLS never leaked a row in any test — it is correct, and it is the right
defence-in-depth boundary. But note precisely what was measured: the RLS predicate appears as
`Filter:` on the Index Scan node, i.e. **applied after the ANN index produces candidates**. RLS
quals are not pushed into an ANN scan.

So: RLS is safe. RLS + ANN + default settings is not. Decision 3a removes the ANN index, which
removes the interaction entirely. The application connects as a **non-owner, non-superuser**
role with `FORCE ROW LEVEL SECURITY` enabled.

This satisfies NFR-03 and AC-8.

---

## Decision 4 — Parser router

**Chosen:** content-signature routing across a small set of engines, with a per-page audit loop.
**Confidence: high.**

| Input | Engine | Licence |
|---|---|---|
| `.xlsx`, `.csv` | **openpyxl / pandas directly** | permissive |
| `.docx` | **LibreOffice** headless `.docx`→PDF, then the PDF path (D-19) | MPL-2.0 |
| `.pdf`, text-layer page | **Docling 2.115.0** + TableFormer ACCURATE | MIT |
| `.pdf`, scanned page | **PaddleOCR-VL-1.6** on vLLM | Apache-2.0 |
| images | **PaddleOCR-VL-1.6** | Apache-2.0 |
| no GPU (degraded tier, flagged) | RapidOCR 3.9.2 + PP-StructureV3 | Apache-2.0 |

Hot-swap alternate for scanned pages: **GLM-OCR** (MIT). NFR-04 makes this a config change.

**Spreadsheets bypass document parsing entirely.** A document parser converts a typed,
formula-bearing grid into lossy markdown. `openpyxl`/`pandas` gives native types, merged-cell
ranges and sheet names. Docling *can* ingest xlsx; doing so discards information already in hand.

**Text-layer PDFs do not go to a VLM.** A datasheet with a text layer already contains exact
character data. A VLM re-transcribes it and can hallucinate digits — precisely the failure mode
that destroys numeric spec extraction. Use the text layer for *content*, TableFormer for
*structure*, and reserve VLMs for pages with no text layer.

**Three-tier fallback:**

1. **Per-page audit, always.** Flag pages with zero extracted characters, character count under
   10% of the pypdfium2 text count, or zero table cells where the layout model detected a
   table. Re-run those pages on the next engine.
2. **Dual-parse reconciliation on table-critical pages.** Run Docling/TableFormer *and*
   PaddleOCR-VL on the same table region, align cells, diff. Agreement raises confidence;
   disagreement marks the cell contested and **feeds the confidence model as a feature**. This
   converts redundant compute into a calibration signal, and at hundreds of documents the cost
   is irrelevant.
3. **Never silently drop.** A page failing all engines is recorded as `PARSE_FAILED` with its
   page reference, not omitted. Silent page loss is the worst failure mode in this class of
   pipeline.

**Do not take a dependency on `pdfmux`** despite it being MIT and doing exactly this. Its 1.8.7
release is a day old with heavy version churn, its headline benchmark is vendor-self-published,
and it pulls `anthropic` as a *core* dependency — awkward against NFR-03. Copy the design; it is
~200–300 lines and it is the core of our ingestion layer. Revisit in six months.

---

## Decision 5 — Embeddings and reranking

**Embedding: `Qwen/Qwen3-Embedding-4B`** (Apache-2.0), stored at **1024 dimensions** via
Matryoshka truncation and re-normalisation. **Confidence: high.**

The truncation is load-bearing, not an optimisation: pgvector's HNSW index caps at 2,000
dimensions for `vector`, so the native 2560 could not be indexed if we ever add one. 1024 also
halves storage for negligible recall loss at this scale. 32K context means a whole datasheet
table never overflows.

Alternative: `BAAI/bge-m3` (MIT, 1024 dims) — chosen not for score but because it emits dense,
learned-sparse and ColBERT multi-vector in one pass, giving a second lexical signal without
another service if Postgres FTS proves weak on part numbers.

**Reranker: `BAAI/bge-reranker-v2-m3`** (Apache-2.0, 568M) on TEI. **Confidence: high.**
Rerank top-50 → keep top-5–8. Highest-ROI component in the retrieval path, and it is what lets
us get away without BM25. *Latency figures circulating for this model are secondary-source with
unstated batch size — benchmark before setting an SLA.*

---

## Decision 6 — Chunking

**Confidence: high on tables, medium on overlap.** This revises FR-RAG-01's guidance.

- **Tables are never token-chunked.** Not "where feasible" — never. A Docling `TableItem` is the
  atomic unit. If one genuinely exceeds the embedder window (essentially never for a datasheet),
  split by row groups and **repeat header rows in every part**.
- **Index every table three times**, all pointing at one `table_id`:
  1. `table_full` — whole table, for generation-time context.
  2. `table_row` — each row serialised with column names inlined:
     `"Model: JKM610N-66HL4M-V | Pmax: 610 W | Voc: 41.24 V"`. **This is what makes "what is the
     Voc of module X" retrievable at all.** A bare `"41.24"` is unretrievable; table-level
     embeddings reliably fail row-level lookups because the specific value is averaged away.
  3. `table_summary` — 1–3 generated sentences naming the table's contents, headers and covered
     models. Highest-leverage single trick here, because query vocabulary
     ("temperature coefficient", "derating") frequently appears nowhere in the cells.
- **Overlap 0–10%, not 10–20%.** Systematic analysis found overlap gave no measurable benefit and
  only increased indexing cost. Docling supplies real section boundaries, which is most of what
  overlap was compensating for. Keep ~50 tokens only when splitting *within* a section; zero at
  section boundaries. *Medium confidence — worth an A/B on our own eval set.*
- **Prose: 512 tokens, split on structure first**, then packed — never at a fixed offset that
  ignores headings.
- **Add contextual retrieval.** Prepend 1–2 sentences of document/section context to each chunk
  *before embedding*. Reported ~67% reduction in retrieval failures when combined with reranking;
  the one-time cost at our volume is negligible.

---

## Decision 7 — Schema-constrained extraction and confidence

**Chosen:** vLLM native structured outputs (xgrammar) as the enforcement layer, Pydantic v2 as
the single source of truth, Instructor only as a thin validation/retry layer **in `json_schema`
mode**. **Confidence: high.**

The confidentiality constraint (NFR-03) forces self-hosted inference. That is not merely a
compliance fact — **it is the only reason genuine per-field confidence is achievable**, because
logprob access is increasingly restricted across hosted providers. We control the server, so we
get logprobs.

**But only on the right path.** Instructor's default tool-calling mode does **not** return
logprobs — tool/function calls do not emit them. Making `TOOLS` mode the primary path throws
away the confidence signal entirely. Use `json_schema` mode against the vLLM OpenAI-compatible
endpoint, with `logprobs` enabled.

Grammar-constrained decoding also means zero parse failures and zero retries.

### Confidence scoring — three things banned outright

| Method | ROC AUC | Verdict |
|---|---|---|
| Source grounding + parser/cross-read agreement + domain checks | 0.896 | **Build first** |
| Full multi-signal fusion + isotonic calibration | **0.928** | **Target state** |
| Token logprobs alone | 0.705 | Useful *feature*, useless *alone* |
| Self-consistency, N=5 | 0.744 | **Do not build** — 5× cost, only 6 distinct score values |
| **LLM self-reported confidence** | **0.692** | **Banned** — worse than logprobs and dangerously plausible-looking |

Feature groups for the fused classifier, strongest first:

- **Source grounding** — does the value appear verbatim in the parsed source? Fuzzy match ratio;
  parser/OCR confidence for the region; how many competing values sit nearby. *Strongest group;
  OCR/quality features alone beat logprobs+entropy.*
- **Cross-read disagreement** — run two structurally asymmetric extractions (field-guided
  "extract field X" and document-guided "list every spec present") and score agreement. Their
  failure modes are opposite: field-guided confabulates absent fields, document-guided misses
  non-salient ones. Plus the parser disagreement from Decision 4.
- **Decoding uncertainty** — logprob and entropy statistics **over the value span only**.
  Structural JSON tokens are grammar-forced and saturate above 0.999, which is exactly why naive
  logprob-mean fails.
- **Domain plausibility** — `Voc > Vmp`, `Isc > Imp`, `Pmax ≈ Vmp × Imp ± 1%`, c-Si efficiency
  ≤ 27%, power temperature coefficient negative, contract line total = qty × unit price. Cheap,
  high-precision, and used as **both a feature and a hard gate**.
- **Field type** — calibration differs sharply; numeric calibrates well, free text is
  overconfident at high probabilities.

Fuse with gradient boosting, calibrate with isotonic regression.

**Cold start** (there are no labels on day one): ship a rule-based score from grounding, parser
agreement, cross-read agreement and plausibility; route conservatively; log every human
correction as a label — the audit log already has the schema. Fit calibration at ~150–300
reviewed instances per field type; fit the full classifier at ~1–2k. Deliberately seed the
review pool with hard documents; negative-sample quality dominates dataset size.

---

## Decision 8 — Excel generation

**Chosen: keep openpyxl, pinned exactly at `==3.1.5`.** **Confidence: high.**

At our real shape (8 tabs × 60 params × 12 suppliers = 5,760 value cells) the full 13-tab
workbook builds and saves in **0.10 s at 116 KB** — measured. Performance arguments for
xlsxwriter are worth ~0.5 s and are irrelevant.

The decisive factor runs the other way: **xlsxwriter is write-only.** AC-4 ("every cell resolves
to a source") is exactly the invariant that needs a test asserting *on the generated artifact*.
With openpyxl you `load_workbook()` and assert; with xlsxwriter you would add openpyxl as a test
dependency anyway and maintain two Excel libraries to gain nothing. Rust-backed options
(`python-calamine` read-only, `fastxlsx` no formatting) are disqualified, not merely inferior.

The exact pin is non-negotiable: `docProps/app.xml` literally embeds
`Openpyxl 3.1.5`, so a patch bump silently changes the output hash.

### Decision 8a — Provenance lives in hidden parallel columns

**Confidence: high.** Only cell *content* is moved by a sort. Everything keyed by cell address
is not.

This disqualifies the option the TRS offered first: **a provenance tab keyed by
`PV Modules!D17` is silently wrong the moment anyone sorts.** The values move; the key does not.
It is the most dangerous choice available because it fails invisibly and still looks
authoritative.

Layout per comparison tab:

```
A          B..M              N..Y  (hidden)
Parameter  Supplier 1..12    _state1.._state12
                             "web,low|CLM-00012003|ds_3.pdf#p47|0.62"
```

Two constraints, both load-bearing and both silent when violated:

1. **No blank column between the value block and the state block** — a gap makes Excel treat
   them as separate regions, and a sort moves one without the other.
2. **`ws.auto_filter.ref` must span the hidden state columns** (`A1:Y61`, not `A1:M61`). Hidden
   columns *inside* the sorted range move with it; columns outside it do not.

Make both assertions in the generator and regression tests. This is the highest-risk detail in
the build.

Cell comments are **decorative only** — hover-to-see-source is good UX, so attach them to
flagged cells, but nothing may ever read provenance back out of them. They are address-keyed,
openpyxl loses their formatting and dimensions on round-trip, threaded comments are unsupported,
and openpyxl hardcodes both `MoveWithCells` and `SizeWithCells` with no option to change it.
Applying them to every cell costs 4.1× file size; flagged-only costs ~120 per tab.

Use `=HYPERLINK(...)` **formulas**, never `cell.hyperlink` — same address-key problem.

### Decision 8b — Three orthogonal visual channels

**Confidence: high.** A cell has one background, and FR-OUT-04's states co-occur. The naive
four-fills scheme also fails greyscale outright — measured, the conflict and web swatches differ
by **2 points of grey out of 255**, and adjacent swatches need ≈12 to be distinguishable on a
mono laser printer.

| Axis | Channel | State | Encoding |
|---|---|---|---|
| Origin | **Fill** | web-supplemented | `FFBDD7EE` fill, `FF1F3864` font |
| | | missing | `FFD9D9D9` fill, `FF595959` italic, literal `n/a` |
| Confidence | **Font** | low-confidence | `FF7F6000` **italic** |
| Conflict | **Border** | unresolved | `medium` left+right `FFC00000`, `FF9C0006` bold font |

Composition is then automatic: web + low = blue fill *and* brown italic; conflict + web + low =
all three. Three channels, not 2⁴ styles.

Greyscale and colour-blind safety come from no state depending on fill *alone*: conflict is a
border (shape), low-confidence is italic (shape), missing is literal text. The one weak link,
web-supplemented, is closed with a printable glyph via `numFmt` inside the `dxf` —
`formatCode='0.0" ᵂ"'` — verified to work in openpyxl. Measured contrast: 5.71 / 7.81 / 4.76 /
4.96, all above the WCAG AA 4.5 threshold. Hues sit on the blue/orange/neutral axis, so
deuteranopia and protanopia retain the distinction.

Always pass 8-digit ARGB — openpyxl serialises `"BDD7EE"` as `rgb="00BDD7EE"` (zero alpha).

Rejected: icon sets and data bars. Both are value-driven, not state-driven — they cannot express
"this figure came from the web", and data bars actively mislead by implying magnitude comparison
across parameters with different units.

### Decision 8c — Determinism, and what it can and cannot prove

**Confidence: high.**

openpyxl's XML output is *already* deterministic. There is no `sharedStrings.xml` (it writes
inline strings), no `calcChain.xml`, no UUIDs. `PYTHONHASHSEED` is not a factor. Float
serialisation via `"%.16g"` is deterministic. The problem reduces to two timestamps and ZIP
container metadata.

**The trap:** `save_workbook()` sets `workbook.properties.modified = now()` **unconditionally,
after** you set it. Setting it pre-save and normalising all zip mtimes still produced files
differing by one second. Two verified fixes exist; **drive `ExcelWriter` directly** to bypass
`save_workbook`, which is cleaner than post-hoc rewriting `core.xml`.

Then normalise the archive: fixed `date_time` of **1980-01-01 12:00** (not midnight — DOS
timestamps are *local*, and midnight underflows the 1980 floor in negative UTC offsets),
`create_system = 3` (always "Unix"), `external_attr = 0o644 << 16`, empty `extra`, sorted entries
with `[Content_Types].xml` forced first.

Non-obvious and verified: **`ZipFile(compresslevel=N)` is silently ignored** when you pass a
hand-built `ZipInfo` to `writestr()`. You must set `zi._compresslevel`. Anyone who thinks they
pinned compression via the constructor has not.

Validation: 9 permutations of `PYTHONHASHSEED` × `TZ` produced **one unique hash**.

**But hash the canonical projection, not the workbook.** The xlsx is lossy, so its hash cannot
prove what AC-7 wants:

```
0.1+0.2  ->  0aa4889a1d362431409e
0.3      ->  0aa4889a1d362431409e     COLLIDE
```

`%.16g` maps two **distinct** float64 values to identical bytes. The store can hold two different
numbers that produce an identical workbook hash — a real, if narrow, integrity gap for a
document full of prices, and unfixable at the xlsx layer.

So: the **artifact of record** is a canonical sorted-key JSON projection with floats via `repr()`
so float64 round-trips exactly. The deterministic xlsx render is kept anyway — workbooks become
diffable and cache-stable, and asserting its hash in CI is a **renderer-regression test** that
loudly catches an accidental openpyxl upgrade. Store both hashes.

> **Unverified and blocking for this decision:** the normalised file was validated by openpyxl
> round-trip and OPC structural checks only — **it was never opened in real Excel or
> LibreOffice.** Test `[Content_Types].xml`-first ordering and the 1980 timestamps in desktop
> Excel before this ships. Tracked in [tasks.md](tasks.md) as a gating task.

---

## Decision 9 — Immutable audit log

**Chosen:** privilege separation as the boundary, hash chaining per document stream, trigger as a
secondary tripwire. **Confidence: high.**

Measured attack matrix:

| Attack | Trigger | Privilege separation | RULE |
|---|---|---|---|
| `UPDATE`/`DELETE` by app | blocked | blocked | **silently discarded** |
| `TRUNCATE` | blocked | blocked | not covered |
| `ALTER TABLE … DISABLE TRIGGER` | **bypassed** | blocked | n/a |
| `session_replication_role='replica'` | **bypassed** | blocked | bypassed |
| superuser | bypassed | bypassed | bypassed |

Three corrections worth carrying:

- `BEFORE TRUNCATE` **statement-level** triggers do exist and do block TRUNCATE. (Row-level
  TRUNCATE triggers are what don't exist.)
- **`session_replication_role = 'replica'` is the bypass people forget** — it disabled the
  trigger and the DELETE succeeded, with no DDL trace and no `ALTER TABLE`.
- **RULEs are the worst option.** `ON UPDATE DO INSTEAD NOTHING` makes the UPDATE *report
  success* while discarding it. Silent is strictly worse than loud for an audit log.
- **RLS is the wrong tool for immutability** — it restricts *visibility*; immutability is a
  *privilege* problem. A superuser bypasses even `FORCE RLS`.

So: own `audit.*` with a `NOLOGIN` role the application never assumes, grant the app
`INSERT, SELECT` only. Add the trigger too — it is free and catches a mis-grant — but do not
count it as the boundary. **Accept that a superuser always wins**; the answer to that threat
model is shipping the log *out* (logical replication to a WORM sink), not more triggers.

**Hash chaining: yes.** Measured 5,000 chained appends in 45 ms, full re-verification in 3.6 ms.
It is the only mechanism that survives the superuser bypass — a superuser can edit a row but
cannot make the chain re-verify.

**The concurrency trap, reproduced:** a naive chain trigger under 8 concurrent writers produced
**160 rows with 118 distinct `prev_hash` — 42 silent forks.** Fixes in the order tested:
`UNIQUE(stream, prev_hash)` turns silent forks into loud errors (necessary, insufficient); an
advisory lock *inside the trigger* **does not work** (the statement snapshot is taken before the
trigger acquires the lock, so the waiter still reads a stale tip); an advisory lock **as its own
statement before the INSERT** works — 0 forks.

Chain **per document** (`stream = 'doc:1234'`), not globally, so cross-document concurrency stays
unconstrained. Canonicalise payloads in Python (RFC 8785), not SQL: `jsonb` normalises key order
but **preserves numeric formatting**, so `1.0` and `1.00` stay textually distinct.

**Write audit entries in the same transaction as the business write.** Rollback erasing the audit
record is *correct*: if the extraction rolled back, it did not happen, and logging that it did
would be a false record. The genuine "we attempted X and it failed" case is a different event
class, logged from the exception handler in a new transaction. Do **not** use the dblink
autonomous-transaction trick — it re-introduces exactly the failure it is meant to avoid.

---

## Decision 10 — The eight port Protocols are synchronous

**Chosen:** all eight stay `def`, not `async def`. **Confidence: medium-high.** Recorded here so it
stops being re-litigated. The original NFR-04 set was the first six; D-25 and P2-C4 added
`LexicalSearchPort` and `WebSearchPort` without changing this decision's pattern.

Concurrency in this system is **per-process, not per-coroutine**. Decision 1 makes the runner a
Postgres job table with a `SELECT … FOR UPDATE SKIP LOCKED` worker loop — that pattern *is* the
concurrency mechanism, and scaling it means more worker processes, which sidesteps the GIL
entirely.

Taking each Protocol in turn:

- **ParserPort, OCRPort** are CPU-bound in-process (Docling, TableFormer, tokenisation).
  Coroutines buy nothing; an async facade that immediately dispatches to a process pool is
  ceremony, not concurrency.
- **EmbedderPort, RerankerPort** already take **batches**. One request of 64 texts beats 64
  concurrent requests of 1 against a batching GPU server — the parallelism lives inside the
  payload, not at the call boundary.
- **VectorStorePort** is a local Postgres round-trip under Decision 3a (exact search, no ANN).
- **LexicalSearchPort** is the same local Postgres round-trip on the other retrieval leg
  (tsvector / pg_trgm, Decision 3b / D-25). Async buys nothing a second connection in another
  process does not already give.
- **LLMPort** is one genuinely I/O-bound port, and a `ThreadPoolExecutor` gives real overlap
  because the GIL is released across socket reads.
- **WebSearchPort** is the other I/O-bound port (D-26 / P2-C4): producer HTTP with a quota.
  Same ThreadPool treatment as LLMPort; the rate limit lives in
  `Settings.web_search_rate_limit_per_minute`, not in an async semaphore.

Sync forecloses nothing: a caller can drive any of these concurrently without touching the
interface, and because Protocols are **structural**, an `AsyncLLMPort` can be added *alongside*
later — additive, not a breaking change across eight interfaces.

The cost of the alternative is concrete. `async def` would propagate through
`services/ingestion.ingest`, `indexing.index_document`, `retrieval.retrieve`, the worker loop and
the CLI; psycopg3's sync and async APIs are separate class hierarchies, so it would pull the whole
data layer async — including the RLS `SET LOCAL` of Decision 3c and the
advisory-lock-then-INSERT sequence of Decision 9, which only came out right after measuring 42
silent chain forks. It would also make every frozen fixture an async fake, undermining the
decoupling strategy in tasks.md.

Scale check: hundreds of documents, batch/offline, in-flight I/O concurrency in the *tens*.
Threads and coroutines are indistinguishable there; threads are simpler.

**The real gap this decision exposes** is that there are no concurrency limits anywhere. Added to
`config.Settings` as `max_concurrent_parse`, `max_concurrent_llm` and
`web_search_rate_limit_per_minute` — orthogonal to interface shape, and the thing actually worth
specifying.

---

## Revised dependency set

```toml
dependencies = [
    "pydantic>=2.9",
    "pydantic-settings>=2.5",
    "openpyxl==3.1.5",        # exact — version string is embedded in output
    "psycopg[binary]>=3.2",
    "pgvector>=0.3",
]

[project.optional-dependencies]
parse  = ["docling>=2.115", "pandas>=2.2", "pymupdf>=1.24"]
extract = ["instructor>=1.15", "openai>=1.50"]   # json_schema mode against vLLM
solar  = ["pvlib>=0.11"]
dev    = ["pytest>=8.3", "ruff>=0.7", "mypy>=1.11", "testcontainers>=4.8"]
```

`langgraph` is removed per Decision 1. The `agent` extra becomes `extract`.

---

## Open items carried into implementation

Items the research could not settle, each assigned in [tasks.md](tasks.md):

1. **The normalised xlsx has never been opened in desktop Excel.** Gating.
2. **Iterative-scan recall under RLS** — the row *count* was verified, the true nearest neighbours
   were not. Moot while Decision 3a holds, but it gates any future ANN adoption.
3. **No public benchmark exists for PV/inverter/BESS datasheet extraction.** Every accuracy number
   here is extrapolated from invoice and general-document benchmarks. Building a 30–50 document
   gold set is the highest-value artifact of week one.
4. **Reranker latency** figures are secondary-source with unstated batch size.
5. **`.set_node_defaults()`** appears in a LangGraph blog but not the API reference. Moot under
   Decision 1.
