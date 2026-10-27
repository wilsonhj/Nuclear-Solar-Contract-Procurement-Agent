"""The ports' behavioural contracts — NFR-04, ADR-001 decision 2.

> **What a green run here proves, and what it does not.** Every adapter these
> contracts run against is an in-memory reference that was written to satisfy
> them. Green means the contract is **expressible and self-consistent** — some
> object can satisfy it. It is not evidence that Docling, vLLM, pgvector or any
> other real backend can, because none of them exist in this repository and none
> has ever been run against this file. Decision 10's current count is eight
> swap points; nothing has yet been swapped. The executable form of this
> paragraph is `test_every_registered_adapter_is_an_in_memory_reference` in
> `test_conformance_matrix.py`, which goes red the day the sentence stops being
> true.

**Why the tests exist before the adapters.** `docs/development.md` records that
the first adapter "decides the layout for everyone after it"; ADR-001 answers it
by landing the contract suite first, so that the first real adapter arrives to a
contract instead of defining one. Every assertion below is therefore written
against the Protocol, never against a reference's internals.

**How a capability gates a test** — `cases(port, capability)`:

* declared → the test runs and must pass;
* declared absent, of either kind → **`xfail(strict=True)`**, carrying the kind
  and the reason;
* neither declared nor explained → the test runs unmarked and fails on its own
  merits, and `test_every_adapter_accounts_for_every_capability_of_its_port`
  names the omission precisely.

**Why an absence is never a `skip`, which was the first design and is wrong.**
`skip` does not execute the contract, so a `NOT_APPLICABLE` declaration with a
plausible reason attached removes a test from the suite and nothing ever looks at
it again — which is the "silently absent" state ADR-001 adopted this pattern to
prevent, spelled with a reason string. A strict `xfail` executes the body and
demands it fail, so an adapter that has quietly *grown* the capability goes red
and gets its declaration corrected. What that still cannot catch is a
misclassified kind — an `UNIMPLEMENTED` gap dressed as `NOT_APPLICABLE` — because
inapplicability is not falsifiable from outside. The kind is therefore a claim to
a reader (permanent property versus work item) that the matrix records and
`AbsenceKind` names; what the suite *enforces* is that the contract genuinely
fails, whichever kind is claimed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any, Literal, cast

import pytest

from procurement_agent.adapters import (
    PARSED_ELEMENT_KINDS,
    PARSED_ELEMENT_ROLES,
    AdapterEntry,
    Capability,
    adapters_for,
)
from procurement_agent.adapters.lexical_store import LexicalStoreSamples
from procurement_agent.adapters.ocr import OCRSamples
from procurement_agent.adapters.parser import ParserSamples
from procurement_agent.adapters.vector_store import ChunkMetadata, VectorStoreSamples
from procurement_agent.adapters.web_search import WebSearchSamples
from procurement_agent.ports import (
    EmbedderPort,
    LexicalSearchPort,
    LLMPort,
    OCRPort,
    ParsedElement,
    ParserPort,
    RerankerPort,
    RetrievedChunk,
    VectorStorePort,
    WebHit,
    WebSearchPort,
    WebSearchRateLimitExceeded,
)
from procurement_agent.schema import ComponentCategory, SourceTier, TableData

# --------------------------------------------------------------------------
# Parametrisation, and the capability gate
# --------------------------------------------------------------------------


def cases(port: type[Any], capability: Capability | None = None) -> list[object]:
    """Every registered adapter for `port`, marked by its stance on `capability`.

    The gate is applied at collection rather than inside the test body, which is
    what makes `strict=True` available: an imperative `pytest.xfail()` aborts
    before the assertions run, so it can never report an adapter that has
    outgrown its own declaration. Here the body executes and pytest compares the
    outcome to the declaration.

    A missing declaration is deliberately *not* handled here. Filling it in with
    a default would be this suite committing the exact silence ADR-001 adopted
    the pattern to prevent; leaving it unmarked lets the contract test fail on
    the behaviour while the matrix test fails on the omission.
    """
    params: list[object] = []
    for entry in adapters_for(port):
        absence = entry.absences.get(capability) if capability is not None else None
        marks: tuple[pytest.MarkDecorator, ...] = ()
        if absence is not None:
            marks = (
                pytest.mark.xfail(
                    reason=f"{entry.name} [{absence.kind}]: {absence.reason}",
                    strict=True,
                ),
            )
        params.append(pytest.param(entry, id=entry.name, marks=marks))
    return params


# The registry is a heterogeneous table, so `AdapterEntry.factory` is typed
# `Callable[[], object]` and each port casts once, here. The casts are not taken
# on trust: `test_every_adapter_satisfies_its_port_at_runtime` checks each entry
# with `isinstance` against the very Protocol cast to, and `mypy --strict`
# checks the reference classes against the same Protocols in their own modules.


def _parser(entry: AdapterEntry) -> ParserPort:
    return cast("ParserPort", entry.factory())


def _ocr(entry: AdapterEntry) -> OCRPort:
    return cast("OCRPort", entry.factory())


def _embedder(entry: AdapterEntry) -> EmbedderPort:
    return cast("EmbedderPort", entry.factory())


def _store(entry: AdapterEntry) -> VectorStorePort:
    return cast("VectorStorePort", entry.factory())


def _reranker(entry: AdapterEntry) -> RerankerPort:
    return cast("RerankerPort", entry.factory())


def _llm(entry: AdapterEntry) -> LLMPort:
    return cast("LLMPort", entry.factory())


def _lexical(entry: AdapterEntry) -> LexicalSearchPort:
    return cast("LexicalSearchPort", entry.factory())


def _web(entry: AdapterEntry) -> WebSearchPort:
    return cast("WebSearchPort", entry.factory())


@dataclass
class _Element:
    """A `ParsedElement` the suite builds itself, to feed `OCRPort.needs_ocr`.

    Not frozen: `ParsedElement` declares plain annotated attributes, which a type
    checker reads as mutable, and a frozen implementation is therefore not
    guaranteed to satisfy it. That is a property of the Protocol as written, and
    it is recorded here rather than worked around silently.
    """

    kind: str
    text: str
    page: int | None
    bbox: tuple[float, float, float, float] | None = None
    table: TableData | None = None
    page_quality: float | None = None
    role: Literal["body", "furniture", "footnote", "caption"] = "body"


@dataclass
class _Chunk:
    """A `RetrievedChunk` the suite builds itself, for reranker and LLM inputs."""

    chunk_id: str
    document_id: str
    text: str
    page: int | None
    source_tier: SourceTier
    score: float


def _cosine(left: list[float], right: list[float]) -> float:
    norm = math.sqrt(sum(x * x for x in left)) * math.sqrt(sum(y * y for y in right))
    return sum(x * y for x, y in zip(left, right, strict=True)) / norm if norm else 0.0


def _assert_element_shape(element: object) -> None:
    """The members `ParsedElement` declares, plus the vocabularies it names.

    Typed `object` on purpose: a `ParsedElement` annotation would make the
    three-field stub below a type error instead of the runtime pin it is.
    `kind` is typed `str` and documented as "heading, body, table or figure". A
    Protocol cannot express that, so it is checked here: an adapter emitting
    `"paragraph"` or `"Table"` is not wrong under the type and is unusable to
    every consumer that switches on the value. `role` is a `Literal` on the
    Protocol, which is equally unenforced at runtime.
    """
    for field in ("kind", "text", "page", "bbox", "table", "page_quality", "role"):
        assert hasattr(element, field), (
            f"P2-C1 TextElement members are not optional; parsed element is missing {field}"
        )
    parsed = cast("ParsedElement", element)
    assert isinstance(parsed.text, str)
    kind = parsed.kind
    assert kind in PARSED_ELEMENT_KINDS, f"{kind!r} is outside the vocabulary"
    assert parsed.page is None or (isinstance(parsed.page, int) and parsed.page >= 1)
    bbox = parsed.bbox
    assert bbox is None or (len(bbox) == 4 and all(isinstance(value, float) for value in bbox))
    table = parsed.table
    if kind == "table":
        assert table is not None, "table-kind element must carry TableData"
        assert isinstance(table, TableData)
    else:
        assert table is None, "non-table kind must not carry TableData"
    quality = parsed.page_quality
    assert quality is None or 0.0 <= quality <= 1.0
    role = parsed.role
    assert role in PARSED_ELEMENT_ROLES, (
        f"unknown parsed role {role!r}; freeze PARSED_ELEMENT_ROLES"
    )


def _element_identity(element: ParsedElement) -> tuple[object, ...]:
    """Every P2-C1 member, so DETERMINISTIC_OUTPUT cannot ignore TableData."""
    return (
        element.kind,
        element.text,
        element.page,
        element.bbox,
        element.table,
        element.page_quality,
        element.role,
    )


def test_assert_element_shape_rejects_a_three_field_element() -> None:
    """P2-C1 is an adapter contract, not only a Protocol annotation pin."""

    @dataclass
    class Legacy:
        kind: str
        text: str
        page: int | None

    with pytest.raises(AssertionError, match="P2-C1"):
        _assert_element_shape(Legacy(kind="body", text="hello", page=1))


def test_assert_element_shape_rejects_an_unknown_role() -> None:
    @dataclass
    class Weird:
        kind: str = "body"
        text: str = "x"
        page: int | None = 1
        bbox: tuple[float, float, float, float] | None = None
        table: TableData | None = None
        page_quality: float | None = None
        role: str = "header"

    with pytest.raises(AssertionError, match="role"):
        _assert_element_shape(Weird())


def test_assert_element_shape_rejects_table_data_on_a_body_element() -> None:
    table = TableData(rows=(("Parameter", "Value"),), header_rows=1, caption=None, merged=())
    element = _Element(kind="body", text="Parameter | Value", page=1, table=table)
    with pytest.raises(AssertionError, match="table"):
        _assert_element_shape(element)


# --------------------------------------------------------------------------
# ParserPort — FR-ING-01/02/03/05
# --------------------------------------------------------------------------


@pytest.mark.parametrize("entry", cases(ParserPort))
def test_a_foreign_content_signature_is_refused(entry: AdapterEntry) -> None:
    """FR-ING-01 routes by signature, so `supports()` has to be able to say no.

    A parser that answers `True` to everything makes the router a no-op and the
    "no single engine wins across document types" finding in `ports/__init__.py`
    unimplementable — every document would be handed to whichever adapter the
    registry lists first.
    """
    parser = _parser(entry)
    samples = cast("ParserSamples", entry.samples)
    assert parser.supports(samples.signature) is True
    assert parser.supports(samples.foreign_signature) is False


@pytest.mark.parametrize("entry", cases(ParserPort))
def test_an_unknown_signature_is_refused_rather_than_raising(entry: AdapterEntry) -> None:
    """The router asks every parser about every document, including junk."""
    assert _parser(entry).supports("application/x-not-a-real-media-type") is False


@pytest.mark.parametrize("entry", cases(ParserPort))
def test_routing_is_by_content_signature_not_by_file_extension(entry: AdapterEntry) -> None:
    """ "never by file extension" is the clause with a known failure mode.

    A renamed `.xlsx` that is really a ZIP of something else, or a `.pdf` that is
    a scan, is the case FR-ING-01 was written for. An adapter that accepts the
    extension form of its own media type is sniffing the name, and this is the
    cheapest way to catch it from outside.
    """
    parser = _parser(entry)
    samples = cast("ParserSamples", entry.samples)
    extension = "." + samples.signature.rsplit("/", 1)[-1]
    assert parser.supports(extension) is False
    assert parser.supports(f"supplier-quote{extension}") is False


@pytest.mark.parametrize("entry", cases(ParserPort))
def test_a_parsed_document_yields_elements_of_the_declared_shape(entry: AdapterEntry) -> None:
    """FR-ING-03/05: layout-aware units, not one undifferentiated blob of text."""
    elements = _parser(entry).parse(cast("ParserSamples", entry.samples).document)
    assert elements
    for element in elements:
        _assert_element_shape(element)
    assert any(element.text.strip() for element in elements)


@pytest.mark.parametrize("entry", cases(ParserPort))
def test_an_empty_document_yields_no_elements(entry: AdapterEntry) -> None:
    """The empty fixture `docs/development.md` step 6 asks every adapter for."""
    assert _parser(entry).parse(b"") == []


@pytest.mark.parametrize("entry", cases(ParserPort))
def test_input_it_cannot_read_raises_rather_than_yielding_nothing(entry: AdapterEntry) -> None:
    """An empty list for unreadable bytes is a page audit that can never fire.

    This is the ingestion-side spelling of the rule `docs/development.md` states
    for conflicts — do not turn "not compared" into "no conflict". A parser that
    returns `[]` from a corrupt file is indistinguishable from one that read an
    empty file, and FR-ING-09's per-page audit for missing text is built on
    telling those apart. `docs/development.md` is explicit that the remedy for a
    provider that cannot manage it is "a translation layer or reject the adapter
    output", not a weaker Protocol.

    Which exception is deliberately unasserted: `ports` declares no error
    vocabulary at all. ADR-001 decision 4 is where that taxonomy is due, and this
    contract tightens to a named class when it exists.
    """
    with pytest.raises(Exception):  # noqa: B017 - see the docstring's closing note
        _parser(entry).parse(cast("ParserSamples", entry.samples).malformed)


@pytest.mark.parametrize("entry", cases(ParserPort, Capability.DETERMINISTIC_OUTPUT))
def test_parsing_is_deterministic_across_instances(entry: AdapterEntry) -> None:
    """Two fresh instances, same bytes, same elements.

    Fresh instances rather than two calls on one, because the failure worth
    catching is cached or accumulated state — a parser whose second document is
    parsed differently because of its first. AC-7 needs byte-identical
    regeneration, and every stage upstream of the workbook has to hold its own
    end of that.
    """
    document = cast("ParserSamples", entry.samples).document
    first = [_element_identity(e) for e in _parser(entry).parse(document)]
    second = [_element_identity(e) for e in _parser(entry).parse(document)]
    assert first == second


@pytest.mark.parametrize("entry", cases(ParserPort, Capability.TABLE_STRUCTURE))
def test_a_table_survives_as_a_table_element(entry: AdapterEntry) -> None:
    """FR-ING-02/05. A table flattened into prose loses the row/column relation
    that every canonical parameter in the contract is read out of."""
    elements = _parser(entry).parse(cast("ParserSamples", entry.samples).document)
    tables = [element for element in elements if element.kind == "table"]
    assert tables, "TABLE_STRUCTURE requires a table element"
    assert all(element.table is not None for element in tables)


@pytest.mark.parametrize("entry", cases(ParserPort, Capability.PAGE_NUMBERS))
def test_every_parsed_element_carries_a_page_number(entry: AdapterEntry) -> None:
    """FR-ING-03, and NFR-01's "no value without provenance" at the source end."""
    elements = _parser(entry).parse(cast("ParserSamples", entry.samples).document)
    assert elements
    assert all(element.page is not None for element in elements)


