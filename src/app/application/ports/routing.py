"""Where the LLM lands in Sprint 4 (§11.1, §12).

Two ports, both `async`, both with deterministic implementations behind them
this sprint. The asynchrony is not speculative: a synchronous signature would
force every call site to change the day a network call goes behind it, which is
exactly the rewrite these ports exist to prevent.

Neither signature mentions LangGraph or LangChain. The graph is an adapter; the
application layer must keep talking in its own vocabulary, or Sprint 4 swaps a
node and finds the coupling everywhere.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from app.domain.results import TaskType
from app.domain.workflow.plan import ExecutionPlan


class IntentRouterPort(Protocol):
    async def route(self, texts: Sequence[str]) -> tuple[TaskType, ...]:
        """Classify one batch. No side effects (§12).

        A tuple because Q28 and §39.3 allow several intents in one message,
        even though this sprint's implementation never returns more than one.
        """
        ...


class ExecutionPlannerPort(Protocol):
    async def plan(self, intents: Sequence[TaskType], *, texts: Sequence[str]) -> ExecutionPlan:
        """Turn intents into a task DAG (§11.2, DEC-002).

        Returns a plan rather than a topology: MainGraph is compiled once and
        never rebuilt, and what varies per message is this value.
        """
        ...
