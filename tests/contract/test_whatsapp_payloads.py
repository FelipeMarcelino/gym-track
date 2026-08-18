"""Contract test: Meta's webhook shape, against recorded fixtures (§38).

The parser is the system's only view of the provider. If Meta changes the
payload, this test is where it must fail — the alternative is a webhook that
returns 200 and drops every message it no longer recognises.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.infrastructure.postgres.models import MessageContentType, MessagingProvider
from app.infrastructure.whatsapp.payloads import parse_webhook

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict[str, Any]:
    loaded = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_a_text_message_is_parsed_completely() -> None:
    (message,) = parse_webhook(fixture("whatsapp_text_message"))

    assert message.provider is MessagingProvider.WHATSAPP
    assert message.external_id == "5511987654321"
    assert message.external_message_id.startswith("wamid.")
    assert message.content_type is MessageContentType.TEXT
    assert message.text == "fiz supino 3x10 80kg"
    assert message.sent_at == datetime(2025, 8, 18, 12, 0, tzinfo=UTC)


def test_an_audio_message_is_kept_without_a_transcript() -> None:
    """Speech-to-text is out of this sprint; the message is still captured."""
    (message,) = parse_webhook(fixture("whatsapp_audio_message"))

    assert message.content_type is MessageContentType.AUDIO
    assert message.text is None
    assert message.provider_type == "audio"


def test_a_status_callback_yields_no_messages() -> None:
    """Meta posts delivery statuses to the same endpoint. They are not messages,
    and treating them as an error would make the provider retry forever."""
    assert parse_webhook(fixture("whatsapp_status_callback")) == []


def test_several_messages_keep_their_arrival_order() -> None:
    messages = parse_webhook(fixture("whatsapp_batched_messages"))

    assert [message.external_message_id for message in messages] == [
        "wamid.AAA",
        "wamid.BBB",
        "wamid.CCC",
    ]


def test_an_unsupported_type_is_stored_rather_than_dropped() -> None:
    """Dropping a user's message silently is worse than storing one the system
    cannot act on yet — and the provider's own name for it is kept, so the gap
    is measurable."""
    messages = parse_webhook(fixture("whatsapp_batched_messages"))
    sticker = messages[-1]

    assert sticker.content_type is MessageContentType.UNSUPPORTED
    assert sticker.provider_type == "sticker"
    assert sticker.text is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"entry": []},
        {"entry": [{"changes": []}]},
        {"entry": [{"changes": [{"value": {}}]}]},
        {"entry": [{"changes": [{"value": {"messages": []}}]}]},
        {"entry": "not-a-list"},
    ],
)
def test_shapes_without_messages_are_handled_quietly(payload: dict[str, Any]) -> None:
    assert parse_webhook(payload) == []


@pytest.mark.parametrize(
    "message",
    [
        {"id": "wamid.X", "type": "text", "text": {"body": "oi"}},
        {"from": "5511987654321", "type": "text", "text": {"body": "oi"}},
    ],
)
def test_a_message_without_identity_or_id_is_not_a_message(message: dict[str, Any]) -> None:
    """Without an id there is no dedupe key; without a sender there is no
    identity. Either way there is nothing this system can durably hold."""
    payload = {"entry": [{"changes": [{"value": {"messages": [message]}}]}]}

    assert parse_webhook(payload) == []


def test_an_unparseable_timestamp_falls_back_to_now() -> None:
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "5511987654321",
                                    "id": "wamid.X",
                                    "type": "text",
                                    "timestamp": "not-a-number",
                                    "text": {"body": "oi"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }

    (message,) = parse_webhook(payload)

    assert (datetime.now(UTC) - message.sent_at).total_seconds() < 5
