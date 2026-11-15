"""Счётчик попыток входа в базе (задача G-12).

Счётчик перебора жил в памяти процесса: за несколькими воркерами лимит делился
на их число, а перезапуск обнулял его целиком — перебор достаточно было
переждать. Теперь попытки лежат в базе, общей для всех воркеров.

Таблица заполняется только неудачными попытками и сама себя чистит: записи
старше окна подсчёта удаляются при следующей попытке. Паролей в ней нет.

Revision ID: 91154bb1d1d1
Revises: e5067e70d1ee
Create Date: 2026-11-15 12:02:54.548082
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "91154bb1d1d1"
down_revision: str | None = "e5067e70d1ee"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "login_attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=190), nullable=False),
        sa.Column("address", sa.String(length=64), nullable=False),
        sa.Column("happened_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("login_attempts", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_login_attempts_address"), ["address"], unique=False)
        batch_op.create_index(batch_op.f("ix_login_attempts_email"), ["email"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_login_attempts_happened_at"), ["happened_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("login_attempts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_login_attempts_happened_at"))
        batch_op.drop_index(batch_op.f("ix_login_attempts_email"))
        batch_op.drop_index(batch_op.f("ix_login_attempts_address"))

    op.drop_table("login_attempts")
