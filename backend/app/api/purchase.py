from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..db import get_session
from ..schemas import PurchaseParamsOut, PurchasePlanOut, PurchaseRowOut
from ..services.matrix import MatrixFilters
from ..services.purchase import PurchaseRow, build_purchase_plan

router = APIRouter(prefix="/purchase", tags=["Закупка"])


def _to_out(row: PurchaseRow) -> PurchaseRowOut:
    s = row.suggestion
    return PurchaseRowOut(
        pair_id=row.pair_id,
        store_name=row.store_name,
        vendor_code=row.vendor_code,
        title=row.title,
        category=row.category,
        supplier=row.supplier,
        current_stock=row.current_stock,
        needed=s.needed,
        shortage_week=s.shortage_week,
        order_date=s.order_date,
        arrival_week=s.arrival_week,
        qty=s.qty,
        qty_before_constraints=s.qty_before_constraints,
        cost=s.cost,
        volume_m3=s.volume_m3,
        safety_stock=s.safety_stock,
        is_overdue=s.is_overdue,
        reason=s.reason,
        params=PurchaseParamsOut.model_validate(row.params),
    )


@router.get("", response_model=PurchasePlanOut, summary="Закупочная потребность")
def get_purchase_plan(
    store_id: int | None = Query(None),
    category_id: int | None = Query(None),
    product_id: int | None = Query(None),
    search: str | None = Query(None),
    only_needed: bool = Query(False, description="Только позиции, требующие заказа"),
    session: Session = Depends(get_session),
) -> PurchasePlanOut:
    """Дата и объём заказа по исходному спросу с учётом lead time, MOQ, PCB, safety."""
    rows = build_purchase_plan(
        session,
        MatrixFilters(
            store_id=store_id,
            category_id=category_id,
            product_id=product_id,
            search=search,
        ),
    )
    if only_needed:
        rows = [r for r in rows if r.suggestion.needed]
    out = [_to_out(r) for r in rows]
    return PurchasePlanOut(
        rows=out,
        total_qty=sum(r.qty for r in out),
        total_cost=sum(r.cost for r in out),
        total_volume_m3=sum(r.volume_m3 for r in out),
        overdue_count=sum(1 for r in out if r.is_overdue and r.needed),
    )