# --------------------------------------------------------------------------
# OCRPort — FR-ING-04
# --------------------------------------------------------------------------


@pytest.mark.parametrize("entry", cases(OCRPort))
def test_a_document_with_no_text_layer_needs_ocr(entry: AdapterEntry) -> None:
    """The image-only PDF: the case FR-ING-04's fallback exists for."""
    assert _ocr(entry).needs_ocr([]) is True


@pytest.mark.parametrize("entry", cases(OCRPort))
def test_a_full_text_layer_does_not_need_ocr(entry: AdapterEntry) -> None:
    """The other direction, and the expensive one to get wrong.

    OCR over a page that already has text costs time and *loses* fidelity — a
    recognised character is a guess where an embedded one is a fact. A detector
    that answers `True` unconditionally passes the test above and is useless.
    """
    text_layer: list[ParsedElement] = [
        _Element(kind="heading", text="Rated performance at STC", page=1),
        _Element(
            kind="body", text="The module is rated 615 W at standard test conditions.", page=1
        ),
        _Element(kind="body", text="Open-circuit voltage is 41.5 V and Isc is 18.6 A.", page=1),
    ]
    assert _ocr(entry).needs_ocr(text_layer) is False


@pytest.mark.parametrize("entry", cases(OCRPort))
def test_recognition_yields_elements_of_the_declared_shape(entry: AdapterEntry) -> None:
    elements = _ocr(entry).recognize(cast("OCRSamples", entry.samples).scan)
    assert elements
    for element in elements:
        _assert_element_shape(element)


