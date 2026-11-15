"""Ведение справочников: магазины, поставщики, категории, товары, пары (задача B-14).

Главная проверка — та, ради которой задача появилась: каталог можно завести
с нуля, не трогая демонстрационный сид, и заведённая позиция сразу считается.
До этого развёртывание на живых данных упиралось в то, что магазин и товар
создать нечем.

Вторая: справочник не умеет стирать историю. Удалить то, на что ссылаются
факты, поставки или заказы, нельзя — такую запись отключают.
"""

from __future__ import annotations

import pytest
from pytest import approx

CATALOG = "/api/v1/catalog"
MATRIX = "/api/v1/matrix?level=sku&limit=1"
PURCHASE = "/api/v1/purchase?only_needed=false"


def _sku_count(client) -> int:
    response = client.get(MATRIX)
    assert response.status_code == 200, response.text
    return response.json()["context"]["sku_count"]


def _epoch() -> int:
    from app.db import SessionLocal
    from app.models import AppSettings

    with SessionLocal() as session:
        return session.get(AppSettings, 1).calc_epoch


def _post(client, path: str, payload: dict, expect: int = 201):
    response = client.post(f"{CATALOG}{path}", json=payload)
    assert response.status_code == expect, response.text
    return response.json()


def _put(client, path: str, payload: dict, expect: int = 200):
    response = client.put(f"{CATALOG}{path}", json=payload)
    assert response.status_code == expect, response.text
    return response.json()


def _drop(client, path: str, expect: int = 204) -> None:
    response = client.delete(f"{CATALOG}{path}")
    assert response.status_code == expect, response.text


@pytest.fixture
def blank(client):
    """Пустой набор справочников под тест, убираемый за собой.

    База тестов общая, поэтому всё созданное удаляется в обратном порядке:
    пара, товар, категория, поставщик, кабинет.
    """
    made: dict[str, list[int]] = {
        "pairs": [],
        "products": [],
        "categories": [],
        "suppliers": [],
        "stores": [],
    }
    yield made
    for pair_id in made["pairs"]:
        client.delete(f"{CATALOG}/pairs/{pair_id}")
    for product_id in made["products"]:
        client.delete(f"{CATALOG}/products/{product_id}")
    for category_id in made["categories"]:
        client.delete(f"{CATALOG}/categories/{category_id}")
    for supplier_id in made["suppliers"]:
        client.delete(f"{CATALOG}/suppliers/{supplier_id}")
    for store_id in made["stores"]:
        client.delete(f"{CATALOG}/stores/{store_id}")


def test_catalogue_can_be_entered_from_nothing_and_is_calculated(client, blank):
    """Кабинет, поставщик, категория, товар, пара — и позиция уже в расчёте."""
    was = _sku_count(client)

    store = _post(client, "/stores", {"code": "b14-shop", "name": "Проверочный кабинет"})
    blank["stores"].append(store["id"])
    supplier = _post(client, "/suppliers", {"name": "Проверочный поставщик", "lead_time_days": 21})
    blank["suppliers"].append(supplier["id"])
    category = _post(client, "/categories", {"name": "Проверочная категория", "niche": "Проверка"})
    blank["categories"].append(category["id"])

    product = _post(
        client,
        "/products",
        {
            "vendor_code": "B14-001",
            "title": "Товар из справочника",
            "category_id": category["id"],
            "supplier_id": supplier["id"],
            "cost_price": 300,
            "price": 900,
            "volume_m3": 0.01,
        },
    )
    blank["products"].append(product["id"])
    assert product["store_count"] == 0, "товар без кабинета нигде не считается"
    assert _sku_count(client) == was, "и в матрице его ещё нет"

    pairs = _post(
        client,
        f"/products/{product['id']}/stores",
        {"store_id": store["id"], "nm_id": "900001", "base_potential": 40},
    )
    blank["pairs"].append(pairs[0]["id"])

    # Начальная цена легла в историю, а не осталась числом без прошлого (B-9).
    card = client.get(f"{CATALOG}/products/{product['id']}/prices").json()
    assert card["cost_price"] == approx(300)
    assert len(card["history"]) == 1
    assert card["history"][0]["note"] == "начальная загрузка"
    assert card["history"][0]["author"], "автор из сессии"

    # Позиция в расчёте: строка в матрице и строка в закупке с той самой ценой.
    assert _sku_count(client) == was + 1
    row = next(r for r in client.get(PURCHASE).json()["rows"] if r["vendor_code"] == "B14-001")
    assert row["params"]["cost_price"] == approx(300)
    assert row["params"]["lead_time_days"] == 21, "срок взят у поставщика"


