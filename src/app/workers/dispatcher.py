"""The WhatsApp dispatcher (§25, Q119, Q120).

It sends a response group **in sequence order**, and sends message N only once
N-1 has reached a dispatch-safe state. A split reply that arrives out of order
reads as nonsense, and the split exists precisely because the reply was too
long to be one message.

The retry mechanism is the queue's tiers, not a sleep loop: a transient
provider failure re-raises so the delivery is redelivered later (§9.3 forbids
sleeping while holding a consumer slot). What makes that safe is Q120 -- on
redelivery the already-delivered prefix of the group is skipped, so a failure
at sequence 2 never resends sequence 1.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.ports.whatsapp import (
    PermanentSendError,
    TransientSendError,
    WhatsAppClient,
)
from app.config import ApplicationSettings
from app.domain.events import DomainEventEnvelope
from app.infrastructure.postgres.engine import unit_of_work
from app.infrastructure.postgres.models import (
    DeliveryState,
    MessagingProvider,
    OutboundMessage,
    UserIdentifier,
)
from app.observability import consumed_message_scope
from app.security.identifiers import decrypt_external_id

logger = logging.getLogger(__name__)

#: Delivery states a message may move to from each state. Terminal states have
#: no successors: a delivered message is never re-dispatched, and that is what
#: makes redelivery of the group safe (Q120).
ALLOWED_TRANSITIONS: Final[dict[DeliveryState, frozenset[DeliveryState]]] = {
    DeliveryState.PENDING: frozenset({DeliveryState.DISPATCHING, DeliveryState.FAILED}),
    # Re-claiming a message left DISPATCHING by a crashed dispatcher is a
    # transition to itself: the provider may or may not have accepted it, and
    # the idempotency key is what makes sending again safe.
    DeliveryState.DISPATCHING: frozenset(
        {
            DeliveryState.DISPATCHING,
            DeliveryState.DISPATCHED,
            DeliveryState.FAILED,
            DeliveryState.PENDING,
        }
    ),
    DeliveryState.DISPATCHED: frozenset({DeliveryState.DELIVERED}),
    DeliveryState.DELIVERED: frozenset(),
    # Terminal. A duplicate `response.ready` -- from the at-least-once outbox
    # or a DLQ replay -- must not make the dispatcher call a provider that has
    # already rejected this message. Reviving it is an operator action.
    DeliveryState.FAILED: frozenset(),
}

#: States that mean "this message is done; the next one may go".
DISPATCH_SAFE: Final[frozenset[DeliveryState]] = frozenset(
    {DeliveryState.DISPATCHED, DeliveryState.DELIVERED}
)


class InvalidDeliveryTransitionError(RuntimeError):
    def __init__(self, current: DeliveryState, target: DeliveryState) -> None:
        super().__init__(f"cannot move a delivery from {current.value} to {target.value}")
        self.current = current
        self.target = target


def transition(message: OutboundMessage, target: DeliveryState) -> None:
    """Move a delivery forward, refusing to move it backwards."""
    if target not in ALLOWED_TRANSITIONS[message.delivery_state]:
        raise InvalidDeliveryTransitionError(message.delivery_state, target)
    message.delivery_state = target


@dataclass(frozen=True, slots=True)
class DispatchResult:
    response_group_id: UUID
    dispatched: int
    skipped: int
    failed: int


class WhatsAppDispatcher:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        client: WhatsAppClient,
        settings: ApplicationSettings,
    ) -> None:
        self._session_factory = session_factory
        self._client = client
        self._settings = settings

    async def dispatch(self, body: dict[str, Any]) -> DispatchResult:
        """Deliver one response group, resuming where a previous attempt stopped."""
        envelope = DomainEventEnvelope.from_message(body)
        response_group_id = UUID(str(envelope.payload["response_group_id"]))

        with consumed_message_scope(
            {"trace_id": envelope.trace_id, "correlation_id": envelope.correlation_id}
        ):
            result = await self._dispatch_group(response_group_id)
            logger.info(
                "response group dispatched",
                extra={
                    "response_group_id": str(response_group_id),
                    "dispatched": result.dispatched,
                    "skipped": result.skipped,
                    "failed": result.failed,
                },
            )
            return result

    async def _dispatch_group(self, response_group_id: UUID) -> DispatchResult:
        """Deliver the group one message at a time, committing between sends.

        Each message gets its own transaction on purpose. A single transaction
        around the whole group would mean that a dispatcher dying after the
        provider accepted a message -- but before the commit -- rolls that
        message back to PENDING even though the user already has it.

        The window cannot be closed entirely: a crash between "provider
        accepted" and "row updated" is always possible. What closes it is the
        idempotency key, which is stable per outbound message, so the resend
        after the crash resolves to the same provider message instead of a
        second one.
        """
        dispatched = 0
        skipped = 0
        failed = 0
        transient: TransientSendError | None = None

        recipient = await self._recipient_for(response_group_id)

        for message_id, text in await self._pending_messages(response_group_id):
            state = await self._claim(message_id)
            if state is None:
                skipped += 1
                continue
            if state is DeliveryState.FAILED:
                # A message that was permanently rejected stops the group, just
                # as it did on the attempt that rejected it.
                failed += 1
                break

            try:
                sent = await self._client.send_text(
                    recipient=recipient,
                    text=text,
                    # Stable across restarts and redeliveries: the row's own id.
                    idempotency_key=str(message_id),
                )
            except PermanentSendError as error:
                await self._record_failure(message_id, error)
                failed += 1
                logger.error(
                    "outbound message permanently rejected",
                    extra={"response_group_id": str(response_group_id)},
                )
                # The rest of the group is not sent: a reply missing its middle
                # is worse than a reply that is late.
                break
            except TransientSendError as error:
                await self._release(message_id)
                transient = error
                logger.warning(
                    "outbound message deferred to the retry tiers",
                    extra={"response_group_id": str(response_group_id)},
                )
                break
            else:
                await self._record_dispatched(message_id, sent.provider_message_id)
                dispatched += 1

        if transient is not None:
            raise transient

        return DispatchResult(
            response_group_id=response_group_id,
            dispatched=dispatched,
            skipped=skipped,
            failed=failed,
        )

    async def _pending_messages(self, response_group_id: UUID) -> list[tuple[UUID, str]]:
        """The group in sequence order, as plain values.

        Read outside the per-message transactions, so nothing is held open
        across a provider call.
        """
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    sa.select(OutboundMessage)
                    .where(OutboundMessage.response_group_id == response_group_id)
                    .order_by(OutboundMessage.sequence)
                )
            ).all()

        if not rows:
            raise LookupError(f"response group {response_group_id} has no messages")
        return [(row.id, row.text) for row in rows]

    async def _claim(self, message_id: UUID) -> DeliveryState | None:
        """Move a message into DISPATCHING, or report why it was not sent.

        Returns None when the message is already dispatch-safe (Q120: the
        skip that keeps a retry from resending what went out), FAILED when it
        is terminally rejected, and DISPATCHING once claimed.
        """
        async with unit_of_work(self._session_factory) as session:
            message = await self._locked(session, message_id)
            if message.delivery_state in DISPATCH_SAFE:
                return None
            if message.delivery_state is DeliveryState.FAILED:
                return DeliveryState.FAILED

            transition(message, DeliveryState.DISPATCHING)
            message.attempts += 1
            return DeliveryState.DISPATCHING

    async def _record_dispatched(self, message_id: UUID, provider_message_id: str) -> None:
        async with unit_of_work(self._session_factory) as session:
            message = await self._locked(session, message_id)
            transition(message, DeliveryState.DISPATCHED)
            message.provider_message_id = provider_message_id
            message.dispatched_at = datetime.now(UTC)
            message.failure_reason = None

    async def _record_failure(self, message_id: UUID, error: PermanentSendError) -> None:
        async with unit_of_work(self._session_factory) as session:
            message = await self._locked(session, message_id)
            transition(message, DeliveryState.FAILED)
            message.failure_reason = repr(error)[:2000]

    async def _release(self, message_id: UUID) -> None:
        async with unit_of_work(self._session_factory) as session:
            message = await self._locked(session, message_id)
            transition(message, DeliveryState.PENDING)
            message.failure_reason = None

    async def _locked(self, session: AsyncSession, message_id: UUID) -> OutboundMessage:
        message = await session.scalar(
            sa.select(OutboundMessage).where(OutboundMessage.id == message_id).with_for_update()
        )
        if message is None:  # pragma: no cover - the row was read moments ago
            raise LookupError(f"outbound message {message_id} vanished")
        return message

    async def _recipient_for(self, response_group_id: UUID) -> str:
        async with self._session_factory() as session:
            user_id = await session.scalar(
                sa.select(OutboundMessage.user_id).where(
                    OutboundMessage.response_group_id == response_group_id
                )
            )
            if user_id is None:
                raise LookupError(f"response group {response_group_id} has no messages")
            return await self._recipient(session, user_id)

    async def mark_delivered(self, response_group_id: UUID, sequence: int) -> None:
        """Record a provider delivery receipt (§25)."""
        async with unit_of_work(self._session_factory) as session:
            message = await session.scalar(
                sa.select(OutboundMessage).where(
                    OutboundMessage.response_group_id == response_group_id,
                    OutboundMessage.sequence == sequence,
                )
            )
            if message is None:
                raise LookupError(f"no message {sequence} in group {response_group_id}")
            transition(message, DeliveryState.DELIVERED)
            message.delivered_at = datetime.now(UTC)

    async def _group(self, session: AsyncSession, response_group_id: UUID) -> list[OutboundMessage]:
        rows = await session.scalars(
            sa.select(OutboundMessage)
            .where(OutboundMessage.response_group_id == response_group_id)
            .order_by(OutboundMessage.sequence)
            .with_for_update()
        )
        return list(rows.all())

    async def _recipient(self, session: AsyncSession, user_id: UUID) -> str:
        """Decrypt the external identifier -- the one place it must be plaintext.

        Everything else resolves identity through the HMAC lookup (Q143); the
        provider needs the real value, and only here.
        """
        identifier = await session.scalar(
            sa.select(UserIdentifier).where(
                UserIdentifier.user_id == user_id,
                UserIdentifier.provider == MessagingProvider.WHATSAPP,
            )
        )
        if identifier is None:
            raise LookupError(f"user {user_id} has no WhatsApp identifier")
        return decrypt_external_id(
            identifier.external_id_ciphertext,
            self._settings.security.bsuid_encryption_key,
        )
