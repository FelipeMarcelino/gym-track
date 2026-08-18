"""WS-6: the partition contract, frozen (§9.2, DEC-006).

The golden pairs below are the most load-bearing assertion in this sprint. If
they change, every existing user's message stream is reordered and two workflow
executions for one conversation can interleave — so a failure here is never
"update the expected values", it is "revert the hash change".
"""

from __future__ import annotations

import subprocess
import sys
from collections import Counter
from uuid import UUID, uuid4

import pytest

from app.infrastructure.rabbitmq.partitioning import (
    DEFAULT_PARTITIONS,
    partition_for,
    partition_queue_name,
    queue_for_user,
    stable_hash,
)

GOLDEN_PARTITIONS: list[tuple[str, int, str]] = [
    ("00000000-0000-0000-0000-000000000000", 14, "workflow.14"),
    ("0198f2c7-4a00-7b3c-8f21-6d5e4c3b2a19", 10, "workflow.10"),
    ("11111111-1111-1111-1111-111111111111", 27, "workflow.27"),
    ("550e8400-e29b-41d4-a716-446655440000", 15, "workflow.15"),
    ("6ba7b810-9dad-11d1-80b4-00c04fd430c8", 24, "workflow.24"),
    ("ffffffff-ffff-ffff-ffff-ffffffffffff", 7, "workflow.07"),
    ("0198f2c7-4a02-7d5e-a123-456789abcdef", 8, "workflow.08"),
    ("123e4567-e89b-12d3-a456-426614174000", 29, "workflow.29"),
]


@pytest.mark.parametrize(("user_id", "expected_partition", "expected_queue"), GOLDEN_PARTITIONS)
def test_partition_assignment_is_frozen(
    user_id: str, expected_partition: int, expected_queue: str
) -> None:
    assert partition_for(UUID(user_id)) == expected_partition
    assert queue_for_user(UUID(user_id)) == expected_queue


def test_hash_is_stable_across_a_process_boundary() -> None:
    """Python's `hash()` is salted per process; §9.2 forbids it for this reason.
    A subprocess with a different seed must reach the same partition."""
    user_id = "550e8400-e29b-41d4-a716-446655440000"
    program = (
        "from uuid import UUID;"
        "from app.infrastructure.rabbitmq.partitioning import partition_for;"
        f"print(partition_for(UUID('{user_id}')))"
    )

    outputs = {
        subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
        ).stdout.strip()
        for seed in ("0", "1", "12345")
    }

    assert outputs == {"15"}


def test_the_same_user_always_lands_on_the_same_partition() -> None:
    user_id = uuid4()
    assert len({partition_for(user_id) for _ in range(100)}) == 1


def test_users_spread_across_every_partition() -> None:
    """A hash that clusters would leave partitions idle while others queue."""
    counts = Counter(partition_for(uuid4()) for _ in range(20_000))

    assert len(counts) == DEFAULT_PARTITIONS
    average = 20_000 / DEFAULT_PARTITIONS
    assert min(counts.values()) > average * 0.7
    assert max(counts.values()) < average * 1.3


def test_the_byte_representation_is_what_is_hashed() -> None:
    """A UUID has several textual forms and one byte form; hashing the text
    would make the partition depend on how the caller happened to render it."""
    plain = UUID("550e8400e29b41d4a716446655440000")
    dashed = UUID("550e8400-e29b-41d4-a716-446655440000")
    braced = UUID("{550e8400-e29b-41d4-a716-446655440000}")

    assert stable_hash(plain) == stable_hash(dashed) == stable_hash(braced)


def test_queue_names_are_zero_padded_so_they_sort_like_numbers() -> None:
    names = [partition_queue_name(partition) for partition in range(DEFAULT_PARTITIONS)]

    assert names[0] == "workflow.00"
    assert names[-1] == "workflow.31"
    assert names == sorted(names)


def test_a_partition_outside_the_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="outside"):
        partition_queue_name(DEFAULT_PARTITIONS)


def test_partition_count_must_be_positive() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        partition_for(uuid4(), 0)


def test_a_smaller_partition_count_is_honoured_for_tests() -> None:
    assert all(partition_for(uuid4(), 4) < 4 for _ in range(200))
    assert partition_queue_name(3, 4) == "workflow.03"
