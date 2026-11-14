"""Агрегация недельной матрицы по уровням просмотра (ТЗ, стр. 3).

Минимальная расчётная единица — пара (магазин, товар). Один физический остаток
принадлежит ровно одной паре, поэтому уровни «магазины» и «категории» — это просто
разные группировки одного множества пар. Двойной учёт запасов невозможен
по построению (критерий приёмки № 4).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from operator import attrgetter

from .types import SeriesResult, Status, WeekCell

#: Показатели, которые складываются при агрегации.
ADDITIVE_FIELDS = (
    "original_forecast",
    "adapted_forecast",
    "real_stock",
    "adapted_stock",
    "supply",
    "in_transit",
    "shortage_units",
)

#: Все складываемые показатели ячейки одним обращением.
#: Поимённый ``getattr`` на каждый показатель каждой ячейки — это на итоге
#: по тысяче позиций четверть миллиона вызовов из Python; здесь их столько же,
#: сколько ячеек, а складывает их уже ``sum``.
_additive = attrgetter(*ADDITIVE_FIELDS)

_STATUS_RANK = {Status.OK: 0, Status.OVER: 1, Status.SHORT: 2}


def worst_status(statuses: Iterable[Status]) -> Status:
    """Статус группы — худший из статусов её позиций."""
    return max(statuses, key=lambda s: _STATUS_RANK[s], default=Status.OK)


def aggregate_cells(series: Sequence[SeriesResult], weeks: Sequence[str]) -> list[WeekCell]:
    """Суммирует показатели нескольких позиций по неделям.

    DIFF — процент, он не складывается и на агрегированных уровнях не показывается.
    Факт продаж суммируется только если он есть хотя бы у одной позиции,
    иначе остаётся ``None`` (плановая неделя).
    """
    maps = [s.by_week() for s in series]
    out: list[WeekCell] = []

    for week in weeks:
        cells = [m[week] for m in maps if week in m]
        agg = WeekCell(week=week)
        if not cells:
            out.append(agg)
            continue

        agg.is_closed = all(c.is_closed for c in cells)
        totals = map(sum, zip(*map(_additive, cells), strict=True))
        for field, total in zip(ADDITIVE_FIELDS, totals, strict=True):
            setattr(agg, field, total)

        facts = [c.fact_sales for c in cells if c.fact_sales is not None]
        agg.fact_sales = sum(facts) if facts else None

        agg.diff_percent = None
        agg.stock_limited = any(c.stock_limited for c in cells)
        agg.is_manual = any(c.is_manual for c in cells)
        out.append(agg)

    return out


def aggregate(series: Sequence[SeriesResult], weeks: Sequence[str]) -> SeriesResult:
    return SeriesResult(
        cells=aggregate_cells(series, weeks),
        status=worst_status(s.status for s in series),
        coverage_weeks=None,
    )
