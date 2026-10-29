# Glossary

The domain words this repository uses for its seams, one meaning each. Where a term is fixed
by a ratified decision, the entry names it; the decision wins if the two ever disagree
([specification authority](docs/architecture.md#specification-authority)). Requirement text is
cited by ID only.

Architecture vocabulary (module, interface, depth, seam, adapter, leverage, locality) is used in
its ordinary deep-module sense and is not redefined here.

## Evidence

**Source document** — one ingested file, identified by the hash of its original bytes
(`SourceDocument`). A `.docx` keeps its own bytes and hash even though it is paged through PDF
(D-19).

**Parsed element** — the unit a parser or OCR adapter returns (`ports.ParsedElement`): a `kind`
from the closed set `heading | body | table | figure`, its text, and where it sits. Extended only
additively (D-23). _Avoid_: block, chunk (a chunk is a retrieval unit).

**Source reference** — the locator a value carries back to its evidence (`SourceRef`): document,
page, section, and for tables the cell. Built from a parsed element in one place (ADR-002
decision 7).

**Source tier** — `system_of_record` or `web_supplement`. A web supplement never overwrites the
system of record (C-2, FR-WEB-03).

## Claims and the canonical value

**Claim** — one extractor's assertion of one field's value for one component, with its condition
and source reference (`FieldClaim`). Claims are append-only; nothing edits one.

**Human claim** — a claim whose `extractor_version` starts with `human:` and which carries the
reviewer's `Resolution`. It is the only way a human decision enters the store (D-16), and its row
links to that resolution by `claim.resolution_id` (D-27).

**Reducer** — the function that turns every claim for a component into its canonical fields
(`services.claims.project`). The only write path from claims to canonical values (ADR-002
decision 2).

**Canonical field** — the reduced value of one field, with unit, condition, provenance,
confidence and conflict status (`CanonicalField`). There is no canonical table; canonical fields
are recomputed from claims (ADR-002 decision 2).

**Condition** — the measurement circumstances a value holds under (temperature, basis, side, …).
Two values under different conditions are not a conflict; they are not comparable (D-1).

## Identity

**Identity keys** — the normalised supplier, model family and nameplate that say which product a
claim is about (`services.identity.identity_keys`, D-4). _Avoid_: grouping key.

**Surrogate ID** — the hash of a component's identity keys. Built by one factory, never assembled
by a caller (ADR-002 decision 5).

## Conflicts and review

**Conflict** — two comparable claims for one field whose values disagree beyond that field's
tolerance. Exactly one judge decides this (ADR-002 decision 1).

**Tolerance** — the per-field band inside which two values are the same answer, never one
global band (D-2). Looked up through `policy_for`, never a bare table (ADR-002 decision 6).

**Conflict queue entry** — the reviewable record of one conflict, with its candidates, severity
and conflict class (`ConflictQueueEntry`). Every field whose status is `open` has one, and
low-confidence and insufficient-evidence fields route to the same queue (FR-HITL-05).

**Conflict status** — `none`, `open`, `insufficient_evidence`, or `resolved`. `resolved` is
derived from a settling human claim, never stored by hand (D-18).

**Resolution** — a reviewer's decision on a queue entry: one of five actions (`ResolutionAction`).

**Settling action** — `select_value`, `enter_override` or `keep_system_of_record`. At most one per
queue entry. `defer` and `request_more_web_search` are recorded but settle nothing (ADR-002
decision 2).

## Output

**Compose gate** — the check that refuses to build a workbook while an unresolved entry at or
above the threshold severity exists (`orchestrator.compose_gate_blocks`). A blocked compose
returns the blocking entries as a manifest; it can be bypassed only by an audited
`compose_override` (ADR-002 decision 3).

**Projection** — the deterministic, hashable view of the store that a workbook is rendered from
(`services.output.projection`, D-14, D-29). Same store and policy, same bytes.

**Workbook** — the 13-tab XLSX rendered from a projection (`WorkbookTab`). Read-only output;
resolution happens in the application, not in Excel (D-10).

**Open Items** — the workbook tab listing what still needs a human, read from the conflict queue
(ADR-002 decision 4).

## Local workspace

**Local workspace** — the single-user, loopback-only browser surface for reading documents and
exporting a workbook without an LLM endpoint. Server-rendered per D-21, with one fixed local
principal in place of OIDC ([A-64](specs/001-procurement-agent/analysis.md)).
