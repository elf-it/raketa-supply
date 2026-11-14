"""Выгрузка матрицы и закупочного плана в XLSX.

Переходный период неизбежен: пока система не проверена на боевых данных, заказчик
будет сверять её с действующими Google Sheets, а часть людей ещё какое-то время
останется в таблицах. Поэтому выгружается не «отчёт», а ровно то, что на экране:
те же строки, те же недели, те же показатели, посчитанные тем же ядром.

Строки не склеиваются вертикально, хотя на экране показатели одной позиции сидят
под одним названием: объединённые ячейки ломают сортировку и автофильтр, а файл
открывают именно чтобы отфильтровать и отсортировать. Поэтому левые колонки
повторяются в каждой строке показателя, зато над таблицей стоит автофильтр.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from io import BytesIO

from openpyxl import Workbook
from openpyxl.utils import get_column_letter

from ..calc import Level, Status
from .matrix import Matrix, Row
from .purchase import PurchaseRow
from .xlsx import (
    CENTER,
    CLOSED_FILL,
    HEADER_FILL,
    MONEY,
    PERCENT,
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

LEVEL_LABELS: dict[Level, str] = {
    Level.STORE: "по магазинам",
    Level.CATEGORY: "по категориям",
    Level.SKU: "по артикулам",
}

STATUS_LABELS: dict[Status, str] = {
    Status.OK: "норма",
    Status.SHORT: "дефицит",
    Status.OVER: "избыток",
}

#: Показатель, подпись в файле, формат. Порядок — как в таблице на экране,
#: но выгружаются все показатели: ширина листа ничем не ограничена.
METRIC_ROWS: list[tuple[str, str, str]] = [
    ("original_forecast", "План / исходный прогноз", UNITS),
    ("adapted_forecast", "Адаптированный прогноз", UNITS),
    ("fact_sales", "Факт продаж", UNITS),
    ("diff_percent", "DIFF, %", PERCENT),
    ("real_stock", "Реальный сток", UNITS),
    ("adapted_stock", "Адаптированный сток", UNITS),
    ("supply", "Поставки", UNITS),
    ("shortage_units", "Недопоставленный спрос", UNITS),
]

WEEK_WIDTH = 9


def matrix_filename(matrix: Matrix, today: date | None = None) -> str:
    return safe_filename(
        f"Матрица {LEVEL_LABELS[matrix.level]} {(today or date.today()).isoformat()}"
    )


def purchase_filename(today: date | None = None) -> str:
    return safe_filename(f"Закупочная потребность {(today or date.today()).isoformat()}")


# ------------------------------------------------------------------------------ матрица


def _left_columns(level: Level) -> list[Column]:
    if level is Level.SKU:
        return [
            Column("Магазин", 16),
            Column("Артикул", 16),
            Column("Товар", 40),
            Column("Категория", 20),
            Column("Статус", 10),
            Column("Показатель", 26),
        ]
    head = "Магазин" if level is Level.STORE else "Категория"
    return [
        Column(head, 28),
        Column("SKU", 7, UNITS),
        Column("Статус", 10),
        Column("Показатель", 26),
    ]


def _left_values(level: Level, row: Row, metric_label: str) -> list[object]:
    if level is Level.SKU:
        return [
            row.meta.get("store_name"),
            row.meta.get("vendor_code"),
            row.title,
            row.meta.get("category"),
            STATUS_LABELS[row.status],
            metric_label,
        ]
    return [
        row.title,
        row.meta.get("sku_count"),
        STATUS_LABELS[row.status],
        metric_label,
    ]


def _cell_value(metric: str, raw: object) -> float | None:
    if not isinstance(raw, int | float) or isinstance(raw, bool):
        return None
    # Округление до того же, что показывает экран: иначе в файле всплывают хвосты
    # двоичных дробей, которых в интерфейсе никто не видел.
    return round(float(raw), 1) if metric == "diff_percent" else round(float(raw))


def _month_spans(weeks: list[dict]) -> list[tuple[str, int]]:
    spans: list[tuple[str, int]] = []
    for week in weeks:
        if spans and spans[-1][0] == week["month_label"]:
            spans[-1] = (spans[-1][0], spans[-1][1] + 1)
        else:
            spans.append((week["month_label"], 1))
    return spans


def _write_block(
    sheet, start: int, level: Level, row: Row, weeks: list[dict], first_week_column: int
) -> int:
    """Пишет позицию: по строке на каждый показатель. Возвращает следующую строку."""
    left = _left_columns(level)
    for offset, (metric, label, number_format) in enumerate(METRIC_ROWS):
        line = start + offset
        data_row(sheet, line, left, _left_values(level, row, label))
        values = row.metrics.get(metric, {})
        for index, week in enumerate(weeks):
            put(
                sheet,
                line,
                first_week_column + index,
                _cell_value(metric, values.get(week["key"])),
                number_format,
                fill=CLOSED_FILL if week["is_closed"] else None,
            )
    return start + len(METRIC_ROWS)


#: Как выглядит отметка о моменте выгрузки. Время нужно обратной загрузке (G-9):
#: по нему видно, не поменял ли кто-то эти недели, пока файл правили. Зона явная
#: и всегда UTC — в базе отметки журнала тоже в UTC, и сравнивать их с местным
#: временем сервера значило бы ошибаться на часовой пояс в опасную сторону.
#: Секунды здесь не педантизм: отметка сравнивается с журналом правок, и округление
#: до минуты превращало бы чужую правку, сделанную в ту же минуту, в ложный конфликт.
EXPORT_STAMP_LABEL = "Выгружено"
EXPORT_STAMP_FORMAT = "%d.%m.%Y %H:%M:%S"
EXPORT_STAMP_ZONE = "UTC"


def export_stamp(moment: datetime) -> str:
    return f"{moment.strftime(EXPORT_STAMP_FORMAT)} {EXPORT_STAMP_ZONE}"


def week_header(key: str) -> str:
    """Заголовок колонки недели: «W46» и год под ним, как в таблице на экране."""
    year, number = key.split("-W")
    return f"W{number}\n{year}"


def build_matrix_workbook(
    matrix: Matrix,
    store_name: str | None = None,
    category_name: str | None = None,
    now: datetime | None = None,
) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Матрица"

    weeks = matrix.weeks
    left = _left_columns(matrix.level)
    first_week_column = len(left) + 1

    title_row(sheet, f"Недельная матрица {LEVEL_LABELS[matrix.level]}")
    picked = matrix.context["filters"].get("status")
    months_row = (
        meta_block(
            sheet,
            3,
            [
                (EXPORT_STAMP_LABEL, export_stamp(now or datetime.now(UTC))),
                ("Магазин", store_name or "все магазины"),
                ("Категория", category_name or "все ниши и категории"),
                ("Поиск", matrix.context["filters"].get("search")),
                # Без этой строки по файлу из трёх строк не понять, потерялись ли
                # остальные позиции или их скрыл отбор.
                (
                    "Отбор по статусу",
                    f"{STATUS_LABELS[Status(picked)]}"
                    f" · {matrix.context['sku_count']} из {matrix.context['sku_count_total']}"
                    if picked
                    else None,
                ),
                ("Последняя закрытая неделя", matrix.context["last_closed_week"]),
                ("Горизонт планирования", f"{matrix.context['horizon_weeks']} нед."),
                ("Позиций в выборке", matrix.context["sku_count"]),
                ("Закрытые недели", "выделены серым: это факт, а не план"),
            ],
        )
        + 1
    )

    # Месяцы стоят над неделями отдельной строкой, а не объединены с шапкой таблицы:
    # автофильтр работает по одной строке, и это должна быть строка заголовков.
    column = first_week_column
    for label, span in _month_spans(weeks):
        cell = put(sheet, months_row, column, label, bold=True, fill=HEADER_FILL)
        cell.alignment = CENTER
        if span > 1:
            sheet.merge_cells(
                start_row=months_row,
                start_column=column,
                end_row=months_row,
                end_column=column + span - 1,
            )
        column += span

    head = months_row + 1
    header_row(sheet, head, left)
    for index, week in enumerate(weeks):
        cell = put(
            sheet,
            head,
            first_week_column + index,
            # Год стоит в самой ячейке, а не только в строке месяцев: номера недель
            # начинаются заново, и на длинном горизонте «W02» без года не опознать —
            # ни глазами, ни обратной загрузкой. Год берётся из ключа недели,
            # а не из даты её понедельника: неделя 2027-W01 начинается в декабре
            # 2026-го, и по дате файл вернулся бы не в ту неделю.
            week_header(week["key"]),
            bold=True,
            fill=CLOSED_FILL if week["is_closed"] else HEADER_FILL,
        )
        cell.alignment = CENTER
        sheet.column_dimensions[get_column_letter(first_week_column + index)].width = WEEK_WIDTH

    line = head + 1
    for row in matrix.rows:
        line = _write_block(sheet, line, matrix.level, row, weeks, first_week_column)

    last_data_line = line - 1
    if matrix.rows:
        # Фильтр не захватывает итоги: иначе они уезжают при сортировке.
        sheet.auto_filter.ref = (
            f"A{head}:{get_column_letter(first_week_column + len(weeks) - 1)}{last_data_line}"
        )

    totals_line = line + 1
    _write_block(sheet, totals_line, matrix.level, matrix.totals, weeks, first_week_column)

    sheet.freeze_panes = sheet.cell(row=head + 1, column=first_week_column)

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------- закупочный план

PURCHASE_COLUMNS = [
    Column("№", 5),
    Column("Магазин", 16),
    Column("Артикул", 16),
    Column("Товар", 40),
    Column("Категория", 20),
    Column("Поставщик", 24),
    Column("Остаток сейчас", 15, UNITS),
    Column("Дефицит с недели", 17),
    Column("Заказать до", 13),
    Column("Неделя прихода", 16),
    Column("Количество", 13, UNITS),
    Column("До ограничений", 16, UNITS),
    Column("Спрос окна", 13, UNITS),
    Column("Страховой запас", 17, UNITS),
    Column("Остаток к началу окна", 22, UNITS),
    Column("В пути", 10, UNITS),
    Column("Окно покрытия", 20),
    Column("Цена закупки, ₽", 17, MONEY),
    Column("Сумма, ₽", 15, MONEY),
    Column("Объём, м³", 12, VOLUME),
    Column("Срок поставки, дн.", 18, UNITS),
    Column("MOQ", 8, UNITS),
    Column("Кратность", 11, UNITS),
    Column("Просрочено", 12),
    Column("Комментарий", 46),
]


def _purchase_values(number: int, row: PurchaseRow) -> list[object]:
    s = row.suggestion
    window = f"{s.coverage_from}…{s.coverage_to}" if s.needed and s.coverage_from else None
    return [
        number,
        row.store_name,
        row.vendor_code,
        row.title,
        row.category,
        row.supplier,
        round(row.current_stock),
        s.shortage_week,
        # Настоящая дата, а не строка: иначе Excel не отсортирует и не отфильтрует срок.
        s.order_date,
        s.arrival_week,
        s.qty if s.needed else None,
        round(s.qty_before_constraints) if s.needed else None,
        round(s.window_demand) if s.needed else None,
        round(s.safety_stock) if s.needed else None,
        round(s.stock_before) if s.needed else None,
        round(s.window_transit) if s.needed else None,
        window,
        round(row.params.cost_price, 2),
        round(s.cost, 2) if s.needed else None,
        round(s.volume_m3, 3) if s.needed else None,
        row.params.lead_time_days,
        row.params.moq,
        row.params.pack_size,
        "да" if s.is_overdue and s.needed else None,
        s.reason or None,
    ]


def build_purchase_workbook(
    rows: list[PurchaseRow],
    only_needed: bool,
    store_name: str | None = None,
    category_name: str | None = None,
    today: date | None = None,
) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Закупка"

    needed = [row for row in rows if row.suggestion.needed]
    title_row(sheet, "Закупочная потребность")
    head = (
        meta_block(
            sheet,
            3,
            [
                ("Выгружено", (today or date.today()).strftime("%d.%m.%Y")),
                ("Магазин", store_name or "все магазины"),
                ("Категория", category_name or "все ниши и категории"),
                ("Отбор", "только позиции к заказу" if only_needed else "все позиции"),
                ("Позиций к заказу", len(needed)),
                ("Просрочено", sum(1 for row in needed if row.suggestion.is_overdue)),
            ],
        )
        + 1
    )
    header_row(sheet, head, PURCHASE_COLUMNS)

    ordered = sorted(
        rows,
        key=lambda row: (
            row.suggestion.order_date or date.max,
            row.store_name,
            row.vendor_code,
        ),
    )
    for number, row in enumerate(ordered, start=1):
        line = head + number
        data_row(sheet, line, PURCHASE_COLUMNS, _purchase_values(number, row))
        sheet.cell(row=line, column=9).number_format = "DD.MM.YYYY"

    total_line = head + len(ordered) + 1
    put(sheet, total_line, 10, "Итого", bold=True)
    totals = {
        11: sum(row.suggestion.qty for row in needed),
        19: round(sum(row.suggestion.cost for row in needed), 2),
        20: round(sum(row.suggestion.volume_m3 for row in needed), 3),
    }
    for column, value in totals.items():
        put(sheet, total_line, column, value, PURCHASE_COLUMNS[column - 1].number_format, bold=True)

    if ordered:
        sheet.auto_filter.ref = (
            f"A{head}:{get_column_letter(len(PURCHASE_COLUMNS))}{head + len(ordered)}"
        )
    sheet.freeze_panes = sheet.cell(row=head + 1, column=5)

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
