"""Сверка наших чисел с файлом заказчика (задача C-10).

Алгоритм DIFF и правила статусов в ТЗ не заданы: «точные формулы и исключения
оформляются отдельно». В системе они сделаны допущениями (вопрос Q-1), и пока
действующие таблицы заказчика не сверены с расчётом, это допущения и остаются.

Сверять глазами нечего: тысяча позиций на пятьдесят недель — это пятьдесят
тысяч чисел. Поэтому здесь не «сверка» как событие, а инструмент, который
её делает: файл заказчика против наших чисел, строка к строке.

Решения и почему так:

* **Ничего не пишется.** Сверка — это вопрос «почему у нас по-разному»,
  а не загрузка данных. Обратная загрузка правок прогноза есть отдельно
  (задача G-9) и спрашивает подтверждения; здесь её нет совсем, чтобы сверку
  можно было запускать без опаски хоть десять раз.

* **Расхождения разделяются по причине, а не считаются в одну кучу.** Число
  «разошлось 4 800 ячеек» не отвечает ни на один вопрос. Ячейка расходится
  по одной из трёх причин, и они требуют разного: у нас стоит ручная правка,
  у нас стоит DIFF, или расходится сама формула. Первые две — это наши же
  настройки, и к алгоритму они отношения не имеют. Сверять нужно третью.

* **Ищется общий множитель.** Если наши числа повсюду в одно и то же число
  раз отличаются от файла, дело не в алгоритме, а в коэффициенте — и это
  ответ на вопрос Q-1 за одну строку вместо месяца переписки. Поэтому
  считается медиана отношения и доля ячеек рядом с ней.

* **Формат файла — три колонки.** Артикул, неделя, значение; магазин, DIFF
  и статус — по желанию. Не потому, что так удобнее читать, а потому, что
  любую таблицу можно привести к трём колонкам, а угадывать устройство чужого
  файла, которого никто не видел, значит писать разбор под вымышленный формат.
  Книгу с неделями по горизонтали сверка узнаёт и говорит, что сделать.

Чего эта задача не делает: саму сверку. Файлов заказчика нет (вопрос Q-1),
и пока они не пришли, C-10 остаётся открытой — закрыть её может только прогон
на настоящих выгрузках.
"""

from __future__ import annotations

import csv
import io
import re
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime

from openpyxl import load_workbook
from sqlalchemy.orm import Session

from ..calc import Status, week_key
from .matrix import (
    MatrixFilters,
    build_week_axis,
    calc_settings,
    compute_results,
    load_app_settings,
    load_pairs,
    resolve_last_closed_week,
)

#: Потолок на размер файла: сверка читает его целиком в память.
MAX_FILE_BYTES = 20 * 1024 * 1024
#: Потолок на число строк — тот же порядок, что у загрузки номенклатуры.
MAX_ROWS = 200_000

#: Сколько расхождений показывать подробно. Остальное — в счётчиках: список
#: из пяти тысяч строк человек не читает, а причины и множитель видны и так.
MAX_LINES = 200

#: Допуск: ячейка считается совпавшей, если разница не больше штуки или одного
#: процента. Округление и копейки не должны выглядеть расхождением алгоритма.
TOLERANCE_UNITS = 1.0
TOLERANCE_SHARE = 0.01

#: Насколько близко к медиане должно лежать отношение, чтобы считаться «тем же»,
#: и какая доля ячеек делает множитель общим.
RATIO_BAND = 0.05
RATIO_SHARE = 0.8

#: Имена колонок, которые сверка узнаёт. Регистр и лишние пробелы не важны.
ALIASES: dict[str, tuple[str, ...]] = {
    "vendor_code": ("артикул", "артикул продавца", "код", "sku", "vendor_code", "vendorcode"),
    "nm_id": ("номенклатура", "артикул wb", "nmid", "nm_id", "нм", "нмид"),
    "store": ("магазин", "кабинет", "store", "склад"),
    "week": ("неделя", "week", "нед"),
    "forecast": ("прогноз", "план", "значение", "forecast", "plan", "value", "штук"),
    "diff": ("diff", "дифф", "дифференциал", "множитель", "коэффициент"),
    "status": ("статус", "status"),
}

#: Статусы в файле заказчика могут быть названы по-русски.
STATUS_WORDS = {
    "over": Status.OVER,
    "избыток": Status.OVER,
    "перезатарен": Status.OVER,
    "short": Status.SHORT,
    "дефицит": Status.SHORT,
    "нехватка": Status.SHORT,
    "ok": Status.OK,
    "норма": Status.OK,
    "в норме": Status.OK,
}

