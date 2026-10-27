"""Structured logging (ADR-001 decision 7).

Until this module no file in `src/` imported `logging`, so a worker that failed had no
trace to read. One JSON object per line, on stderr, is the whole design: a line is
greppable, `jq`-able, and carries its context as keys instead of formatted prose.

Two properties are deliberate rather than incidental:

* **A `SecretStr` never renders.** Context arrives through `extra=`, and the
  encoder falls back to `str()`, which a `SecretStr` masks. `Settings` wraps the
  bearer tokens and DSNs for exactly this reason (`tests/test_settings_secrets.py`).
* **Exception text is NOT redacted.** A traceback is rendered verbatim, so an
  exception whose message embeds a credential or a DSN will log it. The rule is on the
  raiser: do not put a secret in an exception message. This module cannot enforce it.

This is a formatter and one configuration function. It is not an audit trail:
`procurement_agent.audit` is the tamper-evident record, and nothing here substitutes for it.
"""

from __future__ import annotations

import json
import logging
import math
import sys
from datetime import UTC, datetime
from typing import IO

#: The logger every module in the package hangs under, so one call configures all of them.
ROOT_LOGGER_NAME = "procurement_agent"

# Attributes every LogRecord carries. Anything else on the record arrived through
# `extra=` and is context to emit; computed once from a real record so a stdlib change
# to the attribute set cannot silently leak internals into the output.
_RECORD_FIELDS = frozenset(logging.LogRecord("", logging.INFO, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


_CORE_KEYS = frozenset({"ts", "level", "logger", "message", "exc", "stack"})
_MAX_DEPTH = 8


def _plain(value: object, depth: int = 0) -> object:
    """Reduce context to what `json.dumps(allow_nan=False)` cannot reject.

    `default=str` only reaches *values*: a dict keyed by a tuple, an Enum or a UUID raised
    `TypeError`, a NaN was written as the bare token `NaN` (not JSON; a strict parser drops the
    whole line), and a cycle raised `ValueError`. `Handler.emit` swallows all three and prints
    a traceback in place of the record, which is the opposite of what a logging call is for.
    """
    if depth >= _MAX_DEPTH:
        return str(value)
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, dict):
        return {str(k): _plain(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_plain(v, depth + 1) for v in value]
    return str(value)  # a SecretStr renders as its mask


class JsonFormatter(logging.Formatter):
    """One sorted-key JSON object per record."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RECORD_FIELDS:
                # An `extra={"level": ...}` must not overwrite the record's real severity.
                payload[f"ctx_{key}" if key in _CORE_KEYS else key] = _plain(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False, default=str)


def configure_logging(level: str = "INFO", stream: IO[str] | None = None) -> logging.Logger:
    """Install the JSON handler on the package logger and return it.

    Idempotent: a second call replaces the handler this function installed rather than
    stacking another, so a long-lived process can reconfigure without duplicating lines.
    Pass `Settings.log_level`; this function deliberately does not read the environment itself.
    `propagate` is off for the same reason - a root handler configured by the host
    application would otherwise emit every record twice, once structured and once not.
    """
    resolved = logging.getLevelNamesMapping().get(level.upper())
    if resolved is None:
        # Resolved FIRST: an unknown level used to raise from `setLevel` after the old
        # handler was already removed, leaving the logger half-reconfigured.
        raise ValueError(f"unknown log level {level!r}")
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    for existing in [h for h in logger.handlers if getattr(h, "_procurement_json", False)]:
        logger.removeHandler(existing)
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler._procurement_json = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    logger.setLevel(resolved)
    logger.propagate = False
    return logger
