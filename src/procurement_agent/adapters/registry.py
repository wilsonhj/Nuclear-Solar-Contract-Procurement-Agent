"""Which adapters the conformance suite runs, and what each of them claims.

**Everything in here today is a reference.** A green conformance run therefore
proves the eight Protocols are *expressible* - that some object satisfies each of
them, and that the contracts are consistent enough to be satisfied at all. It is
not evidence about Docling, vLLM, pgvector or any other backend, none of which
exist in this repository. NFR-04 plus D-25/P2-C4 ask that the eight swap points
be swappable; nothing has been swapped.
`test_every_registered_adapter_is_an_in_memory_reference`
is that sentence in executable form, so it goes red rather than stale when the
first real adapter lands.

**Adding a real adapter.** Import it *inside* its factory, never at module scope:
this module is imported to collect the suite, and a module-scope
`import docling` would make collection fail in the lean `uv sync --extra dev`
environment CI's `checks` job uses. That single rule is what lets one registry
hold adapters whose dependencies are mutually exclusive.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..ports import (
    EmbedderPort,
    LexicalSearchPort,
    LLMPort,
    OCRPort,
    ParserPort,
    RerankerPort,
    VectorStorePort,
    WebHit,
    WebSearchPort,
)
from .capabilities import AdapterEntry, Capability, not_applicable, unimplemented
from .embedder.memory import InMemoryEmbedder
from .lexical_store import LexicalStoreSamples
from .lexical_store.memory import InMemoryLexicalStore
from .llm.memory import InMemoryLLM
from .ocr import OCRSamples
from .ocr.memory import InMemoryOCR
from .parser import ParserSamples
from .parser.memory import InMemoryParser
from .reranker.memory import InMemoryReranker
from .vector_store import VectorStoreSamples
from .vector_store.memory import InMemoryVectorStore
from .web_search import WebSearchSamples
from .web_search.memory import InMemoryWebSearch

__all__ = ["REGISTERED_ADAPTERS", "adapters_for"]


_NO_SEMANTICS = (
    "hashes tokens into buckets, so a paraphrase sharing no words with the query "
    "scores near zero while a decoy sharing two scores high. Carrying meaning is "
    "what an embedding model is for, and no reference can stand in for it."
)

_LEXICAL_ONLY = (
    "scores by query-term overlap, which is the thing a reranker exists to improve "
    "on. Decision 3b's lexical leg is already tsvector/pg_trgm; a term-matching "
    "reranker adds nothing to the fusion and cannot rank a paraphrase first."
)

_NO_PAGES = (
    "a comma-delimited grid has no pagination to report. Inapplicable rather than "
    "missing: there is no page number to be had, so this is a permanent property "
    "of the format and not a gap anyone should try to close."
)

_NO_TABLE_RECOVERY = (
    "recognises text only, emitting one body element per page. A scanned datasheet "
    "does have tables, so this is a gap rather than an inapplicable contract - the "
    "capability is meaningful here and someone could close it."
)

_WEB_SEARCH_QUERY = "JKM610N-66HL4M-V"


def _memory_web_search() -> InMemoryWebSearch:
    """Seed a fixture map. `datetime` stays here, not in the reference module."""
    when = datetime(2020, 1, 1, tzinfo=UTC)
    return InMemoryWebSearch(
        {
            _WEB_SEARCH_QUERY: [
                WebHit(
                    url="https://example.invalid/jinko/jkm610n",
                    title="JKM610N-66HL4M-V",
                    retrieved_at=when,
                    provider="memory",
                ),
                WebHit(
                    url="https://example.invalid/cec/jkm610n",
                    title="CEC listing",
                    retrieved_at=when,
                    provider="memory",
                ),
                WebHit(
                    url="https://example.invalid/datasheet/jkm610n",
                    title="Datasheet",
                    retrieved_at=when,
                    provider="memory",
                ),
            ]
        }
    )


REGISTERED_ADAPTERS: tuple[AdapterEntry, ...] = (
    AdapterEntry(
        name="parser:memory",
        port=ParserPort,
        factory=InMemoryParser,
        capabilities=frozenset({Capability.DETERMINISTIC_OUTPUT, Capability.TABLE_STRUCTURE}),
        absences={Capability.PAGE_NUMBERS: not_applicable(_NO_PAGES)},
        is_reference=True,
        samples=ParserSamples(
            signature="text/csv",
            foreign_signature="application/pdf",
            document=(
                b"parameter,value,unit\n"
                b"rated_ac_power,352,kVA\n"
                b"cooling_method,forced air,\n"
                b"enclosure_rating,IP66,\n"
            ),
            # Lone 0xff, which is not a legal UTF-8 lead byte anywhere. Chosen
            # over truncated multi-byte text because it cannot be salvaged by a
            # more permissive decoder either, so the contract stays meaningful
            # for an adapter that is more forgiving than this one.
            malformed=b"\xff\xfe\x00\x01 not text",
        ),
    ),
    AdapterEntry(
        name="ocr:memory",
        port=OCRPort,
        factory=InMemoryOCR,
        capabilities=frozenset({Capability.DETERMINISTIC_OUTPUT, Capability.PAGE_NUMBERS}),
        absences={Capability.TABLE_STRUCTURE: unimplemented(_NO_TABLE_RECOVERY)},
        is_reference=True,
        samples=OCRSamples(
            scan=(
                b"Sungrow SG350HX inverter, rated 352 kVA at 30 C.\f"
                b"Cooling is forced air. Enclosure rating IP66.\f"
                b"Warranty ten years from commissioning."
            )
        ),
    ),
    AdapterEntry(
        name="embedder:memory",
        port=EmbedderPort,
        factory=InMemoryEmbedder,
        capabilities=frozenset({Capability.DETERMINISTIC_OUTPUT}),
        absences={Capability.SEMANTIC_SIMILARITY: unimplemented(_NO_SEMANTICS)},
        is_reference=True,
    ),
    AdapterEntry(
        name="vector_store:memory",
        port=VectorStorePort,
        factory=InMemoryVectorStore,
        capabilities=frozenset(
            {
                Capability.DETERMINISTIC_OUTPUT,
                Capability.METADATA_FILTERING,
                Capability.ACCESS_FILTERING,
                Capability.INCREMENTAL_UPDATE,
                Capability.EXHAUSTIVE_RECALL,
            }
        ),
        is_reference=True,
        # Matches `InMemoryVectorStore`'s default width. A real store's number
        # comes from its column definition, not from a preference.
        samples=VectorStoreSamples(dimensions=8),
    ),
    AdapterEntry(
        name="reranker:memory",
        port=RerankerPort,
        factory=InMemoryReranker,
        capabilities=frozenset({Capability.DETERMINISTIC_OUTPUT}),
        absences={Capability.SEMANTIC_SIMILARITY: unimplemented(_LEXICAL_ONLY)},
        is_reference=True,
    ),
    AdapterEntry(
        name="llm:memory",
        port=LLMPort,
        factory=InMemoryLLM,
        capabilities=frozenset(
            {
                Capability.DETERMINISTIC_OUTPUT,
                Capability.INSUFFICIENT_EVIDENCE,
                Capability.SCHEMA_CONSTRAINED,
            }
        ),
        is_reference=True,
    ),
    AdapterEntry(
        name="lexical_store:memory",
        port=LexicalSearchPort,
        factory=InMemoryLexicalStore,
        capabilities=frozenset(
            {
                Capability.DETERMINISTIC_OUTPUT,
                Capability.METADATA_FILTERING,
                Capability.ACCESS_FILTERING,
                Capability.TRIGRAM_TOLERANCE,
            }
        ),
        is_reference=True,
        samples=LexicalStoreSamples(),
    ),
    AdapterEntry(
        name="web_search:memory",
        port=WebSearchPort,
        factory=_memory_web_search,
        capabilities=frozenset({Capability.DETERMINISTIC_OUTPUT}),
        absences={
            Capability.RATE_LIMITED: not_applicable(
                "a fixture map has no provider quota; rate limits belong to a "
                "networked backend such as Brave"
            )
        },
        is_reference=True,
        samples=WebSearchSamples(query=_WEB_SEARCH_QUERY),
    ),
)


def adapters_for(port: type[Any]) -> tuple[AdapterEntry, ...]:
    """Every registered adapter for one port, in registration order.

    Identity comparison rather than `issubclass`: `issubclass` against a Protocol
    with non-method members raises `TypeError`, and an adapter is bound to its
    port by the entry it was registered under, not by inheritance.
    """
    return tuple(entry for entry in REGISTERED_ADAPTERS if entry.port is port)
