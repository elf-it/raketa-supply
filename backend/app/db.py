from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings


def _register_unicode_lower(dbapi_connection, _record) -> None:
    """Делает ``lower()`` в SQLite понимающим кириллицу.

    Встроенный ``lower()`` в SQLite обрабатывает только ASCII: ``lower('Анна')``
    возвращает строку без изменений, поэтому поиск без учёта регистра по русским
    названиям и авторам молча не находил ничего. PostgreSQL в проде делает это сам,
    так что подмена нужна только для разработки и тестов.
    """
    dbapi_connection.create_function(
        "lower",
        1,
        lambda value: value.lower() if isinstance(value, str) else value,
        deterministic=True,
    )


def make_engine(url: str | None = None):
    settings = get_settings()
    url = url or settings.database_url
    kwargs: dict = {"future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _register_unicode_lower)
    return engine


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
