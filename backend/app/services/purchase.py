"""Закупочная потребность по выборке позиций."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..calc import PurchaseParams, PurchaseSuggestion, suggest_purchase
from ..models import AppSettings, Product, Supplier
from .matrix import (
    MatrixFilters,
    Pair,
    build_week_axis,
    calc_settings,
    compute_results,
    load_app_settings,
    load_pairs,
    resolve_last_closed_week,
)


@dataclass
class PurchaseRow:
    pair_id: int
    store_name: str
    vendor_code: str
    title: str
    category: str | None
    supplier: str | None
    suggestion: PurchaseSuggestion
    params: PurchaseParams
    current_stock: float


def resolve_params(
    product: Product, supplier: Supplier | None, settings: AppSettings
) -> PurchaseParams:
    """Параметры позиции: товар → поставщик → глобальные дефолты."""
    lead = product.lead_time_days
    if lead is None and supplier is not None:
        lead = supplier.lead_time_days
    moq = product.moq
    if moq is None and supplier is not None:
        moq = supplier.moq
    return PurchaseParams(
        lead_time_days=lead if lead is not None else settings.default_lead_time_days,
        moq=moq if moq is not None else settings.default_moq,
        pack_size=product.pack_size or settings.default_pack_size,
        coverage_weeks=product.coverage_weeks or settings.default_coverage_weeks,
        safety_weeks=(
            product.safety_weeks
            if product.safety_weeks is not None
            else settings.default_safety_weeks
        ),
        cost_price=product.cost_price,
        volume_m3=product.volume_m3,
    )


def build_purchase_plan(
    session: Session,
    filters: MatrixFilters,
    today: date | None = None,
) -> list[PurchaseRow]:
    today = today or date.today()
    app_settings = load_app_settings(session)
    settings = calc_settings(app_settings)
    last_closed = resolve_last_closed_week(app_settings, today)
    axis = build_week_axis(last_closed, 1, app_settings.horizon_weeks, today)
    weeks = [w["key"] for w in axis]
    future_weeks = [w for w in weeks if w > last_closed]

    pairs: list[Pair] = load_pairs(session, filters)
    if not pairs:
        return []

    # Те же ряды, что у матрицы, и из того же кэша — только ось своя: закупке
    # не нужна история, ей нужен весь горизонт вперёд.
    results = compute_results(session, pairs, weeks, last_closed, settings, app_settings.calc_epoch)

    products = {
        p.id: p
        for p in session.scalars(
            select(Product).where(Product.id.in_([x.product_id for x in pairs]))
        ).all()
    }
    suppliers = {s.id: s for s in session.scalars(select(Supplier)).all()}

    rows: list[PurchaseRow] = []
    for pair in pairs:
        result = results[pair.id]
        cells = result.by_week()

        # Спрос для закупки — исходный, а не адаптированный (ключевой принцип ТЗ).
        demand = {w: cells[w].original_forecast for w in future_weeks if w in cells}
        # Ожидаемый приход берётся из того же ряда: отдельный поход в базу дал бы
        # второй источник правды о том, что уже заказано.
        in_transit = {w: cells[w].in_transit for w in future_weeks if w in cells}
        # Физический остаток на конец последней закрытой недели.
        closed_cells = [c for c in result.cells if c.is_closed]
        current_stock = closed_cells[-1].adapted_stock if closed_cells else result.opening_stock

        product = products[pair.product_id]
        params = resolve_params(product, suppliers.get(product.supplier_id or -1), app_settings)

        suggestion = suggest_purchase(
            weeks=future_weeks,
            demand=demand,
            in_transit=in_transit,
            opening_stock=current_stock,
            params=params,
            today=today,
            settings=settings,
        )
        rows.append(
            PurchaseRow(
                pair_id=pair.id,
                store_name=pair.store_name,
                vendor_code=pair.vendor_code,
                title=pair.title,
                category=pair.category_name,
                supplier=suppliers[product.supplier_id].name
                if product.supplier_id in suppliers
                else None,
                suggestion=suggestion,
                params=params,
                current_stock=current_stock,
            )
        )

    rows.sort(
        key=lambda r: (
            not r.suggestion.needed,
            r.suggestion.order_date or date.max,
        )
    )
    return rows
