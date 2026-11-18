"""Отзыв сессии: что значит «выйти» (задача G-20).

Токен сессии подписан и самодостаточен — сервер проверяет подпись, а не ищет
запись в хранилище сессий. У этого есть цена: отозвать выданный токен нечем,
если о нём ничего не помнить. До этой службы выход удалял куку в браузере,
а токен оставался действительным до конца своих двенадцати часов — то есть
на общем компьютере «я вышел» означало «кука удалена здесь», а не «вход
закрыт». Скопированная кука переживала выход.

Решения и почему так:

* **Список отозванных, а не список действующих.** Второй — это полноценное
  хранилище сессий: строка на каждый вход, продление на каждый запрос.
  Первый — строка на выход, то есть на событие редкое, и в нём нет ничего,
  кроме номера токена.

* **Строка живёт до срока самого токена.** После ``exp`` токен отвергает
  подпись, и помнить о нём незачем. Срока хранения у этих строк поэтому нет
  и быть не может: держать дольше бессмысленно, удалять раньше — значит
  вернуть отозванный токен в работу. Чистит их ``app.cleanup`` без ключа
  и без вопросов, в отличие от журналов.

* **Отзывается одна сессия, та, из которой вышли.** Выход на чужом компьютере
  не должен закрывать вход на телефоне. «Закрыть все» — это смена пароля,
  она отзывает всё сразу по ``password_changed_at`` (``api/deps.py``).

* **Токен без номера не принимается вовсе.** Такие выдавались до этой задачи;
  отозвать их нечем, то есть для них выход так и остался бы бессильным.
  Цена — при обновлении все, кто был внутри, входят заново один раз.
  Молча оставить необратимые сессии на двенадцать часов хуже.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import RevokedSession


def _naive(moment: datetime) -> datetime:
    """UTC без зоны — как хранит база."""
    return moment.astimezone(UTC).replace(tzinfo=None)


def expiry_of(payload: dict) -> datetime | None:
    """Момент, до которого о токене нужно помнить."""
    exp = payload.get("exp")
    if not isinstance(exp, int | float):
        return None
    return _naive(datetime.fromtimestamp(exp, UTC))


def revoke(session: Session, payload: dict) -> bool:
    """Отзывает сессию токена. ``False``, если отзывать нечего.

    Нечего — это токен без номера или без срока: такие выдавались до задачи
    G-20, и хранить о них строку бесполезно, потому что сверять её не с чем.
    """
    jti = payload.get("jti")
    expires_at = expiry_of(payload)
    if not isinstance(jti, str) or not jti or expires_at is None:
        return False

    session.add(RevokedSession(jti=jti, expires_at=expires_at))
    try:
        session.commit()
    except IntegrityError:
        # Второй выход по той же куке — уже отозвано, и это не ошибка.
        session.rollback()
    return True


def is_revoked(session: Session, jti: str) -> bool:
    return session.get(RevokedSession, jti) is not None


def forget_expired(session: Session, now: datetime | None = None) -> int:
    """Убирает строки, чей токен и так уже просрочен. Возвращает число строк."""
    moment = now or datetime.now(UTC).replace(tzinfo=None)
    stale = session.scalars(
        select(RevokedSession.jti).where(RevokedSession.expires_at < moment)
    ).all()
    if not stale:
        return 0
    session.execute(delete(RevokedSession).where(RevokedSession.jti.in_(stale)))
    session.commit()
    return len(stale)
