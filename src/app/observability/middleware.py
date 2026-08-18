"""Entry points where a correlation context is opened.

Written against the raw ASGI protocol rather than a framework: the API is
FastAPI (§35), but nothing here needs to know that, and the message helper has
no framework at all on the other side.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from typing import Any, Final

from app.observability.correlation import (
    CorrelationContext,
    background_scope,
    correlation_scope,
    new_correlation_id,
    request_scope,
)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

CORRELATION_HEADER: Final = b"x-correlation-id"
TRACE_HEADER: Final = b"x-trace-id"


class CorrelationMiddleware:
    """Opens a request-scoped trace and echoes both ids back on the response.

    An inbound `x-correlation-id` is honoured so that a caller which already
    has a correlation -- a retry, or an internal client -- keeps it. The trace
    id is always minted here: it identifies this request, not the caller's.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers: list[tuple[bytes, bytes]] = list(scope.get("headers", []))
        incoming = _header(headers, CORRELATION_HEADER)

        with request_scope(correlation_id=incoming) as context:

            async def send_with_correlation(message: Message) -> None:
                if message["type"] == "http.response.start":
                    response_headers = list(message.get("headers", []))
                    response_headers.append((TRACE_HEADER, context.trace_id.encode()))
                    response_headers.append((CORRELATION_HEADER, context.correlation_id.encode()))
                    message["headers"] = response_headers
                await send(message)

            await self._app(scope, receive, send_with_correlation)


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    for key, value in headers:
        if key.lower() == name:
            return value.decode()
    return None


@contextmanager
def consumed_message_scope(headers: Mapping[str, Any]) -> Iterator[CorrelationContext]:
    """Restore the correlation a message was published with.

    A consumer continues the *same* trace when the publisher passed one, since
    the work is a continuation of that interaction rather than something new.
    A message that arrives without correlation metadata gets a fresh context
    instead of silently joining whatever ran last on this worker.
    """
    trace_id = _string(headers.get("trace_id"))
    correlation_id = _string(headers.get("correlation_id"))
    workflow_execution_id = _string(headers.get("workflow_execution_id"))

    with correlation_scope(
        trace_id=trace_id,
        correlation_id=correlation_id or new_correlation_id(),
        workflow_execution_id=workflow_execution_id,
    ) as context:
        yield context


@contextmanager
def background_job_scope(headers: Mapping[str, Any]) -> Iterator[CorrelationContext]:
    """A job started later: new trace, inherited correlation (Q131)."""
    correlation_id = _string(headers.get("correlation_id")) or new_correlation_id()
    with background_scope(correlation_id) as context:
        yield context


def _string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode()
    return str(value)
