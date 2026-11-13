"""Начальная схема: справочники, недельные факты, поставки, правки и журнал.

Соответствует состоянию моделей на момент перехода с ``create_all`` на миграции.
Отдельных ревизий на ранее добавленные колонки нет — до этой точки схема
пересоздавалась целиком, боевых баз не существовало.

Revision ID: c5d1ee4946b5
Revises:
Create Date: 2026-11-13 23:14:40.766863
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c5d1ee4946b5"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("last_closed_week", sa.String(length=8), nullable=True),
        sa.Column("horizon_weeks", sa.Integer(), nullable=False),
        sa.Column("history_weeks", sa.Integer(), nullable=False),
        sa.Column("over_coverage_weeks", sa.Float(), nullable=False),
        sa.Column("short_horizon_weeks", sa.Integer(), nullable=False),
        sa.Column("demand_avg_weeks", sa.Integer(), nullable=False),
        sa.Column("default_lead_time_days", sa.Integer(), nullable=False),
        sa.Column("default_moq", sa.Integer(), nullable=False),
        sa.Column("default_pack_size", sa.Integer(), nullable=False),
        sa.Column("default_coverage_weeks", sa.Integer(), nullable=False),
        sa.Column("default_safety_weeks", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "categories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("niche", sa.String(length=128), nullable=True),
        sa.Column("abc_class", sa.String(length=2), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "stores",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column(
            "marketplace",
            sa.Enum("WB", "OZON", "YM", name="marketplace", native_enum=False),
            nullable=False,
        ),
        sa.Column("cabinet_ref", sa.String(length=64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_table(
        "suppliers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("lead_time_days", sa.Integer(), nullable=False),
        sa.Column("moq", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("vendor_code", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("supplier_id", sa.Integer(), nullable=True),
        sa.Column("cost_price", sa.Float(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("volume_m3", sa.Float(), nullable=False),
        sa.Column("lead_time_days", sa.Integer(), nullable=True),
        sa.Column("moq", sa.Integer(), nullable=True),
        sa.Column("pack_size", sa.Integer(), nullable=True),
        sa.Column("coverage_weeks", sa.Integer(), nullable=True),
        sa.Column("safety_weeks", sa.Float(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"],
            ["suppliers.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("products", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_products_vendor_code"), ["vendor_code"], unique=True)

    op.create_table(
        "seasonality",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("iso_week", sa.Integer(), nullable=False),
        sa.Column("coefficient", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("category_id", "iso_week", name="uq_seasonality_week"),
    )
    with op.batch_alter_table("seasonality", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_seasonality_category_id"), ["category_id"], unique=False
        )

    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("store_id", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("RUNNING", "SUCCESS", "FAILED", name="syncstatus", native_enum=False),
            nullable=False,
        ),
        sa.Column("facts_upserted", sa.Integer(), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["store_id"],
            ["stores.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("sync_runs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_sync_runs_store_id"), ["store_id"], unique=False)

    op.create_table(
        "store_products",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("store_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("nm_id", sa.String(length=32), nullable=True),
        sa.Column("barcode", sa.String(length=64), nullable=True),
        sa.Column("base_potential", sa.Float(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
        ),
        sa.ForeignKeyConstraint(
            ["store_id"],
            ["stores.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("store_id", "product_id", name="uq_store_product"),
    )
    with op.batch_alter_table("store_products", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_store_products_nm_id"), ["nm_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_store_products_product_id"), ["product_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_store_products_store_id"), ["store_id"], unique=False)

    op.create_table(
        "adjustment_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("store_product_id", sa.Integer(), nullable=False),
        sa.Column("week", sa.String(length=8), nullable=False),
        sa.Column("field", sa.String(length=32), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("batch_id", sa.String(length=32), nullable=True),
        sa.Column("old_value", sa.Float(), nullable=True),
        sa.Column("new_value", sa.Float(), nullable=True),
        sa.Column("author", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["store_product_id"],
            ["store_products.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("adjustment_log", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_adjustment_log_batch_id"), ["batch_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_adjustment_log_operation"), ["operation"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_adjustment_log_store_product_id"), ["store_product_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_adjustment_log_week"), ["week"], unique=False)

    op.create_table(
        "forecast_adjustments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("store_product_id", sa.Integer(), nullable=False),
        sa.Column("week", sa.String(length=8), nullable=False),
        sa.Column("diff_percent", sa.Float(), nullable=True),
        sa.Column("manual_forecast", sa.Float(), nullable=True),
        sa.Column("manual_adapted", sa.Float(), nullable=True),
        sa.Column("author", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["store_product_id"],
            ["store_products.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("store_product_id", "week", name="uq_forecast_adjustment"),
    )
    with op.batch_alter_table("forecast_adjustments", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_forecast_adjustments_store_product_id"),
            ["store_product_id"],
            unique=False,
        )
        batch_op.create_index(batch_op.f("ix_forecast_adjustments_week"), ["week"], unique=False)

    op.create_table(
        "supplies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("store_product_id", sa.Integer(), nullable=False),
        sa.Column("week", sa.String(length=8), nullable=False),
        sa.Column("qty", sa.Float(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("PLANNED", "IN_TRANSIT", "RECEIVED", name="supplystatus", native_enum=False),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["store_product_id"],
            ["store_products.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("supplies", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_supplies_store_product_id"), ["store_product_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_supplies_week"), ["week"], unique=False)

    op.create_table(
        "week_facts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("store_product_id", sa.Integer(), nullable=False),
        sa.Column("week", sa.String(length=8), nullable=False),
        sa.Column("order_units", sa.Float(), nullable=False),
        sa.Column("buyout_units", sa.Float(), nullable=False),
        sa.Column("sales_units", sa.Float(), nullable=False),
        sa.Column("stock_end_units", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["store_product_id"],
            ["store_products.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("store_product_id", "week", name="uq_week_fact"),
    )
    with op.batch_alter_table("week_facts", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_week_facts_store_product_id"), ["store_product_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_week_facts_week"), ["week"], unique=False)

    op.create_table(
        "week_plans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("store_product_id", sa.Integer(), nullable=False),
        sa.Column("week", sa.String(length=8), nullable=False),
        sa.Column("plan_units", sa.Float(), nullable=False),
        sa.Column("frozen_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["store_product_id"],
            ["store_products.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("store_product_id", "week", name="uq_week_plan"),
    )
    with op.batch_alter_table("week_plans", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_week_plans_store_product_id"), ["store_product_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_week_plans_week"), ["week"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("week_plans", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_week_plans_week"))
        batch_op.drop_index(batch_op.f("ix_week_plans_store_product_id"))

    op.drop_table("week_plans")
    with op.batch_alter_table("week_facts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_week_facts_week"))
        batch_op.drop_index(batch_op.f("ix_week_facts_store_product_id"))

    op.drop_table("week_facts")
    with op.batch_alter_table("supplies", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_supplies_week"))
        batch_op.drop_index(batch_op.f("ix_supplies_store_product_id"))

    op.drop_table("supplies")
    with op.batch_alter_table("forecast_adjustments", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_forecast_adjustments_week"))
        batch_op.drop_index(batch_op.f("ix_forecast_adjustments_store_product_id"))

    op.drop_table("forecast_adjustments")
    with op.batch_alter_table("adjustment_log", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_adjustment_log_week"))
        batch_op.drop_index(batch_op.f("ix_adjustment_log_store_product_id"))
        batch_op.drop_index(batch_op.f("ix_adjustment_log_operation"))
        batch_op.drop_index(batch_op.f("ix_adjustment_log_batch_id"))

    op.drop_table("adjustment_log")
    with op.batch_alter_table("store_products", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_store_products_store_id"))
        batch_op.drop_index(batch_op.f("ix_store_products_product_id"))
        batch_op.drop_index(batch_op.f("ix_store_products_nm_id"))

    op.drop_table("store_products")
    with op.batch_alter_table("sync_runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sync_runs_store_id"))

    op.drop_table("sync_runs")
    with op.batch_alter_table("seasonality", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_seasonality_category_id"))

    op.drop_table("seasonality")
    with op.batch_alter_table("products", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_products_vendor_code"))

    op.drop_table("products")
    op.drop_table("suppliers")
    op.drop_table("stores")
    op.drop_table("categories")
    op.drop_table("app_settings")
