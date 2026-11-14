"""Правка черновика заказа (B-12).

Главное, что здесь проверяется: правка доходит до расчёта. Черновик не виден
формулам, но после размещения его строки становятся поставками, и в пути должно
оказаться исправленное количество в исправленной неделе — иначе правка была
косметической, а система продолжала считать по прежним числам.

Второе: размещённый заказ не правится. Он уже ушёл поставщику, и расхождение
между нашим документом и его копией опаснее неудобства пересобрать заказ.
"""

from __future__ import annotations

from pytest import approx

API = "/api/v1/orders"


def _needed_rows(client) -> list[dict]:
    plan = client.get("/api/v1/purchase?only_needed=true").json()
    rows = [row for row in plan["rows"] if row["qty"] > 0 and row["arrival_week"]]
    assert rows, "в демо-данных нет позиций к заказу"
    return rows


def _line_of(row: dict) -> dict:
    return {"pair_id": row["pair_id"], "qty": row["qty"], "arrival_week": row["arrival_week"]}


def _create(client, rows: list[dict]) -> dict:
    response = client.post(API, json={"lines": [_line_of(row) for row in rows]})
    assert response.status_code == 201, response.text
    return response.json()


def _two_of_one_supplier(client) -> list[dict]:
    by_supplier: dict[str, list[dict]] = {}
    for row in _needed_rows(client):
        by_supplier.setdefault(row["supplier"], []).append(row)
    for rows in by_supplier.values():
        if len(rows) >= 2:
            return rows[:2]
    raise AssertionError("в демо-данных нет поставщика с двумя позициями к заказу")


def _cancel(client, order_id: int) -> None:
    assert client.post(f"{API}/{order_id}/status", json={"status": "cancelled"}).status_code == 200


def _row_by_pair(client, pair_id: int) -> dict | None:
    plan = client.get("/api/v1/purchase?only_needed=false").json()
    return next((r for r in plan["rows"] if r["pair_id"] == pair_id), None)


def _last_closed(client) -> str:
    return client.get("/api/v1/matrix?level=sku").json()["context"]["last_closed_week"]


def _supply(client, pair_id: int, week: str) -> float:
    matrix = client.get("/api/v1/matrix?level=sku").json()
    row = next(r for r in matrix["rows"] if r["meta"]["pair_id"] == pair_id)
    return row["metrics"]["supply"].get(week) or 0.0


def test_quantity_and_week_are_editable(client):
    """Правка строки меняет количество, неделю и сумму заказа."""
    row = _needed_rows(client)[0]
    order = _create(client, [row])
    try:
        line = order["lines"][0]
        unit_cost = line["cost"] / line["qty"]
        later = client.get("/api/v1/matrix?level=sku").json()["weeks"][-1]["key"]

        response = client.patch(
            f"{API}/{order['id']}/lines/{line['id']}",
            json={"qty": line["qty"] + 7, "arrival_week": later},
        )
        assert response.status_code == 200, response.text
        edited = response.json()
        changed = edited["lines"][0]

        assert changed["qty"] == line["qty"] + 7
        assert changed["arrival_week"] == later
        assert edited["total_qty"] == line["qty"] + 7
        # Цена за штуку — снимок на момент создания заказа, и правка её не трогает.
        assert changed["cost"] == approx(changed["qty"] * unit_cost)
        assert edited["total_cost"] == approx(changed["cost"])
    finally:
        _cancel(client, order["id"])


