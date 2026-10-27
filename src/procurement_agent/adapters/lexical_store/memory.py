"""An in-memory `LexicalSearchPort`: token overlap plus trigram Jaccard.

The part-number case Decision 3b exists for — `JKM610N-66HL4M-V` matching
`JKM610N 66HL4M V` — is expressible here because both sides collapse to the
same alphanumeric trigrams. A real backend uses `tsvector` and `pg_trgm`; this
reference proves the Protocol can carry that test.

`allowed_document_ids=None` returns nothing. Omitting the allow-list is a
forgotten entitlement (NFR-03 / AC-8), not authorised-for-all.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...ports import LexicalSearchPort, RetrievedChunk
from ...schema import ComponentCategory, SourceTier
from ..limits import require_non_negative_limit

__all__ = ["InMemoryLexicalStore", "StoredLexicalChunk"]


@dataclass
class StoredLexicalChunk:
    """The concrete `RetrievedChunk` this store returns."""

    chunk_id: str
    document_id: str
    text: str
    page: int | None
    source_tier: SourceTier
    score: float


@dataclass(frozen=True)
class _Row:
    text: str
    document_id: str
    page: int | None
    source_tier: SourceTier
    category: ComponentCategory
    supplier: str


class InMemoryLexicalStore:
    """Keyed by chunk id. Search filters first, then scores survivors."""

    def __init__(self) -> None:
        self._rows: dict[str, _Row] = {}

    def upsert(
        self,
        chunk_ids: list[str],
        texts: list[str],
        document_ids: list[str],
        pages: list[int | None],
        source_tiers: list[SourceTier],
        categories: list[ComponentCategory],
        suppliers: list[str],
    ) -> None:
        if not (
            len(chunk_ids)
            == len(texts)
            == len(document_ids)
            == len(pages)
            == len(source_tiers)
            == len(categories)
            == len(suppliers)
        ):
            raise ValueError("upsert lists are positionally correlated and must be the same length")
        for chunk_id, text, document_id, page, source_tier, category, supplier in zip(
            chunk_ids, texts, document_ids, pages, source_tiers, categories, suppliers, strict=True
        ):
            self._rows[chunk_id] = _Row(text, document_id, page, source_tier, category, supplier)

    def search_lexical(
        self,
        query: str,
        *,
        limit: int,
        category: ComponentCategory | None = None,
        supplier: str | None = None,
        source_tier: SourceTier | None = None,
        allowed_document_ids: set[str] | None = None,
    ) -> list[RetrievedChunk]:
        require_non_negative_limit(limit)
        hits = [
            StoredLexicalChunk(
                chunk_id=chunk_id,
                document_id=row.document_id,
                text=row.text,
                page=row.page,
                source_tier=row.source_tier,
                score=_lexical_score(query, row.text),
            )
            for chunk_id, row in self._rows.items()
            if (allowed_document_ids is not None and row.document_id in allowed_document_ids)
            and (category is None or row.category is category)
            and (supplier is None or row.supplier == supplier)
            and (source_tier is None or row.source_tier is source_tier)
        ]
        hits.sort(key=lambda hit: (-hit.score, hit.chunk_id))
        return list(hits[:limit])


def _alnum(text: str) -> str:
    return "".join(character.lower() for character in text if character.isalnum())


def _trigrams(text: str) -> frozenset[str]:
    folded = _alnum(text)
    if len(folded) < 3:
        return frozenset({folded} if folded else ())
    return frozenset(folded[index : index + 3] for index in range(len(folded) - 2))


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _token_overlap(query: str, text: str) -> float:
    query_tokens = {token.lower() for token in query.split() if token}
    text_tokens = {token.lower() for token in text.split() if token}
    if not query_tokens or not text_tokens:
        return 0.0
    return len(query_tokens & text_tokens) / len(query_tokens)


def _lexical_score(query: str, text: str) -> float:
    return _token_overlap(query, text) + _jaccard(_trigrams(query), _trigrams(text))


def _conforms(adapter: InMemoryLexicalStore) -> LexicalSearchPort:
    return adapter
