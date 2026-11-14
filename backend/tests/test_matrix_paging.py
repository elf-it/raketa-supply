"""Постраничная выдача матрицы (задача G-10).

Экран держит в DOM только видимые строки, но до этого браузер получал всю
выборку целиком: на тысяче позиций ответ ``/matrix`` — это шесть мегабайт JSON.
Окно строк отрезает передачу, и здесь проверяется главное: оно режет только
``rows``. Итоги, KPI и счётчики остаются по всей выборке — иначе итог под
таблицей менялся бы от того, насколько человек успел прокрутить.
"""

from __future__ import annotations

MATRIX = "/api/v1/matrix"


def _payload(client, query: str = "") -> dict:
    response = client.get(f"{MATRIX}?{query}")
    assert response.status_code == 200, response.text
    return response.json()


def _keys(payload: dict) -> list[str]:
    return [row["key"] for row in payload["rows"]]


def test_windows_add_up_to_the_whole_selection(client):
    """Страницы подряд дают ту же выборку в том же порядке: ни пропуска, ни дубля."""
    full = _payload(client, "level=sku")
    assert len(full["rows"]) > 6, "демо-каталог слишком мал, чтобы проверить страницы"

    collected: list[str] = []
    offset = 0
    while True:
        page = _payload(client, f"level=sku&offset={offset}&limit=5")
        if not page["rows"]:
            break
        assert len(page["rows"]) <= 5
        collected += _keys(page)
        offset += 5

    assert collected == _keys(full)


def test_totals_and_kpi_ignore_the_window(client):
    """Итоги и KPI — по всей выборке, а не по странице.

    Это и есть причина, по которой окно живёт в сервисе, а не в запросе к базе:
    строки урезаются после расчёта, а расчёт всё равно нужен весь.
    """
    full = _payload(client, "level=sku")
    page = _payload(client, "level=sku&offset=2&limit=3")

    assert len(page["rows"]) == 3
    assert page["totals"] == full["totals"]
    assert page["kpi"] == full["kpi"]
    assert page["context"]["sku_count"] == full["context"]["sku_count"]
    assert page["context"]["sku_count_total"] == full["context"]["sku_count_total"]
    assert page["context"]["row_count"] == len(full["rows"])
    assert page["context"]["row_offset"] == 2
    assert page["context"]["row_limit"] == 3
    # Без окна сервер по-прежнему отдаёт всё: выгрузка и сверка на это опираются.
    assert full["context"]["row_limit"] is None
    assert full["context"]["row_count"] == len(full["rows"])


def test_pair_ids_cover_the_whole_selection(client):
    """Список пар приходит целиком даже с окном в три строки.

    «Отметить все» и массовая правка работают по нему: без него правка «ко всем
    строкам под фильтрами» молча накрыла бы одну подгруженную страницу.
    """
    full = _payload(client, "level=sku")
    page = _payload(client, "level=sku&limit=3")

    expected = [row["meta"]["pair_id"] for row in full["rows"]]
    assert page["context"]["pair_ids"] == expected
    assert len(page["context"]["pair_ids"]) == page["context"]["row_count"]

    # На уровнях групп массовой правки нет, и список пар там не нужен.
    assert _payload(client, "level=store&limit=1")["context"]["pair_ids"] is None


def test_status_filter_counts_before_the_window(client):
    """Строк в выборке столько, сколько прошло отбор, а не сколько отдано."""
    filtered = _payload(client, "level=sku&status=SHORT")
    assert filtered["rows"], "в демо-данных нет позиций со статусом SHORT"

    page = _payload(client, "level=sku&status=SHORT&limit=1")
    assert len(page["rows"]) == 1
    assert page["rows"][0]["status"] == "SHORT"
    assert page["context"]["row_count"] == len(filtered["rows"])
    assert page["totals"] == filtered["totals"]


def test_group_level_pages_by_groups(client):
    """На уровне групп страница — это группы, а не позиции внутри них."""
    full = _payload(client, "level=category")
    assert len(full["rows"]) > 1

    page = _payload(client, "level=category&limit=1")
    assert _keys(page) == _keys(full)[:1]
    assert page["context"]["row_count"] == len(full["rows"])
    # Итог по всем категориям, а не по одной показанной.
    assert page["totals"] == full["totals"]
    assert page["context"]["sku_count"] == full["context"]["sku_count"]


def test_offset_past_the_end_is_empty_not_an_error(client):
    page = _payload(client, "level=sku&offset=10000&limit=5")
    assert page["rows"] == []
    assert page["context"]["row_count"] > 0
    # Таблица по этому ответу поймёт, что строки есть, просто не здесь.
    assert page["context"]["row_offset"] == 10000


def test_broken_window_is_rejected(client):
    assert client.get(f"{MATRIX}?level=sku&offset=-1").status_code == 422
    assert client.get(f"{MATRIX}?level=sku&limit=0").status_code == 422


def test_export_ignores_the_window(client):
    """В файл идёт вся выборка, а не та страница, до которой доскроллили."""
    from io import BytesIO

    from openpyxl import load_workbook

    full = _payload(client, "level=sku")
    response = client.get(f"{MATRIX}/export?level=sku&limit=1&offset=5")
    assert response.status_code == 200

    sheet = load_workbook(BytesIO(response.content)).active
    values = {
        str(cell.value) for row in sheet.iter_rows() for cell in row if cell.value is not None
    }
    vendor_codes = {row["meta"]["vendor_code"] for row in full["rows"]}
    assert vendor_codes <= values
