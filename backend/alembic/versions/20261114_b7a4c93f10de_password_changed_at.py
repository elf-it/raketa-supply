"""Отметка смены пароля у пользователя (задача G-8).

Revision ID: b7a4c93f10de
Revises: 4effa61686bb
Create Date: 2026-11-14 10:12:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7a4c93f10de"
down_revision: str | None = "4effa61686bb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("password_changed_at", sa.DateTime(), nullable=True))
    # Существующим учётным записям отметка равна дате заведения: пароль с тех пор
    # не меняли, и уже выданные сессии обрывать не за что.
    op.execute("UPDATE users SET password_changed_at = created_at")


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("password_changed_at")
