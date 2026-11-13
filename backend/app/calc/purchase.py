"""Расчёт закупочной потребности (схема 03 из ТЗ).

Считается по **исходному** спросу, а не по адаптированному: иначе дефицит занижал бы
потребность, что ТЗ запрещает прямым текстом.

Формулы и допущения — `docs/03-calc-spec.md`.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

from .types import CalcSettings, PurchaseParams, PurchaseSuggestion
from .weeks import week_key, week_start


def safety_stock(
    weeks: list[str],
    demand: dict[str, float],
    params: PurchaseParams,
    settings: CalcSettings,
) -> float:
    """Страховой запас в штуках: safety_weeks × средний недельный спрос."""
    window = weeks[: settings.demand_avg_weeks]
    if not window:
        return 0.0
    avg = sum(demand.get(w, 0.0) for w in window) / len(window)
    return params.safety_weeks * avg


def projected_stock(
    weeks: list[str],
    demand: dict[str, float],
    in_transit: dict[str, float],
    opening_stock: float,
) -> dict[str, float]:
    """Прогноз остатка только по уже заказанным партиям, без плановых закупок."""
    out: dict[str, float] = {}
    prev = float(opening_stock)
    for week in weeks:
        prev = prev + float(in_transit.get(week, 0.0)) - float(demand.get(week, 0.0))
        out[week] = prev
    return out


def apply_constraints(qty: float, params: PurchaseParams) -> int:
    """MOQ и кратность упаковки (PCB)."""
    if qty <= 0:
        return 0
    result = qty
    if params.pack_size and params.pack_size > 1:
        result = math.ceil(result / params.pack_size) * params.pack_size
    if params.moq and result < params.moq:
        result = params.moq
        if params.pack_size and params.pack_size > 1:
            result = math.ceil(result / params.pack_size) * params.pack_size
    return int(result)


def suggest_purchase(
    weeks: list[str],
    demand: dict[str, float],
    in_transit: dict[str, float],
    opening_stock: float,
    params: PurchaseParams,
    today: date,
    settings: CalcSettings | None = None,
) -> PurchaseSuggestion:
    """Дата заказа и объём для одной пары (магазин, товар).

    ``weeks`` — плановый горизонт по возрастанию, без закрытых недель.
    ``opening_stock`` — физический остаток на конец последней закрытой недели.
    """
    settings = settings or CalcSettings()
    if not weeks:
        return PurchaseSuggestion(needed=False, reason="пустой горизонт")

    safety = safety_stock(weeks, demand, params, settings)
    projected = projected_stock(weeks, demand, in_transit, opening_stock)

    shortage_week = next((w for w in weeks if projected[w] < safety), None)
    if shortage_week is None:
        return PurchaseSuggestion(
            needed=False,
            safety_stock=safety,
            reason="остатка хватает на весь горизонт с учётом страхового запаса",
        )

    idx = weeks.index(shortage_week)
    stock_before = opening_stock if idx == 0 else projected[weeks[idx - 1]]
    window = weeks[idx : idx + max(1, params.coverage_weeks)]

    window_demand = sum(demand.get(w, 0.0) for w in window)
    window_transit = sum(in_transit.get(w, 0.0) for w in window)
    need = window_demand + safety - stock_before - window_transit

    qty = apply_constraints(need, params)

    order_date = week_start(shortage_week) - timedelta(days=params.lead_time_days)
    is_overdue = order_date < today
    effective_order = max(order_date, today)
    arrival_week = week_key(effective_order + timedelta(days=params.lead_time_days))

    return PurchaseSuggestion(
        needed=qty > 0,
        shortage_week=shortage_week,
        order_date=order_date,
        arrival_week=arrival_week,
        qty=qty,
        qty_before_constraints=max(0.0, need),
        cost=qty * params.cost_price,
        volume_m3=qty * params.volume_m3,
        safety_stock=safety,
        is_overdue=is_overdue,
        reason=(
            f"остаток опускается ниже страхового запаса на неделе {shortage_week}"
            if qty > 0
            else "расчётная потребность неположительна"
        ),
    )
