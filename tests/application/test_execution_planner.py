"""WS-4: the deterministic router and planner behind the Sprint 4 ports.

Both are thin on purpose. What is worth testing is the shape they hand over --
the task keys, which a redelivery must reproduce exactly, and the absence of
dependencies, which is a statement about what a deterministic router can know.
"""

from __future__ import annotations

import pytest

from app.application.services.execution_planner import (
    DeterministicExecutionPlanner,
    DeterministicIntentRouter,
)
from app.domain.results import ResultVisibility, TaskType

pytestmark = [pytest.mark.asyncio]


async def test_a_marked_message_routes_to_the_workout_handler() -> None:
    assert await DeterministicIntentRouter().route(("#log supino 80kg 10",)) == (
        TaskType.LOG_WORKOUT,
    )


async def test_an_ordinary_message_routes_to_conversation() -> None:
    assert await DeterministicIntentRouter().route(("bom dia",)) == (TaskType.CONVERSATION,)


async def test_the_planner_emits_one_task_per_intent() -> None:
    plan = await DeterministicExecutionPlanner().plan(
        (TaskType.LOG_WORKOUT,), texts=("#log supino 80kg 10",)
    )

    assert [task.task_type for task in plan.tasks] == [TaskType.LOG_WORKOUT]
    assert plan.tasks[0].result_visibility is ResultVisibility.USER_VISIBLE


async def test_the_planner_emits_no_dependencies() -> None:
    """A deterministic router produces intents that do not inform each other.

    Sprint 4's planner says "recommendation needs analysis" (Q81) by emitting a
    `Dependency`; nothing downstream changes shape to accommodate it, which is
    the whole reason the plan is a value.
    """
    plan = await DeterministicExecutionPlanner().plan(
        (TaskType.LOG_WORKOUT, TaskType.CONVERSATION), texts=("x",)
    )

    assert all(task.depends_on == () for task in plan.tasks)
    assert len(plan.ready()) == 2, "independent tasks are ready together"


async def test_task_keys_survive_a_re_plan_of_the_same_batch() -> None:
    """`execution_tasks` has UNIQUE (workflow_execution_id, task_key) and WS-5
    writes with ON CONFLICT DO NOTHING. A redelivery that re-planned into
    different keys would write a second set of rows for the same work."""
    planner = DeterministicExecutionPlanner()
    intents = (TaskType.LOG_WORKOUT, TaskType.CONVERSATION)

    first = await planner.plan(intents, texts=("x",))
    second = await planner.plan(intents, texts=("x",))

    assert [task.key for task in first.tasks] == [task.key for task in second.tasks]


async def test_two_intents_of_one_type_get_distinct_keys() -> None:
    """The index in the key is not decoration: a plan is refused at
    construction if two tasks share one."""
    plan = await DeterministicExecutionPlanner().plan(
        (TaskType.CONVERSATION, TaskType.CONVERSATION), texts=("x",)
    )

    assert len({task.key for task in plan.tasks}) == 2
