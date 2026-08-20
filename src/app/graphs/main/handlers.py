"""The task handler registry (§11.3).

MainGraph does not care whether a handler is a deterministic service, a
LangGraph subgraph or a tool-calling agent -- it resolves a task type and calls
it. Sprint 3 replaces the registry's *contents*; this shape is what keeps that
an addition rather than a rewrite.

The stub handler here is deliberately trivial and deliberately obvious. Any
domain logic that leaks into it has to be moved in Sprint 2, so it acknowledges
and says nothing else.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import UUID

from app.application.services.strict_syntax import StrictSyntaxError, parse
from app.domain.results import DomainResult, OutboundText, ResultVisibility, TaskType
from app.domain.training.confirmations import (
    clarification_request,
    partial_confirmation,
    workout_confirmation,
)

if TYPE_CHECKING:
    from app.application.ports.workout_builder import WorkoutCommandBuilderPort
    from app.application.services.workout_logging import WorkoutApplicationService


@dataclass(frozen=True, slots=True)
class TaskInput:
    """Everything a handler is given about one workflow input."""

    task_type: TaskType
    message_batch_id: UUID
    user_id: UUID
    conversation_id: UUID
    #: The batch's fragments, in arrival order.
    texts: tuple[str, ...]
    #: The messages those fragments came from. §26.2 needs them: a stored set
    #: has to name the message it exists because of, and the batch alone
    #: cannot answer "which of my messages produced this".
    message_ids: tuple[UUID, ...] = ()


TaskHandler = Callable[[TaskInput], Awaitable[DomainResult]]


class UnknownTaskTypeError(LookupError):
    def __init__(self, task_type: str) -> None:
        super().__init__(f"no handler is registered for task type {task_type!r}")
        self.task_type = task_type


#: Temporary seam. Sprint 2 replaces this with the deterministic workout domain
#: and Sprint 3 with MainGraph; nothing else in the worker changes.
ACKNOWLEDGEMENT: Final = "Recebi sua mensagem. Ainda estou aprendendo a responder de verdade."


async def acknowledge(task: TaskInput) -> DomainResult:
    """The entire intelligence of this sprint: say something fixed, correctly."""
    return DomainResult(
        task_type=TaskType.CONVERSATION,
        visibility=ResultVisibility.USER_VISIBLE,
        messages=(OutboundText(text=ACKNOWLEDGEMENT, sequence=0),),
        facts={"fragments": str(len(task.texts))},
    )


TASK_HANDLERS: Final[dict[TaskType, TaskHandler]] = {
    TaskType.CONVERSATION: acknowledge,
}


def make_log_workout_handler(
    workout: WorkoutApplicationService, builder: WorkoutCommandBuilderPort
) -> TaskHandler:
    """The LOG_WORKOUT handler, closed over the services it needs.

    A factory rather than a module-level function because the handler owns a
    database session factory: a registry that could be imported without one
    would be a registry that acknowledges workouts it cannot write.
    """

    async def log_workout(task: TaskInput) -> DomainResult:
        try:
            structured = parse(task.texts)
        except StrictSyntaxError as error:
            # The user typed the marker and got the grammar wrong. Telling them
            # what failed beats a confirmation for a workout nobody wrote.
            return _reply(f"Não entendi: {error.problem}. Tente de novo?", facts={})

        if structured is None:  # pragma: no cover - routing sends only marked batches
            return await acknowledge(task)

        outcome = await builder.build(
            structured,
            user_id=task.user_id,
            conversation_id=task.conversation_id,
            message_batch_id=task.message_batch_id,
            source_message_ids=task.message_ids,
        )

        if outcome.command is None:
            return _reply(clarification_request(outcome.deferred), facts={})

        result = await workout.log_workout(outcome.command)
        text = (
            partial_confirmation(result, outcome.deferred)
            if outcome.deferred
            else workout_confirmation(result)
        )
        return _reply(
            text,
            facts={
                "training_session_id": str(result.training_session_id),
                "exercises": ", ".join(item.canonical_name for item in result.exercises),
                "sets": str(result.set_count),
            },
        )

    return log_workout


def _reply(text: str, *, facts: dict[str, str]) -> DomainResult:
    return DomainResult(
        task_type=TaskType.LOG_WORKOUT,
        visibility=ResultVisibility.USER_VISIBLE,
        messages=(OutboundText(text=text, sequence=0),),
        facts=facts,
    )


def build_task_handlers(
    *,
    workout: WorkoutApplicationService | None = None,
    builder: WorkoutCommandBuilderPort | None = None,
) -> dict[TaskType, TaskHandler]:
    """The registry, composed with whatever dependencies were supplied.

    LOG_WORKOUT is absent when the workout service is: a worker built without
    the domain must fail loudly on a workout rather than acknowledge one, and
    an acknowledgement for training nobody recorded is the failure hardest to
    notice.
    """
    handlers: dict[TaskType, TaskHandler] = {TaskType.CONVERSATION: acknowledge}
    if workout is not None and builder is not None:
        handlers[TaskType.LOG_WORKOUT] = make_log_workout_handler(workout, builder)
    return handlers


def resolve_handler(task_type: TaskType | str) -> TaskHandler:
    """Find the handler for a task type, or fail loudly.

    Returning a default handler for an unknown type would turn a routing bug
    into a plausible-looking reply, which is the failure mode hardest to notice
    in production.
    """
    try:
        key = TaskType(task_type)
    except ValueError as error:
        raise UnknownTaskTypeError(str(task_type)) from error

    handler = TASK_HANDLERS.get(key)
    if handler is None:
        raise UnknownTaskTypeError(str(task_type))
    return handler
