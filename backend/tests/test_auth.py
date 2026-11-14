"""Авторизация и роли (задача G-1)."""

from __future__ import annotations

import time
from contextlib import contextmanager

import pytest
from conftest import ADMIN, EDITOR, VIEWER

from app.security import hash_password, verify_password

#: Ручки, которые меняют данные и требуют роль не ниже редактора.
WRITE_ENDPOINTS = [
    (
        "post",
        "/api/v1/forecast/bulk",
        {"pair_ids": [1], "weeks": ["2026-W50"], "operation": "clear"},
    ),
    ("put", "/api/v1/forecast/1/2026-W50", {"manual_forecast": 10}),
    ("post", "/api/v1/forecast/close-week", {"week": "2026-W46"}),
    ("put", "/api/v1/reference/base-potential/1", {"base_potential": 10}),
    ("put", "/api/v1/reference/seasonality/1", {"coefficients": {"5": 1.0}}),
    ("post", "/api/v1/sync/wb", None),
]

READ_ENDPOINTS = [
    "/api/v1/matrix?level=sku",
    "/api/v1/catalog/stores",
    "/api/v1/purchase",
    "/api/v1/forecast/log",
    "/api/v1/reference/base-potential",
]


# ------------------------------------------------------------------------ пароли


def test_password_hash_is_not_the_password():
    stored = hash_password("правильный-пароль")
    assert "правильный-пароль" not in stored
    assert stored.startswith("$2b$")
    assert verify_password("правильный-пароль", stored)
    assert not verify_password("другой-пароль", stored)


def test_long_passwords_are_not_truncated():
    """bcrypt сам обрезает вход до 72 байт — из-за этого длинные пароли совпадали бы."""
    base = "к" * 100
    assert len(base.encode()) > 72
    stored = hash_password(base)
    assert verify_password(base, stored)
    assert not verify_password(base + "хвост", stored)


def test_broken_hash_does_not_crash_login():
    assert verify_password("что угодно", "не-хеш-вовсе") is False
    assert verify_password("что угодно", "") is False


# -------------------------------------------------------------------------- вход


def test_login_and_me(client):
    me = client.get("/api/v1/auth/me").json()
    assert me["email"] == EDITOR[0]
    assert me["role"] == "editor"


def test_login_rejects_wrong_password(anon_client):
    response = anon_client.post(
        "/api/v1/auth/login", json={"email": EDITOR[0], "password": "не тот пароль"}
    )
    assert response.status_code == 401


def test_login_answer_does_not_reveal_who_exists(anon_client):
    """Иначе форма входа превращается в способ узнать, кто заведён в системе."""
    unknown = anon_client.post(
        "/api/v1/auth/login", json={"email": "ghost@raketa.local", "password": "x" * 12}
    )
    wrong = anon_client.post("/api/v1/auth/login", json={"email": EDITOR[0], "password": "x" * 12})
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


