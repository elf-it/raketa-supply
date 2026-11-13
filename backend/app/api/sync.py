from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_session
from ..integrations.wb import make_client, sync_all
from ..models import SyncRun
from ..schemas import SyncOut

router = APIRouter(prefix="/sync", tags=["Синхронизация"])


@router.post("/wb", response_model=list[SyncOut], summary="Загрузить данные всех кабинетов WB")
def post_sync_wb(
    days: int = Query(90, ge=1, le=400, description="Глубина загрузки в днях"),
    session: Session = Depends(get_session),
) -> list[SyncOut]:
    """Загружает все активные кабинеты одновременно, не смешивая магазины.

    Источник задаётся переменной ``WB_CLIENT``: ``fixtures`` или ``http``.
    """
    settings = get_settings()
    client = make_client(settings.wb_client, settings.wb_token_map, settings.wb_api_base)
    reports = sync_all(session, client, days=days)
    return [
        SyncOut(
            store_code=r.store_code,
            facts_upserted=r.facts_upserted,
            unknown_nm_ids=r.unknown_nm_ids or [],
            error=r.error,
        )
        for r in reports
    ]


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
