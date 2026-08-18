"""Contract test: the envelope's wire format is frozen (§27.1, §38).

A message published by today's code is consumed by whatever version of the
consumer happens to be deployed. If this test fails, the change is a
compatibility decision -- a new `event_version`, or a deliberate break -- not a
refactor. Updating the fixture to match new code defeats the point of having it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from app.domain.events import DomainEventEnvelope

FIXTURE = Path(__file__).parent / "fixtures" / "domain_event_envelope.json"
GOLDEN = json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_the_golden_fixture_deserializes() -> None:
    envelope = DomainEventEnvelope.model_validate(GOLDEN)

    assert envelope.event_id == UUID("0198f2c7-4a00-7b3c-8f21-6d5e4c3b2a19")
    assert envelope.event_type == "message.received"
    assert envelope.event_version == 1
    assert envelope.occurred_at == datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    assert envelope.payload["external_message_id"] == "wamid.HBgNNTUxMTk5"


def test_serialization_matches_the_golden_fixture_byte_for_byte() -> None:
    serialized = DomainEventEnvelope.model_validate(GOLDEN).model_dump(mode="json")

    assert serialized == GOLDEN, (
        "the wire format changed: bump event_version and add a fixture rather than editing this one"
    )


def test_the_envelope_round_trips() -> None:
    envelope = DomainEventEnvelope.model_validate(GOLDEN)

    assert DomainEventEnvelope.model_validate(envelope.model_dump(mode="json")) == envelope


def test_the_field_set_is_exactly_what_section_27_1_declares() -> None:
    assert set(DomainEventEnvelope.model_fields) == {
        "event_id",
        "event_type",
        "event_version",
        "aggregate_type",
        "aggregate_id",
        "user_id",
        "trace_id",
        "correlation_id",
        "causation_id",
        "payload",
        "occurred_at",
    }


def test_unknown_fields_are_rejected_rather_than_silently_dropped() -> None:
    """A consumer that quietly ignores a field it does not know is how a
    producer's bug becomes invisible."""
    with pytest.raises(ValueError, match=r"extra_forbidden|Extra inputs"):
        DomainEventEnvelope.model_validate({**GOLDEN, "surprise": 1})


def test_envelopes_are_immutable() -> None:
    envelope = DomainEventEnvelope.model_validate(GOLDEN)

    with pytest.raises(ValueError, match="frozen"):
        envelope.event_type = "something.else"


def test_event_ids_are_time_ordered() -> None:
    """The id doubles as the dedupe key and as a `domain_events` primary key,
    so it inherits the UUIDv7 ordering that table depends on."""
    ids = [
        DomainEventEnvelope(event_type="t", aggregate_type="a", aggregate_id=UUID(int=1)).event_id
        for _ in range(100)
    ]

    assert ids == sorted(ids)
