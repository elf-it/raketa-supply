"""Первый администратор на чистой базе (задача G-5).

Проверяется то, ради чего команда существует: после неё человек действительно
входит в систему через обычную форму входа — а не просто появляется строка
в таблице. И второе: восстановление доступа закрывает прежние сессии, иначе
смена пароля после утечки не решала бы ту задачу, ради которой её делают.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from conftest import VIEWER
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.admin import PASSWORD_ENV, active_admins, ensure_admin, main, resolve_password
from app.config import get_settings
from app.db import SessionLocal
from app.models import Role, User
from app.security import SESSION_COOKIE, new_session_id, verify_password

NEW = "runbook-admin@raketa.local"


def _login(email: str, password: str) -> int:
    """Вход новым пользователем через обычную ручку, своим клиентом."""
    from app.main import app

    with TestClient(app) as fresh:
        return fresh.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        ).status_code


def _drop(email: str) -> None:
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.email == email))
        if user is not None:
            session.delete(user)
            session.commit()


def test_creates_an_administrator_who_can_actually_log_in(client):
    """client — чтобы база была развёрнута сидом, как в проде после миграций."""
    assert client.get("/api/v1/auth/me").status_code == 200

    with SessionLocal() as session:
        user, created = ensure_admin(session, f"  {NEW.upper()} ", " Ольга Орлова ", "runbook-1234")
    try:
        assert created
        assert user.email == NEW, "почта приводится к нижнему регистру и обрезается"
        assert user.full_name == "Ольга Орлова"
        assert user.role is Role.ADMIN and user.is_active
        assert verify_password("runbook-1234", user.password_hash)
        assert _login(NEW, "runbook-1234") == 200
        assert _login(NEW, "runbook-12345") == 401
    finally:
        _drop(NEW)

    assert _login(NEW, "runbook-1234") == 401, "запись убрана — входа нет"


def test_second_run_updates_the_same_person(client):
    with SessionLocal() as session:
        first, created_first = ensure_admin(session, NEW, "Ольга Орлова", "runbook-1234")
        second, created_second = ensure_admin(session, NEW, "Ольга Орлова", "runbook-5678")
    try:
        assert created_first and not created_second
        assert first.id == second.id, "вторая запись не заводится"
        assert _login(NEW, "runbook-5678") == 200
        assert _login(NEW, "runbook-1234") == 401, "прежний пароль больше не подходит"
    finally:
        _drop(NEW)


def _minute_old_cookie(user_id: int, role: str) -> str:
    """Кука, выданная минуту назад.

    Своей такой не получить: вход всегда ставит ``iat`` «сейчас», а отметка
    смены пароля сравнивается с ним в целых секундах — сессия, выданная в ту же
    секунду, уцелеет намеренно (``api/deps.py``). Тест на живом входе поэтому
    зависел бы от того, успела ли между входом и правкой щёлкнуть секунда.

    Номер сессии здесь обязателен: с задачи G-20 токен без него не принимается
    вовсе, и кука гасла бы не из-за смены пароля, а из-за отсутствия номера —
    то есть тест проходил бы, ничего не проверяя.
    """
    moment = datetime.now(UTC) - timedelta(minutes=1)
    return jwt.encode(
        {
            "sub": str(user_id),
            "role": role,
            "jti": new_session_id(),
            "iat": moment,
            "exp": moment + timedelta(hours=12),
        },
        get_settings().secret_key,
        algorithm="HS256",
    )


def test_restoring_access_closes_the_old_sessions(client):
    """Наблюдателю возвращают права администратора — и его прежняя кука гаснет."""
    from app.main import app

    with SessionLocal() as session:
        before = session.scalar(select(User).where(User.email == VIEWER[0]))
        assert before is not None
        kept = (before.role, before.is_active, before.password_hash, before.password_changed_at)
        cookie = _minute_old_cookie(before.id, before.role.value)
        # Пароль последний раз меняли два часа назад — иначе минутная кука была бы
        # выдана раньше последней смены и отвергалась бы и без восстановления
        # доступа: сид заводит людей прямо перед тестами.
        before.password_changed_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2)
        session.commit()

    try:
        with TestClient(app) as old_session:
            old_session.cookies.set(SESSION_COOKIE, cookie)
            assert old_session.get("/api/v1/auth/me").status_code == 200, "кука рабочая"

            with SessionLocal() as session:
                user, created = ensure_admin(
                    session, VIEWER[0], "Пётр, руководитель", "runbook-9012"
                )

            assert not created
            assert user.role is Role.ADMIN and user.is_active
            assert old_session.get("/api/v1/auth/me").status_code == 401, "прежняя сессия закрыта"
        assert _login(VIEWER[0], "runbook-9012") == 200
    finally:
        with SessionLocal() as session:
            back = session.scalar(select(User).where(User.email == VIEWER[0]))
            assert back is not None
            back.role, back.is_active, back.password_hash, back.password_changed_at = kept
            session.commit()

    # Прежний пароль снова рабочий: остальные тесты входят наблюдателем как обычно.
    assert _login(*VIEWER[:2]) == 200


def test_password_comes_from_the_environment_or_is_generated(monkeypatch):
    monkeypatch.setenv(PASSWORD_ENV, " задан-снаружи-1234 ")
    given, show = resolve_password()
    assert given == "задан-снаружи-1234"
    assert not show, "переданный пароль печатать некому и некуда"

    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    generated, show = resolve_password()
    assert show and len(generated) >= 12
    assert generated != resolve_password()[0], "пароль не постоянный"


def test_short_password_from_the_environment_is_refused(monkeypatch):
    monkeypatch.setenv(PASSWORD_ENV, "коротк")

    with pytest.raises(SystemExit) as refusal:
        resolve_password()

    assert PASSWORD_ENV in str(refusal.value)


def test_command_checks_the_schema_and_does_not_print_a_given_password(client, monkeypatch, capsys):
    """Проверка схемы — та же, что у приложения: на устаревшей базе команда не пишет."""
    monkeypatch.setenv(PASSWORD_ENV, "runbook-3456")
    try:
        main(NEW, "Ольга Орлова")
        out = capsys.readouterr().out

        assert NEW in out
        assert "runbook-3456" not in out, "заданный пароль в вывод не попадает"
        assert "Создан" in out
        with SessionLocal() as session:
            assert active_admins(session) >= 2, "в сиде уже есть администратор, стало больше"
        assert _login(NEW, "runbook-3456") == 200
    finally:
        _drop(NEW)


def test_bad_email_is_refused(client):
    with SessionLocal() as session:
        with pytest.raises(SystemExit):
            ensure_admin(session, "  ", "Никто", "runbook-1234")
        with pytest.raises(SystemExit):
            ensure_admin(session, "без-собаки", "Никто", "runbook-1234")
        assert session.scalar(select(User).where(User.email == "без-собаки")) is None
