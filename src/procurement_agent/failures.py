"""A closed failure taxonomy with a fixed precedence (ADR-001 decision 4).

Nothing in this repository could say *why* an attempt failed. The runner (contract C8) will
need that to choose between retrying and quarantining, and the gold-set eval harness (B.9)
will need it to keep a broken harness out of an accuracy figure. This module is the one place
that decides, so the two cannot disagree.

**The rule this exists for: a failure is classified before its evidence reaches a report.**
A harness bug scored as a model miss poisons every downstream number, and the mistake is
invisible unless a rule says which classification wins when more than one signal is present.
The pattern is predict-rlm's `telemetry.py` (a closed class tuple, a fixed `_PRECEDENCE`,
the highest-ranked class present wins); the classes below are this repository's own, each
grounded in something that exists.

Precedence runs root-cause-first, so a class outranks the ones it can *cause*:

==================  ==========================================================================
contract_violation  Our code broke one of its own invariants. Every other signal in the same
                    attempt may be a consequence, and no number from it can be trusted.
store_failure       A hung or failing database shows up downstream as a timeout or an
                    unparseable result; the store is the cause.
timeout             A deadline expired (a `TimeoutError`). Outranks `adapter_failure`: "it was
                    slow" is the more informative fact, and it names the bound to look at. Only
                    `store_failure` outranks it, so a store that was the one timing out tags
                    its error that way. An adapter whose call timed out is a `timeout`; it does
                    not need, and cannot win with, an `adapter_failure` tag.
adapter_failure     A port adapter raised, or its endpoint was unreachable. Outranks the
                    document classes because a parser *crashing* on a file looks exactly like a
                    file that cannot be parsed.
unparseable_doc...  The document itself cannot be read (corrupt, encrypted, unsupported).
invalid_input       A value was rejected at the contract boundary. Outranks the model class
                    because bad input produces bad model output; do not blame the model first.
schema_violation    The model produced output outside the contract.
unknown             Nothing recognised. Last, so it can never mask a real class.
==================  ==========================================================================

`Fault` groups the classes (harness, then data, then model, then unattributed) and the
precedence keeps each group contiguous: **every harness class outranks every data class,
which outranks every model class.** An eval harness excludes `HARNESS` and `UNATTRIBUTED`
attempts from accuracy figures and reports them separately, rather than scoring them as
misses.

**What is deliberately not a failure class.** `LLMPort.extract` returning `None` is the
FR-RAG-04 "insufficient evidence" outcome. It is a *result*, and routing it through a failure
class would send a correct "I do not know" to retry or quarantine. It stays an outcome.

**A type is evidence only when the type alone implies the cause.** `AutonomousOverwriteError`
qualifies: its docstring calls it "a programming error, not a data condition". A validation
rejection does not: `OffContractFieldError` and `VocabularyError` are raised for supplier
data, for a caller bug and for model output alike, and only the raise site knows which. Those
are left unregistered and classified by an explicit `ClassifiedError` tag from the code that
knows. The same reasoning is why `STORE_FAILURE`, `ADAPTER_FAILURE`, `UNPARSEABLE_DOCUMENT` and
`SCHEMA_VIOLATION` have no entry in the type table: no exception type in this package implies
them, so only a tagged exception from the adapter or stage that raised it can reach them.

**Wrappers carry no evidence.** `services.claims` re-raises other errors as a `ProposalError`;
classifying the wrapper would blame the wrong party. A wrapper is left unregistered, so the
exception it wraps decides.

**Incomplete evidence is never trusted below the top class.** The chain walk reads at most
`_MAX_LINKS` distinct exceptions. If it stops before it has read them all, a lower-ranked class
found early cannot be trusted, because an unread link might hold a higher one, so the verdict is
`UNKNOWN` unless the class found is already the top of the precedence. A truncated walk
therefore fails loud rather than reporting a harness failure as a model miss.

**Interruptions are not failures.** `asyncio.CancelledError`, `KeyboardInterrupt` and
`SystemExit` carry no evidence and classify as `UNKNOWN`. The runner must handle a cancellation
or a lost lease *before* it classifies; classifying one would quarantine a job that was merely
interrupted.

**Importing this module is cheap.** The type table is built on first use, not at import, so an
adapter or a stage can import `ClassifiedError` to tag its errors without loading the rest of
the package, and a service can import it without a circular import.

Messages are never read. A classification is derived from exception *types* and explicit
tags, so it cannot leak a credential that an exception message embeds, and `log_context`
omits the message for the same reason.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from typing import ClassVar

__all__ = [
    "PRECEDENCE",
    "ClassifiedError",
    "Disposition",
    "Fault",
    "FailureClass",
    "FailureInfo",
    "INFO",
    "JOB_STATUSES",
    "classify",
    "classify_exception",
    "log_context",
]


class Fault(StrEnum):
    """Whose side of the line a failure is on."""

    HARNESS = "harness"
    """Our code or infrastructure. Never counts against extraction quality."""
    DATA = "data"
    """The input document or a value in it."""
    MODEL = "model"
    """The extraction model's output."""
    UNATTRIBUTED = "unattributed"
    """Not recognised, so not assumed to be anyone's: counting it as a model miss would
    poison an accuracy figure, and counting it as ours would hide a real model failure."""