def test_edit_reaches_the_calculation(client):
    """Размещается исправленный заказ, а не тот, что собрали изначально."""
    row = _needed_rows(client)[0]
    pair_id = row["pair_id"]
    order = _create(client, [row])
    line = order["lines"][0]
    week = line["arrival_week"]
    reduced = max(1, line["qty"] // 2)
    assert reduced < line["qty"], "количество в демо-данных слишком мало для правки"

    before_supply = _supply(client, pair_id, week)
    before_need = _row_by_pair(client, pair_id)["qty"]

    client.patch(f"{API}/{order['id']}/lines/{line['id']}", json={"qty": reduced})
    try:
        placed = client.post(f"{API}/{order['id']}/status", json={"status": "placed"})
        assert placed.status_code == 200, placed.text

        # В пути оказалось исправленное количество, а не то, что собрали изначально.
        assert _supply(client, pair_id, week) - before_supply == approx(reduced)

        # Урезанный заказ потребность уменьшил, но закрыл не целиком.
        after = _row_by_pair(client, pair_id)
        assert after["qty"] < before_need
        assert after["qty"] > 0 or after["needed"] is False
    finally:
        _cancel(client, order["id"])


def test_line_can_be_removed_but_not_the_last_one(client):
    """Позицию из черновика убирают, но пустой заказ оставить нельзя."""
    rows = _two_of_one_supplier(client)
    order = _create(client, rows)
    try:
        first, second = order["lines"][0], order["lines"][1]
        response = client.delete(f"{API}/{order['id']}/lines/{first['id']}")
        assert response.status_code == 200, response.text
        left = response.json()
        assert left["lines_count"] == 1
        assert left["lines"][0]["id"] == second["id"]
        assert left["total_qty"] == second["qty"]

        # Последняя строка держит заказ: его отменяют целиком, а не опустошают.
        response = client.delete(f"{API}/{order['id']}/lines/{second['id']}")
        assert response.status_code == 422
        assert "последняя строка" in response.json()["detail"]
    finally:
        _cancel(client, order["id"])


def test_placed_order_is_frozen(client):
    """Размещённый заказ уже у поставщика: правка задним числом запрещена."""
    row = _needed_rows(client)[0]
    order = _create(client, [row])
    line = order["lines"][0]
    client.post(f"{API}/{order['id']}/status", json={"status": "placed"})
    try:
        edits = (
            client.patch(f"{API}/{order['id']}/lines/{line['id']}", json={"qty": 1}),
            client.delete(f"{API}/{order['id']}/lines/{line['id']}"),
            client.patch(f"{API}/{order['id']}", json={"note": "поздно"}),
        )
        for response in edits:
            assert response.status_code == 409, response.text
            assert "черновике" in response.json()["detail"]

        # Заказ не пострадал: строка и количество на месте.
        current = client.get(f"{API}/{order['id']}").json()
        assert current["lines"][0]["qty"] == line["qty"]
    finally:
        _cancel(client, order["id"])


def test_arrival_week_must_be_an_open_week(client):
    """Приход в закрытую неделю расчёт не увидит, поэтому не принимается."""
    row = _needed_rows(client)[0]
    closed = _last_closed(client)
    order = _create(client, [row])
    try:
        line = order["lines"][0]
        response = client.patch(
            f"{API}/{order['id']}/lines/{line['id']}", json={"arrival_week": closed}
        )
        assert response.status_code == 422
        assert "закрыта" in response.json()["detail"]

        # Та же проверка на входе: закрытую неделю нельзя задать и при создании.
        bad = dict(_line_of(row), arrival_week=closed)
        assert client.post(API, json={"lines": [bad]}).status_code == 422

        # Неделя вне календаря — тоже отказ, а не строка, которую никто не найдёт.
        nonsense = client.patch(
            f"{API}/{order['id']}/lines/{line['id']}", json={"arrival_week": "2026-W99"}
        )
        assert nonsense.status_code == 422
        assert "ISO-неделю" in nonsense.json()["detail"]
    finally:
        _cancel(client, order["id"])


def test_empty_patch_is_rejected(client):
    """Пустая правка — опечатка в имени поля, а не успешное ничего."""
    row = _needed_rows(client)[0]
    order = _create(client, [row])
    try:
        response = client.patch(f"{API}/{order['id']}/lines/{order['lines'][0]['id']}", json={})
        assert response.status_code == 422
        assert "Нечего менять" in response.json()["detail"]

        # Обнулить количество нельзя: позицию убирают из заказа.
        zero = client.patch(f"{API}/{order['id']}/lines/{order['lines'][0]['id']}", json={"qty": 0})
        assert zero.status_code == 422
    finally:
        _cancel(client, order["id"])


def test_note_edit_reaches_the_file(client):
    """Примечание уходит поставщику в шапке файла — значит, его правят до отправки."""
    from io import BytesIO

    from openpyxl import load_workbook

    row = _needed_rows(client)[0]
    order = _create(client, [row])
    try:
        response = client.patch(
            f"{API}/{order['id']}", json={"note": "  Отгрузить одной машиной  "}
        )
        assert response.status_code == 200, response.text
        assert response.json()["note"] == "Отгрузить одной машиной"

        sheet = load_workbook(BytesIO(client.get(f"{API}/{order['id']}/export").content)).active
        text = [cell.value for line in sheet.iter_rows() for cell in line if cell.value]
        assert "Отгрузить одной машиной" in text

        # Пустая строка — это «нет примечания», а не примечание из пробелов.
        cleared = client.patch(f"{API}/{order['id']}", json={"note": "   "})
        assert cleared.json()["note"] is None
    finally:
        _cancel(client, order["id"])


def test_unknown_order_and_line(client):
    row = _needed_rows(client)[0]
    order = _create(client, [row])
    try:
        assert client.patch(f"{API}/{order['id']}/lines/999999", json={"qty": 1}).status_code == 404
        assert client.delete(f"{API}/{order['id']}/lines/999999").status_code == 404
        assert client.patch(f"{API}/999999/lines/1", json={"qty": 1}).status_code == 404
        assert client.patch(f"{API}/999999", json={"note": "нет"}).status_code == 404
    finally:
        _cancel(client, order["id"])


def test_viewer_cannot_edit_a_draft(client, viewer_client):
    row = _needed_rows(client)[0]
    order = _create(client, [row])
    line_id = order["lines"][0]["id"]
    try:
        assert (
            viewer_client.patch(f"{API}/{order['id']}/lines/{line_id}", json={"qty": 1}).status_code
            == 403
        )
        assert viewer_client.delete(f"{API}/{order['id']}/lines/{line_id}").status_code == 403
        assert viewer_client.patch(f"{API}/{order['id']}", json={"note": "нет"}).status_code == 403
    finally:
        _cancel(client, order["id"])
