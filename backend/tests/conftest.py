"""Общая настройка тестов.

База для тестов — отдельный SQLite-файл во временной папке. Переменные окружения
выставляются до импорта приложения, иначе движок успеет создаться на боевом URL.

Клиенты заведены по ролям: большинство тестов работает от редактора, отдельные —
от наблюдателя и администратора, чтобы проверять границы прав.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="raketa-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(TMP / 'test.db').as_posix()}"
os.environ["WB_CLIENT"] = "fixtures"
os.environ["SECRET_KEY"] = "tests-only-secret-key"

import pytest  # noqa: E402

FIXTURES_DIR = TMP / "fixtures"

#: Демо-пользователи сида: почта, пароль и отображаемое имя.
ADMIN = ("admin@raketa.local", "demo-admin-2026", "Ольга, администратор")
EDITOR = ("editor@raketa.local", "demo-editor-2026", "Анна, снабжение")
VIEWER = ("viewer@raketa.local", "demo-viewer-2026", "Пётр, руководитель")


@pytest.fixture(scope="session")
def seeded_db():
    from app import seed

    seed.main(reset_db=True, fixtures_dir=FIXTURES_DIR)
    return TMP / "test.db"


def _make_client(credentials: tuple[str, str, str] | None):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    client.__enter__()
    if credentials is not None:
        email, password, _ = credentials
        response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert response.status_code == 200, response.text
    return client


@pytest.fixture(scope="session")
def client(seeded_db):
    """Редактор: может читать и править данные, но не управлять пользователями."""
    test_client = _make_client(EDITOR)
    yield test_client
    test_client.__exit__(None, None, None)


@pytest.fixture(scope="session")
def viewer_client(seeded_db):
    test_client = _make_client(VIEWER)
    yield test_client
    test_client.__exit__(None, None, None)


@pytest.fixture(scope="session")
def admin_client(seeded_db):
    test_client = _make_client(ADMIN)
    yield test_client
    test_client.__exit__(None, None, None)


@pytest.fixture(scope="session")
def anon_client(seeded_db):
    """Без входа — для проверки, что закрытые ручки действительно закрыты."""
    test_client = _make_client(None)
    yield test_client
    test_client.__exit__(None, None, None)