WEEK_FORMATS = (
    re.compile(r"^(?P<year>\d{4})[-\s]?W(?P<week>\d{1,2})$", re.IGNORECASE),
    re.compile(r"^W(?P<week>\d{1,2})[-\s/]+(?P<year>\d{4})$", re.IGNORECASE),
    re.compile(r"^(?P<week>\d{1,2})[-\s/]+(?P<year>\d{4})$"),
)

# Причины, по которым ячейка попадает в отчёт.
MATCH = "совпало"
OURS_MANUAL = "у нас ручная правка"
OURS_DIFF = "у нас DIFF не 100 %"
FORMULA = "расходится формула"
NO_PAIR = "позиция не найдена в системе"
NO_WEEK = "недели нет в горизонте"
BAD_VALUE = "значение не разобрать"


class ReconcileError(ValueError):
    """Файл не годится в работу целиком."""


@dataclass(frozen=True)
class TheirRow:
    """Строка файла заказчика."""

    line: int
    key: str
    store: str
    week: str
    forecast: float | None = None
    diff_percent: float | None = None
    status: str | None = None


@dataclass(frozen=True)
class Line:
    """Одно расхождение — так, чтобы человек нашёл его и у себя, и у нас."""

    vendor_code: str
    store_name: str
    title: str
    week: str
    ours: float
    theirs: float
    delta: float
    ratio: float | None
    reason: str
    line: int


@dataclass
class Reason:
    """Счётчик с примерами: число без примеров ничего не объясняет."""

    reason: str
    count: int = 0
    examples: list[str] = field(default_factory=list)

    def add(self, example: str) -> None:
        self.count += 1
        if len(self.examples) < 5:
            self.examples.append(example)


@dataclass
class ReconcileReport:
    rows_read: int = 0
    compared: int = 0
    matched: int = 0
    diverged: int = 0
    #: Расхождения по причинам.
    reasons: list[Reason] = field(default_factory=list)
    #: Самые крупные расхождения — подробно.
    lines: list[Line] = field(default_factory=list)
    #: Медиана отношения «наше / их» и доля ячеек рядом с ней.
    median_ratio: float | None = None
    ratio_share: float | None = None
    status_compared: int = 0
    status_mismatched: int = 0
    status_examples: list[str] = field(default_factory=list)
    #: Вывод словами — то, ради чего сверка и затевалась.
    verdict: str = ""


# ------------------------------------------------------------------- чтение файла


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value).strip()


