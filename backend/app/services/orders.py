"""Заказы поставщикам.

Закупочная потребность пересчитывается при каждом запросе, поэтому сама по себе
она не помнит решений человека. Заказ — документ, который их фиксирует: что именно
решили купить, у кого, когда разместили и чем кончилось.

Расчёт видит заказ не напрямую, а через поставки (:class:`Supply`): размещение
создаёт их со статусом «в пути», и потребность уменьшается сама, без отдельной
ветки в формулах. Отмена удаляет поставки, и потребность возвращается.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

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


@dataclass(frozen=True)
class OrderLineInput:
    pair_id: int
    qty: int
    arrival_week: str


def create_order(
    session: Session,
    lines: list[OrderLineInput],
    author: User | None = None,
    note: str | None = None,
) -> PurchaseOrder:
    """Создаёт черновик заказа. Поставщик выводится из строк, а не задаётся извне."""
    if not lines:
        raise OrderError("В заказе нет ни одной строки")
    if any(line.qty <= 0 for line in lines):
        raise OrderError("Количество в строке заказа должно быть положительным")

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
        raise OrderError(
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
