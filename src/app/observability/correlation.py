"""Correlation context carried across awaits, tasks and process boundaries (§30.3).

Three identifiers travel with every unit of work:

* ``trace_id`` -- one span tree. A webhook request has its own; the interaction
  that fragments eventually form has another (Q131).
* ``correlation_id`` -- the thread tying an interaction to the background work
  it causes later. Background jobs open a *new* trace but keep this.
* ``workflow_execution_id`` -- present once a workflow is running.

They live in contextvars rather than being threaded through call signatures,
because the alternative is passing a context object into every function that
might one day log something.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Final

_TRACE_ID_BYTES: Final = 16
_CORRELATION_ID_BYTES: Final = 16


@dataclass(frozen=True, slots=True)
class CorrelationContext:
    trace_id: str
    correlation_id: str
    workflow_execution_id: str | None = None
    #: For an interaction trace, the request traces it absorbed (Q131).
    linked_trace_ids: tuple[str, ...] = ()


_context: ContextVar[CorrelationContext | None] = ContextVar("correlation_context", default=None)


def new_trace_id() -> str:
    """A 128-bit trace id, rendered like W3C traceparent's trace-id."""
    return secrets.token_hex(_TRACE_ID_BYTES)


def new_correlation_id() -> str:
    return secrets.token_hex(_CORRELATION_ID_BYTES)


def current_context() -> CorrelationContext | None:
    return _context.get()


def current_metadata() -> dict[str, str]:
    """The correlation fields, shaped for a log record or a telemetry tag set."""
    context = _context.get()
    if context is None:
        return {}
    metadata = {
        "trace_id": context.trace_id,
        "correlation_id": context.correlation_id,
    }
    if context.workflow_execution_id is not None:
        metadata["workflow_execution_id"] = context.workflow_execution_id
    return metadata


@contextmanager
def correlation_scope(
    *,
    trace_id: str | None = None,
    correlation_id: str | None = None,
    workflow_execution_id: str | None = None,
    linked_trace_ids: Sequence[str] = (),
) -> Iterator[CorrelationContext]:
    """Bind a correlation context for the duration of the block.

    Omitted values are inherited from the surrounding context when there is
    one, so a nested scope can add ``workflow_execution_id`` without having to
    restate what it already knows.
    """
    parent = _context.get()
    context = CorrelationContext(
        trace_id=trace_id or (parent.trace_id if parent else new_trace_id()),
        correlation_id=correlation_id
        or (parent.correlation_id if parent else new_correlation_id()),
        workflow_execution_id=workflow_execution_id
        or (parent.workflow_execution_id if parent else None),
        linked_trace_ids=tuple(linked_trace_ids),
    )
    token = _context.set(context)
    try:
        yield context
    finally:
        _context.reset(token)


@contextmanager
def request_scope(*, correlation_id: str | None = None) -> Iterator[CorrelationContext]:
    """A short trace for one inbound HTTP request.

    Deliberately not the interaction trace: a webhook request cannot know which
    batch its message will join, so it cannot mint the shared trace Q131 asks
    for. It mints its own and hands the id to the aggregator through
    ``messages.trace_id``.
    """
    with correlation_scope(
        trace_id=new_trace_id(),
        correlation_id=correlation_id or new_correlation_id(),
    ) as context:
        yield context


@contextmanager
def interaction_scope(
    request_trace_ids: Sequence[str],
    *,
    correlation_id: str | None = None,
) -> Iterator[CorrelationContext]:
    """The single trace for one InputBatch, minted when the batch is persisted.

    This is the trace Q131 requires: one per interaction, linking back to every
    webhook request that contributed a fragment to it.
    """
    with correlation_scope(
        trace_id=new_trace_id(),
        correlation_id=correlation_id or new_correlation_id(),
        linked_trace_ids=tuple(request_trace_ids),
    ) as context:
        yield context


@contextmanager
def background_scope(correlation_id: str) -> Iterator[CorrelationContext]:
    """Work started later: a new trace, the same correlation (Q131).

    Attaching background work to the originating trace would leave traces open
    for as long as the slowest follow-up job, so the link is the correlation id.
    """
    with correlation_scope(
        trace_id=new_trace_id(),
        correlation_id=correlation_id,
    ) as context:
        yield context


def with_workflow_execution(workflow_execution_id: str) -> CorrelationContext:
    """Attach a workflow execution to the context already in scope."""
    context = _context.get()
    if context is None:
        raise RuntimeError("no correlation context is bound")
    updated = replace(context, workflow_execution_id=workflow_execution_id)
    _context.set(updated)
    return updated
