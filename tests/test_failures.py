"""The failure taxonomy (ADR-001 decision 4): closed, totally ordered, and kept honest.

The property that matters is not that each exception maps to a class but that the *order*
means something: when two signals are present the root cause wins, and every harness class
outranks every model class, so a broken harness can never be scored as a model miss.
"""

import asyncio
import importlib
import itertools
import pkgutil
import re
import subprocess
import sys
from pathlib import Path

import pytest

import procurement_agent
from procurement_agent import failures
from procurement_agent.audit.envelope import EVENT_TYPES_V1, UnknownEventTypeError
from procurement_agent.failures import (
    INFO,
    JOB_STATUSES,
    PRECEDENCE,
    ClassifiedError,
    Disposition,
    FailureClass,
    Fault,
    classify,
    classify_exception,
    log_context,
)
from procurement_agent.schema.encoding import UnencodableValueError
from procurement_agent.schema.field import DeclaredBandUnitError, VocabularyError
from procurement_agent.schema.registry import OffContractFieldError
from procurement_agent.services.claims import ProposalError, StoredValueLossError
from procurement_agent.services.conflict_hitl import (
    AutonomousOverwriteError,
    IncomparableCandidatesError,
)
from procurement_agent.services.vertical_slice import SanitizedCSVError

_JOB_DDL = Path(__file__).parent.parent / "sql" / "08_job.sql"


class ModelOutputRejected(ClassifiedError):
    """What the extraction stage would raise for a model output it validated and refused."""

    failure_class = FailureClass.SCHEMA_VIOLATION


# --- the order is a total order over the closed set -------------------------------------


def test_the_precedence_is_a_permutation_of_every_class() -> None:
    """A class missing from the tuple could never win; a duplicate makes the order ambiguous."""
    assert len(PRECEDENCE) == len(set(PRECEDENCE))
    assert set(PRECEDENCE) == set(FailureClass)


def test_unknown_is_last_so_it_can_never_mask_a_real_class() -> None:
    assert PRECEDENCE[-1] is FailureClass.UNKNOWN


def test_every_harness_class_outranks_every_data_class_and_every_model_class() -> None:
    """The reason the taxonomy exists: the groups are contiguous and ordered, so a harness
    failure present beside a model failure is always the one reported."""
    order = [INFO[c].fault for c in PRECEDENCE]
    groups = [fault for fault, _ in itertools.groupby(order)]
    assert groups == [Fault.HARNESS, Fault.DATA, Fault.MODEL, Fault.UNATTRIBUTED]


@pytest.mark.parametrize(
    ("higher", "lower"), list(itertools.combinations(PRECEDENCE, 2)), ids=lambda c: c.value
)
def test_the_higher_ranked_class_wins_in_either_order(
    higher: FailureClass, lower: FailureClass
) -> None:
    assert classify([higher, lower]) is higher
    assert classify([lower, higher]) is higher


def test_the_precedence_is_the_documented_order() -> None:
    """Pinned against a literal, not against `PRECEDENCE` itself. The pairwise test above derives
    its expectation from the tuple and so cannot notice the tuple changing; this is the one
    place a reorder is a deliberate act that has to touch the test and the module docstring."""
    assert [c.value for c in PRECEDENCE] == [
        "contract_violation",
        "store_failure",
        "timeout",
        "adapter_failure",
        "unparseable_document",
        "invalid_input",
        "schema_violation",
        "unknown",
    ]


class _StoreDown(ClassifiedError):
    failure_class = FailureClass.STORE_FAILURE


class _AdapterDown(ClassifiedError):
    failure_class = FailureClass.ADAPTER_FAILURE


def test_a_store_that_timed_out_reports_the_store_not_the_timeout() -> None:
    """The documented way for a store to say it was the one timing out."""
    try:
        try:
            raise TimeoutError("query exceeded its bound")
        except TimeoutError as cause:
            raise _StoreDown("vector store") from cause
    except _StoreDown as wrapped:
        assert classify_exception(wrapped) is FailureClass.STORE_FAILURE


def test_an_adapter_call_that_timed_out_is_a_timeout_not_an_adapter_failure() -> None:
    """The module docstring says an adapter's tag cannot win over the timeout beneath it, so
    adapter authors should not bother tagging a timeout. This pins that statement."""
    try:
        try:
            raise TimeoutError("ocr call exceeded its bound")
        except TimeoutError as cause:
            raise _AdapterDown("ocr") from cause
    except _AdapterDown as wrapped:
        assert classify_exception(wrapped) is FailureClass.TIMEOUT


