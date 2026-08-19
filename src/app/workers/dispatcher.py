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
    DeliveryState.DISPATCHING: frozenset(
        {DeliveryState.DISPATCHED, DeliveryState.FAILED, DeliveryState.PENDING}
    ),
    DeliveryState.DISPATCHED: frozenset({DeliveryState.DELIVERED}),
    DeliveryState.DELIVERED: frozenset(),
    DeliveryState.FAILED: frozenset({DeliveryState.DISPATCHING}),
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
            return await self._dispatch_group(response_group_id)

    async def _dispatch_group(self, response_group_id: UUID) -> DispatchResult:
        dispatched = 0
        skipped = 0
        failed = 0
        # Raised only after the transaction commits. Raising inside it would
        # roll back the prefix that *did* go out, and the next delivery would
        # send those messages to the user a second time -- the exact failure
        # Q120 forbids.
        transient: TransientSendError | None = None

        async with unit_of_work(self._session_factory) as session:
            messages = await self._group(session, response_group_id)
            if not messages:
                raise LookupError(f"response group {response_group_id} has no messages")

            recipient = await self._recipient(session, messages[0].user_id)

            for message in messages:
                if message.delivery_state in DISPATCH_SAFE:
                    # Q120: a retry of the group must not resend what already
                    # went out. This skip is the entire guarantee.
                    skipped += 1
                    continue

                transition(message, DeliveryState.DISPATCHING)
                message.attempts += 1

                try:
                    sent = await self._client.send_text(recipient=recipient, text=message.text)
                except PermanentSendError as error:
                    transition(message, DeliveryState.FAILED)
                    message.failure_reason = repr(error)[:2000]
                    failed += 1
                    logger.error(
                        "outbound message permanently rejected",
                        extra={
                            "response_group_id": str(response_group_id),
                            "sequence": message.sequence,
                        },
                    )
                    # The rest of the group is not sent: a reply missing its
                    # middle is worse than a reply that is late.
                    break
                except TransientSendError as error:
                    transition(message, DeliveryState.PENDING)
                    message.failure_reason = None
                    transient = error
                    logger.warning(
                        "outbound message deferred to the retry tiers",
                        extra={
                            "response_group_id": str(response_group_id),
                            "sequence": message.sequence,
                            "attempts": message.attempts,
                        },
                    )
                    break
                else:
                    transition(message, DeliveryState.DISPATCHED)
                    message.provider_message_id = sent.provider_message_id
                    message.dispatched_at = datetime.now(UTC)
                    dispatched += 1

        if transient is not None:
            raise transient

        return DispatchResult(
            response_group_id=response_group_id,
            dispatched=dispatched,
            skipped=skipped,
            failed=failed,
        )

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
