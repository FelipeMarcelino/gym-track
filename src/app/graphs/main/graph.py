"""MainGraph: static, compiled once, and shaped exactly like §11.1.

DEC-002 in one sentence: the topology is fixed and the *plan* is what varies
per message. So this module builds a graph whose nodes and edges never depend
on the input, and `tests/graph/test_main_graph_topology.py` asserts that shape
against the specification's own diagram rather than against this file.

Compilation happens once per process (Q121). A graph rebuilt per message would
still work and would quietly make the "static" in DEC-002 a comment.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import cache
from itertools import pairwise
from typing import Any, Final

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.application.ports.routing import ExecutionPlannerPort, IntentRouterPort
from app.graphs.main import nodes
from app.graphs.main.state import MainGraphState, WorkerContext

#: The §11.1 order, as data. The topology test parses the same list out of the
#: specification and compares, so a node added here without amending the spec
#: (or an ADR) fails rather than drifting.
NODE_SEQUENCE: Final[tuple[str, ...]] = (
    "load_base_context",
    "resolve_pending_workflow",
    "normalize_input",
    "intent_router",
    "execution_planner",
    "task_scheduler",
    "collect_results",
    "response_normalizer",
    "response_guard",
    "persist_outbound",
)


@cache
def compiled_main_graph(
    *,
    router: IntentRouterPort,
    planner: ExecutionPlannerPort,
    checkpointer: BaseCheckpointSaver[str] | None = None,
) -> CompiledStateGraph[MainGraphState, WorkerContext]:
    """The process's graph. Built on first use and reused afterwards (Q121).

    `build_main_graph` below is the pure constructor -- tests want a fresh one
    per case. This is the accessor an entrypoint calls, and the memo is what
    makes "static" a property of the running process rather than a claim in a
    docstring: compilation walks every node and edge, and doing it per message
    would still work while paying for a rebuild on every WhatsApp fragment.

    The cache is keyed on the collaborators, so a worker composed differently
    -- one without the workout service, say -- gets its own graph rather than
    silently reusing another's.

    `cache` rather than a bounded LRU, and that is the point rather than an
    oversight. A bound of one would make two coexisting compositions evict each
    other: A, then B, then A
    again compiles A twice, which is exactly the reuse this function exists to
    provide. There is no growth to bound anyway -- an entry is one composition,
    and a process composes at startup. Something building compositions in a
    loop is the bug this accessor is meant to prevent, not a case to cap.
    """
    return build_main_graph(router=router, planner=planner, checkpointer=checkpointer)


def build_main_graph(
    *,
    router: IntentRouterPort,
    planner: ExecutionPlannerPort,
    checkpointer: BaseCheckpointSaver[str] | None = None,
) -> CompiledStateGraph[MainGraphState, WorkerContext]:
    """Compile the graph. Call once, at process start (Q121)."""
    builder: StateGraph[MainGraphState, WorkerContext, MainGraphState, MainGraphState] = StateGraph(
        MainGraphState, context_schema=WorkerContext
    )

    # `Any` rather than a callable alias: LangGraph's `add_node` overloads
    # infer their input schema from the concrete function, and a generic alias
    # matches none of them. The nodes themselves are typed where they are
    # written; this mapping only carries them to the builder.
    implementations: Mapping[str, Any] = {
        "load_base_context": nodes.load_base_context,
        "resolve_pending_workflow": nodes.resolve_pending_workflow,
        "normalize_input": nodes.normalize_input,
        "intent_router": nodes.make_intent_router(router),
        "execution_planner": nodes.make_execution_planner(planner),
        "task_scheduler": nodes.task_scheduler,
        "collect_results": nodes.collect_results,
        "response_normalizer": nodes.response_normalizer,
        "response_guard": nodes.response_guard,
        "persist_outbound": nodes.persist_outbound,
    }

    for name in NODE_SEQUENCE:
        builder.add_node(name, implementations[name])

    builder.add_edge(START, NODE_SEQUENCE[0])
    for source, target in pairwise(NODE_SEQUENCE):
        builder.add_edge(source, target)
    builder.add_edge(NODE_SEQUENCE[-1], END)

    return builder.compile(checkpointer=checkpointer)