def test_no_evidence_is_unknown() -> None:
    assert classify([]) is FailureClass.UNKNOWN


# --- each class is wired to what the DDL actually commits to ---------------------------


def test_every_class_has_exactly_one_info_row() -> None:
    assert set(INFO) == set(FailureClass)


def test_every_audit_event_type_is_one_the_taxonomy_already_defines() -> None:
    """D-13 section 5: additions are by amendment and removals are forbidden, so a class
    must name an existing event type rather than invent one."""
    for failure_class, info in INFO.items():
        assert info.audit_event_type in EVENT_TYPES_V1, failure_class


def test_an_unreadable_document_records_parse_failure_and_nothing_else_does() -> None:
    parse_failures = {c for c, info in INFO.items() if info.audit_event_type == "parse_failure"}
    assert parse_failures == {FailureClass.UNPARSEABLE_DOCUMENT}


def _ddl_job_statuses() -> set[str]:
    # Anchored on the column, whitespace-tolerant, so a reformat or an earlier CHECK on another
    # column cannot make this read the wrong list.
    check = re.search(r"CHECK\s*\(\s*status\s+IN\s*\(([^)]*)\)", _JOB_DDL.read_text(), re.S)
    assert check is not None, "sql/08_job.sql no longer has a `CHECK (status IN (...))`"
    return set(re.findall(r"'(\w+)'", check.group(1)))


def test_every_disposition_names_the_job_statuses_it_may_write() -> None:
    assert set(JOB_STATUSES) == set(Disposition)


def test_every_status_a_disposition_writes_is_one_the_ddl_accepts() -> None:
    """RETRY is 'pending' then 'failed' (I.2) and QUARANTINE is 'quarantined' (I.4). If the
    CHECK drops one, the runner would write a status the table refuses."""
    written = {status for statuses in JOB_STATUSES.values() for status in statuses}
    assert written <= _ddl_job_statuses()


def test_quarantine_writes_exactly_the_quarantined_status() -> None:
    assert JOB_STATUSES[Disposition.QUARANTINE] == ("quarantined",)


def test_transient_infrastructure_failures_retry_and_deterministic_ones_do_not() -> None:
    retried = {c for c, info in INFO.items() if info.disposition is Disposition.RETRY}
    assert retried == {
        FailureClass.STORE_FAILURE,
        FailureClass.TIMEOUT,
        FailureClass.ADAPTER_FAILURE,
        FailureClass.SCHEMA_VIOLATION,
    }


def test_an_unrecognised_failure_is_quarantined_not_retried() -> None:
    """The repository's defaults fail loud; an unknown failure should reach a human."""
    assert INFO[FailureClass.UNKNOWN].disposition is Disposition.QUARANTINE


# --- exceptions classify by type, through wrappers, never by message -------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (AutonomousOverwriteError("x"), FailureClass.CONTRACT_VIOLATION),
        (IncomparableCandidatesError("x"), FailureClass.CONTRACT_VIOLATION),
        (StoredValueLossError("x"), FailureClass.CONTRACT_VIOLATION),
        (UnknownEventTypeError("x"), FailureClass.CONTRACT_VIOLATION),
        (UnencodableValueError("x"), FailureClass.CONTRACT_VIOLATION),
        (TimeoutError("x"), FailureClass.TIMEOUT),
        (SanitizedCSVError("x"), FailureClass.INVALID_INPUT),
        (RuntimeError("x"), FailureClass.UNKNOWN),
        # Validation rejections: the same type is raised for supplier data, a caller bug and
        # model output, so the type alone is not evidence. Pinned so none is quietly registered.
        (OffContractFieldError("x"), FailureClass.UNKNOWN),
        (VocabularyError("x"), FailureClass.UNKNOWN),
        (DeclaredBandUnitError("x"), FailureClass.UNKNOWN),
    ],
    ids=lambda v: type(v).__name__ if isinstance(v, BaseException) else v.value,
)
def test_an_exception_classifies_by_its_type(exc: BaseException, expected: FailureClass) -> None:
    assert classify_exception(exc) is expected


