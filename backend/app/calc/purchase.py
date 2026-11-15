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


#: Недель в году — делитель, превращающий доходность за цикл в годовую.
WEEKS_IN_YEAR = 52.0


def order_roi(
    qty: int,
    window_demand: float,
    coverage_weeks_used: int,
    params: PurchaseParams,
) -> tuple[float | None, float | None, float | None]:
    """Доходность вложенных в заказ денег: проценты годовых, недели распродажи, цикл.

    **[ДОПУЩЕНИЕ, Q-2]** В ТЗ ROI назван без определения. Принято: деньги
    вкладываются в момент заказа и возвращаются с маржой, когда заказ распродан,
    поэтому

        ROI = (цена − себестоимость) / себестоимость × 52 / цикл,
        цикл = недели до прихода + недели распродажи,
        недели распродажи = количество / средний недельный спрос окна.

    Почему годовые, а не «маржа на рубль»: закупка — это выбор, во что вложить
    ограниченные деньги. Позиция с маржой 30 %, распродающаяся за месяц, лучше
    позиции с маржой 80 %, которая лежит полгода, и увидеть это можно только
    приведя обе к одному сроку.

    Чего в числе нет: комиссии маркетплейса, логистики, хранения и возвратов —
    этих данных в системе нет вовсе. Значит, ROI **завышен** и годится для
    сравнения позиций между собой, а не как обещание доходности. Когда заказчик
    ответит на Q-2, меняется одна эта функция.

    ``None`` возвращается там, где считать не из чего: нет цены, себестоимости,
    спроса или количества. Ноль означал бы «не окупается», а это не то же самое.
    """
    weeks_of_demand = max(1, coverage_weeks_used)
    weekly_demand = window_demand / weeks_of_demand
    if qty <= 0 or params.cost_price <= 0 or params.price <= 0 or weekly_demand <= 0:
        return None, None, None

    sell_out_weeks = qty / weekly_demand
    cycle_weeks = params.lead_time_days / 7 + sell_out_weeks
    if cycle_weeks <= 0:
        return None, None, None

    margin = (params.price - params.cost_price) / params.cost_price
    return margin * WEEKS_IN_YEAR / cycle_weeks * 100, sell_out_weeks, cycle_weeks


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

    order_date = week_start(shortage_week) - timedelta(days=params.lead_time_days)
    is_overdue = order_date < today
    effective_order = max(order_date, today)
    arrival_week = week_key(effective_order + timedelta(days=params.lead_time_days))

    # Окно покрытия начинается не раньше недели, в которую товар физически может
    # прийти. Когда заказ не просрочен, это и есть неделя дефицита. Когда просрочен —
    # более поздняя: недели до прихода закрыть уже нечем, и считать на них закупку
    # значит заказывать то, что всё равно не успеет. Без этого заказ, размещённый
    # по просроченной строке, не уменьшал бы потребность, и она висела бы вечно.
    if arrival_week > shortage_week and arrival_week not in weeks:
        return PurchaseSuggestion(
            needed=False,
            shortage_week=shortage_week,
            order_date=order_date,
            arrival_week=arrival_week,
            safety_stock=safety,
            is_overdue=is_overdue,
            reason="товар придёт за горизонтом планирования",
        )

    start_week = max(shortage_week, arrival_week)
    start_idx = weeks.index(start_week)
    # Дефицит не переносится на будущее: непроданное из-за нехватки — потерянные
    # продажи, а не долг. Поэтому минус на входе в окно обнуляется, иначе заказ
    # раздувался бы на объём, который уже не продать.
    stock_before = max(0.0, opening_stock if start_idx == 0 else projected[weeks[start_idx - 1]])
    window = weeks[start_idx : start_idx + max(1, params.coverage_weeks)]

    window_demand = sum(demand.get(w, 0.0) for w in window)
    window_transit = sum(in_transit.get(w, 0.0) for w in window)
    need = window_demand + safety - stock_before - window_transit

    qty = apply_constraints(need, params)
    roi_percent, sell_out_weeks, cycle_weeks = order_roi(qty, window_demand, len(window), params)

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
        coverage_from=window[0],
        coverage_to=window[-1],
        coverage_weeks_used=len(window),
        window_demand=window_demand,
        window_transit=window_transit,
        stock_before=stock_before,
        roi_percent=roi_percent,
        sell_out_weeks=sell_out_weeks,
        cycle_weeks=cycle_weeks,
        reason=(
            f"остаток опускается ниже страхового запаса на неделе {shortage_week}"
            if qty > 0
            else "расчётная потребность неположительна"
        ),
    )
