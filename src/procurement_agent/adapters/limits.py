"""Shared search-limit guard. Negative `limit` is a Python slice, not a cap."""

from __future__ import annotations

__all__ = ["require_non_negative_limit"]


def require_non_negative_limit(limit: int) -> None:
    if limit < 0:
        raise ValueError("limit must be >= 0; a negative value is a Python slice, not a cap")
