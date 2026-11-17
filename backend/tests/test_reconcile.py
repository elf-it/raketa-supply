"""Сверка наших чисел с файлом заказчика (задача C-10).

Файлов заказчика нет (вопрос Q-1), поэтому проверяется инструмент: его
кормят файлом, собранным из наших же чисел, и смотрят, что он говорит —
на совпадении, на общем множителе и на расхождениях, которые объясняются
нашими собственными правками.

Самое важное свойство: сверка **ничего не пишет**. Инструмент, который
что-то меняет, перестают запускать.
"""

from __future__ import annotations

import io
from datetime import date

import pytest
from openpyxl import Workbook
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import AdjustmentLog, ForecastAdjustment
from app.services import reconcile

MATRIX = "/api/v1/matrix"
RECONCILE = "/api/v1/matrix/reconcile"


# --------------------------------------------------------------- разбор файла


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-W46", "2026-W46"),
        ("2026 W46", "2026-W46"),
        ("W46 2026", "2026-W46"),
        ("w6 2026", "2026-W06"),
        ("46/2026", "2026-W46"),
        ("2026-11-09", "2026-W46"),
        (date(2026, 11, 9), "2026-W46"),
        # Год обязателен: угадывать его в файле, который сверяют с расчётом,
        # нельзя — ошибка выглядела бы расхождением алгоритма.
        ("46", None),
        ("неделя сорок шестая", None),
        ("2026-W54", None),
        ("", None),
    ],
)
def test_the_week_is_understood_in_several_notations(value, expected):
    assert reconcile.parse_week(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (120, 120.0),
        ("120", 120.0),
        ("120,5", 120.5),
        ("1 200", 1200.0),
        ("95%", 95.0),
        ("", None),
        ("много", None),
        (True, None),
    ],
)
def test_numbers_survive_a_spreadsheet(value, expected):
    assert reconcile._number(value) == expected


def _book(rows: list[list]) -> bytes:
    book = Workbook()
    sheet = book.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_headers_are_found_by_their_names():
    data = _book(
        [
            ["Выгрузка из таблицы планирования"],
            [],
            ["Магазин", "Артикул продавца", "Неделя", "План", "Статус"],
            ["Магазин 01", "art-1", "2026-W46", 120, "OK"],
        ]
    )

    rows = reconcile.read_file(data, "plan.xlsx")

    assert len(rows) == 1
    assert rows[0].key == "art-1"
    assert rows[0].week == "2026-W46"
    assert rows[0].forecast == 120.0
    assert rows[0].status == "OK"
    assert rows[0].line == 4, "номер строки нужен, чтобы человек нашёл её у себя"


def test_english_headers_in_any_order_are_understood():
    data = _book([["week", "forecast", "sku"], ["2026-W46", 10, "art-1"]])

    rows = reconcile.read_file(data, "plan.xlsx")

    assert (rows[0].key, rows[0].week, rows[0].forecast) == ("art-1", "2026-W46", 10.0)


def test_a_csv_is_read_too():
    data = "Артикул;Неделя;Прогноз\nart-1;2026-W46;120\n".encode()

    rows = reconcile.read_file(data, "plan.csv")

    assert rows[0].forecast == 120.0


def test_a_matrix_with_weeks_across_the_top_is_refused_with_advice():
    """Самая вероятная ошибка — отдать таблицу как она лежит."""
    data = _book(
        [
            ["Артикул", "W45 2026", "W46 2026", "W47 2026", "W48 2026"],
            ["art-1", 100, 110, 120, 130],
        ]
    )

    with pytest.raises(reconcile.ReconcileError) as error:
        reconcile.read_file(data, "plan.xlsx")

    assert "колонками" in str(error.value)
    assert "Артикул" in str(error.value), "в отказе должно быть написано, что нужно"


def test_a_file_without_headers_says_what_is_missing():
    data = _book([["что-то", "совсем", "другое"], [1, 2, 3]])

    with pytest.raises(reconcile.ReconcileError, match="Артикул"):
        reconcile.read_file(data, "plan.xlsx")


def test_an_empty_file_is_refused():
    with pytest.raises(reconcile.ReconcileError, match="пустой"):
        reconcile.read_file(b"", "plan.xlsx")


