"""Phase 2 Track 0 — the nine additive contract freeze (P2-C1..P2-C9).

Named Verify tests from phase-2-execution.md. Each pin is the thing a later
track compiles against; a silent reshape here is a broken consumer, not a
style change.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import re
from datetime import UTC, datetime
from typing import Literal, get_args, get_origin, get_type_hints

import pytest
from pydantic import ValidationError

from procurement_agent.adapters.lexical_store.memory import InMemoryLexicalStore
from procurement_agent.adapters.parsed_element import TextElement
from procurement_agent.adapters.vector_store import ChunkMetadata
from procurement_agent.config import Settings
from procurement_agent.ports import ParsedElement, WebHit
from procurement_agent.schema import (
    EXTRACTOR_VERSION_PREFIXES,
    GOLD_EXTRACTOR_PREFIX,
    HUMAN_EXTRACTOR_PREFIX,
    WEB_EXTRACTOR_PREFIX,
    ChunkRecord,
    PrincipalContext,
    SourceTier,
    TableData,
)
from procurement_agent.schema.enums import ComponentCategory
from procurement_agent.schema.registry import (
    FIELD_SPECS,
    FieldScope,
    FieldSpec,
    Shape,
    ValueType,
)
from procurement_agent.schema.sql_freeze import (
    CLAIM_HUMAN_RESOLUTION_CHECK,
    CLAIM_HUMAN_RESOLUTION_CONSTRAINT,
    RUN_EVENT_TYPES,
    SQL_RESERVED_FILES,
)
from procurement_agent.services import indexing
from procurement_agent.services.claims import HUMAN_PREFIX

REPO = pathlib.Path(__file__).resolve().parent.parent

# Spec order, not frozenset order. The README pin is this phrase, because each
# token alone already occurs in the removal sentence or later in the file.
_RUN_EVENT_ORDER = (
    "web_search",
    "compose_override",
    "run_started",
    "run_finished",
)


def _track0_freeze_section(readme: str) -> str:
    """Text under the Track 0 heading, stopping at the next heading.

    ``split(...)[1]`` is the rest of the file. Decision 6 still says
    ``web_search``, so an unbounded slice does not pin the run_event list.
    """
    parts = readme.split("## Track 0 freeze", 1)
    if len(parts) != 2:
        raise AssertionError("sql/README.md must have a Track 0 freeze section")
    body = parts[1]
    next_heading = body.find("\n## ")
    return body if next_heading < 0 else body[:next_heading]


def _pins_run_event_list(section: str) -> bool:
    """The four types in spec order, as one backticked list.

    Each name on its own is not a pin: ``web_search`` is also the value
    ``sql/12`` removes from ``audit.event``, and Decision 6 repeats it.
    """
    listed = ", ".join(f"`{name}`" for name in _RUN_EVENT_ORDER)
    return listed in section


def test_parsed_element_annotations_include_optional_bbox_table_page_quality_role() -> None:
    """P2-C1: the Protocol carries the four fields FR-ING-04/05 could not state."""
    assert set(ParsedElement.__annotations__) == {
        "kind",
        "text",
        "page",
        "bbox",
        "table",
        "page_quality",
        "role",
    }
    hints = get_type_hints(ParsedElement)
    assert hints["bbox"] == tuple[float, float, float, float] | None
    assert hints["table"] == TableData | None
    assert hints["page_quality"] == float | None
    assert hints["role"] == Literal["body", "furniture", "footnote", "caption"]


def test_chunk_record_fields() -> None:
    """P2-C2: story-2 §1, imported from the schema package, not a second home."""
    assert ChunkRecord.__module__ == "procurement_agent.schema.chunk"
    assert set(ChunkRecord.__annotations__) == {
        "chunk_id",
        "document_id",
        "kind",
        "text",
        "body",
        "context_prefix",
        "page",
        "section",
        "table_id",
        "ordinal",
    }
    kind = get_type_hints(ChunkRecord)["kind"]
    assert get_origin(kind) is Literal
    assert set(get_args(kind)) == {"prose", "table_full", "table_row", "table_summary"}


def test_chunk_metadata_gains_chunk_kind_table_id_section() -> None:
    """P2-C2: upsert vocabulary grows; existing keys stay."""
    keys = set(ChunkMetadata.__annotations__)
    assert {
        "document_id",
        "text",
        "page",
        "source_tier",
        "category",
        "supplier",
        "chunk_kind",
        "table_id",
        "section",
    } == keys
    assert ChunkMetadata.__required_keys__ == frozenset(
        {"document_id", "text", "page", "source_tier", "category", "supplier"}
    )
    assert ChunkMetadata.__optional_keys__ == frozenset({"chunk_kind", "table_id", "section"})


def test_chunk_returns_list_of_chunk_record() -> None:
    """P2-C2: return type only. The body is still Track 2's."""
    assert get_type_hints(indexing.chunk)["return"] == list[ChunkRecord]
    with pytest.raises(NotImplementedError):
        indexing.chunk([], size_tokens=512, overlap_ratio=0.05)


