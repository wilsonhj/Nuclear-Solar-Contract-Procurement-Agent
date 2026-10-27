"""The capability vocabulary, and the declaration an adapter makes about it.

ADR-001 decision 2 adopts predict-rlm's runtime-contract pattern: a backend
declares a `frozenset` of capabilities plus a mapping of the ones it lacks with
reasons, and the contract tests gate on the declaration. The property that makes
it worth the machinery is one sentence of the ADR - an unimplemented capability
is **"declared, not silently absent"**. Everything below exists to make the
silent version impossible rather than merely discouraged.

**Two adaptations of the borrowed pattern, both deliberate.**

*The mapping is keyed on capabilities, not on contract names.* predict-rlm's
`xfail_contracts` names individual tests; that couples every declaration to the
test-function names of the day, so renaming a test silently un-excuses an
adapter. Keying on the capability means the declaration answers a question about
the adapter ("does it filter at retrieval time?") rather than about the suite,
and `test_every_adapter_accounts_for_every_capability_of_its_port` can check the
answer set is complete - which is not checkable against an open-ended set of test
names.

*An absence carries a kind as well as a reason.* `NOT_APPLICABLE` and
`UNIMPLEMENTED` are different facts: a delimited-grid parser has no pages to
report, while an OCR engine without table recovery has tables it is failing to
recover. `docs/development.md` forbids the same collapse one layer down - do not
turn "not compared" into "no conflict" - and for the same reason: a reader
deciding what to build next cannot act on a matrix that renders "cannot apply"
and "does not yet" identically. The kind is a claim to that reader; it does not
change how the suite runs the contract, and `AbsenceKind` says why.

**`UNXFAILABLE` is where the pattern stops.** Two of these are not features an
adapter may lack with an apology; they are admission criteria. See below.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..ports import (
    EmbedderPort,
    LexicalSearchPort,
    LLMPort,
    OCRPort,
    ParserPort,
    RerankerPort,
    VectorStorePort,
    WebSearchPort,
)

__all__ = [
    "CAPABILITIES_BY_PORT",
    "UNXFAILABLE",
    "AbsenceKind",
    "AdapterEntry",
    "Capability",
    "DeclaredAbsence",
    "not_applicable",
    "unimplemented",
]


class Capability(StrEnum):
    """Axes on which two implementations of the same port legitimately differ.

    A member earns its place by being (a) something real backends actually vary
    on, (b) traceable to a requirement or a recorded decision, and (c) observable
    through the Protocol alone. The third rules more out than it looks:
    FR-ING-04's bounding boxes used to fail it; they now sit on `ParsedElement.bbox`
    (P2-C1) and are an ordinary Protocol member rather than a capability.

    * `DETERMINISTIC_OUTPUT` - the same input yields the same output, across
      instances. AC-7's byte-identical regeneration is downstream of every stage
      holding this; a sampling temperature is the honest reason to lack it.
    * `PAGE_NUMBERS` - FR-ING-03. Elements carry the page they came from. A
      spreadsheet or a delimited grid has no pagination, which is the archetypal
      `NOT_APPLICABLE`.
    * `TABLE_STRUCTURE` - FR-ING-02/05. A table survives as a table rather than
      being flattened into prose, which is how every canonical parameter in the
      frozen contract is actually printed.
    * `SEMANTIC_SIMILARITY` - FR-RAG-02/03. Meaning, not tokens: a paraphrase
      sharing no words outranks a decoy sharing several. This is the axis that
      separates a model from a hash, and no reference can hold it.
    * `METADATA_FILTERING` - FR-RAG-02. Category, supplier and tier filters
      applied inside the search.
    * `ACCESS_FILTERING` - NFR-03 and AC-8. `allowed_document_ids` enforced at
      retrieval, not after it. Unxfailable.
    * `INCREMENTAL_UPDATE` - FR-RAG-05. Add, update and delete by stable ID with
      no full re-index.
    * `EXHAUSTIVE_RECALL` - plan Decision 3a. A filtered top-k returns k when k
      rows qualify. The plan chose exact search because pgvector was *measured*
      silently under-returning here; an approximate index must declare this
      absent, and the reason string is where a reviewer learns recall is
      approximate before shipping it.
    * `INSUFFICIENT_EVIDENCE` - FR-RAG-04. The model can return "I do not know"
      instead of a fabricated value. Unxfailable.
    * `SCHEMA_CONSTRAINED` - FR-ING-07 and plan Decision 7. Output conforms to
      the requested JSON schema, keys and types.
    * `TRIGRAM_TOLERANCE` - Decision 3b / D-25. A lexical search matches
      hyphen/space variants of the same part number (`JKM610N-66HL4M-V` ~
      `JKM610N 66HL4M V`).
    * `RATE_LIMITED` - a networked search backend honours a producer-side
      quota. Observable through `search()`: a burst beyond quota raises
      `WebSearchRateLimitExceeded` (`test_a_burst_beyond_quota_is_refused`).
      An in-memory fixture map has no quota to honour.
    """

    DETERMINISTIC_OUTPUT = "deterministic_output"
    PAGE_NUMBERS = "page_numbers"
    TABLE_STRUCTURE = "table_structure"
    SEMANTIC_SIMILARITY = "semantic_similarity"
    METADATA_FILTERING = "metadata_filtering"
    ACCESS_FILTERING = "access_filtering"
    INCREMENTAL_UPDATE = "incremental_update"
    EXHAUSTIVE_RECALL = "exhaustive_recall"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    SCHEMA_CONSTRAINED = "schema_constrained"
    TRIGRAM_TOLERANCE = "trigram_tolerance"
    RATE_LIMITED = "rate_limited"


class AbsenceKind(StrEnum):
    """Why a capability is missing — a claim to a reader, not a change of gate.

    `NOT_APPLICABLE` - the capability is meaningless for this backend. A
    delimited-grid parser has no pages to report; there is no work item here and
    there never will be.

    `UNIMPLEMENTED` - the capability is meaningful and missing. An OCR engine
    without table recovery has tables it is failing to recover, and someone could
    close that.

    **Both run their contract under `xfail(strict=True)`**, and the kind is
    carried in the reason. Skipping the first kind was the first design and was
    wrong: a skip does not execute the contract, so an inapplicability claim with
    a plausible reason would quietly delete a test - the "silently absent" state
    ADR-001 adopted this pattern to prevent. Strict, so an adapter that has
    outgrown either declaration goes red instead of passing unnoticed; that is
    the direction a plain `xfail` never reports, and the same asymmetry
    `services/conflict_hitl/severity.py` records closing for its own table.

    What no gate can check is a *misclassified* kind, because inapplicability is
    not falsifiable from outside the backend. That is the honest limit of the
    mechanism, and it is why the reason string is held to being usable.
    """

    NOT_APPLICABLE = "not_applicable"
    UNIMPLEMENTED = "unimplemented"


@dataclass(frozen=True)
class DeclaredAbsence:
    """A capability this adapter does not have, and why."""

    kind: AbsenceKind
    reason: str


def not_applicable(reason: str) -> DeclaredAbsence:
    return DeclaredAbsence(AbsenceKind.NOT_APPLICABLE, reason)


def unimplemented(reason: str) -> DeclaredAbsence:
    return DeclaredAbsence(AbsenceKind.UNIMPLEMENTED, reason)


@dataclass(frozen=True)
class AdapterEntry:
    """One row of the conformance matrix.

    The declaration sits *beside* the adapter rather than on it, as a class
    attribute would. That is what keeps `ports`' Protocols structural: an object
    that already has the right methods - a vendor client, or a wrapper written
    outside this repository - can be entered in the matrix without being
    modified, which is the whole reason `docs/development.md` says "an adapter
    may wrap an SDK without subclassing a project base class". A registry is also
    the only discovery mechanism that does not require importing every adapter
    module to find out what exists, and importing them all is exactly what a lean
    `uv sync --extra dev` environment cannot do.

    `factory` is a callable rather than an instance so that construction - and,
    for a vendor adapter, the import that precedes it - happens at test time and
    only for the entries actually collected.

    `port` is typed `type[Any]` because mypy refuses a Protocol class as the
    value of a `type[SomeProtocol]` annotation; the runtime check that the
    factory's product really satisfies it is
    `test_every_adapter_satisfies_its_port_at_runtime`, and the static half is
    each reference's own `_conforms()` helper.

    `samples` carries the port-specific fixtures the shared contracts cannot
    invent - a document only this parser can read, the vector width this store
    was configured for. `None` for the three ports whose inputs are ordinary
    Python values the suite can build itself.
    """

    name: str
    port: type[Any]
    factory: Callable[[], object]
    capabilities: frozenset[Capability]
    absences: Mapping[Capability, DeclaredAbsence] = field(default_factory=dict)
    is_reference: bool = False
    samples: object = None


CAPABILITIES_BY_PORT: Mapping[type[Any], frozenset[Capability]] = {
    ParserPort: frozenset(
        {
            Capability.DETERMINISTIC_OUTPUT,
            Capability.PAGE_NUMBERS,
            Capability.TABLE_STRUCTURE,
        }
    ),
    OCRPort: frozenset(
        {
            Capability.DETERMINISTIC_OUTPUT,
            Capability.PAGE_NUMBERS,
            Capability.TABLE_STRUCTURE,
        }
    ),
    EmbedderPort: frozenset(
        {
            Capability.DETERMINISTIC_OUTPUT,
            Capability.SEMANTIC_SIMILARITY,
        }
    ),
    VectorStorePort: frozenset(
        {
            Capability.DETERMINISTIC_OUTPUT,
            Capability.METADATA_FILTERING,
            Capability.ACCESS_FILTERING,
            Capability.INCREMENTAL_UPDATE,
            Capability.EXHAUSTIVE_RECALL,
        }
    ),
    RerankerPort: frozenset(
        {
            Capability.DETERMINISTIC_OUTPUT,
            Capability.SEMANTIC_SIMILARITY,
        }
    ),
    LLMPort: frozenset(
        {
            Capability.DETERMINISTIC_OUTPUT,
            Capability.INSUFFICIENT_EVIDENCE,
            Capability.SCHEMA_CONSTRAINED,
        }
    ),
    LexicalSearchPort: frozenset(
        {
            Capability.DETERMINISTIC_OUTPUT,
            Capability.METADATA_FILTERING,
            Capability.ACCESS_FILTERING,
            Capability.TRIGRAM_TOLERANCE,
        }
    ),
    WebSearchPort: frozenset(
        {
            Capability.DETERMINISTIC_OUTPUT,
            Capability.RATE_LIMITED,
        }
    ),
}
"""Which capabilities each port is answerable for.

