# Cross-artifact analysis

**Date:** 2026-07-28
**Artifacts checked:** [spec.md](spec.md) · [plan.md](plan.md) · [clarifications.md](clarifications.md) ·
[contracts/canonical-parameters.md](contracts/canonical-parameters.md) · the existing scaffolding
in `src/procurement_agent/` · `pyproject.toml` · `docs/requirements-traceability.md` ·
`docs/open-questions.md`

Findings are ranked by severity. **C = critical** (will produce wrong behaviour or blocks
parallel work), **H = high** (contradiction between artifacts), **M = medium** (drift or
duplication).

---

## Summary

| ID | Severity | Finding | Status |
|---|---|---|---|
| A-1 | **C** | `CanonicalField` has no `condition` — the schema cannot represent the distinction that D-1 shows causes most false conflicts | **Fixed** |
| A-2 | **C** | `pyproject.toml` depends on `langgraph`, which plan Decision 1 rejects | **Fixed** |
| A-3 | **C** | `INTERRUPTING_STAGES` contains a stage that Decision 2 removes from the pipeline | **Fixed** |
| A-4 | **H** | `numeric_conflict_tolerance` is a single global float; D-2 requires a per-field table with three kinds | **Fixed** |
| A-5 | **H** | `hitl_confidence_threshold: 0.80` is the wrong shape per D-3 | **Fixed** |
| A-6 | **H** | `openpyxl>=3.1` is unpinned; Decision 8 requires `==3.1.5` exactly | **Fixed** |
| A-7 | **H** | `chunk_overlap_ratio = 0.15` is outside the 0–10% band Decision 6 sets | **Fixed** |
| A-8 | **H** | spec.md FR-RAG-01 says tables kept whole *"where feasible"*; Decision 6 says never token-chunk | **Resolved in plan** |
| A-9 | **H** | Two research streams gave *different* determinism fixes and *different* epochs | **Resolved** |
| A-10 | **M** | `canonical-parameters.md` predates D-1 and has no `condition` column | **Fixed** |
| A-11 | **M** | `flags_for()` takes one confidence threshold; D-3 tiers τ by field criticality | **Deferred** |
| A-12 | **M** | `docs/open-questions.md` is now largely superseded by `clarifications.md` | **Fixed** |
| A-13 | **M** | `requirements-traceability.md` marks FR-WEB-04 "open"; D-2 resolves it | **Fixed** |
| A-14 | **M** | `pvlib` extra is misleading without the D-8a caveat | **Fixed** |

---

## A-1 (Critical) — The canonical field cannot express `condition`

**Artifacts:** `src/procurement_agent/schema/field.py` vs [clarifications.md D-1](clarifications.md)

`CanonicalField` carries `value`, `unit` and `verbatim_value`. It has no way to record *under
what conditions* a value holds.

The research shows this is not a nicety. For the Sungrow SG350HX, the EU datasheet, the US
datasheet and the CEC listing produce **four apparent conflicts and zero real ones** — every one
is a condition mismatch (30 °C vs 40 °C, full-power vs full MPPT window, CEC vs European
weighting). The same pattern holds for PV (STC vs NOCT) and BESS (AC vs DC side, BOL vs EOL).

Without `condition`, the conflict engine generates spurious items at a rate that trains reviewers
to ignore the queue — which defeats the tool's entire premise (C-3).

**Note this contradicts the TRS**, which fixes the field object at eight keys in section 5. The
addition is justified: the TRS's own section 7 lists parameters like
*"rated AC kVA @temp"* and *"STC/NMOT ratings"* that cannot be represented without it. The eight
TRS keys are preserved; `condition` is a ninth.

**Fixed** — `condition` added to `CanonicalField`, and a `test_canonical_field_has_the_eight_spec_keys`
test updated to assert nine with a comment explaining the deviation.

---

## A-2 (Critical) — Dependency contradicts the orchestration decision

**Artifacts:** `pyproject.toml` vs [plan.md Decision 1](plan.md)

The `agent` extra declares `langgraph>=0.2`. Decision 1 rejects LangGraph as the pipeline
orchestrator on the grounds that NFR-02 and FR-OUT-06 already force canonical state into
application tables, making a checkpointer a second copy of state we already own.

Leaving the dependency in place would have a team reach for it.

**Fixed** — `agent` extra renamed `extract`, `langgraph` removed, `instructor` + `openai` added
for the vLLM `json_schema` path per Decision 7.

---

## A-3 (Critical) — Pipeline contains a stage the design removes

**Artifacts:** `src/procurement_agent/orchestrator/__init__.py` vs [plan.md Decision 2](plan.md)

`Stage.AWAIT_HUMAN_RESOLUTION` exists and sits in `INTERRUPTING_STAGES`. Decision 2 makes the
human gate a **policy check at compose time**, not a pipeline pause — because "defer" is a
mandated resolution action (FR-HITL-04) and a blocking workflow has no coherent semantics for
indefinite deferral.

**Fixed** — the stage is removed and `INTERRUPTING_STAGES` is replaced by a documented
`compose_gate` concept.

---

## A-4 (High) — Global tolerance float versus per-field table

**Artifacts:** `src/procurement_agent/config.py` vs [clarifications.md D-2](clarifications.md)

`numeric_conflict_tolerance: 0.02` applies one relative band to every field. Measured data shows
this is wrong in both directions: a ±2% band on a 650 W nameplate is ±13 W, which **merges three
adjacent 5 W SKUs**; the same band on a −0.29 %/°C temperature coefficient is ±0.006, **below
datasheet precision**.

**Fixed** — replaced with a reference to the per-field table, and the placeholder float removed
so nobody uses it by accident.

---

## A-5 (High) — Confidence threshold is the wrong shape

**Artifacts:** `src/procurement_agent/config.py` vs [clarifications.md D-3](clarifications.md)

`hitl_confidence_threshold: 0.80` presumes a single calibrated score exists on day one. It does
not, and a hardcoded float is not derived from anything. D-3 specifies a target precision on
auto-accepted fields with τ read off a risk–coverage curve, tiered by field criticality.

**Fixed** — replaced with `target_precision_auto_accepted: 0.99` and
`review_budget_fraction: 0.20`, both of which are *targets* the calibration fits to.

---

## A-6 (High) — `openpyxl` must be pinned exactly

**Artifacts:** `pyproject.toml` vs [plan.md Decision 8c](plan.md)

`docProps/app.xml` literally embeds the string `Openpyxl 3.1.5`. A patch bump silently changes
the output hash and breaks every historical AC-7 assertion with zero data change. `>=3.1` permits
exactly that.

**Fixed** — pinned to `==3.1.5`.

---

## A-7 (High) — Chunk overlap outside the researched band

**Artifacts:** `src/procurement_agent/config.py` vs [plan.md Decision 6](plan.md)

`chunk_overlap_ratio = 0.15` sits inside the TRS's 10–20% guidance but outside Decision 6's
revised 0–10%. Systematic analysis found overlap gave no measurable benefit; Docling supplies
real section boundaries, which is most of what overlap compensated for.

**Fixed** — default 0.05, `le` bound tightened to 0.10 so the out-of-band value cannot be set.

---

## A-8 (High) — "Tables kept whole where feasible" is too weak

**Artifacts:** [spec.md](spec.md) FR-RAG-01 vs [plan.md](plan.md) Decision 6

spec.md carries the TRS wording verbatim. Decision 6 hardens it: tables are **never**
token-chunked, and each is indexed three ways (`table_full`, `table_row`, `table_summary`).

**Resolved, deliberately not "fixed".** spec.md states the *requirement* as the source documents
express it; plan.md states *how it is met*, and may be stricter. Recording the escalation here
rather than editing the requirement keeps spec.md faithful to the FRD/TRS. Flagged so no
implementer reads "where feasible" as permission.

---

## A-9 (High) — Two research streams disagreed on the determinism fix

**Artifacts:** the Excel research stream vs the orchestration research stream

Both independently found the same root cause — `save_workbook()` re-stamps
`properties.modified = now()` unconditionally *after* the caller sets it — and both verified
byte-identical output. But they proposed **different fixes** and **different epochs**:

| | Excel stream | Orchestration stream |
|---|---|---|
| Fix | Post-save regex rewrite of `docProps/core.xml` | Drive `ExcelWriter` directly, bypassing `save_workbook` |
| Epoch | `(1980, 1, 1, 0, 0, 0)` | `1980-01-01 **12:00**` |
| Entry order | Preserve `namelist()` order | Sort, then force `[Content_Types].xml` first |

**Resolution — take the orchestration stream on all three**, recorded in plan.md Decision 8c:

- **`ExcelWriter` direct** over regex-rewriting XML. Both work, but parsing generated XML with a
  regex to undo a library's own write is the more fragile of two verified options.
- **12:00, not midnight.** The orchestration stream gave a reason the Excel stream did not: DOS
  timestamps are *local*, so midnight underflows the 1980 floor in negative UTC offsets. This is
  also what `strip-nondeterminism` does, which is independent corroboration.
- **Entry ordering** is a genuine toss-up; sorted-with-`[Content_Types]`-first matches the OPC
  preference, so it is marginally safer against strict readers.

This disagreement is worth recording rather than silently picking a winner — if the chosen
approach fails in desktop Excel, the other is a verified fallback.

---

## A-10 (Medium) — Parameter contract predates the `condition` decision

**Artifacts:** [contracts/canonical-parameters.md](contracts/canonical-parameters.md) vs D-1

