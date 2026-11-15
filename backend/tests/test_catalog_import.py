"""Загрузка номенклатуры файлом (задача B-15).

Главная проверка — круговая: то, что система выгружает, она должна уметь
прочитать обратно без единого изменения. Если выгрузка и загрузка разойдутся
хоть одной колонкой, человек получит файл, который невозможно вернуть в систему,
и узнает об этом на тысяче артикулов.

Вторая: предпросмотр не врёт и ничего не пишет. Сначала человек видит, что
именно изменится, и только потом подтверждает.
"""

from __future__ import annotations

from io import BytesIO

import pytest
from openpyxl import Workbook, load_workbook
from pytest import approx

from app.services.catalog_import import COLUMNS, EXPORT_COLUMNS

CATALOG = "/api/v1/catalog"
MATRIX = "/api/v1/matrix?level=sku&limit=1"
HEADERS = [title for title, _ in EXPORT_COLUMNS]


def _book(rows: list[list[object]], headers: list[str] | None = None) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(headers if headers is not None else HEADERS)
    for row in rows:
        sheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _row(**values: object) -> list[object]:
    """Строка файла по именам полей: порядок колонок берётся из выгрузки."""
    return [values.get(field) for _, field in EXPORT_COLUMNS]


def _send(client, data: bytes, apply: bool = False, expect: int = 200) -> dict:
    response = client.post(
        f"{CATALOG}/import?apply={'true' if apply else 'false'}",
        files={"file": ("номенклатура.xlsx", data, "application/vnd.ms-excel")},
    )
    assert response.status_code == expect, response.text
    return response.json()


def _export(client) -> bytes:
    response = client.get(f"{CATALOG}/export")
    assert response.status_code == 200, response.text
    assert "spreadsheet" in response.headers["content-type"]
    return response.content


def _sku_count(client) -> int:
    return client.get(MATRIX).json()["context"]["sku_count"]


def _products(client) -> list[dict]:
    return client.get(f"{CATALOG}/products").json()


@pytest.fixture
def made(client):
    """Убирает за собой всё, что завёл тест: пары, товары, категорию, поставщика."""
    codes: list[str] = []
    names: dict[str, list[str]] = {"categories": [], "suppliers": []}
    yield codes, names

    for product in _products(client):
        if product["vendor_code"] not in codes:
            continue
        for pair in client.get(f"{CATALOG}/products/{product['id']}/stores").json():
            client.delete(f"{CATALOG}/pairs/{pair['id']}")
        client.delete(f"{CATALOG}/products/{product['id']}")
    for category in client.get(f"{CATALOG}/categories").json():
        if category["name"] in names["categories"]:
            client.delete(f"{CATALOG}/categories/{category['id']}")
    for supplier in client.get(f"{CATALOG}/suppliers").json():
        if supplier["name"] in names["suppliers"]:
            client.delete(f"{CATALOG}/suppliers/{supplier['id']}")


def test_export_columns_match_what_the_import_reads(client):
    """Выгрузка пишет ровно те колонки, которые загрузка ищет по названию."""
    for title, field in EXPORT_COLUMNS:
        assert title.lower() in COLUMNS, f"колонку «{title}» загрузка не узнает"
        assert COLUMNS[title.lower()] == field

    sheet = load_workbook(BytesIO(_export(client)), read_only=True).worksheets[0]
    head = [cell.value for cell in next(sheet.iter_rows(max_row=1))]
    assert head == HEADERS


def test_round_trip_changes_nothing(client):
    """Собственная выгрузка, загруженная обратно, не меняет ни одной записи."""
    plan = _send(client, _export(client))

    assert plan["rows_read"] > 0
    assert plan["total_changes"] == 0, plan
    assert plan["issues"]["count"] == 0, plan["issues"]["examples"]


def test_file_fills_an_empty_catalogue(client, made):
    """Товары, категория, поставщик и пары — из файла, и позиция сразу считается."""
    codes, names = made
    codes += ["B15-001", "B15-002"]
    names["categories"].append("Проверочная категория")
    names["suppliers"].append("Проверочный поставщик")
    store = client.get(f"{CATALOG}/stores").json()[0]
    was = _sku_count(client)

    data = _book(
        [
            _row(
                vendor_code="B15-001",
                title="Первый из файла",
                category="Проверочная категория",
                niche="Проверка",
                supplier="Проверочный поставщик",
                cost_price=300,
                price=900,
                store=store["code"],
                nm_id="950001",
                base_potential=30,
            ),
            _row(
                vendor_code="B15-002",
                title="Второй из файла",
                category="Проверочная категория",
                supplier="Проверочный поставщик",
                cost_price=150,
                store=store["code"],
                nm_id="950002",
                base_potential=10,
            ),
        ]
    )

    preview = _send(client, data)
    assert preview["applied"] is False
    assert preview["new_products"]["count"] == 2
    assert preview["new_pairs"]["count"] == 2
    assert preview["new_categories"]["count"] == 1
    assert preview["new_suppliers"]["count"] == 1
    assert preview["price_records"]["count"] == 2
    assert not any(p["vendor_code"] == "B15-001" for p in _products(client)), "разбор не пишет"

    applied = _send(client, data, apply=True)

    assert applied["applied"] is True
    assert applied["new_products"]["count"] == 2
    assert applied["issues"]["count"] == 0, applied["issues"]["examples"]
    first = next(p for p in _products(client) if p["vendor_code"] == "B15-001")
    assert first["title"] == "Первый из файла"
    assert first["category"] == "Проверочная категория"
    assert first["cost_price"] == approx(300)
    assert first["store_count"] == 1
    assert _sku_count(client) == was + 2

    # Цена из файла — запись истории с пометкой, а не число без прошлого (B-9).
    card = client.get(f"{CATALOG}/products/{first['id']}/prices").json()
    assert len(card["history"]) == 1
    assert card["history"][0]["note"] == "загрузка из файла"
    assert card["history"][0]["author"]

    # Второй раз тот же файл ничего не меняет.
    again = _send(client, data)
    assert again["total_changes"] == 0, again