def test_a_subclass_inherits_its_parents_class() -> None:
    class DeeperLoss(StoredValueLossError):
        pass

    assert classify_exception(DeeperLoss("x")) is FailureClass.CONTRACT_VIOLATION


def test_a_wrapper_carries_no_evidence_of_its_own() -> None:
    """`services.claims` re-raises other errors as a ProposalError. Classifying the wrapper
    would blame the wrong party, so the error it wraps decides."""
    assert classify_exception(ProposalError("x")) is FailureClass.UNKNOWN
    try:
        try:
            raise ModelOutputRejected("not in the contract")
        except ModelOutputRejected as cause:
            raise ProposalError("cannot project") from cause
    except ProposalError as wrapped:
        assert classify_exception(wrapped) is FailureClass.SCHEMA_VIOLATION


def test_the_explicit_programming_error_subclass_is_not_absorbed_by_its_wrapper_parent() -> None:
    assert classify_exception(StoredValueLossError("x")) is FailureClass.CONTRACT_VIOLATION


def test_a_harness_cause_outranks_the_model_class_that_accompanied_it() -> None:
    """A timeout that surfaced as an off-contract value is a timeout, not a model miss."""
    try:
        try:
            raise TimeoutError("llm call exceeded its bound")
        except TimeoutError as cause:
            raise ModelOutputRejected("truncated output") from cause
    except ModelOutputRejected as wrapped:
        assert classify_exception(wrapped) is FailureClass.TIMEOUT


def test_a_cause_beneath_an_unrecognised_wrapper_is_still_found() -> None:
    try:
        try:
            raise TimeoutError("x")
        except TimeoutError as cause:
            raise RuntimeError("stage failed") from cause
    except RuntimeError as wrapped:
        assert classify_exception(wrapped) is FailureClass.TIMEOUT


def test_the_implicit_context_chain_counts_as_well_as_the_explicit_cause() -> None:
    try:
        try:
            raise TimeoutError("x")
        except TimeoutError:
            raise RuntimeError("raised while handling")  # noqa: B904 - exercising __context__
    except RuntimeError as wrapped:
        assert wrapped.__cause__ is None
        assert classify_exception(wrapped) is FailureClass.TIMEOUT


def test_a_failure_inside_an_exception_group_is_found() -> None:
    """A timeout under `asyncio.TaskGroup` surfaces as a group whose members are linked by
    neither __cause__ nor __context__; without walking `.exceptions` it was UNKNOWN and was
    quarantined instead of retried."""
    group = ExceptionGroup("stage", [TimeoutError("llm")])
    assert group.__cause__ is None and group.__context__ is None
    assert classify_exception(group) is FailureClass.TIMEOUT


def test_a_group_mixing_harness_and_model_failures_reports_the_harness() -> None:
    group = ExceptionGroup("stage", [ModelOutputRejected("a"), TimeoutError("b")])
    assert classify_exception(group) is FailureClass.TIMEOUT


def test_a_nested_exception_group_is_walked() -> None:
    inner = ExceptionGroup("inner", [TimeoutError("x")])
    assert classify_exception(ExceptionGroup("outer", [inner])) is FailureClass.TIMEOUT


def test_a_context_the_author_severed_with_from_none_is_not_evidence() -> None:
    """`raise X from None` says the new error is unrelated; Python's traceback honours it, and
    so must the classifier or a disowned timeout decides the class."""
    try:
        try:
            raise TimeoutError("x")
        except TimeoutError:
            raise ModelOutputRejected("unrelated") from None
    except ModelOutputRejected as wrapped:
        assert wrapped.__suppress_context__ is True
        assert classify_exception(wrapped) is FailureClass.SCHEMA_VIOLATION


def test_an_explicit_cause_is_still_followed_when_the_context_is_suppressed() -> None:
    """`raise X from cause` also sets __suppress_context__, but the cause is deliberate."""
    try:
        try:
            raise TimeoutError("x")
        except TimeoutError as cause:
            raise ModelOutputRejected("y") from cause
    except ModelOutputRejected as wrapped:
        assert wrapped.__suppress_context__ is True
        assert classify_exception(wrapped) is FailureClass.TIMEOUT


def test_a_cyclic_chain_terminates() -> None:
    a, b = RuntimeError("a"), RuntimeError("b")
    a.__cause__, b.__cause__ = b, a
    assert classify_exception(a) is FailureClass.UNKNOWN


