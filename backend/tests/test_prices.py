"""История цен и себестоимости (задача B-9).

Главная проверка — та, ради которой история вообще нужна: новая цена не стирает
прежнюю. До этой задачи правка карточки товара уничтожала старое значение, и на
вопрос «почему сентябрьский заказ был дешевле» ответа в системе не было.

Вторая: карточка и история не расходятся. Карточку читает расчёт закупки, и если
запись в историю не доходит до неё (или доходит не та), закупка считается по цене,
которой нет ни в одном документе. Поэтому после каждой правки истории тест
смотрит и на карточку, и на закупочную сумму.
"""

from __future__ import annotations

from datetime import date, timedelta

from pytest import approx

CATALOG = "/api/v1/catalog/products"
PURCHASE = "/api/v1/purchase?only_needed=false"
TODAY = date.today()


def _products(client) -> list[dict]:
    response = client.get(CATALOG)
    assert response.status_code == 200, response.text
    return response.json()


def _with_history(client) -> dict:
    """Товар, у которого цену уже меняли: в сиде это каждый третий."""
    for product in _products(client):
        card = _card(client, product["id"])
        if len(card["history"]) > 1:
            return product
    raise AssertionError("в демо-данных нет товара с историей цен")


def _card(client, product_id: int) -> dict:
    response = client.get(f"{CATALOG}/{product_id}/prices")
    assert response.status_code == 200, response.text
    return response.json()


