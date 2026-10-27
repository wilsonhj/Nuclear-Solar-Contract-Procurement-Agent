"""An in-memory `RerankerPort` scoring by query-term overlap.

Term overlap is the thing a reranker is supposed to improve on, so this reference
declares `SEMANTIC_SIMILARITY` absent and its contract `xfail(strict=True)`s. It
is the honest position: a cross-encoder ranks a paraphrase above a decoy that
shares more words, and nothing here can.

What it does hold is the structural half, which is where a real reranker is most
likely to go wrong: it returns only chunks it was given, it respects the limit,
and the source tier survives - the stage most likely to rebuild its result
objects is the one FR-RAG-03 most needs to keep them intact through.
"""

from __future__ import annotations

import re
from dataclasses import replace

from ...ports import RerankerPort, RetrievedChunk
from ..limits import require_non_negative_limit
from ..vector_store.memory import StoredChunk

__all__ = ["InMemoryReranker"]

_TOKEN = re.compile(r"[a-z0-9]+")


class InMemoryReranker:
    """Jaccard overlap between query tokens and chunk tokens."""

    def rerank(
        self, query: str, chunks: list[RetrievedChunk], *, limit: int
    ) -> list[RetrievedChunk]:
        """Rescore, then take the best `limit`.

        The returned chunks carry the *new* score, not the retrieval score. That
        is what makes reranking legible to RRF downstream: a fused rank computed
        from a score the reranker did not set is the retrieval order with extra
        steps. Every other field is copied through unchanged - in particular
        `source_tier`, which FR-RAG-03 requires to stay attached at all times.

        Ties break on `chunk_id`, for the reason the store's `search` gives.
        """
        require_non_negative_limit(limit)
        wanted = set(_TOKEN.findall(query.lower()))
        rescored = [
            replace(_as_stored(chunk), score=_overlap(wanted, chunk.text)) for chunk in chunks
        ]
        rescored.sort(key=lambda chunk: (-chunk.score, chunk.chunk_id))
        return list(rescored[:limit])


def _as_stored(chunk: RetrievedChunk) -> StoredChunk:
    """Copy any `RetrievedChunk` into this package's concrete carrier.

    The Protocol says nothing about how a chunk is constructed, so a reranker
    cannot assume its input is the same class the store returned - the candidates
    arrive from a fusion step that may have built its own. Rebuilding from the
    six declared members is the only portable move, and it is also the check that
    all six survived the trip.
    """
    return StoredChunk(
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        text=chunk.text,
        page=chunk.page,
        source_tier=chunk.source_tier,
        score=chunk.score,
    )


def _overlap(wanted: set[str], text: str) -> float:
    found = set(_TOKEN.findall(text.lower()))
    union = wanted | found
    return len(wanted & found) / len(union) if union else 0.0


def _conforms(adapter: InMemoryReranker) -> RerankerPort:
    """Static structural check, run by `mypy --strict`."""
    return adapter
