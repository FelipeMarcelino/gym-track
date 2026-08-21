"""WS-4: the graph runs, with fakes and an in-memory checkpointer.

No database and no broker: what is being checked is that the topology is
wired -- every node reached, in order, with the state each one needs. The
handlers that make those nodes do something arrive in WS-5 through WS-7.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.domain.results import ResultVisibility, TaskType
from app.domain.workflow.plan import ExecutionPlan, PlannedTask
from app.graphs.main.graph import NODE_SEQUENCE, build_main_graph
from app.graphs.main.state import (
    GRAPH_VERSION,
    MainGraphState,
    initial_state,
    new_delivery_id,
    thread_for,
    thread_id_for,
)


@dataclass
class _RecordingRouter:
    intents: tuple[TaskType, ...] = (TaskType.CONVERSATION,)
    calls: list[tuple[str, ...]] = field(default_factory=list)

    async def route(self, texts: Sequence[str]) -> tuple[TaskType, ...]:
        self.calls.append(tuple(texts))
        return self.intents


@dataclass
class _RecordingPlanner:
    calls: list[tuple[TaskType, ...]] = field(default_factory=list)

    async def plan(self, intents: Sequence[TaskType], *, texts: Sequence[str]) -> ExecutionPlan:
        self.calls.append(tuple(intents))
        return ExecutionPlan(
            tasks=tuple(
                PlannedTask(
                    key=f"{intent.value}:{index}",
                    task_type=intent,
                    result_visibility=ResultVisibility.USER_VISIBLE,
                )
                for index, intent in enumerate(intents)
            )
        )


def _state(texts: tuple[str, ...] = ("bom dia",)) -> MainGraphState:
    return initial_state(
        execution_id=uuid4(),
        user_id=uuid4(),
        conversation_id=uuid4(),
        message_batch_id=uuid4(),
        thread_id="conversation:execution:delivery",
        trace_id="trace-1",
        correlation_id="correlation-1",
        texts=texts,
    )


async def _run(state: MainGraphState, **overrides: Any) -> dict[str, Any]:
    router = overrides.get("router") or _RecordingRouter()
    planner = overrides.get("planner") or _RecordingPlanner()
    graph = build_main_graph(router=router, planner=planner, checkpointer=InMemorySaver())
    config = thread_for(uuid4(), uuid4(), new_delivery_id())
    return dict(await graph.ainvoke(state, config=config))


@pytest.mark.asyncio
async def test_a_message_walks_the_whole_graph() -> None:
    router = _RecordingRouter()
    planner = _RecordingPlanner()

    result = await _run(_state(("fiz supino", "80kg")), router=router, planner=planner)

    assert router.calls == [("fiz supino", "80kg")], "the router sees the batch's fragments"
    assert planner.calls == [(TaskType.CONVERSATION,)], "the planner sees what was routed"
    assert result["intents"] == ("conversation",)
    assert isinstance(result["execution_plan"], ExecutionPlan)


@pytest.mark.asyncio
async def test_the_plan_reaches_the_state_the_scheduler_will_read() -> None:
    """WS-5 picks up from here, so the handover is worth asserting now."""
    result = await _run(_state(), router=_RecordingRouter(intents=(TaskType.LOG_WORKOUT,)))

    plan = result["execution_plan"]
    assert [task.task_type for task in plan.tasks] == [TaskType.LOG_WORKOUT]
    assert plan.ready(), "the first task must be runnable when the scheduler gets it"


@pytest.mark.asyncio
async def test_normalize_input_is_identity_this_sprint() -> None:
    """A deliberate deviation from §11.1, asserted so that removing it later is
    a visible change rather than a silent one."""
    texts = ("Fiz  SUPINO", "80kg 😀")

    result = await _run(_state(texts))

    assert result["normalized_input"] == texts


@pytest.mark.asyncio
async def test_the_state_carries_references_and_not_records() -> None:
    """Q27, enforced by a test rather than by good intentions.

    Every key the graph produces must be one §11.4 declares. A field holding a
    user's history would be caught here, which is the only place it can be
    caught cheaply.
    """
    result = await _run(_state())

    assert set(result) <= set(MainGraphState.__annotations__)


@pytest.mark.asyncio
async def test_the_graph_version_travels_with_the_run() -> None:
    result = await _run(_state())

    assert result["graph_version"] == GRAPH_VERSION


@pytest.mark.asyncio
async def test_every_node_appears_in_the_stream() -> None:
    """The order the nodes actually execute in, not the order they were
    declared in."""
    graph = build_main_graph(
        router=_RecordingRouter(), planner=_RecordingPlanner(), checkpointer=InMemorySaver()
    )
    config = thread_for(uuid4(), uuid4(), new_delivery_id())

    seen: list[str] = []
    async for step in graph.astream(_state(), config=config, stream_mode="updates"):
        seen.extend(step.keys())

    assert seen == list(NODE_SEQUENCE)


def test_the_thread_id_names_conversation_execution_and_delivery() -> None:
    """Measured in `test_langgraph_semantics.py`: `checkpoint_ns` cannot carry
    this, so the thread id does. Q29 needs the execution, and a retry after a
    rolled-back transaction needs the delivery."""
    conversation, execution, delivery = uuid4(), uuid4(), uuid4()

    thread = thread_id_for(conversation, execution, delivery)

    assert thread == f"{conversation}:{execution}:{delivery}"


def test_a_resume_carries_the_checkpoint_to_fork_from() -> None:
    config = thread_for(uuid4(), uuid4(), uuid4(), checkpoint_id="checkpoint-42")

    assert config["configurable"]["checkpoint_id"] == "checkpoint-42"


def test_an_ordinary_invocation_does_not_pin_a_checkpoint() -> None:
    """Pinning one would resume the past instead of continuing the present."""
    config = thread_for(uuid4(), uuid4(), uuid4())

    assert "checkpoint_id" not in config["configurable"]


def test_two_deliveries_of_one_execution_get_different_threads() -> None:
    """A retry must not inherit a checkpoint describing work that rolled back,
    and `attempts` cannot be the discriminator: it is incremented inside the
    very transaction whose rollback stranded the checkpoint."""
    conversation, execution = uuid4(), uuid4()

    first = thread_id_for(conversation, execution, new_delivery_id())
    second = thread_id_for(conversation, execution, new_delivery_id())

    assert first != second
