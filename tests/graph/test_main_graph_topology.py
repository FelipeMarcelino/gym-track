"""WS-4: the graph's shape, checked against the specification's own diagram.

DEC-002 says the topology is static and the *plan* is what varies. A test that
compared the graph to a list in the same repository would only prove the list
matches itself, so this one parses §11.1's diagram out of the spec and compares
against that. An edge added by hand to route around a bug fails here, which is
what makes adding it a decision instead of a detail.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langgraph.graph import END, START

from app.application.services.execution_planner import (
    DeterministicExecutionPlanner,
    DeterministicIntentRouter,
)
from app.graphs.main.graph import NODE_SEQUENCE, build_main_graph
from app.graphs.main.state import GRAPH_VERSION

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC = REPO_ROOT / "doc" / "whatsapp_training_ai_architecture_v1.1.md"


def _spec_nodes() -> tuple[str, ...]:
    """The node names §11.1 lists, in order.

    The diagram nests the scheduler's internals one level deeper (fan-out,
    reducer, repeat); those are how `task_scheduler` works, not separate nodes,
    so only the top level counts.
    """
    lines = SPEC.read_text(encoding="utf-8").splitlines()
    start = lines.index("## 11.1 Static graph, dynamic plan")
    block_start = lines.index("```text", start) + 1
    block_end = lines.index("```", block_start)

    names: list[str] = []
    for raw in lines[block_start:block_end]:
        if not raw.startswith("  -> "):
            continue
        name = raw.removeprefix("  -> ").strip()
        # "persist_outbound + outbox" is one node and a note about what it
        # writes.
        name = name.split(" + ", 1)[0]
        if name in {"START", "END"}:
            continue
        names.append(name)
    return tuple(names)


@pytest.fixture
def graph():  # type: ignore[no-untyped-def]
    return build_main_graph(
        router=DeterministicIntentRouter(), planner=DeterministicExecutionPlanner()
    )


def test_the_spec_diagram_was_parsed() -> None:
    """Guards the parser: a spec edit that breaks it must not silently turn
    every assertion below into a comparison of two empty tuples."""
    assert len(_spec_nodes()) >= 8


def test_the_graph_has_exactly_the_nodes_the_spec_lists(graph) -> None:  # type: ignore[no-untyped-def]
    compiled = set(graph.get_graph().nodes) - {START, END, "__start__", "__end__"}

    assert compiled == set(_spec_nodes())


def test_the_nodes_are_in_the_order_the_spec_lists(graph) -> None:  # type: ignore[no-untyped-def]
    assert NODE_SEQUENCE == _spec_nodes()


def test_the_edges_are_the_straight_line_the_spec_draws(graph) -> None:  # type: ignore[no-untyped-def]
    """The test that catches a "quick fix" edge added to route around a bug."""
    edges = {(edge.source, edge.target) for edge in graph.get_graph().edges if not edge.conditional}
    expected = {("__start__", NODE_SEQUENCE[0]), (NODE_SEQUENCE[-1], "__end__")}
    expected |= set(zip(NODE_SEQUENCE, NODE_SEQUENCE[1:], strict=False))

    assert edges == expected


def test_the_graph_is_compiled_from_a_fixed_topology(graph) -> None:  # type: ignore[no-untyped-def]
    """Q121: two builds produce the same shape, because nothing about the
    topology depends on an input. What varies is the plan."""
    other = build_main_graph(
        router=DeterministicIntentRouter(), planner=DeterministicExecutionPlanner()
    )

    assert set(other.get_graph().nodes) == set(graph.get_graph().nodes)


def test_the_graph_version_is_a_pinned_constant() -> None:
    """Q132. Recorded on every execution row, so a graph change without a
    version bump makes two different shapes indistinguishable in the data."""
    assert GRAPH_VERSION == "main.v1"
