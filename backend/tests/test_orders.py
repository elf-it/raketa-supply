"""Заказы поставщикам: документ, который фиксирует решение человека.

Главная проверка — та, ради которой заказ и заведён: размещённый заказ уменьшает
остаток потребности, а отменённый возвращает её обратно.
"""

from __future__ import annotations

API = "/api/v1/orders"


def _first_needed(client) -> dict:
    plan = client.get("/api/v1/purchase?only_needed=true").json()
    assert plan["rows"], "в демо-данных нет позиций к заказу"
    return plan["rows"][0]


def _row_by_pair(client, pair_id: int) -> dict | None:
    plan = client.get("/api/v1/purchase?only_needed=false").json()
    return next((r for r in plan["rows"] if r["pair_id"] == pair_id), None)


def _line_of(row: dict) -> dict:
    return {
        "pair_id": row["pair_id"],
        "qty": row["qty"],
        "arrival_week": row["arrival_week"],
    }


def _create(client, row: dict) -> dict:
    response = client.post(API, json={"lines": [_line_of(row)]})
    assert response.status_code == 201, response.text
    return response.json()


def _status(client, order_id: int, status: str) -> dict:
    response = client.post(f"{API}/{order_id}/status", json={"status": status})
    assert response.status_code == 200, response.text
    return response.json()


def test_draft_does_not_touch_the_calculation(client):
    """Черновик — ещё не заказ: потребность не должна от него меняться."""
    row = _first_needed(client)
    before = _row_by_pair(client, row["pair_id"])
    order = _create(client, row)
    try:
        after = _row_by_pair(client, row["pair_id"])
        assert order["status"] == "draft"
        assert after["qty"] == before["qty"]
        assert after["needed"] is True
    finally:
        _status(client, order["id"], "cancelled")


def test_placed_order_reduces_remaining_need(client):
    """Размещение превращает строки в товары в пути, и потребность падает."""
    row = _first_needed(client)
    pair_id = row["pair_id"]
    before = _row_by_pair(client, pair_id)

    order = _create(client, row)
    try:
        placed = _status(client, order["id"], "placed")
        assert placed["placed_at"] is not None

        after = _row_by_pair(client, pair_id)
        assert after["qty"] < before["qty"] or after["needed"] is False
    finally:
        _status(client, order["id"], "cancelled")

    # Отмена убирает ожидаемый приход, и потребность возвращается к исходной.
    restored = _row_by_pair(client, pair_id)
    assert restored["qty"] == before["qty"]


def test_received_order_keeps_the_supply(client):
    """Принятый заказ — это уже пришедший товар, из плана он не исчезает."""
    row = _first_needed(client)
    order = _create(client, row)
    _status(client, order["id"], "placed")
    received = _status(client, order["id"], "received")

    assert received["status"] == "received"
    assert received["closed_at"] is not None
    assert received["next_statuses"] == []

    matrix = client.get("/api/v1/matrix?level=sku").json()
    line = received["lines"][0]
    pair_row = next(r for r in matrix["rows"] if r["meta"]["pair_id"] == line["pair_id"])
    supply = pair_row["metrics"]["supply"].get(line["arrival_week"])
    assert supply is not None and supply >= line["qty"]


def test_receiving_early_does_not_bring_the_need_back(client):
    """Отметка «принят» до недели прихода не должна возрождать потребность.

    Факта по будущей неделе ещё нет, значит товар в остатке не учтён, и выкидывать
    его из ожидаемого прихода нельзя — иначе система попросила бы купить то же самое.
    """
    row = _first_needed(client)
    pair_id = row["pair_id"]
    order = _create(client, row)
    _status(client, order["id"], "placed")
    after_placed = _row_by_pair(client, pair_id)

    _status(client, order["id"], "received")
    after_received = _row_by_pair(client, pair_id)

    assert after_received["qty"] == after_placed["qty"]
    assert after_received["needed"] == after_placed["needed"]


def test_status_transitions_are_restricted(client):
    row = _first_needed(client)
    order = _create(client, row)
    try:
        # Из черновика нельзя сразу в «принят».
        response = client.post(f"{API}/{order['id']}/status", json={"status": "received"})
        assert response.status_code == 409
        assert "Нельзя перевести" in response.json()["detail"]
    finally:
        _status(client, order["id"], "cancelled")

    # Отменённый заказ конечен.
    response = client.post(f"{API}/{order['id']}/status", json={"status": "placed"})
    assert response.status_code == 409


