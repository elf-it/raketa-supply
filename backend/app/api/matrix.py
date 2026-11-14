from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from ..calc import Level
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
    session: Session = Depends(get_session),
) -> MatrixOut:
    """Один экран, три уровня агрегации, без дерева строк (ТЗ, стр. 3)."""
    matrix = build_matrix(
        session,
        level,
        MatrixFilters(
            store_id=store_id,
            category_id=category_id,
            product_id=product_id,
            search=search,
        ),
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
    session: Session = Depends(get_session),
) -> Response:
    """Те же строки и недели, что на экране: файл собирается тем же сервисом."""
    matrix = build_matrix(
        session,
        level,
        MatrixFilters(
            store_id=store_id,
            category_id=category_id,
            product_id=product_id,
            search=search,
        ),
    )
    store_name, category_name = filter_names(session, store_id, category_id)
    return xlsx_response(
        build_matrix_workbook(matrix, store_name, category_name),
        matrix_filename(matrix),
        f"matrix-{level.value}.xlsx",
    )
