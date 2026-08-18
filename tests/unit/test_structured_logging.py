"""WS-4: a log line must carry its correlation and never carry a secret."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from app.observability import (
    REDACTED,
    StructuredFormatter,
    background_job_scope,
    configure_logging,
    consumed_message_scope,
    interaction_scope,
    request_scope,
)


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)
    try:
        yield stream
    finally:
        logging.getLogger().handlers.clear()


def _records(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_every_record_is_one_json_object(log_stream: io.StringIO) -> None:
    logging.getLogger("app.test").info("hello")

    record = _records(log_stream)[0]
    assert record["level"] == "INFO"
    assert record["logger"] == "app.test"
    assert record["message"] == "hello"
    assert "timestamp" in record


def test_a_record_emitted_inside_a_request_carries_that_requests_trace(
    log_stream: io.StringIO,
) -> None:
    with request_scope() as context:
        logging.getLogger("app.api").info("received")

    record = _records(log_stream)[0]
    assert record["trace_id"] == context.trace_id
    assert record["correlation_id"] == context.correlation_id


def test_records_outside_a_scope_have_no_correlation_fields(log_stream: io.StringIO) -> None:
    logging.getLogger("app.boot").info("starting")

    record = _records(log_stream)[0]
    assert "trace_id" not in record


def test_the_interaction_trace_reaches_the_log_with_its_links(
    log_stream: io.StringIO,
) -> None:
    """Q131's linkage has to be visible in telemetry, not only in the database."""
    request_traces = []
    for _ in range(3):
        with request_scope() as request_context:
            request_traces.append(request_context.trace_id)

    with interaction_scope(request_traces):
        logging.getLogger("app.aggregator").info("batch persisted")

    record = _records(log_stream)[0]
    assert set(record["linked_trace_ids"]) == set(request_traces)


def test_a_consumer_continues_the_trace_it_was_handed(log_stream: io.StringIO) -> None:
    with consumed_message_scope({"trace_id": "trace-1", "correlation_id": "corr-1"}):
        logging.getLogger("app.worker").info("consumed")

    record = _records(log_stream)[0]
    assert record["trace_id"] == "trace-1"
    assert record["correlation_id"] == "corr-1"


def test_a_background_job_logs_a_new_trace_under_the_same_correlation(
    log_stream: io.StringIO,
) -> None:
    with background_job_scope({"correlation_id": "corr-1"}):
        logging.getLogger("app.worker").info("background")

    record = _records(log_stream)[0]
    assert record["correlation_id"] == "corr-1"
    assert record["trace_id"] != "corr-1"


def test_extra_fields_are_redacted_before_they_are_written(log_stream: io.StringIO) -> None:
    logging.getLogger("app.api").info(
        "identity resolved",
        extra={"user_id": "u-1", "bsuid": "5511987654321", "phone_number": "+5511987654321"},
    )

    record = _records(log_stream)[0]
    assert record["user_id"] == "u-1"
    assert record["bsuid"] == REDACTED
    assert record["phone_number"] == REDACTED


def test_the_message_itself_is_redacted(log_stream: io.StringIO) -> None:
    logging.getLogger("app.api").info("inbound from +55 11 91234-5678")

    assert "91234" not in log_stream.getvalue()


def test_an_exception_traceback_is_redacted(log_stream: io.StringIO) -> None:
    try:
        raise ValueError("token=super-secret-value +5511987654321")
    except ValueError:
        logging.getLogger("app.api").exception("failed")

    assert "5511987654321" not in log_stream.getvalue()


def test_no_secret_survives_a_realistic_record() -> None:
    """The §33 promise, asserted rather than assumed."""
    formatter = StructuredFormatter()
    record = logging.LogRecord(
        name="app.api",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="webhook %s",
        args=("accepted",),
        exc_info=None,
    )
    record.__dict__.update(
        {
            "password": "hunter2",
            "external_id_lookup_hmac": "deadbeef",
            "payload": {"from": "+5511987654321", "text": "fiz 3 series"},
        }
    )

    emitted = formatter.format(record)

    for leak in ("hunter2", "deadbeef", "5511987654321"):
        assert leak not in emitted
    assert "fiz 3 series" in emitted, "operational content must still be readable"
