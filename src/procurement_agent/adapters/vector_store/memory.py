"""An in-memory `VectorStorePort`: a dict, scanned exhaustively.

**Exhaustive by construction, which is the interesting part.** plan Decision 3a
chose exact search over an ANN index because pgvector was measured *silently*
under-returning on a filtered top-k. A dict scan cannot reproduce that defect, so
this reference declares `EXHAUSTIVE_RECALL` and passes its contract - and the
value of that is not the passing test but the row it puts in the matrix. An
approximate-index adapter arriving later has to declare the capability absent and
write down why, in front of a reviewer, before it ships.

The filters are applied *before* scoring rather than to the result. That is
`architecture.md` invariant 8 - access control is enforced during retrieval - and
it is why `search` takes `allowed_document_ids` as a parameter instead of the
caller filtering afterwards.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, cast

from ...ports import RetrievedChunk, VectorStorePort
from ...schema import ComponentCategory, SourceTier
from ..limits import require_non_negative_limit
from . import ChunkMetadata

__all__ = ["InMemoryVectorStore", "StoredChunk"]

_REQUIRED_KEYS = frozenset(ChunkMetadata.__required_keys__)


@dataclass
class StoredChunk:
    """The concrete `RetrievedChunk` this store returns.

    Not frozen for the same reason as `TextElement`: `RetrievedChunk` declares
    plain annotated attributes, which a type checker reads as read-write.
    """

    chunk_id: str
    document_id: str
    text: str
    page: int | None
    source_tier: SourceTier
    score: float


@dataclass(frozen=True)
class _Row:
    vector: list[float]
    metadata: ChunkMetadata


class InMemoryVectorStore:
    """Keyed by chunk id, so upsert-by-id and delete-by-id are the dict's own."""

    def __init__(self, dimensions: int = 8) -> None:
        self._dimensions = dimensions
        self._rows: dict[str, _Row] = {}

    def upsert(
        self, chunk_ids: list[str], vectors: list[list[float]], metadata: list[dict[str, Any]]
    ) -> None:
        """FR-RAG-05: add or update by stable ID, never a full re-index.

        The three lists are positionally correlated and the Protocol has no way
        to say so, so a length mismatch is checked rather than zipped away -
        `zip()` without `strict=True` would silently drop the tail, attaching
        every vector after the short list to nothing and leaving no trace.

        The key check exists because `ChunkMetadata` is a convention this package
        supplies, not a type the Protocol enforces (see the package docstring). A
        missing `document_id` would otherwise surface as a `KeyError` at search
        time, in a different process, with the ingesting document long gone.
        """
        if not len(chunk_ids) == len(vectors) == len(metadata):
            raise ValueError(
                f"upsert got {len(chunk_ids)} ids, {len(vectors)} vectors and "
                f"{len(metadata)} metadata rows; they are positionally correlated"
            )

        for chunk_id, vector, row in zip(chunk_ids, vectors, metadata, strict=True):
            if len(vector) != self._dimensions:
                raise ValueError(
                    f"{chunk_id}: vector of width {len(vector)} for a store of width "
                    f"{self._dimensions}. A real store's width is fixed by its column."
                )
            missing = _REQUIRED_KEYS - set(row)
            if missing:
                raise ValueError(f"{chunk_id}: metadata is missing {sorted(missing)}")
            self._rows[chunk_id] = _Row(list(vector), cast("ChunkMetadata", dict(row)))

    def delete(self, chunk_ids: list[str]) -> None:
        for chunk_id in chunk_ids:
            self._rows.pop(chunk_id, None)

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
        """Filter, then score every survivor, then take the best `limit`.

        The order of those three steps is the contract. Scoring first and
        filtering the top-k afterwards returns fewer rows than asked for
        whenever the leaders are excluded, which is the post-filter defect
        `test_access_control_is_applied_inside_the_search_not_after_it` probes -
        and it exposes restricted content to whatever ran the scoring.

        Ties break on `chunk_id`. Without a tie-break, `sorted` is stable and
        therefore leaks *insertion* order, which is arrival order wearing a
        deterministic mask - the defect `_ordering_key`'s docstring records
        shipping twice in this repository.

        `allowed_document_ids=None` returns nothing. Omitting the allow-list is
        a forgotten entitlement (NFR-03 / AC-8), not authorised-for-all.
        """
        require_non_negative_limit(limit)
        hits = [
            StoredChunk(
                chunk_id=chunk_id,
                document_id=row.metadata["document_id"],
                text=row.metadata["text"],
                page=row.metadata["page"],
                source_tier=row.metadata["source_tier"],
                score=_cosine(vector, row.vector),
            )
            for chunk_id, row in self._rows.items()
            if (
                allowed_document_ids is not None
                and row.metadata["document_id"] in allowed_document_ids
            )
            and (category is None or row.metadata["category"] is category)
            and (supplier is None or row.metadata["supplier"] == supplier)
            and (source_tier is None or row.metadata["source_tier"] is source_tier)
        ]
        hits.sort(key=lambda hit: (-hit.score, hit.chunk_id))
        return list(hits[:limit])


def _cosine(left: list[float], right: list[float]) -> float:
    norm = math.sqrt(sum(x * x for x in left)) * math.sqrt(sum(y * y for y in right))
    return sum(x * y for x, y in zip(left, right, strict=True)) / norm if norm else 0.0


def _conforms(adapter: InMemoryVectorStore) -> VectorStorePort:
    """Static structural check, run by `mypy --strict`."""
    return adapter