def test_product_removed_from_sale_leaves_the_calculation(client):
    """«Снять с продажи» должно что-то значить: позиция уходит из расчёта."""
    products = client.get(f"{CATALOG}/products").json()
    product = next(p for p in products if p["store_count"] > 0)
    was = _sku_count(client)

    _put(client, f"/products/{product['id']}", {"is_active": False})
    try:
        assert _sku_count(client) == was - product["store_count"]
        assert all(
            r["vendor_code"] != product["vendor_code"] for r in client.get(PURCHASE).json()["rows"]
        ), "и закупку он больше не просит"
    finally:
        _put(client, f"/products/{product['id']}", {"is_active": True})

    assert _sku_count(client) == was


def test_disabled_store_takes_its_positions_out(client):
    stores = client.get(f"{CATALOG}/stores").json()
    store = stores[0]
    was = _sku_count(client)
    pairs_here = sum(
        1
        for product in client.get(f"{CATALOG}/products").json()
        for pair in client.get(f"{CATALOG}/products/{product['id']}/stores").json()
        if pair["store_id"] == store["id"] and pair["is_active"]
    )
    assert pairs_here > 0

    _put(client, f"/stores/{store['id']}", {"is_active": False})
    try:
        assert _sku_count(client) == was - pairs_here
    finally:
        _put(client, f"/stores/{store['id']}", {"is_active": True})

    assert _sku_count(client) == was


def test_editing_a_product_sends_the_calculation_back_to_work(client):
    """Товар общий для всех пар, поэтому его правка обесценивает кэш целиком."""
    product = client.get(f"{CATALOG}/products").json()[0]
    before = _epoch()

    _put(client, f"/products/{product['id']}", {"title": product["title"] + " "})
    try:
        assert _epoch() > before
    finally:
        _put(client, f"/products/{product['id']}", {"title": product["title"]})


def test_same_nm_id_in_one_store_is_refused(client, blank):
    """Загрузка WB ищет пару по номеру номенклатуры: двух одинаковых быть не может."""
    store = client.get(f"{CATALOG}/stores").json()[0]
    taken = next(
        pair
        for product in client.get(f"{CATALOG}/products").json()
        for pair in client.get(f"{CATALOG}/products/{product['id']}/stores").json()
        if pair["store_id"] == store["id"] and pair["nm_id"]
    )

    product = _post(client, "/products", {"vendor_code": "B14-002", "title": "Двойник"})
    blank["products"].append(product["id"])

    response = client.post(
        f"{CATALOG}/products/{product['id']}/stores",
        json={"store_id": store["id"], "nm_id": taken["nm_id"]},
    )

    assert response.status_code == 409, response.text
    assert "загрузка WB" in response.json()["detail"]


def test_duplicates_are_refused(client, blank):
    store = _post(client, "/stores", {"code": "b14-dup", "name": "Кабинет"})
    blank["stores"].append(store["id"])

    # Код кабинета сравнивается без учёта регистра: STORE01 и store01 — один кабинет.
    assert (
        client.post(f"{CATALOG}/stores", json={"code": "B14-DUP", "name": "Он же"}).status_code
        == 409
    )

    product = _post(client, "/products", {"vendor_code": "B14-003", "title": "Товар"})
    blank["products"].append(product["id"])
    assert (
        client.post(
            f"{CATALOG}/products", json={"vendor_code": "b14-003", "title": "Он же"}
        ).status_code
        == 409
    )

    pairs = _post(client, f"/products/{product['id']}/stores", {"store_id": store["id"]})
    blank["pairs"].append(pairs[0]["id"])
    assert (
        client.post(
            f"{CATALOG}/products/{product['id']}/stores", json={"store_id": store["id"]}
        ).status_code
        == 409
    )