def _put(client, product_id: int, **payload) -> dict:
    """Запись цены. Дата по умолчанию — сегодня: так цена становится действующей."""
    payload.setdefault("valid_from", TODAY.isoformat())
    response = client.post(f"{CATALOG}/{product_id}/prices", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _refuse(client, product_id: int, **payload) -> str:
    payload.setdefault("valid_from", TODAY.isoformat())
    response = client.post(f"{CATALOG}/{product_id}/prices", json=payload)
    assert response.status_code == 422, response.text
    return response.json()["detail"]


def _drop(client, product_id: int, price_id: int) -> dict:
    response = client.delete(f"{CATALOG}/{product_id}/prices/{price_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _row_on(card: dict, day: date) -> dict | None:
    return next((row for row in card["history"] if row["valid_from"] == day.isoformat()), None)


def _epoch() -> int:
    """Общий номер версии расчёта: по нему кэш узнаёт, что пора считать заново."""
    from app.db import SessionLocal
    from app.models import AppSettings

    with SessionLocal() as session:
        return session.get(AppSettings, 1).calc_epoch


def test_seed_history_agrees_with_the_card(client):
    """Нынешняя цена карточки — это последняя запись истории, а не что-то своё."""
    product = _with_history(client)
    card = _card(client, product["id"])

    newest = card["history"][0]
    assert card["cost_price"] == approx(newest["cost_price"])
    assert card["price"] == approx(newest["price"])
    assert card["valid_from"] == newest["valid_from"]
    assert product["price_valid_from"] == newest["valid_from"]
    # Записи идут от новой к старой, и подорожание в сиде именно подорожание.
    assert newest["cost_price"] > card["history"][-1]["cost_price"]


def test_new_price_does_not_erase_the_old_one(client):
    product = _with_history(client)
    before = _card(client, product["id"])
    was = before["cost_price"]

    card = _put(client, product["id"], cost_price=was + 7, note="счёт поставщика")
    added = _row_on(card, TODAY)
    try:
        assert card["cost_price"] == approx(was + 7)
        assert card["valid_from"] == TODAY.isoformat()
        assert added is not None and added["note"] == "счёт поставщика"
        assert added["author"], "автор берётся из сессии"
        # Прежние записи остались на месте — ради этого вся задача.
        assert card["history"][1:] == before["history"]
    finally:
        assert added is not None
        restored = _drop(client, product["id"], added["id"])

    assert restored["cost_price"] == approx(was)
    assert restored["history"] == before["history"]


def test_price_change_reaches_the_purchase_cost(client):
    """Себестоимость участвует в расчёте закупки: правка должна дойти до суммы."""
    rows = client.get(PURCHASE).json()["rows"]
    row = next(r for r in rows if r["qty"] > 0)
    product = next(p for p in _products(client) if p["vendor_code"] == row["vendor_code"])
    was = product["cost_price"]

    card = _put(client, product["id"], cost_price=was * 2)
    added = _row_on(card, TODAY)
    assert added is not None
    try:
        after = next(
            r for r in client.get(PURCHASE).json()["rows"] if r["pair_id"] == row["pair_id"]
        )

        assert after["params"]["cost_price"] == approx(was * 2)
        assert after["cost"] == approx(after["qty"] * was * 2)
    finally:
        _drop(client, product["id"], added["id"])

    back = next(r for r in client.get(PURCHASE).json()["rows"] if r["pair_id"] == row["pair_id"])
    assert back["params"]["cost_price"] == approx(was)


def test_one_value_can_be_changed_without_touching_the_other(client):
    """Правка себестоимости не должна обнулять цену продажи."""
    product = _products(client)[0]
    before = _card(client, product["id"])

    card = _put(client, product["id"], cost_price=before["cost_price"] + 3)
    added = _row_on(card, TODAY)
    assert added is not None
    try:
        assert card["price"] == approx(before["price"])
        assert added["price"] == approx(before["price"])
    finally:
        _drop(client, product["id"], added["id"])


def test_record_on_a_taken_date_is_rewritten_not_doubled(client):
    """На одну дату у товара одна цена: вторая запись — исправление первой."""
    product = _products(client)[1]
    before = _card(client, product["id"])

    first = _put(client, product["id"], cost_price=before["cost_price"] + 1, note="ошибся")
    second = _put(client, product["id"], cost_price=before["cost_price"] + 2, note="вот так")
    added = _row_on(second, TODAY)
    assert added is not None
    try:
        assert len(second["history"]) == len(first["history"])
        assert added["note"] == "вот так"
        assert second["cost_price"] == approx(before["cost_price"] + 2)
    finally:
        _drop(client, product["id"], added["id"])


def test_backdated_record_leaves_the_card_and_the_calculation_alone(client):
    """Запись задним числом — про историю, а не про сегодняшнюю цену.

    Расчёт её не видит, поэтому и сбрасывать кэш из-за неё нельзя: иначе
    приведение истории в порядок пересчитывало бы всю выборку.
    """
    product = _with_history(client)
    before = _card(client, product["id"])
    oldest = date.fromisoformat(before["history"][-1]["valid_from"])
    epoch_before = _epoch()

    card = _put(
        client,
        product["id"],
        valid_from=(oldest - timedelta(days=30)).isoformat(),
        cost_price=1.5,
    )
    added = _row_on(card, oldest - timedelta(days=30))
    assert added is not None
    try:
        assert card["cost_price"] == approx(before["cost_price"]), "действует по-прежнему новая цена"
        assert card["valid_from"] == before["valid_from"]
        assert len(card["history"]) == len(before["history"]) + 1
        assert card["history"][-1]["cost_price"] == approx(1.5)
        assert _epoch() == epoch_before, "расчёт от записи задним числом не меняется"
    finally:
        _drop(client, product["id"], added["id"])


def test_current_price_change_sends_the_calculation_back_to_work(client):
    """А вот новая действующая цена кэш обесценить обязана."""
    product = _products(client)[2]
    epoch_before = _epoch()

    card = _put(client, product["id"], cost_price=_card(client, product["id"])["cost_price"] + 4)
    added = _row_on(card, TODAY)
    assert added is not None
    try:
        assert _epoch() > epoch_before
    finally:
        _drop(client, product["id"], added["id"])


def test_date_in_the_future_is_refused(client):
    product = _products(client)[0]

    detail = _refuse(
        client,
        product["id"],
        valid_from=(TODAY + timedelta(days=1)).isoformat(),
        cost_price=999,
    )

    assert "дата будущего" in detail
    assert _row_on(_card(client, product["id"]), TODAY + timedelta(days=1)) is None


def test_price_that_does_not_change_anything_is_refused(client):
    """Запись «та же цена» только засоряет историю."""
    product = _products(client)[0]
    card = _card(client, product["id"])

    detail = _refuse(client, product["id"], cost_price=card["cost_price"], price=card["price"])

    assert "записывать нечего" in detail
    assert _card(client, product["id"])["history"] == card["history"]


def test_empty_and_negative_values_are_refused(client):
    product = _products(client)[0]

    assert "не передана ни себестоимость, ни цена" in _refuse(client, product["id"])
    bad = client.post(
        f"{CATALOG}/{product['id']}/prices",
        json={"valid_from": TODAY.isoformat(), "cost_price": -1},
    )
    assert bad.status_code == 422


def test_order_line_shows_that_the_price_has_changed_since(client):
    """Себестоимость в заказе — снимок, и расхождение с нынешней ценой видно.

    Переписывать строку нельзя: сумма документа, уже отправленного поставщику,
    не должна меняться задним числом. Но собранный месяц назад черновик
    посчитан по старой цене, и человек должен это заметить.
    """
    rows = client.get("/api/v1/purchase?only_needed=true").json()["rows"]
    row = next(r for r in rows if r["qty"] > 0 and r["arrival_week"])
    product = next(p for p in _products(client) if p["vendor_code"] == row["vendor_code"])
    created = client.post(
        "/api/v1/orders",
        json={
            "lines": [
                {"pair_id": row["pair_id"], "qty": row["qty"], "arrival_week": row["arrival_week"]}
            ]
        },
    )
    assert created.status_code == 201, created.text
    order = created.json()
    was = product["cost_price"]

    card = _put(client, product["id"], cost_price=was + 50)
    added = _row_on(card, TODAY)
    assert added is not None
    try:
        line = client.get(f"/api/v1/orders/{order['id']}").json()["lines"][0]

        assert line["cost_price"] == approx(was), "снимок в документе не переписан"
        assert line["cost_price_now"] == approx(was + 50)
        assert line["cost"] == approx(line["qty"] * was)
    finally:
        _drop(client, product["id"], added["id"])
        client.post(f"/api/v1/orders/{order['id']}/status", json={"status": "cancelled"})


def test_unknown_product_and_record_answer_404(client):
    assert client.get(f"{CATALOG}/999999/prices").status_code == 404
    assert (
        client.post(
            f"{CATALOG}/999999/prices",
            json={"valid_from": TODAY.isoformat(), "cost_price": 10},
        ).status_code
        == 404
    )

    product, other = _products(client)[0], _products(client)[1]
    assert product["id"] != other["id"]
    assert client.delete(f"{CATALOG}/{product['id']}/prices/999999").status_code == 404

    # Запись чужого товара не считается найденной: иначе удалить можно было бы
    # что угодно, подставив в адрес любой товар.
    foreign = _card(client, other["id"])["history"][0]
    assert (
        client.delete(f"{CATALOG}/{product['id']}/prices/{foreign['id']}").status_code == 404
    )
    assert _card(client, other["id"])["history"][0] == foreign, "чужая запись на месте"


def test_viewer_can_read_but_not_write(client, viewer_client):
    product = _products(client)[0]

    assert viewer_client.get(f"{CATALOG}/{product['id']}/prices").status_code == 200
    assert (
        viewer_client.post(
            f"{CATALOG}/{product['id']}/prices",
            json={"valid_from": TODAY.isoformat(), "cost_price": 1},
        ).status_code
        == 403
    )
    row = _card(client, product["id"])["history"][0]["id"]
    assert viewer_client.delete(f"{CATALOG}/{product['id']}/prices/{row}").status_code == 403
