"""What the adapter side has to supply about `ports.ParsedElement`.

`ParsedElement` is the unit both `ParserPort` and `OCRPort` return. Track 0
amends the Protocol with optional `bbox`, `table`, `page_quality` and `role`
(D-23 / P2-C1), so a consumer may rely on those members. `TextElement` is the
concrete shape the in-memory references return and must stay a structural match.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..ports import ParsedElement
from ..schema import TableData

__all__ = ["PARSED_ELEMENT_KINDS", "PARSED_ELEMENT_ROLES", "TextElement"]


PARSED_ELEMENT_KINDS: frozenset[str] = frozenset({"heading", "body", "table", "figure"})
"""The `kind` vocabulary, which the Protocol states only in prose.

`kind` is typed `str` and documented as "heading, body, table or figure". A
Protocol cannot express a closed vocabulary over a `str`, so an adapter emitting
`"paragraph"` or `"Table"` is correct under the type and unusable to every
consumer that switches on the value. `_assert_element_shape` in the contract
suite is what holds adapters to it meanwhile.
"""

PARSED_ELEMENT_ROLES: frozenset[str] = frozenset({"body", "furniture", "footnote", "caption"})
"""The `role` vocabulary. A Protocol `Literal` is not enforced at runtime."""


@dataclass
class TextElement:
    """The concrete `ParsedElement` the in-memory references return.

    **Not frozen, and that is a finding rather than a preference.**
    `ParsedElement` declares plain annotated attributes, which a type checker
    reads as read-write, so a frozen implementation is not guaranteed to satisfy
    it. `docs/architecture.md` asks for "frozen models for immutable evidence",
    and a parsed element is evidence; the Protocol as written cannot promise that
    an immutable one is admissible. Declaring the members `ReadOnly`, or as
    properties, would be the fix, and it belongs in `ports/`.
    """

    kind: str
    text: str
    page: int | None
    bbox: tuple[float, float, float, float] | None = None
    table: TableData | None = None
    page_quality: float | None = None
    role: Literal["body", "furniture", "footnote", "caption"] = "body"

    def __post_init__(self) -> None:
        if self.role not in PARSED_ELEMENT_ROLES:
            raise ValueError(f"role {self.role!r} is outside {sorted(PARSED_ELEMENT_ROLES)}")
        if (self.kind == "table") != (self.table is not None):
            raise ValueError("ParsedElement.table is present iff kind == 'table'")


def _conforms(element: TextElement) -> ParsedElement:
    """Static proof, checked by `mypy --strict`, that the shape still matches.

    `isinstance` against a `runtime_checkable` Protocol checks attribute presence
    only, and `ParsedElement` is not even runtime-checkable. This is the check
    that fails when a member is renamed or retyped.
    """
    return element
