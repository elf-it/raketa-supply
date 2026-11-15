"""Ведение справочников: магазины, поставщики, категории, товары, пары (задача B-14).

До этой задачи каталог умел появляться единственным способом — демонстрационным
сидом, то есть вместе с выдуманными продажами. На чистой базе развёртывание
упиралось в то, что завести магазин и товар нечем, а загрузка WB приносит факты
только по известным товарам: незнакомый ``nmId`` она складывает в
``unknown_nm_ids`` и проходит мимо.

Решения и почему так:

* **Ничего не удаляется, пока на это что-то ссылается.** Удалить магазин,
  по которому есть недельные факты, значит выбросить историю продаж; удалить
  поставщика — оборвать уже отправленные заказы. Поэтому удаление разрешено
  только тогда, когда ссылаться нечему (опечатались при заведении), а обычный
  путь — отключить: отключённая пара и отключённый магазин выпадают из расчёта,
  но их прошлое остаётся на месте.
* **Цены здесь не правятся.** В карточке товара лежат действующие значения,
  и писать в них умеет только ``services/prices.py`` — иначе карточка
  и история цен разойдутся (задача B-9). Начальную себестоимость при заведении
  товара этот модуль проводит через тот же сервис, то есть она сразу становится
  первой записью истории, а не числом без прошлого.
* **``nm_id`` уникален внутри кабинета.** Загрузка WB строит отображение
  ``nm_id → пара`` словарём, и две пары с одним номером молча слились бы в одну:
  факты легли бы на ту, что оказалась в запросе последней. Это не та ошибка,
  которую замечают, — поэтому отказ на записи.
* **Код магазина и артикул товара сравниваются без учёта регистра.** В базе
  уникальность по значению, а ``STORE01`` и ``store01`` — один и тот же кабинет
  для человека; упустить это значит однажды грузить один кабинет дважды.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    Category,
    ForecastAdjustment,
    Marketplace,
    Product,
    PurchaseOrder,
    PurchaseOrderLine,
    Seasonality,
    Store,
    StoreProduct,
    Supplier,
    Supply,
    WeekFact,
    WeekPlan,
)
from . import prices as price_service


class CatalogError(ValueError):
    """Отказ по смыслу: запись осталась бы противоречивой."""


class CatalogConflict(ValueError):
    """Занятое значение: такой код, артикул или пара уже есть."""


# ------------------------------------------------------------------- вспомогательное


def _text(value: str | None, field: str, limit: int) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise CatalogError(f"{field}: пустое значение")
    if len(cleaned) > limit:
        raise CatalogError(f"{field}: длиннее {limit} символов")
    return cleaned


def _count(session: Session, model, *where) -> int:
    return session.scalar(select(func.count()).select_from(model).where(*where)) or 0


def _refuse_if_used(what: str, blockers: dict[str, int], advice: str) -> None:
    """Отказ на удаление с перечислением того, что мешает.

    Сообщение называет причину числами: «на складе три пары и 120 недельных
    фактов» — иначе человек видит запрет и не понимает, что именно он потеряет.
    """
    used = {name: count for name, count in blockers.items() if count}
    if not used:
        return
    listed = ", ".join(f"{name}: {count}" for name, count in used.items())
    raise CatalogError(f"{what} уже используется ({listed}). {advice}")


def _taken(session: Session, model, column, value: str, exclude_id: int | None = None) -> bool:
    stmt = select(func.count()).select_from(model).where(func.lower(column) == value.lower())
    if exclude_id is not None:
        stmt = stmt.where(model.id != exclude_id)
    return bool(session.scalar(stmt))


# ------------------------------------------------------------------------- магазины


def create_store(
    session: Session,
    *,
    code: str,
    name: str,
    marketplace: Marketplace = Marketplace.WB,
    cabinet_ref: str | None = None,
) -> Store:
    """Кабинет маркетплейса. Ключ API здесь не задаётся — он живёт в окружении."""
    code = _text(code, "код кабинета", 32)
    name = _text(name, "название", 128)
    if _taken(session, Store, Store.code, code):
        raise CatalogConflict(f"кабинет с кодом {code} уже есть")

    store = Store(
        code=code,
        name=name,
        marketplace=marketplace,
        cabinet_ref=(cabinet_ref or "").strip() or None,
    )
    session.add(store)
    session.commit()
    session.refresh(store)
    return store


def update_store(
    session: Session,
    store: Store,
    *,
    name: str | None = None,
    marketplace: Marketplace | None = None,
    cabinet_ref: str | None = None,
    is_active: bool | None = None,
) -> Store:
    """Код кабинета не меняется: по нему ищется ключ API в ``WB_TOKENS``.

    Переименование кабинета безобидно, а смена кода означала бы, что загрузка
    пойдёт в другой кабинет при том же наборе пар.
    """
    if name is not None:
        store.name = _text(name, "название", 128)
    if marketplace is not None:
        store.marketplace = marketplace
    if cabinet_ref is not None:
        store.cabinet_ref = cabinet_ref.strip() or None
    if is_active is not None:
        store.is_active = is_active
    session.commit()
    session.refresh(store)
    return store


def delete_store(session: Session, store: Store) -> None:
    _refuse_if_used(
        f"кабинет {store.code}",
        {"товаров в кабинете": _count(session, StoreProduct, StoreProduct.store_id == store.id)},
        "Отключите его: расчёт перестанет его видеть, а продажи останутся.",
    )
    session.delete(store)
    session.commit()


# ------------------------------------------------------------------------ поставщики


def create_supplier(
    session: Session, *, name: str, lead_time_days: int = 14, moq: int = 0
) -> Supplier:
    name = _text(name, "название", 128)
    if _taken(session, Supplier, Supplier.name, name):
        raise CatalogConflict(f"поставщик «{name}» уже есть")
    supplier = Supplier(name=name, lead_time_days=lead_time_days, moq=moq)
    session.add(supplier)
    session.commit()
    session.refresh(supplier)
    return supplier


def update_supplier(
    session: Session,
    supplier: Supplier,
    *,
    name: str | None = None,
    lead_time_days: int | None = None,
    moq: int | None = None,
) -> Supplier:
    """Срок поставки и MOQ поставщика — то, что берётся, когда у товара пусто.

    Правка сразу меняет закупочную потребность по всем его товарам: дата заказа
    считается от срока поставки.
    """
    if name is not None:
        name = _text(name, "название", 128)
        if _taken(session, Supplier, Supplier.name, name, exclude_id=supplier.id):
            raise CatalogConflict(f"поставщик «{name}» уже есть")
        supplier.name = name
    if lead_time_days is not None:
        supplier.lead_time_days = lead_time_days
    if moq is not None:
        supplier.moq = moq
    session.commit()
    session.refresh(supplier)
    return supplier


def delete_supplier(session: Session, supplier: Supplier) -> None:
    _refuse_if_used(
        f"поставщик «{supplier.name}»",
        {
            "товаров": _count(session, Product, Product.supplier_id == supplier.id),
            "заказов": _count(session, PurchaseOrder, PurchaseOrder.supplier_id == supplier.id),
        },
        "Перенесите товары на другого поставщика или оставьте запись как есть.",
    )
    session.delete(supplier)
    session.commit()


# ------------------------------------------------------------------------- категории


def create_category(
    session: Session, *, name: str, niche: str | None = None, abc_class: str | None = None
) -> Category:
    name = _text(name, "название", 128)
    if _taken(session, Category, Category.name, name):
        raise CatalogConflict(f"категория «{name}» уже есть")
    category = Category(
        name=name,
        niche=(niche or "").strip() or None,
        abc_class=(abc_class or "").strip() or None,
    )
    session.add(category)
    session.commit()
    session.refresh(category)
    return category


def update_category(
    session: Session,
    category: Category,
    *,
    name: str | None = None,
    niche: str | None = None,
    abc_class: str | None = None,
) -> Category:
    if name is not None:
        name = _text(name, "название", 128)
        if _taken(session, Category, Category.name, name, exclude_id=category.id):
            raise CatalogConflict(f"категория «{name}» уже есть")
        category.name = name
    if niche is not None:
        category.niche = niche.strip() or None
    if abc_class is not None:
        category.abc_class = abc_class.strip() or None
    session.commit()
    session.refresh(category)
    return category


def delete_category(session: Session, category: Category) -> None:
    _refuse_if_used(
        f"категория «{category.name}»",
        {
            "товаров": _count(session, Product, Product.category_id == category.id),
            "недель сезонности": _count(
                session, Seasonality, Seasonality.category_id == category.id
            ),
        },
        "Перенесите товары в другую категорию; коэффициенты сезонности уйдут вместе с ней.",
    )
    session.delete(category)
    session.commit()


# ---------------------------------------------------------------------------- товары


def _check_links(session: Session, category_id: int | None, supplier_id: int | None) -> None:
    if category_id is not None and session.get(Category, category_id) is None:
        raise CatalogError("категория не найдена")
    if supplier_id is not None and session.get(Supplier, supplier_id) is None:
        raise CatalogError("поставщик не найден")


def create_product(
    session: Session,
    *,
    vendor_code: str,
    title: str,
    category_id: int | None = None,
    supplier_id: int | None = None,
    volume_m3: float = 0.0,
    lead_time_days: int | None = None,
    moq: int | None = None,
    pack_size: int | None = None,
    coverage_weeks: int | None = None,
    safety_weeks: float | None = None,
    cost_price: float | None = None,
    price: float | None = None,
    author: str | None = None,
    today: date | None = None,
) -> Product:
    """Новая номенклатура. Цены проводятся через сервис цен, а не пишутся в карточку.

    Поэтому у товара, заведённого с себестоимостью, сразу есть первая запись
    истории — с датой, автором и пометкой «начальная загрузка». Товар без цены
    завести можно: цену допишут на экране цен, когда придёт счёт поставщика.
    """
    vendor_code = _text(vendor_code, "артикул", 64)
    title = _text(title, "название", 256)
    if _taken(session, Product, Product.vendor_code, vendor_code):
        raise CatalogConflict(f"товар с артикулом {vendor_code} уже есть")
    _check_links(session, category_id, supplier_id)

    product = Product(
        vendor_code=vendor_code,
        title=title,
        category_id=category_id,
        supplier_id=supplier_id,
        volume_m3=volume_m3,
        lead_time_days=lead_time_days,
        moq=moq,
        pack_size=pack_size,
        coverage_weeks=coverage_weeks,
        safety_weeks=safety_weeks,
    )
    session.add(product)
    session.commit()
    session.refresh(product)

    if (cost_price or 0) > 0 or (price or 0) > 0:
        price_service.set_price(
            session,
            product,
            valid_from=today or date.today(),
            cost_price=cost_price,
            price=price,
            author=author,
            note="начальная загрузка",
            today=today,
        )
        session.refresh(product)
    return product


def update_product(
    session: Session,
    product: Product,
    *,
    vendor_code: str | None = None,
    title: str | None = None,
    category_id: int | None = None,
    supplier_id: int | None = None,
    volume_m3: float | None = None,
    lead_time_days: int | None = None,
    moq: int | None = None,
    pack_size: int | None = None,
    coverage_weeks: int | None = None,
    safety_weeks: float | None = None,
    is_active: bool | None = None,
    clear: set[str] | None = None,
) -> Product:
    """Карточка товара без цен: себестоимость и цена правятся на экране цен.

    ``clear`` снимает ограничение закупки обратно к значению по умолчанию:
    пустое поле означает «брать из поставщика или настроек», и отличить
    «не передали» от «обнулить» иначе нельзя.
    """
    if vendor_code is not None:
        vendor_code = _text(vendor_code, "артикул", 64)
        if _taken(session, Product, Product.vendor_code, vendor_code, exclude_id=product.id):
            raise CatalogConflict(f"товар с артикулом {vendor_code} уже есть")
        product.vendor_code = vendor_code
    if title is not None:
        product.title = _text(title, "название", 256)
    if category_id is not None or supplier_id is not None:
        _check_links(session, category_id, supplier_id)
    if category_id is not None:
        product.category_id = category_id
    if supplier_id is not None:
        product.supplier_id = supplier_id
    if volume_m3 is not None:
        product.volume_m3 = volume_m3
    if lead_time_days is not None:
        product.lead_time_days = lead_time_days
    if moq is not None:
        product.moq = moq
    if pack_size is not None:
        product.pack_size = pack_size
    if coverage_weeks is not None:
        product.coverage_weeks = coverage_weeks
    if safety_weeks is not None:
        product.safety_weeks = safety_weeks
    if is_active is not None:
        product.is_active = is_active
    for field_name in clear or set():
        if field_name not in {
            "category_id",
            "supplier_id",
            "lead_time_days",
            "moq",
            "pack_size",
            "coverage_weeks",
            "safety_weeks",
        }:
            raise CatalogError(f"нечего снимать в поле {field_name}")
        setattr(product, field_name, None)
    session.commit()
    session.refresh(product)
    return product


def delete_product(session: Session, product: Product) -> None:
    """Удаление вместе с историей цен — она без товара бессмысленна.

    Пары не удаляются заодно: за ними стоят факты и заказы, поэтому товар
    с парами не удаляется вовсе.
    """
    _refuse_if_used(
        f"товар {product.vendor_code}",
        {
            "кабинетов с этим товаром": _count(
                session, StoreProduct, StoreProduct.product_id == product.id
            )
        },
        "Снимите его с продажи: расчёт перестанет его видеть, продажи останутся.",
    )
    session.delete(product)
    session.commit()


# ------------------------------------------------------------- пары (магазин, товар)


def pairs_of(session: Session, product_id: int) -> list[StoreProduct]:
    return list(
        session.scalars(
            select(StoreProduct)
            .where(StoreProduct.product_id == product_id)
            .order_by(StoreProduct.store_id)
        ).all()
    )


def _check_nm_id(
    session: Session, store_id: int, nm_id: str | None, exclude_id: int | None = None
) -> str | None:
    """Номер номенклатуры кабинета: один на кабинет, иначе факты лягут не туда."""
    cleaned = (nm_id or "").strip()
    if not cleaned:
        return None
    stmt = select(StoreProduct.id).where(
        StoreProduct.store_id == store_id, StoreProduct.nm_id == cleaned
    )
    if exclude_id is not None:
        stmt = stmt.where(StoreProduct.id != exclude_id)
    if session.scalar(stmt) is not None:
        raise CatalogConflict(
            f"номенклатура {cleaned} в этом кабинете уже привязана к другому товару: "
            "загрузка WB не смогла бы решить, на какую пару класть факты"
        )
    return cleaned


def add_pair(
    session: Session,
    *,
    product: Product,
    store_id: int,
    nm_id: str | None = None,
    barcode: str | None = None,
    base_potential: float = 0.0,
) -> StoreProduct:
    """Товар в кабинете — минимальная расчётная единица.

    Без пары товар не считается нигде: все недельные данные привязаны к ней.
    Базовый потенциал задаётся здесь же, иначе новая пара прогнозировала бы
    ноль до похода на экран потенциала.
    """
    store = session.get(Store, store_id)
    if store is None:
        raise CatalogError("кабинет не найден")
    if _count(
        session,
        StoreProduct,
        StoreProduct.store_id == store_id,
        StoreProduct.product_id == product.id,
    ):
        raise CatalogConflict(f"{product.vendor_code} уже заведён в кабинете {store.code}")

    pair = StoreProduct(
        store_id=store_id,
        product_id=product.id,
        nm_id=_check_nm_id(session, store_id, nm_id),
        barcode=(barcode or "").strip() or None,
        base_potential=base_potential,
    )
    session.add(pair)
    session.commit()
    session.refresh(pair)
    return pair


def update_pair(
    session: Session,
    pair: StoreProduct,
    *,
    nm_id: str | None = None,
    barcode: str | None = None,
    base_potential: float | None = None,
    is_active: bool | None = None,
) -> StoreProduct:
    if nm_id is not None:
        pair.nm_id = _check_nm_id(session, pair.store_id, nm_id, exclude_id=pair.id)
    if barcode is not None:
        pair.barcode = barcode.strip() or None
    if base_potential is not None:
        pair.base_potential = base_potential
    if is_active is not None:
        pair.is_active = is_active
    session.commit()
    session.refresh(pair)
    return pair


def delete_pair(session: Session, pair: StoreProduct) -> None:
    """Пару можно убрать, только пока за ней ничего не стоит.

    Факты, поставки, планы закрытых недель, ручные правки и строки заказов
    ссылаются на пару; удалить её вместе с ними значит стереть историю продаж
    позиции в кабинете. Обычный путь — отключить.
    """
    _refuse_if_used(
        "пара магазин/товар",
        {
            "недельных фактов": _count(session, WeekFact, WeekFact.store_product_id == pair.id),
            "поставок": _count(session, Supply, Supply.store_product_id == pair.id),
            "планов закрытых недель": _count(
                session, WeekPlan, WeekPlan.store_product_id == pair.id
            ),
            "ручных правок": _count(
                session, ForecastAdjustment, ForecastAdjustment.store_product_id == pair.id
            ),
            "строк заказов": _count(
                session, PurchaseOrderLine, PurchaseOrderLine.store_product_id == pair.id
            ),
        },
        "Отключите её: позиция выпадет из расчёта, а её прошлое останется.",
    )
    session.delete(pair)
    session.commit()
