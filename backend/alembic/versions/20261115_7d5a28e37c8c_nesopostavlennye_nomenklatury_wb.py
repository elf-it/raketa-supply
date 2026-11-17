"""Несопоставленные номенклатуры WB (задача B-10).

Загрузка находит пару по номеру номенклатуры в пределах кабинета, а незнакомый
номер пропускает. Раньше он исчезал вместе с отчётом о прогоне: в журнале
оставалось «не сопоставлено: 7», и какие именно — узнать было негде. Теперь
такие номера копятся в таблице, пока их не привяжут к товару.

Revision ID: 7d5a28e37c8c
Revises: 91154bb1d1d1
Create Date: 2026-11-15 13:05:11.210417
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "7d5a28e37c8c"
down_revision: str | None = "91154bb1d1d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "unmatched_nm_ids",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("store_id", sa.Integer(), nullable=False),
        sa.Column("nm_id", sa.String(length=32), nullable=False),
        sa.Column("first_seen", sa.DateTime(), nullable=False),
        sa.Column("last_seen", sa.DateTime(), nullable=False),
        sa.Column("times_seen", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("store_id", "nm_id", name="uq_unmatched_nm_id"),
    )
    with op.batch_alter_table("unmatched_nm_ids", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_unmatched_nm_ids_nm_id"), ["nm_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_unmatched_nm_ids_store_id"), ["store_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("unmatched_nm_ids", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_unmatched_nm_ids_store_id"))
        batch_op.drop_index(batch_op.f("ix_unmatched_nm_ids_nm_id"))

    op.drop_table("unmatched_nm_ids")