@pytest.mark.parametrize("entry", cases(OCRPort, Capability.DETERMINISTIC_OUTPUT))
def test_recognition_is_deterministic_across_instances(entry: AdapterEntry) -> None:
    scan = cast("OCRSamples", entry.samples).scan
    first = [_element_identity(e) for e in _ocr(entry).recognize(scan)]
    second = [_element_identity(e) for e in _ocr(entry).recognize(scan)]
    assert first == second


@pytest.mark.parametrize("entry", cases(OCRPort, Capability.PAGE_NUMBERS))
def test_every_recognized_element_carries_a_page_number(entry: AdapterEntry) -> None:
    """A recognised value with no page cannot satisfy NFR-01 downstream."""
    elements = _ocr(entry).recognize(cast("OCRSamples", entry.samples).scan)
    assert elements
    assert all(element.page is not None for element in elements)


@pytest.mark.parametrize("entry", cases(OCRPort, Capability.TABLE_STRUCTURE))
def test_a_scanned_table_survives_recognition(entry: AdapterEntry) -> None:
    """FR-ING-04 asks for tables, not just text, out of a scan."""
    elements = _ocr(entry).recognize(cast("OCRSamples", entry.samples).scan)
    tables = [element for element in elements if element.kind == "table"]
    assert tables, "TABLE_STRUCTURE requires a table element"
    assert all(element.table is not None for element in tables)


# --------------------------------------------------------------------------
# EmbedderPort — FR-RAG-02
# --------------------------------------------------------------------------

_TEXTS = [
    "Sungrow SG350HX rated AC power 352 kVA at 30 degrees Celsius",
    "Trina Vertex N TSM-NEG21C nameplate 615 W",
    "Battery round-trip efficiency measured at the AC terminals",
]


@pytest.mark.parametrize("entry", cases(EmbedderPort))
def test_dimensions_matches_the_width_of_every_vector(entry: AdapterEntry) -> None:
    """`dimensions` is what `sql/03_chunk.sql`'s vector column is sized from.

    A declared width that disagrees with the delivered one is not caught until
    an INSERT fails in a worker, by which point the batch that produced it is
    already partly written. plan Decision 5 also allows swapping the embedding
    model outright, so this is the property that makes the swap detectable.
    """
    embedder = _embedder(entry)
    vectors = embedder.embed(_TEXTS)
    assert all(len(vector) == embedder.dimensions for vector in vectors)


@pytest.mark.parametrize("entry", cases(EmbedderPort))
def test_one_vector_per_text_and_the_order_is_the_input_order(entry: AdapterEntry) -> None:
    """Positional correspondence is the whole contract of a batch API.

    Checked against singleton calls rather than against itself: a batch that
    silently reorders — by length, or by a dedup that drops a repeat — returns
    the right *count* and attaches every vector to the wrong chunk id. There is
    no later stage at which that is visible; it surfaces as retrieval quality
    nobody can explain.
    """
    embedder = _embedder(entry)
    batch = embedder.embed(_TEXTS)
    assert len(batch) == len(_TEXTS)
    for index, text in enumerate(_TEXTS):
        assert batch[index] == embedder.embed([text])[0]


@pytest.mark.parametrize("entry", cases(EmbedderPort))
def test_an_empty_batch_yields_no_vectors(entry: AdapterEntry) -> None:
    """FR-RAG-05's incremental path reaches this whenever a delta is empty."""
    assert _embedder(entry).embed([]) == []


@pytest.mark.parametrize("entry", cases(EmbedderPort))
def test_every_component_is_a_finite_number(entry: AdapterEntry) -> None:
    """NaN never equals itself, so one poisons every distance it appears in.

    `schema/encoding.py` refuses non-finite floats for the same reason at the
    other end of the pipeline. A NaN here does not raise: it produces a chunk
    that is silently unrankable, which reads as a retrieval miss.
    """
    for vector in _embedder(entry).embed(_TEXTS):
        assert all(math.isfinite(component) for component in vector)


