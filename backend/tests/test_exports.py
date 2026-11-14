"""Выгрузка матрицы и закупочного плана в XLSX.

Файл проверяется чтением книги, а не кодом ответа: он уходит человеку, который
будет сверять его со своими Google Sheets, и расхождение с экраном — это дефект,
даже если HTTP ответил 200.
"""

from __future__ import annotations

from datetime import date, datetime
from io import BytesIO

from openpyxl import load_workbook

MATRIX = "/api/v1/matrix"
PURCHASE = "/api/v1/purchase"


def _sheet(content: bytes):
    return load_workbook(BytesIO(content)).active


def _grid(sheet) -> list[list]:
    return [[cell.value for cell in row] for row in sheet.iter_rows()]


def _head(grid: list[list], marker: str) -> int:
    """Номер строки с заголовками таблицы (в сетке с нуля)."""
    for index, row in enumerate(grid):
        if marker in row:
            return index
    raise AssertionError(f"в книге нет шапки таблицы с колонкой «{marker}»")


def _find(grid: list[list], head: int, column: int, value) -> list:
    for row in grid[head + 1 :]:
        if row[column] == value:
            return row
    raise AssertionError(f"в книге нет строки со значением «{value}»")


# ------------------------------------------------------------------------------ матрица


def test_matrix_export_repeats_the_screen(client):
    """Числа в файле — те же, что в матрице: иначе сверка с таблицами бессмысленна."""
    matrix = client.get(f"{MATRIX}?level=sku").json()
    response = client.get(f"{MATRIX}/export?level=sku")
    assert response.status_code == 200

    grid = _grid(_sheet(response.content))
    head = _head(grid, "Показатель")
    labels = grid[head]
    metric_column = labels.index("Показатель")
    weeks = [week["key"] for week in matrix["weeks"]]

    row = matrix["rows"][0]
    line = next(
        line
        for line in grid[head + 1 :]
        if line[labels.index("Артикул")] == row["meta"]["vendor_code"]
        and line[metric_column] == "План / исходный прогноз"
    )
    assert line[labels.index("Товар")] == row["title"]
    for index, week in enumerate(weeks):
        expected = row["metrics"]["original_forecast"][week]
        assert line[metric_column + 1 + index] == round(expected)

    # Будущих фактов нет, и в файле на их месте должна быть пустая ячейка, а не ноль:
    # ноль означал бы «не продавали», а не «неделя ещё не закрыта».
    facts = _find(grid, head, metric_column, "Факт продаж")
    future = [week for week in weeks if week > matrix["context"]["last_closed_week"]]
    assert all(facts[metric_column + 1 + weeks.index(week)] is None for week in future)


def test_matrix_export_shows_every_metric_on_group_levels(client):
    """На экране уровень группы показывает один показатель, в файле — все.

    Ширина листа ничем не ограничена, а человек, который сверяет выгрузку, иначе
    вынужден был бы качать файл столько раз, сколько показателей.
    """
    for level in ("store", "category"):
        grid = _grid(_sheet(client.get(f"{MATRIX}/export?level={level}").content))
        head = _head(grid, "Показатель")
        assert grid[head][0] == ("Магазин" if level == "store" else "Категория")
        labels = {line[grid[head].index("Показатель")] for line in grid[head + 1 :]}
        assert "Адаптированный сток" in labels
        assert "Недопоставленный спрос" in labels


def test_matrix_export_separates_fact_from_plan(client):
    """Закрытые недели в файле выделены заливкой: иначе факт и план не отличить."""
    matrix = client.get(f"{MATRIX}?level=sku").json()
    sheet = _sheet(client.get(f"{MATRIX}/export?level=sku").content)
    grid = _grid(sheet)
    head = _head(grid, "Показатель")
    first_week = grid[head].index("Показатель") + 2  # колонки openpyxl считает с единицы

    fills = []
    for index, week in enumerate(matrix["weeks"]):
        cell = sheet.cell(row=head + 1, column=first_week + index)
        fills.append((week["is_closed"], cell.fill.start_color.rgb))
    closed = {rgb for is_closed, rgb in fills if is_closed}
    planned = {rgb for is_closed, rgb in fills if not is_closed}
    assert closed and planned and not (closed & planned)


