"""WS-2: two notions of "over", and why they are not one set.

A task that paused for a clarification is over *for this delivery* and not
finished: it has no result and no `finished_at`, and its dependants may still
run once the user answers. A single set covering both would either let an
`ALLOW_PARTIAL` dependant run while its dependency is still waiting on a human,
or keep a partition open until somebody replies.
"""

from __future__ import annotations

from app.domain.workflow.tasks import (
    DELIVERY_TERMINAL_STATUSES,
    FINISHED_TASK_STATUSES,
    TaskStatus,
)


def test_a_waiting_task_ends_the_delivery_without_finishing() -> None:
    assert TaskStatus.WAITING_FOR_USER in DELIVERY_TERMINAL_STATUSES
    assert TaskStatus.WAITING_FOR_USER not in FINISHED_TASK_STATUSES


def test_finishing_is_the_narrower_notion() -> None:
    assert FINISHED_TASK_STATUSES < DELIVERY_TERMINAL_STATUSES


def test_the_finished_set_is_what_the_database_pairs_with_finished_at() -> None:
    """Migration 0011 spells this out in SQL. If the two ever disagree, a task
    is refused by a CHECK the domain thinks it satisfies."""
    assert {status.value for status in FINISHED_TASK_STATUSES} == {
        "completed",
        "failed",
        "skipped",
    }


def test_a_task_still_in_flight_is_in_neither() -> None:
    for status in (TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RUNNING):
        assert status not in DELIVERY_TERMINAL_STATUSES
