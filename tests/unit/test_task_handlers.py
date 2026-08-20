"""WS-9: the handler registry and the result contract (§11.2, §11.3)."""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

import pytest

from app.domain.results import DomainResult, OutboundText, ResultVisibility, TaskType
from app.graphs.main.handlers import (
    ACKNOWLEDGEMENT,
    TaskInput,
    UnknownTaskTypeError,
    build_task_handlers,
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


@pytest.mark.parametrize("task_type", ["recommendation", "training_analysis"])
def test_a_declared_but_unimplemented_task_type_raises(task_type: str) -> None:
    """§11.3 fixes these names so a later handler slots into an existing one."""
    with pytest.raises(UnknownTaskTypeError):
        resolve_handler(task_type)


def test_a_registry_without_a_workout_service_does_not_offer_log_workout() -> None:
    """A worker composed without the domain must fail loudly on a workout
    rather than acknowledge one: an acknowledgement for training nobody
    recorded is the failure hardest to notice."""
    registry = build_task_handlers()

    assert TaskType.LOG_WORKOUT not in registry
    assert TaskType.CONVERSATION in registry


def test_the_composed_registry_offers_the_workout_handler() -> None:
    registry = build_task_handlers(workout=cast(Any, object()), builder=cast(Any, object()))

    assert TaskType.LOG_WORKOUT in registry


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


async def test_the_log_workout_handler_confirms_what_was_written() -> None:
    """§25: the reply names the exercise and the count, so the user can catch a
    mistake we could not."""
    from app.domain.training.workout_log import LoggedExercise, WorkoutLoggedResult
    from app.graphs.main.handlers import make_log_workout_handler

    written = WorkoutLoggedResult(
        training_session_id=uuid4(),
        session_opened=True,
        exercises=(
            LoggedExercise(
                session_exercise_id=uuid4(),
                canonical_name="Supino reto",
                block_index=0,
                set_count=3,
            ),
        ),
    )

    class _Workout:
        async def log_workout(self, command: object) -> WorkoutLoggedResult:
            return written

    class _Builder:
        async def build(self, structured: object, **kwargs: object) -> object:
            from app.application.commands.workout import (
                ActivityCommand,
                BuildOutcome,
                LogWorkoutCommand,
                SetCommand,
                operation_id_for,
            )
            from app.domain.training.activities import ActivityType, SetType
            from app.domain.training.provenance import Provenance

            batch_id = cast(Any, kwargs["message_batch_id"])
            return BuildOutcome(
                command=LogWorkoutCommand(
                    operation_id=operation_id_for(batch_id),
                    user_id=cast(Any, kwargs["user_id"]),
                    conversation_id=cast(Any, kwargs["conversation_id"]),
                    message_batch_id=batch_id,
                    source_message_ids=(),
                    activities=(
                        ActivityCommand(
                            exercise_id=uuid4(),
                            canonical_name="Supino reto",
                            activity_type=ActivityType.STRENGTH,
                            effort=None,
                            sets=(
                                SetCommand(
                                    set_index=0,
                                    set_type=SetType.WORKING,
                                    repetitions=10,
                                    repetitions_provenance=Provenance.EXPLICIT,
                                    load_kg=None,
                                    load_mode=None,
                                    load_provenance=Provenance.EXPLICIT,
                                    raw_load_text=None,
                                    distance_m=None,
                                    distance_provenance=Provenance.EXPLICIT,
                                    duration_s=None,
                                    duration_provenance=Provenance.EXPLICIT,
                                    effort=None,
                                ),
                            ),
                        ),
                    ),
                ),
                deferred=(),
            )

    handler = make_log_workout_handler(cast(Any, _Workout()), cast(Any, _Builder()))
    result = await handler(
        TaskInput(
            task_type=TaskType.LOG_WORKOUT,
            message_batch_id=uuid4(),
            user_id=uuid4(),
            conversation_id=uuid4(),
            texts=("#log supino 80kg 10 9 8",),
        )
    )

    assert result.task_type is TaskType.LOG_WORKOUT
    assert result.visibility is ResultVisibility.USER_VISIBLE
    assert "Supino reto" in result.messages[0].text
    assert result.facts["sets"] == "3"


async def test_an_unparseable_marked_line_is_answered_rather_than_acknowledged() -> None:
    """The user typed the marker. Telling them nothing was understood beats a
    confirmation for a workout that was never written."""
    from app.graphs.main.handlers import make_log_workout_handler

    class _Unused:
        pass

    handler = make_log_workout_handler(cast(Any, _Unused()), cast(Any, _Unused()))
    result = await handler(
        TaskInput(
            task_type=TaskType.LOG_WORKOUT,
            message_batch_id=uuid4(),
            user_id=uuid4(),
            conversation_id=uuid4(),
            texts=("#log supino 3x10",),
        )
    )

    assert result.visibility is ResultVisibility.USER_VISIBLE
    assert result.messages
    assert "3x10" in result.messages[0].text
