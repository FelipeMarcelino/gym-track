"""WS-9: the handler registry and the result contract (§11.2, §11.3)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.domain.results import DomainResult, OutboundText, ResultVisibility, TaskType
from app.graphs.main.handlers import (
    ACKNOWLEDGEMENT,
    TaskInput,
    UnknownTaskTypeError,
    resolve_handler,
)


def task(*texts: str) -> TaskInput:
    return TaskInput(
        task_type=TaskType.CONVERSATION,
        message_batch_id=uuid4(),
        user_id=uuid4(),
        conversation_id=uuid4(),
        texts=texts,
    )


async def test_the_registry_resolves_a_known_task_type() -> None:
    handler = resolve_handler(TaskType.CONVERSATION)

    result = await handler(task("fiz supino"))

    assert result.visibility is ResultVisibility.USER_VISIBLE
    assert [message.text for message in result.messages] == [ACKNOWLEDGEMENT]


async def test_the_stub_stays_trivial() -> None:
    """It must say the same thing regardless of input. Any behaviour that
    depends on the message is domain logic that belongs in Sprint 2."""
    first = await resolve_handler(TaskType.CONVERSATION)(task("fiz supino 3x10"))
    second = await resolve_handler(TaskType.CONVERSATION)(task("bom dia", "tudo bem?"))

    assert [m.text for m in first.messages] == [m.text for m in second.messages]
    assert second.facts["fragments"] == "2"


@pytest.mark.parametrize("task_type", ["log_workout", "recommendation", "training_analysis"])
def test_a_declared_but_unimplemented_task_type_raises(task_type: str) -> None:
    """§11.3 fixes these names so a later handler slots into an existing one."""
    with pytest.raises(UnknownTaskTypeError):
        resolve_handler(task_type)


def test_an_unknown_task_type_raises_rather_than_falling_back() -> None:
    """A default handler would turn a routing bug into a plausible-looking
    reply, which is the failure mode hardest to notice in production."""
    with pytest.raises(UnknownTaskTypeError, match=r"não-existe|no handler"):
        resolve_handler("não-existe")


def test_an_internal_result_may_not_carry_messages() -> None:
    with pytest.raises(ValueError, match="nothing to say"):
        DomainResult(
            task_type=TaskType.CONVERSATION,
            visibility=ResultVisibility.INTERNAL,
            messages=(OutboundText(text="oi", sequence=0),),
        )


@pytest.mark.parametrize(
    "sequences",
    [(1, 0), (0, 0), (2, 1, 3)],
)
def test_response_sequences_must_increase(sequences: tuple[int, ...]) -> None:
    """The dispatcher delivers strictly in sequence order (§25); an ambiguous
    order here becomes an out-of-order reply there."""
    with pytest.raises(ValueError, match="increasing sequence"):
        DomainResult(
            task_type=TaskType.CONVERSATION,
            visibility=ResultVisibility.USER_VISIBLE,
            messages=tuple(OutboundText(text=f"m{index}", sequence=index) for index in sequences),
        )


def test_a_valid_multi_message_result_is_accepted() -> None:
    result = DomainResult(
        task_type=TaskType.CONVERSATION,
        visibility=ResultVisibility.USER_VISIBLE,
        messages=(
            OutboundText(text="primeira", sequence=0),
            OutboundText(text="segunda", sequence=1),
        ),
    )

    assert [message.sequence for message in result.messages] == [0, 1]
