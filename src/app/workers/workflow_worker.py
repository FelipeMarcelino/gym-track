"""The workflow worker (§11, §28, Q118, Q130).

One InputBatch in, one response group out, in a single transaction:
`workflow_executions`, `outbound_messages`, `domain_events` and `outbox_events`
commit together or not at all.

The ACK happens after that commit and explicitly **not** after the provider
delivers (Q130). Waiting for delivery would hold a partition's single active
consumer for the length of a WhatsApp round trip, and would make provider
downtime look like broker backlog. Delivery is the dispatcher's problem, and
the outbound rows are already durable when the ACK is sent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.events import DomainEventEnvelope
from app.domain.results import DomainResult, ResultVisibility, TaskType
from app.graphs.main.handlers import TaskInput, resolve_handler
from app.infrastructure.postgres.engine import unit_of_work
from app.infrastructure.postgres.models import (
    DeliveryState,
    Message,
    MessageBatch,
    MessageBatchItem,
    OutboundMessage,
    WorkflowExecution,
    WorkflowExecutionStatus,
)
from app.infrastructure.postgres.outbox import record_domain_event
from app.infrastructure.rabbitmq.topology import Exchanges
from app.observability import consumed_message_scope, with_workflow_execution

logger = logging.getLogger(__name__)

RESPONSE_READY = "response.ready"
OUTBOUND_ROUTING_KEY = "outbound.dispatch"


@dataclass(frozen=True, slots=True)
class WorkflowOutcome:
    workflow_execution_id: UUID
    response_group_id: UUID | None
    outbound_messages: int
    #: False when the batch had already been processed and this delivery was a
    #: redelivery, which must not produce a second set of effects (§28).
    executed: bool


class WorkflowWorker:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        task_type: TaskType = TaskType.CONVERSATION,
    ) -> None:
        self._session_factory = session_factory
        # Sprint 3 replaces this with a planner deciding the task type per
        # batch; until then every batch is one conversation task.
        self._task_type = task_type

    async def handle(self, payload: dict[str, Any]) -> WorkflowOutcome:
        """Process one InputBatchReady. Safe to call twice with the same batch."""
        message_batch_id = UUID(str(payload["message_batch_id"]))
        headers = {
            "trace_id": payload.get("trace_id"),
            "correlation_id": payload.get("correlation_id"),
        }

        with consumed_message_scope(headers):
            async with unit_of_work(self._session_factory) as session:
                batch = await session.get(MessageBatch, message_batch_id)
                if batch is None:
                    raise LookupError(f"message batch {message_batch_id} does not exist")

                execution, resumed = await self._execution_for(session, batch)
                with_workflow_execution(str(execution.id))

                if resumed and execution.status is WorkflowExecutionStatus.SUCCEEDED:
                    # A redelivery of work that already committed. The outbound
                    # rows exist, the dispatcher owns them, and doing anything
                    # else here would double the user's reply.
                    logger.info(
                        "workflow execution already completed; skipping",
                        extra={"message_batch_id": str(message_batch_id)},
                    )
                    return WorkflowOutcome(
                        workflow_execution_id=execution.id,
                        response_group_id=None,
                        outbound_messages=0,
                        executed=False,
                    )

                execution.attempts += 1
                texts = await self._batch_texts(session, message_batch_id)

                result = await resolve_handler(self._task_type)(
                    TaskInput(
                        task_type=self._task_type,
                        message_batch_id=message_batch_id,
                        user_id=batch.user_id,
                        conversation_id=batch.conversation_id,
                        texts=texts,
                    )
                )

                response_group_id = await self._persist_result(session, batch, execution, result)

                execution.status = WorkflowExecutionStatus.SUCCEEDED
                execution.finished_at = datetime.now(UTC)

            # The commit has happened. Only now may the message be acked.
            return WorkflowOutcome(
                workflow_execution_id=execution.id,
                response_group_id=response_group_id,
                outbound_messages=len(result.messages),
                executed=True,
            )

    async def _execution_for(
        self, session: AsyncSession, batch: MessageBatch
    ) -> tuple[WorkflowExecution, bool]:
        """Create the batch's execution, or resume the one that exists (Q118).

        `UNIQUE(message_batch_id)` is what makes this safe under redelivery:
        two deliveries cannot each open an execution for one batch.
        """
        existing = await session.scalar(
            sa.select(WorkflowExecution)
            .where(WorkflowExecution.message_batch_id == batch.id)
            .with_for_update()
        )
        if existing is not None:
            return existing, True

        execution = WorkflowExecution(
            message_batch_id=batch.id,
            user_id=batch.user_id,
            conversation_id=batch.conversation_id,
            status=WorkflowExecutionStatus.RUNNING,
            trace_id=batch.trace_id,
            correlation_id=batch.correlation_id,
        )
        session.add(execution)
        await session.flush()
        return execution, False

    async def _batch_texts(self, session: AsyncSession, batch_id: UUID) -> tuple[str, ...]:
        rows = await session.scalars(
            sa.select(Message.text)
            .join(MessageBatchItem, MessageBatchItem.message_id == Message.id)
            .where(MessageBatchItem.message_batch_id == batch_id)
            .order_by(MessageBatchItem.position)
        )
        return tuple(text for text in rows.all() if text is not None)

    async def _persist_result(
        self,
        session: AsyncSession,
        batch: MessageBatch,
        execution: WorkflowExecution,
        result: DomainResult,
    ) -> UUID | None:
        await record_domain_event(
            session,
            DomainEventEnvelope(
                event_type=f"workflow.{result.task_type.value}.completed",
                aggregate_type="workflow_execution",
                aggregate_id=execution.id,
                user_id=batch.user_id,
                payload={
                    "message_batch_id": str(batch.id),
                    "visibility": result.visibility.value,
                    **result.facts,
                },
            ),
            exchange=Exchanges.DOMAIN_EVENTS,
            routing_key=f"workflow.{result.task_type.value}.completed",
        )

        if result.visibility is not ResultVisibility.USER_VISIBLE or not result.messages:
            return None

        response_group_id = uuid4()
        for message in result.messages:
            session.add(
                OutboundMessage(
                    user_id=batch.user_id,
                    conversation_id=batch.conversation_id,
                    workflow_execution_id=execution.id,
                    response_group_id=response_group_id,
                    sequence=message.sequence,
                    text=message.text,
                    delivery_state=DeliveryState.PENDING,
                    trace_id=batch.trace_id,
                    correlation_id=batch.correlation_id,
                )
            )

        # The dispatcher is woken by this event, in the same transaction as the
        # rows it will read (DEC-005).
        await record_domain_event(
            session,
            DomainEventEnvelope(
                event_type=RESPONSE_READY,
                aggregate_type="response_group",
                aggregate_id=response_group_id,
                user_id=batch.user_id,
                payload={
                    "response_group_id": str(response_group_id),
                    "user_id": str(batch.user_id),
                    "conversation_id": str(batch.conversation_id),
                    "messages": len(result.messages),
                },
            ),
            exchange=Exchanges.WHATSAPP_OUTBOUND,
            routing_key=OUTBOUND_ROUTING_KEY,
        )
        return response_group_id
