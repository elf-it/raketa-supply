"""Вход, выход и управление пользователями."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_session
from ..models import ROLE_ORDER, Role, User
from ..schemas import LoginIn, UserCreateIn, UserOut, UserUpdateIn
from ..security import SESSION_COOKIE, create_token, hash_password, verify_password
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


@router.post("/login", response_model=UserOut, summary="Вход")
def login(payload: LoginIn, response: Response, session: Session = Depends(get_session)) -> UserOut:
    """Выдаёт сессионную куку.

    Ответ одинаков и для несуществующей почты, и для неверного пароля,
    и для отключённого пользователя: иначе форма входа превращается
    в способ узнать, кто заведён в системе.
    """
    user = session.scalar(
        select(User).where(func.lower(User.email) == payload.email.strip().lower())
    )
    if user is None or not user.is_active:
        # Пароль всё равно проверяем, чтобы ответ не был заметно быстрее.
        verify_password(payload.password, "$2b$12$" + "." * 53)
        raise BAD_CREDENTIALS
    if not verify_password(payload.password, user.password_hash):
        raise BAD_CREDENTIALS

    _set_session_cookie(response, create_token(user.id, user.role.value))
    return UserOut.model_validate(user)


@router.post("/logout", summary="Выход")
def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "ok"}


@router.get("/me", response_model=UserOut, summary="Текущий пользователь")
def me(user: User = Depends(current_user)) -> UserOut:
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

    session.commit()
    return UserOut.model_validate(user)