def test_history_is_not_deletable_through_the_catalogue(client):
    """Удаление отказывает и называет числом, что именно мешает."""
    store = client.get(f"{CATALOG}/stores").json()[0]
    refusal = client.delete(f"{CATALOG}/stores/{store['id']}")
    assert refusal.status_code == 422
    assert "товаров в кабинете" in refusal.json()["detail"]
    assert "Отключите" in refusal.json()["detail"]

    product = next(p for p in client.get(f"{CATALOG}/products").json() if p["store_count"] > 0)
    assert client.delete(f"{CATALOG}/products/{product['id']}").status_code == 422

    pair = client.get(f"{CATALOG}/products/{product['id']}/stores").json()[0]
    assert pair["fact_count"] > 0
    pair_refusal = client.delete(f"{CATALOG}/pairs/{pair['id']}")
    assert pair_refusal.status_code == 422
    assert "недельных фактов" in pair_refusal.json()["detail"]

    category_id = product["category_id"]
    assert client.delete(f"{CATALOG}/categories/{category_id}").status_code == 422
    assert client.delete(f"{CATALOG}/suppliers/{product['supplier_id']}").status_code == 422

    # Ничего из этого не исчезло.
    assert client.get(f"{CATALOG}/stores").json()
    assert any(p["id"] == product["id"] for p in client.get(f"{CATALOG}/products").json())


def test_price_cannot_be_set_through_the_product_card(client):
    """Писать в действующую цену умеет только сервис цен (задача B-9)."""
    product = client.get(f"{CATALOG}/products").json()[0]

    response = client.put(
        f"{CATALOG}/products/{product['id']}", json={"cost_price": 1, "title": product["title"]}
    )

    assert response.status_code == 422, "молча пропустить цену нельзя"
    assert client.get(f"{CATALOG}/products").json()[0]["cost_price"] == approx(
        product["cost_price"]
    )


def test_purchase_limits_can_be_cleared_back_to_defaults(client, blank):
    """Пустое ограничение — это «брать у поставщика или из настроек», а не ноль."""
    product = _post(
        client,
        "/products",
        {"vendor_code": "B14-004", "title": "С ограничениями", "moq": 500, "pack_size": 10},
    )
    blank["products"].append(product["id"])
    assert product["moq"] == 500

    cleared = _put(client, f"/products/{product['id']}", {"clear": ["moq", "pack_size"]})

    assert cleared["moq"] is None and cleared["pack_size"] is None
    assert (
        client.put(
            f"{CATALOG}/products/{product['id']}", json={"clear": ["cost_price"]}
        ).status_code
        == 422
    ), "снять можно только ограничение закупки"


def test_unknown_ids_answer_404(client):
    assert client.put(f"{CATALOG}/stores/999999", json={"name": "нет"}).status_code == 404
    assert client.put(f"{CATALOG}/suppliers/999999", json={"name": "нет"}).status_code == 404
    assert client.put(f"{CATALOG}/categories/999999", json={"name": "нет"}).status_code == 404
    assert client.put(f"{CATALOG}/products/999999", json={"title": "нет"}).status_code == 404
    assert client.put(f"{CATALOG}/pairs/999999", json={"nm_id": "1"}).status_code == 404
    assert client.delete(f"{CATALOG}/pairs/999999").status_code == 404
    assert client.get(f"{CATALOG}/products/999999/stores").status_code == 404
    # Ссылка на несуществующую категорию — не «не найдено», а негодное значение.
    bad = client.post(
        f"{CATALOG}/products", json={"vendor_code": "B14-005", "title": "х", "category_id": 999999}
    )
    assert bad.status_code == 422 and "категория" in bad.json()["detail"]


def test_viewer_reads_but_does_not_write(client, viewer_client):
    product = client.get(f"{CATALOG}/products").json()[0]
    store = client.get(f"{CATALOG}/stores").json()[0]

    assert viewer_client.get(f"{CATALOG}/suppliers").status_code == 200
    assert viewer_client.get(f"{CATALOG}/products/{product['id']}/stores").status_code == 200
    assert (
        viewer_client.post(f"{CATALOG}/stores", json={"code": "b14-no", "name": "нет"}).status_code
        == 403
    )
    assert (
        viewer_client.put(f"{CATALOG}/products/{product['id']}", json={"title": "нет"}).status_code
        == 403
    )
    assert viewer_client.delete(f"{CATALOG}/stores/{store['id']}").status_code == 403
    assert (
        viewer_client.post(
            f"{CATALOG}/products/{product['id']}/stores", json={"store_id": store["id"]}
        ).status_code
        == 403
    )