@pytest.mark.parametrize("entry", cases(EmbedderPort, Capability.DETERMINISTIC_OUTPUT))
def test_embedding_is_deterministic_across_instances(entry: AdapterEntry) -> None:
    """FR-RAG-05 updates chunks incrementally, so old and new vectors coexist.

    If the same text embeds differently on Tuesday, the index becomes a mixture
    of two coordinate systems and neighbourhoods drift with no data change.
    """
    assert _embedder(entry).embed(_TEXTS) == _embedder(entry).embed(_TEXTS)


@pytest.mark.parametrize("entry", cases(EmbedderPort, Capability.SEMANTIC_SIMILARITY))
def test_an_embedded_paraphrase_outranks_a_lexical_decoy(entry: AdapterEntry) -> None:
    """The one contract a hashing reference cannot fake, and the point of the axis.

    The decoy *shares more words* with the query than the paraphrase does, which
    shares none. Any bag-of-tokens scheme therefore ranks it first. Passing this
    requires the embedding to carry meaning, which is exactly what separates a
    reference from a model — and why the reference declares the capability absent
    instead of the suite quietly omitting the test.
    """
    embedder = _embedder(entry)
    query, paraphrase, decoy = embedder.embed(
        [
            "photovoltaic module rated output",
            "solar panel nameplate power",
            "photovoltaic module carton weight and shipping dimensions",
        ]
    )
    assert _cosine(query, paraphrase) > _cosine(query, decoy)


# --------------------------------------------------------------------------
# VectorStorePort — FR-RAG-02/05, NFR-03, plan Decision 3a
# --------------------------------------------------------------------------

_ALL_DOCUMENTS = frozenset(f"doc-{index}" for index in range(6))


def _graded(dimensions: int, rank: float) -> list[float]:
    """A unit vector whose cosine against `_query()` falls strictly with `rank`.

    Rotated in the plane of the first two axes so that the corpus has a total
    order with no ties. Ties would make "ordered by descending score" and "the
    top-1 is the nearest allowed chunk" pass for a store that returns rows in
    insertion order.

    `rank` is a float so that a test can insert a chunk strictly *between* two
    existing ones. Landing exactly on another chunk's rank leaves the outcome to
    whatever tie-break the store happens to use, which is not part of the
    contract and must not be what an assertion depends on.
    """
    angle = rank * (math.pi / 24)
    return [math.cos(angle), math.sin(angle), *([0.0] * (dimensions - 2))]


def _query(dimensions: int) -> list[float]:
    return _graded(dimensions, 0)


def _stocked(entry: AdapterEntry) -> tuple[VectorStorePort, int]:
    """Six chunks, ranks 0..5, alternating category, supplier and tier.

    `doc-0` is nearest the query and `doc-5` furthest, so a filter that excludes
    the leaders is the interesting case in every test below.
    """
    store = _store(entry)
    dimensions = cast("VectorStoreSamples", entry.samples).dimensions
    assert dimensions >= 2, "the graded corpus needs two axes to rotate in"

    metadata: list[dict[str, Any]] = []
    for rank in range(6):
        row: ChunkMetadata = {
            "document_id": f"doc-{rank}",
            "text": f"chunk {rank}: rated AC power and cooling method",
            "page": rank + 1,
            "source_tier": SourceTier.SYSTEM_OF_RECORD
            if rank % 2 == 0
            else SourceTier.WEB_SUPPLEMENT,
            "category": ComponentCategory.INVERTERS_PCS
            if rank < 3
            else ComponentCategory.PV_MODULES,
            "supplier": "sungrow" if rank < 3 else "trina",
        }
        metadata.append(dict(row))

    store.upsert(
        chunk_ids=[f"chunk-{rank}" for rank in range(6)],
        vectors=[_graded(dimensions, rank) for rank in range(6)],
        metadata=metadata,
    )
    return store, dimensions


_STOCKED_DOCUMENT_IDS = {f"doc-{rank}" for rank in range(6)}


@pytest.mark.parametrize("entry", cases(VectorStorePort))
def test_a_stored_chunk_comes_back_from_search(entry: AdapterEntry) -> None:
    store, dimensions = _stocked(entry)
    hits = store.search(_query(dimensions), limit=3, allowed_document_ids=_STOCKED_DOCUMENT_IDS)
    assert [hit.chunk_id for hit in hits] == ["chunk-0", "chunk-1", "chunk-2"]


@pytest.mark.parametrize("entry", cases(VectorStorePort))
def test_search_returns_no_more_than_the_limit(entry: AdapterEntry) -> None:
    store, dimensions = _stocked(entry)
    assert (
        len(store.search(_query(dimensions), limit=2, allowed_document_ids=_STOCKED_DOCUMENT_IDS))
        == 2
    )


@pytest.mark.parametrize("entry", cases(VectorStorePort))
def test_search_rejects_a_negative_limit(entry: AdapterEntry) -> None:
    """`hits[:limit]` with limit=-1 is a drop-last slice, not a cap."""
    store, dimensions = _stocked(entry)
    with pytest.raises(ValueError, match="limit"):
        store.search(_query(dimensions), limit=-1, allowed_document_ids=_STOCKED_DOCUMENT_IDS)


@pytest.mark.parametrize("entry", cases(VectorStorePort))
def test_results_are_ordered_best_first(entry: AdapterEntry) -> None:
    """RRF (plan Decision 3b) fuses by rank, so rank has to mean something."""
    store, dimensions = _stocked(entry)
    scores = [
        hit.score
        for hit in store.search(
            _query(dimensions), limit=6, allowed_document_ids=_STOCKED_DOCUMENT_IDS
        )
    ]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.parametrize("entry", cases(VectorStorePort))
def test_the_source_tier_survives_the_round_trip(entry: AdapterEntry) -> None:
    """FR-RAG-03: a system-of-record chunk must stay distinguishable from a web one.

    `RetrievedChunk.source_tier` carries it rather than a later lookup precisely
    so that it cannot be lost here. If a store drops it, every downstream rule
    built on the hard rule — FR-WEB-03's no-overwrite, FR-HITL-02's no
    auto-arbitration — is deciding on a default.
    """
    store, dimensions = _stocked(entry)
    tiers = {
        hit.chunk_id: hit.source_tier
        for hit in store.search(
            _query(dimensions), limit=6, allowed_document_ids=_STOCKED_DOCUMENT_IDS
        )
    }
    assert tiers["chunk-0"] is SourceTier.SYSTEM_OF_RECORD
    assert tiers["chunk-1"] is SourceTier.WEB_SUPPLEMENT


