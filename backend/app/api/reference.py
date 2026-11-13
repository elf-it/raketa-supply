"""Базовый потенциал SKU и сезонность категорий (ТЗ, п. 03)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Category, Seasonality, StoreProduct
from ..schemas import (
    BasePotentialIn,
    BasePotentialOut,
    SeasonalityIn,
    SeasonalityOut,
)
from ..services.matrix import MatrixFilters
from ..services.reference import DEFAULT_FACT_WEEKS, base_potential_rows

router = APIRouter(prefix="/reference", tags=["Справочные показатели"])


@router.get(
    "/base-potential",
    response_model=list[BasePotentialOut],
    summary="Базовый потенциал SKU",
)
def list_base_potential(
    store_id: int | None = Query(None),
    category_id: int | None = Query(None),
    search: str | None = Query(None),
    fact_weeks: int = Query(
        DEFAULT_FACT_WEEKS, ge=1, le=52, description="Сколько закрытых недель брать в ориентир"
    ),
    session: Session = Depends(get_session),
) -> list[BasePotentialOut]:
    """Потенциал вместе с фактом закрытых недель и оценкой по нему."""
    rows = base_potential_rows(
        session,
        MatrixFilters(store_id=store_id, category_id=category_id, search=search),
        fact_weeks=fact_weeks,
    )
    return [
        BasePotentialOut(
            pair_id=row.pair.id,
            store_name=row.pair.store_name,
            vendor_code=row.pair.vendor_code,
            title=row.pair.title,
            category=row.pair.category_name,
            base_potential=row.pair.base_potential,
            fact_weeks=row.fact_weeks,
            avg_fact_units=row.avg_fact_units,
            suggested_potential=row.suggested_potential,
            excluded_weeks=row.excluded_weeks,
        )
        for row in rows
    ]


@router.put(
    "/base-potential/{pair_id}",
    response_model=BasePotentialOut,
    summary="Изменить базовый потенциал",
)
def put_base_potential(
    pair_id: int, payload: BasePotentialIn, session: Session = Depends(get_session)
) -> BasePotentialOut:
    pair = session.get(StoreProduct, pair_id)
    if pair is None:
        raise HTTPException(status_code=404, detail="пара магазин/товар не найдена")
    pair.base_potential = payload.base_potential
    session.commit()

    # Возвращаем строку целиком, вместе с ориентиром по факту.
    rows = base_potential_rows(
        session, MatrixFilters(store_id=pair.store_id, product_id=pair.product_id)
    )
    row = next((r for r in rows if r.pair.id == pair_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail="пара магазин/товар неактивна")
    return BasePotentialOut(
        pair_id=row.pair.id,
        store_name=row.pair.store_name,
        vendor_code=row.pair.vendor_code,
        title=row.pair.title,
        category=row.pair.category_name,
        base_potential=row.pair.base_potential,
        fact_weeks=row.fact_weeks,
        avg_fact_units=row.avg_fact_units,
        suggested_potential=row.suggested_potential,
        excluded_weeks=row.excluded_weeks,
    )


@router.get("/seasonality", response_model=list[SeasonalityOut], summary="Сезонность категорий")
def list_seasonality(session: Session = Depends(get_session)) -> list[SeasonalityOut]:
    categories = session.scalars(select(Category).order_by(Category.name)).all()
    rows = session.scalars(select(Seasonality)).all()
    by_category: dict[int, dict[int, float]] = {}
    for row in rows:
        by_category.setdefault(row.category_id, {})[row.iso_week] = row.coefficient
    return [
        SeasonalityOut(category_id=c.id, category=c.name, coefficients=by_category.get(c.id, {}))
        for c in categories
    ]


@router.put(
    "/seasonality/{category_id}",
    response_model=SeasonalityOut,
    summary="Изменить коэффициенты сезонности",
)
def put_seasonality(
    category_id: int, payload: SeasonalityIn, session: Session = Depends(get_session)
) -> SeasonalityOut:
    category = session.get(Category, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="категория не найдена")
    for iso_week in payload.coefficients:
        if not 1 <= iso_week <= 53:
            raise HTTPException(
                status_code=400, detail=f"номер недели вне диапазона 1..53: {iso_week}"
            )

    existing = {
        row.iso_week: row
        for row in session.scalars(
            select(Seasonality).where(Seasonality.category_id == category_id)
        ).all()
    }
    for iso_week, coefficient in payload.coefficients.items():
        row = existing.get(iso_week)
        if row is None:
            session.add(
                Seasonality(
                    category_id=category_id,
                    iso_week=iso_week,
                    coefficient=coefficient,
                )
            )
        else:
            row.coefficient = coefficient
    session.commit()

    rows = session.scalars(select(Seasonality).where(Seasonality.category_id == category_id)).all()
    return SeasonalityOut(
        category_id=category_id,
        category=category.name,
        coefficients={r.iso_week: r.coefficient for r in rows},
    )
