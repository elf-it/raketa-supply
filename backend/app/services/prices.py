"""История цен и себестоимости товара (задача B-9).

Себестоимость участвует в расчёте закупки, а цена — в оценке товара, и обе
меняются: поставщик присылает новый прайс, курс уводит закупку, в карточке
исправляют опечатку. До этой задачи правка карточки стирала прежнее значение
без следа, и на вопрос «почему сентябрьский заказ был дешевле» ответить было
нечем: сумма в документе заказа уже не совпадала ни с чем в системе.

Разделение обязанностей такое: в карточке товара (:class:`Product`) лежат
значения, действующие сейчас — их читают расчёт, закупка и выгрузки, и ничего
про историю они не знают. Череда изменений живёт в :class:`ProductPrice`,
и карточка получает значения из неё.

Почему карточка не считается из истории на каждом чтении: её читает расчёт
закупки по всей выборке, и соединение с историей на каждую позицию стоило бы
дороже, чем вся экономия. Расхождение между карточкой и историей исключено тем,
что писать в карточку умеет только этот модуль.

Кэш расчёта обесценивать вручную не нужно: номер версии поднимает слушатель
``before_flush`` на изменение карточки товара (``app/revisions.py``). Поэтому
значения в карточку присваиваются только когда они действительно изменились —
иначе запись цены задним числом, которую расчёт даже не увидит, заставляла бы
пересчитывать всю выборку.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Product, ProductPrice


class PriceError(ValueError):
    """Отказ по содержанию: дата, значение или отсутствие перемены."""


def history(session: Session, product_id: int) -> list[ProductPrice]:
    """Записи о ценах товара от новой к старой."""
    return list(
        session.scalars(
            select(ProductPrice)
            .where(ProductPrice.product_id == product_id)
            .order_by(ProductPrice.valid_from.desc(), ProductPrice.id.desc())
        )
    )


def effective(rows: Sequence[ProductPrice], on: date) -> ProductPrice | None:
    """Запись, действующая на дату: последняя из начавшихся не позже неё.

    ``rows`` ожидаются в порядке от новой к старой — так их отдаёт :func:`history`.
    """
    for row in rows:
        if row.valid_from <= on:
            return row
    return None


def valid_from_by_product(
    session: Session, product_ids: Iterable[int] | None = None, today: date | None = None
) -> dict[int, date]:
    """Дата, с которой действует нынешняя цена, по товарам.

    Нужна списку товаров: без неё в строке стоит цена, про которую неизвестно,
    вчерашняя она или прошлогодняя. Одним запросом, а не по записи на товар.
    """
    today = today or date.today()
    stmt = (
        select(ProductPrice.product_id, func.max(ProductPrice.valid_from))
        .where(ProductPrice.valid_from <= today)
        .group_by(ProductPrice.product_id)
    )
    ids = None if product_ids is None else list(product_ids)
    if ids is not None:
        if not ids:
            return {}
        stmt = stmt.where(ProductPrice.product_id.in_(ids))
    result: dict[int, date] = {}
    for product_id, moment in session.execute(stmt).all():
        # SQLite отдаёт дату строкой, Postgres — объектом date.
        result[product_id] = moment if isinstance(moment, date) else date.fromisoformat(moment)
    return result


def find_row(session: Session, product_id: int, price_id: int) -> ProductPrice | None:
    """Запись истории, принадлежащая именно этому товару."""
    row = session.get(ProductPrice, price_id)
    return row if row is not None and row.product_id == product_id else None


def set_price(
    session: Session,
    product: Product,
    *,
    valid_from: date,
    cost_price: float | None = None,
    price: float | None = None,
    author: str | None = None,
    note: str | None = None,
    today: date | None = None,
) -> ProductPrice:
    """Записывает цену, действующую с даты, и обновляет карточку товара.

    Передать можно одно значение из двух: второе берётся из записи, которая
    действовала на эту дату, — иначе правка себестоимости обнуляла бы цену.

    Запись на занятую дату переписывается: на одну дату у товара одна цена,
    а вторая попытка — это исправление первой.
    """
    today = today or date.today()
    if valid_from > today:
        raise PriceError(
            f"дата будущего не принимается: {valid_from:%d.%m.%Y} позже сегодняшнего "
            f"{today:%d.%m.%Y}. Применить такую цену в срок системе нечем, "
            "а считать по ней заказы заранее — значит считать по цене, которой ещё нет"
        )
    if cost_price is None and price is None:
        raise PriceError("нечего записывать: не передана ни себестоимость, ни цена")
    for label, value in (("себестоимость", cost_price), ("цена", price)):
        if value is not None and value < 0:
            raise PriceError(f"{label} не может быть отрицательной: {value}")

    rows = history(session, product.id)
    existing = next((row for row in rows if row.valid_from == valid_from), None)
    # Основа для незаполненного значения и для проверки «а менять-то что?».
    # Пустая история — основа сама карточка: цена в ней есть, даты у неё нет.
    base = existing or effective(rows, valid_from)
    was_cost = base.cost_price if base else product.cost_price
    was_price = base.price if base else product.price
    new_cost = cost_price if cost_price is not None else was_cost
    new_price = price if price is not None else was_price

    if base is not None and _same(new_cost, base.cost_price) and _same(new_price, base.price):
        kept = "уже записаны на эту дату" if existing else f"действуют с {base.valid_from:%d.%m.%Y}"
        raise PriceError(f"эти значения {kept}: записывать нечего")

    if existing is not None:
        existing.cost_price = new_cost
        existing.price = new_price
        existing.author = author
        existing.note = note
        # Отметка «когда записали» относится к содержимому, а оно сменилось.
        existing.created_at = func.now()
        row = existing
    else:
        row = ProductPrice(
            product_id=product.id,
            valid_from=valid_from,
            cost_price=new_cost,
            price=new_price,
            author=author,
            note=note,
        )
        session.add(row)

    session.flush()
    _apply_current(session, product, today)
    session.commit()
    session.refresh(row)
    return row


def delete_price(
    session: Session, product: Product, row: ProductPrice, today: date | None = None
) -> None:
    """Убирает ошибочную запись и возвращает карточке то, что действует после этого.

    Если убрали последнюю запись, карточка остаётся с нынешними значениями:
    восстанавливать их неоткуда, и обнулять цену товара из-за правки истории
    было бы хуже, чем оставить историю пустой.
    """
    session.delete(row)
    session.flush()
    _apply_current(session, product, today or date.today())
    session.commit()


def _same(left: float, right: float) -> bool:
    """Равенство в копейках: цены приходят из формы как числа с плавающей точкой."""
    return round(left, 2) == round(right, 2)


def _apply_current(session: Session, product: Product, today: date) -> bool:
    """Переносит действующие значения в карточку товара. Возвращает, менялось ли.

    Присваивание только при настоящем расхождении: любое изменение карточки
    поднимает общий номер версии расчёта и обесценивает кэш всей выборки.
    """
    row = effective(history(session, product.id), today)
    if row is None:
        return False
    if _same(product.cost_price, row.cost_price) and _same(product.price, row.price):
        return False
    product.cost_price = row.cost_price
    product.price = row.price
    return True
