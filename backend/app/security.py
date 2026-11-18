"""Пароли и токены сессии.

Решения и почему так:

* **bcrypt поверх SHA-256.** Сам bcrypt молча обрезает пароль до 72 байт, то есть
  длинные парольные фразы теряли бы хвост, а в кириллице 72 байта — это всего
  36 символов. Поэтому пароль сначала сворачивается в SHA-256 и кодируется base64,
  и только результат уходит в bcrypt. Схема известная (``bcrypt_sha256``), длина
  входа перестаёт иметь значение.
* **Токен в httpOnly-куке, а не в localStorage.** Из localStorage токен достаётся
  любым исполненным на странице скриптом. Кука с ``httpOnly`` недоступна JS вовсе,
  а ``SameSite=Lax`` закрывает межсайтовые POST-запросы, так что отдельные
  CSRF-токены не нужны: интерфейс и API живут на одном источнике.
* **Секрет только из окружения.** В отладке есть значение по умолчанию, в проде
  приложение без ``SECRET_KEY`` не стартует: подписанный общеизвестным ключом токен
  подделывается кем угодно.
* **У каждого токена есть номер (``jti``).** Токен самодостаточен: сервер не держит
  список выданных сессий, а проверяет подпись. Без номера отозвать одну сессию
  нечем — выход мог бы только удалить куку в браузере, оставив токен рабочим
  до конца срока (задача G-20). Номер позволяет держать короткий список
  отозванных вместо длинного списка действующих.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from .config import get_settings

ALGORITHM = "HS256"
SESSION_COOKIE = "raketa_session"


def _prepare(password: str) -> bytes:
    """SHA-256 + base64: снимает ограничение bcrypt в 72 байта."""
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prepare(password), bcrypt.gensalt()).decode("ascii")


def password_changed_now() -> datetime:
    """Отметка «пароль сменили сейчас» — в UTC и без зоны, как хранит база.

    По ней сессии, выданные раньше, перестают приниматься (``api/deps.py``).
    Выставляют её все, кто меняет пароль: и сам человек, и администратор.
    """
    return datetime.now(UTC).replace(tzinfo=None)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_prepare(password), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        # Испорченный или пустой хеш — это неуспешная проверка, а не падение входа.
        return False


def new_session_id() -> str:
    """Номер сессии (``jti``) — случайный и неугадываемый.

    Нужен, чтобы выход мог отозвать **одну** сессию: без номера отозвать
    можно было бы только все сессии человека разом (задача G-20).
    """
    return secrets.token_urlsafe(32)


def create_token(user_id: int, role: str) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "role": role,
        "jti": new_session_id(),
        "iat": now,
        "exp": now + timedelta(hours=settings.session_ttl_hours),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_token(token: str) -> dict | None:
    """Разбор токена; ``None`` на любом некорректном или просроченном."""
    try:
        return jwt.decode(token, get_settings().secret_key, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None
