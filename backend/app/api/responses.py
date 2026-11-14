"""Ответы с файлом.

Кириллица в имени файла — отдельная история: заголовок ``Content-Disposition``
ограничен latin-1, поэтому имя уходит по RFC 5987, а рядом остаётся ASCII-запасной
вариант для старых клиентов. Место для этого одно на все выгрузки.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import Response
from sqlalchemy.orm import Session

from ..models import Category, Store

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def xlsx_response(content: bytes, filename: str, fallback: str) -> Response:
    disposition = f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=content,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": disposition},
    )


def filter_names(
    session: Session, store_id: int | None, category_id: int | None
) -> tuple[str | None, str | None]:
    """Названия для шапки файла: в файле нужны названия, а не номера фильтров."""
    store = session.get(Store, store_id) if store_id else None
    category = session.get(Category, category_id) if category_id else None
    return (store.name if store else None, category.name if category else None)
