"""WS-9 against real PostgreSQL: idempotent execution and atomic effects."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.events import DomainEventEnvelope
from app.domain.results import TaskType
from app.graphs.main.handlers import ACKNOWLEDGEMENT
from app.infrastructure.postgres.engine import unit_of_work
from app.infrastructure.postgres.models import (
    Conversation,
    DeliveryState,
    DomainEvent,
    Message,
    MessageBatch,
    MessageBatchItem,
    MessageBatchStatus,
    MessageContentType,
    MessageDirection,
    MessagingProvider,
    OutboundMessage,
    OutboxEvent,
    User,
    WorkflowExecution,
    WorkflowExecutionStatus,
)
from app.workers.message_aggregator import INPUT_BATCH_READY
from app.workers.workflow_worker import RESPONSE_READY, WorkflowWorker

pytestmark = [pytest.mark.integration]


@pytest.fixture
def worker(session_factory: async_sessionmaker[AsyncSession]) -> WorkflowWorker:
    return WorkflowWorker(session_factory=session_factory)


async def _seed_batch(
    session_factory: async_sessionmaker[AsyncSession], *texts: str
) -> dict[str, Any]:
    async with unit_of_work(session_factory) as session:
        user = User()
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.flush()

        batch = MessageBatch(
            user_id=user.id,
            conversation_id=conversation.id,
            status=MessageBatchStatus.FLUSHED,
            generation=len(texts),
            trace_id=uuid4().hex,
            correlation_id=uuid4().hex,
        )
        session.add(batch)
        await session.flush()

        for position, text in enumerate(texts or ("oi",)):
            message = Message(
                user_id=user.id,
                conversation_id=conversation.id,
                provider=MessagingProvider.WHATSAPP,
                external_message_id=f"wamid.{uuid4()}",
                direction=MessageDirection.INBOUND,
                content_type=MessageContentType.TEXT,
                text=text,
                received_at=datetime.now(UTC),
            )
            session.add(message)
            await session.flush()
            session.add(
                MessageBatchItem(
                    message_batch_id=batch.id, message_id=message.id, position=position
                )
            )

        # Exactly what the aggregator's outbox row carries.
        return DomainEventEnvelope(
            event_type=INPUT_BATCH_READY,
            aggregate_type="message_batch",
            aggregate_id=batch.id,
            user_id=user.id,
            trace_id=batch.trace_id,
            correlation_id=batch.correlation_id,
            payload={
                "message_batch_id": str(batch.id),
                "user_id": str(user.id),
                "conversation_id": str(conversation.id),
            },
        ).model_dump(mode="json")


def _envelope_for_missing_batch() -> dict[str, Any]:
    return DomainEventEnvelope(
        event_type=INPUT_BATCH_READY,
        aggregate_type="message_batch",
        aggregate_id=uuid4(),
        payload={"message_batch_id": str(uuid4())},
    ).model_dump(mode="json")


async def _count(session_factory: async_sessionmaker[AsyncSession], model: Any) -> int:
    async with session_factory() as session:
        return int(await session.scalar(sa.select(sa.func.count()).select_from(model)) or 0)


async def test_a_batch_produces_one_execution_and_one_response_group(
    worker: WorkflowWorker, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    payload = await _seed_batch(session_factory, "fiz supino", "3x10")

    outcome = await worker.handle(payload)

    assert outcome.executed
    assert outcome.outbound_messages == 1

    async with session_factory() as session:
        execution = (await session.scalars(sa.select(WorkflowExecution))).one()
        outbound = (await session.scalars(sa.select(OutboundMessage))).one()

    assert execution.status is WorkflowExecutionStatus.SUCCEEDED
    assert execution.message_batch_id == UUID(payload["payload"]["message_batch_id"])
    assert outbound.text == ACKNOWLEDGEMENT
    assert outbound.sequence == 0
    assert outbound.delivery_state is DeliveryState.PENDING
    assert outbound.response_group_id == outcome.response_group_id


async def test_redelivery_resumes_the_execution_and_creates_no_second_one(
    worker: WorkflowWorker, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """§28: redelivery is normal, and must produce no second business effect."""
    payload = await _seed_batch(session_factory, "oi")

    first = await worker.handle(payload)
    second = await worker.handle(payload)

    assert first.executed
    assert not second.executed
    assert second.workflow_execution_id == first.workflow_execution_id

    assert await _count(session_factory, WorkflowExecution) == 1
    assert await _count(session_factory, OutboundMessage) == 1, "the user is not answered twice"


async def test_domain_rows_outbound_and_outbox_commit_together(
    worker: WorkflowWorker, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    payload = await _seed_batch(session_factory, "oi")

    await worker.handle(payload)

    async with session_factory() as session:
        events = (await session.scalars(sa.select(DomainEvent))).all()
        outbox = (await session.scalars(sa.select(OutboxEvent))).all()

    kinds = {event.event_type for event in events}
    assert "workflow.conversation.completed" in kinds
    assert RESPONSE_READY in kinds
    assert len(outbox) == len(events), "every domain event has its outbox row"


async def test_nothing_is_written_when_the_handler_fails(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Atomicity in the direction that matters: a failed handler must not leave
    an execution row claiming the batch was processed."""
    payload = await _seed_batch(session_factory, "oi")

    class HandlerFailureError(RuntimeError):
        pass

    async def exploding_handler(task: Any) -> Any:
        raise HandlerFailureError

    # Injected rather than patched: the worker takes its registry now, so the
    # seam a caller would use is the seam the test uses.
    worker = WorkflowWorker(
        session_factory=session_factory,
        handlers={TaskType.CONVERSATION: exploding_handler},
    )

    with pytest.raises(HandlerFailureError):
        await worker.handle(payload)

    assert await _count(session_factory, WorkflowExecution) == 0
    assert await _count(session_factory, OutboundMessage) == 0
    assert await _count(session_factory, DomainEvent) == 0
    assert await _count(session_factory, OutboxEvent) == 0


