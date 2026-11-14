"""Общие части книг Excel: стили, шапка, табличная механика.

Выгрузок в проекте уже несколько — заказ поставщику, матрица, закупочный план.
Рамки, заливка и ширина колонок у них одинаковые, и дублировать это в каждом
сборщике означает три разных файла с одинаковым смыслом и разной шириной колонок.
"""

from __future__ import annotations

from dataclasses import dataclass

from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

TITLE_FONT = Font(bold=True, size=14)
BOLD = Font(bold=True)

HEADER_FILL = PatternFill("solid", fgColor="EEF5F1")
#: Закрытая неделя — это факт, а не план; в файле их различает заливка.
CLOSED_FILL = PatternFill("solid", fgColor="F1F3F5")

THIN = Side(style="thin", color="C9CED2")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)

#: Форматы чисел, которыми пользуются все выгрузки.
UNITS = "# ##0"
MONEY = "# ##0.00"
VOLUME = "0.000"
PERCENT = "# ##0.0"


@dataclass(frozen=True)
class Column:
    """Колонка таблицы: заголовок, ширина и формат значений."""

    title: str
    width: int
    number_format: str | None = None


def safe_filename(stem: str) -> str:
    """Имя файла для человека, без символов, на которых спотыкаются ОС и заголовки."""
    for bad in '\\/:*?"<>|\r\n':
        stem = stem.replace(bad, " ")
    return f"{' '.join(stem.split())}.xlsx"


def title_row(sheet: Worksheet, text: str) -> None:
    sheet["A1"] = text
    sheet["A1"].font = TITLE_FONT


def meta_block(sheet: Worksheet, start_row: int, items: list[tuple[str, object]]) -> int:
    """Пишет блок «подпись — значение» и возвращает первую свободную строку."""
    row = start_row
    for label, value in items:
        if value is None or value == "":
            continue
        sheet.cell(row=row, column=1, value=label).font = BOLD
        sheet.cell(row=row, column=3, value=value)
        row += 1
    return row


def header_row(sheet: Worksheet, row: int, columns: list[Column], first: int = 1) -> None:
    """Шапка таблицы с заливкой, рамкой и шириной колонок."""
    for index, column in enumerate(columns, start=first):
        cell = sheet.cell(row=row, column=index, value=column.title)
        cell.font = BOLD
        cell.fill = HEADER_FILL
        cell.border = BORDER
        cell.alignment = CENTER
        sheet.column_dimensions[get_column_letter(index)].width = column.width


def put(
    sheet: Worksheet,
    row: int,
    column: int,
    value: object,
    number_format: str | None = None,
    bold: bool = False,
    fill: PatternFill | None = None,
):
    """Ячейка таблицы: рамка всегда, формат и выделение — по месту."""
    cell = sheet.cell(row=row, column=column, value=value)
    cell.border = BORDER
    if number_format:
        cell.number_format = number_format
    if bold:
        cell.font = BOLD
    if fill:
        cell.fill = fill
    return cell


def data_row(sheet: Worksheet, row: int, columns: list[Column], values: list[object]) -> None:
    """Строка таблицы по описанию колонок: формат берётся из колонки."""
    for index, (column, value) in enumerate(zip(columns, values, strict=True), start=1):
        put(sheet, row, index, value, column.number_format)
