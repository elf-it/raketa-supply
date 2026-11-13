"""Запуск миграций из кода.

Схема живёт в Alembic, а не в ``create_all``: на боевых данных пересоздавать
таблицы нельзя. Приложение само миграции не применяет — оно только проверяет,
что база на последней ревизии, и падает с понятным текстом, если это не так.
Применять их должен явный шаг развёртывания.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from .config import get_settings

BACKEND_DIR = Path(__file__).resolve().parent.parent
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
ALEMBIC_DIR = BACKEND_DIR / "alembic"


def engine_url(engine: Engine) -> str:
    """URL движка в пригодном для подключения виде.

    ``str(engine.url)`` заменяет пароль звёздочками, и Alembic с таким URL
    не подключился бы к Postgres.
    """
    return engine.url.render_as_string(hide_password=False)


def alembic_config(url: str | None = None) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    config.set_main_option("sqlalchemy.url", url or get_settings().database_url)
    return config


def head_revision(url: str | None = None) -> str | None:
    script = ScriptDirectory.from_config(alembic_config(url))
    return script.get_current_head()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def upgrade(url: str | None = None, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str | None = None, revision: str = "base") -> None:
    command.downgrade(alembic_config(url), revision)


def assert_current(engine: Engine) -> None:
    """Проверяет, что база на последней ревизии.

    Приложение не создаёт и не правит схему само, поэтому расхождение — это повод
    остановиться, а не молча работать на устаревшей базе.
    """
    head = head_revision(engine_url(engine))
    current = current_revision(engine)
    if current == head:
        return
    if current is None:
        raise RuntimeError(
            "База не инициализирована: нет таблицы alembic_version. "
            "Выполните `alembic upgrade head` в папке backend "
            "или `python -m app.seed --reset` для демо-данных."
        )
    raise RuntimeError(
        f"Схема базы устарела: ревизия {current}, ожидается {head}. "
        "Выполните `alembic upgrade head` в папке backend."
    )
