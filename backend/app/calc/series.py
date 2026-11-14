"""Прогноз → остатки (схема 02 из ТЗ).

Ключевой принцип ТЗ: обрезается только адаптированный прогноз, исходный спрос
остаётся нетронутым и именно он идёт в расчёт закупки. Нулевые продажи из-за
шортажа не превращаются в нулевую потребность.

Формулы — `docs/03-calc-spec.md`.
"""

from __future__ import annotations

from .types import CalcSettings, SeriesInput, SeriesResult, Status, WeekCell
from .weeks import week_number


def seasonal_demand(data: SeriesInput, week: str) -> float:
    """Базовый потенциал SKU × коэффициент сезонности категории."""
    coef = data.seasonality.get(week)
    if coef is None:
        coef = data.seasonality.get(str(week_number(week)), 1.0)
    return data.base_potential * coef


def original_forecast(data: SeriesInput, week: str) -> float:
    """Исходный прогноз — плановый спрос без оглядки на наличие товара."""
    if data.is_closed(week):
        # После закрытия недели прогноз зафиксирован как её план.
        if week in data.plan:
            return float(data.plan[week])
    elif week in data.manual_forecast:
        # Ручная правка имеет абсолютный приоритет (критерий приёмки № 2).
        return float(data.manual_forecast[week])
    diff = data.diff_percent.get(week, 100.0) / 100.0
    return round(seasonal_demand(data, week) * diff)


def compute_series(data: SeriesInput, settings: CalcSettings | None = None) -> SeriesResult:
    """Полная недельная матрица для одной пары (магазин, товар)."""
    settings = settings or CalcSettings()
    cells: list[WeekCell] = []

    real_stock = float(data.opening_stock)
    adapted_stock = float(data.opening_stock)

    for week in data.weeks:
        closed = data.is_closed(week)
        supply = float(data.supply.get(week, 0.0))
        in_transit = float(data.in_transit.get(week, 0.0))
        original = original_forecast(data, week)

        if closed:
            fact = data.fact_sales.get(week)
            adapted = float(fact) if fact is not None else original
            desired = adapted
            # Физический остаток из WB — источник истины для закрытых недель.
            fact_stock = data.fact_stock_end.get(week)
            if fact_stock is not None:
                real_stock = float(fact_stock)
                adapted_stock = float(fact_stock)
            else:
                real_stock = real_stock + supply - original
                adapted_stock = max(0.0, adapted_stock + supply - adapted)
            cell = WeekCell(
                week=week,
                is_closed=True,
                original_forecast=original,
                adapted_forecast=adapted,
                real_stock=real_stock,
                adapted_stock=adapted_stock,
                fact_sales=float(fact) if fact is not None else None,
                supply=supply,
                in_transit=in_transit,
                diff_percent=None,
                shortage_units=max(0.0, original - adapted),
                stock_limited=False,
                is_manual=False,
            )
        else:
            available = adapted_stock + supply
            desired = float(data.manual_adapted.get(week, original))
            adapted = min(desired, available)
            adapted_stock = available - adapted
            real_stock = real_stock + supply - original
            cell = WeekCell(
                week=week,
                is_closed=False,
                original_forecast=original,
                adapted_forecast=adapted,
                real_stock=real_stock,
                adapted_stock=adapted_stock,
                fact_sales=None,
                supply=supply,
                in_transit=in_transit,
                diff_percent=data.diff_percent.get(week, 100.0),
                shortage_units=max(0.0, original - adapted),
                stock_limited=adapted < desired,
                is_manual=week in data.manual_forecast or week in data.manual_adapted,
            )

        cells.append(cell)

    result = SeriesResult(cells=cells, opening_stock=float(data.opening_stock))
    result.coverage_weeks = _coverage_weeks(data, cells, settings)
    result.status = _status(cells, result.coverage_weeks, settings)
    return result


def _future_cells(cells: list[WeekCell]) -> list[WeekCell]:
    return [c for c in cells if not c.is_closed]


def _coverage_weeks(
    data: SeriesInput, cells: list[WeekCell], settings: CalcSettings
) -> float | None:
    """На сколько недель спроса хватает текущего остатка."""
    future = _future_cells(cells)
    if not future:
        return None
    window = future[: settings.demand_avg_weeks]
    avg = sum(c.original_forecast for c in window) / len(window)
    if avg <= 0:
        return None
    # Остаток на момент начала планового периода.
    opening = data.opening_stock
    closed = [c for c in cells if c.is_closed]
    if closed:
        opening = closed[-1].adapted_stock
    return opening / avg


def _status(cells: list[WeekCell], coverage: float | None, settings: CalcSettings) -> Status:
    # SHORT — только про ближайшее окно: на длинном горизонте без плановых закупок
    # дефицит наступает у любой позиции, и статус перестал бы что-либо различать.
    window = _future_cells(cells)[: max(1, settings.short_horizon_weeks)]
    if any(c.shortage_units > 0 for c in window):
        return Status.SHORT
    if coverage is not None and coverage > settings.over_coverage_weeks:
        return Status.OVER
    return Status.OK
