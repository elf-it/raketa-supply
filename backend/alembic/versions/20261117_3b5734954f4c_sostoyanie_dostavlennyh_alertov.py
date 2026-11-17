"""Состояние доставленных алертов (задача G-16).

Доставка алертов обязана помнить, о чём уже сообщила: алерт, приходящий каждый
прогон, перестают читать, и доставка становится дороже своего отсутствия.
Строка — один открытый повод; когда повод исчезает, уходит сообщение
о восстановлении, а строка удаляется.

Revision ID: 3b5734954f4c
Revises: 7d5a28e37c8c
Create Date: 2026-11-17 11:21:54.895597
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "3b5734954f4c"
down_revision: str | None = "7d5a28e37c8c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alert_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("since", sa.DateTime(), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(), nullable=False),
        sa.Column("sends", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("alert_states")
