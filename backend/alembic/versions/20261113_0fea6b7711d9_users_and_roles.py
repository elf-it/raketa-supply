"""Пользователи и роли: права доступа (задача G-1).

Пароль хранится только как bcrypt-хеш. Демо-пользователи заводятся сидом,
а не миграцией: миграция не должна создавать учётные записи.

Revision ID: 0fea6b7711d9
Revises: c5d1ee4946b5
Create Date: 2026-11-13 23:25:42.927450
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0fea6b7711d9"
down_revision: str | None = "c5d1ee4946b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=190), nullable=False),
        sa.Column("full_name", sa.String(length=128), nullable=False),
        sa.Column(
            "role",
            sa.Enum("VIEWER", "EDITOR", "ADMIN", name="role", native_enum=False),
            nullable=False,
        ),
        sa.Column("password_hash", sa.String(length=128), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_users_email"), ["email"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_users_email"))

    op.drop_table("users")
