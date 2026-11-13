"""Миграции (задача A-6).

Главный риск при переходе с ``create_all`` — миграции и модели расходятся: код
ждёт колонку, которой в базе нет. Поэтому тест прогоняет миграции на чистой базе
и требует, чтобы автогенерация после этого не нашла ни одного отличия от моделей.
Этот тест ломается ровно тогда, когда кто-то правит модель и забывает ревизию.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from app import migrations
from app.models import Base


@pytest.fixture
def fresh_url(tmp_path: Path) -> str:
    return f"sqlite:///{(tmp_path / 'migrated.db').as_posix()}"


def test_upgrade_creates_schema_matching_models(fresh_url):
    migrations.upgrade(fresh_url)
    engine = create_engine(fresh_url)

    tables = set(inspect(engine).get_table_names())
    expected = set(Base.metadata.tables) | {"alembic_version"}
    assert tables == expected

    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "compare_server_default": True},
        )
        diff = compare_metadata(context, Base.metadata)

    assert diff == [], (
        "Схема миграций разошлась с моделями. "
        "Создайте ревизию: alembic revision --autogenerate -m '<что изменилось>'"
    )
    engine.dispose()


def test_history_has_a_single_head():
    """Две головы — это результат слияния веток, и `upgrade head` на нём упадёт."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(migrations.alembic_config("sqlite://"))
    heads = script.get_heads()
    assert len(heads) == 1, f"в истории миграций несколько голов: {heads}"


def test_downgrade_removes_everything(fresh_url):
    migrations.upgrade(fresh_url)
    migrations.downgrade(fresh_url, "base")

    engine = create_engine(fresh_url)
    tables = set(inspect(engine).get_table_names())
    # alembic_version остаётся, но пустой — это нормально для Alembic.
    assert tables <= {"alembic_version"}
    assert migrations.current_revision(engine) is None
    engine.dispose()


def test_upgrade_is_idempotent(fresh_url):
    migrations.upgrade(fresh_url)
    migrations.upgrade(fresh_url)

    engine = create_engine(fresh_url)
    assert migrations.current_revision(engine) == migrations.head_revision(fresh_url)
    engine.dispose()


def test_assert_current_explains_what_to_do(fresh_url):
    engine = create_engine(fresh_url)

    with pytest.raises(RuntimeError, match="alembic upgrade head"):
        migrations.assert_current(engine)

    migrations.upgrade(fresh_url)
    migrations.assert_current(engine)  # не должно бросать
    engine.dispose()


def test_engine_url_keeps_the_password():
    """str(engine.url) прячет пароль, и Alembic с таким URL не подключился бы.

    Движок здесь не создаётся: проверяется работа с URL, а не подключение,
    поэтому хватает объекта с атрибутом ``url``.
    """
    dsn = "postgresql+psycopg://user:s3cret@localhost:5432/raketa"

    class _FakeEngine:
        url = make_url(dsn)

    assert "***" in str(_FakeEngine.url)
    assert migrations.engine_url(_FakeEngine) == dsn  # type: ignore[arg-type]


def test_seeded_database_is_at_head(seeded_db):
    """Сид разворачивает схему миграциями, а не create_all."""
    from app.db import engine

    assert migrations.current_revision(engine) == migrations.head_revision(
        migrations.engine_url(engine)
    )


def test_tempdir_fixture_is_isolated(fresh_url):
    """Страховка: тест миграций не трогает базу разработки."""
    assert tempfile.gettempdir().replace("\\", "/") in fresh_url or "/tmp" in fresh_url
