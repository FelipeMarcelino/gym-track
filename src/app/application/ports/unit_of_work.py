"""The transaction boundary, expressed without naming a database (§36).

Application code commits work through this port. It is deliberately tiny: the
only thing the application layer needs to say about persistence is where a unit
of work begins and ends -- everything else belongs to the adapters.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import Protocol


class UnitOfWork(Protocol):
    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...

    async def flush(self) -> None: ...


class UnitOfWorkFactory(Protocol):
    """Opens a transaction. Exiting the context commits, or rolls back on error."""

    def __call__(self) -> AbstractAsyncContextManager[UnitOfWork]: ...


__all__ = ["AsyncIterator", "UnitOfWork", "UnitOfWorkFactory"]
