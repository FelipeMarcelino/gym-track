"""Declarative base and the mixins every table shares (§36, Q155)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain.identifiers import new_uuid7


def enum_column(enum_type: type[StrEnum], **kwargs: Any) -> sa.Enum:
    """Persist an enum as a checked string rather than a native PostgreSQL type.

    A native enum type needs its own migration for every added value and cannot
    be altered inside a transaction on older servers. A VARCHAR with a CHECK
    constraint keeps the same guarantee at the database level while staying
    cheap to evolve, which matters for a schema this early.
    """
    return sa.Enum(
        enum_type,
        native_enum=False,
        length=64,
        values_callable=lambda enum: [member.value for member in enum],
        **kwargs,
    )


class Base(DeclarativeBase):
    """UUIDv7 primary key plus creation/update stamps on every table."""

    id: Mapped[UUID] = mapped_column(sa.Uuid, primary_key=True, default=new_uuid7)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )


def select_active[ModelT: "SoftDeleteMixin"](model: type[ModelT]) -> sa.Select[tuple[ModelT]]:
    """Select rows that are not soft-deleted.

    Soft deletion is only a guarantee if the default read path honours it, so
    queries go through this helper rather than repeating the predicate and
    eventually forgetting it somewhere.
    """
    return sa.select(model).where(model.deleted_at.is_(None))


class SoftDeleteMixin:
    """Marks a row deleted without losing it (§26).

    Only entities whose history is worth keeping carry this. Append-only tables
    do not: nothing is ever deleted from them in the first place.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True, default=None
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None
