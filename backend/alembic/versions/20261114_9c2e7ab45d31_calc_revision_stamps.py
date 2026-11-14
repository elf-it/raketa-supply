"""Отметки версий данных расчёта: кэш недельных рядов (задача G-6).

Номер версии хранится в базе, а не в памяти приложения: только так правка,
прошедшая через один процесс, обесценивает кэш у всех остальных.

Revision ID: 9c2e7ab45d31
Revises: b7a4c93f10de
Create Date: 2026-11-14 21:40:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9c2e7ab45d31"
down_revision: str | None = "b7a4c93f10de"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default нужен не только существующим строкам: номер поднимается
    # запросом `calc_revision = calc_revision + 1`, и NULL в нём превратил бы
    # версию пары в NULL навсегда.
    with op.batch_alter_table("store_products", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("calc_revision", sa.Integer(), nullable=False, server_default="1")
        )
    with op.batch_alter_table("app_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("calc_epoch", sa.Integer(), nullable=False, server_default="1")
        )


def downgrade() -> None:
    with op.batch_alter_table("app_settings", schema=None) as batch_op:
        batch_op.drop_column("calc_epoch")
    with op.batch_alter_table("store_products", schema=None) as batch_op:
        batch_op.drop_column("calc_revision")
