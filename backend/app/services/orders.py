"""Заказы поставщикам.

Закупочная потребность пересчитывается при каждом запросе, поэтому сама по себе
она не помнит решений человека. Заказ — документ, который их фиксирует: что именно
решили купить, у кого, когда разместили и чем кончилось.

Расчёт видит заказ не напрямую, а через поставки (:class:`Supply`): размещение
создаёт их со статусом «в пути», и потребность уменьшается сама, без отдельной
ветки в формулах. Отмена удаляет поставки, и потребность возвращается.

Правится только черновик. Размещённый заказ уже ушёл поставщику и живёт
в расчёте как товар в пути: правка задним числом означала бы, что документ
у нас и документ у поставщика разошлись, а потребность пересчиталась под нашу
версию. Такой заказ отменяют и собирают заново — **[ДОПУЩЕНИЕ, Q-11]**,
пока неизвестно, ведётся ли заказ здесь или в учётной системе.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..calc import week_start
from ..models import (
    OrderStatus,
    Product,
    PurchaseOrder,
    PurchaseOrderLine,
    StoreProduct,
    Supply,
    SupplyStatus,
    User,
)
from .matrix import load_app_settings, resolve_last_closed_week

#: Разрешённые переходы. Из конечных статусов выхода нет: заказ не воскрешают,
#: вместо этого создают новый. Порядок внутри списка — порядок кнопок на экране:
#: сначала движение вперёд, отмена последней.
TRANSITIONS: dict[OrderStatus, list[OrderStatus]] = {
    OrderStatus.DRAFT: [OrderStatus.PLACED, OrderStatus.CANCELLED],
    OrderStatus.PLACED: [OrderStatus.RECEIVED, OrderStatus.CANCELLED],
    OrderStatus.RECEIVED: [],
    OrderStatus.CANCELLED: [],
}

STATUS_LABELS: dict[OrderStatus, str] = {
    OrderStatus.DRAFT: "Черновик",
    OrderStatus.PLACED: "Размещён",
    OrderStatus.RECEIVED: "Принят",
    OrderStatus.CANCELLED: "Отменён",
}


class OrderError(ValueError):
    """Нарушение правил работы с заказом: состав строк или переход статуса."""


class OrderStateError(OrderError):
    """Действие не подходит к текущему статусу заказа."""


class OrderLineNotFound(OrderError):
    """В этом заказе нет такой строки."""


@dataclass(frozen=True)
class OrderLineInput:
    pair_id: int
    qty: int
    arrival_week: str


def check_arrival_week(session: Session, week: str, today: date | None = None) -> None:
    """Проверяет неделю прихода: формат ``2026-W48`` и что неделя ещё не закрыта.

    Приход в закрытую неделю расчёт не учтёт: остаток закрытой недели берётся
    из факта, а закупочная потребность смотрит только на открытые недели. Такая
    строка молча не уменьшила бы потребность — заказ есть, а система просит
    купить то же самое ещё раз.
    """
    try:
        week_start(week)
    except ValueError as error:
        raise OrderError(
            f"Неделя прихода {week!r} не похожа на ISO-неделю вида 2026-W48"
        ) from error

    last_closed = resolve_last_closed_week(load_app_settings(session), today)
    if week <= last_closed:
        raise OrderError(
            f"Неделя прихода {week} уже закрыта (последняя закрытая — {last_closed}): "
            "приход в прошлое расчёт не учтёт"
        )


def create_order(
    session: Session,
    lines: list[OrderLineInput],
    author: User | None = None,
    note: str | None = None,
    today: date | None = None,
) -> PurchaseOrder:
    """Создаёт черновик заказа. Поставщик выводится из строк, а не задаётся извне."""
    if not lines:
        raise OrderError("В заказе нет ни одной строки")
    if any(line.qty <= 0 for line in lines):
        raise OrderError("Количество в строке заказа должно быть положительным")
    for line in lines:
        check_arrival_week(session, line.arrival_week, today)

    pair_ids = [line.pair_id for line in lines]
    if len(set(pair_ids)) != len(pair_ids):
        raise OrderError("Одна и та же позиция встречается в заказе дважды")

    pairs = {
        pair.id: pair
        for pair in session.scalars(select(StoreProduct).where(StoreProduct.id.in_(pair_ids))).all()
    }
    missing = [pid for pid in pair_ids if pid not in pairs]
    if missing:
        raise OrderError(f"Позиции не найдены: {missing}")

    products = {
        product.id: product
        for product in session.scalars(
            select(Product).where(Product.id.in_([p.product_id for p in pairs.values()]))
        ).all()
    }

    # Заказ уходит одному поставщику целиком: строки от разных поставщиков в одном
    # документе означали бы, что его нельзя ни отправить, ни принять как единое целое.
    suppliers = {products[pairs[pid].product_id].supplier_id for pid in pair_ids}
    if len(suppliers) > 1:
        raise OrderError("В одном заказе товары разных поставщиков")

    order = PurchaseOrder(
        supplier_id=next(iter(suppliers)),
        status=OrderStatus.DRAFT,
        note=note,
        created_by_id=author.id if author else None,
    )
    for line in lines:
        product = products[pairs[line.pair_id].product_id]
        order.lines.append(
            PurchaseOrderLine(
                store_product_id=line.pair_id,
                qty=line.qty,
                arrival_week=line.arrival_week,
                cost_price=product.cost_price,
                volume_m3=product.volume_m3,
            )
        )
    session.add(order)
    session.commit()
    session.refresh(order)
    return order


def set_status(session: Session, order: PurchaseOrder, status: OrderStatus) -> PurchaseOrder:
    """Переводит заказ в новый статус и приводит поставки в соответствие."""
    if status == order.status:
        return order
    if status not in TRANSITIONS[order.status]:
        raise OrderStateError(
            f"Нельзя перевести заказ из «{STATUS_LABELS[order.status]}» "
            f"в «{STATUS_LABELS[status]}»"
        )

    # Время берётся у базы, как и created_at: иначе отметки одного документа
    # оказались бы на разных часах — серверных и СУБД.
    now = func.now()
    if status is OrderStatus.PLACED:
        for line in order.lines:
            session.add(
                Supply(
                    store_product_id=line.store_product_id,
                    week=line.arrival_week,
                    qty=line.qty,
                    status=SupplyStatus.IN_TRANSIT,
                    note=f"заказ №{order.id}",
                    order_line_id=line.id,
                )
            )
        order.placed_at = now
    elif status is OrderStatus.RECEIVED:
        for supply in _supplies(session, order):
            supply.status = SupplyStatus.RECEIVED
        order.closed_at = now
    elif status is OrderStatus.CANCELLED:
        # Отменённый заказ не должен оставлять за собой ожидаемый приход.
        line_ids = [line.id for line in order.lines]
        if line_ids:
            session.execute(delete(Supply).where(Supply.order_line_id.in_(line_ids)))
        order.closed_at = now

    order.status = status
    session.commit()
    session.refresh(order)
    return order


def _require_draft(order: PurchaseOrder, action: str) -> None:
    if order.status is not OrderStatus.DRAFT:
        raise OrderStateError(
            f"{action} можно только в черновике, а заказ в статусе "
            f"«{STATUS_LABELS[order.status]}». Размещённый заказ отменяют "
            "и собирают заново"
        )


def _line(order: PurchaseOrder, line_id: int) -> PurchaseOrderLine:
    line = next((item for item in order.lines if item.id == line_id), None)
    if line is None:
        raise OrderLineNotFound(f"В заказе №{order.id} нет строки {line_id}")
    return line


def update_line(
    session: Session,
    order: PurchaseOrder,
    line_id: int,
    qty: int | None = None,
    arrival_week: str | None = None,
    today: date | None = None,
) -> PurchaseOrder:
    """Меняет количество и неделю прихода в строке черновика.

    Себестоимость и объём остаются теми, что записаны при создании заказа:
    они снимок карточки товара на тот момент, а не следствие количества.
    Сумма строки пересчитывается сама, потому что считается как количество
    на записанную цену.
    """
    _require_draft(order, "Править строки")
    line = _line(order, line_id)
    if qty is None and arrival_week is None:
        raise OrderError("Нечего менять: укажите количество или неделю прихода")
    if qty is not None:
        if qty <= 0:
            raise OrderError(
                "Количество в строке должно быть положительным. "
                "Ненужную позицию убирают из заказа, а не обнуляют"
            )
        line.qty = qty
    if arrival_week is not None:
        check_arrival_week(session, arrival_week, today)
        line.arrival_week = arrival_week

    session.commit()
    session.refresh(order)
    return order


def delete_line(session: Session, order: PurchaseOrder, line_id: int) -> PurchaseOrder:
    """Убирает строку из черновика. Последнюю строку убрать нельзя."""
    _require_draft(order, "Убирать строки")
    line = _line(order, line_id)
    if len(order.lines) == 1:
        # Заказ без строк нельзя ни отправить, ни принять, и поставщик у него
        # выведен из строк — пустой документ остался бы без поставщика.
        raise OrderError("Это последняя строка заказа. Заказ целиком отменяют кнопкой «Отменить»")

    session.delete(line)
    session.commit()
    session.refresh(order)
    return order


def update_note(session: Session, order: PurchaseOrder, note: str | None) -> PurchaseOrder:
    """Меняет примечание черновика — оно уходит поставщику в шапке файла."""
    _require_draft(order, "Править примечание")
    text = (note or "").strip()
    order.note = text or None
    session.commit()
    session.refresh(order)
    return order


def _supplies(session: Session, order: PurchaseOrder) -> list[Supply]:
    line_ids = [line.id for line in order.lines]
    if not line_ids:
        return []
    return list(session.scalars(select(Supply).where(Supply.order_line_id.in_(line_ids))).all())


def get_order(session: Session, order_id: int) -> PurchaseOrder | None:
    return session.get(PurchaseOrder, order_id)


def list_orders(
    session: Session,
    status: OrderStatus | None = None,
    supplier_id: int | None = None,
) -> list[PurchaseOrder]:
    query = select(PurchaseOrder).order_by(PurchaseOrder.created_at.desc(), PurchaseOrder.id.desc())
    if status is not None:
        query = query.where(PurchaseOrder.status == status)
    if supplier_id is not None:
        query = query.where(PurchaseOrder.supplier_id == supplier_id)
    return list(session.scalars(query).unique().all())
