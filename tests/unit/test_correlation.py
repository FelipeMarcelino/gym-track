"""WS-4: correlation must survive awaits, stay out of each other's way, and
produce exactly one interaction trace per InputBatch (§30.3, Q131)."""

from __future__ import annotations

import asyncio

import pytest

from app.observability import (
    background_scope,
    correlation_scope,
    current_context,
    current_metadata,
    interaction_scope,
    request_scope,
    with_workflow_execution,
)


async def test_context_survives_an_await_boundary() -> None:
    with request_scope() as context:
        await asyncio.sleep(0)
        assert current_context() == context


async def test_concurrent_tasks_do_not_leak_context_into_each_other() -> None:
    """The failure this prevents is the worst kind: correct-looking logs
    attributed to the wrong interaction."""

    async def handle(marker: str) -> tuple[str, str]:
        with request_scope() as opened:
            # Yield control so the tasks interleave inside their scopes.
            await asyncio.sleep(0)
            observed = current_context()
            assert observed == opened, "another task's context leaked into this one"
            await asyncio.sleep(0)
            return marker, opened.trace_id

    results = await asyncio.gather(*(handle(str(index)) for index in range(20)))
    trace_ids = [trace_id for _, trace_id in results]

    assert len(set(trace_ids)) == len(trace_ids)
    assert current_context() is None, "the scope must not survive its own block"


async def test_context_is_restored_after_a_nested_scope() -> None:
    with request_scope() as outer:
        with correlation_scope(trace_id="inner-trace") as inner:
            assert inner.trace_id == "inner-trace"
            assert inner.correlation_id == outer.correlation_id, "correlation is inherited"
        assert current_context() == outer


def test_scope_is_reset_even_when_the_block_raises() -> None:
    with pytest.raises(RuntimeError), request_scope():
        raise RuntimeError

    assert current_context() is None


def test_three_fragments_produce_three_request_traces_and_one_interaction_trace() -> None:
    """Q131 in one test: fragments arrive as separate webhook requests, and the
    interaction trace is minted later, by the aggregator, linking them all."""
    request_traces = []
    for _ in range(3):
        with request_scope() as request_context:
            request_traces.append(request_context.trace_id)

    assert len(set(request_traces)) == 3

    with interaction_scope(request_traces) as interaction:
        assert interaction.trace_id not in request_traces, "the batch trace is its own trace"
        assert set(interaction.linked_trace_ids) == set(request_traces)
        assert len(interaction.linked_trace_ids) == 3


def test_background_work_gets_a_new_trace_and_keeps_the_correlation() -> None:
    with interaction_scope([], correlation_id="corr-1") as interaction:
        interaction_trace = interaction.trace_id

    with background_scope("corr-1") as background:
        assert background.correlation_id == "corr-1"
        assert background.trace_id != interaction_trace


def test_workflow_execution_id_joins_the_context_in_place() -> None:
    with correlation_scope(trace_id="t", correlation_id="c"):
        assert "workflow_execution_id" not in current_metadata()

        with_workflow_execution("wf-1")

        assert current_metadata() == {
            "trace_id": "t",
            "correlation_id": "c",
            "workflow_execution_id": "wf-1",
        }


def test_metadata_is_empty_outside_any_scope() -> None:
    assert current_metadata() == {}


def test_attaching_a_workflow_without_a_context_is_an_error() -> None:
    with pytest.raises(RuntimeError, match="no correlation context"):
        with_workflow_execution("wf-1")
