"""Structured JSON logging carrying the correlation fields (§30.3).

Every record is one JSON object with `trace_id`, `correlation_id` and, once a
workflow is running, `workflow_execution_id` -- pulled from the contextvars
rather than passed in, so a log call deep inside a handler needs no ceremony to
be correlatable.

The redactor runs inside the formatter. That placement is deliberate: it is the
narrowest point every log line must pass through, so there is no code path that
can emit a record without it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any, Final

from app.observability.correlation import current_context
from app.observability.redaction import TelemetryRedactor

#: Keys the formatter owns. An `extra` of the same name is emitted under an
#: `extra_` prefix instead of replacing them: a record that claims a trace it
#: was not emitted under is worse than no record at all.
RESERVED_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "correlation_id",
        "level",
        "linked_trace_ids",
        "logger",
        "message",
        "timestamp",
        "trace_id",
        "workflow_execution_id",
    }
)

#: LogRecord attributes that are metadata about logging itself; anything else a
#: caller attaches through `extra` is treated as payload and redacted.
_STANDARD_RECORD_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class StructuredFormatter(logging.Formatter):
    def __init__(self, redactor: TelemetryRedactor | None = None) -> None:
        super().__init__()
        self._redactor = redactor or TelemetryRedactor()

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": self._redactor.redact_text(record.getMessage()),
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
        }

        context = current_context()
        if context is not None:
            payload["trace_id"] = context.trace_id
            payload["correlation_id"] = context.correlation_id
            if context.workflow_execution_id is not None:
                payload["workflow_execution_id"] = context.workflow_execution_id
            if context.linked_trace_ids:
                payload["linked_trace_ids"] = list(context.linked_trace_ids)

        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_RECORD_FIELDS and not key.startswith("_")
        }
        if extras:
            redacted = self._redactor.redact(extras)
            assert isinstance(redacted, Mapping)
            for key, value in redacted.items():
                payload[f"extra_{key}" if key in RESERVED_FIELDS else key] = value

        if record.exc_info:
            payload["exception"] = self._redactor.redact_text(self.formatException(record.exc_info))

        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(
    level: str = "INFO",
    *,
    redactor: TelemetryRedactor | None = None,
    stream: Any = None,
) -> None:
    """Install the structured formatter as the only handler on the root logger."""
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredFormatter(redactor))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
