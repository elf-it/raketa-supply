"""Отбор строк матрицы по статусу OVER/SHORT/OK (задача D-4).

Статус — не поле в базе, а результат расчёта, поэтому отбор идёт после расчёта.
Главное, что здесь проверяется: отбор ничего не теряет и не удваивает, а итоги
под таблицей сходятся с тем, что в ней показано.
"""

from __future__ import annotations

MATRIX = "/api/v1/matrix"
STATUSES = ("OK", "SHORT", "OVER")


def _rows(client, level: str, status: str | None = None) -> list[dict]:
    query = f"{MATRIX}?level={level}" + (f"&status={status}" if status else "")
    return client.get(query).json()["rows"]


def test_filter_keeps_only_rows_with_that_status(client):
    for level in ("sku", "store", "category"):
        everything = _rows(client, level)
        for status in STATUSES:
            picked = _rows(client, level, status)
            assert all(row["status"] == status for row in picked)
            assert len(picked) == len([r for r in everything if r["status"] == status])


def test_statuses_split_the_whole_selection(client):
    """Три отбора в сумме дают исходную выборку: строка не теряется и не двоится."""
    for level in ("sku", "store", "category"):
        everything = {row["key"] for row in _rows(client, level)}
        picked: list[str] = []
        for status in STATUSES:
            picked += [row["key"] for row in _rows(client, level, status)]
        assert sorted(picked) == sorted(everything)


def test_totals_follow_the_filter(client):
    """Итог под таблицей считается по показанным строкам, а не по всей выборке.

    Иначе в отборе «дефицит» под тремя строками стоял бы итог восемнадцати,
    и сверять его было бы не с чем.
    """
    payload = client.get(f"{MATRIX}?level=sku&status=SHORT").json()
    weeks = [week["key"] for week in payload["weeks"]]
    assert payload["rows"], "в демо-данных нет позиций со статусом SHORT"

    for metric in ("original_forecast", "adapted_stock"):
        for week in weeks:
            rows_sum = sum(row["metrics"][metric][week] for row in payload["rows"])
            assert abs(rows_sum - payload["totals"]["metrics"][metric][week]) < 1e-6

    full = client.get(f"{MATRIX}?level=sku").json()
    assert payload["context"]["sku_count"] == len(payload["rows"])
    assert payload["context"]["sku_count_total"] == full["context"]["sku_count"]
    assert payload["context"]["filters"]["status"] == "SHORT"
    # KPI — те же строки: иначе плитки считают одно, а таблица показывает другое.
    assert payload["kpi"]["sku_count"] == len(payload["rows"])
    assert payload["kpi"]["short_skus"] == len(payload["rows"])


def test_group_level_filters_whole_groups(client):
    """Магазин с дефицитом остаётся магазином целиком, а не списком плохих позиций.

    Статус группы — худший из статусов её позиций. Если бы отбор выкидывал из группы
    благополучные артикулы, итог по магазину перестал бы быть итогом по магазину.
    """
    everything = _rows(client, "store")
    for status in STATUSES:
        picked = _rows(client, "store", status)
        for row in picked:
            same = next(r for r in everything if r["key"] == row["key"])
            assert row["meta"]["sku_count"] == same["meta"]["sku_count"]

    payload = client.get(f"{MATRIX}?level=store&status=SHORT").json()
    weeks = [week["key"] for week in payload["weeks"]]
    for week in weeks:
        rows_sum = sum(row["metrics"]["original_forecast"][week] for row in payload["rows"])
        assert abs(rows_sum - payload["totals"]["metrics"]["original_forecast"][week]) < 1e-6


def test_empty_selection_is_not_an_error(client):
    """Пустой отбор — обычный ответ: таблица покажет «ничего не попало»."""
    payload = client.get(f"{MATRIX}?level=sku&search=такого-товара-нет&status=SHORT").json()
    assert payload["rows"] == []
    assert payload["context"]["sku_count"] == 0
    assert payload["kpi"]["sku_count"] == 0
    week = payload["weeks"][0]["key"]
    assert payload["totals"]["metrics"]["original_forecast"][week] == 0


def test_unknown_status_is_rejected(client):
    assert client.get(f"{MATRIX}?level=sku&status=НЕТ").status_code == 422


def test_export_says_what_the_filter_hid(client):
    """В файле отбор подписан: иначе три строки выглядят как потерянные данные."""
    from io import BytesIO

    from openpyxl import load_workbook

    payload = client.get(f"{MATRIX}?level=sku&status=SHORT").json()
    response = client.get(f"{MATRIX}/export?level=sku&status=SHORT")
    assert response.status_code == 200

    sheet = load_workbook(BytesIO(response.content)).active
    header = [
        str(cell.value)
        for row in sheet.iter_rows(max_row=12)
        for cell in row
        if cell.value is not None
    ]
    assert "Отбор по статусу" in header
    mark = next(value for value in header if value.startswith("дефицит"))
    assert f"{payload['context']['sku_count']} из {payload['context']['sku_count_total']}" in mark

    vendor_codes = {row["meta"]["vendor_code"] for row in payload["rows"]}
    assert vendor_codes and vendor_codes <= set(header) | {
        str(cell.value) for row in sheet.iter_rows() for cell in row if cell.value is not None
    }
