"""WS-10 against real PostgreSQL: sequence order, resumption and delivery state."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.ports.whatsapp import PermanentSendError, TransientSendError
from app.config import ApplicationSettings
from app.domain.events import DomainEventEnvelope
from app.infrastructure.postgres.engine import unit_of_work
from app.infrastructure.postgres.models import (
    Conversation,
    DeliveryState,
    MessagingProvider,
    OutboundMessage,
    User,
    UserIdentifier,
)
from app.infrastructure.whatsapp.fake_client import FakeWhatsAppClient
from app.security.identifiers import encrypt_external_id, lookup_hash
from app.workers.dispatcher import (
    WhatsAppDispatcher,
)
from app.workers.workflow_worker import RESPONSE_READY

pytestmark = [pytest.mark.integration]

BSUID = "5511987654321"


@pytest.fixture
def client() -> FakeWhatsAppClient:
    return FakeWhatsAppClient()


@pytest.fixture
def dispatcher(
    session_factory: async_sessionmaker[AsyncSession],
    client: FakeWhatsAppClient,
    migrated_database: ApplicationSettings,
) -> WhatsAppDispatcher:
    return WhatsAppDispatcher(
        session_factory=session_factory, client=client, settings=migrated_database
    )


async def _seed_group(
    session_factory: async_sessionmaker[AsyncSession],
    settings: ApplicationSettings,
    *texts: str,
) -> dict[str, Any]:
    async with unit_of_work(session_factory) as session:
        user = User()
        session.add(user)
        await session.flush()
        session.add(
            UserIdentifier(
                user_id=user.id,
                provider=MessagingProvider.WHATSAPP,
                external_id_ciphertext=encrypt_external_id(
                    BSUID, settings.security.bsuid_encryption_key
                ),
                external_id_lookup_hmac=lookup_hash(BSUID, settings.security.bsuid_lookup_hmac_key),
            )
        )
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.flush()

        response_group_id = uuid4()
        for sequence, text in enumerate(texts):
            session.add(
                OutboundMessage(
                    user_id=user.id,
                    conversation_id=conversation.id,
                    response_group_id=response_group_id,
                    sequence=sequence,
                    text=text,
                    delivery_state=DeliveryState.PENDING,
                )
            )

        return DomainEventEnvelope(
            event_type=RESPONSE_READY,
            aggregate_type="response_group",
            aggregate_id=response_group_id,
            user_id=user.id,
            payload={
                "response_group_id": str(response_group_id),
                "user_id": str(user.id),
                "conversation_id": str(conversation.id),
            },
        ).model_dump(mode="json")


async def _states(
    session_factory: async_sessionmaker[AsyncSession], response_group_id: UUID
) -> list[DeliveryState]:
    async with session_factory() as session:
        rows = (
            await session.scalars(
                sa.select(OutboundMessage)
                .where(OutboundMessage.response_group_id == response_group_id)
                .order_by(OutboundMessage.sequence)
            )
        ).all()
    return [row.delivery_state for row in rows]


async def test_a_response_group_is_delivered_in_sequence_order(
    dispatcher: WhatsAppDispatcher,
    client: FakeWhatsAppClient,
    session_factory: async_sessionmaker[AsyncSession],
    migrated_database: ApplicationSettings,
) -> None:
    """A split reply arriving out of order reads as nonsense, and it was split
    precisely because it was too long to be one message."""
    envelope = await _seed_group(
        session_factory, migrated_database, "primeira", "segunda", "terceira"
    )

    result = await dispatcher.dispatch(envelope)

    assert client.texts == ["primeira", "segunda", "terceira"]
    assert result.dispatched == 3
    assert (
        await _states(session_factory, UUID(envelope["payload"]["response_group_id"]))
        == [DeliveryState.DISPATCHED] * 3
    )


async def test_the_recipient_is_the_decrypted_identifier(
    dispatcher: WhatsAppDispatcher,
    client: FakeWhatsAppClient,
    session_factory: async_sessionmaker[AsyncSession],
    migrated_database: ApplicationSettings,
) -> None:
    """The one place where the plaintext identifier is needed (Q143)."""
    envelope = await _seed_group(session_factory, migrated_database, "oi")

    await dispatcher.dispatch(envelope)

    assert [record.recipient for record in client.sent] == [BSUID]


async def test_a_failure_on_sequence_two_does_not_dispatch_sequence_three(
    dispatcher: WhatsAppDispatcher,
    client: FakeWhatsAppClient,
    session_factory: async_sessionmaker[AsyncSession],
    migrated_database: ApplicationSettings,
) -> None:
    envelope = await _seed_group(
        session_factory, migrated_database, "primeira", "segunda", "terceira"
    )
    client.fail_once("segunda", permanent=True)

    result = await dispatcher.dispatch(envelope)

    assert client.texts == ["primeira"], "a reply missing its middle is worse than a late one"
    assert result.failed == 1
    assert await _states(session_factory, UUID(envelope["payload"]["response_group_id"])) == [
        DeliveryState.DISPATCHED,
        DeliveryState.FAILED,
        DeliveryState.PENDING,
    ]


async def test_retrying_a_group_does_not_resend_what_was_already_delivered(
    dispatcher: WhatsAppDispatcher,
    client: FakeWhatsAppClient,
    session_factory: async_sessionmaker[AsyncSession],
    migrated_database: ApplicationSettings,
) -> None:
    """Q120, and the reason redelivery of the group is safe at all."""
    envelope = await _seed_group(
        session_factory, migrated_database, "primeira", "segunda", "terceira"
    )
    client.fail_once("segunda")

    with pytest.raises(TransientSendError):
        await dispatcher.dispatch(envelope)

    assert client.texts == ["primeira"]

    result = await dispatcher.dispatch(envelope)

    assert client.texts == ["primeira", "segunda", "terceira"], (
        "the already-delivered prefix must not be sent twice"
    )
    assert result.skipped == 1
    assert result.dispatched == 2


async def test_a_transient_failure_keeps_the_message_claimable(
    dispatcher: WhatsAppDispatcher,
    client: FakeWhatsAppClient,
    session_factory: async_sessionmaker[AsyncSession],
    migrated_database: ApplicationSettings,
) -> None:
    """It returns to PENDING and its attempt count survives, so the retry tiers
    can redeliver it instead of the worker sleeping on it (§9.3)."""
    envelope = await _seed_group(session_factory, migrated_database, "unica")
    client.fail_once("unica")

    with pytest.raises(TransientSendError):
        await dispatcher.dispatch(envelope)

    async with session_factory() as session:
        message = (await session.scalars(sa.select(OutboundMessage))).one()

    assert message.delivery_state is DeliveryState.PENDING
    assert message.attempts == 1
    assert message.provider_message_id is None


async def test_a_permanent_failure_records_its_reason(
    dispatcher: WhatsAppDispatcher,
    client: FakeWhatsAppClient,
    session_factory: async_sessionmaker[AsyncSession],
    migrated_database: ApplicationSettings,
) -> None:
    envelope = await _seed_group(session_factory, migrated_database, "unica")
    client.failures["unica"] = PermanentSendError("invalid recipient")

    await dispatcher.dispatch(envelope)

    async with session_factory() as session:
        message = (await session.scalars(sa.select(OutboundMessage))).one()

    assert message.delivery_state is DeliveryState.FAILED
    assert message.failure_reason is not None
    assert "invalid recipient" in message.failure_reason


async def test_delivery_receipts_move_a_message_to_delivered(
    dispatcher: WhatsAppDispatcher,
    session_factory: async_sessionmaker[AsyncSession],
    migrated_database: ApplicationSettings,
) -> None:
    envelope = await _seed_group(session_factory, migrated_database, "unica")
    response_group_id = UUID(envelope["payload"]["response_group_id"])

    await dispatcher.dispatch(envelope)
    await dispatcher.mark_delivered(response_group_id, 0)

    assert await _states(session_factory, response_group_id) == [DeliveryState.DELIVERED]


async def test_a_delivered_message_is_never_dispatched_again(
    dispatcher: WhatsAppDispatcher,
    client: FakeWhatsAppClient,
    session_factory: async_sessionmaker[AsyncSession],
    migrated_database: ApplicationSettings,
) -> None:
    envelope = await _seed_group(session_factory, migrated_database, "unica")
    response_group_id = UUID(envelope["payload"]["response_group_id"])

    await dispatcher.dispatch(envelope)
    await dispatcher.mark_delivered(response_group_id, 0)
    result = await dispatcher.dispatch(envelope)

    assert client.texts == ["unica"]
    assert result.skipped == 1
    assert result.dispatched == 0


async def test_an_empty_response_group_is_an_error(
    dispatcher: WhatsAppDispatcher,
) -> None:
    envelope = DomainEventEnvelope(
        event_type=RESPONSE_READY,
        aggregate_type="response_group",
        aggregate_id=uuid4(),
        payload={"response_group_id": str(uuid4())},
    ).model_dump(mode="json")

    with pytest.raises(LookupError, match="no messages"):
        await dispatcher.dispatch(envelope)


async def test_the_dispatcher_role_can_read_what_it_needs_to_send(
    client: FakeWhatsAppClient,
    migrated_database: ApplicationSettings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Run with the dispatcher's own credentials, not the admin's.

    The admin-backed factory the other tests use would hide a missing grant,
    and a deployed dispatcher would then abort every group before reaching the
    provider (Q145).
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config import ServiceName
    from app.infrastructure.postgres.engine import create_session_factory

    envelope = await _seed_group(session_factory, migrated_database, "primeira", "segunda")

    engine = create_async_engine(migrated_database.postgres.dsn_for(ServiceName.DISPATCHER))
    try:
        constrained = WhatsAppDispatcher(
            session_factory=create_session_factory(engine),
            client=client,
            settings=migrated_database,
        )
        result = await constrained.dispatch(envelope)
    finally:
        await engine.dispose()

    assert result.dispatched == 2
    assert client.texts == ["primeira", "segunda"]


async def test_a_send_accepted_before_a_crash_is_not_sent_twice(
    dispatcher: WhatsAppDispatcher,
    client: FakeWhatsAppClient,
    session_factory: async_sessionmaker[AsyncSession],
    migrated_database: ApplicationSettings,
) -> None:
    """The window between "provider accepted" and "row updated" cannot be closed,
    so the idempotency key is what makes crossing it safe."""
    envelope = await _seed_group(session_factory, migrated_database, "unica")
    response_group_id = UUID(envelope["payload"]["response_group_id"])

    async with session_factory() as session:
        message = (await session.scalars(sa.select(OutboundMessage))).one()
        message_id = message.id

    # Simulate: the provider accepted it, then the process died before the
    # state could be recorded.
    await client.send_text(recipient=BSUID, text="unica", idempotency_key=str(message_id))
    async with unit_of_work(session_factory) as session:
        crashed = (await session.scalars(sa.select(OutboundMessage))).one()
        crashed.delivery_state = DeliveryState.DISPATCHING

    result = await dispatcher.dispatch(envelope)

    assert len(client.sent) == 1, "the provider must see this message exactly once"
    assert result.dispatched == 1
    assert await _states(session_factory, response_group_id) == [DeliveryState.DISPATCHED]


async def test_a_permanently_failed_message_is_not_retried_by_a_duplicate_event(
    dispatcher: WhatsAppDispatcher,
    client: FakeWhatsAppClient,
    session_factory: async_sessionmaker[AsyncSession],
    migrated_database: ApplicationSettings,
) -> None:
    """At-least-once publication means this event arrives more than once."""
    envelope = await _seed_group(session_factory, migrated_database, "unica")
    client.failures["unica"] = PermanentSendError("invalid recipient")
    client.sticky = True

    first = await dispatcher.dispatch(envelope)
    second = await dispatcher.dispatch(envelope)

    assert first.failed == 1
    assert second.failed == 1
    assert client.sent == [], "the provider is never called again for a rejected message"
