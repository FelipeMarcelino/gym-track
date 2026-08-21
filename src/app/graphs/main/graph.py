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


def build_main_graph(
    *,
    router: IntentRouterPort,
    planner: ExecutionPlannerPort,
    checkpointer: BaseCheckpointSaver[str] | None = None,
) -> CompiledStateGraph[MainGraphState, WorkerContext]:
    """Compile the graph.

    **Called once, by the composition root** -- `worker_runtime` builds it at
    process start and hands it to the worker, the same way it already hands
    over the engine, the session factory and the handlers. Q121 is a property
    of that startup, not of this function.

    An earlier draft memoised this instead, and review took it apart in two
    steps: a bound of one made two compositions evict each other, and keying a
    cache on the collaborators requires them to be hashable, which no port
    demands and a plain `@dataclass` implementation does not satisfy. Both are
    symptoms of the same mistake -- a cache pretending there is no composition
    root when there is one. The entrypoint owns the graph; WS-7 wires it and
    asserts it builds once.
    """
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