def _wrapped(inner: Exception, depth: int) -> Exception:
    head = inner
    for _ in range(depth):
        wrapper = RuntimeError("w")
        wrapper.__cause__ = head
        head = wrapper
    return head


def test_a_chain_longer_than_the_cap_fails_loud_rather_than_guessing() -> None:
    """The cause is past the cap, so it is not read. Nothing may be reported with confidence."""
    deep = _wrapped(TimeoutError("deep"), failures._MAX_LINKS * 4)
    assert classify_exception(deep) is FailureClass.UNKNOWN


def test_a_lower_class_found_early_is_not_trusted_when_a_higher_one_may_be_unread() -> None:
    """The invariant the module exists for: a model miss must never be reported while a
    harness cause sits unread further down. Before the fix this returned SCHEMA_VIOLATION."""
    top = ModelOutputRejected("rejected")
    top.__cause__ = _wrapped(TimeoutError("harness cause"), failures._MAX_LINKS + 5)
    assert classify_exception(top) is FailureClass.UNKNOWN


def test_the_top_class_is_trusted_even_when_the_walk_is_cut_short() -> None:
    """Nothing outranks CONTRACT_VIOLATION, so finding it settles the verdict regardless of what
    remains unread."""
    top = AutonomousOverwriteError("invariant")
    top.__cause__ = _wrapped(TimeoutError("irrelevant"), failures._MAX_LINKS + 5)
    assert classify_exception(top) is FailureClass.CONTRACT_VIOLATION


def test_a_chain_exactly_at_the_cap_is_read_in_full() -> None:
    at_cap = _wrapped(TimeoutError("cause"), failures._MAX_LINKS - 1)
    assert classify_exception(at_cap) is FailureClass.TIMEOUT
    past_cap = _wrapped(TimeoutError("cause"), failures._MAX_LINKS)
    assert classify_exception(past_cap) is FailureClass.UNKNOWN


def test_members_sharing_a_cause_do_not_spend_the_budget_re_reading_it() -> None:
    """Only newly visited exceptions count. 40 siblings share one cause, so the walk pops it 40
    times but visits it once; the 45 distinct links fit under the cap, while the ~85 pops do
    not. The harness evidence is in the member popped last, so it is only reached if the
    repeats were free."""
    shared = RuntimeError("shared")
    siblings: list[Exception] = []
    for _ in range(40):
        sibling = RuntimeError("sibling")
        sibling.__cause__ = shared
        siblings.append(sibling)
    group = ExceptionGroup("g", [_wrapped(TimeoutError("real"), 2), *siblings])
    assert classify_exception(group) is FailureClass.TIMEOUT


@pytest.mark.parametrize(
    "interruption", [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit()], ids=repr
)
def test_an_interruption_is_not_a_failure_and_classifies_as_unknown(
    interruption: BaseException,
) -> None:
    """Pinned so the behaviour is a decision rather than an accident: the runner must handle a
    cancellation or lost lease before classifying, or it would quarantine an interrupted job."""
    assert classify_exception(interruption) is FailureClass.UNKNOWN


def test_the_message_is_never_read() -> None:
    """A classification from text would be forgeable by whatever an exception happens to say."""
    assert classify_exception(RuntimeError("timeout store_failure contract_violation")) is (
        FailureClass.UNKNOWN
    )
    assert classify_exception(TimeoutError("")) is FailureClass.TIMEOUT


# --- importing the taxonomy must not load the package it classifies ----------------------


