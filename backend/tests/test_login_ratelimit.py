"""Ограничение частоты попыток входа (задачи G-8, G-12).

Счётчик лежит в базе, и это главное, что здесь проверяется: в памяти процесса
он был не защитой, а её видимостью — у каждого воркера своя доля лимита,
а перезапуск обнулял счётчик целиком, так что перебор достаточно было
переждать.

Вторая проверка — про адрес: за прокси его берут из заголовка, но только если
так настроено. Иначе любой клиент назвал бы себя чужим адресом и счётчик
по адресу перестал бы что-либо значить.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from conftest import ADMIN, EDITOR
from sqlalchemy import func, select
from starlette.requests import Request

from app.db import SessionLocal
from app.models import LoginAttempt
from app.ratelimit import FORWARDED_HEADER, LoginGuard, client_address, get_login_guard

WRONG = {"password": "совершенно-не-тот-пароль"}
NOW = datetime(2026, 11, 15, 10, 0, 0)


def _attempts() -> int:
    with SessionLocal() as session:
        return session.scalar(select(func.count()).select_from(LoginAttempt)) or 0


def _request(headers: dict[str, str] | None = None, host: str = "10.0.0.5") -> Request:
    return Request(
        {
            "type": "http",
            "headers": [
                (key.lower().encode(), value.encode()) for key, value in (headers or {}).items()
            ],
            "client": (host, 50000),
        }
    )


# ---------------------------------------------------------------------- счётчик


def test_counter_closes_the_door_after_the_limit():
    guard = LoginGuard(per_account=3, per_address=100, window_seconds=600)
    for _ in range(2):
        guard.failure("someone@raketa.local", "10.0.0.1")
        assert guard.retry_after("someone@raketa.local", "10.0.0.1") == 0

    guard.failure("someone@raketa.local", "10.0.0.1")
    wait = guard.retry_after("someone@raketa.local", "10.0.0.1")
    assert 0 < wait <= 601


def test_counter_is_the_same_for_every_worker():
    """Ради этого счётчик и переехал в базу: воркеров несколько, лимит один."""
    first = LoginGuard(per_account=2, per_address=100, window_seconds=600)
    second = LoginGuard(per_account=2, per_address=100, window_seconds=600)

    first.failure("someone@raketa.local", "10.0.0.1")
    second.failure("someone@raketa.local", "10.0.0.1")

    # Третий «воркер» поднялся только что и о прошлых попытках знает всё равно.
    third = LoginGuard(per_account=2, per_address=100, window_seconds=600)
    assert third.retry_after("someone@raketa.local", "10.0.0.1") > 0


def test_counter_forgets_old_attempts():
    """Окно скользит: иначе забытый пароль запирал бы человека навсегда."""
    guard = LoginGuard(per_account=1, per_address=100, window_seconds=600)
    guard.failure("someone@raketa.local", "10.0.0.1", now=NOW)

    assert guard.retry_after("someone@raketa.local", "10.0.0.1", now=NOW) > 0
    later = NOW + timedelta(seconds=601)
    assert guard.retry_after("someone@raketa.local", "10.0.0.1", now=later) == 0


def test_old_attempts_do_not_pile_up():
    """Это счётчик, а не журнал: хранить историю чужих опечаток незачем."""
    guard = LoginGuard(per_account=5, per_address=100, window_seconds=600)
    guard.failure("someone@raketa.local", "10.0.0.1", now=NOW)
    guard.failure("other@raketa.local", "10.0.0.2", now=NOW)
    assert _attempts() == 2

    guard.failure("someone@raketa.local", "10.0.0.1", now=NOW + timedelta(seconds=601))

    assert _attempts() == 1, "старые записи убраны следующей же попыткой"


def test_successful_login_clears_the_count():
    """Вспомнил пароль с третьей попытки — не повод запирать на четверть часа."""
    guard = LoginGuard(per_account=3, per_address=100, window_seconds=600)
    guard.failure("someone@raketa.local", "10.0.0.1")
    guard.failure("someone@raketa.local", "10.0.0.1")
    guard.success("someone@raketa.local", "10.0.0.1")

    guard.failure("someone@raketa.local", "10.0.0.1")
    guard.failure("someone@raketa.local", "10.0.0.1")
    assert guard.retry_after("someone@raketa.local", "10.0.0.1") == 0


def test_counting_is_per_address_so_nobody_locks_a_colleague_out():
    """Счётчик по одной почте был бы способом заблокировать коллегу."""
    guard = LoginGuard(per_account=2, per_address=100, window_seconds=600)
    for _ in range(5):
        guard.failure("victim@raketa.local", "10.0.0.66")

    assert guard.retry_after("victim@raketa.local", "10.0.0.66") > 0
    assert guard.retry_after("victim@raketa.local", "10.0.0.1") == 0


def test_address_counter_catches_spraying_across_accounts():
    """Перебирают не пароль, а почту: по паре попыток на сотню записей."""
    guard = LoginGuard(per_account=10, per_address=3, window_seconds=600)
    for number in range(3):
        guard.failure(f"user{number}@raketa.local", "10.0.0.7")

    assert guard.retry_after("someone-else@raketa.local", "10.0.0.7") > 0
    assert guard.retry_after("someone-else@raketa.local", "10.0.0.8") == 0


def test_password_is_never_written_anywhere():
    """В таблице попыток — только почта, адрес и время."""
    guard = LoginGuard(per_account=5, per_address=100, window_seconds=600)
    guard.failure("someone@raketa.local", "10.0.0.1")

    with SessionLocal() as session:
        row = session.scalars(select(LoginAttempt)).first()
    assert row is not None
    assert {column.name for column in LoginAttempt.__table__.columns} == {
        "id",
        "email",
        "address",
        "happened_at",
    }
    assert row.email == "someone@raketa.local"


# ---------------------------------------------------------------------- адрес


def test_address_comes_from_the_connection_when_there_is_no_proxy():
    """Без настройки заголовок не читается: его подставляет кто угодно."""
    spoofed = _request({FORWARDED_HEADER: "1.2.3.4"}, host="10.0.0.5")

    assert client_address(spoofed, hops=0) == "10.0.0.5"


def test_address_behind_one_proxy_is_taken_from_the_header():
    forwarded = _request({FORWARDED_HEADER: "203.0.113.9"}, host="127.0.0.1")

    assert client_address(forwarded, hops=1) == "203.0.113.9"


def test_client_cannot_prepend_a_fake_address():
    """Клиент приписывает своё слева; читается то, что заполнил наш прокси."""
    spoofed = _request({FORWARDED_HEADER: "9.9.9.9, 203.0.113.9"}, host="127.0.0.1")

    assert client_address(spoofed, hops=1) == "203.0.113.9"
    assert client_address(spoofed, hops=2) == "9.9.9.9", "два прокси — и адрес левее"


def test_short_chain_falls_back_to_the_connection():
    """Запрос пришёл не через ожидаемую цепочку — значит, заголовку веры нет."""
    direct = _request({}, host="127.0.0.1")
    short = _request({FORWARDED_HEADER: "203.0.113.9"}, host="127.0.0.1")

    assert client_address(direct, hops=1) == "127.0.0.1"
    assert client_address(short, hops=2) == "127.0.0.1"


# -------------------------------------------------------------------------- вход


def test_login_closes_after_too_many_failures(anon_client, monkeypatch):
    guard = get_login_guard()
    monkeypatch.setattr(guard, "per_account", 3)

    for _ in range(3):
        response = anon_client.post("/api/v1/auth/login", json={"email": EDITOR[0], **WRONG})
        assert response.status_code == 401

    # Дальше не пускают даже с верным паролем: иначе лимит обходился бы
    # очередной попыткой, а перебор ровно из них и состоит.
    locked = anon_client.post(
        "/api/v1/auth/login", json={"email": EDITOR[0], "password": EDITOR[1]}
    )
    assert locked.status_code == 429
    assert "Retry-After" in locked.headers
    assert int(locked.headers["Retry-After"]) > 0
    assert "попыток" in locked.json()["detail"]

    # Чужая учётная запись при этом открыта: заперта пара «почта + адрес».
    other = anon_client.post("/api/v1/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})
    assert other.status_code == 200
    anon_client.post("/api/v1/auth/logout")
    anon_client.cookies.clear()


def test_login_opens_again_after_a_successful_attempt(anon_client, monkeypatch):
    guard = get_login_guard()
    monkeypatch.setattr(guard, "per_account", 3)

    for _ in range(2):
        anon_client.post("/api/v1/auth/login", json={"email": EDITOR[0], **WRONG})

    assert (
        anon_client.post(
            "/api/v1/auth/login", json={"email": EDITOR[0], "password": EDITOR[1]}
        ).status_code
        == 200
    )

    for _ in range(2):
        assert (
            anon_client.post("/api/v1/auth/login", json={"email": EDITOR[0], **WRONG}).status_code
            == 401
        )
    anon_client.post("/api/v1/auth/logout")
    anon_client.cookies.clear()


def test_failed_attempts_outlive_the_process(anon_client, monkeypatch):
    """Перезапуск больше не обнуляет счётчик: он не в памяти.

    Проверка идёт от входа, а не от объекта: именно так лимит обходился раньше —
    процесс перезапускали, и перебор продолжался с чистого листа.
    """
    guard = get_login_guard()
    monkeypatch.setattr(guard, "per_account", 2)

    for _ in range(2):
        anon_client.post("/api/v1/auth/login", json={"email": EDITOR[0], **WRONG})

    # «Перезапуск»: счётчик собран заново, памяти прошлого у него нет.
    restarted = LoginGuard(per_account=2, per_address=60, window_seconds=900)
    assert restarted.retry_after(EDITOR[0], "testclient") > 0