def test_matrix_export_keeps_totals_out_of_the_filter(client):
    """Итоги лежат ниже автофильтра: при сортировке они не должны уезжать в середину."""
    sheet = _sheet(client.get(f"{MATRIX}/export?level=store").content)
    grid = _grid(sheet)
    head = _head(grid, "Показатель")

    assert sheet.auto_filter.ref is not None
    last_filtered = int(sheet.auto_filter.ref.split(":")[1].lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
    totals = next(index for index, line in enumerate(grid, start=1) if line[0] == "Итого по WB")
    assert totals > last_filtered
    assert sheet.auto_filter.ref.startswith(f"A{head + 1}:")


def test_matrix_export_names_the_filters(client):
    """Шапка файла объясняет, что за выборка: без неё файл нечем опознать."""
    stores = client.get("/api/v1/catalog/stores").json()
    store = stores[0]
    grid = _grid(_sheet(client.get(f"{MATRIX}/export?level=sku&store_id={store['id']}").content))
    flat = [value for line in grid[:12] for value in line if isinstance(value, str)]
    assert store["name"] in flat
    assert "все ниши и категории" in flat


# ---------------------------------------------------------------------- закупочный план


def test_purchase_export_explains_the_quantity(client):
    """В файле остаётся разбор количества: это главный вопрос к любой цифре закупки."""
    plan = client.get(f"{PURCHASE}?only_needed=true").json()
    assert plan["rows"], "в демо-данных нет позиций к заказу"
    api_row = plan["rows"][0]

    response = client.get(f"{PURCHASE}/export?only_needed=true")
    assert response.status_code == 200
    grid = _grid(_sheet(response.content))
    head = _head(grid, "Количество")
    labels = grid[head]
    line = _find(grid, head, labels.index("Артикул"), api_row["vendor_code"])

    def cell(title: str):
        return line[labels.index(title)]

    assert cell("Количество") == api_row["qty"]
    assert cell("Спрос окна") == round(api_row["window_demand"])
    assert cell("Страховой запас") == round(api_row["safety_stock"])
    assert cell("Остаток к началу окна") == round(api_row["stock_before"])
    assert cell("В пути") == round(api_row["window_transit"])
    assert cell("Окно покрытия") == f"{api_row['coverage_from']}…{api_row['coverage_to']}"
    assert cell("Поставщик") == api_row["supplier"]


def test_purchase_export_order_date_is_a_date(client):
    """Срок заказа — дата, а не текст: иначе Excel не отсортирует и не отфильтрует."""
    plan = client.get(f"{PURCHASE}?only_needed=true").json()
    api_row = plan["rows"][0]
    grid = _grid(_sheet(client.get(f"{PURCHASE}/export?only_needed=true").content))
    head = _head(grid, "Количество")
    line = _find(grid, head, grid[head].index("Артикул"), api_row["vendor_code"])

    value = line[grid[head].index("Заказать до")]
    assert isinstance(value, datetime)
    assert value.date() == date.fromisoformat(api_row["order_date"])


def test_purchase_export_follows_the_selection(client):
    """В файле столько строк, сколько отдал расчёт, и отбор подписан в шапке.

    Сравнивать два файла по длине нельзя: в демо-данных заказа требуют все позиции,
    и оба отбора дают одну и ту же выборку. Проверяется то, что проверять осмысленно:
    файл не теряет и не добавляет строк относительно плана.
    """
    for only_needed in ("true", "false"):
        plan = client.get(f"{PURCHASE}?only_needed={only_needed}").json()
        grid = _grid(_sheet(client.get(f"{PURCHASE}/export?only_needed={only_needed}").content))
        head = _head(grid, "Количество")
        labels = grid[head]
        numbered = [line for line in grid[head + 1 :] if isinstance(line[labels.index("№")], int)]
        assert len(numbered) == len(plan["rows"])
        assert ("только позиции к заказу" if only_needed == "true" else "все позиции") in [
            value for line in grid[:head] for value in line if isinstance(value, str)
        ]


def test_purchase_export_explains_a_position_without_an_order():
    """У позиции без заказа количество пустое, а причина — текстом.

    Сборщик книги — чистая функция, и такую позицию проще собрать руками, чем
    выводить из демо-данных: в них заказа требуют все восемнадцать позиций.
    """
    from app.calc.types import PurchaseParams, PurchaseSuggestion
    from app.services.matrix_export import build_purchase_workbook
    from app.services.purchase import PurchaseRow

    row = PurchaseRow(
        pair_id=1,
        store_name="Склад МСК",
        vendor_code="ART-1",
        title="Товар без потребности",
        category="Проверка",
        supplier="Поставщик",
        supplier_id=1,
        suggestion=PurchaseSuggestion(needed=False, reason="остатков хватает на весь горизонт"),
        params=PurchaseParams(),
        current_stock=120.0,
    )
    grid = _grid(_sheet(build_purchase_workbook([row], only_needed=False)))
    head = _head(grid, "Количество")
    labels = grid[head]
    line = _find(grid, head, labels.index("Артикул"), "ART-1")

    assert line[labels.index("Количество")] is None
    assert line[labels.index("Сумма, ₽")] is None
    assert line[labels.index("Остаток сейчас")] == 120
    assert line[labels.index("Комментарий")] == "остатков хватает на весь горизонт"


def test_purchase_export_totals_match_the_plan(client):
    plan = client.get(f"{PURCHASE}?only_needed=true").json()
    grid = _grid(_sheet(client.get(f"{PURCHASE}/export?only_needed=true").content))
    head = _head(grid, "Количество")
    labels = grid[head]
    totals = _find(grid, head, labels.index("Неделя прихода"), "Итого")

    assert totals[labels.index("Количество")] == plan["total_qty"]
    assert totals[labels.index("Сумма, ₽")] == round(plan["total_cost"], 2)


# -------------------------------------------------------------------------------- общее


def test_export_filenames_are_russian_with_an_ascii_fallback(client):
    """Кириллица уходит по RFC 5987: заголовок ответа — только latin-1."""
    for path, fallback, marker in (
        (
            f"{MATRIX}/export?level=sku",
            "matrix-sku.xlsx",
            "%D0%9C%D0%B0%D1%82%D1%80%D0%B8%D1%86%D0%B0",
        ),
        (f"{PURCHASE}/export", "purchase-plan.xlsx", "%D0%97%D0%B0%D0%BA%D1%83%D0%BF"),
    ):
        disposition = client.get(path).headers["content-disposition"]
        assert f'filename="{fallback}"' in disposition
        assert marker in disposition


def test_viewer_can_export(client, viewer_client):
    """Выгрузка — чтение: наблюдателю она доступна."""
    assert viewer_client.get(f"{MATRIX}/export?level=sku").status_code == 200
    assert viewer_client.get(f"{PURCHASE}/export").status_code == 200