Not every capability applies to every port, and listing one that does not is how
a matrix acquires rows nobody can fill honestly. The set is exact in both
directions: an adapter must account for all of its port's capabilities and none
of any other's, and every member of `Capability` must appear here somewhere -
unclaimed vocabulary reads as coverage while contributing none, which is the
defect `tolerance.FIELD_TOLERANCES` shipped with nineteen invented keys.
"""


UNXFAILABLE: frozenset[Capability] = frozenset(
    {
        Capability.ACCESS_FILTERING,
        Capability.INSUFFICIENT_EVIDENCE,
    }
)
"""Capabilities that may not be declared absent, with or without a reason.

Both are refusals rather than features, and a refusal that an adapter is excused
from is not a refusal. `ACCESS_FILTERING` is NFR-03 and AC-8: a store that cannot
enforce `allowed_document_ids` inside the search lets an uncleared user influence
a retrieved result, and `architecture.md` invariant 8 says filtering afterwards
is already too late. `INSUFFICIENT_EVIDENCE` is FR-RAG-04: a model that cannot
decline produces values with no source behind them, which NFR-01 forbids and
which nothing downstream can distinguish from a real extraction.

An adapter that lacks either is not an adapter with a gap. It is rejected -
`docs/development.md`: "Do not weaken a Protocol merely because one provider
omits required evidence. Add a translation layer or reject the adapter output."
"""
