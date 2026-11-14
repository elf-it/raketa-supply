from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi import status as http_status
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import OrderStatus, PurchaseOrder, User
from ..schemas import OrderIn, OrderLineOut, OrderOut, OrderStatusIn
from ..services import orders as service
from ..services.order_export import build_order_workbook, order_filename
from .deps import require_editor

router = APIRouter(prefix="/orders", tags=["Заказы"])

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _to_out(order: PurchaseOrder, with_lines: bool = True) -> OrderOut:
    lines = [
        OrderLineOut(
            id=line.id,
            pair_id=line.store_product_id,
            store_name=line.pair.store.name,
            vendor_code=line.pair.product.vendor_code,
            title=line.pair.product.title,
            qty=line.qty,
            arrival_week=line.arrival_week,
            cost=line.qty * line.cost_price,
            volume_m3=line.qty * line.volume_m3,
        )
        for line in order.lines
    ]
    return OrderOut(
        id=order.id,
        status=order.status,
        status_label=service.STATUS_LABELS[order.status],
        supplier=order.supplier.name if order.supplier else None,
        note=order.note,
        created_at=order.created_at,
        created_by=order.created_by.display_name if order.created_by else None,
        placed_at=order.placed_at,
        closed_at=order.closed_at,
        lines_count=len(order.lines),
        total_qty=sum(line.qty for line in order.lines),
        total_cost=sum(line.qty * line.cost_price for line in order.lines),
        total_volume_m3=sum(line.qty * line.volume_m3 for line in order.lines),
        next_statuses=service.TRANSITIONS[order.status],
        lines=lines if with_lines else [],
    )


@router.get("", response_model=list[OrderOut], summary="Заказы поставщикам")
def list_orders(
    order_status: OrderStatus | None = Query(None, alias="status"),
    supplier_id: int | None = Query(None),
    session: Session = Depends(get_session),
) -> list[OrderOut]:
    return [_to_out(order) for order in service.list_orders(session, order_status, supplier_id)]


@router.post(
    "",
    response_model=OrderOut,
    status_code=http_status.HTTP_201_CREATED,
    summary="Создать черновик заказа",
)
def create_order(
    payload: OrderIn,
    session: Session = Depends(get_session),
    user: User = Depends(require_editor),
) -> OrderOut:
    """Создаёт черновик из строк закупочной потребности. Поставщик берётся из строк."""
    try:
        order = service.create_order(
            session,
            [
                service.OrderLineInput(
                    pair_id=line.pair_id, qty=line.qty, arrival_week=line.arrival_week
                )
                for line in payload.lines
            ],
            author=user,
            note=payload.note,
        )
    except service.OrderError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return _to_out(order)


@router.get("/{order_id}", response_model=OrderOut, summary="Заказ целиком")
def get_order(order_id: int, session: Session = Depends(get_session)) -> OrderOut:
    order = service.get_order(session, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    return _to_out(order)


@router.get("/{order_id}/export", summary="Выгрузка заказа в XLSX")
def export_order(order_id: int, session: Session = Depends(get_session)) -> Response:
    """Файл для отправки поставщику: шапка заказа, строки и итоги."""
    order = service.get_order(session, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Заказ не найден")

    filename = order_filename(order)
    # Content-Disposition ограничен latin-1, поэтому кириллическое имя уходит
    # по RFC 5987, а рядом остаётся ASCII-запасной вариант для старых клиентов.
    disposition = (
        f"attachment; filename=\"order-{order.id}.xlsx\"; filename*=UTF-8''{quote(filename)}"
    )
    return Response(
        content=build_order_workbook(order),
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": disposition},
    )


@router.post("/{order_id}/status", response_model=OrderOut, summary="Сменить статус заказа")
def set_status(
    order_id: int,
    payload: OrderStatusIn,
    session: Session = Depends(get_session),
    user: User = Depends(require_editor),
) -> OrderOut:
    """Размещение создаёт поставки «в пути», отмена их убирает, приёмка закрывает заказ."""
    order = service.get_order(session, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    try:
        order = service.set_status(session, order, payload.status)
    except service.OrderError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _to_out(order)
