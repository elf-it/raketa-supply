"""Журнал корректировок: кто, когда и что поменял.

Требование приёмки по интерфейсу. Журнал читается двумя способами:

* **по операциям** — одна строка на действие человека, включая массовую правку,
  которая сама по себе порождает сотни записей;
* **по ячейкам** — отдельные изменения с прежним и новым значением.

Записи приходят из таблицы, которую наполняют пользователи, поэтому содержимое
полей ``author`` в интерфейсе выводится как текст, а не как разметка.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from ..models import AdjustmentLog, Category, Product, Store, StoreProduct

#: Человеческие названия полей правки.
FIELD_LABELS = {
    "manual_forecast": "Прогноз",
    "manual_adapted": "Адаптированный прогноз",
    "diff_percent": "DIFF",
}


@dataclass
class JournalFilters:
    store_id: int | None = None
    category_id: int | None = None
    pair_id: int | None = None
    search: str | None = None
    week_from: str | None = None
    week_to: str | None = None
    field: str | None = None
    author: str | None = None
    operation: str | None = None
    batch_id: str | None = None


@dataclass
class JournalEntry:
    id: int
    pair_id: int
    store_name: str
    vendor_code: str
    title: str
    category: str | None
    week: str
    field: str
    field_label: str
    operation: str
    batch_id: str | None
    old_value: float | None
    new_value: float | None
    author: str | None
    created_at: datetime


@dataclass
class JournalBatch:
    batch_id: str
    operation: str
    author: str | None
    created_at: datetime
    cells: int
    pairs: int
    week_from: str
    week_to: str


def _filtered(stmt: Select, filters: JournalFilters) -> Select:
    """Общие условия для записей и для групп."""
    stmt = (
        stmt.join(StoreProduct, StoreProduct.id == AdjustmentLog.store_product_id)
        .join(Store, Store.id == StoreProduct.store_id)
        .join(Product, Product.id == StoreProduct.product_id)
        .outerjoin(Category, Category.id == Product.category_id)
    )
    if filters.store_id:
        stmt = stmt.where(StoreProduct.store_id == filters.store_id)
    if filters.category_id:
        stmt = stmt.where(Product.category_id == filters.category_id)
    if filters.pair_id:
        stmt = stmt.where(AdjustmentLog.store_product_id == filters.pair_id)
    if filters.search:
        pattern = f"%{filters.search.strip().lower()}%"
        stmt = stmt.where(
            func.lower(Product.title).like(pattern) | func.lower(Product.vendor_code).like(pattern)
        )
    if filters.week_from:
        stmt = stmt.where(AdjustmentLog.week >= filters.week_from)
    if filters.week_to:
        stmt = stmt.where(AdjustmentLog.week <= filters.week_to)
    if filters.field:
        stmt = stmt.where(AdjustmentLog.field == filters.field)
    if filters.author:
        stmt = stmt.where(
            func.lower(func.coalesce(AdjustmentLog.author, "")).like(
                f"%{filters.author.strip().lower()}%"
            )
        )
    if filters.operation:
        stmt = stmt.where(AdjustmentLog.operation == filters.operation)
    if filters.batch_id:
        stmt = stmt.where(AdjustmentLog.batch_id == filters.batch_id)
    return stmt


def count_entries(session: Session, filters: JournalFilters) -> int:
    stmt = _filtered(select(func.count(AdjustmentLog.id)), filters)
    return int(session.scalar(stmt) or 0)


def list_entries(
    session: Session,
    filters: JournalFilters,
    limit: int = 100,
    offset: int = 0,
) -> list[JournalEntry]:
    stmt = _filtered(
        select(
            AdjustmentLog,
            Store.name,
            Product.vendor_code,
            Product.title,
            Category.name,
        ),
        filters,
    ).order_by(AdjustmentLog.created_at.desc(), AdjustmentLog.id.desc())

    rows = session.execute(stmt.limit(limit).offset(offset)).all()
    return [
        JournalEntry(
            id=log.id,
            pair_id=log.store_product_id,
            store_name=store_name,
            vendor_code=vendor_code,
            title=title,
            category=category,
            week=log.week,
            field=log.field,
            field_label=FIELD_LABELS.get(log.field, log.field),
            operation=log.operation,
            batch_id=log.batch_id,
            old_value=log.old_value,
            new_value=log.new_value,
            author=log.author,
            created_at=log.created_at,
        )
        for log, store_name, vendor_code, title, category in rows
    ]


def count_batches(session: Session, filters: JournalFilters) -> int:
    inner = (
        _filtered(select(AdjustmentLog.batch_id), filters)
        .where(AdjustmentLog.batch_id.is_not(None))
        .group_by(AdjustmentLog.batch_id)
        .subquery()
    )
    return int(session.scalar(select(func.count()).select_from(inner)) or 0)


def list_batches(
    session: Session,
    filters: JournalFilters,
    limit: int = 50,
    offset: int = 0,
) -> list[JournalBatch]:
    """Массовые правки, свёрнутые в одну строку на операцию.

    Поячеечные правки сюда не попадают: у них нет ``batch_id``, и сворачивать
    их не во что — для них есть режим «по ячейкам».
    """
    stmt = (
        _filtered(
            select(
                AdjustmentLog.batch_id,
                func.min(AdjustmentLog.operation),
                func.min(AdjustmentLog.author),
                func.min(AdjustmentLog.created_at),
                func.count(AdjustmentLog.id),
                func.count(func.distinct(AdjustmentLog.store_product_id)),
                func.min(AdjustmentLog.week),
                func.max(AdjustmentLog.week),
            ),
            filters,
        )
        .where(AdjustmentLog.batch_id.is_not(None))
        .group_by(AdjustmentLog.batch_id)
        .order_by(func.min(AdjustmentLog.created_at).desc())
    )

    rows = session.execute(stmt.limit(limit).offset(offset)).all()
    return [
        JournalBatch(
            batch_id=batch_id,
            operation=operation,
            author=author,
            created_at=created_at,
            cells=cells,
            pairs=pairs,
            week_from=week_from,
            week_to=week_to,
        )
        for (
            batch_id,
            operation,
            author,
            created_at,
            cells,
            pairs,
            week_from,
            week_to,
        ) in rows
    ]
