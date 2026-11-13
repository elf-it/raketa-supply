from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..db import get_session
from ..schemas import (
    AdjustmentIn,
    AdjustmentOut,
    BulkAdjustmentIn,
    BulkAdjustmentOut,
    CloseWeekIn,
    JournalBatchPageOut,
    JournalPageOut,
)
from ..services.adjustments import bulk_adjust, close_week, set_adjustment
from ..services.journal import (
    JournalFilters,
    count_batches,
    count_entries,
    list_batches,
    list_entries,
)

router = APIRouter(prefix="/forecast", tags=["Прогноз"])


@router.put(
    "/{pair_id}/{week}",
    response_model=AdjustmentOut,
    summary="Ручная правка прогноза или DIFF",
)
def put_adjustment(
    pair_id: int,
    week: str,
    payload: AdjustmentIn,
    session: Session = Depends(get_session),
) -> AdjustmentOut:
    """Правка переживает пересчёт — критерий приёмки № 2.

    Поля, перечисленные в ``clear``, сбрасываются к расчётному значению.
    """
    try:
        row = set_adjustment(
            session,
            pair_id,
            week,
            diff_percent=payload.diff_percent,
            manual_forecast=payload.manual_forecast,
            manual_adapted=payload.manual_adapted,
            clear=tuple(payload.clear),
            author=payload.author,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return AdjustmentOut(
        pair_id=row.store_product_id,
        week=row.week,
        diff_percent=row.diff_percent,
        manual_forecast=row.manual_forecast,
        manual_adapted=row.manual_adapted,
        author=row.author,
    )


@router.post(
    "/bulk",
    response_model=BulkAdjustmentOut,
    summary="Массовая правка прогноза",
)
def post_bulk_adjustment(
    payload: BulkAdjustmentIn, session: Session = Depends(get_session)
) -> BulkAdjustmentOut:
    """Правка сразу по набору позиций и недель.

    Закрытые недели пропускаются — их план зафиксирован. Каждое изменение попадает
    в журнал корректировок, поэтому массовую правку можно разобрать по шагам.
    """
    try:
        result = bulk_adjust(
            session,
            pair_ids=payload.pair_ids,
            weeks=payload.weeks,
            operation=payload.operation,
            value=payload.value,
            target_week=payload.target_week,
            author=payload.author,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return BulkAdjustmentOut.model_validate(result)


def _journal_filters(
    store_id: int | None,
    category_id: int | None,
    pair_id: int | None,
    search: str | None,
    week_from: str | None,
    week_to: str | None,
    field: str | None,
    author: str | None,
    operation: str | None,
    batch_id: str | None,
) -> JournalFilters:
    return JournalFilters(
        store_id=store_id,
        category_id=category_id,
        pair_id=pair_id,
        search=search,
        week_from=week_from,
        week_to=week_to,
        field=field,
        author=author,
        operation=operation,
        batch_id=batch_id,
    )


@router.get("/log", response_model=JournalPageOut, summary="Журнал корректировок по ячейкам")
def get_log(
    store_id: int | None = Query(None),
    category_id: int | None = Query(None),
    pair_id: int | None = Query(None),
    search: str | None = Query(None, description="Название или артикул"),
    week_from: str | None = Query(None, description="Неделя не раньше, например 2026-W46"),
    week_to: str | None = Query(None),
    field: str | None = Query(None, description="manual_forecast, manual_adapted, diff_percent"),
    author: str | None = Query(None),
    operation: str | None = Query(None, description="manual или имя массовой операции"),
    batch_id: str | None = Query(None, description="Записи одной массовой правки"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> JournalPageOut:
    """Отдельные изменения: что было, что стало, кто и когда."""
    filters = _journal_filters(
        store_id,
        category_id,
        pair_id,
        search,
        week_from,
        week_to,
        field,
        author,
        operation,
        batch_id,
    )
    return JournalPageOut(
        total=count_entries(session, filters),
        limit=limit,
        offset=offset,
        entries=list_entries(session, filters, limit, offset),  # type: ignore[arg-type]
    )


@router.get(
    "/log/batches",
    response_model=JournalBatchPageOut,
    summary="Журнал корректировок по операциям",
)
def get_log_batches(
    store_id: int | None = Query(None),
    category_id: int | None = Query(None),
    pair_id: int | None = Query(None),
    search: str | None = Query(None),
    week_from: str | None = Query(None),
    week_to: str | None = Query(None),
    field: str | None = Query(None),
    author: str | None = Query(None),
    operation: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> JournalBatchPageOut:
    """Массовые правки, свёрнутые в одну строку на операцию.

    Поячеечные правки сюда не попадают: у них нет общего идентификатора,
    и сворачивать их не во что.
    """
    filters = _journal_filters(
        store_id, category_id, pair_id, search, week_from, week_to, field, author, operation, None
    )
    return JournalBatchPageOut(
        total=count_batches(session, filters),
        limit=limit,
        offset=offset,
        batches=list_batches(session, filters, limit, offset),  # type: ignore[arg-type]
    )


@router.post("/close-week", summary="Закрыть неделю и зафиксировать её план")
def post_close_week(payload: CloseWeekIn, session: Session = Depends(get_session)) -> dict:
    frozen = close_week(session, payload.week, payload.author)
    return {"week": payload.week, "frozen_rows": frozen}
