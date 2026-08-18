"""WS-4: the ASGI middleware that opens a request scope, tested without a framework."""

from __future__ import annotations

import re
from collections.abc import MutableMapping
from typing import Any

import pytest

from app.observability import CorrelationMiddleware, current_context, new_correlation_id
from app.observability.middleware import CORRELATION_HEADER, TRACE_HEADER


async def _call(
    middleware: CorrelationMiddleware, headers: list[tuple[bytes, bytes]] | None = None
) -> list[MutableMapping[str, Any]]:
    sent: list[MutableMapping[str, Any]] = []

    async def receive() -> MutableMapping[str, Any]:
        return {"type": "http.request", "body": b""}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    await middleware(
        {"type": "http", "headers": headers or [], "path": "/webhooks/whatsapp"},
        receive,
        send,
    )
    return sent


async def test_a_request_runs_inside_a_correlation_scope() -> None:
    seen: dict[str, str] = {}

    async def app(scope: Any, receive: Any, send: Any) -> None:
        context = current_context()
        assert context is not None
        seen["trace_id"] = context.trace_id
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent = await _call(CorrelationMiddleware(app))

    response_headers = dict(sent[0]["headers"])
    assert response_headers[TRACE_HEADER].decode() == seen["trace_id"]
    assert current_context() is None


async def test_an_inbound_correlation_id_is_honoured() -> None:
    """A retry or an internal caller keeps its correlation across the boundary."""
    accepted = new_correlation_id()

    async def app(scope: Any, receive: Any, send: Any) -> None:
        context = current_context()
        assert context is not None
        assert context.correlation_id == accepted
        await send({"type": "http.response.start", "status": 200, "headers": []})

    sent = await _call(
        CorrelationMiddleware(app), headers=[(b"X-Correlation-Id", accepted.encode())]
    )

    assert dict(sent[0]["headers"])[CORRELATION_HEADER] == accepted.encode()


async def test_each_request_gets_its_own_trace() -> None:
    traces: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        context = current_context()
        assert context is not None
        traces.append(context.trace_id)
        await send({"type": "http.response.start", "status": 200, "headers": []})

    middleware = CorrelationMiddleware(app)
    for _ in range(3):
        await _call(middleware)

    assert len(set(traces)) == 3, "three webhook requests are three request traces (Q131)"


async def test_non_http_scopes_pass_through_untouched() -> None:
    called = False

    async def app(scope: Any, receive: Any, send: Any) -> None:
        nonlocal called
        called = True
        assert current_context() is None

    async def receive() -> MutableMapping[str, Any]:
        return {"type": "lifespan.startup"}

    async def send(message: MutableMapping[str, Any]) -> None:
        return None

    await CorrelationMiddleware(app)({"type": "lifespan"}, receive, send)

    assert called


async def test_the_scope_closes_even_when_the_application_raises() -> None:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await _call(CorrelationMiddleware(app))

    assert current_context() is None


@pytest.mark.parametrize(
    "supplied",
    [
        b"+5511912345678",
        b"nao-e-um-id",
        b"' OR 1=1--",
        b"x" * 4096,
        b"",
    ],
)
async def test_a_correlation_id_that_is_not_ours_is_replaced(supplied: bytes) -> None:
    """The header ends up in every log record for the request, so a
    caller-controlled string there is a PII leak and an unbounded field."""
    seen: dict[str, str] = {}

    async def app(scope: Any, receive: Any, send: Any) -> None:
        context = current_context()
        assert context is not None
        seen["correlation_id"] = context.correlation_id
        await send({"type": "http.response.start", "status": 200, "headers": []})

    await _call(CorrelationMiddleware(app), headers=[(b"x-correlation-id", supplied)])

    assert seen["correlation_id"] != supplied.decode(errors="replace")
    assert re.fullmatch(r"[0-9a-f]{32}", seen["correlation_id"])


async def test_a_correlation_id_in_our_own_format_is_honoured() -> None:
    minted = new_correlation_id()
    seen: dict[str, str] = {}

    async def app(scope: Any, receive: Any, send: Any) -> None:
        context = current_context()
        assert context is not None
        seen["correlation_id"] = context.correlation_id
        await send({"type": "http.response.start", "status": 200, "headers": []})

    await _call(CorrelationMiddleware(app), headers=[(b"x-correlation-id", minted.encode())])

    assert seen["correlation_id"] == minted
