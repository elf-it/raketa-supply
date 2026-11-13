"""Общая настройка тестов.

База для тестов — отдельный SQLite-файл во временной папке. Переменные окружения
выставляются до импорта приложения, иначе движок успеет создаться на боевом URL.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="raketa-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(TMP / 'test.db').as_posix()}"
os.environ["WB_CLIENT"] = "fixtures"

import pytest  # noqa: E402

FIXTURES_DIR = TMP / "fixtures"


@pytest.fixture(scope="session")
def seeded_db():
    from app import seed

    seed.main(reset_db=True, fixtures_dir=FIXTURES_DIR)
    return TMP / "test.db"


@pytest.fixture(scope="session")
def client(seeded_db):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