def test_lexical_search_port_none_allowlist_returns_nothing() -> None:
    """P2-C3: the VectorStorePort rule, on the seventh Protocol."""
    store = InMemoryLexicalStore()
    store.upsert(
        chunk_ids=["chunk-0"],
        texts=["JKM610N-66HL4M-V rated power"],
        document_ids=["doc-0"],
        pages=[1],
        source_tiers=[SourceTier.SYSTEM_OF_RECORD],
        categories=[ComponentCategory.PV_MODULES],
        suppliers=["jinko"],
    )
    omitted = store.search_lexical("JKM610N", limit=6, allowed_document_ids=None)
    empty = store.search_lexical("JKM610N", limit=6, allowed_document_ids=set())
    allowed = store.search_lexical("JKM610N", limit=6, allowed_document_ids={"doc-0"})
    assert omitted == []
    assert empty == []
    assert [hit.document_id for hit in allowed] == ["doc-0"]


def test_web_hit_has_no_snippet_or_rank() -> None:
    """P2-C4 / D-20: the provider list is transient; only the URL is kept."""
    assert set(WebHit.__annotations__) == {"url", "title", "retrieved_at", "provider"}
    assert "snippet" not in WebHit.__annotations__
    assert "rank" not in WebHit.__annotations__


def test_web_hit_retrieved_at_rejects_naive_datetimes() -> None:
    """WebHit is the first boundary that produces the timestamp; naive is refused."""
    aware = datetime(2020, 1, 1, tzinfo=UTC)
    hit = WebHit(
        url="https://example.invalid/jinko",
        title="JKM610N",
        retrieved_at=aware,
        provider="memory",
    )
    assert hit.retrieved_at is aware

    with pytest.raises(ValueError, match="timezone-aware"):
        WebHit(
            url="https://example.invalid/jinko",
            title="JKM610N",
            retrieved_at=datetime(2020, 1, 1),  # noqa: DTZ001 - the point of the test
            provider="memory",
        )


def test_principal_context_is_the_schema_type() -> None:
    """P2-C5: one class, in schema/. 4a must not mint a second under services/."""
    assert PrincipalContext.__module__ == "procurement_agent.schema.principal"
    hints = get_type_hints(PrincipalContext)
    assert hints["subject"] is str
    assert hints["cleared_for_restricted"] is bool
    assert hints["denied_suppliers"] == frozenset[str]
    parameters = inspect.signature(PrincipalContext).parameters
    assert parameters["denied_suppliers"].default == frozenset()

    services = REPO / "src" / "procurement_agent" / "services"
    duplicated = [
        path
        for path in services.rglob("*.py")
        if "class PrincipalContext" in path.read_text(encoding="utf-8")
    ]
    assert not duplicated, f"PrincipalContext class body under services/: {duplicated}"


def test_sql_readme_reserves_10_through_14() -> None:
    """P2-C6/C7: reservation rows, not the DDL files. 4a/7 write those.

    The freeze strings live in a dedicated section so ``"10" in readme`` and
    ``"web_search" in readme`` cannot pass on "PostgreSQL 17.10" or the v1
    ``audit.event`` taxonomy.
    """
    readme = (REPO / "sql" / "README.md").read_text(encoding="utf-8")
    freeze = _track0_freeze_section(readme)
    for name in SQL_RESERVED_FILES:
        assert name in freeze, f"sql/README.md does not reserve {name}"
    sql_dir = REPO / "sql"
    leaked = sorted(p.name for p in sql_dir.glob("[0-9][0-9]_*.sql") if p.name >= "10_")
    assert leaked == [], f"Track 0 must not write sql/10–14; found {leaked}"
    assert CLAIM_HUMAN_RESOLUTION_CONSTRAINT in freeze
    assert CLAIM_HUMAN_RESOLUTION_CHECK in freeze
    assert set(_RUN_EVENT_ORDER) == set(RUN_EVENT_TYPES)
    assert _pins_run_event_list(freeze), (
        "the Track 0 section must list the run_event types as one phrase; "
        "a later `web_search` or the removal sentence is not that list"
    )
    assert "drops `recorded_at`'s DEFAULT" in freeze or "drops recorded_at's DEFAULT" in freeze


