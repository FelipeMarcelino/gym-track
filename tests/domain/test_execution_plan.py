"""WS-3: the plan walks correctly, with no database, no graph and no mock.

§11.2 makes the execution plan *data*. That is what lets these tests exist:
every scheduling rule the sprint depends on -- what may run, what a failure
skips, what a pause means for the delivery -- is decidable from a value, so it
can be falsified before a single node is wired.

The rule worth re-reading before changing anything here is the skip rule. A
`REQUIRE_SUCCESS` dependant of a failed task is SKIPPED, transitively, and
SKIPPED is not FAILED: it never ran, has no error, and must not be reported to
the user as a failure.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.domain.results import ResultVisibility, TaskType
from app.domain.workflow.plan import (
    Dependency,
    EmptyPlanError,
    ExecutionPlan,
    PlanCycleError,
    PlannedTask,
    UnknownDependencyError,
    WorkflowOutcome,
)
from app.domain.workflow.tasks import DependencyPolicy, TaskStatus

FIXTURES = Path(__file__).parent / "fixtures" / "execution_plans.json"


def _task(
    key: str,
    *,
    depends_on: tuple[Dependency, ...] = (),
    visibility: ResultVisibility = ResultVisibility.USER_VISIBLE,
) -> PlannedTask:
    return PlannedTask(
        key=key,
        task_type=TaskType.CONVERSATION,
        result_visibility=visibility,
        depends_on=depends_on,
    )


def _keys(tasks: tuple[PlannedTask, ...]) -> set[str]:
    return {task.key for task in tasks}


def _requires(*keys: str) -> tuple[Dependency, ...]:
    return tuple(Dependency(task_key=key) for key in keys)


def test_independent_tasks_are_ready_together() -> None:
    """Parallelism is derived from dependencies, never stored (§11.2)."""
    plan = ExecutionPlan(tasks=(_task("a"), _task("b")))

    assert _keys(plan.ready()) == {"a", "b"}


def test_a_diamond_schedules_in_two_waves() -> None:
    plan = ExecutionPlan(
        tasks=(
            _task("a"),
            _task("b", depends_on=_requires("a")),
            _task("c", depends_on=_requires("a")),
            _task("d", depends_on=_requires("b", "c")),
        )
    )

    assert _keys(plan.ready()) == {"a"}

    plan = plan.completed("a", facts={})
    assert _keys(plan.ready()) == {"b", "c"}

    plan = plan.completed("b", facts={})
    assert _keys(plan.ready()) == {"c"}, "d must wait for both of its dependencies"

    plan = plan.completed("c", facts={})
    assert _keys(plan.ready()) == {"d"}


def test_a_required_predecessor_that_fails_skips_the_whole_subtree() -> None:
    """Transitively. A grandchild of a failed task never becomes runnable, and
    a plan that only skipped the direct dependant would deadlock on it."""
    plan = ExecutionPlan(
        tasks=(
            _task("a"),
            _task("b", depends_on=_requires("a")),
            _task("c", depends_on=_requires("b")),
        )
    )

    plan = plan.failed("a", error="boom")

    assert plan.task("b").status is TaskStatus.SKIPPED
    assert plan.task("c").status is TaskStatus.SKIPPED
    assert plan.ready() == ()
    assert plan.is_terminal()


def test_a_skipped_task_is_not_a_failed_task() -> None:
    """It never ran, so it has no error of its own -- and a workflow that
    committed something must not be reported as a failure (Q28)."""
    plan = ExecutionPlan(tasks=(_task("a"), _task("b", depends_on=_requires("a")), _task("c")))

    plan = plan.completed("c", facts={"written": "1"}).failed("a", error="boom")

    assert plan.task("b").status is TaskStatus.SKIPPED
    assert plan.task("b").error is None
    assert plan.outcome() is WorkflowOutcome.PARTIAL_SUCCESS


def test_everything_failing_is_a_failure_not_a_partial_success() -> None:
    plan = ExecutionPlan(tasks=(_task("a"), _task("b", depends_on=_requires("a"))))

    plan = plan.failed("a", error="boom")

    assert plan.outcome() is WorkflowOutcome.FAILED


def test_allow_partial_runs_anyway() -> None:
    """The dependency's absence is the dependant's problem to handle, not a
    reason to discard work the user asked for."""
    plan = ExecutionPlan(
        tasks=(
            _task("a"),
            _task(
                "b",
                depends_on=(Dependency("a", DependencyPolicy.ALLOW_PARTIAL),),
            ),
        )
    )

    plan = plan.failed("a", error="boom")

    assert plan.task("b").status is TaskStatus.PENDING
    assert _keys(plan.ready()) == {"b"}


def test_allow_partial_still_waits_for_its_dependency_to_finish() -> None:
    """ "Run with whatever it produced" means running *after* it, or there is
    nothing produced to run with."""
    plan = ExecutionPlan(
        tasks=(
            _task("a"),
            _task("b", depends_on=(Dependency("a", DependencyPolicy.ALLOW_PARTIAL),)),
        )
    )

    assert _keys(plan.ready()) == {"a"}


def test_optional_never_blocks() -> None:
    """The distinction from ALLOW_PARTIAL, and the reason both exist: an
    optional dependency orders nothing. If its result is there, use it."""
    plan = ExecutionPlan(
        tasks=(
            _task("a"),
            _task("b", depends_on=(Dependency("a", DependencyPolicy.OPTIONAL),)),
        )
    )

    assert _keys(plan.ready()) == {"a", "b"}

    plan = plan.failed("a", error="boom")
    assert plan.task("b").status is TaskStatus.PENDING


def test_a_cycle_is_refused_at_construction() -> None:
    """Discovered here it is an error naming both keys; discovered at
    scheduling time it is a worker that consumes a partition forever."""
    with pytest.raises(PlanCycleError) as failure:
        ExecutionPlan(
            tasks=(
                _task("a", depends_on=_requires("b")),
                _task("b", depends_on=_requires("a")),
            )
        )

    assert "a" in str(failure.value) and "b" in str(failure.value)


def test_a_task_depending_on_itself_is_a_cycle() -> None:
    with pytest.raises(PlanCycleError):
        ExecutionPlan(tasks=(_task("a", depends_on=_requires("a")),))


def test_an_unknown_dependency_is_refused() -> None:
    """A typo in a task key would otherwise deadlock `ready()` forever, which
    presents as a workflow that never finishes rather than as a mistake."""
    with pytest.raises(UnknownDependencyError) as failure:
        ExecutionPlan(tasks=(_task("a", depends_on=_requires("typo")),))

    assert "typo" in str(failure.value)


def test_two_tasks_cannot_share_a_key() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        ExecutionPlan(tasks=(_task("a"), _task("a")))


def test_an_empty_plan_is_refused() -> None:
    """A planner that produced nothing is a bug, and a plan that is terminal
    the moment it is built would hide it behind a successful workflow."""
    with pytest.raises(EmptyPlanError):
        ExecutionPlan(tasks=())


def test_a_plan_is_terminal_only_when_nothing_can_run() -> None:
    plan = ExecutionPlan(tasks=(_task("a"), _task("b", depends_on=_requires("a"))))

    assert not plan.is_terminal()

    plan = plan.completed("a", facts={})
    assert not plan.is_terminal(), "b is still pending"

    plan = plan.completed("b", facts={})
    assert plan.is_terminal()
    assert plan.outcome() is WorkflowOutcome.SUCCEEDED


def test_a_running_plan_is_not_terminal() -> None:
    plan = ExecutionPlan(tasks=(_task("a"),)).started("a")

    assert plan.task("a").status is TaskStatus.RUNNING
    assert not plan.is_terminal()


def test_a_waiting_task_ends_this_delivery() -> None:
    """WAITING_FOR_USER is terminal for the delivery and not for the workflow:
    the message is answered and acked, and the run resumes on another one."""
    plan = ExecutionPlan(tasks=(_task("a"), _task("b", depends_on=_requires("a"))))

    plan = plan.waiting("a")

    assert plan.is_terminal()
    assert plan.outcome() is WorkflowOutcome.WAITING_FOR_USER
    assert plan.task("b").status is TaskStatus.PENDING, "not skipped: it may still run on resume"


def test_a_pause_does_not_end_a_delivery_that_can_still_do_something() -> None:
    """Terminality is a question about what can run, not a list of statuses.

    Here `b` does not require `a` to succeed, so a suspended `a` leaves real
    work available. A plan that decided terminality by looking at statuses
    would end the delivery with a runnable task in it -- and the user would get
    silence for something the system could have done immediately.
    """
    plan = ExecutionPlan(
        tasks=(
            _task("a"),
            _task("b", depends_on=(Dependency("a", DependencyPolicy.OPTIONAL),)),
        )
    )

    plan = plan.waiting("a")

    assert not plan.is_terminal()
    assert _keys(plan.ready()) == {"b"}


def test_a_transition_returns_a_new_plan() -> None:
    """Immutability is what makes a checkpointed plan safe to keep: the value
    that was written cannot change under the run that wrote it."""
    plan = ExecutionPlan(tasks=(_task("a"),))

    advanced = plan.completed("a", facts={})

    assert plan.task("a").status is TaskStatus.PENDING
    assert advanced.task("a").status is TaskStatus.COMPLETED


def test_completing_a_task_keeps_its_facts() -> None:
    plan = ExecutionPlan(tasks=(_task("a"),)).completed("a", facts={"sets": "3"})

    assert plan.task("a").facts == {"sets": "3"}


def test_an_unknown_key_cannot_be_transitioned() -> None:
    plan = ExecutionPlan(tasks=(_task("a"),))

    with pytest.raises(KeyError):
        plan.completed("b", facts={})


def _plan_from_fixture(spec: dict[str, Any]) -> ExecutionPlan:
    return ExecutionPlan(
        tasks=tuple(
            PlannedTask(
                key=task["key"],
                task_type=TaskType(task.get("task_type", "conversation")),
                result_visibility=ResultVisibility(task.get("visibility", "user_visible")),
                depends_on=tuple(
                    Dependency(
                        task_key=dependency["task_key"],
                        policy=DependencyPolicy(dependency["policy"]),
                    )
                    for dependency in task.get("depends_on", [])
                ),
            )
            for task in spec["tasks"]
        )
    )


@pytest.mark.parametrize(
    "case", json.loads(FIXTURES.read_text(encoding="utf-8")), ids=lambda case: case["name"]
)
def test_the_golden_plans_walk_the_same_way(case: dict[str, Any]) -> None:
    """The regression net for the day someone "simplifies" the skip rule.

    Each case names its waves in order and the outcome they add up to, so a
    change in scheduling shows up as a diff in a file a reviewer can read
    rather than as a subtly different production trace.
    """
    plan = _plan_from_fixture(case)

    for wave in case["waves"]:
        assert _keys(plan.ready()) == set(wave["ready"])
        for key in wave.get("completed", []):
            plan = plan.completed(key, facts={})
        for key in wave.get("failed", []):
            plan = plan.failed(key, error="boom")
        for key in wave.get("waiting", []):
            plan = plan.waiting(key)

    assert plan.is_terminal()
    assert plan.outcome() is WorkflowOutcome(case["outcome"])
    assert {task.key: task.status.value for task in plan.tasks} == case["final_statuses"]
