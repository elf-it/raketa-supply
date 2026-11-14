"""Загрузка правок прогноза из выгруженного файла (задача G-9).

Главная проверка — та, ради которой вся осторожность: файл, скачанный и не
тронутый, не должен менять ничего. В нём стоит весь прогноз выборки, и если
принять его целиком, каждая ячейка станет ручной правкой, а расчёт по потенциалу
и сезонности встанет намертво — тихо и необратимо.

Вторая: правка, сделанная в системе уже после выгрузки, не затирается числом
из файла. Там, где файл и система разошлись, человек должен увидеть конфликт,
а не узнать о пропаже чужой работы из журнала.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import BytesIO

from openpyxl import load_workbook

from app.services.matrix_export import export_stamp

MATRIX = "/api/v1/matrix"
EXPORT = f"{MATRIX}/export?level=sku"
IMPORT = f"{MATRIX}/import"
FORECAST_LABEL = "План / исходный прогноз"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _download(client, url: str = EXPORT) -> bytes:
    response = client.get(url)
    assert response.status_code == 200, response.text
    return response.content


def _book(data: bytes):
    return load_workbook(BytesIO(data))


def _save(workbook) -> bytes:
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _head(sheet) -> int:
    for line in range(1, 30):
        if sheet.cell(row=line, column=6).value == "Показатель":
            return line
    raise AssertionError("в файле нет строки заголовков")


def _week_column(sheet, week: str) -> int:
    year, number = week.split("-W")
    wanted = f"W{number}\n{year}"
    head = _head(sheet)
    for column in range(7, sheet.max_column + 1):
        if sheet.cell(row=head, column=column).value == wanted:
            return column
    raise AssertionError(f"в файле нет недели {week}")


def _forecast_line(sheet, vendor_code: str) -> int:
    for line in range(_head(sheet) + 1, sheet.max_row + 1):
        if (
            str(sheet.cell(row=line, column=2).value) == vendor_code
            and sheet.cell(row=line, column=6).value == FORECAST_LABEL
        ):
            return line
    raise AssertionError(f"в файле нет строки прогноза для {vendor_code}")


def _edit(data: bytes, vendor_code: str, week: str, value: object) -> bytes:
    workbook = _book(data)
    sheet = workbook.active
    sheet.cell(
        row=_forecast_line(sheet, vendor_code),
        column=_week_column(sheet, week),
        value=value,
    )
    return _save(workbook)


def _restamp(data: bytes, value: object) -> bytes:
    """Подменяет отметку о времени выгрузки — ею определяется, что считать конфликтом."""
    workbook = _book(data)
    sheet = workbook.active
    for line in range(1, 20):
        if sheet.cell(row=line, column=1).value == "Выгружено":
            sheet.cell(row=line, column=3).value = value
    return _save(workbook)


def _upload(client, data: bytes, apply: bool = False):
    return client.post(
        f"{IMPORT}?apply={'true' if apply else 'false'}",
        files={"file": ("Матрица.xlsx", data, XLSX)},
    )


def _ok(client, data: bytes, apply: bool = False) -> dict:
    response = _upload(client, data, apply)
    assert response.status_code == 200, response.text
    return response.json()


def _first_sku(client) -> tuple[dict, str]:
    """Первая позиция выборки и первая открытая неделя."""
    matrix = client.get(f"{MATRIX}?level=sku").json()
    week = next(w["key"] for w in matrix["weeks"] if not w["is_closed"])
    return matrix["rows"][0], week


def _forecast(client, pair_id: int, week: str) -> float:
    matrix = client.get(f"{MATRIX}?level=sku").json()
    row = next(r for r in matrix["rows"] if r["meta"]["pair_id"] == pair_id)
    return row["metrics"]["original_forecast"][week]


def test_untouched_file_changes_nothing(client):
    """Скачали и загрузили обратно — в системе не должно шевельнуться ничего.

    Это главный тест файла: в нём стоит весь прогноз, и принять его целиком
    значило бы превратить каждую ячейку выборки в ручную правку.
    """
    before = client.get(f"{MATRIX}?level=sku").json()

    report = _ok(client, _download(client), apply=True)

    assert report["change_count"] == 0
    assert report["applied_cells"] == 0
    assert report["cells_read"] > 0, "файл прочитан, просто менять в нём нечего"
    assert report["issues"] == [], "закрытые недели есть в любой выгрузке — это не новость"
    assert client.get(f"{MATRIX}?level=sku").json()["rows"] == before["rows"]


def test_edited_cell_comes_back(client):
    row, week = _first_sku(client)
    pair_id = row["meta"]["pair_id"]
    was = _forecast(client, pair_id, week)
    data = _edit(_download(client), row["meta"]["vendor_code"], week, was + 137)

    preview = _ok(client, data)
    assert preview["change_count"] == 1
    assert preview["applied"] is False
    assert preview["changes"][0]["week"] == week
    assert preview["changes"][0]["old_value"] == was
    assert preview["changes"][0]["new_value"] == was + 137
    # Разбор ничего не пишет: до подтверждения система не меняется.
    assert _forecast(client, pair_id, week) == was

    applied = _ok(client, data, apply=True)
    try:
        assert applied["applied"] is True
        assert applied["applied_cells"] == 1
        assert _forecast(client, pair_id, week) == was + 137

        # В журнале это загрузка, а не правка неизвестного происхождения.
        log = client.get(f"/api/v1/forecast/log?batch_id={applied['batch_id']}").json()
        assert log["total"] == 1
        assert log["entries"][0]["operation"] == "import"
        assert log["entries"][0]["week"] == week
        assert log["entries"][0]["new_value"] == was + 137
        assert log["entries"][0]["author"]
    finally:
        client.put(f"/api/v1/forecast/{pair_id}/{week}", json={"clear": ["manual_forecast"]})


def test_change_made_after_the_export_is_not_overwritten(client):
    """Пока файл правили, ячейку поменяли в системе. Чужую работу не теряем."""
    row, week = _first_sku(client)
    pair_id = row["meta"]["pair_id"]
    vendor_code = row["meta"]["vendor_code"]
    was = _forecast(client, pair_id, week)
    # Файл правили пять минут: иначе «после выгрузки» и «во время выгрузки»
    # в тесте сливаются в одну секунду, и проверять было бы нечего.
    data = _restamp(
        _edit(_download(client), vendor_code, week, was + 11),
        export_stamp(datetime.now(UTC) - timedelta(minutes=5)),
    )

    # Кто-то другой правит ту же ячейку уже после выгрузки.
    edit = client.put(f"/api/v1/forecast/{pair_id}/{week}", json={"manual_forecast": was + 500})
    assert edit.status_code == 200, edit.text
    try:
        report = _ok(client, data, apply=True)

        assert report["change_count"] == 0
        assert report["conflict_count"] == 1
        conflict = report["conflicts"][0]
        assert conflict["week"] == week
        assert conflict["new_value"] == was + 11
        assert conflict["old_value"] == was + 500
        assert conflict["changed_by"]
        # Значение осталось тем, которое поставили в системе.
        assert _forecast(client, pair_id, week) == was + 500
    finally:
        client.put(f"/api/v1/forecast/{pair_id}/{week}", json={"clear": ["manual_forecast"]})


def test_closed_weeks_and_unknown_rows_are_reported(client):
    """Что не взяли — сказано, а не пропущено молча."""
    matrix = client.get(f"{MATRIX}?level=sku").json()
    closed_week = next(w["key"] for w in matrix["weeks"] if w["is_closed"])
    vendor_code = matrix["rows"][0]["meta"]["vendor_code"]

    workbook = _book(_edit(_download(client), vendor_code, closed_week, 9999))
    sheet = workbook.active
    # Позиция, которой в системе нет: артикул из файла поменяли на чужой.
    other = matrix["rows"][1]["meta"]["vendor_code"]
    sheet.cell(row=_forecast_line(sheet, other), column=2, value="НЕТ-ТАКОГО-АРТИКУЛА")

    report = _ok(client, _save(workbook))

    reasons = {issue["reason"]: issue for issue in report["issues"]}
    assert report["change_count"] == 0, "закрытая неделя и чужой артикул правками не считаются"
    shut = next(issue for reason, issue in reasons.items() if "закрыли" in reason)
    assert closed_week in " ".join(shut["examples"])
    assert any("не найдена" in reason for reason in reasons)
    assert "НЕТ-ТАКОГО-АРТИКУЛА" in " ".join(
        next(issue for reason, issue in reasons.items() if "не найдена" in reason)["examples"]
    )


def test_values_that_are_not_units_are_refused_cell_by_cell(client):
    """Опечатка в одной ячейке не должна валить весь файл."""
    row, week = _first_sku(client)
    matrix = client.get(f"{MATRIX}?level=sku").json()
    second = matrix["rows"][1]["meta"]
    was = _forecast(client, second["pair_id"], week)

    data = _edit(_download(client), row["meta"]["vendor_code"], week, "много")
    data = _edit(data, second["vendor_code"], week, was + 5)

    report = _ok(client, data)

    assert report["change_count"] == 1, "годная правка принимается несмотря на соседнюю опечатку"
    assert report["changes"][0]["vendor_code"] == second["vendor_code"]
    assert any("штуки" in issue["reason"] for issue in report["issues"])


def test_negative_forecast_is_not_accepted(client):
    row, week = _first_sku(client)
    data = _edit(_download(client), row["meta"]["vendor_code"], week, -10)

    report = _ok(client, data)

    assert report["change_count"] == 0
    assert any("штуки" in issue["reason"] for issue in report["issues"])


def test_group_level_file_is_refused(client):
    """У строки магазина нет однозначного разложения на позиции."""
    response = _upload(client, _download(client, f"{MATRIX}/export?level=store"))

    assert response.status_code == 422
    assert "по артикулам" in response.json()["detail"]


def test_foreign_file_is_refused(client):
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.active["A1"] = "Какой-то свой файл"
    response = _upload(client, _save(workbook))

    assert response.status_code == 422
    assert "выгрузка матрицы" in response.json()["detail"]

    assert _upload(client, b"not a workbook at all").status_code == 422


def test_file_without_the_export_time_is_refused(client):
    """Без времени выгрузки нечем отличить правку человека от чужой правки."""
    data = _download(client)

    empty = _upload(client, _restamp(data, None))
    assert empty.status_code == 422
    assert "времени выгрузки" in empty.json()["detail"]

    # Файл старой версии: дата без времени. Разница в часах здесь решающая,
    # поэтому такой файл тоже просим выгрузить заново.
    old = _upload(client, _restamp(data, "15.11.2026"))
    assert old.status_code == 422
    assert "времени выгрузки" in old.json()["detail"]


def test_viewer_cannot_import(client, viewer_client):
    data = _download(client)
    assert (
        viewer_client.post(
            f"{IMPORT}?apply=true", files={"file": ("м.xlsx", data, XLSX)}
        ).status_code
        == 403
    )