The contract was frozen before D-1 landed and specifies only key/type/unit. Several of its own
entries already imply a condition — `rated_ac_power_temp`, `stc_rating` vs `nmot_rating`,
`usable_energy_per_container` vs `nameplate_energy_per_container`.

**Fixed** — a `condition` column and a section explaining which parameter families require one.
Note this makes the earlier ad-hoc `rated_ac_power_temp` field redundant with the general
mechanism; it is retained as a convenience but documented as derivable.

---

## A-11 (Medium) — Output flagging assumes one global threshold

**Artifacts:** `src/procurement_agent/services/output/flags_for()` vs D-3

`flags_for(field, confidence_threshold=...)` takes a single float. D-3 tiers τ by field
criticality, so the low-confidence flag should be computed against the *field's own* threshold.

**Deferred, not fixed.** The signature change is cheap, but the tiering policy it depends on is
itself an output of the calibration work in WP-B. Changing the signature now would freeze a
guess. Tracked as a task dependency instead: WP-G takes a `threshold_for(field_name)` callable
once WP-B defines it.

---

## A-12 (Medium) — `docs/open-questions.md` is now superseded

Written during scaffolding, before any research. Eight of its twelve items are resolved in
`clarifications.md`; leaving both invites a team to work from the stale one.

**Fixed** — reduced to a pointer at `clarifications.md`, retaining only the items that remain
genuinely open.

---

## A-13 (Medium) — Traceability marks resolved requirements as open

`docs/requirements-traceability.md` lists FR-WEB-04 as **open** and FR-OUT-05 as **open**. D-2
resolves the first; the second is now covered by the certification fields in the parameter
contract.

**Fixed** — statuses updated, with links to the deciding clarification.

---

## A-14 (Medium) — The `pvlib` extra is a trap without its caveat

`solar = ["pvlib>=0.11"]` reads as an endorsement. D-8a measured that pvlib's bundled CEC library
is frozen at March 2019 and contains **zero modules ≥600 W**, while this project's modules are
600–740 W. `retrieve_sam('CECMod')` would return a library with not one candidate product, and
pvlib's own name normaliser is currently broken against SAM's curly-brace format.

**Fixed** — comment added at the dependency pointing at D-8a.

---

---

# Round 2 — findings from open issues #1–#9 (2026-07-28)

Nine issues were reviewed against these artifacts. Most were already answered by the spec; four
found real defects, **three of them regressions introduced by round 1**.

