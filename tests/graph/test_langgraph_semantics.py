"""WS-4: the five things the plan wrote down as unmeasured, pinned as tests.

The sprint plan named these as assumptions and said to measure them before
building on them. They were measured, and one of them was false. Turning the
measurements into tests is what stops a LangGraph upgrade from changing any of
them silently: this file fails loudly instead of WS-8 and WS-9 failing subtly.

Everything here uses an in-memory checkpointer and a toy graph on purpose. It
is a statement about the *library*, not about gym-track.
"""

from __future__ import annotations

from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict

pytestmark = [pytest.mark.asyncio]


class _State(TypedDict, total=False):
    visited: list[str]
    answer: str


def _graph(trace: list[str]) -> Any:
    """before -> asking (interrupts) -> after."""

    def before(state: _State) -> dict[str, Any]:
        trace.append("before")
        return {"visited": [*state.get("visited", []), "before"]}

    def asking(state: _State) -> dict[str, Any]:
        trace.append("asking:entered")
        reply = interrupt({"question": "quantas repetições?"})
        trace.append("asking:resumed")
        return {"answer": str(reply)}

    def after(state: _State) -> dict[str, Any]:
        trace.append("after")
        return {}

    builder = StateGraph(_State)
    builder.add_node("before", before)
    builder.add_node("asking", asking)
    builder.add_node("after", after)
    builder.add_edge(START, "before")
    builder.add_edge("before", "asking")
    builder.add_edge("asking", "after")
    builder.add_edge("after", END)
    return builder.compile(checkpointer=InMemorySaver())


def _thread(name: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": name}}


async def test_a_suspended_graph_reports_its_interrupt_in_the_result() -> None:
    """WS-8 reads the pause out of what `ainvoke` returns, through one
    accessor. This is the shape that accessor may depend on."""
    trace: list[str] = []
    app = _graph(trace)

    result = await app.ainvoke({"visited": []}, config=_thread("t1"))

    assert "__interrupt__" in result
    (pause,) = result["__interrupt__"]
    assert pause.value == {"question": "quantas repetições?"}
    assert pause.id


async def test_the_paused_state_names_the_node_and_the_checkpoint() -> None:
    """`pending_clarifications` stores the checkpoint to fork from. This is
    where that value comes from."""
    trace: list[str] = []
    app = _graph(trace)
    config = _thread("t2")

    await app.ainvoke({"visited": []}, config=config)
    state = await app.aget_state(config)

    assert state.next == ("asking",)
    assert state.config["configurable"]["checkpoint_id"]


async def test_nodes_before_the_interrupt_do_not_run_again() -> None:
    """Half of Q126's assumption, and the half that holds."""
    trace: list[str] = []
    app = _graph(trace)
    config = _thread("t3")

    await app.ainvoke({"visited": []}, config=config)
    trace.clear()

    await app.ainvoke(Command(resume="8 8 8"), config=config)

    assert "before" not in trace


async def test_the_interrupted_node_re_runs_from_its_start() -> None:
    """The other half, and it is **not** what the plan assumed.

    A resumed node does not continue from the `interrupt()` call: it executes
    again from the top, and only the interrupt itself returns a value instead
    of suspending. Everything the node did before that line therefore happens
    twice.

    This is why Q126 says writes before an interrupt must be idempotent if they
    are unavoidable, and why WS-8's handler commits its valid activities under
    an operation id claimed in `processed_operations` -- on resume that commit
    runs a second time and the claim is what makes it a no-op.
    """
    trace: list[str] = []
    app = _graph(trace)
    config = _thread("t4")

    await app.ainvoke({"visited": []}, config=config)
    trace.clear()

    result = await app.ainvoke(Command(resume="8 8 8"), config=config)

    assert trace == ["asking:entered", "asking:resumed", "after"]
    assert result["answer"] == "8 8 8"


async def test_checkpoint_ns_is_not_a_usable_top_level_namespace() -> None:
    """Measured, and it is why `thread_id` is composite.

    The plan's D4 isolated two runs of one conversation with `checkpoint_ns`.
    A run started that way *writes*, but its state cannot be read back:
    LangGraph treats the field as a subgraph namespace. WS-9 has to read and
    resume that state, so the field is unusable for this and the composite
    thread id is what ships.
    """
    trace: list[str] = []
    app = _graph(trace)
    config: dict[str, Any] = {"configurable": {"thread_id": "t5", "checkpoint_ns": "exec-a"}}

    await app.ainvoke({"visited": []}, config=config)

    with pytest.raises(ValueError, match="Subgraph"):
        await app.aget_state(config)


async def test_a_composite_thread_id_isolates_two_runs_of_one_conversation() -> None:
    """The replacement, asserted: Q29 allows a paused workflow and an
    unrelated new one in the same conversation, and neither may become the
    other's latest checkpoint."""
    trace: list[str] = []
    app = _graph(trace)
    paused = _thread("conversation:execution-a:delivery-1")
    unrelated = _thread("conversation:execution-b:delivery-1")

    await app.ainvoke({"visited": []}, config=paused)
    await app.ainvoke({"visited": []}, config=unrelated)

    assert (await app.aget_state(paused)).next == ("asking",)
    assert (await app.aget_state(unrelated)).next == ("asking",)

    await app.ainvoke(Command(resume="8 8 8"), config=paused)

    assert (await app.aget_state(paused)).next == ()
    assert (await app.aget_state(unrelated)).next == ("asking",), (
        "resuming one run must not advance the other"
    )


async def test_a_resume_can_fork_from_an_explicit_checkpoint() -> None:
    """WS-7's reconciliation depends on this.

    A redelivered answer must re-enter the interrupt point rather than resume
    whatever the previous attempt left behind, so the config carries the
    checkpoint id stored beside the clarification.
    """
    trace: list[str] = []
    app = _graph(trace)
    config = _thread("t6")

    await app.ainvoke({"visited": []}, config=config)
    pause_point = (await app.aget_state(config)).config["configurable"]["checkpoint_id"]

    await app.ainvoke(Command(resume="8 8 8"), config=config)
    assert (await app.aget_state(config)).next == ()

    trace.clear()
    forked = {"configurable": {"thread_id": "t6", "checkpoint_id": pause_point}}
    result = await app.ainvoke(Command(resume="8 8 8"), config=forked)

    assert trace == ["asking:entered", "asking:resumed", "after"]
    assert result["answer"] == "8 8 8"


async def test_a_fork_replays_the_answer_that_was_recorded() -> None:
    """A consequence worth knowing before relying on it.

    Forking from a checkpoint that already has a resume write replays *that*
    write; the value passed to the second `Command(resume=...)` is ignored. For
    WS-7 that is the behaviour we want -- a retry re-applies the same answer --
    but it means a retry cannot correct one.
    """
    trace: list[str] = []
    app = _graph(trace)
    config = _thread("t7")

    await app.ainvoke({"visited": []}, config=config)
    pause_point = (await app.aget_state(config)).config["configurable"]["checkpoint_id"]
    await app.ainvoke(Command(resume="first"), config=config)

    forked = {"configurable": {"thread_id": "t7", "checkpoint_id": pause_point}}
    result = await app.ainvoke(Command(resume="second"), config=forked)

    assert result["answer"] == "first"
