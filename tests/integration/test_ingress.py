"""WS-7 end to end at the edge: signature, dedupe, identity, conversation, health."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.app import create_app
from app.api.dependencies import ApiContext
from app.config import ApplicationSettings
from app.infrastructure.postgres.engine import unit_of_work
from app.infrastructure.postgres.models import (
    Conversation,
    ConversationStatus,
    DomainEvent,
    Message,
    OutboxEvent,
    User,
    UserIdentifier,
)
from app.security.identifiers import decrypt_external_id, lookup_hash
from app.security.signatures import SIGNATURE_HEADER, sign

pytestmark = [pytest.mark.integration]

BSUID = "5511987654321"


def webhook_body(external_message_id: str, *, sender: str = BSUID, text: str = "oi") -> bytes:
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": sender,
                                    "id": external_message_id,
                                    "timestamp": "1755518400",
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    return json.dumps(payload).encode()


@pytest.fixture
def context(
    migrated_database: ApplicationSettings,
    admin_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> ApiContext:
    return ApiContext(
        settings=migrated_database,
        engine=admin_engine,
        session_factory=session_factory,
    )


@pytest.fixture
async def client(context: ApiContext) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(context)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ingress") as client:
        yield client


@pytest.fixture
def post(client: httpx.AsyncClient, context: ApiContext) -> Callable[[bytes], Any]:
    async def _post(body: bytes, *, signature: str | None = None) -> httpx.Response:
        # `None` means "sign it properly"; an empty string is a caller
        # deliberately sending a blank header.
        headers = {
            SIGNATURE_HEADER: (
                sign(body, context.settings.security.whatsapp_app_secret)
                if signature is None
                else signature
            ),
            "content-type": "application/json",
        }
        return await client.post("/webhooks/whatsapp", content=body, headers=headers)

    return _post


async def _count(session_factory: async_sessionmaker[AsyncSession], model: Any) -> int:
    async with session_factory() as session:
        return int(await session.scalar(sa.select(sa.func.count()).select_from(model)) or 0)


async def test_an_invalid_signature_is_rejected_before_anything_is_persisted(
    post: Any, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A check that runs after persistence protects nothing."""
    response = await post(webhook_body("wamid.1"), signature="sha256=deadbeef")

    assert response.status_code == 401
    assert await _count(session_factory, Message) == 0
    assert await _count(session_factory, User) == 0


async def test_a_missing_signature_header_is_rejected(post: Any) -> None:
    response = await post(webhook_body("wamid.1"), signature="")

    assert response.status_code == 401


