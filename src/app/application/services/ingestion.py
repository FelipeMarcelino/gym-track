"""Turning a provider webhook into durable rows (§7, §8, §35.1, Q112).

The order here is the contract: verify, resolve identity, deduplicate, persist,
then enqueue. §8 is explicit that raw inbound data is persisted **before** the
system relies on Redis state, so a debounce window lost to a Redis restart can
always be rebuilt from `messages`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import ApplicationSettings
from app.infrastructure.postgres.models import (
    Conversation,
    ConversationStatus,
    Message,
    MessageDirection,
    MessagingProvider,
    User,
    UserIdentifier,
)
from app.infrastructure.whatsapp.payloads import InboundMessage
from app.security.identifiers import encrypt_external_id, lookup_hash


@dataclass(frozen=True, slots=True)
class IngestedMessage:
    message_id: UUID
    user_id: UUID
    conversation_id: UUID
    #: False when the provider redelivered a message already stored (§28).
    is_new: bool


async def resolve_user(
    session: AsyncSession,
    *,
    provider: MessagingProvider,
    external_id: str,
    settings: ApplicationSettings,
) -> User:
    """Find the user behind an external identifier, or create both (§7.1).

    The lookup is by HMAC, never by decrypting stored ciphertext: an index seek
    on a keyed hash keeps identity resolution cheap and keeps the plaintext
    identifier out of every query path that only needs to know *who* this is.
    """
    lookup = lookup_hash(external_id, settings.security.bsuid_lookup_hmac_key)

    existing = await session.scalar(
        sa.select(User)
        .join(UserIdentifier, UserIdentifier.user_id == User.id)
        .where(
            UserIdentifier.provider == provider,
            UserIdentifier.external_id_lookup_hmac == lookup,
        )
    )
    if existing is not None:
        return existing

    user = User()
    session.add(user)
    await session.flush()
    session.add(
        UserIdentifier(
            user_id=user.id,
            provider=provider,
            external_id_ciphertext=encrypt_external_id(
                external_id, settings.security.bsuid_encryption_key
            ),
            external_id_lookup_hmac=lookup,
        )
    )
    await session.flush()
    return user


async def resolve_conversation(
    session: AsyncSession,
    *,
    user_id: UUID,
    settings: ApplicationSettings,
    now: datetime | None = None,
) -> Conversation:
    """Reuse the active conversation, or rotate it after inactivity (§7.2, Q30).

    Rotation is by *inactivity*, not by elapsed time since the conversation
    started: a long conversation that never goes quiet is still one
    conversation, and it maps to one LangGraph thread later.
    """
    moment = now or datetime.now(UTC)
    timeout: timedelta = settings.workflow.conversation_timeout

    active = await session.scalar(
        sa.select(Conversation)
        .where(
            Conversation.user_id == user_id,
            Conversation.status == ConversationStatus.ACTIVE,
            Conversation.deleted_at.is_(None),
        )
        .order_by(Conversation.last_activity_at.desc())
        .limit(1)
    )

    if active is not None:
        if moment - active.last_activity_at <= timeout:
            active.last_activity_at = moment
            return active
        active.status = ConversationStatus.CLOSED
        active.closed_at = moment
        await session.flush()

    conversation = Conversation(
        user_id=user_id,
        status=ConversationStatus.ACTIVE,
        started_at=moment,
        last_activity_at=moment,
    )
    session.add(conversation)
    await session.flush()
    return conversation


async def persist_inbound_message(
    session: AsyncSession,
    inbound: InboundMessage,
    *,
    user_id: UUID,
    conversation_id: UUID,
    trace_id: str | None,
    received_at: datetime | None = None,
) -> IngestedMessage:
    """Store the message, or recognise one already stored (§28).

    Deduplication is `ON CONFLICT DO NOTHING` against
    `UNIQUE(provider, external_message_id)` rather than a read followed by a
    write: the provider retries webhooks concurrently, and a check-then-insert
    would let two requests both pass the check.
    """
    statement = (
        insert(Message)
        .values(
            user_id=user_id,
            conversation_id=conversation_id,
            provider=inbound.provider,
            external_message_id=inbound.external_message_id,
            direction=MessageDirection.INBOUND,
            content_type=inbound.content_type,
            text=inbound.text,
            provider_sent_at=inbound.sent_at,
            received_at=received_at or datetime.now(UTC),
            trace_id=trace_id,
        )
        .on_conflict_do_nothing(constraint="uq_messages_provider_external_id")
        .returning(Message.id)
    )
    inserted = await session.scalar(statement)

    if inserted is not None:
        return IngestedMessage(
            message_id=inserted,
            user_id=user_id,
            conversation_id=conversation_id,
            is_new=True,
        )

    existing = await session.scalar(
        sa.select(Message).where(
            Message.provider == inbound.provider,
            Message.external_message_id == inbound.external_message_id,
        )
    )
    if existing is None:  # pragma: no cover - only reachable if the row vanished
        raise RuntimeError("insert was skipped but no existing message was found")

    return IngestedMessage(
        message_id=existing.id,
        user_id=existing.user_id,
        conversation_id=existing.conversation_id,
        is_new=False,
    )
