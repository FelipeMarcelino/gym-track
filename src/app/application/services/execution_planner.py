"""The deterministic router and planner (§11.1, D5, D6).

Sprint 4 replaces both bodies with structured LLM calls. Everything about the
shape here exists so that is a substitution: the ports are the seam, the plan
is a value, and the graph never learns which kind of thing produced it.

For real traffic this sprint the planner emits **single-task plans**, because a
deterministic router cannot discover a second intent in a `#log` line. That is
a stated gap rather than a hidden one: the DAG engine's dependencies, policies
and skipping are exercised by WS-3's plan-level tests over synthetic plans, and
Sprint 4's planner is what makes multi-task plans arrive from real messages.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.domain.results import ResultVisibility, TaskType
from app.domain.workflow.plan import ExecutionPlan, PlannedTask
from app.graphs.main.routing import route


class DeterministicIntentRouter:
    """Sprint 2's `route()` behind the port, unchanged in behaviour."""

    async def route(self, texts: Sequence[str]) -> tuple[TaskType, ...]:
        return (route(texts),)


class DeterministicExecutionPlanner:
    """One task per routed intent, in the order they were routed.

    No dependencies: a deterministic router produces intents that do not
    inform each other. The moment Sprint 4's planner can say "recommendation
    needs analysis" (Q81), it says so by emitting a `Dependency` -- and nothing
    here or downstream changes shape to accommodate it.
    """

    async def plan(self, intents: Sequence[TaskType], *, texts: Sequence[str]) -> ExecutionPlan:
        return ExecutionPlan(
            tasks=tuple(
                PlannedTask(
                    key=self.task_key(intent, index),
                    task_type=intent,
                    result_visibility=ResultVisibility.USER_VISIBLE,
                )
                for index, intent in enumerate(intents)
            )
        )

    @staticmethod
    def task_key(intent: TaskType, index: int) -> str:
        """Stable within a plan and across a re-plan of the same batch.

        `execution_tasks` has `UNIQUE (workflow_execution_id, task_key)` and
        WS-5 writes with `ON CONFLICT DO NOTHING`, so a redelivery that
        re-plans must produce the same keys or it writes a second set of rows
        for the same work.
        """
        return f"{intent.value}:{index}"
