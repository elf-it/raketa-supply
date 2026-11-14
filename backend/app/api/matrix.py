from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from sqlalchemy.orm import Session

from ..calc import Level, Status
from ..db import get_session
from ..models import StoreProduct, User
from ..schemas import MatrixImportOut, MatrixOut
from ..services import matrix_import
from ..services.matrix import MatrixFilters, build_matrix
from ..services.matrix_export import build_matrix_workbook, matrix_filename
from .deps import require_editor
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


@router.get("/row", response_model=MatrixOut, summary="Одна строка матрицы с итогами")
def get_matrix_row(
    pair_id: int = Query(..., description="Позиция, строку которой нужно пересчитать"),
    level: Level = Query(Level.SKU, description="Уровень: store, category, sku"),
    store_id: int | None = Query(None),
    category_id: int | None = Query(None),
    product_id: int | None = Query(None),
    search: str | None = Query(None),
    row_status: Status | None = Query(None, alias="status"),
    session: Session = Depends(get_session),
) -> MatrixOut:
    """Ответ на правку ячейки: строка этой позиции, итоги, KPI и счётчики.

    Правка пересчитывает одну позицию — пары независимы друг от друга, — но
    меняет и итоги под таблицей, и KPI над ней. Поэтому отдаётся та же матрица
    с теми же фильтрами, только в окне строк ровно одна: загруженные страницы
    на тысяче позиций весят мегабайты, и перечитывать их ради одной строки
    незачем (задача G-11).

    ``rows`` приходит пустым, если позиции в выборке нет: при отборе по статусу
    правка может вытолкнуть строку из выборки, и за ней сдвигаются остальные —
    тогда экран перечитывает выборку целиком. Несуществующая позиция — другой
    случай, это 404.

    На уровнях магазинов и категорий отдаётся строка группы, в которую входит
    позиция, — та, что на экране и меняется.
    """
    if session.get(StoreProduct, pair_id) is None:
        raise HTTPException(status_code=404, detail="позиция не найдена")
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
        pair_id=pair_id,
    )
    return MatrixOut(
        level=matrix.level,
        weeks=matrix.weeks,  # type: ignore[arg-type]
        rows=matrix.rows,  # type: ignore[arg-type]
        totals=matrix.totals,  # type: ignore[arg-type]
        kpi=matrix.kpi,  # type: ignore[arg-type]
        context=matrix.context,
    )


#: Сколько строк разбора кладём в ответ. Остальное — счётчиками: на тысяче правок
#: подробный список некуда показать, а решение принимают по числам и по началу списка.
IMPORT_PREVIEW_ROWS = 50


@router.post(
    "/import",
    response_model=MatrixImportOut,
    summary="Загрузка правок прогноза из выгруженного файла",
)
def import_matrix(
    file: UploadFile = File(..., description="Выгрузка матрицы по артикулам с правками"),
    apply: bool = Query(False, description="false — только разбор, в базу ничего не пишется"),
    session: Session = Depends(get_session),
    user: User = Depends(require_editor),
) -> MatrixImportOut:
    """Возвращает прогноз из файла в систему — только изменённые ячейки.

    По умолчанию ничего не пишет: сначала человек видит, что именно изменится,
    и только потом подтверждает. Поэтому файл приходит дважды — разбор и
    применение независимы, и сервер не помнит между ними ничего.
    """
    data = file.file.read()
    try:
        report = (
            matrix_import.apply_import(session, data, author=user.display_name)
            if apply
            else matrix_import.analyze(session, data)
        )
    except matrix_import.MatrixImportError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    return MatrixImportOut(
        exported_at=report.exported_at,
        applied=report.applied,
        rows_read=report.rows_read,
        cells_read=report.cells_read,
        change_count=len(report.changes),
        conflict_count=len(report.conflicts),
        applied_cells=report.applied_cells,
        batch_id=report.batch_id,
        changes=report.changes[:IMPORT_PREVIEW_ROWS],  # type: ignore[arg-type]
        conflicts=report.conflicts[:IMPORT_PREVIEW_ROWS],  # type: ignore[arg-type]
        issues=report.issues,  # type: ignore[arg-type]
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