@pytest.mark.parametrize("entry", cases(VectorStorePort, Capability.INCREMENTAL_UPDATE))
def test_upserting_a_known_id_replaces_rather_than_duplicating(entry: AdapterEntry) -> None:
    """FR-RAG-05: add/update by stable ID, with no full re-index.

    A store that appends on a repeated id returns the same chunk twice, and the
    duplicate consumes a slot in every top-k after it.

    `chunk-5` is re-inserted at rank 0.5 — strictly between the two nearest
    chunks — so the assertion reads the *new* vector's position rather than a
    tie-break. Both halves have to be checked: an update that keeps the old
    vector and an update that keeps the old text are different bugs, and FR-RAG-05
    is not satisfied by either.
    """
    store, dimensions = _stocked(entry)
    store.upsert(
        chunk_ids=["chunk-5"],
        vectors=[_graded(dimensions, 0.5)],
        metadata=[
            dict(
                ChunkMetadata(
                    document_id="doc-5",
                    text="chunk 5, revised",
                    page=6,
                    source_tier=SourceTier.WEB_SUPPLEMENT,
                    category=ComponentCategory.PV_MODULES,
                    supplier="trina",
                )
            )
        ],
    )
    hits = store.search(_query(dimensions), limit=6, allowed_document_ids=_STOCKED_DOCUMENT_IDS)
    assert [hit.chunk_id for hit in hits] == [
        "chunk-0",
        "chunk-5",
        "chunk-1",
        "chunk-2",
        "chunk-3",
        "chunk-4",
    ]
    assert hits[1].text == "chunk 5, revised"


@pytest.mark.parametrize("entry", cases(VectorStorePort, Capability.INCREMENTAL_UPDATE))
def test_a_deleted_chunk_stops_being_returned(entry: AdapterEntry) -> None:
    store, dimensions = _stocked(entry)
    store.delete(["chunk-0"])
    assert "chunk-0" not in {
        hit.chunk_id
        for hit in store.search(
            _query(dimensions), limit=6, allowed_document_ids=_STOCKED_DOCUMENT_IDS
        )
    }


@pytest.mark.parametrize("entry", cases(VectorStorePort, Capability.ACCESS_FILTERING))
def test_omitting_the_allow_list_returns_nothing(entry: AdapterEntry) -> None:
    """NFR-03 / AC-8: `None` is not authorised-for-all.

    Contract tests used to pass an explicit set, so they stayed green while
    `allowed_document_ids is None` returned every stored chunk - including
    restricted documents. An omitted allow-list is a forgotten entitlement, not
    a trusted internal path. Empty set and `None` both return nothing; a caller
    who may see every document must pass that set.
    """
    store, dimensions = _stocked(entry)
    omitted = store.search(_query(dimensions), limit=6, allowed_document_ids=None)
    empty = store.search(_query(dimensions), limit=6, allowed_document_ids=set())
    assert omitted == []
    assert empty == []


@pytest.mark.parametrize("entry", cases(VectorStorePort, Capability.ACCESS_FILTERING))
def test_a_document_outside_the_allowed_set_is_never_returned(entry: AdapterEntry) -> None:
    """NFR-03 and AC-8. The leak direction."""
    store, dimensions = _stocked(entry)
    allowed = {"doc-3", "doc-4", "doc-5"}
    hits = store.search(_query(dimensions), limit=6, allowed_document_ids=allowed)
    assert {hit.document_id for hit in hits} <= allowed


@pytest.mark.parametrize("entry", cases(VectorStorePort, Capability.ACCESS_FILTERING))
def test_access_control_is_applied_inside_the_search_not_after_it(entry: AdapterEntry) -> None:
    """The property `architecture.md` invariant 8 states, tested from outside.

    "Filtering after retrieval can expose restricted content to a model or
    reranker" — but a post-filter also *under-returns*, and that is the half a
    black-box test can see. Ask for one result when the three nearest chunks are
    restricted: enforcing at retrieval yields the best permitted chunk, while
    fetching one and discarding it yields nothing. Both implementations satisfy
    the leak test above; only one satisfies this.
    """
    store, dimensions = _stocked(entry)
    hits = store.search(
        _query(dimensions), limit=1, allowed_document_ids={"doc-3", "doc-4", "doc-5"}
    )
    assert [hit.chunk_id for hit in hits] == ["chunk-3"]


@pytest.mark.parametrize("entry", cases(VectorStorePort, Capability.METADATA_FILTERING))
def test_a_metadata_filter_is_applied_inside_the_search_too(entry: AdapterEntry) -> None:
    """FR-RAG-02's filters, under the same limit=1 probe as the access filter.

    The category and supplier filters are how a comparison is scoped to one tab
    and one bidder; a post-filter here silently returns fewer rows than asked
    for, which looks like a sparse corpus rather than a bug.
    """
    store, dimensions = _stocked(entry)
    hits = store.search(
        _query(dimensions),
        limit=1,
        category=ComponentCategory.PV_MODULES,
        allowed_document_ids=_STOCKED_DOCUMENT_IDS,
    )
    assert [hit.chunk_id for hit in hits] == ["chunk-3"]

    tiered = store.search(
        _query(dimensions),
        limit=1,
        source_tier=SourceTier.WEB_SUPPLEMENT,
        allowed_document_ids=_STOCKED_DOCUMENT_IDS,
    )
    assert [hit.chunk_id for hit in tiered] == ["chunk-1"]

    supplied = store.search(
        _query(dimensions),
        limit=1,
        supplier="trina",
        allowed_document_ids=_STOCKED_DOCUMENT_IDS,
    )
    assert [hit.chunk_id for hit in supplied] == ["chunk-3"]


@pytest.mark.parametrize("entry", cases(VectorStorePort, Capability.EXHAUSTIVE_RECALL))
def test_a_filtered_top_k_still_returns_k(entry: AdapterEntry) -> None:
    """plan Decision 3a's measured finding, turned into a contract.

    The plan chose exact search because "pgvector was measured silently
    under-returning on a filtered top-k" — an approximate index walks its graph,
    applies the filter to what it happened to visit, and returns three rows when
    six qualify. Silently: no error, no warning, just a short list. An adapter
    that cannot promise this must declare the capability absent, and then the
    reason string is where the reviewer finds out that recall is approximate
    before shipping it.
    """
    store, dimensions = _stocked(entry)
    hits = store.search(
        _query(dimensions), limit=3, allowed_document_ids={"doc-3", "doc-4", "doc-5"}
    )
    assert [hit.chunk_id for hit in hits] == ["chunk-3", "chunk-4", "chunk-5"]


