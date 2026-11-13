"""Ручные правки прогноза и журнал корректировок.

Критерий приёмки № 2: ручные правки сохраняются и переживают пересчёт.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..calc import BulkOperation, original_forecast
from ..models import AdjustmentLog, ForecastAdjustment, StoreProduct, WeekPlan

EDITABLE_FIELDS = ("diff_percent", "manual_forecast", "manual_adapted")

#: Потолок на размер одной массовой правки, чтобы случайный запрос не положил базу.
MAX_BULK_CELLS = 20_000


def get_adjustment(session: Session, pair_id: int, week: str) -> ForecastAdjustment | None:
    return session.scalar(
        select(ForecastAdjustment).where(
            ForecastAdjustment.store_product_id == pair_id,
            ForecastAdjustment.week == week,
        )
    )


def _ensure_row(session: Session, pair_id: int, week: str) -> ForecastAdjustment:
    row = get_adjustment(session, pair_id, week)
    if row is None:
        row = ForecastAdjustment(store_product_id=pair_id, week=week)
        session.add(row)
        session.flush()
    return row


def _apply_updates(
    session: Session,
    row: ForecastAdjustment,
    updates: dict[str, float | None],
    author: str | None,
) -> int:
    """Пишет поля и заводит запись в журнале на каждое реальное изменение.

    Без commit: вызывающий решает, когда фиксировать, — для массовой правки это
    одна транзакция на весь набор.
    """
    changed = 0
    for name, new_value in updates.items():
        if name not in EDITABLE_FIELDS:
            raise ValueError(f"неизвестное поле правки {name!r}")
        old_value = getattr(row, name)
        if old_value == new_value:
            continue
        setattr(row, name, new_value)
        session.add(
            AdjustmentLog(
                store_product_id=row.store_product_id,
                week=row.week,
                field=name,
                old_value=old_value,
                new_value=new_value,
                author=author,
            )
        )
        changed += 1
    if changed:
        row.author = author or row.author
    return changed


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
    """Создаёт или обновляет правку одной ячейки.

    ``clear`` перечисляет поля, которые нужно сбросить в ``None`` — это кнопка
    «Сбросить» на макете, возвращающая ячейку к расчётному значению.
    """
    if session.get(StoreProduct, pair_id) is None:
        raise ValueError(f"пара магазин/товар {pair_id} не найдена")

    row = _ensure_row(session, pair_id, week)

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

    _apply_updates(session, row, updates, author)
    session.commit()
    return row


# ------------------------------------------------------------------- массовые правки


@dataclass
class BulkResult:
    operation: BulkOperation
    updated_cells: int = 0
    affected_pairs: int = 0
    skipped_closed: int = 0
    moved_units: float = 0.0
    weeks: list[str] = field(default_factory=list)
    target_week: str | None = None


def bulk_adjust(
    session: Session,
    pair_ids: list[int],
    weeks: list[str],
    operation: BulkOperation,
    *,
    value: float | None = None,
    target_week: str | None = None,
    author: str | None = None,
    today: date | None = None,
) -> BulkResult:
    """Массовая правка прогноза по набору пар и недель.

    Что пишет каждая операция и почему:

    * ``scale`` — умножает **видимый** прогноз на ``value`` процентов. Если в ячейке
      стоит ручной прогноз, умножается он; иначе умножается DIFF. DIFF — ровно та
      ручка, которая для этого и предназначена, и в отличие от фиксированного числа
      она продолжает следовать за базовым потенциалом и сезонностью.
    * ``delta`` — прибавляет штуки, поэтому результат приходится зафиксировать
      как ручной прогноз: в процентах его не выразить.
    * ``set`` — ставит одно и то же значение.
    * ``clear`` — сбрасывает ручной прогноз и DIFF, возвращая ячейку к формуле.
    * ``move`` — переносит спрос выбранных недель в ``target_week``: источники
      обнуляются, их сумма добавляется к целевой неделе.

    Закрытые недели пропускаются: их план зафиксирован. Каждое изменение попадает
    в журнал корректировок.
    """
    from .matrix import (
        MatrixFilters,
        build_series_inputs,
        load_app_settings,
        load_pairs,
        resolve_last_closed_week,
    )

    if not pair_ids:
        raise ValueError("не выбрано ни одной позиции")
    if not weeks:
        raise ValueError("не выбрано ни одной недели")
    if len(pair_ids) * len(weeks) > MAX_BULK_CELLS:
        raise ValueError(
            f"слишком большая правка: {len(pair_ids) * len(weeks)} ячеек, "
            f"предел — {MAX_BULK_CELLS}"
        )

    if operation in (BulkOperation.SCALE, BulkOperation.DELTA, BulkOperation.SET):
        if value is None:
            raise ValueError(f"для операции {operation.value} нужно значение")
        if operation is BulkOperation.SCALE and value <= 0:
            raise ValueError("множитель должен быть больше нуля")
        if operation is BulkOperation.SET and value < 0:
            raise ValueError("прогноз не может быть отрицательным")

    app_settings = load_app_settings(session)
    last_closed = resolve_last_closed_week(app_settings, today)

    open_weeks = sorted({w for w in weeks if w > last_closed})
    skipped_closed = (len(set(weeks)) - len(open_weeks)) * len(pair_ids)
    if not open_weeks:
        return BulkResult(
            operation=operation, skipped_closed=skipped_closed, weeks=[], target_week=target_week
        )

    move_target = ""
    if operation is BulkOperation.MOVE:
        if not target_week:
            raise ValueError("для переноса нужна целевая неделя")
        if target_week <= last_closed:
            raise ValueError("нельзя переносить спрос в закрытую неделю")
        if target_week in open_weeks:
            raise ValueError("целевая неделя не может быть среди переносимых")
        move_target = target_week

    # Текущие значения нужны всем операциям, кроме clear и set.
    horizon = sorted(set(open_weeks) | ({target_week} if target_week else set()))
    pairs = [p for p in load_pairs(session, MatrixFilters()) if p.id in set(pair_ids)]
    if not pairs:
        raise ValueError("ни одна из выбранных позиций не найдена")
    inputs = build_series_inputs(session, pairs, horizon, last_closed)

    result = BulkResult(
        operation=operation,
        skipped_closed=skipped_closed,
        weeks=open_weeks,
        target_week=target_week if operation is BulkOperation.MOVE else None,
    )
    touched_pairs: set[int] = set()

    for pair in pairs:
        data = inputs[pair.id]

        if operation is BulkOperation.MOVE:
            moved = 0.0
            for week in open_weeks:
                moved += original_forecast(data, week)
            if moved <= 0:
                continue
            for week in open_weeks:
                row = _ensure_row(session, pair.id, week)
                changed = _apply_updates(
                    session, row, {"manual_forecast": 0.0, "diff_percent": None}, author
                )
                if changed:
                    result.updated_cells += 1
                    touched_pairs.add(pair.id)
            target_row = _ensure_row(session, pair.id, move_target)
            target_value = original_forecast(data, move_target) + moved
            if _apply_updates(
                session, target_row, {"manual_forecast": round(target_value)}, author
            ):
                result.updated_cells += 1
                touched_pairs.add(pair.id)
            result.moved_units += moved
            continue

        for week in open_weeks:
            updates: dict[str, float | None] = {}

            if operation is BulkOperation.CLEAR:
                updates = {"manual_forecast": None, "diff_percent": None}
            elif operation is BulkOperation.SET:
                updates = {"manual_forecast": float(value or 0)}
            elif operation is BulkOperation.DELTA:
                current = original_forecast(data, week)
                updates = {"manual_forecast": max(0.0, round(current + float(value or 0)))}
            elif operation is BulkOperation.SCALE:
                factor = float(value or 100) / 100.0
                if week in data.manual_forecast:
                    updates = {"manual_forecast": round(data.manual_forecast[week] * factor)}
                else:
                    current_diff = data.diff_percent.get(week, 100.0)
                    updates = {"diff_percent": round(current_diff * factor, 2)}

            row = _ensure_row(session, pair.id, week)
            if _apply_updates(session, row, updates, author):
                result.updated_cells += 1
                touched_pairs.add(pair.id)

    result.affected_pairs = len(touched_pairs)
    session.commit()
    return result


# ------------------------------------------------------------------------ закрытие недели


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
