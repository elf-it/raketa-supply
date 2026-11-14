from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from ..calc import Level, Status
from ..db import get_session
from ..schemas import MatrixOut
from ..services.matrix import MatrixFilters, build_matrix
from ..services.matrix_export import build_matrix_workbook, matrix_filename
from .responses import filter_names, xlsx_response

router = APIRouter(prefix="/matrix", tags=["Матрица"])


@router.get("", response_model=MatrixOut, summary="Недельная матрица по уровню просмотра")
def get_matrix(
    level: Level = Query(Level.SKU, description="Уровень: store, category, sku"),
    store_id: int | None = Query(None, description="Фильтр по магазину"),
    category_id: int | None = Query(None, description="Фильтр по категории"),
    product_id: int | None = Query(None, description="Фильтр по товару"),
    search: str | None = Query(None, description="Поиск по названию, артикулу или nmId"),
    row_status: Status | None = Query(None, alias="status", description="Отбор строк по статусу"),
    offset: int = Query(0, ge=0, description="С какой строки выборки отдавать"),
    limit: int | None = Query(None, ge=1, description="Сколько строк отдать; пусто — все"),
    session: Session = Depends(get_session),
) -> MatrixOut:
    """Один экран, три уровня агрегации, без дерева строк (ТЗ, стр. 3).

    ``offset`` и ``limit`` режут только строки: итоги, KPI и счётчики в ``context``
    считаются по всей выборке. Сколько в ней строк всего — в ``context.row_count``.
    """
    matrix = build_matrix(
        session,
        level,
        MatrixFilters(
            store_id=store_id,
            category_id=category_id,
            product_id=product_id,
            search=search,
        ),
        status=row_status,
        offset=offset,
        limit=limit,
    )
    return MatrixOut(
        level=matrix.level,
        weeks=matrix.weeks,  # type: ignore[arg-type]
        rows=matrix.rows,  # type: ignore[arg-type]
        totals=matrix.totals,  # type: ignore[arg-type]
        kpi=matrix.kpi,  # type: ignore[arg-type]
        context=matrix.context,
    )


@router.get("/export", summary="Выгрузка матрицы в XLSX")
def export_matrix(
    level: Level = Query(Level.SKU),
    store_id: int | None = Query(None),
    category_id: int | None = Query(None),
    product_id: int | None = Query(None),
    search: str | None = Query(None),
    row_status: Status | None = Query(None, alias="status"),
    session: Session = Depends(get_session),
) -> Response:
    """Те же строки и недели, что на экране: файл собирается тем же сервисом.

    Окна строк здесь нет намеренно: в файл идёт вся выборка под фильтрами, а не
    та страница, до которой человек успел доскроллить. Выгрузка — как раз способ
    получить всё целиком.
    """
    matrix = build_matrix(
        session,
        level,
        MatrixFilters(
            store_id=store_id,
            category_id=category_id,
            product_id=product_id,
            search=search,
        ),
        status=row_status,
    )
    store_name, category_name = filter_names(session, store_id, category_id)
    return xlsx_response(
        build_matrix_workbook(matrix, store_name, category_name),
        matrix_filename(matrix),
        f"matrix-{level.value}.xlsx",
    )
