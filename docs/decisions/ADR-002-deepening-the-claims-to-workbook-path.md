# ADR-002 — Deepening the claims-to-workbook path

**Status:** Proposed · **Date:** 2026-10-28 ·
**Rank:** an ADR, so below `plan.md` and every ratified clarification, above `tasks.md`
([architecture.md](../architecture.md#specification-authority)).

Nothing here amends a plan decision or a clarification. Where a decision below touches a frozen
interface it does so only as far as a ratified clarification already authorises — D-23 for
`ParsedElement`, D-27 for the claim–resolution link — which keeps ADR-001 decision 8's ladder:
the contract change comes last and is not invented here. Two places where this ADR departs from
a ratified decision are registered rather than chosen silently: [A-63 and
A-64](../../specs/001-procurement-agent/analysis.md#round-8--adr-002-registrations-2026-10-28).

Terms are defined in [GLOSSARY.md](../../GLOSSARY.md).

> **Partly overtaken by #47.** The context below describes `b295651`. PR #47 ("Make claim
> reduction, confidentiality, and audit inserts agree") routes projection and conflict detection
> through `services.claims.reduce_field` and records a review as a `human:` claim, which
> delivers most of decisions 1 and 2. Each of those decisions now says what #47 did and what is
> left. This ADR is stacked on #47 and assumes it merges first.

---

## Context

An architecture review of `main` at `b295651` looked for modules whose interface is nearly as
wide as their implementation, and for behaviour that is decided in more than one place. The path
from claims to the workbook was where both concentrated:

- **Two judges for one question.** Whether two claims disagree is decided by `_status_for` in
  `services/claims` (exact comparison of the asserted value) and again by `_detect_conflicts` in
  `services/vertical_slice.py` (its own pairing), while the tolerance-aware judge
  `ConflictVerdict` in `services/conflict_hitl` decides neither. Observed: PV nameplate 650 Wp
  against 650.5 Wp, inside the ±1 Wp band in `conflict_hitl/tolerance.py`, is stored `open` with
  no queue entry — a field FR-HITL-05 says must never be silently left.
- **A decision that does not survive a re-run.** `review_conflict` writes the reviewer's
  outcome with `CanonicalField.evolve` and records no human claim, so the reducer cannot
  reproduce it. D-16 already settled the shape (a decision is a claim); the slice predates it.
- **A PV-only pipeline.** `run_sanitized_pv_csv` fixes its columns and category; the reduction
  it performs is not reachable for the seven other categories (D-9).
- **The workbook recomputes what the projection already knows.** `write_workbook` takes
  components and a threshold and calls `flags_for` twice
  (`services/output/__init__.py:208`, and `:329` with a threshold of `0.0`), so the tab a
  reviewer reads and the hash D-14 fixes are derived separately.
- **Identity assembled by callers.** `surrogate_id` is declared in `schema` and filled from
  outside it; a caller can build a component with any key.
- **Policy as bare tables.** Tolerance, units and field specs are looked up by string key in
  several modules, and `W` / `Wp` are compared as spellings.
- **A parsed element that cannot carry its location.** `ParsedElement` has `kind`, `text`,
  `page`; `adapters/parsed_element.py` records three gaps D-23 and D-19 already decided how to
  close.
- **Transactions spread across callers.** `persist_vertical_slice` and the audit writer each
  manage their own commit; `transactional_audit.write_and_append_event` has no caller in `src/`.

A local browser workspace built on an older base (`claude/local-workspace-d50f653`, not merged)
was run against these paths. It confirmed the gate holds at the API (409 with the field names)
and exposed three defects carried into decision 11: cross-origin `POST` and `DELETE` were
accepted (201, 204), a foreign `Host` was served (200), and a blocked batch offered no in-app
way to resolve it.

---

## Decisions

Decisions 1–9 are numbered for the review candidate they settle.

### 1 — One judge decides whether claims conflict

**Chosen:** `conflict_hitl`'s verdict is the only predicate for "these two claims disagree".
`_status_for` and the detector both call it; neither compares values itself. The detector sees
the whole component, not one intake's documents. Invariant: every field whose status is `open`
has a queue entry, and no queue entry exists for a pair the judge calls equal.

**Delivered by #47:** `reduce_field` builds the queue from `values_conflict` over condition-
compatible pairs, and the slice uses it. Run against #47, 650 Wp against 650.5 Wp is `none` with
an empty queue, and 650 against 655 is `open` with one HIGH entry. `_status_for` in `project` now
asks `values_conflict` for comparable populated values. **Left:** align the remaining status
paths with the judge and the review queue. A missing-versus-present group is still `open` while
the judge treats it as a gap and produces no conflict entry; differing answers under
`NEVER_COMPARE` also remain `open` without an entry. The slice still reduces only the claims
whose document and field are in the current intake (`vertical_slice.py`, the `relevant` filter),
not the whole component.

### 2 — The reducer is the only write path; a review is a human claim

**Chosen:** reduction returns canonical fields and does not persist; persistence is a separate
step. The reducer runs over every claim in the store for the component, not one intake's. A
review is recorded as a human claim per D-16 and linked by `claim.resolution_id` per D-27, whose
`sql/10_claim_resolution_link.sql` lands in the same PR; the resolution row is kept. There is no
canonical-value table — canonical fields are always recomputed.

A partial unique index on `resolution(entry_id)` restricted to settling actions
(`select_value`, `enter_override`, `keep_system_of_record`) enforces one settlement per entry.
The persistence path also preserves the eight-column lookup's full-payload comparison in
`vertical_slice.py`. An exact retry returns the existing resolution ID without appending a
resolution or audit event; a competing payload for the same settled entry is refused. If the
index rejects a concurrent insert, the caller re-reads and compares the committed payload before
classifying it as a retry or a competing decision. `defer` and `request_more_web_search` stay
append-only.

**Delivered by #47:** a review is a `human:` claim; after review the field is `resolved`, a
re-run over the stored claims keeps it, and a second resolution of the same entry is refused in
Python. **Left:** D-27's `sql/10_claim_resolution_link.sql`, the settling-action index (the refusal
is not yet a database constraint), and reduction over the whole store (decision 1).

### 3 — A category-generic reduction stage, and a compose gate that returns

**Chosen:** one reduction stage for every category, driven by the field registry rather than
fixed CSV columns. A blocked compose returns a blocked outcome carrying the full completeness
manifest required by plan decision 2. It lists every unresolved conflict, including those below
the gating threshold, every `review_required` field and every `PARSE_FAILED` page, and records
whether the gate blocked at that threshold (Story 6, G.8). The outcome identifies the blocking
entries separately. The only bypass is an audited `compose_override` event.

### 4 — The workbook renders the projection

**Chosen:** `write_workbook` takes the projection (D-14, D-29), not components and a threshold.
Layout lives in a render model built from the projection; tabs 8a/8b follow later. Open Items
renders both unresolved conflict entries and `review_required` fields derived from the
projection's flags, including low-confidence and insufficient-evidence fields with no pairwise
conflict (FR-HITL-05; Story 6, G.7).

Implement D-29's `policy.thresholds` map before the renderer refactor. Its one structural
re-baseline adds that key to the two-supplier fixture and updates the golden digest, preserving
the field rows and `policy_version` as D-29 requires. The renderer refactor then preserves that
updated digest.

### 5 — Identity is derived at construction

**Chosen:** components are built through one factory that derives identity keys and
`surrogate_id` (D-4). PV only for now; `None` allowed where identity cannot be derived.
Fixture keys that were typed rather than derived are regenerated and the fixtures re-baselined.

The schema half — making `surrogate_id` required or computed — is a separate, later PR, opened
only once the factory is the only constructor in `src/`. That PR checks the projection golden
digest (`tests/fixtures/workbooks/two-supplier-pv-store.canonical-bytes.sha256`) and bumps
`PROJECTION_VERSION` in the same change if it moves.

### 6 — Policy is looked up, and units are one module

**Chosen:** the bare-key tables sit behind `policy_for(key, category)`. Units move to one module
in `schema`, where `W` and `Wp` are different units, not spellings. Values are checked at commit
or intake, not at render.

### 7 — `ParsedElement` grows as D-23 ratified, plus D-19's `section`

**Chosen:** add D-23's four optional fields (`bbox`, `table`, `page_quality`, `role`); `page`
stays and `kind` keeps its four values. Add `section` (the heading path D-19 requires on every
element), which D-23 does not list — registered as A-63. Spreadsheet content is `kind="table"`
with `TableData`; the cell address goes in `SourceRef`, built from an element in one function.
Once `bbox` exists, `BOUNDING_BOXES` becomes an ordinary capability and
`UNEXPRESSIBLE_BOUNDING_BOXES` and its pinning test are deleted. The `__annotations__` pin is
updated in the same PR, as D-23 requires.

### 8 — One canonical-text function

**Chosen:** the text normalisations now repeated across modules become one function. Unify the
code, not the bytes: the projection golden digest must not move.

### 9 — A unit of work owns the transaction

**Chosen:** services open one unit of work and commit it; repositories and the audit writer
take the connection rather than committing. Row locks are taken in sorted key order.

### 10 — One PR per decision, in dependency order

**Chosen:** each decision is its own PR, built in its own worktree, with a refactor commit
before any behaviour-fix commit. Order:

1. #47, then this ADR and the glossary;
2. in parallel, because their files are disjoint: 7, 8, 6, and 5's code half;
3. in sequence, because each edits `vertical_slice.py` and `services/claims`: what #47 left of
   1 → 3 → what #47 left of 2 → 9;
4. 4, after 1 and 3;
5. 5's schema half;
6. the local workspace port (decision 11).

### 11 — The local workspace is ported, not merged

**Chosen:** the workspace is rebuilt on `main` after decisions 1, 3 and 7, replacing its own
conflict finder, identity code and renderer with the modules above. Its readers become parser
adapters and its extraction uses `spec_for`. In the port:

- the UI is server-rendered with Jinja2 and HTMX per D-21, on the existing FastAPI routes;
  the slice's `app.js` is not carried;
- a single fixed local principal stands in for OIDC, the server binds to loopback only, and
  every request is checked for `Origin` and `Host` — registered as A-64;
- XLSX input is bounded against decompression bombs before it is opened;
- the dev dependency `httpx2` becomes `httpx`;
- conflicts are resolved in the application, never in Excel (D-10);
- dependencies follow `main`'s `pyproject.toml`; the slice's PyMuPDF entry is dropped.

The licence gate is CONTRIBUTING's — Apache-2.0, MIT and BSD. The `pyproject.toml` comment that
said "Apache-2.0 and MIT only" is corrected in this PR.

---

## Rejected alternatives

- **Replacing `page` with a typed locator union.** Contradicts D-23's additive extension.
- **Locating Word content by paragraph index.** D-19 pages Word through PDF.
- **Keeping the slice's static JavaScript UI.** Contradicts D-21.
- **`UNIQUE (entry_id, resolved_by, resolved_at)` for idempotency.** D-27 rejects time-based
  linking because decisions collide within a second; the settling-action index does not depend
  on time.
- **A canonical-value table.** A second copy of what the reducer derives, and the reason a
  review was lost on re-run.
- **One global tolerance band.** D-2.

---

## Verification

- **Decision 1:** a test that 650 Wp against 650.5 Wp is neither `open` nor queued, and a
  property test that every `open` field has a queue entry and every pairwise conflict entry
  comes from the judge. Cover missing-versus-present values and `NEVER_COMPARE` separately so
  gaps and incomparable evidence remain visible without fabricating a value conflict.
- **Decision 2:** reviewing, re-running the reducer over the same store, and re-reviewing yields
  the same canonical field and one resolution row; a second settling resolution is refused by
  the index. An exact persistence retry returns the same resolution ID and emits no new audit
  event; a competing payload is refused, including when two writers race.
- **Decision 3:** a blocked compose returns the full manifest, including below-threshold
  conflicts, `review_required` fields and `PARSE_FAILED` pages, with the blockers identified
  separately; an override without an audit event is impossible to construct.
- **Decision 4:** D-29's one-key fixture re-baseline preserves field rows and `policy_version`;
  the renderer refactor preserves the updated golden digest. Open Items lists unresolved
  conflicts and every `review_required` field, including a lone low-confidence claim and an
  insufficient-evidence field with no pairwise conflict.
- **Decisions 5, 8:** the golden digest does not move unless `PROJECTION_VERSION` does.
- **Decision 7:** the port contract suite passes with the new annotations pin.
- **Decision 11:** run the workspace — a cross-origin `POST` and `DELETE` and a foreign `Host`
  are refused, and a blocked batch can be resolved and exported without leaving the app.