@pytest.mark.parametrize("entry", cases(VectorStorePort, Capability.DETERMINISTIC_OUTPUT))
def test_search_is_deterministic_for_an_unchanged_store(entry: AdapterEntry) -> None:
    """AC-7 is byte-identity from an unchanged store; retrieval is upstream of it."""
    store, dimensions = _stocked(entry)
    first = [
        hit.chunk_id
        for hit in store.search(
            _query(dimensions), limit=6, allowed_document_ids=_STOCKED_DOCUMENT_IDS
        )
    ]
    second = [
        hit.chunk_id
        for hit in store.search(
            _query(dimensions), limit=6, allowed_document_ids=_STOCKED_DOCUMENT_IDS
        )
    ]
    assert first == second


# --------------------------------------------------------------------------
# RerankerPort — FR-RAG-03, plan Decision 3b
# --------------------------------------------------------------------------


def _candidates() -> list[RetrievedChunk]:
    return [
        _Chunk(
            chunk_id=f"chunk-{rank}",
            document_id=f"doc-{rank}",
            text=text,
            page=rank + 1,
            source_tier=SourceTier.SYSTEM_OF_RECORD if rank % 2 == 0 else SourceTier.WEB_SUPPLEMENT,
            score=1.0 - rank / 10,
        )
        for rank, text in enumerate(
            [
                "The inverter is rated 352 kVA at 30 degrees Celsius.",
                "Cooling is forced air with an IP66 enclosure.",
                "Shipping carton dimensions and pallet weight.",
                "Warranty term is ten years from commissioning.",
            ]
        )
    ]


@pytest.mark.parametrize("entry", cases(RerankerPort))
def test_reranking_returns_only_chunks_it_was_given(entry: AdapterEntry) -> None:
    """A reranker reorders evidence; it may not introduce any.

    NFR-01 and FR-7 say no value exists without provenance. A chunk id that was
    not in the candidate list has no `SourceRef` behind it, so anything extracted
    from it is unsourceable by construction.
    """
    candidates = _candidates()
    ranked = _reranker(entry).rerank("rated ac power", candidates, limit=4)
    assert {hit.chunk_id for hit in ranked} <= {chunk.chunk_id for chunk in candidates}


@pytest.mark.parametrize("entry", cases(RerankerPort))
def test_reranking_rejects_a_negative_limit(entry: AdapterEntry) -> None:
    """`rescored[:limit]` with limit=-1 drops the last candidate instead of refusing."""
    with pytest.raises(ValueError, match="limit"):
        _reranker(entry).rerank("rated ac power", _candidates(), limit=-1)


@pytest.mark.parametrize("entry", cases(RerankerPort))
def test_reranking_returns_no_more_than_the_limit(entry: AdapterEntry) -> None:
    """The limit is the context budget the LLM stage is sized against."""
    assert len(_reranker(entry).rerank("rated ac power", _candidates(), limit=2)) == 2


@pytest.mark.parametrize("entry", cases(RerankerPort))
def test_reranking_nothing_returns_nothing(entry: AdapterEntry) -> None:
    """FR-RAG-04's "insufficient evidence" begins with an empty candidate set."""
    assert _reranker(entry).rerank("rated ac power", [], limit=5) == []


@pytest.mark.parametrize("entry", cases(RerankerPort))
def test_the_source_tier_survives_reranking(entry: AdapterEntry) -> None:
    """FR-RAG-03 requires the tier to stay attached "at all times", which
    includes the stage most likely to rebuild its result objects."""
    ranked = _reranker(entry).rerank("rated ac power", _candidates(), limit=4)
    tiers = {hit.chunk_id: hit.source_tier for hit in ranked}
    assert tiers["chunk-0"] is SourceTier.SYSTEM_OF_RECORD
    assert tiers["chunk-1"] is SourceTier.WEB_SUPPLEMENT


@pytest.mark.parametrize("entry", cases(RerankerPort, Capability.DETERMINISTIC_OUTPUT))
def test_reranking_is_deterministic_across_instances(entry: AdapterEntry) -> None:
    candidates = _candidates()
    first = [hit.chunk_id for hit in _reranker(entry).rerank("cooling method", candidates, limit=4)]
    second = [
        hit.chunk_id for hit in _reranker(entry).rerank("cooling method", candidates, limit=4)
    ]
    assert first == second


@pytest.mark.parametrize("entry", cases(RerankerPort, Capability.SEMANTIC_SIMILARITY))
def test_a_reranked_paraphrase_outranks_a_lexical_decoy(entry: AdapterEntry) -> None:
    """Reranking is what lets the design get away without BM25 (Decision 3b).

    The lexical leg is `tsvector`/`pg_trgm`; the reranker is the stage that is
    supposed to fix what term matching gets wrong. A reranker that is itself
    term matching adds nothing to the fusion, so this is the capability that
    decides whether the port is earning its place.
    """
    decoy = _Chunk(
        chunk_id="decoy",
        document_id="doc-decoy",
        text="module carton weight, module carton dimensions, module carton labels",
        page=1,
        source_tier=SourceTier.SYSTEM_OF_RECORD,
        score=0.5,
    )
    paraphrase = _Chunk(
        chunk_id="paraphrase",
        document_id="doc-paraphrase",
        text="each panel produces 615 watts under standard test conditions",
        page=1,
        source_tier=SourceTier.SYSTEM_OF_RECORD,
        score=0.5,
    )
    ranked = _reranker(entry).rerank("module nameplate power output", [decoy, paraphrase], limit=1)
    assert [hit.chunk_id for hit in ranked] == ["paraphrase"]


# --------------------------------------------------------------------------
# LLMPort — FR-ING-07, FR-RAG-04
# --------------------------------------------------------------------------

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rated_ac_power_kva": {"type": "number"},
        "cooling_method": {"type": "string"},
    },
    "required": ["rated_ac_power_kva", "cooling_method"],
    "additionalProperties": False,
}

_PROMPT = "Extract the inverter's rated AC power and cooling method from the context."


def _evidence(*lines: str) -> list[RetrievedChunk]:
    return [
        _Chunk(
            chunk_id=f"chunk-{index}",
            document_id="doc-0",
            text=line,
            page=index + 1,
            source_tier=SourceTier.SYSTEM_OF_RECORD,
            score=1.0,
        )
        for index, line in enumerate(lines)
    ]


@pytest.mark.parametrize("entry", cases(LLMPort, Capability.INSUFFICIENT_EVIDENCE))
def test_no_context_is_insufficient_evidence_rather_than_a_guess(entry: AdapterEntry) -> None:
    """FR-RAG-04: retrieved context only, and an explicit way to return nothing.

    `LLMPort.extract` returns `None` rather than raising precisely so that "I do
    not know" is an ordinary result. An adapter that answers from parametric
    knowledge when handed no context produces a value with no `SourceRef` behind
    it, which NFR-01 forbids and which nothing downstream can detect — the value
    looks exactly like an extracted one.
    """
    assert _llm(entry).extract(prompt=_PROMPT, context=[], json_schema=_SCHEMA) is None


