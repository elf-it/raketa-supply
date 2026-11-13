"""Ручные правки прогноза и журнал корректировок.

Критерий приёмки № 2: ручные правки сохраняются и переживают пересчёт.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AdjustmentLog, ForecastAdjustment, StoreProduct, WeekPlan

EDITABLE_FIELDS = ("diff_percent", "manual_forecast", "manual_adapted")


def get_adjustment(session: Session, pair_id: int, week: str) -> ForecastAdjustment | None:
    return session.scalar(
        select(ForecastAdjustment).where(
            ForecastAdjustment.store_product_id == pair_id,
            ForecastAdjustment.week == week,
        )
    )


def set_adjustment(
    session: Session,
    pair_id: int,
    week: str,
    *,
    diff_percent: float | None = None,
    manual_forecast: float | None = None,
    manual_adapted: float | None = None,
    clear: tuple[str, ...] = (),
    author: str | None = None,
) -> ForecastAdjustment:
    """Создаёт или обновляет правку, записывая каждое изменение в журнал.

    ``clear`` перечисляет поля, которые нужно сбросить в ``None`` — это кнопка
    «Сбросить» на макете, возвращающая ячейку к расчётному значению.
    """
    if session.get(StoreProduct, pair_id) is None:
        raise ValueError(f"пара магазин/товар {pair_id} не найдена")

    row = get_adjustment(session, pair_id, week)
    if row is None:
        row = ForecastAdjustment(store_product_id=pair_id, week=week)
        session.add(row)
        session.flush()

    updates: dict[str, float | None] = {}
    if diff_percent is not None:
        updates["diff_percent"] = diff_percent
    if manual_forecast is not None:
        updates["manual_forecast"] = manual_forecast
    if manual_adapted is not None:
        updates["manual_adapted"] = manual_adapted
    for name in clear:
        if name not in EDITABLE_FIELDS:
            raise ValueError(f"нельзя сбросить поле {name!r}")
        updates[name] = None

    for name, new_value in updates.items():
        old_value = getattr(row, name)
        if old_value == new_value:
            continue
        setattr(row, name, new_value)
        session.add(
            AdjustmentLog(
                store_product_id=pair_id,
                week=week,
                field=name,
                old_value=old_value,
                new_value=new_value,
                author=author,
            )
        )

    row.author = author or row.author
    session.commit()
    return row


def close_week(session: Session, week: str, author: str | None = None) -> int:
    """Фиксирует прогноз недели как её план.

    По макету: «После закрытия недели он фиксируется как её план». План считается
    по тем же формулам, что и в матрице, и дальше не пересчитывается.
    """
    from ..calc import compute_series
    from .matrix import (
        MatrixFilters,
        build_series_inputs,
        calc_settings,
        load_app_settings,
        load_pairs,
    )

    app_settings = load_app_settings(session)
    pairs = load_pairs(session, MatrixFilters())
    if not pairs:
        return 0

    inputs = build_series_inputs(session, pairs, [week], app_settings.last_closed_week)
    existing = {
        (row.store_product_id, row.week): row
        for row in session.scalars(select(WeekPlan).where(WeekPlan.week == week)).all()
    }

    frozen = 0
    for pair in pairs:
        data = inputs[pair.id]
        data.last_closed_week = None  # считаем как плановую неделю
        result = compute_series(data, calc_settings(app_settings))
        value = result.cells[0].original_forecast
        row = existing.get((pair.id, week))
        if row is None:
            session.add(WeekPlan(store_product_id=pair.id, week=week, plan_units=value))
        else:
            row.plan_units = value
        frozen += 1

    app_settings.last_closed_week = max(week, app_settings.last_closed_week or week)
    session.commit()
    return frozen


def adjustment_history(session: Session, pair_id: int | None = None, limit: int = 200):
    stmt = select(AdjustmentLog).order_by(AdjustmentLog.created_at.desc()).limit(limit)
    if pair_id is not None:
        stmt = stmt.where(AdjustmentLog.store_product_id == pair_id)
    return list(session.scalars(stmt).all())