class Disposition(StrEnum):
    """What the runner does next (tasks.md I.2 and I.4)."""

    RETRY = "retry"
    """`job.status` stays `pending` with backoff while attempts remain, then `failed` (I.2)."""
    QUARANTINE = "quarantine"
    """`job.status = 'quarantined'` (I.4): retrying a deterministic failure only repeats it."""


#: The `job.status` values each disposition may write (`sql/08_job.sql`). RETRY stays
#: `pending` while attempts remain and ends `failed` (I.2); QUARANTINE is `quarantined` (I.4).
#: How many attempts remain is the runner's to decide (contract C8), so it is not encoded here.
JOB_STATUSES: Mapping[Disposition, tuple[str, ...]] = {
    Disposition.RETRY: ("pending", "failed"),
    Disposition.QUARANTINE: ("quarantined",),
}


class FailureClass(StrEnum):
    CONTRACT_VIOLATION = "contract_violation"
    STORE_FAILURE = "store_failure"
    TIMEOUT = "timeout"
    ADAPTER_FAILURE = "adapter_failure"
    UNPARSEABLE_DOCUMENT = "unparseable_document"
    INVALID_INPUT = "invalid_input"
    SCHEMA_VIOLATION = "schema_violation"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class FailureInfo:
    fault: Fault
    disposition: Disposition
    audit_event_type: str
    """A member of `audit.envelope.EVENT_TYPES_V1`: the event an attempt records on failure."""


#: Highest precedence first. A permutation of every `FailureClass`, pinned by a test, with
#: `UNKNOWN` last and each `Fault` group contiguous.
PRECEDENCE: tuple[FailureClass, ...] = (
    FailureClass.CONTRACT_VIOLATION,
    FailureClass.STORE_FAILURE,
    FailureClass.TIMEOUT,
    FailureClass.ADAPTER_FAILURE,
    FailureClass.UNPARSEABLE_DOCUMENT,
    FailureClass.INVALID_INPUT,
    FailureClass.SCHEMA_VIOLATION,
    FailureClass.UNKNOWN,
)

#: Transient infrastructure failures retry, and so do model schema violations: a model's output
#: varies between attempts, so a bounded retry can succeed where the same input to
#: deterministic code cannot. Deterministic failures quarantine, as does `UNKNOWN`: this
#: repository's defaults fail loud (`DEFAULT_CRITICALITY` is CRITICAL), and a failure nobody
#: recognised should reach a human rather than retry itself into the limit.
INFO: Mapping[FailureClass, FailureInfo] = {
    FailureClass.CONTRACT_VIOLATION: FailureInfo(
        Fault.HARNESS, Disposition.QUARANTINE, "attempt_failed"
    ),
    FailureClass.STORE_FAILURE: FailureInfo(Fault.HARNESS, Disposition.RETRY, "attempt_failed"),
    FailureClass.TIMEOUT: FailureInfo(Fault.HARNESS, Disposition.RETRY, "attempt_failed"),
    FailureClass.ADAPTER_FAILURE: FailureInfo(Fault.HARNESS, Disposition.RETRY, "attempt_failed"),
    FailureClass.UNPARSEABLE_DOCUMENT: FailureInfo(
        Fault.DATA, Disposition.QUARANTINE, "parse_failure"
    ),
    FailureClass.INVALID_INPUT: FailureInfo(Fault.DATA, Disposition.QUARANTINE, "attempt_failed"),
    FailureClass.SCHEMA_VIOLATION: FailureInfo(Fault.MODEL, Disposition.RETRY, "attempt_failed"),
    FailureClass.UNKNOWN: FailureInfo(Fault.UNATTRIBUTED, Disposition.QUARANTINE, "attempt_failed"),
}


class ClassifiedError(Exception):
    """Base for an exception that states its own class.

    An adapter raising its own error subclasses this and sets `failure_class`, so the
    classification travels with the exception instead of being re-derived from its text. The
    port Protocols are signature-stable, so this is how an adapter reports *why* without
    changing them.
    """

    failure_class: ClassVar[FailureClass] = FailureClass.UNKNOWN


#: At most this many distinct exceptions are read from one chain, and the loop runs at most
#: `_MAX_STEPS` times in total (repeats included), so a cycle or a pathological graph cannot
#: hang the classifier.
_MAX_LINKS = 64
_MAX_STEPS = 1024