def test_second_cabinet_is_another_row_not_another_product(client, made):
    """Один артикул в двух кабинетах — две строки и одна карточка."""
    codes, _ = made
    codes.append("B15-003")
    stores = client.get(f"{CATALOG}/stores").json()
    data = _book(
        [
            _row(
                vendor_code="B15-003",
                title="В двух кабинетах",
                store=stores[0]["code"],
                nm_id="950003",
                base_potential=5,
            ),
            _row(
                vendor_code="B15-003",
                title="В двух кабинетах",
                store=stores[1]["code"],
                nm_id="950004",
                base_potential=7,
            ),
        ]
    )

    plan = _send(client, data, apply=True)

    assert plan["new_products"]["count"] == 1
    assert plan["new_pairs"]["count"] == 2
    product = next(p for p in _products(client) if p["vendor_code"] == "B15-003")
    pairs = client.get(f"{CATALOG}/products/{product['id']}/stores").json()
    assert {pair["nm_id"] for pair in pairs} == {"950003", "950004"}
    assert {pair["base_potential"] for pair in pairs} == {5, 7}


def test_empty_cell_does_not_erase(client, made):
    """Пустая ячейка — «не трогать», иначе файл снёс бы параметры всего каталога."""
    codes, _ = made
    codes.append("B15-004")
    _send(client, _book([_row(vendor_code="B15-004", title="С MOQ", moq=500)]), apply=True)
    product = next(p for p in _products(client) if p["vendor_code"] == "B15-004")
    assert product["moq"] == 500

    # Тот же артикул без колонок параметров: только артикул и название.
    plan = _send(
        client,
        _book([["B15-004", "С MOQ переименован"]], headers=["Артикул", "Название"]),
        apply=True,
    )

    assert plan["updated_products"]["count"] == 1
    updated = next(p for p in _products(client) if p["vendor_code"] == "B15-004")
    assert updated["title"] == "С MOQ переименован"
    assert updated["moq"] == 500, "MOQ не тронут"


def test_unknown_cabinet_and_taken_nm_id_are_refused_by_row(client, made):
    """Отказ по строке не роняет файл: остальные строки загружаются."""
    codes, _ = made
    codes.append("B15-005")
    store = client.get(f"{CATALOG}/stores").json()[0]
    taken = next(
        pair
        for product in _products(client)
        for pair in client.get(f"{CATALOG}/products/{product['id']}/stores").json()
        if pair["store_id"] == store["id"] and pair["nm_id"]
    )

    plan = _send(
        client,
        _book(
            [
                _row(vendor_code="B15-005", title="Нормальная строка"),
                _row(vendor_code="B15-006", title="Чужой кабинет", store="store-99"),
                _row(
                    vendor_code="B15-005",
                    title="Занятая номенклатура",
                    store=store["code"],
                    nm_id=taken["nm_id"],
                ),
            ]
        ),
        apply=True,
    )

    assert plan["new_products"]["count"] == 1, "взяли только годную строку"
    assert plan["issues"]["count"] == 2
    text = " | ".join(plan["issues"]["examples"])
    assert "кабинеты файл не создаёт" in text.lower()
    assert "уже привязана" in text
    assert not any(p["vendor_code"] == "B15-006" for p in _products(client))


def test_new_product_without_a_title_is_refused(client):
    plan = _send(client, _book([_row(vendor_code="B15-007")]))

    assert plan["new_products"]["count"] == 0
    assert "название" in plan["issues"]["examples"][0]


def test_garbage_and_strange_files_are_refused_whole(client):
    assert "не открывается" in _send(client, b"not a workbook", expect=422)["detail"]
    assert "Файл пустой" in _send(client, b"", expect=422)["detail"]

    no_header = _send(client, _book([[1, 2]], headers=["Что-то", "Ещё"]), expect=422)
    assert "Артикул" in no_header["detail"]

    empty = _send(client, _book([], headers=HEADERS), expect=422)
    assert "ни одной строки с артикулом" in empty["detail"]


def test_numbers_written_as_text_are_understood(client, made):
    """Из чужой выгрузки числа приезжают строками: «1 234,50» — это цена."""
    codes, _ = made
    codes.append("B15-008")

    _send(
        client,
        _book([_row(vendor_code="B15-008", title="Текстовые числа", cost_price="1 234,50")]),
        apply=True,
    )

    product = next(p for p in _products(client) if p["vendor_code"] == "B15-008")
    assert product["cost_price"] == approx(1234.5)


def test_viewer_may_look_but_not_load(client, viewer_client):
    assert viewer_client.get(f"{CATALOG}/export").status_code == 200
    response = viewer_client.post(
        f"{CATALOG}/import?apply=true",
        files={"file": ("x.xlsx", _book([]), "application/vnd.ms-excel")},
    )
    assert response.status_code == 403
