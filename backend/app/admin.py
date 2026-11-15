"""Первый администратор на чистой базе (задача G-5).

Учётные записи заводит администратор на экране пользователей — но в пустой базе
администратора нет, и войти некем. Демонстрационный сид для этого не годится:
он принесёт вместе с людьми выдуманный каталог и выдуманные продажи, а в проде
нужна только живая номенклатура заказчика.

    python -m app.admin anna@example.com "Анна Петрова"

Этой же командой возвращают доступ, когда единственный администратор потерял
пароль: отдать его некому — ручка «задать новый пароль» сама требует прав
администратора. Команда возвращает роль, включает запись и задаёт пароль
заново, закрывая прежние сессии этого человека.

Пароль не берётся из аргумента командной строки: он остался бы в истории
оболочки и в списке процессов, который виден всем на машине. Либо команда
генерирует его и печатает один раз, либо берёт из переменной окружения
``RAKETA_ADMIN_PASSWORD`` — тогда она не печатает его вовсе.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from os import environ

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import SessionLocal, engine
from .migrations import assert_current
from .models import Role, User
from .security import hash_password, password_changed_now

PASSWORD_ENV = "RAKETA_ADMIN_PASSWORD"
#: Та же нижняя граница, что у ручек смены пароля (``schemas.py``).
MIN_PASSWORD = 8


def resolve_password() -> tuple[str, bool]:
    """Пароль и признак «его придётся показать человеку».

    Заданный через окружение пароль не печатается: он и так известен тому, кто
    запускает команду, а вывод уходит в журналы развёртывания.
    """
    given = environ.get(PASSWORD_ENV, "").strip()
    if given:
        if len(given) < MIN_PASSWORD:
            raise SystemExit(
                f"{PASSWORD_ENV}: пароль короче {MIN_PASSWORD} символов — так же, "
                "как отказала бы форма смены пароля"
            )
        return given, False
    return secrets.token_urlsafe(12), True


def ensure_admin(session: Session, email: str, full_name: str, password: str) -> tuple[User, bool]:
    """Заводит администратора или возвращает права уже существующему человеку.

    Возвращает запись и признак «создана заново». Пароль задаётся всегда:
    команду запускают либо на пустой базе, либо когда входа нет.
    """
    email = email.strip().lower()
    name = full_name.strip()
    if not email or "@" not in email:
        raise SystemExit(f"Непохоже на почту: {email!r}")

    user = session.scalar(select(User).where(func.lower(User.email) == email))
    created = user is None
    if user is None:
        user = User(
            email=email,
            full_name=name or email,
            role=Role.ADMIN,
            password_hash=hash_password(password),
        )
        session.add(user)
    else:
        user.role = Role.ADMIN
        user.is_active = True
        if name:
            user.full_name = name
        user.password_hash = hash_password(password)
    # Отметка закрывает сессии, выданные раньше: если доступ восстанавливают
    # после утечки пароля, чужая кука не должна дожить до конца своих часов.
    user.password_changed_at = password_changed_now()
    session.commit()
    return user, created


def active_admins(session: Session) -> int:
    return (
        session.scalar(
            select(func.count(User.id)).where(User.role == Role.ADMIN, User.is_active.is_(True))
        )
        or 0
    )


def main(email: str, full_name: str) -> None:
    # Схему команда не создаёт и не правит — как и приложение. Иначе первый
    # администратор заводился бы в базе, где миграции ещё не применяли.
    assert_current(engine)
    password, show = resolve_password()

    with SessionLocal() as session:
        user, created = ensure_admin(session, email, full_name, password)
        admins = active_admins(session)

    what = "Создан" if created else "Обновлён"
    print(f"{what} администратор {user.email} ({user.display_name}).")
    if not created:
        print("Роль администратора возвращена, запись включена, прежние сессии закрыты.")
    if show:
        print(f"Пароль: {password}")
        print(
            "Он показан один раз и остался в истории терминала — "
            "смените его после первого входа."
        )
    print(f"Активных администраторов в базе: {admins}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Завести администратора или вернуть ему доступ",
        epilog=f"Пароль: из переменной {PASSWORD_ENV}, иначе генерируется и печатается один раз.",
    )
    parser.add_argument("email", help="почта для входа")
    parser.add_argument("full_name", help="имя, которым подписываются правки")
    args = parser.parse_args()
    try:
        main(args.email, args.full_name)
    except (SystemExit, RuntimeError) as error:
        # Текст проверки схемы и отказов — человеку в stderr, а не трассировкой.
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
