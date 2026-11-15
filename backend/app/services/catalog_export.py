"""Выгрузка каталога в книгу Excel — она же шаблон загрузки (задача B-15).

Отдельного пустого шаблона нет намеренно: шаблон, который живёт сам по себе,
рано или поздно разойдётся с тем, что система умеет читать. Здесь одна книга
с одними колонками: на пустой базе в ней только шапка — это и есть шаблон,
на наполненной в ней весь каталог, который можно поправить в Excel и загрузить
обратно.

Строка — товар в кабинете: номер номенклатуры живёт на паре, и один артикул
в трёх кабинетах занимает три строки. Товар, не заведённый ни в один кабинет,
тоже попадает в файл — строкой с пустыми колонками кабинета, иначе выгрузка
молча теряла бы часть номенклатуры.
"""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO

from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..models import Product, Store, StoreProduct
from .catalog_import import EXPORT_COLUMNS
from .xlsx import MONEY, UNITS, VOLUME, Column, data_row, header_row, safe_filename

#: Ширина и формат колонок по их полю. Остальные — текст обычной ширины.
WIDTHS: dict[str, int] = {
    "vendor_code": 18,
    "title": 40,
    "category": 18,
    "niche": 16,
    "supplier": 22,
    "store": 18,
    "nm_id": 18,
    "barcode": 16,
}
FORMATS: dict[str, str] = {
    "cost_price": MONEY,
    "price": MONEY,
    "volume_m3": VOLUME,
    "base_potential": UNITS,
}


def catalog_filename(now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    return safe_filename(f"Номенклатура {moment:%Y-%m-%d}")


def _columns() -> list[Column]:
    return [
        Column(title=title, width=WIDTHS.get(field, 14), number_format=FORMATS.get(field))
        for title, field in EXPORT_COLUMNS
    ]


def _values(product: Product, pair: StoreProduct | None, store: Store | None) -> list[object]:
    source: dict[str, object] = {
        "vendor_code": product.vendor_code,
        "title": product.title,
        "category": product.category.name if product.category else None,
        "niche": product.category.niche if product.category else None,
        "supplier": product.supplier.name if product.supplier else None,
        "cost_price": product.cost_price,
        "price": product.price,
        "volume_m3": product.volume_m3,
        "lead_time_days": product.lead_time_days,
        "moq": product.moq,
        "pack_size": product.pack_size,
        "coverage_weeks": product.coverage_weeks,
        "safety_weeks": product.safety_weeks,
        "store": store.code if store else None,
        "nm_id": pair.nm_id if pair else None,
        "barcode": pair.barcode if pair else None,
        "base_potential": pair.base_potential if pair else None,
    }
    return [source[field] for _, field in EXPORT_COLUMNS]


def build_catalog_workbook(session: Session) -> bytes:
    """Весь каталог одной таблицей. Пустая база даёт книгу из одной шапки."""
    products = list(
        session.scalars(
            select(Product)
            .options(selectinload(Product.category), selectinload(Product.supplier))
            .order_by(Product.vendor_code)
        ).all()
    )
    pairs: dict[int, list[StoreProduct]] = {}
    for pair in session.scalars(select(StoreProduct).order_by(StoreProduct.store_id)).all():
        pairs.setdefault(pair.product_id, []).append(pair)
    stores = {store.id: store for store in session.scalars(select(Store)).all()}

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Номенклатура"

    columns = _columns()
    # Шапка стоит первой строкой, без заголовка и блока подписей: этот файл не
    # только читают, но и заполняют, в том числе вставкой из чужой выгрузки.
    header_row(sheet, 1, columns)
    sheet.freeze_panes = sheet.cell(row=2, column=1)

    line = 2
    for product in products:
        own = pairs.get(product.id, [])
        if not own:
            data_row(sheet, line, columns, _values(product, None, None))
            line += 1
            continue
        for pair in own:
            data_row(sheet, line, columns, _values(product, pair, stores.get(pair.store_id)))
            line += 1

    sheet.auto_filter.ref = f"A1:{sheet.cell(row=1, column=len(columns)).coordinate}"

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
