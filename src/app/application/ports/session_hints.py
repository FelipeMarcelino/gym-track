"""The shape of an expiry-hint store, so nothing depends on Redis to have one.

§18 makes PostgreSQL's `last_activity_at` authoritative and allows Redis to
"provide expiration hints". Stating that as a port keeps the dependency
pointing the right way: the manager that observes activity and the worker that
sweeps both speak to this, and a deployment without Redis passes None.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID


class SessionHintStore(Protocol):
    async def note_activity(self, user_id: UUID) -> None:
        """Remember that this user was active, so the sweep can find them."""
        ...

    async def expiry_candidates(self, *, limit: int = 100) -> list[UUID]:
        """Users worth looking at first. Never the set of users to look at."""
        ...
