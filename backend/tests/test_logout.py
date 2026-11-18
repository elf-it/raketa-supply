"""Выход закрывает сессию, а не только куку (задача G-20).

Токен подписан и самодостаточен, поэтому до этой задачи выход удалял куку
в браузере, а сам токен оставался действительным до конца своих двенадцати
часов: скопированная кука переживала выход. Здесь проверяется, что теперь
не переживает — и что при этом не закрываются чужие сессии и не ломается
выход с испорченной кукой.

Клиенты здесь свои, а не общий `client` из conftest: он живёт весь прогон,
и выход в одном тесте погасил бы сессию всем следующим.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from conftest import EDITOR
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.config import get_settings
from app.db import SessionLocal
from app.models import RevokedSession
from app.security import ALGORITHM, SESSION_COOKIE, create_token, new_session_id
from app.services.sessions import forget_expired, revoke

ME = "/api/v1/auth/me"
LOGOUT = "/api/v1/auth/logout"
MATRIX = "/api/v1/matrix"


def _fresh_client(credentials: tuple[str, str, str]) -> TestClient:
    from app.main import app

    client = TestClient(app)
    client.__enter__()
    email, password, _ = credentials
    answer = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert answer.status_code == 200, answer.text
    return client


@pytest.fixture
def logged_in(seeded_db):
    """Свой вошедший клиент; после теста убираются строки **своих** сессий.

    Чистится именно своё, а не таблица целиком: выход вызывают и другие тесты,
    и их отзывы — такие же законные строки. Тест, подчищающий за всеми, заодно
    прячет ошибку в их уборке.
    """
    clients: list[TestClient] = []
    mine: list[str] = []

    def make(credentials=EDITOR) -> TestClient:
        client = _fresh_client(credentials)
        clients.append(client)
        mine.append(_jti(_cookie(client)))
        return client

    yield make

    for client in clients:
        client.__exit__(None, None, None)
    with SessionLocal() as session:
        session.execute(delete(RevokedSession).where(RevokedSession.jti.in_(mine or ["-"])))
        session.commit()


def _jti(token: str) -> str:
    return jwt.decode(token, get_settings().secret_key, [ALGORITHM])["jti"]


def _revoked(jti: str) -> bool:
    with SessionLocal() as session:
        return session.get(RevokedSession, jti) is not None


def _rows() -> int:
    with SessionLocal() as session:
        return len(session.scalars(select(RevokedSession.jti)).all())


def _cookie(client: TestClient) -> str:
    value = client.cookies.get(SESSION_COOKIE)
    assert value, "вход должен был поставить куку"
    return value


def _with_cookie(client: TestClient, value: str) -> TestClient:
    """Подставляет куку клиенту.

    Именно клиенту, а не запросу: per-request cookies в TestClient объявлены
    устаревшими, и поведение при их сохранении неопределённое — тест на этом
    молча проверял бы не то.
    """
    client.cookies.set(SESSION_COOKIE, value)
    return client


# ------------------------------------------------------------------ сам выход


def test_logout_closes_the_session_not_just_the_cookie(logged_in):
    """Главное в задаче: кука, снятая до выхода, после выхода не работает."""
    client = logged_in()
    stolen = _cookie(client)
    assert client.get(ME).status_code == 200

    assert client.post(LOGOUT).status_code == 200

    # Клиент куку забыл — подставляем ту, что «увели» раньше.
    _with_cookie(client, stolen)
    assert client.get(ME).status_code == 401
    assert client.get(MATRIX).status_code == 401


def test_logout_leaves_the_other_sessions_of_the_same_person(logged_in):
    """Выход на чужом компьютере не должен закрывать вход на телефоне.

    «Закрыть все» — это смена пароля, она отзывает всё по password_changed_at.
    """
    phone = logged_in()
    shared = logged_in()
    assert phone.get(ME).status_code == 200

    assert shared.post(LOGOUT).status_code == 200

    assert phone.get(ME).status_code == 200, "закрылась не та сессия"


def test_logout_works_with_a_cookie_that_cannot_be_read(logged_in):
    """Иначе человек с истёкшей кукой получает ошибку вместо выхода."""
    client = logged_in()
    client.cookies.clear()
    before = _rows()

    # Значение намеренно из ASCII: кука другого и не бывает.
    broken = _with_cookie(client, "not-a-token-at-all").post(LOGOUT)
    client.cookies.clear()
    missing = client.post(LOGOUT)

    assert broken.status_code == 200
    assert missing.status_code == 200
    assert _rows() == before, "отзывать было нечего, а строка бы появилась"


def test_logging_out_twice_with_the_same_cookie_is_not_an_error(logged_in):
    """Две вкладки, два выхода — отзыв уже есть, и это не ошибка."""
    client = logged_in()
    stolen = _cookie(client)

    first = client.post(LOGOUT)
    before = _rows()
    second = _with_cookie(client, stolen).post(LOGOUT)

    assert (first.status_code, second.status_code) == (200, 200)
    assert _revoked(_jti(stolen)), "первый выход должен был отозвать"
    assert _rows() == before, "второй выход не должен добавлять вторую строку"


# -------------------------------------------------------------- номер сессии


def test_every_session_gets_its_own_number():
    """Без номера отзывался бы не один вход, а все входы человека разом."""
    first = jwt.decode(create_token(1, "editor"), get_settings().secret_key, [ALGORITHM])
    second = jwt.decode(create_token(1, "editor"), get_settings().secret_key, [ALGORITHM])

    assert first["jti"] != second["jti"]
    assert len(first["jti"]) == 43, "длина должна совпадать с колонкой в базе"


def test_a_token_without_a_number_is_not_accepted(logged_in):
    """Такие выдавались до G-20, и отозвать их нечем — значит выход бессилен.

    Один обязательный повторный вход при обновлении лучше, чем двенадцать
    часов сессии, которую нельзя закрыть.
    """
    client = logged_in()
    now = datetime.now(UTC)
    old_style = jwt.encode(
        {"sub": "2", "role": "editor", "iat": now, "exp": now + timedelta(hours=12)},
        get_settings().secret_key,
        algorithm=ALGORITHM,
    )

    answer = _with_cookie(client, old_style).get(ME)

    assert answer.status_code == 401


def test_a_number_someone_made_up_does_not_help(logged_in):
    """Подпись проверяется первой: свой номер в чужом токене ничего не даёт."""
    client = logged_in()
    now = datetime.now(UTC)
    forged = jwt.encode(
        {
            "sub": "2",
            "role": "admin",
            "jti": new_session_id(),
            "iat": now,
            "exp": now + timedelta(hours=12),
        },
        "не тот ключ",
        algorithm=ALGORITHM,
    )

    assert _with_cookie(client, forged).get(ME).status_code == 401


# ------------------------------------------------------------------- хранение


def test_the_revocation_is_remembered_only_until_the_token_expires(seeded_db):
    """Держать дольше бессмысленно: просроченный токен отвергает подпись."""
    now = datetime.now(UTC).replace(tzinfo=None)
    with SessionLocal() as session:
        try:
            stale = {"jti": new_session_id(), "exp": (now - timedelta(minutes=1)).timestamp()}
            alive = {"jti": new_session_id(), "exp": (now + timedelta(hours=5)).timestamp()}
            assert revoke(session, stale) is True
            assert revoke(session, alive) is True

            removed = forget_expired(session, now)

            assert removed >= 1
            assert session.get(RevokedSession, stale["jti"]) is None
            assert session.get(RevokedSession, alive["jti"]) is not None
        finally:
            session.execute(
                delete(RevokedSession).where(RevokedSession.jti.in_([stale["jti"], alive["jti"]]))
            )
            session.commit()


def test_a_token_without_an_expiry_is_not_worth_remembering(seeded_db):
    """Сверять такую строку не с чем: когда её удалять — неизвестно."""
    before = _rows()
    with SessionLocal() as session:
        assert revoke(session, {"jti": new_session_id()}) is False
        assert revoke(session, {"exp": 1}) is False
    assert _rows() == before, "строка появилась там, где сверять её не с чем"
