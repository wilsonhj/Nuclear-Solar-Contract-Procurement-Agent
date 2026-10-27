"""Structured logging (ADR-001 decision 7): one JSON object per line, secrets masked."""

import io
import json
import logging
import sys
from collections.abc import Iterator
from typing import get_args

import pytest
from pydantic import SecretStr

from procurement_agent.config import LogLevel
from procurement_agent.logging_config import (
    LEVELS,
    ROOT_LOGGER_NAME,
    _plain,
    configure_logging,
)


@pytest.fixture(autouse=True)
def _restore_logger() -> Iterator[None]:
    """`configure_logging` mutates a process-global; leave it as found."""
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    handlers, level, propagate = list(logger.handlers), logger.level, logger.propagate
    yield
    logger.handlers[:] = handlers
    logger.setLevel(level)
    logger.propagate = propagate


def _lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_a_record_is_one_json_object_with_its_context() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream)
    logging.getLogger("procurement_agent.test").info(
        "claim committed", extra={"document_id": "d-1", "attempt": 2}
    )
    (record,) = _lines(stream)
    assert record["message"] == "claim committed"
    assert record["level"] == "INFO"
    assert record["logger"] == "procurement_agent.test"
    assert record["document_id"] == "d-1"
    assert record["attempt"] == 2
    assert "ts" in record


def test_a_secret_in_the_context_is_masked_not_logged() -> None:
    """The reason `Settings` wraps its credentials: they can reach a log line."""
    stream = io.StringIO()
    configure_logging("INFO", stream)
    logging.getLogger("procurement_agent.test").info(
        "calling llm",
        extra={
            "api_key": SecretStr("hunter2-sentinel"),
            "nested": {"dsn": SecretStr("pw-sentinel")},
        },
    )
    out = stream.getvalue()
    assert "hunter2-sentinel" not in out
    assert "pw-sentinel" not in out
    assert "**********" in out


def test_an_unserialisable_value_is_rendered_rather_than_raised() -> None:
    """A logging call must not be what takes a worker down."""
    stream = io.StringIO()
    configure_logging("INFO", stream)
    logging.getLogger("procurement_agent.test").info("x", extra={"blob": object()})
    (record,) = _lines(stream)
    assert str(record["blob"]).startswith("<object object")


def test_an_exception_is_carried_in_the_record() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream)
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logging.getLogger("procurement_agent.test").exception("stage failed")
    (record,) = _lines(stream)
    assert "RuntimeError: boom" in str(record["exc"])


def test_the_level_filters() -> None:
    stream = io.StringIO()
    configure_logging("WARNING", stream)
    log = logging.getLogger("procurement_agent.test")
    log.info("dropped")
    log.warning("kept")
    assert [r["message"] for r in _lines(stream)] == ["kept"]


def test_reconfiguring_replaces_the_handler_instead_of_stacking_it() -> None:
    """Otherwise a long-lived process duplicates every line per reconfiguration."""
    first, second = io.StringIO(), io.StringIO()
    configure_logging("INFO", first)
    configure_logging("INFO", second)
    logging.getLogger("procurement_agent.test").info("once")
    assert first.getvalue() == ""
    assert len(_lines(second)) == 1


def test_records_do_not_also_reach_the_root_logger() -> None:
    """A host application's root handler would otherwise emit each record twice."""
    configure_logging("INFO", io.StringIO())
    assert logging.getLogger(ROOT_LOGGER_NAME).propagate is False


def test_context_that_json_cannot_encode_is_sanitised_not_dropped() -> None:
    """Each of these used to make `json.dumps` raise, and `Handler.emit` then printed a
    traceback to stderr instead of the record."""
    stream = io.StringIO()
    configure_logging("INFO", stream)
    cycle: dict[str, object] = {}
    cycle["self"] = cycle
    logging.getLogger("procurement_agent.test").info(
        "x",
        extra={
            "tuple_keyed": {(1, 2): 3},
            "nan": float("nan"),
            "inf": float("inf"),
            "cycle": cycle,
            "secret_key": {SecretStr("k-sentinel"): 1},
        },
    )
    (record,) = _lines(stream)  # parses as strict JSON: no bare NaN token
    assert record["nan"] == "nan"
    assert record["inf"] == "inf"
    assert "k-sentinel" not in stream.getvalue()


def test_context_cannot_overwrite_the_records_own_fields() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream)
    logging.getLogger("procurement_agent.test").error(
        "real message", extra={"level": "billing", "ts": "yesterday", "logger": "x"}
    )
    (record,) = _lines(stream)
    assert record["level"] == "ERROR"
    assert record["logger"] == "procurement_agent.test"
    assert record["ts"] != "yesterday"
    assert record["ctx_level"] == "billing"
    assert record["ctx_ts"] == "yesterday"


def test_stack_info_is_emitted() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream)
    logging.getLogger("procurement_agent.test").warning("where", stack_info=True)
    (record,) = _lines(stream)
    assert "test_stack_info_is_emitted" in str(record["stack"])


def test_an_unknown_level_leaves_the_existing_configuration_untouched() -> None:
    """The failed call used to remove the old handler before `setLevel` raised."""
    stream = io.StringIO()
    configure_logging("WARNING", stream)
    with pytest.raises(ValueError, match="unknown log level"):
        configure_logging("bogus", io.StringIO())
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    assert logger.level == logging.WARNING
    logging.getLogger("procurement_agent.test").warning("still routed")
    assert [r["message"] for r in _lines(stream)] == ["still routed"]


def test_the_accepted_levels_are_exactly_the_settings_literal() -> None:
    """Two spellings of one list drift; this pins them together."""
    assert LEVELS == set(get_args(LogLevel))


@pytest.mark.parametrize("level", ["notset", "NOTSET", "warn", "FATAL", "10", ""])
def test_levels_outside_the_five_names_are_refused(level: str) -> None:
    """NOTSET on a non-root logger silently inherits the ROOT logger's level, so the caller who
    asked for everything would get the host application's WARNING."""
    with pytest.raises(ValueError, match="unknown log level"):
        configure_logging(level, io.StringIO())


def test_the_default_stream_follows_sys_stderr_after_it_is_swapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handler used to bind the `sys.stderr` object present at configure time, so a later
    swap or close (pytest capture, a daemonising wrapper) lost every subsequent record."""
    first, second = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stderr", first)
    configure_logging("INFO")
    first.close()
    monkeypatch.setattr(sys, "stderr", second)
    logging.getLogger("procurement_agent.test").info("after the swap")
    assert [r["message"] for r in _lines(second)] == ["after the swap"]


def test_set_context_is_emitted_in_a_stable_order() -> None:
    """String hashing is randomised per process, so raw set iteration order is not repeatable."""
    assert _plain({"ccc", "aaa", "bbb", "ddd"}) == ["aaa", "bbb", "ccc", "ddd"]
    assert _plain(frozenset({3, 1, 2})) == [1, 2, 3]
