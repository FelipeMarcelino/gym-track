"""0005 — exercise catalog (§16, Q43, Q44)

The global canonical catalog the resolver reads: exercises, the names people
actually type for them, muscles, equipment and the relations between movements.

Alias uniqueness is two partial indexes rather than one composite constraint.
PostgreSQL treats NULLs as distinct, so `UNIQUE(user_id, normalized_alias)`
would accept two *global* aliases with the same text -- and stage 2 of the
resolver would then have to choose between them. A global alias and a user's
learned alias may share text; two globals may not.

This migration also seeds the curated catalog, so a clean clone has one. The
seed itself is convergent and lives in `app.infrastructure.postgres.seeding`,
because a migration runs once and is stamped forever: an edited catalog would
otherwise reach fresh databases only. `make seed` reconciles the rest.

Revision ID: 0005_exercise_catalog
Revises: 0004_one_batch_per_message
Create Date: 2026-08-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.config import load_settings
from app.infrastructure.postgres.provisioning import sync_service_roles
from app.infrastructure.postgres.seeding import seed_catalog_sync

revision: str = "0005_exercise_catalog"
down_revision: str | None = "0004_one_batch_per_message"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "equipment",
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("is_implement", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_equipment_slug"),
    )
    op.create_table(
        "exercises",
        sa.Column("canonical_name", sa.String(length=160), nullable=False),
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column(
            "activity_type",
            sa.Enum(
                "strength",
                "distance_activity",
                "timed_activity",
                "mixed_activity",
                "mobility",
                "other",
                name="activitytype",
                native_enum=False,
                create_constraint=True,
                length=64,
            ),
            nullable=False,
        ),
        sa.Column(
            "default_load_mode",
            sa.Enum(
                "total",
                "per_side",
                "per_implement",
                "bodyweight",
                "bodyweight_plus",
                "bodyweight_minus",
                name="loadmode",
                native_enum=False,
                create_constraint=True,
                length=64,
            ),
            nullable=False,
        ),
        sa.Column("is_bodyweight", sa.Boolean(), nullable=False),
        sa.Column("locale", sa.String(length=16), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("canonical_name", name="uq_exercises_canonical_name"),
        sa.UniqueConstraint("slug", name="uq_exercises_slug"),
    )
    op.create_table(
        "muscles",
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("muscle_group", sa.String(length=80), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_muscles_slug"),
    )
    op.create_table(
        "exercise_aliases",
        sa.Column("exercise_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("alias", sa.String(length=160), nullable=False),
        sa.Column("normalized_alias", sa.String(length=160), nullable=False),
        sa.Column(
            "source",
            sa.Enum(
                "seed",
                "user_confirmed",
                name="aliassource",
                native_enum=False,
                create_constraint=True,
                length=64,
            ),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["exercise_id"], ["exercises.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_exercise_aliases_exercise_id"), "exercise_aliases", ["exercise_id"], unique=False
    )
    op.create_index(
        "ix_exercise_aliases_normalized", "exercise_aliases", ["normalized_alias"], unique=False
    )
    op.create_index(
        "uq_exercise_aliases_global",
        "exercise_aliases",
        ["normalized_alias"],
        unique=True,
        postgresql_where=sa.text("user_id IS NULL AND deleted_at IS NULL"),
    )
    op.create_index(
        "uq_exercise_aliases_user",
        "exercise_aliases",
        ["user_id", "normalized_alias"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL AND deleted_at IS NULL"),
    )
    op.create_table(
        "exercise_equipment",
        sa.Column("exercise_id", sa.Uuid(), nullable=False),
        sa.Column("equipment_id", sa.Uuid(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["equipment_id"], ["equipment.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["exercise_id"], ["exercises.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("exercise_id", "equipment_id", name="uq_exercise_equipment_pair"),
    )
    op.create_index(
        op.f("ix_exercise_equipment_exercise_id"),
        "exercise_equipment",
        ["exercise_id"],
        unique=False,
    )
    op.create_table(
        "exercise_muscles",
        sa.Column("exercise_id", sa.Uuid(), nullable=False),
        sa.Column("muscle_id", sa.Uuid(), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "primary",
                "secondary",
                "stabilizer",
                name="musclerole",
                native_enum=False,
                create_constraint=True,
                length=64,
            ),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["exercise_id"], ["exercises.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["muscle_id"], ["muscles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("exercise_id", "muscle_id", name="uq_exercise_muscles_pair"),
    )
    op.create_index(
        op.f("ix_exercise_muscles_exercise_id"), "exercise_muscles", ["exercise_id"], unique=False
    )
    op.create_table(
        "exercise_relations",
        sa.Column("from_exercise_id", sa.Uuid(), nullable=False),
        sa.Column("to_exercise_id", sa.Uuid(), nullable=False),
        sa.Column(
            "relation_type",
            sa.Enum(
                "variation_of",
                "substitute_for",
                "similar_movement",
                "progression_of",
                "regression_of",
                name="exerciserelationtype",
                native_enum=False,
                create_constraint=True,
                length=64,
            ),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "from_exercise_id <> to_exercise_id", name="ck_exercise_relations_not_self"
        ),
        sa.ForeignKeyConstraint(["from_exercise_id"], ["exercises.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["to_exercise_id"], ["exercises.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "from_exercise_id", "to_exercise_id", "relation_type", name="uq_exercise_relations"
        ),
    )
    op.create_index(
        op.f("ix_exercise_relations_from_exercise_id"),
        "exercise_relations",
        ["from_exercise_id"],
        unique=False,
    )

    # A database with a schema but no catalog resolves nothing, so the seed is
    # part of the upgrade rather than a step someone has to remember.
    seed_catalog_sync(op.get_bind())

    # Migration 0002 granted what existed then. These tables did not, so the
    # policy is applied again now that they do -- the same convergence that
    # lets `make provision` fix an existing database.
    sync_service_roles(op.get_bind(), load_settings())


def downgrade() -> None:
    op.drop_index(op.f("ix_exercise_relations_from_exercise_id"), table_name="exercise_relations")
    op.drop_table("exercise_relations")
    op.drop_index(op.f("ix_exercise_muscles_exercise_id"), table_name="exercise_muscles")
    op.drop_table("exercise_muscles")
    op.drop_index(op.f("ix_exercise_equipment_exercise_id"), table_name="exercise_equipment")
    op.drop_table("exercise_equipment")
    op.drop_index(
        "uq_exercise_aliases_user",
        table_name="exercise_aliases",
        postgresql_where=sa.text("user_id IS NOT NULL AND deleted_at IS NULL"),
    )
    op.drop_index(
        "uq_exercise_aliases_global",
        table_name="exercise_aliases",
        postgresql_where=sa.text("user_id IS NULL AND deleted_at IS NULL"),
    )
    op.drop_index("ix_exercise_aliases_normalized", table_name="exercise_aliases")
    op.drop_index(op.f("ix_exercise_aliases_exercise_id"), table_name="exercise_aliases")
    op.drop_table("exercise_aliases")
    op.drop_table("muscles")
    op.drop_table("exercises")
    op.drop_table("equipment")