def test_logout_clears_the_session(admin_client):
    assert admin_client.get("/api/v1/auth/me").status_code == 200
    admin_client.post("/api/v1/auth/logout")
    assert admin_client.get("/api/v1/auth/me").status_code == 401
    # Возвращаем сессию, чтобы не ломать другие тесты.
    admin_client.post("/api/v1/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})
    assert admin_client.get("/api/v1/auth/me").status_code == 200


def test_session_cookie_is_http_only(anon_client):
    """Из localStorage токен достаётся любым скриптом на странице, из httpOnly-куки — нет."""
    response = anon_client.post(
        "/api/v1/auth/login", json={"email": VIEWER[0], "password": VIEWER[1]}
    )
    assert response.status_code == 200
    header = response.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=lax" in header
    anon_client.post("/api/v1/auth/logout")


def test_garbage_token_is_rejected(anon_client):
    anon_client.cookies.set("raketa_session", "eyJhbGciOiJIUzI1NiJ9.cG9ka.ZGVsa2E")
    assert anon_client.get("/api/v1/auth/me").status_code == 401
    anon_client.cookies.clear()


# -------------------------------------------------------------------- права доступа


@pytest.mark.parametrize("path", READ_ENDPOINTS)
def test_reading_requires_login(anon_client, path):
    assert anon_client.get(path).status_code == 401


@pytest.mark.parametrize("method, path, payload", WRITE_ENDPOINTS)
def test_writing_requires_login(anon_client, method, path, payload):
    response = (
        getattr(anon_client, method)(path, json=payload)
        if payload
        else getattr(anon_client, method)(path)
    )
    assert response.status_code == 401


@pytest.mark.parametrize("path", READ_ENDPOINTS)
def test_viewer_can_read(viewer_client, path):
    assert viewer_client.get(path).status_code == 200


@pytest.mark.parametrize("method, path, payload", WRITE_ENDPOINTS)
def test_viewer_cannot_write(viewer_client, method, path, payload):
    response = (
        getattr(viewer_client, method)(path, json=payload)
        if payload
        else getattr(viewer_client, method)(path)
    )
    assert response.status_code == 403
    assert "прав" in response.json()["detail"]


def test_editor_cannot_manage_users(client):
    assert client.get("/api/v1/auth/users").status_code == 403
    response = client.post(
        "/api/v1/auth/users",
        json={
            "email": "new@raketa.local",
            "full_name": "Кто-то",
            "role": "viewer",
            "password": "достаточно-длинный",
        },
    )
    assert response.status_code == 403


def test_health_stays_open(anon_client):
    assert anon_client.get("/api/v1/health").status_code == 200


# ---------------------------------------------------------- управление пользователями


def test_admin_creates_and_updates_users(admin_client):
    created = admin_client.post(
        "/api/v1/auth/users",
        json={
            "email": "Novyi@Raketa.local",
            "full_name": "Новый сотрудник",
            "role": "viewer",
            "password": "достаточно-длинный-пароль",
        },
    )
    assert created.status_code == 201
    user = created.json()
    assert user["email"] == "novyi@raketa.local"  # почта нормализуется
    assert user["role"] == "viewer"

    duplicate = admin_client.post(
        "/api/v1/auth/users",
        json={
            "email": "novyi@raketa.local",
            "full_name": "Он же",
            "role": "viewer",
            "password": "достаточно-длинный-пароль",
        },
    )
    assert duplicate.status_code == 409

    updated = admin_client.put(
        f"/api/v1/auth/users/{user['id']}", json={"role": "editor", "is_active": False}
    ).json()
    assert updated["role"] == "editor"
    assert updated["is_active"] is False


def test_short_password_is_rejected(admin_client):
    response = admin_client.post(
        "/api/v1/auth/users",
        json={
            "email": "weak@raketa.local",
            "full_name": "Слабый пароль",
            "role": "viewer",
            "password": "1234",
        },
    )
    assert response.status_code == 422


def test_disabled_user_cannot_log_in(admin_client, anon_client):
    created = admin_client.post(
        "/api/v1/auth/users",
        json={
            "email": "blocked@raketa.local",
            "full_name": "Отключённый",
            "role": "editor",
            "password": "достаточно-длинный-пароль",
        },
    ).json()
    admin_client.put(f"/api/v1/auth/users/{created['id']}", json={"is_active": False})

    response = anon_client.post(
        "/api/v1/auth/login",
        json={"email": "blocked@raketa.local", "password": "достаточно-длинный-пароль"},
    )
    assert response.status_code == 401


def test_last_admin_cannot_be_demoted(admin_client):
    """Иначе системой некому управлять и это не откатить из интерфейса."""
    users = admin_client.get("/api/v1/auth/users").json()
    admins = [u for u in users if u["role"] == "admin" and u["is_active"]]
    assert len(admins) == 1

    response = admin_client.put(f"/api/v1/auth/users/{admins[0]['id']}", json={"role": "viewer"})
    assert response.status_code == 409
    assert "последний" in response.json()["detail"]


def test_admin_cannot_disable_self(admin_client):
    me = admin_client.get("/api/v1/auth/me").json()
    response = admin_client.put(f"/api/v1/auth/users/{me['id']}", json={"is_active": False})
    assert response.status_code == 409


def test_admin_can_be_demoted_while_another_remains(admin_client):
    """Снять права администратора можно — до той границы, где он остаётся один.

    Понижение себя сервер тоже разрешает: пока администраторов двое, это
    не запирание, а решение человека, и роль вернёт коллега. Экран перед таким
    понижением переспрашивает.
    """
    second = admin_client.post(
        "/api/v1/auth/users",
        json={
            "email": "second-admin@raketa.local",
            "full_name": "Второй администратор",
            "role": "admin",
            "password": "достаточно-длинный-пароль",
        },
    ).json()

    stepped_down = admin_client.put(f"/api/v1/auth/users/{second['id']}", json={"role": "editor"})
    assert stepped_down.status_code == 200
    assert stepped_down.json()["role"] == "editor"


def test_admin_renames_a_user(admin_client):
    """Имя автора правки берётся из учётной записи, поэтому его правят здесь."""
    created = admin_client.post(
        "/api/v1/auth/users",
        json={
            "email": "renamed@raketa.local",
            "full_name": "Было",
            "role": "viewer",
            "password": "достаточно-длинный-пароль",
        },
    ).json()

    updated = admin_client.put(
        f"/api/v1/auth/users/{created['id']}", json={"full_name": "  Стало, снабжение  "}
    ).json()
    assert updated["full_name"] == "Стало, снабжение"

    listed = admin_client.get("/api/v1/auth/users").json()
    assert any(row["full_name"] == "Стало, снабжение" for row in listed)


def test_users_say_when_they_were_created(admin_client):
    """Экран показывает дату заведения: по ней видно, кто появился недавно."""
    me = admin_client.get("/api/v1/auth/me").json()
    assert me["created_at"]
    assert all(row["created_at"] for row in admin_client.get("/api/v1/auth/users").json())


def test_new_password_replaces_the_old_one(admin_client, anon_client):
    """Пароль не восстанавливают, а задают заново: это и есть «забыл пароль»."""
    created = admin_client.post(
        "/api/v1/auth/users",
        json={
            "email": "forgetful@raketa.local",
            "full_name": "Забыл пароль",
            "role": "editor",
            "password": "первый-длинный-пароль",
        },
    ).json()

    admin_client.put(
        f"/api/v1/auth/users/{created['id']}", json={"password": "второй-длинный-пароль"}
    )

    old = anon_client.post(
        "/api/v1/auth/login",
        json={"email": "forgetful@raketa.local", "password": "первый-длинный-пароль"},
    )
    assert old.status_code == 401

    new = anon_client.post(
        "/api/v1/auth/login",
        json={"email": "forgetful@raketa.local", "password": "второй-длинный-пароль"},
    )
    assert new.status_code == 200
    anon_client.post("/api/v1/auth/logout")
    anon_client.cookies.clear()


def test_disabling_cuts_the_session_already_issued(admin_client):
    """Отключение действует сразу, а не когда истечёт выданная кука."""
    from fastapi.testclient import TestClient

    from app.main import app

    created = admin_client.post(
        "/api/v1/auth/users",
        json={
            "email": "fired@raketa.local",
            "full_name": "Уволенный",
            "role": "editor",
            "password": "достаточно-длинный-пароль",
        },
    ).json()

    with TestClient(app) as theirs:
        login = theirs.post(
            "/api/v1/auth/login",
            json={"email": "fired@raketa.local", "password": "достаточно-длинный-пароль"},
        )
        assert login.status_code == 200
        assert theirs.get("/api/v1/matrix?level=sku").status_code == 200

        admin_client.put(f"/api/v1/auth/users/{created['id']}", json={"is_active": False})

        assert theirs.get("/api/v1/auth/me").status_code == 401
        assert theirs.get("/api/v1/matrix?level=sku").status_code == 401


# ------------------------------------------------------------- смена своего пароля


@contextmanager
def signed_in(email: str, password: str):
    """Отдельный клиент со своей сессией — как второй браузер."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert response.status_code == 200, response.text
        yield client


def test_password_change_requires_login(anon_client):
    response = anon_client.post(
        "/api/v1/auth/me/password",
        json={"current_password": "x" * 12, "new_password": "y" * 12},
    )
    assert response.status_code == 401


def test_person_changes_own_password(admin_client, anon_client):
    """Человеку не нужен администратор, чтобы сменить себе пароль."""
    admin_client.post(
        "/api/v1/auth/users",
        json={
            "email": "selfserve@raketa.local",
            "full_name": "Сам себе",
            "role": "viewer",
            "password": "первый-длинный-пароль",
        },
    )

    with signed_in("selfserve@raketa.local", "первый-длинный-пароль") as theirs:
        changed = theirs.post(
            "/api/v1/auth/me/password",
            json={
                "current_password": "первый-длинный-пароль",
                "new_password": "второй-длинный-пароль",
            },
        )
        assert changed.status_code == 200
        # Своя сессия переживает смену: иначе человека выбрасывало бы на вход.
        assert theirs.get("/api/v1/auth/me").status_code == 200

    old = anon_client.post(
        "/api/v1/auth/login",
        json={"email": "selfserve@raketa.local", "password": "первый-длинный-пароль"},
    )
    assert old.status_code == 401

    new = anon_client.post(
        "/api/v1/auth/login",
        json={"email": "selfserve@raketa.local", "password": "второй-длинный-пароль"},
    )
    assert new.status_code == 200
    anon_client.post("/api/v1/auth/logout")
    anon_client.cookies.clear()


def test_password_change_needs_the_current_password(admin_client):
    """Уведённой куки не должно хватать, чтобы забрать учётную запись насовсем."""
    admin_client.post(
        "/api/v1/auth/users",
        json={
            "email": "stolen@raketa.local",
            "full_name": "Чужая сессия",
            "role": "viewer",
            "password": "настоящий-длинный-пароль",
        },
    )

    with signed_in("stolen@raketa.local", "настоящий-длинный-пароль") as theirs:
        response = theirs.post(
            "/api/v1/auth/me/password",
            json={"current_password": "мимо-пароль", "new_password": "новый-длинный-пароль"},
        )
        # Не 401: интерфейс понял бы его как «сессия кончилась» и выбросил
        # человека на экран входа вместе с формой, которую он заполнял.
        assert response.status_code == 409
        assert "Текущий пароль" in response.json()["detail"]
        assert theirs.get("/api/v1/auth/me").status_code == 200

        same = theirs.post(
            "/api/v1/auth/me/password",
            json={
                "current_password": "настоящий-длинный-пароль",
                "new_password": "настоящий-длинный-пароль",
            },
        )
        assert same.status_code == 409

        short = theirs.post(
            "/api/v1/auth/me/password",
            json={"current_password": "настоящий-длинный-пароль", "new_password": "1234"},
        )
        assert short.status_code == 422


def test_password_change_cuts_the_other_sessions(admin_client):
    """Ради этого пароль и меняют: открытый где-то ещё вход должен закрыться."""
    admin_client.post(
        "/api/v1/auth/users",
        json={
            "email": "two-browsers@raketa.local",
            "full_name": "Два браузера",
            "role": "viewer",
            "password": "первый-длинный-пароль",
        },
    )

    with (
        signed_in("two-browsers@raketa.local", "первый-длинный-пароль") as elsewhere,
        signed_in("two-browsers@raketa.local", "первый-длинный-пароль") as here,
    ):
        assert elsewhere.get("/api/v1/auth/me").status_code == 200
        # ``iat`` округлён до секунды, поэтому отметка смены должна лечь
        # в следующую: иначе исход теста зависел бы от скорости bcrypt.
        time.sleep(1.1)

        changed = here.post(
            "/api/v1/auth/me/password",
            json={
                "current_password": "первый-длинный-пароль",
                "new_password": "второй-длинный-пароль",
            },
        )
        assert changed.status_code == 200

        assert here.get("/api/v1/auth/me").status_code == 200
        assert elsewhere.get("/api/v1/auth/me").status_code == 401
        assert elsewhere.get("/api/v1/matrix?level=sku").status_code == 401


def test_admin_keeps_working_after_changing_own_password(admin_client):
    """Администратор может задать пароль и себе — экран пользователей это умеет."""
    me = admin_client.get("/api/v1/auth/me").json()
    assert (
        admin_client.put(
            f"/api/v1/auth/users/{me['id']}", json={"password": "временный-длинный-пароль"}
        ).status_code
        == 200
    )
    # Своя сессия продолжается: ручка выдала новую куку взамен отсечённой.
    assert admin_client.get("/api/v1/auth/me").status_code == 200

    admin_client.put(f"/api/v1/auth/users/{me['id']}", json={"password": ADMIN[1]})
    assert admin_client.get("/api/v1/auth/me").status_code == 200


# ------------------------------------------------------------------------ журнал


def test_journal_author_comes_from_the_session(client):
    """Автор, который подставляет клиент, ничего не доказывает."""
    payload = client.get("/api/v1/matrix?level=sku").json()
    pair_id = payload["rows"][0]["meta"]["pair_id"]
    week = next(w["key"] for w in payload["weeks"] if not w["is_closed"])

    response = client.put(
        f"/api/v1/forecast/{pair_id}/{week}",
        json={"manual_forecast": 555, "author": "директор"},
    )
    assert response.status_code == 200

    log = client.get(f"/api/v1/forecast/log?pair_id={pair_id}&limit=5").json()
    latest = log["entries"][0]
    assert latest["author"] == EDITOR[2]
    assert latest["author"] != "директор"

    client.put(f"/api/v1/forecast/{pair_id}/{week}", json={"clear": ["manual_forecast"]})
