"""Вход, выход и управление пользователями."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_session
from ..models import ROLE_ORDER, Role, User
from ..observability import event, mask_email
from ..ratelimit import client_address, get_login_guard
from ..schemas import LoginIn, PasswordChangeIn, UserCreateIn, UserOut, UserUpdateIn
from ..security import (
    SESSION_COOKIE,
    create_token,
    decode_token,
    hash_password,
    password_changed_now,
    verify_password,
)
from ..services.sessions import revoke
from .deps import current_user, require_admin

router = APIRouter(prefix="/auth", tags=["Доступ"])

BAD_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверная почта или пароль"
)


def _set_session_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        max_age=settings.session_ttl_hours * 3600,
        path="/",
    )


def _log_failure(email: str, address: str, reason: str) -> None:
    """Неудачный вход в лог: по нему виден перебор.

    Почта маскируется до первой буквы (``app/observability.py``): для разбора
    перебора этого достаточно, а лог читают шире, чем базу. Пароль, даже
    неверный, не пишется никогда — в нём бывает чужой верный.
    """
    event(
        "неудачный вход",
        level=logging.WARNING,
        email=mask_email(email),
        address=address,
        reason=reason,
    )


def _too_many_attempts(seconds: int) -> HTTPException:
    minutes = max(1, -(-seconds // 60))
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=f"Слишком много неудачных попыток входа. Попробуйте через {minutes} мин.",
        headers={"Retry-After": str(seconds)},
    )


@router.post("/login", response_model=UserOut, summary="Вход")
def login(
    payload: LoginIn,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
) -> UserOut:
    """Выдаёт сессионную куку.

    Ответ одинаков и для несуществующей почты, и для неверного пароля,
    и для отключённого пользователя: иначе форма входа превращается
    в способ узнать, кто заведён в системе.

    Неудачные попытки считаются (``app/ratelimit.py``): после лимита вход
    закрывается на окно времени и отвечает 429. Счётчик проверяется до сверки
    пароля — перебор не должен даже доходить до bcrypt.
    """
    email = payload.email.strip().lower()
    address = client_address(request)
    guard = get_login_guard()
    wait = guard.retry_after(email, address)
    if wait:
        raise _too_many_attempts(wait)

    user = session.scalar(select(User).where(func.lower(User.email) == email))
    if user is None or not user.is_active:
        # Пароль всё равно проверяем, чтобы ответ не был заметно быстрее.
        verify_password(payload.password, "$2b$12$" + "." * 53)
        guard.failure(email, address)
        _log_failure(email, address, "нет такой записи или она отключена")
        raise BAD_CREDENTIALS
    if not verify_password(payload.password, user.password_hash):
        guard.failure(email, address)
        _log_failure(email, address, "пароль не подошёл")
        raise BAD_CREDENTIALS

    guard.success(email, address)
    event("вход", user=user.id, role=user.role.value)
    _set_session_cookie(response, create_token(user.id, user.role.value))
    return UserOut.model_validate(user)


@router.post("/logout", summary="Выход")
def logout(
    response: Response,
    session_cookie: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    session: Session = Depends(get_session),
) -> dict:
    """Закрывает сессию, а не только куку (задача G-20).

    Прав здесь намеренно не требуется: выход должен срабатывать и с истёкшей,
    и с испорченной кукой — иначе человек остаётся с нерабочей сессией
    и ошибкой вместо выхода. Удаление куки происходит всегда, отзыв — когда
    токен разбирается и в нём есть номер.

    Подделать выход со стороннего сайта нельзя: кука стоит с ``SameSite=Lax``,
    и межсайтовый POST её не принесёт. Да и ущерб от такой подделки — лишний
    повторный вход.
    """
    response.delete_cookie(SESSION_COOKIE, path="/")
    payload = decode_token(session_cookie) if session_cookie else None
    if payload and revoke(session, payload):
        event("выход", user=payload.get("sub"))
    return {"status": "ok"}


@router.get("/me", response_model=UserOut, summary="Текущий пользователь")
def me(user: User = Depends(current_user)) -> UserOut:
    return UserOut.model_validate(user)


@router.post("/me/password", response_model=UserOut, summary="Сменить свой пароль")
def change_my_password(
    payload: PasswordChangeIn,
    response: Response,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> UserOut:
    """Смена собственного пароля: прежний пароль обязателен.

    Прежний пароль спрашивается не для проверки памяти, а потому что сессия
    могла остаться открытой на чужой машине: без него уведённой куки хватало бы,
    чтобы забрать учётную запись насовсем.

    Отказ отвечает 409, а не 401: 401 интерфейс понимает как «сессия кончилась»
    и выбрасывает человека на экран входа вместе с формой, которую он заполнял.
    Сессия тут как раз в порядке — не подошёл введённый пароль.

    Все остальные сессии этого человека закрываются (``api/deps.py``), а взамен
    выдаётся новая кука — та, из которой пришёл запрос, остаётся рабочей.
    """
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=409, detail="Текущий пароль не подошёл")
    if verify_password(payload.new_password, user.password_hash):
        raise HTTPException(status_code=409, detail="Новый пароль совпадает с прежним")

    user.password_hash = hash_password(payload.new_password)
    user.password_changed_at = password_changed_now()
    session.commit()

    _set_session_cookie(response, create_token(user.id, user.role.value))
    return UserOut.model_validate(user)


@router.get("/users", response_model=list[UserOut], summary="Пользователи")
def list_users(
    _: User = Depends(require_admin), session: Session = Depends(get_session)
) -> list[UserOut]:
    rows = session.scalars(select(User).order_by(User.email)).all()
    return [UserOut.model_validate(row) for row in rows]


@router.post(
    "/users",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Создать пользователя",
)
def create_user(
    payload: UserCreateIn,
    _: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> UserOut:
    email = payload.email.strip().lower()
    if session.scalar(select(User).where(func.lower(User.email) == email)):
        raise HTTPException(status_code=409, detail="Пользователь с такой почтой уже есть")

    user = User(
        email=email,
        full_name=payload.full_name.strip(),
        role=payload.role,
        password_hash=hash_password(payload.password),
    )
    session.add(user)
    session.commit()
    return UserOut.model_validate(user)


@router.put("/users/{user_id}", response_model=UserOut, summary="Изменить пользователя")
def update_user(
    user_id: int,
    payload: UserUpdateIn,
    response: Response,
    actor: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> UserOut:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    # Защита от запирания самого себя: последний администратор должен остаться.
    losing_admin = user.role is Role.ADMIN and (
        (payload.role is not None and ROLE_ORDER[payload.role] < ROLE_ORDER[Role.ADMIN])
        or payload.is_active is False
    )
    if losing_admin:
        admins = session.scalar(
            select(func.count(User.id)).where(User.role == Role.ADMIN, User.is_active.is_(True))
        )
        if (admins or 0) <= 1:
            raise HTTPException(
                status_code=409,
                detail="Это последний активный администратор: сначала назначьте другого",
            )
    if user.id == actor.id and payload.is_active is False:
        raise HTTPException(status_code=409, detail="Нельзя отключить самого себя")
    # Понизить себя, пока есть другой администратор, разрешено: это не запирание,
    # а решение человека — вернуть роль сможет коллега. Запрета на сервере нет
    # намеренно, иначе проверка «последний администратор» стала бы недостижимой:
    # чужую роль администратор снимает только когда администраторов двое. Экран
    # на всякий случай переспрашивает.

    if payload.full_name is not None:
        user.full_name = payload.full_name.strip()
    if payload.role is not None:
        user.role = payload.role
    if payload.is_active is not None:
        user.is_active = payload.is_active
    if payload.password is not None:
        user.password_hash = hash_password(payload.password)
        # Новый пароль закрывает все открытые сессии этого человека: именно так
        # поступают, когда учётную запись увели. Себе администратор при этом
        # выдаёт новую куку, иначе вылетал бы на экран входа следующим запросом.
        user.password_changed_at = password_changed_now()
        if user.id == actor.id:
            _set_session_cookie(response, create_token(user.id, user.role.value))

    session.commit()
    return UserOut.model_validate(user)
