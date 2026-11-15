"""Ограничение частоты попыток входа (задачи G-8, G-12).

Без него подбор пароля замедляет только стоимость bcrypt — это сотни попыток
в минуту с одной машины. Счётчик неудачных попыток закрывает вход на окно
времени и делает перебор бессмысленно медленным.

Решения и почему так:

* **Счётчик живёт в базе.** Раньше он лежал в памяти процесса, и в проде это
  была дыра, а не защита: воркеров несколько, у каждого своя доля лимита,
  а перезапуск обнулял счётчик целиком — перебор достаточно было переждать.
  База у всех воркеров одна. Отдельное хранилище (Redis) ради одного счётчика
  означало бы ещё один сервис в развёртывании; попытки входа редки, и один
  запрос на попытку дешевле этого.

* **Ключ — пара (почта, адрес), а не почта.** Счётчик по одной почте
  превращается в способ заблокировать коллегу: достаточно слать за него
  неверные пароли. С адресом в ключе чужие попытки не мешают человеку войти
  со своего места.

* **Отдельный счётчик по адресу** — на случай перебора не пароля, а почты:
  по одной попытке на сотню учётных записей не набрала бы лимит ни одна пара.
  Порог у него намеренно выше: за одним адресом может сидеть целый офис.

* **Удачный вход очищает счётчик пары,** иначе человек, вспомнивший пароль
  с пятой попытки, остался бы заперт на четверть часа.

* **Адрес берётся из соединения, а за прокси — из заголовка, и только если
  так настроено.** Доверять ``X-Forwarded-For`` без настройки нельзя: его
  подставляет кто угодно, и счётчик по адресу обходился бы одной строкой
  в запросе. ``TRUSTED_PROXY_HOPS`` говорит, сколько прокси стоит перед
  приложением; адрес берётся ровно с той позиции, которую заполнил ближайший
  из них, а приписанное клиентом остаётся слева и не читается.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from fastapi import Request
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import SessionLocal
from .models import LoginAttempt

#: Заголовок, в котором прокси передаёт адрес клиента.
FORWARDED_HEADER = "X-Forwarded-For"

#: Если адреса нет вовсе (тесты, внутренние вызовы) — пусть будет явная метка,
#: а не пустая строка: по ней видно, что адрес не известен, а не «нулевой».
UNKNOWN_ADDRESS = "-"


def _now() -> datetime:
    """UTC без зоны — как хранит база."""
    return datetime.now(UTC).replace(tzinfo=None)


def client_address(request: Request, hops: int | None = None) -> str:
    """Адрес клиента с учётом доверенных прокси.

    ``hops`` — сколько прокси стоит перед приложением. Ноль (по умолчанию)
    означает «приложение видит клиента напрямую», и заголовок не читается
    вовсе: иначе любой клиент назвал бы себя чужим адресом и обошёл счётчик.
    """
    depth = get_settings().trusted_proxy_hops if hops is None else hops
    direct = request.client.host if request.client else UNKNOWN_ADDRESS
    if depth <= 0:
        return direct

    chain = [part.strip() for part in request.headers.get(FORWARDED_HEADER, "").split(",")]
    chain = [part for part in chain if part]
    if len(chain) < depth:
        # Заголовка нет или он короче, чем должен быть: запрос пришёл не через
        # ожидаемую цепочку прокси, и верить ему нельзя.
        return direct
    return chain[-depth]


class LoginGuard:
    """Скользящее окно неудачных попыток входа, общее для всех воркеров."""

    def __init__(
        self,
        per_account: int,
        per_address: int,
        window_seconds: int,
        sessions: Callable[[], Session] = SessionLocal,
    ) -> None:
        self.per_account = per_account
        self.per_address = per_address
        self.window_seconds = window_seconds
        self.sessions = sessions

    def _cutoff(self, now: datetime) -> datetime:
        return now - timedelta(seconds=self.window_seconds)

    def _wait(self, session: Session, where, limit: int, now: datetime) -> int:
        """Сколько секунд ждать по одному условию; 0 — можно пробовать."""
        cutoff = self._cutoff(now)
        count = (
            session.scalar(
                select(func.count())
                .select_from(LoginAttempt)
                .where(LoginAttempt.happened_at > cutoff, *where)
            )
            or 0
        )
        if count < limit:
            return 0
        oldest = session.scalar(
            select(func.min(LoginAttempt.happened_at)).where(
                LoginAttempt.happened_at > cutoff, *where
            )
        )
        if oldest is None:
            return 0
        # Ждать нужно до того момента, когда из окна выпадет самая старая попытка.
        return max(1, int(self.window_seconds - (now - oldest).total_seconds() + 1))

    def retry_after(self, email: str, address: str, now: datetime | None = None) -> int:
        """Сколько секунд осталось до следующей разрешённой попытки."""
        moment = now or _now()
        key = email.strip().lower()
        with self.sessions() as session:
            return max(
                self._wait(
                    session,
                    (LoginAttempt.email == key, LoginAttempt.address == address),
                    self.per_account,
                    moment,
                ),
                self._wait(session, (LoginAttempt.address == address,), self.per_address, moment),
            )

    def failure(self, email: str, address: str, now: datetime | None = None) -> None:
        moment = now or _now()
        with self.sessions() as session:
            session.add(
                LoginAttempt(email=email.strip().lower(), address=address, happened_at=moment)
            )
            # Попытки старше окна уже ни на что не влияют: это счётчик,
            # а не журнал, и расти бесконечно он не должен.
            session.execute(
                delete(LoginAttempt).where(LoginAttempt.happened_at <= self._cutoff(moment))
            )
            session.commit()

    def success(self, email: str, address: str) -> None:
        """Удачный вход снимает счётчик пары, но не общий счётчик адреса."""
        with self.sessions() as session:
            session.execute(
                delete(LoginAttempt).where(
                    LoginAttempt.email == email.strip().lower(),
                    LoginAttempt.address == address,
                )
            )
            session.commit()

    def reset(self) -> None:
        """Полная очистка — нужна тестам, чтобы они не мешали друг другу."""
        with self.sessions() as session:
            session.execute(delete(LoginAttempt))
            session.commit()


@lru_cache
def get_login_guard() -> LoginGuard:
    settings = get_settings()
    return LoginGuard(
        per_account=settings.login_max_attempts,
        per_address=settings.login_max_attempts_per_address,
        window_seconds=settings.login_window_minutes * 60,
    )
