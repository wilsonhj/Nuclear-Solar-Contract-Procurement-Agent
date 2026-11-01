# Procurement Agent Domain Glossary

Procurement Agent is an evidence-processing system that extracts, compares, and projects supplier technical and commercial claims across renewable-energy equipment categories into a deterministic comparison artifact while routing disagreements to human review.

## Evidence & Provenance

**Source Document**:
An immutable, content-addressed file (such as a contract, purchase order, datasheet, test report, or public certification listing) ingested as primary or supplementary evidence.
_Avoid_: input file, attachment, blob

**Source Tier**:
The authority level of an evidence source, distinguishing binding **System of Record** documents (contracts, purchase orders, supplier datasheets) from supplementary **Web Supplement** data (public registries, web lookups).
_Avoid_: trust level, priority score

**Provenance Reference**:
The mandatory citation tying every extracted or resolved value back to its origin document (or public URL), page, section span, and extractor identity.
_Avoid_: citation, footnote, link

**Access Restriction**:
A confidentiality marker on a **Source Document** (and inherited by all derived records) indicating that the content contains commercially sensitive terms (such as pricing, purchase orders, or contracts) visible only to cleared principals.
_Avoid_: permission flag, private mode

## Claims & Canonical State

**Component Instance**:
A specific supplier product offering identified by equipment category, canonical supplier name, normalized model number, and optional nameplate bin.
_Avoid_: SKU, product row, item

**Condition**:
The operating regime or rating basis under which a technical parameter holds (such as STC vs. NOCT basis, ambient temperature, cooling stage, duration, or standards regime).
_Avoid_: qualifier, context, metadata tag

**Declared Band**:
A manufacturer-stated tolerance interval printed on a **Source Document** (such as a `0 ~ +5 W` power bin tolerance) that governs whether two nominal figures agree.
_Avoid_: margin of error, fuzz factor

**Field Claim**:
An immutable assertion made by an extractor (or a human reviewer) that a **Component Instance** holds a specific value and unit for a contract parameter under a stated **Condition** in a **Source Document**.
_Avoid_: extraction row, raw value, cell value

**Canonical Field**:
The reduced representation of a parameter for one **Condition** group on a **Component Instance**, carrying the preferred value, **Provenance Reference**, confidence score, conflict status, and any human **Resolution**.
_Avoid_: final value, merged attribute

**Claim Reduction**:
The deterministic projection of an append-only set of **Field Claims** into **Canonical Fields** and **Conflict Queue Entries**, enforcing that **Web Supplement** claims never autonomously overwrite **System of Record** claims.
_Avoid_: deduplication, merging, arbitration

## Disagreement & Human Review

**Tolerance Policy**:
The per-parameter domain rule (exact, absolute, relative, one-sided guarantee, declared band, set-equal, or never-compare) combined with printed-precision rounding floors used to judge whether two condition-compatible candidates disagree.
_Avoid_: threshold, global epsilon

**Conflict Candidate**:
One competing side of a parameter disagreement, preserving its value, unit, **Condition**, verbatim source text, **Source Tier**, **Provenance Reference**, and confidence.
_Avoid_: option, duplicate

**Conflict Queue Entry**:
A pairwise disagreement between two condition-compatible **Conflict Candidates** for the same parameter on a **Component Instance**, classified by conflict cause and assigned a review **Severity**.
_Avoid_: ticket, alert, diff

**Severity**:
A discrete criticality classification (`CRITICAL`, `HIGH`, `MEDIUM`, `LOW`, `INFO`) governing review priority and whether an unresolved **Conflict Queue Entry** blocks workbook composition.
_Avoid_: risk score, confidence weight

**Resolution**:
An immutable, attributed human decision (`SELECT_VALUE`, `KEEP_SYSTEM_OF_RECORD`, `ENTER_OVERRIDE`, `REQUEST_MORE_WEB_SEARCH`, or `DEFER`) with rationale and before/after values that settles or re-queues a **Conflict Queue Entry**.
_Avoid_: edit, hotfix, cell override

## Composition & Audit

**Compose Gate**:
The policy check before workbook generation that blocks composition whenever any unresolved **Conflict Queue Entry** exceeds the configured **Severity** threshold, unless an explicit, audited incomplete-composition override is recorded.
_Avoid_: build check, export filter

**Canonical Projection**:
The deterministic, content-hashed JSON representation of all **Component Instances**, **Conflict Queue Entries**, **Source Documents**, and active policy thresholds, serving as the artifact of record from which the spreadsheet is rendered.
_Avoid_: dump, snapshot, cache

**Audit Stream**:
A per-document (or per-run) cryptographic hash chain of immutable lifecycle events (`document_ingested`, `extraction`, `conflict_detected`, `resolution`, `compose`, `attempt_failed`) serialized in canonical JSON.
_Avoid_: application log, trace
