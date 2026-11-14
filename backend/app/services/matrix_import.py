"""Загрузка правок прогноза из выгруженной матрицы (задача G-9).

Выгрузка была односторонней: файл открывали, правили в нём прогноз и переносили
числа обратно руками. Эта обратная дорога читает тот же файл, который отдаёт
система, и возвращает из него ровно одну строку показателя — «План / исходный
прогноз». Остальные строки производные: факт из маркетплейса, сток и DIFF —
следствие прогноза, и принимать их обратно означало бы спорить с расчётом.

**Загружается только уровень артикулов.** У строки магазина или категории нет
однозначного разложения на позиции: поставить группе «на сто штук больше» можно
десятком способов, и выбирать за человека система не должна.

**Берутся только изменившиеся ячейки.** В файле стоит весь прогноз, а не только
исправленное, и если принять его целиком, каждая ячейка выборки превратится
в ручную правку — весь расчёт по потенциалу и сезонности встанет намертво.
Поэтому значение сравнивается с тем, что система показывает сейчас, и в работу
идёт только расхождение.

**Чужие правки не затираются молча.** Расхождение значит одно из двух: человек
исправил ячейку в файле или кто-то поменял её в системе, пока файл правили.
Различить это по самому числу нельзя, поэтому в файле стоит время выгрузки:
если после него в журнале есть правка этой ячейки, расхождение считается
конфликтом. Конфликты показываются с автором и временем, но не применяются —
**[ДОПУЩЕНИЕ, Q-13]**: пока неизвестно, как заказчик собирается жить переходный
период, «последний не выигрывает» — единственное правило, которое не теряет
работу молча.

Чего эта проверка не ловит: пересчёт. Если после выгрузки поменяли базовый
потенциал или сезонность, расчётное значение сдвинулось само, записи в журнале
нет, и старое число из файла встанет ручной правкой. Такая правка видна
в журнале и снимается кнопкой «Сбросить к формуле».
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from io import BytesIO
from uuid import uuid4

from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..calc import original_forecast
from ..models import AdjustmentLog, Product, Store, StoreProduct
from .adjustments import MAX_BULK_CELLS, set_forecast_cells
from .matrix import (
    MatrixFilters,
    build_series_inputs,
    load_app_settings,
    load_pairs,
    resolve_last_closed_week,
)
from .matrix_export import EXPORT_STAMP_FORMAT, EXPORT_STAMP_LABEL, EXPORT_STAMP_ZONE

#: Операция в журнале. Отличать загрузку от правки руками важно: это разные
#: ответы на вопрос «откуда взялось это число».
OPERATION = "import"

TITLE_PREFIX = "Недельная матрица"
FORECAST_LABEL = "План / исходный прогноз"
#: Левые колонки выгрузки по артикулам. По ним же отличается уровень файла:
#: у магазинов и категорий набор другой.
SKU_COLUMNS = ("Магазин", "Артикул", "Товар", "Категория", "Статус", "Показатель")
HEADER_MARKER = "Показатель"

#: Потолок на размер файла. Выгрузка на тысячу позиций и полсотни недель —
#: около трёх мегабайт, так что это запас, а не ограничение.
MAX_FILE_BYTES = 10 * 1024 * 1024

WEEK_HEADER = re.compile(r"^W\s*(\d{1,2})\s+(\d{4})$")


class MatrixImportError(ValueError):
    """Файл не годится в работу целиком: не та книга, не тот уровень, нет отметки."""


@dataclass(frozen=True)
class Change:
    """Ячейка, которую файл меняет."""

    pair_id: int
    store_name: str
    vendor_code: str
    title: str
    week: str
    old_value: float
    new_value: float


@dataclass(frozen=True)
class Conflict:
    """Правка, которую не применяем: ячейку уже изменили после выгрузки."""

    pair_id: int
    store_name: str
    vendor_code: str
    title: str
    week: str
    old_value: float
    new_value: float
    changed_at: datetime
    changed_by: str | None


@dataclass
class Issue:
    """Что из файла не взяли и почему. С примерами: счётчик без них ничего не говорит."""

    reason: str
    count: int = 0
    examples: list[str] = field(default_factory=list)

    def add(self, example: str) -> None:
        self.count += 1
        if len(self.examples) < 5:
            self.examples.append(example)


@dataclass
class ImportReport:
    exported_at: datetime
    rows_read: int = 0
    cells_read: int = 0
    changes: list[Change] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    applied: bool = False
    applied_cells: int = 0
    batch_id: str | None = None


# ----------------------------------------------------------------------- чтение книги


@dataclass(frozen=True)
class FileRow:
    store_name: str
    vendor_code: str
    values: dict[str, object]


@dataclass(frozen=True)
class ParsedFile:
    exported_at: datetime
    weeks: list[str]
    rows: list[FileRow]


def _text(value: object) -> str:
    """Текст ячейки. Артикул из таблицы может вернуться числом, и это тот же артикул."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _parse_stamp(value: object) -> datetime:
    text = _text(value).replace(EXPORT_STAMP_ZONE, "").strip()
    try:
        return datetime.strptime(text, EXPORT_STAMP_FORMAT)
    except ValueError as error:
        raise MatrixImportError(
            f"В файле нет времени выгрузки в поле «{EXPORT_STAMP_LABEL}». "
            "Без него не проверить, не изменил ли кто-то эти недели, пока файл правили. "
            "Выгрузите матрицу заново и правьте свежий файл"
        ) from error