@cache
def _by_type() -> tuple[tuple[type[BaseException], FailureClass], ...]:
    """Exception types whose type alone implies the cause, matched by `isinstance`.

    A subclass inherits its parent's class. `ProposalError` is absent on purpose (a wrapper),
    while `StoredValueLossError`, its explicit programming-error subclass, is present. See the
    module docstring for why validation errors such as `OffContractFieldError` are absent.

    Built on first use so that importing this module does not import the services.
    """
    from .audit.envelope import UnknownEventTypeError
    from .schema.encoding import UnencodableValueError
    from .services.claims import StoredValueLossError
    from .services.conflict_hitl import AutonomousOverwriteError, IncomparableCandidatesError
    from .services.vertical_slice import SanitizedCSVError

    return (
        (AutonomousOverwriteError, FailureClass.CONTRACT_VIOLATION),
        (IncomparableCandidatesError, FailureClass.CONTRACT_VIOLATION),
        (StoredValueLossError, FailureClass.CONTRACT_VIOLATION),
        (UnknownEventTypeError, FailureClass.CONTRACT_VIOLATION),
        (UnencodableValueError, FailureClass.CONTRACT_VIOLATION),
        (TimeoutError, FailureClass.TIMEOUT),
        (SanitizedCSVError, FailureClass.INVALID_INPUT),
    )


def classify(evidence: Iterable[FailureClass]) -> FailureClass:
    """The highest-precedence class present, or `UNKNOWN` for no evidence."""
    present = set(evidence)
    for failure_class in PRECEDENCE:
        if failure_class in present:
            return failure_class
    return FailureClass.UNKNOWN


def _own_evidence(exc: BaseException) -> FailureClass | None:
    """The class one exception is evidence of, ignoring what it wraps."""
    if isinstance(exc, ClassifiedError):
        # Read from the class, not the instance: the tag is class-level state, so the
        # classification depends on `type(exc)` alone and an exception built without running
        # `__init__` classifies exactly as one built normally.
        tag = type(exc).failure_class
        if tag is not FailureClass.UNKNOWN:
            return tag
    for exc_type, failure_class in _by_type():
        if isinstance(exc, exc_type):
            return failure_class
    return None


def _evidence(exc: BaseException) -> tuple[set[FailureClass], bool]:
    """The classes found on `exc` and what it was raised from, while handling, or alongside,
    and whether every link was read.

    Follows `__cause__`, `__context__` unless the author severed it with `raise ... from None`
    (`__suppress_context__`, which Python's traceback honours), and the members of a
    `BaseExceptionGroup`: a timeout inside `asyncio.TaskGroup` surfaces as a group whose members
    are linked by neither cause nor context.

    Only *newly visited* exceptions count against `_MAX_LINKS`: several members of a group
    commonly share one cause, and re-reading it must not spend budget meant for links not yet
    seen. `_MAX_STEPS` bounds the loop independently, because a cycle never grows the visited
    set and a bound written in terms of it alone would not stop one.
    """
    seen: set[int] = set()
    found: set[FailureClass] = set()
    pending: list[BaseException] = [exc]
    for _ in range(_MAX_STEPS):
        if not pending:
            return found, True
        current = pending.pop()
        if id(current) in seen:
            continue
        if len(seen) >= _MAX_LINKS:
            return found, False
        seen.add(id(current))
        if (evidence := _own_evidence(current)) is not None:
            found.add(evidence)
        links: list[BaseException | None] = [current.__cause__]
        if not current.__suppress_context__:
            links.append(current.__context__)
        if isinstance(current, BaseExceptionGroup):
            links.extend(current.exceptions)
        pending.extend(link for link in links if link is not None)
    return found, False


def classify_exception(exc: BaseException) -> FailureClass:
    """Classify an exception from its whole chain, never its message.

    Every link contributes its own evidence and precedence picks the winner, so an
    infrastructure failure beneath an unrecognised wrapper is still found, and a harness class
    still outranks a model class that merely accompanied it. If the walk could not read every
    link, only the top class is trusted (see the module docstring); anything lower is `UNKNOWN`.
    """
    evidence, complete = _evidence(exc)
    verdict = classify(evidence)
    if not complete and verdict is not PRECEDENCE[0]:
        return FailureClass.UNKNOWN
    return verdict


def log_context(exc: BaseException) -> dict[str, str]:
    """Structured-log context for a failure: the class, never the message.

    Pass as `extra=` to a `procurement_agent` logger. The message is omitted because the
    logger masks only `SecretStr` context, and an exception message is exactly where a DSN
    ends up.
    """
    failure_class = classify_exception(exc)
    info = INFO[failure_class]
    return {
        "failure_class": failure_class.value,
        "fault": info.fault.value,
        "disposition": info.disposition.value,
        "exception_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
    }
