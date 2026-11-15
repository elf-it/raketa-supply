"""Зависимости доступа.

Роли упорядочены: ``viewer`` → ``editor`` → ``admin``, каждая включает предыдущую.
Чтение доступно всем вошедшим, изменение данных — с ``editor``, управление
пользователями — только ``admin``.
"""

from __future__ import annotations

from datetime import UTC

from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import ROLE_ORDER, Role, User
from ..observability import set_actor
from ..security import SESSION_COOKIE, decode_token

UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Нужно войти в систему"
)


def current_user(
    request: Request,
    session_cookie: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    session: Session = Depends(get_session),
) -> User:
    if not session_cookie:
        raise UNAUTHORIZED
    payload = decode_token(session_cookie)
    if not payload:
        raise UNAUTHORIZED

    try:
        user_id = int(payload.get("sub", ""))
    except (TypeError, ValueError):
        raise UNAUTHORIZED from None

    user = session.get(User, user_id)
    # Отключённый пользователь не должен доживать на уже выданном токене.
    if user is None or not user.is_active:
        raise UNAUTHORIZED
    if _issued_before_password_change(payload, user):
        raise UNAUTHORIZED
    # Кто делает запрос — в строку лога (задача G-4). Здесь и только здесь:
    # раньше проверки доступа это неизвестно, а почта в лог не уходит.
    set_actor(user.id, user.role.value, request)
    return user


def _issued_before_password_change(payload: dict, user: User) -> bool:
    """Выдан ли токен до того, как пароль сменили.

    Смена пароля должна закрывать чужой вход сразу, а не через двенадцать часов,
    когда истечёт уже выданный токен, — иначе смена пароля не решает ровно ту
    задачу, ради которой её делают.

    ``iat`` в JWT хранится в целых секундах, поэтому и отметка сравнивается
    в целых: иначе кука, выданная ручкой смены пароля в ответ на саму смену,
    отвергала бы себя же — её ``iat`` округлён вниз, а отметка с долями секунды
    оказалась бы позже. Цена округления — чужая сессия, выданная в ту же
    секунду, что и смена, проживёт до следующего совпадения; секунда роли
    не играет, а предсказуемость — играет.
    """
    changed = user.password_changed_at
    if changed is None:
        return False
    issued = payload.get("iat")
    if not isinstance(issued, int | float):
        # Токена без ``iat`` мы не выдаём; чужой — тем более не нужен.
        return True
    moment = changed if changed.tzinfo is not None else changed.replace(tzinfo=UTC)
    return int(issued) < int(moment.timestamp())


def require_role(minimum: Role):
    """Зависимость «роль не ниже, чем»."""

    def dependency(user: User = Depends(current_user)) -> User:
        if ROLE_ORDER[user.role] < ROLE_ORDER[minimum]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Недостаточно прав: нужна роль {minimum.value} или выше",
            )
        return user

    return dependency


#: Готовые зависимости для маршрутов.
require_viewer = require_role(Role.VIEWER)
require_editor = require_role(Role.EDITOR)
require_admin = require_role(Role.ADMIN)
