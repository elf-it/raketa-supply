"""Боевая сборка в контейнерах не должна разойтись с приложением (задача A-7).

Файл compose и образы живут рядом с кодом, но не выполняются ни тестами,
ни линтером: переименованная настройка тихо перестанет доезжать до приложения,
и узнают об этом на развёртывании. Здесь проверяется то, что можно проверить
без Docker: что имена настроек существуют, что небезопасные значения не стоят
по умолчанию и что проверка здоровья контейнера — это живость, а не готовность.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from app.config import Settings

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.prod.yml"
NGINX = ROOT / "frontend" / "nginx.conf"
BACKEND_DOCKERFILE = ROOT / "backend" / "Dockerfile"
EXAMPLE = ROOT / ".env.prod.example"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _env(service: dict) -> dict[str, str]:
    return {key: str(value) for key, value in (service.get("environment") or {}).items()}


def test_every_setting_in_compose_exists_in_the_application(compose):
    """Опечатка или переименование настройки — это тихо непереданное значение."""
    known = set(Settings.model_fields)
    for name in ("backend", "migrate"):
        for key in _env(compose["services"][name]):
            assert key.lower() in known, f"{name}: приложение не знает настройки {key}"


def test_unsafe_defaults_are_not_shipped(compose):
    env = _env(compose["services"]["backend"])

    assert env["DEBUG"] == "false", "иначе не включатся проверки ключа и куки"
    assert "COOKIE_SECURE:-true" in env["COOKIE_SECURE"]
    # Ключ и пароль базы обязаны прийти снаружи: `:?` роняет compose с текстом,
    # а не поднимает систему с ключом по умолчанию.
    assert env["SECRET_KEY"].startswith("${SECRET_KEY:?")
    assert "POSTGRES_PASSWORD:?" in _env(compose["services"]["db"])["POSTGRES_PASSWORD"]
    assert "SECRET_KEY=" in EXAMPLE.read_text(encoding="utf-8"), "образец .env рядом"


def test_number_of_proxies_matches_the_nginx_in_front(compose):
    """TRUSTED_PROXY_HOPS врать нельзя: по нему читается адрес клиента."""
    nginx = NGINX.read_text(encoding="utf-8")
    hops = re.findall(r"proxy_set_header\s+X-Forwarded-For", nginx)

    assert len(hops) == 1, "в этом compose перед приложением ровно один прокси"
    assert "TRUSTED_PROXY_HOPS:-1" in _env(compose["services"]["backend"])["TRUSTED_PROXY_HOPS"]


def test_schema_is_migrated_by_a_separate_step(compose):
    """Приложение миграции не применяет: несколько воркеров начали бы разом."""
    migrate = compose["services"]["migrate"]
    backend = compose["services"]["backend"]

    assert migrate["command"] == ["alembic", "upgrade", "head"]
    assert backend["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert backend["depends_on"]["db"]["condition"] == "service_healthy"


def test_container_health_is_liveness_not_readiness(compose):
    """Готовность краснеет от неудачной загрузки WB, а контейнер за это убивать
    нельзя: перезапуск приложения данных в кабинете не поправит (задача G-4)."""
    probe = " ".join(compose["services"]["backend"]["healthcheck"]["test"])

    assert "/api/v1/health" in probe
    assert "/ready" not in probe


def test_backend_image_is_lean_and_not_root():
    dockerfile = BACKEND_DOCKERFILE.read_text(encoding="utf-8")

    assert "requirements.txt" in dockerfile
    assert "requirements-dev.txt" not in dockerfile, "линтер и pytest в образе не нужны"
    assert re.search(r"^USER raketa$", dockerfile, re.MULTILINE), "процесс не от root"
    ignored = (ROOT / "backend" / ".dockerignore").read_text(encoding="utf-8")
    assert "tests/" in ignored and ".env" in ignored


def test_interface_and_api_live_on_one_origin():
    """Кука стоит с SameSite=Lax: с другого домена браузер её не отправит."""
    nginx = NGINX.read_text(encoding="utf-8")

    assert "location /api/" in nginx
    assert "proxy_pass http://raketa_api" in nginx
    # Номер запроса из прокси приложение сохраняет (задача G-4).
    assert "proxy_set_header X-Request-Id $request_id" in nginx