def test_scattered_web_search_mentions_do_not_pin_the_run_event_list() -> None:
    """Dropping ``web_search`` from the type list still left it in the removal sentence."""
    decoy = """
## Track 0 freeze

`12_audit_event_taxonomy.sql` removes `web_search` from `audit.event` and
mentions compose_override, run_started and run_finished in prose.

## Later

`web_search`, `compose_override`, `run_started`, `run_finished`
"""
    assert not _pins_run_event_list(_track0_freeze_section(decoy))


def test_sql_apply_glob_is_two_digit() -> None:
    """P2-C6/C7: `0*.sql` silently skips `10_`…`14_`. Two-digit is the apply order."""
    needle = "[0-9][0-9]_*.sql"
    readme = (REPO / "sql" / "README.md").read_text(encoding="utf-8")
    assert needle in readme
    assert "sql/0*.sql" not in readme

    schema = (REPO / "tests" / "test_sql_schema.py").read_text(encoding="utf-8")
    behaviour = (REPO / "tests" / "test_sql_behaviour.py").read_text(encoding="utf-8")
    audit = (REPO / "tests" / "test_audit_live.py").read_text(encoding="utf-8")
    for source, name in (
        (schema, "test_sql_schema.py"),
        (behaviour, "test_sql_behaviour.py"),
        (audit, "test_audit_live.py"),
    ):
        assert 'glob("[0-9][0-9]_*.sql")' in source, f"{name} still applies 0*.sql"


def test_extractor_version_prefixes() -> None:
    """P2-C8: the four prefixes, pinned. No claim is written here."""
    assert HUMAN_EXTRACTOR_PREFIX == "human:"
    assert WEB_EXTRACTOR_PREFIX == "web:"
    assert GOLD_EXTRACTOR_PREFIX == "gold:"
    assert HUMAN_PREFIX == HUMAN_EXTRACTOR_PREFIX
    assert EXTRACTOR_VERSION_PREFIXES == (
        "<pipeline>@<semver-or-hash>",
        f"{HUMAN_EXTRACTOR_PREFIX}<oidc-sub>",
        f"{WEB_EXTRACTOR_PREFIX}<provider>@<version>",
        f"{GOLD_EXTRACTOR_PREFIX}<annotator>",
    )
    assert EXTRACTOR_VERSION_PREFIXES[1].startswith(HUMAN_EXTRACTOR_PREFIX)
    assert EXTRACTOR_VERSION_PREFIXES[2].startswith(WEB_EXTRACTOR_PREFIX)
    assert EXTRACTOR_VERSION_PREFIXES[3].startswith(GOLD_EXTRACTOR_PREFIX)


def test_fieldspec_has_optional_web_query_template() -> None:
    """P2-C9: silence is the default; Story 3 fills templates, not this freeze."""
    field = FieldSpec.model_fields["web_query_template"]
    assert field.annotation == str | None
    assert field.default is None
    assert all(spec.web_query_template is None for spec in FIELD_SPECS)
    assert FieldSpec.model_config.get("extra") == "forbid"
    with pytest.raises(ValidationError):
        FieldSpec.model_validate(
            {
                "key": "nameplate_power_w",
                "shape": Shape.SCALAR,
                "value_type": ValueType.FLOAT,
                "categories": frozenset({ComponentCategory.PV_MODULES}),
                "scope": FieldScope.CATEGORY,
                "web_query_key": "{manufacturer} {model}",
            }
        )


