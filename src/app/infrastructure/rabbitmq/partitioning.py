"""The workflow partition contract (§9.2, DEC-006).

Ordering per user is infrastructure behaviour here, not a distributed lock: a
user's messages always land on the same queue, and that queue has a single
active consumer. The mapping is therefore a **contract**, not an
implementation detail -- changing it reorders every user's message stream and
can interleave two workflow executions for the same conversation.

Python's built-in `hash()` is explicitly forbidden by §9.2: it is salted per
process, so the same user would map to different partitions in two workers of
the same deployment. blake2b is stable across processes, machines and Python
versions.
"""

from __future__ import annotations

from hashlib import blake2b
from typing import Final
from uuid import UUID

#: Frozen by DEC-006. Configurable only so tests can shrink it deliberately.
DEFAULT_PARTITIONS: Final = 32

QUEUE_PREFIX: Final = "workflow"


def stable_hash(user_id: UUID) -> int:
    """A stable 64-bit digest of the user id.

    The 16 raw bytes are hashed, not the string form: a UUID has several
    textual renderings and only one byte representation.
    """
    return int.from_bytes(blake2b(user_id.bytes, digest_size=8).digest(), "big")


def partition_for(user_id: UUID, partitions: int = DEFAULT_PARTITIONS) -> int:
    if partitions < 1:
        raise ValueError("partitions must be at least 1")
    return stable_hash(user_id) % partitions


def partition_queue_name(partition: int, partitions: int = DEFAULT_PARTITIONS) -> str:
    """`workflow.00` .. `workflow.31`, zero-padded so they sort like numbers."""
    if not 0 <= partition < partitions:
        raise ValueError(f"partition {partition} is outside 0..{partitions - 1}")
    width = max(2, len(str(partitions - 1)))
    return f"{QUEUE_PREFIX}.{partition:0{width}d}"


def queue_for_user(user_id: UUID, partitions: int = DEFAULT_PARTITIONS) -> str:
    return partition_queue_name(partition_for(user_id, partitions), partitions)
