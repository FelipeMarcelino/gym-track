"""Parsing Meta's webhook payload into something the domain can use (§35.1).

The provider's shape is deeply nested and carries several event kinds on the
same endpoint -- messages, delivery statuses, template events. Parsing is
therefore lenient about *which* entries appear and strict about the fields it
uses: an unknown message type becomes an explicitly UNSUPPORTED message rather
than an exception, because dropping a user's message silently is worse than
storing one the system cannot act on yet.

The contract test in tests/contract runs this against recorded fixtures, so a
provider format change fails loudly instead of quietly yielding nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.infrastructure.postgres.models import MessageContentType, MessagingProvider

#: Meta's `type` values mapped onto what the system stores. Anything absent
#: from this table is persisted as UNSUPPORTED.
_CONTENT_TYPES: dict[str, MessageContentType] = {
    "text": MessageContentType.TEXT,
    "audio": MessageContentType.AUDIO,
    "voice": MessageContentType.AUDIO,
    "image": MessageContentType.IMAGE,
}


@dataclass(frozen=True, slots=True)
class InboundMessage:
    provider: MessagingProvider
    external_id: str
    external_message_id: str
    content_type: MessageContentType
    text: str | None
    sent_at: datetime
    #: What the provider called this message type, kept even when unsupported
    #: so the gap is measurable rather than invisible.
    provider_type: str


def parse_webhook(payload: dict[str, Any]) -> list[InboundMessage]:
    """Extract every user message from a webhook body, in arrival order."""
    messages: list[InboundMessage] = []

    for entry in _as_list(payload.get("entry")):
        for change in _as_list(entry.get("changes")):
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            for raw in _as_list(value.get("messages")):
                parsed = _parse_message(raw)
                if parsed is not None:
                    messages.append(parsed)

    return messages


def _parse_message(raw: dict[str, Any]) -> InboundMessage | None:
    external_message_id = raw.get("id")
    sender = raw.get("from")
    if not isinstance(external_message_id, str) or not isinstance(sender, str):
        # Without an id there is no dedupe key and without a sender there is no
        # identity: such an entry is not a message this system can hold.
        return None

    provider_type = str(raw.get("type", "unknown"))
    content_type = _CONTENT_TYPES.get(provider_type, MessageContentType.UNSUPPORTED)

    text: str | None = None
    if content_type is MessageContentType.TEXT:
        body = raw.get("text")
        text = body.get("body") if isinstance(body, dict) else None

    return InboundMessage(
        provider=MessagingProvider.WHATSAPP,
        external_id=sender,
        external_message_id=external_message_id,
        content_type=content_type,
        text=text,
        sent_at=_timestamp(raw.get("timestamp")),
        provider_type=provider_type,
    )


def _timestamp(value: Any) -> datetime:
    """Meta sends Unix seconds as a string. An unparseable value means now."""
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


def _as_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