# ------------------------------------------------------------ сверка с нашими


def _our_numbers(client, weeks: int = 6) -> tuple[list[dict], list[str]]:
    """Наши числа с экрана: строки по артикулам и ось недель."""
    answer = client.get(MATRIX, params={"level": "sku", "limit": 5})
    assert answer.status_code == 200, answer.text
    body = answer.json()
    keys = [week["key"] for week in body["weeks"] if not week["is_closed"]][:weeks]
    return body["rows"], keys


def _their_file(rows: list[dict], weeks: list[str], factor: float = 1.0) -> bytes:
    """Файл «заказчика», собранный из наших же чисел."""
    table: list[list] = [["Магазин", "Артикул", "Неделя", "Прогноз"]]
    for row in rows:
        forecast = row["metrics"]["original_forecast"]
        for week in weeks:
            value = forecast.get(week)
            if value is None:
                continue
            table.append(
                [
                    row["meta"]["store_name"],
                    row["meta"]["vendor_code"],
                    week,
                    round(value * factor),
                ]
            )
    return _book(table)


def _run(data: bytes, filename: str = "plan.xlsx"):
    with SessionLocal() as session:
        return reconcile.reconcile(session, data, filename)


def test_our_own_numbers_come_back_as_a_match(client):
    rows, weeks = _our_numbers(client)

    report = _run(_their_file(rows, weeks))

    assert report.compared > 0
    assert report.diverged == 0
    assert report.matched == report.compared
    assert "сходятся" in report.verdict


def test_a_common_factor_is_named_as_a_factor(client):
    """Это и есть ответ на вопрос Q-1 за одну строку: дело не в алгоритме."""
    rows, weeks = _our_numbers(client)

    report = _run(_their_file(rows, weeks, factor=0.8))

    assert report.diverged > 0
    assert report.median_ratio == pytest.approx(1.25, abs=0.05)
    assert report.ratio_share and report.ratio_share >= 0.8
    assert "множитель" in report.verdict


def test_rounding_is_not_a_divergence(client):
    """Разница в штуку — это округление, а не разные формулы."""
    rows, weeks = _our_numbers(client)
    table: list[list] = [["Артикул", "Неделя", "Прогноз"]]
    for row in rows:
        forecast = row["metrics"]["original_forecast"]
        for week in weeks:
            value = forecast.get(week)
            if value:
                table.append([row["meta"]["vendor_code"], week, round(value) + 1])

    report = _run(_book(table))

    assert report.compared > 0
    assert report.diverged == 0


def test_a_divergence_explained_by_our_own_edit_says_so(client):
    """Ячейка с ручной правкой расходится не из-за алгоритма."""
    rows, weeks = _our_numbers(client)
    pair_id = rows[0]["meta"]["pair_id"]
    week = weeks[0]
    data = _their_file(rows, weeks)

    patched = client.put(f"/api/v1/forecast/{pair_id}/{week}", json={"manual_forecast": 777})
    assert patched.status_code == 200, patched.text
    try:
        report = _run(data)

        reasons = {reason.reason: reason.count for reason in report.reasons}
        assert reasons.get(reconcile.OURS_MANUAL) == 1
        assert reconcile.FORMULA not in reasons
        assert "правками" in report.verdict
    finally:
        client.put(f"/api/v1/forecast/{pair_id}/{week}", json={"clear": ["manual_forecast"]})


def test_a_divergence_explained_by_diff_says_so(client):
    """Ячейка с нашим множителем DIFF расходится не из-за алгоритма.

    Ячейка берётся та, где DIFF уже стоит в демо-данных: так проверяется ровно
    разбор причины, без правок в базе по ходу теста.
    """
    rows, weeks = _our_numbers(client, weeks=8)
    found = None
    for row in rows:
        for week in weeks:
            diff = row["metrics"]["diff_percent"].get(week)
            value = row["metrics"]["original_forecast"].get(week)
            if diff is not None and abs(diff - 100.0) > 0.01 and value:
                found = (row, week, value)
                break
        if found:
            break
    assert found, "в демо-данных должна быть хотя бы одна ячейка с DIFF ≠ 100 %"

    row, week, value = found
    table = [
        ["Артикул", "Неделя", "Прогноз"],
        [row["meta"]["vendor_code"], week, round(value) + 50],
    ]

    report = _run(_book(table))

    reasons = {reason.reason: reason.count for reason in report.reasons}
    assert reasons.get(reconcile.OURS_DIFF) == 1
    assert reconcile.FORMULA not in reasons


