"""One function per §11.1 node.

Several are deliberately thin this sprint, and each says so where it is. A node
is a coordination step: the moment one computes something a service should own,
Sprint 2's boundaries start dissolving and the graph becomes the place logic
goes to hide.

WS-5 fills `task_scheduler`, WS-6 the three response nodes, WS-7
`persist_outbound`, WS-9 `resolve_pending_workflow`.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.runtime import Runtime

from app.application.ports.routing import ExecutionPlannerPort, IntentRouterPort
from app.domain.results import TaskType
from app.graphs.main.state import GRAPH_VERSION, MainGraphState, WorkerContext

logger = logging.getLogger(__name__)


async def load_base_context(
    state: MainGraphState, runtime: Runtime[WorkerContext]
) -> dict[str, Any]:
    """Identity, session and pending state (§11.1, Q27).

    Lazily, and by reference. The batch's identifiers are already in the state
    the worker built; what a handler needs beyond them, it fetches for itself.
    Loading a user's history here is how §11.4's "not a copy of the database"
    stops being true.
    """
    return {"graph_version": GRAPH_VERSION}


async def resolve_pending_workflow(
    state: MainGraphState, runtime: Runtime[WorkerContext]
) -> dict[str, Any]:
    """Record what the incoming batch means for an open question (§39.2).

    **Records, and does not compute.** A resumed run re-enters at the
    interrupted node and never visits this one, so a classification decided
    here would run only on the path where its answer is not needed. WS-9 runs
    `PendingWorkflowResolver` in the worker, before choosing how to invoke, and
    passes the decision in. The node exists because §11.1 lists it and because
    the graph should carry the fact; ADR-016 records the deviation.
    """
    return {}


async def normalize_input(state: MainGraphState, runtime: Runtime[WorkerContext]) -> dict[str, Any]:
    """Identity this sprint, and asserted to be.

    The batch's fragments are already normalized text by the time the graph
    sees them. The node exists so Sprint 4's transcript, emoji and unit
    normalization has a place to land that is not inside a handler.
    """
    return {}


#: Repeated from `graph.py` rather than imported: importing it the other way
#: would make the node module depend on the builder that consumes it.
_Node = Callable[[MainGraphState, Runtime[WorkerContext]], Awaitable[dict[str, Any]]]


def make_intent_router(router: IntentRouterPort) -> _Node:
    async def intent_router(
        state: MainGraphState, runtime: Runtime[WorkerContext]
    ) -> dict[str, Any]:
        intents = await router.route(state.get("normalized_input", ()))
        return {"intents": tuple(intent.value for intent in intents)}

    return intent_router


def make_execution_planner(planner: ExecutionPlannerPort) -> _Node:
    async def execution_planner(
        state: MainGraphState, runtime: Runtime[WorkerContext]
    ) -> dict[str, Any]:
        intents = tuple(TaskType(value) for value in state.get("intents", ()))
        plan = await planner.plan(intents, texts=state.get("normalized_input", ()))
        return {"execution_plan": plan}

    return execution_planner


async def task_scheduler(state: MainGraphState, runtime: Runtime[WorkerContext]) -> dict[str, Any]:
    """WS-5. Fans READY tasks out, reduces results, repeats until terminal."""
    return {}


async def collect_results(state: MainGraphState, runtime: Runtime[WorkerContext]) -> dict[str, Any]:
    """WS-6. Merges USER_VISIBLE results in plan order into one response."""
    return {}


async def response_normalizer(
    state: MainGraphState, runtime: Runtime[WorkerContext]
) -> dict[str, Any]:
    """WS-6. Deterministic templates behind a port this sprint (§25, DEC-004)."""
    return {}


async def response_guard(state: MainGraphState, runtime: Runtime[WorkerContext]) -> dict[str, Any]:
    """WS-6. Every number in a segment must be a fact of that entity (Q136)."""
    return {}


async def persist_outbound(
    state: MainGraphState, runtime: Runtime[WorkerContext]
) -> dict[str, Any]:
    """WS-7. Outbound rows and the outbox, in the worker's transaction (Q130)."""
    return {}
