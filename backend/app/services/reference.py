"""Базовый потенциал SKU: текущее значение и ориентир по факту.

Задавать потенциал вслепую бессмысленно, поэтому вместе со значением отдаём то,
что продавалось на самом деле, и оценку потенциала, полученную обратным ходом
из факта и сезонности.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..calc import shift_week, week_number
from ..models import Seasonality, WeekFact
from .matrix import (
    MatrixFilters,
    Pair,
    load_app_settings,
    load_pairs,
    resolve_last_closed_week,
)

#: Сколько закрытых недель берём в качестве ориентира.
DEFAULT_FACT_WEEKS = 8


@dataclass
class PotentialRow:
    pair: Pair
    fact_weeks: int
    avg_fact_units: float | None
    suggested_potential: float | None
    #: Недели, выброшенные из оценки: остаток упал до нуля, факт занижен дефицитом.
    excluded_weeks: int


def base_potential_rows(
    session: Session,
    filters: MatrixFilters,
    fact_weeks: int = DEFAULT_FACT_WEEKS,
    today: date | None = None,
) -> list[PotentialRow]:
    app_settings = load_app_settings(session)
    last_closed = resolve_last_closed_week(app_settings, today)
    weeks = [shift_week(last_closed, -i) for i in range(max(1, fact_weeks))][::-1]

    pairs = load_pairs(session, filters)
    if not pairs:
        return []

    facts: dict[int, dict[str, WeekFact]] = defaultdict(dict)
    for row in session.scalars(
        select(WeekFact).where(
            WeekFact.store_product_id.in_([p.id for p in pairs]),
            WeekFact.week.in_(weeks),
        )
    ).all():
        facts[row.store_product_id][row.week] = row

    coefficients: dict[int, dict[int, float]] = defaultdict(dict)
    for row in session.scalars(select(Seasonality)).all():
        coefficients[row.category_id][row.iso_week] = row.coefficient

    rows: list[PotentialRow] = []
    for pair in pairs:
        pair_facts = facts.get(pair.id, {})
        coefs = coefficients.get(pair.category_id or -1, {})

        sales: list[float] = []
        normalized: list[float] = []
        excluded = 0
        for week in weeks:
            fact = pair_facts.get(week)
            if fact is None:
                continue
            sales.append(fact.sales_units)
            # Неделя, в которой товар кончился, занижает факт — в оценку не берём.
            if fact.stock_end_units is not None and fact.stock_end_units <= 0:
                excluded += 1
                continue
            coef = coefs.get(week_number(week), 1.0)
            if coef > 0:
                normalized.append(fact.sales_units / coef)

        rows.append(
            PotentialRow(
                pair=pair,
                fact_weeks=len(sales),
                avg_fact_units=round(sum(sales) / len(sales), 1) if sales else None,
                suggested_potential=(
                    round(sum(normalized) / len(normalized), 1) if normalized else None
                ),
                excluded_weeks=excluded,
            )
        )
    return rows
