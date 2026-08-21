"""The execution plan, as data (§11.1, §11.2, DEC-002, Q121, Q122, Q127).

MainGraph is static and compiled once. What varies per message is *this* -- a
DAG of tasks with dependency policies, carried through the graph as a value.
The whole point of DEC-002 is that a new kind of request changes a plan rather
than a topology, so everything here is decidable without a database, a graph or
a mock, and the tests prove exactly that.

Two absences are deliberate and worth defending:

* **No `can_run_parallel`.** Parallelism is derived from `depends_on`. Storing
  it would create a second source of truth that can disagree with the first,
  and §11.2 says not to.
* **No stored `READY`.** Readiness is a *question about* a plan, not a fact
  inside one, for the same reason. `TaskStatus.READY` exists because §11.2's
  vocabulary lists it and the persistence layer records the moment a scheduler
  picked a task up; the plan itself never stores it.

Everything is immutable. A transition returns a new plan, so the value written
into a checkpoint cannot change under the run that wrote it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum

from app.domain.results import ResultVisibility, TaskType
from app.domain.workflow.tasks import FINISHED_TASK_STATUSES, DependencyPolicy, TaskStatus


class PlanError(ValueError):
    """A plan that could not be built. Always a bug in the planner."""


class EmptyPlanError(PlanError):
    def __init__(self) -> None:
        super().__init__("an execution plan with no tasks is a planner that produced nothing")


class UnknownDependencyError(PlanError):
    def __init__(self, task_key: str, dependency: str) -> None:
        super().__init__(f"task {task_key!r} depends on {dependency!r}, which is not in the plan")
        self.task_key = task_key
        self.dependency = dependency


class PlanCycleError(PlanError):
    def __init__(self, keys: tuple[str, ...]) -> None:
        super().__init__(f"the plan's dependencies form a cycle: {', '.join(keys)}")
        self.keys = keys


class WorkflowOutcome(StrEnum):
    """How one delivery of one plan ended (Q28).

    Kept in the domain, and mirrored by `WorkflowExecutionStatus` in the ORM
    rather than shared with it: the database also needs RUNNING, which is a
    fact about a row and not about a plan.
    """

    SUCCEEDED = "succeeded"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"
    WAITING_FOR_USER = "waiting_for_user"


@dataclass(frozen=True, slots=True)
class Dependency:
    """One edge, and what its dependant does when it does not succeed (Q127)."""

    task_key: str
    policy: DependencyPolicy = DependencyPolicy.REQUIRE_SUCCESS


@dataclass(frozen=True, slots=True)
class PlannedTask:
    key: str
    task_type: TaskType
    result_visibility: ResultVisibility
    payload: Mapping[str, str] = field(default_factory=dict)
    depends_on: tuple[Dependency, ...] = ()
    status: TaskStatus = TaskStatus.PENDING
    #: What the handler produced. The response guard reads these, so they are
    #: carried rather than re-derived.
    facts: Mapping[str, str] = field(default_factory=dict)
    #: Set only by `failed`. A SKIPPED task never ran and must stay None, or a
    #: workflow reports a failure the user did not cause.
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    tasks: tuple[PlannedTask, ...]

    def __post_init__(self) -> None:
        if not self.tasks:
            raise EmptyPlanError

        keys = [task.key for task in self.tasks]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise PlanError(f"duplicate task keys in the plan: {', '.join(duplicates)}")

        known = set(keys)
        for task in self.tasks:
            for dependency in task.depends_on:
                if dependency.task_key not in known:
                    raise UnknownDependencyError(task.key, dependency.task_key)

        self._refuse_cycles()

    def _refuse_cycles(self) -> None:
        """Depth-first, so the error can name the keys that form the cycle.

        Found here it is a message a reviewer can act on. Found at scheduling
        time it is a `ready()` that returns nothing forever while tasks remain
        pending -- a worker holding a partition and never acking.
        """
        edges = {task.key: tuple(d.task_key for d in task.depends_on) for task in self.tasks}
        visiting: list[str] = []
        done: set[str] = set()

        def walk(key: str) -> None:
            if key in done:
                return
            if key in visiting:
                cycle = visiting[visiting.index(key) :]
                raise PlanCycleError((*cycle, key))
            visiting.append(key)
            for dependency in edges[key]:
                walk(dependency)
            visiting.pop()
            done.add(key)

        for key in edges:
            walk(key)

    # -- reading -----------------------------------------------------------

    def task(self, key: str) -> PlannedTask:
        for task in self.tasks:
            if task.key == key:
                return task
        raise KeyError(key)

    def ready(self) -> tuple[PlannedTask, ...]:
        """Every PENDING task whose dependencies allow it to run now.

        Returning several is a statement about the *plan*, not an instruction
        to run them concurrently: they share the worker's one session, and
        WS-5 runs them one at a time for that reason.
        """
        return tuple(
            task
            for task in self.tasks
            if task.status is TaskStatus.PENDING and self._dependencies_allow(task)
        )

    def _dependencies_allow(self, task: PlannedTask) -> bool:
        for dependency in task.depends_on:
            status = self.task(dependency.task_key).status
            match dependency.policy:
                case DependencyPolicy.REQUIRE_SUCCESS:
                    if status is not TaskStatus.COMPLETED:
                        return False
                case DependencyPolicy.ALLOW_PARTIAL:
                    # "Run with whatever it produced" still means running after
                    # it: there is nothing produced to run with otherwise.
                    #
                    # `FINISHED_TASK_STATUSES` and not the wider delivery set:
                    # a dependency merely *waiting* on a user has produced
                    # nothing, and releasing its dependant then would run it
                    # early with the absence it was meant to tolerate rather
                    # than the partial result it was meant to use.
                    if status not in FINISHED_TASK_STATUSES:
                        return False
                case DependencyPolicy.OPTIONAL:
                    # Orders nothing. If the result is there, the handler uses
                    # it; if not, it proceeds. This is the whole distinction
                    # from ALLOW_PARTIAL.
                    continue
        return True

    def is_terminal(self) -> bool:
        """Nothing can run again in this delivery.

        Asked as a question about what *can* happen rather than as a list of
        statuses, because "pending" is not the same as "runnable". A task whose
        required dependency is suspended stays PENDING forever in this delivery
        -- it may run when the user answers, and not before -- so a plan that
        waited for it to leave PENDING would hold the partition until somebody
        replied. `ready()` already knows all of that.

        The RUNNING clause is separate because a task in flight is not ready
        and not finished, and the plan is obviously still live.
        """
        return not self.ready() and not any(
            task.status is TaskStatus.RUNNING for task in self.tasks
        )

    def outcome(self) -> WorkflowOutcome:
        statuses = {task.status for task in self.tasks}

        if TaskStatus.WAITING_FOR_USER in statuses:
            return WorkflowOutcome.WAITING_FOR_USER
        if TaskStatus.COMPLETED in statuses:
            unfinished = statuses & {TaskStatus.FAILED, TaskStatus.SKIPPED}
            return WorkflowOutcome.PARTIAL_SUCCESS if unfinished else WorkflowOutcome.SUCCEEDED
        # Nothing completed. Skips only ever follow a failure, so this is a
        # workflow that produced no business effect at all.
        return WorkflowOutcome.FAILED

    # -- transitions -------------------------------------------------------

    def started(self, key: str) -> ExecutionPlan:
        return self._with(key, status=TaskStatus.RUNNING)

    def completed(self, key: str, *, facts: Mapping[str, str]) -> ExecutionPlan:
        return self._with(key, status=TaskStatus.COMPLETED, facts=dict(facts))

    def waiting(self, key: str) -> ExecutionPlan:
        """The task interrupted. Its dependants stay PENDING rather than being
        skipped: they may still run when the user answers."""
        return self._with(key, status=TaskStatus.WAITING_FOR_USER)

    def failed(self, key: str, *, error: str) -> ExecutionPlan:
        """Record the failure, then skip whatever required it -- transitively.

        Skipping only the direct dependants would leave their own dependants
        PENDING with a dependency that can never complete, which is a plan that
        never reaches a terminal state.
        """
        plan = self._with(key, status=TaskStatus.FAILED, error=error)
        return plan._skip_dependants_of(key)

    def _skip_dependants_of(self, key: str) -> ExecutionPlan:
        plan = self
        for task in self.tasks:
            if task.status is not TaskStatus.PENDING:
                continue
            requires_it = any(
                dependency.task_key == key and dependency.policy is DependencyPolicy.REQUIRE_SUCCESS
                for dependency in task.depends_on
            )
            if requires_it:
                # No error: it never ran. `outcome()` reads that distinction,
                # and so does everything the user is eventually told.
                plan = plan._with(task.key, status=TaskStatus.SKIPPED)
                plan = plan._skip_dependants_of(task.key)
        return plan

    def _with(
        self,
        key: str,
        *,
        status: TaskStatus,
        facts: Mapping[str, str] | None = None,
        error: str | None = None,
    ) -> ExecutionPlan:
        """One task replaced, the rest shared. Typed rather than `**kwargs`:
        the fields a transition may touch are exactly three, and letting a
        caller pass any name would make a typo a silent no-op."""
        self.task(key)  # raises KeyError for an unknown key, before anything changes

        def transition(task: PlannedTask) -> PlannedTask:
            return replace(
                task,
                status=status,
                facts=dict(facts) if facts is not None else task.facts,
                error=error if error is not None else task.error,
            )

        return ExecutionPlan(
            tasks=tuple(transition(task) if task.key == key else task for task in self.tasks)
        )
