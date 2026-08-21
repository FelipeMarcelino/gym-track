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

    `WAITING_FOR_USER` is **suspended, not finished**. It ends the current
    delivery -- the message is answered and acked -- while the task itself has
    produced nothing, carries no `finished_at`, and may still complete when the
    user replies. `DELIVERY_TERMINAL_STATUSES` and `FINISHED_TASK_STATUSES`
    below are that distinction, and describing it as "terminal" without saying
    which of the two is meant is how a caller ends up stamping a completion
    time the database then refuses.
    """

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    WAITING_FOR_USER = "waiting_for_user"
    SKIPPED = "skipped"


#: The task ran -- or was skipped -- and produced whatever it is ever going to
#: produce. This is the set that pairs with `finished_at` in the database, and
#: the one a dependant with `ALLOW_PARTIAL` waits for: "run with whatever it
#: produced" needs something to have been produced.
FINISHED_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.SKIPPED}
)

#: Nothing more will happen in *this delivery*. A wider set, and the difference
#: is exactly `WAITING_FOR_USER`: a paused task has not finished -- it has no
#: result, no `finished_at`, and its dependants may still run when the user
#: answers -- but the message is answered and acked, so the plan is done for
#: now.
#:
#: Keeping these two apart is not pedantry. Collapsing them either lets an
#: `ALLOW_PARTIAL` dependant run while its dependency is still waiting on a
#: human, or holds a partition open until somebody replies.
DELIVERY_TERMINAL_STATUSES: frozenset[TaskStatus] = FINISHED_TASK_STATUSES | {
    TaskStatus.WAITING_FOR_USER
}


class DependencyPolicy(StrEnum):
    """What a dependant does when its dependency did not succeed (Q127).

    * `REQUIRE_SUCCESS` -- the dependant is skipped, transitively.
    * `ALLOW_PARTIAL` -- it runs anyway; the absence is its problem to handle.
    * `OPTIONAL` -- the dependency never blocked it in the first place.
    """

    REQUIRE_SUCCESS = "require_success"
    ALLOW_PARTIAL = "allow_partial"
    OPTIONAL = "optional"
