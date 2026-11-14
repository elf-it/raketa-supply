"""Добавление позиции в существующий черновик (задача B-13).

Главная проверка — та же, что у правки строк: дописанная позиция должна дойти
до расчёта. После размещения она обязана оказаться в пути наравне с теми, с
которыми заказ создавали, иначе «добавить» означало бы только строку в файле.

Второе: чего добавить нельзя. Товар другого поставщика — заказ уходит поставщику
целиком. Вторая строка той же позиции — иначе человек незаметно для себя закажет
её дважды. И ничего нельзя дописать в размещённый заказ: он уже у поставщика.
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


def _cancel(client, order_id: int) -> None:
    assert client.post(f"{API}/{order_id}/status", json={"status": "cancelled"}).status_code == 200


def _add(client, order_id: int, row: dict) -> dict:
    response = client.post(f"{API}/{order_id}/lines", json=_line_of(row))
    assert response.status_code == 201, response.text
    return response.json()


def _two_of_one_supplier(client) -> tuple[dict, dict]:
    by_supplier: dict[int | None, list[dict]] = {}
    for row in _needed_rows(client):
        by_supplier.setdefault(row["supplier_id"], []).append(row)
    for rows in by_supplier.values():
        if len(rows) >= 2:
            return rows[0], rows[1]
    raise AssertionError("в демо-данных нет поставщика с двумя позициями к заказу")


def _of_another_supplier(client, row: dict) -> dict:
    other = next(
        (r for r in _needed_rows(client) if r["supplier_id"] != row["supplier_id"]),
        None,
    )
    assert other is not None, "в демо-данных все позиции у одного поставщика"
    return other


def _supply(client, pair_id: int, week: str) -> float:
    matrix = client.get("/api/v1/matrix?level=sku").json()
    line = next(r for r in matrix["rows"] if r["meta"]["pair_id"] == pair_id)
    return line["metrics"]["supply"].get(week) or 0.0


def test_forgotten_position_joins_the_draft(client):
    """Второй документ тому же поставщику больше не нужен."""
    first, second = _two_of_one_supplier(client)
    order = _create(client, [first])
    try:
        grown = _add(client, order["id"], second)
        added = next(line for line in grown["lines"] if line["pair_id"] == second["pair_id"])

        assert grown["lines_count"] == 2
        assert grown["supplier"] == order["supplier"]
        assert added["qty"] == second["qty"]
        assert added["arrival_week"] == second["arrival_week"]
        assert grown["total_qty"] == first["qty"] + second["qty"]
        assert grown["total_cost"] == approx(order["total_cost"] + added["cost"])
        # Цена строки — снимок карточки товара на момент, когда строка попала
        # в документ, а не пересчёт по текущему справочнику при чтении.
        assert added["cost"] == approx(second["qty"] * second["params"]["cost_price"])
    finally:
        _cancel(client, order["id"])


def test_added_line_reaches_the_calculation(client):
    """Дописанная позиция — такая же строка заказа: в пути она тоже окажется."""
    first, second = _two_of_one_supplier(client)
    order = _create(client, [first])
    before = _supply(client, second["pair_id"], second["arrival_week"])
    _add(client, order["id"], second)
    try:
        placed = client.post(f"{API}/{order['id']}/status", json={"status": "placed"})
        assert placed.status_code == 200, placed.text

        assert _supply(client, second["pair_id"], second["arrival_week"]) - before == approx(
            second["qty"]
        )
    finally:
        _cancel(client, order["id"])
        # Отмена убирает приход и дописанной строки тоже.
        assert _supply(client, second["pair_id"], second["arrival_week"]) == approx(before)


def test_another_suppliers_goods_are_refused(client):
    """Смешанный заказ нельзя ни отправить, ни принять как единое целое."""
    row = _needed_rows(client)[0]
    stranger = _of_another_supplier(client, row)
    order = _create(client, [row])
    try:
        response = client.post(f"{API}/{order['id']}/lines", json=_line_of(stranger))

        assert response.status_code == 422
        detail = response.json()["detail"]
        # В отказе названы оба поставщика: иначе непонятно, что именно не так.
        assert stranger["supplier"] in detail
        assert row["supplier"] in detail
        assert client.get(f"{API}/{order['id']}").json()["lines_count"] == 1
    finally:
        _cancel(client, order["id"])


def test_a_position_is_not_added_twice(client):
    """Молча сложить количество нельзя: это заказ вдвое больше задуманного."""
    row = _needed_rows(client)[0]
    order = _create(client, [row])
    try:
        response = client.post(f"{API}/{order['id']}/lines", json=_line_of(row))

        assert response.status_code == 422
        assert "уже есть в заказе" in response.json()["detail"]
        current = client.get(f"{API}/{order['id']}").json()
        assert current["lines_count"] == 1
        assert current["total_qty"] == row["qty"]
    finally:
        _cancel(client, order["id"])


def test_arrival_week_is_checked_as_everywhere_else(client):
    """Приход в закрытую неделю расчёт не учтёт — значит, и дописать его нельзя."""
    first, second = _two_of_one_supplier(client)
    closed = client.get("/api/v1/matrix?level=sku").json()["context"]["last_closed_week"]
    order = _create(client, [first])
    try:
        past = client.post(
            f"{API}/{order['id']}/lines", json=dict(_line_of(second), arrival_week=closed)
        )
        assert past.status_code == 422
        assert "закрыта" in past.json()["detail"]

        nonsense = client.post(
            f"{API}/{order['id']}/lines", json=dict(_line_of(second), arrival_week="2026-W99")
        )
        assert nonsense.status_code == 422
        assert "ISO-неделю" in nonsense.json()["detail"]

        assert client.get(f"{API}/{order['id']}").json()["lines_count"] == 1
    finally:
        _cancel(client, order["id"])


def test_placed_order_takes_no_new_lines(client):
    """Размещённый заказ уже у поставщика: дописывать в него нечего."""
    first, second = _two_of_one_supplier(client)
    order = _create(client, [first])
    client.post(f"{API}/{order['id']}/status", json={"status": "placed"})
    try:
        response = client.post(f"{API}/{order['id']}/lines", json=_line_of(second))

        assert response.status_code == 409
        assert "черновике" in response.json()["detail"]
        assert client.get(f"{API}/{order['id']}").json()["lines_count"] == 1
    finally:
        _cancel(client, order["id"])


def test_unknown_order_and_position(client):
    row = _needed_rows(client)[0]
    order = _create(client, [row])
    try:
        assert client.post(f"{API}/999999/lines", json=_line_of(row)).status_code == 404

        unknown = client.post(
            f"{API}/{order['id']}/lines", json=dict(_line_of(row), pair_id=999999)
        )
        assert unknown.status_code == 422
        assert "не найдены" in unknown.json()["detail"]

        # Количество проверяет схема: нулевую строку заказывать бессмысленно.
        assert (
            client.post(f"{API}/{order['id']}/lines", json=dict(_line_of(row), qty=0)).status_code
            == 422
        )
    finally:
        _cancel(client, order["id"])


def test_viewer_cannot_add_a_line(client, viewer_client):
    first, second = _two_of_one_supplier(client)
    order = _create(client, [first])
    try:
        response = viewer_client.post(f"{API}/{order['id']}/lines", json=_line_of(second))
        assert response.status_code == 403
        assert client.get(f"{API}/{order['id']}").json()["lines_count"] == 1
    finally:
        _cancel(client, order["id"])
