"""WS-3: UUIDv7 must be well-formed and ordered (decision D5)."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest

from app.domain.identifiers import new_uuid7, uuid7_timestamp_ms


def test_layout_follows_rfc_9562() -> None:
    value = new_uuid7()

    assert value.version == 7
    assert value.variant == "specified in RFC 4122"


def test_timestamp_is_the_current_millisecond() -> None:
    before = time.time_ns() // 1_000_000
    value = new_uuid7()
    after = time.time_ns() // 1_000_000

    assert before <= uuid7_timestamp_ms(value) <= after


def test_identifiers_are_monotonic_within_a_batch() -> None:
    """The property that makes these keys worth having: insertion order sorts."""
    batch = [new_uuid7() for _ in range(10_000)]

    assert batch == sorted(batch), "a batch minted in one burst must sort in creation order"
    assert len(set(batch)) == len(batch)


def test_identifiers_stay_monotonic_across_threads() -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        generated = list(pool.map(lambda _: new_uuid7(), range(4_000)))

    assert len(set(generated)) == len(generated), "concurrent minting produced a collision"


def test_ordering_survives_the_string_form() -> None:
    """Text-sorted UUIDs must agree with the integer order, since indexes and
    logs both sort the rendered form."""
    batch = [str(new_uuid7()) for _ in range(1_000)]

    assert batch == sorted(batch)


def test_timestamp_extraction_rejects_other_versions() -> None:
    with pytest.raises(ValueError, match="not a UUIDv7"):
        uuid7_timestamp_ms(UUID("00000000-0000-4000-8000-000000000000"))