@pytest.mark.parametrize("entry", cases(LLMPort, Capability.INSUFFICIENT_EVIDENCE))
def test_a_field_absent_from_the_context_is_not_invented(entry: AdapterEntry) -> None:
    """The harder half: context exists, but not the field that was asked for.

    This is where fabrication actually happens — a plausible cooling method for
    an inverter of that size is easy to produce and impossible to distinguish
    from a read one. Returning `None` for the whole extraction is the contract's
    answer; a partial object is a different design and is not what the Protocol
    declares.
    """
    context = _evidence("rated_ac_power_kva: 352")
    assert _llm(entry).extract(prompt=_PROMPT, context=context, json_schema=_SCHEMA) is None


@pytest.mark.parametrize("entry", cases(LLMPort, Capability.SCHEMA_CONSTRAINED))
def test_the_result_carries_the_required_keys_at_the_declared_types(entry: AdapterEntry) -> None:
    """FR-ING-07, and plan Decision 7's reason for `json_schema` over tool-calling.

    Keys and types both: a `"352"` where the schema says `number` passes a naive
    key check and fails `CanonicalField` validation two stages later, where the
    document that produced it is no longer in hand.
    """
    context = _evidence("rated_ac_power_kva: 352", "cooling_method: forced air")
    result = _llm(entry).extract(prompt=_PROMPT, context=context, json_schema=_SCHEMA)
    assert result is not None
    assert set(result) == {"rated_ac_power_kva", "cooling_method"}
    assert isinstance(result["rated_ac_power_kva"], float | int)
    assert isinstance(result["cooling_method"], str)


@pytest.mark.parametrize("entry", cases(LLMPort, Capability.DETERMINISTIC_OUTPUT))
def test_extraction_is_deterministic_across_instances(entry: AdapterEntry) -> None:
    """Claims are keyed by document, field and `extractor_version`.

    Two different values under one extractor version are two claims that
    disagree with no way to tell which run produced which — the append-only store
    keeps both forever. A sampling temperature is a legitimate reason to declare
    this absent; it is not a legitimate reason to leave it undeclared.
    """
    context = _evidence("rated_ac_power_kva: 352", "cooling_method: forced air")
    first = _llm(entry).extract(prompt=_PROMPT, context=context, json_schema=_SCHEMA)
    second = _llm(entry).extract(prompt=_PROMPT, context=context, json_schema=_SCHEMA)
    assert first == second


# --------------------------------------------------------------------------
# LexicalSearchPort — D-25 / P2-C3
# --------------------------------------------------------------------------

_LEXICAL_QUERY = "rated AC power"
_LEXICAL_TEXT = "rated AC power and cooling method"


def _stock_lexical(
    entry: AdapterEntry,
    *,
    texts: list[str] | None = None,
    chunk_ids: list[str] | None = None,
    document_ids: list[str] | None = None,
) -> LexicalSearchPort:
    """Stock via `LexicalStoreSamples.load`, never by naming the memory adapter.

    Six equal-scoring chunks so unfiltered top-1 is `chunk-0` (tie-break by id)
    and an allow-list of docs 3–5 plus `limit=1` yields `chunk-3`. That is the
    same inside-search probe as `VectorStorePort`, expressed without vectors.
    """
    store = _lexical(entry)
    samples = cast("LexicalStoreSamples", entry.samples)
    count = len(texts) if texts is not None else 6
    samples.load(
        store,
        chunk_ids=chunk_ids or [f"chunk-{index}" for index in range(count)],
        texts=texts or [_LEXICAL_TEXT] * count,
        document_ids=document_ids or [f"doc-{index}" for index in range(count)],
        pages=[index + 1 for index in range(count)],
        source_tiers=[
            SourceTier.SYSTEM_OF_RECORD if index % 2 == 0 else SourceTier.WEB_SUPPLEMENT
            for index in range(count)
        ],
        categories=[
            ComponentCategory.INVERTERS_PCS if index < 3 else ComponentCategory.PV_MODULES
            for index in range(count)
        ],
        suppliers=["sungrow" if index < 3 else "trina" for index in range(count)],
    )
    return store


@pytest.mark.parametrize("entry", cases(LexicalSearchPort, Capability.ACCESS_FILTERING))
def test_lexical_omitting_the_allow_list_returns_nothing(entry: AdapterEntry) -> None:
    """NFR-03 / AC-8 on the seventh port. `None` is not authorised-for-all."""
    store = _stock_lexical(entry)
    omitted = store.search_lexical(_LEXICAL_QUERY, limit=6, allowed_document_ids=None)
    empty = store.search_lexical(_LEXICAL_QUERY, limit=6, allowed_document_ids=set())
    assert omitted == []
    assert empty == []


@pytest.mark.parametrize("entry", cases(LexicalSearchPort, Capability.ACCESS_FILTERING))
def test_lexical_a_document_outside_the_allowed_set_is_never_returned(
    entry: AdapterEntry,
) -> None:
    """NFR-03 and AC-8. The leak direction."""
    store = _stock_lexical(entry)
    allowed = {"doc-3", "doc-4", "doc-5"}
    hits = store.search_lexical(_LEXICAL_QUERY, limit=6, allowed_document_ids=allowed)
    assert {hit.document_id for hit in hits} <= allowed


@pytest.mark.parametrize("entry", cases(LexicalSearchPort, Capability.ACCESS_FILTERING))
def test_lexical_access_control_is_applied_inside_the_search_not_after_it(
    entry: AdapterEntry,
) -> None:
    """Unfiltered top-1 is chunk-0; the allowed set's best permitted row is chunk-3."""
    store = _stock_lexical(entry)
    unfiltered = store.search_lexical(
        _LEXICAL_QUERY, limit=1, allowed_document_ids=_STOCKED_DOCUMENT_IDS
    )
    assert [hit.chunk_id for hit in unfiltered] == ["chunk-0"]
    hits = store.search_lexical(
        _LEXICAL_QUERY, limit=1, allowed_document_ids={"doc-3", "doc-4", "doc-5"}
    )
    assert [hit.chunk_id for hit in hits] == ["chunk-3"]


@pytest.mark.parametrize("entry", cases(LexicalSearchPort, Capability.METADATA_FILTERING))
def test_lexical_a_metadata_filter_is_applied_inside_the_search(entry: AdapterEntry) -> None:
    """FR-RAG-02's filters, under the same limit=1 probe as the access filter."""
    store = _stock_lexical(entry)
    hits = store.search_lexical(
        _LEXICAL_QUERY,
        limit=1,
        category=ComponentCategory.PV_MODULES,
        allowed_document_ids=_STOCKED_DOCUMENT_IDS,
    )
    assert [hit.chunk_id for hit in hits] == ["chunk-3"]

    supplied = store.search_lexical(
        _LEXICAL_QUERY,
        limit=1,
        supplier="trina",
        allowed_document_ids=_STOCKED_DOCUMENT_IDS,
    )
    assert [hit.chunk_id for hit in supplied] == ["chunk-3"]


