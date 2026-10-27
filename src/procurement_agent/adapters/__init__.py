"""Concrete implementations of the eight `ports` Protocols — and the layout call.

`docs/development.md` records the hazard this package resolves: there was no
adapter layout convention, so "the first one decides the layout for everyone
after it", and moving them afterwards is the expensive version of the decision.
ADR-001 decision 2 answers it by landing the conformance suite and one in-memory
reference per port *before* any real adapter, so the first vendor adapter arrives
to a contract rather than defining one. The layout below is that decision; it is
written here rather than in a document because this is the file the next adapter
author opens.

## The shape: one package per port, one module per backend

    adapters/<port>/<backend>.py        e.g. adapters/vector_store/pgvector.py
    adapters/<port>/memory.py           the in-memory reference, always

**Why port-first rather than backend-first.** The alternative — `adapters/docling/`
holding that vendor's parser and OCR side by side, one package per optional extra
— groups by dependency, and it is the better fit for `pyproject.toml`, where the
extras are per vendor. It was rejected on two grounds. NFR-04's unit is the swap
point, not the vendor: the question asked of this tree is "what can I put behind
`VectorStorePort`", and port-first makes that a directory listing while
backend-first makes it a search. And `ports/__init__.py` records that `ParserPort`
is expected to have *several* implementations selected by content signature
(FR-ING-01), so the siblings a router chooses between are the thing that most
needs to sit together. A vendor spanning two ports appears in two places; those
are two separate classes with two separate contracts, and both guard the same
extra, so the cost is a duplicated import guard rather than duplicated logic.

**Why not beside the owning service**, which `docs/development.md` floats as "the
obvious candidate". Three of the eight ports have no single owning service:
`VectorStorePort` is written by `services.indexing` and read by
`services.retrieval`, `EmbedderPort` likewise, and `RerankerPort` belongs to a
stage `services.retrieval` only coordinates. Placing an adapter under one of them
would make the other import it sideways. The stronger objection is layering:
`services` is the pure-policy layer, and a vendor SDK import inside it means
`import procurement_agent.services.retrieval` can fail in the lean
`uv sync --extra dev` environment that CI's `checks` job uses. Keeping every
vendor import under `adapters/` keeps that failure at the edge, where a caller
chose the backend.

**Why the in-memory references live here and not under `tests/`.** They are the
executable proof that each Protocol is satisfiable, so they are an artifact of
the port rather than of the suite - and a suite that tests code living inside
itself proves less than one testing code it imports. They are also useful to
other teams: `specs/001-procurement-agent/phase-1-execution.md` states that a
track's deliverable is "the artifact the next track consumes exists and is
asserted", and a service test that needs a vector store should use
`InMemoryVectorStore` rather than mint a mock whose behaviour nobody agreed.
Being importable from `src/` is what makes that possible. They are named
`InMemory*`, take no configuration, and
`test_the_references_import_nothing_outside_the_standard_library` holds them to
having no dependencies at all.

## Rules for a real adapter

1. It goes in `adapters/<port>/<backend>.py` and imports its vendor SDK at module
   scope. `registry.py` must import it **lazily, inside the factory**, so that
   collecting the conformance suite never imports an extra that is not installed.
2. It registers an `AdapterEntry` with `is_reference=False` and a complete
   capability declaration - see `capabilities.py` for what "complete" is checked
   to mean.
3. It supplies the port's samples object, which is the machine-readable half of
   `docs/development.md`'s "add sanitized success, empty, malformed, and timeout
   fixtures": the shared contracts cannot construct a document only that backend
   can read, so the adapter hands them one.

## Two conventions this package supplies because `ports` under-specifies

`PARSED_ELEMENT_KINDS` and `ChunkMetadata` (in `vector_store/`) are stated in
Protocol docstrings and absent from the Protocol types. They live here, on the
adapter side of the boundary. `ParsedElement` itself is no longer the three-field
shape: Track 0 added `bbox`, `table`, `page_quality` and `role` (P2-C1).
"""

from __future__ import annotations

from .capabilities import (
    CAPABILITIES_BY_PORT,
    UNXFAILABLE,
    AbsenceKind,
    AdapterEntry,
    Capability,
    DeclaredAbsence,
    not_applicable,
    unimplemented,
)
from .parsed_element import PARSED_ELEMENT_KINDS, PARSED_ELEMENT_ROLES, TextElement
from .registry import REGISTERED_ADAPTERS, adapters_for

__all__ = [
    "CAPABILITIES_BY_PORT",
    "PARSED_ELEMENT_KINDS",
    "PARSED_ELEMENT_ROLES",
    "REGISTERED_ADAPTERS",
    "UNXFAILABLE",
    "AbsenceKind",
    "AdapterEntry",
    "Capability",
    "DeclaredAbsence",
    "TextElement",
    "adapters_for",
    "not_applicable",
    "unimplemented",
]