async def test_a_valid_webhook_persists_the_message_and_an_outbox_event(
    post: Any, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    response = await post(webhook_body("wamid.1", text="fiz supino"))

    assert response.status_code == 202
    assert response.json() == {"accepted": 1, "duplicates": 0}

    async with session_factory() as session:
        message = (await session.scalars(sa.select(Message))).one()
        event = (await session.scalars(sa.select(DomainEvent))).one()
        outbox = (await session.scalars(sa.select(OutboxEvent))).one()

    assert message.text == "fiz supino"
    assert message.external_message_id == "wamid.1"
    assert event.event_type == "message.received"
    assert outbox.routing_key == "message.received"
    assert outbox.domain_event_id == event.id


async def test_the_same_external_message_id_yields_exactly_one_row(
    post: Any, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The provider redelivers; §28's unique key is what makes that harmless."""
    first = await post(webhook_body("wamid.duplicate"))
    second = await post(webhook_body("wamid.duplicate"))

    assert first.json() == {"accepted": 1, "duplicates": 0}
    assert second.json() == {"accepted": 0, "duplicates": 1}
    assert await _count(session_factory, Message) == 1
    assert await _count(session_factory, DomainEvent) == 1, "no second event either"


async def test_first_contact_creates_an_identity_and_the_second_reuses_it(
    post: Any, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await post(webhook_body("wamid.1"))
    await post(webhook_body("wamid.2"))

    assert await _count(session_factory, User) == 1
    assert await _count(session_factory, UserIdentifier) == 1
    assert await _count(session_factory, Message) == 2


async def test_a_different_sender_is_a_different_user(
    post: Any, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await post(webhook_body("wamid.1", sender=BSUID))
    await post(webhook_body("wamid.2", sender="5511900000000"))

    assert await _count(session_factory, User) == 2


async def test_identity_is_found_by_hmac_without_decrypting(
    post: Any,
    session_factory: async_sessionmaker[AsyncSession],
    context: ApiContext,
) -> None:
    """Q143: lookup is an index seek on a keyed hash; the ciphertext is only
    there for the rare case where the plaintext must leave the system."""
    await post(webhook_body("wamid.1"))

    expected = lookup_hash(BSUID, context.settings.security.bsuid_lookup_hmac_key)

    async with session_factory() as session:
        identifier = (
            await session.scalars(
                sa.select(UserIdentifier).where(UserIdentifier.external_id_lookup_hmac == expected)
            )
        ).one()

    assert identifier.external_id_ciphertext != BSUID.encode()
    assert (
        decrypt_external_id(
            identifier.external_id_ciphertext,
            context.settings.security.bsuid_encryption_key,
        )
        == BSUID
    )


async def test_messages_share_one_conversation_while_it_stays_active(
    post: Any, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await post(webhook_body("wamid.1"))
    await post(webhook_body("wamid.2"))

    assert await _count(session_factory, Conversation) == 1


async def test_a_conversation_rotates_only_past_the_inactivity_threshold(
    post: Any,
    session_factory: async_sessionmaker[AsyncSession],
    context: ApiContext,
) -> None:
    """Rotation is by inactivity, not by age: a long conversation that never
    goes quiet is still one conversation (§7.2, Q30)."""
    await post(webhook_body("wamid.1"))

    timeout = context.settings.workflow.conversation_timeout
    async with unit_of_work(session_factory) as session:
        conversation = (await session.scalars(sa.select(Conversation))).one()
        conversation.last_activity_at = datetime.now(UTC) - timeout - timedelta(minutes=1)
        first_id = conversation.id

    await post(webhook_body("wamid.2"))

    async with session_factory() as session:
        conversations = {
            conversation.id: conversation
            for conversation in (await session.scalars(sa.select(Conversation))).all()
        }

    assert len(conversations) == 2
    assert conversations[first_id].status is ConversationStatus.CLOSED
    assert conversations[first_id].closed_at is not None


async def test_the_request_trace_is_stored_on_the_message(
    post: Any, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """WS-4 item 16: the aggregator links the interaction trace back to these."""
    response = await post(webhook_body("wamid.1"))

    async with session_factory() as session:
        message = (await session.scalars(sa.select(Message))).one()

    assert message.trace_id == response.headers["x-trace-id"]


async def test_three_fragments_produce_three_distinct_request_traces(
    post: Any, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    responses = [await post(webhook_body(f"wamid.{index}")) for index in range(3)]
    traces = {response.headers["x-trace-id"] for response in responses}

    assert len(traces) == 3

    async with session_factory() as session:
        stored = {message.trace_id for message in (await session.scalars(sa.select(Message))).all()}

    assert stored == traces


async def test_a_status_callback_is_acknowledged_without_persisting_anything(
    post: Any, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    body = json.dumps(
        {"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.x"}]}}]}]}
    ).encode()

    response = await post(body)

    assert response.status_code == 202
    assert response.json() == {"accepted": 0, "duplicates": 0}
    assert await _count(session_factory, Message) == 0


async def test_health_is_alive_without_touching_the_database(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ready_reports_the_database(client: httpx.AsyncClient) -> None:
    response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"postgres": True}}