@pytest.mark.parametrize("entry", cases(LexicalSearchPort, Capability.DETERMINISTIC_OUTPUT))
def test_lexical_search_is_deterministic_for_an_unchanged_store(entry: AdapterEntry) -> None:
    store = _stock_lexical(entry)
    first = [
        hit.chunk_id
        for hit in store.search_lexical(
            _LEXICAL_QUERY, limit=6, allowed_document_ids=_STOCKED_DOCUMENT_IDS
        )
    ]
    second = [
        hit.chunk_id
        for hit in store.search_lexical(
            _LEXICAL_QUERY, limit=6, allowed_document_ids=_STOCKED_DOCUMENT_IDS
        )
    ]
    assert first == second


@pytest.mark.parametrize("entry", cases(LexicalSearchPort))
def test_lexical_search_rejects_a_negative_limit(entry: AdapterEntry) -> None:
    store = _stock_lexical(entry)
    with pytest.raises(ValueError, match="limit"):
        store.search_lexical(_LEXICAL_QUERY, limit=-1, allowed_document_ids=_STOCKED_DOCUMENT_IDS)


@pytest.mark.parametrize("entry", cases(LexicalSearchPort, Capability.TRIGRAM_TOLERANCE))
def test_hyphenated_and_spaced_part_numbers_match(entry: AdapterEntry) -> None:
    """Decision 3b / D-25: token overlap alone is not enough.

    `JKM610N-66HL4M-V` is one whitespace token; `JKM610N 66HL4M V` is three. A
    store that only intersects tokens scores both the decoy and the part-number
    at 0.0, and id-sort then returns chunk-0. Trigram Jaccard is what ranks
    chunk-1 first.
    """
    hyphenated = "JKM610N-66HL4M-V"
    spaced = "JKM610N 66HL4M V"
    decoy = "cooling method forced air"
    allowed = {"doc-0", "doc-1"}

    hyphenated_store = _stock_lexical(
        entry,
        texts=[decoy, hyphenated],
        chunk_ids=["chunk-0", "chunk-1"],
        document_ids=["doc-0", "doc-1"],
    )
    hyphenated_hits = hyphenated_store.search_lexical(spaced, limit=1, allowed_document_ids=allowed)
    assert [hit.chunk_id for hit in hyphenated_hits] == ["chunk-1"]

    spaced_store = _stock_lexical(
        entry,
        texts=[decoy, spaced],
        chunk_ids=["chunk-0", "chunk-1"],
        document_ids=["doc-0", "doc-1"],
    )
    spaced_hits = spaced_store.search_lexical(hyphenated, limit=1, allowed_document_ids=allowed)
    assert [hit.chunk_id for hit in spaced_hits] == ["chunk-1"]


# --------------------------------------------------------------------------
# WebSearchPort — P2-C4 / D-20
# --------------------------------------------------------------------------


def _web_query(entry: AdapterEntry) -> str:
    return cast("WebSearchSamples", entry.samples).query


@pytest.mark.parametrize("entry", cases(WebSearchPort, Capability.DETERMINISTIC_OUTPUT))
def test_web_search_is_deterministic(entry: AdapterEntry) -> None:
    query = _web_query(entry)
    first = _web(entry).search(query, limit=3)
    second = _web(entry).search(query, limit=3)
    assert first
    assert first == second


@pytest.mark.parametrize("entry", cases(WebSearchPort))
def test_web_search_respects_limit(entry: AdapterEntry) -> None:
    adapter = _web(entry)
    query = _web_query(entry)
    full = adapter.search(query, limit=10)
    assert len(full) >= 2, "the fixture map must have enough hits for limit to bite"
    assert len(adapter.search(query, limit=1)) == 1


@pytest.mark.parametrize("entry", cases(WebSearchPort))
def test_web_search_rejects_a_negative_limit(entry: AdapterEntry) -> None:
    with pytest.raises(ValueError, match="limit"):
        _web(entry).search(_web_query(entry), limit=-1)


@pytest.mark.parametrize("entry", cases(WebSearchPort))
def test_web_hits_carry_only_webhit_fields(entry: AdapterEntry) -> None:
    """D-20: the provider list is transient; snippet and rank are not persisted."""
    hits = _web(entry).search(_web_query(entry), limit=3)
    assert hits
    expected = set(WebHit.__annotations__)
    for hit in hits:
        fields = set(getattr(type(hit), "__annotations__", {}))
        assert "snippet" not in fields
        assert "rank" not in fields
        assert expected <= fields
        assert not hasattr(hit, "snippet")
        assert not hasattr(hit, "rank")
        retrieved = hit.retrieved_at
        assert retrieved.tzinfo is not None
        assert retrieved.tzinfo.utcoffset(retrieved) is not None


@pytest.mark.parametrize("entry", cases(WebSearchPort, Capability.RATE_LIMITED))
def test_a_burst_beyond_quota_is_refused(entry: AdapterEntry) -> None:
    """Observable through search() alone: a fixture map answers every call.

    A networked backend that declares RATE_LIMITED must refuse some call in a
    no-delay burst with WebSearchRateLimitExceeded. Empty results and unrelated
    errors do not prove throttling. The memory reference is NOT_APPLICABLE and
    xfails this.
    """
    adapter = _web(entry)
    query = _web_query(entry)
    first = adapter.search(query, limit=1)
    assert first, "the fixture map must have a hit so a miss is distinguishable from a refusal"
    refusals = 0
    for _ in range(60):
        try:
            adapter.search(query, limit=1)
        except WebSearchRateLimitExceeded:
            refusals += 1
    assert refusals >= 1, "RATE_LIMITED requires a burst to be refused"


@pytest.mark.parametrize("outcome", ["error", "empty", "quota"])
def test_rate_limit_contract_requires_explicit_quota_exhaustion(outcome: str) -> None:
    """Neither an adapter crash nor a search miss demonstrates quota enforcement."""
    reference = adapters_for(WebSearchPort)[0]

    class SearchWithOutcome:
        def __init__(self) -> None:
            self.calls = 0
            self.source = _web(reference)

        def search(self, query: str, *, limit: int) -> list[WebHit]:
            self.calls += 1
            if self.calls == 2:
                if outcome == "quota":
                    raise WebSearchRateLimitExceeded("quota exhausted")
                if outcome == "error":
                    raise RuntimeError("unrelated deserialization failure")
                return []
            return self.source.search(query, limit=limit)

    entry = replace(reference, factory=SearchWithOutcome)
    if outcome == "quota":
        test_a_burst_beyond_quota_is_refused(entry)
    else:
        expected = RuntimeError if outcome == "error" else AssertionError
        with pytest.raises(expected):
            test_a_burst_beyond_quota_is_refused(entry)