def test_no_parallel_web_query_key_on_another_schema_type() -> None:
    """P2-C9: FieldSpec is the only home for a web query template."""
    schema = REPO / "src" / "procurement_agent" / "schema"
    offenders: list[str] = []
    for path in schema.rglob("*.py"):
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "web_query" not in line:
                continue
            if path.name == "registry.py" and "web_query_template" in line:
                continue
            offenders.append(f"{path.relative_to(REPO)}:{line_no}:{line.strip()}")
    assert offenders == [], "a second web-query key leaked onto another type:\n" + "\n".join(
        offenders
    )


def test_current_state_baseline_is_the_post_36_count() -> None:
    """P2-A-8: the measured baseline this worktree started from, not the 1000 pin."""
    text = (REPO / "docs" / "current-state.md").read_text(encoding="utf-8")
    assert "1039 passed" in text or "1039 passing" in text
    assert "42 skipped" in text
    assert "4 xfailed" in text or "4 expected failures" in text
    assert "**1000 passing" not in text


def test_plan_decision_4_names_pypdfium2_and_libreoffice_for_word() -> None:
    """P2-A-18: the probe is pypdfium2; Word pages come from LibreOffice → PDF."""
    plan = (REPO / "specs" / "001-procurement-agent" / "plan.md").read_text(encoding="utf-8")
    start = plan.index("## Decision 4")
    end = plan.index("\n## Decision 5")
    decision_4 = plan[start:end]
    assert "pypdfium2" in decision_4
    assert "LibreOffice" in decision_4
    assert "raw PyMuPDF text count" not in decision_4
    assert "MSWord backend" not in decision_4 or "LibreOffice" in decision_4


def test_decision_10_names_the_settings_field_that_exists() -> None:
    """The quota knob is ``web_search_rate_limit_per_minute``. There is no shorter alias."""
    plan = (REPO / "specs" / "001-procurement-agent" / "plan.md").read_text(encoding="utf-8")
    start = plan.index("## Decision 10")
    end = plan.index("\n## Revised dependency")
    decision = plan[start:end]
    assert "web_search_rate_limit_per_minute" in decision
    assert re.search(r"web_search_rate_limit(?!_per_minute)", decision) is None
    assert "web_search_rate_limit_per_minute" in Settings.model_fields
    assert "web_search_rate_limit" not in Settings.model_fields


def test_current_decision_10_count_files_do_not_claim_six() -> None:
    """P2-A-24: Decision 10's current count is eight. Historical Phase 1 may say six."""
    files = (
        REPO / "src" / "procurement_agent" / "ports" / "__init__.py",
        REPO / "tests" / "port_contracts" / "test_conformance_matrix.py",
        REPO / "tests" / "port_contracts" / "test_port_contracts.py",
        REPO / "docs" / "decisions" / "ADR-001-cross-repo-pattern-adoption.md",
        REPO / "docs" / "current-state.md",
        REPO / "src" / "procurement_agent" / "adapters" / "registry.py",
        REPO / "src" / "procurement_agent" / "adapters" / "__init__.py",
        REPO / "specs" / "001-procurement-agent" / "plan.md",
    )
    phrases = (
        "six port",
        "six Protocol",
        "six swap",
        "Six interfaces",
        "six Protocols",
        "the six ports",
        "the six port",
    )
    offenders: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        for phrase in phrases:
            if phrase.lower() in lowered:
                offenders.append(f"{path.relative_to(REPO)}: {phrase!r}")
    assert not offenders, "current Decision 10 count files still claim six:\n" + "\n".join(
        offenders
    )


def test_no_principal_context_class_body_under_services() -> None:
    """Companion to P2-C5: AST, so a comment cannot satisfy the grep pin."""
    services = REPO / "src" / "procurement_agent" / "services"
    hits: list[str] = []
    for path in services.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "PrincipalContext":
                hits.append(str(path.relative_to(REPO)))
    assert hits == []


def test_text_element_rejects_an_unknown_role() -> None:
    """P2-C1 role is a closed vocabulary; the reference must not accept 'header'."""
    with pytest.raises(ValueError, match="role"):
        TextElement(kind="body", text="x", page=1, role="header")  # type: ignore[arg-type]


def test_text_element_table_kind_requires_table_data() -> None:
    """Present iff kind == 'table' — a table element without TableData is a flatten."""
    with pytest.raises(ValueError, match="table"):
        TextElement(kind="table", text="Parameter | Value", page=1)
