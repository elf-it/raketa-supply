from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..db import get_session
from ..models import Category, Product, Store
from ..schemas import CategoryOut, ProductOut, SettingsOut, StoreOut
from ..services.matrix import load_app_settings

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
        )
        for p in session.scalars(stmt).all()
    ]


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
