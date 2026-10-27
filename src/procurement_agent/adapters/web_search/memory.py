"""An in-memory `WebSearchPort`: a deterministic fixture map.

Hits are injected at construction. The reference does not import `datetime`,
because a clock would make `DETERMINISTIC_OUTPUT` a lie and would fail the
conformance scan that forbids moving sources in a reference module.
"""

from __future__ import annotations

from ...ports import WebHit, WebSearchPort
from ..limits import require_non_negative_limit

__all__ = ["InMemoryWebSearch"]


class InMemoryWebSearch:
    """Returns pre-seeded hits for an exact query string, in insertion order."""

    def __init__(self, hits: dict[str, list[WebHit]] | None = None) -> None:
        self._hits = {query: list(results) for query, results in (hits or {}).items()}

    def search(self, query: str, *, limit: int) -> list[WebHit]:
        require_non_negative_limit(limit)
        return list(self._hits.get(query, [])[:limit])


def _conforms(adapter: InMemoryWebSearch) -> WebSearchPort:
    return adapter