| ID | Severity | Finding | Status |
|---|---|---|---|
| A-15 | **C** | README still documented the interrupt architecture and a `--extra agent` that no longer exists — while A-2/A-3 above claimed "Fixed" | **Fixed** |
| A-16 | **H** | `requirements-traceability.md` cited three symbols deleted in round 1, and marked eight rows `enforced` with no test covering the cited artifact | **Fixed** |
| A-17 | **H** | PV power tolerance modelled as `(min, max)` in watts cannot represent a relative declared band | **Fixed** |
| A-18 | **M** | `ports/__init__.py` documented an `adapters/` package that does not exist | **Fixed** |
| A-19 | **M** | No canonical row ordering across component instances; `ComponentInstance` had no ID | **Fixed** |
| A-20 | **M** | Sync vs async on the six Protocols was never decided | **Fixed** — plan Decision 10 |
| A-21 | **M** | No concurrency limits anywhere in config | **Fixed** |
| A-22 | **M** | C8 lacked the append-only claim invariant | **Fixed** |
| A-23 | **H** | **FR-RAG-02's ANN mandate was edited out of spec.md rather than registered.** The TRS says every chunk *shall* be stored in an ANN vector index (HNSW/IVF, cosine); plan Decision 3a reverses it. The decision is well-supported — pgvector's filtered search was measured silently under-returning on a filtered top-k, and an HNSW index cost more than the table — but a mandatory `shall` was paraphrased away instead of recorded here. | **Fixed** — TRS wording restored in spec.md, reversal marked inline |
| A-24 | **H** | **FR-RAG-03's BM25 clause was generalised to "keyword search".** Reversed by plan Decision 3b (no permissively licensed true-BM25 for PostgreSQL), also unregistered. | **Fixed** — TRS wording restored, reversal marked inline. The inline note's *description of the replacement* was itself wrong; that is a separate finding, [A-40](#a-40-low--a-24s-inline-note-named-the-wrong-replacement). |
| A-25 | **M** | **NFR-03's mechanism clause was dropped.** The TRS says access control *must* be enforced at retrieval time *via metadata filtering*; plan Decision 3c substitutes `FORCE ROW LEVEL SECURITY`. Defensible, but an unflagged change to a `must`. | **Fixed** — mechanism restored, substitution marked inline |
| A-26 | **H** | **AC-6 was weakened on the exact point the TRS calls its key correction.** "TRD against the correct IEEE 2800 voltage-class limit" became "harmonic distortion against the correct voltage-class limit", and spec.md mentioned TRD, TDD, IEEE 2800 and IEEE 519 zero times — while the frozen contract warns at length that getting TRD-vs-TDD wrong "produces a compliance matrix that passes suppliers it should fail", and tasks.md still said "AC-6 TRD". The one normative document generalised the distinction away. | **Fixed** |
| A-27 | **M** | **The coverage audit certified itself with the wrong count.** "All 26 FR IDs in spec.md match the TRS analysis" — spec.md contains **32** unique FR IDs (FR-ING-01..10, FR-RAG-01..05, FR-WEB-01..05, FR-HITL-01..06, FR-OUT-01..06); 26 is the count with FR-OUT-01..06 omitted. A full ID sweep found none missing and none invented, so only the arithmetic was wrong — but the claim that closes the audit was false, which left the audit unverified. | **Fixed** |

## A-15 (Critical) — the analysis claimed a fix that had not fully landed

The most serious finding of this round, because it is a defect *in this document*. A-2 and A-3
were marked **Fixed** on the strength of edits to `pyproject.toml` and `orchestrator/__init__.py`.
`README.md` was not checked, and still contained:

- `--extra agent` (LangGraph, Instructor) — an extra that no longer exists, so the documented
  install command would fail
- "Pipeline stages and interrupt policy" in the layout section
- a pipeline diagram showing `[interrupt: human approval]`

**Lesson worth recording:** "Fixed" was asserted from the edits made rather than from a search for
the symbol across the repository. A grep for the removed names would have caught all three.

## A-16 (High) — traceability overstated test coverage

Eleven rows corrected. Three cited deleted symbols — a round-1 regression: symbols were removed
without updating the document that referenced them. Eight claimed `enforced` while no test touched
the cited artifact; **no test imports `procurement_agent.ports` at all**, yet NFR-04 was marked
`enforced` with `ports/` as its entire citation.

A `partial` status was added, because several rows were genuinely half-covered and forcing them to
`enforced` or `declared` lost information.

## A-17 (High) — declared bands are not conflict tolerances

Verified from primary datasheet text that three power-tolerance conventions are in current use:
`0~+5 W` (Trina), `0~+10 W` (Canadian Solar, three sheets), and `0~+3%` (Jinko).

D-2's existing rebuttal — that Trina's `±3%` is a *measuring* tolerance rather than a label band —
is correct for Trina but **does not dispose of Jinko**, whose `0~+3%` sits in the electrical-data
table as the label tolerance itself. The rebuttal answered a coincidentally-equal number from a
different manufacturer and a different row.

Storing a relative band in watts is circular precisely where it matters: Pmax is the field most
likely to be disputed, and resolving a Pmax conflict would need a tolerance whose value depends on
which Pmax candidate was already chosen. Contract changed to a `DeclaredBand` with an explicit
`kind`.

## A-19 (Medium) — ordering, and why the obvious key is wrong

AC-7 needs a total order over component instances; "sorted-key JSON" orders keys *within* objects
only. The natural key `(category, manufacturer, model, field)` is **not unique** — 36 duplicated
`(Manufacturer, Model Number)` pairs and 157 model numbers appearing under more than one
manufacturer. A `surrogate_id` tie-break is required, placed **last** so row order stays readable.

## Relationship to PR #11

A second PR (`docs/agent-topology.md`, `docs/defaults.md`) was opened independently, 9 seconds
after this one, from the same parent commit. Neither references the other. It **merges cleanly at
the git level** — verified in an isolated clone, no conflict markers — but **merging both unedited
produces a self-contradicting repository.**

**It does not contradict our reading of the source documents.** It quotes our
`services/__init__.py` conclusion — that the TRS names services, not agents, with no roster or
message protocol — and calls it accurate. It then asks a different question: given that, *should*
there be an agent topology? Its answer is notably anti-inflationary: a genuine team of
differentiated agents earns its keep in exactly two places (the parser router, and the
compliance/tax tabs, which have disjoint authorities and no shared state). Everywhere else,
*"'agent' would be a persona wrapped around a function call."*

**Where it conflicts, and who is better evidenced:**

| Topic | PR #11 | This PR | Assessment |
|---|---|---|---|
| Orchestrator | LangGraph + Postgres checkpointer | Postgres state machine | **Ours.** PR #11 states LangGraph as a premise inherited from the scaffolding and never argues for it — its own confidence note covers the throughput figures and a config-key fact, not the choice. It offers no counter to Decision 1's two load-bearing arguments. |
| Vector index | HNSW with tuned parameters | No ANN index | **Ours.** Measured on a live container with the query plan reproduced. PR #11 never considers filtered-search recall or the RLS interaction. |
| Chunk overlap | 15% "(unchanged)" | 0–10% | **Ours**, though ours is self-marked medium confidence. Mechanically, 0.15 now raises a Pydantic validation error — merging both ships a doc unimplementable against code in the same commit. |
| Traceability statuses | Downgrades five rows, adds `partial` | Left them `enforced` | **Theirs, conceded without argument.** Verified: no test touches the cited symbols. Adopted as A-16. |
| Identity resolution | No non-exact merge at all | Scored auto-merge at ≥0.90 | **Split.** Ours is measurement-backed and requires electrical corroboration, so it is not fuzzy string matching — but their conservatism argument ("a wrong merge either fabricates a conflict or hides one, and neither is visible afterwards") is real and our D-4 does not rebut it. |

**Adopted from PR #11 this round:** the propose/commit invariant (C8), `declared_tolerance`
(A-17), the canonical sort key (A-19), the traceability corrections (A-16), the
`ports/__init__.py` docstring fix (A-18), union-not-vote (E.3a), OIDC subject claim (D-12a),
technology-specific bifaciality bands, and the `ef_construction >= 2*m` hard-error note.

**Also adopted: a licence-verification warning.** PR #11 demonstrated that the widely repeated
"CC BY-NC 4.0" label for Jina v4 is wrong — it is the Qwen Research License. Our v5 entry comes
from the same class of secondary source. The rejection is unaffected; the licence *name* is what
would reach procurement paperwork.

**Recommended sequence:** merge this PR first (it changes code, config and contracts), rebase
PR #11's docs onto it, keep `agent-topology.md` with three retargeting edits, and do **not** land
`defaults.md` as a peer of `clarifications.md` — two authoritative defaults documents is the
failure mode. Fold its non-overlapping parts in instead.

## Issues that needed no change

- **#1** (per-field tolerance) — already D-2, which argues from a downloaded CEC export rather
  than search snippets. Its proposed 0.5% relative on nameplate is *weaker* than D-2's ±1 W.
- **#3** (fuse deterministic signals) — already plan Decision 7, reached independently from the
  same evidence, with identical AUC figures. One real gap closed: a Tier A never-auto-accept class.
- **#4** (LangGraph `max_concurrency`) — moot under Decision 1, but the technical claim was
  verified and is worth knowing: it is a top-level `RunnableConfig` key, and nesting it under
  `configurable` disables the cap silently because `ensure_config()` sweeps unrecognised top-level
  keys *into* `configurable`. The hazard is langchain-core-wide, not LangGraph-specific.
- **#8** (propose/commit split) — core claim correct, prescribed remedy was LangGraph-shaped.
  Recorded as the C8 append-only invariant instead.
- **#9** (identity resolution) — already D-4/D-4a, which are strictly stronger. Its blocking claim
  was wrong: AC-7 is scoped to an *unchanged store*, and compose never re-runs the matcher.

---

---

# Round 3 — findings from the Phase 0 substrate review (2026-08-04)

The C1/C4 DDL, the severity lookup and the `CanonicalField` update paths were reviewed against a
**live PostgreSQL 16 cluster with pgvector 0.6.0** — the first time anything in `sql/` had been
executed rather than parsed. Every finding below was reproduced before being fixed and
re-reproduced after; the commands and results are in `sql/README.md`.

> **Renumbered 2026-08-04, A-23..A-30 → A-28..A-35.** This block originally restarted at A-23,
> which round 2 had already used, so five IDs each named two different findings and every citation
> of one resolved to the wrong finding half the time. Round 2 is the older block and is cited from
> outside this file — `spec.md`, `docs/architecture.md` and `services/{indexing,retrieval}`
> all cite A-23/A-24/A-25 for the FR-RAG-02, FR-RAG-03 and NFR-03 reversals — so it keeps its
> numbers and this block moved. A repository-wide grep found **no** in-tree reference to a Round 3
> ID, so nothing else needed editing; a citation made outside the tree before this date should add
> five. Registered below as [A-36](#a-36-high--five-finding-ids-each-named-two-findings).

| ID | Severity | Finding | Status |
|---|---|---|---|
| A-28 | **M** | `requirements-traceability.md` had no AC-7 or AC-8 row while `spec.md` defines eight acceptance criteria | **Fixed** — rows supplied on `agent/developer-documentation` (PR #22); see the note below |
| A-29 | **H** | RLS on `document`/`chunk` only: `claim.value`, `audit.event.payload`, `conflict.explanation`, `resolution.value_before`/`value_after` and `job.payload` all returned restricted content to a role that could not see the document | **Fixed** |
| A-30 | **H** | `INSERT ... ON CONFLICT`/`RETURNING` failed for `access_restricted = true` and succeeded for `false` — the schema penalised the safe action | **Fixed** — separate write role |
| A-31 | **M** | `00_roles.sql` never re-asserted role attributes, so a `procurement_app` carrying `SUPERUSER BYPASSRLS` survived a clean re-run of the file that exists to prevent exactly that | **Fixed** |
| A-32 | **M** | `08_job.sql` granted full-table `UPDATE`, including `idempotency_key` — the whole of I.2's at-least-once guarantee | **Fixed** — column-level grant |
| A-33 | **H** | `_gross_divergence` could not fire for 105 of 124 contract keys, including 22 of 24 Tier A and all 12 CRITICAL fields, while its docstring named the decimal-comma trap as its purpose | **Fixed** — order-of-magnitude fallback |
| A-34 | **M** | Five further routes reached the forbidden RESOLVED-with-no-`Resolution` state, and `evolve()` silently replaced a recorded `Resolution` | **Fixed**, except one undefendable route — see below |
| A-35 | **M** | 81 of 120 one-step mutants of the `CRITICALITY` table survived the suite: membership was checked both ways, values were pinned for ~36 keys | **Fixed** — all 124 pinned |

## A-28 (Medium) — the traceability table stopped at AC-6

`spec.md:194-197` defines AC-7 (byte-identical regeneration) and AC-8 (an uncleared user cannot
influence a retrieved result) as additions to the TRS's six, and `tasks.md` assigns owners for
both. `requirements-traceability.md`'s acceptance-criteria table listed AC-1 through AC-6 and
stopped, so the two criteria that exist *because* they "silently rot without a test" were the two
with no row saying whether a test existed.

The document's own audit note is the reason this matters more than a missing line: it promises a
closed vocabulary a reader can trust, where `enforced` means a regression test protects the
requirement. A criterion with no row at all is outside that vocabulary entirely — it reads as
though the table is complete when it is not.

**Fixed** — the rows landed on `agent/developer-documentation` (PR #22) while this branch was in
review, with statuses `partial` (AC-7: `test_workbook_determinism.py` covers `normalize_archive`,
but `write_workbook` raises `NotImplementedError` so no complete workbook is regenerated) and
`declared` (AC-8: `VectorStorePort.search(allowed_document_ids=...)` declares the parameter and no
adapter implements it). Both were checked against the code and left as written rather than
duplicated here, so the two branches merge without a conflict.

**AC-8's row needs one revision once both branches land**, and it is recorded here rather than
edited into a file another branch owns: this branch adds row-level security to all seven tables
that hold document content, with `FORCE ROW LEVEL SECURITY` and a live-verified confidentiality
derivation, plus `tests/test_sql_schema.py`. The *Where* column citing only `ports` is no longer
the whole story, and `declared` understates it. It is not `enforced` either — the enforcement is
DDL that CI does not execute.

**Closed 2026-08-04, and the last sentence above did not survive.** Both branches landed, and the
row is now `partial` with the RLS citation written out. But the reason given here for withholding
`enforced` — "DDL that CI does not execute" — expired when #25 added the `sql` job, which runs
`tests/test_sql_behaviour.py` against a pgvector service container and fails if the suite skips.
The conclusion stands on a different ground, recorded in the row itself: there is no retrieval
path, so nothing yet does the thing AC-8 constrains. Worth leaving both sentences visible rather
than editing the first away — a register entry whose *reasoning* silently changes underneath its
*verdict* is how A-15 happened.

## A-34 (Medium) — the forbidden state had five more doors than the count said

`requirements-traceability.md`'s FR-HITL-06 row said "two routes remain open". Measured, seven
did: `model_construct`, an instance `__dict__` write, `object.__setattr__`, `deepcopy` and
`pickle` round trips of either, and — separately — `evolve()` and plain assignment each replacing
a recorded `Resolution` with a different one, silently, because the resulting state is legal and
no validator can see a transition.

Six are closed at the point they occur. The route that remains is writing the instance `__dict__`
directly; `object.__setattr__` is the same write in a different spelling, not a second route, so
the honest count is one. No Python object can defend against it, and
`test_the_dict_write_route_is_documented_as_open` asserts it as open on purpose, so the gap is a
recorded fact rather than an oversight.

---

---

# Round 4 — the traceability audit after CI landed (2026-08-04)

`docs/requirements-traceability.md` was re-read row by row, opening each cited artifact and each
cited test. Prompted by #25, which made the `sql/` guarantees executable, and by the three service
modules (`claims`, `identity`, `confidence`) plus `conflict_hitl/severity.py` landing before it.

**The table holds 48 requirement rows and 8 acceptance-criterion rows**, counted from the file:
8 FRD + 10 FR-ING + 5 FR-RAG + 5 FR-WEB + 6 FR-HITL + 6 FR-OUT + 8 NFR. The brief for this audit
said 49, and A-27 is the standing reminder that a count asserted rather than computed is how an
audit certifies itself wrong. No ID is missing — the 48 reconcile against spec.md's 32 TRS FR IDs
plus the 8 FRD requirements and 8 NFRs.

| ID | Severity | Finding | Status |
|---|---|---|---|
| A-36 | **H** | **Five finding IDs each named two different findings.** Round 3 restarted numbering at A-23, which round 2 had already used, so A-23..A-27 were ambiguous — and this file is the document the contributor docs tell people to cite | **Fixed** — Round 3 renumbered A-28..A-35 |
| A-37 | **H** | **The traceability table understated merged work — A-16 with the sign reversed.** Ten rows sat below what the code and tests support, including NFR-02 reading "store not yet built" beside a nine-file schema whose tripwires run against a live server on every pull request | **Fixed** |
| A-38 | **M** | The table had no rule for weighing a live-database test against a structural one, and the two are not interchangeable: two of four reintroduced defects were invisible to `test_sql_schema.py` | **Fixed** — rule stated once, applied uniformly |
| A-39 | **M** | **A-31's fix covers role *attributes* and not role *memberships*.** `GRANT procurement_ingest TO procurement_app` survives a clean re-run of `00_roles.sql`, defeating the Decision 9 boundary, and no test names the cause | **Fixed** |
| A-40 | **L** | A-24 restored the FR-RAG-03 reversal but its inline note named the *replacement* wrongly — "the embedding model's sparse output", a Decision 5 reserve, where Decision 3b chose Postgres `tsvector`/`pg_trgm` fused with RRF (k=60). The `⚠️` note shipped as the register's own closed remedy | **Fixed** — `spec.md` note corrected against Decision 3b; A-24's row kept intact |

## A-36 (High) — five finding IDs each named two findings

The defect is in this file, which is what makes it high rather than tidy. `CONTRIBUTING.md` and
`docs/architecture.md` both direct contributors here and tell them to cite a register ID when a
plan decision reverses a normative requirement, and three of the five collided IDs are exactly
those citations: A-23 (FR-RAG-02's ANN mandate), A-24 (FR-RAG-03's BM25 clause), A-25 (NFR-03's
mechanism). A reader following one of those from `spec.md` had even odds of landing on a
PostgreSQL finding about role attributes.

Renumbering the *second* block is the resolution rather than a prefix scheme (`R3-1`, say),
because the collision is only in this file: a repository-wide grep found every in-tree citation
pointing at a round-2 ID, so moving round 3 costs nothing outside and a second ID format would be
a permanent tax on every future reader. Both prose sections below the round 3 table moved with
their entries.

**Lesson, and it is A-15's again in a new place:** the count was asserted from the block being
written rather than from a scan of the file it was being appended to. `grep -o '^| A-[0-9]*' |
sort | uniq -d` takes a second and is now the check.

## A-37 (High) — the register's own remedy, applied in the wrong direction only

A-16 established that `enforced` must mean a test exists, and the rows that overclaimed were
corrected downward. The vocabulary held. What did not hold is the other direction: between that
audit and this one, `sql/` gained row-level security on seven tables, an append-only
claim/resolution/audit trio with statement-level TRUNCATE tripwires, a separate ingest role, and —
in #25 — a CI job that executes all of it against a pgvector container and fails if the suite
skips rather than runs. `services/claims`, `services/confidence` and `services/identity` landed
alongside (1,458 lines, plus 565 in `conflict_hitl/severity.py`), each with a dedicated test file.
Ten rows should have risen and none did.

The asymmetry is worth naming because it is predictable: a demotion is prompted by an audit, while
a promotion is prompted by nothing at all — the author of a merged PR is not reading a traceability
table. Both directions cost the same thing, which is a reader's willingness to believe the column.
NFR-02's "store not yet built", sitting beside `sql/07_audit_event.sql` and four live tests that
walk its hash chain, is not a smaller error than an unearned `enforced`; it is the same error.

**Not raised, and deliberately.** FR-RAG-02 and FR-RAG-03 gained a genuine storage home in
`sql/03_chunk.sql` and stayed `declared`, because the only live test touching that table asserts
Decision 3a's *reversal* of FR-RAG-02 rather than the requirement. Counting it would be A-16's
defect reached by a new route.

## A-38 (Medium) — a structural test and a behavioural one are not the same evidence

`test_sql_schema.py` asserts DDL text; `test_sql_behaviour.py` runs the attack. Both are worth
keeping and the traceability table needs to cite them differently, which it had no rule for. The
difference was measured rather than assumed, by reintroducing four closed defects one at a time:

| defect reintroduced | `test_sql_schema.py` | `test_sql_behaviour.py` |
|---|---|---|
| `UNIQUE (content_hash)` dropped from `02_document.sql` | **green** | red |
| `claim`'s confidentiality policy widened to `USING (true)` | red | red |
| `resolution`'s statement-level TRUNCATE tripwire deleted | red | red |
| `GRANT procurement_ingest TO procurement_app` added | **green** | red |

Two of four are invisible to the structural suite — including the privilege-separation boundary
that Decision 9 rests on, where nothing in the DDL text changes at all and the grant is simply
added. So a `sql/` row cites the behavioural test where one exists and says so where only the
structural one does.

The second half of the rule is what a live test is *not* worth. It skips silently without
`PROCUREMENT_TEST_DSN` and runs only in CI's `sql` job, and — more importantly — it proves the
schema behaves, not that any Python path uses the schema. Every row in this position still has an
unimplemented application half, so each is `partial` and names which half is live-tested. `AC-5`
is the clearest case: the store refuses a duplicate `content_hash` under test, and
`services/ingestion.ingest` still raises `NotImplementedError`, so nothing re-ingests.

## A-39 (Medium) — A-31 normalises role attributes, not role memberships

> **Fixed 2026-08-04.** `00_roles.sql` now revokes every membership among the
> four project roles on each run, beside the `ALTER ROLE`s that normalise
> attributes, and `tests/test_sql_behaviour.py::test_no_project_role_is_a_member_of_another`
> asserts `pg_auth_members` is empty for them — naming the cause where
> `test_the_app_role_cannot_escalate_to_the_ingest_role` names the consequence.
>
> Verified by the discriminating case rather than by a plain revert: with a
> stray `GRANT procurement_ingest TO procurement_app` already in the cluster,
> the suite is **24 passed** with the revoke loop and **9 failed** without it.
> A plain revert cannot show this — the session fixture rebuilds the schema from
> scratch, so there is no pre-existing grant to revoke, which is exactly the
> state the fix exists for.

Found by accident, which is the only reason it is here: the fourth mutation above
(`GRANT procurement_ingest TO procurement_app`) was reverted in the *file* and the suite stayed
red, because the grant had been made in the *cluster* and nothing takes it back.

Reproduced deliberately afterwards, against PostgreSQL 16:

```
GRANT procurement_ingest TO procurement_app;   -- the stray grant
psql -f sql/00_roles.sql                       -- a clean re-run of the unmodified file
SELECT ... FROM pg_auth_members ...            -- procurement_app IS MEMBER OF procurement_ingest
```

The membership survives. Every attribute assertion passes at the same time — `rolsuper`,
`rolbypassrls`, `rolcanlogin` are all correct — because A-31 fixed exactly those, with
unconditional top-level `ALTER ROLE ... NOSUPERUSER NOBYPASSRLS ...` statements and a `pg_roles`
assertion block. `ALTER ROLE` does not touch `pg_auth_members`, and nothing else in the file does
either.

**This is A-31's own finding in the one form its fix does not reach**, stated in A-31's words: a
pre-existing over-privileged `procurement_app` survives a clean re-run of the file that exists to
prevent exactly that. The consequence is Decision 9's boundary: with the membership in place
`procurement_app` can `SET ROLE procurement_ingest` and read every restricted row through the
ingest read-back policy that `test_the_write_role_can_read_back_what_it_writes` requires. Eight of
the 23 live tests go red, so the *consequence* is caught loudly — but
`test_owner_roles_cannot_log_in_and_the_app_is_unprivileged` reads `rolsuper` and `rolbypassrls`
and not `pg_auth_members`, so nothing names the *cause*, and a reader sees eight confidentiality
failures rather than one role grant.

**Not a defect in the committed DDL**, which never issues that grant — it is a hardening gap, and
the same one A-31 judged worth closing. The remedy is symmetric with A-31's: an unconditional
`REVOKE` of the memberships the design forbids, plus a `pg_auth_members` clause in the existing
assertion block so a bootstrap identity that cannot revoke fails loudly instead of silently. A
matching assertion in `test_owner_roles_cannot_log_in_and_the_app_is_unprivileged` would name the
cause.

**Left open deliberately.** The fix is in `sql/00_roles.sql` and `tests/test_sql_behaviour.py`,
neither of which this branch owns. Registering it is the whole point of the register: A-15's
lesson was that a fix asserted from the edits made rather than from a search across the repository
is not a fix, and the converse holds too — a finding made in a file you cannot edit is still a
finding, and dropping it because it is inconvenient to fix is how it stays lost.

## A-40 (Low) — A-24's inline note named the wrong replacement

A-24 closed as **Fixed**: the FR-RAG-03 reversal was registered and the TRS `shall` was marked
overridden inline rather than paraphrased away. That much held. What its inline note got wrong is
the *replacement* — it read "lexical matching from the embedding model's sparse output, not BM25",
and Decision 3b chose no such thing. The plan's lexical leg is Postgres `tsvector`/GIN full-text
plus `pg_trgm` trigram, fused with Reciprocal Rank Fusion (k=60) (plan Decision 3b). The embedding
model's sparse output is a *Decision 5 contingency* — swap Qwen3-Embedding-4B for `bge-m3` if
Postgres full-text proves weak on part numbers — held in reserve and not adopted.

This is filed as its own entry, not as an amendment to A-24, for the reason A-37 is its own entry:
a remedy that shipped and later proved defective is a new finding, and rewriting the closed row
would erase that A-24's `⚠️` note was wrong for the interval between the two. The register is an
audit trail; corrections append.

The three code docstrings that had repeated one or other stale position — `ports/__init__.py`,
`services/retrieval/__init__.py`, `services/indexing/__init__.py` — were already corrected against
Decision 3b. The `spec.md` note was the last carrier, and this change corrects it; `docs/
architecture.md`'s "inaccuracy" table, which had flagged the note and pointed at it as what
remained, now records the note as corrected and points here.

---


# Round 5 — the post-merge documentation audit (2026-08-05)

Prompted by pulling `main` at `30d1198` after #19 and #21–#27 merged, and re-reading every
status-claiming artifact against the code. Four parallel readers covered the service modules,
`sql/` + CI, the prose documentation, and the traceability table.

**Read the method note before the findings, because half of this round's raw output was wrong.**
Four of the eight candidate findings did not survive verification, and every one of the four
failed the same way: **the reader diffed against a base commit that was not the one the artifact
was written against.**

- C3 was reported as marked **done** with an element missing, because `grep -rn span src/` finds
  nothing. `section` *is* C3's span; the mapping was recorded in one paragraph of
  `current-state.md`. The real defect was the naming, not the status — registered as A-45.
- `current-state.md`'s "23 skipped" was reported as an off-by-one. It is correct at commit
  `72deacf`, which that file **names in its own second line**. A dated snapshot is not a stale
  claim.
- A-34 was reported as misquoting FR-HITL-06's "three routes remain open" as "two". A-34 audits
  the code that shipped in #19, and at `e7da9ad` the row said **two**. The quote is exact; the
  reader compared against `17b9c90`.
- The traceability table was searched for `enforced` inflation and has none: the `enforced` row
  set is byte-identical across the whole changeset, and all ten raises stop at `partial`.

**Lesson, and it is A-27's in a new place.** A-27 recorded that a count asserted rather than
computed is how an audit certifies itself wrong. This round adds the companion: *a diff taken
against a base the artifact never claimed is how an audit invents defects that were never there.*
The check is one line — read the baseline the document declares before diffing it — and three of
the four false positives would have died on it. A register that recorded only the confirmed
findings would leave the next reader to rediscover all four.

> **Postscript, added in review of the pull request that carried this round.** Five parallel
> reviewers were run over the branch before merge. They found three defects **in this round's own
> work**, and two of the three are A-27's failure verbatim — a universal asserted from partial
> data, by the same author who had just written the note above warning against it.
>
> - A-44 and the `ci.yml` comment claimed *"no `pg16`-family tag has ever shipped below 0.8.0."*
>   Eight such tags exist. The claim came from one unfiltered page of a registry listing.
> - The commit message claimed NFR-01 and NFR-08 were *"the only `enforced` rows naming no test."*
>   FR-WEB-03 and FR-HITL-02 had the same gap; both are now A-47.
> - The round audited three of the four status-claiming documents and missed the fourth —
>   registered as A-46.
>
> Two conclusions worth more than the fixes. First, **writing the lesson down does not confer
> immunity to it**: the note above was already in the file when both false universals were
> written. Second, the thing that caught all three was *review by readers who did not write the
> work* — every one of the three had survived the author's own verification pass, and the two
> false universals had each been "verified" by a command whose output the author had read.

| ID | Severity | Finding | Status |
|---|---|---|---|
| A-41 | **H** | **`README.md` was frozen at `ce6a8bb` while the Phase 0 substrate landed**, so six claims went stale at once: the contract count ("seven of the eight are still open"), the status table's "PostgreSQL schema … designed, not implemented", "the planned runtime — none of this exists yet", the build-plan position, a repository tree with no `sql/` and none of the three new service packages, and a validation block listing three of the four gates. All six understate the repository, which is the safer direction and still leaves the file self-contradictory against `current-state.md` — which it names, two lines in, as the source of truth that wins | **Fixed** |
| A-42 | **H** | **The `docs/source/` confidentiality rule was deleted in the README rewrite and restored nowhere.** `.gitignore` was left as its only trace, so the mechanism survived and the rule and its rationale did not. The same rewrite dropped "embedding" from the endpoint constraint, narrowing a two-endpoint rule to one — and the embedder is the easier of the two to point at a public API by accident | **Fixed** |
| A-43 | **M** | **Two status documents disagreed about AC-5.** `current-state.md` read "Nothing. `content_hash` exists but carries no uniqueness constraint / **open**" while `requirements-traceability.md` and this register both had `partial` with a live-database test named. The constraint landed in `e7da9ad`, an ancestor of the audit's own declared baseline, so the row was wrong when written rather than stale | **Fixed** |
| A-44 | **M** | **CI asserted a pin it did not have and coverage it did not provide.** `ci.yml` said the `pgvector/pgvector:pg16` tag "pins the major version and the extension version together" and that the image exercises `01_extensions_and_settings.sql`'s sub-0.8.0 skip path. `pg16` pins only the PostgreSQL major — pinned-extension tags take the `<pgvector>-pg<major>` form — and the pinned image is 0.8.6, so the branch is not exercised | **Fixed** — pinned to `0.8.6-pg16`, verified by applying all nine files and running the 24 live tests green against that exact image; comment now records that the skip path is *not* covered, and that it is coverable |
| A-45 | **M** | **Contract C3's `span` is spelled `section`, and four of the five places C3 is cited did not say so.** `tasks.md`, `current-state.md`'s contract table, `field.py` and `claims/__init__.py` all recited the four-tuple and claimed "all four elements on `SourceRef`"; only one paragraph of `current-state.md` recorded the rename. An audit consequently grepped for `span`, found nothing, and concluded an element was missing — a correct inference from four fifths of the evidence | **Fixed** — mapping named at every site, pinned by `test_source_ref_carries_c3s_four_elements` |
| A-46 | **M** | **The drift audit drifted: it corrected three of the four status-claiming documents and skipped `docs/architecture.md`.** `development.md`'s own documentation-expectations table names that file, says what it claims ("design-versus-implementation boundary, and the services table") and when to update it ("a service stops being a stub"). Both halves were stale: the audit-event section read "This design has not been implemented yet" beside a hash chain that is live-tested on every pull request, and the services table omitted `claims`, `confidence` and `identity` — three fully implemented modules | **Fixed** — persistence section re-scoped to schema-built/Python-absent, audit-event paragraph rewritten against `sql/07_audit_event.sql`, three rows added to the services table |
| A-47 | **M** | **Two `enforced` rows have named no test since the file's first commit.** FR-WEB-03 and FR-HITL-02 both cite `assert_no_autonomous_overwrite` bare, while FR-4 cites the *same function* and does name its test — so the citation existed and was simply never copied across. `enforced` promises a regression test protects the requirement; a row that does not say which one cannot be checked without re-deriving it, which is the work the vocabulary exists to save | **Fixed** — both now cite `tests/test_source_of_record_rule.py` |

## A-41 (High) — the summary outlived the thing it summarised

`README.md` opens its status table by saying it is "a summary derived from
[Current state](docs/current-state.md), which is the source of truth. If the two disagree, that
document is right and this one is stale." That sentence is the correct design, and it is also
what made the drift invisible: a reader who hits a wrong claim has no signal that they are
holding the stale copy, because staleness is only detectable by opening the other file.

`docs/development.md` already anticipated this exactly — its documentation-expectations table
names `README.md` § Current state among the four documents a status-changing PR must update, and
adds that it "is the one that gets forgotten." It was. The gap is that nothing *executes* that
expectation; every other gate in this repository that matters is a test or a CI job.

**Not fixed here, and worth stating as the residual:** a derived summary with no mechanical link
to its source will drift again. The options are to delete the table and link out, or to generate
it. Both are larger changes than this round, and the second needs `current-state.md` to carry
machine-readable status markers it does not have today.

## A-42 (High) — a control whose only remaining trace was its enforcement

The deleted sentence was: "Nothing under `docs/source/` is committed — the source requirements
documents are marked confidential. See `.gitignore`."

What survived is `.gitignore:4`. That is the enforcement, and it is doing its job — but a rule
that exists only as a mechanism cannot be reasoned about, extended, or defended in review. The
nearest survivors after the rewrite were both weaker and differently scoped: `CONTRIBUTING.md`'s
do-not-commit list names "supplier-confidential documents" without naming the FRD, the TRS, or
the ignored path, and `README.md`'s own security section had folded its git rule into a list
prefixed "the production design **requires**" and closed "these controls are **designed but not
implemented**" — which reads as aspirational, and is the one framing under which a contributor
might conclude the rule is not yet in force.

This is precisely the category `ce6a8bb` was written to catch — "content deleted with nothing
left defining it" — and it slipped through that same commit. The lesson is that the check has to
run against *deletions*, not just against surviving text; a grep for what a document still says
cannot find what it stopped saying.

## A-43 (Medium) — the disagreement mattered more than the error

Either document being wrong alone is an ordinary staleness bug. Both being present and
disagreeing is worse in a specific way: the contradiction is only visible to someone who opens
both files and compares one row, which is exactly what nobody does. A reader who consults one
document gets a confident answer either way.

The `enforced`/`partial`/`declared`/`open` vocabulary was introduced so that a status word would
be a promise a reader could rely on without re-deriving it. That promise is per-vocabulary, not
per-file, and it is broken the moment two files using the same vocabulary disagree about the same
ID. `requirements-traceability.md` is the one that defines the vocabulary, so it wins ties by
construction; `current-state.md` has been corrected to match, with the correction recorded inline
rather than silently overwritten.

## A-44 (Medium) — the comment was the specification, and it was wrong twice

Two independent false claims in one five-line comment, and they fail in opposite directions. The
pin claim **overstates** reproducibility: CI was riding a rolling tag, so the live suite could
change its extension version with no diff in the workflow file — the author plainly intended a
pin and did not get one. The coverage claim **overstates** verification: the sub-0.8.0 branch of
`01_extensions_and_settings.sql` is asserted to be exercised by an image well above 0.8.0.

The second is the more dangerous of the two, because it is the shape A-16 named: a comment
asserting that something is covered is treated as evidence that it is covered, and it survives
review precisely because it is adjacent to a real, passing job. The fix pins the tag *and* says
plainly that the branch is uncovered — the coverage gap is left open deliberately, to keep the
job to one container, and the comment now says so rather than implying the gap is forced.

Verified rather than assumed: the pinned image was pulled, all nine files applied to it, and
`test_sql_behaviour.py` run against it — 24 passed, pgvector reporting `0.8.6`.

> **Corrected in review, before this landed.** The first version of this finding — and of the
> `ci.yml` comment it describes — asserted that *"no `pg16`-family tag has ever shipped below
> 0.8.0, so the branch is unreachable in CI."* That is false: `0.6.0-pg16` through `0.7.4-pg16`
> are published and still pullable, eight tags. The branch is **coverable and uncovered**, which
> is a choice, not a limit. The error came from reading one unfiltered, recency-ordered page of
> the registry's tag list and generalising from it — *asserted rather than computed*, which is
> the exact failure this round's method note names. See the postscript there.

## A-45 (Medium) — the one contract element whose name is not its name

C3 is `(document_id, page, span, extractor_version)`. Three of those four are attribute names on
`SourceRef`. The fourth is not: `span` is `section`.

There is no separate contract file for C3 — the tuple in `tasks.md` *is* the definition — so
nothing anywhere resolves `span` to an attribute except one paragraph of `current-state.md`. Four
other sites recited the tuple and then asserted "all four elements on `SourceRef`", which is true
and unverifiable in the same breath: the reader cannot confirm it without already knowing the
mapping the sentence omits.

The failure this produced is worth recording precisely, because it is the *inverse* of the usual
one. The register's standing worry is a status marker claiming more than the artifact supports.
Here the artifact genuinely supported the marker, and the documentation was written so that a
careful reader checking the claim would conclude it did not — an audit did exactly that and filed
C3 as inflated. Documentation that cannot survive being checked is a defect even when the thing
it documents is correct.

The mapping is now stated at all five sites and pinned by a test that fails if `section` is
renamed away without the contract being renamed with it.

**Granularity settled 2026-08-07 by the lead architect: `section` stands, and there is no
rename.** The question was whether a table/section locator is the right resolution for C3's
span, as against character offsets into the source text. It is, and the argument nobody had
written down is this:

C3's purpose is that a stored value can be traced back to where it came from. Two locators
already cover the two document paths that exist. For a scanned page, FR-ING-04 requires a
bounding box and `SourceRef.bounding_box` carries it — geometry, which is finer than any
character offset and is what a reviewer actually needs to find a number on a page image. For a
text-layer document, `section` names the table or heading a value was read from, which is how
datasheets are organised and how a human re-finds a value. Character offsets would be more
precise and less useful: nobody locates a figure in a datasheet by counting characters, and an
offset breaks the moment a document is re-parsed by a different extractor version, which C3's
own fourth element exists to record.

Renaming `section` to `span` was considered and rejected separately: it would cost the column,
the tests and the fixtures for no behaviour change, and the mapping is now documented at every
site that cites the contract.

## A-46 (Medium) — the drift audit drifted

`docs/development.md`'s documentation-expectations table exists precisely to prevent this. It
names four documents that make status claims, says a behaviour change "goes stale in all four at
once", and instructs: "Update every one that your change falsifies." This round read three of
them and did not open the fourth.

Both of `architecture.md`'s status-bearing halves were stale, and the table names both:

- **The design-versus-implementation boundary.** "Audit events are planned as per-document hash
  chains … This design has not been implemented yet" — written when `sql/07_audit_event.sql`
  already existed, and left standing after four separate constraints were added to that chain and
  wired to live tests that run on every pull request.
- **The services table.** `services.claims`, `services.confidence` and `services.identity` were
  absent entirely — three modules with no `NotImplementedError` in them at all, and the table's
  own update trigger is "a service stops being a stub."

The interesting part is *why* the omission happened, because the reader that found
`architecture.md`'s staleness in the first place was one of this round's own four, and reported
it. It was dropped between that report and the fix list. So the failure was not detection, it was
**the hand-off from finding to fixing** — and nothing in the process checks that the second list
is a superset of the first. That is a gap the documentation-expectations table cannot close,
because it governs which documents to update, not whether the audit's own findings all survive
triage.

## A-47 (Medium) — two rows that never named a test, and one that did

`enforced` is defined in the traceability doc's own header as "implemented **and covered by a
test**", and the audit note beneath it says the vocabulary is worth more than a flattering count
because a reader must be able to trust it without re-deriving it. A row that says `enforced` and
names no test hands that re-derivation straight back.

FR-WEB-03 and FR-HITL-02 had done so since the file's first commit. Neither was a judgement call
about weak coverage: **FR-4 cites the same function, `assert_no_autonomous_overwrite`, and does
name its test.** The citation existed the whole time and was never copied across, through two
separate passes (`791201b`, `ce6a8bb`) that re-read the table row by row for exactly this.

Recorded rather than quietly fixed because of how it was found. The pull request carrying this
round claimed in its commit message that NFR-01 and NFR-08 were "the only `enforced` rows naming
no test" — an assertion, not the output of `grep '| enforced |'`, and wrong by two. The same
one-line check that A-36 and A-27 both prescribe would have produced the right answer and the
complete fix in one step.

---


# Round 6 — the contract advisory pass (2026-08-06)

Prompted by an advisory review of C1–C7 ahead of Phase 0's remaining freezes. One new finding;
the rest of that review produced recommendations rather than defects, drafted as [D-13 and
D-14](clarifications.md).

| ID | Severity | Finding | Status |
|---|---|---|---|
| A-48 | **M** | **FR-OUT-06 and AC-7 cannot both be satisfied by a wall-clock `generated_on` stamp, and no *specification* artifact says so.** FR-OUT-06 requires the workbook to carry "a generated-on timestamp"; AC-7 requires two generations from an unchanged store to be byte-identical, and `tasks.md` G.5 verifies it with `sleep(1.1)` between the runs specifically so a clock-derived value would differ. `services/output/__init__.py:151-153` already states the resolution — "no timestamps or ordering derived from anything but the store itself, plus an explicit generated-on stamp" — but it sits in a docstring on an unimplemented function, and neither `spec.md`, the traceability table nor this register carries it | **Fixed** — D-14 adopted 2026-08-07. `generated_on` is the maximum store write-timestamp over the rows the projection reflects, folded from timestamps already inside the projection so AC-7-safety is structural; an empty store renders an explicit null |
| A-49 | **M** | **`audit.event` declares an event type it structurally cannot store, and it is the one NFR-02 names.** The taxonomy includes `'web_search'` (`sql/07:84`), but `document_id` is `NOT NULL REFERENCES public.document` (`:57`) under `CHECK (stream = 'doc:' \|\| document_id)` (`:116`), and `search_for_gap(field_name, supplier, model)` carries no document — by definition, since FR-WEB-01 triggers it precisely when *no* document supplied the value. No `DocumentType` member covers a web page either, so the FK cannot be satisfied by registering the source. Compounding it, `spec.md:153` says "**web** queries…" while `plan.md:66` paraphrases it as "…**query**…", dropping *web*, and D-13's first draft inherited the plan's wording and cited cross-document *retrieval* queries — which the normative text never names | **Fixed in principle** — D-13 adopted 2026-08-07: run-scoped events get a separately chained `audit.run_event` table keyed `run:<id>`, which is where gap-triggered web searches land. The event type stays in `audit.event`'s v1 taxonomy but is unreachable there; removing it is a taxonomy amendment WP-H should make when it writes the emitter |
| A-50 | **H** | **D-14 banned enum `repr()` from hashed array order in one bullet and prescribed it in the next.** The condition-group bullet correctly routed ordering through `encode_value()`; the candidate bullet pinned ordering to `conflict_hitl._ordering_key`, whose **first component is `repr(candidate.condition.grouping_key())`** (`conflict_hitl/__init__.py:67`) — verified to render as `(<MeasurementBasis.STC: 'stc'>, None, …)`. Third instance of A-6's class, and the first to survive the remediation of its own predecessor | **Fixed** — D-14 now states one rule governing both bullets: nothing deciding hashed array order may contain an enum `repr()`. The projection sorts by `_ordering_key`'s *field sequence* with every component routed through `encode_value()` |
| A-51 | **M** | **`ProjectionPolicy.policy_version` is a label for a τ table that is not yet built, and how the label would be checked against it is undecided.** `project_store` (`projection.py:167`) hashes `ProjectionPolicy` directly, and `confidence_threshold` — a bare `float` today — is embedded byte-for-byte in the preimage, so changing it now moves the hash; there is no live exploit. `policy_version`'s own docstring (`projection.py:120-126`) instead ties it to task B.10's field-tiered `threshold_for(field_name)` (D-3), which nothing in the codebase implements yet — already deferred once, for the same reason, at [A-11](#a-11-medium--output-flagging-assumes-one-global-threshold). `services/conflict_hitl/tolerance.py` is a distinct, already-shipped table for a *different* contract (D-2/task E.1's numeric conflict tolerance), and `projection.py:47` states this module does not import `conflict_hitl` — that table plays no part in this gap | **Deferred** — same root cause as A-11. Closing it needs a decision, made when B.10 lands, that the tiered table is embedded by value in `ProjectionPolicy` rather than looked up through the label alone |
| A-52 | **M** | **Audit emission must originate from wherever a state transition commits, never from a parent-process observer — a constraint this architecture satisfies today by accident, not by rule.** `audit/writer.py`'s advisory-lock design (D-13) forces every writer to `INSERT` independently under its own per-stream lock, because a lock taken inside a trigger reads a stale chain tip — 8 concurrent writers measured 42 silent forks without it (`audit/writer.py:1-8`). That shape happens to also rule out a failure Kedro's `ParallelRunner` demonstrates concretely: its docstring claims it "will not execute node and dataset hooks" while `Task._run_node_synchronization` (`kedro/runner/task.py:115-143`) rebuilds a fresh, unshared `PluginManager` per worker — hooks *do* fire, and a hook accumulating state across calls silently fragments with no reconciliation. `orchestrator.run()` raises `NotImplementedError` (`orchestrator/__init__.py:95-97`), so nothing exercises this today and no code is broken | **Constraint recorded, not a defect** — already written into `orchestrator/__init__.py:11-14` and `docs/architecture.md`'s "Persistence and execution" section by a separate task; this row only indexes it into the register |

## A-48 (Medium) — a constraint that only one docstring knows about

The tension is exact, and it is only a *contradiction* under the reading nobody wrote down:

- `spec.md:144` (FR-OUT-06): the workbook "carries a generated-on timestamp plus the vintage of
  every source."
- `spec.md` AC-7: two generations from an unchanged store are byte-identical — and
  `tasks.md:229` (G.5) verifies precisely that with `sleep(1.1)` between the two runs. The sleep
  exists *because* a naive timestamp would otherwise pass by accident on a fast machine.

A wall-clock stamp changes between those two runs, so it fails AC-7. Removing the stamp fails
FR-OUT-06. **A store-derived stamp satisfies both texts with no normative edit at all** — which
is what makes this a documentation defect rather than a spec defect.

**The resolution already exists, in exactly one place, and it is the wrong place.**
`services/output/__init__.py:151-153` says the workbook "must be deterministically regenerable
from the canonical store (FR-OUT-06), which means no timestamps or ordering derived from
anything but the store itself, plus an explicit generated-on stamp." That sentence *is* the
answer. But it is a docstring on a function that raises `NotImplementedError`, and nothing in
`spec.md`, the traceability table or this register repeats it. An implementer reading only the
normative artifacts would reach for `datetime.now()` and satisfy FR-OUT-06 while breaking AC-7,
with no document telling them otherwise until G.5 went red.

**Why it stayed invisible.** Both requirements are individually testable and neither test exists
yet: nothing has ever emitted a `generated_on` value, and AC-7 is covered only at the archive
level by `normalize_archive`. Two requirements can sit in tension indefinitely while both are
marked `partial` — the traceability table tracks whether each row is *supported*, and has no
mechanism for noticing that two supported rows constrain each other. That is a real gap in the
technique, not just in this instance, and it is the part worth keeping.

**Resolved 2026-08-07.** D-14 was adopted, promoting that docstring's rule to a ratified
decision and naming the derivation: the maximum store write-timestamp over the rows the
projection reflects, folded from timestamps already inside the projection so AC-7 safety is
structural. An empty store renders an explicit null.

---


## A-49 (Medium) — an event type with nowhere to go

`sql/07_audit_event.sql` is unusually careful about making its guarantees structural: `stream` is
tied to `document_id` by CHECK so a stream can never name a document that does not exist, and the
FK is `ON DELETE RESTRICT`. Those are the file's two strongest properties and neither should be
weakened.

The consequence nobody traced is that they make one of the taxonomy's own seven values
unstorable. A gap-triggered web search has no originating document:

- FR-WEB-01 fires it only "when a required field has no system-of-record value";
- `services/web_search.search_for_gap(field_name, supplier, model)` takes no `document_id`, and
  its docstring already cites NFR-02 for query logging;
- the eight `DocumentType` members are all supplier-document kinds, so the web source cannot be
  registered as a `document` row to satisfy the FK either.

**Why this matters more than the gap D-13 originally cited.** NFR-02 enumerates "web queries,
extractions, conflicts and resolutions" — web queries are named explicitly. Retrieval queries are
not, and `plan.md:66` only appears to require them because it paraphrases NFR-02 with the word
*web* dropped. So the register had recorded a gap the spec does not create, while missing one it
does.

**Options, none free.** Fan one search out to each involved document's stream with a shared
correlation id — full chain coverage, no new table, N rows per search, and it only works where
*some* document is involved. Or put web searches in the `audit.run_event` table D-13 recommends
for the compose-gate override, accepting that they are then chained per run rather than per
document. Or register web sources as documents, which needs a ninth `DocumentType` and changes
what `document` means.

**Resolved 2026-08-07, in principle.** D-13 was adopted, and run-scoped events get a
separately chained `audit.run_event` table keyed `run:<id>` — which is where gap-triggered web
searches land. The `web_search` value stays in `audit.event`'s v1 taxonomy but is unreachable
there; removing it is an additive-only taxonomy amendment for WP-H to make when it writes the
emitter. Nothing is fixed in code, because no code emits events yet.

## A-50 (High) — the ban and the breach, two bullets apart

A-6 established the class: an artifact hash that moves when the data has not. `openpyxl` stamping
its own version into `docProps/app.xml` was the first instance. D-14's `repr(grouping_key())`
condition-group sort was the second, found in review and fixed. This is the third, and it was
introduced *by that fix*, in the adjacent bullet.

The candidate bullet pinned projection ordering to `conflict_hitl._ordering_key`, on the correct
reasoning that arrival order violates FR-OUT-06 purity. What it did not check is what that
function is made of:

```python
return (
    repr(candidate.condition.grouping_key()),   # <- the banned expression, verbatim
    repr(candidate.value),
    ...
```

Verified: the first component renders as `(<MeasurementBasis.STC: 'stc'>, None, …)`. So the
bullet forbidding enum `repr()` in hashed order and the bullet mandating it sat six lines apart.

**Why it survived.** Both bullets were written in the same edit, by the same author, in the same
minute — and each is correct in isolation. The candidate bullet reasoned about *arrival versus
content*, which is a different axis from *stable versus implementation-defined encoding*. Getting
one axis right reads as getting the bullet right. The defect only appears when you open
`_ordering_key` and read what it returns, which the bullet's own argument gave no reason to do.

**The fix is one rule instead of two bullets each carrying their own.** A rule stated once, above
both, cannot be satisfied by one and violated by the other.

**Residual, deliberately not fixed here.** `_ordering_key` and `conflict_groupings`
(`conflict_hitl/__init__.py:175`, `sorted(grouped, key=lambda k: repr(k))`) both still order by
`repr()`. That is correct for an in-memory sort and this decision does not change them — but WP-G
must not reach for either when it writes the projection, and converging them on `encode_value()`
before WP-G would remove the trap rather than documenting around it.

---

## A-51 (Medium) — a label for a table that has not landed

**Artifacts:** `src/procurement_agent/services/output/projection.py` vs [clarifications.md D-14](clarifications.md)

D-14's first judgement call is "Policy version and the computed `CellFlag`s both go inside the
projection" (`clarifications.md:877`), and `project_store` does exactly that: `"policy":
encode_value(policy)` (`projection.py:167`) walks every field of `ProjectionPolicy` through the
same closed-world encoder the store itself uses. `confidence_threshold` — a bare `float` today —
is one of those fields, so it is embedded byte-for-byte in the preimage. **There is no live
exploit here:** change `confidence_threshold` and the hash moves, exactly as D-14 requires.

**The gap is in the field beside it.** `policy_version`'s own docstring (`projection.py:120-126`)
does not describe it as redundant with `confidence_threshold` — it ties it to a *different* τ
that does not exist in code yet: "The same decision makes the B.10 tau table **versioned,
append-only data**… That table joins this model when it lands." Task B.10 is
`threshold_for(field_name)`, D-3's field-tiered confidence threshold (`tasks.md:166`) — already
deferred once, for the same reason, at
[A-11](#a-11-medium--output-flagging-assumes-one-global-threshold): "the tiering policy it
depends on is itself an output of the calibration work in WP-B." Nothing in `src/` defines
`threshold_for` today; the only places it is named are `tasks.md`, A-11, and this entry.

**A correction to how this finding first arrived.** The B.10 table is not
`services/conflict_hitl/tolerance.py`. That module implements a different, already-shipped table
for a different contract — D-2's per-field numeric conflict tolerance, task **E.1** by its own
docstring's citation (`tolerance.py:1`) — and it has no version field either, but for an
unrelated reason: it was never required to have one. `services/output/projection.py:47` states
plainly that this module does not import `services.conflict_hitl`, so the D-2 table's values
reach the projection only indirectly, already folded into stored `conflict_status`/`resolution`
data before the projection ever runs — the same pattern `ProjectionPolicy`'s own docstring uses
to justify leaving `assign_severity` out of the policy model entirely (`projection.py:128-131`).
The D-2 table is not part of this gap.

**What is actually undecided.** Once B.10's tiered table exists, `ProjectionPolicy` needs to hold
something that lets a verifier reconstruct exactly which thresholds produced a given projection —
and the docstring's own words, "versioned, append-only data" referenced by a label, describe a
*lookup-by-reference* design, not the *embed-by-value* design `confidence_threshold` already uses
safely. Lookup-by-reference is exactly the shape D-14's decision 1 was ratified to rule out: a
caller-typed `policy_version` string trusted to match the constants actually deployed, with
nothing checking it. Two operators re-tuning τ differently could commit different thresholds
under the same label, and a hash covering only the label, not the table, would not catch it. That
failure mode does not exist today only because the table it would apply to does not exist today
either.

**Severity: Medium, not High.** Nothing in shipped code is wrong: `confidence_threshold` is
safely embedded, and the docstring is honest that the τ table "joins this model when it lands"
rather than claiming coverage it does not have. This is drift between a ratified decision's
stated consequence ("must land with it," `clarifications.md:892`) and an unimplemented feature,
not a live contradiction — the same shape as
[A-49](#a-49-medium--an-event-type-with-nowhere-to-go), which this register also holds at Medium.

**Deferred** — same root cause as A-11 (WP-B calibration work not done). No code or decision yet
says whether the B.10 table will be embedded by value in `ProjectionPolicy`, as `confidence_
threshold` is today, or referenced by `policy_version` alone. That decision should be made when
B.10 lands, not deferred a second time along with it.

---

## A-52 (Medium) — a constraint satisfied by accident, now written down

**Artifacts:** `src/procurement_agent/audit/writer.py`, `src/procurement_agent/orchestrator/__init__.py` vs Kedro `ParallelRunner`

`audit/writer.py`'s advisory-lock design exists for a reason unrelated to process pools: a lock
taken inside a trigger on `audit.event` reads the chain tip too late to serialise concurrent
writers — 8 concurrent writers measured **42** silent forks without it (`audit/writer.py:1-8`,
matching `sql/07_audit_event.sql:20`). The fix `append_event` (`audit/writer.py:174` onward)
settled on is that every writer locks its own stream, reads the tip, builds the envelope and
inserts — all inside its own transaction, independently of any other writer.

That design has a second property nobody asked it for: it also rules out a distinct failure mode,
demonstrated concretely in Kedro (`kedro-org/kedro@c3e1c9c`). `ParallelRunner`'s docstring claims
"This runner will not execute `node` and `dataset` hooks" (`kedro/runner/parallel_runner.py:46-
49`) — read on its own, a reasonable design note. The code contradicts it:
`Task._run_node_synchronization` (`kedro/runner/task.py:115-143`) builds a fresh `PluginManager`
and registers `settings.HOOKS` in every worker process, and `Task.execute` uses it. Hooks *do*
fire — against a fresh, unshared manager per process. A hook that accumulates state across calls
(a running total, a buffer meant to flush once) silently fragments across N processes with
nothing to reconcile the pieces, and nothing detects it — the docstring's flat "will not execute"
is exactly wrong in the direction that hides the danger.

**The constraint this generalises to:** audit emission must happen from wherever a state
transition actually commits — per worker, per transaction — never from a parent-process observer
assuming it saw everything its children did. `append_event`'s design already satisfies this,
because there is no shared accumulator for a worker pool to fragment: every write is
independently transactional by construction, for D-13's reasons, not for this one.

**This is recorded as a constraint, not a defect, because none exists.** `orchestrator.run()`
(`orchestrator/__init__.py:95-97`) raises `NotImplementedError` — there is no process-pool mode to
violate the constraint yet, and no code anywhere routes audit emission through a shared observer.
The substance of this finding — what a future worker-pool mode must do, and the Kedro citation
that makes the failure concrete — is already written into `orchestrator/__init__.py:11-14` and
`docs/architecture.md`'s "Persistence and execution" section, by a separate task running in
parallel with this one. This entry only indexes that work into the register so a future audit of
the register does not have to rediscover it.

**Severity: Medium.** Not High or Critical, because nothing is currently broken and no artifact
contradicts another — `orchestrator/__init__.py` and `docs/architecture.md` already agree with
each other and with the code. Not Low, because the failure mode it heads off — an audit chain
that looks complete and silently is not — is exactly the class of defect D-13's advisory lock
exists to prevent one layer down, and a system built to survive a superuser bypass (Decision 9)
has the least margin for a *silent* gap in what it can attest to.

**Constraint recorded** — no code changed by this entry. Closing action, if any, belongs to
whoever implements a process-pool orchestrator: keep audit emission inside each worker's own
transaction, never behind a shared hook/observer.

---

# Round 7 — Phase 1 integration defects closed on `claude/phase-1-integration` (2026-09-02)

IDs start at A-53 so they do not collide with this branch's existing A-51 / A-52 (policy-version
label; advisory-lock accidental constraint). Findings that PR #39 numbered A-51–A-60 are
remapped here. Unit-gate and `comparable_with(dimensions=)` stay as this branch already shipped
them; the permissive variants from #39 are not carried.

| ID | Severity | Finding | Status |
|---|---|---|---|
| A-53 | **C** | The reducer could not preserve or produce a RESOLVED field, so an idempotent re-run erased a human decision | **Fixed** — [D-16](clarifications.md) |
| A-54 | **H** | One number in three Python types was three answers in `_asserted` | **Fixed** — `_render(..., fold_equal=True)` folds int/float/Decimal, including signed zero |
| A-55 | **H** | Contract `list[str]` fields had no tolerance rule, so identical certification lists in different order were a CRITICAL conflict | **Fixed** — [D-17](clarifications.md) |
| A-56 | **M** | Replaying a resolution from its serialised `dict` form was refused as tampering | **Fixed** — `_as_resolution` coerces before comparing |
| A-57 | **M** | `claim_natural_key` omitted `condition`, so the Sungrow trio collided with "one extractor, two answers" | **Fixed** — unique includes `condition`; `sql/09_claim_natural_key_condition.sql` for live DBs |
| A-58 | **M** | FR-HITL-06's RESOLVED invariant was guarded route by route; `evolve(conflict_status=OPEN)` on a resolved field hid remaining queue hits | **Fixed structurally** — [D-18](clarifications.md): `UnresolvedStatus`, derived RESOLVED, evolve raises |
| A-59 | **L** | `normalize_archive` set private `ZipInfo._compresslevel`, a 3.13-renamed alias | **Fixed** — `writestr(..., compresslevel=...)` |
| A-60 | **M** | In-memory vector `search(allowed_document_ids=None)` fail-opened | **Fixed** — omitting the allow-list returns nothing |
| A-61 | **M** | `FieldClaim._identity` used `repr(grouping_key())`, the encoding D-14 bans | **Fixed** — `encode_value` JSON |
| A-62 | **M** | Naive `resolved_at` / `detected_at` were accepted while `encode_value` refuses them | **Fixed** — `_aware_datetime` on both |

## Consistency checks that passed

- All **32** FR IDs in spec.md match the TRS analysis; none invented, none dropped. (An earlier version of this line said 26, which is the count with FR-OUT-01..06 omitted — see A-27. The sweep itself was correct; the total was not.)
- All 13 `WorkbookTab` members match FR-OUT-02, in order, and the first eight match
  `ComponentCategory` (asserted by test).
- The five `ConflictClass` and five `ResolutionAction` members match FR-HITL-01 and FR-HITL-04
  exactly.
- The eight `DocumentType` members match FR-ING-06.
- The six `ports/` Protocols match NFR-04's six named swap points exactly.
- `SourceTier` has exactly the two tiers the source-of-record rule requires.
- AC-7 and AC-8, added in spec.md beyond the TRS's six, each trace to an existing requirement
  (FR-OUT-06 and NFR-03 respectively) rather than inventing scope.
- No requirement in spec.md lacks a home in either the code, the plan, or tasks.md.

## Coverage gaps that are real but intentional

- **AC-1 and AC-6 have no tests.** They need the ingestion path and a labelled corpus, which
  is WP-B and D-11 work. Recorded as open in the traceability doc rather than papered over.
  (**AC-5 left this group on 2026-08-04** — see A-37. Its store-level invariant is live-tested by
  `test_a_duplicate_content_hash_is_refused`; only the ingest path that would exercise it is
  missing, so it is `partial` rather than `open`.)
- **FR-ING-02 and FR-ING-05 have no home in code yet** beyond `ParserPort`. Correct at
  scaffolding stage; assigned to WP-A.
- **NFR-06 and NFR-07 have no verification.** Both are scale/latency properties that need a
  running system. Assigned to WP-I.

---

# Round 8 — ADR-002 registrations (2026-10-28)

Two places where [ADR-002](../../docs/decisions/ADR-002-deepening-the-claims-to-workbook-path.md)
goes beyond a ratified decision, registered here so neither is chosen silently.

| ID | Severity | Finding | Status |
|---|---|---|---|
| A-63 | **M** | D-19 requires `section` (the heading path) on every parsed element in every format, but D-23's additive extension of `ParsedElement` lists four fields and not `section`, so the ratified interface cannot carry what D-19 promises | **Registered** — ADR-002 decision 7 adds `section` as a fifth optional field in the same PR as D-23's four |
| A-64 | **M** | D-21 puts every request behind an OIDC session; the single-user local workspace has no identity provider to sign in to | **Registered** — ADR-002 decision 11: one fixed local principal, loopback bind only, `Origin` and `Host` checked on every request. Applies to the local workspace only, never to the reviewer surface |
