"""Ограничение частоты попыток входа (задача G-8)."""

from __future__ import annotations

import time

from conftest import ADMIN, EDITOR

from app.ratelimit import LoginGuard, get_login_guard

WRONG = {"password": "совершенно-не-тот-пароль"}


# ---------------------------------------------------------------------- счётчик


def test_counter_closes_the_door_after_the_limit():
    guard = LoginGuard(per_account=3, per_address=100, window_seconds=600)
    for _ in range(2):
        guard.failure("someone@raketa.local", "10.0.0.1")
        assert guard.retry_after("someone@raketa.local", "10.0.0.1") == 0

    guard.failure("someone@raketa.local", "10.0.0.1")
    wait = guard.retry_after("someone@raketa.local", "10.0.0.1")
    assert 0 < wait <= 601


def test_counter_forgets_old_attempts():
    """Окно скользит: иначе забытый пароль запирал бы человека навсегда."""
    guard = LoginGuard(per_account=1, per_address=100, window_seconds=1)
    guard.failure("someone@raketa.local", "10.0.0.1")
    assert guard.retry_after("someone@raketa.local", "10.0.0.1") > 0

    time.sleep(1.1)
    assert guard.retry_after("someone@raketa.local", "10.0.0.1") == 0


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