async def test_the_ack_happens_strictly_after_the_commit(
    worker: WorkflowWorker, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Q130: ACK after persistence, explicitly not after provider delivery.

    Asserted by ordering: at the moment the worker returns — which is the only
    point at which a consumer may ack — the rows are already visible to another
    session.
    """
    payload = await _seed_batch(session_factory, "oi")
    acked_at: list[str] = []

    outcome = await worker.handle(payload)
    async with session_factory() as session:
        visible = await session.scalar(sa.select(sa.func.count()).select_from(OutboundMessage))
    acked_at.append("ack")

    assert visible == 1, "the outbound rows were durable before the ack"
    assert acked_at == ["ack"]
    assert outcome.outbound_messages == 1


async def test_the_execution_inherits_the_interaction_trace(
    worker: WorkflowWorker, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Q131: the batch's trace reaches the workflow and the outbound rows."""
    payload = await _seed_batch(session_factory, "oi")

    await worker.handle(payload)

    async with session_factory() as session:
        execution = (await session.scalars(sa.select(WorkflowExecution))).one()
        outbound = (await session.scalars(sa.select(OutboundMessage))).one()

    assert execution.trace_id == payload["trace_id"]
    assert outbound.trace_id == payload["trace_id"]
    assert payload["trace_id"] is not None


async def test_a_missing_batch_is_an_error_rather_than_a_silent_skip(
    worker: WorkflowWorker,
) -> None:
    with pytest.raises(LookupError, match="does not exist"):
        await worker.handle(_envelope_for_missing_batch())


async def test_the_batch_fragments_reach_the_handler_in_order(
    worker: WorkflowWorker, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    payload = await _seed_batch(session_factory, "primeiro", "segundo", "terceiro")

    await worker.handle(payload)

    async with session_factory() as session:
        event = (
            await session.scalars(
                sa.select(DomainEvent).where(
                    DomainEvent.event_type == "workflow.conversation.completed"
                )
            )
        ).one()

    assert event.payload["fragments"] == "3"


async def test_a_logged_workout_travels_the_whole_worker_path(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """WS-9 end to end: a `#log` batch is routed to the workout handler, the
    domain commits, and the user is told what was recorded.

    Everything below this line has been tested in isolation; this asserts that
    the pieces were wired to each other rather than each to a fake.
    """
    from app.application.services.training_sessions import TrainingSessionManager
    from app.application.services.workout_command_builder import SessionScopedCommandBuilder
    from app.application.services.workout_logging import WorkoutApplicationService
    from app.graphs.main.handlers import build_task_handlers
    from app.infrastructure.postgres.models import ExerciseSet, SessionExercise, TrainingSession

    payload = await _seed_batch(session_factory, "#log supino 80kg 10 9 8")
    worker = WorkflowWorker(
        session_factory=session_factory,
        handlers=build_task_handlers(
            workout=WorkoutApplicationService(
                session_factory=session_factory,
                sessions=TrainingSessionManager(timeout=timedelta(hours=3)),
            ),
            builder=SessionScopedCommandBuilder(session_factory),
        ),
    )

    outcome = await worker.handle(payload)

    assert outcome.executed is True
    assert outcome.outbound_messages == 1
    assert await _count(session_factory, TrainingSession) == 1
    assert await _count(session_factory, SessionExercise) == 1
    assert await _count(session_factory, ExerciseSet) == 3

    async with session_factory() as session:
        reply = (await session.scalars(sa.select(OutboundMessage.text))).one()

    assert "Supino reto" in reply
    assert ACKNOWLEDGEMENT not in reply


async def test_an_ordinary_message_still_gets_the_acknowledgement(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Routing must not swallow everything else: a greeting is not a workout,
    and the Sprint 1 path has to keep working unchanged."""
    from app.application.services.training_sessions import TrainingSessionManager
    from app.application.services.workout_command_builder import SessionScopedCommandBuilder
    from app.application.services.workout_logging import WorkoutApplicationService
    from app.graphs.main.handlers import build_task_handlers
    from app.infrastructure.postgres.models import TrainingSession

    payload = await _seed_batch(session_factory, "bom dia")
    worker = WorkflowWorker(
        session_factory=session_factory,
        handlers=build_task_handlers(
            workout=WorkoutApplicationService(
                session_factory=session_factory,
                sessions=TrainingSessionManager(timeout=timedelta(hours=3)),
            ),
            builder=SessionScopedCommandBuilder(session_factory),
        ),
    )

    await worker.handle(payload)

    async with session_factory() as session:
        reply = (await session.scalars(sa.select(OutboundMessage.text))).one()

    assert reply == ACKNOWLEDGEMENT
    assert await _count(session_factory, TrainingSession) == 0
