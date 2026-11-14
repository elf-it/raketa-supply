"""Выгрузка заказа поставщику в XLSX.

Заказ фиксируется в системе, но отправляют его за её пределы — почтой, в мессенджер,
в учётную систему поставщика. Нужен файл, который человек на той стороне откроет без
вопросов, поэтому это настоящая книга Excel, а не CSV: CSV в русском Excel открывается
то в одной кодировке, то в другой, и разделитель зависит от настроек системы.
"""

from __future__ import annotations

from io import BytesIO

from openpyxl import Workbook

from ..models import PurchaseOrder
from .orders import STATUS_LABELS
from .xlsx import (
    BOLD,
    MONEY,
    UNITS,
    VOLUME,
    Column,
    data_row,
    header_row,
    meta_block,
    put,
    safe_filename,
    title_row,
)

COLUMNS = [
    Column("№", 5),
    Column("Артикул", 18),
    Column("Товар", 42),
    Column("Магазин", 14),
    Column("Неделя прихода", 16),
    Column("Количество", 13, UNITS),
    Column("Цена закупки, ₽", 17, MONEY),
    Column("Сумма, ₽", 15, MONEY),
    Column("Объём, м³", 12, VOLUME),
]


def order_filename(order: PurchaseOrder) -> str:
    """Имя файла для человека: по нему видно, что за заказ, не открывая."""
    supplier = order.supplier.name if order.supplier else "без поставщика"
    return safe_filename(f"Заказ №{order.id} {supplier}")


def build_order_workbook(order: PurchaseOrder) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = f"Заказ {order.id}"

    title_row(sheet, f"Заказ поставщику № {order.id}")

    # Дата без времени: отметки в базе хранятся в UTC, а у читателя файла свой пояс —
    # на уровне даты расхождение не играет роли, на уровне часов ввело бы в заблуждение.
    placed = order.placed_at or order.created_at
    head_row = (
        meta_block(
            sheet,
            3,
            [
                ("Поставщик", order.supplier.name if order.supplier else "—"),
                ("Статус", STATUS_LABELS[order.status]),
                ("Дата заказа", placed.strftime("%d.%m.%Y")),
                ("Составил", order.created_by.display_name if order.created_by else "—"),
                ("Примечание", order.note),
            ],
        )
        + 1
    )
    header_row(sheet, head_row, COLUMNS)

    lines = sorted(order.lines, key=lambda line: (line.arrival_week, line.id))
    for number, line in enumerate(lines, start=1):
        data_row(
            sheet,
            head_row + number,
            COLUMNS,
            [
                number,
                line.pair.product.vendor_code,
                line.pair.product.title,
                line.pair.store.name,
                line.arrival_week,
                line.qty,
                round(line.cost_price, 2),
                round(line.qty * line.cost_price, 2),
                # Без округления в ячейку попадает хвост двоичной дроби вроде 0.5399999999.
                round(line.qty * line.volume_m3, 3),
            ],
        )

    total_row = head_row + len(lines) + 1
    sheet.cell(row=total_row, column=5, value="Итого").font = BOLD
    totals = {
        6: sum(line.qty for line in lines),
        8: round(sum(line.qty * line.cost_price for line in lines), 2),
        9: round(sum(line.qty * line.volume_m3 for line in lines), 3),
    }
    for column, value in totals.items():
        put(sheet, total_row, column, value, COLUMNS[column - 1].number_format, bold=True)

    # Шапка таблицы остаётся на месте при прокрутке длинного заказа.
    sheet.freeze_panes = sheet.cell(row=head_row + 1, column=1)

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