def test_unknown_positions_and_weeks_are_counted_separately(client):
    rows, weeks = _our_numbers(client)
    table = [
        ["Артикул", "Неделя", "Прогноз"],
        ["нет-такого-артикула", weeks[0], 100],
        [rows[0]["meta"]["vendor_code"], "2019-W02", 100],
        [rows[0]["meta"]["vendor_code"], weeks[0], "много"],
    ]

    report = _run(_book(table))

    reasons = {reason.reason: reason.count for reason in report.reasons}
    assert reasons[reconcile.NO_PAIR] == 1
    assert reasons[reconcile.NO_WEEK] == 1
    assert reasons[reconcile.BAD_VALUE] == 1
    assert report.compared == 0
    assert "не удалось" in report.verdict


def test_statuses_are_compared_too(client):
    rows, weeks = _our_numbers(client)
    ours = rows[0]["status"]
    theirs = "SHORT" if ours != "SHORT" else "OVER"
    table = [
        ["Артикул", "Неделя", "Прогноз", "Статус"],
        [
            rows[0]["meta"]["vendor_code"],
            weeks[0],
            round(rows[0]["metrics"]["original_forecast"][weeks[0]]),
            theirs,
        ],
    ]

    report = _run(_book(table))

    assert report.status_compared == 1
    assert report.status_mismatched == 1
    assert report.status_examples and theirs.lower() in report.status_examples[0].lower()


def test_the_biggest_divergences_come_first(client):
    rows, weeks = _our_numbers(client)
    table: list[list] = [["Артикул", "Неделя", "Прогноз"]]
    code = rows[0]["meta"]["vendor_code"]
    forecast = rows[0]["metrics"]["original_forecast"]
    table.append([code, weeks[0], round(forecast[weeks[0]]) + 5])
    table.append([code, weeks[1], round(forecast[weeks[1]]) + 500])

    report = _run(_book(table))

    assert [abs(line.delta) for line in report.lines] == [500.0, 5.0]


def test_reconciling_writes_nothing(client):
    """Инструмент, который что-то меняет, перестают запускать."""
    rows, weeks = _our_numbers(client)
    data = _their_file(rows, weeks, factor=0.5)

    with SessionLocal() as session:
        logs_before = session.scalar(select(func.count()).select_from(AdjustmentLog))
        cells_before = session.scalar(select(func.count()).select_from(ForecastAdjustment))

    report = _run(data)
    assert report.diverged > 0

    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(AdjustmentLog)) == logs_before
        assert session.scalar(select(func.count()).select_from(ForecastAdjustment)) == cells_before

    # И числа на экране те же, что были.
    again, _ = _our_numbers(client)
    assert again[0]["metrics"]["original_forecast"] == rows[0]["metrics"]["original_forecast"]


# ------------------------------------------------------------------- ручка


def test_the_endpoint_answers_the_viewer(viewer_client, client):
    """Сверка ничего не меняет, поэтому прав на просмотр достаточно."""
    rows, weeks = _our_numbers(client)
    data = _their_file(rows, weeks)

    answer = viewer_client.post(
        RECONCILE, files={"file": ("plan.xlsx", data, "application/vnd.ms-excel")}
    )

    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["compared"] > 0
    assert body["diverged"] == 0
    assert "сходятся" in body["verdict"]


def test_the_endpoint_is_closed_without_a_session(anon_client):
    answer = anon_client.post(RECONCILE, files={"file": ("plan.xlsx", b"x", "text/csv")})

    assert answer.status_code == 401


def test_a_file_that_cannot_be_read_gives_422(client):
    answer = client.post(RECONCILE, files={"file": ("plan.xlsx", b"not a book", "text/csv")})

    assert answer.status_code == 422
    assert "CSV" in answer.json()["detail"]
