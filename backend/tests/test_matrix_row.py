"""Точечное обновление строки после правки (задача G-11).

Ручка `/matrix/row` отдаёт ту же матрицу с теми же фильтрами, только строк в ней
одна. Поэтому главная проверка — совпадение: строка, итоги и KPI должны быть
ровно такими же, какими их отдаёт обычный запрос матрицы. Расхождение означало бы,
что экран после правки показывает не то же, что показал бы после перезагрузки.
"""

from __future__ import annotations

MATRIX = "/api/v1/matrix?level=sku"
ROW = "/api/v1/matrix/row?level=sku"


def _full(client, url: str = MATRIX) -> dict:
    response = client.get(url)
    assert response.status_code == 200, response.text
    return response.json()


def _point(client, pair_id: int, url: str = ROW) -> dict:
    response = client.get(f"{url}&pair_id={pair_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _pair_of(row: dict) -> int:
    return row["meta"]["pair_id"]


def test_row_repeats_the_full_matrix(client):
    full = _full(client)
    index = 2
    expected = full["rows"][index]

    point = _point(client, _pair_of(expected))

    assert point["rows"] == [expected]
    assert point["totals"] == full["totals"]
    assert point["kpi"] == full["kpi"]
    assert point["weeks"] == full["weeks"]
    assert point["context"]["row_count"] == full["context"]["row_count"]
    assert point["context"]["sku_count"] == full["context"]["sku_count"]
    assert point["context"]["pair_ids"] == full["context"]["pair_ids"]


def test_row_says_where_in_the_selection_it_sits(client):
    """Экран кладёт строку на её место, а не ищет на глаз."""
    full = _full(client)
    index = 3

    point = _point(client, _pair_of(full["rows"][index]))

    assert point["context"]["row_offset"] == index
    assert point["context"]["row_limit"] == 1


def test_row_is_much_smaller_than_the_matrix(client):
    """Ради чего всё: ответ на правку не тащит за собой всю выборку."""
    full = client.get(MATRIX)
    point = client.get(f"{ROW}&pair_id={_pair_of(full.json()['rows'][0])}")

    assert len(point.content) * 3 < len(
        full.content
    ), "на демо-каталоге из 18 строк выигрыш и так заметен; на тысяче он кратный"


def test_row_shows_the_edit_and_the_new_totals(client):
    before = _full(client)
    row = before["rows"][0]
    pair_id = _pair_of(row)
    week = next(w["key"] for w in before["weeks"] if not w["is_closed"])

    edit = client.put(f"/api/v1/forecast/{pair_id}/{week}", json={"manual_forecast": 4242})
    assert edit.status_code == 200, edit.text
    try:
        point = _point(client, pair_id)

        assert point["rows"][0]["metrics"]["original_forecast"][week] == 4242
        assert (
            point["totals"]["metrics"]["original_forecast"][week]
            != (before["totals"]["metrics"]["original_forecast"][week])
        )
        # И это ровно то, что показал бы обычный запрос.
        assert point["rows"] == [_full(client)["rows"][0]]
        assert point["totals"] == _full(client)["totals"]
    finally:
        client.put(f"/api/v1/forecast/{pair_id}/{week}", json={"clear": ["manual_forecast"]})


def test_row_leaves_the_selection_when_the_status_filter_no_longer_holds(client):
    """Правка может вытолкнуть строку из отбора по статусу.

    Тогда точечно обновлять нечего: за выбывшей строкой сдвинулись все остальные,
    и экран перечитывает выборку целиком. Пустой список строк — это ответ, а не
    ошибка, поэтому и статус 200.
    """
    ok_rows = _full(client, f"{MATRIX}&status=OK")["rows"]
    assert ok_rows, "в демо-данных нет позиций со статусом ОК"
    pair_id = _pair_of(ok_rows[0])
    week = next(w["key"] for w in _full(client)["weeks"] if not w["is_closed"])

    edit = client.put(f"/api/v1/forecast/{pair_id}/{week}", json={"manual_forecast": 99999})
    assert edit.status_code == 200, edit.text
    try:
        point = _point(client, pair_id, f"{ROW}&status=OK")

        assert point["rows"] == []
        assert point["context"]["row_count"] == len(ok_rows) - 1
        # Итоги при этом честные: они по новой выборке, уже без этой строки.
        assert point["totals"] == _full(client, f"{MATRIX}&status=OK")["totals"]
    finally:
        client.put(f"/api/v1/forecast/{pair_id}/{week}", json={"clear": ["manual_forecast"]})


def test_group_level_returns_the_row_the_pair_lives_in(client):
    """На уровне магазинов меняется строка магазина, а не строка позиции."""
    sku = _full(client)["rows"][0]
    stores = _full(client, "/api/v1/matrix?level=store")

    point = _point(client, _pair_of(sku), "/api/v1/matrix/row?level=store")

    assert len(point["rows"]) == 1
    assert point["rows"][0]["key"] == f"store-{sku['meta']['store_id']}"
    assert point["rows"] == [r for r in stores["rows"] if r["key"] == point["rows"][0]["key"]]


def test_unknown_pair_is_not_found(client):
    assert client.get(f"{ROW}&pair_id=999999").status_code == 404


def test_pair_id_is_required(client):
    assert client.get(MATRIX.replace("/matrix?", "/matrix/row?")).status_code == 422


def test_viewer_may_read_a_row(viewer_client):
    full = _full(viewer_client)
    assert _point(viewer_client, _pair_of(full["rows"][0]))["rows"]