async def test_ready_fails_while_health_still_succeeds_when_postgres_is_gone(
    migrated_database: ApplicationSettings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The distinction that keeps an outage from becoming a restart loop."""
    from sqlalchemy.ext.asyncio import create_async_engine

    unreachable = create_async_engine(
        migrated_database.postgres.admin_dsn().replace(
            f":{migrated_database.postgres.port}/", ":1/"
        )
    )
    app = create_app(
        ApiContext(
            settings=migrated_database,
            engine=unreachable,
            session_factory=session_factory,
        )
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://ingress") as client:
        health = await client.get("/health")
        ready = await client.get("/ready")

    await unreachable.dispose()

    assert health.status_code == 200
    assert ready.status_code == 503
    assert ready.json()["checks"] == {"postgres": False}


async def test_an_audio_message_stores_its_media_reference(
    post: Any, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    body = json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": BSUID,
                                        "id": "wamid.audio",
                                        "timestamp": "1755518400",
                                        "type": "audio",
                                        "audio": {"id": "media-123", "voice": True},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    ).encode()

    await post(body)

    async with session_factory() as session:
        message = (await session.scalars(sa.select(Message))).one()

    assert message.provider_media_id == "media-123"


async def test_a_redelivery_does_not_touch_conversation_state(
    post: Any,
    session_factory: async_sessionmaker[AsyncSession],
    context: ApiContext,
) -> None:
    """A retry arriving after the timeout used to close the live conversation
    and open an empty one — committed, because the ingest path exits its unit
    of work successfully once it recognises the duplicate."""
    await post(webhook_body("wamid.1"))

    timeout = context.settings.workflow.conversation_timeout
    async with unit_of_work(session_factory) as session:
        conversation = (await session.scalars(sa.select(Conversation))).one()
        conversation.last_activity_at = datetime.now(UTC) - timeout - timedelta(minutes=1)
        stale_activity = conversation.last_activity_at
        conversation_id = conversation.id

    response = await post(webhook_body("wamid.1"))

    assert response.json() == {"accepted": 0, "duplicates": 1}

    async with session_factory() as session:
        conversations = (await session.scalars(sa.select(Conversation))).all()

    assert len(conversations) == 1, "a redelivery must not rotate the conversation"
    assert conversations[0].id == conversation_id
    assert conversations[0].status is ConversationStatus.ACTIVE
    assert conversations[0].last_activity_at == stale_activity


async def test_concurrent_first_contact_creates_exactly_one_user(
    post: Any, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Both requests can miss the identity lookup; the loser must find the
    winner's row rather than return a 500."""
    import asyncio

    responses = await asyncio.gather(
        post(webhook_body("wamid.a")),
        post(webhook_body("wamid.b")),
        post(webhook_body("wamid.c")),
    )

    assert [response.status_code for response in responses] == [202, 202, 202]
    assert await _count(session_factory, User) == 1
    assert await _count(session_factory, UserIdentifier) == 1
    assert await _count(session_factory, Message) == 3


async def test_concurrent_deliveries_after_a_timeout_do_not_split_the_conversation(
    post: Any,
    session_factory: async_sessionmaker[AsyncSession],
    context: ApiContext,
) -> None:
    """Two rotations would put one user's fragments into two threads (§7.2)."""
    import asyncio

    await post(webhook_body("wamid.seed"))

    timeout = context.settings.workflow.conversation_timeout
    async with unit_of_work(session_factory) as session:
        conversation = (await session.scalars(sa.select(Conversation))).one()
        conversation.last_activity_at = datetime.now(UTC) - timeout - timedelta(minutes=1)

    await asyncio.gather(
        post(webhook_body("wamid.x")),
        post(webhook_body("wamid.y")),
    )

    async with session_factory() as session:
        active = (
            await session.scalars(
                sa.select(Conversation).where(Conversation.status == ConversationStatus.ACTIVE)
            )
        ).all()
        total = (await session.scalars(sa.select(Conversation))).all()

    assert len(active) == 1, "only one conversation may be active for a user"
    assert len(total) == 2, "the timed-out one was closed, not duplicated"
