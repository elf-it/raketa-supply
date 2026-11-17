"""Сопоставление SKU: несопоставленные номенклатуры, дубли, слияние (задача B-10).

Главные проверки — те, ради которых задача появилась. Первая: номер, которого
нет в каталоге, перестал исчезать в отчёте о прогоне и виден по имени — иначе
продажи позиции не считаются, и узнать об этом неоткуда. Вторая: слияние дублей
переносит прошлое, а не стирает его.
"""

from __future__ import annotations

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Product, Store, StoreProduct, UnmatchedNmId, WeekFact
from app.services import matching

CATALOG = "/api/v1/catalog"
SYNC = "/api/v1/sync/wb"
MATRIX = "/api/v1/matrix?level=sku&limit=1"


def _pair_with_nm_id() -> tuple[int, str, int]:
    """Пара из демо-данных: её номер и кабинет. Вернёт (pair_id, nm_id, store_id)."""
    with SessionLocal() as session:
        pair = session.scalars(
            select(StoreProduct).where(StoreProduct.nm_id.is_not(None)).order_by(StoreProduct.id)
        ).first()
        assert pair is not None
        return pair.id, pair.nm_id or "", pair.store_id


def _set_nm_id(pair_id: int, value: str | None) -> None:
    with SessionLocal() as session:
        pair = session.get(StoreProduct, pair_id)
        assert pair is not None
        pair.nm_id = value
        session.commit()


def _forget(nm_id: str) -> None:
    with SessionLocal() as session:
        for row in session.scalars(select(UnmatchedNmId).where(UnmatchedNmId.nm_id == nm_id)).all():
            session.delete(row)
        session.commit()


def test_sync_remembers_a_number_it_could_not_place(client):
    """Номер без пары больше не исчезает в отчёте: его видно по имени."""
    pair_id, nm_id, store_id = _pair_with_nm_id()
    _set_nm_id(pair_id, None)
    try:
        assert client.post(SYNC).status_code == 200

        rows = client.get(f"{CATALOG}/unmatched").json()
        mine = next(row for row in rows if row["nm_id"] == nm_id)
        assert mine["store_id"] == store_id
        assert mine["times_seen"] >= 1
        assert mine["store_name"], "кабинет называется, а не нумеруется"

        # Повторная загрузка не плодит строк, а считает встречи.
        assert client.post(SYNC).status_code == 200
        again = next(
            row for row in client.get(f"{CATALOG}/unmatched").json() if row["nm_id"] == nm_id
        )
        assert again["times_seen"] == mine["times_seen"] + 1

        # Номер вернули в каталог — загрузка убирает его из списка сама,
        # даже если привязку сделали мимо системы (например, файлом).
        _set_nm_id(pair_id, nm_id)
        assert client.post(SYNC).status_code == 200
        assert not any(row["nm_id"] == nm_id for row in client.get(f"{CATALOG}/unmatched").json())
    finally:
        _set_nm_id(pair_id, nm_id)
        _forget(nm_id)


def test_binding_puts_the_number_on_a_pair_and_clears_the_list(client):
    """Привязка — это пара с этим номером; следующая загрузка кладёт на неё факты."""
    pair_id, nm_id, store_id = _pair_with_nm_id()
    with SessionLocal() as session:
        product_id = session.get(StoreProduct, pair_id).product_id
    _set_nm_id(pair_id, None)
    try:
        client.post(SYNC)
        assert any(row["nm_id"] == nm_id for row in client.get(f"{CATALOG}/unmatched").json())

        answer = client.post(
            f"{CATALOG}/unmatched/bind",
            json={"store_id": store_id, "nm_id": nm_id, "product_id": product_id},
        )
        assert answer.status_code == 200, answer.text
        bound = next(pair for pair in answer.json() if pair["store_id"] == store_id)
        assert bound["nm_id"] == nm_id

        assert not any(
            row["nm_id"] == nm_id for row in client.get(f"{CATALOG}/unmatched").json()
        ), "сопоставленный номер в списке не нужен"

        # Факты приезжают следующей загрузкой.
        assert client.post(SYNC).status_code == 200
        with SessionLocal() as session:
            facts = session.scalars(
                select(WeekFact).where(WeekFact.store_product_id == pair_id)
            ).all()
        assert facts, "после привязки продажи снова считаются"
    finally:
        _set_nm_id(pair_id, nm_id)
        _forget(nm_id)


