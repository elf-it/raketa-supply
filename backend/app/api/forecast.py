from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..db import get_session
from ..schemas import AdjustmentIn, AdjustmentLogOut, AdjustmentOut, CloseWeekIn
from ..services.adjustments import adjustment_history, close_week, set_adjustment

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


@router.get("/log", response_model=list[AdjustmentLogOut], summary="Журнал корректировок")
def get_log(
    pair_id: int | None = Query(None),
    limit: int = Query(200, le=1000),
    session: Session = Depends(get_session),
) -> list[AdjustmentLogOut]:
    return [
        AdjustmentLogOut(
            id=row.id,
            pair_id=row.store_product_id,
            week=row.week,
            field=row.field,
            old_value=row.old_value,
            new_value=row.new_value,
            author=row.author,
            created_at=row.created_at.isoformat(),
        )
        for row in adjustment_history(session, pair_id, limit)
    ]


@router.post("/close-week", summary="Закрыть неделю и зафиксировать её план")
def post_close_week(payload: CloseWeekIn, session: Session = Depends(get_session)) -> dict:
    frozen = close_week(session, payload.week, payload.author)
    return {"week": payload.week, "frozen_rows": frozen}
