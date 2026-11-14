"""Выгрузка заказа поставщику в XLSX.

Заказ фиксируется в системе, но отправляют его за её пределы — почтой, в мессенджер,
в учётную систему поставщика. Нужен файл, который человек на той стороне откроет без
вопросов, поэтому это настоящая книга Excel, а не CSV: CSV в русском Excel открывается
то в одной кодировке, то в другой, и разделитель зависит от настроек системы.
"""

from __future__ import annotations

from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from ..models import PurchaseOrder
from .orders import STATUS_LABELS

#: Заголовок, ширина колонки, формат числа.
COLUMNS: list[tuple[str, int, str | None]] = [
    ("№", 5, None),
    ("Артикул", 18, None),
    ("Товар", 42, None),
    ("Магазин", 14, None),
    ("Неделя прихода", 16, None),
    ("Количество", 13, "# ##0"),
    ("Цена закупки, ₽", 17, "# ##0.00"),
    ("Сумма, ₽", 15, "# ##0.00"),
    ("Объём, м³", 12, "0.000"),
]

HEADER_FILL = PatternFill("solid", fgColor="EEF5F1")
THIN = Side(style="thin", color="C9CED2")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def order_filename(order: PurchaseOrder) -> str:
    """Имя файла для человека: по нему видно, что за заказ, не открывая."""
    supplier = (order.supplier.name if order.supplier else "без поставщика").strip()
    # Символы, на которых спотыкаются файловые системы и заголовок Content-Disposition.
    for bad in '\\/:*?"<>|\r\n':
        supplier = supplier.replace(bad, " ")
    return f"Заказ №{order.id} {supplier}.xlsx".strip()


def build_order_workbook(order: PurchaseOrder) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = f"Заказ {order.id}"

    sheet["A1"] = f"Заказ поставщику № {order.id}"
    sheet["A1"].font = Font(bold=True, size=14)

    # Дата без времени: отметки в базе хранятся в UTC, а у читателя файла свой пояс —
    # на уровне даты расхождение не играет роли, на уровне часов ввело бы в заблуждение.
    placed = order.placed_at or order.created_at
    meta = [
        ("Поставщик", order.supplier.name if order.supplier else "—"),
        ("Статус", STATUS_LABELS[order.status]),
        ("Дата заказа", placed.strftime("%d.%m.%Y")),
        ("Составил", order.created_by.display_name if order.created_by else "—"),
    ]
    if order.note:
        meta.append(("Примечание", order.note))

    row = 3
    for label, value in meta:
        sheet.cell(row=row, column=1, value=label).font = Font(bold=True)
        sheet.cell(row=row, column=3, value=value)
        row += 1

    head_row = row + 1
    for index, (title, width, _) in enumerate(COLUMNS, start=1):
        cell = sheet.cell(row=head_row, column=index, value=title)
        cell.font = Font(bold=True)
        cell.fill = HEADER_FILL
        cell.border = BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        sheet.column_dimensions[get_column_letter(index)].width = width

    lines = sorted(order.lines, key=lambda line: (line.arrival_week, line.id))
    for number, line in enumerate(lines, start=1):
        values = [
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
        ]
        for index, value in enumerate(values, start=1):
            cell = sheet.cell(row=head_row + number, column=index, value=value)
            cell.border = BORDER
            number_format = COLUMNS[index - 1][2]
            if number_format:
                cell.number_format = number_format

    total_row = head_row + len(lines) + 1
    sheet.cell(row=total_row, column=5, value="Итого").font = Font(bold=True)
    totals = {
        6: sum(line.qty for line in lines),
        8: round(sum(line.qty * line.cost_price for line in lines), 2),
        9: round(sum(line.qty * line.volume_m3 for line in lines), 3),
    }
    for column, value in totals.items():
        cell = sheet.cell(row=total_row, column=column, value=value)
        cell.font = Font(bold=True)
        cell.border = BORDER
        cell.number_format = COLUMNS[column - 1][2] or "General"

    # Шапка таблицы остаётся на месте при прокрутке длинного заказа.
    sheet.freeze_panes = sheet.cell(row=head_row + 1, column=1)

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