def test_binding_refuses_a_number_taken_in_the_same_cabinet(client):
    """Правило справочников действует и здесь: номер в кабинете один."""
    _, nm_id, store_id = _pair_with_nm_id()
    other = next(
        product
        for product in client.get(f"{CATALOG}/products").json()
        if not any(
            pair["store_id"] == store_id
            for pair in client.get(f"{CATALOG}/products/{product['id']}/stores").json()
        )
    )

    answer = client.post(
        f"{CATALOG}/unmatched/bind",
        json={"store_id": store_id, "nm_id": nm_id, "product_id": other["id"]},
    )

    assert answer.status_code == 409, answer.text
    assert "уже привязана" in answer.json()["detail"]


def test_duplicates_are_found_by_barcode_title_and_vendor_code(client):
    """Один товар, заведённый дважды, делит спрос и закупку пополам."""
    original = next(p for p in client.get(f"{CATALOG}/products").json() if p["store_count"] > 0)
    busy = {
        pair["store_id"]
        for pair in client.get(f"{CATALOG}/products/{original['id']}/stores").json()
    }
    free = next(s for s in client.get(f"{CATALOG}/stores").json() if s["id"] not in busy)
    twin = client.post(
        f"{CATALOG}/products",
        json={"vendor_code": f"{original['vendor_code']}-копия", "title": original["title"]},
    ).json()
    # Дубль без кабинетов сливать нечем, и подсказкой он не считается.
    pairs = client.post(
        f"{CATALOG}/products/{twin['id']}/stores",
        json={"store_id": free["id"], "nm_id": "980009"},
    ).json()
    try:
        hints = client.get(f"{CATALOG}/duplicates").json()
        mine = [
            hint
            for hint in hints
            if {hint["left_id"], hint["right_id"]} == {original["id"], twin["id"]}
        ]

        assert mine, "товар с тем же названием должен попасть в подсказки"
        assert mine[0]["reason"] in ("одинаковое название", "общий штрихкод")
        assert mine[0]["left_pairs"] > 0 and mine[0]["right_pairs"] > 0
    finally:
        client.delete(f"{CATALOG}/pairs/{pairs[0]['id']}")
        client.delete(f"{CATALOG}/products/{twin['id']}")


def test_merge_moves_cabinets_with_their_history(client):
    """Пары переезжают вместе с фактами, а исходный товар остаётся с историей."""
    store = client.get(f"{CATALOG}/stores").json()[-1]
    target = next(
        product
        for product in client.get(f"{CATALOG}/products").json()
        if not any(
            pair["store_id"] == store["id"]
            for pair in client.get(f"{CATALOG}/products/{product['id']}/stores").json()
        )
    )
    source = client.post(
        f"{CATALOG}/products",
        json={"vendor_code": "B10-двойник", "title": "Двойник для слияния", "cost_price": 90},
    ).json()
    pairs = client.post(
        f"{CATALOG}/products/{source['id']}/stores",
        json={"store_id": store["id"], "nm_id": "970001", "base_potential": 5},
    ).json()
    was = client.get(MATRIX).json()["context"]["sku_count"]
    try:
        answer = client.post(
            f"{CATALOG}/products/{source['id']}/merge", json={"into": target["id"]}
        )
        assert answer.status_code == 200, answer.text
        assert answer.json()["moved_pairs"] == 1

        moved = client.get(f"{CATALOG}/products/{target['id']}/stores").json()
        assert any(pair["id"] == pairs[0]["id"] for pair in moved), "пара переехала"
        assert client.get(f"{CATALOG}/products/{source['id']}/stores").json() == []

        source_after = next(
            p for p in client.get(f"{CATALOG}/products").json() if p["id"] == source["id"]
        )
        assert source_after["is_active"] is False, "товар без кабинетов снят с продажи"
        assert not [
            hint
            for hint in client.get(f"{CATALOG}/duplicates").json()
            if source["id"] in (hint["left_id"], hint["right_id"])
        ], "слитый товар не должен висеть в подсказках: сливать у него уже нечего"
        # Позиций в расчёте столько же: пара не исчезла, а сменила хозяина.
        assert client.get(MATRIX).json()["context"]["sku_count"] == was
    finally:
        client.delete(f"{CATALOG}/pairs/{pairs[0]['id']}")
        client.delete(f"{CATALOG}/products/{source['id']}")


