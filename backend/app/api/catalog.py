from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..db import get_session
from ..models import Category, Product, Store, User
from ..schemas import (
    CategoryOut,
    PriceCardOut,
    PriceIn,
    PriceOut,
    ProductOut,
    SettingsOut,
    StoreOut,
)
from ..services import prices as price_service
from ..services.matrix import load_app_settings
from .deps import require_editor

router = APIRouter(prefix="/catalog", tags=["Справочники"])


@router.get("/stores", response_model=list[StoreOut], summary="Кабинеты")
def list_stores(session: Session = Depends(get_session)) -> list[StoreOut]:
    stores = session.scalars(select(Store).order_by(Store.code)).all()
    return [
        StoreOut(
            id=s.id,
            code=s.code,
            name=s.name,
            marketplace=s.marketplace.value,
            is_active=s.is_active,
        )
        for s in stores
    ]


@router.get("/categories", response_model=list[CategoryOut], summary="Категории")
def list_categories(session: Session = Depends(get_session)) -> list[CategoryOut]:
    rows = session.scalars(select(Category).order_by(Category.name)).all()
    return [CategoryOut.model_validate(row) for row in rows]


@router.get("/products", response_model=list[ProductOut], summary="Товары")
def list_products(
    search: str | None = Query(None),
    session: Session = Depends(get_session),
) -> list[ProductOut]:
    stmt = (
        select(Product)
        .options(selectinload(Product.category), selectinload(Product.supplier))
        .order_by(Product.vendor_code)
    )
    if search:
        pattern = f"%{search.strip().lower()}%"
        from sqlalchemy import func

        stmt = stmt.where(
            func.lower(Product.title).like(pattern) | func.lower(Product.vendor_code).like(pattern)
        )
    products = list(session.scalars(stmt).all())
    since = price_service.valid_from_by_product(session, [p.id for p in products])
    return [
        ProductOut(
            id=p.id,
            vendor_code=p.vendor_code,
            title=p.title,
            category=p.category.name if p.category else None,
            supplier=p.supplier.name if p.supplier else None,
            cost_price=p.cost_price,
            price=p.price,
            volume_m3=p.volume_m3,
            lead_time_days=p.lead_time_days,
            moq=p.moq,
            pack_size=p.pack_size,
            price_valid_from=since.get(p.id),
        )
        for p in products
    ]


def _product_or_404(session: Session, product_id: int) -> Product:
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="товар не найден")
    return product


def _price_card(session: Session, product: Product) -> PriceCardOut:
    rows = price_service.history(session, product.id)
    current = price_service.effective(rows, date.today())
    return PriceCardOut(
        product_id=product.id,
        vendor_code=product.vendor_code,
        title=product.title,
        supplier=product.supplier.name if product.supplier else None,
        cost_price=product.cost_price,
        price=product.price,
        valid_from=current.valid_from if current else None,
        history=[PriceOut.model_validate(row) for row in rows],
    )


@router.get(
    "/products/{product_id}/prices",
    response_model=PriceCardOut,
    summary="История цен товара",
)
def list_prices(product_id: int, session: Session = Depends(get_session)) -> PriceCardOut:
    """Нынешние цены и вся череда изменений, от новой к старой (задача B-9)."""
    return _price_card(session, _product_or_404(session, product_id))


@router.post(
    "/products/{product_id}/prices",
    response_model=PriceCardOut,
    status_code=http_status.HTTP_201_CREATED,
    summary="Записать новую цену товара",
)
def add_price(
    product_id: int,
    payload: PriceIn,
    session: Session = Depends(get_session),
    user: User = Depends(require_editor),
) -> PriceCardOut:
    """Цена с даты. Если дата уже занята, запись на ней переписывается."""
    product = _product_or_404(session, product_id)
    try:
        price_service.set_price(
            session,
            product,
            valid_from=payload.valid_from,
            cost_price=payload.cost_price,
            price=payload.price,
            author=user.display_name,
            note=payload.note,
        )
    except price_service.PriceError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return _price_card(session, product)


@router.delete(
    "/products/{product_id}/prices/{price_id}",
    response_model=PriceCardOut,
    summary="Убрать запись из истории цен",
)
def delete_price(
    product_id: int,
    price_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_editor),
) -> PriceCardOut:
    """Для ошибочной записи: дата или сумма введены неверно.

    Карточка товара после этого получает то, что действует без убранной записи.
    """
    product = _product_or_404(session, product_id)
    row = price_service.find_row(session, product_id, price_id)
    if row is None:
        raise HTTPException(status_code=404, detail="запись истории цен не найдена")
    price_service.delete_price(session, product, row)
    return _price_card(session, product)


@router.get("/settings", response_model=SettingsOut, summary="Настройки расчёта")
def get_calc_settings(session: Session = Depends(get_session)) -> SettingsOut:
    row = load_app_settings(session)
    return SettingsOut(
        last_closed_week=row.last_closed_week,
        horizon_weeks=row.horizon_weeks,
        history_weeks=row.history_weeks,
        over_coverage_weeks=row.over_coverage_weeks,
        short_horizon_weeks=row.short_horizon_weeks,
        demand_avg_weeks=row.demand_avg_weeks,
        default_lead_time_days=row.default_lead_time_days,
        default_moq=row.default_moq,
        default_pack_size=row.default_pack_size,
        default_coverage_weeks=row.default_coverage_weeks,
        default_safety_weeks=row.default_safety_weeks,
        wb_client=get_settings().wb_client,
    )