def test_order_keeps_one_supplier(client):
    """Заказ уходит одному поставщику: смешанный документ некому отправить."""
    plan = client.get("/api/v1/purchase?only_needed=false").json()
    by_supplier: dict[str, dict] = {}
    for row in plan["rows"]:
        by_supplier.setdefault(row["supplier"], row)
    assert len(by_supplier) > 1, "в демо-данных один поставщик — проверка бессмысленна"

    lines = [_line_of(row) for row in list(by_supplier.values())[:2]]
    response = client.post(API, json={"lines": lines})
    assert response.status_code == 422
    assert "разных поставщиков" in response.json()["detail"]


def test_order_rejects_duplicate_and_empty_lines(client):
    row = _first_needed(client)
    line = _line_of(row)

    assert client.post(API, json={"lines": [line, line]}).status_code == 422
    # Пустой список отсекает уже схема запроса.
    assert client.post(API, json={"lines": []}).status_code == 422


def test_order_remembers_cost_at_creation(client):
    """Сумма заказа не должна меняться задним числом вслед за карточкой товара."""
    row = _first_needed(client)
    order = _create(client, row)
    try:
        assert order["total_cost"] == row["cost"]
        assert order["total_qty"] == row["qty"]
        assert order["created_by"] == "Анна, снабжение"
        assert order["supplier"] == row["supplier"]
    finally:
        _status(client, order["id"], "cancelled")


def test_orders_are_listed_and_filtered(client):
    row = _first_needed(client)
    order = _create(client, row)
    try:
        drafts = client.get(f"{API}?status=draft").json()
        assert any(item["id"] == order["id"] for item in drafts)
        placed = client.get(f"{API}?status=placed").json()
        assert all(item["id"] != order["id"] for item in placed)
    finally:
        _status(client, order["id"], "cancelled")


def test_viewer_cannot_create_or_move_orders(client, viewer_client):
    row = _first_needed(client)
    assert viewer_client.post(API, json={"lines": [_line_of(row)]}).status_code == 403

    order = _create(client, row)
    try:
        response = viewer_client.post(f"{API}/{order['id']}/status", json={"status": "placed"})
        assert response.status_code == 403
        # Читать заказы наблюдателю можно.
        assert viewer_client.get(API).status_code == 200
    finally:
        _status(client, order["id"], "cancelled")


def test_timestamps_carry_the_time_zone(client):
    """Без явной зоны браузер принял бы время UTC за местное и сдвинул момент."""
    row = _first_needed(client)
    order = _create(client, row)
    try:
        placed = _status(client, order["id"], "placed")
        for moment in (order["created_at"], placed["placed_at"]):
            assert moment.endswith("Z") or moment.endswith("+00:00"), moment
    finally:
        _status(client, order["id"], "cancelled")


def test_export_is_a_readable_workbook(client):
    """Файл уходит поставщику, поэтому проверяется не ответ, а содержимое книги."""
    from io import BytesIO

    from openpyxl import load_workbook

    row = _first_needed(client)
    order = _create(client, row)
    try:
        response = client.get(f"{API}/{order['id']}/export")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument"
        )

        sheet = load_workbook(BytesIO(response.content)).active
        text = [
            str(cell.value) for line in sheet.iter_rows() for cell in line if cell.value is not None
        ]
        assert f"Заказ поставщику № {order['id']}" in text
        assert row["supplier"] in text
        assert row["title"] in text
        assert row["vendor_code"] in text
        # Итог в файле должен сходиться с итогом заказа, а не считаться заново.
        assert float(order["total_cost"]) in [float(value) for value in text if _is_number(value)]
    finally:
        _status(client, order["id"], "cancelled")


def _is_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def test_export_filename_survives_cyrillic(client):
    """Кириллица в имени файла уходит по RFC 5987: заголовок — только latin-1."""
    row = _first_needed(client)
    order = _create(client, row)
    try:
        disposition = client.get(f"{API}/{order['id']}/export").headers["content-disposition"]
        assert f'filename="order-{order["id"]}.xlsx"' in disposition
        assert "filename*=UTF-8''" in disposition
        assert "%D0%97%D0%B0%D0%BA%D0%B0%D0%B7" in disposition  # «Заказ» в процентах
    finally:
        _status(client, order["id"], "cancelled")


def test_viewer_can_export(client, viewer_client):
    """Выгрузка — чтение: наблюдателю она доступна."""
    row = _first_needed(client)
    order = _create(client, row)
    try:
        assert viewer_client.get(f"{API}/{order['id']}/export").status_code == 200
    finally:
        _status(client, order["id"], "cancelled")


def test_unknown_order_is_404(client):
    assert client.get(f"{API}/999999").status_code == 404
    assert client.get(f"{API}/999999/export").status_code == 404
    assert client.post(f"{API}/999999/status", json={"status": "placed"}).status_code == 404
