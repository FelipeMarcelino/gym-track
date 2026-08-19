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
from typing import Final
from uuid import UUID

from app.domain.results import DomainResult, OutboundText, ResultVisibility, TaskType


@dataclass(frozen=True, slots=True)
class TaskInput:
    """Everything a handler is given about one workflow input."""

    task_type: TaskType
    message_batch_id: UUID
    user_id: UUID
    conversation_id: UUID
    #: The batch's fragments, in arrival order.
    texts: tuple[str, ...]


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
