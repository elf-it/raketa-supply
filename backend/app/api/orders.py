from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi import status as http_status
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import OrderStatus, PurchaseOrder, User
from ..schemas import (
    OrderIn,
    OrderLineOut,
    OrderLinePatchIn,
    OrderNoteIn,
    OrderOut,
    OrderStatusIn,
)
from ..services import orders as service
from ..services.order_export import build_order_workbook, order_filename
from .deps import require_editor
from .responses import xlsx_response

router = APIRouter(prefix="/orders", tags=["Заказы"])


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


def _as_http(error: service.OrderError) -> HTTPException:
    """Код ответа по смыслу отказа, а не один на все случаи.

    404 — такой строки в заказе нет, 409 — действие не подходит к статусу
    заказа, 422 — переданное значение не годится по содержанию.
    """
    if isinstance(error, service.OrderLineNotFound):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, service.OrderStateError):
        return HTTPException(status_code=409, detail=str(error))
    return HTTPException(status_code=422, detail=str(error))


def _order_or_404(session: Session, order_id: int) -> PurchaseOrder:
    order = service.get_order(session, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    return order


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
    return _to_out(_order_or_404(session, order_id))


@router.get("/{order_id}/export", summary="Выгрузка заказа в XLSX")
def export_order(order_id: int, session: Session = Depends(get_session)) -> Response:
    """Файл для отправки поставщику: шапка заказа, строки и итоги."""
    order = _order_or_404(session, order_id)
    return xlsx_response(
        build_order_workbook(order), order_filename(order), f"order-{order.id}.xlsx"
    )


@router.patch(
    "/{order_id}/lines/{line_id}",
    response_model=OrderOut,
    summary="Правка строки черновика",
)
def update_line(
    order_id: int,
    line_id: int,
    payload: OrderLinePatchIn,
    session: Session = Depends(get_session),
    user: User = Depends(require_editor),
) -> OrderOut:
    """Меняет количество и неделю прихода. Только в черновике."""
    order = _order_or_404(session, order_id)
    try:
        order = service.update_line(
            session, order, line_id, qty=payload.qty, arrival_week=payload.arrival_week
        )
    except service.OrderError as error:
        raise _as_http(error) from error
    return _to_out(order)


@router.delete(
    "/{order_id}/lines/{line_id}",
    response_model=OrderOut,
    summary="Убрать строку из черновика",
)
def delete_line(
    order_id: int,
    line_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_editor),
) -> OrderOut:
    """Убирает позицию из черновика. Последнюю строку убрать нельзя: заказ отменяют."""
    order = _order_or_404(session, order_id)
    try:
        order = service.delete_line(session, order, line_id)
    except service.OrderError as error:
        raise _as_http(error) from error
    return _to_out(order)


@router.patch("/{order_id}", response_model=OrderOut, summary="Правка примечания черновика")
def update_note(
    order_id: int,
    payload: OrderNoteIn,
    session: Session = Depends(get_session),
    user: User = Depends(require_editor),
) -> OrderOut:
    """Примечание уходит поставщику в шапке файла, поэтому правится до размещения."""
    order = _order_or_404(session, order_id)
    try:
        order = service.update_note(session, order, payload.note)
    except service.OrderError as error:
        raise _as_http(error) from error
    return _to_out(order)


@router.post("/{order_id}/status", response_model=OrderOut, summary="Сменить статус заказа")
def set_status(
    order_id: int,
    payload: OrderStatusIn,
    session: Session = Depends(get_session),
    user: User = Depends(require_editor),
) -> OrderOut:
    """Размещение создаёт поставки «в пути», отмена их убирает, приёмка закрывает заказ."""
    order = _order_or_404(session, order_id)
    try:
        order = service.set_status(session, order, payload.status)
    except service.OrderError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _to_out(order)
