"""Swap points required by NFR-04.

    "Parsers, OCR, embedders, vector store, reranker, and LLM must be swappable
    behind stable interfaces."

Eight interfaces: the six NFR-04 named, plus `LexicalSearchPort` (D-25) and
`WebSearchPort` (P2-C4). Decision 10's current count is eight. They are
structural Protocols rather than base classes so that adapters can wrap
third-party clients without inheriting from anything here, and so no concrete
dependency leaks into the core package. When a real adapter lands it will
depend on an optional extra declared in pyproject.toml, never on the core - the
extras there currently declare dependency groups only.

**These Protocols are synchronous, deliberately.** Concurrency in this system is
per-process, not per-coroutine: the runner is a Postgres job table with a
`SELECT ... FOR UPDATE SKIP LOCKED` worker loop (plan.md Decision 1), so scaling
means more worker processes. Parse and OCR are CPU-bound in-process; embedding
and reranking already take batches, so their parallelism is inside the payload
rather than at the call boundary; and the vector store is a local Postgres
round-trip under Decision 3a. A caller that needs overlap can wrap any of these
in a ThreadPoolExecutor or ProcessPoolExecutor without touching the interface,
and an async variant can be added alongside later - Protocols are structural, so
that is additive rather than a breaking change.

The reference memo's parser-router finding applies directly: no single engine
wins across document types, so ParserPort is expected to have several
implementations selected by content signature (FR-ING-01), not one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from ..schema import ComponentCategory, SourceTier, TableData

__all__ = [
    "EmbedderPort",
    "LLMPort",
    "LexicalSearchPort",
    "OCRPort",
    "ParsedElement",
    "ParserPort",
    "RerankerPort",
    "RetrievedChunk",
    "VectorStorePort",
    "WebHit",
    "WebSearchPort",
    "WebSearchRateLimitExceeded",
]


class ParsedElement(Protocol):
    """A layout-aware unit of a parsed document (FR-ING-03/04/05, D-23)."""

    kind: str
    """heading, body, table or figure."""
    text: str
    page: int | None
    bbox: tuple[float, float, float, float] | None
    """Axis-aligned envelope in page points, origin top-left."""
    table: TableData | None
    """Present iff `kind == "table"`."""
    page_quality: float | None
    """0–1 OCR/parser confidence for the page region."""
    role: Literal["body", "furniture", "footnote", "caption"]


class RetrievedChunk(Protocol):
    """A chunk returned from retrieval.

    FR-RAG-03 requires that system-of-record chunks stay distinguishable from
    web supplements at all times, so source_tier travels with every chunk rather
    than being looked up later.
    """

    chunk_id: str
    document_id: str
    text: str
    page: int | None
    source_tier: SourceTier
    score: float


@runtime_checkable
class ParserPort(Protocol):
    """Format-native parsing. One implementation per document family."""

    def supports(self, content_signature: str) -> bool:
        """FR-ING-01 routes by content signature, never by file extension."""
        ...

    def parse(self, data: bytes) -> list[ParsedElement]: ...


@runtime_checkable
class OCRPort(Protocol):
    """Fallback for scanned PDFs and images (FR-ING-04)."""

    def needs_ocr(self, elements: list[ParsedElement]) -> bool:
        """Auto-detect absent or low text coverage."""
        ...

    def recognize(self, data: bytes) -> list[ParsedElement]:
        """Handle skew, rotation, multi-column and tables; retain bounding boxes."""
        ...


@runtime_checkable
class EmbedderPort(Protocol):
    """Embedding generation (FR-RAG-02).

    NFR-03 requires a self-hosted or enterprise endpoint for confidential
    documents, with no third-party training on contract data.
    """

    @property
    def dimensions(self) -> int: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


@runtime_checkable
class VectorStorePort(Protocol):
    """Vector store over chunks.

    FR-RAG-02 asks for an ANN index (HNSW/IVF, cosine); **plan Decision 3a
    reverses that** - exact search, because pgvector was measured silently
    under-returning on a filtered top-k, and an HNSW index cost more than the
    table. The Protocol is agnostic; do not build an ANN index on the strength
    of the requirement text alone.
    """

    def upsert(
        self, chunk_ids: list[str], vectors: list[list[float]], metadata: list[dict[str, Any]]
    ) -> None:
        """FR-RAG-05: incremental add/update by stable ID, no full re-index."""
        ...

    def delete(self, chunk_ids: list[str]) -> None: ...

    def search(
        self,
        vector: list[float],
        *,
        limit: int,
        category: ComponentCategory | None = None,
        supplier: str | None = None,
        source_tier: SourceTier | None = None,
        allowed_document_ids: set[str] | None = None,
    ) -> list[RetrievedChunk]:
        """Metadata-filtered search.

        allowed_document_ids carries NFR-03 access control. It is a search
        parameter rather than a post-filter because the requirement is that
        access control is enforced at retrieval time.

        `None` is not authorised-for-all. An omitted allow-list is a forgotten
        entitlement and must return nothing (the same as an empty set). A caller
        who may see every document has to pass that set.
        """
        ...


@runtime_checkable
class RerankerPort(Protocol):
    """Reranking over hybrid retrieval candidates.

    FR-RAG-03 says vector + BM25; **plan Decision 3b reverses the BM25 half** -
    there is no permissively licensed true-BM25 for PostgreSQL, so the lexical
    leg is Postgres `tsvector`/GIN plus `pg_trgm`, fused with RRF (k=60).

    Not the embedding model's sparse output, which this docstring previously
    claimed: that is a *contingency* under Decision 5 (swap Qwen3-Embedding-4B
    for `bge-m3`, which emits dense, learned-sparse and ColBERT vectors in one
    pass) held in reserve if Postgres FTS proves weak on part numbers. The same
    substitution appears in the deviation note beside FR-RAG-03 in `spec.md`,
    which is why it is spelled out here. Reranking is what lets the design get
    away without BM25 at all, which is this port's stake in the decision.
    """

    def rerank(
        self, query: str, chunks: list[RetrievedChunk], *, limit: int
    ) -> list[RetrievedChunk]: ...


@runtime_checkable
class LLMPort(Protocol):
    """Schema-constrained generation (FR-ING-07).

    Extraction uses JSON-schema or tool-use output with validation. Prompts use
    retrieved context only and must be able to return an explicit
    "insufficient evidence" result rather than fabricating a value (FR-RAG-04),
    which is why extract returns None rather than raising on a miss.
    """

    def extract(
        self,
        *,
        prompt: str,
        context: list[RetrievedChunk],
        json_schema: dict[str, Any],
    ) -> dict[str, Any] | None: ...


@runtime_checkable
class LexicalSearchPort(Protocol):
    """Lexical retrieval over chunks (D-25).

    A seventh Protocol beside VectorStorePort so a vector adapter is not forced
    to implement tsvector/trigram search. `allowed_document_ids=None` returns
    nothing: an omitted allow-list is a forgotten entitlement, same rule as
    VectorStorePort.
    """

    def search_lexical(
        self,
        query: str,
        *,
        limit: int,
        category: ComponentCategory | None = None,
        supplier: str | None = None,
        source_tier: SourceTier | None = None,
        allowed_document_ids: set[str] | None = None,
    ) -> list[RetrievedChunk]: ...


@dataclass(frozen=True)
class WebHit:
    """One provider result. The list is transient; only the URL is kept (D-20)."""

    url: str
    title: str | None
    retrieved_at: datetime
    provider: str

    def __post_init__(self) -> None:
        """Reject a naive `retrieved_at`, matching `SourceRef`'s rule.

        This is the first boundary that produces the timestamp. A naive value
        names no instant, so FR-WEB-04's temporal comparisons would be fiction.
        """
        value = self.retrieved_at
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError(
                "retrieved_at must be timezone-aware; a naive datetime names no "
                "instant. Attach the zone at the boundary that produced it."
            )


class WebSearchRateLimitExceeded(RuntimeError):
    """The web-search adapter refused a query because its quota is exhausted."""


@runtime_checkable
class WebSearchPort(Protocol):
    """Gap-triggered web search (P2-C4). Returns hits, not snippets or ranks."""

    def search(self, query: str, *, limit: int) -> list[WebHit]:
        """Return matches; raise WebSearchRateLimitExceeded on quota exhaustion.

        An empty list means no matches, never a rate-limit refusal. Other
        adapter failures propagate separately from quota exhaustion.
        """
        ...
