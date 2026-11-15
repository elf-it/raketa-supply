"""История цен и себестоимости товара (задача B-9).

Прежние значения карточки товара здесь не восстанавливаются: дату, с которой
действовала нынешняя цена, взять неоткуда, а поставить дату миграции означало бы
записать в историю неправду. История начинается с первой правки цены; до неё
экран так и говорит — «когда установлена, неизвестно».

Revision ID: e5067e70d1ee
Revises: 9c2e7ab45d31
Create Date: 2026-11-15 06:36:52.224203
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e5067e70d1ee"
down_revision: str | None = "9c2e7ab45d31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_prices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("cost_price", sa.Float(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("author", sa.String(length=64), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("product_id", "valid_from", name="uq_product_price_date"),
    )
    with op.batch_alter_table("product_prices", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_product_prices_product_id"), ["product_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_product_prices_valid_from"), ["valid_from"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("product_prices", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_product_prices_valid_from"))
        batch_op.drop_index(batch_op.f("ix_product_prices_product_id"))

    op.drop_table("product_prices")