def test_merge_refuses_when_both_live_in_one_cabinet(client):
    """Это не дубль, а две позиции: слияние потеряло бы продажи одной из них."""
    products = client.get(f"{CATALOG}/products").json()
    first = next(p for p in products if p["store_count"] > 0)
    first_stores = {
        pair["store_id"] for pair in client.get(f"{CATALOG}/products/{first['id']}/stores").json()
    }
    second = next(
        p
        for p in products
        if p["id"] != first["id"]
        and first_stores
        & {pair["store_id"] for pair in client.get(f"{CATALOG}/products/{p['id']}/stores").json()}
    )

    answer = client.post(f"{CATALOG}/products/{first['id']}/merge", json={"into": second["id"]})

    assert answer.status_code == 422, answer.text
    assert "в одном кабинете" in answer.json()["detail"]
    # Ничего не переехало.
    assert client.get(f"{CATALOG}/products/{first['id']}/stores").json()


def test_merge_into_itself_is_refused(client):
    product = client.get(f"{CATALOG}/products").json()[0]

    answer = client.post(f"{CATALOG}/products/{product['id']}/merge", json={"into": product["id"]})

    assert answer.status_code == 422
    assert "сам с собой" in answer.json()["detail"]


def test_viewer_may_look_but_not_match(client, viewer_client):
    product = client.get(f"{CATALOG}/products").json()[0]
    store = client.get(f"{CATALOG}/stores").json()[0]

    assert viewer_client.get(f"{CATALOG}/unmatched").status_code == 200
    assert viewer_client.get(f"{CATALOG}/duplicates").status_code == 200
    assert (
        viewer_client.post(
            f"{CATALOG}/unmatched/bind",
            json={"store_id": store["id"], "nm_id": "1", "product_id": product["id"]},
        ).status_code
        == 403
    )
    assert (
        viewer_client.post(
            f"{CATALOG}/products/{product['id']}/merge", json={"into": product["id"]}
        ).status_code
        == 403
    )


def test_normalised_key_ignores_separators_and_case():
    assert matching._key("SKU-001") == matching._key("sku 001")
    assert matching._key("Лампа H11") == matching._key("лампа  h-11")
    assert matching._key(None) == ""


def test_store_is_named_in_the_list(client):
    """Список читает человек: в нём должны быть названия, а не номера."""
    with SessionLocal() as session:
        store = session.scalars(select(Store).order_by(Store.code)).first()
        session.add(UnmatchedNmId(store_id=store.id, nm_id="999999"))
        session.commit()
    try:
        rows = matching.unmatched(SessionLocal())
        mine = next(row for row in rows if row.nm_id == "999999")
        assert mine.store_name == store.name
        assert mine.store_code == store.code
    finally:
        _forget("999999")


def test_product_without_cabinets_cannot_be_merged(client):
    """Переносить нечего — значит, это не слияние, а ошибка человека."""
    empty = client.post(
        f"{CATALOG}/products", json={"vendor_code": "B10-пустой", "title": "Без кабинетов"}
    ).json()
    target = client.get(f"{CATALOG}/products").json()[0]
    try:
        with SessionLocal() as session:
            source = session.get(Product, empty["id"])
            into = session.get(Product, target["id"])
            try:
                matching.merge(session, source=source, target=into)
                raise AssertionError("слияние пустого товара должно быть отказано")
            except matching.MatchError as error:
                assert "нет кабинетов" in str(error)
    finally:
        client.delete(f"{CATALOG}/products/{empty['id']}")
