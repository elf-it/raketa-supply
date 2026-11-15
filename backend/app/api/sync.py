from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_session
from ..integrations.wb import make_client, sync_all
from ..models import SyncRun, User
from ..observability import event
from ..schemas import SyncHealthOut, SyncOut
from ..services.monitoring import sync_health
from .deps import require_editor

router = APIRouter(prefix="/sync", tags=["Синхронизация"])


@router.post("/wb", response_model=list[SyncOut], summary="Загрузить данные всех кабинетов WB")
def post_sync_wb(
    days: int = Query(90, ge=1, le=400, description="Глубина загрузки в днях"),
    _: User = Depends(require_editor),
    session: Session = Depends(get_session),
) -> list[SyncOut]:
    """Загружает все активные кабинеты одновременно, не смешивая магазины.

    Источник задаётся переменной ``WB_CLIENT``: ``fixtures`` или ``http``.
    """
    settings = get_settings()
    client = make_client(settings.wb_client, settings.wb_token_map, settings.wb_api_base)
    reports = sync_all(session, client, days=days)
    failed = [report.store_code for report in reports if not report.ok]
    event(
        "синхронизация завершена",
        level=logging.WARNING if failed else logging.INFO,
        stores=len(reports),
        facts=sum(report.facts_upserted for report in reports),
        failed=",".join(failed) or "-",
        source=settings.wb_client,
    )
    return [
        SyncOut(
            store_code=r.store_code,
            facts_upserted=r.facts_upserted,
            unknown_nm_ids=r.unknown_nm_ids or [],
            error=r.error,
        )
        for r in reports
    ]


@router.get("/status", response_model=SyncHealthOut, summary="Состояние загрузки данных")
def sync_status(session: Session = Depends(get_session)) -> SyncHealthOut:
    """Свежесть данных по каждому кабинету — для плашки в интерфейсе (задача G-4).

    Здесь, в отличие от `/health/ready`, есть названия кабинетов и время
    последней загрузки: ручка закрыта ролью, и это уже не проверка снаружи,
    а ответ человеку на вопрос «почему числа не те».
    """
    return SyncHealthOut.model_validate(sync_health(session))


@router.get("/runs", summary="Журнал синхронизаций")
def list_runs(
    limit: int = Query(50, le=500), session: Session = Depends(get_session)
) -> list[dict]:
    rows = session.scalars(select(SyncRun).order_by(SyncRun.started_at.desc()).limit(limit)).all()
    return [
        {
            "id": r.id,
            "store_id": r.store_id,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "status": r.status.value,
            "facts_upserted": r.facts_upserted,
            "message": r.message,
        }
        for r in rows
    ]
