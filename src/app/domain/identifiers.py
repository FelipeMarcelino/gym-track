"""UUIDv7 generation (decision D5).

Time-ordered primary keys buy index locality on append-heavy tables like
`messages` and `domain_events`, where UUIDv4 would scatter every insert across
the B-tree. RFC 9562 §5.7 layout:

    48 bits  Unix milliseconds
     4 bits  version (7)
    12 bits  rand_a -- used here as a sub-millisecond counter
     2 bits  variant (0b10)
    62 bits  rand_b

`rand_a` carries a counter rather than randomness so that identifiers minted
inside the same millisecond still sort in creation order. Without it, ordering
would hold only at millisecond granularity, and a batch of rows written in one
transaction is exactly the case where that is not good enough.
"""

from __future__ import annotations

import secrets
import threading
import time
from uuid import UUID

_COUNTER_BITS = 12
_MAX_COUNTER = (1 << _COUNTER_BITS) - 1

_lock = threading.Lock()
_last_timestamp_ms = -1
_counter = 0


def new_uuid7() -> UUID:
    """Return a fresh, monotonically increasing UUIDv7."""
    global _last_timestamp_ms, _counter

    with _lock:
        timestamp_ms = time.time_ns() // 1_000_000

        if timestamp_ms > _last_timestamp_ms:
            _last_timestamp_ms = timestamp_ms
            _counter = secrets.randbelow(_MAX_COUNTER // 2)
        elif _counter >= _MAX_COUNTER:
            # More than 2048 ids in one millisecond: step into the next one
            # rather than wrap the counter and break ordering.
            _last_timestamp_ms += 1
            timestamp_ms = _last_timestamp_ms
            _counter = 0
        else:
            timestamp_ms = _last_timestamp_ms
            _counter += 1

        counter = _counter

    value = (timestamp_ms & 0xFFFF_FFFF_FFFF) << 80
    value |= 0x7 << 76
    value |= counter << 64
    value |= 0b10 << 62
    value |= secrets.randbits(62)
    return UUID(int=value)


def uuid7_timestamp_ms(value: UUID) -> int:
    """Extract the millisecond timestamp a UUIDv7 was minted at."""
    if value.version != 7:
        raise ValueError(f"{value} is not a UUIDv7")
    return value.int >> 80