def _number(value: object) -> float | None:
    """Число из ячейки: принимает запятую, пробелы разрядов и знак процента."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    text = _text(value).replace("%", "").replace(" ", "").replace(" ", "")
    text = text.replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_week(value: object) -> str | None:
    """Неделя из ячейки: ``2026-W46``, ``W46 2026``, ``46/2026`` или дата.

    Голый номер недели без года не принимается: угадывать год за человека
    в файле, который сверяют с расчётом, — худшее из возможных мест.
    """
    if isinstance(value, datetime | date):
        return week_key(value.date() if isinstance(value, datetime) else value)
    text = _text(value)
    if not text:
        return None
    for pattern in WEEK_FORMATS:
        match = pattern.match(text)
        if match:
            week = int(match.group("week"))
            if 1 <= week <= 53:
                return f"{match.group('year')}-W{week:02d}"
            return None
    try:
        return week_key(date.fromisoformat(text[:10]))
    except ValueError:
        return None


def _map_columns(head: list[str]) -> dict[str, int]:
    """Какая колонка где. Имя узнаётся по списку синонимов."""
    found: dict[str, int] = {}
    for index, raw in enumerate(head):
        name = _text(raw).lower().strip()
        if not name:
            continue
        for field_name, aliases in ALIASES.items():
            if field_name in found:
                continue
            if name in aliases or any(name.startswith(alias) for alias in aliases):
                found[field_name] = index
                break
    return found


def _looks_like_weeks(head: list[str]) -> int:
    return sum(1 for cell in head if parse_week(cell))


def _rows_from_grid(grid: list[list[object]]) -> list[TheirRow]:
    """Строки из таблицы: ищет заголовок, потом читает данные под ним."""
    columns: dict[str, int] = {}
    header_line = -1
    for index, row in enumerate(grid[:30]):
        head = [_text(cell) for cell in row]
        found = _map_columns(head)
        if "week" in found and "forecast" in found and ("vendor_code" in found or "nm_id" in found):
            columns, header_line = found, index
            break

    if header_line < 0:
        # Самая вероятная ошибка — отдали матрицу как она лежит в таблице.
        wide = max((_looks_like_weeks([_text(c) for c in row]) for row in grid[:30]), default=0)
        if wide >= 3:
            raise ReconcileError(
                "Похоже, в файле недели стоят колонками. Сверка ждёт три колонки: "
                "«Артикул», «Неделя», «Прогноз» — по строке на позицию и неделю. "
                "Любую таблицу можно привести к такому виду, а угадывать чужую "
                "раскладку сверка не станет: ошибка в разборе выглядела бы "
                "расхождением расчёта"
            )
        raise ReconcileError(
            "В файле не нашлось заголовков «Артикул», «Неделя» и «Прогноз» "
            "(подойдут и «Артикул продавца», «Номенклатура», «План», «Значение»). "
            "Заголовки ищутся в первых тридцати строках"
        )

    rows: list[TheirRow] = []
    for number, row in enumerate(grid[header_line + 1 :], start=header_line + 2):
        if len(rows) >= MAX_ROWS:
            raise ReconcileError(f"В файле больше {MAX_ROWS} строк — разделите его на части")

        def cell(name: str, line=row) -> object:
            index = columns.get(name)
            return line[index] if index is not None and index < len(line) else None

        key = _text(cell("vendor_code")) or _text(cell("nm_id"))
        week = parse_week(cell("week"))
        if not key and week is None:
            continue
        rows.append(
            TheirRow(
                line=number,
                key=key,
                store=_text(cell("store")),
                week=week or "",
                forecast=_number(cell("forecast")),
                diff_percent=_number(cell("diff")),
                status=_text(cell("status")) or None,
            )
        )
    return rows


def read_file(data: bytes, filename: str = "") -> list[TheirRow]:
    """Разбирает файл заказчика: книга Excel или CSV."""
    if not data:
        raise ReconcileError("Файл пустой")
    if len(data) > MAX_FILE_BYTES:
        raise ReconcileError(f"Файл больше {MAX_FILE_BYTES // (1024 * 1024)} МБ")

    name = (filename or "").lower()
    if name.endswith((".csv", ".txt", ".tsv")):
        return _rows_from_grid(_read_csv(data))
    try:
        workbook = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as error:  # noqa: BLE001 — openpyxl бросает что угодно
        raise ReconcileError(
            "Файл не открывается ни как книга Excel (.xlsx), ни как CSV. "
            "Из Google Sheets выгрузите .xlsx или .csv"
        ) from error
    try:
        sheet = workbook.worksheets[0]
        grid = [list(row) for row in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()
    return _rows_from_grid(grid)


def _read_csv(data: bytes) -> list[list[object]]:
    text = data.decode("utf-8-sig", errors="replace")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
    except csv.Error:
        dialect = csv.excel
    return [list(row) for row in csv.reader(io.StringIO(text), dialect)]


# ----------------------------------------------------------------------- сверка


def _our_pairs(session: Session) -> tuple[dict[str, int], dict[int, object]]:
    """Ключ файла → наша пара. Ключом может быть артикул или номер WB."""
    pairs = load_pairs(session, MatrixFilters())
    by_id = {pair.id: pair for pair in pairs}
    index: dict[str, int] = {}
    for pair in pairs:
        for key in (pair.vendor_code, pair.nm_id):
            if key:
                index.setdefault(_text(key).lower(), pair.id)
        if pair.store_name and pair.vendor_code:
            index.setdefault(
                f"{_text(pair.store_name).lower()}|{_text(pair.vendor_code).lower()}", pair.id
            )
    return index, by_id


def _their_status(value: str | None) -> Status | None:
    text = (value or "").strip().lower()
    return STATUS_WORDS.get(text)


def _verdict(report: ReconcileReport) -> str:
    """Вывод словами — то, за чем сверку и запускают."""
    if not report.compared:
        return (
            "Сверить не удалось ни одной ячейки: ни одна позиция файла "
            "не нашлась в системе или ни одна неделя не попала в горизонт."
        )
    share = report.matched / report.compared
    if not report.diverged:
        return (
            f"Числа сходятся: {report.compared} ячеек в пределах допуска. "
            "Алгоритм DIFF и правила статусов можно считать подтверждёнными "
            "на этом файле."
        )
    head = f"Сошлось {report.matched} из {report.compared} ячеек ({share:.0%})."

    # Общий множитель проверяется первым: если отношение одинаково на всех
    # ячейках, оно объясняет и те расхождения, что попали на наши правки.
    # Правки не могли бы дать ровное отношение — с ними оно разъехалось бы.
    if (
        report.median_ratio
        and report.ratio_share
        and report.ratio_share >= RATIO_SHARE
        and abs(report.median_ratio - 1.0) > TOLERANCE_SHARE
    ):
        return (
            f"{head} Наши числа в {report.median_ratio:.2f}× от ваших, и так "
            f"на {report.ratio_share:.0%} сверенных ячеек — это похоже на разный "
            "общий множитель, а не на разный алгоритм."
        )
    explained = sum(r.count for r in report.reasons if r.reason in (OURS_MANUAL, OURS_DIFF))
    if explained and explained > report.diverged / 2:
        return (
            f"{head} Больше половины расхождений ({explained} из {report.diverged}) "
            "объясняются нашими же правками и множителями DIFF, а не алгоритмом: "
            "сверять нужно те ячейки, где ни правки, ни DIFF нет."
        )
    return (
        f"{head} Расхождения не сводятся ни к нашим правкам, ни к одному "
        "множителю — значит, различается сам расчёт: базовый потенциал, "
        "сезонность или правило DIFF."
    )


def reconcile(
    session: Session,
    data: bytes,
    filename: str = "",
    today: date | None = None,
) -> ReconcileReport:
    """Сверяет файл заказчика с нашими числами. В базу не пишет ничего."""
    their_rows = read_file(data, filename)
    report = ReconcileReport(rows_read=len(their_rows))

    settings = load_app_settings(session)
    last_closed = resolve_last_closed_week(settings, today)
    # Ось та же, что на экране: сверять имеет смысл ровно те недели, которые
    # система показывает и считает.
    axis = [
        column["key"]
        for column in build_week_axis(
            last_closed, settings.history_weeks, settings.horizon_weeks, today
        )
    ]
    weeks = set(axis)

    index, by_id = _our_pairs(session)
    no_pair = Reason(NO_PAIR)
    no_week = Reason(NO_WEEK)
    bad = Reason(BAD_VALUE)
    manual = Reason(OURS_MANUAL)
    diff = Reason(OURS_DIFF)
    formula = Reason(FORMULA)

    # Что из файла вообще можно сверять.
    wanted: dict[int, list[TheirRow]] = {}
    their_status: dict[int, str] = {}
    for row in their_rows:
        pair_id = index.get(f"{row.store.lower()}|{row.key.lower()}") or index.get(row.key.lower())
        if pair_id is None:
            no_pair.add(f"строка {row.line}: {row.key or '—'}")
            continue
        if row.status and pair_id not in their_status:
            their_status[pair_id] = row.status
        if row.week not in weeks:
            no_week.add(f"строка {row.line}: {row.week or '—'}")
            continue
        if row.forecast is None:
            bad.add(f"строка {row.line}: {row.key}")
            continue
        wanted.setdefault(pair_id, []).append(row)

    if wanted:
        pairs = [by_id[pair_id] for pair_id in wanted]
        results = compute_results(
            session, pairs, axis, last_closed, calc_settings(settings), settings.calc_epoch
        )
        ratios: list[float] = []
        lines: list[Line] = []

        for pair in pairs:
            result = results[pair.id]
            cells = result.by_week()
            for row in wanted[pair.id]:
                cell = cells.get(row.week)
                if cell is None:
                    no_week.add(f"строка {row.line}: {row.week}")
                    continue
                ours = float(cell.original_forecast)
                theirs = float(row.forecast or 0.0)
                report.compared += 1
                if theirs > 0:
                    ratios.append(ours / theirs)

                tolerance = max(TOLERANCE_UNITS, abs(theirs) * TOLERANCE_SHARE)
                if abs(ours - theirs) <= tolerance:
                    report.matched += 1
                    continue

                report.diverged += 1
                example = f"{pair.vendor_code} · {row.week}: у нас {ours:g}, в файле {theirs:g}"
                if cell.is_manual:
                    reason = OURS_MANUAL
                    manual.add(example)
                elif cell.diff_percent is not None and abs(cell.diff_percent - 100.0) > 0.01:
                    reason = OURS_DIFF
                    diff.add(example)
                else:
                    reason = FORMULA
                    formula.add(example)

                lines.append(
                    Line(
                        vendor_code=pair.vendor_code,
                        store_name=pair.store_name,
                        title=pair.title,
                        week=row.week,
                        ours=ours,
                        theirs=theirs,
                        delta=ours - theirs,
                        ratio=(ours / theirs) if theirs > 0 else None,
                        reason=reason,
                        line=row.line,
                    )
                )

            want_status = _their_status(their_status.get(pair.id))
            if want_status is not None:
                report.status_compared += 1
                if want_status is not result.status:
                    report.status_mismatched += 1
                    if len(report.status_examples) < 10:
                        report.status_examples.append(
                            f"{pair.vendor_code}: у нас {result.status.value}, "
                            f"в файле {want_status.value}"
                        )

        if ratios:
            report.median_ratio = round(statistics.median(ratios), 4)
            band = report.median_ratio * RATIO_BAND
            near = sum(1 for value in ratios if abs(value - report.median_ratio) <= band)
            report.ratio_share = round(near / len(ratios), 4)

        # Сначала самые крупные расхождения: с них и начинают разбираться.
        lines.sort(key=lambda line: abs(line.delta), reverse=True)
        report.lines = lines[:MAX_LINES]

    report.reasons = [r for r in (formula, manual, diff, no_pair, no_week, bad) if r.count]
    report.verdict = _verdict(report)
    return report
