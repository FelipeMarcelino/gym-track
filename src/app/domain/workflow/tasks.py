"""The execution task vocabulary (§11.2, Q127, Q128).

Two enums, and the interesting one is `DependencyPolicy`: a dependency that
failed does not mean the same thing to every dependant, and collapsing the
three cases into "blocked" is how a workflow either runs a task it should have
skipped or skips one it could have run with what it had.
"""

from __future__ import annotations

from enum import StrEnum


class TaskStatus(StrEnum):
    """§11.2's lifecycle.

    `SKIPPED` is deliberately not `FAILED`: a skipped task never ran, has no
    error of its own, and must not be reported to the user as a failure.
    `WAITING_FOR_USER` is terminal *for one delivery* -- the workflow ends and
    resumes on a later message.
    """

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    WAITING_FOR_USER = "waiting_for_user"
    SKIPPED = "skipped"


#: Statuses that mean the task is over and will not run again in this delivery.
TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.SKIPPED}
)


class DependencyPolicy(StrEnum):
    """What a dependant does when its dependency did not succeed (Q127).

    * `REQUIRE_SUCCESS` -- the dependant is skipped, transitively.
    * `ALLOW_PARTIAL` -- it runs anyway; the absence is its problem to handle.
    * `OPTIONAL` -- the dependency never blocked it in the first place.
    """

    REQUIRE_SUCCESS = "require_success"
    ALLOW_PARTIAL = "allow_partial"
    OPTIONAL = "optional"