def read_matrix_file(data: bytes) -> ParsedFile:
    """Разбирает книгу. Ошибки здесь — про файл целиком, а не про отдельные ячейки."""
    if not data:
        raise MatrixImportError("Файл пустой")
    if len(data) > MAX_FILE_BYTES:
        raise MatrixImportError(
            f"Файл больше {MAX_FILE_BYTES // (1024 * 1024)} МБ — это не выгрузка матрицы"
        )
    try:
        workbook = load_workbook(BytesIO(data), data_only=True, read_only=True)
    except Exception as error:  # noqa: BLE001 — openpyxl бросает что угодно
        raise MatrixImportError(
            "Файл не открывается как книга Excel (.xlsx). Если правили в Google Sheets, "
            "выгрузите оттуда именно .xlsx, а не .csv"
        ) from error

    try:
        sheet = workbook.worksheets[0]
        grid = [list(row) for row in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()

    if not grid or not _text(grid[0][0] if grid[0] else None).startswith(TITLE_PREFIX):
        raise MatrixImportError(
            f"Это не выгрузка матрицы: в первой ячейке ожидается «{TITLE_PREFIX} …». "
            "Загружать нужно файл, скачанный кнопкой «Скачать .xlsx» на экране матрицы"
        )

    stamp: object = None
    header_line = -1
    for index, row in enumerate(grid[1:30], start=1):
        label = _text(row[0] if row else None)
        if label == EXPORT_STAMP_LABEL and len(row) > 2:
            stamp = row[2]
        if HEADER_MARKER in [_text(cell) for cell in row[: len(SKU_COLUMNS)]]:
            header_line = index
            break
    if header_line < 0:
        raise MatrixImportError("В файле не нашлась строка заголовков таблицы")

    head = grid[header_line]
    left = tuple(_text(cell) for cell in head[: len(SKU_COLUMNS)])
    if left != SKU_COLUMNS:
        raise MatrixImportError(
            "Загружается только выгрузка по артикулам: у магазинов и категорий "
            "строка — это группа позиций, и разложить её правку обратно по позициям "
            "система за вас не может"
        )

    weeks: list[str] = []
    for cell in head[len(SKU_COLUMNS) :]:
        match = WEEK_HEADER.match(_text(cell).replace("\n", " ").strip())
        if not match:
            break
        number, year = match.groups()
        weeks.append(f"{year}-W{int(number):02d}")
    if not weeks:
        raise MatrixImportError(
            "В файле не нашлось ни одной колонки недели вида «W46 2026». "
            "Похоже, это выгрузка старой версии системы — скачайте матрицу заново"
        )

    first_week = len(SKU_COLUMNS)
    rows: list[FileRow] = []
    for row in grid[header_line + 1 :]:
        if len(row) <= first_week or _text(row[5]) != FORECAST_LABEL:
            continue
        vendor_code = _text(row[1])
        if not vendor_code:
            # Строка итогов: у неё нет артикула, и правке она не подлежит.
            continue
        values = {
            week: row[first_week + index] if first_week + index < len(row) else None
            for index, week in enumerate(weeks)
        }
        rows.append(FileRow(store_name=_text(row[0]), vendor_code=vendor_code, values=values))

    return ParsedFile(exported_at=_parse_stamp(stamp), weeks=weeks, rows=rows)


# -------------------------------------------------------------------------- разбор правок


def _pairs_by_key(session: Session) -> dict[tuple[str, str], int]:
    rows = session.execute(
        select(Store.name, Product.vendor_code, StoreProduct.id)
        .join(Store, Store.id == StoreProduct.store_id)
        .join(Product, Product.id == StoreProduct.product_id)
    )
    return {(store.strip().lower(), code.strip().lower()): pair_id for store, code, pair_id in rows}


def _later_changes(
    session: Session, pair_ids: set[int], moment: datetime
) -> dict[tuple[int, str], AdjustmentLog]:
    """Последняя правка каждой ячейки после момента выгрузки."""
    if not pair_ids:
        return {}
    logs = session.scalars(
        select(AdjustmentLog)
        .where(
            AdjustmentLog.store_product_id.in_(pair_ids),
            AdjustmentLog.created_at > moment,
        )
        .order_by(AdjustmentLog.created_at)
    ).all()
    return {(log.store_product_id, log.week): log for log in logs}


def analyze(session: Session, data: bytes, today: date | None = None) -> ImportReport:
    """Что файл изменит, если его применить. Ничего не пишет."""
    parsed = read_matrix_file(data)
    report = ImportReport(exported_at=parsed.exported_at, rows_read=len(parsed.rows))

    unknown = Issue("позиция не найдена в системе")
    closed = Issue("неделю закрыли — правка из файла не применяется")
    bad = Issue("значение не похоже на штуки")

    settings = load_app_settings(session)
    last_closed = resolve_last_closed_week(settings, today)
    known = _pairs_by_key(session)

    # Пары из файла, которые вообще есть в системе.
    wanted: dict[int, FileRow] = {}
    for row in parsed.rows:
        pair_id = known.get((row.store_name.strip().lower(), row.vendor_code.strip().lower()))
        if pair_id is None:
            unknown.add(f"{row.vendor_code} · {row.store_name}")
            continue
        wanted[pair_id] = row

    if wanted and parsed.weeks:
        pairs = [pair for pair in load_pairs(session, MatrixFilters()) if pair.id in wanted]
        # Недели берём все, включая закрытые: по ним видно, не правил ли человек
        # в файле неделю, которую закрыли уже после выгрузки. Молчать о такой
        # правке нельзя — она просто не применится.
        inputs = build_series_inputs(session, pairs, parsed.weeks, last_closed)
        titles = {pair.id: (pair.title, pair.store_name, pair.vendor_code) for pair in pairs}
        conflicting = _later_changes(session, set(wanted), parsed.exported_at)

        for pair in pairs:
            row = wanted[pair.id]
            title, store_name, vendor_code = titles[pair.id]
            for week in parsed.weeks:
                raw = row.values.get(week)
                if raw is None or _text(raw) == "":
                    continue
                if isinstance(raw, bool) or not isinstance(raw, int | float):
                    bad.add(f"{vendor_code} · {week}: {_text(raw)!r}")
                    continue
                if raw < 0:
                    # Отрицательный прогноз — опечатка: спрос не бывает ниже нуля.
                    bad.add(f"{vendor_code} · {week}: {_text(raw)}")
                    continue

                report.cells_read += 1
                current = original_forecast(inputs[pair.id], week)
                new_value = float(round(raw))
                # Сравнение в том же округлении, в каком число попало в файл:
                # иначе выгруженный «187» вернулся бы правкой на 186,8.
                if round(current) == round(new_value):
                    continue

                if week <= last_closed:
                    # Неделя закрыта: её план зафиксирован, а продажи уже случились.
                    # Говорим об этом только когда в файле правда другое число —
                    # закрытые недели есть в каждой выгрузке, и сообщать о них
                    # на каждой загрузке значило бы кричать без повода.
                    closed.add(f"{vendor_code} · {week}")
                    continue

                log = conflicting.get((pair.id, week))
                if log is not None:
                    report.conflicts.append(
                        Conflict(
                            pair_id=pair.id,
                            store_name=store_name,
                            vendor_code=vendor_code,
                            title=title,
                            week=week,
                            old_value=current,
                            new_value=new_value,
                            changed_at=log.created_at,
                            changed_by=log.author,
                        )
                    )
                    continue
                report.changes.append(
                    Change(
                        pair_id=pair.id,
                        store_name=store_name,
                        vendor_code=vendor_code,
                        title=title,
                        week=week,
                        old_value=current,
                        new_value=new_value,
                    )
                )

    report.issues = [issue for issue in (unknown, closed, bad) if issue.count]
    if len(report.changes) > MAX_BULK_CELLS:
        raise MatrixImportError(
            f"В файле {len(report.changes)} изменённых ячеек, предел одной правки — "
            f"{MAX_BULK_CELLS}. Разделите файл на части"
        )
    return report


def apply_import(
    session: Session,
    data: bytes,
    *,
    author: str | None = None,
    today: date | None = None,
) -> ImportReport:
    """Применяет правки из файла. Конфликты остаются непримененными."""
    report = analyze(session, data, today)
    if not report.changes:
        return report

    batch_id = uuid4().hex
    report.applied_cells = set_forecast_cells(
        session,
        [(change.pair_id, change.week, change.new_value) for change in report.changes],
        operation=OPERATION,
        author=author,
        batch_id=batch_id,
    )
    report.applied = True
    report.batch_id = batch_id
    return report