def test_importing_the_taxonomy_does_not_import_the_services() -> None:
    """An adapter imports `ClassifiedError` to tag its errors, and a service will too. If the
    taxonomy imported the services at module level that would be a circular import, and every
    adapter would load the workbook writer just to raise a tagged error."""
    code = (
        "import sys, procurement_agent.failures;"
        "print(sorted(m for m in sys.modules if m.startswith('procurement_agent.services')))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"


# --- adapters state their own class -----------------------------------------------------


def test_an_adapter_error_classifies_by_its_tag() -> None:
    class OcrCrashed(ClassifiedError):
        failure_class = FailureClass.ADAPTER_FAILURE

    assert classify_exception(OcrCrashed("x")) is FailureClass.ADAPTER_FAILURE


def test_the_tag_is_read_from_the_class_not_the_instance() -> None:
    """The tripwire builds instances without `__init__`; classification must not depend on
    state that only `__init__` could have set."""

    class Plain(ClassifiedError):
        pass

    instance = Plain("x")
    instance.failure_class = FailureClass.ADAPTER_FAILURE  # type: ignore[misc]
    assert classify_exception(instance) is FailureClass.UNKNOWN


def test_an_untagged_classified_error_is_no_evidence_rather_than_a_guess() -> None:
    class Untagged(ClassifiedError):
        pass

    assert classify_exception(Untagged("x")) is FailureClass.UNKNOWN
    try:
        try:
            raise TimeoutError("x")
        except TimeoutError as cause:
            raise Untagged("y") from cause
    except Untagged as wrapped:
        assert classify_exception(wrapped) is FailureClass.TIMEOUT


# --- the context handed to the logger ---------------------------------------------------


def test_the_log_context_names_the_class_and_never_the_message() -> None:
    context = log_context(TimeoutError("dsn=postgres://u:hunter2-sentinel@db/x"))
    assert context == {
        "failure_class": "timeout",
        "fault": "harness",
        "disposition": "retry",
        "exception_type": "builtins.TimeoutError",
    }
    assert "hunter2-sentinel" not in "".join(context.values())


# --- the tripwire: a new exception cannot slip past the taxonomy -----------------------

#: Exceptions deliberately left unclassified, each with the reason. An entry that becomes
#: classified fails the test below, so this list cannot rot into a second source of truth.
_UNCLASSIFIED_BY_DESIGN = {
    "procurement_agent.failures.ClassifiedError": (
        "the base class of tagged exceptions; a subclass states the class, the base does not"
    ),
    "procurement_agent.services.claims.ProposalError": (
        "a wrapper: it re-raises other errors, so the error it wraps decides"
    ),
    "procurement_agent.schema.registry.OffContractFieldError": (
        "a field key outside the frozen contract: raised for supplier data, a caller bug and "
        "model output alike. The raise site knows which and tags it"
    ),
    "procurement_agent.schema.field.VocabularyError": (
        "a condition dimension that is neither text nor a member: from supplier text, a caller "
        "bug or model output. The type alone does not say"
    ),
    "procurement_agent.schema.field.DeclaredBandUnitError": (
        "raised when a band meets a nominal in another unit. Its docstring does not say whether "
        "that is a supplier-data fault or a caller bug, and there is no pipeline raise site yet "
        "to say; classifying it now would be a guess"
    ),
}


def _package_exceptions() -> dict[str, type[BaseException]]:
    found: dict[str, type[BaseException]] = {}
    for module_info in pkgutil.walk_packages(procurement_agent.__path__, "procurement_agent."):
        if module_info.name.endswith("__main__"):
            continue  # a CLI entry point, not a library module
        module = importlib.import_module(module_info.name)
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and issubclass(value, BaseException)
                and value.__module__ == module.__name__
            ):
                found[f"{value.__module__}.{value.__qualname__}"] = value
    return found


def test_the_tripwire_actually_finds_exceptions() -> None:
    """Guards the parametrisation below: an empty walk would make the next test vacuous."""
    assert len(_package_exceptions()) >= 10


def test_every_exception_in_the_package_is_classified_or_listed_with_a_reason() -> None:
    unaccounted = []
    for name, exc_type in _package_exceptions().items():
        classified = classify_exception(BaseException.__new__(exc_type)) is not (
            FailureClass.UNKNOWN
        )
        if not classified and name not in _UNCLASSIFIED_BY_DESIGN:
            unaccounted.append(name)
    assert not unaccounted, (
        f"{unaccounted} classify as UNKNOWN. Register each in failures._BY_TYPE, give it a "
        "ClassifiedError tag, or add it to _UNCLASSIFIED_BY_DESIGN with the reason."
    )


def test_no_exception_listed_as_unclassified_is_in_fact_classified() -> None:
    package = _package_exceptions()
    for name in _UNCLASSIFIED_BY_DESIGN:
        assert name in package, f"{name} no longer exists; remove it from the list"
        assert classify_exception(BaseException.__new__(package[name])) is FailureClass.UNKNOWN, (
            f"{name} is classified now; remove it from _UNCLASSIFIED_BY_DESIGN"
        )
